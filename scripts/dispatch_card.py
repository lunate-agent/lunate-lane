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
    from . import common, open_pr
except ImportError:  # pragma: no cover - the script path
    import common
    import open_pr

SLUG = common.config().slug


class CardError(common.LaneError):
    """The card cannot become a pull request, and the reason is named."""


def gh(*args: str, repo: pathlib.Path) -> str:
    """A gh read that refuses to answer with an empty string on failure."""
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=str(repo), timeout=120)
    except OSError as exc:
        raise CardError(f"gh could not be run ({exc.__class__.__name__}: {exc})") from exc
    except subprocess.SubprocessError as exc:
        raise CardError(f"gh {' '.join(args[:2])} did not finish ({exc.__class__.__name__})") from exc
    if done.returncode != 0:
        tail = ((done.stderr or "") + (done.stdout or "")).strip()[:200]
        raise CardError(f"gh {' '.join(args[:2])} failed: {tail}")
    return (done.stdout or "").strip()


def card(repo: pathlib.Path, number: int) -> dict:
    """The issue, or a named failure. Never an empty card."""
    raw = gh("issue", "view", str(number), "-R", SLUG,
             "--json", "number,title,state,milestone,labels,body", repo=repo)
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
    try:
        exists = subprocess.run(
            common.git_argv(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"),
            capture_output=True, text=True, timeout=60, env=common.git_env(),
        )
    except OSError as exc:
        raise CardError(f"git could not be run in {repo} ({exc.__class__.__name__})") from exc
    except subprocess.SubprocessError as exc:
        raise CardError(f"the branch check did not finish ({exc.__class__.__name__})") from exc
    if exists.returncode != 0:
        raise CardError(f"#{number} has no local branch {name!r} in {repo}")
    ahead = _count(repo, name)
    if ahead == 0:
        raise CardError(f"#{number}'s branch {name!r} has no commits ahead of {BASE} — nothing to open")
    return name


BASE = f"origin/{common.config().base}"


def _count(repo: pathlib.Path, name: str) -> int:
    """How many commits `name` is ahead of the base. An unreadable answer is a refusal, not a zero.

    Rule one: if git cannot say, this does not decide. A missing base ref makes `rev-list` fail and print
    nothing, which would otherwise read as "nothing to open" — the right answer is that we do not know.
    """
    try:
        done = subprocess.run(
            common.git_argv(repo, "rev-list", "--count", f"{BASE}..{name}"),
            capture_output=True, text=True, timeout=60, env=common.git_env(),
        )
    except OSError as exc:
        raise CardError(f"git could not be run in {repo} ({exc.__class__.__name__})") from exc
    except subprocess.SubprocessError as exc:
        raise CardError(f"counting commits ahead of {BASE} did not finish ({exc.__class__.__name__})") from exc
    if done.returncode != 0:
        tail = ((done.stderr or "") + (done.stdout or "")).strip()[:160]
        raise CardError(f"git could not count commits ahead of {BASE} in {repo}: {tail or 'no output'}")
    raw = (done.stdout or "").strip()
    if not raw.isdigit():
        raise CardError(f"git answered {raw!r} when counting commits ahead of {BASE} — refusing to decide")
    return int(raw)


def body_for(data: dict) -> str:
    """The pull request body: the closing keyword and a link to the card, nothing else. The card is the one copy of
    its text: a copy here goes stale when the card is edited, and copied closing keywords would close other issues.
    """
    number = int(data["number"])
    return f"Closes #{number}\n\nCard: https://github.com/{SLUG}/issues/{number}\n"


def dispatch(repo: pathlib.Path, number: int, arm: bool = True, known: tuple = ()) -> dict:
    """Card to pull request. Raises rather than half-opening anything. `arm=False` leaves auto-merge off; `known`
    are values the pre-push secret scan must catch."""
    branch = branch_for(repo, number)
    data = card(repo, number)
    title = data["title"]
    return open_pr.open_pr(repo, branch, title, body_for(data), common.config().base, arm=arm, known=known)


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
