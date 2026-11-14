"""Open a pull request as the worker identity, then arm auto-merge.

The point is authorship: GitHub forbids self-approval, so a pull request has to be authored by
`lunate-worker` for the `lunate-reviewer` identity to be able to approve it — which is what makes
code-owner review on `adr/` work.

Nothing here reports a token. A token is minted, used for one push and one API call, and dropped.

    python3 scripts/open_pr.py --branch lane/x --title "..." --body-file body.md
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # imported as scripts.open_pr by the tests, or run as a script
    from . import common, github, identity, secret_scan
except ImportError:  # pragma: no cover - the script path
    import sys

    import common
    import identity
    import secret_scan
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from scripts import github

SLUG = common.config().slug
API = "https://api.github.com"
WORKER_LOGIN = "lunate-worker[bot]"


class OpenPrError(common.LaneError):
    """Anything that stops the pull request being opened. Never carries a token."""


class ReadyRunError(OpenPrError):
    """The pull request was marked ready, but CI never ran it as ready on its head — not even after one draft and
    ready toggle. It is left ready and unarmed; the card is parked with this as the reason."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(message)
        self.number = number


class SecretFound(OpenPrError):
    """What would be pushed or posted holds something secret-shaped. Names the kind and where — the file and the
    line in the branch for the diff (`path`, `line`) — never the value."""

    def __init__(self, kind: str, where: str, path: str = "", line: int = 0) -> None:
        self.kind, self.where, self.path, self.line = kind, where, path, line
        super().__init__(f"the {self.location} holds something shaped like a {kind} — nothing was pushed")

    @property
    def location(self) -> str:
        return f"{self.where} at `{self.path}:{self.line}`" if self.path else self.where


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff: str):
    """(path, line in the new file, text) for each added line of a `--unified=0` diff."""
    path, number = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else ("" if target == "/dev/null" else target)
        elif raw.startswith("@@"):
            match = HUNK.match(raw)
            number = int(match.group(1)) if match else 0
        elif raw.startswith("+"):
            yield path, number, raw[1:]
            number += 1


def scan(repo: pathlib.Path, branch: str, base: str, title: str, body: str, known: tuple = ()) -> None:
    """Before every push (#12): the diff's added lines against the base, the title and the body. A match raises
    SecretFound naming the kind and, for the diff, the first matching file and line; an unreadable diff is a
    failure, never a clean scan."""
    try:
        diff = common.git(repo, "diff", "--no-color", "--no-textconv", "--no-ext-diff", "--unified=0",
                          f"origin/{base}...refs/heads/{branch}")
    except common.GitError as exc:
        raise OpenPrError(f"the diff of {branch} could not be read for the secret scan: {exc}") from exc
    lines = list(added_lines(diff))
    added = "\n".join(text for _, _, text in lines)
    kind = secret_scan.secret_kind(added, known, strict=True)
    if kind:  # the whole first (a shape could span lines), then the first line that holds it, for where
        at = next(((path, n) for path, n, text in lines if secret_scan.secret_kind(text, known, strict=True)),
                  ("", 0))
        raise SecretFound(kind, "diff's added lines", *at)
    for where, text in (("title", title), ("body", body)):
        kind = secret_scan.secret_kind(text, known, strict=True)
        if kind:
            raise SecretFound(kind, where)


def _api(token: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "lunate-lane",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise OpenPrError(f"GitHub refused {method} {path} ({exc.code}): {detail}") from exc
    except Exception as exc:
        raise OpenPrError(f"GitHub could not be reached for {method} {path} ({exc.__class__.__name__})") from exc


# The token goes only to a prompt for exactly the push URL's host; any other prompt gets nothing (exit 1).
ASKPASS = """#!/bin/sh
case "$1" in
  {password_prompt}) cat {token_file} ;;
  {username_prompt}) echo x-access-token ;;
  *) exit 1 ;;
esac
"""
# What the push mirror's own config may hold: what `git init --bare` writes, nothing that redirects or runs.
MIRROR_KEYS = frozenset({"core.repositoryformatversion", "core.filemode", "core.bare", "core.ignorecase",
                         "core.precomposeunicode", "core.logallrefupdates"})
SAFE_GIT = common.SAFE_GIT


def push_url(host: str = "github.com") -> str:
    return f"https://x-access-token@{host}/{SLUG}.git"


def _secret_dir() -> pathlib.Path:
    """`<app config dir>/run`: mode 700 under the directory every session's sandbox denies (harness)."""
    path = common.config().app_config.parent / "run"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise OpenPrError(f"the push's secret directory {path} could not be prepared ({exc.__class__.__name__})") from exc
    return path


_git_env = common.git_env


PUSH_FILE = re.compile(r"^push-(\d+)-\d+\.(token|askpass)$")


def sweep_secret_dir() -> int:
    """Remove token and askpass files whose push is gone (its process was killed before its `finally`). Run at
    the start of every feeder tick and every push. Returns how many went."""
    run_dir = _secret_dir()
    gone = 0
    try:
        for entry in os.scandir(run_dir):
            match = PUSH_FILE.match(entry.name)
            if not match:
                continue
            try:
                os.kill(int(match.group(1)), 0)
                continue  # its push is still running
            except ProcessLookupError:
                pass
            except PermissionError:
                continue  # alive, someone else's
            os.unlink(entry.path)
            gone += 1
    except OSError as exc:
        raise OpenPrError(f"stale push files in {run_dir} could not be swept ({exc.__class__.__name__})") from exc
    return gone


def push_env(token: str, run_dir: pathlib.Path, url: str) -> dict:
    """The push's environment, and its askpass helper and token file in `run_dir` (mode 600).

    The token is in neither the environment nor any argv: git asks `GIT_ASKPASS` for the password, and the helper
    reads the file — only when git's prompt names exactly `url`'s scheme and host (git's prompt is
    `Password for 'https://x-access-token@github.com': `). Any process of the same user can read another's argv
    and environment on macOS, but not that file: it sits in a directory the session sandbox denies.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    origin = f"{parts.scheme}://x-access-token@{host}"
    stem = f"push-{os.getpid()}-{time.monotonic_ns()}"
    token_file, helper = run_dir / f"{stem}.token", run_dir / f"{stem}.askpass"
    try:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(token)
        fd = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w") as handle:
            handle.write(ASKPASS.format(
                token_file=shlex.quote(str(token_file)),
                password_prompt=shlex.quote(f"Password for '{origin}': "),
                username_prompt=shlex.quote(f"Username for '{parts.scheme}://{host}': ")))
    except OSError as exc:
        for path in (token_file, helper):
            path.unlink(missing_ok=True)
        raise OpenPrError(f"the push's askpass files could not be written in {run_dir} ({exc.__class__.__name__})") from exc
    return _git_env({"GIT_ASKPASS": str(helper), "SSH_ASKPASS": str(helper),
                     "LANE_PUSH_FILES": f"{token_file}{os.pathsep}{helper}"})


def _drop_push_files(env: dict) -> None:
    for name in env.get("LANE_PUSH_FILES", "").split(os.pathsep):
        if name:
            try:
                pathlib.Path(name).unlink(missing_ok=True)
            except OSError:
                pass


def _run_git(argv: list, cwd: pathlib.Path | None, env: dict, what: str, token: str = "") -> str:
    try:
        done = subprocess.run(["git", *SAFE_GIT, *argv], capture_output=True, text=True,
                              cwd=str(cwd) if cwd else None, env=env, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenPrError(f"{what} could not be run ({exc.__class__.__name__}: {exc})") from exc
    if done.returncode != 0:
        tail = (done.stderr or "") + (done.stdout or "")
        raise OpenPrError(f"{what} was refused: {(tail.replace(token, '[REDACTED]') if token else tail).strip()[:200]}")
    return done.stdout


def push_mirror() -> pathlib.Path:
    """`<state>/push-mirror.git`: a bare repository only the lane writes (the session sandbox denies the state
    directory), whose config is checked to hold nothing but what `git init --bare` puts there."""
    mirror = common.state_dir() / "push-mirror.git"
    env = _git_env()
    if not (mirror / "HEAD").exists():
        _run_git(["init", "-q", "--bare", str(mirror)], None, env, "creating the push mirror")
    keys = {line.split("=", 1)[0].lower()
            for line in _run_git(["-C", str(mirror), "config", "--local", "--list"], None, env,
                                 "reading the push mirror's config").splitlines() if line}
    if keys - MIRROR_KEYS:
        raise OpenPrError(f"the push mirror {mirror} has config it must not have ({', '.join(sorted(keys - MIRROR_KEYS))})"
                          " — nothing was pushed; delete the mirror and the next push recreates it")
    return mirror


def push(repo: pathlib.Path, branch: str, token: str, url: str | None = None) -> None:
    """Push the named local branch of `repo` (a worktree) to `url` — `https://github.com/<repo>.git` unless given.

    The branch is first fetched into the lane's private bare mirror, and pushed from there to the explicit URL:
    the worktree's config (which a session could write: a `pushurl`, an `insteadOf`) is never consulted for where
    the push goes. System and global config are off, hooks point nowhere, no credential helper is asked, and the
    token reaches git only through the askpass helper, for exactly that host (push_env). Never a force push (#8).
    """
    url = url or push_url()
    env = _git_env()
    try:
        exists = subprocess.run(["git", *SAFE_GIT, "-C", str(repo), "rev-parse", "--verify", "--quiet",
                                 f"refs/heads/{branch}"], capture_output=True, text=True, env=env, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenPrError(f"git could not check {branch!r} in {repo} ({exc.__class__.__name__}: {exc})") from exc
    if exists.returncode not in (0, 1):
        raise OpenPrError(f"git could not check {branch!r} in {repo}: {exists.stderr.strip()[:160]}")
    if exists.returncode != 0:
        raise OpenPrError(f"there is no local branch {branch!r} in {repo} to push")
    source = repo / ".git"
    if source.is_symlink() or not source.is_dir():  # never follow a worktree's .git pointer file
        raise OpenPrError(f"{repo} is not a checkout with its own .git directory — push from the checkout")
    mirror = push_mirror()
    ref = f"refs/heads/{branch}"
    _run_git(["-C", str(mirror), "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(source.resolve()),
              f"+{ref}:{ref}"],
             None, env, f"copying {branch} into the push mirror")
    sweep_secret_dir()
    secret_env = push_env(token, _secret_dir(), url)
    try:
        _run_git(["-C", str(mirror), "push", url, f"{ref}:{ref}"], None, secret_env, f"the push of {branch}", token)
    finally:
        _drop_push_files(secret_env)


def _graphql(token: str, query: str, variables: dict, what: str) -> dict:
    """A GraphQL call with the worker token. Errors in the answer, or no data, raise."""
    request = urllib.request.Request(
        f"{API}/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        method="POST",
        headers={"Authorization": f"token {token}", "User-Agent": "lunate-lane"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise OpenPrError(f"the {what} call was refused ({exc.code})") from exc
    except Exception as exc:
        raise OpenPrError(f"GitHub could not be reached for the {what} call ({exc.__class__.__name__})") from exc
    if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
        detail = payload.get("errors") if isinstance(payload, dict) else payload
        raise OpenPrError(f"{what} was refused: {str(detail)[:160]}")
    return payload["data"]


def queue_method(token: str, base: str) -> str:
    """The merge method of the base branch's merge queue, read from its rules (ruleset `protect-default` sets
    `merge_queue.merge_method`). The lane arms with it rather than guessing (#6); no queue rule is a refusal."""
    rules = _api(token, "GET", f"/repos/{SLUG}/rules/branches/{base}")
    if not isinstance(rules, list):
        raise OpenPrError(f"the rules for {base} came back as {type(rules).__name__}, not a list")
    methods = {str((rule.get("parameters") or {}).get("merge_method") or "").upper()
               for rule in rules if isinstance(rule, dict) and rule.get("type") == "merge_queue"}
    methods.discard("")
    if len(methods) != 1:
        raise OpenPrError(f"{base} has {'no' if not methods else 'more than one'} merge queue method in its rules "
                          f"({', '.join(sorted(methods)) or 'none'}) — refusing to guess how auto-merge lands")
    return methods.pop()


def arm_auto_merge(token: str, node_id: str | None, method: str) -> None:
    """Arm auto-merge through the merge queue with the queue's own method, or raise. A refused auto-merge is not
    a success.

    Rule one applies to this file first: if auto-merge cannot be armed, the caller must exit non-zero
    rather than report a pull request as delivered.
    """
    if not node_id:
        raise OpenPrError("auto-merge could not be armed — no node id was returned for the pull request")
    query = (
        "mutation($id:ID!,$method:PullRequestMergeMethod!)"
        "{enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:$method})"
        "{pullRequest{autoMergeRequest{enabledAt}}}}"
    )
    data = _graphql(token, query, {"id": node_id, "method": method}, "auto-merge")
    armed = (data.get("enablePullRequestAutoMerge") or {}).get("pullRequest") or {}
    if not armed.get("autoMergeRequest"):
        raise OpenPrError("auto-merge reported no error but came back unarmed")


def disarm_auto_merge(token: str, node_id: str) -> None:
    """Turn auto-merge off. Called only on a pull request that reads as armed."""
    query = ("mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id})"
             "{pullRequest{autoMergeRequest{enabledAt}}}}")
    _graphql(token, query, {"id": node_id}, "disable auto-merge")


STATE = ("query($id:ID!){node(id:$id){... on PullRequest{number isDraft headRefOid "
         "autoMergeRequest{enabledAt mergeMethod}}}}")
HEAD_WAIT = 120  # seconds GitHub gets to show the pushed commit as the pull request's head
HEAD_POLL = 2.0


def pr_state(token: str, node_id: str | None) -> dict:
    """{draft, armed, merge_method} as GitHub reads them now. Unreadable is a failure, never 'off' or 'ready'."""
    if not node_id:
        raise OpenPrError("the pull request's state could not be read — no node id for it")
    node = _graphql(token, STATE, {"id": node_id}, "pull request state read").get("node")
    if not isinstance(node, dict) or "autoMergeRequest" not in node or not isinstance(node.get("isDraft"), bool):
        raise OpenPrError(f"pull request {node_id} came back without its draft and auto-merge state")
    request = node["autoMergeRequest"]
    method = (str(request.get("mergeMethod") or "") or None) if isinstance(request, dict) else None
    return {"draft": node["isDraft"], "armed": request is not None, "merge_method": method,
            "head": str(node.get("headRefOid") or "")}


def wait_for_head(token: str, number: int, node_id: str | None, head: str) -> None:
    """Return once GitHub shows `head` as the pull request's head; a named failure after HEAD_WAIT seconds.

    Marking a pull request ready before GitHub has moved its head to a just-pushed commit sends the
    `ready_for_review` event with the old head: its CI run is cancelled by the next one, and the new head keeps
    only the `synchronize` run, which still saw a draft — #813's head was left with nothing but ci-ok's
    deliberate draft failure.
    """
    deadline = time.monotonic() + HEAD_WAIT
    while True:
        seen = pr_state(token, node_id)["head"]
        if seen == head:
            return
        if time.monotonic() >= deadline:
            raise OpenPrError(f"#{number}: GitHub still shows {seen[:9] or 'no head'} as the head, not the pushed "
                              f"{head[:9]}, after {HEAD_WAIT}s — not marked ready; the next clean finish tries again")
        time.sleep(HEAD_POLL)


RUN_WAIT = 180  # seconds GitHub gets to start the draft (synchronize/opened) CI run for a pushed head
READY_WAIT = 300  # seconds it gets to start a CI run that sees the pull request as ready
READY_SETTLE = 20.0  # a ready run must still be uncancelled this long after it is first seen
RUN_POLL = 5.0


def wait_for_draft_run(number: int, head: str) -> frozenset:
    """Return the ids of the CI runs on `head` once at least one exists — the run the push started while the pull
    request is a draft. Marking it ready before that run exists let the late draft run cancel the ready run
    through CI's concurrency group (#816: ready run 43322494831 cancelled by draft run 43322494914, created the same
    second), leaving the head with only ci-ok's deliberate draft failure."""
    cfg = common.config()
    deadline = time.monotonic() + RUN_WAIT
    while True:
        runs = github.ci_runs(cfg, head)
        if runs:
            return frozenset(r.id for r in runs)
        if time.monotonic() >= deadline:
            raise OpenPrError(f"#{number}: no {cfg.ci_workflow} run started on the pushed {head[:9]} within "
                              f"{RUN_WAIT}s — not marked ready; it stays a draft and the feeder relaunches it")
        time.sleep(RUN_POLL)


def ensure_ready_run(token: str, number: int, node_id: str, head: str, draft_runs: frozenset) -> str:
    """After marking ready: return once a CI run that saw the pull request as ready (`github.ready_run`) is on
    `head` and has not been cancelled, read twice READY_SETTLE seconds apart. If the only ready run was cancelled
    (CI's concurrency group), toggle draft then ready once and wait for the new one. Anything else — a second
    cancelled ready run, or none at all within READY_WAIT — is a `ReadyRunError`. Returns what happened."""
    cfg = common.config()
    toggled, seen = False, None
    deadline = time.monotonic() + READY_WAIT
    while True:
        runs = github.ci_runs(cfg, head)
        ready = [r for r in runs if github.ready_run(r, draft_runs)]
        live = [r for r in ready if not r.cancelled]
        fresh_cancelled = [r for r in ready if r.cancelled and r.id not in draft_runs]
        if live:
            if seen is None:
                seen = time.monotonic()
            elif time.monotonic() - seen >= READY_SETTLE:
                return f"ready run {live[-1].id}" + (" after one draft/ready toggle" if toggled else "")
        elif fresh_cancelled:
            seen = None
            ids = ", ".join(str(r.id) for r in fresh_cancelled)
            if toggled:
                raise ReadyRunError(number, f"#{number}: the ready {cfg.ci_workflow} run on {head[:9]} was cancelled "
                                            f"again after one draft/ready toggle (run(s) {ids})")
            convert_to_draft(token, node_id)
            draft_runs = frozenset(r.id for r in runs)  # every run so far: the next new one is the ready one
            mark_ready(token, node_id)
            toggled = True
            deadline = time.monotonic() + READY_WAIT
            continue
        else:
            seen = None
        if time.monotonic() >= deadline:
            raise ReadyRunError(number, f"#{number}: no {cfg.ci_workflow} run saw it as ready on {head[:9]} within "
                                        f"{READY_WAIT}s of marking it ready"
                                        + (" (after one draft/ready toggle)" if toggled else ""))
        time.sleep(RUN_POLL)


def auto_merge_state(token: str, node_id: str | None) -> tuple[bool, str | None]:
    """(on, the merge method GitHub records) for this pull request."""
    state = pr_state(token, node_id)
    return state["armed"], state["merge_method"]


def auto_merge_enabled(token: str, node_id: str | None) -> bool:
    """Whether GitHub says auto-merge is on for this pull request. Unreadable is a failure, not 'off'."""
    return pr_state(token, node_id)["armed"]


def mark_ready(token: str, node_id: str) -> None:
    _graphql(token, "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
                    "{pullRequest{isDraft}}}", {"id": node_id}, "mark ready for review")


def convert_to_draft(token: str, node_id: str) -> None:
    _graphql(token, "mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id})"
                    "{pullRequest{isDraft}}}", {"id": node_id}, "convert to draft")


def hold(token: str, number: int, node_id: str | None) -> None:
    """Make the pull request a draft with auto-merge off, and read both back — before unclean work is pushed (#5).

    Lunate's CI runs its heavy legs only on ready pull requests, and an armed pull request could land
    half-done work, so nothing that is not a clean finish is pushed to one that is ready or armed.
    """
    state = pr_state(token, node_id)
    if state["armed"]:
        disarm_auto_merge(token, node_id)  # type: ignore[arg-type]
    if not state["draft"]:
        convert_to_draft(token, node_id)  # type: ignore[arg-type]
    after = pr_state(token, node_id)
    if after["armed"] or not after["draft"]:
        raise OpenPrError(f"#{number}: should be a draft with auto-merge off but GitHub reads it as "
                          f"{'a draft' if after['draft'] else 'ready'} with auto-merge "
                          f"{'on' if after['armed'] else 'off'} — nothing was pushed")


def _settle(token: str, number: int, node_id: str | None, arm: bool, base: str, head: str | None = None) -> dict:
    """After the push: a clean finish is marked ready, then armed with the queue's method; anything else stays a
    draft with auto-merge off. Then report what GitHub says — never the intent (#5, #6).

    The method GitHub records is reported but not required to match: with a merge queue required, GitHub has
    recorded MERGE on a pull request armed with SQUASH (#813), while the queue landed every merge as one squash
    commit. What lands is checked on the merged commit instead (`github.landed_parents`).
    """
    queue, ready = None, ""
    if arm:
        if head:
            wait_for_head(token, number, node_id, head)  # ready_for_review must carry the pushed head
        if pr_state(token, node_id)["draft"]:
            # the draft run first, so it cannot start after the ready run and cancel it; then a ready run that
            # stays uncancelled, before arming — a ReadyRunError leaves it ready and unarmed
            draft_runs = wait_for_draft_run(number, head) if head else frozenset()
            mark_ready(token, node_id)  # type: ignore[arg-type]
            if head:
                ready = ensure_ready_run(token, number, node_id, head, draft_runs)  # type: ignore[arg-type]
        queue = queue_method(token, base)
        arm_auto_merge(token, node_id, queue)
    else:
        hold(token, number, node_id)
    state = pr_state(token, node_id)
    if state["armed"] != arm or state["draft"] == arm:
        raise OpenPrError(f"#{number} should be {'ready and armed' if arm else 'a draft, not armed'} but GitHub reads "
                          f"it as {'a draft' if state['draft'] else 'ready'}, auto-merge "
                          f"{'on' if state['armed'] else 'off'}")
    return {"auto_merge": "armed" if state["armed"] else "not armed", "draft": state["draft"],
            "queue_method": queue, "merge_method": state["merge_method"], "ready_run": ready}


def _worker_token() -> str:
    """A named failure if the identity cannot mint, never a raw traceback."""
    try:
        return identity.installation_token("worker")
    except identity.IdentityError as exc:
        raise OpenPrError(f"the worker identity could not mint a token: {exc}") from exc


def find_open_pr(branch: str, token: str) -> dict | None:
    """The open pull request for this branch, if any. Reusing one beats failing with a 422."""
    pulls = _api(token, "GET", f"/repos/{SLUG}/pulls?state=open&head={SLUG.split('/')[0]}:{branch}")
    if not isinstance(pulls, list):
        raise OpenPrError(f"the open pull requests for {branch!r} came back as {type(pulls).__name__}, not a list")
    return pulls[0] if pulls else None


def _owned(existing: dict, branch: str) -> tuple[int, str]:
    number = existing.get("number")
    author = (existing.get("user") or {}).get("login", "")
    if author != WORKER_LOGIN:
        raise OpenPrError(f"#{number} is already open for {branch!r} but authored by {author!r}, not {WORKER_LOGIN}")
    return int(number), author


def open_pr(repo: pathlib.Path, branch: str, title: str, body: str, base: str = "main", arm: bool = True,
            known: tuple = ()) -> dict:
    """Open or reuse the pull request for `branch`, as the worker identity — always opened as a draft (#5).

    `arm=True` is a clean finish: push, mark ready, then arm auto-merge. `arm=False` keeps it a draft with
    auto-merge off — and makes a reused pull request so before the push: the lane opens a card's pull request at
    its first commit, so the worker knows the number for its changelog fragment, and a rework that is not clean
    must not sit on a ready or armed pull request, not even briefly.

    Raises on anything short of a pull request that exists, is authored by `lunate-worker[bot]`, and whose draft
    and auto-merge state, read back from GitHub, are the ones asked for. The result carries that read.

    Before anything else the diff, title and body are scanned (`scan`, #12): a match pushes and changes nothing.
    `known` are values that must never leave (the model credential).
    """
    scan(repo, branch, base, title, body, known)
    try:
        head = common.git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
    except common.GitError as exc:
        raise OpenPrError(f"there is no local branch {branch!r} in {repo} to push") from exc
    token = _worker_token()

    # the author is checked before the push: never overwrite a branch whose PR belongs to someone else
    existing = find_open_pr(branch, token)
    if existing:
        number, author = _owned(existing, branch)
        if not arm:
            hold(token, number, existing.get("node_id"))
        # a re-dispatch usually carries new commits, and reuse must deliver them
        push(repo, branch, token)
        return {"number": number, "url": existing.get("html_url"), "author": author,
                **_settle(token, number, existing.get("node_id"), arm, base, head), "reused": True}

    push(repo, branch, token)
    try:
        pull = _api(token, "POST", f"/repos/{SLUG}/pulls",
                    {"title": title, "body": body, "head": branch, "base": base, "draft": True})
    except OpenPrError:
        # a race: another run opened it between the check and this call
        raced = find_open_pr(branch, token)
        if raced:
            number, author = _owned(raced, branch)
            return {"number": number, "url": raced.get("html_url"), "author": author,
                    **_settle(token, number, raced.get("node_id"), arm, base, head), "reused": True}
        raise

    number = pull.get("number")
    author = (pull.get("user") or {}).get("login", "")
    if not number:
        raise OpenPrError("the pull request was not returned with a number")
    if author != WORKER_LOGIN:
        # never leave behind a pull request the reviewer identity cannot approve
        try:
            _api(token, "PATCH", f"/repos/{SLUG}/pulls/{number}", {"state": "closed"})
            closed = " and closed again"
        except OpenPrError:
            closed = " and it could NOT be closed"
        raise OpenPrError(f"the pull request came back authored by {author!r}, not {WORKER_LOGIN}{closed}")

    return {"number": number, "url": pull.get("html_url"), "author": author,
            **_settle(token, number, pull.get("node_id"), arm, base, head), "reused": False}


def disarm(number: int, node_id: str) -> None:
    """Turn auto-merge off on an armed pull request whose card waits on an open issue, as the worker identity, and
    read it back. It stays ready; the feeder enqueues it once it is approved after the blockers close."""
    token = _worker_token()
    if not pr_state(token, node_id)["armed"]:
        return
    disarm_auto_merge(token, node_id)
    if pr_state(token, node_id)["armed"]:
        raise OpenPrError(f"#{number}'s auto-merge still reads as on after turning it off")


ENQUEUE = ("mutation($id:ID!,$head:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$id,expectedHeadOid:$head})"
           "{mergeQueueEntry{state position}}}")
QUEUE_ENTRY = ("query($id:ID!){node(id:$id){... on PullRequest{isDraft headRefOid isInMergeQueue "
               "mergeQueueEntry{state position enqueuedAt}}}}")


def queue_entry(token: str, node_id: str) -> dict:
    """{draft, head, entry}: the pull request's merge queue entry as GitHub reads it now (entry None when it is not
    queued). Unreadable is a failure, never 'not queued'."""
    node = _graphql(token, QUEUE_ENTRY, {"id": node_id}, "merge queue entry read").get("node")
    if not isinstance(node, dict) or "mergeQueueEntry" not in node or not isinstance(node.get("isDraft"), bool):
        raise OpenPrError(f"pull request {node_id} came back without its merge queue state")
    entry = node["mergeQueueEntry"] if isinstance(node["mergeQueueEntry"], dict) else None
    return {"draft": node["isDraft"], "head": str(node.get("headRefOid") or ""), "entry": entry}


def enqueue(number: int, node_id: str, head: str) -> dict:
    """Put a ready pull request into the merge queue at `head` (`enqueuePullRequest`, expected head given, so a
    push in between is refused rather than queued), as the worker identity, and read the entry back. A refused
    call whose read-back shows the entry anyway (GitHub's auto-merge got there first) is a success; anything else
    raises. Returns {state, position}."""
    token = _worker_token()
    before = queue_entry(token, node_id)
    if before["draft"]:
        raise OpenPrError(f"#{number} is a draft — a draft is never enqueued")
    if before["head"] != head:
        raise OpenPrError(f"#{number}'s head is {before['head'][:8]}, not {head[:8]} — not enqueued")
    refused = ""
    if before["entry"] is None:
        try:
            _graphql(token, ENQUEUE, {"id": node_id, "head": head}, "enqueue")
        except OpenPrError as exc:
            refused = f" (the enqueue was refused: {exc})"
    after = queue_entry(token, node_id)
    if after["entry"] is None or after["head"] != head:
        raise OpenPrError(f"#{number} is not in the merge queue at {head[:8]} after enqueueing{refused}")
    return {"state": str(after["entry"].get("state") or ""), "position": after["entry"].get("position")}


def return_to_draft(branch: str, expected: int) -> None:
    """At the start of a rework: the branch's open pull request goes back to draft with auto-merge off (#5)."""
    token = _worker_token()
    existing = find_open_pr(branch, token)
    if not existing:
        raise OpenPrError(f"no open pull request for {branch!r} to return to draft")
    number, _ = _owned(existing, branch)
    if number != expected:
        raise OpenPrError(f"the open pull request for {branch!r} is #{number}, not #{expected}")
    hold(token, number, existing.get("node_id"))


def comment(number: int, body: str) -> None:
    """A comment on the pull request, as the worker identity."""
    _api(_worker_token(), "POST", f"/repos/{SLUG}/issues/{number}/comments", {"body": body})


def close_pr(number: int, branch: str) -> str:
    """Close a pull request and delete its branch, as the worker identity. For self-tests."""
    token = _worker_token()
    _api(token, "PATCH", f"/repos/{SLUG}/pulls/{number}", {"state": "closed"})
    try:
        _api(token, "DELETE", f"/repos/{SLUG}/git/refs/heads/{branch}")
    except OpenPrError as exc:
        return f"closed, but the branch was not deleted: {exc}"
    return "closed and the branch is deleted"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=str(pathlib.Path.home() / "dev" / "lunate"))
    parser.add_argument("--branch", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", help="the body as a string")
    parser.add_argument("--body-file")
    parser.add_argument("--base", default="main")
    parser.add_argument("--close", type=int, help="close this pull request and delete --branch instead of opening")
    args = parser.parse_args()
    try:
        if args.close:
            print(close_pr(args.close, args.branch))
            return 0
        if args.body_file:
            try:
                body = pathlib.Path(args.body_file).read_text()
            except OSError as exc:
                raise OpenPrError(f"the body file {args.body_file} could not be read: {exc}") from exc
        else:
            body = args.body or ""
        result = open_pr(pathlib.Path(args.repo), args.branch, args.title, body, args.base)
    except OpenPrError as exc:
        print(f"FAILED — {exc}")
        return 1
    print(f"opened #{result['number']} as {result['author']} — auto-merge {result['auto_merge']} — {result['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
