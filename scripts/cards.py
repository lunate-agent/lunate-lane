"""The feeder's decisions, as pure functions: which card is next, and whether there is room for it.

Nothing here reads GitHub or the disk except `load_pins`; the feeder hands in what it read, so every rule can
be tested without a network.

    order:  pins first, then `kind/bug`, then cards other open issues wait on, then oldest first
    room:   total WIP (cards with an open worker PR or a running worker, less parked cards with no worker
            running) below the limit, and no other card of the same area in flight — a parked card still holds
            its area
    skip:   an issue labelled `kind/decision` is a question for a maintainer, not work: never started. Blocking is
            GitHub's native blocked-by and nothing else
    rework: a worker PR whose reviewer requested changes on its current head goes back to a worker on its own
            branch, before any new card, and does not add to WIP — its card is already in flight; so does a
            stuck one, and an orphaned draft (a draft with no worker running and the card not parked)
    person: a request for changes whose body says NEEDS-PERSON parks the card instead of a rework
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

from scripts.common import ConfigError
from scripts.github import Card, Pull, needs_person, stuck_reason, verdict

ORPHANED = "orphaned draft"
PERSON = "needs a person: "


@dataclasses.dataclass(frozen=True)
class Launch:
    card: int
    rework_pr: int | None
    areas: frozenset
    reason: str = ""  # why a rework: changes requested, why the pull request is stuck, or an orphaned draft

    @property
    def person(self) -> str | None:
        """The reviewer's NEEDS-PERSON reason: this launch parks the card instead of starting a worker."""
        return self.reason[len(PERSON):] if self.reason.startswith(PERSON) else None


@dataclasses.dataclass(frozen=True)
class Rules:
    wip_limit: int
    worker_app: str
    reviewer_app: str
    bug_label: str
    area_prefix: str
    decision_label: str = "kind/decision"


def load_pins(path: pathlib.Path) -> tuple:
    """The optional pins file, `{"cards": [n, ...]}`. Absent means no pins; unreadable or malformed raises."""
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pins = tuple(int(n) for n in data["cards"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ConfigError(f"the pins file {path} could not be read as {{\"cards\": [numbers]}}: {exc}") from exc
    return pins


def areas_of(labels, prefix: str) -> frozenset:
    return frozenset(label for label in labels if label.startswith(prefix))


def order(cards: list[Card], pins: tuple, bug_label: str) -> list[Card]:
    """Pinned cards in pin order, then bugs, then cards others depend on, then oldest first."""
    def key(card: Card) -> tuple:
        pinned = pins.index(card.number) if card.number in pins else len(pins)
        return (pinned, bug_label not in card.labels, not card.blocking, card.created, card.number)

    return sorted(cards, key=key)


def rework_reason(p: Pull, reviewer_app: str, required: frozenset = frozenset(),
                  reran: frozenset = frozenset()) -> str | None:
    """Why this pull request goes back to a worker: the reviewer's request for changes on the current head (a
    push since then clears it), a stuck pull request — red, conflicting, or out of the queue (#8) — or a draft
    nobody is working on (the caller leaves out running and parked cards). A request for changes that says
    NEEDS-PERSON is `needs a person: <reason>`: the feeder parks the card instead."""
    if verdict(p, reviewer_app) == "CHANGES_REQUESTED":  # a dismissed one, or one a person approved over, is not
        bodies = [r.body for r in p.reviews if r.author == reviewer_app and r.commit == p.head
                  and r.state == "CHANGES_REQUESTED"]
        person = needs_person(bodies[-1]) if bodies else None
        return PERSON + person if person else "changes requested"
    stuck = stuck_reason(p, required, reviewer_app, reran)
    if stuck:
        return stuck
    if p.state == "OPEN" and p.draft:
        return ORPHANED
    return None


def needs_rework(p: Pull, reviewer_app: str, required: frozenset = frozenset()) -> bool:
    return rework_reason(p, reviewer_app, required) is not None


def plan(*, cards: list[Card], open_pulls: list[Pull], worker_pulls: list[Pull], running: dict,
         rules: Rules, pins: tuple = (), parked: frozenset = frozenset(),
         required: frozenset = frozenset(), reran: frozenset = frozenset()) -> tuple[list[Launch], list[str]]:
    """What to start this tick: reworks first, then new cards while there is room. Returns (launches, notes).

    `open_pulls` is every open pull request (any author): a card one of them carries or closes is never started.
    `worker_pulls` are the worker identity's open pull requests, in full. `running` maps the card of each
    running worker to its areas. A `parked` card is neither reworked nor started; it does not count toward WIP
    (unless its worker still runs), but its open pull request keeps its areas occupied.
    """
    by_number = {card.number: card for card in cards}

    def pull_areas(p: Pull) -> frozenset:
        own = by_number.get(p.card).labels if p.card in by_number else frozenset()
        return areas_of(p.labels | own, rules.area_prefix)

    notes: list[str] = []
    # a parked card waits on a person, not on the lane: it frees its WIP slot, but its area stays occupied
    in_flight = {("card", p.card) if p.card is not None else ("pr", p.number) for p in worker_pulls
                 if p.card not in parked or p.card in running}
    in_flight |= {("card", card) for card in running}
    occupied = frozenset().union(*(pull_areas(p) for p in worker_pulls), *running.values())
    taken = {n for p in open_pulls for n in (p.card, *p.cards) if n is not None} | set(running)

    reasons = {p.number: rework_reason(p, rules.reviewer_app, required, reran) for p in worker_pulls}
    launches = [Launch(p.card, p.number, pull_areas(p), reasons[p.number])
                for p in sorted(worker_pulls, key=lambda p: p.number)
                if p.card is not None and p.card not in running and p.card not in parked and reasons[p.number]]
    for p in worker_pulls:
        if p.card is None and reasons[p.number]:
            notes.append(f"#{p.number} needs rework ({reasons[p.number]}) but carries no card — left for the orchestrator")
        elif p.card in parked and p.card not in running and reasons[p.number]:
            notes.append(f"#{p.number} needs rework ({reasons[p.number]}) but card #{p.card} is parked — left for a person")

    wip = len(in_flight)
    for card in order(cards, pins, rules.bug_label):
        if wip >= rules.wip_limit:
            notes.append(f"WIP {wip} of {rules.wip_limit} — no new card")
            break
        areas = areas_of(card.labels, rules.area_prefix)
        if card.number in taken or rules.decision_label in card.labels:
            continue
        if card.number in parked:
            notes.append(f"#{card.number} is parked — left for a person")
            continue
        if card.blocked_by:
            notes.append(f"#{card.number} waits on " + ", ".join(f"#{n}" for n in card.blocked_by))
            continue
        if areas & occupied:
            notes.append(f"#{card.number} skipped — {', '.join(sorted(areas & occupied))} already in flight")
            continue
        launches.append(Launch(card.number, None, areas))
        wip += 1
        occupied |= areas
        taken.add(card.number)
    return launches, notes
