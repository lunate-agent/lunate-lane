"""Cron job: review every worker pull request that is green and not yet reviewed at its head.

    .venv/bin/python scripts/review.py            # review everything due, one at a time
    .venv/bin/python scripts/review.py --dry-run  # say what is due and why the rest is skipped

The review runs on the reviewer model (which the config keeps different from the worker's), in a detached
worktree at the head, with the diff and the issue in its brief. It writes a JSON verdict; the lane re-reads the
pull request and, only if it is still open at the same head, posts the verdict as a real GitHub review —
APPROVE or REQUEST_CHANGES — as the `lunate-reviewer` identity, then reads the review back and fails the run if
GitHub does not name `lunate-reviewer` as its author. A missing or malformed verdict posts nothing.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import common, dispatch_card, github, harness, identity  # noqa: E402
from scripts.common import LaneError  # noqa: E402

JOB = "review"
EVENTS = ("APPROVE", "REQUEST_CHANGES")


class ReviewError(LaneError):
    """The review could not be run, or produced no usable verdict."""


def skip_reason(p: github.Pull, required: frozenset, running: dict, reviewer: str, others=()) -> str | None:
    """Why this pull request is not reviewed now, or None if it is due. `others` are the open pull requests (any
    author, with their files): one that is older and touches the same OpenSpec capability holds this one back."""
    if p.state != "OPEN":
        return f"is {p.state.lower()}"
    if p.draft:
        return "is a draft"
    if p.card in running:
        return f"its worker on #{p.card} is still running"
    if p.card is None:
        return "carries no card to review against"
    if p.mergeable == "CONFLICTING":
        return "conflicts with the base branch — it goes back to a worker first"
    checks = github.check_state(p, required)
    if checks != "green":
        return f"required checks are {checks}"
    if github.reviewer_verdict(p, reviewer) is not None:
        return f"already reviewed at {p.head[:8]}"
    overlap = github.spec_overlap(p, [o for o in others if o.number != p.number])
    if overlap:
        older, shared = overlap
        return f"older #{older} also changes the OpenSpec capability {', '.join(sorted(shared))} — it lands first"
    return None


FULL_REVIEW = ("src/", "tests/", "openspec/", ".github/")


def reviewer_model(cfg: common.Config, p: github.Pull) -> str:
    """The full model for anything touching code, tests, specs or CI; the light one for docs-only (#15). A file
    list that was not read, or is empty, gets the full model — never the lighter review by default."""
    if not p.files or any(path.startswith(FULL_REVIEW) for path in p.files):
        return cfg.reviewer_full
    return cfg.reviewer_light


def due(cfg: common.Config) -> list[github.Pull]:
    required = github.required_checks(cfg)
    running = harness.running_workers()
    found = []
    pulls = github.open_pulls(cfg)
    for summary in pulls:
        if summary.author != cfg.worker_app:
            continue
        p = github.pull(cfg, summary.number)
        reason = skip_reason(p, required, running, cfg.reviewer_app, pulls)
        if reason:
            common.log(JOB, f"#{p.number} skipped — {reason}")
        else:
            found.append(p)
    return found


def read_verdict(path: pathlib.Path) -> tuple[str, str]:
    """(event, body) from the reviewer's verdict file. Anything else is a ReviewError — never an approval."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewError(f"the reviewer wrote no verdict to {path}") from exc
    except (OSError, ValueError) as exc:
        raise ReviewError(f"the verdict {path} could not be read as JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("event") not in EVENTS:
        raise ReviewError(f"the verdict's event must be one of {', '.join(EVENTS)}")
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ReviewError("the verdict has no body")
    return data["event"], body.strip()


def post(cfg: common.Config, p: github.Pull, event: str, body: str, model: str) -> bool:
    """Post the review as the reviewer identity — only if the pull request is still open at the reviewed head."""
    now = github.pull(cfg, p.number)
    if now.state != "OPEN":
        common.log(JOB, f"#{p.number} is {now.state.lower()} now — the review is not posted")
        return False
    if now.head != p.head:
        common.log(JOB, f"#{p.number} moved to {now.head[:8]} during the review — not posted, the next run reviews it")
        return False
    try:
        token = identity.installation_token("reviewer")
    except identity.IdentityError as exc:
        raise ReviewError(f"the reviewer identity could not mint a token: {exc}") from exc
    footer = f"\n\n---\nReviewed at `{p.head[:9]}` on `{model}`."
    posted = common.rest(token, "POST", f"/repos/{cfg.slug}/pulls/{p.number}/reviews",
                         {"commit_id": p.head, "event": event, "body": body + footer})
    review_id = posted.get("id") if isinstance(posted, dict) else None
    if not review_id:
        raise ReviewError(f"#{p.number}: the review was sent but GitHub returned no id — its author is unchecked")
    back = common.rest(token, "GET", f"/repos/{cfg.slug}/pulls/{p.number}/reviews/{review_id}")
    author = github.login(back.get("user") if isinstance(back, dict) else None)
    if author != cfg.reviewer_app:
        raise ReviewError(f"#{p.number}: review {review_id} reads back as authored by {author or 'nobody'!r}, "
                          f"not {cfg.reviewer_app} — check app.json; a submitted review cannot be deleted, "
                          "dismiss it by hand")
    return True


def run_review(cfg: common.Config, p: github.Pull) -> tuple[str, str, str]:
    """Run the reviewer session at the head and return its verdict and the model it ran on."""
    model = reviewer_model(cfg, p)
    repo, worktree = cfg.checkout, common.state_dir("reviews") / f"pr-{p.number}"
    scratch = common.state_dir("scratch", f"review-{p.number}")
    verdict_file, diff_file = scratch / "verdict.json", scratch / "diff.patch"
    try:
        verdict_file.unlink(missing_ok=True)
    except OSError as exc:
        raise ReviewError(f"the old verdict {verdict_file} could not be removed: {exc}") from exc
    common.git(repo, "fetch", "-q", "origin", p.head)
    if worktree.exists():
        common.git(repo, "worktree", "remove", "--force", str(worktree))
    driver = os.getpid()
    harness.record_review(p.number, driver, worktree)  # so a SIGKILLed run's session and worktree get cleared
    common.git(repo, "worktree", "add", "--detach", str(worktree), p.head)
    try:
        try:
            diff_file.write_text(common.git(worktree, "diff", f"origin/{cfg.base}...HEAD"))
        except OSError as exc:
            raise ReviewError(f"the diff {diff_file} could not be written: {exc}") from exc
        issue = dispatch_card.card(worktree, p.card)
        fragments = ("never edit `CHANGELOG.md`; the PR must add `changes/" + str(p.number) + ".md`."
                     if common.git(repo, "ls-tree", "--name-only", f"origin/{cfg.base}", "--", "changes")
                     else "the `changes/` convention is not on main yet; `CHANGELOG.md` must not be edited.")
        report = github.worker_report(cfg, p.number) or "(the worker posted no session report on this pull request)"
        prompt = common.render("review-brief.md", PR=str(p.number), REPO=cfg.slug, TITLE=p.title, HEAD=p.head[:9],
                               WORKTREE=str(worktree), BASE=cfg.base, DIFF_FILE=str(diff_file),
                               FRAGMENT_CHECK=fragments, VERDICT_FILE=str(verdict_file), CARD=str(p.card),
                               SPEC_NEIGHBOURS=spec_neighbours(p, github.open_pulls(cfg)), WORKER_REPORT=report,
                               BODY=(issue.get("body") or "").strip())
        log_path = common.state_dir("sessions") / f"review-{p.number}-{p.head[:8]}.jsonl"
        outcome = harness.run_session(cfg, model=model, worktree=worktree, prompt=prompt,
                                      scratch=scratch, log_path=log_path,
                                      on_start=lambda pgid, sid: harness.note_review_session(p.number, driver, pgid, sid))
        if outcome.stalled or outcome.rc != 0:
            raise ReviewError(f"the review session did not finish cleanly (rc {outcome.rc}, stalled {outcome.stalled})")
        return (*read_verdict(verdict_file), model)
    finally:
        common.git(repo, "worktree", "remove", "--force", str(worktree))
        harness.clear_review(p.number, driver)


def spec_neighbours(p: github.Pull, others) -> str:
    """The other open pull requests that change one of `p`'s OpenSpec capabilities, for the review brief."""
    mine = p.capabilities
    if not mine:
        return "This pull request changes no OpenSpec capability."
    lines = [f"This pull request changes: {', '.join(sorted(mine))}."]
    for other in sorted(others, key=lambda o: o.number):
        shared = mine & other.capabilities
        if other.number != p.number and shared:
            paths = [f for f in other.files or () if github.capabilities([f]) & shared]
            lines.append(f"- #{other.number} \"{other.title}\" also changes {', '.join(sorted(shared))}: "
                         + ", ".join(f"`{f}`" for f in paths[:20]) + (" …" if len(paths) > 20 else ""))
    if len(lines) == 1:
        lines.append("No other open pull request changes these capabilities.")
    return "\n".join(lines)


def remove_review_worktree(cfg: common.Config, worktree: pathlib.Path) -> None:
    """Remove a review worktree a dead run left behind, and prune its registration. Only under <state>/reviews."""
    if common.state_dir("reviews") not in worktree.parents:
        raise LaneError(f"{worktree} is not a lane review worktree — left alone")
    if (worktree / ".git").exists():
        common.git(cfg.checkout, "worktree", "remove", "--force", str(worktree))
    elif worktree.exists():
        try:
            shutil.rmtree(worktree)
        except OSError as exc:
            raise common.StateError(f"the review worktree {worktree} could not be removed: {exc}") from exc
    common.git(cfg.checkout, "worktree", "prune")


def reap(cfg: common.Config, job: str) -> None:
    for pr in harness.reap_reviews(lambda path: remove_review_worktree(cfg, path)):
        common.log(job, f"cleared the review of #{pr} that a dead run left behind (session and worktree)")


def tick(dry_run: bool = False) -> None:
    cfg = common.config()
    failed = []
    if not dry_run:
        reap(cfg, JOB)
    pulls = due(cfg)
    if not pulls:
        common.log(JOB, "nothing due for review")
    for p in pulls:
        if dry_run:
            common.log(JOB, f"#{p.number} would be reviewed at {p.head[:8]} on {reviewer_model(cfg, p)}")
            continue
        try:
            event, body, model = run_review(cfg, p)
            if post(cfg, p, event, body, model):
                common.log(JOB, f"#{p.number} reviewed at {p.head[:8]} on {model}: {event}")
        except LaneError as exc:
            common.log(JOB, f"#{p.number} FAILED — {exc}")
            failed.append(p.number)
    if failed:
        raise LaneError("no review was posted for " + ", ".join(f"#{n}" for n in failed))


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    harness.exit_on_signals()  # a killed run still kills its session and removes its worktree
    return common.run_job(JOB, lambda: tick(dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
