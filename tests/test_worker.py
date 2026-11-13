"""The worker driver against real temporary repositories; GitHub and the session itself are replaced."""

import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, dispatch_card, github, harness, identity, open_pr, worker  # noqa: E402
from scripts.common import GitError, LaneError  # noqa: E402
from tests.helpers import REQUIRED, TempState, git, origin_and_clone, pull, review  # noqa: E402

RED = (("ci-ok", "COMPLETED", "FAILURE"), ("build-and-test", "COMPLETED", "FAILURE"))
RED_RUNS = (github.Run("ci-ok", "FAILURE", 0, "https://ci/ok", 9, True),
            github.Run("build-and-test", "FAILURE", 4242, "https://ci/4242", 9, True))

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


class DriverGitTests(RepoCase):
    """Review of #17, item 3: config a session could have written makes the driver's git run nothing."""

    def test_fsmonitor_hooks_credential_helpers_and_global_filters_never_run(self) -> None:
        marker = self.state.root / "ran"
        # the branch, with a .gitattributes that names a filter, is made before any config is poisoned
        git(self.clone, "switch", "-q", "-c", "wt/issue-3", "origin/main")
        (self.clone / ".gitattributes").write_text("* filter=evil diff=evil\n")
        (self.clone / "a.txt").write_text("a\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", "attrs")
        git(self.clone, "push", "-q", "origin", "wt/issue-3")
        git(self.clone, "switch", "-q", "--detach", "origin/main")
        git(self.clone, "branch", "-q", "-D", "wt/issue-3")
        evil = self.state.root / "evil.sh"
        evil.write_text(f'#!/bin/sh\nexec </dev/null\necho "$0 $*" >> "{marker}"\nexit 1\n')
        evil.chmod(0o755)
        hooks = self.state.root / "hooks"
        hooks.mkdir()
        for name in ("post-checkout", "post-merge", "reference-transaction"):
            (hooks / name).write_text(f'#!/bin/sh\necho hook >> "{marker}"\n')
            (hooks / name).chmod(0o755)
        config = self.clone / ".git" / "config"
        # in the checkout's config (which the sandbox now keeps sessions from writing): what -c neutralises
        config.write_text(config.read_text() + f'[core]\n\tfsmonitor = {evil}\n\thooksPath = {hooks}\n'
                          f'[credential]\n\thelper = !{evil}\n')
        home = self.state.root / "home"
        home.mkdir()
        # in the user's global config: filters and textconv too, all dropped with the global config
        (home / ".gitconfig").write_text(f'[filter "evil"]\n\tsmudge = {evil}\n\tclean = {evil}\n'
                                         f'[diff "evil"]\n\ttextconv = {evil}\n[core]\n\tfsmonitor = {evil}\n'
                                         f'\thooksPath = {hooks}\n[credential]\n\thelper = !{evil}\n')
        with mock.patch.dict(os.environ, {"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}):
            path = worker.prepare_worktree(self.cfg, 3)
            (path / "a.txt").write_text("changed\n")
            worker.problems(self.cfg, harness.Outcome(0, False, 1, False), path, "x")
            worker.ref_exists(self.clone, "refs/heads/wt/issue-3")
            worker.conflicts(self.cfg, path)
            dispatch_card._count(self.clone, "wt/issue-3")
            common.wt_git(self.cfg, path, "diff", "--no-textconv", "--no-ext-diff", "origin/main...HEAD")
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")
        self.assertEqual((path / "a.txt").read_text(), "changed\n")


class PointerTests(RepoCase):
    """Review of #17, round two, item 2: the driver's git never takes the repository from a worktree's pointer
    files. Each attack is first shown to work on plain git, then shown not to reach the lane's."""

    def evil_repo(self):
        evil = self.state.root / "evil"
        git(self.state.root, "init", "-q", "-b", "main", str(evil))
        for key, value in (("user.email", "e@example.invalid"), ("user.name", "e")):
            git(evil, "config", key, value)
        (evil / "x").write_text("evil\n")
        git(evil, "add", "x")
        git(evil, "commit", "-qm", "evil")
        return evil, git(evil, "rev-parse", "HEAD")

    def plain(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True).stdout.strip()

    def test_a_redirected_gitdir_pointer_does_not_move_the_drivers_git(self) -> None:
        path = worker.prepare_worktree(self.cfg, 3)
        real = common.wt_git(self.cfg, path, "rev-parse", "HEAD")
        evil, evil_head = self.evil_repo()
        (path / ".git").write_text(f"gitdir: {evil / '.git'}\n")
        self.assertEqual(self.plain(path, "rev-parse", "HEAD"), evil_head, "the attack works on plain git")
        self.assertEqual(common.wt_git(self.cfg, path, "rev-parse", "HEAD"), real)
        self.assertEqual(worker.prepare_worktree(self.cfg, 3), path)
        self.assertEqual(worker.problems(self.cfg, harness.Outcome(0, False, 1, False), path, real),
                         ["the session made no new commit"], "status and HEAD come from the real worktree")
        with self.assertRaises(open_pr.OpenPrError):
            open_pr.push(path, "wt/issue-3", "t", url="file:///nowhere")

    def test_a_rewritten_commondir_does_not_move_the_drivers_git(self) -> None:
        path = worker.prepare_worktree(self.cfg, 4)
        real = common.wt_git(self.cfg, path, "rev-parse", "origin/main")
        evil, evil_head = self.evil_repo()
        admin = common.worktree_admin(self.cfg, path)
        git(evil, "update-ref", "refs/remotes/origin/main", evil_head)
        (admin / "commondir").write_text(str(evil / ".git") + "\n")
        self.assertEqual(self.plain(path, "rev-parse", "origin/main"), evil_head, "the attack works on plain git")
        # GIT_COMMON_DIR alone does not override the commondir file (git 2.54): the lane refuses instead
        with self.assertRaises(GitError) as caught:
            common.wt_git(self.cfg, path, "rev-parse", "origin/main")
        self.assertIn("commondir points at", str(caught.exception))
        (admin / "commondir").write_text("../..\n")
        self.assertEqual(common.wt_git(self.cfg, path, "rev-parse", "origin/main"), real)

    def test_an_admin_directory_that_belongs_elsewhere_is_refused(self) -> None:
        path = worker.prepare_worktree(self.cfg, 5)
        admin = common.worktree_admin(self.cfg, path)
        (admin / "gitdir").write_text(str(self.state.root / "elsewhere" / ".git") + "\n")
        with self.assertRaises(GitError):
            common.wt_git(self.cfg, path, "status")

    def test_a_checkout_whose_git_is_a_pointer_file_is_refused(self) -> None:
        import dataclasses

        path = worker.prepare_worktree(self.cfg, 6)
        with self.assertRaises(GitError):
            common.checkout_git(dataclasses.replace(self.cfg, checkout=path))

    def test_diffs_run_no_textconv_or_external_diff(self) -> None:
        marker = self.state.root / "ran"
        path = worker.prepare_worktree(self.cfg, 7)
        for key, value in (("diff.evil.textconv", f"touch {marker}; cat"), ("diff.external", f"touch {marker}; true")):
            git(self.clone, "config", key, value)
        (path / ".gitattributes").write_text("* diff=evil\n")
        (path / "README").write_text("changed\n")
        common.wt_git(self.cfg, path, "diff", "--no-textconv", "--no-ext-diff")
        open_pr.scan(self.clone, "wt/issue-7", "main", "t", "b")
        self.assertFalse(marker.exists())


class ConflictTests(RepoCase):
    def test_a_conflicting_branch_goes_back_with_the_conflicting_files_and_the_worktree_untouched(self) -> None:
        path = worker.prepare_worktree(self.cfg, 3)
        for repo, text in ((path, "branch\n"), (self.clone, "main\n")):
            git(repo, "config", "user.email", "w@example.invalid")
            git(repo, "config", "user.name", "w")
        (path / "README").write_text("branch\n")
        git(path, "commit", "-qam", "branch edit")
        git(self.clone, "switch", "-q", "main")
        (self.clone / "README").write_text("main\n")
        git(self.clone, "commit", "-qam", "main edit")
        git(self.clone, "push", "-q", "origin", "main")
        git(self.clone, "fetch", "-q", "origin")
        head = git(path, "rev-parse", "HEAD")
        with mock.patch.object(github, "pull", return_value=pull(12, 3, mergeable="CONFLICTING")), \
                mock.patch.object(github, "required_checks", return_value=REQUIRED):
            text = worker.rework_findings(self.cfg, 12, path)
        self.assertIn("The branch conflicts with `main`", text)
        self.assertIn("- `README`", text)
        self.assertEqual(git(path, "rev-parse", "HEAD"), head)
        self.assertEqual(git(path, "status", "--porcelain"), "")

    def test_a_branch_that_merges_cleanly_has_no_conflicting_files(self) -> None:
        path = worker.prepare_worktree(self.cfg, 4)
        self.assertEqual(worker.conflicts(self.cfg, path), [])


class BriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg
        self.required = mock.patch.object(github, "required_checks", return_value=REQUIRED)
        self.required.start()
        self.addCleanup(self.required.stop)

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

    def test_failing_checks_go_back_with_their_names_and_log_tails(self) -> None:
        """#8: the failing job's log tail is in the findings; the required roll-up is named too."""
        log = "\n".join(f"2026-11-12T10:00:{i:02d}.0000000Z line {i}" for i in range(100)) + "\nerror CS0103: x"
        with mock.patch.object(github, "pull", return_value=pull(12, 3, checks=RED, runs=RED_RUNS)), \
                mock.patch.object(common, "gh", return_value=log) as gh:
            text = worker.rework_findings(self.cfg, 12)
        self.assertIn("Required checks failing on `h1`: ci-ok.", text)
        self.assertIn("`build-and-test` (failure)", text)
        self.assertIn("error CS0103: x", text)
        self.assertIn("line 99", text)
        self.assertNotIn("line 10\n", text, "only the tail of the log")
        self.assertNotIn("2026-11-12T10:00", text, "timestamps are stripped")
        self.assertEqual(gh.call_args.args[:2], ("api", "repos/lunate-agent/lunate/actions/jobs/4242/logs"))
        self.assertLess(text.index("build-and-test` — failure"), text.index("ci-ok` — failure"))

    def test_an_infra_failure_rerun_once_that_failed_again_goes_back_saying_so(self) -> None:
        runs = (github.Run("ci-ok", "CANCELLED", 1, "u", 9), github.Run("verify (macos, 2/4)", "CANCELLED", 2, "u", 9))
        p = pull(12, 3, checks=(("ci-ok", "COMPLETED", "CANCELLED"),), runs=runs)
        with mock.patch.object(github, "pull", return_value=p), mock.patch.object(common, "gh", return_value="log"):
            with self.assertRaises(LaneError):
                worker.rework_findings(self.cfg, 12)  # not rerun yet: nothing to rework, the feeder reruns it
            harness.note_rerun(12, "h1", (9,))
            text = worker.rework_findings(self.cfg, 12)
        self.assertIn("were rerun once on this head, and failed the same way again", text)

    def test_a_log_that_cannot_be_read_is_said_so_in_the_findings(self) -> None:
        with mock.patch.object(github, "pull", return_value=pull(12, 3, checks=RED, runs=RED_RUNS)), \
                mock.patch.object(common, "gh", side_effect=common.GhError("gh api failed (1): HTTP 410")):
            text = worker.rework_findings(self.cfg, 12)
        self.assertIn("(the log could not be read: gh api failed (1): HTTP 410)", text)

    def test_a_queue_removal_goes_back_with_its_reason(self) -> None:
        p = pull(12, 3, reviews=[review("APPROVED")], queue_removal="FAILED_CHECKS")
        with mock.patch.object(github, "pull", return_value=p):
            text = worker.rework_findings(self.cfg, 12)
        self.assertIn("out of the merge queue after approval (failed_checks)", text)

    def test_the_rework_brief_says_merge_never_rebase_never_force(self) -> None:
        text = worker.brief(self.cfg, ISSUE, pathlib.Path("/w"), 12, "### Checks are failing", True)
        self.assertIn("`git merge origin/main`", text)
        self.assertIn("Never `git rebase`", text)
        self.assertIn("without force", text)
        self.assertIn("run the named tests again", text)

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
            self.assertEqual(worker.rework_findings(self.cfg, 12), "### The reviewer requested changes\n\nb")
        with mock.patch.object(github, "pull", return_value=pull(12, 3, state="MERGED")):
            with self.assertRaises(LaneError):
                worker.rework_findings(self.cfg, 12)


class DriveCase(RepoCase):
    """The fixture of a whole run: the session, the dispatch and the comments replaced."""

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
            mock.patch.object(github, "required_checks", return_value=REQUIRED),
        ]
        self.drafted = []
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        super().tearDown()

    def dispatch(self, repo, number, arm=True, known=()):
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



class DriveTests(DriveCase):
    """A whole run: the session commits, the PR opens unarmed at the first commit, then is armed at the end."""

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

                def flaky(repo, number, arm=True, known=(), error=error):
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

    def test_a_committed_secret_is_never_pushed_and_parks_the_card_naming_only_its_kind(self) -> None:
        """#12, through the real dispatch and open_pr: the scan stops both the early open and the finish."""
        token = "ghp_" + "K4m" * 10
        self.patches[1].stop()
        del self.patches[1]
        pushed = []

        def run(cfg, *, worktree, log_path, on_tick, **_):
            git(worktree, "config", "user.email", "w@example.invalid")
            git(worktree, "config", "user.name", "w")
            (worktree / "Secrets.cs").write_text(f'const string T = "{token}";\n')
            git(worktree, "add", "Secrets.cs")
            git(worktree, "commit", "-q", "-m", "oops")
            on_tick()
            log_path.write_text("")
            return harness.Outcome(0, False, 60, False)

        with mock.patch.object(harness, "run_session", side_effect=run), \
                mock.patch.object(open_pr, "push", side_effect=lambda *a, **k: pushed.append(a)):
            self.assertEqual(worker.drive(self.cfg, 3, None), 1)
        self.assertEqual(pushed, [])
        self.assertEqual(harness.parked_details(), {3: "secret"})
        self.assertEqual(len(self.comments), 1)
        target, body = self.comments[0]
        self.assertEqual(target, 3)
        self.assertIn("shaped like a GitHub token in the diff's added lines", body)
        log = (self.cfg.state / "logs" / "worker-3.log").read_text()
        self.assertIn("secret scan: the diff's added lines holds something shaped like a GitHub token", log)
        for text in (body, log):
            self.assertNotIn(token, text)
            self.assertNotIn(token[4:], text)

    def test_three_runs_without_a_new_commit_park_the_card_once_and_say_so_on_the_issue(self) -> None:
        for run in range(1, 5):
            harness.note_attempt(3)  # what the feeder does at each launch
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
        self.assertEqual(harness.rework_rounds(3, 77), 0, "drive counts nothing: the feeder or main already did")
        self.assertIn("#77 is a draft again", (self.cfg.state / "logs" / "worker-3.log").read_text())

    def test_a_hand_started_rework_counts_its_round_before_anything_can_fail(self) -> None:
        """Review of #17, item 5: the worktree cannot even be cut, and the round is counted all the same."""
        with mock.patch.object(sys, "argv", ["worker.py", "--issue", "3", "--rework", "77"]), \
                mock.patch.object(harness, "live_pid", return_value=None), \
                mock.patch.object(worker, "prepare_worktree", side_effect=GitError("merge --ff-only failed (128)")):
            self.assertEqual(worker.main(), 1)
        self.assertEqual(harness.rework_rounds(3, 77), 1)

    def test_a_rework_that_keeps_adding_nothing_is_parked_on_its_pull_request(self) -> None:
        sha = self.push_branch("wt/issue-3", "first.cs")
        with mock.patch.object(github, "pull", return_value=pull(77, 3, head=sha,
                                                                 reviews=[review("CHANGES_REQUESTED", commit=sha)])):
            for _ in range(3):
                harness.note_attempt(3)
                with self.fake_session(commit=False):
                    worker.drive(self.cfg, 3, 77)
        parking = [c for c in self.comments if "is parked" in c[1]]
        self.assertEqual([c[0] for c in parking], [77])

    def test_a_new_commit_resets_the_idle_count(self) -> None:
        for commit in (False, False, True, False, False):
            harness.note_attempt(3)
            with self.fake_session(commit=commit):
                worker.drive(self.cfg, 3, None)
        self.assertEqual((harness.parked_cards(), harness.idle_runs(3)), (frozenset(), 2))
        harness.note_attempt(3)
        self.assertEqual(harness.note_run(3, False), (3, True))

    def test_a_hand_started_new_card_counts_its_attempt_before_anything_can_fail(self) -> None:
        with mock.patch.object(sys, "argv", ["worker.py", "--issue", "3"]), \
                mock.patch.object(harness, "live_pid", return_value=None), \
                mock.patch.object(worker, "prepare_worktree", side_effect=GitError("worktree add failed (128)")):
            self.assertEqual(worker.main(), 1)
        self.assertEqual(harness.idle_runs(3), 1)

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


class OrphanBriefTests(unittest.TestCase):
    """A: the brief of an orphaned draft says what its last run reported, and lets a complete branch finish
    without a new commit."""

    setUp, tearDown = BriefTests.setUp, BriefTests.tearDown

    REPORT = ("**Worker session** on `m` at `3d35f9855`, 24 min.\n**Incomplete — the pull request stays a draft and "
              "auto-merge is not armed:** the session made no new commit.\n\nAll checks met.")

    def test_816s_brief_names_the_last_runs_reason_and_resumes(self) -> None:
        p = pull(816, 780, head="3d35f9855", draft=True, checks=RED)
        with mock.patch.object(github, "pull", return_value=p), \
                mock.patch.object(github, "worker_report", return_value=self.REPORT):
            text, resume = worker.rework_brief(self.cfg, 816)
        self.assertTrue(resume)
        self.assertIn("### The last run did not finish", text)
        self.assertIn("> **Incomplete — the pull request stays a draft and auto-merge is not armed:** the session "
                      "made no new commit.", text)
        self.assertIn("no new commit is needed then", text)
        self.assertNotIn("Checks are failing", text, "a draft's deliberate ci-ok failure is not a finding")

    def test_a_draft_with_a_request_for_changes_is_not_a_resume(self) -> None:
        p = pull(816, 780, draft=True, reviews=[review("CHANGES_REQUESTED", body="fix x.cs:3")])
        with mock.patch.object(github, "pull", return_value=p), \
                mock.patch.object(github, "worker_report", return_value=""):
            text, resume = worker.rework_brief(self.cfg, 816)
        self.assertFalse(resume)
        self.assertIn("fix x.cs:3", text)
        self.assertIn("(no reason on record", text)


class OrphanDriveTests(DriveCase):
    def test_a_resume_that_finds_the_work_complete_is_marked_ready_without_a_commit(self) -> None:
        sha = self.push_branch("wt/issue-3", "first.cs")
        with mock.patch.object(github, "pull", return_value=pull(77, 3, head=sha, draft=True)), \
                mock.patch.object(github, "worker_report", return_value=""):
            harness.note_attempt(3)
            with self.fake_session(commit=False, text="verified: 1. met — a.cs:1"):
                self.assertEqual(worker.drive(self.cfg, 3, 77), 0)
        self.assertEqual(self.dispatched, [True], "clean with no new commit: pushed, marked ready and armed")
        self.assertNotIn("Incomplete", self.comments[0][1])
        self.assertEqual(harness.idle_runs(3), 1, "it still counts as an idle run toward the cap")

    def test_a_rework_with_changes_requested_still_needs_a_commit(self) -> None:
        sha = self.push_branch("wt/issue-3", "first.cs")
        with mock.patch.object(github, "pull", return_value=pull(77, 3, head=sha, draft=True,
                                                                 reviews=[review("CHANGES_REQUESTED", commit=sha)])), \
                mock.patch.object(github, "worker_report", return_value=""):
            with self.fake_session(commit=False):
                self.assertEqual(worker.drive(self.cfg, 3, 77), 1)
        self.assertEqual(self.dispatched, [False])
        self.assertIn("the session made no new commit", self.comments[0][1])


class ReadyRunDriveTests(DriveCase):
    def test_ci_never_running_it_as_ready_parks_the_card_by_name_and_still_posts_the_report(self) -> None:
        reason = "#77: the ready CI run on 3d35f9855 was cancelled again after one draft/ready toggle (run(s) 9)"

        def dispatch(repo, number, arm=True, known=()):
            self.dispatched.append(arm)
            if arm:
                raise open_pr.ReadyRunError(77, reason)
            return {"number": 77, "auto_merge": "not armed"}

        dispatch_card.dispatch.side_effect = dispatch
        with self.fake_session():
            self.assertEqual(worker.drive(self.cfg, 3, None), 1)
        self.assertEqual(harness.parked_info(), {3: ("ready", reason)})
        report, parking = self.comments
        self.assertIn(f"**Marked ready, but not armed:** {reason}.", report[1])
        self.assertIn("done: 1. met", report[1])
        self.assertEqual(parking[0], 77)
        self.assertIn("convert it to a draft and mark it ready again", parking[1])


class NeedsPersonDriveTests(DriveCase):
    def test_a_worker_that_says_needs_person_is_pushed_unarmed_and_parked_at_once(self) -> None:
        with self.fake_session(text="1. met. 2. not met.\\nNEEDS-PERSON: check 2 needs a maintainer decision on the API"):
            self.assertEqual(worker.drive(self.cfg, 3, None), 1)
        self.assertEqual(self.dispatched, [False, False], "never armed")
        self.assertEqual(harness.parked_info(), {3: ("person", "check 2 needs a maintainer decision on the API")})
        report, parking = self.comments
        self.assertIn("the worker says it needs a person: check 2 needs a maintainer decision", report[1])
        self.assertEqual(parking[0], 77)
        self.assertIn("card #3 is parked", parking[1])

    def test_with_no_commits_the_issue_gets_the_reason(self) -> None:
        with self.fake_session(commit=False, text="NEEDS-PERSON: the failure is in src/Other.cs, which the card does not touch"):
            worker.drive(self.cfg, 3, None)
        self.assertEqual([c[0] for c in self.comments], [3])
        self.assertIn("the failure is in src/Other.cs", self.comments[0][1])
        self.assertEqual(harness.parked_cards(), frozenset({3}))

    def test_the_brief_tells_the_worker_how_to_stop(self) -> None:
        text = worker.brief(self.cfg, ISSUE, pathlib.Path("/w"), None, "", True)
        self.assertIn("NEEDS-PERSON: <what has to be decided or supplied", text)
        self.assertIn("evidence\ncan only come from a real release", text)
