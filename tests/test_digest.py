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


class ComposeTests(unittest.TestCase):
    def test_the_message_names_stuck_pull_requests_and_why(self) -> None:
        old = pull(5, 9, created="2026-11-10T08:00:00Z", reviews=[review("CHANGES_REQUESTED")])
        fresh = pull(6, 10, created="2026-11-12T08:00:00Z", auto_merge=True)
        text = digest.compose(CFG, [card(1), card(2)], [old, fresh], [{"number": 801}], REQUIRED, NOW)
        self.assertIn("0.4.0: 2 open issue(s)", text)
        self.assertIn("Open PRs (2):", text)
        self.assertIn("Merged in the last 24h (1): #801", text)
        self.assertIn("Stuck more than a day (1):", text)
        self.assertIn("#5 open 2d — changes requested", text)
        self.assertNotIn("#6 open", text)

    def test_a_quiet_day_says_so(self) -> None:
        text = digest.compose(CFG, [], [], [], REQUIRED, NOW)
        self.assertIn("Open PRs: none", text)
        self.assertIn("Merged in the last 24h (0): none", text)
        self.assertIn("Stuck more than a day: none", text)


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
