"""Parks a person cannot miss: the parked card and its open pull request carry the label `parked.label` and are
assigned to `parked.assignee` (lane.json), as the worker identity, read back.

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
    return _call(token, method, path, payload)


def _get(path: str) -> dict:
    return common.gh_json("api", path)


def _card_pulls(cfg: common.Config, card: int) -> set:
    return {p.number for p in github.open_pulls(cfg) if p.card == card}


def _blockers(cfg: common.Config, card: int) -> tuple:
    return _blockers(cfg, card)


def _pr_card(cfg: common.Config, number: int) -> int | None:
    return github.pull(cfg, number).card


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
    label, who, failed = cfg.parked_label, cfg.parked_assignee, []
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
            except LaneError as exc:
                failed.append(f"#{number}: {exc}")
    rec.update(targets=targets, error="; ".join(failed)[:400] if failed else "")
    records[card] = rec
    _write(records)
    where = ", ".join(f"#{n}" for n in targets)
    if failed:
        common.log(job, f"parked card #{card}: FAILED to label {label} / assign @{who} — {rec['error']}")
    else:
        common.log(job, f"parked card #{card}: labelled {label} and assigned @{who} on {where} (read back)")
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
