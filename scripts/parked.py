"""Parks a person cannot miss: the parked card and its open pull request carry the label `parked.label` and are
assigned to `parked.assignee` (lane.json), as the worker identity, read back. The pull request's auto-merge is
turned off too (read back): a parked card's work never merges on its own. Unparking does not re-arm it — the
normal path does, once it is ready, approved and green.

Every park sets them (`flag`), every unpark takes them off (`unflag`): the label always, the assignment only where
the lane added it (one the person had before stays). A label or assignment that fails, or does not read back, is
logged and kept in `<state>/parked-flags.json` with its error — the digest shows it under "Waiting on you" — and
the feeder's `reconcile` tries again each tick; it also flags a park and unflags an unpark that happened without
it (a park record edited by hand, an idle park lifted by a new commit, the parks from before this existed).
"""

from __future__ import annotations

import json
import os
import urllib.parse

from scripts import common, github, open_pr
from scripts.common import LaneError, StateError

COLOR = "d93f0b"
DESCRIPTION = "The lane stopped working this card: it needs a person"


# every GitHub call of this module, so a test can stand in for GitHub in one place
def _token() -> str:
    return open_pr._worker_token()


def _call(token: str, method: str, path: str, payload: dict | None = None) -> dict:
    return open_pr._api(token, method, path, payload)


def _get(path: str) -> dict:
    return common.gh_json("api", path)


def _card_pulls(cfg: common.Config, card: int) -> set:
    return {p.number for p in github.open_pulls(cfg) if p.card == card}


def _blockers(cfg: common.Config, card: int) -> tuple:
    return github.open_blockers(cfg, card)


def _pr_card(cfg: common.Config, number: int) -> int | None:
    return github.pull(cfg, number).card


def _disarm(number: int, node_id: str) -> None:
    open_pr.disarm(number, node_id)  # off, and read back; nothing when it is not armed


def _path():
    return common.state_dir() / "parked-flags.json"


def _read() -> dict:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {int(card): {"targets": [int(n) for n in rec["targets"]], "assigned": [int(n) for n in rec["assigned"]],
                            "error": str(rec.get("error") or "")} for card, rec in data.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StateError(f"the park flags {path} could not be read: {exc}") from exc


def _write(records: dict) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({str(card): rec for card, rec in sorted(records.items())}))
        os.replace(tmp, path)
    except OSError as exc:
        raise StateError(f"the park flags {path} could not be written: {exc}") from exc


def errors() -> dict:
    """card -> why its label or assignment (or their removal) failed, for the digest."""
    return {card: rec["error"] for card, rec in _read().items() if rec["error"]}


def flagged() -> frozenset:
    return frozenset(_read())


def _targets(cfg: common.Config, card: int, pr: int | None) -> list[int]:
    """The card and every open pull request carrying it (its `wt/issue-<n>` branch, else the first issue it closes)."""
    found = {int(card)} | ({int(pr)} if pr else set())
    found |= _card_pulls(cfg, card)
    return sorted(found)


def _issue(token: str, cfg: common.Config, number: int) -> tuple[set, set]:
    data = _call(token, "GET", f"/repos/{cfg.slug}/issues/{int(number)}")
    return ({str(label.get("name")) for label in data.get("labels") or []},
            {str(user.get("login")) for user in data.get("assignees") or []})


def _ensure_label(token: str, cfg: common.Config) -> None:
    try:
        _call(token, "POST", f"/repos/{cfg.slug}/labels",
                     {"name": cfg.parked_label, "color": COLOR, "description": DESCRIPTION})
    except open_pr.OpenPrError as exc:
        if "(422)" not in str(exc):  # 422: it exists already
            raise


def flag(cfg: common.Config, card: int, pr: int | None = None, job: str = "lane") -> list[str]:
    """Label and assign the card and its open pull request(s), and read both back. Returns the failures (also
    logged and recorded for the digest); never raises for a GitHub refusal."""
    records = _read()
    rec = records.get(card) or {"targets": [], "assigned": [], "error": ""}
    label, who, failed, disarmed = cfg.parked_label, cfg.parked_assignee, [], []
    try:
        targets = sorted(set(rec["targets"]) | set(_targets(cfg, card, pr)))
        token = _token()
        _ensure_label(token, cfg)
    except LaneError as exc:
        targets, failed = sorted(set(rec["targets"]) | {card} | ({pr} if pr else set())), [f"#{card}: {exc}"]
    if not failed:
        for number in targets:
            try:
                labels, assignees = _issue(token, cfg, number)
                if label not in labels:
                    _call(token, "POST", f"/repos/{cfg.slug}/issues/{number}/labels", {"labels": [label]})
                if who not in assignees:
                    _call(token, "POST", f"/repos/{cfg.slug}/issues/{number}/assignees", {"assignees": [who]})
                    rec["assigned"] = sorted(set(rec["assigned"]) | {number})
                labels, assignees = _issue(token, cfg, number)
                missing = [f"label {label}"] * (label not in labels) + [f"assignee @{who}"] * (who not in assignees)
                if missing:
                    failed.append(f"#{number} reads back without {' and '.join(missing)}")
                if number != card:  # a parked card's pull request never merges on its own (#856, armed since 15:05)
                    pr = _call(token, "GET", f"/repos/{cfg.slug}/pulls/{number}")
                    if pr.get("state") == "open" and pr.get("auto_merge"):
                        _disarm(number, str(pr["node_id"]))
                        disarmed.append(number)
            except LaneError as exc:
                failed.append(f"#{number}: {exc}")
    rec.update(targets=targets, error="; ".join(failed)[:400] if failed else "")
    records[card] = rec
    _write(records)
    where = ", ".join(f"#{n}" for n in targets)
    if failed:
        common.log(job, f"parked card #{card}: FAILED to label {label} / assign @{who} — {rec['error']}")
    else:
        common.log(job, f"parked card #{card}: labelled {label} and assigned @{who} on {where} (read back)"
                        + (f"; auto-merge turned off on " + ", ".join(f"#{n}" for n in disarmed) + " (read back)"
                           if disarmed else ""))
    return failed


def unflag(cfg: common.Config, card: int, job: str = "lane") -> list[str]:
    """Take the label off the card and its pull request(s), and the assignment where the lane added it, and read
    it back. The record goes when everything reads back clean; otherwise it stays, with the error."""
    records = _read()
    rec = records.get(card) or {"targets": [], "assigned": [], "error": ""}
    label, who, failed = cfg.parked_label, cfg.parked_assignee, []
    try:
        targets = sorted(set(rec["targets"]) | set(_targets(cfg, card, None)))
        token = _token()
    except LaneError as exc:
        targets, failed = rec["targets"], [f"#{card}: {exc}"]
    if not failed:
        for number in targets:
            try:
                labels, assignees = _issue(token, cfg, number)
                if label in labels:
                    _call(token, "DELETE",
                                 f"/repos/{cfg.slug}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}")
                if number in rec["assigned"] and who in assignees:
                    _call(token, "DELETE", f"/repos/{cfg.slug}/issues/{number}/assignees", {"assignees": [who]})
                labels, assignees = _issue(token, cfg, number)
                left = [f"label {label}"] * (label in labels) + \
                       [f"assignee @{who}"] * (number in rec["assigned"] and who in assignees)
                if left:
                    failed.append(f"#{number} still reads with {' and '.join(left)}")
            except LaneError as exc:
                failed.append(f"#{number}: {exc}")
    if failed:
        rec.update(targets=targets, error=("unpark: " + "; ".join(failed))[:400])
        records[card] = rec
        common.log(job, f"unparked card #{card}: FAILED to remove {label} / @{who} — {rec['error']}")
    else:
        records.pop(card, None)
        common.log(job, f"unparked card #{card}: {label} removed from " + (", ".join(f"#{n}" for n in targets) or "nothing")
                        + (f", @{who} unassigned from " + ", ".join(f"#{n}" for n in rec["assigned"]) if rec["assigned"]
                           else ""))
    _write(records)
    return failed


def reconcile(cfg: common.Config, parked, job: str) -> None:
    """Flag every parked card not flagged cleanly yet, and unflag every flagged card no longer parked."""
    records = _read()
    for card in sorted(parked):
        if card not in records or records[card]["error"]:
            flag(cfg, card, job=job)
    for card in sorted(set(records) - set(parked)):
        unflag(cfg, card, job=job)


# --- a NEEDS-PERSON that only asks for another pull request or card to merge first is a wait, not a park ---

WAIT, DONE, PARK = "wait", "done", "park"


def _blocker(cfg: common.Config, ref: int) -> tuple[int | None, str]:
    """The issue that `#ref` stands for, and whether it is still to come: (issue, 'open'), (None, 'done') when the
    pull request merged or the issue closed, (None, 'park') when it cannot be waited on (a pull request closed
    unmerged, or one that carries no card)."""
    data = _get(f"repos/{cfg.slug}/issues/{int(ref)}")
    if "pull_request" in data:
        if data.get("state") != "open":
            return (None, DONE) if (data["pull_request"] or {}).get("merged_at") else (None, PARK)
        card = _pr_card(cfg, ref)
        if card is None:
            return None, PARK
        data = _get(f"repos/{cfg.slug}/issues/{int(card)}")
    return (int(data["number"]), "open") if data.get("state") == "open" else (None, DONE)


def defer(cfg: common.Config, card: int, reason: str, job: str, dry_run: bool = False) -> str:
    """For a NEEDS-PERSON `reason` that only says another pull request or card must merge first
    (`github.merge_first`): record that card as blocking this one (GitHub's blocked-by, read back) and return
    'wait' — the feeder neither parks nor reworks a card that waits, and takes it up when the blocker closes. 'done'
    when the blocker has merged already (rework it instead); 'park' for anything else, and when the link cannot be
    made or read back (logged)."""
    ref = github.merge_first(reason)
    if ref is None:
        return PARK
    try:
        blocker, state = _blocker(cfg, ref)
        if blocker is None:
            common.log(job, f"card #{card}: NEEDS-PERSON asks #{ref} to merge first — "
                            + ("it has merged; reworked instead of parked" if state == DONE else
                               "it cannot be waited on (closed unmerged, or no card); parked"))
            return state
        if blocker == card:
            return PARK
        if blocker in _blockers(cfg, card):
            return WAIT
        if dry_run:
            common.log(job, f"would record card #{card} as blocked by #{blocker} instead of parking (#{ref} must merge first)")
            return WAIT
        token = _token()
        issue_id = int(_call(token, "GET", f"/repos/{cfg.slug}/issues/{blocker}")["id"])
        _call(token, "POST", f"/repos/{cfg.slug}/issues/{int(card)}/dependencies/blocked_by",
                     {"issue_id": issue_id})
        if blocker not in _blockers(cfg, card):
            raise LaneError(f"#{card} does not read back as blocked by #{blocker}")
    except LaneError as exc:
        common.log(job, f"card #{card}: could not record it as blocked by #{ref}'s card ({exc}) — parked instead")
        return PARK
    common.log(job, f"card #{card} waits on #{blocker} (NEEDS-PERSON only asked #{ref} to merge first): blocked-by "
                    "recorded and read back instead of parking; it resumes when that closes")
    return WAIT


# --- a closed issue or pull request carries no park ---

def _lane_assigned(cfg: common.Config, number: int, who: str) -> bool:
    """Whether the lane (the worker identity) made the last assignment of `who` on this issue or pull request. Read
    from the timeline: the events listing names the assignee as the actor of an App's assignment."""
    events = _get(f"repos/{cfg.slug}/issues/{int(number)}/timeline?per_page=100")
    mine = {cfg.worker_app, f"{cfg.worker_app}[bot]"}
    last = [e for e in events if e.get("event") in ("assigned", "unassigned")
            and str((e.get("assignee") or {}).get("login")) == who]
    return bool(last) and last[-1].get("event") == "assigned" and str((last[-1].get("actor") or {}).get("login")) in mine


def sweep_closed(cfg: common.Config, parked, job: str, dry_run: bool = False) -> list[int]:
    """Take the park label off every closed issue and pull request that still carries it, and the assignment where
    the lane made it (its flag record says so, or the worker identity made the last assignment) — a closed card,
    or a closed pull request of a card. `reconcile` only knows what its records name; this also catches a label
    whose record was lost (#780: labelled after it closed, during the backfill of 2026-11-13, with no record).
    A card still parked keeps its own label. Read back; a failure is logged and never stops the tick, and the
    next tick tries again. Returns the numbers cleared."""
    label, who = cfg.parked_label, cfg.parked_assignee
    try:
        found = _get(f"repos/{cfg.slug}/issues?state=closed&labels={urllib.parse.quote(label, safe='')}&per_page=100")
    except LaneError as exc:
        common.log(job, f"FAILED to list the closed issues still labelled {label} — {exc}")
        return []
    stale = sorted(int(i["number"]) for i in found or [] if int(i["number"]) not in set(parked))
    if not stale:
        return []
    if dry_run:
        for number in stale:
            common.log(job, f"would remove {label} from closed #{number}")
        return []
    records = _read()
    by_record = {n for rec in records.values() for n in rec["assigned"]}
    cleared, token = [], None
    for number in stale:
        try:
            token = token or _token()
            labels, assignees = _issue(token, cfg, number)
            unassign = who in assignees and (number in by_record or _lane_assigned(cfg, number, who))  # theirs stays
            if label in labels:
                _call(token, "DELETE", f"/repos/{cfg.slug}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}")
            if unassign:
                _call(token, "DELETE", f"/repos/{cfg.slug}/issues/{number}/assignees", {"assignees": [who]})
            labels, assignees = _issue(token, cfg, number)
            left = [f"label {label}"] * (label in labels) + [f"assignee @{who}"] * (unassign and who in assignees)
            if left:
                raise LaneError(f"#{number} still reads with {' and '.join(left)}")
        except LaneError as exc:
            common.log(job, f"FAILED to clear closed #{number} of {label} — {exc}")
            continue
        for card, rec in list(records.items()):  # a record naming it no longer has to
            if number in rec["targets"] or number in rec["assigned"]:
                rec["targets"] = [n for n in rec["targets"] if n != number]
                rec["assigned"] = [n for n in rec["assigned"] if n != number]
                if card not in set(parked) and not rec["targets"]:
                    records.pop(card)
        common.log(job, f"closed #{number}: {label} removed" + (f", @{who} unassigned" if unassign else "")
                        + " (read back) — a closed issue or pull request carries no park")
        cleared.append(number)
    _write(records)
    return cleared


# --- a parked card that is closed waits on nobody ---

def clear_closed(cfg: common.Config, cards, job: str, dry_run: bool = False) -> list[int]:
    """Unpark every parked card that is closed (no worker running it), and log it; `reconcile` then takes the
    label and assignment off. Returns the cards cleared."""
    from scripts import harness

    cleared = []
    for card in sorted(cards):
        if _get(f"repos/{cfg.slug}/issues/{int(card)}").get("state") != "closed":
            continue
        if harness.live_pid(card) is not None:
            continue
        if dry_run:
            common.log(job, f"would clear the park of card #{card}: it is closed")
            continue
        harness.unpark(card)
        common.log(job, f"cleared the park of card #{card}: it is closed")
        cleared.append(card)
    return cleared
