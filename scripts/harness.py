"""Run one headless Pi session, and know which worker sessions are running.

Vendored from the old lane's `worker_harness.py` (the Pi adapter, its minimal environment and the leaked
tool-markup check) and `lunate-claims.py` (a pid file per running worker), cut down to what this lane uses.

A session gets a minimal environment: PATH (with the pinned .NET SDK first when it exists), HOME, a private
TMPDIR, and the model credential. It gets no GitHub credential: `gh` points at an empty config directory and
git's credential helpers are blanked, so a session cannot push or open a pull request — the lane does that,
as the worker identity, after reading what the session left behind.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import signal
import subprocess
import time
from typing import Callable

from scripts import common
from scripts.common import LaneError, StateError

DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TOOL_MARKUP = ("</arg_value>", "</arg_key>", "<arg_value>", "<arg_key>")


class HarnessError(LaneError):
    """The session could not be started, or its output could not be read."""


@dataclasses.dataclass(frozen=True)
class Outcome:
    rc: int | None
    stalled: bool
    elapsed: int
    leaked_markup: bool


def read_credential(cfg: common.Config) -> str:
    """The model credential, from the dotenv file the config names. Its value is never reported."""
    path = pathlib.Path(cfg.harness["credential_file"]).expanduser()
    key = cfg.harness["credential_key"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise HarnessError(f"the credential file {path} could not be read ({exc.__class__.__name__})") from exc
    for line in lines:
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value
    raise HarnessError(f"{key} is not set in {path}")


def session_env(cfg: common.Config, scratch: pathlib.Path, extra: dict | None = None) -> dict:
    """The whole environment a session gets — nothing inherited beyond PATH."""
    nogh = scratch / "gh-config"
    nogh.mkdir(parents=True, exist_ok=True)
    path = os.environ.get("PATH") or DEFAULT_PATH
    env = {"PATH": path, "HOME": str(pathlib.Path.home()), "TMPDIR": str(scratch),
           "GH_CONFIG_DIR": str(nogh), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
           cfg.harness["credential_env"]: read_credential(cfg)}
    dotnet = pathlib.Path.home() / ".dotnet"
    if (dotnet / "dotnet").exists():
        env["PATH"] = f"{dotnet}{os.pathsep}{path}"
        env["DOTNET_ROOT"] = str(dotnet)
    env.update(extra or {})
    return env


def argv(cfg: common.Config, model: str, session_dir: pathlib.Path, session_id: str, prompt: str) -> list:
    provider, slash, model_id = model.partition("/")
    if not slash:
        raise HarnessError(f"model {model!r} must be provider/model")
    return [cfg.harness["binary"], "--print", "--mode", "json", "--provider", provider, "--model", model_id,
            "--session-dir", str(session_dir), "--session-id", session_id,
            "--approve", "--no-extensions", "--no-mcp", "--", prompt]


def run_session(cfg: common.Config, *, model: str, worktree: pathlib.Path, prompt: str, scratch: pathlib.Path,
                log_path: pathlib.Path, extra_env: dict | None = None, on_tick: Callable[[], None] | None = None,
                tick: float = 30.0) -> Outcome:
    """Run Pi to completion in `worktree`, killing it when its event stream stalls or it passes the hard cap."""
    timeout, idle = int(cfg.harness["session_timeout"]), int(cfg.harness["stall_idle"])
    session_id = f"{worktree.name}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    command = argv(cfg, model, scratch / "sessions", session_id, prompt)
    env = session_env(cfg, scratch, extra_env)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as out:
        try:
            proc = subprocess.Popen(command, cwd=str(worktree), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise HarnessError(f"{command[0]} could not be started ({exc.__class__.__name__}: {exc})") from exc
        started = last_activity = time.monotonic()
        size, stalled = -1, False
        try:
            while proc.poll() is None:
                time.sleep(tick)
                now = time.monotonic()
                current = log_path.stat().st_size
                if current != size:
                    size, last_activity = current, now
                if now - started > timeout or now - last_activity > idle:
                    stalled = True
                    _kill_group(proc)
                    break
                if on_tick is not None:
                    on_tick()
        except BaseException:
            _kill_group(proc)  # never leave a session running behind a driver that is going away
            raise
    return Outcome(proc.returncode, stalled, int(time.monotonic() - started), leaked_markup(log_path))


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=20)
            return
        except subprocess.TimeoutExpired:
            continue


def last_text(log_path: pathlib.Path) -> str:
    """The last assistant text in a Pi JSON event log. An unreadable log raises; no message is ''."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise HarnessError(f"the session log {log_path} could not be read ({exc.__class__.__name__})") from exc
    last = ""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "message_end":
            for part in (event.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    last = part.get("text") or ""
    return last


# Shapes of things that must never be posted. Checked in order; the first match names the kind.
SECRET_SHAPES = (
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("credential assignment", re.compile(
        r"(?i)\b[\w-]*(?:api[_-]?key|token|secret|passw(?:or)?d)[\w-]*[\"']?\s*[:=]\s*[\"']?[^\s\"']{12,}")),
)
_OPAQUE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")


def _opaque(run: str) -> bool:
    """A long run that mixes upper case, lower case and digits — key-shaped. Hex (commit SHAs) and words are not."""
    return any(c.isupper() for c in run) and any(c.islower() for c in run) and any(c.isdigit() for c in run)


def secret_kind(text: str, known: tuple = ()) -> str | None:
    """The kind of token- or key-shaped thing in `text`, or None. Never returns the match itself."""
    for value in known:
        if value and value in text:
            return "configured credential"
    for kind, pattern in SECRET_SHAPES:
        if pattern.search(text):
            return kind
    if any(_opaque(match.group(0)) for match in _OPAQUE.finditer(text)):
        return "opaque key-like string"
    return None


def leaked_markup(log_path: pathlib.Path) -> bool:
    """The session ended with tool-call markup as prose: rc=0, but the work was not done."""
    return any(mark in last_text(log_path) for mark in TOOL_MARKUP)


# --- running workers: one pid file per card under <state>/workers ---

def _record(card: int) -> pathlib.Path:
    return common.state_dir("workers") / f"{int(card)}.json"


def record_worker(card: int, pid: int, areas: frozenset, rework_pr: int | None) -> None:
    path = _record(card)
    tmp = path.with_suffix(".tmp")
    data = {"card": card, "pid": pid, "areas": sorted(areas), "rework_pr": rework_pr, "started": time.time()}
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError as exc:
        raise StateError(f"the worker record {path} could not be written: {exc}") from exc


def clear_worker(card: int, pid: int) -> None:
    """Remove the card's record, but only if it is this process's own."""
    path = _record(card)
    try:
        if path.exists() and json.loads(path.read_text()).get("pid") == pid:
            path.unlink()
    except (OSError, ValueError) as exc:
        raise StateError(f"the worker record {path} could not be cleared: {exc}") from exc


def worker_alive(pid: int, card: int) -> bool:
    """The pid is alive and is the worker driver for this card (a recycled pid is not)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        done = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StateError(f"ps could not be run to check worker pid {pid} ({exc.__class__.__name__})") from exc
    if done.returncode == 1 and not done.stdout.strip():
        return False  # the process exited between the two checks
    if done.returncode != 0:
        raise StateError(f"ps failed ({done.returncode}) checking worker pid {pid}: {done.stderr.strip()[:120]}")
    return "worker.py" in done.stdout and re.search(rf"--issue {int(card)}(\s|$)", done.stdout) is not None


def _read_record(path: pathlib.Path) -> tuple:
    try:
        data = json.loads(path.read_text())
        return int(data["card"]), int(data["pid"]), frozenset(data["areas"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError(f"the worker record {path} could not be read: {exc}") from exc


def live_pid(card: int) -> int | None:
    """The pid of the live worker for this card, or None. A dead record is removed; an unreadable one raises."""
    path = _record(card)
    if not path.exists():
        return None
    _, pid, _ = _read_record(path)
    if worker_alive(pid, card):
        return pid
    path.unlink(missing_ok=True)
    return None


def running_workers() -> dict:
    """card -> areas for every live worker. A record that cannot be read raises; a dead one is removed."""
    found = {}
    for path in sorted(common.state_dir("workers").glob("*.json")):
        card, pid, areas = _read_record(path)
        if worker_alive(pid, card):
            found[card] = areas
        else:
            path.unlink(missing_ok=True)
    return found
