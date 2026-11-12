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
import subprocess
import urllib.error
import urllib.request

import identity

SLUG = "lunate-agent/lunate"
API = "https://api.github.com"
WORKER_LOGIN = "lunate-worker[bot]"


class OpenPrError(RuntimeError):
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


def push(repo: pathlib.Path, branch: str, token: str, remote: str = "https://github.com") -> None:
    """Push the branch with the worker's token. The token never reaches the report.

    The token is passed by git config rewrite rather than in argv, so it cannot show up in a process listing.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_ASKPASS": "echo",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.https://x-access-token:{token}@{remote}/{SLUG}.git.insteadOf",
            "GIT_CONFIG_VALUE_0": f"{remote}/{SLUG}.git",
        }
    )
    done = subprocess.run(
        ["git", "push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}"],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
        timeout=180,
    )
    if done.returncode != 0:
        tail = ((done.stderr or "") + (done.stdout or "")).replace(token, "[REDACTED]")
        raise OpenPrError(f"the push was refused: {tail.strip()[:200]}")


def arm_auto_merge(token: str, node_id: str | None, method: str = "SQUASH") -> str:
    """Arm auto-merge through the merge queue. A failure here is reported, not swallowed."""
    if not node_id:
        return "not armed — no node id was returned"
    query = (
        "mutation($id:ID!,$method:PullRequestMergeMethod!)"
        "{enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:$method})"
        "{pullRequest{autoMergeRequest{enabledAt}}}}"
    )
    request = urllib.request.Request(
        f"{API}/graphql",
        data=json.dumps({"query": query, "variables": {"id": node_id, "method": method}}).encode(),
        method="POST",
        headers={"Authorization": f"token {token}", "User-Agent": "lunate-lane"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except Exception as exc:
        return f"not armed — {exc.__class__.__name__}"
    if payload.get("errors"):
        return f"not armed — {str(payload['errors'])[:120]}"
    return "armed"


def open_pr(repo: pathlib.Path, branch: str, title: str, body: str, base: str = "main") -> dict:
    """Mint, push, open, verify the author, arm auto-merge. Raises rather than half-succeeding."""
    token = identity.installation_token("worker")
    push(repo, branch, token)
    pull = _api(
        token,
        "POST",
        f"/repos/{SLUG}/pulls",
        {"title": title, "body": body, "head": branch, "base": base},
    )
    number = pull.get("number")
    author = (pull.get("user") or {}).get("login", "")
    if not number:
        raise OpenPrError("the pull request was not returned with a number")
    if author != WORKER_LOGIN:
        raise OpenPrError(f"the pull request came back authored by {author!r}, not {WORKER_LOGIN}")
    armed = arm_auto_merge(token, pull.get("node_id"))
    return {"number": number, "url": pull.get("html_url"), "author": author, "auto_merge": armed}


def close_pr(number: int, branch: str) -> str:
    """Close a pull request and delete its branch, as the worker identity. For self-tests."""
    token = identity.installation_token("worker")
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
    parser.add_argument("--body-file")
    parser.add_argument("--base", default="main")
    parser.add_argument("--close", type=int, help="close this pull request and delete --branch instead of opening")
    args = parser.parse_args()
    try:
        if args.close:
            print(close_pr(args.close, args.branch))
            return 0
        body = pathlib.Path(args.body_file).read_text() if args.body_file else ""
        result = open_pr(pathlib.Path(args.repo), args.branch, args.title, body, args.base)
    except OpenPrError as exc:
        print(f"FAILED — {exc}")
        return 1
    print(f"opened #{result['number']} as {result['author']} — auto-merge {result['auto_merge']} — {result['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
