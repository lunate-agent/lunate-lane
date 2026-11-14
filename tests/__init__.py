"""No test reaches GitHub. Every module under tests/ imports this package first, so before any test runs:

- an HTTP call to api.github.com (an App token mint, a REST or GraphQL call — all `urllib.request.urlopen`) and
- any `gh` command (`subprocess.run(["gh", ...])`, which `common.gh` and `dispatch_card` use)

fail loudly instead of reaching the real repository. A test stands in for GitHub with a mock; a mock that
patches `urllib.request.urlopen` or `common.gh` replaces this guard for its own duration. Only the explicitly
live identity check (`tests/test_identity.py`, a token mint and one read) opts out, with `live_github()`.

Until 2026-11-14 a test that parked a card minted a real worker token (`identity.CONFIG` was read from the live
config at import, so a TempState did not redirect it) and posted real "card is parked" comments on lunate #1
and #2, and labelled and assigned closed #1, #2, #9, #50 and #780, on every suite run since 2026-11-13 21:02.
"""

import contextlib
import urllib.request

_ALLOWED = {"live": False}
_real_urlopen = urllib.request.urlopen


class GitHubReached(BaseException):  # not an Exception: the lane's `except Exception` must not swallow it
    pass


def _guarded_urlopen(request, *args, **kwargs):
    url = getattr(request, "full_url", None) or str(request)
    if "api.github.com" in url and not _ALLOWED["live"]:
        method = getattr(request, "get_method", lambda: "GET")()
        raise GitHubReached(f"a test reached GitHub: {method} {url} — stand in for it with a mock")
    return _real_urlopen(request, *args, **kwargs)


urllib.request.urlopen = _guarded_urlopen


import subprocess

_real_run = subprocess.run


def _is_gh(argv) -> bool:
    if isinstance(argv, (list, tuple)):
        return bool(argv) and str(argv[0]) == "gh"  # the lane always runs it by name, from PATH
    return str(argv).lstrip().startswith("gh ")


def _guarded_run(argv, *args, **kwargs):
    if _is_gh(argv) and not _ALLOWED["live"]:
        shown = " ".join(map(str, argv[1:3])) if isinstance(argv, (list, tuple)) else str(argv)[:40]
        raise GitHubReached(f"a test ran `gh {shown} …` — stand in for it with a mock")
    return _real_run(argv, *args, **kwargs)


subprocess.run = _guarded_run


@contextlib.contextmanager
def live_github():
    """For the one deliberately live test: let it reach GitHub for its duration."""
    _ALLOWED["live"] = True
    try:
        yield
    finally:
        _ALLOWED["live"] = False
