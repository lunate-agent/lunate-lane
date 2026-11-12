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
import shlex
import subprocess
import time
import urllib.error
import urllib.request

try:  # imported as scripts.open_pr by the tests, or run as a script
    from . import common, identity
except ImportError:  # pragma: no cover - the script path
    import common
    import identity

SLUG = common.config().slug
API = "https://api.github.com"
WORKER_LOGIN = "lunate-worker[bot]"


class OpenPrError(common.LaneError):
    """Anything that stops the pull request being opened. Never carries a token."""


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


ASKPASS = """#!/bin/sh
case "$1" in
  Username*) echo x-access-token ;;
  *) cat {token_file} ;;
esac
"""


def _secret_dir() -> pathlib.Path:
    """`<app config dir>/run`: mode 700 under the directory every session's sandbox denies (harness)."""
    path = common.config().app_config.parent / "run"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as exc:
        raise OpenPrError(f"the push's secret directory {path} could not be prepared ({exc.__class__.__name__})") from exc
    return path


def push_env(token: str, run_dir: pathlib.Path, hooks_dir: pathlib.Path, host: str = "github.com") -> dict:
    """The push's environment, and its askpass helper and token file in `run_dir` (mode 600).

    The token is in neither the environment nor any argv: git asks `GIT_ASKPASS` for the password, and the helper
    reads the file. Any process of the same user can read another's argv and environment on macOS (`ps -E`,
    `sysctl KERN_PROCARGS2`, which the session sandbox cannot block), but not that file: it sits in a directory
    the sandbox denies. The old URL rewrite put the token in `git-remote-https`'s argv, visible to plain `ps`.
    """
    stem = f"push-{os.getpid()}-{time.monotonic_ns()}"
    token_file, helper = run_dir / f"{stem}.token", run_dir / f"{stem}.askpass"
    try:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(token)
        fd = os.open(helper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w") as handle:
            handle.write(ASKPASS.format(token_file=shlex.quote(str(token_file))))
    except OSError as exc:
        for path in (token_file, helper):
            path.unlink(missing_ok=True)
        raise OpenPrError(f"the push's askpass files could not be written in {run_dir} ({exc.__class__.__name__})") from exc
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_CONFIG_", "GH_", "GITHUB_"))}
    env.update({
        "GIT_ASKPASS": str(helper),
        "SSH_ASKPASS": str(helper),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "3",
        # the user name only — no secret in the URL, so none in the transport helper's argv
        "GIT_CONFIG_KEY_0": f"url.https://x-access-token@{host}/.insteadOf",
        "GIT_CONFIG_VALUE_0": f"https://{host}/",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
        "GIT_CONFIG_KEY_2": "core.hooksPath",
        "GIT_CONFIG_VALUE_2": str(hooks_dir),
        "LANE_PUSH_FILES": f"{token_file}{os.pathsep}{helper}",
    })
    return env


def _drop_push_files(env: dict) -> None:
    for name in env.get("LANE_PUSH_FILES", "").split(os.pathsep):
        if name:
            try:
                pathlib.Path(name).unlink(missing_ok=True)
            except OSError:
                pass


def push(repo: pathlib.Path, branch: str, token: str, host: str = "github.com") -> None:
    """Push the named local branch. The token never reaches the report, an argv, an environment, or the keychain.

    Every credential helper is blanked (so no helper is consulted and nothing is stored in the login keychain),
    hooks point at an empty directory, and the token reaches git only through the askpass helper (push_env).
    """
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True, text=True, cwd=str(repo), timeout=60,
    )
    if exists.returncode != 0:
        raise OpenPrError(f"there is no local branch {branch!r} in {repo} to push")
    hooks_dir = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"lane-hooks-{os.getpid()}"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    env = push_env(token, _secret_dir(), hooks_dir, host)
    try:
        done = subprocess.run(
            # never a force push (#8): a branch that moved is merged into, not overwritten, and a refused push is a
            # named failure
            ["git", "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}"],
            capture_output=True, text=True, cwd=str(repo), env=env, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenPrError(f"the push of {branch} could not be run ({exc.__class__.__name__})") from exc
    finally:
        _drop_push_files(env)
        try:
            hooks_dir.rmdir()
        except OSError:
            pass
    if done.returncode != 0:
        tail = ((done.stderr or "") + (done.stdout or "")).replace(token, "[REDACTED]")
        raise OpenPrError(f"the push of {branch} was refused: {tail.strip()[:200]}")


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


STATE = ("query($id:ID!){node(id:$id){... on PullRequest{number isDraft "
         "autoMergeRequest{enabledAt mergeMethod}}}}")


def pr_state(token: str, node_id: str | None) -> dict:
    """{draft, armed, merge_method} as GitHub reads them now. Unreadable is a failure, never 'off' or 'ready'."""
    if not node_id:
        raise OpenPrError("the pull request's state could not be read — no node id for it")
    node = _graphql(token, STATE, {"id": node_id}, "pull request state read").get("node")
    if not isinstance(node, dict) or "autoMergeRequest" not in node or not isinstance(node.get("isDraft"), bool):
        raise OpenPrError(f"pull request {node_id} came back without its draft and auto-merge state")
    request = node["autoMergeRequest"]
    method = (str(request.get("mergeMethod") or "") or None) if isinstance(request, dict) else None
    return {"draft": node["isDraft"], "armed": request is not None, "merge_method": method}


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


def _settle(token: str, number: int, node_id: str | None, arm: bool, base: str) -> dict:
    """After the push: a clean finish is marked ready, then armed with the queue's method; anything else stays a
    draft with auto-merge off. Then report what GitHub says — never the intent (#5, #6).

    The method GitHub records is reported but not required to match: with a merge queue required, GitHub has
    recorded MERGE on a pull request armed with SQUASH (#813), while the queue landed every merge as one squash
    commit. What lands is checked on the merged commit instead (`github.landed_parents`).
    """
    queue = None
    if arm:
        if pr_state(token, node_id)["draft"]:
            mark_ready(token, node_id)  # type: ignore[arg-type]
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
            "queue_method": queue, "merge_method": state["merge_method"]}


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


def open_pr(repo: pathlib.Path, branch: str, title: str, body: str, base: str = "main", arm: bool = True) -> dict:
    """Open or reuse the pull request for `branch`, as the worker identity — always opened as a draft (#5).

    `arm=True` is a clean finish: push, mark ready, then arm auto-merge. `arm=False` keeps it a draft with
    auto-merge off — and makes a reused pull request so before the push: the lane opens a card's pull request at
    its first commit, so the worker knows the number for its changelog fragment, and a rework that is not clean
    must not sit on a ready or armed pull request, not even briefly.

    Raises on anything short of a pull request that exists, is authored by `lunate-worker[bot]`, and whose draft
    and auto-merge state, read back from GitHub, are the ones asked for. The result carries that read.
    """
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
                **_settle(token, number, existing.get("node_id"), arm, base), "reused": True}

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
                    **_settle(token, number, raced.get("node_id"), arm, base), "reused": True}
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
            **_settle(token, number, pull.get("node_id"), arm, base), "reused": False}


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
            body = pathlib.Path(args.body_file).read_text()
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
