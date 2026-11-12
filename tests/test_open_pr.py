"""The failure paths. Each one is a case where the old lane would have reported success.

No network: every GitHub call is replaced, so a failure is the only thing that can make these pass.
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import open_pr  # noqa: E402


def fake_api(monkey_self=None, **_):
    raise AssertionError("should not be called")


class FailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = pathlib.Path("/tmp/does-not-matter")
        self.saved = (open_pr._api, open_pr.identity.installation_token, open_pr.push)
        open_pr.identity.installation_token = lambda name: "token-for-tests"  # type: ignore[assignment]

    def tearDown(self) -> None:
        open_pr._api, open_pr.identity.installation_token, open_pr.push = self.saved

    def test_auto_merge_refused_is_a_failure_not_a_pass(self) -> None:
        """A PR that opens but cannot arm auto-merge must raise."""
        open_pr._api = lambda *a, **k: {"number": 1, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN},
                                       "node_id": "N1"}
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
            return {"number": 7, "html_url": "u", "user": {"login": "wdaniel1993"}, "node_id": "N7"}

        open_pr._api = api
        open_pr.push = lambda *a, **k: None
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b")
        self.assertIn("closed again", str(caught.exception))
        self.assertIn(("PATCH", "/repos/lunate-agent/lunate/pulls/7"), calls)

    def test_an_existing_worker_pr_is_reused_not_duplicated(self) -> None:
        posts = []

        def api(token, method, path, payload=None):
            if method == "GET":
                return [{"number": 42, "html_url": "u", "user": {"login": open_pr.WORKER_LOGIN}, "node_id": "N42"}]
            posts.append(method)
            return {}

        open_pr._api = api
        open_pr.push = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not push for an open PR"))
        armed = []
        saved_arm = open_pr.arm_auto_merge
        open_pr.arm_auto_merge = lambda token, node_id, method="SQUASH": armed.append(node_id)
        try:
            result = open_pr.open_pr(self.repo, "lane/x", "t", "b")
        finally:
            open_pr.arm_auto_merge = saved_arm
        self.assertTrue(result["reused"])
        self.assertEqual(result["number"], 42)
        self.assertEqual(armed, ["N42"])

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
