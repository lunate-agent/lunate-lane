"""Cron job: start workers — reworks first, then the next cards of the milestone while there is room.

    .venv/bin/python scripts/feed.py            # one tick
    .venv/bin/python scripts/feed.py --dry-run  # say what would start, start nothing

Reads GitHub (the milestone's open issues, the open pull requests, the worker identity's pull requests in full)
and the running-worker records, asks `cards.plan` what to start, and starts each as a detached `worker.py`. Any
read that fails stops the tick with a named error: an unreadable count is never read as room.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import cards, common, github, harness  # noqa: E402
from scripts.common import LaneError  # noqa: E402

JOB = "feed"


def rules(cfg: common.Config) -> cards.Rules:
    return cards.Rules(cfg.wip_limit, cfg.worker_app, cfg.reviewer_app, cfg.bug_label, cfg.area_prefix)


def decide(cfg: common.Config) -> tuple[list[cards.Launch], list[str]]:
    """Read everything the decision needs, then decide. Every read raises on failure."""
    milestone = github.milestone_cards(cfg)
    pulls = github.open_pulls(cfg)
    worker_pulls = [github.pull(cfg, p.number) for p in pulls if p.author == cfg.worker_app]
    running = harness.running_workers()
    pins = cards.load_pins(cfg.pins)
    return cards.plan(cards=milestone, open_pulls=pulls, worker_pulls=worker_pulls, running=running,
                      rules=rules(cfg), pins=pins)


def start(cfg: common.Config, launch: cards.Launch) -> int:
    """Start `worker.py` detached for one launch, and record it as running before returning."""
    argv = [sys.executable, str(common.ROOT / "scripts" / "worker.py"), "--issue", str(launch.card)]
    if launch.rework_pr is not None:
        argv += ["--rework", str(launch.rework_pr)]
    output = common.state_dir("logs") / f"worker-{launch.card}.out"
    try:
        with output.open("a") as sink:
            proc = subprocess.Popen(argv, cwd=str(common.ROOT), stdin=subprocess.DEVNULL, stdout=sink,
                                    stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        raise LaneError(f"the worker for #{launch.card} could not be started ({exc.__class__.__name__}: {exc})") from exc
    harness.record_worker(launch.card, proc.pid, launch.areas, launch.rework_pr)
    return proc.pid


def tick(dry_run: bool = False) -> None:
    cfg = common.config()
    launches, notes = decide(cfg)
    for note in notes:
        common.log(JOB, note)
    if not launches:
        common.log(JOB, "nothing to start")
    for launch in launches:
        what = f"rework of #{launch.rework_pr} for card #{launch.card}" if launch.rework_pr else f"card #{launch.card}"
        if dry_run:
            common.log(JOB, f"would start {what}")
            continue
        pid = start(cfg, launch)
        common.log(JOB, f"started {what} (pid {pid})")


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    return common.run_job(JOB, lambda: tick(dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
