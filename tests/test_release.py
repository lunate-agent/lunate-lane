"""The release job (lunate-lane#21): each step's decision, with GitHub replaced. No network."""

import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, feed, github, harness, lane, open_pr, release  # noqa: E402
from tests.helpers import REQUIRED, TempState, git, origin_and_clone, pull, review  # noqa: E402

MERGED = "5a7ac135" + "0" * 32


class VersionTests(unittest.TestCase):
    def test_versions_parse_and_order(self) -> None:
        self.assertEqual(release.parse("v0.5.0").tag, "v0.5.0")
        self.assertLess(release.parse("0.5.0-rc.1").key, release.parse("0.5.0").key)
        self.assertLess(release.parse("0.4.0").key, release.parse("0.5.0-rc.1").key)
        self.assertEqual(release.parse("0.5.0").next_milestone, "0.6.0")
        self.assertEqual(release.parse("0.5.0-rc.2").base, "0.5.0")
        for bad in ("0.5", "five", "0.5.0-beta", ""):
            with self.subTest(bad=bad), self.assertRaises(common.LaneError):
                release.parse(bad)


class ReleaseCase(unittest.TestCase):
    """A fake of the REST reads the release job makes, routed by path prefix."""

    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg
        self.routes = {}
        self.posts = []
        self.patches = [
            mock.patch.object(release, "_api", side_effect=self.api),
            mock.patch.object(release, "_worker", return_value="ghs_worker"),
            mock.patch.object(release, "_reviewer", return_value="ghs_reviewer"),
            mock.patch.object(common, "rest", side_effect=self.rest),
            mock.patch.object(common, "git", return_value=""),
            mock.patch.object(github, "required_checks", return_value=REQUIRED),
            mock.patch("sys.stdout", new=io.StringIO()),
        ]
        for patch in self.patches:
            patch.start()
        release.start("0.5.0", "test")

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.state.close()

    def api(self, path):
        for prefix, answer in sorted(self.routes.items(), key=lambda kv: -len(kv[0])):
            if path.startswith(prefix):
                return answer(path) if callable(answer) else answer
        raise AssertionError(f"unexpected read {path}")

    def rest(self, token, method, path, payload=None):
        self.posts.append((token, method, path, payload))
        return {"id": 1}

    def data(self):
        return release.intent()


class PreflightTests(ReleaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.routes.update({
            "repos/lunate-agent/lunate/pulls?": [],
            "repos/lunate-agent/lunate/milestones": [{"title": "0.5.0", "number": 7, "open_issues": 0, "state": "open"}],
            "repos/lunate-agent/lunate/commits/": {"check_runs": [{"status": "completed", "conclusion": "success",
                                                                   "started_at": "2026-11-21T10:00:00Z"}]},
        })
        self.props = mock.patch.object(release, "_main_props", return_value="0.4.0")
        self.has = mock.patch.object(release, "_has", return_value=True)
        self.props.start()
        self.has.start()

    def tearDown(self) -> None:
        self.has.stop()
        self.props.stop()
        super().tearDown()

    def run_step(self):
        return release.step_preflight(self.cfg, release.parse("0.5.0"), self.data())

    def test_an_empty_milestone_and_a_green_main_pass(self) -> None:
        self.assertIsNone(self.run_step())

    def test_a_version_that_is_not_newer_is_blocked(self) -> None:
        release._main_props.return_value = "0.5.0"
        with self.assertRaisesRegex(release.Blocked, "0.5.0 is not newer than 0.5.0"):
            self.run_step()

    def test_open_issues_block_unless_allowed(self) -> None:
        self.routes["repos/lunate-agent/lunate/milestones"] = [{"title": "0.5.0", "number": 7, "open_issues": 4}]
        with self.assertRaisesRegex(release.Blocked, "still has 4 open issue"):
            self.run_step()
        release.start("0.5.0", "test", allow_open=True)
        self.assertIsNone(self.run_step())

    def test_no_release_mode_in_the_assembler_is_blocked_by_name(self) -> None:
        release._has.return_value = False
        with self.assertRaisesRegex(release.Blocked, "no --release mode yet"):
            self.run_step()

    def test_a_red_main_blocks_and_a_running_one_waits(self) -> None:
        self.routes["repos/lunate-agent/lunate/commits/"] = {"check_runs": [{"status": "in_progress"}]}
        self.assertIn("still running", self.run_step())
        self.routes["repos/lunate-agent/lunate/commits/"] = {"check_runs": [{"status": "completed",
                                                                             "conclusion": "failure"}]}
        with self.assertRaisesRegex(release.Blocked, "is not green"):
            self.run_step()

    def test_it_is_skipped_once_the_release_pull_request_exists(self) -> None:
        self.routes["repos/lunate-agent/lunate/pulls?"] = [{"number": 970, "state": "open",
                                                           "head": {"ref": "release/0.5.0"}}]
        release._main_props.return_value = "9.9.9"  # would block, were it read
        self.assertIsNone(self.run_step())


class GateTests(ReleaseCase):
    """The one human gate: only the maintainer's approval at the head lands the release pull request."""

    def setUp(self) -> None:
        super().setUp()
        self.routes["repos/lunate-agent/lunate/pulls?"] = [{"number": 970, "state": "open",
                                                           "head": {"ref": "release/0.5.0"}}]
        self.enqueue = mock.patch.object(open_pr, "enqueue", return_value={"state": "QUEUED", "position": 1})
        self.enqueue.start()

    def tearDown(self) -> None:
        self.enqueue.stop()
        super().tearDown()

    def merge(self, p):
        with mock.patch.object(github, "pull", return_value=p):
            return release.step_merge(self.cfg, release.parse("0.5.0"), self.data())

    def test_without_the_maintainers_approval_it_waits_and_is_never_enqueued(self) -> None:
        me = self.cfg.parked_assignee
        for reviews in ([], [review("APPROVED")], [review("APPROVED", author="someone-else")],
                        [review("APPROVED", author=me, commit="old")],
                        [review("APPROVED", author=me), review("CHANGES_REQUESTED", author=me)]):
            with self.subTest(reviews=reviews):
                waits = self.merge(pull(970, None, branch="release/0.5.0", reviews=reviews))
                self.assertIn(f"@{me}'s approval of #970", waits)
        open_pr.enqueue.assert_not_called()

    def test_approved_and_green_is_enqueued_at_its_head(self) -> None:
        p = pull(970, None, branch="release/0.5.0", node_id="PR_970",
                 reviews=[review("APPROVED", author=self.cfg.parked_assignee)])
        self.assertIn("in the merge queue", self.merge(p))
        open_pr.enqueue.assert_called_once_with(970, "PR_970", "h1")

    def test_red_is_blocked_and_merged_is_done(self) -> None:
        with self.assertRaisesRegex(release.Blocked, "red on a required check"):
            self.merge(pull(970, None, branch="release/0.5.0", checks=(("ci-ok", "COMPLETED", "FAILURE"),)))
        self.assertIsNone(self.merge(pull(970, None, branch="release/0.5.0", state="MERGED")))

    def test_the_feeder_never_enqueues_a_release_pull_request(self) -> None:
        """Approved by anyone, green, not queued: the feeder leaves it to the release job's gate."""
        p = pull(970, None, branch="release/0.5.0", title="chore(release): 0.5.0", reviews=[review("APPROVED")])
        with mock.patch.object(harness, "running_workers", return_value={}), \
                mock.patch.object(github, "milestone_cards", return_value=[]), \
                mock.patch.object(github, "open_pulls", return_value=[p]), \
                mock.patch.object(github, "pull", return_value=p), \
                mock.patch.object(github, "newer_decision", return_value=""):
            enqueue = feed.decide_all(self.cfg)[3]
        self.assertEqual(enqueue, [])
        self.assertTrue(github.needs_enqueue(p, REQUIRED, self.cfg.reviewer_app), "it would be, were it a card's")


class TagTests(ReleaseCase):
    def setUp(self) -> None:
        super().setUp()
        self.routes["repos/lunate-agent/lunate/pulls?"] = [{"number": 970, "state": "closed",
                                                           "merged_at": "2026-11-21T12:00:00Z",
                                                           "merge_commit_sha": MERGED,
                                                           "head": {"ref": "release/0.5.0"}}]
        self.tags = []
        self.routes["repos/lunate-agent/lunate/git/matching-refs/tags/"] = lambda path: self.tags
        mock.patch.object(release, "_on_base", return_value=True).start()
        mock.patch.object(release, "_main_props", return_value="0.5.0").start()
        self.addCleanup(mock.patch.stopall)

    def tag(self):
        return release.step_tag(self.cfg, release.parse("0.5.0"), self.data())

    def test_the_tag_is_created_on_the_merged_commit_and_read_back(self) -> None:
        def created(token, method, path, payload=None):
            self.posts.append((token, method, path, payload))
            self.tags.append({"ref": "refs/tags/v0.5.0", "object": {"type": "commit", "sha": MERGED}})
            return {}

        common.rest.side_effect = created
        self.assertIn("Release workflow", self.tag())
        self.assertEqual(self.posts, [("ghs_worker", "POST", "/repos/lunate-agent/lunate/git/refs",
                                       {"ref": "refs/tags/v0.5.0", "sha": MERGED})])
        self.assertIsNone(self.tag(), "the next advance finds it and goes on")
        self.assertEqual(len(self.posts), 1, "never tagged twice")

    def test_a_tag_elsewhere_is_blocked_and_a_wrong_version_is_never_tagged(self) -> None:
        self.tags.append({"ref": "refs/tags/v0.5.0", "object": {"type": "commit", "sha": "f" * 40}})
        with self.assertRaisesRegex(release.Blocked, "already exists at ffffffff"):
            self.tag()
        self.tags.clear()
        release._main_props.return_value = "0.4.0"
        with self.assertRaisesRegex(release.Blocked, "does not read 0.5.0"):
            self.tag()
        self.assertEqual(self.posts, [])


class PublishedTests(ReleaseCase):
    def published(self):
        return release.step_published(self.cfg, release.parse("0.5.0"), self.data())

    def test_it_waits_for_the_run_then_reads_the_release_back(self) -> None:
        runs = []
        self.routes["repos/lunate-agent/lunate/actions/workflows/release.yml"] = lambda p: {"workflow_runs": runs}
        self.assertIn("to start", self.published())
        runs.append({"id": 4, "status": "in_progress", "created_at": "t"})
        self.assertIn("run 4", self.published())
        runs[0].update(status="completed", conclusion="success")
        assets = [{"name": n} for n in ("SHA256SUMS", "a.zip", "b.tar.gz", "c.tar.gz", "lunate.nupkg", "ext.nupkg")]
        self.routes["repos/lunate-agent/lunate/releases/tags/v0.5.0"] = {"draft": False, "assets": assets}
        self.assertIsNone(self.published())
        self.routes["repos/lunate-agent/lunate/releases/tags/v0.5.0"] = {"draft": False, "assets": assets[1:]}
        with self.assertRaisesRegex(release.Blocked, "needs 6 and SHA256SUMS"):
            self.published()
        runs[0]["conclusion"] = "failure"
        with self.assertRaisesRegex(release.Blocked, "ended failure"):
            self.published()


class ChannelsTests(ReleaseCase):
    def test_only_the_channel_files_may_change(self) -> None:
        self.assertEqual(release.channels_check(["packaging/homebrew/lunate.rb", "packaging/scoop/lunate.json",
                                                 "packaging/winget/wdaniel1993.lunate.yaml", "README.md"]), [])
        self.assertEqual(release.channels_check(["README.md", "src/Program.cs"]), ["src/Program.cs"])

    def test_the_reviewer_approves_a_green_channels_pull_request_of_the_right_shape(self) -> None:
        v = release.parse("0.5.0")
        p = pull(975, None, branch="chore/0.5.0-channels", files=("packaging/scoop/lunate.json", "README.md"))
        with mock.patch.object(github, "pull", return_value=p):
            release._approve_channels(self.cfg, v, 975)
        (token, method, path, payload), = self.posts
        self.assertEqual((token, method, path), ("ghs_reviewer", "POST", "/repos/lunate-agent/lunate/pulls/975/reviews"))
        self.assertEqual((payload["event"], payload["commit_id"]), ("APPROVE", "h1"))
        self.posts.clear()
        stray = pull(975, None, branch="chore/0.5.0-channels", files=("packaging/scoop/lunate.json", "src/x.cs"))
        with mock.patch.object(github, "pull", return_value=stray), self.assertRaisesRegex(release.Blocked, "src/x.cs"):
            release._approve_channels(self.cfg, v, 975)
        red = pull(975, None, branch="chore/0.5.0-channels", checks=(("ci-ok", "COMPLETED", "FAILURE"),))
        with mock.patch.object(github, "pull", return_value=red):
            release._approve_channels(self.cfg, v, 975)
        self.assertEqual(self.posts, [], "nothing approved that is red or the wrong shape")

    def test_a_prerelease_skips_the_channels_the_mirror_and_the_milestone(self) -> None:
        rc = release.parse("0.5.0-rc.1")
        for step in (release.step_channels, release.step_mirror, release.step_close):
            self.assertIsNone(step(self.cfg, rc, {}))
        self.assertEqual(self.posts, [])


class CloseTests(ReleaseCase):
    def test_the_milestone_is_closed_the_next_created_and_the_feeder_moved(self) -> None:
        milestones = [{"title": "0.5.0", "number": 7, "open_issues": 0, "state": "open"}]
        self.routes["repos/lunate-agent/lunate/milestones"] = lambda path: milestones

        def rest(token, method, path, payload=None):
            self.posts.append((method, path, payload))
            if method == "POST":
                milestones.append({"title": payload["title"], "number": 8, "open_issues": 0, "state": "open"})
            else:
                milestones[0]["state"] = "closed"
            return {}

        common.rest.side_effect = rest
        self.assertIsNone(release.step_close(self.cfg, release.parse("0.5.0"), {}))
        self.assertEqual(self.posts, [("PATCH", "/repos/lunate-agent/lunate/milestones/7", {"state": "closed"}),
                                      ("POST", "/repos/lunate-agent/lunate/milestones", {"title": "0.6.0"})])
        self.assertEqual(common.config().milestone, "0.6.0")
        self.assertEqual(common.with_milestone(self.cfg).milestone, "0.6.0", "read from lane state on the next run")
        self.posts.clear()
        self.assertIsNone(release.step_close(self.cfg, release.parse("0.5.0"), {}))
        self.assertEqual(self.posts, [], "done is done")

    def test_an_issue_added_during_the_release_blocks_the_close(self) -> None:
        self.routes["repos/lunate-agent/lunate/milestones"] = [{"title": "0.5.0", "number": 7, "open_issues": 1,
                                                               "state": "open"}]
        with self.assertRaisesRegex(release.Blocked, "gained 1 open issue"):
            release.step_close(self.cfg, release.parse("0.5.0"), {})


class AdvanceTests(ReleaseCase):
    def test_it_stops_at_the_first_step_that_waits_and_records_why(self) -> None:
        calls = []
        steps = (("a", lambda *a: calls.append("a")), ("b", lambda *a: calls.append("b") or "the maintainer"),
                 ("c", lambda *a: calls.append("c")))
        with mock.patch.object(release, "STEPS", steps):
            self.assertEqual(release.advance(self.cfg), "release 0.5.0 — b: waits on the maintainer")
        self.assertEqual(calls, ["a", "b"])
        self.assertIn("b: waits on the maintainer", release.status())

    def test_a_blocked_step_is_recorded_and_raised_and_done_is_moved_aside(self) -> None:
        def blocked(*a):
            raise release.Blocked("main is red")

        with mock.patch.object(release, "STEPS", (("a", blocked),)):
            with self.assertRaises(release.Blocked):
                release.advance(self.cfg)
        self.assertIn("a: blocked — main is red", release.status())
        with mock.patch.object(release, "STEPS", (("a", lambda *a: None),)):
            self.assertEqual(release.advance(self.cfg), "release 0.5.0 — done")
        self.assertIsNone(release.intent())
        done = json.loads((self.cfg.state / "releases" / "0.5.0.json").read_text())
        self.assertEqual(done["outcome"], "released")

    def test_one_release_at_a_time(self) -> None:
        with self.assertRaisesRegex(common.LaneError, "release 0.5.0 is still open"):
            release.start("0.6.0", "test")
        self.assertIn("abandoned", release.abandon("wrong version"))
        release.start("0.6.0", "test")

    def test_lane_release_exits_non_zero_when_blocked(self) -> None:
        def blocked(*a):
            raise release.Blocked("main is red")

        out = io.StringIO()
        with mock.patch.object(release, "STEPS", (("preflight", blocked),)), \
                mock.patch.object(harness, "exit_on_signals"), mock.patch("sys.stdout", new=out):
            self.assertEqual(lane.main(["release", "0.5.0"]), 1)
            self.assertEqual(lane.main(["release", "--status"]), 0)
        self.assertIn("release 0.5.0: FAILED — main is red", out.getvalue())
        self.assertIn("preflight: blocked — main is red", out.getvalue())


class SandboxedAssembleTests(unittest.TestCase):
    """The release pull request's edit runs the repository's own script in the session sandbox, on a real
    worktree: it may change the changelog, the version and the fragments, and nothing else."""

    ASSEMBLE = '''import pathlib, sys
version = sys.argv[sys.argv.index("--release") + 1] if "--release" in sys.argv else None
if version:
    root = pathlib.Path(".")
    log = root / "CHANGELOG.md"
    log.write_text(log.read_text().replace("## [Unreleased]", "## [Unreleased]\\n\\n## [" + version + "] - 2026-11-21"))
    props = root / "Directory.Build.props"
    props.write_text(props.read_text().replace("0.4.0", version))
    for f in (root / "changes").glob("*.md"):
        f.unlink()
    if "{stray}" == "yes":
        (root / "src.cs").write_text("x")
'''

    def setUp(self) -> None:
        if not pathlib.Path(harness.SANDBOX_EXEC).exists():
            self.skipTest("sandbox-exec is not on this host")
        self.state = TempState()
        root = self.state.root
        self.origin, self.clone = origin_and_clone(root / "repos")
        self.state.cfg = self.cfg = __import__("dataclasses").replace(self.state.cfg, checkout=self.clone)
        common._CONFIG = self.cfg
        self.out = mock.patch("sys.stdout", new=io.StringIO())
        self.out.start()

    def tearDown(self) -> None:
        self.out.stop()
        self.state.close()

    def seed(self, stray: str = "no") -> None:
        (self.clone / "scripts").mkdir(exist_ok=True)
        (self.clone / "changes").mkdir(exist_ok=True)
        (self.clone / "scripts" / "changelog-assemble").write_text(self.ASSEMBLE.replace("{stray}", stray))
        (self.clone / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [0.4.0] - 2026-11-14\n")
        (self.clone / "Directory.Build.props").write_text("<Project><PropertyGroup><Version>0.4.0</Version>"
                                                         "</PropertyGroup></Project>\n")
        (self.clone / "changes" / "900.md").write_text("### Added\n\n- a thing (#900).\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "seed")
        git(self.clone, "push", "-q", "origin", "HEAD:main")

    def test_the_release_edit_runs_sandboxed_and_opens_a_ready_unarmed_pull_request(self) -> None:
        self.seed()
        opened = []
        with mock.patch.object(release, "_pull_for", return_value=None), \
                mock.patch.object(harness, "_git_identity", return_value={"name": "T", "email": "t@example.com"}), \
                mock.patch.object(open_pr, "open_pr", side_effect=lambda *a, **k: opened.append((a, k)) or
                                  {"number": 970}):
            waits = release.step_pr(self.cfg, release.parse("0.5.0"), {})
        self.assertEqual(waits, "the maintainer's approval of #970")
        (args, kwargs), = opened
        self.assertEqual(args[1:4][:2], ("release/0.5.0", "chore(release): 0.5.0"))
        self.assertEqual((kwargs["arm"], kwargs["ready_only"]), (False, True))
        self.assertIn("## [0.5.0] - 2026-11-21", args[3])
        changed = git(self.clone, "show", "--name-status", "--format=", "release/0.5.0").split("\n")
        self.assertEqual(sorted(changed), ["D\tchanges/900.md", "M\tCHANGELOG.md", "M\tDirectory.Build.props"])
        self.assertFalse((self.cfg.state / "worktrees" / "release-0.5.0").exists(), "the worktree is removed")

    def test_a_failed_open_is_retried_from_the_commit_without_assembling_again(self) -> None:
        self.seed()
        calls = []
        with mock.patch.object(release, "_pull_for", return_value=None), \
                mock.patch.object(harness, "_git_identity", return_value={"name": "T", "email": "t@example.com"}), \
                mock.patch.object(open_pr, "open_pr", side_effect=open_pr.OpenPrError("GitHub said 502")):
            with self.assertRaisesRegex(open_pr.OpenPrError, "502"):
                release.step_pr(self.cfg, release.parse("0.5.0"), {})
        tip = git(self.clone, "rev-parse", "release/0.5.0")
        with mock.patch.object(release, "_pull_for", return_value=None), \
                mock.patch.object(release, "_sandboxed_run", side_effect=AssertionError("assembled again")), \
                mock.patch.object(open_pr, "open_pr", side_effect=lambda *a, **k: calls.append(a) or {"number": 971}):
            self.assertEqual(release.step_pr(self.cfg, release.parse("0.5.0"), {}), "the maintainer's approval of #971")
        self.assertEqual(git(self.clone, "rev-parse", "release/0.5.0"), tip, "the same commit, not a new one")
        self.assertIn("## [0.5.0] - 2026-11-21", calls[0][3])

    def test_a_release_edit_that_touches_anything_else_is_blocked(self) -> None:
        self.seed(stray="yes")
        with mock.patch.object(release, "_pull_for", return_value=None), \
                mock.patch.object(open_pr, "open_pr") as opened:
            with self.assertRaisesRegex(release.Blocked, "src.cs.*kept for a look"):
                release.step_pr(self.cfg, release.parse("0.5.0"), {})
            with self.assertRaisesRegex(release.Blocked, "earlier attempt's worktree"):
                release.step_pr(self.cfg, release.parse("0.5.0"), {})
        opened.assert_not_called()
        self.assertTrue((self.cfg.state / "worktrees" / "release-0.5.0" / "src.cs").exists(), "kept, never forced")


if __name__ == "__main__":
    unittest.main()
