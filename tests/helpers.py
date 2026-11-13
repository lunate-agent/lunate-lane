"""Shared test fixtures: a config whose state lives in a temporary directory, and small builders."""

from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import common, github  # noqa: E402

REQUIRED = frozenset({"ci-ok"})
GREEN = (("ci-ok", "COMPLETED", "SUCCESS"),)


class FakeIssues:
    """GitHub's issues and pull requests as `scripts.parked` sees them: labels, assignees, state, blocked-by.
    Installed by every TempState, so no test reaches GitHub through a park. `fail` names path fragments refused;
    `drop_assignee` makes GitHub accept an assignment and not keep it (as it does for a user without access)."""

    def __init__(self) -> None:
        self.issues: dict = {}
        self.labels_made: list = []
        self.pulls_of: dict = {}  # card -> {open pull request numbers}
        self.pr_card: dict = {}  # pull request -> its card
        self.blocked: dict = {}  # card -> {blocker numbers}
        self.fail: set = set()
        self.drop_assignee = False
        self.calls: list = []

    def issue(self, number: int, **over) -> dict:
        n = int(number)
        if n not in self.issues:
            self.issues[n] = {"number": n, "id": 10_000 + n, "state": "open", "labels": set(), "assignees": set()}
        self.issues[n].update(over)
        return self.issues[n]

    def _shape(self, n: int) -> dict:
        i = self.issue(n)
        out = {"number": n, "id": i["id"], "state": i["state"], "labels": [{"name": x} for x in sorted(i["labels"])],
               "assignees": [{"login": x} for x in sorted(i["assignees"])]}
        if "pull_request" in i:
            out["pull_request"] = i["pull_request"]
        return out

    def call(self, token, method, path, payload=None):
        from scripts import open_pr
        self.calls.append((method, path, payload))
        if any(f in path for f in self.fail):
            raise open_pr.OpenPrError(f"GitHub refused {method} {path} (403): forbidden")
        parts = path.strip("/").split("/")  # repos/o/r/...
        rest = parts[3:]
        if rest == ["labels"]:
            if payload["name"] in self.labels_made:
                raise open_pr.OpenPrError(f"GitHub refused POST {path} (422): already_exists")
            self.labels_made.append(payload["name"])
            return {}
        n, tail = int(rest[1]), rest[2:]
        i = self.issue(n)
        if method == "GET" and not tail:
            return self._shape(n)
        if tail == ["labels"] and method == "POST":
            i["labels"] |= set(payload["labels"])
        elif tail[:1] == ["labels"] and method == "DELETE":
            import urllib.parse
            i["labels"].discard(urllib.parse.unquote(tail[1]))
        elif tail == ["assignees"] and method == "POST":
            if not self.drop_assignee:
                i["assignees"] |= set(payload["assignees"])
        elif tail == ["assignees"] and method == "DELETE":
            i["assignees"] -= set(payload["assignees"])
        elif tail == ["dependencies", "blocked_by"] and method == "POST":
            blocker = next(k for k, v in self.issues.items() if v["id"] == payload["issue_id"])
            self.blocked.setdefault(n, set()).add(blocker)
        else:
            raise AssertionError(f"FakeIssues: unexpected {method} {path}")
        return {}

    def get(self, path):
        return self.call("", "GET", "/" + path)

    def install(self) -> list:
        from unittest import mock
        from scripts import parked
        return [mock.patch.object(parked, "_token", return_value="t"), mock.patch.object(parked, "_call", self.call),
                mock.patch.object(parked, "_get", self.get),
                mock.patch.object(parked, "_card_pulls", lambda cfg, card: set(self.pulls_of.get(card, ()))),
                mock.patch.object(parked, "_blockers", lambda cfg, card: tuple(sorted(
                    b for b in self.blocked.get(card, ()) if self.issue(b)["state"] == "open"))),
                mock.patch.object(parked, "_pr_card", lambda cfg, n: self.pr_card.get(n))]


class TempState:
    """Point the lane's config at a temporary state directory for the duration of a test, with a FakeIssues
    standing in for GitHub wherever a park labels, assigns or links (`self.github`)."""

    def __init__(self, **over) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="lane-state-"))
        self.cfg = dataclasses.replace(common.load_config(), state=self.root / "state", checkout=self.root / "checkout",
                                       pins=self.root / "pins.json", app_config=self.root / "config" / "app.json",
                                       **over)
        self.saved = common._CONFIG
        common._CONFIG = self.cfg
        self.github = FakeIssues()
        self._fakes = self.github.install()
        for patch in self._fakes:
            patch.start()

    def close(self) -> None:
        import shutil

        common._CONFIG = self.saved
        for patch in reversed(self._fakes):
            patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)


def card(number: int, *labels: str, created: str | None = None, blocked_by=(), blocking=()) -> github.Card:
    return github.Card(number=number, title=f"card {number}", labels=frozenset(labels),
                       created=created or f"2026-11-{number % 28 + 1:02d}T00:00:00Z",
                       blocked_by=tuple(blocked_by), blocking=tuple(blocking))


def pull(number: int, card_number: int | None = None, *, author: str = "lunate-worker", head: str = "h1",
         reviews=(), labels=(), checks=GREEN, **over) -> github.Pull:
    branch = f"wt/issue-{card_number}" if card_number is not None else f"feature/{number}"
    values = dict(number=number, author=author, branch=branch, head=head, reviews=tuple(reviews),
                  labels=frozenset(labels), checks=tuple(checks), title=f"pr {number}",
                  created="2026-11-12T00:00:00Z", files=(), mergeable="MERGEABLE")
    values.update(over)
    return github.Pull(**values)


def review(state: str, commit: str = "h1", author: str = "lunate-reviewer", body: str = "findings") -> github.Review:
    return github.Review(author=author, state=state, commit=commit, body=body)


def git(repo: pathlib.Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


def origin_and_clone(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A bare origin with one commit on main, and a clone of it — the shape of ~/dev/lunate."""
    seed, origin, clone = root / "seed", root / "origin.git", root / "checkout"
    seed.mkdir(parents=True)
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.email", "lane@example.invalid")
    git(seed, "config", "user.name", "lane test")
    (seed / "README").write_text("seed\n")
    git(seed, "add", "README")
    git(seed, "commit", "-q", "-m", "seed")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    git(clone, "config", "user.email", "lane@example.invalid")
    git(clone, "config", "user.name", "lane test")
    return origin, clone
