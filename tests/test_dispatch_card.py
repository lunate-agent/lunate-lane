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

    def test_a_card_with_no_branch_is_refused_before_the_issue_is_even_read(self) -> None:
        reads = []
        dispatch_card.gh = lambda *a, **k: reads.append(a) or self._issue()
        touched = []
        open_pr.open_pr = lambda *a, **k: touched.append(1) or {}
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.dispatch(self.repo, 794)
        self.assertIn("no local branch", str(caught.exception))
        self.assertEqual(touched, [], "no pull request may be opened without a branch to push")
        self.assertEqual(reads, [], "the branch is checked before the issue is read")

    def test_a_card_whose_branch_exists_becomes_a_pull_request(self) -> None:
        dispatch_card.gh = lambda *a, **k: self._issue()
        subprocess.run(["git", "switch", "-q", "-c", "wt/issue-794"], cwd=self._tmp, capture_output=True)
        (self.repo / "work.txt").write_text("the worker's work\n")
        subprocess.run(["git", "add", "work.txt"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "the worker's work"], cwd=self._tmp, capture_output=True)
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
        self.assertEqual(seen["title"], "Per-PR changelog fragments")


class GhWrapperTests(unittest.TestCase):
    """The real read path: a non-zero gh, an unparseable answer, a card with no title, and main()'s exit."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="lane-gh-")
        self.repo = pathlib.Path(self._tmp)
        self.saved = dispatch_card.subprocess.run

    def tearDown(self) -> None:
        dispatch_card.subprocess.run = self.saved
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _result(self, stdout="", stderr="", returncode=0):
        class R:
            pass

        r = R()
        r.stdout, r.stderr, r.returncode = stdout, stderr, returncode
        return r

    def test_a_non_zero_gh_becomes_a_card_error_with_its_message(self) -> None:
        dispatch_card.subprocess.run = lambda *a, **k: self._result(stderr="not authenticated", returncode=1)
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.gh("issue", "view", "794", repo=self.repo)
        self.assertIn("not authenticated", str(caught.exception))

    def test_gh_missing_becomes_a_card_error_not_a_traceback(self) -> None:
        def missing(*a, **k):
            raise FileNotFoundError("gh")

        dispatch_card.subprocess.run = missing
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.gh("issue", "view", "794", repo=self.repo)
        self.assertIn("gh could not be run", str(caught.exception))

    def test_a_timeout_becomes_a_card_error(self) -> None:
        def slow(*a, **k):
            raise subprocess.TimeoutExpired("gh", 120)

        dispatch_card.subprocess.run = slow
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.gh("issue", "view", "794", repo=self.repo)
        self.assertIn("did not finish", str(caught.exception))

    def test_unparseable_json_is_a_card_error(self) -> None:
        dispatch_card.subprocess.run = lambda *a, **k: self._result(stdout="{not json")
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.card(self.repo, 794)
        self.assertIn("could not be parsed", str(caught.exception))

    def test_a_card_with_no_title_is_refused(self) -> None:
        dispatch_card.subprocess.run = lambda *a, **k: self._result(
            stdout=json.dumps({"number": 794, "state": "OPEN", "title": "", "milestone": None, "body": ""}))
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.card(self.repo, 794)
        self.assertIn("no title", str(caught.exception))

    def test_main_returns_one_and_names_the_failure(self) -> None:
        import contextlib
        import io

        dispatch_card.subprocess.run = lambda *a, **k: self._result(stderr="boom", returncode=1)
        sys.argv = ["dispatch_card.py", "--repo", self._tmp, "--issue", "794"]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = dispatch_card.main()
        self.assertEqual(code, 1)
        self.assertIn("FAILED —", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
