"""Drive one worker session for one card, then hand its commits to GitHub as the worker identity.

    .venv/bin/python scripts/worker.py --issue 794              # a new card
    .venv/bin/python scripts/worker.py --issue 794 --rework 812 # the reviewer requested changes on #812

The feeder starts this detached. It cuts (or continues) the card's worktree on `wt/issue-<n>`, writes the
brief, and runs the session. The pull request is opened as a draft, without auto-merge, as soon as the branch
has its first commit, so the worker can name its changelog fragment after it. When the session ends with commits
the branch is pushed again and — only if the run was clean — marked ready, then armed. A stalled, failed or dirty
run leaves it a draft and unarmed with a comment saying why; the orchestrator takes it from there. A rework
returns its pull request to draft before the session.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import re
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import cards, common, dispatch_card, github, harness, open_pr, secret_scan  # noqa: E402
from scripts.common import GitError, LaneError  # noqa: E402

COMMENT_LIMIT = 60000


def ref_exists(repo: pathlib.Path, ref: str) -> bool:
    """Whether a ref exists. git's 'no' (exit 1) is an answer; any other failure raises."""
    try:
        done = subprocess.run(common.git_argv(repo, "rev-parse", "--verify", "--quiet", ref),
                              capture_output=True, text=True, timeout=60, env=common.git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git could not check {ref} in {repo} ({exc.__class__.__name__})") from exc
    if done.returncode not in (0, 1):
        raise GitError(f"git could not check {ref} in {repo}: {done.stderr.strip()[:160]}")
    return done.returncode == 0


class WorktreeError(GitError):
    """The card's worktree cannot be cut or reused, and the reason — which worktree is in the way — is named."""


def worktrees(repo: pathlib.Path) -> dict:
    """branch -> worktree path for every worktree of `repo` that has a branch checked out. Stale registrations
    (their directory is gone) are pruned first, so a deleted worktree does not hold its branch."""
    common.git(repo, "worktree", "prune")
    found, path = {}, None
    for line in common.git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = pathlib.Path(line[len("worktree "):])
        elif line.startswith("branch refs/heads/") and path is not None:
            found[line[len("branch refs/heads/"):]] = path
    return found


def prepare_worktree(cfg: common.Config, number: int) -> pathlib.Path:
    """The card's worktree on `wt/issue-<n>`: reused, continued from the local or remote branch, or cut from base.

    A branch git would refuse to check out because another worktree holds it (the old lane kept its worktrees at
    `<checkout>/.worktrees/<card>`) is a named `WorktreeError` saying which one, never git's bare exit 128 (#4).
    All of it — the fetch, the prune, the add, the fast-forward — runs under the checkout lock, and a new branch
    is created with --no-track, so cutting a worktree never writes `.git/config`.
    """
    with common.checkout_lock():
        return _prepare_worktree(cfg, number)


def _prepare_worktree(cfg: common.Config, number: int) -> pathlib.Path:
    repo, branch = cfg.checkout, f"wt/issue-{number}"
    path = common.state_dir("worktrees") / f"issue-{number}"
    common.git(repo, "fetch", "-q", "origin")
    remote = ref_exists(repo, f"refs/remotes/origin/{branch}")
    if (path / ".git").exists():
        current = common.wt_git(cfg, path, "rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            raise WorktreeError(f"the worktree {path} is on {current!r}, not {branch!r}")
    elif path.exists():
        raise WorktreeError(f"{path} exists but is not a worktree — move it aside and the next run cuts the card")
    else:
        holder = worktrees(repo).get(branch)
        if holder is not None and holder.resolve() != path.resolve():
            raise WorktreeError(f"{branch!r} is already checked out in the worktree {holder}; remove that worktree "
                                f"(`git -C {repo} worktree remove {holder}`) or switch it to another branch, and "
                                "the next run continues the card here")
        if ref_exists(repo, f"refs/heads/{branch}"):
            common.git(repo, "worktree", "add", str(path), branch)
        elif remote:
            common.git(repo, "worktree", "add", "--no-track", "-b", branch, str(path), f"origin/{branch}")
        else:
            common.git(repo, "worktree", "add", "--no-track", "-b", branch, str(path), f"origin/{cfg.base}")
    if remote:
        reconcile(cfg, path, number)
    return path


def is_ancestor(repo: pathlib.Path, older: str, newer: str) -> bool:
    """Whether `older` is an ancestor of (or is) `newer`. git's 'no' (exit 1) is an answer; anything else raises."""
    try:
        done = subprocess.run(common.git_argv(repo, "merge-base", "--is-ancestor", older, newer),
                              capture_output=True, text=True, timeout=60, env=common.git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git could not compare {older[:9]} and {newer[:9]} ({exc.__class__.__name__})") from exc
    if done.returncode not in (0, 1):
        raise GitError(f"git could not compare {older[:9]} and {newer[:9]}: {done.stderr.strip()[:160]}")
    return done.returncode == 0


def reconcile(cfg: common.Config, path: pathlib.Path, number: int) -> str:
    """Bring the local `wt/issue-<n>` level with `origin/wt/issue-<n>` without rewriting either, so the lane's
    push (never forced) goes through. Returns what it did: '', 'fast-forward', 'superseded' or 'merged'.

    A session that amends or rebases a commit the lane already pushed (#819: the early open pushed `acf29de6`,
    the session amended it 24 seconds later) leaves the two diverged. When every commit only the remote has was
    once on this local branch (it is in the branch's reflog), the local branch is the rewrite of it: the remote
    is recorded as merged with the local tree kept (`-s ours`). Commits the branch never had — someone else's —
    are merged normally; a conflict there is a named failure, never a guess.
    """
    repo, branch = cfg.checkout, f"wt/issue-{number}"
    local = common.wt_git(cfg, path, "rev-parse", "HEAD")
    remote = common.git(repo, "rev-parse", f"refs/remotes/origin/{branch}")
    if is_ancestor(repo, remote, local):
        return ""
    if is_ancestor(repo, local, remote):
        common.wt_git(cfg, path, "merge", "--ff-only", "-q", remote)
        return "fast-forward"
    known = common.git(repo, "reflog", "show", "--format=%H", f"refs/heads/{branch}").split()
    job = f"worker-{number}"
    if any(is_ancestor(repo, remote, seen) for seen in known):
        common.wt_git(cfg, path, "merge", "-q", "-s", "ours", "--no-edit", "-m",
                      f"Keep the rewritten {branch}: it supersedes {remote[:9]}, which was pushed before the rewrite",
                      remote)
        common.log(job, f"{branch} was rewritten after {remote[:9]} was pushed — recorded it as merged, kept the "
                        f"local tree at {local[:9]}")
        return "superseded"
    try:
        common.wt_git(cfg, path, "merge", "-q", "--no-edit", remote)
    except GitError as exc:
        try:
            common.wt_git(cfg, path, "merge", "--abort")
        except GitError:
            pass
        raise WorktreeError(f"{branch} and origin/{branch} diverged and origin has commits this branch never had; "
                            f"merging them conflicts — a person resolves it ({str(exc)[:160]})") from exc
    common.log(job, f"{branch} had diverged from origin — merged origin's {remote[:9]} into {local[:9]}")
    return "merged"


def has_fragments(cfg: common.Config, repo: pathlib.Path) -> bool:
    """Whether the `changes/` fragment convention exists on the base branch."""
    return bool(common.git(repo, "ls-tree", "--name-only", f"origin/{cfg.base}", "--", "changes"))


LOG_TAIL_LINES = 60
LOG_TAIL_CHARS = 6000
MAX_FAILING_LOGS = 5


def log_tail(cfg: common.Config, job_id: int) -> str:
    """The last lines of a GitHub Actions job's log. Supporting material for a brief: a log that cannot be read
    is said so in the brief (the decision to rework was made on readable state), never left out silently."""
    if not job_id:
        return "(not a GitHub Actions job — no log to fetch; see the check's page)"
    try:
        raw = common.gh("api", f"repos/{cfg.slug}/actions/jobs/{int(job_id)}/logs", timeout=120)
    except LaneError as exc:
        return f"(the log could not be read: {exc})"
    lines = [re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", line) for line in raw.splitlines()]
    return "\n".join(lines[-LOG_TAIL_LINES:])[-LOG_TAIL_CHARS:]


def conflicts(cfg: common.Config, worktree: pathlib.Path) -> list[str]:
    """The files that conflict when origin/<base> is merged into the branch, without touching the worktree."""
    try:
        head = common.wt_git(cfg, worktree, "rev-parse", "HEAD")  # the worktree's HEAD, via the lane's git-dir
        done = subprocess.run(common.git_argv(cfg.checkout, "merge-tree", "--write-tree", "--name-only",
                                              "--no-messages", f"origin/{cfg.base}", head),
                              capture_output=True, text=True, timeout=120, env=common.git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git merge-tree could not be run in {worktree} ({exc.__class__.__name__})") from exc
    if done.returncode not in (0, 1):
        raise GitError(f"git merge-tree failed in {worktree}: {done.stderr.strip()[:160]}")
    return [line for line in done.stdout.splitlines()[1:] if line.strip()] if done.returncode == 1 else []


def rework_findings(cfg: common.Config, pr: int, worktree: pathlib.Path | None = None) -> str:
    return rework_brief(cfg, pr, worktree)[0]


def rework_brief(cfg: common.Config, pr: int, worktree: pathlib.Path | None = None) -> tuple[str, bool]:
    """Everything that sends this pull request back, read now: the reviewer's request for changes on the head,
    the failing checks with their log tails, the conflicting files, the merge queue's removal (#8), and for an
    orphaned draft what its last run reported. None of them is a refusal to rework.

    Returns (findings, resume): `resume` is True when the only reason is an orphaned draft — the run may then end
    with no new commit if the work is already complete (it still counts as an idle run)."""
    p = github.pull(cfg, pr)
    if p.state != "OPEN":
        raise LaneError(f"#{pr} is {p.state.lower()} — nothing to rework")
    required = github.required_checks(cfg)
    sections = []
    found = [r.body for r in p.reviews if r.author == cfg.reviewer_app and r.commit == p.head
             and r.state == "CHANGES_REQUESTED"]
    if found:
        sections.append("### The reviewer requested changes\n\n" + found[-1].strip())
    stuck = github.stuck_reason(p, required, cfg.reviewer_app, harness.reran())
    if stuck and stuck.startswith("required checks"):
        failing = [r for r in p.runs if r[1] not in github.PASSED and r[1]]
        failing.sort(key=lambda r: (r[0] in required, r[0]))  # the jobs first, the required roll-up last
        parts = [f"Required checks failing on `{p.head[:9]}`: "
                 + ", ".join(sorted(n for n in required if github.check_state(
                     dataclasses.replace(p, checks=tuple(c for c in p.checks if c[0] == n)), frozenset({n})) == "failing"))
                 + ". Failing check runs: " + ", ".join(f"`{r[0]}` ({r[1].lower()})" for r in failing) + "."]
        for name, conclusion, job, url, *_ in failing[:MAX_FAILING_LOGS]:
            parts.append(f"#### `{name}` — {conclusion.lower()}\n{url}\n\n```\n{log_tail(cfg, job)}\n```")
        if stuck != "required checks failing":
            parts.insert(0, "They were cancelled or timed out, were rerun once on this head, and failed the same "
                            "way again: look for what makes them hang or run out of time (a test that never ends, "
                            "a step that waits on input), not only for a failing assertion.")
        sections.append("### Checks are failing\n\n" + "\n\n".join(parts))
    elif stuck == "conflicts with the base branch":
        files = conflicts(cfg, worktree) if worktree is not None else []
        sections.append(f"### The branch conflicts with `{cfg.base}`\n\n"
                        + ("Conflicting files when `origin/" + cfg.base + "` is merged in:\n"
                           + "\n".join(f"- `{f}`" for f in files) if files else
                           "GitHub reports a conflict; merge `origin/" + cfg.base + "` to see it."))
    elif stuck:
        sections.append(f"### It is {stuck}\n\nThe merge queue tests the branch together with what lands before "
                        f"it. Merge `origin/{cfg.base}`, find what breaks in combination, and fix it.")
    resume = False
    if p.draft:
        resume = not sections
        report = github.worker_report(cfg, pr)
        incomplete = [line.strip() for line in report.splitlines() if line.strip().startswith("**Incomplete")]
        sections.append(
            "### The last run did not finish\n\nThis pull request was left a draft by a worker run that did not "
            "finish cleanly, and nobody has worked on it since. That run reported:\n\n> "
            + (incomplete[-1] if incomplete else "(no reason on record — read the branch and the card)")
            + "\n\nPick the card up where it stopped. If the work on the branch is already complete, verify it — "
            "the named tests, `openspec validate --all --strict`, the acceptance checks — and end with your report: "
            "no new commit is needed then, and the lane marks the pull request ready.")
    if not sections:
        raise LaneError(f"#{pr} has no request for changes on its head {p.head[:8]}, is not stuck and is not a "
                        "draft — nothing to rework")
    return "\n\n".join(sections), resume


def brief(cfg: common.Config, issue: dict, worktree: pathlib.Path, pr: int | None, findings: str,
          fragments: bool, decisions: str = "") -> str:
    number = issue["number"]
    if pr is not None:
        pr_line = f"It is #{pr}."
        fragment_name = f"`changes/{pr}.md`"
    else:
        pr_line = ("The lane opens it after your first commit and writes its number to the file named by "
                   "`$LANE_PR_FILE` (`cat \"$LANE_PR_FILE\"`). Commit a first slice early, then read it.")
        fragment_name = "`changes/<the pull request number>.md`"
    if github.is_release(f"wt/issue-{number}", issue["title"]):
        fragment = ("This is a release card (its title starts with `chore(release)`): you may edit `CHANGELOG.md` — "
                    "the release collects the `changes/` fragments into it — and this pull request needs no "
                    "`changes/<n>.md` fragment of its own.")
    elif fragments:
        fragment = (f"Never edit `CHANGELOG.md`. Write the change's line(s) to {fragment_name}, under the release's "
                    "own headings (look at the existing files in `changes/`), and commit it before the archive.")
    else:
        fragment = "The `changes/` fragment convention is not on main yet: do not edit `CHANGELOG.md`, add nothing."
    rework = ""
    if findings:
        rework = ("\n## This is a rework\n\nThis pull request went back to a worker for what follows. Address all of "
                  "it on this same branch, then finish as below (the archive stays the last commit — redo it if your "
                  f"fixes land after it).\n\n**Bring the branch up to date by merging, never by rebasing:** "
                  f"`git merge origin/{cfg.base}` (the lane has fetched it), resolve every conflict in the files, and "
                  "commit the merge. Never `git rebase`, never rewrite a pushed commit — the lane pushes without "
                  "force, so rewritten history is refused. A conflict in `openspec/specs/**` is resolved by reverting "
                  "the archive commit, merging, and re-archiving, never by hand. Then run the named tests again.\n\n"
                  + findings.strip() + "\n")
    return common.render("worker-brief.md", NUMBER=str(number), REPO=cfg.slug, TITLE=issue["title"],
                         WORKTREE=str(worktree), BRANCH=f"wt/issue-{number}", BASE=cfg.base, PR_LINE=pr_line,
                         FRAGMENT_RULE=fragment, REWORK=rework, DECISIONS=decisions,
                         BODY=(issue.get("body") or "").strip())


def problems(cfg: common.Config, outcome: harness.Outcome, worktree: pathlib.Path, start_head: str,
             resume: bool = False) -> list[str]:
    """Why a finished session is not clean. rc=0 only says the process exited. A `resume` of an orphaned draft
    may end with no new commit: its work can be complete already."""
    found = []
    if outcome.stalled:
        found.append(f"the session stalled or passed its time cap and was stopped after {outcome.elapsed}s")
    elif outcome.rc != 0:
        found.append(f"the session exited with {outcome.rc}")
    if outcome.leaked_markup:
        found.append("the session's last message is tool-call markup, not a report")
    if common.wt_git(cfg, worktree, "status", "--porcelain"):
        found.append("the session left uncommitted changes in the worktree")
    if not resume and common.wt_git(cfg, worktree, "rev-parse", "HEAD") == start_head:
        found.append("the session made no new commit")
    return found


def drive(cfg: common.Config, number: int, rework_pr: int | None) -> int:
    job = f"worker-{number}"
    worktree = prepare_worktree(cfg, number)
    issue = dispatch_card.card(cfg.checkout, number)
    findings, resume = rework_brief(cfg, rework_pr, worktree) if rework_pr else ("", False)
    if rework_pr:
        # back to draft before the session: CI's heavy legs and the reviewer wait for the clean finish (#5)
        open_pr.return_to_draft(f"wt/issue-{number}", rework_pr)
        rounds = harness.rework_rounds(number, rework_pr)  # counted before this run started (feed, main)
        common.log(job, f"#{rework_pr} is a draft again for rework round {rounds} of {harness.REWORK_LIMIT}, "
                        "auto-merge off")
    scratch = common.state_dir("scratch", f"issue-{number}")
    pr_file = scratch / "pr-number"
    try:
        pr_file.unlink(missing_ok=True)
        if rework_pr:
            pr_file.write_text(str(rework_pr))
    except OSError as exc:
        raise common.StateError(f"the pull request number file {pr_file} could not be written: {exc}") from exc
    text = brief(cfg, issue, worktree, rework_pr, findings, has_fragments(cfg, cfg.checkout),
                 github.decisions_section(github.decisions_for(cfg, number)))
    start_head = common.wt_git(cfg, worktree, "rev-parse", "HEAD")
    opened: dict = {"pr": rework_pr, "tried": False}

    def open_early() -> None:
        if opened["pr"] is not None or opened["tried"]:
            return
        try:
            if common.wt_git(cfg, worktree, "rev-parse", "HEAD") == start_head:
                return
            opened["tried"] = True  # one attempt; the end of the session tries again and fails loudly
            opened["pr"] = dispatch_card.dispatch(cfg.checkout, number, arm=False,
                                                  known=(harness.read_credential(cfg),))["number"]
            try:
                pr_file.write_text(str(opened["pr"]))
            except OSError as exc:
                raise common.StateError(f"#{opened['pr']} opened, but {pr_file} could not be written: {exc}") from exc
            common.log(job, f"opened #{opened['pr']} at the first commit, auto-merge not armed yet")
        except LaneError as exc:
            common.log(job, f"could not open the pull request at the first commit — {exc}")

    session_log = common.state_dir("sessions") / f"issue-{number}-{os.getpid()}.jsonl"
    common.log(job, f"session on {cfg.worker_model}" + (f", rework of #{rework_pr}" if rework_pr else ""))
    driver = os.getpid()
    outcome = harness.run_session(cfg, model=cfg.worker_model, worktree=worktree, prompt=text, scratch=scratch,
                                  log_path=session_log, extra_env={"LANE_PR_FILE": str(pr_file)}, on_tick=open_early,
                                  branch=f"wt/issue-{number}",
                                  on_start=lambda pgid, sid: harness.note_session(number, driver, pgid, sid))
    return finish(cfg, number, worktree, outcome, start_head, session_log, resume)


def person_reason(cfg: common.Config, report: str) -> str | None:
    """The worker's `NEEDS-PERSON: <reason>`, if its final message has one, with anything secret-shaped in it
    redacted, as in the report itself."""
    reason = github.needs_person(report)
    if reason is None:
        return None
    return secret_scan.redact(reason, (harness.read_credential(cfg),))[0]


def finish(cfg: common.Config, number: int, worktree: pathlib.Path, outcome: harness.Outcome, start_head: str,
           session_log: pathlib.Path, resume: bool = False) -> int:
    job = f"worker-{number}"
    found = problems(cfg, outcome, worktree, start_head, resume)
    try:  # read before the push, for its NEEDS-PERSON; an unreadable log never holds the commits back
        report = harness.last_text(session_log).strip() or "(the session left no final message)"
    except harness.HarnessError as exc:
        report = f"(the session's final message could not be read: {exc})"
    person = person_reason(cfg, report)
    if person is not None:  # the worker stopped for a person: never armed, and the card parks at once
        found.append(f"the worker says it needs a person: {person}")
    idle, parked_now = harness.note_run(number, common.wt_git(cfg, worktree, "rev-parse", "HEAD") != start_head)
    if idle:
        common.log(job, f"no new commit — idle run {idle} of {harness.IDLE_RUN_LIMIT}")
    if dispatch_card._count(cfg.checkout, f"wt/issue-{number}") == 0:
        common.log(job, "the session ended with no commits — no pull request. " + "; ".join(found))
        if person is not None:
            park_for_person(cfg, number, number, person)
        elif parked_now:
            park(cfg, number, number, "idle", idle)
        return 1
    not_ready = ""
    # a slice started before its predecessor: pushed and marked ready like any clean finish, but never armed while
    # its card waits on an open issue — the feeder re-arms it once it is approved after they close
    blockers = github.open_blockers(cfg, number)
    if blockers:
        common.log(job, f"card #{number} {github.waits_line(blockers)} — the pull request is left unarmed")
    with common.checkout_lock():  # the session may have rewritten a commit the early open already pushed
        common.git(cfg.checkout, "fetch", "-q", "origin")
        if ref_exists(cfg.checkout, f"refs/remotes/origin/wt/issue-{number}"):
            reconcile(cfg, worktree, number)
    try:
        result = dispatch_card.dispatch(cfg.checkout, number, arm=not found and not blockers,
                                        known=(harness.read_credential(cfg),))
    except open_pr.ReadyRunError as exc:
        # pushed and marked ready, but CI never ran it as ready: left unarmed, and parked by name below
        not_ready = str(exc)
        common.log(job, f"#{exc.number} — {not_ready}")
        found.append(not_ready)
        result = {"number": exc.number, "auto_merge": "not armed"}
    except open_pr.SecretFound as exc:
        # nothing was pushed; the card stops here until a person looks (#12). The kind, never the value.
        common.log(job, f"secret scan: {exc}")
        if harness.park(number, "secret"):
            park(cfg, number, rework_pr_of(cfg, number) or number, "secret",
                 detail=f"the secret scan before the push found something shaped like a {exc.kind} in the "
                        f"{exc.where}; nothing was pushed")
        return 1
    head = common.wt_git(cfg, worktree, "rev-parse", "HEAD")[:9]
    lines = [f"**Worker session** on `{cfg.worker_model}` at `{head}`, {outcome.elapsed // 60} min."]
    if not_ready:
        lines.append(f"**Marked ready, but not armed:** {not_ready}.")
    elif found:
        lines.append(f"**Incomplete — the pull request stays a draft and auto-merge is {result['auto_merge']}:** "
                     + "; ".join(found) + ".")
    report, kinds = secret_scan.redact(report, (harness.read_credential(cfg),))
    for kind in kinds:  # each span is replaced, the rest is posted; the full message stays in the local log
        common.log(job, f"the session's final message holds something shaped like a {kind} — redacted before "
                        f"posting; the full message stays in {session_log}")
    lines.append("\n" + report)
    open_pr.comment(result["number"], "\n".join(lines)[:COMMENT_LIMIT])
    method = (f" (queue {result['queue_method']}, GitHub records {result.get('merge_method') or 'none'})"
              if result.get("queue_method") else "")
    common.log(job, f"#{result['number']} pushed at {head}, auto-merge {result['auto_merge']}{method}"
               + (f" — {'; '.join(found)}" if found else ""))
    if person is not None:
        park_for_person(cfg, number, result["number"], person)
    elif not_ready:
        if harness.park(number, "ready", not_ready):
            park(cfg, number, result["number"], "ready", detail=not_ready)
    elif parked_now:
        park(cfg, number, result["number"], "idle", idle)
    return 1 if found else 0


def park_for_person(cfg: common.Config, card: int, target: int, reason: str) -> None:
    """The worker wrote NEEDS-PERSON: park the card now, with its reason, whatever the counters say."""
    if harness.park(card, "person", reason):
        park(cfg, card, target, "person", detail=f"the worker says it needs a person: {reason}")


PARK_WHY = {
    "idle": "{n} worker runs in a row ended with no new commit (cap: {cap})",
    "reworks": "it went back to a worker {n} times (cap: {cap}) and is still not ready",
}


PARK_ADVICE = {
    "default": "split or clarify the card, or push a fix",
    "person": "make the decision or supply what the reason names, on the card or the pull request",
    "ready": "the pull request is ready with auto-merge off; convert it to a draft and mark it ready again, and "
             "check that CI starts a run that sees it as ready",
}


def rework_pr_of(cfg: common.Config, card: int) -> int | None:
    """The card's open pull request, from the worker record or the pr-number file the run wrote, if any."""
    path = cfg.state / "scratch" / f"issue-{card}" / "pr-number"
    try:
        text = path.read_text().strip() if path.exists() else ""
    except OSError as exc:
        raise common.StateError(f"{path} could not be read: {exc}") from exc
    return int(text) if text.isdigit() else None


def park(cfg: common.Config, card: int, target: int, why: str, n: int = 0, detail: str = "") -> None:
    """Say, once, on the pull request (or the issue when there is none), that the lane stops working this card."""
    cap = harness.IDLE_RUN_LIMIT if why == "idle" else harness.REWORK_LIMIT
    reason = PARK_WHY[why].format(n=n, cap=cap) if why in PARK_WHY else detail
    open_pr.comment(target, (
        f"**Lane: card #{card} is parked.** {reason[:1].upper() + reason[1:].rstrip('.')}. The feeder will not start "
        f"or rework it again. It needs a person: {PARK_ADVICE.get(why, PARK_ADVICE['default'])}. To let the lane "
        f"pick it up again, run `scripts/lane.py unpark {card} --reason '...'` on the lane host."))
    label = {"idle": f"after {n} idle runs", "reworks": f"after {n} rework rounds"}.get(why, f"({why})")
    common.log(f"worker-{card}", f"parked {label} — commented on #{target}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--rework", type=int, help="the pull request whose requested changes to address")
    args = parser.parse_args()
    job, pid = f"worker-{args.issue}", os.getpid()
    harness.exit_on_signals()
    try:
        cfg = common.config()
        live = harness.live_pid(args.issue)
        if live not in (None, pid):
            common.log(job, f"worker {live} is already running this card — leaving")
            return 1
        if live is None:  # started by hand rather than by the feeder, which records the worker itself
            harness.note_attempt(args.issue)  # the feeder counts its own launches; a hand-started one counts here
            if args.rework:
                harness.note_rework(args.issue, args.rework)
            labels = [label["name"] for label in dispatch_card.card(cfg.checkout, args.issue).get("labels") or []]
            harness.record_worker(args.issue, pid, cards.areas_of(labels, cfg.area_prefix), args.rework)
        try:
            return drive(cfg, args.issue, args.rework)
        finally:
            harness.clear_worker(args.issue, pid)
    except LaneError as exc:
        common.log(job, f"FAILED — {exc}")
        try:
            harness.note_death(args.issue, str(exc))
        except LaneError as also:
            common.log(job, f"FAILED — its death could not be recorded: {also}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
