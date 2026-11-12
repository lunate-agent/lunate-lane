"""The worker driver against real temporary repositories; GitHub and the session itself are replaced."""

import io
import json
import os
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
        with self.assertRaises(worker.WorktreeError):
            worker.prepare_worktree(self.cfg, 5)

    def test_a_free_local_branch_is_checked_out(self) -> None:
        git(self.clone, "branch", "wt/issue-8", "origin/main")
        path = worker.prepare_worktree(self.cfg, 8)
        self.assertEqual(git(path, "rev-parse", "--abbrev-ref", "HEAD"), "wt/issue-8")

    def test_a_branch_checked_out_in_another_worktree_is_a_named_refusal_not_gits_128(self) -> None:
        """#4: the old lane's `<checkout>/.worktrees/issue-N` holds the branch."""
        holder = self.clone / ".worktrees" / "issue-794"
        git(self.clone, "worktree", "add", "-q", "-b", "wt/issue-794", str(holder), "origin/main")
        with self.assertRaises(worker.WorktreeError) as caught:
            worker.prepare_worktree(self.cfg, 794)
        message = str(caught.exception)
        self.assertIn(str(holder.resolve()), message)
        self.assertIn("already checked out", message)
        self.assertNotIn("fatal", message)
        self.assertNotIn("128", message)
        self.assertFalse((self.cfg.state / "worktrees" / "issue-794").exists())

    def test_a_worktree_whose_directory_is_gone_does_not_hold_its_branch(self) -> None:
        import shutil

        holder = self.clone / ".worktrees" / "issue-9"
        git(self.clone, "worktree", "add", "-q", "-b", "wt/issue-9", str(holder), "origin/main")
        shutil.rmtree(holder)
        path = worker.prepare_worktree(self.cfg, 9)
        self.assertEqual(git(path, "rev-parse", "--abbrev-ref", "HEAD"), "wt/issue-9")

    def test_a_directory_in_the_way_that_is_not_a_worktree_is_refused(self) -> None:
        (self.cfg.state / "worktrees" / "issue-10").mkdir(parents=True)
        with self.assertRaises(worker.WorktreeError) as caught:
            worker.prepare_worktree(self.cfg, 10)
        self.assertIn("is not a worktree", str(caught.exception))

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

    def test_the_brief_carries_the_spec_conflict_rule_and_asks_for_intent_decisions(self) -> None:
        text = worker.brief(self.cfg, ISSUE, pathlib.Path("/w"), None, "", True)
        self.assertIn("is never resolved by hand", text)
        self.assertIn("git revert", text)
        self.assertIn("git merge origin/main", text)
        self.assertIn("openspec archive <change> --yes", text)
        self.assertIn("grill-me", text)
        self.assertIn("**Intent decisions**", text)

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
            mock.patch.object(harness, "read_credential", return_value="model-credential-value-1234"),
            mock.patch.object(open_pr, "return_to_draft", side_effect=lambda branch, pr: self.drafted.append(pr)),
            mock.patch.object(open_pr, "_worker_token", side_effect=AssertionError("no live token in tests")),
        ]
        self.drafted = []
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

    def test_an_unclean_rework_on_an_armed_pull_request_leaves_it_disarmed(self) -> None:
        """Through the real dispatch and open_pr: only GitHub (a stateful fake) and the push are replaced. The
        rework returns the ready, armed pull request to draft before the session (#5), and it stays one."""
        armed, draft = {"N77": True}, {"N77": False}
        pushed = []

        def graphql(token, query, variables, what):
            node = variables["id"]
            if "disablePullRequestAutoMerge" in query:
                armed[node] = False
                return {}
            if "convertPullRequestToDraft" in query:
                draft[node] = True
                return {}
            if "enablePullRequestAutoMerge" in query or "markPullRequestReadyForReview" in query:
                raise AssertionError("an unclean rework must never be made ready or armed")
            return {"node": {"number": 77, "isDraft": draft[node],
                             "autoMergeRequest": {"enabledAt": "t"} if armed[node] else None}}

        def api(token, method, path, payload=None):
            if method == "GET":
                return [{"number": 77, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN}, "node_id": "N77"}]
            raise AssertionError(f"unexpected {method} {path}")

        for index in (5, 4, 1):  # the real dispatch, return_to_draft and token path from here on
            self.patches[index].stop()
            del self.patches[index]
        sha = self.push_branch("wt/issue-3", "first.cs")
        with self.fake_session(stalled=True), \
                mock.patch.object(open_pr, "_graphql", side_effect=graphql), \
                mock.patch.object(open_pr, "_api", side_effect=api), \
                mock.patch.object(open_pr, "_worker_token", return_value="t"), \
                mock.patch.object(open_pr, "push", side_effect=lambda repo, branch, token:
                                  pushed.append((armed["N77"], draft["N77"]))), \
                mock.patch.object(github, "pull", return_value=pull(77, 3, head=sha,
                                                                    reviews=[review("CHANGES_REQUESTED", commit=sha)])):
            self.assertEqual(worker.drive(self.cfg, 3, 77), 1)
        self.assertFalse(armed["N77"])
        self.assertTrue(draft["N77"])
        self.assertEqual(pushed, [(False, True)], "the push happened only after it was a draft with auto-merge off")
        self.assertIn("auto-merge is not armed", self.comments[0][1])
        self.assertIn("auto-merge not armed", (self.cfg.state / "logs" / "worker-3.log").read_text())

    def test_a_token_in_the_final_message_is_withheld_and_logged(self) -> None:
        token = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
        for text, kind in ((f"done, and here is {token}", "GitHub token"),
                           ("my key is model-credential-value-1234", "configured credential")):
            with self.subTest(kind=kind):
                self.comments.clear()
                with self.fake_session(text=text):
                    worker.drive(self.cfg, 3, None)
                body = self.comments[0][1]
                self.assertIn("Output withheld", body)
                self.assertIn(kind, body)
                self.assertNotIn(token, body)
                self.assertNotIn("model-credential-value-1234", body)
                log = (self.cfg.state / "logs" / "worker-3.log").read_text()
                self.assertIn(f"shaped like a {kind} — not posted", log)
                self.assertNotIn(token, log)

    def test_three_runs_without_a_new_commit_park_the_card_once_and_say_so_on_the_issue(self) -> None:
        for run in range(1, 5):
            with self.fake_session(commit=False):
                self.assertEqual(worker.drive(self.cfg, 3, None), 1)
            self.assertEqual(harness.parked_cards(), frozenset({3}) if run >= 3 else frozenset())
        self.assertEqual(len(self.comments), 1, "one parking comment, not one per idle run")
        target, body = self.comments[0]
        self.assertEqual(target, 3, "no pull request exists, so the issue gets the comment")
        self.assertIn("card #3 is parked", body)
        self.assertIn("attempts/3.json", body)
        self.assertIn("parked after 3 idle runs", (self.cfg.state / "logs" / "worker-3.log").read_text())

    def test_a_rework_returns_its_pull_request_to_draft_before_the_session(self) -> None:
        sha = self.push_branch("wt/issue-3", "first.cs")
        order = []
        open_pr.return_to_draft.side_effect = lambda branch, pr: order.append(("draft", branch, pr))
        with mock.patch.object(github, "pull", return_value=pull(77, 3, head=sha,
                                                                 reviews=[review("CHANGES_REQUESTED", commit=sha)])):
            with self.fake_session() as session:
                session.side_effect, real = None, session.side_effect
                session.side_effect = lambda *a, **k: order.append(("session",)) or real(*a, **k)
                worker.drive(self.cfg, 3, 77)
        self.assertEqual(order[:2], [("draft", "wt/issue-3", 77), ("session",)])
        self.assertIn("#77 is a draft again", (self.cfg.state / "logs" / "worker-3.log").read_text())

    def test_a_rework_that_keeps_adding_nothing_is_parked_on_its_pull_request(self) -> None:
        sha = self.push_branch("wt/issue-3", "first.cs")
        with mock.patch.object(github, "pull", return_value=pull(77, 3, head=sha,
                                                                 reviews=[review("CHANGES_REQUESTED", commit=sha)])):
            for _ in range(3):
                with self.fake_session(commit=False):
                    worker.drive(self.cfg, 3, 77)
        parking = [c for c in self.comments if "is parked" in c[1]]
        self.assertEqual([c[0] for c in parking], [77])

    def test_a_new_commit_resets_the_idle_count(self) -> None:
        for commit in (False, False, True, False, False):
            with self.fake_session(commit=commit):
                worker.drive(self.cfg, 3, None)
        self.assertEqual(harness.parked_cards(), frozenset())
        self.assertEqual(harness.note_run(3, False), (3, True))

    def test_the_session_group_is_recorded_for_clean_up(self) -> None:
        harness.record_worker(3, os.getpid(), frozenset(), None)

        def run(cfg, *, on_start, log_path, **_):
            on_start(4242, "issue-3-20261112-120000-1")
            log_path.write_text("")
            return harness.Outcome(0, False, 1, False)

        with mock.patch.object(harness, "run_session", side_effect=run):
            worker.drive(self.cfg, 3, None)
        record = json.loads((self.cfg.state / "workers" / "3.json").read_text())
        self.assertEqual((record["session_pgid"], record["session_id"]), (4242, "issue-3-20261112-120000-1"))

    def test_a_closed_issue_is_never_worked(self) -> None:
        dispatch_card.card.side_effect = dispatch_card.CardError("issue #3 is CLOSED, not open")
        with self.fake_session() as session:
            with self.assertRaises(dispatch_card.CardError):
                worker.drive(self.cfg, 3, None)
        session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
