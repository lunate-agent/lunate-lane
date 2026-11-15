"""The pinned Lunate build: only a commit on main is pinned; its publish runs in the sandbox with no model key; the
binary, its commit and its sha256 land in lane.json; and the harness refuses a binary whose hash does not match.
A fake runner stands in for `dotnet publish` and the built binary — the suite never builds."""

import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, harness, lunate_pin  # noqa: E402
from tests.helpers import TempState, git, origin_and_clone  # noqa: E402
from tests.test_harness import SECRET, HarnessCase  # noqa: E402


class PinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        root = self.state.root
        self.cfg = self.state.cfg
        self.envfile = root / "worker.env"
        self.envfile.write_text(f"OPENCODE_GO_API_KEY={SECRET}\n")
        self.envfile.chmod(0o600)
        self.cfg.harness.update(credential_file=str(self.envfile))
        self.real_home = root / "realhome"
        self.real_home.mkdir()
        self.home = mock.patch.object(pathlib.Path, "home", return_value=self.real_home)
        self.home.start()
        self.origin, self.clone = origin_and_clone(root)
        self.main = git(self.clone, "rev-parse", "HEAD").strip()
        git(self.clone, "checkout", "-q", "-b", "side")
        (self.clone / "side").write_text("x\n")
        git(self.clone, "add", "side")
        git(self.clone, "commit", "-q", "-m", "side")
        self.side = git(self.clone, "rev-parse", "HEAD").strip()
        git(self.clone, "checkout", "-q", "main")
        self.config_path = root / "lane.json"
        self.config_path.write_text(json.dumps({"repo": "x/y", "harness": {"binary": "pi", "session_timeout": 5400}},
                                               indent=2) + "\n")
        self.calls = []

    def tearDown(self) -> None:
        self.home.stop()
        self.state.close()

    def runner(self, version_sha=None, publish_rc=0):
        def run(argv, **kw):
            self.calls.append((argv, kw))
            if "publish" in argv:
                out = pathlib.Path(argv[argv.index("-o") + 1])
                out.mkdir(parents=True, exist_ok=True)
                (out / "lunate").write_bytes(b"built " + self.main.encode())
                return subprocess.CompletedProcess(argv, publish_rc)
            return subprocess.CompletedProcess(argv, 0, stdout=f"0.5.0-dev+{(version_sha or self.main)}\n", stderr="")
        return run

    def test_a_main_commit_is_published_in_the_sandbox_and_pinned_in_lane_json(self) -> None:
        done = lunate_pin.pin(self.cfg, self.main[:9], self.config_path, runner=self.runner())
        publish, version = self.calls
        self.assertEqual(publish[0][:3], [harness.SANDBOX_EXEC, "-f", str(publish[0][2])])
        self.assertIn("dotnet", publish[0])
        self.assertIn("-p:PublishSingleFile=true", publish[0])
        self.assertNotIn("OPENCODE_API_KEY", publish[1]["env"], "a build needs no model key")
        self.assertNotEqual(publish[1]["env"]["HOME"], str(self.real_home))
        self.assertEqual(version[0][0], harness.SANDBOX_EXEC, "the built binary is run in the sandbox too")
        dest = self.cfg.state / "tools" / f"lunate-main-{self.main[:12]}" / "lunate"
        self.assertEqual(dest.read_bytes(), b"built " + self.main.encode())
        pinned = json.loads(self.config_path.read_text())["harness"]
        self.assertEqual(pinned["lunate_commit"], self.main)
        self.assertEqual(pinned["lunate_sha256"], harness.file_sha256(dest))
        self.assertEqual(pinned["lunate_binary"], str(dest))
        self.assertEqual(pinned["session_timeout"], 5400, "the rest of lane.json is kept")
        self.assertEqual(done["commit"], self.main)
        self.assertFalse((self.cfg.state / "tools" / "src" / f"lunate-{self.main[:12]}").exists(),
                         "the source worktree is removed")
        self.assertNotIn(f"lunate-{self.main[:12]}", git(self.clone, "worktree", "list"))

    def test_a_commit_not_on_main_is_refused_and_nothing_is_built(self) -> None:
        with self.assertRaisesRegex(lunate_pin.PinError, "not on origin/main"):
            lunate_pin.pin(self.cfg, self.side, self.config_path, runner=self.runner())
        self.assertEqual(self.calls, [])
        self.assertNotIn("lunate_sha256", self.config_path.read_text())

    def test_a_failed_publish_leaves_lane_json_alone_and_cleans_up(self) -> None:
        with self.assertRaisesRegex(lunate_pin.PinError, "dotnet publish failed"):
            lunate_pin.pin(self.cfg, self.main, self.config_path, runner=self.runner(publish_rc=1))
        self.assertNotIn("lunate_sha256", self.config_path.read_text())
        self.assertNotIn(f"lunate-{self.main[:12]}", git(self.clone, "worktree", "list"))

    def test_a_binary_that_reports_another_commit_is_not_pinned(self) -> None:
        with self.assertRaisesRegex(lunate_pin.PinError, "not commit"):
            lunate_pin.pin(self.cfg, self.main, self.config_path, runner=self.runner(version_sha="0" * 40))
        self.assertNotIn("lunate_sha256", self.config_path.read_text())


class VerifyTests(HarnessCase):
    def setUp(self) -> None:
        super().setUp()
        self.binary = self.state.root / "lunate"
        self.binary.write_bytes(b"pinned build")
        self.cfg.harness.update(lunate_binary=str(self.binary), lunate_commit="16f9e25e914a9bc74da028ad0337666137ad0279",
                                lunate_sha256=harness.file_sha256(self.binary))

    def test_the_pinned_binary_passes(self) -> None:
        self.assertEqual(harness.verify_lunate(self.cfg), self.binary)

    def test_a_changed_binary_is_refused(self) -> None:
        self.binary.write_bytes(b"something else")
        with self.assertRaisesRegex(harness.HarnessError, "refused: the Lunate binary .* lane.json pins .*16f9e25e914a"):
            harness.verify_lunate(self.cfg)

    def test_no_pin_is_refused(self) -> None:
        self.cfg.harness.pop("lunate_sha256")
        with self.assertRaisesRegex(harness.HarnessError, "pins no Lunate build"):
            harness.verify_lunate(self.cfg)

    def test_a_refused_binary_never_starts_a_session(self) -> None:
        self.binary.write_text("#!/bin/sh\ntouch \"$LUNATE_HOME/ran\"\n")
        self.binary.chmod(0o755)
        with self.assertRaises(harness.HarnessError):
            harness.run_session(self.cfg, model="opencode-go/m", worktree=self.worktree, prompt="p",
                                scratch=self.state.root / "scratch", log_path=self.state.root / "s.jsonl",
                                harness=harness.LUNATE, tick=0.2)
        self.assertFalse((self.state.root / "scratch" / "lunate-home" / "ran").exists())


if __name__ == "__main__":
    unittest.main()
