"""Reading GitHub's state: incomplete reads raise, checks are judged on the required ones only. No network."""

import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, github  # noqa: E402
from scripts.common import GhError  # noqa: E402
from tests.helpers import REQUIRED, pull, review  # noqa: E402

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
