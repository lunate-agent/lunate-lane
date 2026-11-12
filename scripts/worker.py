"""Drive one worker session for one card, then hand its commits to GitHub as the worker identity.

    .venv/bin/python scripts/worker.py --issue 794              # a new card
    .venv/bin/python scripts/worker.py --issue 794 --rework 812 # the reviewer requested changes on #812

The feeder starts this detached. It cuts (or continues) the card's worktree on `wt/issue-<n>`, writes the
brief, and runs the session. The pull request is opened, without auto-merge, as soon as the branch has its
first commit, so the worker can name its changelog fragment after it. When the session ends with commits the
branch is pushed again and auto-merge is armed — only if the run was clean. A stalled, failed or dirty run
leaves the pull request unarmed with a comment saying why; the orchestrator takes it from there.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import cards, common, dispatch_card, github, harness, open_pr  # noqa: E402
from scripts.common import GitError, LaneError  # noqa: E402

COMMENT_LIMIT = 60000


def ref_exists(repo: pathlib.Path, ref: str) -> bool:
    """Whether a ref exists. git's 'no' (exit 1) is an answer; any other failure raises."""
    try:
        done = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git could not check {ref} in {repo} ({exc.__class__.__name__})") from exc
    if done.returncode not in (0, 1):
        raise GitError(f"git could not check {ref} in {repo}: {done.stderr.strip()[:160]}")
    return done.returncode == 0


def prepare_worktree(cfg: common.Config, number: int) -> pathlib.Path:
    """The card's worktree on `wt/issue-<n>`: reused, continued from the local or remote branch, or cut from base."""
    repo, branch = cfg.checkout, f"wt/issue-{number}"
    path = common.state_dir("worktrees") / f"issue-{number}"
    common.git(repo, "fetch", "-q", "origin")
    remote = ref_exists(repo, f"refs/remotes/origin/{branch}")
    if (path / ".git").exists():
        current = common.git(path, "rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            raise GitError(f"the worktree {path} is on {current!r}, not {branch!r}")
    elif ref_exists(repo, f"refs/heads/{branch}"):
        common.git(repo, "worktree", "add", str(path), branch)
    elif remote:
        common.git(repo, "worktree", "add", "-b", branch, str(path), f"origin/{branch}")
    else:
        common.git(repo, "worktree", "add", "-b", branch, str(path), f"origin/{cfg.base}")
    if remote:
        common.git(path, "merge", "--ff-only", "-q", f"origin/{branch}")
    return path


def has_fragments(cfg: common.Config, repo: pathlib.Path) -> bool:
    """Whether the `changes/` fragment convention exists on the base branch."""
    return bool(common.git(repo, "ls-tree", "--name-only", f"origin/{cfg.base}", "--", "changes"))


def rework_findings(cfg: common.Config, pr: int) -> str:
    """The reviewer's request for changes on the pull request's current head. None is a refusal to rework."""
    p = github.pull(cfg, pr)
    if p.state != "OPEN":
        raise LaneError(f"#{pr} is {p.state.lower()} — nothing to rework")
    found = [r.body for r in p.reviews if r.author == cfg.reviewer_app and r.commit == p.head
             and r.state == "CHANGES_REQUESTED"]
    if not found:
        raise LaneError(f"#{pr} has no request for changes on its head {p.head[:8]} — nothing to rework")
    return found[-1]


def brief(cfg: common.Config, issue: dict, worktree: pathlib.Path, pr: int | None, findings: str,
          fragments: bool) -> str:
    number = issue["number"]
    if pr is not None:
        pr_line = f"It is #{pr}."
        fragment_name = f"`changes/{pr}.md`"
    else:
        pr_line = ("The lane opens it after your first commit and writes its number to the file named by "
                   "`$LANE_PR_FILE` (`cat \"$LANE_PR_FILE\"`). Commit a first slice early, then read it.")
        fragment_name = "`changes/<the pull request number>.md`"
    if fragments:
        fragment = (f"Never edit `CHANGELOG.md`. Write the change's line(s) to {fragment_name}, under the release's "
                    "own headings (look at the existing files in `changes/`), and commit it before the archive.")
    else:
        fragment = "The `changes/` fragment convention is not on main yet: do not edit `CHANGELOG.md`, add nothing."
    rework = ""
    if findings:
        rework = ("\n## This is a rework\n\nThe reviewer requested changes on this pull request. Address every "
                  "finding on this same branch, then finish as below (the archive stays the last commit — "
                  "redo it if your fixes land after it).\n\n" + findings.strip() + "\n")
    return common.render("worker-brief.md", NUMBER=str(number), REPO=cfg.slug, TITLE=issue["title"],
                         WORKTREE=str(worktree), BRANCH=f"wt/issue-{number}", BASE=cfg.base, PR_LINE=pr_line,
                         FRAGMENT_RULE=fragment, REWORK=rework, BODY=(issue.get("body") or "").strip())


def problems(outcome: harness.Outcome, worktree: pathlib.Path, start_head: str) -> list[str]:
    """Why a finished session is not clean. rc=0 only says the process exited."""
    found = []
    if outcome.stalled:
        found.append(f"the session stalled or passed its time cap and was stopped after {outcome.elapsed}s")
    elif outcome.rc != 0:
        found.append(f"the session exited with {outcome.rc}")
    if outcome.leaked_markup:
        found.append("the session's last message is tool-call markup, not a report")
    if common.git(worktree, "status", "--porcelain"):
        found.append("the session left uncommitted changes in the worktree")
    if common.git(worktree, "rev-parse", "HEAD") == start_head:
        found.append("the session made no new commit")
    return found


def drive(cfg: common.Config, number: int, rework_pr: int | None) -> int:
    job = f"worker-{number}"
    worktree = prepare_worktree(cfg, number)
    issue = dispatch_card.card(worktree, number)
    findings = rework_findings(cfg, rework_pr) if rework_pr else ""
    scratch = common.state_dir("scratch", f"issue-{number}")
    pr_file = scratch / "pr-number"
    pr_file.unlink(missing_ok=True)
    if rework_pr:
        pr_file.write_text(str(rework_pr))
    text = brief(cfg, issue, worktree, rework_pr, findings, has_fragments(cfg, worktree))
    start_head = common.git(worktree, "rev-parse", "HEAD")
    opened: dict = {"pr": rework_pr, "tried": False}

    def open_early() -> None:
        if opened["pr"] is not None or opened["tried"]:
            return
        try:
            if common.git(worktree, "rev-parse", "HEAD") == start_head:
                return
            opened["tried"] = True  # one attempt; the end of the session tries again and fails loudly
            opened["pr"] = dispatch_card.dispatch(worktree, number, arm=False)["number"]
            pr_file.write_text(str(opened["pr"]))
            common.log(job, f"opened #{opened['pr']} at the first commit, auto-merge not armed yet")
        except LaneError as exc:
            common.log(job, f"could not open the pull request at the first commit — {exc}")

    session_log = common.state_dir("sessions") / f"issue-{number}-{os.getpid()}.jsonl"
    common.log(job, f"session on {cfg.worker_model}" + (f", rework of #{rework_pr}" if rework_pr else ""))
    outcome = harness.run_session(cfg, model=cfg.worker_model, worktree=worktree, prompt=text, scratch=scratch,
                                  log_path=session_log, extra_env={"LANE_PR_FILE": str(pr_file)}, on_tick=open_early)
    return finish(cfg, number, worktree, outcome, start_head, session_log)


def finish(cfg: common.Config, number: int, worktree: pathlib.Path, outcome: harness.Outcome, start_head: str,
           session_log: pathlib.Path) -> int:
    job = f"worker-{number}"
    found = problems(outcome, worktree, start_head)
    if dispatch_card._count(worktree, f"wt/issue-{number}") == 0:
        common.log(job, "the session ended with no commits — no pull request. " + "; ".join(found))
        return 1
    result = dispatch_card.dispatch(worktree, number, arm=not found)
    head = common.git(worktree, "rev-parse", "HEAD")[:9]
    lines = [f"**Worker session** on `{cfg.worker_model}` at `{head}`, {outcome.elapsed // 60} min."]
    if found:
        lines.append(f"**Incomplete — auto-merge is {result['auto_merge']}:** " + "; ".join(found) + ".")
    report = harness.last_text(session_log).strip() or "(the session left no final message)"
    lines.append("\n" + report)
    open_pr.comment(result["number"], "\n".join(lines)[:COMMENT_LIMIT])
    common.log(job, f"#{result['number']} pushed at {head}, auto-merge {result['auto_merge']}"
               + (f" — {'; '.join(found)}" if found else ""))
    return 1 if found else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--rework", type=int, help="the pull request whose requested changes to address")
    args = parser.parse_args()
    job, pid = f"worker-{args.issue}", os.getpid()
    try:
        cfg = common.config()
        live = harness.live_pid(args.issue)
        if live not in (None, pid):
            common.log(job, f"worker {live} is already running this card — leaving")
            return 1
        if live is None:  # started by hand rather than by the feeder, which records the worker itself
            labels = [label["name"] for label in dispatch_card.card(cfg.checkout, args.issue).get("labels") or []]
            harness.record_worker(args.issue, pid, cards.areas_of(labels, cfg.area_prefix), args.rework)
        try:
            return drive(cfg, args.issue, args.rework)
        finally:
            harness.clear_worker(args.issue, pid)
    except LaneError as exc:
        common.log(job, f"FAILED — {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
