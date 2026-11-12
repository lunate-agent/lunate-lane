"""What every lane script shares: the config, named errors, gh/git/HTTP calls that raise, logs and locks.

Rule one lives here. Every read goes through `run`, `gh_json`, `graphql`, `git` or `rest`, and each of them
either returns what it read or raises a named `LaneError`. None of them turns a failure into an empty value.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterator

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = pathlib.Path(os.environ.get("LANE_CONFIG") or ROOT / "lane.json")
API = "https://api.github.com"


class LaneError(RuntimeError):
    """A named failure. It stops the decision it happened in. Never carries a token."""


class ConfigError(LaneError):
    """The config file is missing, unreadable or inconsistent."""


class GhError(LaneError):
    """A gh call failed, timed out or answered something unparseable."""


class GitError(LaneError):
    """A git call failed or timed out."""


class ApiError(LaneError):
    """A direct GitHub REST call (with an App token) was refused or unreachable."""


class StateError(LaneError):
    """A runtime state file under the state directory could not be read or written."""


class LockBusy(LaneError):
    """Another run of the same job holds its lock."""


@dataclasses.dataclass(frozen=True)
class Config:
    slug: str
    milestone: str
    base: str
    wip_limit: int
    worker_app: str
    reviewer_app: str
    worker_model: str
    reviewer_model: str
    bug_label: str
    area_prefix: str
    harness: dict
    checkout: pathlib.Path
    state: pathlib.Path
    app_config: pathlib.Path
    pins: pathlib.Path

    @property
    def owner(self) -> str:
        return self.slug.split("/")[0]

    @property
    def name(self) -> str:
        return self.slug.split("/")[1]


def _need(data: dict, *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ConfigError(f"the config has no {'.'.join(keys)}")
        value = value[key]
    return value


def load_config(path: pathlib.Path | None = None) -> Config:
    """The lane's one config file, validated. A missing key is a named failure, never a default."""
    path = path or CONFIG_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"the config {path} could not be read: {exc}") from exc
    expand = lambda *keys: pathlib.Path(str(_need(data, *keys))).expanduser()  # noqa: E731
    cfg = Config(
        slug=str(_need(data, "repo")), milestone=str(_need(data, "milestone")), base=str(_need(data, "base")),
        wip_limit=int(_need(data, "wip_limit")),
        worker_app=str(_need(data, "apps", "worker")), reviewer_app=str(_need(data, "apps", "reviewer")),
        worker_model=str(_need(data, "models", "worker")), reviewer_model=str(_need(data, "models", "reviewer")),
        bug_label=str(_need(data, "labels", "bug")), area_prefix=str(_need(data, "labels", "area_prefix")),
        harness=dict(_need(data, "harness")),
        checkout=expand("paths", "checkout"), state=expand("paths", "state"),
        app_config=expand("paths", "app_config"), pins=expand("paths", "pins"),
    )
    if cfg.slug.count("/") != 1:
        raise ConfigError(f"repo must be owner/name, not {cfg.slug!r}")
    if cfg.wip_limit < 1:
        raise ConfigError("wip_limit must be at least 1")
    if cfg.worker_model == cfg.reviewer_model:
        raise ConfigError("the reviewer model must differ from the worker model")
    for key in ("binary", "credential_file", "credential_key", "credential_env", "session_timeout", "stall_idle"):
        _need(data, "harness", key)
    return cfg


_CONFIG: Config | None = None


def config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def run(argv: list[str], *, error: type[LaneError] = LaneError, cwd: pathlib.Path | None = None,
        timeout: int = 120, env: dict | None = None, stdin: str | None = None) -> str:
    """Run a command and return its stdout, or raise `error` naming what failed."""
    what = " ".join(argv[:3])
    try:
        done = subprocess.run(argv, capture_output=True, text=True, cwd=str(cwd) if cwd else None,
                              timeout=timeout, env=env, input=stdin)
    except OSError as exc:
        raise error(f"{argv[0]} could not be run ({exc.__class__.__name__}: {exc})") from exc
    except subprocess.SubprocessError as exc:
        raise error(f"{what} did not finish ({exc.__class__.__name__})") from exc
    if done.returncode != 0:
        tail = ((done.stderr or "") + (done.stdout or "")).strip()[:300]
        raise error(f"{what} failed ({done.returncode}): {tail or 'no output'}")
    return done.stdout or ""


def gh(*args: str, timeout: int = 120) -> str:
    return run(["gh", *args], error=GhError, timeout=timeout)


def gh_json(*args: str, timeout: int = 120) -> Any:
    raw = gh(*args, timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhError(f"gh {' '.join(args[:2])} answered something that is not JSON: {exc}") from exc


def graphql(query: str, **variables: Any) -> dict:
    """A GraphQL read through gh. Strings go as -f, everything else as -F; errors in the answer raise."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-f" if isinstance(value, str) else "-F", f"{key}={value}"]
    payload = gh_json(*args)
    if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
        raise GhError(f"the GraphQL read failed: {str(payload.get('errors') if isinstance(payload, dict) else payload)[:200]}")
    return payload["data"]


def git(repo: pathlib.Path, *args: str, timeout: int = 300, env: dict | None = None) -> str:
    return run(["git", "-C", str(repo), *args], error=GitError, timeout=timeout, env=env).strip()


def rest(token: str, method: str, path: str, payload: dict | None = None) -> Any:
    """A GitHub REST call with an App token. The token is in a header only, never in a message."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=data, method=method, headers={
        "Authorization": f"token {token}", "Accept": "application/vnd.github+json", "User-Agent": "lunate-lane"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200].replace(token, "[REDACTED]")
        raise ApiError(f"GitHub refused {method} {path} ({exc.code}): {detail}") from exc
    except Exception as exc:
        raise ApiError(f"GitHub could not be reached for {method} {path} ({exc.__class__.__name__})") from exc


def render(template: str, **values: str) -> str:
    """Fill `{{KEY}}` placeholders in templates/<template> in one pass, so inserted text is never re-read."""
    path = ROOT / "templates" / template
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"the template {path} could not be read: {exc}") from exc

    def fill(match: "re.Match[str]") -> str:
        if match.group(1) not in values:
            raise ConfigError(f"the template {template} needs {match.group(1)}, which was not given")
        return str(values[match.group(1)])

    return re.sub(r"\{\{([A-Z_]+)\}\}", fill, text)


def state_dir(*parts: str) -> pathlib.Path:
    path = config().state.joinpath(*parts)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StateError(f"the state directory {path} could not be created: {exc}") from exc
    return path


def log(job: str, message: str) -> None:
    """One timestamped line to the job's log under the state directory, and to stdout for cron."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {job}: {message}"
    print(line, flush=True)
    path = state_dir("logs") / f"{job}.log"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        raise StateError(f"the log {path} could not be written: {exc}") from exc


@contextlib.contextmanager
def job_lock(job: str) -> Iterator[None]:
    """An exclusive, non-blocking flock per job. Released by the kernel even if the process dies."""
    path = state_dir("locks") / f"{job}.lock"
    try:
        handle = path.open("a")
    except OSError as exc:
        raise StateError(f"the lock {path} could not be opened: {exc}") from exc
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy(f"another {job} run holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_job(job: str, body: Callable[[], None]) -> int:
    """A cron entry point: take the job's lock, run, and turn a named failure into a logged non-zero exit."""
    try:
        with job_lock(job):
            body()
    except LockBusy as exc:
        _last_word(job, f"skipped — {exc}")
        return 0
    except LaneError as exc:
        _last_word(job, f"FAILED — {exc}")
        return 1
    return 0


def _last_word(job: str, message: str) -> None:
    """Log the job's outcome; if even the log cannot be written, say both on stderr rather than a traceback."""
    try:
        log(job, message)
    except LaneError as exc:
        print(f"{job}: {message} (and the log could not be written: {exc})", file=sys.stderr, flush=True)
