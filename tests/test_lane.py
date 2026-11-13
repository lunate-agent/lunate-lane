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
        rc, text = run("unpark", "794", "--reason", "decided in #820")
        self.assertEqual(rc, 0)
        self.assertIn("card #794 unparked (was parked (reworks)); idle runs and rework rounds cleared", text)
        self.assertEqual(harness.parked_cards(), frozenset())
        self.assertEqual((harness.idle_runs(794), harness.rework_rounds(794, 813)), (0, 0))
        self.assertIn("not parked", run("unpark", "794", "--reason", "decided in #820")[1])

    def test_unpark_refuses_while_a_worker_runs_the_card(self) -> None:
        harness.park(794, "reworks")
        harness.live_pid.return_value = 4242
        rc, text = run("unpark", "794", "--reason", "decided in #820")
        self.assertEqual(rc, 1)
        self.assertIn("a worker (pid 4242) is running card #794", text)
        self.assertEqual(harness.parked_cards(), frozenset({794}))

    def test_a_running_feeder_tick_is_a_refusal_not_a_race(self) -> None:
        harness.park(794, "reworks")
        with common.job_lock("feed"):
            rc, text = run("unpark", "794", "--reason", "decided in #820")
        self.assertEqual(rc, 1)
        self.assertIn("a feeder tick is running", text)
        self.assertEqual(harness.parked_cards(), frozenset({794}))


class WhoAndWhyTests(unittest.TestCase):
    """4: every park and unpark leaves a line with the time, who ran it and why (nobody could tell who unparked
    #794 overnight)."""

    setUp, tearDown = LaneTests.setUp, LaneTests.tearDown

    def log(self) -> str:
        return (self.state.cfg.state / "logs" / "lane.log").read_text()

    def test_park_and_unpark_log_time_user_and_reason(self) -> None:
        with mock.patch.dict("os.environ", {"LANE_ACTOR": "daniel"}), \
                mock.patch.object(lane.getpass, "getuser", return_value="lane-host-user"):
            run("park", "794", "--reason", "waits on #820")
            run("unpark", "794", "--reason", "#820 decided")
        park, unpark = self.log().strip().splitlines()
        self.assertRegex(park, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d lane: parked card #794 by hand — by daniel as "
                               r"lane-host-user via \S+, (no )?terminal — reason: waits on #820$")
        self.assertRegex(unpark, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d lane: unparked card #794 by hand \(was parked "
                                 r"\(manual\)\) — by daniel as lane-host-user via \S+, (no )?terminal — reason: #820 "
                                 r"decided; idle runs and rework rounds cleared$")

    def test_unpark_needs_a_reason(self) -> None:
        harness.park(794, "reworks")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                lane.main(["unpark", "794"])
        self.assertEqual(run("unpark", "794", "--reason", " ")[0], 1)
        self.assertEqual(harness.parked_cards(), frozenset({794}))

    def test_a_park_lifted_by_hand_is_named_and_one_lifted_by_the_lane_is_not(self) -> None:
        self.assertEqual(harness.lifted_by_hand(), [], "the first check only remembers")
        for card in (794, 804, 780):
            harness.park(card, "reworks")
        harness.park(781, "idle")
        self.assertEqual(harness.lifted_by_hand(), [])
        (self.state.cfg.state / "attempts" / "794.json").unlink()  # what happened to #794 overnight
        run("unpark", "804", "--reason", "fixed by hand")
        harness.note_run(781, new_commit=True)  # a new commit lifts an idle park
        self.assertEqual(harness.lifted_by_hand(), [794])
        self.assertEqual(harness.lifted_by_hand(), [], "said once")

    def test_status_names_how_the_last_run_died(self) -> None:
        harness.note_death(817, "git -c core.fsmonitor=false failed (128): hint: Diverging branches can't be "
                                "fast-forwarded, you need to either:\nhint:\nhint: \tgit merge --no-ff")
        text = run("status", "817")[1]
        self.assertIn("last run: worker died: git -c core.fsmonitor=false failed (128): hint: Diverging branches "
                      "can't be fast-forwarded, you need to either:", text)
        harness.note_run(817, new_commit=False)
        self.assertNotIn("died", run("status", "817")[1], "a session that reached its end clears it")
