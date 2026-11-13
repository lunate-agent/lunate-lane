"""The person's commands: status, park and unpark one card. No network."""

import io
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, digest, harness, lane  # noqa: E402
from tests.helpers import TempState  # noqa: E402


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = lane.main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class LaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.live = mock.patch.object(harness, "live_pid", return_value=None)
        self.live.start()

    def tearDown(self) -> None:
        self.live.stop()
        self.state.close()

    def test_status_shows_attempts_rounds_and_why_it_is_parked(self) -> None:
        harness.note_attempt(794)
        for _ in range(3):
            harness.note_rework(794, 813)
        harness.park(794, "reworks")
        rc, text = run("status", "794")
        self.assertEqual(rc, 0)
        for line in ("card #794", "worker: none running", "idle runs: 1 of 3", "rework rounds: #813: 3 of 3",
                     "parked: yes — reworks"):
            self.assertIn(line, text)

    def test_park_with_a_reason_shows_in_status_and_the_digest(self) -> None:
        rc, text = run("park", "780", "--reason", "waits on the API decision")
        self.assertEqual((rc, text.strip().splitlines()[-1]), (0, "card #780 parked: waits on the API decision"))
        self.assertEqual(harness.parked_info(), {780: ("manual", "waits on the API decision")})
        self.assertIn("parked: yes — manual: waits on the API decision", run("status", "780")[1])
        self.assertEqual(digest.parked_line(harness.parked_info()[780]), "parked by hand: waits on the API decision")
        self.assertIn("parked card #780 by hand", (self.state.cfg.state / "logs" / "lane.log").read_text())

    def test_park_needs_a_reason_and_refuses_an_already_parked_card(self) -> None:
        self.assertEqual(run("park", "780", "--reason", "  ")[0], 1)
        harness.park(780, "idle")
        rc, text = run("park", "780", "--reason", "x")
        self.assertEqual(rc, 1)
        self.assertIn("already parked (idle)", text)
        self.assertEqual(harness.parked_info(), {780: ("idle", "")})

    def test_unpark_clears_both_counts(self) -> None:
        harness.note_attempt(794)
        harness.note_rework(794, 813)
        harness.park(794, "reworks")
        rc, text = run("unpark", "794")
        self.assertEqual(rc, 0)
        self.assertIn("card #794 unparked (was parked (reworks)); idle runs and rework rounds cleared", text)
        self.assertEqual(harness.parked_cards(), frozenset())
        self.assertEqual((harness.idle_runs(794), harness.rework_rounds(794, 813)), (0, 0))
        self.assertIn("not parked", run("unpark", "794")[1])

    def test_unpark_refuses_while_a_worker_runs_the_card(self) -> None:
        harness.park(794, "reworks")
        harness.live_pid.return_value = 4242
        rc, text = run("unpark", "794")
        self.assertEqual(rc, 1)
        self.assertIn("a worker (pid 4242) is running card #794", text)
        self.assertEqual(harness.parked_cards(), frozenset({794}))

    def test_a_running_feeder_tick_is_a_refusal_not_a_race(self) -> None:
        harness.park(794, "reworks")
        with common.job_lock("feed"):
            rc, text = run("unpark", "794")
        self.assertEqual(rc, 1)
        self.assertIn("a feeder tick is running", text)
        self.assertEqual(harness.parked_cards(), frozenset({794}))
