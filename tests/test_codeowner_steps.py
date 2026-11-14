"""2026-11-14: a pull request waiting on code-owner review is not enqueued (and not a FAILED tick) but listed for the
maintainer; a card's "## Maintainer steps" never stop a card for a person."""

import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import cards, common, digest, feed, github, harness, open_pr, review as review_job  # noqa: E402
from tests.helpers import REQUIRED, TempState, card, pull, review  # noqa: E402
from tests.test_digest import CFG, NOW  # noqa: E402

# GitHub's answer to enqueuePullRequest on #820 and #838 every tick from 23:30 to 01:10, as the feed log kept it
REFUSAL = ("enqueue was refused: [{'type': 'UNPROCESSABLE', 'path': ['enqueuePullRequest'], 'locations': "
           "[{'line': 1, 'column': 38}], 'message': 'Pull request Waiting on code owner review from ")
CODEOWNERS = ("/adr/", "/.github/", "/AGENTS.md", "/.agents/")  # lunate's, 2026-11-14
FILES_838 = (".github/workflows/ci.yml", "CONTRIBUTING.md", "changes/838.md", "scripts/ci-ok.sh")
FILES_820 = (".markdownlint-cli2.yaml", "AGENTS.md", "CONTRIBUTING.md", "docs/README.md")

# #817's park reason, verbatim from the worker's final report on 2026-11-14 00:25
REASON_817 = ("the two Maintainer steps require consecutive green nightly macOS full-suite runs after merge. The #834 "
              "quarantine skips this test on macOS and PR CI no longer runs macOS, so no pull request can produce "
              "that evidence; a maintainer must merge and watch the nightlies (or explicitly accept the "
              "deterministic reproduction above).")


def waiting(number, card_number, files, decision="REVIEW_REQUIRED", **over):
    """The bot approved the head; GitHub still wants the code owner's approval."""
    return pull(number, card_number, files=files, node_id=f"PR_{number}", reviews=[review("APPROVED")],
                review_decision=decision, title=f"pr {number}", **over)


class CodeOwnerGateTests(unittest.TestCase):
    def test_only_github_s_approved_review_decision_is_enqueued(self) -> None:
        self.assertFalse(github.needs_enqueue(waiting(838, 830, FILES_838), REQUIRED, "lunate-reviewer"))
        self.assertFalse(github.needs_enqueue(waiting(838, 830, FILES_838, ""), REQUIRED, "lunate-reviewer"))
        self.assertTrue(github.needs_enqueue(waiting(838, 830, FILES_838, "APPROVED"), REQUIRED, "lunate-reviewer"))
        self.assertTrue(github.waits_for_approval(waiting(838, 830, FILES_838), REQUIRED, "lunate-reviewer"))

    def test_codeowners_patterns_match_as_github_does_for_lunate_s_file(self) -> None:
        self.assertEqual(github.owned_paths(FILES_838, CODEOWNERS), (".github/workflows/ci.yml",))
        self.assertEqual(github.owned_paths(FILES_820, CODEOWNERS), ("AGENTS.md",))
        self.assertEqual(github.owned_paths(("docs/AGENTS.md", "adr.md", "x/.github/a"), CODEOWNERS), ())
        self.assertEqual(github.owned_paths(("a/b.md", "b.md", "src/x.cs"), ("*.md", "docs/")), ("a/b.md", "b.md"))
        self.assertEqual(github.owned_paths(("docs/a", "x/docs/b"), ("docs/",)), ("docs/a", "x/docs/b"))

    def test_codeowners_are_read_from_the_first_place_github_looks(self) -> None:
        text = "# who reviews what\n\n/adr/ @wdaniel1993\n/AGENTS.md @wdaniel1993\n"
        with mock.patch.object(common, "gh", side_effect=[text]) as gh:
            self.assertEqual(github.codeowners(CFG), ("/adr/", "/AGENTS.md"))
        self.assertIn(".github/CODEOWNERS", gh.call_args.args[1])
        with mock.patch.object(common, "gh", side_effect=[common.GhError("gh api failed (1): HTTP 404: Not Found")] * 3):
            self.assertEqual(github.codeowners(CFG), ())
        with mock.patch.object(common, "gh", side_effect=common.GhError("HTTP 502")):
            with self.assertRaises(common.GhError):
                github.codeowners(CFG)


class CodeOwnerFeedTests(unittest.TestCase):
    """#820 and #838 on 2026-11-13: approved by the bot, green, refused by the queue until the code owner approved.
    Every tick from 23:30 FAILED on the refusal; now they wait, and the tick after the approval enqueues."""

    def setUp(self) -> None:
        self.state = TempState()
        self.p = waiting(838, 830, FILES_838)
        self.patches = [
            mock.patch.object(github, "milestone_cards", return_value=[]),
            mock.patch.object(github, "open_pulls", return_value=[pull(838, 830)]),
            mock.patch.object(github, "pull", side_effect=lambda cfg, n: self.p),
            mock.patch.object(harness, "running_workers", return_value={}),
            mock.patch.object(github, "required_checks", return_value=REQUIRED),
            mock.patch.object(feed, "start", side_effect=AssertionError("nothing is started")),
            mock.patch.object(feed.cleanup, "tidy"),
            mock.patch("sys.stdout", new=io.StringIO()),
            mock.patch.object(github, "run_draft", return_value=False),
            mock.patch.object(open_pr, "enqueue", side_effect=self.github_enqueue),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.state.close()

    def github_enqueue(self, number, node_id, head):
        if self.p.review_decision != "APPROVED":
            raise open_pr.OpenPrError(f"#{number} is not in the merge queue at {head[:8]} after enqueueing "
                                      f"(the enqueue was refused: {REFUSAL})")
        return {"state": "QUEUED", "position": 1}

    def test_waiting_on_the_code_owner_is_not_enqueued_nor_failed_and_is_enqueued_after_the_approval(self) -> None:
        with mock.patch.object(sys, "argv", ["feed.py"]):
            self.assertEqual(feed.main(), 0)
            self.assertEqual(feed.main(), 0)
            open_pr.enqueue.assert_not_called()
            log = (self.state.cfg.state / "logs" / "feed.log").read_text()
            self.assertNotIn("FAILED", log)
            self.p = waiting(838, 830, FILES_838, "APPROVED")  # the maintainer approved
            self.assertEqual(feed.main(), 0)
        open_pr.enqueue.assert_called_once_with(838, "PR_838", "h1")
        self.assertIn("enqueued #838: ready, approved at h1", (self.state.cfg.state / "logs" / "feed.log").read_text())

    def test_an_infra_ejection_waiting_on_the_code_owner_is_not_re_enqueued(self) -> None:
        e = github.Ejection("infra", "failed_checks", "2026-11-13T23:00:00Z", "w", 1)
        self.p = waiting(838, 830, FILES_838, queue_removal="failed_checks", queue_removed_at=e.at, ejection=e)
        with mock.patch.object(sys, "argv", ["feed.py"]):
            self.assertEqual(feed.main(), 0)
        open_pr.enqueue.assert_not_called()
        self.assertFalse(harness.requeues(838, "h1"))


class CodeOwnerDigestTests(unittest.TestCase):
    def test_waiting_on_you_lists_code_owner_review_with_the_owned_paths(self) -> None:
        pulls = [waiting(820, 751, FILES_820), waiting(838, 830, FILES_838),
                 waiting(840, 841, ("src/a.cs",)),  # nothing code-owned: an ordinary approval
                 waiting(842, 843, FILES_838, "APPROVED")]  # approved: the feeder enqueues it, not on you
        lines = digest.compose(CFG, [], pulls, [], REQUIRED, NOW, {}, codeowners=CODEOWNERS).splitlines()
        self.assertEqual(lines[1], "Waiting on you (3):")
        self.assertEqual(lines[2], "  code-owner review: #820 (AGENTS.md) https://github.com/lunate-agent/lunate/pull/820")
        self.assertEqual(lines[3], "  code-owner review: #838 (.github/workflows/ci.yml) "
                                   "https://github.com/lunate-agent/lunate/pull/838")
        self.assertEqual(lines[4], "  approve #840 pr 840 https://github.com/lunate-agent/lunate/pull/840")
        self.assertNotIn("FAILED", "\n".join(lines))


class MaintainerStepsTests(unittest.TestCase):
    """#817 was parked at 00:25 on 2026-11-14 for its Maintainer steps, which are for the maintainer after merge."""

    def test_817s_reason_is_not_a_needs_person(self) -> None:
        report = "Cause named, fix applied.\n\nNEEDS-PERSON: " + REASON_817 + "\n"
        self.assertTrue(github.after_merge_only(REASON_817))
        self.assertIsNone(github.needs_person(report))
        self.assertEqual(github.raw_needs_person(report), REASON_817)
        other = "NEEDS-PERSON: whether `cache prune` may delete pinned entries; the Maintainer steps say nothing"
        self.assertEqual(github.needs_person(other), other.split(": ", 1)[1], "only a reason *about* them is dropped")

    def test_the_worker_does_not_park_for_it_and_says_so(self) -> None:
        from scripts import worker
        state = TempState()
        self.addCleanup(state.close)
        with mock.patch.object(harness, "read_credential", return_value=None):
            self.assertIsNone(worker.person_reason(state.cfg, "Done.\nNEEDS-PERSON: " + REASON_817))
        log = (state.cfg.state / "logs" / "worker.log").read_text()
        self.assertIn("NEEDS-PERSON about a maintainer step after merge is not a park", log)

    def test_a_review_that_asks_a_person_for_them_is_an_ordinary_rework_not_a_park(self) -> None:
        body = "1. met\n\nNEEDS-PERSON: " + REASON_817
        p = pull(819, 817, reviews=[review("CHANGES_REQUESTED", body=body)])
        self.assertEqual(cards.rework_reason(p, "lunate-reviewer", REQUIRED), "changes requested")

    def test_an_approval_mentioning_them_stays_an_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "verdict.json"
            path.write_text(json.dumps({"event": "APPROVE", "body": "1. met\n\nNEEDS-PERSON: " + REASON_817, "written_by": "scripts/verdict.py"}))
            self.assertEqual(review_job.read_verdict(path)[0], "APPROVE")
            path.write_text(json.dumps({"event": "APPROVE", "body": "NEEDS-PERSON: check 3 needs a decision", "written_by": "scripts/verdict.py"}))
            self.assertEqual(review_job.read_verdict(path)[0], "REQUEST_CHANGES")

    def test_both_briefs_say_maintainer_steps_are_never_a_reason(self) -> None:
        worker_brief = (common.ROOT / "templates" / "worker-brief.md").read_text()
        review_brief = (common.ROOT / "templates" / "review-brief.md").read_text()
        self.assertIn('**A card\'s "## Maintainer steps" are never a reason for NEEDS-PERSON.**', worker_brief)
        self.assertIn('**A card\'s "## Maintainer steps" are never a reason for REQUEST_CHANGES or NEEDS-PERSON.**',
                      review_brief)
        # rendered, as the lane fills them: the rule survives, before the card's own text
        body = "## Acceptance\n\n1. x\n\n## Maintainer steps\n\n- watch the nightlies\n"
        rendered = common.render("review-brief.md", PR="819", REPO="o/r", TITLE="t", HEAD="h", CARD="817", BODY=body,
                                 DECISIONS="", **{k: "" for k in _placeholders("review-brief.md")
                                                  if k not in {"PR", "REPO", "TITLE", "HEAD", "CARD", "BODY",
                                                               "DECISIONS"}})
        self.assertLess(rendered.index("are never a reason for REQUEST_CHANGES"), rendered.index("watch the nightlies"))


def _placeholders(template: str) -> set:
    import re
    return set(re.findall(r"\{\{(\w+)\}\}", (common.ROOT / "templates" / template).read_text()))


if __name__ == "__main__":
    unittest.main()
