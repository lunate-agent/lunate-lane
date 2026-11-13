"""Reading GitHub's state: incomplete reads raise, checks are judged on the required ones only. No network."""

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, github  # noqa: E402
from scripts import review as review_job  # noqa: E402
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
                "commits": {"nodes": [{"commit": {"oid": "abc", "checkSuites": page([]), "statusCheckRollup": {"contexts": page([
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


AGG = "Every required job succeeded"


def suite(suite_id, status, created, run, attempt=1, workflow=7):
    return {"databaseId": suite_id, "status": status, "createdAt": created,
            "workflowRun": {"databaseId": run, "runAttempt": attempt, "workflow": {"databaseId": workflow}}}


def check(name, status, conclusion, run_id, of, failing_step=None):
    steps = [{"name": failing_step, "conclusion": "FAILURE"}] if failing_step else [{"name": "s", "conclusion": "SUCCESS"}]
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion, "databaseId": run_id,
            "detailsUrl": "u", "checkSuite": of, "steps": {"pageInfo": {"hasNextPage": False}, "nodes": steps}}


class NewestCheckTests(unittest.TestCase):
    """Each required check is judged by its newest check run on the head, as GitHub does; a newer run or attempt
    of the same workflow that is queued or in progress keeps it pending."""

    REQUIRED = frozenset({"ci-ok"})

    def read(self, suites, contexts):
        node = PullTests().node(commits={"nodes": [{"commit": {"oid": "abc", "checkSuites": page(suites),
                                                               "statusCheckRollup": {"contexts": page(contexts)}}}]},
                                reviews={"pageInfo": {"hasPreviousPage": False}, "nodes": []})  # not reviewed yet
        with mock.patch.object(common, "graphql", return_value=node):
            return github.pull(CFG, 5)

    def draft_run(self):
        draft = suite(1, "COMPLETED", "2026-11-12T21:24:54Z", 43310020882)
        return draft, [check("ci-ok", "COMPLETED", "FAILURE", 100, draft, AGG),
                       check("verify (macos-latest, 1/4)", "COMPLETED", "SKIPPED", 101, draft)]

    def test_815_and_813s_shape_reads_pending_while_the_ready_run_is_queued_or_running(self) -> None:
        """Opened as a draft (ci-ok fails on purpose), then marked ready on the same head."""
        draft, contexts = self.draft_run()
        queued = suite(2, "QUEUED", "2026-11-12T21:25:58Z", 43310195848)
        running = {**queued, "status": "IN_PROGRESS"}
        for ready, extra in ((queued, []), (running, [check("verify (macos-latest, 1/4)", "IN_PROGRESS", None,
                                                             200, running)])):
            with self.subTest(ready=ready["status"]):
                p = self.read([draft, ready], contexts + extra)
                self.assertEqual(github.check_state(p, self.REQUIRED), "pending")
                self.assertIsNone(github.stuck_reason(p, self.REQUIRED, "lunate-reviewer"))
                self.assertIn("pending", review_job.skip_reason(p, self.REQUIRED, {}, "lunate-reviewer"))

    def test_the_same_shape_reads_green_and_is_due_for_review_once_the_ready_run_passes(self) -> None:
        draft, contexts = self.draft_run()
        done = suite(2, "COMPLETED", "2026-11-12T21:25:58Z", 43310195848)
        p = self.read([draft, done], contexts + [check("verify (macos-latest, 1/4)", "COMPLETED", "SUCCESS", 200, done),
                                                 check("ci-ok", "COMPLETED", "SUCCESS", 201, done)])
        self.assertEqual(github.check_state(p, self.REQUIRED), "green")
        self.assertEqual([c for c in p.checks if c[0] == "ci-ok"], [("ci-ok", "COMPLETED", "SUCCESS")])
        self.assertIsNone(review_job.skip_reason(p, self.REQUIRED, {}, "lunate-reviewer"))

    def test_a_rerun_of_failed_jobs_is_pending_then_green_after_a_cancelled_first_attempt(self) -> None:
        """Same workflow run, attempt 2: the suite is re-queued, attempt 1's runs stay on the head."""
        first = [lambda s: check("verify (macos-latest, 2/4)", "COMPLETED", "CANCELLED", 90, s),
                 lambda s: check("ci-ok", "COMPLETED", "FAILURE", 100, s, AGG)]
        rerunning = suite(1, "IN_PROGRESS", "2026-11-12T21:24:54Z", 43310020882, attempt=2)
        p = self.read([rerunning], [f(rerunning) for f in first])
        self.assertEqual(github.check_state(p, self.REQUIRED), "pending")
        rerun = suite(1, "COMPLETED", "2026-11-12T21:24:54Z", 43310020882, attempt=2)
        p = self.read([rerun], [f(rerun) for f in first] + [
            check("verify (macos-latest, 2/4)", "COMPLETED", "SUCCESS", 150, rerun),
            check("ci-ok", "COMPLETED", "SUCCESS", 160, rerun)])
        self.assertEqual(github.check_state(p, self.REQUIRED), "green")
        self.assertEqual(sorted((r.name, r.conclusion) for r in p.runs),
                         [("ci-ok", "SUCCESS"), ("verify (macos-latest, 2/4)", "SUCCESS")], "attempt 1 is superseded")

    def test_a_newer_run_that_really_fails_is_failing_and_real(self) -> None:
        draft, contexts = self.draft_run()
        done = suite(2, "COMPLETED", "2026-11-12T21:25:58Z", 43310195848)
        p = self.read([draft, done], contexts + [
            check("verify (macos-latest, 1/4)", "COMPLETED", "FAILURE", 200, done, "Test slice"),
            check("ci-ok", "COMPLETED", "FAILURE", 201, done, AGG)])
        self.assertEqual(github.check_state(p, self.REQUIRED), "failing")
        self.assertEqual(github.failure_kind(p, self.REQUIRED), "real")
        self.assertEqual(github.stuck_reason(p, self.REQUIRED, "lunate-reviewer"), "required checks failing")

    def test_another_workflow_still_running_does_not_hold_ci_ok_back(self) -> None:
        draft, contexts = self.draft_run()
        codeql = suite(3, "IN_PROGRESS", "2026-11-12T21:30:00Z", 99, workflow=8)
        p = self.read([draft, codeql], contexts)
        self.assertEqual(github.check_state(p, self.REQUIRED), "failing")

    def test_more_check_suites_than_one_page_is_refused(self) -> None:
        draft, contexts = self.draft_run()
        node = PullTests().node(commits={"nodes": [{"commit": {"oid": "abc", "checkSuites": page([draft], more=True),
                                                               "statusCheckRollup": {"contexts": page(contexts)}}}]})
        with mock.patch.object(common, "graphql", return_value=node), self.assertRaises(GhError):
            github.pull(CFG, 5)


class QueueAndDisarmTests(unittest.TestCase):
    """Review of #17: only a queue removal of the current head counts; a person disarming is not a rework.

    The removal's `beforeCommit` is the merge group's commit, never the head (#824: 94545e80 is the queue's squash
    of f6343b65), so a removal counts for the head when it came after the head's commit."""

    HEAD = {"commit": {"oid": "abc", "committedDate": "2026-11-13T09:31:26Z", "checkSuites": page([]),
                       "statusCheckRollup": {"contexts": page([
                           {"__typename": "CheckRun", "name": "ci-ok", "status": "COMPLETED", "conclusion": "SUCCESS"}])}}}

    def read(self, removed_at=None, disarm=None, actor=None, in_queue=False, mergeable="MERGEABLE"):
        over = {"commits": {"nodes": [self.HEAD]}, "isInMergeQueue": in_queue, "mergeable": mergeable}
        if removed_at is not None:
            over["queueRemoval"] = {"nodes": [{"createdAt": removed_at, "reason": "failed_checks",
                                               "actor": actor or {"__typename": "Bot", "login": "github-merge-queue"}}]}
        if disarm is not None:
            over["autoMergeEvents"] = {"nodes": [{"__typename": "AutoMergeDisabledEvent", "actor": disarm}]}
        ejection = github.Ejection("infra", "failed_checks", str(removed_at), "x")
        with mock.patch.object(common, "graphql", return_value=PullTests().node(**over)), \
                mock.patch.object(github, "queue_ejection", return_value=ejection) as classify:
            p = github.pull(CFG, 5)
        self.classified = classify.call_args
        return p

    def test_a_removal_after_the_heads_commit_counts_and_an_older_one_does_not(self) -> None:
        p = self.read("2026-11-13T18:11:07Z")
        self.assertEqual((p.queue_removal, p.queue_removed_at), ("failed_checks", "2026-11-13T18:11:07Z"))
        self.assertEqual(p.ejection.kind, "infra")
        self.assertEqual(self.classified.args[1:], (5, "failed_checks", "2026-11-13T18:11:07Z", "MERGEABLE"))
        for older in ("2026-11-13T09:00:00Z", None):
            with self.subTest(older=older):
                p = self.read(older)
                self.assertEqual((p.queue_removal, p.ejection), ("", None))
                self.assertIsNone(self.classified)

    def test_back_in_the_queue_is_not_an_ejection(self) -> None:
        p = self.read("2026-11-13T18:11:07Z", in_queue=True)
        self.assertEqual((p.queue_removal, p.ejection), ("failed_checks", None))
        self.assertIsNone(self.classified)

    def test_a_person_taking_it_out_of_the_queue_is_their_decision(self) -> None:
        p = self.read("2026-11-13T18:11:07Z", actor={"__typename": "User", "login": "a-maintainer"})
        self.assertEqual((p.queue_removal, p.dequeued_by, p.ejection), ("", "a-maintainer", None))
        self.assertFalse(github.needs_enqueue(p, REQUIRED, "lunate-reviewer"))

    def test_a_person_disarming_is_recorded_and_an_app_or_the_lane_is_not(self) -> None:
        self.assertEqual(self.read(disarm={"__typename": "User", "login": "a-maintainer"}).disarmed_by, "a-maintainer")
        self.assertEqual(self.read(disarm={"__typename": "Bot", "login": "github-merge-queue"}).disarmed_by, "")
        self.assertEqual(self.read(disarm={"__typename": "User", "login": "lunate-worker"}).disarmed_by, "")

    def test_only_a_real_queue_failure_or_a_queue_conflict_is_a_rework(self) -> None:
        approved = [helper_review("APPROVED")]
        real = github.Ejection("real", "failed_checks", "t", "w", 1, "verify (macos-latest, 2/4)", 2, ("Verify (bash)",))
        infra = github.Ejection("infra", "failed_checks", "t", "w", 1)
        conflict = github.Ejection("conflict", "merge_conflict", "t", "w")
        for p, expected in ((pull(5, 9, reviews=approved, queue_removal="failed_checks", ejection=real),
                             "queue failure: verify (macos-latest, 2/4)"),
                            (pull(5, 9, reviews=approved, queue_removal="failed_checks", ejection=infra), None),
                            (pull(5, 9, reviews=approved, queue_removal="merge_conflict", ejection=conflict,
                                  mergeable="CONFLICTING"), "queue conflict"),
                            (pull(5, 9, reviews=approved, mergeable="CONFLICTING"), "conflicts with the base branch"),
                            (pull(5, 9, reviews=approved, queue_removal="failed_checks", ejection=real,
                                  disarmed_by="a-maintainer"), None),
                            (pull(5, 9, reviews=approved, disarmed_by="a-maintainer"), None),
                            (pull(5, 9, reviews=approved), None)):
            with self.subTest(p=(p.ejection, p.mergeable, p.disarmed_by)):
                self.assertEqual(github.stuck_reason(p, frozenset({"ci-ok"}), "lunate-reviewer"), expected)


class EnqueueTests(unittest.TestCase):
    def test_only_a_ready_approved_green_mergeable_unqueued_pull_request_is_enqueued(self) -> None:
        ok = dict(reviews=[helper_review("APPROVED")])
        infra = github.Ejection("infra", "failed_checks", "t", "w", 1)
        cases = ((pull(5, 9, **ok), True), (pull(5, 9, **ok, auto_merge=True), True),
                 (pull(5, 9, **ok, in_queue=True), False), (pull(5, 9, **ok, draft=True), False),
                 (pull(5, 9, **ok, disarmed_by="a-maintainer"), False), (pull(5, 9, **ok, dequeued_by="a-maintainer"), False),
                 (pull(5, 9, **ok, mergeable="CONFLICTING"), False), (pull(5, 9, **ok, mergeable="UNKNOWN"), False),
                 (pull(5, 9, **ok, queue_removal="failed_checks", ejection=infra), False),
                 (pull(5, 9, **ok, checks=[("ci-ok", "IN_PROGRESS", "")]), False), (pull(5, 9), False),
                 (pull(5, 9, reviews=[helper_review("APPROVED", commit="old")]), False))
        for p, expected in cases:
            with self.subTest(p=p):
                self.assertIs(github.needs_enqueue(p, frozenset({"ci-ok"}), "lunate-reviewer"), expected)


class FailureKindTests(unittest.TestCase):
    """Review of #17, item 1: what is the runner's fault (rerun once) and what is a real failure (rework)."""

    def run_node(self, name, conclusion, *, steps=("SUCCESS",), more=False, run_id=77):
        """`steps`: conclusions, or (name, conclusion) pairs."""
        nodes = [{"name": s[0], "conclusion": s[1]} if isinstance(s, tuple) else {"name": "step", "conclusion": s}
                 for s in steps]
        return {"__typename": "CheckRun", "name": name, "status": "COMPLETED", "conclusion": conclusion,
                "databaseId": 5, "detailsUrl": "u", "checkSuite": {"workflowRun": {"databaseId": run_id}},
                "steps": {"pageInfo": {"hasNextPage": more}, "nodes": nodes}}

    def ci_ok(self, failing_step="Every required job succeeded", conclusion="FAILURE", run_id=77):
        steps = [("Set up job", "SUCCESS"), ("Checkout", "SUCCESS"), ("Every required job succeeded", "SUCCESS"),
                 ("Post Checkout", "SUCCESS"), ("Complete job", "SUCCESS")]
        steps = [(n, "FAILURE" if n == failing_step else c) for n, c in steps]
        return self.run_node("ci-ok", conclusion, steps=tuple(steps), run_id=run_id)

    def leg(self, name, conclusion, failing=None, run_id=77):
        steps = [("Set up job", "SUCCESS"), ("Checkout", "SUCCESS"), ("Restore", "SUCCESS"),
                 ("Test slice", failing or ("CANCELLED" if conclusion == "CANCELLED" else "SUCCESS")),
                 ("Complete job", "SKIPPED" if conclusion == "CANCELLED" else "SUCCESS")]
        return self.run_node(name, conclusion, steps=tuple(steps), run_id=run_id)

    def shape_813(self, *extra):
        return (self.run_node("plan", "SUCCESS"), self.run_node("changes", "SUCCESS"),
                self.leg("verify (macos-latest, 1/4)", "SUCCESS"), self.leg("verify (macos-latest, 2/4)", "CANCELLED"),
                self.leg("verify (macos-latest, 3/4)", "CANCELLED"), self.leg("verify (macos-latest, 4/4)", "SUCCESS"),
                self.leg("verify (ubuntu-latest, 1/4)", "SUCCESS"), self.run_node("verify-docs", "SKIPPED"),
                *extra, self.ci_ok())

    def test_813s_exact_shape_is_infra_and_names_its_run(self) -> None:
        """Two cancelled macOS legs, ci-ok red on its aggregate step and nothing else: the runner's fault."""
        kind, p = self.kind(*self.shape_813())
        self.assertEqual(kind, "infra")
        self.assertEqual(github.infra_runs(p), (77,))

    def test_a_real_failing_leg_next_to_cancelled_ones_is_real(self) -> None:
        self.assertEqual(self.kind(*self.shape_813(self.leg("verify (ubuntu-latest, 2/4)", "FAILURE",
                                                            failing="FAILURE")))[0], "real")

    def test_ci_ok_failing_anywhere_but_its_aggregate_step_or_over_green_jobs_is_real(self) -> None:
        cases = {
            "ci-ok fails at checkout": (self.leg("verify (macos, 2/4)", "CANCELLED"), self.ci_ok("Checkout")),
            "ci-ok red, every job green (e.g. a draft)": (self.leg("verify (macos, 2/4)", "SUCCESS"), self.ci_ok()),
            "the cancelled job is another workflow's": (self.leg("verify (macos, 2/4)", "CANCELLED", run_id=88),
                                                        self.ci_ok()),
            "a cancelled job with a failed step": (self.leg("verify (macos, 2/4)", "CANCELLED", failing="FAILURE"),
                                                  self.ci_ok()),
            "ci-ok's steps unread": (self.leg("verify (macos, 2/4)", "CANCELLED"),
                                     {**self.ci_ok(), "steps": {"pageInfo": {"hasNextPage": True}, "nodes": []}}),
        }
        for name, runs in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.kind(*runs)[0], "real")

    def kind(self, *contexts):
        node = PullTests().node(commits={"nodes": [{"commit": {"oid": "abc", "checkSuites": page([]), "statusCheckRollup": {
            "contexts": page(list(contexts))}}}]})
        with mock.patch.object(common, "graphql", return_value=node):
            p = github.pull(CFG, 5)
        return github.failure_kind(p, frozenset({"ci-ok"})), p

    def test_cancelled_legs_and_a_cancelled_required_check_are_infra_and_name_their_run(self) -> None:
        kind, p = self.kind(self.run_node("ci-ok", "CANCELLED", steps=("SUCCESS", "CANCELLED")),
                            self.run_node("verify (macos, 2/4)", "CANCELLED", steps=("SUCCESS", "CANCELLED")),
                            self.run_node("verify (macos, 3/4)", "TIMED_OUT", steps=()))
        self.assertEqual(kind, "infra")
        self.assertEqual(github.infra_runs(p), (77,))

    def test_a_required_check_that_failed_for_real_is_never_infra_even_behind_cancelled_legs(self) -> None:
        """Review of #17, round two, item 4: #813's shape — ci-ok FAILURE, the legs under it cancelled."""
        for own in (self.run_node("ci-ok", "FAILURE", steps=("SUCCESS", "FAILURE")),
                    self.run_node("ci-ok", "FAILURE", steps=("SUCCESS",)),
                    self.run_node("ci-ok", "CANCELLED", steps=("FAILURE",))):
            with self.subTest(own=(own["conclusion"], [s["conclusion"] for s in own["steps"]["nodes"]])):
                kind, _ = self.kind(own, self.run_node("verify (macos, 2/4)", "CANCELLED"),
                                    self.run_node("verify (macos, 3/4)", "TIMED_OUT", steps=()))
                self.assertEqual(kind, "real")

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


class CiRunsTests(unittest.TestCase):
    """B: the CI workflow's pull-request runs on a head, as GitHub returned them for #816's head 3d35f9855."""

    @staticmethod
    def suite(run_id, conclusion, workflow="CI", plan=None, event="pull_request"):
        return {"status": "COMPLETED", "conclusion": conclusion, "createdAt": "2026-11-12T22:43:19Z",
                "workflowRun": {"databaseId": run_id, "event": event, "workflow": {"name": workflow}},
                "checkRuns": {"nodes": [plan] if plan else []}}

    def answer(self, *suites, more=False):
        return {"repository": {"object": {"checkSuites": page(list(suites), more)}}}

    def test_816s_suites_read_as_one_cancelled_ready_run_and_one_draft_run(self) -> None:
        data = self.answer(
            self.suite(43322494831, "CANCELLED", plan={"status": "COMPLETED", "conclusion": "CANCELLED"}),
            self.suite(43322494914, "FAILURE", plan={"status": "COMPLETED", "conclusion": "SKIPPED"}),
            self.suite(43322495063, "SUCCESS", workflow="Process"),
            self.suite(43322495125, "SUCCESS", workflow="pack"),
            {"status": "COMPLETED", "conclusion": "SUCCESS", "createdAt": "t", "workflowRun": None,
             "checkRuns": {"nodes": []}})
        with mock.patch.object(common, "graphql", return_value=data) as read:
            runs = github.ci_runs(common.config(), "3d35f9855" + "0" * 31)
        self.assertEqual(read.call_args.kwargs["job"], "plan")
        self.assertEqual([(r.id, r.conclusion, r.ready_job) for r in runs],
                         [(43322494831, "CANCELLED", "CANCELLED"), (43322494914, "FAILURE", "SKIPPED")])
        ready = [r.id for r in runs if github.ready_run(r, frozenset())]
        self.assertEqual(ready, [43322494831])

    def test_a_run_without_its_ready_job_is_ready_only_when_it_came_after_marking_ready(self) -> None:
        queued = github.CiRun(7, "QUEUED", "", "t", "")
        self.assertTrue(github.ready_run(queued, frozenset({6})))
        self.assertFalse(github.ready_run(queued, frozenset({7})))

    def test_an_unreadable_commit_or_a_cut_off_list_raises(self) -> None:
        for data in ({"repository": {"object": None}}, self.answer(self.suite(1, "SUCCESS"), more=True)):
            with self.subTest(data=data), mock.patch.object(common, "graphql", return_value=data):
                with self.assertRaises(GhError):
                    github.ci_runs(common.config(), "a" * 40)


class NeedsPersonTests(unittest.TestCase):
    """C: the NEEDS-PERSON line, in the forms a model writes it."""

    def test_the_reason_is_read_from_the_first_line_that_says_it(self) -> None:
        for text, reason in (
                ("done.\nNEEDS-PERSON: a maintainer must pick the default locale\nmore", "a maintainer must pick the default locale"),
                ("**NEEDS-PERSON:** check 4 needs a real release", "check 4 needs a real release"),
                ("`NEEDS-PERSON` — the failure is in src/Other.cs, untouched", "the failure is in src/Other.cs, untouched"),
                ("NEEDS-PERSON", "(no reason given)")):
            with self.subTest(text=text):
                self.assertEqual(github.needs_person(text), reason)

    def test_no_line_is_none(self) -> None:
        for text in ("", "all checks met", "needs a person? no"):
            self.assertIsNone(github.needs_person(text))

    def test_a_mention_mid_sentence_is_not_a_line(self) -> None:
        # #818's review, which parked card #749 with the reason "item."
        review_818 = ("Decisions 1–9 are consistent with the issue and the checked-in deltas. **Decision 10 is "
                      "incorrect:** “no release note” does not waive the required per-PR fragment. Add "
                      "`changes/818.md` as above; this is a worker-fixable omission, not a `NEEDS-PERSON` item.")
        self.assertIsNone(github.needs_person(review_818))
        self.assertIsNone(github.needs_person("Do not write NEEDS-PERSON: here, it is fixable."))
        self.assertIsNone(github.needs_person("- not a NEEDS-PERSON item"))

    def test_a_line_may_open_with_a_list_marker_or_a_quote(self) -> None:
        for text in ("findings\n- NEEDS-PERSON: the ruleset change", "> **NEEDS-PERSON:** the ruleset change",
                     "  NEEDS-PERSON: the ruleset change"):
            with self.subTest(text=text):
                self.assertEqual(github.needs_person(text), "the ruleset change")


class PersonOutranksTests(unittest.TestCase):
    SHAPE = dict(head="5809330d1", reviews=[helper_review("DISMISSED", commit="5809330d1"),
                                            helper_review("APPROVED", commit="5809330d1", author="a-maintainer")])

    def test_813s_shape_is_enqueued_when_ready_and_green(self) -> None:
        self.assertTrue(github.needs_enqueue(pull(813, 794, **self.SHAPE), REQUIRED, "lunate-reviewer"))
        self.assertEqual(github.person_approved(pull(813, 794, **self.SHAPE), "lunate-reviewer"), "a-maintainer")

    def test_not_enqueued_as_a_draft_or_red_or_already_queued(self) -> None:
        for over in (dict(draft=True), dict(checks=(("ci-ok", "COMPLETED", "FAILURE"),)), dict(in_queue=True)):
            with self.subTest(over=over):
                self.assertFalse(github.needs_enqueue(pull(813, 794, **self.SHAPE, **over), REQUIRED, "lunate-reviewer"))

    def test_the_bots_own_approval_alone_is_not_a_person(self) -> None:
        p = pull(813, 794, reviews=[helper_review("APPROVED")])
        self.assertEqual(github.verdict(p, "lunate-reviewer"), "APPROVED")
        self.assertEqual(github.person_approved(p, "lunate-reviewer"), "")


class DecisionReadTests(unittest.TestCase):
    """3 and 5: decision issues, oldest first with what they block; the Decision: comment of a closed blocker."""

    def test_open_decisions_come_oldest_first_with_the_open_cards_they_block(self) -> None:
        nodes = [{"number": 822, "title": "b", "createdAt": "2026-11-05T00:00:00Z", "blocking": page([])},
                 {"number": 820, "title": "a", "createdAt": "2026-11-01T00:00:00Z",
                  "blocking": page([{"number": 794, "state": "OPEN"}, {"number": 700, "state": "CLOSED"}])}]
        with mock.patch.object(common, "graphql", return_value={"repository": {"issues": page(nodes)}}) as gql:
            got = github.decision_issues(CFG)
        self.assertEqual([(d.number, d.blocking) for d in got], [(820, (794,)), (822, ())])
        self.assertEqual(gql.call_args.kwargs["label"], "kind/decision")

    def blocker(self, number, state, labels, comments, older=False):
        return {"number": number, "title": f"decide {number}", "state": state, "labels": page([{"name": n} for n in labels]),
                "comments": {"pageInfo": {"hasPreviousPage": older},
                             "nodes": [{"body": b, "authorAssociation": a} for a, b in comments]}}

    def read(self, *blockers):
        answer = {"repository": {"issue": {"blockedBy": page(list(blockers))}}}
        with mock.patch.object(common, "graphql", return_value=answer):
            return github.decisions_for(CFG, 794)

    def test_the_latest_maintainer_decision_of_each_closed_decision_blocker_verbatim(self) -> None:
        got = self.read(
            self.blocker(820, "CLOSED", ["kind/decision"], [
                ("OWNER", "Decision: the lane opens release PRs."),
                ("NONE", "Decision: the lane merges them too."),  # not a maintainer
                ("OWNER", "Thinking about it more."),
                ("OWNER", "Decision: the lane opens release PRs; a maintainer merges them.\n\n- keep `--write`")]),
            self.blocker(821, "OPEN", ["kind/decision"], [("OWNER", "Decision: still open, not decided")]),
            self.blocker(700, "CLOSED", ["kind/feature"], [("OWNER", "Decision: not a decision issue")]),
            self.blocker(823, "CLOSED", ["kind/decision"], [("MEMBER", "closing, see chat")]))
        self.assertEqual(got, [
            github.Decision(820, "decide 820", "Decision: the lane opens release PRs; a maintainer merges them.\n\n"
                                               "- keep `--write`"),
            github.Decision(823, "decide 823", "")])
        section = github.decisions_section(got)
        self.assertIn("## Decisions this card waited on (verbatim)", section)
        self.assertIn("### #820 decide 820\n\nDecision: the lane opens release PRs; a maintainer merges them.", section)
        self.assertIn("### #823 decide 823\n\n(closed with no `Decision:` comment", section)
        self.assertEqual(github.decisions_section([]), "")

    def test_a_decision_beyond_the_comments_read_is_refused(self) -> None:
        with self.assertRaises(GhError):
            self.read(self.blocker(820, "CLOSED", ["kind/decision"], [("OWNER", "ok")], older=True))


class WaitsForApprovalTests(unittest.TestCase):
    def test_only_a_ready_green_head_the_bot_cannot_approve_further_waits_on_a_person(self) -> None:
        bot_ok = [review("APPROVED")]
        yes = [pull(815, 804, reviews=bot_ok, review_decision="REVIEW_REQUIRED"),
               pull(900, None, author="a-maintainer", review_decision="REVIEW_REQUIRED")]
        no = [pull(815, 804, reviews=bot_ok, review_decision="APPROVED"),
              pull(815, 804, review_decision="REVIEW_REQUIRED"),  # the bot's turn
              pull(815, 804, reviews=bot_ok, review_decision="REVIEW_REQUIRED", draft=True),
              pull(815, 804, reviews=bot_ok, review_decision="REVIEW_REQUIRED", mergeable="CONFLICTING"),
              pull(815, 804, reviews=bot_ok, review_decision="REVIEW_REQUIRED",
                   checks=[("ci-ok", "COMPLETED", "FAILURE")])]
        for p in yes:
            self.assertTrue(github.waits_for_approval(p, REQUIRED, "lunate-reviewer"), p)
        for p in no:
            self.assertFalse(github.waits_for_approval(p, REQUIRED, "lunate-reviewer"), p)


class WaitsOnTests(unittest.TestCase):
    def test_the_cards_open_blockers_are_read_from_the_issue_it_closes(self) -> None:
        issues = [{"number": 751, "blockedBy": page([{"number": 749, "state": "OPEN"}, {"number": 750, "state": "OPEN"},
                                                    {"number": 700, "state": "CLOSED"}])},
                  {"number": 9, "blockedBy": page([{"number": 5, "state": "OPEN"}])}]
        self.assertEqual(github._waits_on({"headRefName": "wt/issue-751"}, issues, 820), (749, 750))
        self.assertEqual(github._waits_on({"headRefName": "feature/x"}, issues, 820), (5, 749, 750))
        with self.assertRaises(GhError):
            github._waits_on({"headRefName": "wt/issue-751"},
                             [{"number": 751, "blockedBy": page([], more=True)}], 820)

    def test_a_waiting_pull_request_is_never_enqueued_nor_waiting_for_your_approval(self) -> None:
        p = pull(820, 751, reviews=[review("APPROVED")], waits_on=(749,), review_decision="REVIEW_REQUIRED")
        self.assertTrue(github.needs_enqueue(pull(820, 751, reviews=[review("APPROVED")]), REQUIRED, "lunate-reviewer"))
        self.assertFalse(github.needs_enqueue(p, REQUIRED, "lunate-reviewer"))
        self.assertFalse(github.waits_for_approval(p, REQUIRED, "lunate-reviewer"))

    def test_open_blockers_of_a_card(self) -> None:
        answer = {"repository": {"issue": {"blockedBy": page([{"number": 750, "state": "OPEN"},
                                                              {"number": 749, "state": "OPEN"},
                                                              {"number": 1, "state": "CLOSED"}])}}}
        with mock.patch.object(common, "graphql", return_value=answer):
            self.assertEqual(github.open_blockers(CFG, 751), (749, 750))


class RunDraftTests(unittest.TestCase):
    """Whether a workflow run started from a draft payload, read from its ci-ok job's log (#815's two runs)."""

    DRAFT_LOG = ("2026-11-12T22:13:05.7385814Z   EVENT_NAME: pull_request\n2026-11-12T22:13:05.7385814Z   DRAFT: true\n"
                 "2026-11-12T22:13:05.7585875Z ##[error]ci-ok: the head is a draft (draft=true), so no gate ran\n")
    READY_LOG = "2026-11-12T22:33:17.2280238Z   DRAFT: false\r\n2026-11-12T22:33:17.2441609Z ci-ok: draft=false\n"

    def setUp(self) -> None:
        from tests.helpers import TempState

        self.state = TempState()
        self.cfg = self.state.cfg

    def tearDown(self) -> None:
        self.state.close()

    def draft(self, jobs, logs):
        def log(*args, **_):
            job = int(args[1].rsplit("/", 2)[-2])
            if job not in logs:
                raise GhError("gh api failed (1): HTTP 404")
            return logs[job]

        with mock.patch.object(common, "gh_json", return_value={"jobs": jobs}) as listing, \
                mock.patch.object(common, "gh", side_effect=log):
            found = github.run_draft(self.cfg, 43317817862)
        self.assertEqual(listing.call_args.args,
                         ("api", "repos/lunate-agent/lunate/actions/runs/43317817862/jobs?filter=all&per_page=100"))
        return found

    def test_the_payload_flag_is_read_from_the_aggregate_jobs_log(self) -> None:
        jobs = [{"id": 7, "name": "plan"}, {"id": 8, "name": "ci-ok"}]
        self.assertIs(self.draft(jobs, {7: "x  DRAFT: false\n", 8: self.DRAFT_LOG}), True)
        self.assertIs(self.draft(jobs, {8: self.READY_LOG}), False)

    def test_an_attempt_without_a_log_falls_back_to_another_attempt(self) -> None:
        jobs = [{"id": 8, "name": "ci-ok"}, {"id": 9, "name": "ci-ok"}]  # attempt 2 (9) was cancelled unstarted
        self.assertIs(self.draft(jobs, {8: self.READY_LOG}), False)

    def test_no_log_and_no_flag_are_unknown(self) -> None:
        self.assertIsNone(self.draft([{"id": 8, "name": "ci-ok"}], {}))
        self.assertIsNone(self.draft([{"id": 8, "name": "ci-ok"}], {8: "x  DRAFT: \n"}), "a merge_group run")
        self.assertIsNone(self.draft([], {}))

    def test_a_failed_job_listing_raises(self) -> None:
        with mock.patch.object(common, "gh_json", side_effect=GhError("HTTP 502")), self.assertRaises(GhError):
            github.run_draft(self.cfg, 1)


FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


class QueueEjectionTests(unittest.TestCase):
    """What took a pull request out of the merge queue, from its entry's last merge_group run — today's shapes
    (2026-11-13), read from lunate-agent/lunate and kept in tests/fixtures."""

    def gh(self, jobs_run=None, jobs=None, runs=None):
        listing = json.loads((FIXTURES / "merge-group-runs-2026-11-13.json").read_text())
        rows = runs if runs is not None else listing["workflow_runs"]
        total = listing["total_count"] if runs is None else len(rows)
        tsv = f"T\t{total}\n" + "".join(f"{r['id']}\t{r['name']}\t{r['head_branch']}\t{r['created_at']}\n" for r in rows)
        if jobs is None and jobs_run is not None:
            jobs = json.loads((FIXTURES / f"merge-group-{jobs_run}-jobs.json").read_text())
        self.calls = []

        def gh(*args, **kwargs):
            self.calls.append(args)
            return tsv

        def gh_json(*args, **kwargs):
            self.calls.append(args)
            return jobs

        return mock.patch.multiple(common, gh=gh, gh_json=gh_json)

    def test_824_a_macos_shard_not_acquired_with_0_steps_is_infra(self) -> None:
        with self.gh(43474820240):
            e = github.queue_ejection(CFG, 824, "failed_checks", "2026-11-13T18:11:07Z", "MERGEABLE")
        self.assertEqual((e.kind, e.run_id), ("infra", 43474820240))
        self.assertEqual(e.why, "verify (macos-latest, 3/4) cancelled with 0 step(s) in merge_group run 43474820240")
        self.assertIn("event=merge_group", self.calls[0][2])
        self.assertIn("created=2026-11-12T18:11:07Z..2026-11-13T18:11:07Z", self.calls[0][2])
        self.assertEqual(self.calls[1][1], f"repos/{CFG.slug}/actions/runs/43474820240/jobs?filter=latest&per_page=100")

    def test_829_removed_with_no_failing_job_is_infra(self) -> None:
        """#829's run: the macOS shard 4/4 never started; ci-ok failed only at its aggregating step. With that shard
        left out, no job failed at all — infra either way."""
        with self.gh(43470709108):
            e = github.queue_ejection(CFG, 829, "failed_checks", "2026-11-13T17:38:36Z", "MERGEABLE")
        self.assertEqual((e.kind, e.run_id), ("infra", 43470709108))
        self.assertIn("verify (macos-latest, 4/4) cancelled with 0 step(s)", e.why)
        jobs = json.loads((FIXTURES / "merge-group-43470709108-jobs.json").read_text())
        jobs["jobs"] = [j for j in jobs["jobs"] if j["name"] != "verify (macos-latest, 4/4)"]
        jobs["total_count"] = len(jobs["jobs"])
        with self.gh(jobs=jobs):
            e = github.queue_ejection(CFG, 829, "failed_checks", "2026-11-13T17:38:36Z", "MERGEABLE")
        self.assertEqual((e.kind, e.why), ("infra", "no job failed in merge_group run 43470709108"))

    def test_827_unmergeable_is_a_conflict_and_mergeable_again_is_requeued(self) -> None:
        with self.gh():
            conflict = github.queue_ejection(CFG, 827, "merge_conflict", "2026-11-13T17:38:36Z", "CONFLICTING")
            cleared = github.queue_ejection(CFG, 827, "merge_conflict", "2026-11-13T17:38:36Z", "MERGEABLE")
            pending = github.queue_ejection(CFG, 827, "merge_conflict", "2026-11-13T17:38:36Z", "UNKNOWN")
        self.assertEqual((conflict.kind, cleared.kind, pending.kind), ("conflict", "infra", "pending"))
        self.assertEqual(self.calls, [], "a conflict is GitHub's mergeability, not a run")

    def test_815_a_failing_step_is_real_with_the_job(self) -> None:
        runs = [{"id": 43474822487, "name": "CI", "created_at": "2026-11-13T17:38:41Z",
                 "head_branch": "gh-readonly-queue/main/pr-815-94545e80821e0f45cf7fa096975dcdbcf6876ff1"}]
        with self.gh(43474822487, runs=runs):
            e = github.queue_ejection(CFG, 815, "failed_checks", "2026-11-13T18:20:00Z", "MERGEABLE")
        self.assertEqual((e.kind, e.job, e.job_id, e.steps, e.run_id),
                         ("real", "verify (macos-latest, 2/4)", 132055913825, ("Verify (bash)",), 43474822487))

    def test_ci_ok_failing_at_another_step_is_real(self) -> None:
        jobs = {"total_count": 1, "jobs": [{"id": 9, "name": "ci-ok", "conclusion": "failure",
                                            "steps": [{"name": "Set up job", "conclusion": "failure"}]}]}
        with self.gh(jobs=jobs):
            e = github.queue_ejection(CFG, 824, "failed_checks", "2026-11-13T18:11:07Z", "MERGEABLE")
        self.assertEqual((e.kind, e.job), ("real", "ci-ok"))

    def test_no_run_for_the_entry_is_infra_and_another_prs_run_is_not_its(self) -> None:
        with self.gh(jobs={}):
            e = github.queue_ejection(CFG, 830, "failed_checks", "2026-11-13T18:11:07Z", "MERGEABLE")
        self.assertEqual((e.kind, e.why), ("infra", "no merge_group run of its entry was found"))
        with self.gh(43474820240):  # pr-82 is a prefix of pr-824's branch, not its own
            self.assertEqual(github.queue_run(CFG, 82, "2026-11-13T18:11:07Z"), None)

    def test_an_incomplete_listing_is_refused(self) -> None:
        listing = json.loads((FIXTURES / "merge-group-runs-2026-11-13.json").read_text())
        with mock.patch.object(common, "gh", return_value=f"T\t{listing['total_count'] + 1}\n"), \
                self.assertRaises(GhError):
            github.queue_run(CFG, 824, "2026-11-13T18:11:07Z")
        jobs = json.loads((FIXTURES / "merge-group-43474820240-jobs.json").read_text())
        jobs["total_count"] += 1
        with self.gh(jobs=jobs), self.assertRaises(GhError):
            github.queue_ejection(CFG, 824, "failed_checks", "2026-11-13T18:11:07Z", "MERGEABLE")
