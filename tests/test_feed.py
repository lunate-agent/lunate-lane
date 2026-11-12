"""The feeder tick end to end, with GitHub and the worker launch replaced. No network, no worker started."""

import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import feed, github, harness  # noqa: E402
from scripts.common import GhError, StateError  # noqa: E402
from tests.helpers import TempState, card, pull, review  # noqa: E402


class FeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.started = []
        self.patches = [
            mock.patch.object(github, "milestone_cards", return_value=[card(1, "area/tui"), card(2, "kind/bug")]),
            mock.patch.object(github, "open_pulls", return_value=[pull(50, 9)]),
            mock.patch.object(github, "pull", return_value=pull(50, 9, reviews=[review("CHANGES_REQUESTED")])),
            mock.patch.object(harness, "running_workers", return_value={}),
            mock.patch.object(feed, "start", side_effect=lambda cfg, launch: self.started.append(launch) or 1),
            mock.patch("sys.stdout", new=io.StringIO()),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.state.close()

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
            mock.patch.object(feed, "start", side_effect=lambda cfg, launch: self.started.append(launch) or 1),
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
