"""Shapes of secrets that must never leave the lane host: in a posted report, a pushed diff, a title or a body.

No lane imports, so `open_pr` (also run as a script) and `harness` can both use it.
"""

from __future__ import annotations

import re

# Shapes of things that must never be posted. Checked in order; the first match names the kind.
SECRET_SHAPES = (
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("OpenCode key", re.compile(r"\boc_[A-Za-z0-9_-]{8,}")),  # the model key's prefix: a fragment is enough
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("credential assignment", re.compile(
        r"(?i)\b[\w-]*(?:api[_-]?key|token|secret|passw(?:or)?d)[\w-]*[\"']?\s*[:=]\s*[\"']?[^\s\"']{12,}")),
)
LOOSE = frozenset({"credential assignment"})
_OPAQUE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


def _opaque(run: str) -> bool:
    """A long run that mixes upper case, lower case and digits — key-shaped. Hex (commit SHAs) and words are not."""
    return any(c.isupper() for c in run) and any(c.islower() for c in run) and any(c.isdigit() for c in run)


def secret_kind(text: str, known: tuple = (), strict: bool = False) -> str | None:
    """The kind of token- or key-shaped thing in `text`, or None. Never returns the match itself.

    `strict` keeps to the shapes that are secrets by construction (and the known values): the credential-
    assignment and opaque-string heuristics would fire on ordinary code (`cancellationToken = …`, lockfile
    hashes), and a push scan that parks a card must not.
    """
    for value in known:
        if value and value in text:
            return "configured credential"
    for kind, pattern in SECRET_SHAPES:
        if strict and kind in LOOSE:
            continue
        if pattern.search(text):
            return kind
    if not strict and any(_opaque(match.group(0)) for match in _OPAQUE.finditer(text)):
        return "opaque key-like string"
    return None
