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


class TempState:
    """Point the lane's config at a temporary state directory for the duration of a test."""

    def __init__(self, **over) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="lane-state-"))
        self.cfg = dataclasses.replace(common.load_config(), state=self.root / "state", checkout=self.root / "checkout",
                                       pins=self.root / "pins.json", app_config=self.root / "config" / "app.json",
                                       **over)
        self.saved = common._CONFIG
        common._CONFIG = self.cfg

    def close(self) -> None:
        import shutil

        common._CONFIG = self.saved
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
