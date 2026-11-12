"""The feeder's decisions: order, WIP and area limits, rework before new work. Pure, no network."""

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import cards  # noqa: E402
from scripts.common import ConfigError  # noqa: E402
from tests.helpers import REQUIRED, card, pull, review  # noqa: E402

RULES = cards.Rules(wip_limit=5, worker_app="lunate-worker", reviewer_app="lunate-reviewer",
                    bug_label="kind/bug", area_prefix="area/")


def plan(cards_, open_pulls=(), worker_pulls=(), running=None, pins=(), rules=RULES, parked=frozenset()):
    worker_pulls = list(worker_pulls)
    return cards.plan(cards=list(cards_), open_pulls=list(open_pulls) or worker_pulls, worker_pulls=worker_pulls,
                      running=running or {}, rules=rules, pins=tuple(pins), parked=frozenset(parked),
                      required=REQUIRED)


def started(launches):
    return [(launch.card, launch.rework_pr) for launch in launches]


class OrderTests(unittest.TestCase):
    def test_bugs_first_then_cards_others_wait_on_then_oldest(self) -> None:
        old = card(1, created="2026-01-01T00:00:00Z")
        newer = card(2, created="2026-02-01T00:00:00Z")
        blocking = card(3, created="2026-03-01T00:00:00Z", blocking=(9,))
        bug = card(4, "kind/bug", created="2026-04-01T00:00:00Z")
        ordered = [c.number for c in cards.order([newer, old, blocking, bug], (), "kind/bug")]
        self.assertEqual(ordered, [4, 3, 1, 2])

    def test_pins_override_the_order(self) -> None:
        bug = card(4, "kind/bug")
        plain = card(5)
        other = card(6)
        ordered = [c.number for c in cards.order([bug, plain, other], (6, 5), "kind/bug")]
        self.assertEqual(ordered, [6, 5, 4])


class PinsFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="lane-pins-"))

    def test_an_absent_pins_file_means_no_pins(self) -> None:
        self.assertEqual(cards.load_pins(self.dir / "absent.json"), ())

    def test_a_malformed_pins_file_is_a_named_failure_not_no_pins(self) -> None:
        path = self.dir / "pins.json"
        for text in ("{not json", json.dumps({"cards": ["x"]}), json.dumps({"other": []}), json.dumps([1])):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(ConfigError):
                    cards.load_pins(path)

    def test_an_unreadable_pins_file_is_a_named_failure(self) -> None:
        (self.dir / "pins.json").mkdir()
        with self.assertRaises(ConfigError):
            cards.load_pins(self.dir / "pins.json")

    def test_pins_are_read_in_order(self) -> None:
        (self.dir / "pins.json").write_text(json.dumps({"cards": [7, 3]}))
        self.assertEqual(cards.load_pins(self.dir / "pins.json"), (7, 3))


class WipTests(unittest.TestCase):
    def test_new_cards_start_until_the_wip_limit(self) -> None:
        launches, notes = plan([card(n) for n in range(1, 9)])
        self.assertEqual(len(launches), 5)
        self.assertTrue(any("WIP 5 of 5" in note for note in notes))

    def test_open_worker_prs_and_running_workers_both_count(self) -> None:
        prs = [pull(100 + n, n) for n in range(1, 3)]
        launches, _ = plan([card(n) for n in range(10, 20)], worker_pulls=prs, running={7: frozenset(), 8: frozenset()})
        self.assertEqual(len(launches), 1)

    def test_a_worker_and_its_own_pr_are_one_card_in_flight(self) -> None:
        launches, _ = plan([card(n) for n in range(10, 20)], worker_pulls=[pull(101, 1)], running={1: frozenset()})
        self.assertEqual(len(launches), 4)

    def test_a_full_lane_starts_nothing_new(self) -> None:
        prs = [pull(100 + n, n) for n in range(1, 6)]
        launches, _ = plan([card(20)], worker_pulls=prs)
        self.assertEqual(launches, [])

    def test_a_card_any_open_pr_carries_or_closes_is_not_started(self) -> None:
        foreign = pull(200, 1, author="someone", cards=(2,))
        launches, _ = plan([card(1), card(2), card(3)], open_pulls=[foreign])
        self.assertEqual(started(launches), [(3, None)])

    def test_a_card_with_an_open_blocker_waits(self) -> None:
        launches, notes = plan([card(1, blocked_by=(9,)), card(2)])
        self.assertEqual(started(launches), [(2, None)])
        self.assertIn("#1 waits on #9", notes)


class AreaTests(unittest.TestCase):
    def test_one_card_per_area_in_one_tick(self) -> None:
        launches, _ = plan([card(1, "area/tui"), card(2, "area/tui"), card(3, "area/core")])
        self.assertEqual(started(launches), [(1, None), (3, None)])

    def test_an_area_held_by_an_open_worker_pr_is_skipped(self) -> None:
        launches, notes = plan([card(2, "area/tui"), card(3)], worker_pulls=[pull(101, 1, labels=("area/tui",))])
        self.assertEqual(started(launches), [(3, None)])
        self.assertTrue(any("area/tui already in flight" in note for note in notes))

    def test_an_area_is_taken_from_the_milestone_card_when_the_pr_closes_nothing(self) -> None:
        launches, _ = plan([card(1, "area/tui"), card(2, "area/tui")], worker_pulls=[pull(101, 1)])
        self.assertEqual(started(launches), [])

    def test_an_area_held_by_a_running_worker_is_skipped(self) -> None:
        launches, _ = plan([card(2, "area/core"), card(3, "area/tui")], running={1: frozenset({"area/core"})})
        self.assertEqual(started(launches), [(3, None)])

    def test_cards_without_an_area_are_not_area_limited(self) -> None:
        launches, _ = plan([card(1), card(2), card(3)])
        self.assertEqual(len(launches), 3)


class ReworkTests(unittest.TestCase):
    def test_changes_requested_at_the_head_goes_back_before_new_cards(self) -> None:
        sent_back = pull(101, 1, reviews=[review("CHANGES_REQUESTED")])
        launches, _ = plan([card(5)], worker_pulls=[sent_back])
        self.assertEqual(started(launches), [(1, 101), (5, None)])

    def test_rework_starts_even_when_wip_is_full(self) -> None:
        prs = [pull(100 + n, n) for n in range(1, 5)] + [pull(105, 5, reviews=[review("CHANGES_REQUESTED")])]
        launches, _ = plan([card(20)], worker_pulls=prs)
        self.assertEqual(started(launches), [(5, 105)])

    def test_a_request_on_an_older_head_is_not_rework(self) -> None:
        pushed_since = pull(101, 1, head="h2", reviews=[review("CHANGES_REQUESTED", commit="h1")])
        launches, _ = plan([], worker_pulls=[pushed_since])
        self.assertEqual(launches, [])

    def test_a_later_approval_on_the_same_head_is_not_rework(self) -> None:
        approved = pull(101, 1, reviews=[review("CHANGES_REQUESTED"), review("APPROVED")])
        launches, _ = plan([], worker_pulls=[approved])
        self.assertEqual(launches, [])

    def test_no_second_worker_on_a_card_already_being_reworked(self) -> None:
        sent_back = pull(101, 1, reviews=[review("CHANGES_REQUESTED")])
        launches, _ = plan([], worker_pulls=[sent_back], running={1: frozenset()})
        self.assertEqual(launches, [])

    def test_another_identitys_review_does_not_send_work_back(self) -> None:
        other = pull(101, 1, reviews=[review("CHANGES_REQUESTED", author="someone")])
        launches, _ = plan([], worker_pulls=[other])
        self.assertEqual(launches, [])

    def test_a_sent_back_pr_with_no_card_is_named_not_started(self) -> None:
        orphan = pull(101, None, reviews=[review("CHANGES_REQUESTED")])
        launches, notes = plan([], worker_pulls=[orphan])
        self.assertEqual(launches, [])
        self.assertTrue(any("#101 needs rework (changes requested) but carries no card" in note for note in notes))


RED = (("ci-ok", "COMPLETED", "FAILURE"),)


class StuckTests(unittest.TestCase):
    """#8: a stuck worker pull request goes back to a worker like changes-requested, and adds nothing to WIP."""

    def test_red_conflicting_and_dropped_from_the_queue_each_go_back_with_their_reason(self) -> None:
        pulls = [pull(101, 1, checks=RED), pull(102, 2, mergeable="CONFLICTING"),
                 pull(103, 3, reviews=[review("APPROVED")], queue_removal="MERGE_CONFLICT")]
        launches, _ = plan([card(9)], worker_pulls=pulls, rules=cards.Rules(3, *list(vars(RULES).values())[1:]))
        self.assertEqual([(l.card, l.rework_pr, l.reason) for l in launches], [
            (1, 101, "required checks failing"), (2, 102, "conflicts with the base branch"),
            (3, 103, "out of the merge queue after approval (merge_conflict)")])

    def test_a_draft_a_running_card_or_a_parked_card_is_not_sent_back(self) -> None:
        self.assertEqual(started(plan([], worker_pulls=[pull(101, 1, checks=RED, draft=True)])[0]), [])
        self.assertEqual(started(plan([], worker_pulls=[pull(101, 1, checks=RED)], running={1: frozenset()})[0]), [])
        launches, notes = plan([], worker_pulls=[pull(101, 1, mergeable="CONFLICTING")], parked={1})
        self.assertEqual(started(launches), [])
        self.assertIn("#101 needs rework (conflicts with the base branch) but card #1 is parked — left for a person",
                      notes)

    def test_green_pending_queued_armed_or_unknown_is_not_stuck(self) -> None:
        for p in (pull(101, 1), pull(101, 1, checks=[("ci-ok", "IN_PROGRESS", "")]), pull(101, 1, mergeable="UNKNOWN"),
                  pull(101, 1, reviews=[review("APPROVED")], in_queue=True, queue_removal="X"),
                  pull(101, 1, reviews=[review("APPROVED")], auto_merge=True, queue_removal="X"),
                  pull(101, 1, reviews=[review("APPROVED")])):
            with self.subTest(p=p):
                self.assertEqual(started(plan([], worker_pulls=[p])[0]), [])

    def test_an_infra_failure_is_rerun_first_and_reworked_only_when_it_fails_again_on_that_head(self) -> None:
        from scripts import github

        runs = (github.Run("ci-ok", "CANCELLED", 1, "u", 9), github.Run("verify", "CANCELLED", 2, "u", 9))
        p = pull(101, 1, checks=(("ci-ok", "COMPLETED", "CANCELLED"),), runs=runs)
        self.assertEqual(started(cards.plan(cards=[], open_pulls=[p], worker_pulls=[p], running={}, rules=RULES,
                                            required=REQUIRED)[0]), [])
        launches, _ = cards.plan(cards=[], open_pulls=[p], worker_pulls=[p], running={}, rules=RULES,
                                 required=REQUIRED, reran=frozenset({(101, "h1")}))
        self.assertEqual(launches[0].reason, "required checks cancelled or timed out again after a rerun")
        moved = cards.plan(cards=[], open_pulls=[p], worker_pulls=[p], running={}, rules=RULES, required=REQUIRED,
                           reran=frozenset({(101, "h0")}))[0]
        self.assertEqual(started(moved), [], "a rerun of an older head does not count for this one")

    def test_changes_requested_wins_the_reason_when_the_head_is_also_red(self) -> None:
        launches, _ = plan([], worker_pulls=[pull(101, 1, checks=RED, reviews=[review("CHANGES_REQUESTED")])])
        self.assertEqual(launches[0].reason, "changes requested")


class ParkedTests(unittest.TestCase):
    def test_a_parked_card_is_not_started_and_the_next_one_is(self) -> None:
        launches, notes = plan([card(1, "kind/bug"), card(2)], parked={1})
        self.assertEqual(started(launches), [(2, None)])
        self.assertIn("#1 is parked after runs with no new commit — left for a person", notes)

    def test_a_parked_card_is_not_sent_back_for_rework_but_still_counts_as_in_flight(self) -> None:
        sent_back = pull(101, 1, reviews=[review("CHANGES_REQUESTED")])
        rules = cards.Rules(1, "lunate-worker", "lunate-reviewer", "kind/bug", "area/")
        launches, notes = plan([card(5)], worker_pulls=[sent_back], parked={1}, rules=rules)
        self.assertEqual(started(launches), [])
        self.assertIn("#101 needs rework (changes requested) but card #1 is parked — left for a person", notes)
        self.assertIn("WIP 1 of 1 — no new card", notes)


if __name__ == "__main__":
    unittest.main()
