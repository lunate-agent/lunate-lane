"""Parks a person cannot miss: label and assignee on the card and its pull request, read back; closed cards
cleared; a NEEDS-PERSON that only asks another pull request to merge first becomes a blocked-by wait."""

import io
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import cards, digest, feed, github, harness, lane, open_pr, parked, worker  # noqa: E402
from tests.helpers import REQUIRED, TempState, card, pull, review  # noqa: E402

REASON_807 = ("PR #827 (S7, head `b468752d`) must merge first, or the maintainer must fold this fixed block into "
              "it. Only then can the example and its `run: offline` marker move to "
              "`docs/ci.md#configure-a-machine-without-a-terminal`; that sequencing cannot be decided inside this card")


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.cfg, self.gh = self.state.cfg, self.state.github
        self.out = mock.patch("sys.stdout", new=io.StringIO())
        self.out.start()

    def tearDown(self) -> None:
        self.out.stop()
        self.state.close()

    def has(self, number):
        i = self.gh.issue(number)
        return ("lane/parked" in i["labels"], "wdaniel1993" in i["assignees"])

    def log(self, job="lane"):
        path = self.cfg.state / "logs" / f"{job}.log"
        return path.read_text() if path.exists() else ""


class FlagTests(Case):
    def test_the_card_and_its_open_pull_request_are_labelled_and_assigned_and_read_back(self) -> None:
        self.gh.pulls_of[807] = {826}
        self.assertEqual(parked.flag(self.cfg, 807, job="feed"), [])
        self.assertEqual((self.has(807), self.has(826)), ((True, True), (True, True)))
        self.assertEqual(self.gh.labels_made, ["lane/parked"])
        self.assertEqual(parked.errors(), {})
        self.assertEqual(parked.flagged(), frozenset({807}))
        self.assertIn("parked card #807: labelled lane/parked and assigned @wdaniel1993 on #807, #826 (read back)",
                      self.log("feed"))
        self.assertEqual(parked.flag(self.cfg, 807), [], "again: the label exists already (422) and nothing changes")

    def test_unflag_takes_off_the_label_and_only_the_assignment_the_lane_added(self) -> None:
        self.gh.pulls_of[807] = {826}
        self.gh.issue(826, assignees={"wdaniel1993"})  # assigned by the person before the park
        parked.flag(self.cfg, 807)
        self.assertEqual(parked.unflag(self.cfg, 807), [])
        self.assertEqual((self.has(807), self.has(826)), ((False, False), (False, True)))
        self.assertEqual(parked.flagged(), frozenset())
        self.assertIn("unparked card #807: lane/parked removed from #807, #826, @wdaniel1993 unassigned from #807",
                      self.log())

    def test_an_assignment_github_does_not_keep_is_a_failure_logged_and_in_the_digest(self) -> None:
        self.gh.drop_assignee = True
        failed = parked.flag(self.cfg, 836, job="feed")
        self.assertEqual(failed, ["#836 reads back without assignee @wdaniel1993"])
        self.assertIn("parked card #836: FAILED to label lane/parked / assign @wdaniel1993 — #836 reads back", self.log("feed"))
        self.assertEqual(parked.errors(), {836: "#836 reads back without assignee @wdaniel1993"})
        text = digest.compose(self.cfg, [], [], [], REQUIRED, digest.datetime.datetime.now(digest.datetime.timezone.utc),
                              {836: ("manual", "ruleset change")}, flag_errors=parked.errors())
        self.assertIn("  card #836: lane/parked / @wdaniel1993 FAILED — #836 reads back without assignee @wdaniel1993",
                      text)
        self.gh.drop_assignee = False
        parked.reconcile(self.cfg, {836}, "feed")  # retried each tick until it reads back
        self.assertEqual((parked.errors(), self.has(836)), ({}, (True, True)))

    def test_a_refused_call_never_raises_and_is_recorded(self) -> None:
        self.gh.fail.add("/assignees")
        failed = parked.flag(self.cfg, 837)
        self.assertEqual(len(failed), 1)
        self.assertIn("(403)", failed[0])
        self.assertIn(837, parked.errors())
        self.gh.fail.clear()
        parked.flag(self.cfg, 837)
        self.gh.fail.add("/labels/")
        self.assertTrue(parked.unflag(self.cfg, 837))
        self.assertTrue(parked.errors()[837].startswith("unpark: "))
        self.assertEqual(parked.flagged(), frozenset({837}), "kept so the next tick tries again")
        self.gh.fail.clear()
        parked.reconcile(self.cfg, set(), "feed")
        self.assertEqual((parked.flagged(), self.has(837)), (frozenset(), (False, False)))

    def test_reconcile_backfills_every_parked_card_and_clears_every_unparked_one(self) -> None:
        for number, reason in ((807, "person"), (836, "manual"), (837, "manual")):
            harness.park(number, reason, "x")
        parked.reconcile(self.cfg, harness.parked_cards(), "feed")
        self.assertEqual([self.has(n) for n in (807, 836, 837)], [(True, True)] * 3)
        calls = len(self.gh.calls)
        parked.reconcile(self.cfg, harness.parked_cards(), "feed")
        self.assertEqual(len(self.gh.calls), calls, "a clean flag is not redone every tick")
        harness.unpark(837)  # however it was lifted
        parked.reconcile(self.cfg, harness.parked_cards(), "feed")
        self.assertEqual(self.has(837), (False, False))


class ParkPathTests(Case):
    def test_every_park_comment_also_labels_and_assigns(self) -> None:
        with mock.patch.object(open_pr, "comment") as comment:
            for why, detail in (("idle", ""), ("reworks", ""), ("person", "a decision"), ("ready", "no ready run"),
                                ("secret", "a token")):
                with self.subTest(why=why):
                    harness.park(900, why, detail)
                    worker.park(self.cfg, 900, 950, why, 3, detail)
                    self.assertEqual((self.has(900), self.has(950)), ((True, True), (True, True)))
                    harness.unpark(900)
                    parked.unflag(self.cfg, 900)
        self.assertEqual(comment.call_count, 5, "the park comment stays")

    def test_lane_park_and_unpark_set_and_clear_them_and_say_so(self) -> None:
        with mock.patch.dict("os.environ", {"LANE_ACTOR": "test"}):
            out = io.StringIO()
            with mock.patch("sys.stdout", new=out):
                self.assertEqual(lane.main(["park", "836", "--reason", "ruleset change"]), 0)
            self.assertEqual(self.has(836), (True, True))
            self.gh.fail.add("/labels/")
            out = io.StringIO()
            with mock.patch("sys.stdout", new=out):
                self.assertEqual(lane.main(["unpark", "836", "--reason", "done"]), 0)
            self.assertIn("FAILED to set or clear lane/parked / @wdaniel1993: #836", out.getvalue())
            self.assertNotIn(836, harness.parked_cards())

    def test_a_closed_card_is_unparked_and_cleared(self) -> None:
        harness.park(804, "person", "x")
        harness.park(836, "manual", "y")
        harness.park(817, "person", "z")
        parked.reconcile(self.cfg, harness.parked_cards(), "feed")
        self.gh.issue(804, state="closed")
        self.gh.issue(817, state="closed")
        with mock.patch.object(harness, "live_pid", side_effect=lambda c: 1 if c == 817 else None):
            self.assertEqual(parked.clear_closed(self.cfg, harness.parked_cards(), "feed", dry_run=True), [])
            self.assertEqual(parked.clear_closed(self.cfg, harness.parked_cards(), "feed"), [804])
        parked.reconcile(self.cfg, harness.parked_cards(), "feed")
        self.assertEqual(harness.parked_cards(), frozenset({817, 836}), "a running worker's card is left")
        self.assertEqual((self.has(804), self.has(836)), ((False, False), (True, True)))
        self.assertIn("would clear the park of card #804: it is closed", self.log("feed"))
        self.assertIn("cleared the park of card #804: it is closed", self.log("feed"))
        self.assertEqual(harness.lifted_by_hand(), [], "the lane lifted it, not a hand")


class DeferTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.gh.issue(827, pull_request={"merged_at": None})
        self.gh.pr_card[827] = 755

    def test_807_waits_on_827s_card_instead_of_parking(self) -> None:
        self.assertEqual(parked.defer(self.cfg, 807, REASON_807, "feed", dry_run=True), parked.WAIT)
        self.assertEqual(self.gh.blocked, {}, "a dry run records nothing")
        self.assertEqual(parked.defer(self.cfg, 807, REASON_807, "feed"), parked.WAIT)
        self.assertEqual(self.gh.blocked, {807: {755}})
        posts = [c for c in self.gh.calls if c[0] == "POST"]
        self.assertEqual(parked.defer(self.cfg, 807, REASON_807, "feed"), parked.WAIT)
        self.assertEqual([c for c in self.gh.calls if c[0] == "POST"], posts, "linked once")
        self.assertIn("card #807 waits on #755 (NEEDS-PERSON only asked #827 to merge first): blocked-by recorded",
                      self.log("feed"))

    def test_merged_already_is_a_rework_and_closed_unmerged_or_anything_else_parks(self) -> None:
        self.gh.issue(827, state="closed", pull_request={"merged_at": "2026-11-13T20:00:00Z"})
        self.assertEqual(parked.defer(self.cfg, 807, REASON_807, "feed"), parked.DONE)
        self.gh.issue(827, state="closed", pull_request={"merged_at": None})
        self.assertEqual(parked.defer(self.cfg, 807, REASON_807, "feed"), parked.PARK)
        self.assertEqual(parked.defer(self.cfg, 807, "the maintainer must decide the sequencing", "feed"), parked.PARK)
        self.assertEqual(parked.defer(self.cfg, 807, "#827 and #828 must merge first", "feed"), parked.PARK)
        self.assertEqual(self.gh.blocked, {})

    def test_a_card_reference_waits_on_the_card_and_a_link_that_does_not_take_parks(self) -> None:
        self.assertEqual(parked.defer(self.cfg, 807, "waits on #755", "feed"), parked.WAIT)
        self.assertEqual(self.gh.blocked, {807: {755}})
        self.gh.fail.add("dependencies")
        self.assertEqual(parked.defer(self.cfg, 808, "card #755 must land first.", "feed"), parked.PARK)
        self.assertIn("card #808: could not record it as blocked by #755's card", self.log("feed"))


class DeferFeedTests(Case):
    """The review asked for #827 to merge first: the card waits instead of parking, then is reworked."""

    def setUp(self) -> None:
        super().setUp()
        self.gh.issue(827, pull_request={"merged_at": None})
        self.gh.pr_card[827] = 755
        self.started = []
        body = "**Blocking**\nNEEDS-PERSON: " + REASON_807
        self.p = pull(826, 807, reviews=[review("CHANGES_REQUESTED", body=body)], node_id="PR_826")
        self.patches = [mock.patch.object(github, "milestone_cards", return_value=[]),
                        mock.patch.object(github, "open_pulls", return_value=[self.p]),
                        mock.patch.object(github, "pull", side_effect=lambda cfg, n: self.p),
                        mock.patch.object(harness, "running_workers", return_value={}),
                        mock.patch.object(github, "required_checks", return_value=REQUIRED),
                        mock.patch.object(feed, "start", side_effect=lambda cfg, l: self.started.append(l) or 1),
                        mock.patch.object(feed.cleanup, "tidy"), mock.patch.object(open_pr, "comment")]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        super().tearDown()

    def test_it_waits_then_is_reworked_once_827_merged(self) -> None:
        self.assertEqual(feed.main(), 0)
        self.assertEqual((self.started, harness.parked_cards(), self.gh.blocked), ([], frozenset(), {807: {755}}))
        self.p = pull(826, 807, reviews=self.p.reviews, node_id="PR_826", waits_on=(755,))
        self.assertEqual(feed.main(), 0)
        self.assertEqual(self.started, [])
        self.gh.issue(827, state="closed", pull_request={"merged_at": "2026-11-13T20:00:00Z"})
        self.gh.issue(755, state="closed")
        self.p = pull(826, 807, reviews=self.p.reviews, node_id="PR_826")
        self.assertEqual(feed.main(), 0)
        self.assertEqual([(l.card, l.rework_pr, l.reason) for l in self.started], [(807, 826, "changes requested")])
        self.assertEqual(harness.parked_cards(), frozenset())

    def test_the_worker_saying_so_waits_too(self) -> None:
        worker.park_for_person(self.cfg, 807, 826, REASON_807)
        self.assertEqual((harness.parked_cards(), self.gh.blocked), (frozenset(), {807: {755}}))
        worker.park_for_person(self.cfg, 807, 826, "the maintainer must decide")
        self.assertEqual(harness.parked_cards(), frozenset({807}))
        self.assertEqual(self.has(807), (True, True))

    def test_its_draft_is_not_relaunched_while_the_card_waits(self) -> None:
        draft = pull(826, 807, draft=True, waits_on=(755,))
        launches, notes = cards.plan(cards=[], open_pulls=[draft], worker_pulls=[draft], running={},
                                     rules=feed.rules(self.cfg), required=REQUIRED)
        self.assertEqual(launches, [])
        self.assertIn("#826 is a draft and card #807 waits on #755 — resumes when they close", notes)
        launches, _ = cards.plan(cards=[], open_pulls=[draft], worker_pulls=[pull(826, 807, draft=True)], running={},
                                 rules=feed.rules(self.cfg), required=REQUIRED)
        self.assertEqual([l.reason for l in launches], [cards.ORPHANED])


class MergeFirstTests(unittest.TestCase):
    def test_only_a_single_merge_first_dependency_is_read(self) -> None:
        for text, expected in ((REASON_807, 827), ("waits on #755", 755), ("#755 has to land first.", 755),
                               ("blocked by card #755", 755), ("- **NEEDS** PR #12 should be merged first", None),
                               ("PR #12 should be merged first", 12), ("the maintainer must decide on #12", None),
                               ("#1 must merge first and #2 too", None), ("#5 must merge first; see #6", None),
                               ("", None)):
            with self.subTest(text=text):
                self.assertEqual(github.merge_first(text), expected)


class SeamTests(unittest.TestCase):
    """The seams a test replaces must themselves reach GitHub's helpers (the first live backfill found `_call`
    calling itself: every test had replaced it)."""

    def test_each_seam_calls_through_to_its_helper(self) -> None:
        from scripts import common
        with mock.patch.object(open_pr, "_api", return_value={"ok": 1}) as api, \
                mock.patch.object(open_pr, "_worker_token", return_value="t") as token, \
                mock.patch.object(common, "gh_json", return_value={"state": "open"}) as gh_json, \
                mock.patch.object(github, "open_pulls", return_value=[pull(826, 807), pull(9, 3)]), \
                mock.patch.object(github, "open_blockers", return_value=(755,)), \
                mock.patch.object(github, "pull", return_value=pull(827, 755)):
            self.assertEqual(parked._call("t", "POST", "/x", {"a": 1}), {"ok": 1})
            api.assert_called_once_with("t", "POST", "/x", {"a": 1})
            self.assertEqual(parked._token(), "t")
            token.assert_called_once_with()
            self.assertEqual(parked._get("repos/o/r/issues/1"), {"state": "open"})
            gh_json.assert_called_once_with("api", "repos/o/r/issues/1")
            cfg = common.load_config()
            self.assertEqual(parked._card_pulls(cfg, 807), {826})
            self.assertEqual(parked._blockers(cfg, 807), (755,))
            self.assertEqual(parked._pr_card(cfg, 827), 755)


class DeathParksTests(Case):
    """A worker that dies is retried, not parked — but each death is an idle run (counted at launch, never reset
    by a run that did not reach its end), so after the third death in a row the next tick parks the card through
    the normal idle path: comment, label, assignment, digest."""

    def test_the_third_death_in_a_row_parks_the_card_as_idle(self) -> None:
        import os
        from scripts import dispatch_card

        launched = []

        def start(cfg, launch):  # the feeder's own record, then the real worker, whose run dies by name
            launched.append(launch.card)
            harness.record_worker(launch.card, os.getpid(), launch.areas, launch.rework_pr)
            # this process stands in for the launched `worker.py --issue <n>` the feeder recorded
            with mock.patch.object(sys, "argv", ["worker.py", "--issue", str(launch.card)]), \
                    mock.patch.object(harness, "worker_alive", side_effect=lambda pid, c: pid == os.getpid()), \
                    mock.patch.object(worker, "drive", side_effect=worker.WorktreeError("worktree in the way")):
                self.assertEqual(worker.main(), 1)
            return os.getpid()

        comments = []
        with mock.patch.object(github, "milestone_cards", return_value=[card(817)]), \
                mock.patch.object(github, "open_pulls", return_value=[]), \
                mock.patch.object(harness, "running_workers", return_value={}), \
                mock.patch.object(github, "required_checks", return_value=REQUIRED), \
                mock.patch.object(dispatch_card, "card", return_value={"labels": []}), \
                mock.patch.object(feed, "start", side_effect=start), mock.patch.object(feed.cleanup, "tidy"), \
                mock.patch.object(open_pr, "comment", side_effect=lambda n, body: comments.append((n, body))):
            for death in (1, 2, 3):
                self.assertEqual(feed.main(), 0)
                self.assertEqual((harness.idle_runs(817), harness.died_info()), (death, {817: "worktree in the way"}))
                self.assertEqual(harness.parked_cards(), frozenset(), "a death is retried, not parked")
                self.assertEqual(self.has(817), (False, False))
            self.assertEqual(feed.main(), 0)
        self.assertEqual(launched, [817, 817, 817], "no fourth run")
        self.assertEqual(harness.parked_details(), {817: "idle"})
        self.assertEqual(self.has(817), (True, True))
        self.assertEqual(len(comments), 1)
        self.assertIn("3 worker runs in a row ended with no new commit (cap: 3)", comments[0][1])
        self.assertIn("parked card #817 after 3 runs with no new commit", self.log("feed"))
        text = digest.compose(self.cfg, [card(817)], [], [], REQUIRED,
                              digest.datetime.datetime.now(digest.datetime.timezone.utc), harness.parked_info(),
                              died=harness.died_info(), flag_errors=parked.errors())
        self.assertIn("  parked #817: runs with no new commit https://github.com/lunate-agent/lunate/issues/817", text)


class SweepClosedTests(Case):
    """#780 on 2026-11-14: closed at 21:34 (UTC 20:34) on the 13th, then labelled lane/parked and assigned by the
    worker identity during the backfill at 22:22, with no flag record — so reconcile never saw it."""

    def shape_780(self):
        self.gh.issue(780, state="closed", labels={"kind/bug", "lane/parked"}, assignees={"wdaniel1993"},
                      events=[{"event": "assigned", "actor": {"login": "lunate-worker[bot]"},
                               "assignee": {"login": "wdaniel1993"}}])

    def test_780_loses_the_label_and_the_lanes_assignment(self) -> None:
        self.shape_780()
        self.assertEqual(parked.sweep_closed(self.cfg, frozenset(), "feed"), [780])
        self.assertEqual(self.has(780), (False, False))
        self.assertIn("kind/bug", self.gh.issue(780)["labels"], "only the lane's label goes")
        self.assertIn("closed #780: lane/parked removed, @wdaniel1993 unassigned (read back)", self.log("feed"))
        self.assertEqual(parked.sweep_closed(self.cfg, frozenset(), "feed"), [], "nothing left the next tick")

    def test_an_assignment_a_person_made_stays(self) -> None:
        self.gh.issue(781, state="closed", labels={"lane/parked"}, assignees={"wdaniel1993"},
                      events=[{"event": "assigned", "actor": {"login": "wdaniel1993"}, "assignee": {"login": "wdaniel1993"}}])
        self.assertEqual(parked.sweep_closed(self.cfg, frozenset(), "feed"), [781])
        self.assertEqual(self.has(781), (False, True))

    def test_a_closed_pull_request_of_a_parked_card_is_cleared_and_the_open_card_kept(self) -> None:
        self.gh.pulls_of[798] = {850}
        parked.flag(self.cfg, 798, 850)
        self.gh.issue(850, state="closed")
        self.assertEqual(parked.sweep_closed(self.cfg, frozenset({798}), "feed"), [850])
        self.assertEqual((self.has(798), self.has(850)), ((True, True), (False, False)))
        self.assertEqual(parked._read()[798]["targets"], [798])
        self.gh.issue(799, state="closed", labels={"lane/parked"})  # a parked card itself is clear_closed's job
        self.assertEqual(parked.sweep_closed(self.cfg, frozenset({798, 799}), "feed"), [])

    def test_a_refusal_is_logged_and_never_stops_the_tick(self) -> None:
        self.shape_780()
        self.gh.fail.add("/issues/780/labels")
        self.assertEqual(parked.sweep_closed(self.cfg, frozenset(), "feed"), [])
        self.assertIn("FAILED to clear closed #780 of lane/parked", self.log("feed"))
        with mock.patch.object(parked, "_get", side_effect=parked.common.GhError("gh api failed (1): HTTP 502")):
            self.assertEqual(parked.sweep_closed(self.cfg, frozenset(), "feed"), [])
        self.assertIn("FAILED to list the closed issues still labelled lane/parked", self.log("feed"))

    def test_the_feeder_sweeps_each_tick_and_a_dry_run_only_says_so(self) -> None:
        self.shape_780()
        with mock.patch.object(github, "milestone_cards", return_value=[]), \
                mock.patch.object(github, "open_pulls", return_value=[]), \
                mock.patch.object(harness, "running_workers", return_value={}), \
                mock.patch.object(github, "required_checks", return_value=REQUIRED), \
                mock.patch.object(feed.cleanup, "tidy"):
            with mock.patch.object(sys, "argv", ["feed.py", "--dry-run"]):
                self.assertEqual(feed.main(), 0)
            self.assertEqual(self.has(780), (True, True))
            self.assertIn("would remove lane/parked from closed #780", self.log("feed"))
            with mock.patch.object(sys, "argv", ["feed.py"]):
                self.assertEqual(feed.main(), 0)
        self.assertEqual(self.has(780), (False, False))


class ParkDisarmsTests(Case):
    """#856 (card #850), 2026-11-14: parked at 15:20 after 3 rework rounds, its auto-merge armed since 15:05 —
    green and approved, it would have merged with its card parked. Every park now turns auto-merge off."""

    def test_856s_park_turns_auto_merge_off_and_reads_it_back(self) -> None:
        self.gh.pulls_of[850] = {856}
        self.gh.pulls[856] = {"state": "open", "auto_merge": {"enabled_by": {"login": "lunate-worker"}}}
        self.assertEqual(parked.flag(self.cfg, 850, 856, job="feed"), [])
        self.assertEqual(self.gh.disarmed, [(856, "PR_856")])
        self.assertIsNone(self.gh.pulls[856]["auto_merge"])
        self.assertIn("auto-merge turned off on #856 (read back)", self.log("feed"))

    def test_a_pull_request_not_armed_is_left_alone_and_unpark_never_arms(self) -> None:
        self.gh.pulls_of[850] = {856}
        parked.flag(self.cfg, 850, 856)
        self.assertEqual(self.gh.disarmed, [])
        parked.unflag(self.cfg, 850)
        self.assertIsNone(self.gh.pulls[856]["auto_merge"], "unparking does not re-arm: the normal path does")

    def test_a_disarm_that_does_not_read_back_is_a_failure_for_the_digest_and_is_retried(self) -> None:
        self.gh.pulls_of[850] = {856}
        self.gh.pulls[856] = {"state": "open", "auto_merge": {"enabled_by": {"login": "lunate-worker"}}}
        self.gh.fail.add("/pulls/856/auto-merge")
        failed = parked.flag(self.cfg, 850, 856)
        self.assertEqual(failed, ["#856: #856's auto-merge still reads as on after turning it off"])
        self.assertIn(850, parked.errors())
        self.gh.fail.clear()
        parked.reconcile(self.cfg, {850}, "feed")  # the next tick tries again
        self.assertEqual(parked.errors(), {})
        self.assertIsNone(self.gh.pulls[856]["auto_merge"])

    def test_every_park_path_disarms(self) -> None:
        """The feeder's rework-cap park goes through worker.park, which flags; so do idle, person, ready, secret
        and lane.py park."""
        self.gh.pulls_of[850] = {856}
        self.gh.pulls[856] = {"state": "open", "auto_merge": {"enabled_by": {"login": "lunate-worker"}}}
        harness.park(850, "reworks")
        worker.park(self.cfg, 850, 856, "reworks", 3)
        self.assertEqual(self.gh.disarmed, [(856, "PR_856")])
