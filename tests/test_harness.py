"""The session runner and the running-worker records. A fake `pi` stands in for the real one."""

import json
import os
import pathlib
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import harness  # noqa: E402
from scripts.common import StateError  # noqa: E402
from tests.helpers import TempState  # noqa: E402

SECRET = "sk-test-credential-value"


def event(text):
    return json.dumps({"type": "message_end", "message": {"content": [{"type": "text", "text": text}]}})


class HarnessCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        root = self.state.root
        self.envfile = root / "worker.env"
        self.envfile.write_text(f"# comment\nOTHER=x\nexport OPENCODE_GO_API_KEY=\"{SECRET}\"\n")
        self.fake = root / "pi"
        self.cfg = self.state.cfg
        self.cfg.harness.update(binary=str(self.fake), credential_file=str(self.envfile), stall_idle=2,
                                session_timeout=30)
        self.worktree = root / "wt"
        self.worktree.mkdir()
        # the real home a session must not see, with Pi's files and things that are not Pi's
        self.real_home = root / "realhome"
        for relative, text in ((".pi/agent/settings.json", "{}"), (".pi/agent/models-store.json", "{}"),
                               (".pi/agent/bin/fd", "fd"), (".pi/agent/auth.json", "{\"k\": \"secret\"}"),
                               (".config/lunate-lane/app.json", "{}"), (".ssh/id_ed25519", "key"),
                               (".gitconfig", "[credential]\n\thelper = osxkeychain\n")):
            (self.real_home / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.real_home / relative).write_text(text)
        self.home_patch = mock.patch.object(harness.pathlib.Path, "home", return_value=self.real_home)
        self.home_patch.start()
        self.cfg.checkout.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(self.cfg.checkout)], check=True)
        subprocess.run(["git", "-C", str(self.cfg.checkout), "config", "user.name", "Lane Test"], check=True)
        subprocess.run(["git", "-C", str(self.cfg.checkout), "config", "user.email", "lane@example.invalid"],
                       check=True)

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.state.close()

    def fake_pi(self, body: str) -> None:
        self.fake.write_text("#!/bin/sh\n" + body)
        self.fake.chmod(0o755)


class CredentialAndEnvTests(HarnessCase):
    def test_the_credential_is_read_from_the_named_key(self) -> None:
        self.assertEqual(harness.read_credential(self.cfg), SECRET)

    def test_a_missing_file_or_key_is_named_and_never_shows_a_value(self) -> None:
        self.envfile.write_text("OTHER=x\n")
        with self.assertRaises(harness.HarnessError) as caught:
            harness.read_credential(self.cfg)
        self.assertIn("OPENCODE_GO_API_KEY is not set", str(caught.exception))
        self.envfile.unlink()
        with self.assertRaises(harness.HarnessError):
            harness.read_credential(self.cfg)

    def test_the_session_gets_no_github_credential(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "ghs_x", "GITHUB_TOKEN": "ghs_y", "PATH": "/usr/bin"}):
            env = harness.session_env(self.cfg, self.state.root / "scratch")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(list(pathlib.Path(env["GH_CONFIG_DIR"]).iterdir()), [])
        self.assertEqual(env["OPENCODE_API_KEY"], SECRET)

    def test_the_session_gets_a_home_of_its_own_with_only_what_pi_needs(self) -> None:
        scratch = self.state.root / "scratch"
        env = harness.session_env(self.cfg, scratch)
        home = pathlib.Path(env["HOME"])
        self.assertEqual(home, scratch / "home")
        self.assertNotEqual(home, self.real_home)
        files = sorted(str(f.relative_to(home)) for f in home.rglob("*") if f.is_file())
        self.assertEqual(files, [".gitconfig", ".pi/agent/bin/fd", ".pi/agent/models-store.json",
                                 ".pi/agent/settings.json"])
        self.assertFalse((home / ".pi/agent/auth.json").exists())
        self.assertFalse((home / ".pi/agent/settings.json").is_symlink())
        gitconfig = (home / ".gitconfig").read_text()
        self.assertIn("name = Lane Test", gitconfig)
        self.assertNotIn("credential", gitconfig)

    def test_nothing_a_session_leaves_in_its_home_reaches_the_next(self) -> None:
        scratch = self.state.root / "scratch"
        home = pathlib.Path(harness.session_env(self.cfg, scratch)["HOME"])
        (home / ".bashrc").write_text("planted")
        (home / ".pi/agent/settings.json").write_text("tampered")
        harness.session_env(self.cfg, scratch)
        self.assertFalse((home / ".bashrc").exists())
        self.assertEqual((home / ".pi/agent/settings.json").read_text(), "{}")

    def test_an_unreadable_checkout_identity_is_a_named_failure(self) -> None:
        import shutil

        shutil.rmtree(self.cfg.checkout)
        with self.assertRaises(harness.HarnessError):
            harness.session_env(self.cfg, self.state.root / "scratch")

    def test_the_credential_never_reaches_argv(self) -> None:
        argv = harness.argv(self.cfg, "opencode-go/model", self.state.root, "s1", "the brief")
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(argv[-2:], ["--", "the brief"])

    def test_a_model_without_a_provider_is_refused(self) -> None:
        with self.assertRaises(harness.HarnessError):
            harness.argv(self.cfg, "bare-model", self.state.root, "s1", "p")


class SecretShapeTests(unittest.TestCase):
    def test_token_and_key_shapes_are_caught(self) -> None:
        cases = {
            "GitHub token": ["ghs_" + "a" * 36, "ghp_" + "Ab1" * 12, "github_pat_" + "11ABC_" * 6],
            "API key": ["sk-proj-" + "x" * 30],
            "OpenCode key": ["oc_" + "Zx9Qw8Er" + "7Ty6Ui5Op4As3Df2Gh1Jk0", "(oc_k3yfragm)", "key oc_abcdefgh."],
            "private key": ["-----BEGIN RSA PRIVATE KEY-----\nMIIE", "-----BEGIN PRIVATE KEY-----"],
            "AWS access key": ["AKIAIOSFODNN7EXAMPLE"],
            "JWT": ["eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOjEyMzR9.c2lnbmF0dXJlLXZhbHVl"],
            "credential assignment": ["OPENCODE_API_KEY=abcdefabcdefabcdef", "password: hunter2hunter2!"],
            "opaque key-like string": ["value Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0LmNb here"],
        }
        for kind, texts in cases.items():
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(harness.secret_kind(f"before {text} after"), kind)

    def test_a_known_value_is_caught_even_when_it_has_no_shape(self) -> None:
        self.assertEqual(harness.secret_kind("it is plainword", ("plainword",)), "configured credential")
        self.assertIsNone(harness.secret_kind("nothing here", ("",)))

    def test_ordinary_reports_pass(self) -> None:
        for text in ("1. met — src/Lunate.Tui/Shell.cs:42 (commit 3f9a2c1d4e5b6a7980f1e2d3c4b5a69788776655)",
                     "Tests: dotnet test --filter FullyQualifiedName~Lunate.Tui.ShellTests passed (12/12)",
                     "The token budget is described in docs/adr/0007-session-tokens.md",
                     "Refactored ConfigurationLoaderServiceFactoryProvider into two classes",
                     "the doc_string_formatter helper and the proc_handles_list both pass",
                     "short oc_abc is not a key"):
            with self.subTest(text=text):
                self.assertIsNone(harness.secret_kind(text))


class SessionTests(HarnessCase):
    def run_fake(self, **kw):
        return harness.run_session(self.cfg, model="opencode-go/m", worktree=self.worktree, prompt="brief",
                                   scratch=self.state.root / "scratch", log_path=self.state.root / "s.jsonl",
                                   tick=0.2, **kw)

    def test_a_clean_session_reports_its_exit_and_last_message(self) -> None:
        self.fake_pi(f"echo '{event('all done')}'\n[ \"$OPENCODE_API_KEY\" = '{SECRET}' ] || exit 7\n")
        outcome = self.run_fake()
        self.assertEqual((outcome.rc, outcome.stalled, outcome.leaked_markup), (0, False, False))
        self.assertEqual(harness.last_text(self.state.root / "s.jsonl"), "all done")

    def test_a_silent_session_is_killed_as_stalled(self) -> None:
        self.fake_pi("echo started\nsleep 60\n")
        started = time.monotonic()
        outcome = self.run_fake()
        self.assertTrue(outcome.stalled)
        self.assertLess(time.monotonic() - started, 30)

    def test_leaked_tool_markup_is_noticed(self) -> None:
        self.fake_pi(f"echo '{event('</arg_value><arg_key>timeout</arg_key>')}'\n")
        self.assertTrue(self.run_fake().leaked_markup)

    def test_ticks_run_while_the_session_works(self) -> None:
        self.fake_pi("echo a\nsleep 0.5\necho b\nsleep 0.5\n")
        ticks = []
        self.run_fake(on_tick=lambda: ticks.append(1))
        self.assertGreater(len(ticks), 0)

    def test_a_failing_tick_kills_the_session_instead_of_orphaning_it(self) -> None:
        self.fake_pi("echo $$ > \"$TMPDIR/pid\"\nwhile true; do echo tick; sleep 0.1; done\n")

        pid_file = self.state.root / "scratch" / "pid"

        def boom():
            if pid_file.exists() and pid_file.read_text().strip():
                raise RuntimeError("tick failed")

        with self.assertRaises(RuntimeError):
            self.run_fake(on_tick=boom)
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_a_sigterm_to_the_driver_kills_the_session_group(self) -> None:
        """What `kill <worker pid>` does: the handler raises SystemExit, run_session kills Pi's group on the way out."""
        import signal
        import threading

        self.fake_pi("echo $$ > \"$TMPDIR/pid\"\n(sleep 60) &\necho $! > \"$TMPDIR/child\"\n"
                     "while true; do echo tick; sleep 0.1; done\n")
        scratch = self.state.root / "scratch"
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in saved.items()])
        harness.exit_on_signals()

        def term_when_started():
            for _ in range(100):
                if (scratch / "child").exists() and (scratch / "child").read_text().strip():
                    break
                time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=term_when_started, daemon=True).start()
        with self.assertRaises(SystemExit) as caught:
            self.run_fake()
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        for name in ("pid", "child"):
            with self.subTest(process=name):
                pid = int((scratch / name).read_text())
                time.sleep(0.2)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)

    def test_on_start_gets_the_session_group(self) -> None:
        self.fake_pi("echo $$ > \"$TMPDIR/pid\"\necho done\n")
        seen = []
        self.fake_pi("echo $$ > \"$TMPDIR/pid\"\necho \"$@\" > \"$TMPDIR/args\"\necho done\n")
        self.run_fake(on_start=lambda pgid, sid: seen.append((pgid, sid)))
        scratch = self.state.root / "scratch"
        pgid, sid = seen[0]
        self.assertEqual(pgid, int((scratch / "pid").read_text()))
        self.assertIn(f"--session-id {sid} ", (scratch / "args").read_text())

    def test_an_unreadable_log_is_a_named_failure_not_an_empty_message(self) -> None:
        with self.assertRaises(harness.HarnessError):
            harness.last_text(self.state.root / "absent.jsonl")


class WorkerRecordTests(HarnessCase):
    def test_a_live_worker_is_running_and_a_dead_one_is_cleared(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "worker.py", "--issue", "5"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        harness.record_worker(5, proc.pid, frozenset({"area/tui"}), None)
        harness.record_worker(6, 999999, frozenset(), None)
        self.assertEqual(harness.running_workers(), {5: frozenset({"area/tui"})})
        self.assertFalse((self.cfg.state / "workers" / "6.json").exists())
        self.assertEqual(harness.live_pid(5), proc.pid)

    def orphan(self, session_id: str) -> subprocess.Popen:
        """A stand-in Pi in its own group. A thread reaps it, as launchd reaps a real orphan once it dies."""
        import threading

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "--session-id", session_id],
                                start_new_session=True)
        threading.Thread(target=proc.wait, daemon=True).start()
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(0.2)
        return proc

    def test_a_session_left_by_a_killed_driver_is_killed_when_its_record_is_cleared(self) -> None:
        pi = self.orphan("issue-5-20261112-120000-1")
        harness.record_worker(5, 999999, frozenset(), None)
        record = self.cfg.state / "workers" / "5.json"
        record.write_text(json.dumps({**json.loads(record.read_text()), "session_pgid": pi.pid,
                                      "session_id": "issue-5-20261112-120000-1"}))
        self.assertEqual(harness.running_workers(), {})
        self.assertIsNotNone(pi.wait(timeout=5), "the orphaned session must be gone")
        self.assertFalse(record.exists())

    def test_live_pid_also_clears_an_orphaned_session(self) -> None:
        pi = self.orphan("issue-6-x")
        harness.record_worker(6, 999999, frozenset(), None)
        harness.note_session(6, 999999, pi.pid, "issue-6-x")
        self.assertIsNone(harness.live_pid(6))
        self.assertIsNotNone(pi.wait(timeout=5))

    def test_a_recycled_session_pgid_running_something_else_is_left_alone(self) -> None:
        other = self.orphan("issue-55-x")  # card 5's record must not touch card 55's session
        harness.record_worker(5, 999999, frozenset(), None)
        harness.note_session(5, 999999, other.pid, "issue-5-x")
        self.assertEqual(harness.running_workers(), {})
        self.assertIsNone(other.poll())

    def test_another_session_of_the_same_card_is_left_alone(self) -> None:
        """Same card, same `issue-5-` prefix, different session id: not the session this record names."""
        newer = self.orphan("issue-5-20261112-130000-2")
        harness.record_worker(5, 999999, frozenset(), None)
        harness.note_session(5, 999999, newer.pid, "issue-5-20261112-120000-1")
        self.assertEqual(harness.running_workers(), {})
        self.assertIsNone(newer.poll())
        self.assertFalse((self.cfg.state / "workers" / "5.json").exists())

    def test_a_record_without_a_session_id_is_never_reaped(self) -> None:
        pi = self.orphan("issue-7-x")
        harness.record_worker(7, 999999, frozenset(), None)
        record = self.cfg.state / "workers" / "7.json"
        record.write_text(json.dumps({**json.loads(record.read_text()), "session_pgid": pi.pid}))
        self.assertEqual(harness.running_workers(), {})
        self.assertIsNone(pi.poll())

    def test_a_session_is_noted_only_on_the_drivers_own_record(self) -> None:
        harness.record_worker(5, 111, frozenset(), None)
        with self.assertRaises(StateError):
            harness.note_session(5, 222, 333, "issue-5-x")

    def test_an_unreadable_attempts_record_raises_rather_than_reading_as_not_parked(self) -> None:
        (self.cfg.state / "attempts").mkdir(parents=True, exist_ok=True)
        (self.cfg.state / "attempts" / "5.json").write_text("{")
        with self.assertRaises(StateError):
            harness.parked_cards()
        with self.assertRaises(StateError):
            harness.note_run(5, False)

    def test_a_recycled_pid_running_something_else_is_not_the_worker(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "worker.py", "--issue", "55"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertFalse(harness.worker_alive(proc.pid, 5))
        self.assertTrue(harness.worker_alive(proc.pid, 55))

    def test_an_unreadable_record_raises_rather_than_reading_as_not_running(self) -> None:
        workers = self.cfg.state / "workers"
        workers.mkdir(parents=True)
        (workers / "7.json").write_text("{not json")
        with self.assertRaises(StateError):
            harness.running_workers()

    def test_a_record_is_cleared_only_by_its_own_process(self) -> None:
        harness.record_worker(8, 4242, frozenset(), None)
        harness.clear_worker(8, 1)
        self.assertTrue((self.cfg.state / "workers" / "8.json").exists())
        harness.clear_worker(8, 4242)
        self.assertFalse((self.cfg.state / "workers" / "8.json").exists())

    def test_ps_failing_is_a_named_failure(self) -> None:
        with mock.patch.object(harness.subprocess, "run", side_effect=OSError("no ps")):
            with self.assertRaises(StateError):
                harness.worker_alive(os.getpid(), 1)


if __name__ == "__main__":
    unittest.main()
