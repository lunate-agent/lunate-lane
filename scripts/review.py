"""Cron job: review every worker pull request that is green and not yet reviewed at its head.

    .venv/bin/python scripts/review.py            # review everything due, one at a time
    .venv/bin/python scripts/review.py --dry-run  # say what is due and why the rest is skipped

The review runs on the reviewer model (which the config keeps different from the worker's), in a detached
worktree at the head, with the diff and the issue in its brief. It writes its review as Markdown and hands it in
by running `scripts/verdict.py` once, which writes the JSON verdict atomically and refuses a second one (logged
here); a verdict the command did not write is not used. The lane re-reads the
pull request and, only if it is still open at the same head, posts the verdict as a real GitHub review —
APPROVE or REQUEST_CHANGES — as the `lunate-reviewer` identity, then reads the review back and fails the run if
GitHub does not name `lunate-reviewer` as its author. A missing or malformed verdict posts nothing.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import shutil
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import common, dispatch_card, github, harness, identity, verdict  # noqa: E402
from scripts.common import LaneError  # noqa: E402

JOB = "review"
EVENTS = verdict.EVENTS


class ReviewError(LaneError):
    """The review could not be run, or produced no usable verdict."""


def skip_reason(p: github.Pull, required: frozenset, running: dict, reviewer: str, others=(),
                rereview: bool = False, parked=frozenset()) -> str | None:
    """Why this pull request is not reviewed now, or None if it is due. `others` are the open pull requests (any
    author, with their files): one that is older, ready and not parked (`parked`: the parked cards) and touches
    the same OpenSpec capability holds this one back.
    A `rereview` (`lane.py rereview`) reviews a head the bot already reviewed; every other rule holds."""
    if p.state != "OPEN":
        return f"is {p.state.lower()}"
    if p.draft:
        return "is a draft"
    if p.waits_on:  # a slice started before its predecessor: reviewed once the predecessor has landed
        return github.waits_line(p.waits_on)
    if p.card in running:
        return f"its worker on #{p.card} is still running"
    if p.card is None:
        return "carries no card to review against"
    if p.mergeable == "CONFLICTING":
        return "conflicts with the base branch — it goes back to a worker first"
    checks = github.check_state(p, required)
    if checks != "green":
        return f"required checks are {checks}"
    if not rereview and github.reviewer_verdict(p, reviewer) is not None:
        return f"already reviewed at {p.head[:8]}"
    person = github.person_approved(p, reviewer)
    if person:  # the bot's review was dismissed and a person approved: reviewing again would overrule them
        return f"@{person} approved it at {p.head[:8]}"
    overlap = github.spec_overlap(p, [o for o in others if o.number != p.number], parked)
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
    parked = harness.parked_cards()  # a parked pull request holds no spec overlap back
    found = []
    pulls = github.open_pulls(cfg)
    for summary in pulls:
        if summary.author != cfg.worker_app:
            continue
        p = github.pull(cfg, summary.number)
        reason = skip_reason(p, required, running, cfg.reviewer_app, pulls, parked=parked)
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
    if not isinstance(data, dict) or data.get("written_by") != verdict.WRITTEN_BY:
        raise ReviewError(f"the verdict {path} was not written by {verdict.WRITTEN_BY} — the reviewer wrote it by hand")
    if data.get("event") not in EVENTS:
        raise ReviewError(f"the verdict's event must be one of {', '.join(EVENTS)}")
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ReviewError("the verdict has no body")
    if data["event"] == "APPROVE" and github.needs_person(body) is not None:
        return "REQUEST_CHANGES", body.strip()  # a check nobody can evidence is not an approval
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


SUPERSEDED = "superseded by re-review under the calibrated brief"


def dismiss_standing(cfg: common.Config, number: int, head: str, token: str) -> list[int]:
    """Dismiss the reviewer identity's approvals and requests for changes on `head`, as that identity. Returns the
    dismissed review ids. A listing that may be cut off is refused."""
    reviews = common.rest(token, "GET", f"/repos/{cfg.slug}/pulls/{int(number)}/reviews?per_page=100")
    if not isinstance(reviews, list):
        raise ReviewError(f"#{number}'s reviews came back as something other than a list")
    if len(reviews) >= 100:
        raise ReviewError(f"#{number} has 100 or more reviews — refusing to dismiss on part of them")
    standing = [r["id"] for r in reviews if github.login(r.get("user")) == cfg.reviewer_app
                and r.get("commit_id") == head and r.get("state") in ("APPROVED", "CHANGES_REQUESTED")]
    for review_id in standing:
        common.rest(token, "PUT", f"/repos/{cfg.slug}/pulls/{int(number)}/reviews/{review_id}/dismissals",
                    {"message": SUPERSEDED, "event": "DISMISS"})
    return standing


def rereview(cfg: common.Config, number: int, actor: str = "") -> tuple[str, str]:
    """Review a ready, green worker pull request again at its current head under the current brief, even if the bot
    reviewed that head already: run the session, then dismiss the bot's standing review on the head ("superseded
    by re-review under the calibrated brief") and post the new one. Returns (event, body), or ('SKIPPED', why).
    The caller holds the review job's lock."""
    required = github.required_checks(cfg)
    others = github.open_pulls(cfg)
    p = github.pull(cfg, number)
    if p.author != cfg.worker_app:
        return "SKIPPED", f"is not a {cfg.worker_app} pull request"
    reason = skip_reason(p, required, harness.running_workers(), cfg.reviewer_app, others, rereview=True,
                         parked=harness.parked_cards())
    if reason:
        common.log(JOB, f"#{p.number} not re-reviewed — {reason}")
        return "SKIPPED", reason
    event, body, model = run_review(cfg, p)
    now = github.pull(cfg, p.number)
    if now.state != "OPEN" or now.head != p.head:
        common.log(JOB, f"#{p.number} moved or closed during the re-review — nothing dismissed or posted")
        return "SKIPPED", "moved or closed during the re-review"
    try:
        token = identity.installation_token("reviewer")
    except identity.IdentityError as exc:
        raise ReviewError(f"the reviewer identity could not mint a token: {exc}") from exc
    dismissed = dismiss_standing(cfg, p.number, p.head, token)
    if not post(cfg, p, event, body, model):
        return "SKIPPED", "not posted"
    by = f" by {actor}" if actor else ""
    common.log(JOB, f"#{p.number} re-reviewed at {p.head[:8]} on {model}{by}: {event}"
                    + (f" (dismissed {', '.join(map(str, dismissed))})" if dismissed else ""))
    return event, body


# the system interpreter: the lane's own venv python links into a directory the reviewer's sandbox may not read
# (`scripts/verdict.py` needs only the standard library)
VERDICT_PYTHON = "/usr/bin/python3"


DECISION_ENTRY = re.compile(r"^\s*[-*]\s*\**\d{4}-\d{2}-\d{2}\b")


def card_decisions(body: str) -> str:
    """The card's own `## Decisions` entries (dated list items), verbatim, for the verdict section of the brief —
    where a finding is weighed against them — or '' when the card has none. #809's 2026-11-14 entry kept #848's ADR
    renumbering in scope; the review had flagged it as a scope violation."""
    lines, inside, entries = (body or "").splitlines(), False, []
    for line in lines:
        if line.startswith("## "):
            inside = line.strip().lower() == "## decisions"
            continue
        if inside and line.strip():
            if DECISION_ENTRY.match(line) or (entries and line.startswith((" ", "\t"))):
                entries.append(line.rstrip())
    if not entries:
        return ""
    return ("The card's dated Decisions, verbatim — binding over any finding that contradicts them:\n\n"
            + "\n".join(entries) + "\n")


def verdict_command(verdict_file: pathlib.Path, body_file: pathlib.Path) -> str:
    """The command the reviewer runs once to hand in its verdict (it adds `--event`): the system python and
    `scripts/verdict.py`, which only reads the body and writes inside the reviewer's scratch directory."""
    script = pathlib.Path(verdict.__file__).resolve()
    python = VERDICT_PYTHON if os.path.exists(VERDICT_PYTHON) else sys.executable
    return " ".join(shlex.quote(part) for part in (python, "-B", str(script), "--out", str(verdict_file),
                                                   "--body-file", str(body_file)))


def log_refused(number: int, refused: pathlib.Path) -> None:
    """Each second verdict the command refused during the session: an error in the log, the first verdict stands."""
    try:
        lines = refused.read_text(encoding="utf-8").splitlines() if refused.exists() else []
    except OSError as exc:
        common.log(JOB, f"#{number} FAILED — the refused-verdict record {refused} could not be read: {exc}")
        return
    for line in lines:
        try:
            attempt = json.loads(line)
        except ValueError:
            attempt = {"at": "?", "event": line[:40]}
        common.log(JOB, f"#{number} ERROR — the reviewer tried to write a second verdict ({attempt.get('event')}, "
                        f"{attempt.get('at')}): refused, the first verdict stands")


def run_review(cfg: common.Config, p: github.Pull) -> tuple[str, str, str]:
    """Run the reviewer session at the head and return its verdict and the model it ran on."""
    model = reviewer_model(cfg, p)
    repo, worktree = cfg.checkout, common.state_dir("reviews") / f"pr-{p.number}"
    scratch = common.state_dir("scratch", f"review-{p.number}")
    verdict_file, diff_file = scratch / "verdict.json", scratch / "diff.patch"
    body_file, refused = scratch / "review.md", verdict.refused_path(verdict_file)
    try:
        for old in (verdict_file, body_file, refused):
            old.unlink(missing_ok=True)
    except OSError as exc:
        raise ReviewError(f"the old verdict in {scratch} could not be removed: {exc}") from exc
    driver = os.getpid()
    with common.checkout_lock():  # fetch and worktree add write the shared .git
        common.git(repo, "fetch", "-q", "origin", p.head)
        if worktree.exists() or worktree.is_symlink():
            remove_review_worktree(cfg, p.number)
        harness.record_review(p.number, driver, worktree)  # so a SIGKILLed run's session and worktree get cleared
        common.git(repo, "worktree", "add", "--detach", str(worktree), p.head)
    try:
        try:
            diff_file.write_text(common.git(repo, "diff", "--no-textconv", "--no-ext-diff",
                                            f"origin/{cfg.base}...{p.head}"))
        except OSError as exc:
            raise ReviewError(f"the diff {diff_file} could not be written: {exc}") from exc
        issue = dispatch_card.card(repo, p.card)
        fragments = fragment_check(p, bool(common.git(repo, "ls-tree", "--name-only", f"origin/{cfg.base}", "--",
                                                      "changes")))
        others = github.open_pulls(cfg)
        report = github.worker_report(cfg, p.number) or "(the worker posted no session report on this pull request)"
        prompt = common.render("review-brief.md", PR=str(p.number), REPO=cfg.slug, TITLE=p.title, HEAD=p.head[:9],
                               WORKTREE=str(worktree), BASE=cfg.base, DIFF_FILE=str(diff_file),
                               FRAGMENT_CHECK=fragments, BODY_FILE=str(body_file),
                               VERDICT_CMD=verdict_command(verdict_file, body_file), CARD=str(p.card),
                               SPEC_NEIGHBOURS=spec_neighbours(p, others), WORKER_REPORT=report,
                               SCOPE=scope_context(p, cfg.area_prefix),
                               DECISIONS=github.decisions_section(github.decisions_for(cfg, p.card)),
                               CARD_DECISIONS=card_decisions(issue.get("body") or ""),
                               BODY=(issue.get("body") or "").strip())
        log_path = common.state_dir("sessions") / f"review-{p.number}-{p.head[:8]}.jsonl"
        outcome = harness.run_session(cfg, model=model, worktree=worktree, prompt=prompt,
                                      scratch=scratch, log_path=log_path,
                                      on_start=lambda pgid, sid: harness.note_review_session(p.number, driver, pgid, sid))
        log_refused(p.number, refused)
        if outcome.stalled or outcome.rc != 0:
            raise ReviewError(f"the review session did not finish cleanly (rc {outcome.rc}, stalled {outcome.stalled})")
        return (*read_verdict(verdict_file), model)
    finally:
        remove_review_worktree(cfg, p.number)
        harness.clear_review(p.number, driver)


RELEASE_CHECK = ("this is a release pull request (a `release/<version>` branch or a `chore(release)` title): it may "
                 "edit `CHANGELOG.md` and needs no `changes/<n>.md` fragment. Check that the CHANGELOG entries match "
                 "the fragments it collects.")


def fragment_check(p: github.Pull, fragments: bool) -> str:
    """The changelog rule for the review brief: a release pull request is the one that may edit CHANGELOG.md."""
    if github.is_release(p.branch, p.title):
        return RELEASE_CHECK
    if fragments:
        return "never edit `CHANGELOG.md`; the PR must add `changes/" + str(p.number) + ".md`."
    return "the `changes/` convention is not on main yet; `CHANGELOG.md` must not be edited."


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


def scope_context(p: github.Pull, area_prefix: str) -> str:
    """For the brief's scope check: the card's `area/*` labels. Files other open pull requests also change are not
    listed: overlap is never blocking (a merge conflict goes back to a worker as a rework)."""
    areas = sorted(label for label in p.labels if label.startswith(area_prefix))
    return (f"The card's area: {', '.join(f'`{a}`' for a in areas)}." if areas
            else "The card has no area label: its area is what its own text names.")


def remove_review_worktree(cfg: common.Config, pr: int) -> None:
    """Remove the review worktree of `pr`, and prune its registration. The path is derived from the number and
    resolved; a symlink or anything outside <state>/reviews is refused.

    No --force (review of #17, item 4): a clean worktree goes through `git worktree remove`. One the reviewer's
    build left untracked files in is deleted as a directory instead — it is detached at a head that is already on
    GitHub and holds nothing the lane keeps.
    """
    with common.checkout_lock():
        _remove_review_worktree(cfg, pr)


def _remove_review_worktree(cfg: common.Config, pr: int) -> None:
    worktree = common.inside_state(common.state_dir("reviews") / f"pr-{int(pr)}", "reviews")
    if (worktree / ".git").exists():
        try:
            common.git(cfg.checkout, "worktree", "remove", str(worktree))
        except common.GitError:
            pass  # untracked build output: removed as a directory below
    if worktree.exists():
        try:
            shutil.rmtree(worktree)
        except OSError as exc:
            raise common.StateError(f"the review worktree {worktree} could not be removed: {exc}") from exc
    common.git(cfg.checkout, "worktree", "prune")


def reap(cfg: common.Config, job: str) -> None:
    for pr in harness.reap_reviews(lambda number: remove_review_worktree(cfg, number)):
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
