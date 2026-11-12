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
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                       cwd=self._tmp, capture_output=True)
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


class AheadAndBodyTests(unittest.TestCase):
    """The ahead check refuses to decide when it cannot read, and the body defuses every closing keyword."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="lane-ahead-")
        self.repo = pathlib.Path(self._tmp)
        for cmd in (["git", "init", "-q", "-b", "main"],
                    ["git", "config", "user.email", "lane@example.invalid"],
                    ["git", "config", "user.name", "lane test"]):
            subprocess.run(cmd, cwd=self._tmp, capture_output=True)
        (self.repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=self._tmp, capture_output=True)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_missing_base_ref_is_refused_rather_than_read_as_zero(self) -> None:
        subprocess.run(["git", "switch", "-q", "-c", "wt/issue-1"], cwd=self._tmp, capture_output=True)
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.branch_for(self.repo, 1)
        self.assertIn("could not count commits ahead", str(caught.exception))
        self.assertNotIn("no commits ahead", str(caught.exception))

    def test_a_branch_with_nothing_ahead_of_the_base_is_refused(self) -> None:
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "switch", "-q", "-c", "wt/issue-1"], cwd=self._tmp, capture_output=True)
        with self.assertRaises(dispatch_card.CardError) as caught:
            dispatch_card.branch_for(self.repo, 1)
        self.assertIn("no commits ahead", str(caught.exception))

    def test_every_closing_keyword_is_defused_whatever_its_case(self) -> None:
        text = ("Fixes #3 and closes #4, Resolves lunate-agent/lunate#5, fix #6, resolved #7, "
                "plus a normal #8 mention")
        body = dispatch_card.body_for({"number": 1, "body": text, "milestone": None})
        for leaked in ("Fixes #3", "closes #4", "Resolves lunate-agent/lunate#5", "fix #6", "resolved #7"):
            self.assertNotIn(leaked, body, f"{leaked!r} would close an issue on merge")
        self.assertIn("Closes #1", body)
        self.assertIn("a normal #8 mention", body)

    def test_a_long_issue_body_is_truncated(self) -> None:
        body = dispatch_card.body_for({"number": 1, "body": "x" * 9000, "milestone": None})
        self.assertLess(len(body), 8300)
        self.assertIn("truncated", body)


class GhWrapperTests(unittest.TestCase):
    """The real read path: a non-zero gh, an unparseable answer, a card with no title, and main()'s exit."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="lane-gh-")
        self.repo = pathlib.Path(self._tmp)
        self.saved = dispatch_card.subprocess.run
        # a repository with a seed commit and a base ref, like the real checkout
        for cmd in (["git", "init", "-q", "-b", "main"],
                    ["git", "config", "user.email", "lane@example.invalid"],
                    ["git", "config", "user.name", "lane test"]):
            subprocess.run(cmd, cwd=self._tmp, capture_output=True)
        (self.repo / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                       cwd=self._tmp, capture_output=True)

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
        """The whole way through: a real branch, then gh failing, and main() reporting it by name."""
        import contextlib
        import io
        from unittest import mock

        saved = sys.argv
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                       cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "switch", "-q", "-c", "wt/issue-794"], cwd=self._tmp, capture_output=True)
        (self.repo / "work.txt").write_text("work\n")
        subprocess.run(["git", "add", "work.txt"], cwd=self._tmp, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "work"], cwd=self._tmp, capture_output=True)

        real_run = subprocess.run

        def gh_fails(argv_list, **kwargs):
            if argv_list and argv_list[0] == "gh":
                return self._result(stderr="not authenticated", returncode=1)
            return real_run(argv_list, **kwargs)

        buffer = io.StringIO()
        sys.argv = ["dispatch_card.py", "--repo", self._tmp, "--issue", "794"]
        try:
            with mock.patch.object(dispatch_card.subprocess, "run", side_effect=gh_fails):
                with contextlib.redirect_stdout(buffer):
                    code = dispatch_card.main()
        finally:
            sys.argv = saved
        self.assertEqual(code, 1)
        self.assertIn("FAILED —", buffer.getvalue())
        self.assertIn("not authenticated", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
