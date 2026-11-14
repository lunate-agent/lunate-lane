"""The reviewer's one way to hand in its verdict: run once, at the end of the review, from inside its session.

    python -B scripts/verdict.py --out <scratch>/verdict.json --body-file <scratch>/review.md --event APPROVE

It reads the review body (Markdown, written by the reviewer to `--body-file`), checks the event, and writes the
verdict JSON itself — so the reviewer never hand-writes JSON (#844, 2026-11-14 03:29: one stray `}` and the
verdict could not be read). The write is atomic and happens once: a temporary file in the same directory, then
published under `--out` with a no-clobber link, so a reader sees no file or the whole verdict, never a partial
or doubled one. A second run finds the verdict there, leaves it as it is, records the attempt in
`<out>.refused` (the review job logs each one) and exits 1.

Standard library only: it runs inside the reviewer's sandbox, which may write nothing but its scratch directory.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys
import tempfile

EVENTS = ("APPROVE", "REQUEST_CHANGES")
WRITTEN_BY = "scripts/verdict.py"


class VerdictError(Exception):
    pass


def refused_path(out: pathlib.Path) -> pathlib.Path:
    return out.with_name(out.name + ".refused")


def write(out: pathlib.Path, event: str, body: str) -> None:
    """Publish the verdict at `out` once, atomically. Raises VerdictError when one is there already (the attempt
    is recorded in `<out>.refused`) or the event or body is not usable."""
    if event not in EVENTS:
        raise VerdictError(f"the event must be one of {', '.join(EVENTS)}, not {event!r}")
    if not body.strip():
        raise VerdictError("the review body is empty")
    data = json.dumps({"event": event, "body": body.strip(), "written_by": WRITTEN_BY}, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=".verdict-", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, out)  # the rename that never replaces: fails if a verdict is there already
        except FileExistsError:
            when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(refused_path(out), "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": when, "event": event}) + "\n")
            raise VerdictError(f"a verdict was written to {out} already — this second one is refused, the first "
                               "stands") from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hand in the review verdict, once.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--event", required=True, choices=EVENTS)
    args = parser.parse_args(argv)
    try:
        body = pathlib.Path(args.body_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"verdict: the review body {args.body_file} could not be read: {exc}", file=sys.stderr)
        return 1
    try:
        write(pathlib.Path(args.out), args.event, body)
    except (VerdictError, OSError) as exc:
        print(f"verdict: {exc}", file=sys.stderr)
        return 1
    print(f"verdict: {args.event} written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
