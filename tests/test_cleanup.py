"""Cleanup after finished cards, log rotation, and review sessions a killed run left behind (#13).

Real repositories and real processes; only GitHub's answers are replaced.
"""

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
from scripts import cleanup, common, github, harness, review, worker  # noqa: E402
from scripts.common import GhError  # noqa: E402
from tests.helpers import TempState, git, origin_and_clone, pull  # noqa: E402


class FinishedCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg
        self.origin, self.clone = origin_and_clone(self.state.root)
        self.out = mock.patch("sys.stdout", new=io.StringIO())
        self.out.start()
        self.addCleanup(self.out.stop)
        self.addCleanup(self.state.close)
        self.states = {}  # card -> [(number, state)]
        gh = mock.patch.object(common, "gh_json", side_effect=self.gh_json)
        gh.start()
        self.addCleanup(gh.stop)
        landed = mock.patch.object(github, "landed_parents", side_effect=lambda cfg, n: ("abcdef0123", 1))
        self.landed = landed.start()
        self.addCleanup(landed.stop)

    def gh_json(self, *args, **_):
        card = int(args[args.index("--head") + 1].split("-")[-1])
        return [{"number": n, "state": s} for n, s in self.states.get(card, [])]

    def card(self, number):
        path = worker.prepare_worktree(self.cfg, number)
        (self.cfg.state / "scratch" / f"issue-{number}").mkdir(parents=True, exist_ok=True)
        return path

    def test_a_merged_or_closed_cards_worktree_branch_and_scratch_go_and_the_rest_stay(self) -> None:
        merged, closed, still_open, running, never = (self.card(n) for n in (1, 2, 3, 4, 5))
        self.states = {1: [(11, "MERGED")], 2: [(12, "CLOSED")], 3: [(13, "OPEN")], 4: [(14, "MERGED")], 5: []}
        cleanup.tidy(self.cfg, "feed", [pull(13, 3)], {4: frozenset()})
        branches = git(self.clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/wt/").splitlines()
        self.assertEqual(sorted(branches), ["wt/issue-3", "wt/issue-4", "wt/issue-5"])
        self.assertFalse(merged.exists())
        self.assertFalse(closed.exists())
        for path in (still_open, running, never):
            self.assertTrue((path / ".git").exists())
        self.assertFalse((self.cfg.state / "scratch" / "issue-1").exists())
        self.assertTrue((self.cfg.state / "scratch" / "issue-3").exists())
        self.assertNotIn("issue-1", git(self.clone, "worktree", "list"))
        log = (self.cfg.state / "logs" / "feed.log").read_text()
        self.assertIn("#11 landed as abcdef012 with 1 parent(s)", log)
        self.assertIn("card #1 is done (#11 merged) — removed worktree", log)

    def test_a_merge_that_is_not_a_squash_is_called_out(self) -> None:
        self.card(1)
        self.states = {1: [(11, "MERGED")]}
        self.landed.side_effect = lambda cfg, n: ("abcdef0123", 2)
        cleanup.tidy(self.cfg, "feed", [], {})
        self.assertIn("NOT a squash", (self.cfg.state / "logs" / "feed.log").read_text())

    def test_a_reopened_card_whose_branch_has_an_open_pull_request_stays(self) -> None:
        path = self.card(6)
        self.states = {6: [(16, "CLOSED"), (26, "OPEN")]}
        cleanup.tidy(self.cfg, "feed", [], {})
        self.assertTrue(path.exists())

    def test_a_dry_run_removes_nothing(self) -> None:
        path = self.card(1)
        self.states = {1: [(11, "MERGED")]}
        cleanup.tidy(self.cfg, "feed", [], {}, dry_run=True)
        self.assertTrue(path.exists())
        self.assertIn("would remove card #1", (self.cfg.state / "logs" / "feed.log").read_text())

    def test_an_unreadable_pull_request_list_removes_nothing(self) -> None:
        path = self.card(1)
        common.gh_json.side_effect = GhError("HTTP 502")
        with self.assertRaises(GhError):
            cleanup.tidy(self.cfg, "feed", [], {})
        self.assertTrue(path.exists())


class LogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.addCleanup(self.state.close)
        self.logs = common.state_dir("logs")

    def test_a_log_over_the_limit_is_rotated_and_the_oldest_dropped(self) -> None:
        log = self.logs / "feed.log"
        for n in range(1, 5):
            log.write_text("x" * 20 + f" generation {n}\n")
            self.assertEqual(cleanup.rotate_logs(limit=10, keep=3), ["feed.log"])
        self.assertFalse(log.exists())
        self.assertEqual([(self.logs / f"feed.log.{n}").read_text().split()[-1] for n in (1, 2, 3)], ["4", "3", "2"])
        self.assertFalse((self.logs / "feed.log.4").exists())

    def test_a_small_log_is_left_alone(self) -> None:
        (self.logs / "review.log").write_text("short\n")
        (self.logs / "worker-3.out").write_text("short\n")
        self.assertEqual(cleanup.rotate_logs(limit=1000), [])

    def test_old_session_logs_are_pruned(self) -> None:
        sessions = common.state_dir("sessions")
        old, new = sessions / "issue-1-1.jsonl", sessions / "issue-2-2.jsonl"
        old.write_text("{}")
        new.write_text("{}")
        os.utime(old, (time.time() - 20 * 86400,) * 2)
        self.assertEqual(cleanup.prune_sessions(days=14), 1)
        self.assertEqual(sorted(p.name for p in sessions.iterdir()), ["issue-2-2.jsonl"])


class ReviewReapTests(unittest.TestCase):
    """A review run SIGKILLed mid-session: its record names the session, which the next tick kills."""

    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg
        self.origin, self.clone = origin_and_clone(self.state.root)
        self.addCleanup(self.state.close)
        out = mock.patch("sys.stdout", new=io.StringIO())
        out.start()
        self.addCleanup(out.stop)

    def test_a_dead_review_runs_session_is_killed_and_its_worktree_removed(self) -> None:
        worktree = common.state_dir("reviews") / "pr-77"
        git(self.clone, "worktree", "add", "-q", "--detach", str(worktree), "origin/main")
        import threading

        session = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", "--session-id",
                                    "review-77-x"], start_new_session=True)
        threading.Thread(target=session.wait, daemon=True).start()  # as launchd reaps a real orphan
        self.addCleanup(lambda: session.poll() is None and session.kill())
        time.sleep(0.3)
        self.assertIsNone(session.poll(), "the stand-in session is running")
        harness.record_review(77, 999999, worktree)  # a driver pid that is not running
        harness.note_review_session(77, 999999, session.pid, "review-77-x")
        review.reap(self.cfg, "review")
        self.assertIsNotNone(session.wait(timeout=30), "the session was killed")
        self.assertFalse(worktree.exists())
        self.assertNotIn("pr-77", git(self.clone, "worktree", "list"))
        self.assertEqual(list((self.cfg.state / "reviewing").iterdir()), [])
        self.assertIn("cleared the review of #77", (self.cfg.state / "logs" / "review.log").read_text())

    def test_a_live_review_run_is_left_alone(self) -> None:
        worktree = common.state_dir("reviews") / "pr-78"
        git(self.clone, "worktree", "add", "-q", "--detach", str(worktree), "origin/main")
        harness.record_review(78, os.getpid(), worktree)
        with mock.patch.object(harness, "review_alive", return_value=True):
            review.reap(self.cfg, "review")
        self.assertTrue(worktree.exists())

    def victim(self):
        victim = self.state.root / "victim"
        victim.mkdir()
        (victim / "precious").write_text("keep\n")
        return victim

    def test_a_forged_record_cannot_make_cleanup_touch_anything_outside(self) -> None:
        """Review of #17, item 3: `…/reviews/../../victim` in a record written by a session."""
        victim = self.victim()
        for forged in (common.state_dir("reviews") / ".." / ".." / "victim", victim):
            with self.subTest(worktree=str(forged)):
                record = self.cfg.state / "reviewing" / "77.json"
                common.state_dir("reviewing")
                record.write_text(json.dumps({"pr": 77, "pid": 999999, "worktree": str(forged)}))
                with self.assertRaises(common.StateError):
                    review.reap(self.cfg, "review")
                self.assertEqual((victim / "precious").read_text(), "keep\n")
                record.unlink()

    def test_a_symlinked_review_worktree_is_refused_not_followed(self) -> None:
        victim = self.victim()
        (common.state_dir("reviews") / "pr-78").symlink_to(victim)
        harness.record_review(78, 999999, self.cfg.state / "reviews" / "pr-78")
        with self.assertRaises(common.StateError):
            review.reap(self.cfg, "review")
        self.assertEqual((victim / "precious").read_text(), "keep\n")

    def test_a_symlinked_card_worktree_is_refused_not_followed(self) -> None:
        victim = self.victim()
        (common.state_dir("worktrees") / "issue-7").symlink_to(victim)
        with self.assertRaises(common.StateError):
            cleanup.remove_card(self.cfg, 7)
        self.assertEqual((victim / "precious").read_text(), "keep\n")

    def test_the_review_run_records_itself_and_clears_its_record(self) -> None:
        seen = {}

        def session(cfg, *, on_start, worktree, log_path, scratch, model, **_):
            seen["model"] = model
            on_start(4242, "review-5-x")
            seen["record"] = json.loads((self.cfg.state / "reviewing" / "5.json").read_text())
            (scratch / "verdict.json").write_text(json.dumps({"event": "APPROVE", "body": "ok"}))
            log_path.write_text("")
            return harness.Outcome(0, False, 1, False)

        head = git(self.clone, "rev-parse", "origin/main")
        with mock.patch.object(harness, "run_session", side_effect=session), \
                mock.patch.object(review.dispatch_card, "card", return_value={"body": "b"}), \
                mock.patch.object(github, "worker_report", return_value=""), \
                mock.patch.object(github, "open_pulls", return_value=[]):
            result = review.run_review(self.cfg, pull(5, 9, head=head, files=("docs/a.md",)))
        self.assertEqual(result, ("APPROVE", "ok", self.cfg.reviewer_light))
        self.assertEqual(seen["model"], self.cfg.reviewer_light, "the session ran on the model the review names")
        self.assertEqual((seen["record"]["pid"], seen["record"]["session_pgid"]), (os.getpid(), 4242))
        self.assertFalse((self.cfg.state / "reviewing" / "5.json").exists())


if __name__ == "__main__":
    unittest.main()
