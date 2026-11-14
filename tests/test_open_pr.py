"""The failure paths. Each one is a case where the old lane would have reported success.

No network: every GitHub call is replaced, so a failure is the only thing that can make these pass.
"""

import base64
import http.server
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import open_pr  # noqa: E402
from tests.helpers import TempState  # noqa: E402


# the push's run while the pull request is a draft: its ready-only job (`plan`) skipped
DRAFT_RUN = open_pr.github.CiRun(100, "COMPLETED", "FAILURE", "t", "SKIPPED")


def fake_api(monkey_self=None, **_):
    raise AssertionError("should not be called")


class OpenPrCase(unittest.TestCase):
    """The fixture: a real repository; GitHub's draft and auto-merge state, the push and CI replaced."""

    def setUp(self) -> None:
        # a real repository, so a failure can only come from the branch being absent
        self._tmp = tempfile.mkdtemp(prefix="lane-test-")
        self.repo = pathlib.Path(self._tmp)
        self.state = TempState()
        self.addCleanup(self.state.close)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"],
                     ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "seed"],
                     ["update-ref", "refs/remotes/origin/main", "HEAD"], ["branch", "lane/x"],
                     ["branch", "wt/issue-5"]):
            subprocess.run(["git", *args], cwd=self._tmp, capture_output=True, check=True)
        self.saved = (open_pr._api, open_pr.identity.installation_token, open_pr.push, open_pr._graphql,
                      open_pr.queue_method)
        open_pr.queue_method = lambda token, base: "SQUASH"  # type: ignore[assignment]
        open_pr.identity.installation_token = lambda name: "token-for-tests"  # type: ignore[assignment]
        # a fake of GitHub's auto-merge state, per pull request node id
        self.armed: dict = {}
        self.drafts: dict = {}
        # the pull request's head as GitHub shows it: `lagging` heads first (a push GitHub has not caught up
        # with), then the local branch's
        self.local_head = subprocess.run(["git", "rev-parse", "lane/x"], cwd=self._tmp, capture_output=True,
                                         text=True).stdout.strip()
        self.lagging: list = []
        self.saved_wait = (open_pr.HEAD_WAIT, open_pr.HEAD_POLL)
        open_pr.HEAD_WAIT, open_pr.HEAD_POLL = 2, 0.01
        self.events: list = []
        self.disarm_sticks = False
        open_pr._graphql = self.fake_graphql
        # a fake of CI on the head: the push's draft run, then one ready run per `ready` event. `ci_script`
        # replaces it with a test's own sequence of reads.
        self.saved_ci = (open_pr.github.ci_runs, open_pr.RUN_WAIT, open_pr.READY_WAIT, open_pr.READY_SETTLE,
                         open_pr.RUN_POLL)
        open_pr.RUN_WAIT, open_pr.READY_WAIT, open_pr.READY_SETTLE, open_pr.RUN_POLL = 1, 1, 0.0, 0.01
        self.ci_script = None
        open_pr.github.ci_runs = self.fake_ci

    def fake_ci(self, cfg, head):
        self.events.append(("ci", head))
        if self.ci_script is not None:
            return self.ci_script()
        readies = sum(1 for e in self.events if e[0] == "ready")
        return [DRAFT_RUN] + [open_pr.github.CiRun(200 + i, "IN_PROGRESS", "", "t", "IN_PROGRESS")
                              for i in range(readies)]

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
        head = self.lagging.pop(0) if self.lagging else self.local_head
        return {"node": {"number": 1, "isDraft": self.drafts[node], "headRefOid": head,
                         "autoMergeRequest": {"enabledAt": "t", "mergeMethod": recorded}
                         if self.armed.get(node) else None}}

    def tearDown(self) -> None:
        (open_pr._api, open_pr.identity.installation_token, open_pr.push, open_pr._graphql,
         open_pr.queue_method) = self.saved
        open_pr.HEAD_WAIT, open_pr.HEAD_POLL = self.saved_wait
        (open_pr.github.ci_runs, open_pr.RUN_WAIT, open_pr.READY_WAIT, open_pr.READY_SETTLE,
         open_pr.RUN_POLL) = self.saved_ci
        shutil.rmtree(self._tmp, ignore_errors=True)



class FailurePathTests(OpenPrCase):
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

    def test_the_push_never_forces_so_rewritten_history_is_refused(self) -> None:
        """#8: a real push to a real (local) origin; a rewritten branch is a named failure, never overwritten."""
        open_pr.push = self.saved[2]
        origin = self.repo / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        work = self.repo / "work"
        subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
        for args in (["config", "user.email", "w@example.invalid"], ["config", "user.name", "w"],
                     ["switch", "-q", "-c", "wt/issue-3"], ["commit", "-q", "--allow-empty", "-m", "one"]):
            subprocess.run(["git", "-C", str(work), *args], check=True)
        open_pr.push(work, "wt/issue-3", "token-for-tests", url=str(origin))
        first = subprocess.run(["git", "-C", str(origin), "rev-parse", "wt/issue-3"], capture_output=True,
                               text=True).stdout.strip()
        subprocess.run(["git", "-C", str(work), "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten"],
                       check=True)
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.push(work, "wt/issue-3", "token-for-tests", url=str(origin))
        self.assertIn("refused", str(caught.exception))
        self.assertEqual(subprocess.run(["git", "-C", str(origin), "rev-parse", "wt/issue-3"], capture_output=True,
                                        text=True).stdout.strip(), first)

    def server(self, token: str = ""):
        """A local stand-in for a git host that demands basic auth; it records every request and what it got."""
        seen: dict = {"requests": 0, "auth": []}

        class Server(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen["requests"] += 1
                auth = self.headers.get("Authorization")
                if not auth or base64.b64decode(auth.split()[1]).decode().endswith(":"):  # no password: 401
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="lane"')
                    self.end_headers()
                    return
                seen["auth"].append(base64.b64decode(auth.split()[1]).decode())
                if token:
                    listing = subprocess.run(["ps", "-E", "-ww", "-axo", "command"], capture_output=True, text=True)
                    seen["listed"] = listing.stdout.count("\n")
                    seen["leaked"] = token in listing.stdout
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://x-access-token@127.0.0.1:{server.server_port}/r.git", seen

    def real_push(self):
        open_pr.push = self.saved[2]

    def test_the_push_token_reaches_git_through_askpass_and_no_process_shows_it(self) -> None:
        """#10: a real push from the mirror to a local server that demands basic auth. While git waits on the
        authenticated request, no process's argv or environment holds the token; the askpass files are gone."""
        self.real_push()
        token = "ghs_" + "Zz9" * 12
        url, seen = self.server(token)
        run_dir = open_pr._secret_dir()
        self.assertEqual(oct(run_dir.stat().st_mode & 0o777), "0o700")
        env = open_pr.push_env(token, run_dir, url)
        token_file = pathlib.Path(env["LANE_PUSH_FILES"].split(os.pathsep)[0])
        self.assertEqual(oct(token_file.stat().st_mode & 0o777), "0o600")
        self.assertNotIn(token, "".join(f"{k}={v}" for k, v in env.items()))
        open_pr._drop_push_files(env)
        with self.assertRaises(open_pr.OpenPrError):  # the stand-in answers 404 once authenticated
            open_pr.push(self.repo, "lane/x", token, url=url)
        self.assertEqual(seen["auth"], [f"x-access-token:{token}"])
        self.assertGreater(seen["listed"], 5, "the process listing was read")
        self.assertFalse(seen["leaked"], "the token showed in a process's argv or environment")
        self.assertEqual(list(run_dir.iterdir()), [])

    def test_a_pushurl_or_insteadof_in_the_checkout_cannot_redirect_the_push(self) -> None:
        """Review of #17, item 2, attack one: a session rewrites the checkout's config. The push still goes only
        to the explicit URL, from the lane's mirror."""
        self.real_push()
        token = "ghs_" + "Ab1" * 12
        good, good_seen = self.server()
        evil, evil_seen = self.server()
        good_prefix, evil_prefix = good.rsplit("/", 1)[0] + "/", evil.rsplit("/", 1)[0] + "/"
        for key, value in (("remote.origin.url", evil), ("remote.origin.pushurl", evil),
                           (f"url.{evil_prefix}.insteadOf", good_prefix),
                           (f"url.{evil_prefix}.pushInsteadOf", good_prefix),
                           ("url.http://127.0.0.1:1/.insteadOf", "https://github.com/")):
            subprocess.run(["git", "config", "--add", key, value], cwd=self._tmp, check=True)
        with self.assertRaises(open_pr.OpenPrError):
            open_pr.push(self.repo, "lane/x", token, url=good)
        self.assertEqual(evil_seen["requests"], 0, "the checkout's config sent the push elsewhere")
        self.assertEqual(good_seen["auth"], [f"x-access-token:{token}"])

    def test_a_poisoned_environment_or_global_config_cannot_redirect_the_push(self) -> None:
        self.real_push()
        good, good_seen = self.server()
        evil, evil_seen = self.server()
        good_prefix, evil_prefix = good.rsplit("/", 1)[0] + "/", evil.rsplit("/", 1)[0] + "/"
        rc = self.repo / "evil.gitconfig"
        rc.write_text(f'[url "{evil_prefix}"]\n\tinsteadOf = {good_prefix}\n')
        poison = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{evil_prefix}.insteadOf",
                  "GIT_CONFIG_VALUE_0": good_prefix, "GIT_CONFIG_GLOBAL": str(rc),
                  "GIT_CONFIG_PARAMETERS": f"'url.{evil_prefix}.insteadof'='{good_prefix}'"}
        with mock.patch.dict(os.environ, poison), self.assertRaises(open_pr.OpenPrError):
            open_pr.push(self.repo, "lane/x", "ghs_" + "Cd2" * 12, url=good)
        self.assertEqual(evil_seen["requests"], 0)
        self.assertEqual(len(good_seen["auth"]), 1)

    def test_the_askpass_helper_answers_only_a_prompt_for_the_push_host(self) -> None:
        """Review of #17, item 2, attack two: a prompt naming any other host gets nothing."""
        token = "ghs_" + "Ef3" * 12
        env = open_pr.push_env(token, open_pr._secret_dir(), open_pr.push_url())
        self.addCleanup(open_pr._drop_push_files, env)
        helper = env["GIT_ASKPASS"]

        def ask(prompt):
            return subprocess.run([helper, prompt], capture_output=True, text=True)

        good = ask("Password for 'https://x-access-token@github.com': ")
        self.assertEqual((good.returncode, good.stdout), (0, token))
        self.assertEqual(ask("Username for 'https://github.com': ").stdout.strip(), "x-access-token")
        for prompt in ("Password for 'https://x-access-token@evil.example': ",
                       "Password for 'https://x-access-token@github.com.evil.example': ",
                       "Password for 'http://x-access-token@github.com': ",
                       "Password for 'https://someone@github.com': ",
                       "Password for 'https://x-access-token@127.0.0.1:9': ", ""):
            with self.subTest(prompt=prompt):
                refused = ask(prompt)
                self.assertNotEqual(refused.returncode, 0)
                self.assertNotIn(token, refused.stdout + refused.stderr)

    def test_token_files_a_killed_push_left_behind_are_swept_and_a_running_ones_are_not(self) -> None:
        run_dir = open_pr._secret_dir()
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        stale = [run_dir / f"push-{dead.pid}-1.token", run_dir / f"push-{dead.pid}-1.askpass"]
        live = run_dir / f"push-{os.getpid()}-2.token"
        for path in (*stale, live):
            path.write_text("ghs_x")
        self.assertEqual(open_pr.sweep_secret_dir(), 2)
        self.assertEqual(sorted(p.name for p in run_dir.iterdir()), [live.name])

    def test_the_default_push_url_is_the_explicit_github_url(self) -> None:
        self.assertEqual(open_pr.push_url(), "https://x-access-token@github.com/lunate-agent/lunate.git")

    def test_a_push_mirror_with_redirecting_config_is_refused_before_any_request(self) -> None:
        self.real_push()
        good, good_seen = self.server()
        mirror = open_pr.push_mirror()
        subprocess.run(["git", "-C", str(mirror), "config", "url.http://127.0.0.1:1/.insteadOf", good], check=True)
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.push(self.repo, "lane/x", "t0ken", url=good)
        self.assertIn("config it must not have", str(caught.exception))
        self.assertEqual(good_seen["requests"], 0)

    def commit_on(self, branch, path, text):
        subprocess.run(["git", "switch", "-q", branch], cwd=self._tmp, check=True)
        (self.repo / path).write_text(text)
        subprocess.run(["git", "add", path], cwd=self._tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"add {path}"], cwd=self._tmp, check=True)
        subprocess.run(["git", "switch", "-q", "main"], cwd=self._tmp, check=True)

    def test_a_secret_in_the_diff_stops_the_push_before_github_is_touched(self) -> None:
        """#12: a real repository; the scan reads the diff's added lines and nothing is minted or pushed."""
        token = "ghp_" + "Q7w" * 10
        self.commit_on("lane/x", "Config.cs", f'const string Token = "{token}";\n')
        open_pr.identity.installation_token = lambda name: (_ for _ in ()).throw(AssertionError("no token"))
        open_pr.push = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not push"))
        with self.assertRaises(open_pr.SecretFound) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b")
        self.assertEqual((caught.exception.kind, caught.exception.where), ("GitHub token", "diff's added lines"))
        self.assertEqual((caught.exception.path, caught.exception.line), ("Config.cs", 1))
        self.assertNotIn(token, str(caught.exception))
        self.assertIn("nothing was pushed", str(caught.exception))

    def test_the_match_is_located_by_file_and_new_line_never_by_value(self) -> None:
        """#798 (2026-11-14 04:12): "the diff's added lines" alone, and the worktree gone — nobody could judge it."""
        key = "sk-" + "Vb7" * 8
        self.commit_on("lane/x", "README.md", "# Title\n\nplain\n")
        subprocess.run(["git", "switch", "-q", "lane/x"], cwd=self._tmp, check=True)
        (self.repo / "README.md").write_text("# Title\n\nplain\nmore\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "setup.md").write_text("one\ntwo\n" + f"export OPENAI_API_KEY={key}\n")
        subprocess.run(["git", "add", "-A"], cwd=self._tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "docs"], cwd=self._tmp, check=True)
        subprocess.run(["git", "switch", "-q", "main"], cwd=self._tmp, check=True)
        with self.assertRaises(open_pr.SecretFound) as caught:
            open_pr.scan(self.repo, "lane/x", "main", "t", "b")
        e = caught.exception
        self.assertEqual((e.kind, e.path, e.line), ("API key", "docs/setup.md", 3))
        self.assertEqual(str(e), "the diff's added lines at `docs/setup.md:3` holds something shaped like a API key "
                                 "— nothing was pushed")
        self.assertNotIn(key, str(e))
        self.assertNotIn(key[3:12], str(e))

    def test_added_lines_carry_their_file_and_new_line_number(self) -> None:
        diff = ("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -2,0 +3,2 @@\n+x\n+y\n"
                "@@ -9 +11 @@\n-old\n+new\ndiff --git a/n.md b/n.md\nnew file mode 100644\n--- /dev/null\n"
                "+++ b/n.md\n@@ -0,0 +1 @@\n+first\n")
        self.assertEqual(list(open_pr.added_lines(diff)),
                         [("a.txt", 3, "x"), ("a.txt", 4, "y"), ("a.txt", 11, "new"), ("n.md", 1, "first")])

    def test_the_title_and_body_are_scanned_and_a_known_value_is_caught_without_a_shape(self) -> None:
        jwt = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2Q"
        for title, body, known, where, kind in (
                (f"fix {jwt}", "b", (), "title", "JWT"),
                ("t", "the key is plainvalue42", ("plainvalue42",), "body", "configured credential"),
                ("t", "-----BEGIN RSA PRIVATE KEY-----", (), "body", "private key")):
            with self.subTest(where=where, kind=kind):
                with self.assertRaises(open_pr.SecretFound) as caught:
                    open_pr.scan(self.repo, "lane/x", "main", title, body, known)
                self.assertEqual((caught.exception.where, caught.exception.kind), (where, kind))

    def test_ordinary_code_and_removed_secrets_pass_the_scan(self) -> None:
        """No false park on C#'s CancellationToken, a lockfile hash, or a secret the branch removes."""
        self.commit_on("main", "Old.cs", 'var k = "ghp_' + "R8t" * 10 + '";\n')
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "main"], cwd=self._tmp, check=True)
        subprocess.run(["git", "branch", "-f", "lane/x", "main"], cwd=self._tmp, check=True)
        subprocess.run(["git", "switch", "-q", "lane/x"], cwd=self._tmp, check=True)
        subprocess.run(["git", "rm", "-q", "Old.cs"], cwd=self._tmp, check=True)
        (self.repo / "Loop.cs").write_text("var cancellationToken = CancellationToken.None;\n"
                                           '"contentHash": "Zx3k9Qm2Lp7Wv5Rt8Yb1Nc4Hd6Jf0Gs2Ae9Kq3Uw7Xo5Ti=="\n')
        subprocess.run(["git", "add", "-A"], cwd=self._tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "w"], cwd=self._tmp, check=True)
        subprocess.run(["git", "switch", "-q", "main"], cwd=self._tmp, check=True)
        open_pr.scan(self.repo, "lane/x", "main", "Fix the loop", "Closes #3")

    def test_an_unreadable_diff_is_a_failure_not_a_clean_scan(self) -> None:
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.scan(self.repo, "lane/never", "main", "t", "b")
        self.assertIn("could not be read for the secret scan", str(caught.exception))

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

    def test_ready_waits_until_github_shows_the_pushed_head(self) -> None:
        """#813: marked ready a second after the push, before GitHub had moved the head — ready_for_review went
        out with the old head, and the new head's only CI run still saw a draft."""
        self.reuse()
        self.drafts["N42"] = True
        self.lagging = ["0ld" * 13 + "0"] * 3  # three reads still show the old head
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        reads_before_ready = [e for e in self.events[:self.events.index(("ready", "N42"))] if e == ("read", "N42")]
        self.assertGreaterEqual(len(reads_before_ready), 4, "ready only after the head read back as pushed")
        self.assertLess(self.events.index(("push", "lane/x")), self.events.index(("ready", "N42")))
        self.assertEqual((result["draft"], result["auto_merge"]), (False, "armed"))

    def test_a_head_github_never_catches_up_with_is_a_named_failure_and_stays_a_draft(self) -> None:
        self.reuse()
        self.drafts["N42"] = True
        self.lagging = ["0ld" * 13 + "0"] * 10_000
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        self.assertIn("not marked ready", str(caught.exception))
        self.assertNotIn(("ready", "N42"), self.events)
        self.assertTrue(self.drafts["N42"])

    def test_an_unarmed_push_does_not_wait_for_the_head(self) -> None:
        self.reuse()
        self.lagging = ["0ld" * 13 + "0"] * 10_000
        self.assertEqual(open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)["draft"], True)

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

    def queue(self, entries: dict, heads: dict, refuse: str = "", sticks: bool = True):
        """A GitHub whose queue holds `entries` (node -> entry), for open_pr.enqueue."""
        calls = []

        def fake(token, query, variables, what):
            node = variables["id"]
            if "enqueuePullRequest" in query:
                calls.append((node, variables["head"]))
                if refuse:
                    raise open_pr.OpenPrError(refuse)
                if sticks:
                    entries[node] = {"state": "QUEUED", "position": 2, "enqueuedAt": "t"}
                return {"enqueuePullRequest": {"mergeQueueEntry": entries.get(node)}}
            return {"node": {"isDraft": self.drafts.get(node, False), "headRefOid": heads[node],
                             "isInMergeQueue": node in entries, "mergeQueueEntry": entries.get(node)}}

        open_pr._graphql = fake
        return calls

    def test_enqueue_puts_a_ready_pull_request_in_the_queue_and_reads_the_entry_back(self) -> None:
        self.drafts["N7"] = False
        calls = self.queue({}, {"N7": "abc"})
        self.assertEqual(open_pr.enqueue(7, "N7", "abc"), {"state": "QUEUED", "position": 2})
        self.assertEqual(calls, [("N7", "abc")], "with the expected head, so a push in between is refused")

    def test_enqueue_never_queues_a_draft_or_another_head_and_a_silent_no_op_is_a_failure(self) -> None:
        self.drafts["N8"], self.drafts["N9"], self.drafts["N10"] = True, False, False
        calls = self.queue({}, {"N8": "abc", "N9": "new", "N10": "abc"}, sticks=False)
        for number, node, head in ((8, "N8", "abc"), (9, "N9", "abc"), (10, "N10", "abc")):
            with self.subTest(node=node), self.assertRaises(open_pr.OpenPrError):
                open_pr.enqueue(number, node, head)
        self.assertEqual(calls, [("N10", "abc")])

    def test_a_refused_enqueue_is_a_success_only_when_the_entry_is_there(self) -> None:
        self.drafts["N7"] = False
        entries = {}
        self.queue(entries, {"N7": "abc"}, refuse="enqueue was refused: not mergeable")
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.enqueue(7, "N7", "abc")
        self.assertIn("not in the merge queue at abc", str(caught.exception))
        self.assertIn("not mergeable", str(caught.exception))
        entries["N7"] = {"state": "AWAITING_CHECKS", "position": 1}  # GitHub's auto-merge queued it first
        self.assertEqual(open_pr.enqueue(7, "N7", "abc"), {"state": "AWAITING_CHECKS", "position": 1})

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


def ci(id_, conclusion="", ready_job="IN_PROGRESS", status=None):
    status = status or ("COMPLETED" if conclusion else "IN_PROGRESS")
    return open_pr.github.CiRun(id_, status, conclusion, "2026-11-12T22:43:19Z", ready_job)


class ReadyRunTests(OpenPrCase):
    """B: a clean finish waits for the push's draft CI run before marking ready, then for a CI run that saw the
    pull request as ready and was not cancelled, before arming."""

    # #816 at 22:43:19Z: the ready run 43322494831 (its `plan` job ran, then was cancelled) and the draft run
    # 43322494914 (`plan` skipped, ci-ok's deliberate draft failure) created the same second; the draft run is
    # the newer, and its concurrency group cancelled the ready one
    READY_816 = ci(43322494831, "CANCELLED", "CANCELLED")
    DRAFT_816 = ci(43322494914, "FAILURE", "SKIPPED")

    reuse = FailurePathTests.reuse

    def script(self, *reads):
        """CI's answers, in order: each read returns the next, the last one repeats."""
        reads = list(reads)
        self.ci_script = lambda: reads.pop(0) if len(reads) > 1 else reads[0]

    def kinds(self):
        return [e[0] for e in self.events if e[0] in ("push", "ci", "ready", "draft", "arm")]

    def test_ready_waits_for_the_pushs_draft_run_to_exist(self) -> None:
        self.reuse()
        self.drafts["N42"] = True
        self.script([], [], [ci(1, ready_job="SKIPPED")], [ci(1, ready_job="SKIPPED"), ci(2)])
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        kinds = self.kinds()
        self.assertEqual(kinds[:5], ["push", "ci", "ci", "ci", "ready"], "marked ready only once a run existed")
        self.assertEqual(kinds[-1], "arm")
        self.assertEqual((result["draft"], result["auto_merge"], result["ready_run"]), (False, "armed", "ready run 2"))

    def test_no_draft_run_is_a_named_failure_and_it_stays_a_draft(self) -> None:
        self.reuse()
        self.drafts["N42"] = True
        self.script([])
        with self.assertRaises(open_pr.OpenPrError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        self.assertNotIsInstance(caught.exception, open_pr.ReadyRunError)
        self.assertIn("no CI run started on the pushed", str(caught.exception))
        self.assertNotIn("ready", self.kinds())
        self.assertTrue(self.drafts["N42"])

    def test_816s_cancelled_ready_run_is_toggled_once_and_then_armed(self) -> None:
        self.reuse()
        self.drafts["N42"] = True
        draft_first = ci(43322494700, "CANCELLED", "SKIPPED")  # the run the push started, before the race
        new_ready = ci(43322600000)
        self.script([draft_first],  # before ready: the push's run is there
                    [draft_first, self.READY_816, self.DRAFT_816],  # after ready: #816's race
                    [draft_first, self.READY_816, self.DRAFT_816, new_ready])  # after the toggle
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        kinds = [k for k in self.kinds() if k != "ci"]
        self.assertEqual(kinds, ["push", "ready", "draft", "ready", "arm"])
        self.assertEqual(result["ready_run"], "ready run 43322600000 after one draft/ready toggle")
        self.assertEqual((result["draft"], result["auto_merge"]), (False, "armed"))

    def test_a_ready_run_cancelled_again_after_the_toggle_is_a_named_failure_left_unarmed(self) -> None:
        self.reuse()
        self.drafts["N42"] = True
        draft_first = ci(43322494700, "CANCELLED", "SKIPPED")
        again = ci(43322600000, "CANCELLED", "")  # cancelled while queued: never got its `plan` job
        self.script([draft_first], [draft_first, self.READY_816, self.DRAFT_816],
                    [draft_first, self.READY_816, self.DRAFT_816, again])
        with self.assertRaises(open_pr.ReadyRunError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        self.assertEqual(caught.exception.number, 42)
        self.assertIn("cancelled again after one draft/ready toggle (run(s) 43322600000)", str(caught.exception))
        self.assertEqual([k for k in self.kinds() if k != "ci"], ["push", "ready", "draft", "ready"])
        self.assertFalse(self.armed.get("N42"))

    def test_no_ready_run_at_all_is_a_named_failure(self) -> None:
        self.reuse()
        self.drafts["N42"] = True
        self.script([ci(1, ready_job="SKIPPED")])
        with self.assertRaises(open_pr.ReadyRunError) as caught:
            open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        self.assertIn("no CI run saw it as ready", str(caught.exception))
        self.assertFalse(self.armed.get("N42"))

    def test_a_ready_run_must_stay_uncancelled_across_two_reads(self) -> None:
        self.reuse()
        self.drafts["N42"] = True
        first = ci(1, "CANCELLED", "SKIPPED")
        self.script([first], [first, ci(2)], [first, ci(2, "CANCELLED", "CANCELLED")],
                    [first, ci(2, "CANCELLED", "CANCELLED"), ci(3)])
        result = open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=True)
        self.assertEqual(result["ready_run"], "ready run 3 after one draft/ready toggle")

    def test_an_unarmed_finish_never_reads_ci(self) -> None:
        self.reuse()
        self.script([])
        open_pr.open_pr(self.repo, "lane/x", "t", "b", arm=False)
        self.assertNotIn("ci", self.kinds())
