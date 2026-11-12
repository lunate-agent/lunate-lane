"""Every driver git write to the shared checkout runs under one lock (the first live tick lost #780's launch to
two concurrent `git worktree add`s racing for .git/config.lock). Real processes, real repositories."""

import io
import json
import os
import pathlib
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import cleanup, common, review, worker  # noqa: E402
from tests.helpers import TempState, git, origin_and_clone  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
SETUP = """
import sys
sys.path.insert(0, sys.argv[1])
from scripts import common, worker
print(worker.prepare_worktree(common.config(), int(sys.argv[2])))
"""
HOLD = """
import sys, time
sys.path.insert(0, sys.argv[1])
from scripts import common
with common.checkout_lock():
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


class CheckoutLockCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg
        self.addCleanup(self.state.close)
        self.origin, self.clone = origin_and_clone(self.state.root)
        out = mock.patch("sys.stdout", new=io.StringIO())
        out.start()
        self.addCleanup(out.stop)
        data = json.loads((ROOT / "lane.json").read_text())
        data["paths"].update(checkout=str(self.clone), state=str(self.cfg.state),
                             app_config=str(self.cfg.app_config), pins=str(self.cfg.pins))
        self.config = self.state.root / "lane.json"
        self.config.write_text(json.dumps(data))
        self.env = {**os.environ, "LANE_CONFIG": str(self.config)}

    def spawn(self, script, *args):
        return subprocess.Popen([sys.executable, "-c", script, str(ROOT), *map(str, args)], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def hold(self, seconds):
        holder = self.spawn(HOLD, seconds)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        self.addCleanup(holder.wait)
        return holder


class ConcurrentSetupTests(CheckoutLockCase):
    def test_two_setups_at_once_both_succeed_and_write_no_config(self) -> None:
        """The live failure, run for real: setups started together, round after round."""
        card = 100
        for round_ in range(4):
            procs = [(card + i, self.spawn(SETUP, card + i)) for i in range(4)]
            card += 4
            for number, proc in procs:
                out, err = proc.communicate(timeout=120)
                with self.subTest(round=round_, card=number):
                    self.assertEqual(proc.returncode, 0, err[-600:])
                    self.assertTrue((self.cfg.state / "worktrees" / f"issue-{number}" / ".git").exists())
        tracking = subprocess.run(["git", "-C", str(self.clone), "config", "--get-regexp", r"^branch\.wt/"],
                                  capture_output=True, text=True).stdout
        self.assertEqual(tracking, "", "--no-track: cutting a worktree never writes .git/config")
        self.assertFalse((self.clone / ".git" / "config.lock").exists())


class SerialisationTests(CheckoutLockCase):
    def assert_waits(self, step) -> None:
        holder = self.hold(1.5)
        started = time.monotonic()
        step()
        self.assertGreaterEqual(time.monotonic() - started, 1.2, "the step did not wait for the checkout lock")
        holder.wait(timeout=30)

    def test_card_setup_waits_for_the_lock(self) -> None:
        self.assert_waits(lambda: worker.prepare_worktree(self.cfg, 7))

    def test_cleanup_removal_waits_for_the_lock(self) -> None:
        worker.prepare_worktree(self.cfg, 8)
        self.assert_waits(lambda: cleanup.remove_card(self.cfg, 8))
        self.assertEqual(git(self.clone, "branch", "--list", "wt/issue-8"), "")

    def test_review_worktree_removal_waits_for_the_lock(self) -> None:
        worktree = common.state_dir("reviews") / "pr-9"
        git(self.clone, "worktree", "add", "-q", "--detach", str(worktree), "origin/main")
        self.assert_waits(lambda: review.remove_review_worktree(self.cfg, 9))
        self.assertFalse(worktree.exists())

    def test_a_lock_held_too_long_is_a_named_failure(self) -> None:
        self.hold(3)
        with self.assertRaises(common.StateError) as caught:
            with common.checkout_lock(timeout=0.5):
                pass
        self.assertIn("checkout.lock stayed held", str(caught.exception))

    def test_the_lock_is_reentrant_within_a_run(self) -> None:
        with common.checkout_lock(timeout=1):
            with common.checkout_lock(timeout=1):
                worker.prepare_worktree(self.cfg, 10)
        with common.checkout_lock(timeout=1):  # and released afterwards
            pass


if __name__ == "__main__":
    unittest.main()
