"""#1035 (card #1028) merged with an acceptance check unmet: the worker's report called the SWE-bench comparison "a
maintainer step after merge", the reviewer listed it as non-blocking, and maintainer steps never block. An
acceptance check is the maintainer's only when the card itself lists it under "## Maintainer steps"."""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, review, verdict  # noqa: E402
from tests.helpers import TempState  # noqa: E402

BENCHMARK = ("A benchmark comparison on the SWE-bench subset with the tool on vs off records steps, tokens and "
             "resolve rate. If it doesn't improve anything, the tool is removed again.")
# card #1028's Acceptance, verbatim
CARD_1028 = ("## Scope\n\nAn optional `semantic_search` tool.\n\n## Acceptance\n\n"
             "- The tool is off by default and enabled like `grep`/`find`/`ls`; when disabled it is not offered to "
             "the model.\n"
             "- A search returns ranked file/line hits from the workspace index.\n"
             "- The index is a rebuildable cache: never committed and never written into the session; deleting it "
             "loses nothing but time.\n"
             f"- {BENCHMARK}\n\nRefs #357 #981\n")
# the approval #1035 merged on, abridged to its check lines, verbatim
APPROVAL_1035 = (
    "# Review: PR #1035\n\n**Verdict: APPROVE**\n\n## Acceptance checks\n\n"
    "1. **Met.** The registration path only adds `semantic_search` when the optional name is configured.\n\n"
    "2. **Met.** Ranked path/line/snippet results are asserted.\n\n"
    "3. **Met.** The index cache path is under the Lunate state root, not the workspace.\n\n"
    "4. **Not evidenced yet; maintainer step after merge, not blocking.** The harness supports the prepared-home "
    "on/off arms; `eval/README.md:153-159` states that the comparison needs Docker and a live provider credential "
    "and is a maintainer step.\n\n"
    "## Non-blocking\n\n"
    "- **Check 4: maintainer step after merge** — run the SWE-bench subset comparison with the tool on and off.\n")


class DeferredAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = TempState()
        self.addCleanup(self.state.close)
        self.path = common.state_dir("scratch", "review-1035") / "verdict.json"

    def hand_in(self, event: str, body: str) -> None:
        verdict.write(self.path, event, body)

    def test_1035s_approval_with_a_deferred_acceptance_check_is_sent_back(self) -> None:
        self.assertEqual(review.deferred_acceptance(APPROVAL_1035, CARD_1028), [4])
        self.hand_in("APPROVE", APPROVAL_1035)
        event, body = review.read_verdict(self.path, CARD_1028)
        self.assertEqual(event, "REQUEST_CHANGES")
        self.assertTrue(body.startswith(APPROVAL_1035.strip()), "the reviewer's body is kept")
        self.assertIn("## Blocking (the lane)", body)
        self.assertIn("**check 4 not met.**", body)
        self.assertIn('only an item the card itself lists under "## Maintainer steps" is', body)

    def test_a_request_for_changes_that_defers_one_carries_the_lanes_note_too(self) -> None:
        self.hand_in("REQUEST_CHANGES", APPROVAL_1035 + "\n## Blocking\n\n- the byte cap is not met\n")
        event, body = review.read_verdict(self.path, CARD_1028)
        self.assertEqual(event, "REQUEST_CHANGES")
        self.assertIn("**check 4 not met.**", body, "the worker learns check 4 is its to deliver")

    def test_the_card_listing_the_item_under_maintainer_steps_lets_it_through(self) -> None:
        card = CARD_1028.replace("\nRefs #357", f"\n## Maintainer steps\n\n- [ ] {BENCHMARK}\n\nRefs #357")
        self.assertEqual(review.deferred_acceptance(APPROVAL_1035, card), [])
        self.hand_in("APPROVE", APPROVAL_1035)
        self.assertEqual(review.read_verdict(self.path, card), ("APPROVE", APPROVAL_1035.strip()))

    def test_a_maintainer_step_only_in_that_section_is_numbered_past_the_acceptance(self) -> None:
        card = CARD_1028.replace(f"- {BENCHMARK}\n", "").replace(
            "\nRefs #357", f"\n## Maintainer steps\n\n- {BENCHMARK}\n\nRefs #357")
        self.assertEqual(review.deferred_acceptance(APPROVAL_1035, card), [], "check 4 is the card's Maintainer step")
        other = CARD_1028.replace("\nRefs #357", "\n## Maintainer steps\n\n- Watch the first nightly.\n\nRefs #357")
        self.assertEqual(review.deferred_acceptance(APPROVAL_1035, other), [4],
                         "a card with other Maintainer steps does not cover this acceptance check")

    def test_a_met_check_or_the_word_alone_is_no_deferral(self) -> None:
        body = ("1. **Met.** The card's Maintainer steps are left to the maintainer, as the brief says.\n"
                "2. **Met.**\n\n## Non-blocking\n\n- the card's maintainer steps were not checked, as asked\n")
        self.assertEqual(review.deferred_acceptance(body, CARD_1028), [])
        self.hand_in("APPROVE", body)
        self.assertEqual(review.read_verdict(self.path, CARD_1028)[0], "APPROVE")

    def test_without_the_card_nothing_is_held_by_a_number_it_cannot_place(self) -> None:
        card = "## Maintainer steps\n\n- Watch the first nightly.\n"
        self.assertEqual(review.deferred_acceptance(APPROVAL_1035, card), [])
        self.assertEqual(review.deferred_acceptance(APPROVAL_1035, "no sections"), [4])


if __name__ == "__main__":
    unittest.main()
