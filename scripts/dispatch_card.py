"""Turn a card into a pull request.

The lane's feeder decides *which* card is next; this module does the part that touches GitHub: take the card,
find the branch its worker used, and open (or reuse) the pull request as the worker identity.

    python3 scripts/dispatch_card.py --issue 794

Rule one applies here too: an unreadable issue, or a card whose branch does not exist, is a named failure and
stops the decision. Nothing is inferred from an empty read.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess

try:  # imported as scripts.dispatch_card by the tests, or run as a script
    from . import open_pr
except ImportError:  # pragma: no cover - the script path
    import open_pr

SLUG = "lunate-agent/lunate"


class CardError(RuntimeError):
    """The card cannot become a pull request, and the reason is named."""


def gh(*args: str, repo: pathlib.Path) -> str:
    """A gh read that refuses to answer with an empty string on failure."""
    done = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=str(repo), timeout=120)
    if done.returncode != 0:
        tail = ((done.stderr or "") + (done.stdout or "")).strip()[:200]
        raise CardError(f"gh {' '.join(args[:2])} failed: {tail}")
    return (done.stdout or "").strip()


def card(repo: pathlib.Path, number: int) -> dict:
    """The issue, or a named failure. Never an empty card."""
    raw = gh("issue", "view", str(number), "--json", "number,title,state,labels,milestone,body", repo=repo)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CardError(f"the issue #{number} could not be parsed: {exc}") from exc
    if not data.get("title"):
        raise CardError(f"issue #{number} came back with no title")
    if data.get("state") != "OPEN":
        raise CardError(f"issue #{number} is {data.get('state')}, not open — nothing to dispatch")
    return data


def branch_for(repo: pathlib.Path, number: int) -> str:
    """The branch a worker uses for this card, checked to exist locally."""
    name = f"wt/issue-{number}"
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True, text=True, cwd=str(repo), timeout=60,
    )
    if exists.returncode != 0:
        raise CardError(f"#{number} has no branch {name!r} in {repo}: the worker has not pushed its work")
    return name


def body_for(data: dict) -> str:
    """The pull request body: what closes, and the acceptance as the issue states it."""
    closes = f"Closes #{data['number']}"
    milestone = (data.get("milestone") or {}).get("title")
    head = f"{closes}" + (f" · milestone {milestone}" if milestone else "")
    return f"{head}\n\n{data.get('body') or '(the issue states no body)'}\n"


def dispatch(repo: pathlib.Path, number: int) -> dict:
    """Card to pull request. Raises rather than half-opening anything."""
    data = card(repo, number)
    branch = branch_for(repo, number)
    title = f"{data['title']} (#{number})"
    return open_pr.open_pr(repo, branch, title, body_for(data))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=str(pathlib.Path.home() / "dev" / "lunate"))
    parser.add_argument("--issue", type=int, required=True)
    args = parser.parse_args()
    try:
        result = dispatch(pathlib.Path(args.repo), args.issue)
    except (CardError, open_pr.OpenPrError) as exc:
        print(f"FAILED — {exc}")
        return 1
    reused = " (reused the open one)" if result["reused"] else ""
    print(f"#{args.issue} → #{result['number']} as {result['author']} — auto-merge {result['auto_merge']}{reused} — {result['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
