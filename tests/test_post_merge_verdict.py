"""2026-11-14: an acceptance check only evidence after the merge or outside the repository can satisfy never stops a
card (#843's check 6, #849's check 5); the reviewer's verdict is written once, atomically, by the lane's command
(#844's verdict.json could not be read at 03:29)."""

import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import cards, common, github, harness, review, verdict  # noqa: E402
from tests.helpers import REQUIRED, TempState, git, origin_and_clone, pull  # noqa: E402
from tests.helpers import review as a_review  # noqa: E402

# #843 (card #831) — its review's NEEDS-PERSON line, verbatim. Check 6: "The PR body states the measured wall time
# of a `merge_group` run before and after."
NEEDS_843 = ("NEEDS-PERSON: Acceptance check 6 is not evidenced: the required after value is an actual `merge_group` "
             "wall time, but the report says the first real queue run for this change is only available after merge; "
             "the cited shard timings are not a measured run wall time.")
REVIEW_843 = (NEEDS_843 + "\n\n## Acceptance checks\n\n1. **Met.**\n3. **Not met.** The daily CI nightly is scheduled "
              "at `.github/workflows/ci.yml:27-28`, but CodeQL's only schedule is weekly")
# #849 (card #814) — its review's check 5, verbatim, and the same finding as a NEEDS-PERSON line. Check 5: "The
# reviewer's brief requires checking the Intent decisions list, and says a missing list is a send-back."
CHECK_849 = ("5. **Not met — needs a person.** The requirement is for the lane reviewer brief itself to require "
             "checking the list and sending back a missing one. That brief is outside this repository and is not "
             "changed by this PR")
NEEDS_849 = ("NEEDS-PERSON: check 5 — the lane reviewer brief is outside this repository and is not changed by this "
             "PR, so no pull request here can evidence it.")
# #844's first verdict at 03:29, as the reviewer hand-wrote it: valid JSON, then one stray brace
VERDICT_844 = '{"event": "APPROVE", "body": "## Acceptance checks\\n\\n1. met. CI is green.\\n"}}'


class AfterMergeChecksTests(unittest.TestCase):
    def test_843s_check_6_is_not_a_park_and_the_rest_is_still_judged(self) -> None:
        self.assertTrue(github.after_merge_only(NEEDS_843.split(": ", 1)[1]))
        self.assertIsNone(github.needs_person(REVIEW_843))
        p = pull(843, 831, reviews=[a_review("CHANGES_REQUESTED", body=REVIEW_843)])
        self.assertEqual(cards.rework_reason(p, "lunate-reviewer", REQUIRED), "changes requested",
                         "check 3 not met is still a rework; check 6 alone parks nothing")

    def test_849s_check_5_outside_this_repository_is_not_a_park(self) -> None:
        self.assertIsNone(github.needs_person(NEEDS_849))
        self.assertIsNone(github.needs_person(CHECK_849), "a mid-line 'needs a person' was never one")

    def test_a_decision_or_a_merge_first_still_counts(self) -> None:
        decide = "NEEDS-PERSON: whether `cache prune` may delete pinned entries is a maintainer decision."
        self.assertEqual(github.needs_person(decide), decide.split(": ", 1)[1])
        first = "NEEDS-PERSON: blocked by #826; it can only land after the merge of #826."
        self.assertEqual(github.needs_person(first), first.split(": ", 1)[1], "a merge-first stays a blocked-by wait")

    def test_both_briefs_say_so_and_the_review_lists_it_as_non_blocking(self) -> None:
        review_brief = (common.ROOT / "templates" / "review-brief.md").read_text()
        worker_brief = (common.ROOT / "templates" / "worker-brief.md").read_text()
        verdict_part = review_brief[review_brief.index("## Your verdict"):]
        self.assertIn("**Nor is an acceptance check that only evidence from after the merge or from outside this "
                      "repository can\nsatisfy**", verdict_part)
        self.assertIn("`check <n>: maintainer step after merge — <what evidence, from where>`", verdict_part)
        self.assertIn("**Non-blocking**", verdict_part)
        self.assertIn("judge the\nother checks on their own", verdict_part)
        self.assertIn("the lane's own briefs\nare one", verdict_part)  # #849's check 5
        self.assertIn("`merge_group` wall time", verdict_part)  # #843's check 6
        self.assertNotIn("only a\nreal release can show it", review_brief)
        self.assertIn("never report NEEDS-PERSON for it", worker_brief)
        self.assertNotIn("can only come from a real release", worker_brief)


class VerdictCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.addCleanup(self.state.close)
        self.scratch = common.state_dir("scratch", "review-844")
        self.out, self.body = self.scratch / "verdict.json", self.scratch / "review.md"

    def run_command(self, event: str) -> subprocess.CompletedProcess:
        command = review.verdict_command(self.out, self.body) + f" --event {event}"
        return subprocess.run(command, shell=True, capture_output=True, text=True)

    def test_the_verdict_is_written_once_atomically_and_a_second_write_is_refused(self) -> None:
        self.body.write_text("## Acceptance checks\n\n1. **Met.** \"quoted\" and {braces}}\n")
        first = self.run_command("APPROVE")
        self.assertEqual(first.returncode, 0, first.stderr)
        written = self.out.read_text()
        self.assertEqual(review.read_verdict(self.out),
                         ("APPROVE", '## Acceptance checks\n\n1. **Met.** "quoted" and {braces}}'))
        self.body.write_text("changed my mind")
        second = self.run_command("REQUEST_CHANGES")
        self.assertEqual(second.returncode, 1)
        self.assertIn("this second one is refused, the first stands", second.stderr)
        self.assertEqual(self.out.read_text(), written, "the first verdict is untouched, never doubled")
        self.assertEqual([n.name for n in self.scratch.iterdir() if n.name.startswith(".verdict-")], [],
                         "no temporary file is left")
        review.log_refused(844, verdict.refused_path(self.out))
        log = (self.state.cfg.state / "logs" / "review.log").read_text()
        self.assertIn("#844 ERROR — the reviewer tried to write a second verdict (REQUEST_CHANGES,", log)
        self.assertIn("refused, the first verdict stands", log)

    def test_a_bad_event_or_an_empty_body_writes_nothing(self) -> None:
        self.body.write_text("  \n")
        self.assertEqual(self.run_command("APPROVE").returncode, 1)
        self.assertEqual(self.run_command("COMMENT").returncode, 2)
        self.assertFalse(self.out.exists())

    def test_a_hand_written_verdict_is_not_used(self) -> None:
        self.out.write_text(VERDICT_844)
        with self.assertRaisesRegex(review.ReviewError, "could not be read as JSON: Extra data"):
            review.read_verdict(self.out)
        self.out.write_text(json.dumps({"event": "APPROVE", "body": "fine"}))
        with self.assertRaisesRegex(review.ReviewError, "not written by scripts/verdict.py"):
            review.read_verdict(self.out)


class RunReviewVerdictTests(unittest.TestCase):
    """The whole review run, with a stand-in reviewer that follows the brief's command — twice."""

    def setUp(self) -> None:
        self.state = TempState()
        self.cfg = self.state.cfg
        self.origin, self.clone = origin_and_clone(self.state.root)
        self.addCleanup(self.state.close)
        out = mock.patch("sys.stdout", new=io.StringIO())
        out.start()
        self.addCleanup(out.stop)

    def test_a_reviewer_that_hands_in_twice_gets_its_first_verdict_posted_and_the_second_logged(self) -> None:
        def session(cfg, *, prompt, scratch, log_path, on_start, **_):
            on_start(4242, "review-5-x")
            commands = [line.strip() for line in prompt.splitlines() if line.strip().endswith("--event APPROVE")]
            self.assertEqual(len(commands), 1, "the brief names the command")
            body_file = pathlib.Path(shlex.split(commands[0])[shlex.split(commands[0]).index("--body-file") + 1])
            self.assertEqual(body_file.parent, scratch)
            body_file.write_text("1. **Met.**\n\n**Non-blocking**\n\n- check 6: maintainer step after merge — "
                                 "the `merge_group` wall time of the first queue run\n")
            for event in ("APPROVE", "REQUEST_CHANGES"):
                subprocess.run(commands[0].replace("--event APPROVE", f"--event {event}"), shell=True,
                               capture_output=True, check=False)
            log_path.write_text("")
            return harness.Outcome(0, False, 1, False)

        head = git(self.clone, "rev-parse", "origin/main")
        with mock.patch.object(harness, "run_session", side_effect=session), \
                mock.patch.object(review.dispatch_card, "card", return_value={"body": "b"}), \
                mock.patch.object(github, "worker_report", return_value=""), \
                mock.patch.object(github, "decisions_for", return_value=[]), \
                mock.patch.object(github, "open_pulls", return_value=[]):
            event, body, _ = review.run_review(self.cfg, pull(5, 9, head=head, files=("docs/a.md",)))
        self.assertEqual(event, "APPROVE")
        self.assertIn("check 6: maintainer step after merge", body)
        log = (self.cfg.state / "logs" / "review.log").read_text()
        self.assertIn("#5 ERROR — the reviewer tried to write a second verdict (REQUEST_CHANGES", log)

    def test_a_stale_verdict_from_an_earlier_run_is_cleared_first(self) -> None:
        scratch = common.state_dir("scratch", "review-5")
        verdict.write(scratch / "verdict.json", "APPROVE", "an earlier run's")
        (scratch / "verdict.json.refused").write_text('{"at": "old", "event": "APPROVE"}\n')

        def session(cfg, *, log_path, on_start, **_):
            on_start(4242, "review-5-x")
            log_path.write_text("")
            return harness.Outcome(0, False, 1, False)  # this run hands nothing in

        head = git(self.clone, "rev-parse", "origin/main")
        with mock.patch.object(harness, "run_session", side_effect=session), \
                mock.patch.object(review.dispatch_card, "card", return_value={"body": "b"}), \
                mock.patch.object(github, "worker_report", return_value=""), \
                mock.patch.object(github, "decisions_for", return_value=[]), \
                mock.patch.object(github, "open_pulls", return_value=[]):
            with self.assertRaisesRegex(review.ReviewError, "wrote no verdict"):
                review.run_review(self.cfg, pull(5, 9, head=head, files=("docs/a.md",)))
        self.assertFalse((self.cfg.state / "logs" / "review.log").exists()
                         and "ERROR" in (self.cfg.state / "logs" / "review.log").read_text())


if __name__ == "__main__":
    unittest.main()
