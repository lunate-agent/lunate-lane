"""Cron job: one short daily message — the milestone, WIP, the open pull requests, what is stuck and why, the
parked cards, and what merged since yesterday.

    .venv/bin/python scripts/digest.py

The message goes to stdout (the cron job's delivery) and to `<state>/digest/latest.md`. A read that fails
fails the digest: a digest that silently leaves out what it could not read would report a quiet lane.
"""

from __future__ import annotations

import datetime
import pathlib
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import cleanup, common, github, harness  # noqa: E402
from scripts.common import GhError  # noqa: E402

JOB = "digest"
MERGED_LIMIT = 100


def pull_state(p: github.Pull, required: frozenset, reviewer: str) -> str:
    """One phrase for where a pull request stands, from GitHub's own state only."""
    if p.draft:
        return "draft"
    checks = github.check_state(p, required)
    if checks == "failing":
        return "required checks failing"
    if p.mergeable == "CONFLICTING":
        return "merge conflict"
    if checks == "pending":
        return "checks running"
    verdict = github.reviewer_verdict(p, reviewer)
    if verdict == "CHANGES_REQUESTED":
        return "changes requested"
    if verdict == "APPROVED":
        return "approved, auto-merge armed" if p.auto_merge else "approved, auto-merge NOT armed"
    return "awaiting review" + ("" if p.auto_merge else " (auto-merge not armed)")


def merged_since(cfg: common.Config, since: datetime.datetime) -> list[dict]:
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = common.gh_json("pr", "list", "-R", cfg.slug, "--state", "merged", "--search", f"merged:>={stamp}",
                            "--limit", str(MERGED_LIMIT), "--json", "number,title,mergedAt")
    if not isinstance(merged, list):
        raise GhError("the merged pull requests came back as something other than a list")
    if len(merged) >= MERGED_LIMIT:
        raise GhError(f"{MERGED_LIMIT} or more merges in a day — the list may be truncated")
    return sorted(merged, key=lambda m: m["number"])


def age_days(created: str, now: datetime.datetime) -> float:
    opened = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    return (now - opened).total_seconds() / 86400


PARKED_WHY = {"idle": "runs with no new commit", "reworks": "rework rounds used up",
              "secret": "secret-shaped text in a push"}


def in_flight(cfg: common.Config, pulls: list, running: dict) -> list[str]:
    """What counts toward WIP, as the feeder counts it: each worker pull request's card, and each running worker,
    a worker and its own pull request once."""
    items, seen = [], set()
    for p in sorted((p for p in pulls if p.author == cfg.worker_app), key=lambda p: p.number):
        key = p.card if p.card is not None else ("pr", p.number)
        if key in seen:
            continue
        seen.add(key)
        items.append(f"#{p.number} (card #{p.card}" + (", worker running)" if p.card in running else ")")
                     if p.card is not None else f"#{p.number}")
    items += [f"card #{card} (worker running, no PR yet)" for card in sorted(running) if card not in seen]
    return items


def stuck(cfg: common.Config, pulls: list, required: frozenset, now: datetime.datetime) -> list[tuple]:
    """(number, why) for every open pull request that is stuck: a worker PR that is red, conflicting or out of
    the queue (the feeder sends those back), and any pull request open for more than a day, with its state."""
    found = []
    for p in sorted(pulls, key=lambda p: p.number):
        why = github.stuck_reason(p, required, cfg.reviewer_app) if p.author == cfg.worker_app else None
        age = age_days(p.created, now)
        if why or age > 1:
            parts = [why] if why else [pull_state(p, required, cfg.reviewer_app)]
            if age > 1:
                parts.append(f"open {age:.0f}d")
            found.append((p.number, " — ".join(parts)))
    return found


def compose(cfg: common.Config, cards: list, pulls: list, merged: list, required: frozenset,
            now: datetime.datetime, parked: dict | None = None, running: dict | None = None,
            kept: dict | None = None) -> str:
    running = running or {}
    flight = in_flight(cfg, pulls, running)
    lines = [f"Lunate lane — {now:%Y-%m-%d}", f"{cfg.milestone}: {len(cards)} open issue(s)",
             f"WIP {len(flight)} of {cfg.wip_limit}" + (": " + ", ".join(flight) if flight else "")]
    lines.append(f"Open PRs ({len(pulls)}):" if pulls else "Open PRs: none")
    for p in sorted(pulls, key=lambda p: p.number):
        lines.append(f"  #{p.number} {p.title[:60]} — {pull_state(p, required, cfg.reviewer_app)}")
    held = stuck(cfg, pulls, required, now)
    lines.append(f"Stuck ({len(held)}):" if held else "Stuck: none")
    lines += [f"  #{number} — {why}" for number, why in held]
    parked = parked or {}
    lines.append(f"Parked cards ({len(parked)}) — delete <state>/attempts/<card>.json to unpark:" if parked
                 else "Parked cards: none")
    for number in sorted(parked):
        lines.append(f"  #{number} — {PARKED_WHY.get(parked[number], parked[number])}")
    kept = kept or {}
    lines.append(f"Kept by cleanup ({len(kept)}) — its PR is closed, but removing it would lose work:" if kept
                 else "Kept by cleanup: none")
    lines += [f"  card #{number} — {kept[number]}" for number in sorted(kept)]
    lines.append(f"Merged since yesterday ({len(merged)}):" if merged else "Merged since yesterday: none")
    lines += [f"  #{m['number']} {str(m.get('title') or '')[:60]}" for m in merged]
    return "\n".join(lines)


def tick() -> None:
    cfg = common.config()
    now = datetime.datetime.now(datetime.timezone.utc)
    required = github.required_checks(cfg)
    cards = github.milestone_cards(cfg)
    pulls = [github.pull(cfg, p.number) for p in github.open_pulls(cfg)]
    merged = merged_since(cfg, now - datetime.timedelta(days=1))
    message = compose(cfg, cards, pulls, merged, required, now, harness.parked_details(), harness.running_workers(),
                      cleanup.read_held())
    latest = common.state_dir("digest") / "latest.md"
    try:
        latest.write_text(message + "\n", encoding="utf-8")
    except OSError as exc:
        raise common.StateError(f"the digest {latest} could not be written: {exc}") from exc
    print(message)


def main() -> int:
    return common.run_job(JOB, tick)


if __name__ == "__main__":
    raise SystemExit(main())
