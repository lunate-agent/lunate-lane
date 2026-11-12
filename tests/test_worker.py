"""The worker driver against real temporary repositories; GitHub and the session itself are replaced."""

import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import dispatch_card, github, harness, identity, open_pr, worker  # noqa: E402
from scripts.common import GitError, LaneError  # noqa: E402
from tests.helpers import TempState, git, origin_and_clone, pull, review  # noqa: E402

ISSUE = {"number": 3, "title": "Fix the thing", "state": "OPEN", "labels": [], "milestone": None,
         "body": "## Acceptance\n1. the {thing} works, even with {{TITLE}} in the text"}


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg
        self.origin, self.clone = origin_and_clone(self.state.root)
        self.out = mock.patch("sys.stdout", new=io.StringIO())
        self.out.start()

    def tearDown(self) -> None:
        self.out.stop()
        self.state.close()

    def push_branch(self, branch: str, filename: str) -> str:
        git(self.clone, "switch", "-q", "-c", branch, "origin/main")
        (self.clone / filename).parent.mkdir(parents=True, exist_ok=True)
        (self.clone / filename).write_text("x\n")
        git(self.clone, "add", filename)
        git(self.clone, "commit", "-q", "-m", f"add {filename}")
        git(self.clone, "push", "-q", "origin", f"{branch}:{branch}")
        sha = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "switch", "-q", "--detach", "origin/main")
        git(self.clone, "branch", "-q", "-D", branch)
        return sha


class WorktreeTests(RepoCase):
    def test_a_new_card_is_cut_from_the_base(self) -> None:
        path = worker.prepare_worktree(self.cfg, 3)
        self.assertEqual(git(path, "rev-parse", "--abbrev-ref", "HEAD"), "wt/issue-3")
        self.assertEqual(git(path, "rev-parse", "HEAD"), git(self.clone, "rev-parse", "origin/main"))
        self.assertTrue(str(path).startswith(str(self.cfg.state)))

    def test_a_card_whose_branch_is_on_the_remote_continues_it(self) -> None:
        sha = self.push_branch("wt/issue-4", "work.txt")
        path = worker.prepare_worktree(self.cfg, 4)
        self.assertEqual(git(path, "rev-parse", "HEAD"), sha)

    def test_a_reused_worktree_is_fast_forwarded_to_the_remote(self) -> None:
        path = worker.prepare_worktree(self.cfg, 4)
        sha = self.push_branch("wt/issue-4-elsewhere", "other.txt")
        git(self.clone, "push", "-q", "origin", f"{sha}:refs/heads/wt/issue-4")
        self.assertEqual(git(worker.prepare_worktree(self.cfg, 4), "rev-parse", "HEAD"), sha)
        self.assertEqual(path, worker.prepare_worktree(self.cfg, 4))

    def test_a_worktree_on_another_branch_is_refused(self) -> None:
        path = self.cfg.state / "worktrees" / "issue-5"
        git(self.clone, "worktree", "add", "-q", "-b", "someone-else", str(path), "origin/main")
        with self.assertRaises(GitError):
            worker.prepare_worktree(self.cfg, 5)

    def test_an_unreachable_checkout_is_a_named_failure(self) -> None:
        import shutil

        shutil.rmtree(self.clone)
        with self.assertRaises(GitError):
            worker.prepare_worktree(self.cfg, 6)

    def test_the_fragment_convention_is_read_from_the_base(self) -> None:
        self.assertFalse(worker.has_fragments(self.cfg, self.clone))
        sha = self.push_branch("tmp", "changes/1.md")
        git(self.clone, "push", "-q", "origin", f"{sha}:refs/heads/main")
        git(self.clone, "fetch", "-q", "origin")
        self.assertTrue(worker.has_fragments(self.cfg, self.clone))


class BriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg

    def tearDown(self) -> None:
        self.state.close()

    def test_a_new_card_is_told_where_its_pull_request_number_will_appear(self) -> None:
        text = worker.brief(self.cfg, ISSUE, pathlib.Path("/w"), None, "", True)
        self.assertIn("$LANE_PR_FILE", text)
        self.assertIn("changes/<the pull request number>.md", text)
        self.assertIn("The archive is the last commit", text)
        self.assertIn("never the full suite", text)
        self.assertIn(ISSUE["body"], text)

    def test_a_rework_names_its_pull_request_fragment_and_findings(self) -> None:
        text = worker.brief(self.cfg, ISSUE, pathlib.Path("/w"), 12, "1. not met — x.cs:3", True)
        self.assertIn("It is #12.", text)
        self.assertIn("`changes/12.md`", text)
        self.assertIn("This is a rework", text)
        self.assertIn("1. not met — x.cs:3", text)

    def test_no_fragment_is_asked_for_before_the_convention_exists(self) -> None:
        text = worker.brief(self.cfg, ISSUE, pathlib.Path("/w"), None, "", False)
        self.assertIn("not on main yet", text)
        self.assertNotIn("changes/<the pull request number>.md", text)

    def test_rework_needs_a_request_for_changes_on_the_current_head(self) -> None:
        with mock.patch.object(github, "pull", return_value=pull(12, 3, head="h2",
                                                                 reviews=[review("CHANGES_REQUESTED", commit="h1")])):
            with self.assertRaises(LaneError):
                worker.rework_findings(self.cfg, 12)
        with mock.patch.object(github, "pull", return_value=pull(12, 3, reviews=[review("CHANGES_REQUESTED", body="b")])):
            self.assertEqual(worker.rework_findings(self.cfg, 12), "b")
        with mock.patch.object(github, "pull", return_value=pull(12, 3, state="MERGED")):
            with self.assertRaises(LaneError):
                worker.rework_findings(self.cfg, 12)


class DriveTests(RepoCase):
    """A whole run: the session commits, the PR opens unarmed at the first commit, then is armed at the end."""

    def setUp(self) -> None:
        super().setUp()
        self.dispatched = []
        self.comments = []
        self.patches = [
            mock.patch.object(dispatch_card, "card", return_value=dict(ISSUE)),
            mock.patch.object(dispatch_card, "dispatch", side_effect=self.dispatch),
            mock.patch.object(open_pr, "comment", side_effect=lambda n, body: self.comments.append((n, body))),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        super().tearDown()

    def dispatch(self, repo, number, arm=True):
        self.dispatched.append(arm)
        return {"number": 77, "url": "u", "author": "lunate-worker[bot]", "reused": False,
                "auto_merge": "armed" if arm else "not armed"}

    def fake_session(self, *, commit=True, dirty=False, rc=0, stalled=False, text="done: 1. met — a.cs:1"):
        def run(cfg, *, worktree, log_path, extra_env, on_tick, **_):
            if commit:
                git(worktree, "config", "user.email", "w@example.invalid")
                git(worktree, "config", "user.name", "w")
                self.runs = getattr(self, "runs", 0) + 1
                (worktree / "fix.cs").write_text(f"fixed {self.runs}\n")
                git(worktree, "add", "fix.cs")
                git(worktree, "commit", "-q", "-m", "fix")
                on_tick()
                seen = pathlib.Path(extra_env["LANE_PR_FILE"])
                self.pr_seen = seen.read_text() if seen.exists() else ""
            if dirty:
                (worktree / "left.txt").write_text("x\n")
            log_path.write_text('{"type":"message_end","message":{"content":[{"type":"text","text":"%s"}]}}\n' % text)
            return harness.Outcome(rc, stalled, 60, False)

        return mock.patch.object(harness, "run_session", side_effect=run)

    def test_a_clean_run_opens_early_unarmed_then_arms_and_reports(self) -> None:
        with self.fake_session():
            self.assertEqual(worker.drive(self.cfg, 3, None), 0)
        self.assertEqual(self.pr_seen, "77")
        self.assertEqual(self.dispatched, [False, True])
        number, body = self.comments[0]
        self.assertEqual(number, 77)
        self.assertIn("done: 1. met — a.cs:1", body)
        self.assertNotIn("Incomplete", body)

    def test_a_run_with_no_commits_opens_nothing(self) -> None:
        with self.fake_session(commit=False):
            self.assertEqual(worker.drive(self.cfg, 3, None), 1)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.comments, [])

    def test_a_dirty_or_failed_run_is_pushed_unarmed_with_the_reasons(self) -> None:
        with self.fake_session(dirty=True, rc=2):
            self.assertEqual(worker.drive(self.cfg, 3, None), 1)
        self.assertEqual(self.dispatched, [False, False])
        body = self.comments[0][1]
        self.assertIn("auto-merge is not armed", body)
        self.assertIn("exited with 2", body)
        self.assertIn("uncommitted changes", body)

    def test_a_stalled_run_is_not_armed(self) -> None:
        with self.fake_session(stalled=True):
            self.assertEqual(worker.drive(self.cfg, 3, None), 1)
        self.assertEqual(self.dispatched[-1], False)
        self.assertIn("stalled", self.comments[0][1])

    def test_a_failed_early_open_does_not_stop_the_session_and_the_end_tries_again(self) -> None:
        """The real exception types dispatch raises: none of them may escape on_tick and kill the session."""
        for error in (open_pr.OpenPrError("GitHub refused POST /pulls (502)"),
                      dispatch_card.CardError("issue #3 is CLOSED, not open"),
                      identity.IdentityError("the token exchange was refused (401)")):
            with self.subTest(error=type(error).__name__):
                calls = []
                self.dispatched.clear()
                self.comments.clear()

                def flaky(repo, number, arm=True, error=error):
                    calls.append(arm)
                    if len(calls) == 1:
                        raise error
                    return self.dispatch(repo, number, arm)

                dispatch_card.dispatch.side_effect = flaky
                with self.fake_session():
                    self.assertEqual(worker.drive(self.cfg, 3, None), 0)
                self.assertEqual(calls, [False, True])
                self.assertEqual(self.pr_seen, "")
                self.assertIn(f"could not open the pull request at the first commit — {error}",
                              (self.cfg.state / "logs" / "worker-3.log").read_text())
                (self.cfg.state / "logs" / "worker-3.log").unlink()

    def test_a_failure_while_finishing_reaches_the_worker_log_not_a_traceback(self) -> None:
        open_pr.comment.side_effect = open_pr.OpenPrError("GitHub refused POST /issues/77/comments (502)")
        with self.fake_session(), mock.patch.object(sys, "argv", ["worker.py", "--issue", "3"]), \
                mock.patch.object(harness, "live_pid", return_value=None):
            self.assertEqual(worker.main(), 1)
        log = (self.cfg.state / "logs" / "worker-3.log").read_text()
        self.assertIn("FAILED — GitHub refused POST /issues/77/comments (502)", log)
        self.assertFalse((self.cfg.state / "workers" / "3.json").exists())

    def test_a_closed_issue_is_never_worked(self) -> None:
        dispatch_card.card.side_effect = dispatch_card.CardError("issue #3 is CLOSED, not open")
        with self.fake_session() as session:
            with self.assertRaises(dispatch_card.CardError):
                worker.drive(self.cfg, 3, None)
        session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
