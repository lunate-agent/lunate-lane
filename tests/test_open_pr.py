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
        self.saved = (open_pr._api, open_pr.identity.installation_token, open_pr.push, open_pr._graphql)
        open_pr.identity.installation_token = lambda name: "token-for-tests"  # type: ignore[assignment]
        # a fake of GitHub's auto-merge state, per pull request node id
        self.armed: dict = {}
        self.events: list = []
        self.disarm_sticks = False
        open_pr._graphql = self.fake_graphql

    def fake_graphql(self, token, query, variables, what):
        node = variables["id"]
        if "enablePullRequestAutoMerge" in query:
            self.events.append(("arm", node))
            self.armed[node] = True
            return {"enablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": {"enabledAt": "t"}}}}
        if "disablePullRequestAutoMerge" in query:
            self.events.append(("disarm", node))
            self.armed[node] = self.disarm_sticks and self.armed.get(node, False)
            return {"disablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": None}}}
        self.events.append(("read", node))
        return {"node": {"number": 1, "autoMergeRequest": {"enabledAt": "t"} if self.armed.get(node) else None}}

    def tearDown(self) -> None:
        open_pr._api, open_pr.identity.installation_token, open_pr.push, open_pr._graphql = self.saved
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
        self.assertIn("GitHub reads it as on", str(caught.exception))

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
