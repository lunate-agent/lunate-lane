"""The failure paths. Each one is a case where the old lane would have reported success.

No network: every GitHub call is replaced, so a failure is the only thing that can make these pass.
"""

import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import open_pr  # noqa: E402


def fake_api(monkey_self=None, **_):
    raise AssertionError("should not be called")


class FailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        # a real repository, so a failure can only come from the branch being absent
        self._tmp = tempfile.mkdtemp(prefix="lane-test-")
        self.repo = pathlib.Path(self._tmp)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self._tmp, capture_output=True)
        self.saved = (open_pr._api, open_pr.identity.installation_token, open_pr.push, open_pr._graphql,
                      open_pr.queue_method)
        open_pr.queue_method = lambda token, base: "SQUASH"  # type: ignore[assignment]
        open_pr.identity.installation_token = lambda name: "token-for-tests"  # type: ignore[assignment]
        # a fake of GitHub's auto-merge state, per pull request node id
        self.armed: dict = {}
        self.drafts: dict = {}
        self.events: list = []
        self.disarm_sticks = False
        open_pr._graphql = self.fake_graphql

    def fake_graphql(self, token, query, variables, what):
        """GitHub's state per node: draft (a created or unknown pull request starts as one) and auto-merge."""
        node = variables["id"]
        self.drafts.setdefault(node, True)
        if "enablePullRequestAutoMerge" in query:
            if self.drafts[node]:
                raise open_pr.OpenPrError("auto-merge was refused: pull request is in draft state")
            self.events.append(("arm", node))
            self.methods = getattr(self, "methods", []) + [variables["method"]]
            self.armed[node] = True
            return {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"enabledAt": "t"}}}}
        if "disablePullRequestAutoMerge" in query:
            self.events.append(("disarm", node))
            self.armed[node] = self.disarm_sticks and self.armed.get(node, False)
            return {"disablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": None}}}
        if "markPullRequestReadyForReview" in query:
            self.events.append(("ready", node))
            self.drafts[node] = False
            return {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}
        if "convertPullRequestToDraft" in query:
            self.events.append(("draft", node))
            self.drafts[node] = True
            return {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}
        self.events.append(("read", node))
        recorded = getattr(self, "recorded", "SQUASH")
        return {"node": {"number": 1, "isDraft": self.drafts[node],
                         "autoMergeRequest": {"enabledAt": "t", "mergeMethod": recorded}
                         if self.armed.get(node) else None}}

    def tearDown(self) -> None:
        (open_pr._api, open_pr.identity.installation_token, open_pr.push, open_pr._graphql,
         open_pr.queue_method) = self.saved
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_auto_merge_refused_is_a_failure_not_a_pass(self) -> None:
        """A PR that opens but cannot arm auto-merge must raise."""
        open_pr._api = lambda token, method, *a, **k: [] if method == "GET" else {
            "number": 1, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN}, "node_id": "N1"}
        open_pr.push = lambda *a, **k: None

        def refused(*a, **k):
            raise open_pr.OpenPrError("auto-merge was refused: [{'type': 'UNPROCESSABLE'}]")

        saved_arm = open_pr.arm_auto_merge
        open_pr.arm_auto_merge = refused
        try:
            with self.assertRaises(open_pr.OpenPrError) as caught:
                open_pr.open_pr(self.repo, "lane/x", "t", "b")
        finally:
            open_pr.arm_auto_merge = saved_arm
        self.assertIn("auto-merge was refused", str(caught.exception))

    def test_wrong_author_closes_the_pull_request_again(self) -> None:
        calls = []

        def api(token, method, path, payload=None):
            calls.append((method, path))
            if method == "GET":
                return []
            return {"number": 7, "html_url": "u", "user": {"login": "someone-else"}, "node_id": "N7"}

        open_pr._api = api
        open_pr.push = lambda *a, **k: None
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b")
        self.assertIn("closed again", str(caught.exception))
        self.assertIn(("PATCH", "/repos/lunate-agent/lunate/pulls/7"), calls)

    def test_an_existing_worker_pr_is_reused_not_duplicated(self) -> None:
        """Reuse still pushes: a re-dispatch usually carries new commits, and they must reach GitHub."""
        posts = []
        pushes = []

        def api(token, method, path, payload=None):
            if method == "GET":
                return [{"number": 42, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN}, "node_id": "N42"}]
            posts.append(method)
            return {}

        open_pr._api = api
        open_pr.push = lambda *a, **k: pushes.append(a)
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b")
        self.assertTrue(result["reused"])
        self.assertEqual(result["number"], 42)
        self.assertEqual(result["auto_merge"], "armed")
        self.assertIn(("arm", "N42"), self.events)
        self.assertEqual(len(pushes), 1, "reuse must still push the branch, or new commits never arrive")

    def test_a_wrong_author_existing_pr_is_refused_without_pushing(self) -> None:
        """The regression guard: a foreign branch must not be force-pushed before the refusal."""
        pushes = []

        def api(token, method, path, payload=None):
            if method == "GET":
                return [{"number": 9, "html_url": "u", "user": {"login": "someone-else"}, "node_id": "N9"}]
            raise AssertionError(f"must not call {method} on a foreign pull request")

        open_pr._api = api
        open_pr.push = lambda *a, **k: pushes.append(a)
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b")
        self.assertIn("authored by 'someone-else'", str(caught.exception))
        self.assertEqual(pushes, [], "the branch must not be touched when the PR belongs to someone else")

    def test_an_unarmed_open_pushes_and_opens_but_never_arms(self) -> None:
        """The lane opens a card's pull request at its first commit without auto-merge; arming comes later."""
        def api(token, method, path, payload=None):
            if method == "GET":
                return []
            return {"number": 3, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN}, "node_id": "N3"}

        open_pr._api = api
        open_pr.push = lambda *a, **k: None
        saved_arm = open_pr.arm_auto_merge
        open_pr.arm_auto_merge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not arm"))
        try:
            result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        finally:
            open_pr.arm_auto_merge = saved_arm
        self.assertEqual((result["number"], result["auto_merge"]), (3, "not armed"))

    def reuse(self, number=42):
        def api(token, method, path, payload=None):
            if method == "GET":
                return [{"number": number, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN},
                         "node_id": f"N{number}"}]
            raise AssertionError(f"unexpected {method} {path}")

        open_pr._api = api
        open_pr.push = lambda *a, **k: self.events.append(("push", a[1]))

    def test_a_rework_on_an_armed_pull_request_disarms_it_before_pushing(self) -> None:
        """An armed PR plus an unclean rework ends up disarmed — and the result is GitHub's read, not the intent."""
        self.reuse()
        self.armed["N42"] = True
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        self.assertEqual(result["auto_merge"], "not armed")
        self.assertFalse(self.armed["N42"])
        self.assertLess(self.events.index(("disarm", "N42")), self.events.index(("push", "lane/x")),
                        "auto-merge must be off before the unclean work is pushed")
        self.assertEqual(self.events[-1], ("read", "N42"))

    def test_an_unarmed_reuse_of_an_unarmed_pull_request_disarms_nothing(self) -> None:
        self.reuse()
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        self.assertEqual(result["auto_merge"], "not armed")
        self.assertNotIn(("disarm", "N42"), self.events)

    def test_a_disarm_that_does_not_take_is_a_failure_not_a_report_of_not_armed(self) -> None:
        self.reuse()
        self.armed["N42"] = True
        self.disarm_sticks = True
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        self.assertIn("auto-merge on — nothing was pushed", str(caught.exception))
        self.assertNotIn(("push", "lane/x"), self.events, "unclean work must not reach a PR that is still armed")

    def test_a_disarm_call_that_fails_pushes_nothing(self) -> None:
        self.reuse()
        self.armed["N42"] = True
        fake = self.fake_graphql

        def refusing(token, query, variables, what):
            if "disablePullRequestAutoMerge" in query:
                raise open_pr.OpenPrError("disable auto-merge was refused: [FORBIDDEN]")
            return fake(token, query, variables, what)

        open_pr._graphql = refusing
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        self.assertIn("disable auto-merge was refused", str(caught.exception))
        self.assertNotIn(("push", "lane/x"), self.events)

    def test_an_unreadable_auto_merge_state_is_a_failure(self) -> None:
        self.reuse()
        open_pr._graphql = lambda *a, **k: {"node": None}
        with self.assertRaises(open_pr.OpenPrError):
            open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)

    def test_a_non_list_answer_for_open_pull_requests_is_a_failure_not_none_open(self) -> None:
        open_pr._api = lambda *a, **k: {"message": "Bad credentials"}
        open_pr.push = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not push"))
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b")
        self.assertIn("not a list", str(caught.exception))
        open_pr._api = lambda *a, **k: []
        self.assertIsNone(open_pr.find_open_pr("lane/x", "t"))

    def test_a_branch_that_does_not_exist_is_refused_before_pushing(self) -> None:
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.push(self.repo, "lane/never-created", "token-for-tests")
        self.assertIn("no local branch", str(caught.exception))

    def test_a_new_pull_request_is_opened_as_a_draft(self) -> None:
        """#5: created as a draft, and read back as one while unarmed."""
        posts = []

        def api(token, method, path, payload=None):
            if method == "GET":
                return []
            posts.append(payload)
            return {"number": 3, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN}, "node_id": "N3"}

        open_pr._api = api
        open_pr.push = lambda *a, **k: self.events.append(("push", a[1]))
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        self.assertIs(posts[0]["draft"], True)
        self.assertEqual((result["draft"], result["auto_merge"]), (True, "not armed"))
        self.assertNotIn(("ready", "N3"), self.events)

    def test_a_clean_finish_pushes_then_marks_ready_then_arms_and_reads_both_back(self) -> None:
        self.reuse()
        self.drafts["N42"] = True
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        order = [e for e in self.events if e[0] in ("push", "ready", "arm")]
        self.assertEqual(order, [("push", "lane/x"), ("ready", "N42"), ("arm", "N42")])
        self.assertEqual((result["draft"], result["auto_merge"]), (False, "armed"))
        self.assertEqual(self.events[-1], ("read", "N42"), "the result is GitHub's read, not the intent")

    def test_a_rework_returns_a_ready_armed_pull_request_to_draft_before_its_push(self) -> None:
        self.reuse()
        self.drafts["N42"], self.armed["N42"] = False, True
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        push_at = self.events.index(("push", "lane/x"))
        self.assertLess(self.events.index(("disarm", "N42")), push_at)
        self.assertLess(self.events.index(("draft", "N42")), push_at)
        self.assertEqual((result["draft"], result["auto_merge"]), (True, "not armed"))

    def test_the_three_transitions_created_draft_ready_on_clean_draft_again_on_rework(self) -> None:
        def api(token, method, path, payload=None):
            if method == "GET":
                return [{"number": 5, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN}, "node_id": "N5"}] \
                    if "N5" in self.drafts else []
            return {"number": 5, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN}, "node_id": "N5"}

        open_pr._api = api
        open_pr.push = lambda *a, **k: self.events.append(("push", a[1]))
        first = open_pr.open_pr(self.repo, "wt/issue-5", "t", "b", arm=False)
        self.assertEqual((first["reused"], first["draft"]), (False, True))
        clean = open_pr.open_pr(self.repo, "wt/issue-5", "t", "b", arm=True)
        self.assertEqual((clean["draft"], clean["auto_merge"]), (False, "armed"))
        open_pr.return_to_draft("wt/issue-5", 5)
        self.assertEqual((self.drafts["N5"], self.armed["N5"]), (True, False))
        again = open_pr.open_pr(self.repo, "wt/issue-5", "t", "b", arm=True)
        self.assertEqual((again["draft"], again["auto_merge"]), (False, "armed"))

    def test_a_draft_conversion_that_does_not_take_pushes_nothing(self) -> None:
        self.reuse()
        self.drafts["N42"] = False
        fake = self.fake_graphql

        def stuck(token, query, variables, what):
            if "convertPullRequestToDraft" in query:
                return {"convertPullRequestToDraft": {"pullRequest": {"isDraft": False}}}
            return fake(token, query, variables, what)

        open_pr._graphql = stuck
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        self.assertIn("nothing was pushed", str(caught.exception))
        self.assertNotIn(("push", "lane/x"), self.events)

    def test_return_to_draft_refuses_another_pull_request(self) -> None:
        self.reuse(42)
        with self.assertRaises(open_pr.OpenPrError):
            open_pr.return_to_draft("lane/x", 77)
        self.assertNotIn(("draft", "N42"), self.events)

    def test_auto_merge_is_armed_with_the_queues_method_and_the_recorded_method_is_reported(self) -> None:
        """#6: the mutation's method is the merge queue's, read from the rules — and what GitHub records is
        reported as read, even when (as on #813) it records MERGE for a queue that squashes."""
        self.reuse()
        open_pr.queue_method = lambda token, base: "SQUASH"
        self.recorded = "MERGE"
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b")
        self.assertEqual(self.methods, ["SQUASH"])
        self.assertEqual((result["auto_merge"], result["queue_method"], result["merge_method"]),
                         ("armed", "SQUASH", "MERGE"))

    def test_the_queue_method_is_read_from_the_base_branchs_rules(self) -> None:
        open_pr.queue_method = self.saved[4]
        rules = [{"type": "pull_request", "parameters": {"allowed_merge_methods": ["merge", "squash"]}},
                 {"type": "merge_queue", "parameters": {"merge_method": "SQUASH", "grouping_strategy": "ALLGREEN"}}]
        paths = []
        open_pr._api = lambda token, method, path, payload=None: paths.append((method, path)) or rules
        self.assertEqual(open_pr.queue_method("t", "main"), "SQUASH")
        self.assertEqual(paths, [("GET", "/repos/lunate-agent/lunate/rules/branches/main")])

    def test_no_merge_queue_rule_is_a_refusal_not_a_guess(self) -> None:
        open_pr.queue_method = self.saved[4]
        for rules in ([], [{"type": "merge_queue", "parameters": {}}], {"message": "Not Found"},
                      [{"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}},
                       {"type": "merge_queue", "parameters": {"merge_method": "MERGE"}}]):
            with self.subTest(rules=rules):
                open_pr._api = lambda *a, rules=rules, **k: rules
                with self.assertRaises(open_pr.OpenPrError):
                    open_pr.queue_method("t", "main")

    def test_an_unreadable_queue_method_arms_nothing(self) -> None:
        self.reuse()

        def refuse(token, base):
            raise open_pr.OpenPrError("main has no merge queue method in its rules")

        open_pr.queue_method = refuse
        with self.assertRaises(open_pr.OpenPrError):
            open_pr.open_pr(self.repo, "lane/x", "t", "b")
        self.assertNotIn(("arm", "N42"), self.events)

    def test_a_nameless_identity_is_a_named_failure(self) -> None:
        def boom(name):
            raise open_pr.identity.IdentityError(f"unknown identity {name!r}; known: reviewer, worker")

        open_pr.identity.installation_token = boom
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr._worker_token()
        self.assertIn("could not mint a token", str(caught.exception))
        self.assertIn("unknown identity", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
