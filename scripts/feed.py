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

from scripts import cards, cleanup, common, github, harness, open_pr, worker  # noqa: E402
from scripts.common import LaneError  # noqa: E402

JOB = "feed"


def rules(cfg: common.Config) -> cards.Rules:
    return cards.Rules(cfg.wip_limit, cfg.worker_app, cfg.reviewer_app, cfg.bug_label, cfg.area_prefix,
                       cfg.decision_label)


def decide(cfg: common.Config) -> tuple[list[cards.Launch], list[str]]:
    """Read everything the decision needs, then decide. Every read raises on failure."""
    launches, notes, *_ = decide_all(cfg)
    return launches, notes


def decide_all(cfg: common.Config) -> tuple[list[cards.Launch], list[str], list[github.Pull], list[github.Pull],
                                            list[github.Pull]]:
    """The launches and notes; the worker pull requests whose required checks failed for infra reasons and have
    not been rerun on their head yet (rerun once, not reworked); those to re-arm; and armed ones whose card waits on
    an open issue (disarmed: a slice started before its predecessor must not land first)."""
    # the running workers first: a worker that finishes between the two reads leaves its pull request ready, and
    # a draft read before that would look orphaned
    running = harness.running_workers()
    milestone = github.milestone_cards(cfg)
    pulls = github.open_pulls(cfg)
    worker_pulls = [github.pull(cfg, p.number) for p in pulls if p.author == cfg.worker_app]
    pins = cards.load_pins(cfg.pins)
    required = github.required_checks(cfg)
    reran, parked = harness.reran(), harness.parked_cards()
    launches, notes = cards.plan(cards=milestone, open_pulls=pulls, worker_pulls=worker_pulls, running=running,
                                 rules=rules(cfg), pins=pins, parked=parked, required=required, reran=reran)
    rerun = [p for p in worker_pulls if p.state == "OPEN" and not p.draft and p.card not in running
             and p.card not in parked and (p.number, p.head) not in reran
             and github.failure_kind(p, required) == "infra"]
    rearm = [p for p in worker_pulls if p.card not in running and p.card not in parked
             and github.needs_rearm(p, required, cfg.reviewer_app)]
    disarm = [p for p in worker_pulls if p.state == "OPEN" and p.auto_merge and p.waits_on]
    return launches, notes, rerun, rearm, disarm


def rerun(cfg: common.Config, p: github.Pull, dry_run: bool) -> None:
    """Rerun the failed jobs of an infra failure, once per head, and record it before anything else can fail.

    Only a workflow run whose event payload had draft=false (the ready run) is rerun. A draft run's aggregate
    check fails on purpose ("the head is a draft"): rerunning it reports a new red `ci-ok` that is newer than the
    ready run's green one and overrides it (#815). A run whose flag cannot be read is not rerun either. With
    nothing left to rerun, the head is still recorded, so a second tick sends it back as a rework, which pushes a
    new head with fresh runs."""
    runs = github.infra_runs(p)
    what = f"#{p.number}'s required checks were cancelled or timed out at {p.head[:8]}"
    try:
        drafts = {run_id: github.run_draft(cfg, run_id) for run_id in runs}
    except LaneError as exc:
        common.log(JOB, f"did not rerun #{p.number}: could not read whether its run(s) were draft runs ({exc}); "
                        "next tick tries again")
        return
    ready = tuple(run_id for run_id in runs if drafts[run_id] is False)
    held = [f"{run_id} ({'draft=true' if drafts[run_id] else 'draft flag unread'})"
            for run_id in runs if drafts[run_id] is not False]
    if held:
        common.log(JOB, f"{'would not rerun' if dry_run else 'not rerunning'} run(s) {', '.join(held)} of #{p.number}: "
                        "only a run whose event payload had draft=false is rerun — a draft run's rerun reports a "
                        "new red ci-ok over the ready run's")
    if dry_run:
        if ready:
            common.log(JOB, f"would rerun the failed jobs of run(s) {', '.join(map(str, ready))} — {what}")
        return
    harness.note_rerun(p.number, p.head, ready)
    for run_id in ready:
        common.gh("run", "rerun", str(run_id), "--failed", "-R", cfg.slug)
    if ready:
        common.log(JOB, f"reran the failed jobs of run(s) {', '.join(map(str, ready))} — {what}; a second such "
                        "failure on this head is a rework")
    else:
        common.log(JOB, f"nothing rerun for #{p.number} — {what}, in no ready run; the next tick sends it back as "
                        "a rework")


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
    if not dry_run:
        swept = open_pr.sweep_secret_dir()
        if swept:
            common.log(JOB, f"removed {swept} token file(s) a killed push left behind")
    if not dry_run:
        for card in harness.lifted_by_hand():
            common.log(JOB, f"card #{card} is no longer parked, and not by `lane.py unpark`: its attempts record was "
                            "edited or removed by hand")
    launches, notes, reruns, rearms, disarms = decide_all(cfg)
    for p in reruns:
        rerun(cfg, p, dry_run)
    # the launches first, in the order they were chosen, then the notes on why nothing else was (#3)
    for launch in launches:
        what = (f"rework of #{launch.rework_pr} for card #{launch.card} ({launch.reason})" if launch.rework_pr
                else f"card #{launch.card}")
        if launch.person is not None:  # the reviewer said a person must decide: parked at once, never reworked
            if dry_run:
                common.log(JOB, f"would park card #{launch.card} — the review of #{launch.rework_pr} says NEEDS-PERSON: "
                                f"{launch.person}")
                continue
            if harness.park(launch.card, "person", launch.person):
                worker.park(cfg, launch.card, launch.rework_pr, "person",
                            detail=f"the review of #{launch.rework_pr} says it needs a person: {launch.person}")
            common.log(JOB, f"parked card #{launch.card} — the review of #{launch.rework_pr} says NEEDS-PERSON: "
                            f"{launch.person}")
            continue
        rounds = harness.rework_rounds(launch.card, launch.rework_pr) if launch.rework_pr else 0
        if rounds >= harness.REWORK_LIMIT:  # the cap holds whatever the cause (#9)
            if dry_run:
                common.log(JOB, f"would park card #{launch.card} instead of a {what} — {rounds} rework rounds")
                continue
            if harness.park(launch.card, "reworks"):
                worker.park(cfg, launch.card, launch.rework_pr, "reworks", rounds)
            common.log(JOB, f"parked card #{launch.card} after {rounds} rework rounds on #{launch.rework_pr}")
            continue
        idle = harness.idle_runs(launch.card)
        if idle >= harness.IDLE_RUN_LIMIT:  # runs that never got far enough to finish count too
            if dry_run:
                common.log(JOB, f"would park card #{launch.card} instead of {what} — {idle} runs with no new commit")
                continue
            if harness.park(launch.card, "idle"):
                worker.park(cfg, launch.card, launch.rework_pr or launch.card, "idle", idle)
            common.log(JOB, f"parked card #{launch.card} after {idle} runs with no new commit")
            continue
        if dry_run:
            common.log(JOB, f"would start {what}")
            continue
        harness.note_attempt(launch.card)  # before anything that can fail, as for rework rounds
        if launch.rework_pr:
            # counted before anything that can fail — the worker's own setup included — so a rework that keeps
            # failing early still reaches the cap and is parked, instead of relaunching every tick
            harness.note_rework(launch.card, launch.rework_pr)
        pid = start(cfg, launch)
        common.log(JOB, f"started {what} (pid {pid})")
    if not launches:
        common.log(JOB, "nothing to start")
    for note in notes:
        common.log(JOB, note)
    for p in rearms:  # approved and clean, but its arming failed or never happened
        if dry_run:
            common.log(JOB, f"would re-arm #{p.number}: ready, approved at {p.head[:8]}, green, not armed")
            continue
        result = open_pr.rearm(p.number, p.node_id, cfg.base)
        common.log(JOB, f"re-armed #{p.number}: approved at {p.head[:8]} but not armed — auto-merge {result['auto_merge']}"
                        f" (queue {result['queue_method']}, GitHub records {result['merge_method'] or 'none'})")
    for p in disarms:
        what = f"#{p.number}: card #{p.card} {github.waits_line(p.waits_on)}"
        if dry_run:
            common.log(JOB, f"would turn auto-merge off on {what}")
            continue
        open_pr.disarm(p.number, p.node_id)
        common.log(JOB, f"turned auto-merge off on {what} — it is re-armed once approved after they close")
    # after the launches, so a cleanup that fails never holds work back — it still fails the tick, by name
    cleanup.tidy(cfg, JOB, github.open_pulls(cfg), harness.running_workers(), dry_run)


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    return common.run_job(JOB, lambda: tick(dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
