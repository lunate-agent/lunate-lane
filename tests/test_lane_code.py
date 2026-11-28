"""The lane's own checkout behind origin (#789 parked on 2026-11-28 on a case #77 had fixed on 2026-11-25: nothing had
pulled ~/dev/lunate-lane). The feeder compares it at each tick and says so; the digest puts it under "Waiting on
you". Nothing pulls it."""

import io
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import cleanup, common, digest, feed, github, harness  # noqa: E402
from scripts.common import GitError  # noqa: E402
from tests.helpers import REQUIRED, TempState, git, origin_and_clone  # noqa: E402

REAL_LANE_BEHIND = common.lane_behind  # TempState stubs it for every other test
NOW = __import__("datetime").datetime(2026, 11, 28, 7, 0, tzinfo=__import__("datetime").timezone.utc)


class LaneBehindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.addCleanup(self.state.close)
        self.origin, self.clone = origin_and_clone(self.state.root)
        self.seed = self.state.root / "seed"

    def advance_origin(self, n: int) -> None:
        for i in range(n):
            (self.seed / f"f{i}").write_text(f"{i}\n")
            git(self.seed, "add", f"f{i}")
            git(self.seed, "commit", "-q", "-m", f"merge {i}")
        subprocess.run(["git", "-C", str(self.seed), "push", "-q", str(self.origin), "main"], check=True)

    def test_it_counts_what_origin_has_that_the_checkout_does_not_and_never_pulls(self) -> None:
        head = git(self.clone, "rev-parse", "HEAD")
        self.assertEqual(REAL_LANE_BEHIND(self.clone)[0], 0)
        self.advance_origin(3)
        behind, at, origin = REAL_LANE_BEHIND(self.clone)
        self.assertEqual((behind, at), (3, head))
        self.assertEqual(origin, git(self.seed, "rev-parse", "HEAD"))
        self.assertEqual(git(self.clone, "rev-parse", "HEAD"), head, "fetched, never pulled")

    def test_an_unreachable_origin_is_a_git_error(self) -> None:
        git(self.clone, "remote", "set-url", "origin", str(self.state.root / "gone.git"))
        with self.assertRaises(GitError):
            REAL_LANE_BEHIND(self.clone)


class FeedLaneCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.addCleanup(self.state.close)
        for patch in (mock.patch.object(github, "milestone_cards", return_value=[]),
                      mock.patch.object(github, "open_pulls", return_value=[]),
                      mock.patch.object(harness, "running_workers", return_value={}),
                      mock.patch.object(github, "required_checks", return_value=REQUIRED),
                      mock.patch.object(cleanup, "tidy"), mock.patch("sys.stdout", new=io.StringIO())):
            patch.start()
            self.addCleanup(patch.stop)
        self.log = self.state.cfg.state / "logs" / "feed.log"

    def test_a_checkout_behind_origin_is_logged_each_tick_and_waits_on_you(self) -> None:
        with mock.patch.object(common, "lane_behind", return_value=(2, "c" * 40, "d" * 40)):
            self.assertEqual(feed.main(), 0)
            self.assertEqual(feed.main(), 0)
        lines = [l for l in self.log.read_text().splitlines() if "behind origin/main" in l]
        self.assertEqual(len(lines), 2, "every tick, not once")
        self.assertIn(f"lane code 2 commit(s) behind origin/main (at ccccccccc, origin at ddddddddd) — `git -C "
                      f"{common.ROOT} pull --ff-only origin main` to take it", lines[0])
        self.assertEqual(harness.lane_code()["behind"], 2)
        text = digest.compose(self.state.cfg, [], [], [], REQUIRED, NOW, {}, lane_code=harness.lane_code())
        self.assertEqual(text.splitlines()[1:3], ["Waiting on you (1):",
                                                  "  lane code 2 commit(s) behind origin/main: pull "
                                                  + digest._home(common.ROOT)])

    def test_an_up_to_date_checkout_says_nothing_and_clears_the_line(self) -> None:
        harness.note_lane_code(2, "c" * 40, "d" * 40)
        self.assertEqual(feed.main(), 0)  # TempState's stub: 0 behind
        self.assertNotIn("behind origin/main", self.log.read_text())
        self.assertEqual(harness.lane_code()["behind"], 0)
        text = digest.compose(self.state.cfg, [], [], [], REQUIRED, NOW, {}, lane_code=harness.lane_code())
        self.assertEqual(text.splitlines()[1], "Waiting on you: nothing")

    def test_a_failed_fetch_is_logged_and_the_tick_goes_on(self) -> None:
        with mock.patch.object(common, "lane_behind", side_effect=GitError("git fetch failed (128): no network")):
            self.assertEqual(feed.main(), 0)
        self.assertIn("could not compare the lane code with origin/main: git fetch failed (128): no network",
                      self.log.read_text())
        self.assertEqual(harness.lane_code(), {})

    def test_a_dry_run_logs_it_but_records_nothing(self) -> None:
        with mock.patch.object(common, "lane_behind", return_value=(1, "c" * 40, "d" * 40)), \
                mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]):
            self.assertEqual(feed.main(), 0)
        self.assertIn("lane code 1 commit(s) behind origin/main", self.log.read_text())
        self.assertEqual(harness.lane_code(), {})


if __name__ == "__main__":
    unittest.main()
