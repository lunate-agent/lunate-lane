"""Run one headless Pi session, and know which worker sessions are running.

Vendored from the old lane's `worker_harness.py` (the Pi adapter, its minimal environment and the leaked
tool-markup check) and `lunate-claims.py` (a pid file per running worker), cut down to what this lane uses.

A session gets a minimal environment: PATH (with the pinned .NET SDK first when it exists), a HOME of its own
under its scratch directory (seeded with only what Pi needs), a private TMPDIR, and the model credential. It is
handed no GitHub credential: `gh` points at an empty config directory and git's credential helpers are blanked,
so the lane, not the session, pushes and opens pull requests as the worker identity. This is not a sandbox: the
session runs as the same macOS user and could still read files outside its HOME by absolute path (README,
"Known limitations").
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import time
from typing import Callable

from scripts import common
from scripts.common import LaneError, StateError

DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TOOL_MARKUP = ("</arg_value>", "</arg_key>", "<arg_value>", "<arg_key>")
# What Pi needs from the real home, copied into the session's own. Not auth.json: the credential is in the env.
HOME_SEED = (".pi/agent/settings.json", ".pi/agent/models-store.json", ".pi/agent/bin/fd")


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


def _git_identity(cfg: common.Config) -> dict:
    """user.name and user.email as the checkout sees them. Unset (git's exit 1) is left out; a failure raises."""
    found = {}
    for key in ("user.name", "user.email"):
        try:
            done = subprocess.run(["git", "-C", str(cfg.checkout), "config", "--get", key],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError(f"git could not read {key} in {cfg.checkout} ({exc.__class__.__name__})") from exc
        if done.returncode not in (0, 1):
            raise HarnessError(f"git could not read {key} in {cfg.checkout}: {done.stderr.strip()[:160]}")
        if done.returncode == 0 and done.stdout.strip():
            found[key.split(".")[1]] = done.stdout.strip()
    return found


def session_home(cfg: common.Config, scratch: pathlib.Path, real_home: pathlib.Path | None = None) -> pathlib.Path:
    """A fresh HOME for one session: Pi's settings, its model cache and its `fd`, and a git identity — no more.

    Wiped and reseeded each run, so nothing one session leaves there reaches the next. The real home's dotfiles,
    `~/.config` (the App keys' config), `~/.ssh` and Pi's `auth.json` are not in it.
    """
    real_home = real_home or pathlib.Path.home()
    home = scratch / "home"
    try:
        if home.exists():
            shutil.rmtree(home)
        home.mkdir(parents=True)
        for relative in HOME_SEED:
            source = real_home / relative
            if source.is_file():
                (home / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, home / relative)
    except OSError as exc:
        raise HarnessError(f"the session home {home} could not be prepared ({exc.__class__.__name__}: {exc})") from exc
    identity = _git_identity(cfg)
    if identity:
        lines = ["[user]"] + [f"\t{key} = {value}" for key, value in identity.items()]
        (home / ".gitconfig").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return home


def session_env(cfg: common.Config, scratch: pathlib.Path, extra: dict | None = None,
                real_home: pathlib.Path | None = None) -> dict:
    """The whole environment a session gets — nothing inherited beyond PATH, and a HOME of its own."""
    real_home = real_home or pathlib.Path.home()
    nogh = scratch / "gh-config"
    nogh.mkdir(parents=True, exist_ok=True)
    path = os.environ.get("PATH") or DEFAULT_PATH
    env = {"PATH": path, "HOME": str(session_home(cfg, scratch, real_home)), "TMPDIR": str(scratch),
           "GH_CONFIG_DIR": str(nogh), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
           cfg.harness["credential_env"]: read_credential(cfg)}
    dotnet = real_home / ".dotnet"
    if (dotnet / "dotnet").exists():
        env["PATH"] = f"{dotnet}{os.pathsep}{path}"
        env["DOTNET_ROOT"] = str(dotnet)
    packages = real_home / ".nuget" / "packages"
    if packages.is_dir():
        env["NUGET_PACKAGES"] = str(packages)  # a package cache, no credentials: spares a full restore per run
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
                on_start: Callable[[int, str], None] | None = None, tick: float = 30.0) -> Outcome:
    """Run Pi to completion in `worktree`, killing it when its event stream stalls or it passes the hard cap.

    Pi runs in a process group of its own; `on_start` gets that group and the session id, so a record can name
    exactly this session for clean-up if this
    driver is killed outright. Any exception here — including the SystemExit `exit_on_signals` raises on
    SIGTERM — kills that group before it propagates.
    """
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
            if on_start is not None:
                on_start(proc.pid, session_id)
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


def exit_on_signals() -> None:
    """SIGTERM, SIGHUP and SIGINT become SystemExit, so a killed driver still runs its clean-up — the session's
    group is killed (run_session) and its record cleared — instead of leaving Pi running behind it."""
    def leave(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, leave)


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
    ("OpenCode key", re.compile(r"\boc_[A-Za-z0-9_-]{8,}")),  # the model key's prefix: a fragment is enough
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
    _write_record(_record(card), {"card": card, "pid": pid, "areas": sorted(areas), "rework_pr": rework_pr,
                                  "started": time.time()})


def _write_record(path: pathlib.Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError as exc:
        raise StateError(f"the worker record {path} could not be written: {exc}") from exc


def note_session(card: int, pid: int, session_pgid: int, session_id: str) -> None:
    """Add the running session's process group to this driver's own record (another driver's is left alone)."""
    path = _record(card)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise StateError(f"the worker record {path} could not be read: {exc}") from exc
    if data.get("pid") != pid:
        raise StateError(f"the worker record {path} belongs to pid {data.get('pid')}, not {pid}")
    data["session_pgid"] = int(session_pgid)
    data["session_id"] = str(session_id)
    _write_record(path, data)


def _command(pid: int) -> str | None:
    """The command line of a live pid, or None if it is gone. ps failing for another reason raises."""
    try:
        done = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise StateError(f"ps could not be run to check pid {pid} ({exc.__class__.__name__})") from exc
    if done.returncode == 1 and not done.stdout.strip():
        return None
    if done.returncode != 0:
        raise StateError(f"ps failed ({done.returncode}) checking pid {pid}: {done.stderr.strip()[:120]}")
    return done.stdout.strip()


def reap_session(card: int, session: tuple | None, grace: float = 10.0) -> bool:
    """Kill the Pi session a dead driver left behind. `session` is the recorded (process group, session id); only
    a group whose leader still runs with exactly that session id is touched — a recycled pid, or another session
    of the same card, is not. A record without both is never acted on. Returns whether a session was killed."""
    if not session:
        return False
    session_pgid, session_id = session
    command = _command(session_pgid)
    if command is None or not re.search(rf"--session-id {re.escape(session_id)}(\s|$)", command):
        return False
    try:
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, grace)):
            try:
                os.killpg(session_pgid, sig)
            except ProcessLookupError:
                return True
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                try:
                    os.killpg(session_pgid, 0)
                except ProcessLookupError:
                    return True
                time.sleep(0.1)
    except PermissionError as exc:
        raise StateError(f"the orphaned session of card #{card} (group {session_pgid}) could not be signalled: "
                         f"{exc}") from exc
    raise StateError(f"the orphaned session of card #{card} (group {session_pgid}) survived SIGKILL")


def _clear_dead(path: pathlib.Path, card: int, session: tuple | None) -> None:
    reap_session(card, session)
    path.unlink(missing_ok=True)


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
        pgid, session_id = data.get("session_pgid"), data.get("session_id")
        session = (int(pgid), str(session_id)) if pgid and session_id else None
        return int(data["card"]), int(data["pid"]), frozenset(data["areas"]), session
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StateError(f"the worker record {path} could not be read: {exc}") from exc


def live_pid(card: int) -> int | None:
    """The pid of the live worker for this card, or None. A dead record is removed, after killing any session
    its driver left running; an unreadable one raises."""
    path = _record(card)
    if not path.exists():
        return None
    _, pid, _, session = _read_record(path)
    if worker_alive(pid, card):
        return pid
    _clear_dead(path, card, session)
    return None


def _listing(folder: pathlib.Path) -> list[pathlib.Path]:
    """The `*.json` records in a state folder. A folder that cannot be listed is a failure, never 'no records'."""
    try:
        with os.scandir(folder) as entries:
            return sorted(pathlib.Path(e.path) for e in entries if e.name.endswith(".json") and e.is_file())
    except OSError as exc:
        raise StateError(f"the state folder {folder} could not be listed: {exc}") from exc


def running_workers() -> dict:
    """card -> areas for every live worker. A record that cannot be read raises; a dead one is removed, and the
    session its driver left running is killed first."""
    found = {}
    for path in _listing(common.state_dir("workers")):
        card, pid, areas, session = _read_record(path)
        if worker_alive(pid, card):
            found[card] = areas
        else:
            _clear_dead(path, card, session)
    return found


# --- idle runs: a card whose runs leave no new commit is parked after IDLE_RUN_LIMIT of them ---

IDLE_RUN_LIMIT = 3


def _attempts(card: int) -> pathlib.Path:
    return common.state_dir("attempts") / f"{int(card)}.json"


def _read_attempts(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"idle_runs": 0, "parked": False}
    try:
        data = json.loads(path.read_text())
        return {"idle_runs": int(data["idle_runs"]), "parked": bool(data["parked"])}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError(f"the attempts record {path} could not be read: {exc}") from exc


def note_run(card: int, new_commit: bool) -> tuple[int, bool]:
    """Count a finished run: a new commit resets the count, none adds one. Returns (idle runs, parked just now)."""
    path = _attempts(card)
    data = _read_attempts(path)
    if new_commit:
        path.unlink(missing_ok=True)
        return 0, False
    idle = data["idle_runs"] + 1
    parked = idle >= IDLE_RUN_LIMIT
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"idle_runs": idle, "parked": parked, "updated": time.time()}))
        os.replace(tmp, path)
    except OSError as exc:
        raise StateError(f"the attempts record {path} could not be written: {exc}") from exc
    return idle, parked and not data["parked"]


def parked_cards() -> frozenset:
    """Cards the feeder must not start again until a person deletes their attempts record."""
    found = set()
    for path in _listing(common.state_dir("attempts")):
        if _read_attempts(path)["parked"]:
            found.add(int(path.stem))
    return frozenset(found)
