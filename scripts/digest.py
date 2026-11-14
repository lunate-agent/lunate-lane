"""Cron job: one short daily message — what waits on a maintainer first, then the milestone, WIP, the open pull
requests, what is stuck and why, the parked cards, and what merged since yesterday.

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

from scripts import cleanup, common, github, harness, parked  # noqa: E402
from scripts.common import GhError  # noqa: E402

JOB = "digest"
MERGED_LIMIT = 100


def pull_state(p: github.Pull, required: frozenset, reviewer: str) -> str:
    """One phrase for where a pull request stands, from GitHub's own state only."""
    if p.waits_on:
        return github.waits_line(p.waits_on) + (" (auto-merge still armed)" if p.auto_merge else "")
    if p.draft:
        return "draft"
    checks = github.check_state(p, required)
    if checks == "failing":
        return "required checks failing"
    if p.mergeable == "CONFLICTING":
        return "merge conflict"
    if checks == "pending":
        return "checks running"
    verdict = github.verdict(p, reviewer)
    if verdict == "CHANGES_REQUESTED":
        return "changes requested"
    if verdict == "APPROVED":
        if p.disarmed_by:
            return f"approved, auto-merge turned off by @{p.disarmed_by}"
        return "approved, auto-merge armed" if p.auto_merge else "approved, auto-merge NOT armed"
    return "awaiting review" + ("" if p.auto_merge else " (auto-merge not armed)")


def merged_since(cfg: common.Config, since: datetime.datetime) -> list[dict]:
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = common.gh_json("pr", "list", "-R", cfg.slug, "--state", "merged", "--search", f"merged:>={stamp}",
                            "--limit", str(MERGED_LIMIT), "--json", "number,title,mergedAt,closingIssuesReferences")
    if not isinstance(merged, list):
        raise GhError("the merged pull requests came back as something other than a list")
    if len(merged) >= MERGED_LIMIT:
        raise GhError(f"{MERGED_LIMIT} or more merges in a day — the list may be truncated")
    return sorted(merged, key=lambda m: m["number"])


def age_days(created: str, now: datetime.datetime) -> float:
    opened = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    return (now - opened).total_seconds() / 86400


PARKED_WHY = {"idle": "runs with no new commit", "reworks": "rework rounds used up",
              "secret": "secret-shaped text in a push", "person": "needs a person",
              "ready": "CI never ran it as ready", "manual": "parked by hand"}


def parked_line(why) -> str:
    """One parked card's reason: the kind, and for a person's decision or a failed ready run, the words why."""
    reason, detail = why if isinstance(why, tuple) else (why, "")
    text = PARKED_WHY.get(reason, reason)
    return f"{text}: {detail}" if detail else text


def in_flight(cfg: common.Config, pulls: list, running: dict, parked=frozenset()) -> list[str]:
    """What counts toward WIP, as the feeder counts it: each worker pull request's card, and each running worker,
    a worker and its own pull request once. A parked card with no worker running does not count."""
    items, seen = [], set()
    for p in sorted((p for p in pulls if p.author == cfg.worker_app), key=lambda p: p.number):
        key = p.card if p.card is not None else ("pr", p.number)
        if key in seen or (p.card in parked and p.card not in running):
            continue
        seen.add(key)
        items.append(f"#{p.number} (card #{p.card}" + (", worker running)" if p.card in running else ")")
                     if p.card is not None else f"#{p.number}")
    items += [f"card #{card} (worker running, no PR yet)" for card in sorted(running) if card not in seen]
    return items


def stuck(cfg: common.Config, pulls: list, required: frozenset, now: datetime.datetime,
          running=frozenset(), parked=frozenset(), requeued: dict | None = None) -> list[tuple]:
    """(number, why) for every open pull request that is stuck: a worker PR that is red, conflicting or out of
    the queue, or an orphaned draft (the feeder sends those back), and any pull request open for more than a
    day, with its state."""
    found = []
    for p in sorted(pulls, key=lambda p: p.number):
        why = github.stuck_reason(p, required, cfg.reviewer_app) if p.author == cfg.worker_app else None
        if why is None and p.author == cfg.worker_app and github.orphaned_draft(p, running, parked):
            why = "orphaned draft — no worker on it; the feeder relaunches it"
        if why is None and p.author == cfg.worker_app:
            why = queue_stuck(p, (requeued or {}).get(p.number, ()))
        age = age_days(p.created, now)
        if why or age > 1:
            parts = [why] if why else [pull_state(p, required, cfg.reviewer_app)]
            if age > 1:
                parts.append(f"open {age:.0f}d")
            found.append((p.number, " — ".join(parts)))
    return found


def queue_stuck(p: github.Pull, removals: tuple) -> str | None:
    """'stuck in queue' for a pull request the queue removed at its head for an infra reason again after the feeder
    re-enqueued it REQUEUE_LIMIT times, or whose re-enqueue did not take (`removals`: the removals of this head the
    feeder acted on — the latest among them, and still out of the queue)."""
    e = p.ejection
    if e is None or e.kind != "infra" or p.in_queue or e.at not in removals:
        return None
    if len(removals) <= github.REQUEUE_LIMIT:
        return (f"stuck in queue — the re-enqueue after removal {len(removals)} at {p.head[:8]} did not take "
                f"(see feed.log); last: {e.reason}")
    return (f"stuck in queue — removed {len(removals)} times at {p.head[:8]}, re-enqueued {github.REQUEUE_LIMIT}; "
            f"last: {e.reason} ({_short(e.why, WAIT_REASON + 40)})")


WAIT_TITLE = 32
WAIT_REASON = 60


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def waiting_on_you(cfg: common.Config, decisions: list, pulls: list, required: frozenset, parked: dict,
                   merged: list, cards: list, flag_errors: dict | None = None, codeowners: tuple = ()) -> list[str]:
    """The lines a maintainer acts on, short enough for a phone: open decisions (oldest first, with the cards each
    blocks), every parked card whatever the reason (its reason and a link), a park whose label or assignment
    failed, pull requests waiting for a maintainer's approval (`code-owner review: #n (paths)` when the bot approved
    and the paths it changes are code-owned: the feeder enqueues it on the tick after that approval), and a merge that left its card
    open."""
    url = f"https://github.com/{cfg.slug}"
    lines = []
    for d in decisions:
        blocks = (" — blocks " + ", ".join(f"#{n}" for n in d.blocking)) if d.blocking else ""
        lines.append(f"  decide #{d.number} {_short(d.title, WAIT_TITLE)}{blocks}")
    pr_of = {p.card: p.number for p in pulls if p.author == cfg.worker_app and p.card is not None}
    still_open = {c.number for c in cards}
    for number in sorted(parked):  # every park needs a person, whatever its reason (a closed card's is cleared)
        link = f"{url}/pull/{pr_of[number]}" if number in pr_of else f"{url}/issues/{number}"
        lines.append(f"  parked #{number}: {_short(parked_line(parked[number]), WAIT_REASON - 5)} {link}")
    for number, error in sorted((flag_errors or {}).items()):
        lines.append(f"  card #{number}: {cfg.parked_label} / @{cfg.parked_assignee} FAILED — "
                     f"{_short(error, WAIT_REASON + 40)}")
    for p in sorted(pulls, key=lambda p: p.number):
        if not github.waits_for_approval(p, required, cfg.reviewer_app):
            continue
        owned = github.owned_paths(p.files, codeowners) if p.card is not None else ()
        if owned:  # the bot approved; GitHub wants the code owner's too before it can be enqueued
            lines.append(f"  code-owner review: #{p.number} ({_short(', '.join(owned), WAIT_REASON - 20)}) "
                         f"{url}/pull/{p.number}")
        else:
            lines.append(f"  approve #{p.number} {_short(p.title, WAIT_TITLE)} {url}/pull/{p.number}")
    for m in merged:
        for ref in m.get("closingIssuesReferences") or []:
            if ref.get("number") in still_open:
                lines.append(f"  close #{ref['number']} — #{m['number']} merged but it is still open")
    return [f"Waiting on you ({len(lines)}):", *lines] if lines else ["Waiting on you: nothing"]


def compose(cfg: common.Config, cards: list, pulls: list, merged: list, required: frozenset,
            now: datetime.datetime, parked: dict | None = None, running: dict | None = None,
            kept: dict | None = None, decisions: list | None = None, died: dict | None = None,
            requeued: dict | None = None, queue_events: list | None = None, flag_errors: dict | None = None,
            codeowners: tuple = ()) -> str:
    running = running or {}
    parked = parked or {}
    flight = in_flight(cfg, pulls, running, frozenset(parked))
    lines = [f"Lunate lane — {now:%Y-%m-%d}",
             *waiting_on_you(cfg, decisions or [], pulls, required, parked, merged, cards, flag_errors, codeowners),
             f"{cfg.milestone}: {len(cards)} open issue(s)",
             f"WIP {len(flight)} of {cfg.wip_limit}" + (": " + ", ".join(flight) if flight else "")]
    lines.append(f"Open PRs ({len(pulls)}):" if pulls else "Open PRs: none")
    for p in sorted(pulls, key=lambda p: p.number):
        lines.append(f"  #{p.number} {p.title[:60]} — {pull_state(p, required, cfg.reviewer_app)}")
    held = stuck(cfg, pulls, required, now, running, frozenset(parked), requeued)
    # a worker run that failed by name — its setup, its push — before its session could end: the reason, by card
    dead = [(card, why) for card, why in sorted((died or {}).items()) if card not in running]
    lines.append(f"Stuck ({len(held) + len(dead)}):" if held or dead else "Stuck: none")
    lines += [f"  #{number} — {why}" for number, why in held]
    lines += [f"  card #{card} — worker died: {_short(why, WAIT_REASON + 40)}" for card, why in dead]
    lines.append(f"Parked cards ({len(parked)}) — `scripts/lane.py unpark <card> --reason '...'` to unpark:" if parked
                 else "Parked cards: none")
    for number in sorted(parked):
        lines.append(f"  #{number} — {parked_line(parked[number])}")
    events = queue_events or []
    lines.append(f"Merge queue since yesterday ({len(events)}):" if events else "Merge queue since yesterday: nothing")
    lines += [f"  {line}" for line in events]
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
    decisions = github.decision_issues(cfg)
    message = compose(cfg, cards, pulls, merged, required, now, harness.parked_info(), harness.running_workers(),
                      cleanup.read_held(), decisions, harness.died_info(),
                      {p.number: harness.requeues(p.number, p.head) for p in pulls if p.ejection is not None},
                      harness.queue_events((now - datetime.timedelta(days=1)).timestamp()), parked.errors(),
                      github.codeowners(cfg))
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
