"""What the feeder tidies after its launches (#13): finished cards' worktrees and branches, the lane's logs, and
review sessions a killed run left behind.

    finished  a card whose lane pull request (`wt/issue-<n>`) is merged or closed, with none open and no worker
              running: its worktree under <state>/worktrees, its local branch and its scratch directory go
    logs      a log over LOG_LIMIT bytes is rotated to .1 … .LOG_KEEP; session event logs older than
              SESSION_DAYS days are deleted
    reviews   a review whose driver is dead: its session is killed and its worktree removed (harness.reap_reviews)

Only what the lane made is touched: worktrees under its state directory and `wt/issue-<n>` branches.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import time

from scripts import common, github, harness, review
from scripts.common import GhError, StateError

LOG_LIMIT = 5 * 1024 * 1024
LOG_KEEP = 3
SESSION_DAYS = 14
CARD_DIR = re.compile(r"^issue-(\d+)$")
CARD_BRANCH = re.compile(r"^wt/issue-(\d+)$")


def lane_cards(cfg: common.Config) -> set[int]:
    """Cards that have a lane worktree or a local `wt/issue-<n>` branch."""
    found = set()
    folder = common.state_dir("worktrees")
    try:
        names = [entry.name for entry in os.scandir(folder) if entry.is_dir()]
    except OSError as exc:
        raise StateError(f"the state folder {folder} could not be listed: {exc}") from exc
    found |= {int(m.group(1)) for m in map(CARD_DIR.match, names) if m}
    branches = common.git(cfg.checkout, "for-each-ref", "--format=%(refname:short)", "refs/heads/wt/")
    found |= {int(m.group(1)) for m in map(CARD_BRANCH.match, branches.splitlines()) if m}
    return found


def closed_pulls(cfg: common.Config, card: int) -> list[dict]:
    """Every pull request ever opened from `wt/issue-<card>`, with its state."""
    found = common.gh_json("pr", "list", "-R", cfg.slug, "--head", f"wt/issue-{card}", "--state", "all",
                           "--limit", "50", "--json", "number,state")
    if not isinstance(found, list) or len(found) >= 50:
        raise GhError(f"the pull requests of wt/issue-{card} came back unreadable or cut off")
    return found


def finished(cfg: common.Config, open_pulls, running: dict) -> dict:
    """card -> its merged or closed pull requests, for cards the lane is done with. Open or running is not done."""
    carried = {p.card for p in open_pulls if p.card is not None} | {
        int(m.group(1)) for p in open_pulls for m in [CARD_BRANCH.match(p.branch)] if m}
    done = {}
    for card in sorted(lane_cards(cfg) - carried - set(running)):
        pulls = closed_pulls(cfg, card)
        if pulls and all(p.get("state") in ("MERGED", "CLOSED") for p in pulls):
            done[card] = pulls
    return done


def remove_card(cfg: common.Config, card: int) -> list[str]:
    """Remove the card's worktree, local branch and scratch directory. Returns what was removed."""
    removed = []
    path = common.state_dir("worktrees") / f"issue-{card}"
    if path.exists() or path.is_symlink():
        common.inside_state(path, "worktrees")
    if (path / ".git").exists():
        common.git(cfg.checkout, "worktree", "remove", "--force", str(path))
        removed.append(f"worktree {path}")
    elif path.exists():
        _rmtree(path)
        removed.append(f"directory {path}")
    common.git(cfg.checkout, "worktree", "prune")
    branch = f"wt/issue-{card}"
    if common.git(cfg.checkout, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}"):
        common.git(cfg.checkout, "branch", "-D", branch)
        removed.append(f"branch {branch}")
    scratch = cfg.state / "scratch" / f"issue-{card}"
    if scratch.exists():
        _rmtree(scratch)
        removed.append(f"scratch {scratch}")
    return removed


def _rmtree(path: pathlib.Path) -> None:
    common.inside_state(path)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise StateError(f"{path} could not be removed: {exc}") from exc


def rotate_logs(limit: int = LOG_LIMIT, keep: int = LOG_KEEP) -> list[str]:
    """Rotate every log over `limit` bytes: name.log → name.log.1 → … → name.log.<keep> (the oldest is dropped)."""
    folder = common.state_dir("logs")
    rotated = []
    try:
        logs = [pathlib.Path(e.path) for e in os.scandir(folder)
                if e.is_file() and e.name.endswith((".log", ".out")) and e.stat().st_size > limit]
        for log in sorted(logs):
            for n in range(keep - 1, 0, -1):
                older = log.with_name(f"{log.name}.{n}")
                if older.exists():
                    os.replace(older, log.with_name(f"{log.name}.{n + 1}"))
            os.replace(log, log.with_name(f"{log.name}.1"))
            rotated.append(log.name)
    except OSError as exc:
        raise StateError(f"the logs in {folder} could not be rotated: {exc}") from exc
    return rotated


def prune_sessions(days: int = SESSION_DAYS, now: float | None = None) -> int:
    """Delete Pi event logs (and their working copies) older than `days`. Returns how many went."""
    folder = common.state_dir("sessions")
    cutoff = (now or time.time()) - days * 86400
    gone = 0
    try:
        for entry in os.scandir(folder):
            if entry.is_file() and entry.name.endswith(".jsonl") and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
                gone += 1
    except OSError as exc:
        raise StateError(f"old session logs in {folder} could not be pruned: {exc}") from exc
    return gone


def tidy(cfg: common.Config, job: str, open_pulls, running: dict, dry_run: bool = False) -> None:
    for card, pulls in finished(cfg, open_pulls, running).items():
        names = ", ".join(f"#{p['number']} {str(p['state']).lower()}" for p in pulls)
        if dry_run:
            common.log(job, f"would remove card #{card}'s worktree and branch ({names})")
            continue
        for p in pulls:
            if p.get("state") == "MERGED":
                oid, parents = github.landed_parents(cfg, int(p["number"]))
                common.log(job, f"#{p['number']} landed as {oid[:9]} with {parents} parent(s)"
                                + ("" if parents == 1 else " — NOT a squash; check the merge queue's method"))
        removed = remove_card(cfg, card)
        common.log(job, f"card #{card} is done ({names}) — removed " + (", ".join(removed) or "nothing left"))
    if dry_run:
        return
    for name in rotate_logs():
        common.log(job, f"rotated {name}")
    pruned = prune_sessions()
    if pruned:
        common.log(job, f"pruned {pruned} session log(s) older than {SESSION_DAYS} days")
    review.reap(cfg, job)
