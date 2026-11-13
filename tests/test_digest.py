"""The daily digest: each pull request's state, what is stuck and why, and failed reads. No network."""

import dataclasses
import datetime
import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, digest, github  # noqa: E402
from scripts.common import GhError  # noqa: E402
from tests.helpers import REQUIRED, TempState, card, pull, review  # noqa: E402

NOW = datetime.datetime(2026, 11, 12, 12, 0, tzinfo=datetime.timezone.utc)
CFG = dataclasses.replace(common.load_config(), wip_limit=5)  # the tests pin their own limit, not lane.json's


def state(p):
    return digest.pull_state(p, REQUIRED, "lunate-reviewer")


class StateTests(unittest.TestCase):
    def test_each_state_reads_from_github_alone(self) -> None:
        self.assertEqual(state(pull(1, 1, draft=True)), "draft")
        self.assertEqual(state(pull(1, 1, checks=[("ci-ok", "COMPLETED", "FAILURE")])), "required checks failing")
        self.assertEqual(state(pull(1, 1, mergeable="CONFLICTING")), "merge conflict")
        self.assertEqual(state(pull(1, 1, checks=[])), "checks running")
        self.assertEqual(state(pull(1, 1, reviews=[review("CHANGES_REQUESTED")])), "changes requested")
        self.assertEqual(state(pull(1, 1, auto_merge=True, reviews=[review("APPROVED")])), "approved, auto-merge armed")
        self.assertEqual(state(pull(1, 1, reviews=[review("APPROVED")])), "approved, auto-merge NOT armed")
        self.assertEqual(state(pull(1, 1, auto_merge=True)), "awaiting review")
        self.assertEqual(state(pull(1, 1, reviews=[review("APPROVED")], disarmed_by="a-maintainer")),
                         "approved, auto-merge turned off by @a-maintainer")


class ComposeTests(unittest.TestCase):
    def test_the_message_names_stuck_pull_requests_and_why(self) -> None:
        old = pull(5, 9, created="2026-11-10T08:00:00Z", reviews=[review("CHANGES_REQUESTED")])
        fresh = pull(6, 10, created="2026-11-12T08:00:00Z", auto_merge=True)
        red = pull(7, 11, checks=[("ci-ok", "COMPLETED", "FAILURE")])
        real = github.Ejection("real", "failed_checks", "t", "w", 1, "verify (macos-latest, 2/4)", 2, ("Verify (bash)",))
        dropped = pull(8, 12, reviews=[review("APPROVED")], queue_removal="failed_checks", ejection=real)
        foreign = pull(9, None, author="someone", mergeable="CONFLICTING")
        text = digest.compose(CFG, [card(1), card(2)], [old, fresh, red, dropped, foreign],
                              [{"number": 801, "title": "Fix the loop"}], REQUIRED, NOW)
        self.assertIn("0.4.0: 2 open issue(s)", text)
        self.assertIn("Open PRs (5):", text)
        self.assertIn("Stuck (3):", text)
        self.assertIn("  #5 — changes requested — open 2d", text)
        self.assertIn("  #7 — required checks failing", text)
        self.assertIn("  #8 — queue failure: verify (macos-latest, 2/4)", text)
        self.assertNotIn("  #6 —", text)
        self.assertNotIn("  #9 —", text, "another author's fresh conflict is not the lane's to call stuck")
        self.assertIn("Merged since yesterday (1):\n  #801 Fix the loop", text)

    def test_wip_counts_worker_pull_requests_and_running_workers_once_each(self) -> None:
        pulls = [pull(5, 9), pull(6, 10), pull(7, 11, author="someone")]
        text = digest.compose(CFG, [], pulls, [], REQUIRED, NOW, {}, {10: frozenset(), 12: frozenset()})
        self.assertIn("WIP 3 of 5: #5 (card #9), #6 (card #10, worker running), card #12 (worker running, no PR yet)",
                      text)

    def test_parked_cards_are_listed_with_why(self) -> None:
        text = digest.compose(CFG, [], [], [], REQUIRED, NOW, {9: "reworks", 3: "idle", 4: "secret"})
        self.assertIn("Parked cards (3)", text)
        self.assertIn("#3 — runs with no new commit", text)
        self.assertIn("#4 — secret-shaped text in a push", text)
        self.assertIn("#9 — rework rounds used up", text)
        self.assertLess(text.index("#3 —"), text.index("#9 —"))

    def test_a_quiet_day_says_so(self) -> None:
        text = digest.compose(CFG, [], [], [], REQUIRED, NOW)
        for line in ("WIP 0 of 5", "Open PRs: none", "Stuck: none", "Parked cards: none", "Merged since yesterday: none"):
            self.assertIn(line, text)


class ReadTests(unittest.TestCase):
    def test_a_merge_list_that_may_be_truncated_is_refused(self) -> None:
        with mock.patch.object(common, "gh_json", return_value=[{"number": n} for n in range(100)]):
            with self.assertRaises(GhError):
                digest.merged_since(CFG, NOW)

    def test_a_failed_read_fails_the_digest_instead_of_reporting_a_quiet_lane(self) -> None:
        temp = TempState()
        self.addCleanup(temp.close)
        out = io.StringIO()
        with mock.patch.object(github, "required_checks", return_value=REQUIRED), \
                mock.patch.object(github, "milestone_cards", side_effect=GhError("HTTP 502")), \
                mock.patch("sys.stdout", new=out):
            self.assertEqual(digest.main(), 1)
        self.assertNotIn("Open PRs", out.getvalue())
        self.assertIn("FAILED — HTTP 502", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class OrphanTests(unittest.TestCase):
    def test_an_orphaned_draft_is_stuck_by_name_and_a_worked_or_parked_draft_is_not(self) -> None:
        orphan, worked, parked = (pull(816, 780, draft=True), pull(815, 804, draft=True), pull(813, 794, draft=True))
        text = digest.compose(CFG, [], [orphan, worked, parked], [], REQUIRED, NOW, {794: "reworks"},
                              {804: frozenset()})
        self.assertIn("Stuck (1):", text)
        self.assertIn("  #816 — orphaned draft — no worker on it; the feeder relaunches it", text)
        self.assertNotIn("  #815 — orphaned", text)
        self.assertNotIn("  #813 — orphaned", text)


class ParkedReasonTests(unittest.TestCase):
    def test_a_person_or_ready_park_shows_its_words(self) -> None:
        text = digest.compose(CFG, [], [], [], REQUIRED, NOW, {
            794: ("reworks", ""), 780: ("person", "check 2 needs a maintainer decision"),
            804: ("ready", "#815: the ready CI run on f9cb14ed9 was cancelled again after one draft/ready toggle")})
        self.assertIn("#780 — needs a person: check 2 needs a maintainer decision", text)
        self.assertIn("#804 — CI never ran it as ready: #815: the ready CI run on f9cb14ed9 was cancelled", text)
        self.assertIn("#794 — rework rounds used up", text)


class WaitingOnYouTests(unittest.TestCase):
    """3: the top of the digest is what a maintainer acts on, short enough for a phone."""

    def test_decisions_person_parks_approvals_and_an_open_card_after_its_merge(self) -> None:
        decisions = [github.Card(820, "Should release PRs be opened by the lane or by hand?", frozenset(),
                                 "2026-11-01T00:00:00Z", blocking=(794, 801)),
                     github.Card(822, "Pick the cache prune default", frozenset(), "2026-11-05T00:00:00Z")]
        # #815: the bot approved its head, but the ruleset wants a code owner too
        waiting = pull(815, 804, title="Add .github/CODEOWNERS so the ruleset can require a code-owner review",
                       reviews=[review("APPROVED")], review_decision="REVIEW_REQUIRED")
        not_yet = pull(816, 780, review_decision="REVIEW_REQUIRED")  # the bot has not reviewed it: not on you
        person = pull(817, 790)
        merged = [{"number": 813, "title": "Per-PR changelog fragments",
                   "closingIssuesReferences": [{"number": 794}]}]
        text = digest.compose(CFG, [card(794), card(790)], [waiting, not_yet, person], merged, REQUIRED, NOW,
                              {790: ("person", "whether `cache prune` may delete pinned entries"), 11: ("idle", ""),
                               700: ("person", "closed since: waits on nobody")},
                              decisions=decisions)
        lines = text.splitlines()
        self.assertEqual(lines[1], "Waiting on you (7):")
        self.assertEqual(lines[2], "  decide #820 Should release PRs be opened by… — blocks #794, #801")
        self.assertEqual(lines[3], "  decide #822 Pick the cache prune default")
        self.assertEqual(lines[4], "  parked #11: runs with no new commit https://github.com/lunate-agent/lunate/issues/11")
        self.assertEqual(lines[5], "  parked #700: needs a person: closed since: waits on nobody "
                                   "https://github.com/lunate-agent/lunate/issues/700")
        self.assertEqual(lines[6], "  parked #790: needs a person: whether `cache prune` may delete pinne… "
                                   "https://github.com/lunate-agent/lunate/pull/817")
        self.assertEqual(lines[7], "  approve #815 Add .github/CODEOWNERS so the r… "
                                   "https://github.com/lunate-agent/lunate/pull/815")
        self.assertEqual(lines[8], "  close #794 — #813 merged but it is still open")
        self.assertNotIn("#816", "\n".join(lines[:9]))
        bare = [line.split(" https://")[0] for line in lines[:9]]
        self.assertTrue(all(len(line) <= 72 for line in bare), "a phone line, before its link")

    def test_nothing_waiting_is_one_line(self) -> None:
        text = digest.compose(CFG, [], [], [], REQUIRED, NOW, {})
        self.assertEqual(text.splitlines()[1], "Waiting on you: nothing")
        text = digest.compose(CFG, [], [], [], REQUIRED, NOW, {3: ("idle", "")})
        self.assertEqual(text.splitlines()[1:3], ["Waiting on you (1):",
                                                  "  parked #3: runs with no new commit https://github.com/lunate-agent/lunate/issues/3"],
                         "every park is on you, not only a person-park")

    def test_a_parked_card_frees_its_wip_slot_in_the_digest_too(self) -> None:
        text = digest.compose(CFG, [], [pull(813, 794), pull(815, 804)], [], REQUIRED, NOW, {794: ("person", "x")})
        self.assertIn("WIP 1 of 5: #815 (card #804)", text)


class WorkerDiedTests(unittest.TestCase):
    def test_a_worker_that_died_by_name_is_stuck_with_its_reason_until_one_runs_again(self) -> None:
        died = {817: "git -c core.fsmonitor=false failed (128): hint: Diverging branches can't be fast-forwarded"}
        text = digest.compose(CFG, [], [], [], REQUIRED, NOW, {817: ("reworks", "")}, died=died)
        self.assertIn("Stuck (1):", text)
        self.assertIn("  card #817 — worker died: git -c core.fsmonitor=false failed (128): hint: Diverging", text)
        running = digest.compose(CFG, [], [], [], REQUIRED, NOW, {}, {817: frozenset()}, died=died)
        self.assertNotIn("worker died", running, "a worker on it again is not stuck")


class WaitsOnDigestTests(unittest.TestCase):
    def test_a_pull_request_whose_card_waits_says_so(self) -> None:
        self.assertEqual(state(pull(825, 752, draft=True, waits_on=(749,))), "waits on #749")
        self.assertEqual(state(pull(820, 751, auto_merge=True, waits_on=(749, 750))),
                         "waits on #749, #750 (auto-merge still armed)")


class QueueDigestTests(unittest.TestCase):
    def test_the_queue_events_since_yesterday_and_a_pull_request_stuck_in_the_queue(self) -> None:
        infra = github.Ejection("infra", "failed_checks", "2026-11-13T20:00:00Z",
                                "verify (macos-latest, 3/4) cancelled with 0 step(s) in merge_group run 1", 1)
        stuck = pull(824, 3, reviews=[review("APPROVED")], queue_removal="failed_checks", ejection=infra)
        once = pull(829, 4, reviews=[review("APPROVED")], queue_removal="failed_checks",
                    ejection=dataclasses.replace(infra, at="2026-11-13T18:11:07Z"))
        events = ["re-enqueued #829 (infra)", "rework #827 (queue conflict)",
                  "rework #815 (queue failure: verify (macos-latest, 2/4))"]
        removals = ("2026-11-13T18:11:07Z", "2026-11-13T19:00:00Z", "2026-11-13T20:00:00Z")
        text = digest.compose(CFG, [], [stuck, once], [], REQUIRED, NOW, requeued={824: removals, 829: removals[:1]},
                              queue_events=events)
        self.assertIn("Merge queue since yesterday (3):\n  re-enqueued #829 (infra)\n  rework #827 (queue conflict)\n"
                      "  rework #815 (queue failure: verify (macos-latest, 2/4))", text)
        self.assertIn("  #824 — stuck in queue — removed 3 times at h1, re-enqueued 2; last: failed_checks "
                      "(verify (macos-latest, 3/4) cancelled with 0 step(s) in merge_group run 1)", text)
        self.assertIn("  #829 — stuck in queue — the re-enqueue after removal 1 at h1 did not take (see feed.log); "
                      "last: failed_checks", text)
        acted = digest.compose(CFG, [], [once], [], REQUIRED, NOW, requeued={829: ("2026-11-13T17:00:00Z",)})
        self.assertNotIn("  #829 —", acted, "a newer removal not yet acted on is the feeder's, not stuck")
        self.assertIn("Merge queue since yesterday: nothing", digest.compose(CFG, [], [], [], REQUIRED, NOW))

    def test_the_tick_reads_the_events_of_the_last_day(self) -> None:
        state = TempState()
        try:
            from scripts import harness
            harness.note_queue_event("re-enqueued #829 (infra)")
            self.assertEqual(harness.queue_events(0), ["re-enqueued #829 (infra)"])
            self.assertEqual(harness.queue_events(datetime.datetime.now().timestamp() + 60), [])
        finally:
            state.close()
