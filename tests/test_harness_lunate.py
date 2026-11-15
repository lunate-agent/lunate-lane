"""Worker sessions on Lunate as well as Pi: the adapter, the reaper, the choice per card, the fallback to Pi and the
digest's per-harness line. A fake `lunate` stands in for the real one (the real one is exercised by hand and by the
live check, never by the suite)."""

import io
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import digest, harness, worker  # noqa: E402
from tests.helpers import pull  # noqa: E402
from tests.test_harness import SECRET, HarnessCase  # noqa: E402
from tests.test_worker import DriveCase, ISSUE  # noqa: E402

# the shape of a real Lunate 0.4.0 run (the probe of 2026-11-15: s_20261115-033849-e698921a)
EVENTS = [
    {"type": "run_started", "runId": "run_1", "sessionId": "s_20261115-033849-e698921a"},
    {"type": "tool_call_start", "runId": "run_1", "callId": "c1", "toolName": "bash"},
    {"type": "tool_call_result", "runId": "run_1", "callId": "c1", "output": "OPENCODE_API_KEY=KEY\n", "isError": False},
    {"type": "usage_updated", "runId": "run_1", "usage": {"input": 10, "output": 5}, "modelId": "deepseek-v4.1-flash"},
    {"type": "text_message_start", "runId": "run_1", "messageId": "m1"},
    {"type": "text_message_content", "runId": "run_1", "messageId": "m1", "text": "do"},
    {"type": "text_message_content", "runId": "run_1", "messageId": "m1", "text": "ne"},
    {"type": "text_message_end", "runId": "run_1", "messageId": "m1"},
    {"type": "run_finished", "runId": "run_1", "sessionId": "s_20261115-033849-e698921a", "stopReason": "stop"},
]


class LunateSessionTests(HarnessCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg.harness.update(lunate_binary=str(self.fake))

    def fake_lunate(self, extra: str = "") -> None:
        lines = "\n".join(json.dumps(e).replace("KEY", '"$OPENCODE_API_KEY"') for e in EVENTS)
        body = ('printf "%s\\n" "$@" > "$LUNATE_HOME/argv"\n'
                f"cat <<JSON\n{lines}\nJSON\n"
                'mkdir -p "$LUNATE_HOME/sessions" && env > "$LUNATE_HOME/sessions/own.jsonl"\n' + extra)
        self.fake.write_text("#!/bin/sh\n" + body)
        self.fake.chmod(0o755)

    def run_lunate(self, **kw):
        return harness.run_session(self.cfg, model="opencode-go/deepseek-v4.1-flash", worktree=self.worktree,
                                   prompt="You are in a git worktree at " + str(self.worktree), harness=harness.LUNATE,
                                   scratch=self.state.root / "scratch", log_path=self.state.root / "s.jsonl", tick=0.2,
                                   **kw)

    def test_the_flags_map_one_to_one_and_the_run_reads_like_pis(self) -> None:
        self.fake_lunate()
        seen = []
        outcome = self.run_lunate(on_start=lambda pgid, sid: seen.append(sid))
        home = self.state.root / "scratch" / "lunate-home"
        argv = (home / "argv").read_text().splitlines()
        self.assertEqual(argv[:8], ["-p", "--json", "--model", "opencode-go/deepseek-v4.1-flash", "--yolo",
                                    "--no-extensions", "--offline", f"You are in a git worktree at {self.worktree}"])
        self.assertNotIn("--", argv, "Lunate 0.4.0 refuses the end-of-options marker")
        self.assertEqual((outcome.rc, outcome.stalled, outcome.session_id), (0, False, "s_20261115-033849-e698921a"))
        self.assertEqual(harness.last_text(self.state.root / "s.jsonl"), "done")
        self.assertEqual(seen, [harness.LUNATE_MARK + str(self.worktree)])
        log, own = (self.state.root / "s.jsonl").read_text(), (home / "sessions" / "own.jsonl").read_text()
        for text in (log, own):  # the same redaction as for Pi: the transcript and Lunate's own state root
            self.assertNotIn(SECRET, text)
        self.assertIn(f"LUNATE_HOME={home}", own, "the session's own state root")
        self.assertIsNone(harness.session_spend(self.state.root / "s.jsonl"), "Lunate 0.4.0 priced nothing")

    def test_a_later_round_continues_the_session_lunate_reported(self) -> None:
        self.fake_lunate()
        self.run_lunate(session_id="s_20261115-033849-e698921a")
        argv = (self.state.root / "scratch" / "lunate-home" / "argv").read_text().splitlines()
        self.assertEqual(argv[7:9], ["--session", "s_20261115-033849-e698921a"])

    def test_the_reaper_tells_a_lunate_session_apart_by_its_worktree(self) -> None:
        cmd = f"/x/tools/lunate-0.4.0/lunate -p --json --model m --yolo You are in a git worktree at {self.worktree}"
        record = harness.LUNATE_MARK + str(self.worktree)
        with mock.patch.object(harness, "_command", return_value=cmd), \
                mock.patch.object(harness.os, "killpg", side_effect=ProcessLookupError):
            self.assertTrue(harness.reap_session(3, (4242, record)))
        for other in (cmd.replace(str(self.worktree), "/elsewhere"), cmd.replace(" -p ", " "), "/usr/bin/sleep 60"):
            with mock.patch.object(harness, "_command", return_value=other), \
                    mock.patch.object(harness.os, "killpg", side_effect=AssertionError("never signalled")):
                self.assertFalse(harness.reap_session(3, (4242, record)), other)


class HarnessChoiceTests(DriveCase):
    LABELLED = {**ISSUE, "labels": [{"name": "lane/harness-lunate"}]}

    def session(self, commit: bool):
        self.used = []

        def run(cfg, *, worktree, log_path, on_tick, harness="pi", **_):
            self.used.append(harness)
            if commit:
                from tests.helpers import git
                git(worktree, "config", "user.email", "w@example.invalid")
                git(worktree, "config", "user.name", "w")
                self.commits = getattr(self, "commits", 0) + 1
                (worktree / "fix.cs").write_text(f"fixed {self.commits}\n")
                git(worktree, "add", "fix.cs")
                git(worktree, "commit", "-q", "-m", "fix")
                on_tick()
            log_path.write_text('{"type":"message_end","message":{"role":"assistant","content":[{"type":"text",'
                                '"text":"done"}],"usage":{"cost":{"total":0.012}}}}\n')
            return harness_mod.Outcome(0, False, 60, False)

        harness_mod = harness
        return mock.patch.object(harness, "run_session", side_effect=run)

    def test_pi_is_the_default_and_the_label_runs_lunate(self) -> None:
        with self.session(commit=True):
            worker.drive(self.cfg, 3, None)
        self.assertEqual(self.used, ["pi"])
        with self.session(commit=True), mock.patch.object(worker.dispatch_card, "card", return_value=self.LABELLED):
            worker.drive(self.cfg, 3, None)
        self.assertEqual(self.used, ["lunate"])
        stats = harness.session_stats()
        self.assertEqual([(r["harness"], r["kind"], r["new_commit"], r["spend"]) for r in stats],
                         [("pi", "new", True, 0.012), ("lunate", "new", True, 0.012)])

    def test_a_lunate_session_without_a_commit_sends_the_next_attempt_to_pi_once(self) -> None:
        with mock.patch.object(worker.dispatch_card, "card", return_value=self.LABELLED):
            with self.session(commit=False):
                worker.drive(self.cfg, 3, None)
            self.assertEqual(self.used, ["lunate"])
            with self.session(commit=True):
                worker.drive(self.cfg, 3, None)
            self.assertEqual(self.used, ["pi"])
            with self.session(commit=True):
                worker.drive(self.cfg, 3, None)
            self.assertEqual(self.used, ["lunate"], "one attempt on Pi, then Lunate again")
        log = (self.cfg.state / "logs" / "worker-3.log").read_text()
        self.assertIn("fallback to pi: the Lunate session ended without a commit (rc 0)", log)

    def test_a_lunate_setup_failure_falls_back_too(self) -> None:
        with mock.patch.object(worker.dispatch_card, "card", return_value=self.LABELLED), \
                mock.patch.object(harness, "run_session", side_effect=harness.HarnessError("lunate could not be started")):
            with self.assertRaises(harness.HarnessError):
                worker.drive(self.cfg, 3, None)
        self.assertEqual(harness.attempts(3)["fallback"], "Lunate failed in setup: lunate could not be started")


class HarnessDigestTests(unittest.TestCase):
    def test_one_line_per_harness(self) -> None:
        stats = [dict(harness="pi", card=1, kind="new", new_commit=True, spend=0.05, at=1),
                 dict(harness="pi", card=1, kind="send-back", new_commit=True, spend=0.03, at=2),
                 dict(harness="pi", card=2, kind="new", new_commit=False, spend=0.01, at=3),
                 dict(harness="lunate", card=5, kind="new", new_commit=True, spend=None, at=4)]
        merged = [{"number": 11, "closingIssuesReferences": [{"number": 1}]},
                  {"number": 15, "closingIssuesReferences": [{"number": 5}]}]
        self.assertEqual(digest.harness_lines(stats, stats, merged), [
            "Harnesses since yesterday:",
            "  pi: 3 session(s), 1 merge(s), 1.0 rework rounds/merge, 1 idle, send-back 50%, $0.09",
            "  lunate: 1 session(s), 1 merge(s), 0.0 rework rounds/merge, 0 idle, send-back 0%, n/a"])
        self.assertEqual(digest.harness_lines([], [], []), ["Harnesses since yesterday: no sessions"])
