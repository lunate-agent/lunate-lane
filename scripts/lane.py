"""A person's commands for one card's lane state — instead of editing `<state>/attempts/` by hand.

    .venv/bin/python scripts/lane.py status 794                     # idle runs, rework rounds, parked and why
    .venv/bin/python scripts/lane.py park 794 --reason "waits on the API decision"
    .venv/bin/python scripts/lane.py unpark 794                     # unparked, both counts cleared

Park and unpark take the feeder's lock, so they never interleave with a tick (a busy lock is a refusal: try again
in a minute). Unpark refuses while a worker runs the card. Nothing here touches GitHub.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import common, harness  # noqa: E402
from scripts.common import LaneError, LockBusy  # noqa: E402

JOB = "lane"
FEED = "feed"


class RefusedError(LaneError):
    """The command was refused; nothing changed."""


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
    return "\n".join(lines)


def park(card: int, reason: str) -> str:
    if not reason.strip():
        raise RefusedError("a park needs a --reason a person can read in the digest")
    with common.job_lock(FEED):
        if not harness.park(card, "manual", reason.strip()):
            data = harness.attempts(card)
            raise RefusedError(f"card #{card} is already parked ({data['reason'] or 'idle'}"
                               + (f": {data['detail']}" if data["detail"] else "") + ")")
    common.log(JOB, f"parked card #{card} by hand: {reason.strip()}")
    return f"card #{card} parked: {reason.strip()}"


def unpark(card: int) -> str:
    with common.job_lock(FEED):
        live = harness.live_pid(card)
        if live is not None:
            raise RefusedError(f"a worker (pid {live}) is running card #{card} — unpark it once that run has ended")
        before = harness.attempts(card)
        if not harness.unpark(card):
            return f"card #{card} has no attempts record — it was not parked"
    was = f"parked ({before['reason'] or 'idle'})" if before["parked"] else "not parked"
    common.log(JOB, f"unparked card #{card} by hand (was {was}); idle runs and rework rounds cleared")
    return f"card #{card} unparked (was {was}); idle runs and rework rounds cleared"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lane.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "unpark"):
        sub.add_parser(name).add_argument("card", type=int)
    parking = sub.add_parser("park")
    parking.add_argument("card", type=int)
    parking.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(status(args.card))
        elif args.command == "park":
            print(park(args.card, args.reason))
        else:
            print(unpark(args.card))
    except LockBusy as exc:
        print(f"refused — a feeder tick is running ({exc}); try again in a minute", file=sys.stderr)
        return 1
    except LaneError as exc:
        print(f"refused — {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
