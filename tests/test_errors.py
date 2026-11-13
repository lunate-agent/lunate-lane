"""#14: every file or process call that can fail raises the module's named error, so run_job logs it.

Each failure is real — a file where a directory should be, a read-only folder, a missing repository — except
where noted.
"""

import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, digest, github, harness, open_pr, review, worker  # noqa: E402
from scripts.common import LaneError, StateError  # noqa: E402
from tests.helpers import REQUIRED, TempState, git, origin_and_clone, pull  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg
        self.addCleanup(self.state.close)
        out = mock.patch("sys.stdout", new=io.StringIO())
        self.stdout = out.start()
        self.addCleanup(out.stop)

    def read_only(self, folder: pathlib.Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o500)
        self.addCleanup(folder.chmod, 0o755)


class CommonTests(Case):
    def test_an_unwritable_log_is_a_state_error(self) -> None:
        self.read_only(self.cfg.state / "logs")
        with self.assertRaises(StateError) as caught:
            common.log("feed", "hello")
        self.assertIn("feed.log could not be written", str(caught.exception))

    def test_a_job_whose_log_cannot_be_written_still_exits_non_zero_without_a_traceback(self) -> None:
        self.read_only(self.cfg.state / "logs")
        err = io.StringIO()
        with mock.patch("sys.stderr", new=err):
            rc = common.run_job("feed", lambda: (_ for _ in ()).throw(common.GhError("HTTP 502")))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED — HTTP 502", err.getvalue())
        self.assertIn("the log could not be written", err.getvalue())

    def test_an_unopenable_lock_is_a_state_error(self) -> None:
        locks = self.cfg.state / "locks"
        locks.mkdir(parents=True)
        (locks / "feed.lock").mkdir()  # a directory where the lock file goes
        with self.assertRaises(StateError):
            with common.job_lock("feed"):
                pass


class HarnessTests(Case):
    def test_a_scratch_that_is_a_file_is_a_harness_error(self) -> None:
        scratch = self.state.root / "scratch"
        scratch.write_text("not a folder")
        with mock.patch.object(harness, "read_credential", return_value="k"):
            with self.assertRaises(harness.HarnessError) as caught:
                harness.session_env(self.cfg, scratch)
        self.assertIn("could not be prepared", str(caught.exception))

    def test_a_session_log_that_cannot_be_opened_is_a_harness_error(self) -> None:
        (self.state.root / "logs-file").write_text("x")
        worktree = self.state.root / "wt"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.cfg.checkout)], check=True)
        with mock.patch.object(harness, "read_credential", return_value="k"), \
                mock.patch.object(harness, "_git_identity", return_value={}):
            with self.assertRaises(harness.HarnessError) as caught:
                harness.run_session(self.cfg, model="p/m", worktree=worktree, prompt="x",
                                    scratch=self.state.root / "scratch", log_path=self.state.root / "logs-file" / "s.log")
        self.assertIn("could not be opened", str(caught.exception))

    def test_an_unwritable_git_identity_is_a_harness_error(self) -> None:
        """Mocked: the home is created fresh right before, so the write is made to fail directly."""
        real_write = pathlib.Path.write_text

        def refuse(path, *a, **k):
            if path.name == ".gitconfig":
                raise PermissionError(13, "Permission denied")
            return real_write(path, *a, **k)

        with mock.patch.object(harness, "_git_identity", return_value={"name": "n"}), \
                mock.patch.object(pathlib.Path, "write_text", refuse):
            with self.assertRaises(harness.HarnessError):
                harness.session_home(self.cfg, self.state.root / "scratch", self.state.root / "home")

    def test_a_dead_workers_record_that_cannot_be_removed_is_a_state_error(self) -> None:
        harness.record_worker(5, 999999, frozenset(), None)
        self.read_only(self.cfg.state / "workers")
        with self.assertRaises(StateError) as caught:
            harness.running_workers()
        self.assertIn("could not be removed", str(caught.exception))


class OpenPrTests(Case):
    def test_a_repository_that_is_gone_is_an_open_pr_error(self) -> None:
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.push(self.state.root / "gone", "wt/issue-1", "t")
        self.assertIn("git could not check", str(caught.exception))

    def test_an_unmakeable_push_mirror_is_an_open_pr_error(self) -> None:
        _, clone = origin_and_clone(self.state.root)
        git(clone, "branch", "wt/issue-1")
        (self.cfg.state / "push-mirror.git").parent.mkdir(parents=True, exist_ok=True)
        (self.cfg.state / "push-mirror.git").write_text("a file where the mirror goes")
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.push(clone, "wt/issue-1", "t", url=f"file://{self.state.root}/nowhere.git")
        self.assertIn("push mirror", str(caught.exception))

    def test_a_missing_body_file_is_a_named_failure_from_main(self) -> None:
        argv = ["open_pr.py", "--branch", "b", "--title", "t", "--body-file", str(self.state.root / "none.md")]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(open_pr.main(), 1)
        self.assertIn("FAILED — the body file", self.stdout.getvalue())


class DigestAndWorkerTests(Case):
    def test_an_unwritable_digest_is_a_named_failure(self) -> None:
        (self.cfg.state / "digest" / "latest.md").mkdir(parents=True)
        with mock.patch.object(github, "required_checks", return_value=REQUIRED), \
                mock.patch.object(github, "milestone_cards", return_value=[]), \
                mock.patch.object(github, "open_pulls", return_value=[]), \
                mock.patch.object(github, "decision_issues", return_value=[]), \
                mock.patch.object(digest, "merged_since", return_value=[]):
            self.assertEqual(digest.main(), 1)
        self.assertIn("FAILED — the digest", self.stdout.getvalue())

    def test_an_unwritable_pull_request_number_file_is_a_state_error(self) -> None:
        _, clone = origin_and_clone(self.state.root)
        (self.cfg.state / "scratch" / "issue-3" / "pr-number").mkdir(parents=True)
        with mock.patch.object(worker.dispatch_card, "card", return_value={"number": 3, "title": "t", "body": ""}):
            with self.assertRaises(StateError) as caught:
                worker.drive(self.cfg, 3, None)
        self.assertIn("pr-number", str(caught.exception))


class ReviewTests(Case):
    def test_an_unwritable_diff_is_a_review_error_and_the_worktree_is_still_removed(self) -> None:
        _, clone = origin_and_clone(self.state.root)
        (self.cfg.state / "scratch" / "review-5" / "diff.patch").mkdir(parents=True)
        head = git(clone, "rev-parse", "origin/main")
        with self.assertRaises(review.ReviewError) as caught:
            review.run_review(self.cfg, pull(5, 9, head=head))
        self.assertIn("diff", str(caught.exception))
        self.assertFalse((self.cfg.state / "reviews" / "pr-5").exists())
        self.assertTrue(issubclass(review.ReviewError, LaneError))


if __name__ == "__main__":
    unittest.main()
