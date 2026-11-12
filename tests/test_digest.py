"""The daily digest: each pull request's state, what is stuck and why, and failed reads. No network."""

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
CFG = common.load_config()


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
        dropped = pull(8, 12, reviews=[review("APPROVED")], queue_removal="FAILED_CHECKS")
        foreign = pull(9, None, author="someone", mergeable="CONFLICTING")
        text = digest.compose(CFG, [card(1), card(2)], [old, fresh, red, dropped, foreign],
                              [{"number": 801, "title": "Fix the loop"}], REQUIRED, NOW)
        self.assertIn("0.4.0: 2 open issue(s)", text)
        self.assertIn("Open PRs (5):", text)
        self.assertIn("Stuck (3):", text)
        self.assertIn("  #5 — changes requested — open 2d", text)
        self.assertIn("  #7 — required checks failing", text)
        self.assertIn("  #8 — out of the merge queue after approval (failed_checks)", text)
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
