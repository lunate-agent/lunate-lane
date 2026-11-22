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
        self.selftest = mock.patch.object(release, "self_test")
        self.props.start()
        self.has.start()
        self.selftest.start()

    def tearDown(self) -> None:
        self.selftest.stop()
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

    def test_open_cards_in_the_milestone_do_not_hold_the_release(self) -> None:
        self.routes["repos/lunate-agent/lunate/milestones"] = [{"title": "0.5.0", "number": 7, "open_issues": 4}]
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

    def test_a_helper_that_fails_its_self_test_blocks_the_preflight(self) -> None:
        release.self_test.side_effect = release.Blocked("the self-test of scripts/docs-sync failed")
        with self.assertRaisesRegex(release.Blocked, "self-test of scripts/docs-sync"):
            self.run_step()

    def test_the_self_test_is_not_run_on_a_red_main(self) -> None:
        self.routes["repos/lunate-agent/lunate/commits/"] = {"check_runs": [{"status": "in_progress"}]}
        self.run_step()
        release.self_test.assert_not_called()

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

    def test_a_late_fragment_blocks_the_enqueue_and_none_lets_it_through(self) -> None:
        p = pull(970, None, branch="release/0.5.0", node_id="PR_970",
                 reviews=[review("APPROVED", author=self.cfg.parked_assignee)])
        with mock.patch.object(release, "late_fragments", return_value=["changes/1003.md"]):
            with self.assertRaisesRegex(release.Blocked, "changes/1003.md reached main after"):
                self.merge(p)
        open_pr.enqueue.assert_not_called()
        with mock.patch.object(release, "late_fragments", return_value=[]):
            self.assertIn("in the merge queue", self.merge(p))
        open_pr.enqueue.assert_called_once()

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
        self.tag_check = mock.patch.object(release, "tag_check").start()
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

    def test_a_commit_that_fails_the_tags_gate_is_never_tagged(self) -> None:
        self.tag_check.side_effect = release.Blocked("v0.5.0 is not tagged: changes/1003.md is missing")
        with self.assertRaisesRegex(release.Blocked, "changes/1003.md is missing"):
            self.tag()
        self.assertEqual(self.posts, [])

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
    def setUp(self) -> None:
        super().setUp()
        self.routes["repos/lunate-agent/lunate/git/matching-refs/tags/"] = []

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

    def test_a_moved_tag_reads_only_the_run_on_its_own_commit(self) -> None:
        """v0.6.0's first run failed on #1006's merge; the tag moved to #1012's: that old run is not this tag's."""
        old = {"id": 1, "status": "completed", "conclusion": "failure", "created_at": "t1", "head_sha": "old"}
        runs = [old]
        self.routes["repos/lunate-agent/lunate/actions/workflows/release.yml"] = lambda p: {"workflow_runs": runs}
        self.routes["repos/lunate-agent/lunate/git/matching-refs/tags/"] = [
            {"ref": "refs/tags/v0.5.0", "object": {"type": "commit", "sha": "new"}}]
        self.assertIn("to start", self.published())
        runs.append({"id": 2, "status": "in_progress", "created_at": "t0", "head_sha": "new"})
        self.assertIn("run 2", self.published())


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


class TrainTests(ReleaseCase):
    """The release train (lunate-lane#21, Decision 2026-11-22): the cut moves open cards forward, the close reads
    back an empty milestone, and nothing waits for a milestone to empty."""

    def setUp(self) -> None:
        super().setUp()
        self.milestones = [{"title": "0.5.0", "number": 7, "state": "open"}]
        self.items = {975: 7, 976: 7, 990: 7}  # open issue or pull request -> its milestone number
        self.closed_items = {7: 41}
        self.routes["repos/lunate-agent/lunate/milestones"] = lambda path: [
            {**m, "open_issues": sum(1 for v in self.items.values() if v == m["number"]),
             "closed_issues": self.closed_items.get(m["number"], 0)} for m in self.milestones]
        self.routes["repos/lunate-agent/lunate/issues?milestone="] = lambda path: [
            {"number": n} for n, m in sorted(self.items.items()) if f"milestone={m}&" in path]
        common.rest.side_effect = self.github

    def github(self, token, method, path, payload=None):
        self.posts.append((method, path, payload))
        if method == "POST" and path.endswith("/milestones"):
            made = {"title": payload["title"], "number": 8, "state": "open"}
            self.milestones.append(made)
            return made
        if method == "PATCH" and "/issues/" in path:
            self.items[int(path.rsplit("/", 1)[1])] = payload["milestone"]
        elif method == "PATCH" and "/milestones/" in path:
            next(m for m in self.milestones if m["number"] == int(path.rsplit("/", 1)[1])).update(payload)
        return {}

    def test_the_cut_moves_every_open_item_to_the_next_milestone_and_points_the_feeder_there(self) -> None:
        data = self.data()
        self.assertIsNone(release.step_forward(self.cfg, release.parse("0.5.0"), data))
        self.assertEqual(self.items, {975: 8, 976: 8, 990: 8})
        self.assertEqual(self.posts[0], ("POST", "/repos/lunate-agent/lunate/milestones", {"title": "0.6.0"}))
        self.assertEqual(common.config().milestone, "0.6.0")
        self.assertEqual(self.data()["forward"], "3 open item(s) moved to 0.6.0 at the cut")
        self.posts.clear()
        self.assertIsNone(release.step_forward(self.cfg, release.parse("0.5.0"), self.data()))
        self.assertEqual(self.posts, [], "recorded: the cut runs once")

    def test_the_close_moves_what_was_added_since_the_cut_then_reads_back_an_empty_closed_milestone(self) -> None:
        release.step_forward(self.cfg, release.parse("0.5.0"), self.data())
        self.items[999] = 7  # filed into the released milestone after the cut
        self.posts.clear()
        self.assertIsNone(release.step_close(self.cfg, release.parse("0.5.0"), self.data()))
        self.assertEqual(self.posts, [("PATCH", "/repos/lunate-agent/lunate/issues/999", {"milestone": 8}),
                                      ("PATCH", "/repos/lunate-agent/lunate/milestones/7", {"state": "closed"})])
        self.assertEqual(self.milestones[0]["state"], "closed")
        self.posts.clear()
        self.assertIsNone(release.step_close(self.cfg, release.parse("0.5.0"), self.data()))
        self.assertEqual(self.posts, [], "done is done")

    def test_an_item_that_does_not_move_blocks_by_name_and_the_milestone_stays_open(self) -> None:
        common.rest.side_effect = lambda token, method, path, payload=None: (
            self.posts.append((method, path, payload)) or {"title": "0.6.0", "number": 8} if method == "POST" else {})
        self.milestones.append({"title": "0.6.0", "number": 8, "state": "open"})
        with self.assertRaisesRegex(release.Blocked, "still lists #975, #976, #990 open after the move"):
            release.step_close(self.cfg, release.parse("0.5.0"), self.data())
        self.assertEqual(self.milestones[0]["state"], "open")

    def test_the_cut_creates_the_next_milestone_with_the_released_ones_description(self) -> None:
        self.milestones[0]["description"] = "Daily-driver reflexes; short cycle"
        release.step_forward(self.cfg, release.parse("0.5.0"), self.data())
        self.assertEqual(self.posts[0], ("POST", "/repos/lunate-agent/lunate/milestones",
                                         {"title": "0.6.0", "description": "Daily-driver reflexes; short cycle"}))
        self.assertEqual(self.milestones[0]["description"], "Daily-driver reflexes; short cycle", "kept as the record")

    def test_an_existing_next_milestone_gets_the_description_only_when_it_has_none(self) -> None:
        self.milestones[0]["description"] = "Daily-driver reflexes; short cycle"
        self.milestones.append({"title": "0.6.0", "number": 8, "state": "open", "description": ""})
        release.step_forward(self.cfg, release.parse("0.5.0"), self.data())
        self.assertIn(("PATCH", "/repos/lunate-agent/lunate/milestones/8",
                       {"description": "Daily-driver reflexes; short cycle"}), self.posts)
        self.posts.clear()
        self.milestones[1]["description"] = "its own"
        release._move_forward(self.cfg, release.parse("0.5.0"))
        self.assertEqual([p for p in self.posts if "milestones/8" in p[1]], [], "never overwritten")

    def test_a_prerelease_moves_nothing(self) -> None:
        self.assertIsNone(release.step_forward(self.cfg, release.parse("0.5.0-rc.1"), self.data()))
        self.assertEqual(self.posts, [])

    def test_the_cut_runs_right_after_the_tag(self) -> None:
        names = [name for name, _ in release.STEPS]
        self.assertEqual(names[names.index("tag") + 1], "forward")

    def test_the_train_ships_the_feeders_milestone_only_when_a_fragment_merged(self) -> None:
        common._CONFIG = __import__("dataclasses").replace(self.cfg, milestone="0.6.0")
        release.abandon("test")
        common.git.side_effect = lambda repo, *args: "changes/975.md\nchanges/README\n" if "ls-tree" in args else ""
        self.assertEqual(release.train_version(common.config()), ("0.6.0", "1 changelog fragment(s) on main"))
        common.git.side_effect = lambda repo, *args: "changes/README\n" if "ls-tree" in args else ""
        version, why = release.train_version(common.config())
        self.assertIsNone(version)
        self.assertIn("no changelog fragment since the last tag", why)
        release.start("0.6.0", "test")
        self.assertEqual(release.train_version(common.config()), (None, "release 0.6.0 is still open"))


class FragmentHoldTests(ReleaseCase):
    """No changelog fragment lands between the release's cut and its merge (lunate#1003 after #1006)."""

    def test_a_pull_request_adds_its_own_fragment_and_a_release_pull_request_adds_none(self) -> None:
        self.assertEqual(release.adds_fragment(pull(1003, 1002, files=("changes/1003.md", "a.cs"))),
                         "changes/1003.md")
        self.assertIsNone(release.adds_fragment(pull(1004, None, files=("docs/x.md",))))
        self.assertIsNone(release.adds_fragment(pull(1005, None, files=None)))
        self.assertIsNone(release.adds_fragment(pull(1012, None, branch="chore/release-0.6.0-fragments",
                                                     title="chore(release): 0.6.0 — fragments",
                                                     files=("changes/1003.md", "changes/1012.md"))))

    def test_the_hold_runs_from_the_start_until_the_release_pull_request_merges(self) -> None:
        with mock.patch.object(release, "_pull_for", return_value=None):
            self.assertEqual(release.holding_fragments(self.cfg), "release 0.5.0 is about to cut its pull request")
        with mock.patch.object(release, "_pull_for", return_value={"number": 1006, "state": "open"}):
            self.assertEqual(release.holding_fragments(self.cfg), "release 0.5.0's pull request #1006 is open")
        with mock.patch.object(release, "_pull_for", return_value={"number": 1006, "merged_at": "t"}):
            self.assertIsNone(release.holding_fragments(self.cfg))
        release.abandon("test")
        with mock.patch.object(release, "_pull_for", side_effect=AssertionError("no release, no read")):
            self.assertIsNone(release.holding_fragments(self.cfg))

    def test_the_cut_waits_for_a_queued_pull_request_that_adds_a_fragment(self) -> None:
        queued = [pull(1003, 1002, files=("changes/1003.md",), in_queue=True),
                  pull(1004, None, files=("docs/x.md",), in_queue=True),
                  pull(1010, 1009, files=("changes/1010.md",))]  # not queued: the feeder holds it
        with mock.patch.object(release, "_pull_for", return_value=None), \
                mock.patch.object(github, "open_pulls", return_value=queued), \
                mock.patch.object(release, "_cut", side_effect=AssertionError("cut before it landed")):
            waits = release.step_pr(self.cfg, release.parse("0.5.0"), self.data())
        self.assertEqual(waits, "#1003 (changes/1003.md) in the merge queue to land first: each adds a changelog "
                                "fragment the release must assemble")


class FixTests(TagTests):
    """lunate#1012: 0.6.0 fixed after its release pull request merged; the tag goes on the fix's merge."""

    FIX = "f1e" + "0" * 37

    def setUp(self) -> None:
        super().setUp()
        self.fix_pr = {"number": 1012, "merged_at": "2026-11-22T18:00:00Z", "merge_commit_sha": self.FIX,
                       "title": "chore(release): 0.5.0 — late fragments", "head": {"ref": "chore/release-0.5.0-x"}}
        self.routes["repos/lunate-agent/lunate/pulls/1012"] = lambda path: self.fix_pr
        self.check = self.tag_check

    def test_the_tag_goes_on_the_fixs_merge_once_recorded(self) -> None:
        def created(token, method, path, payload=None):
            self.posts.append((token, method, path, payload))
            self.tags.append({"ref": "refs/tags/v0.5.0", "object": {"type": "commit", "sha": payload["sha"]}})
            return {}

        common.rest.side_effect = created
        self.tags.append({"ref": "refs/tags/v0.5.0", "object": {"type": "commit", "sha": MERGED}})
        self.assertIsNone(self.tag(), "before the fix is recorded, the old tag is the release's own")
        out = io.StringIO()
        with mock.patch("sys.stdout", new=out):
            self.assertEqual(lane.main(["release", "--fix-pr", "1012"]), 0)
        self.assertEqual(self.data()["fix_pr"], 1012)
        with self.assertRaisesRegex(release.Blocked, "already exists at 5a7ac135.*delete the tag and the next advance "
                                                     "tags f1e000000"):
            self.tag()
        self.tags.clear()  # the person deletes the remote tag: nothing consumed it
        self.assertIn("Release workflow", self.tag())
        self.assertEqual(self.posts[-1][3], {"ref": "refs/tags/v0.5.0", "sha": self.FIX})
        self.check.assert_called_once_with(self.cfg, release.parse("0.5.0"), self.FIX)

    def test_a_fix_that_is_not_a_merged_release_pull_request_is_refused(self) -> None:
        release.fix(1012)
        self.fix_pr["title"] = "fix: something"
        with self.assertRaisesRegex(release.Blocked, "the fix #1012 is not a release pull request"):
            self.tag()
        self.fix_pr.update(title="chore(release): 0.5.0", merged_at=None)
        with self.assertRaisesRegex(release.Blocked, "the fix #1012 reads as not merged"):
            self.tag()
        self.assertEqual(self.posts, [])

    def test_996_and_1011s_shape_fragments_are_held_until_the_recorded_fix_merges(self) -> None:
        """#1006 had merged, so the hold had ended; #996 and #1011 landed while the fix #1012 was open and its
        merge, the commit to tag, would have failed the gate again."""
        with mock.patch.object(release, "_pull_for", return_value={"number": 970, "merged_at": "t"}):
            self.assertIsNone(release.holding_fragments(self.cfg))
        release.fix(1012)
        self.fix_pr["merged_at"] = None
        self.assertEqual(release.holding_fragments(self.cfg), "release 0.5.0's fix #1012 is open")
        self.fix_pr["merged_at"] = "2026-11-22T19:00:00Z"
        self.assertIsNone(release.holding_fragments(self.cfg))

    def test_fix_pr_goes_alone_and_needs_an_open_release(self) -> None:
        for argv in (["release", "0.5.0", "--fix-pr", "1"], ["release", "--train", "--fix-pr", "1"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
                lane.main(argv)
        release.abandon("test")
        with self.assertRaisesRegex(common.LaneError, "no release is open"):
            release.fix(1)


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

    def test_lane_release_train_starts_the_milestones_release_or_says_why_not(self) -> None:
        release.abandon("test")
        out = io.StringIO()
        with mock.patch.object(release, "train_version", return_value=(None, "nothing to ship")), \
                mock.patch.object(harness, "exit_on_signals"), mock.patch("sys.stdout", new=out):
            self.assertEqual(lane.main(["release", "--train"]), 0)
        self.assertIn("no release train — nothing to ship", out.getvalue())
        self.assertIsNone(release.intent())
        with mock.patch.object(release, "train_version", return_value=("0.6.0", "2 changelog fragment(s) on main")), \
                mock.patch.object(release, "STEPS", (("preflight", lambda *a: "ci-ok on main"),)), \
                mock.patch.object(harness, "exit_on_signals"), mock.patch("sys.stdout", new=out):
            self.assertEqual(lane.main(["release", "--train", "--yes"]), 0)
        self.assertEqual(release.intent()["version"], "0.6.0")
        self.assertIn("the weekly train (2 changelog fragment(s) on main)", release.intent()["by"])
        with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            lane.main(["release", "0.6.0", "--train"])

    def train(self, *argv, tty=False, answer=None):
        """`lane.py release --train …` with a train due (0.6.0); returns (exit code, stdout + stderr)."""
        release.abandon("test")
        out = io.StringIO()
        stdin = mock.Mock(isatty=mock.Mock(return_value=tty))
        with mock.patch.object(release, "train_version", return_value=("0.6.0", "2 changelog fragment(s) on main")), \
                mock.patch.object(release, "STEPS", (("preflight", lambda *a: "ci-ok on main"),)), \
                mock.patch.object(harness, "exit_on_signals"), mock.patch("sys.stdin", new=stdin), \
                mock.patch("builtins.input", side_effect=[answer] if answer is not None else EOFError) as asked, \
                mock.patch("sys.stdout", new=out), mock.patch("sys.stderr", new=out):
            rc = lane.main(["release", "--train", *argv])
        self.asked = asked.call_args_list
        return rc, out.getvalue()

    def test_train_dry_run_says_what_it_would_cut_and_changes_nothing(self) -> None:
        rc, out = self.train("--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would cut release 0.6.0 — the weekly train (2 changelog fragment(s) on main); nothing was "
                      "changed", out)
        self.assertIsNone(release.intent())
        self.assertEqual(self.asked, [])

    def test_train_without_a_terminal_or_yes_is_refused(self) -> None:
        # 2026-11-22: an agent's shell (no terminal) ran `release --train` as a check and cut 0.6.0
        rc, out = self.train()
        self.assertEqual(rc, 1)
        self.assertIn("refused — `lane.py release --train` would cut 0.6.0", out)
        self.assertIn("only with --yes (the cron line)", out)
        self.assertIsNone(release.intent())

    def test_train_from_a_terminal_cuts_only_when_the_version_is_typed_back(self) -> None:
        rc, out = self.train(tty=True, answer="y")
        self.assertEqual(rc, 1)
        self.assertIn("not cut — 'y' is not 0.6.0", out)
        self.assertIsNone(release.intent())
        self.assertEqual(self.train(tty=True)[0], 1, "no answer (EOF) is not a yes")
        self.assertIsNone(release.intent())
        rc, out = self.train(tty=True, answer="0.6.0")
        self.assertEqual(rc, 0)
        self.assertEqual(release.intent()["version"], "0.6.0")
        self.assertIn("Type 0.6.0 to confirm", self.asked[0].args[0])

    def test_dry_run_and_yes_go_with_train_and_not_together(self) -> None:
        for argv in (["release", "0.6.0", "--yes"], ["release", "0.6.0", "--dry-run"],
                     ["release", "--train", "--dry-run", "--yes"]):
            with mock.patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
                lane.main(argv)


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
        self.queue = mock.patch.object(github, "open_pulls", return_value=[])
        self.queue.start()

    def tearDown(self) -> None:
        self.queue.stop()
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

    def test_1003s_shape_a_fragment_that_merged_after_the_cut_is_named_and_the_release_is_not_enqueued(self) -> None:
        """lunate#1006 was cut, #1003 merged with changes/1003.md, then #1006 merged without it: v0.6.0's gate
        failed. Read on the real branches: the fragment on main that the release's cut did not have."""
        self.seed()
        with mock.patch.object(release, "_pull_for", return_value=None), \
                mock.patch.object(harness, "_git_identity", return_value={"name": "T", "email": "t@example.com"}), \
                mock.patch.object(open_pr, "open_pr", return_value={"number": 1006}):
            release.step_pr(self.cfg, release.parse("0.5.0"), {})
        git(self.clone, "push", "-q", "origin", "release/0.5.0")
        self.assertEqual(release.late_fragments(self.cfg, "release/0.5.0"), [], "nothing landed since the cut")
        git(self.clone, "checkout", "-q", "main")
        (self.clone / "changes" / "1003.md").write_text("### Fixed\n\n- a late fix (#1003).\n")
        (self.clone / "changes" / "README.md").write_text("how to write one\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "late")
        git(self.clone, "push", "-q", "origin", "HEAD:main")
        self.assertEqual(release.late_fragments(self.cfg, "release/0.5.0"), ["changes/1003.md"])
        release.start("0.5.0", "test")
        open_release = {"number": 1006, "state": "open", "head": {"ref": "release/0.5.0"}}
        with mock.patch.object(release, "_pull_for", return_value=open_release), \
                mock.patch.object(release, "_land", side_effect=AssertionError("never enqueued")):
            with self.assertRaisesRegex(release.Blocked, "changes/1003.md reached main after the release pull request "
                                                         "#1006 was cut.*close #1006 and delete release/0.5.0"):
                release.step_merge(self.cfg, release.parse("0.5.0"), release.intent())

    def test_the_tag_time_check_runs_the_repositorys_assembler_on_the_commit_to_tag(self) -> None:
        self.seed()
        check = ('import pathlib, sys\n'
                 'late = [f.name for f in pathlib.Path("changes").glob("[0-9]*.md")]\n'
                 'print(f"changes/{late[0]} is missing from CHANGELOG.md", file=sys.stderr) if late else None\n'
                 'sys.exit(1 if late or sys.argv[-2:] != ["--tag", "v0.5.0"] else 0)\n')
        (self.clone / "scripts" / "changelog-assemble").write_text(check)
        git(self.clone, "commit", "-q", "-am", "check")
        with self.assertRaisesRegex(release.Blocked, r"(?s)v0.5.0 is not tagged.*changes/900.md is missing.*--fix-pr"):
            release.tag_check(self.cfg, release.parse("0.5.0"), git(self.clone, "rev-parse", "HEAD"))
        git(self.clone, "rm", "-q", "changes/900.md")
        git(self.clone, "commit", "-q", "-m", "assembled")
        release.tag_check(self.cfg, release.parse("0.5.0"), git(self.clone, "rev-parse", "HEAD"))


class SandboxedChannelsTests(unittest.TestCase):
    """The channels step on a real worktree: a fill that changes nothing means main already carries the release's
    channel files, so the step is recorded as done with no pull request, and the release moves on."""

    FILL = ('import pathlib, sys\n'
            'p = pathlib.Path("packaging/homebrew/lunate.rb")\n'
            'p.write_text("version " + sys.argv[1] + "\\n")\n')
    SYNC = 'import sys\nassert sys.argv[1:] == [], sys.argv  # it writes with no argument\n'

    def setUp(self) -> None:
        if not pathlib.Path(harness.SANDBOX_EXEC).exists():
            self.skipTest("sandbox-exec is not on this host")
        self.state = TempState()
        self.origin, self.clone = origin_and_clone(self.state.root / "repos")
        self.state.cfg = self.cfg = __import__("dataclasses").replace(self.state.cfg, checkout=self.clone)
        common._CONFIG = self.cfg
        self.out = mock.patch("sys.stdout", new=io.StringIO())
        self.out.start()
        release.start("0.5.0", "test")

    def tearDown(self) -> None:
        self.out.stop()
        self.state.close()

    def seed(self, formula: str) -> None:
        for path, text in (("scripts/fill-release-hashes", self.FILL), ("scripts/docs-sync", self.SYNC),
                           ("packaging/homebrew/lunate.rb", formula)):
            (self.clone / path).parent.mkdir(parents=True, exist_ok=True)
            (self.clone / path).write_text(text)
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "seed")
        git(self.clone, "push", "-q", "origin", "HEAD:main")

    def step(self):
        with mock.patch.object(release, "_pull_for", return_value=None), \
                mock.patch.object(common, "gh", return_value=""), \
                mock.patch.object(harness, "_git_identity", return_value={"name": "T", "email": "t@example.com"}), \
                mock.patch.object(open_pr, "open_pr", side_effect=lambda *a, **k: self.opened.append(a) or
                                  {"number": 975}):
            return release.step_channels(self.cfg, release.parse("0.5.0"), release.intent())

    def test_main_already_filled_is_done_with_no_pull_request(self) -> None:
        self.seed("version v0.5.0\n")
        self.opened = []
        self.assertIsNone(self.step())
        self.assertEqual(self.opened, [])
        self.assertIn("already carried v0.5.0's channel manifests", release.intent()["channels"])
        self.assertFalse((self.cfg.state / "worktrees" / "channels-0.5.0").exists(), "the worktree is removed")
        self.assertEqual(git(self.clone, "branch", "--list", "chore/0.5.0-channels"), "", "the branch is removed")
        with mock.patch.object(release, "_pull_for", side_effect=AssertionError("read again")):
            self.assertIsNone(release.step_channels(self.cfg, release.parse("0.5.0"), release.intent()))

    def test_a_stale_main_gets_its_channels_pull_request(self) -> None:
        self.seed("version v0.4.0\n")
        self.opened = []
        self.assertEqual(self.step(), "the required checks on #975")
        (args,) = self.opened
        self.assertEqual(args[1:3], ("chore/0.5.0-channels", "chore: fill 0.5.0 release hashes"))
        self.assertNotIn("channels", release.intent())


class SelfTestTests(unittest.TestCase):
    """The preflight runs every helper the release path calls once, the way a step runs it (`python3 -I`, in the
    session sandbox), so an import error blocks before a release, not in its channels step (lunate#974)."""

    BROKEN = "import sys\nfrom published_release import X\nprint(X)\n"  # -I drops scripts/ from sys.path
    FIXED = ("import pathlib, sys\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))\n"
             "from published_release import X\nprint(X)\n")

    def setUp(self) -> None:
        if not pathlib.Path(harness.SANDBOX_EXEC).exists():
            self.skipTest("sandbox-exec is not on this host")
        self.state = TempState()
        self.origin, self.clone = origin_and_clone(self.state.root / "repos")
        self.state.cfg = self.cfg = __import__("dataclasses").replace(self.state.cfg, checkout=self.clone)
        common._CONFIG = self.cfg

    def tearDown(self) -> None:
        self.state.close()

    def seed(self, **scripts: str) -> str:
        (self.clone / "scripts").mkdir(exist_ok=True)
        (self.clone / "scripts" / "published_release.py").write_text("X = 1\n")
        for script in release.HELPERS:
            name = pathlib.Path(script).name
            path = self.clone / script
            if scripts.get(name) is None and name in scripts:
                path.unlink(missing_ok=True)
                continue
            path.write_text(scripts.get(name, self.FIXED))
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "seed")
        return git(self.clone, "rev-parse", "HEAD").strip()

    def worktree(self) -> pathlib.Path:
        return self.cfg.state / "scratch" / "release-self-test" / "tree"

    def test_every_helper_runs_once_and_the_worktree_is_removed(self) -> None:
        ran = []
        real = release._sandboxed_run
        with mock.patch.object(release, "_sandboxed_run", side_effect=lambda *a: ran.append(a[4]) or real(*a)):
            release.self_test(self.cfg, self.seed())
        self.assertEqual(ran, [[release.PYTHON, "-I", script, "--help"] for script in release.HELPERS])
        self.assertFalse(self.worktree().exists())

    def test_a_helper_that_does_not_import_under_isolated_mode_is_blocked_by_name(self) -> None:
        sha = self.seed(**{"fill-release-hashes": self.BROKEN})
        with self.assertRaisesRegex(release.Blocked, "(?s)self-test of scripts/fill-release-hashes.*ModuleNotFoundError"):
            release.self_test(self.cfg, sha)
        self.assertFalse(self.worktree().exists(), "the self-test's own tree goes even when a run failed")
        release.self_test(self.cfg, self.seed())  # and the next run starts clean

    def test_a_missing_helper_is_blocked_by_name(self) -> None:
        with self.assertRaisesRegex(release.Blocked, "has no scripts/docs-sync"):
            release.self_test(self.cfg, self.seed(**{"docs-sync": None}))

    def test_a_step_runs_only_a_helper_the_self_test_covers(self) -> None:
        with self.assertRaisesRegex(common.LaneError, "not in release.HELPERS"):
            release._helper(self.cfg, self.clone, self.state.root, None, "scripts/new-helper")


if __name__ == "__main__":
    unittest.main()
