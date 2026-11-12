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


def push(repo: pathlib.Path, branch: str, token: str, host: str = "github.com") -> None:
    """Push the named local branch. The token never reaches the report, argv, a file, or the keychain.

    Three things are disabled so nothing durable can see the token: it rides in the URL prefix rewrite, every
    credential helper is blanked (so no helper is consulted and nothing is stored in the login keychain), and
    hooks are pointed at an empty directory.
    """
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True, text=True, cwd=str(repo), timeout=60,
    )
    if exists.returncode != 0:
        raise OpenPrError(f"there is no local branch {branch!r} in {repo} to push")
    hooks_dir = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"lane-hooks-{os.getpid()}"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "GIT_ASKPASS": "true",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "4",
            # a prefix rewrite: the token is in the config, not argv, so it cannot show in a process listing
            "GIT_CONFIG_KEY_0": f"url.https://x-access-token:{token}@{host}/.insteadOf",
            "GIT_CONFIG_VALUE_0": f"https://{host}/",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_CONFIG_KEY_2": "core.hooksPath",
            "GIT_CONFIG_VALUE_2": str(hooks_dir),
            "GIT_CONFIG_KEY_3": "credential.interactive",
            "GIT_CONFIG_VALUE_3": "never",
        }
    )
    done = subprocess.run(
        ["git", "push", "--force-with-lease", "origin", f"refs/heads/{branch}:refs/heads/{branch}"],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
        timeout=180,
    )
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


def arm_auto_merge(token: str, node_id: str | None, method: str = "SQUASH") -> None:
    """Arm auto-merge through the merge queue, or raise. A refused auto-merge is not a success.

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


def auto_merge_enabled(token: str, node_id: str | None) -> bool:
    """Whether GitHub says auto-merge is on for this pull request. Unreadable is a failure, not 'off'."""
    if not node_id:
        raise OpenPrError("auto-merge state could not be read — no node id for the pull request")
    data = _graphql(token, "query($id:ID!){node(id:$id){... on PullRequest{number autoMergeRequest{enabledAt}}}}",
                    {"id": node_id}, "auto-merge read")
    node = data.get("node")
    if not isinstance(node, dict) or "autoMergeRequest" not in node:
        raise OpenPrError(f"pull request {node_id} came back without its auto-merge state")
    return node["autoMergeRequest"] is not None


def _settle(token: str, node_id: str | None, arm: bool) -> str:
    """Arm, or make sure auto-merge is off, then report what GitHub says — never the intent."""
    if arm:
        arm_auto_merge(token, node_id)
    elif auto_merge_enabled(token, node_id):
        disarm_auto_merge(token, node_id)  # type: ignore[arg-type]
    actual = auto_merge_enabled(token, node_id)
    if actual != arm:
        raise OpenPrError(f"auto-merge should be {'on' if arm else 'off'} but GitHub reads it as "
                          f"{'on' if actual else 'off'}")
    return "armed" if actual else "not armed"


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


def open_pr(repo: pathlib.Path, branch: str, title: str, body: str, base: str = "main", arm: bool = True) -> dict:
    """Open or reuse the pull request for `branch`, as the worker identity, then arm auto-merge.

    `arm=False` leaves auto-merge off — and turns it off on a reused pull request that was armed, before the push:
    the lane opens a card's pull request at its first commit, so the worker knows the number for its changelog
    fragment, and arms it only when the session finished cleanly. A rework that is not clean disarms it again.

    Raises on anything short of a pull request that exists, is authored by `lunate-worker[bot]`, and whose
    auto-merge state, read back from GitHub, is the one asked for. `auto_merge` in the result is that read.
    """
    token = _worker_token()

    # the author is checked before the push: never overwrite a branch whose PR belongs to someone else
    existing = find_open_pr(branch, token)
    if existing:
        number = existing.get("number")
        author = (existing.get("user") or {}).get("login", "")
        if author != WORKER_LOGIN:
            raise OpenPrError(
                f"#{number} is already open for {branch!r} but authored by {author!r}, not {WORKER_LOGIN}"
            )
        if not arm and auto_merge_enabled(token, existing.get("node_id")):
            # off before the push: work that is not clean never sits on an armed pull request, not even briefly
            disarm_auto_merge(token, existing["node_id"])
            if auto_merge_enabled(token, existing["node_id"]):
                raise OpenPrError(f"#{number}: auto-merge should be off but GitHub reads it as on after the disarm "
                                  "— nothing was pushed")
        # a re-dispatch usually carries new commits, and reuse must deliver them
        push(repo, branch, token)
        return {"number": number, "url": existing.get("html_url"), "author": author,
                "auto_merge": _settle(token, existing.get("node_id"), arm), "reused": True}

    push(repo, branch, token)
    try:
        pull = _api(
            token,
            "POST",
            f"/repos/{SLUG}/pulls",
            {"title": title, "body": body, "head": branch, "base": base},
        )
    except OpenPrError:
        # a race: another run opened it between the check and this call
        raced = find_open_pr(branch, token)
        if raced:
            raced_author = (raced.get("user") or {}).get("login", "")
            if raced_author != WORKER_LOGIN:
                raise OpenPrError(
                    f"#{raced.get('number')} already exists for {branch!r} but is authored by {raced_author!r}"
                )
            return {"number": raced.get("number"), "url": raced.get("html_url"),
                    "author": (raced.get("user") or {}).get("login", ""),
                    "auto_merge": _settle(token, raced.get("node_id"), arm), "reused": True}
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
            "auto_merge": _settle(token, pull.get("node_id"), arm), "reused": False}


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
