"""A person's commands for one card's lane state — instead of editing `<state>/attempts/` by hand.

    .venv/bin/python scripts/lane.py status 794                     # idle runs, rework rounds, parked and why
    .venv/bin/python scripts/lane.py park 794 --reason "waits on the API decision"
    .venv/bin/python scripts/lane.py unpark 794 --reason "decided in #820"   # unparked, both counts cleared
    .venv/bin/python scripts/lane.py rereview 820                   # review again at its head, under today's brief
    .venv/bin/python scripts/lane.py rereview --all-open            # every open worker pull request, in turn

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

from scripts import common, github, harness, parked, review  # noqa: E402
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
    if data["parked"]:
        lines.append(f"  parked: yes — {data['reason'] or 'idle'}" + (f": {data['detail']}" if data["detail"] else ""))
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
                out.append(f"#{number}: FAILED — {exc}")
                continue
            if event == "SKIPPED":
                out.append(f"#{number}: not re-reviewed — {body}")
            elif event == "APPROVE":
                out.append(f"#{number}: APPROVE")
            else:
                out.append(f"#{number}: {event}")
                out += [f"  {line}" for line in blocking(body)]
    return "\n".join(out) or "no open worker pull requests"


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
    args = parser.parse_args(argv)
    if args.command == "rereview" and bool(args.pr) == args.all_open:
        parser.error("rereview takes pull request numbers or --all-open, not both or neither")
    try:
        if args.command == "status":
            print(status(args.card))
        elif args.command == "rereview":
            print(rereview(args.pr, args.all_open))
        elif args.command == "park":
            print(park(args.card, args.reason))
        else:
            print(unpark(args.card, args.reason))
    except LockBusy as exc:
        job = "a review run" if args.command == "rereview" else "a feeder tick"
        print(f"refused — {job} is running ({exc}); try again in a minute", file=sys.stderr)
        return 1
    except LaneError as exc:
        print(f"refused — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
