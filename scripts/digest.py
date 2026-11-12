"""Cron job: one short daily message — the milestone, the open pull requests, the last day's merges, what is stuck.

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

from scripts import common, github  # noqa: E402
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


def compose(cfg: common.Config, cards: list, pulls: list, merged: list, required: frozenset,
            now: datetime.datetime) -> str:
    lines = [f"Lunate lane — {now:%Y-%m-%d}", f"{cfg.milestone}: {len(cards)} open issue(s)"]
    lines.append(f"Open PRs ({len(pulls)}):" if pulls else "Open PRs: none")
    states = {p.number: pull_state(p, required, cfg.reviewer_app) for p in pulls}
    for p in sorted(pulls, key=lambda p: p.number):
        lines.append(f"  #{p.number} {p.title[:60]} — {states[p.number]}")
    lines.append(f"Merged in the last 24h ({len(merged)}): " + (", ".join(f"#{m['number']}" for m in merged) or "none"))
    stuck = [p for p in pulls if age_days(p.created, now) > 1]
    lines.append(f"Stuck more than a day ({len(stuck)}):" if stuck else "Stuck more than a day: none")
    for p in sorted(stuck, key=lambda p: p.created):
        lines.append(f"  #{p.number} open {age_days(p.created, now):.0f}d — {states[p.number]}")
    return "\n".join(lines)


def tick() -> None:
    cfg = common.config()
    now = datetime.datetime.now(datetime.timezone.utc)
    required = github.required_checks(cfg)
    cards = github.milestone_cards(cfg)
    pulls = [github.pull(cfg, p.number) for p in github.open_pulls(cfg)]
    merged = merged_since(cfg, now - datetime.timedelta(days=1))
    message = compose(cfg, cards, pulls, merged, required, now)
    (common.state_dir("digest") / "latest.md").write_text(message + "\n", encoding="utf-8")
    print(message)


def main() -> int:
    return common.run_job(JOB, tick)


if __name__ == "__main__":
    raise SystemExit(main())
