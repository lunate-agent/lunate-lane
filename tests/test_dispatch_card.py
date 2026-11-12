"""A card becomes a pull request, or the reason is named. No network, no gh."""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import dispatch_card, open_pr  # noqa: E402


class CardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="lane-card-")
        self.repo = pathlib.Path(self._tmp)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "config", "user.email", "lane@example.invalid"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "config", "user.name", "lane test"], cwd=self._tmp, capture_output=True)
        (self.repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=self._tmp, capture_output=True)
        self.saved = (dispatch_card.gh, open_pr.open_pr)

    def tearDown(self) -> None:
        dispatch_card.gh, open_pr.open_pr = self.saved
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _issue(self, **over):
        data = {"number": 794, "title": "Per-PR changelog fragments", "state": "OPEN",
                "labels": [], "milestone": {"title": "0.4.0"}, "body": "## What\nfragments"}
        data.update(over)
        return json.dumps(data)

    def test_an_unreadable_issue_is_a_named_failure_not_an_empty_card(self) -> None:
        def boom(*a, **k):
            raise dispatch_card.CardError("gh issue view failed: not authenticated")

        dispatch_card.gh = boom
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.card(self.repo, 794)
        self.assertIn("not authenticated", str(caught.exception))

    def test_a_closed_issue_is_refused(self) -> None:
        dispatch_card.gh = lambda *a, **k: self._issue(state="CLOSED")
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.card(self.repo, 794)
        self.assertIn("not open", str(caught.exception))

    def test_a_card_with_no_branch_is_refused_before_github_is_touched(self) -> None:
        dispatch_card.gh = lambda *a, **k: self._issue()
        touched = []
        open_pr.open_pr = lambda *a, **k: touched.append(1) or {}
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.dispatch(self.repo, 794)
        self.assertIn("has no branch", str(caught.exception))
        self.assertEqual(touched, [], "no pull request may be opened without a branch to push")

    def test_a_card_whose_branch_exists_becomes_a_pull_request(self) -> None:
        dispatch_card.gh = lambda *a, **k: self._issue()
        subprocess.run(["git", "switch", "-q", "-c", "wt/issue-794"], cwd=self._tmp, capture_output=True)
        seen = {}

        def fake_open(repo, branch, title, body, base="main"):
            seen.update(branch=branch, title=title, body=body)
            return {"number": 808, "url": "u", "author": open_pr.WORKER_LOGIN, "auto_merge": "armed", "reused": False}

        open_pr.open_pr = fake_open
        result = dispatch_card.dispatch(self.repo, 794)
        self.assertEqual(result["number"], 808)
        self.assertEqual(seen["branch"], "wt/issue-794")
        self.assertIn("Closes #794", seen["body"])
        self.assertIn("milestone 0.4.0", seen["body"])
        self.assertEqual(seen["title"], "Per-PR changelog fragments (#794)")


if __name__ == "__main__":
    unittest.main()
