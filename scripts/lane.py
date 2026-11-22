"""A person's commands for one card's lane state — instead of editing `<state>/attempts/` by hand.

    .venv/bin/python scripts/lane.py status 794                     # idle runs, rework rounds, parked and why
    .venv/bin/python scripts/lane.py park 794 --reason "waits on the API decision"
    .venv/bin/python scripts/lane.py unpark 794 --reason "decided in #820"   # unparked, both counts cleared
    .venv/bin/python scripts/lane.py rereview 820                   # review again at its head, under today's brief
    .venv/bin/python scripts/lane.py rereview --all-open            # every open worker pull request, in turn
    .venv/bin/python scripts/lane.py lunate-pin 16f9e25e9           # publish that main commit for the Lunate harness
    .venv/bin/python scripts/lane.py release 0.5.0                  # release it: one deterministic sequence (#21)
    .venv/bin/python scripts/lane.py release --status               # where the open release is, what it waits on
    .venv/bin/python scripts/lane.py release --fix-pr 1012          # tag the open release on a merged release fix

Park and unpark take the feeder's lock, so they never interleave with a tick (a busy lock is a refusal: try again
in a minute). Unpark refuses while a worker runs the card. Both need a --reason, and both write a line to
`<state>/logs/lane.log` with the time, who ran it (the login, `$LANE_ACTOR` if set, the parent process, and
whether a terminal was attached) and the reason. Only rereview touches GitHub: it takes the review job's lock,
runs a review session on a ready, green worker pull request even if the bot reviewed its head already, dismisses
the bot's standing review there ("superseded by re-review under the calibrated brief") and posts the new one.
"""

from __future__ import annotations

import argparse
import getpass
import os
import pathlib
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import common, github, harness, lunate_pin, parked, release, review  # noqa: E402
from scripts.common import LaneError, LockBusy  # noqa: E402

JOB = "lane"
FEED = "feed"
REVIEW = "review"
FINDINGS = 8  # blocking findings shown per pull request


class RefusedError(LaneError):
    """The command was refused; nothing changed."""


def actor() -> str:
    """Who ran this command, as far as the process can tell: `$LANE_ACTOR` (a name a person or agent sets), the
    login, the parent process (a shell, cron, an agent's runner) and whether a terminal was attached."""
    try:
        login = getpass.getuser()
    except (KeyError, OSError):
        login = f"uid {os.getuid()}"
    try:
        parent = subprocess.run(["ps", "-o", "command=", "-p", str(os.getppid())], capture_output=True, text=True,
                                timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        parent = ""
    parent = (parent.split()[0].rsplit("/", 1)[-1] if parent else "unknown")[:40]
    named = os.environ.get("LANE_ACTOR", "").strip()[:60]
    tty = "terminal" if sys.stdin.isatty() else "no terminal"
    return (f"{named} as " if named else "") + f"{login} via {parent}, {tty}"


def status(card: int) -> str:
    data = harness.attempts(card)
    lines = [f"card #{card}"]
    live = harness.live_pid(card)
    lines.append(f"  worker: running (pid {live})" if live else "  worker: none running")
    lines.append(f"  idle runs: {data['idle_runs']} of {harness.IDLE_RUN_LIMIT}")
    rounds = ", ".join(f"#{pr}: {n} of {harness.REWORK_LIMIT}" for pr, n in sorted(data["reworks"].items()))
    lines.append(f"  rework rounds: {rounds or 'none'}")
    fixes = ", ".join(f"#{pr}: {n} of {harness.FIX_LIMIT}" for pr, n in sorted(data["fixes"].items()))
    lines.append(f"  fix reworks (conflict, red check, orphaned draft): {fixes or 'none'}")
    if data["parked"]:
        lines.append(f"  parked: yes — {data['reason'] or 'idle'}" + (f": {data['detail']}" if data["detail"] else ""))
    else:
        at_cap = [(pr, "rework round") for pr, n in sorted(data["reworks"].items()) if n >= harness.REWORK_LIMIT]
        at_cap += [(pr, "fix rework") for pr, n in sorted(data["fixes"].items()) if n >= harness.FIX_LIMIT]
        if at_cap:  # the cap counts a round when it starts: the last one runs, the next one due parks instead
            lines.append(f"  parked: no — #{at_cap[0][0]} is at the cap: its last {at_cap[0][1]} "
                         + ("is running" if live else "has run")
                         + "; the next tick that finds it due for another parks the card instead")
        else:
            lines.append("  parked: no")
    if data["died"]:
        lines.append(f"  last run: worker died: {data['died']}")
    return "\n".join(lines)


def park(card: int, reason: str) -> str:
    if not reason.strip():
        raise RefusedError("a park needs a --reason a person can read in the digest")
    with common.job_lock(FEED):
        if not harness.park(card, "manual", reason.strip()):
            data = harness.attempts(card)
            raise RefusedError(f"card #{card} is already parked ({data['reason'] or 'idle'}"
                               + (f": {data['detail']}" if data["detail"] else "") + ")")
    common.log(JOB, f"parked card #{card} by hand — by {actor()} — reason: {reason.strip()}")
    failed = parked.flag(common.config(), card, job=JOB)
    return f"card #{card} parked: {reason.strip()}" + flags_line(failed)


def flags_line(failed: list) -> str:
    cfg = common.config()
    if failed:
        return f"\n  FAILED to set or clear {cfg.parked_label} / @{cfg.parked_assignee}: " + "; ".join(failed)
    return ""


def unpark(card: int, reason: str) -> str:
    if not reason.strip():
        raise RefusedError("an unpark needs a --reason, for the log")
    with common.job_lock(FEED):
        live = harness.live_pid(card)
        if live is not None:
            raise RefusedError(f"a worker (pid {live}) is running card #{card} — unpark it once that run has ended")
        before = harness.attempts(card)
        if not harness.unpark(card):
            return f"card #{card} has no attempts record — it was not parked"
    was = f"parked ({before['reason'] or 'idle'})" if before["parked"] else "not parked"
    common.log(JOB, f"unparked card #{card} by hand (was {was}) — by {actor()} — reason: {reason.strip()}; "
                    "idle runs and rework rounds cleared")
    failed = parked.unflag(common.config(), card, job=JOB) if card in parked.flagged() or before["parked"] else []
    return f"card #{card} unparked (was {was}); idle runs and rework rounds cleared" + flags_line(failed)


def blocking(body: str) -> list[str]:
    """The lines of a review's **Blocking** section (else its numbered or bulleted findings), one line each."""
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if "blocking" in line.lower() and "non-blocking" not in line.lower()
                  and line.lstrip().startswith(("#", "*"))), None)
    section = lines[start + 1:] if start is not None else lines
    found = []
    for line in section:
        bare = line.strip()
        if start is not None and bare.lstrip("#* ").lower().startswith(("non-blocking", "non blocking")):
            break
        if not bare:
            continue
        if bare[0] in "-*" or (bare[:1].isdigit() and bare[1:3].strip(" ").startswith((".", ")"))):
            found.append(" ".join(bare.split())[:200])
    return found[:FINDINGS]


FAILED = ": FAILED — "


def rereview(numbers: list[int], all_open: bool) -> str:
    cfg = common.config()
    harness.exit_on_signals()  # a killed run still kills its session and removes its worktree
    who = actor()
    out = []
    with common.job_lock(REVIEW):
        if all_open:
            numbers = sorted(p.number for p in github.open_pulls(cfg) if p.author == cfg.worker_app)
        common.log(JOB, f"rereview of {', '.join(f'#{n}' for n in numbers) or 'nothing'} — by {who}")
        for number in numbers:
            try:
                event, body = review.rereview(cfg, number, who)
            except LaneError as exc:
                out.append(f"#{number}{FAILED}{exc}")
                continue
            if event == "SKIPPED":
                out.append(f"#{number}: not re-reviewed — {body}")
            elif event == "APPROVE":
                out.append(f"#{number}: APPROVE")
            else:
                out.append(f"#{number}: {event}")
                out += [f"  {line}" for line in blocking(body)]
    return "\n".join(out) or "no open worker pull requests"


def confirm_train(version: str, why: str) -> bool:
    """A real `release --train` without --yes: from a terminal, the version typed back; with no terminal, refused —
    an agent's shell has none, and that is how 0.6.0 was cut by accident on 2026-11-22. Only the cron line passes
    --yes."""
    if not sys.stdin.isatty():
        print(f"refused — `lane.py release --train` would cut {version} ({why}). Unattended, it runs only with --yes "
              "(the cron line); `--train --dry-run` shows what it would cut", file=sys.stderr)
        return False
    try:
        answer = input(f"Cut release {version} now ({why})? Type {version} to confirm: ")
    except EOFError:
        answer = ""
    if answer.strip() != version:
        print(f"not cut — {answer.strip()!r} is not {version}")
        return False
    return True


def ship_release(args) -> int:
    """`lane.py release`: record the intent (or read it), then advance it under the release lock. Exits 1 when it is
    blocked on something a person must fix, and says what."""
    if args.status:
        print(release.status())
        return 0
    who = actor()
    if args.abandon:
        if not args.abandon.strip():
            raise LaneError("--abandon needs a reason")
        common.log(JOB, f"release abandoned — by {who} — reason: {args.abandon}")
        print(release.abandon(args.abandon))
        return 0
    if args.fix_pr is not None:
        with common.job_lock(release.JOB):
            data = release.fix(args.fix_pr)
        common.log(JOB, f"release {data['version']} — tagged on the merge of #{args.fix_pr} — by {who}")
        print(f"release {data['version']}: the tag goes on the merge of #{args.fix_pr}; the next advance reads it")
        return 0
    cfg = common.config()
    harness.exit_on_signals()
    if args.train:
        version, why = release.train_version(cfg)
        if version is None:
            print(f"no release train — {why}")
            return 0
        if args.dry_run:
            print(f"would cut release {version} — the weekly train ({why}); nothing was changed")
            return 0
        if not args.yes and not confirm_train(version, why):
            return 1
        args.version, who = version, f"the weekly train ({why}), {who}"
    with common.job_lock(release.JOB):
        data = release.start(args.version, who)
        if args.resmoke:
            data.pop("smoke_dispatched", None)
            release._write(data)
        common.log(JOB, f"release {data['version']} — by {who}")
        try:
            print(release.advance(cfg))
        except release.Blocked as exc:
            print(f"release {data['version']}{FAILED}{exc}")
            return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lane.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").add_argument("card", type=int)
    for name in ("park", "unpark"):
        command = sub.add_parser(name)
        command.add_argument("card", type=int)
        command.add_argument("--reason", required=True)
    again = sub.add_parser("rereview")
    again.add_argument("pr", type=int, nargs="*")
    again.add_argument("--all-open", action="store_true")
    sub.add_parser("lunate-pin", help="publish a main commit of lunate in the sandbox and pin it in lane.json"
                   ).add_argument("commit")
    ship = sub.add_parser("release", help="release a version: preflight, release PR, the maintainer's approval, "
                                          "merge, tag, cut (open cards move forward), publish, channels, mirror, smoke, close")
    ship.add_argument("version", nargs="?")
    ship.add_argument("--status", action="store_true")
    ship.add_argument("--train", action="store_true", help="the weekly train: release the feeder's milestone when a "
                                                           "changelog fragment merged since the last tag (cron)")
    ship.add_argument("--dry-run", action="store_true", help="with --train: print what it would cut, change nothing")
    ship.add_argument("--yes", action="store_true", help="with --train: cut without asking (the cron line only)")
    ship.add_argument("--resmoke", action="store_true", help="dispatch the smoke again on the next advance")
    ship.add_argument("--abandon", metavar="REASON", help="drop the open release record (nothing on GitHub is undone)")
    ship.add_argument("--fix-pr", type=int, metavar="N", help="tag the open release on the merge of N, a "
                                                              "chore(release) pull request that fixes it after its merge; "
                                                              "record it once N is open: fragments are held until it merges")
    args = parser.parse_args(argv)
    if args.command == "release" and not (args.version or args.status or args.abandon or args.train
                                          or args.fix_pr is not None):
        parser.error("release takes a version, --train, --status, --fix-pr N or --abandon REASON")
    if args.command == "release" and args.fix_pr is not None and (args.version or args.train or args.abandon):
        parser.error("--fix-pr N goes alone: it fixes the release that is open")
    if args.command == "release" and args.version and args.train:
        parser.error("release takes a version or --train, not both")
    if args.command == "release" and (args.dry_run or args.yes) and not args.train:
        parser.error("--dry-run and --yes go with --train")
    if args.command == "release" and args.dry_run and args.yes:
        parser.error("--dry-run and --yes are opposites; pass one")
    if args.command == "rereview" and bool(args.pr) == args.all_open:
        parser.error("rereview takes pull request numbers or --all-open, not both or neither")
    try:
        if args.command == "status":
            print(status(args.card))
        elif args.command == "rereview":
            text = rereview(args.pr, args.all_open)
            print(text)
            if FAILED in text:  # a review that could not run is a failure, by name, not a quiet exit 0 (#59)
                return 1
        elif args.command == "lunate-pin":
            done = lunate_pin.pin(common.config(), args.commit)
            print(f"pinned {done['version']} ({done['commit'][:12]}) at {done['binary']}, sha256 {done['sha256']}\n"
                  f"lane.json at {common.CONFIG_PATH} is updated — land it through a lane pull request")
        elif args.command == "release":
            return ship_release(args)
        elif args.command == "park":
            print(park(args.card, args.reason))
        else:
            print(unpark(args.card, args.reason))
    except LockBusy as exc:
        job = {"rereview": "a review run", "release": "a release run"}.get(args.command, "a feeder tick")
        print(f"refused — {job} is running ({exc}); try again in a minute", file=sys.stderr)
        return 1
    except LaneError as exc:
        print(f"refused — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
