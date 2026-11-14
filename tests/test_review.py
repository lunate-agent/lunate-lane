"""Reviewer dispatch: what is skipped, what a verdict must look like, and the re-check before posting."""

import io
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, dispatch_card, github, harness, identity, review  # noqa: E402
from scripts.common import LaneError  # noqa: E402
from tests.helpers import REQUIRED, TempState, pull  # noqa: E402
from tests.helpers import review as a_review  # noqa: E402


class SkipTests(unittest.TestCase):
    def reason(self, p, running=None):
        return review.skip_reason(p, REQUIRED, running or {}, "lunate-reviewer")

    def test_a_green_unreviewed_pull_request_is_due(self) -> None:
        self.assertIsNone(self.reason(pull(1, 9)))

    def test_merged_or_closed_pull_requests_are_never_reviewed(self) -> None:
        self.assertEqual(self.reason(pull(1, 9, state="MERGED")), "is merged")
        self.assertEqual(self.reason(pull(1, 9, state="CLOSED")), "is closed")

    def test_drafts_and_running_workers_wait(self) -> None:
        self.assertEqual(self.reason(pull(1, 9, draft=True)), "is a draft")
        self.assertIn("worker on #9 is still running", self.reason(pull(1, 9), {9: frozenset()}))

    def test_checks_that_are_not_green_wait(self) -> None:
        self.assertIn("pending", self.reason(pull(1, 9, checks=[("ci-ok", "IN_PROGRESS", "")])))
        self.assertIn("failing", self.reason(pull(1, 9, checks=[("ci-ok", "COMPLETED", "FAILURE")])))
        self.assertIn("pending", self.reason(pull(1, 9, checks=[])))

    def test_a_head_already_reviewed_is_skipped_but_a_new_head_is_due(self) -> None:
        self.assertIn("already reviewed", self.reason(pull(1, 9, reviews=[a_review("APPROVED")])))
        self.assertIsNone(self.reason(pull(1, 9, head="h2", reviews=[a_review("CHANGES_REQUESTED", commit="h1")])))

    def test_847_and_848_are_not_held_behind_843_while_it_is_parked_or_a_draft(self) -> None:
        """2026-11-14 07:25: #847 and #848 were skipped every run behind #843 (card #831, parked) on ci-pipeline."""
        p843 = pull(843, 831, files=("openspec/changes/archive/2026-11-14-ci-fast-pr-gate/specs/ci-pipeline/spec.md",
                                     "openspec/specs/ci-pipeline/spec.md", ".github/workflows/ci.yml"))
        p847 = pull(847, 805, files=("openspec/changes/archive/2026-11-14-pre-ready-gate/specs/ci-pipeline/spec.md",
                                     "openspec/specs/ci-pipeline/spec.md"))
        p848 = pull(848, 809, files=("openspec/changes/archive/2026-11-14-specs-name-current-docs-paths/specs/"
                                     "ci-pipeline/spec.md", "openspec/specs/ci-pipeline/spec.md",
                                     "openspec/specs/tui/spec.md"))
        others = [p843, p847, p848]
        # parked: neither waits on it — but #848 still waits on #847, which is ready, unparked and older
        self.assertIsNone(review.skip_reason(p847, REQUIRED, {}, "lunate-reviewer", others, parked=frozenset({831})))
        self.assertIn("older #847 also changes the OpenSpec capability ci-pipeline",
                      review.skip_reason(p848, REQUIRED, {}, "lunate-reviewer", others, parked=frozenset({831})))
        # #847 parked as well: #848 is due
        self.assertIsNone(review.skip_reason(p848, REQUIRED, {}, "lunate-reviewer", others,
                                             parked=frozenset({831, 805})))
        # a draft #843 holds nothing back either
        drafts = [pull(843, 831, files=p843.files, draft=True), p847]
        self.assertIsNone(review.skip_reason(p847, REQUIRED, {}, "lunate-reviewer", drafts))
        # ready and unparked, #843 lands first, as before
        self.assertIn("older #843 also changes the OpenSpec capability ci-pipeline",
                      review.skip_reason(p847, REQUIRED, {}, "lunate-reviewer", others))

    def test_the_review_run_reads_the_parked_cards_for_the_overlap(self) -> None:
        state = TempState()
        self.addCleanup(state.close)
        p843 = pull(843, 831, files=("openspec/specs/ci-pipeline/spec.md",))
        p847 = pull(847, 805, files=("openspec/specs/ci-pipeline/spec.md",))
        with mock.patch.object(review.github, "required_checks", return_value=REQUIRED), \
                mock.patch.object(review.github, "open_pulls", return_value=[p843, p847]), \
                mock.patch.object(review.github, "pull", side_effect=lambda cfg, n: {843: p843, 847: p847}[n]), \
                mock.patch.object(review.harness, "running_workers", return_value={}), \
                mock.patch.object(review.harness, "parked_cards", return_value=frozenset({831})), \
                mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual([p.number for p in review.due(state.cfg)], [843, 847])
            review.harness.parked_cards.return_value = frozenset()
            self.assertEqual([p.number for p in review.due(state.cfg)], [843])

    def test_an_older_open_pull_request_on_the_same_capability_holds_a_newer_one_back(self) -> None:
        older = pull(4, 8, author="someone", files=("openspec/specs/agent-loop/spec.md",))
        newer = pull(6, 9, files=("openspec/changes/x/specs/agent-loop/spec.md", "src/A.cs"))
        reason = review.skip_reason(newer, REQUIRED, {}, "lunate-reviewer", [older, newer])
        self.assertIn("older #4", reason)
        self.assertIn("agent-loop", reason)
        self.assertIsNone(review.skip_reason(older, REQUIRED, {}, "lunate-reviewer", [older, newer]))
        unrelated = pull(7, 10, files=("openspec/specs/print-mode/spec.md",))
        self.assertIsNone(review.skip_reason(unrelated, REQUIRED, {}, "lunate-reviewer", [older, newer, unrelated]))

    def test_a_conflicting_pull_request_is_not_reviewed(self) -> None:
        self.assertIn("conflicts with the base branch", self.reason(pull(1, 9, mergeable="CONFLICTING")))

    def test_a_pull_request_with_no_card_is_not_reviewed(self) -> None:
        self.assertIn("no card", self.reason(pull(1, None)))


class VerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.path = self.state.root / "verdict.json"

    def tearDown(self) -> None:
        self.state.close()

    def test_a_missing_verdict_is_never_a_pass(self) -> None:
        with self.assertRaises(review.ReviewError):
            review.read_verdict(self.path)

    def test_a_malformed_verdict_is_never_a_pass(self) -> None:
        for text in ("{", json.dumps({"event": "COMMENT", "body": "x"}), json.dumps({"event": "APPROVE"}),
                     json.dumps({"event": "APPROVE", "body": "  "}), json.dumps(["APPROVE"])):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(review.ReviewError):
                    review.read_verdict(self.path)

    def test_a_well_formed_verdict_is_read(self) -> None:
        self.path.write_text(json.dumps({"event": "REQUEST_CHANGES", "body": "1. not met — x.cs:3", "written_by": "scripts/verdict.py"}))
        self.assertEqual(review.read_verdict(self.path), ("REQUEST_CHANGES", "1. not met — x.cs:3"))


class PostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.reviewed = pull(5, 9, head="h1")
        self.patches = [mock.patch.object(common, "rest"), mock.patch("sys.stdout", new=io.StringIO()),
                        mock.patch.object(identity, "installation_token", return_value="ghs_reviewer")]
        self.rest, _, self.mint = [patch.start() for patch in self.patches]
        self.author = "lunate-reviewer[bot]"
        self.rest.side_effect = lambda token, method, path, payload=None: (
            {"id": 31, "user": {"login": self.author}} if method == "POST" else
            {"id": 31, "user": {"login": self.author}} if path.endswith("/reviews/31") else {})

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.state.close()

    def test_a_pull_request_merged_during_the_review_gets_no_review(self) -> None:
        with mock.patch.object(github, "pull", return_value=pull(5, 9, state="MERGED")):
            self.assertFalse(review.post(self.state.cfg, self.reviewed, "APPROVE", "ok", "p/model-x"))
        self.rest.assert_not_called()
        self.mint.assert_not_called()

    def test_a_pull_request_that_moved_during_the_review_gets_no_review(self) -> None:
        with mock.patch.object(github, "pull", return_value=pull(5, 9, head="h2")):
            self.assertFalse(review.post(self.state.cfg, self.reviewed, "APPROVE", "ok", "p/model-x"))
        self.rest.assert_not_called()

    def test_the_verdict_is_posted_as_a_real_review_pinned_to_the_reviewed_head(self) -> None:
        with mock.patch.object(github, "pull", return_value=pull(5, 9, head="h1")):
            self.assertTrue(review.post(self.state.cfg, self.reviewed, "REQUEST_CHANGES", "fix x", "p/model-x"))
        self.mint.assert_called_once_with("reviewer")
        token, method, path, payload = self.rest.call_args_list[0].args
        self.assertEqual((token, method, path), ("ghs_reviewer", "POST", "/repos/lunate-agent/lunate/pulls/5/reviews"))
        self.assertEqual(self.rest.call_args_list[1].args[1:3], ("GET", "/repos/lunate-agent/lunate/pulls/5/reviews/31"))
        self.assertEqual((payload["commit_id"], payload["event"]), ("h1", "REQUEST_CHANGES"))
        self.assertTrue(payload["body"].startswith("fix x"))
        self.assertTrue(payload["body"].endswith("Reviewed at `h1` on `p/model-x`."), "the review names its model")

    def test_a_review_that_reads_back_under_another_author_fails_the_run(self) -> None:
        self.author = "lunate-worker[bot]"
        with mock.patch.object(github, "pull", return_value=pull(5, 9, head="h1")):
            with self.assertRaises(review.ReviewError) as caught:
                review.post(self.state.cfg, self.reviewed, "APPROVE", "ok", "p/model-x")
        self.assertIn("authored by 'lunate-worker'", str(caught.exception))

    def test_a_review_with_no_id_to_read_back_fails_the_run(self) -> None:
        self.rest.side_effect = lambda *a, **k: {}
        with mock.patch.object(github, "pull", return_value=pull(5, 9, head="h1")):
            with self.assertRaises(review.ReviewError):
                review.post(self.state.cfg, self.reviewed, "APPROVE", "ok", "p/model-x")

    def test_a_wrong_author_makes_the_job_exit_non_zero(self) -> None:
        self.author = "someone"
        with mock.patch.object(review, "due", return_value=[self.reviewed]), \
                mock.patch.object(review, "run_review", return_value=("APPROVE", "ok", "p/model-x")), \
                mock.patch.object(github, "pull", return_value=pull(5, 9, head="h1")), \
                mock.patch.object(sys, "argv", ["r"]):
            self.assertEqual(review.main(), 1)

    def test_an_unreadable_state_before_posting_posts_nothing(self) -> None:
        with mock.patch.object(github, "pull", side_effect=common.GhError("#5 could not be read")):
            with self.assertRaises(common.GhError):
                review.post(self.state.cfg, self.reviewed, "APPROVE", "ok", "p/model-x")
        self.rest.assert_not_called()

    def test_a_reviewer_that_cannot_mint_is_a_named_failure(self) -> None:
        self.mint.side_effect = identity.IdentityError("the token exchange was refused (401)")
        with mock.patch.object(github, "pull", return_value=pull(5, 9, head="h1")):
            with self.assertRaises(review.ReviewError):
                review.post(self.state.cfg, self.reviewed, "APPROVE", "ok", "p/model-x")


class TickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.patches = [
            mock.patch.object(github, "required_checks", return_value=REQUIRED),
            mock.patch.object(harness, "running_workers", return_value={}),
            mock.patch.object(github, "open_pulls", return_value=[pull(5, 9), pull(6, 10), pull(7, 11, author="x")]),
            mock.patch.object(github, "pull", side_effect=lambda cfg, n: pull(n, n + 4)),
            mock.patch("sys.stdout", new=io.StringIO()),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.state.close()

    def test_one_failed_review_does_not_stop_the_next_but_the_run_fails(self) -> None:
        posted = []

        def run_review(cfg, p):
            if p.number == 5:
                raise review.ReviewError("the reviewer wrote no verdict")
            return "APPROVE", "ok", "p/model-x"

        with mock.patch.object(review, "run_review", side_effect=run_review), \
                mock.patch.object(review, "post", side_effect=lambda cfg, p, e, b, m: posted.append(p.number) or True):
            self.assertEqual(review.main(), 1)
        self.assertEqual(posted, [6])
        log = (self.state.cfg.state / "logs" / "review.log").read_text()
        self.assertIn("#5 FAILED — the reviewer wrote no verdict", log)

    def test_a_card_that_cannot_be_read_fails_that_review_only_and_is_logged_not_raised(self) -> None:
        """dispatch_card.CardError is a LaneError: it fails one review, the next still runs, run_job logs it."""
        posted = []

        def run_review(cfg, p):
            if p.number == 5:
                raise dispatch_card.CardError("issue #9 is CLOSED, not open — nothing to dispatch")
            return "APPROVE", "ok", "p/model-x"

        with mock.patch.object(review, "run_review", side_effect=run_review), \
                mock.patch.object(review, "post", side_effect=lambda cfg, p, e, b, m: posted.append(p.number) or True), \
                mock.patch.object(sys, "argv", ["r"]):
            self.assertEqual(review.main(), 1)
        self.assertEqual(posted, [6])
        log = (self.state.cfg.state / "logs" / "review.log").read_text()
        self.assertIn("#5 FAILED — issue #9 is CLOSED", log)
        self.assertIn("review: FAILED — no review was posted for #5", log)

    def test_unreadable_required_checks_review_nothing(self) -> None:
        github.required_checks.side_effect = common.GhError("no required status checks found")
        with mock.patch.object(review, "run_review") as run_review:
            self.assertEqual(review.main(), 1)
        run_review.assert_not_called()

    def test_only_the_worker_identitys_pull_requests_are_considered(self) -> None:
        with mock.patch.object(review, "run_review", return_value=("APPROVE", "ok", "p/model-x")) as run_review, \
                mock.patch.object(review, "post", return_value=True):
            review.main()
        self.assertEqual([c.args[1].number for c in run_review.call_args_list], [5, 6])

    def test_a_dry_run_reviews_nothing(self) -> None:
        with mock.patch.object(review, "run_review") as run_review, mock.patch.object(sys, "argv", ["r", "--dry-run"]):
            self.assertEqual(review.main(), 0)
        run_review.assert_not_called()


class ModelTests(unittest.TestCase):
    """#15: the full model for code, tests, specs and CI; the light one for docs-only."""

    CFG = common.load_config()

    def model(self, files):
        return review.reviewer_model(self.CFG, pull(1, 9, files=files))

    def test_code_tests_specs_and_ci_get_the_full_model(self) -> None:
        for path in ("src/Lunate/Loop.cs", "tests/LoopTests.cs", "openspec/specs/agent-loop/spec.md",
                     ".github/workflows/ci.yml"):
            with self.subTest(path=path):
                self.assertEqual(self.model(("docs/guide.md", path)), "opencode-go/gpt-6-luna")

    def test_docs_only_gets_the_light_model(self) -> None:
        self.assertEqual(self.model(("docs/guide.md", "README.md", "changes/812.md")), "opencode-go/glm-5.3-flash")

    def test_an_unread_or_empty_file_list_gets_the_full_model(self) -> None:
        self.assertEqual(self.model(None), "opencode-go/gpt-6-luna")
        self.assertEqual(self.model(()), "opencode-go/gpt-6-luna")

    def test_a_lookalike_path_is_not_code(self) -> None:
        self.assertEqual(self.model(("docs/src/x.md", "srcs.md")), "opencode-go/glm-5.3-flash")


class BriefTests(unittest.TestCase):
    def test_the_neighbours_name_other_open_pull_requests_on_the_same_capabilities(self) -> None:
        mine = pull(6, 9, files=("openspec/changes/x/specs/agent-loop/spec.md",))
        other = pull(4, 8, title="Loop rewrite", files=("openspec/specs/agent-loop/spec.md", "src/B.cs"))
        text = review.spec_neighbours(mine, [mine, other, pull(5, 7, files=("docs/a.md",))])
        self.assertIn("This pull request changes: agent-loop.", text)
        self.assertIn('#4 "Loop rewrite" also changes agent-loop: `openspec/specs/agent-loop/spec.md`', text)
        self.assertNotIn("#5", text)
        self.assertIn("No other open pull request", review.spec_neighbours(mine, [mine]))
        self.assertIn("changes no OpenSpec capability", review.spec_neighbours(pull(5, 7), []))

    def test_the_review_brief_carries_the_spec_comparison_the_neighbours_and_the_intent_decisions(self) -> None:
        text = common.render("review-brief.md", PR="6", REPO="o/r", TITLE="t", HEAD="abc", WORKTREE="/w",
                             BASE="main", DIFF_FILE="/d", FRAGMENT_CHECK="f", BODY_FILE="/b", VERDICT_CMD="/c", CARD="9",
                             SPEC_NEIGHBOURS="NEIGHBOURS-HERE", WORKER_REPORT="REPORT-HERE", BODY="b", SCOPE="SCOPE-HERE",
                             DECISIONS="## Decisions this card waited on (verbatim)\n\nDecision: DECIDED-HERE\n")
        self.assertIn("git show origin/main:openspec/specs/<capability>/spec.md", text)
        self.assertLess(text.index("Decision: DECIDED-HERE"), text.index("## The issue (#9)"))
        self.assertIn("NEIGHBOURS-HERE", text)
        self.assertIn("REPORT-HERE", text)
        self.assertIn("**Intent decisions**", text)
        self.assertIn("reverting the archive commit", text)


class ErrorTypeTests(unittest.TestCase):
    def test_review_errors_are_lane_errors(self) -> None:
        self.assertTrue(issubclass(review.ReviewError, LaneError))

    def test_every_error_the_lane_catches_is_a_lane_error(self) -> None:
        from scripts import open_pr

        for error in (dispatch_card.CardError, open_pr.OpenPrError, identity.IdentityError):
            with self.subTest(error=error.__name__):
                self.assertTrue(issubclass(error, LaneError))


if __name__ == "__main__":
    unittest.main()


class NeedsPersonVerdictTests(unittest.TestCase):
    setUp, tearDown = VerdictTests.setUp, VerdictTests.tearDown

    def test_an_approval_with_needs_person_is_posted_as_a_request_for_changes(self) -> None:
        body = "1. met.\n4. cannot tell.\nNEEDS-PERSON: check 4 needs a real release"
        self.path.write_text(json.dumps({"event": "APPROVE", "body": body, "written_by": "scripts/verdict.py"}))
        self.assertEqual(review.read_verdict(self.path), ("REQUEST_CHANGES", body))

    def test_the_review_brief_blocks_only_on_the_five_kinds_and_leaves_test_evidence_to_ci(self) -> None:
        """3: overnight send-back was 9 of 9. Only blocking findings send a pull request back."""
        text = (review.common.ROOT / "templates" / "review-brief.md").read_text()
        verdict = text[text.index("## Your verdict"):]
        for kind in ("an acceptance check not met", "a behaviour or spec defect", "a required gate that is red or missing",
                     "a missing changelog fragment, or a missing or misplaced archive", "an OpenSpec lifecycle violation"):
            self.assertIn(kind, verdict)
        self.assertIn("**REQUEST_CHANGES only for a blocking finding**", verdict)
        self.assertIn("**Non-blocking**", verdict)
        self.assertIn("the pull request's title or description", verdict)
        self.assertIn("**Test evidence is CI's job**: never ask the worker to prove which tests it ran", text)
        for gone in ("Report counts.", "the tests you ran with counts", "a scope nit",
                     "a test\nthat does not test the change", "or anything you could not establish"):
            self.assertNotIn(gone, text)

    def test_the_review_brief_offers_needs_person_for_what_no_pull_request_can_evidence(self) -> None:
        text = (review.common.ROOT / "templates" / "review-brief.md").read_text()
        self.assertIn("NEEDS-PERSON: <the check, by number, and why no pull request can evidence it>", text)


class ReleaseFragmentTests(unittest.TestCase):
    """D: the review brief's changelog rule for a release pull request and for every other one."""

    def test_a_release_branch_or_title_may_edit_the_changelog(self) -> None:
        for p in (pull(820, None, branch="release/0.4.0", title="Release 0.4.0"),
                  pull(821, 830, title="chore(release): 0.4.0")):
            with self.subTest(p=p.number):
                text = review.fragment_check(p, True)
                self.assertIn("may edit `CHANGELOG.md` and needs no `changes/<n>.md` fragment", text)

    def test_other_pull_requests_keep_the_rule(self) -> None:
        for p in (pull(822, 9, title="Fix x"), pull(823, None, branch="release/0.4.0/x", title="x"),
                  pull(824, None, branch="feature/release", title="chore: release prep")):
            with self.subTest(p=p.number):
                self.assertEqual(review.fragment_check(p, True),
                                 f"never edit `CHANGELOG.md`; the PR must add `changes/{p.number}.md`.")
        self.assertIn("not on main yet", review.fragment_check(pull(825, 9), False))


class PersonOutranksSkipTests(unittest.TestCase):
    def test_a_head_a_person_approved_after_the_bot_was_dismissed_is_not_reviewed_again(self) -> None:
        p = pull(813, 794, head="5809330d1", reviews=[a_review("DISMISSED", commit="5809330d1"),
                                                      a_review("APPROVED", commit="5809330d1", author="a-maintainer")])
        self.assertEqual(review.skip_reason(p, REQUIRED, {}, "lunate-reviewer"), "@a-maintainer approved it at 5809330d")


class WaitsOnReviewTests(unittest.TestCase):
    def test_a_pull_request_whose_card_waits_on_an_open_issue_is_not_reviewed(self) -> None:
        p = pull(820, 751, waits_on=(749, 750))
        self.assertEqual(review.skip_reason(p, REQUIRED, {}, "lunate-reviewer"), "waits on #749, #750")
        self.assertIsNone(review.skip_reason(pull(820, 751), REQUIRED, {}, "lunate-reviewer"))


class ScopeTests(unittest.TestCase):
    """Scope blocks only outside the card's area or on a file another open pull request changes; a test that
    passes with the change reverted blocks only when the acceptance asks for it."""

    def test_the_brief_draws_the_lines(self) -> None:
        text = (review.common.ROOT / "templates" / "review-brief.md").read_text()
        verdict = text[text.index("## Your verdict"):]
        blocking = verdict[:verdict.index("**Everything else is non-blocking**")]
        rest = verdict[verdict.index("**Everything else is non-blocking**"):]
        self.assertIn("- a scope violation: a change outside the card's area;\n", blocking)
        self.assertNotIn("another open pull request", blocking, "overlap is never blocking (#818, #826)")
        self.assertIn("a test the acceptance asks for that would pass with the change reverted", blocking)
        self.assertIn("a small cleanup in the card's own area", rest)
        self.assertIn("a file or lines\nanother open pull request also changes", rest)
        self.assertIn("is **never** blocking and never a scope finding", text)
        self.assertIn("## Scope\n\n{{SCOPE}}", text)

    def test_the_scope_context_names_the_area_and_no_overlaps(self) -> None:
        # #818's shape: S1 shares openspec/specs/repo-quality/spec.md with #820 — not something the brief raises
        mine = pull(818, 749, labels=["area/docs", "kind/feature"],
                    files=("openspec/specs/repo-quality/spec.md", "scripts/docs-check"))
        self.assertEqual(review.scope_context(mine, "area/"), "The card's area: `area/docs`.")
        self.assertEqual(review.scope_context(pull(9, 9, files=("a.cs",)), "area/"),
                         "The card has no area label: its area is what its own text names.")
