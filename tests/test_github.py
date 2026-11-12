"""Reading GitHub's state: incomplete reads raise, checks are judged on the required ones only. No network."""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, github  # noqa: E402
from scripts.common import GhError  # noqa: E402
from tests.helpers import REQUIRED, pull, review  # noqa: E402
from tests.helpers import review as helper_review  # noqa: E402

CFG = common.load_config()


def page(nodes, more=False, cursor="c1"):
    return {"pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": nodes}


def issue_node(number, labels=(), blocked_by=(), blocking=(), labels_more=False):
    return {"number": number, "title": f"t{number}", "createdAt": "2026-11-01T00:00:00Z",
            "labels": page([{"name": n} for n in labels], labels_more),
            "blockedBy": page([{"number": n, "state": s} for n, s in blocked_by]),
            "blocking": page([{"number": n, "state": s} for n, s in blocking])}


class MilestoneTests(unittest.TestCase):
    def answers(self, *issue_pages):
        responses = [{"repository": {"milestones": {"nodes": [{"title": CFG.milestone, "number": 4}]}}}]
        responses += [{"repository": {"issues": p}} for p in issue_pages]
        return mock.patch.object(common, "graphql", side_effect=responses)

    def test_every_page_is_read_and_only_open_edges_count(self) -> None:
        first = page([issue_node(1, ["kind/bug"], blocked_by=[(9, "OPEN"), (8, "CLOSED")])], more=True)
        second = page([issue_node(2, blocking=[(3, "OPEN")])])
        with self.answers(first, second):
            got = github.milestone_cards(CFG)
        self.assertEqual([c.number for c in got], [1, 2])
        self.assertEqual(got[0].blocked_by, (9,))
        self.assertEqual(got[1].blocking, (3,))

    def test_a_missing_milestone_is_a_named_failure(self) -> None:
        with mock.patch.object(common, "graphql", return_value={"repository": {"milestones": {"nodes": []}}}):
            with self.assertRaises(GhError):
                github.milestone_cards(CFG)

    def test_a_truncated_label_list_is_refused(self) -> None:
        with self.answers(page([issue_node(1, ["a"], labels_more=True)])):
            with self.assertRaises(GhError) as caught:
                github.milestone_cards(CFG)
        self.assertIn("more entries than one page", str(caught.exception))

    def test_a_failed_read_propagates(self) -> None:
        with mock.patch.object(common, "graphql", side_effect=GhError("gh api graphql failed: HTTP 502")):
            with self.assertRaises(GhError):
                github.milestone_cards(CFG)


class PullTests(unittest.TestCase):
    def node(self, **over):
        node = {"number": 5, "title": "t", "state": "OPEN", "isDraft": False, "createdAt": "2026-11-12T00:00:00Z",
                "headRefName": "wt/issue-9", "headRefOid": "abc", "mergeable": "MERGEABLE",
                "author": {"login": "lunate-worker"}, "autoMergeRequest": None,
                "files": page([{"path": "src/A.cs"}, {"path": "openspec/specs/agent-loop/spec.md"}]),
                "closingIssuesReferences": page([{"number": 9, "labels": page([{"name": "area/tui"}])}]),
                "reviews": {"pageInfo": {"hasPreviousPage": False},
                            "nodes": [{"author": {"login": "lunate-reviewer"}, "state": "APPROVED", "body": "ok",
                                       "commit": {"oid": "abc"}}]},
                "commits": {"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": {"contexts": page([
                    {"__typename": "CheckRun", "name": "ci-ok", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"__typename": "StatusContext", "context": "legacy", "state": "PENDING"}])}}}]}}
        node.update(over)
        return {"repository": {"pullRequest": node}}

    def test_a_pull_request_is_read_in_full(self) -> None:
        with mock.patch.object(common, "graphql", return_value=self.node()):
            p = github.pull(CFG, 5)
        self.assertEqual((p.card, p.head, p.labels), (9, "abc", frozenset({"area/tui"})))
        self.assertEqual(p.reviews[0].state, "APPROVED")
        self.assertIn(("legacy", "PENDING", "PENDING"), p.checks)
        self.assertEqual(github.check_state(p, REQUIRED), "green")

    def test_its_files_are_read(self) -> None:
        with mock.patch.object(common, "graphql", return_value=self.node()):
            got = github.pull(CFG, 5)
        self.assertEqual(got.files, ("src/A.cs", "openspec/specs/agent-loop/spec.md"))
        self.assertEqual(got.capabilities, frozenset({"agent-loop"}))

    def test_more_reviews_than_one_page_is_refused(self) -> None:
        many = self.node(reviews={"pageInfo": {"hasPreviousPage": True}, "nodes": []})
        with mock.patch.object(common, "graphql", return_value=many):
            with self.assertRaises(GhError):
                github.pull(CFG, 5)

    def test_a_truncated_closing_issue_label_list_is_refused(self) -> None:
        """A dropped area/ label would weaken the one-card-per-area rule: refuse rather than use part of the list."""
        cut = self.node(closingIssuesReferences=page([{"number": 9, "labels": page([{"name": "x"}], more=True)}]))
        with mock.patch.object(common, "graphql", return_value=cut):
            with self.assertRaises(GhError) as caught:
                github.pull(CFG, 5)
        self.assertIn("#9's labels", str(caught.exception))

    def test_a_missing_pull_request_is_a_named_failure(self) -> None:
        with mock.patch.object(common, "graphql", return_value={"repository": {"pullRequest": None}}):
            with self.assertRaises(GhError):
                github.pull(CFG, 5)

    def test_the_card_comes_from_the_branch_then_the_closing_issue(self) -> None:
        self.assertEqual(pull(1, 42).card, 42)
        self.assertEqual(pull(1, None, cards=(7, 8)).card, 7)
        self.assertIsNone(pull(1, None).card)

    def test_bot_logins_compare_in_their_bare_form(self) -> None:
        self.assertEqual(github.login({"login": "lunate-worker[bot]"}), "lunate-worker")
        self.assertEqual(github.login({"login": "lunate-worker"}), "lunate-worker")


class SpecOverlapTests(unittest.TestCase):
    def test_capabilities_come_from_specs_and_change_deltas_including_archived_ones(self) -> None:
        self.assertEqual(github.capabilities([
            "openspec/specs/agent-loop/spec.md",
            "openspec/changes/add-x/specs/agent-tools/spec.md",
            "openspec/changes/archive/2026-11-12-add-y/specs/print-mode/spec.md",
            "openspec/changes/add-x/proposal.md", "src/Lunate/Loop.cs", "docs/openspec/specs/nope/spec.md"]),
            frozenset({"agent-loop", "agent-tools", "print-mode"}))

    def overlap(self, number, others):
        files = {1: ("openspec/specs/agent-loop/spec.md",), 2: ("src/A.cs",),
                 3: ("openspec/changes/x/specs/agent-loop/spec.md",), 4: ("openspec/specs/agent-tools/spec.md",)}
        pulls = {n: pull(n, n, files=f) for n, f in files.items()}
        return github.spec_overlap(pulls[number], [pulls[n] for n in others])

    def test_an_older_open_pull_request_on_the_same_capability_is_named(self) -> None:
        self.assertEqual(self.overlap(3, [1, 2, 4]), (1, frozenset({"agent-loop"})))

    def test_a_newer_or_unrelated_pull_request_does_not_hold_one_back(self) -> None:
        self.assertIsNone(self.overlap(1, [3]))
        self.assertIsNone(self.overlap(4, [1, 2, 3]))
        self.assertIsNone(self.overlap(2, [1, 3, 4]))

    def test_an_older_draft_does_not_hold_a_newer_one_back_but_an_older_ready_one_does(self) -> None:
        mine = pull(3, 3, files=("openspec/changes/x/specs/agent-loop/spec.md",))
        draft = pull(1, 1, files=("openspec/specs/agent-loop/spec.md",), draft=True)
        ready = pull(2, 2, files=("openspec/specs/agent-loop/spec.md",))
        self.assertIsNone(github.spec_overlap(mine, [draft]))
        self.assertEqual(github.spec_overlap(mine, [draft, ready]), (2, frozenset({"agent-loop"})))

    def test_open_pulls_read_their_draft_state(self) -> None:
        node = {"number": 7, "title": "T", "isDraft": True, "author": {"login": "x"}, "headRefName": "b",
                "closingIssuesReferences": page([]), "files": page([])}
        with mock.patch.object(common, "graphql", return_value={"repository": {"pullRequests": page([node])}}):
            self.assertTrue(github.open_pulls(CFG)[0].draft)

    def test_files_that_were_not_read_are_a_refusal_not_no_overlap(self) -> None:
        with self.assertRaises(GhError):
            github.spec_overlap(pull(3, 3, files=None), [])
        with self.assertRaises(GhError):
            github.spec_overlap(pull(3, 3, files=("openspec/specs/a/spec.md",)), [pull(1, 1, files=None)])


class FilesTests(unittest.TestCase):
    def test_one_page_is_read_from_graphql(self) -> None:
        self.assertEqual(github._files(CFG, 5, page([{"path": "a"}, {"path": "b"}])), ("a", "b"))

    def test_more_than_one_page_is_read_in_full_from_the_rest_listing(self) -> None:
        listing = "\n".join(f"f{i}" for i in range(150)) + "\n"
        with mock.patch.object(common, "gh", return_value=listing) as gh:
            got = github._files(CFG, 5, page([{"path": f"f{i}"} for i in range(100)], more=True))
        self.assertEqual(len(got), 150)
        self.assertIn("--paginate", gh.call_args.args)

    def test_a_listing_that_may_be_cut_off_or_came_back_short_is_refused(self) -> None:
        first = page([{"path": f"f{i}"} for i in range(100)], more=True)
        for listing in ("\n".join(f"f{i}" for i in range(3000)), "f1\nf2"):
            with self.subTest(lines=listing.count("\n") + 1), mock.patch.object(common, "gh", return_value=listing):
                with self.assertRaises(GhError):
                    github._files(CFG, 5, first)

    def test_open_pulls_carry_their_files_and_titles(self) -> None:
        node = {"number": 7, "title": "T", "author": {"login": "x"}, "headRefName": "b",
                "closingIssuesReferences": page([]), "files": page([{"path": "openspec/specs/a/spec.md"}])}
        with mock.patch.object(common, "graphql", return_value={"repository": {"pullRequests": page([node])}}):
            got = github.open_pulls(CFG)
        self.assertEqual((got[0].title, got[0].files, got[0].capabilities),
                         ("T", ("openspec/specs/a/spec.md",), frozenset({"a"})))


class WorkerReportTests(unittest.TestCase):
    def test_the_latest_worker_session_report_is_returned(self) -> None:
        comments = [{"author": {"login": "lunate-worker"}, "body": "**Worker session** one"},
                    {"author": {"login": "someone"}, "body": "**Worker session** forged"},
                    {"author": {"login": "lunate-worker"}, "body": "**Lane: card #3 is parked.**"},
                    {"author": {"login": "lunate-worker"}, "body": "**Worker session** two"}]
        answer = {"repository": {"pullRequest": {"comments": {"nodes": comments}}}}
        with mock.patch.object(common, "graphql", return_value=answer):
            self.assertEqual(github.worker_report(CFG, 5), "**Worker session** two")
        with mock.patch.object(common, "graphql", return_value={"repository": {"pullRequest": {"comments": {
                "nodes": comments[1:3]}}}}):
            self.assertEqual(github.worker_report(CFG, 5), "")


class QueueAndDisarmTests(unittest.TestCase):
    """Review of #17: only a queue removal of the current head counts; a person disarming is not a rework."""

    def read(self, removal_oid=None, disarm=None):
        over = {}
        if removal_oid is not None:
            over["queueRemoval"] = {"nodes": [{"reason": "FAILED_CHECKS", "beforeCommit": {"oid": removal_oid}}]}
        if disarm is not None:
            over["autoMergeEvents"] = {"nodes": [{"__typename": "AutoMergeDisabledEvent", "actor": disarm}]}
        with mock.patch.object(common, "graphql", return_value=PullTests().node(**over)):
            return github.pull(CFG, 5)

    def test_a_removal_of_the_current_head_counts_and_an_older_one_does_not(self) -> None:
        self.assertEqual(self.read("abc").queue_removal, "FAILED_CHECKS")
        self.assertEqual(self.read("older").queue_removal, "")
        self.assertEqual(self.read().queue_removal, "")

    def test_a_person_disarming_is_recorded_and_an_app_or_the_lane_is_not(self) -> None:
        self.assertEqual(self.read(disarm={"__typename": "User", "login": "a-maintainer"}).disarmed_by, "a-maintainer")
        self.assertEqual(self.read(disarm={"__typename": "Bot", "login": "github-merge-queue"}).disarmed_by, "")
        self.assertEqual(self.read(disarm={"__typename": "User", "login": "lunate-worker"}).disarmed_by, "")

    def test_neither_a_person_disarming_nor_an_old_removal_is_a_rework(self) -> None:
        approved = [helper_review("APPROVED")]
        for p, expected in ((pull(5, 9, reviews=approved, queue_removal="FAILED_CHECKS"),
                             "out of the merge queue after approval (failed_checks)"),
                            (pull(5, 9, reviews=approved, queue_removal="FAILED_CHECKS", disarmed_by="a-maintainer"), None),
                            (pull(5, 9, reviews=approved, disarmed_by="a-maintainer"), None),
                            (pull(5, 9, reviews=approved), None)):
            with self.subTest(p=(p.queue_removal, p.disarmed_by)):
                self.assertEqual(github.stuck_reason(p, frozenset({"ci-ok"}), "lunate-reviewer"), expected)


class RearmTests(unittest.TestCase):
    def test_only_a_ready_approved_green_unarmed_unqueued_pull_request_is_rearmed(self) -> None:
        ok = dict(reviews=[helper_review("APPROVED")])
        cases = ((pull(5, 9, **ok), True), (pull(5, 9, **ok, auto_merge=True), False),
                 (pull(5, 9, **ok, in_queue=True), False), (pull(5, 9, **ok, draft=True), False),
                 (pull(5, 9, **ok, disarmed_by="a-maintainer"), False),
                 (pull(5, 9, **ok, mergeable="CONFLICTING"), False), (pull(5, 9, **ok, queue_removal="X"), False),
                 (pull(5, 9, **ok, checks=[("ci-ok", "IN_PROGRESS", "")]), False), (pull(5, 9), False),
                 (pull(5, 9, reviews=[helper_review("APPROVED", commit="old")]), False))
        for p, expected in cases:
            with self.subTest(p=p):
                self.assertIs(github.needs_rearm(p, frozenset({"ci-ok"}), "lunate-reviewer"), expected)


class FailureKindTests(unittest.TestCase):
    """Review of #17, item 1: what is the runner's fault (rerun once) and what is a real failure (rework)."""

    def run_node(self, name, conclusion, *, steps=("SUCCESS",), more=False, run_id=77):
        return {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": conclusion,
                "databaseId": 5, "detailsUrl": "u", "checkSuite": {"workflowRun": {"databaseId": run_id}},
                "steps": {"pageInfo": {"hasNextPage": more}, "nodes": [{"conclusion": c} for c in steps]}}

    def kind(self, *contexts):
        node = PullTests().node(commits={"nodes": [{"commit": {"oid": "abc", "statusCheckRollup": {
            "contexts": page(list(contexts))}}}]})
        with mock.patch.object(common, "graphql", return_value=node):
            p = github.pull(CFG, 5)
        return github.failure_kind(p, frozenset({"ci-ok"})), p

    def test_cancelled_legs_behind_a_failed_roll_up_are_infra_and_name_their_run(self) -> None:
        kind, p = self.kind(self.run_node("ci-ok", "FAILURE", steps=("SUCCESS", "FAILURE")),
                            self.run_node("verify (macos, 2/4)", "CANCELLED", steps=("SUCCESS", "CANCELLED")),
                            self.run_node("verify (macos, 3/4)", "TIMED_OUT", steps=()))
        self.assertEqual(kind, "infra")
        self.assertEqual(github.infra_runs(p), (77,))

    def test_a_required_check_that_itself_failed_to_start_is_infra(self) -> None:
        self.assertEqual(self.kind(self.run_node("ci-ok", "STARTUP_FAILURE", steps=()))[0], "infra")

    def test_a_failing_step_or_a_failed_leg_is_real(self) -> None:
        for legs in ([self.run_node("verify", "FAILURE", steps=("FAILURE",))],
                     [self.run_node("verify", "CANCELLED", steps=("FAILURE", "CANCELLED"))],
                     [self.run_node("verify", "CANCELLED"), self.run_node("lint", "FAILURE", steps=("FAILURE",))],
                     []):
            with self.subTest(legs=[l["name"] + "=" + l["conclusion"] for l in legs]):
                self.assertEqual(self.kind(self.run_node("ci-ok", "FAILURE", steps=("FAILURE",)), *legs)[0], "real")

    def test_what_cannot_be_attributed_is_real(self) -> None:
        unread = self.run_node("verify", "CANCELLED", more=True)
        no_run = self.run_node("verify", "CANCELLED", run_id=None)
        for leg in (unread, no_run):
            with self.subTest(leg=leg["steps"]["pageInfo"]):
                self.assertEqual(self.kind(self.run_node("ci-ok", "FAILURE"), leg)[0], "real")

    def test_green_or_pending_is_neither(self) -> None:
        self.assertIsNone(self.kind(self.run_node("ci-ok", "SUCCESS"))[0])


class LandedTests(unittest.TestCase):
    def answer(self, state, oid="abc", parents=1):
        commit = {"oid": oid, "parents": {"totalCount": parents}} if oid else None
        return mock.patch.object(common, "graphql", return_value={"repository": {"pullRequest": {
            "state": state, "mergeCommit": commit}}})

    def test_a_squash_lands_as_one_parent(self) -> None:
        with self.answer("MERGED", "abc", 1):
            self.assertEqual(github.landed_parents(CFG, 801), ("abc", 1))
        with self.answer("MERGED", "def", 2):
            self.assertEqual(github.landed_parents(CFG, 802), ("def", 2))

    def test_a_pull_request_that_did_not_land_is_a_named_failure(self) -> None:
        for state, oid in (("OPEN", None), ("CLOSED", None), ("MERGED", None)):
            with self.subTest(state=state), self.answer(state, oid):
                with self.assertRaises(GhError):
                    github.landed_parents(CFG, 803)


class ChecksTests(unittest.TestCase):
    def state(self, *checks):
        return github.check_state(pull(1, 1, checks=checks), REQUIRED)

    def test_only_required_checks_decide(self) -> None:
        self.assertEqual(self.state(("ci-ok", "COMPLETED", "SUCCESS"), ("lint", "COMPLETED", "FAILURE")), "green")

    def test_a_required_check_not_reported_yet_is_pending_not_green(self) -> None:
        self.assertEqual(self.state(("lint", "COMPLETED", "SUCCESS")), "pending")
        self.assertEqual(self.state(), "pending")

    def test_a_running_required_check_is_pending(self) -> None:
        self.assertEqual(self.state(("ci-ok", "IN_PROGRESS", "")), "pending")

    def test_any_failed_run_of_a_required_check_is_failing(self) -> None:
        self.assertEqual(self.state(("ci-ok", "COMPLETED", "SUCCESS"), ("ci-ok", "COMPLETED", "FAILURE")), "failing")
        self.assertEqual(self.state(("ci-ok", "COMPLETED", "CANCELLED")), "failing")

    def test_no_required_checks_on_the_base_is_a_refusal(self) -> None:
        with mock.patch.object(common, "gh_json", return_value=[{"type": "deletion"}]):
            with self.assertRaises(GhError):
                github.required_checks(CFG)

    def test_rules_that_are_not_a_list_are_refused(self) -> None:
        with mock.patch.object(common, "gh_json", return_value={"message": "Not Found"}):
            with self.assertRaises(GhError):
                github.required_checks(CFG)

    def test_required_contexts_are_read_from_the_rules(self) -> None:
        rules = [{"type": "required_status_checks",
                  "parameters": {"required_status_checks": [{"context": "ci-ok"}, {"context": "docs"}]}}]
        with mock.patch.object(common, "gh_json", return_value=rules):
            self.assertEqual(github.required_checks(CFG), frozenset({"ci-ok", "docs"}))


class VerdictTests(unittest.TestCase):
    def test_only_the_reviewer_at_the_current_head_counts_and_the_latest_wins(self) -> None:
        p = pull(1, 1, head="h2", reviews=[review("APPROVED", commit="h1"), review("CHANGES_REQUESTED", commit="h2"),
                                          review("APPROVED", commit="h2", author="someone")])
        self.assertEqual(github.reviewer_verdict(p, "lunate-reviewer"), "CHANGES_REQUESTED")

    def test_a_dismissed_review_does_not_count_as_reviewed(self) -> None:
        p = pull(1, 1, reviews=[review("DISMISSED")])
        self.assertIsNone(github.reviewer_verdict(p, "lunate-reviewer"))


if __name__ == "__main__":
    unittest.main()
