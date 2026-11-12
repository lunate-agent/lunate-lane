"""Config, the raising call wrappers, templates, locks and the job wrapper. No network."""

import io
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common  # noqa: E402
from scripts.common import ApiError, ConfigError, GhError, GitError, LaneError, LockBusy  # noqa: E402
from tests.helpers import TempState  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="lane-config-"))
        self.data = json.loads((common.ROOT / "lane.json").read_text())

    def write(self, data) -> pathlib.Path:
        path = self.dir / "lane.json"
        path.write_text(json.dumps(data))
        return path

    def test_the_checked_in_config_loads(self) -> None:
        cfg = common.load_config()
        self.assertEqual((cfg.owner, cfg.name), ("lunate-agent", "lunate"))
        self.assertNotEqual(cfg.worker_model, cfg.reviewer_model)
        self.assertTrue(cfg.state.is_absolute())

    def test_a_missing_key_is_a_named_failure_not_a_default(self) -> None:
        del self.data["models"]["reviewer"]
        with self.assertRaises(ConfigError) as caught:
            common.load_config(self.write(self.data))
        self.assertIn("models.reviewer", str(caught.exception))

    def test_the_reviewer_model_must_differ_from_the_workers(self) -> None:
        self.data["models"]["reviewer"] = self.data["models"]["worker"]
        with self.assertRaises(ConfigError):
            common.load_config(self.write(self.data))

    def test_an_unreadable_config_is_a_named_failure(self) -> None:
        with self.assertRaises(ConfigError):
            common.load_config(self.dir / "absent.json")
        (self.dir / "bad.json").write_text("{")
        with self.assertRaises(ConfigError):
            common.load_config(self.dir / "bad.json")


class RunTests(unittest.TestCase):
    def test_a_non_zero_exit_raises_the_named_error_with_its_output(self) -> None:
        with self.assertRaises(GitError) as caught:
            common.run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], error=GitError)
        self.assertIn("boom", str(caught.exception))

    def test_a_missing_binary_raises_rather_than_returning_empty(self) -> None:
        with self.assertRaises(GhError):
            common.run(["/nonexistent/gh"], error=GhError)

    def test_a_timeout_raises(self) -> None:
        with self.assertRaises(LaneError) as caught:
            common.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
        self.assertIn("did not finish", str(caught.exception))

    def test_gh_answering_non_json_is_a_gh_error(self) -> None:
        with mock.patch.object(common, "gh", return_value="<html>"):
            with self.assertRaises(GhError):
                common.gh_json("api", "x")

    def test_graphql_errors_in_the_answer_raise(self) -> None:
        for answer in ({"errors": [{"message": "rate limited"}], "data": None}, {"data": None}, []):
            with self.subTest(answer=answer), mock.patch.object(common, "gh_json", return_value=answer):
                with self.assertRaises(GhError):
                    common.graphql("query{x}")

    def test_graphql_passes_strings_and_numbers_differently(self) -> None:
        with mock.patch.object(common, "gh_json", return_value={"data": {}}) as gh_json:
            common.graphql("q", name="x", n=5)
        args = gh_json.call_args.args
        self.assertIn("-f", args[args.index("name=x") - 1])
        self.assertEqual(args[args.index("n=5") - 1], "-F")


class RestTests(unittest.TestCase):
    def test_a_refusal_is_an_api_error_and_never_carries_the_token(self) -> None:
        token = "ghs_secret_for_tests"
        refused = urllib.error.HTTPError("u", 422, "x", {}, io.BytesIO(f"bad {token}".encode()))
        self.addCleanup(refused.close)
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            with self.assertRaises(ApiError) as caught:
                common.rest(token, "POST", "/repos/x/y/pulls/1/reviews", {"event": "APPROVE"})
        self.assertIn("422", str(caught.exception))
        self.assertNotIn(token, str(caught.exception))

    def test_an_unreachable_github_is_an_api_error(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no route")):
            with self.assertRaises(ApiError):
                common.rest("t", "GET", "/x")


class RenderTests(unittest.TestCase):
    def test_inserted_text_is_never_substituted_again(self) -> None:
        values = {k: "v" for k in ("NUMBER", "REPO", "TITLE", "WORKTREE", "BRANCH", "BASE", "PR_LINE",
                                   "FRAGMENT_RULE", "REWORK")}
        text = common.render("worker-brief.md", **values, BODY="literal {{TITLE}} and {braces}")
        self.assertIn("literal {{TITLE}} and {braces}", text)

    def test_a_missing_value_is_a_named_failure(self) -> None:
        with self.assertRaises(ConfigError):
            common.render("worker-brief.md", NUMBER="1")

    def test_a_missing_template_is_a_named_failure(self) -> None:
        with self.assertRaises(ConfigError):
            common.render("absent.md")


class LockAndJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()

    def tearDown(self) -> None:
        self.state.close()

    def test_two_runs_of_one_job_cannot_overlap(self) -> None:
        with common.job_lock("feed"):
            with self.assertRaises(LockBusy):
                with common.job_lock("feed"):
                    pass
            with common.job_lock("review"):
                pass  # a different job is not blocked

    def test_a_busy_lock_skips_quietly_and_a_named_failure_exits_non_zero(self) -> None:
        with mock.patch("sys.stdout", new=io.StringIO()):
            with common.job_lock("feed"):
                self.assertEqual(common.run_job("feed", lambda: None), 0)
            self.assertEqual(common.run_job("feed", lambda: (_ for _ in ()).throw(GhError("unreadable"))), 1)
        log = (self.state.cfg.state / "logs" / "feed.log").read_text()
        self.assertIn("skipped — another feed run holds", log)
        self.assertIn("FAILED — unreadable", log)


if __name__ == "__main__":
    unittest.main()
