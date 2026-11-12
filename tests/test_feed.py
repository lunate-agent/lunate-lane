"""The feeder tick end to end, with GitHub and the worker launch replaced. No network, no worker started."""

import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import cleanup, common, feed, github, harness  # noqa: E402
from scripts.common import GhError, StateError  # noqa: E402
from tests.helpers import REQUIRED, TempState, card, pull, review  # noqa: E402


class FeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.started = []
        self.patches = [
            mock.patch.object(github, "milestone_cards", return_value=[card(1, "area/tui"), card(2, "kind/bug")]),
            mock.patch.object(github, "open_pulls", return_value=[pull(50, 9)]),
            mock.patch.object(github, "pull", return_value=pull(50, 9, reviews=[review("CHANGES_REQUESTED")])),
            mock.patch.object(harness, "running_workers", return_value={}),
            mock.patch.object(github, "required_checks", return_value=REQUIRED),
            mock.patch.object(feed, "start", side_effect=lambda cfg, launch: self.started.append(launch) or 1),
            mock.patch.object(cleanup, "tidy"),
            mock.patch("sys.stdout", new=io.StringIO()),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.state.close()

    def test_a_failed_cleanup_fails_the_tick_after_the_launches(self) -> None:
        """#13: tidying runs after the launches; its failure is named, and the work it follows has started."""
        cleanup.tidy.side_effect = StateError("the logs in /x could not be rotated: [Errno 13]")
        self.assertEqual(feed.main(), 1)
        self.assertEqual([l.card for l in self.started], [9, 2, 1])
        self.assertIn("FAILED — the logs in /x could not be rotated", (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_a_fourth_rework_parks_the_card_with_one_comment_instead(self) -> None:
        """#9: three rework rounds on #50, whatever their cause; the fourth is a park, said once."""
        from scripts import open_pr

        for _ in range(3):
            harness.note_rework(9, 50)
        comments = []
        with mock.patch.object(open_pr, "comment", side_effect=lambda n, body: comments.append((n, body))):
            self.assertEqual(feed.main(), 0)
            self.assertEqual(feed.main(), 0)
        self.assertNotIn(50, [l.rework_pr for l in self.started])
        self.assertEqual(harness.parked_details(), {9: "reworks"})
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][0], 50)
        self.assertIn("went back to a worker 3 times (cap: 3)", comments[0][1])
        self.assertIn("attempts/9.json", comments[0][1])
        log = (self.state.cfg.state / "logs" / "feed.log").read_text()
        self.assertIn("parked card #9 after 3 rework rounds on #50", log)
        self.assertIn("#50 needs rework (changes requested) but card #9 is parked", log)
        (self.state.cfg.state / "attempts" / "9.json").unlink()  # a person unparks it
        self.started.clear()
        feed.main()
        self.assertIn(50, [l.rework_pr for l in self.started])

    def test_a_dry_run_only_says_it_would_park(self) -> None:
        for _ in range(3):
            harness.note_rework(9, 50)
        with mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]):
            self.assertEqual(feed.main(), 0)
        self.assertEqual(harness.parked_details(), {})
        self.assertIn("would park card #9 instead of a rework of #50", (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_a_rework_that_fails_before_it_starts_still_counts_and_is_parked_at_the_cap(self) -> None:
        """Review of #17, item 5: every launch counts first, so a rework whose worker dies in its setup (a
        diverged branch, a held worktree, a refused draft) is parked after three ticks instead of forever."""
        from scripts import open_pr

        github.milestone_cards.return_value = []  # only the rework, here
        comments = []
        with mock.patch.object(open_pr, "comment", side_effect=lambda n, body: comments.append(n)):
            for tick in range(1, 6):
                feed.main()
                rounds = harness.rework_rounds(9, 50)
                self.assertEqual(rounds, min(tick, 3), f"tick {tick}")
        self.assertEqual([l.rework_pr for l in self.started].count(50), 3)
        self.assertEqual(harness.parked_details(), {9: "reworks"})
        self.assertEqual(comments, [50])

    def test_a_new_card_whose_setup_keeps_failing_is_parked_after_three_ticks(self) -> None:
        """Every launch counts an idle run before anything can fail: a new card whose worker dies before its
        finish (a held worktree, a closed issue, a broken checkout) is parked on the fourth tick, once, on
        the issue — instead of relaunching every tick."""
        from scripts import open_pr

        github.milestone_cards.return_value = [card(2, "kind/bug")]
        github.open_pulls.return_value = []
        comments = []
        with mock.patch.object(open_pr, "comment", side_effect=lambda n, body: comments.append((n, body))):
            for tick in range(1, 6):
                feed.main()
                self.assertEqual(harness.idle_runs(2), min(tick, 3), f"tick {tick}")
        self.assertEqual([l.card for l in self.started], [2, 2, 2])
        self.assertEqual(harness.parked_details(), {2: "idle"})
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][0], 2, "no pull request: the issue gets the comment")
        self.assertIn("3 worker runs in a row ended with no new commit", comments[0][1])
        log = (self.state.cfg.state / "logs" / "feed.log").read_text()
        self.assertIn("parked card #2 after 3 runs with no new commit", log)
        self.assertIn("#2 is parked after runs with no new commit — left for a person", log)

    def test_a_dry_run_counts_nothing_and_only_says_it_would_park(self) -> None:
        github.milestone_cards.return_value = [card(2)]
        github.open_pulls.return_value = []
        with mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]):
            feed.main()
            self.assertEqual(harness.idle_runs(2), 0)
            for _ in range(3):
                harness.note_attempt(2)
            feed.main()
        self.assertEqual(harness.parked_details(), {})
        self.assertIn("would park card #2 instead of card #2 — 3 runs with no new commit",
                      (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_a_run_that_makes_a_commit_resets_what_its_launch_counted(self) -> None:
        github.milestone_cards.return_value = [card(2)]
        github.open_pulls.return_value = []
        feed.main()
        feed.main()
        self.assertEqual(harness.idle_runs(2), 2)
        harness.note_run(2, True)  # the worker's finish, with a new commit
        self.assertEqual(harness.idle_runs(2), 0)

    def test_a_failed_start_still_counts_the_round(self) -> None:
        feed.start.side_effect = common.LaneError("the worker for #9 could not be started (OSError)")
        self.assertEqual(feed.main(), 1)
        self.assertEqual(harness.rework_rounds(9, 50), 1)

    def test_an_approved_clean_pull_request_that_is_not_armed_is_rearmed(self) -> None:
        """Review of #17: arming failed after mark_ready, and nobody re-armed it."""
        from scripts import open_pr

        github.pull.return_value = pull(50, 9, reviews=[review("APPROVED")], node_id="N50")
        with mock.patch.object(open_pr, "rearm", return_value={"auto_merge": "armed", "queue_method": "SQUASH",
                                                               "merge_method": "MERGE"}) as rearm:
            with mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]):
                feed.main()
            rearm.assert_not_called()
            feed.main()
            rearm.assert_called_once_with(50, "N50", "main")
            github.pull.return_value = pull(50, 9, reviews=[review("APPROVED")], disarmed_by="a-maintainer")
            feed.main()
            self.assertEqual(rearm.call_count, 1, "a person's disarm stands")
        log = (self.state.cfg.state / "logs" / "feed.log").read_text()
        self.assertIn("would re-arm #50", log)
        self.assertIn("re-armed #50: approved at h1 but not armed — auto-merge armed", log)

    def test_a_tick_sweeps_stale_token_files_first(self) -> None:
        from scripts import open_pr

        with mock.patch.object(open_pr, "sweep_secret_dir", return_value=2) as sweep:
            feed.main()
        sweep.assert_called_once_with()
        self.assertIn("removed 2 token file(s) a killed push left behind",
                      (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_two_rework_rounds_still_rework(self) -> None:
        for _ in range(2):
            harness.note_rework(9, 50)
        feed.main()
        self.assertIn(50, [l.rework_pr for l in self.started])

    def infra_pull(self):
        runs = (github.Run("ci-ok", "FAILURE", 1, "u", 4321, True), github.Run("verify", "TIMED_OUT", 2, "u", 4321))
        return pull(50, 9, checks=(("ci-ok", "COMPLETED", "FAILURE"),), runs=runs)

    def test_an_infra_failure_is_rerun_once_then_reworked_if_it_fails_again(self) -> None:
        """Review of #17, item 1: `gh run rerun --failed`, recorded and logged, not a rework round."""
        from scripts import common

        github.pull.return_value = self.infra_pull()
        with mock.patch.object(common, "gh", return_value="") as gh:
            self.assertEqual(feed.main(), 0)
            self.assertEqual(gh.call_args_list[0].args, ("run", "rerun", "4321", "--failed", "-R", "lunate-agent/lunate"))
            self.assertNotIn(50, [l.rework_pr for l in self.started])
            self.assertEqual(harness.reran(), frozenset({(50, "h1")}))
            self.assertEqual(harness.rework_rounds(9, 50), 0)
            feed.main()  # the rerun failed the same way on the same head
            self.assertEqual(gh.call_count, 1, "rerun once per head, never twice")
        self.assertIn(50, [l.rework_pr for l in self.started])
        log = (self.state.cfg.state / "logs" / "feed.log").read_text()
        self.assertIn("reran the failed jobs of run(s) 4321", log)
        self.assertIn("(required checks cancelled or timed out again after a rerun)", log)

    def test_a_dry_run_only_says_it_would_rerun(self) -> None:
        from scripts import common

        github.pull.return_value = self.infra_pull()
        with mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]), mock.patch.object(common, "gh") as gh:
            feed.main()
        gh.assert_not_called()
        self.assertEqual(harness.reran(), frozenset())
        self.assertIn("would rerun the failed jobs of run(s) 4321", (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_a_stuck_pull_request_is_reworked_and_the_log_says_why(self) -> None:
        github.pull.return_value = pull(50, 9, mergeable="CONFLICTING")
        with mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]):
            self.assertEqual(feed.main(), 0)
        self.assertIn("would start rework of #50 for card #9 (conflicts with the base branch)",
                      (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_unreadable_required_checks_start_nothing(self) -> None:
        github.required_checks.side_effect = GhError("no required status checks found on main")
        self.assertEqual(feed.main(), 1)
        self.assertEqual(self.started, [])

    def test_rework_starts_before_new_cards_and_bugs_before_others(self) -> None:
        self.assertEqual(feed.main(), 0)
        self.assertEqual([(l.card, l.rework_pr) for l in self.started], [(9, 50), (2, None), (1, None)])

    def test_an_unreadable_pull_request_list_starts_nothing_and_exits_non_zero(self) -> None:
        github.open_pulls.side_effect = GhError("gh api graphql failed: HTTP 502")
        self.assertEqual(feed.main(), 1)
        self.assertEqual(self.started, [])
        self.assertIn("FAILED — gh api graphql failed", (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_an_unreadable_worker_record_starts_nothing(self) -> None:
        harness.running_workers.side_effect = StateError("the worker record 7.json could not be read")
        self.assertEqual(feed.main(), 1)
        self.assertEqual(self.started, [])

    def test_an_unreadable_pull_request_starts_nothing(self) -> None:
        github.pull.side_effect = GhError("#50 could not be read")
        self.assertEqual(feed.main(), 1)
        self.assertEqual(self.started, [])

    def test_a_malformed_pins_file_starts_nothing(self) -> None:
        self.state.cfg.pins.write_text("{")
        self.assertEqual(feed.main(), 1)
        self.assertEqual(self.started, [])

    def test_a_dry_run_starts_nothing(self) -> None:
        with mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]):
            self.assertEqual(feed.main(), 0)
        self.assertEqual(self.started, [])
        self.assertIn("would start rework of #50", (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_a_tick_that_fills_the_limit_logs_its_launches_before_the_wip_note(self) -> None:
        """#3: five cards start, then the sixth is held — the WIP line comes after the five, not above them."""
        github.milestone_cards.return_value = [card(n) for n in range(780, 786)]
        github.open_pulls.return_value = []
        with mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]):
            self.assertEqual(feed.main(), 0)
        lines = (self.state.cfg.state / "logs" / "feed.log").read_text().splitlines()
        starts = [i for i, line in enumerate(lines) if "would start card" in line]
        wip = [i for i, line in enumerate(lines) if "WIP 5 of 5" in line]
        self.assertEqual(len(starts), 5)
        self.assertEqual(len(wip), 1)
        self.assertGreater(wip[0], max(starts), "the WIP note must follow the launches it explains")

    def test_only_the_worker_identitys_pull_requests_are_read_in_full(self) -> None:
        github.open_pulls.return_value = [pull(50, 9), pull(60, 3, author="someone")]
        feed.main()
        self.assertEqual([c.args[1] for c in github.pull.call_args_list], [50])
        self.assertNotIn(3, [l.card for l in self.started])


class StartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()

    def tearDown(self) -> None:
        self.state.close()

    def test_a_started_worker_is_recorded_with_its_pid_and_argv(self) -> None:
        from scripts import cards

        fake = mock.Mock(pid=4321)
        with mock.patch.object(feed.subprocess, "Popen", return_value=fake) as popen:
            feed.start(self.state.cfg, cards.Launch(12, 99, frozenset({"area/tui"})))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-4:], ["--issue", "12", "--rework", "99"])
        record = (self.state.cfg.state / "workers" / "12.json").read_text()
        self.assertIn('"pid": 4321', record)
        self.assertIn("area/tui", record)


class UnreadableStateTests(unittest.TestCase):
    """A state folder that cannot be listed stops the tick: nothing starts on a count it could not read."""

    def setUp(self) -> None:
        self.state = TempState()
        self.started = []
        self.patches = [
            mock.patch.object(github, "milestone_cards", return_value=[card(1), card(2)]),
            mock.patch.object(github, "open_pulls", return_value=[]),
            mock.patch.object(github, "required_checks", return_value=REQUIRED),
            mock.patch.object(feed, "start", side_effect=lambda cfg, launch: self.started.append(launch) or 1),
            mock.patch.object(cleanup, "tidy"),
            mock.patch.object(sys, "argv", ["feed.py"]),
            mock.patch("sys.stdout", new=io.StringIO()),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.state.close()

    def lock(self, name: str) -> pathlib.Path:
        folder = self.state.cfg.state / name
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o755)
        return folder

    def assert_nothing_started(self, name: str) -> None:
        self.assertEqual(feed.main(), 1)
        self.assertEqual(self.started, [])
        log = (self.state.cfg.state / "logs" / "feed.log").read_text()
        self.assertIn("FAILED — the state folder", log)
        self.assertIn(name, log)

    def test_an_unreadable_attempts_folder_starts_nothing(self) -> None:
        folder = self.lock("attempts")
        with self.assertRaises(StateError):
            harness.parked_cards()
        with mock.patch.object(harness, "running_workers", return_value={}):
            self.assert_nothing_started(str(folder))

    def test_an_unreadable_workers_folder_starts_nothing(self) -> None:
        folder = self.lock("workers")
        with self.assertRaises(StateError):
            harness.running_workers()
        self.assert_nothing_started(str(folder))


if __name__ == "__main__":
    unittest.main()
