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

    def tearDown(self) -> None:
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

    def test_the_credential_never_reaches_argv(self) -> None:
        argv = harness.argv(self.cfg, "opencode-go/model", self.state.root, "s1", "the brief")
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(argv[-2:], ["--", "the brief"])

    def test_a_model_without_a_provider_is_refused(self) -> None:
        with self.assertRaises(harness.HarnessError):
            harness.argv(self.cfg, "bare-model", self.state.root, "s1", "p")


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
