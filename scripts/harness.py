"""Run one headless Pi session, and know which worker sessions are running.

Vendored from the old lane's `worker_harness.py` (the Pi adapter, its minimal environment and the leaked
tool-markup check) and `lunate-claims.py` (a pid file per running worker), cut down to what this lane uses.

A session gets a minimal environment: PATH (with the pinned .NET SDK first when it exists), a HOME of its own
under its scratch directory (seeded with only what Pi needs), a private TMPDIR, and the model credential. It is
handed no GitHub credential: `gh` points at an empty config directory and git's credential helpers are blanked,
so the lane, not the session, pushes and opens pull requests as the worker identity.

Every session — worker and reviewer — runs under `sandbox-exec` (#10): its profile denies reading or writing the
lane's credentials (`~/.config/lunate-lane`), the old lane's (`~/.hermes`), `~/.ssh`, `~/.config/gh` and
`~/Library/Keychains`, and denies process-info calls on other processes. It is the v1 boundary; the session still
runs as the lane's macOS user (README, "Known limitations").
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from typing import Callable

from scripts import common, secret_scan
from scripts.common import LaneError, StateError

DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TOOL_MARKUP = ("</arg_value>", "</arg_key>", "<arg_value>", "<arg_key>")
# What Pi needs from the real home, copied into the session's own. Not auth.json: the credential is in the env.
HOME_SEED = (".pi/agent/settings.json", ".pi/agent/models-store.json", ".pi/agent/bin/fd")
# Under the real home: what a session may neither read nor write.
SANDBOX_DENY = (".config/lunate-lane", ".hermes", ".ssh", ".config/gh", "Library/Keychains")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Tools that schedule or start work outside the session (review of #17, round two, item 3). Exec is denied, and so
# is reading them, so a session cannot copy one into its scratch and run the copy.
NO_EXEC = ("/bin/launchctl", "/usr/bin/crontab", "/usr/bin/at", "/usr/bin/atq", "/usr/bin/atrm", "/usr/bin/batch",
           "/usr/bin/osascript", "/usr/bin/open")


class HarnessError(LaneError):
    """The session could not be started, or its output could not be read."""


@dataclasses.dataclass(frozen=True)
class Outcome:
    rc: int | None
    stalled: bool
    elapsed: int
    leaked_markup: bool
    session_id: str = ""  # to continue the same session (`run_session(session_id=...)`)


def read_credential(cfg: common.Config) -> str:
    """The model credential, from the dotenv file the config names (`~/.config/lunate-lane/env`). A file whose
    mode is not exactly 600 is refused (#11). Its value is never reported."""
    path = pathlib.Path(cfg.harness["credential_file"]).expanduser()
    key = cfg.harness["credential_key"]
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise HarnessError(f"the credential file {path} could not be read ({exc.__class__.__name__})") from exc
    if mode != 0o600:
        raise HarnessError(f"the credential file {path} is mode {mode:o}, not 600 — refusing it; "
                           f"`chmod 600 {path}`")
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
            done = subprocess.run(["git", *common.SAFE_GIT, "-C", str(cfg.checkout), "config", "--get", key],
                                  capture_output=True, text=True, timeout=30)  # a config read: nothing runs
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
        if home.is_symlink():  # a session could leave one; rmtree never follows the ones inside
            raise HarnessError(f"the session home {home} is a symlink — refusing to clear it")
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
        try:
            (home / ".gitconfig").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            raise HarnessError(f"the session's git identity could not be written in {home}: {exc}") from exc
    return home


def session_env(cfg: common.Config, scratch: pathlib.Path, extra: dict | None = None,
                real_home: pathlib.Path | None = None, fresh_home: bool = True) -> dict:
    """The whole environment a session gets — nothing inherited beyond PATH, and a HOME of its own. `fresh_home`
    False keeps the session's HOME as it is (the build gate runs beside a session and must not wipe its home)."""
    real_home = real_home or pathlib.Path.home()
    nogh = scratch / "gh-config"
    try:
        nogh.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HarnessError(f"the session's scratch {scratch} could not be prepared: {exc}") from exc
    path = os.environ.get("PATH") or DEFAULT_PATH
    home = session_home(cfg, scratch, real_home) if fresh_home else scratch / "home"
    if not fresh_home:
        try:
            home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError(f"the session home {home} could not be prepared: {exc}") from exc
    # no git credential helper of any kind (review of #17): Homebrew's system config names osxkeychain and the
    # user's global config names `gh auth git-credential`; the session reads neither, only its own identity
    env = {"PATH": path, "HOME": str(home), "TMPDIR": str(scratch),
           "GH_CONFIG_DIR": str(nogh), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
           # no credential helper; no automatic gc or maintenance, which would write outside what the sandbox
           # allows a session (packed-refs, gc.log)
           "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
           "GIT_CONFIG_KEY_1": "gc.auto", "GIT_CONFIG_VALUE_1": "0",
           "GIT_CONFIG_KEY_2": "maintenance.auto", "GIT_CONFIG_VALUE_2": "false",
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


def _sbpl(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _forms(paths) -> list[str]:
    found: list[str] = []
    for path in paths:
        for form in (os.path.abspath(path), os.path.realpath(path)):
            if form not in found:
                found.append(form)
    return found


def denied_paths(cfg: common.Config, real_home: pathlib.Path | None = None) -> list[str]:
    """The directories a session may neither read nor write, each as given and as resolved."""
    real_home = real_home or pathlib.Path.home()
    return _forms([real_home / d for d in SANDBOX_DENY] + [cfg.app_config.parent])


def _rx(path: str, tail: str = "") -> str:
    """An SBPL regex matching exactly `path` (and `path` + `tail`)."""
    return '(regex #"^' + re.escape(path).replace('"', '\\"') + tail + '$")'


MSBUILD_SOCKETS = '(regex #"^/private/tmp/MSBuild[0-9]+$")'
# `dotnet test`'s IPC pipe: a socket named by a GUID, ^/private/tmp/[0-9a-f]{32}$ — spelled out, because the
# sandbox's regex dialect has no {n} counts (with one, nothing matched). It matches paths case-insensitively
# here (APFS), so A-F match too: still only hex names.
DOTNET_TEST_PIPE = '(regex #"^/private/tmp/' + "[0-9a-f]" * 32 + '$")'


def sandbox_profile(cfg: common.Config, real_home: pathlib.Path | None = None, *, scratch: pathlib.Path,
                    worktree: pathlib.Path, branch: str | None = None) -> str:
    """The session's sandbox (review of #17, round two, item 1). Writes are denied by default; allowed only:

        the session's scratch (its HOME and TMPDIR) and its worktree;
        what git needs to commit there — `.git/objects`, the worktree's own `.git/worktrees/<name>`, and for a
        worker its own branch ref and that ref's log (not other refs: cleanup trusts `refs/remotes/origin`);
        the NuGet package cache the lane points sessions at; /dev/null and /dev/tty;
        MSBuild's node sockets, `/private/tmp/MSBuild<pid>` and nothing else in /tmp (MSBuild puts them in /tmp,
        not TMPDIR, on macOS; denied, every build waited 5 minutes for nodes that could not connect — #856), and
        `dotnet test`'s IPC pipe, `/private/tmp/<32 lowercase hex>` (denied, it crashed with SocketException 13).

    Inside those, the worktree's `.git` pointer and the admin directory's `gitdir`, `commondir` and
    `config.worktree` stay denied. Reads of the lane's and the user's secrets are denied; process-info only on
    the session's own processes. `sysctl(KERN_PROCARGS2)` is not mediated by the sandbox on this macOS, so
    another process's argv and environment stay readable — the lane keeps tokens out of both (open_pr.push).
    """
    real_home = real_home or pathlib.Path.home()
    git_dir = common.checkout_git(cfg)
    admin = git_dir / "worktrees" / worktree.name
    allowed = [f"(subpath {_sbpl(p)})" for p in _forms([scratch, worktree, git_dir / "objects", admin])]
    if branch:
        for ref in (git_dir / "refs" / "heads" / branch, git_dir / "logs" / "refs" / "heads" / branch):
            allowed += [_rx(form, r"(\.lock)?") for form in _forms([ref])]
    packages = real_home / ".nuget" / "packages"
    if packages.is_dir():
        allowed += [f"(subpath {_sbpl(p)})" for p in _forms([packages])]
    allowed += ['(literal "/dev/null")', '(literal "/dev/tty")', MSBUILD_SOCKETS, DOTNET_TEST_PIPE]
    pinned = [_rx(form, r"(\.lock)?") for form in _forms([worktree / ".git", admin / "gitdir", admin / "commondir",
                                                            admin / "config.worktree"])]
    deny = " ".join(f"(subpath {_sbpl(p)})" for p in denied_paths(cfg, real_home))
    return ("(version 1)\n(allow default)\n"
            "(deny file-write*)\n"
            f"(allow file-write* {' '.join(allowed)})\n"
            f"(deny file-write* {' '.join(pinned)})\n"
            f"(deny file-read* file-write* {deny})\n"
            f"(deny process-exec* file-read-data {' '.join(f'(literal {_sbpl(b)})' for b in NO_EXEC)})\n"
            # deny, then allow the session's own sandbox: `(target others)` alone is not enforced on this macOS
            # (a probe still read the lane's pids); this form is, and it keeps a session's own children readable
            # (dotnet's test host watches them)
            "(deny process-info*)\n(allow process-info* (target same-sandbox))\n")


def sandboxed(command: list, profile: pathlib.Path) -> list:
    return [SANDBOX_EXEC, "-f", str(profile), *command]


def write_profile(cfg: common.Config, scratch: pathlib.Path, real_home: pathlib.Path | None = None, *,
                  worktree: pathlib.Path, branch: str | None = None) -> pathlib.Path:
    path = scratch / "session.sb"
    try:
        path.write_text(sandbox_profile(cfg, real_home, scratch=scratch, worktree=worktree, branch=branch),
                        encoding="utf-8")
    except OSError as exc:
        raise HarnessError(f"the sandbox profile {path} could not be written ({exc.__class__.__name__}: {exc})") from exc
    return path


def argv(cfg: common.Config, model: str, session_dir: pathlib.Path, session_id: str, prompt: str) -> list:
    provider, slash, model_id = model.partition("/")
    if not slash:
        raise HarnessError(f"model {model!r} must be provider/model")
    return [cfg.harness["binary"], "--print", "--mode", "json", "--provider", provider, "--model", model_id,
            "--session-dir", str(session_dir), "--session-id", session_id,
            "--approve", "--no-extensions", "--no-mcp", "--", prompt]


def run_session(cfg: common.Config, *, model: str, worktree: pathlib.Path, prompt: str, scratch: pathlib.Path,
                log_path: pathlib.Path, extra_env: dict | None = None, on_tick: Callable[[], None] | None = None,
                branch: str | None = None,
                on_start: Callable[[int, str], None] | None = None, tick: float = 30.0,
                session_id: str | None = None) -> Outcome:
    """Run Pi to completion in `worktree`, killing it when its event stream stalls or it passes the hard cap.

    Pi runs in a process group of its own; `on_start` gets that group and the session id, so a record can name
    exactly this session for clean-up if this
    driver is killed outright. Any exception here — including the SystemExit `exit_on_signals` raises on
    SIGTERM — kills that group before it propagates.
    """
    timeout, idle = int(cfg.harness["session_timeout"]), int(cfg.harness["stall_idle"])
    # a given id continues that session (Pi's --session-id, same --session-dir): the build gate's fix round
    session_id = session_id or f"{worktree.name}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    env = session_env(cfg, scratch, extra_env)
    command = sandboxed(argv(cfg, model, scratch / "sessions", session_id, prompt),
                        write_profile(cfg, scratch, worktree=worktree, branch=branch))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out_file = log_path.open("ab")
    except OSError as exc:
        raise HarnessError(f"the session log {log_path} could not be opened: {exc}") from exc
    known = (env.get(cfg.harness["credential_env"], ""),)
    with out_file as out:
        try:
            proc = subprocess.Popen(command, cwd=str(worktree), env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise HarnessError(f"{command[0]} could not be started ({exc.__class__.__name__}: {exc})") from exc
        # every line is redacted before it is written: the session's own environment holds the model key, and a
        # tool call that prints it (`env`) would otherwise put it in this file (2026-11-14: five worker sessions)
        pump = threading.Thread(target=_redacting_copy, args=(proc.stdout, out, known), daemon=True)
        pump.start()
        started = last_activity = time.monotonic()
        size, stalled = -1, False
        try:
            if on_start is not None:
                on_start(proc.pid, session_id)
            while proc.poll() is None:
                time.sleep(tick)
                now = time.monotonic()
                try:
                    current = log_path.stat().st_size
                except OSError as exc:
                    raise HarnessError(f"the session log {log_path} could not be read: {exc}") from exc
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
        finally:
            pump.join(timeout=30)
            scrub_dir(scratch / "sessions", known)  # Pi's own session files, which it writes itself
    return Outcome(proc.returncode, stalled, int(time.monotonic() - started), leaked_markup(log_path), session_id)


def _redacting_copy(source, out, known: tuple) -> None:
    """Copy the session's output to its log a line at a time, each line redacted (`secret_scan.redact_record`)
    before it is written."""
    for raw in iter(source.readline, b""):
        line = raw.decode("utf-8", errors="replace")
        out.write(secret_scan.redact_record(line, known).encode("utf-8"))
        out.flush()


def scrub_dir(directory: pathlib.Path, known: tuple) -> list[pathlib.Path]:
    """Redact every file under `directory` in place (`secret_scan.redact_record`), atomically per file; returns
    the files changed. Pi writes its own session files while it runs, so they are scrubbed when it stops."""
    changed = []
    if not directory.is_dir() or directory.is_symlink():
        return changed
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            clean = secret_scan.redact_record(text, known)
            if clean != text:
                tmp = path.with_name(path.name + ".scrub")
                tmp.write_text(clean, encoding="utf-8")
                os.replace(tmp, path)
                changed.append(path)
        except OSError as exc:
            raise HarnessError(f"{path} could not be scrubbed of secrets ({exc.__class__.__name__})") from exc
    return changed


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
        except PermissionError:  # macOS answers EPERM for a group whose only member has exited unreaped
            if proc.poll() is not None:
                return
            raise HarnessError(f"the session group {proc.pid} could not be signalled") from None
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


# The secret shapes live in scripts/secret_scan.py, shared with open_pr's scan before every push.
SECRET_SHAPES, secret_kind = secret_scan.SECRET_SHAPES, secret_scan.secret_kind


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
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise StateError(f"the dead worker's record {path} could not be removed: {exc}") from exc


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


# --- running reviews: one record per pull request under <state>/reviewing (#13) ---
#     The review driver is the cron run of review.py; if it is SIGKILLed its Pi session and worktree outlive it.

def _review_record(pr: int) -> pathlib.Path:
    return common.state_dir("reviewing") / f"{int(pr)}.json"


def record_review(pr: int, pid: int, worktree: pathlib.Path) -> None:
    _write_record(_review_record(pr), {"pr": int(pr), "pid": pid, "worktree": str(worktree), "started": time.time()})


def note_review_session(pr: int, pid: int, session_pgid: int, session_id: str) -> None:
    path = _review_record(pr)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise StateError(f"the review record {path} could not be read: {exc}") from exc
    if data.get("pid") != pid:
        raise StateError(f"the review record {path} belongs to pid {data.get('pid')}, not {pid}")
    data["session_pgid"], data["session_id"] = int(session_pgid), str(session_id)
    _write_record(path, data)


def clear_review(pr: int, pid: int) -> None:
    path = _review_record(pr)
    try:
        if path.exists() and json.loads(path.read_text()).get("pid") == pid:
            path.unlink()
    except (OSError, ValueError) as exc:
        raise StateError(f"the review record {path} could not be cleared: {exc}") from exc


def review_alive(pid: int) -> bool:
    """The pid is alive and is a review.py run (a recycled pid is not)."""
    command = _command(pid)
    return command is not None and "review.py" in command


def reap_reviews(remove_worktree: Callable[[int], None]) -> list[int]:
    """Clear every review whose driver is dead: kill the session it left (only that exact session id), remove its
    worktree, drop the record. Returns the pull requests cleared. An unreadable record raises."""
    cleared = []
    for path in _listing(common.state_dir("reviewing")):
        try:
            data = json.loads(path.read_text())
            pr, pid, worktree = int(data["pr"]), int(data["pid"]), pathlib.Path(str(data["worktree"]))
            pgid, session_id = data.get("session_pgid"), data.get("session_id")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(f"the review record {path} could not be read: {exc}") from exc
        if review_alive(pid):
            continue
        expected = common.state_dir("reviews") / f"pr-{pr}"
        if os.path.abspath(worktree) != os.path.abspath(expected):
            raise StateError(f"the review record {path} names {worktree}, not {expected} — a forged record; "
                             "nothing was touched")
        reap_session(pr, (int(pgid), str(session_id)) if pgid and session_id else None)
        remove_worktree(pr)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StateError(f"the review record {path} could not be removed: {exc}") from exc
        cleared.append(pr)
    return cleared


# --- reruns: an infra failure is rerun once per head; <state>/reruns/<pr>.json names the head it was for ---

def _rerun_record(pr: int) -> pathlib.Path:
    return common.state_dir("reruns") / f"{int(pr)}.json"


def note_rerun(pr: int, head: str, runs: tuple) -> None:
    _write_record(_rerun_record(pr), {"pr": int(pr), "head": str(head), "runs": list(runs), "at": time.time()})


def reran() -> frozenset:
    """(pull request, head) for every head whose infra failure was already rerun once."""
    found = set()
    for path in _listing(common.state_dir("reruns")):
        try:
            data = json.loads(path.read_text())
            found.add((int(data["pr"]), str(data["head"])))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(f"the rerun record {path} could not be read: {exc}") from exc
    return frozenset(found)


def prune_reruns(open_prs) -> int:
    """Remove the rerun records of pull requests that are no longer open (review of #17, round two, item 5).
    An unreadable record raises, like every other state read. Returns how many went."""
    keep, gone = {int(n) for n in open_prs}, 0
    for path in _listing(common.state_dir("reruns")):
        try:
            pr = int(json.loads(path.read_text())["pr"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(f"the rerun record {path} could not be read: {exc}") from exc
        if pr not in keep:
            try:
                path.unlink()
            except OSError as exc:
                raise StateError(f"the rerun record {path} could not be removed: {exc}") from exc
            gone += 1
    return gone


# --- queue ejections: an infra removal from the merge queue is re-enqueued at most REQUEUE_LIMIT times per head;
#     <state>/requeues/<pr>.json names the head and the removals (by their time) already acted on. The queue's
#     events (re-enqueued, sent back) go to <state>/queue/events.jsonl for the digest.

QUEUE_EVENT_DAYS = 7


def _requeue_record(pr: int) -> pathlib.Path:
    return common.state_dir("requeues") / f"{int(pr)}.json"


def requeues(pr: int, head: str) -> tuple:
    """The removals of this head already acted on (their times), oldest first. Another head's are history."""
    path = _requeue_record(pr)
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text())
        return tuple(str(at) for at in data["removals"]) if str(data["head"]) == str(head) else ()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError(f"the requeue record {path} could not be read: {exc}") from exc


def note_requeue(pr: int, head: str, removed_at: str) -> int:
    """Record that this removal of this head was acted on, before anything that can fail. Returns the count."""
    done = requeues(pr, head)
    if removed_at not in done:
        done += (str(removed_at),)
    _write_record(_requeue_record(pr), {"pr": int(pr), "head": str(head), "removals": list(done), "at": time.time()})
    return len(done)


def prune_requeues(open_prs) -> int:
    """Remove the requeue records of pull requests that are no longer open. Returns how many went."""
    keep, gone = {int(n) for n in open_prs}, 0
    for path in _listing(common.state_dir("requeues")):
        try:
            pr = int(json.loads(path.read_text())["pr"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(f"the requeue record {path} could not be read: {exc}") from exc
        if pr not in keep:
            try:
                path.unlink()
            except OSError as exc:
                raise StateError(f"the requeue record {path} could not be removed: {exc}") from exc
            gone += 1
    return gone


def _queue_events() -> pathlib.Path:
    return common.state_dir("queue") / "events.jsonl"


def queue_events(since: float) -> list[str]:
    """The queue's event lines since `since` (epoch seconds), oldest first. An unreadable file raises."""
    path = _queue_events()
    if not path.exists():
        return []
    try:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [str(e["line"]) for e in events if float(e["at"]) >= since]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError(f"the queue events {path} could not be read: {exc}") from exc


def note_queue_event(line: str) -> None:
    """Append one event line for the digest, keeping the last QUEUE_EVENT_DAYS days."""
    path, now = _queue_events(), time.time()
    try:
        kept = [l for l in (path.read_text(encoding="utf-8").splitlines() if path.exists() else [])
                if l.strip() and float(json.loads(l)["at"]) >= now - QUEUE_EVENT_DAYS * 86400]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(f"{l}\n" for l in kept + [json.dumps({"at": now, "line": str(line)})]), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StateError(f"the queue events {path} could not be written: {exc}") from exc


# --- attempts: a card is parked after IDLE_RUN_LIMIT runs with no new commit, or after REWORK_LIMIT rework rounds
#     on one pull request (#9). `scripts/lane.py unpark <card> --reason ...` unparks it and clears both counts.
#     The feeder keeps last tick's parked set (`<state>/parked-seen.json`) and the cards the lane itself unparked
#     since (`<state>/unparked.json`): a park that vanished any other way was lifted by hand, and is logged.

IDLE_RUN_LIMIT = 3
REWORK_LIMIT = 3


def _attempts(card: int) -> pathlib.Path:
    return common.state_dir("attempts") / f"{int(card)}.json"


def _read_attempts(path: pathlib.Path) -> dict:
    if not path.exists():
        return {"idle_runs": 0, "parked": False, "reason": "", "detail": "", "reworks": {}, "died": ""}
    try:
        data = json.loads(path.read_text())
        return {"idle_runs": int(data["idle_runs"]), "parked": bool(data["parked"]),
                "reason": str(data.get("reason") or ""), "detail": str(data.get("detail") or ""),
                "reworks": {str(int(k)): int(v) for k, v in (data.get("reworks") or {}).items()},
                "died": str(data.get("died") or "")}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StateError(f"the attempts record {path} could not be read: {exc}") from exc


def _write_attempts(path: pathlib.Path, data: dict) -> None:
    if not data["idle_runs"] and not data["parked"] and not data["reworks"] and not data["died"]:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StateError(f"the attempts record {path} could not be removed: {exc}") from exc
        return
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({**data, "updated": time.time()}))
        os.replace(tmp, path)
    except OSError as exc:
        raise StateError(f"the attempts record {path} could not be written: {exc}") from exc


def note_attempt(card: int) -> int:
    """Count a worker run as idle *before* it starts — by the feeder, or by a hand-started worker — so a run whose
    setup keeps failing (and never reaches its finish) still reaches the cap. A run that ends with a new commit
    resets the count (note_run). Returns the idle runs so far, this one included."""
    path = _attempts(card)
    data = _read_attempts(path)
    data["idle_runs"] += 1
    _write_attempts(path, data)
    return data["idle_runs"]


def idle_runs(card: int) -> int:
    return _read_attempts(_attempts(card))["idle_runs"]


def note_run(card: int, new_commit: bool) -> tuple[int, bool]:
    """A finished run: a new commit resets the idle count (and lifts an idle park); none leaves the count as
    note_attempt already set it and parks the card once it is at the cap. Rework rounds are kept.
    Returns (idle runs, parked just now)."""
    path = _attempts(card)
    data = _read_attempts(path)
    data["died"] = ""  # the session ran to its end: whatever killed an earlier run did not kill this one
    if new_commit:
        data["idle_runs"] = 0
        if data["parked"] and data["reason"] == "idle":
            data["parked"], data["reason"] = False, ""
            _note_lifted(card)
        _write_attempts(path, data)
        return 0, False
    parked_now = data["idle_runs"] >= IDLE_RUN_LIMIT and not data["parked"]
    if parked_now:
        data["parked"], data["reason"] = True, "idle"
    _write_attempts(path, data)
    return data["idle_runs"], parked_now


def note_rework(card: int, pr: int) -> int:
    """Count one rework round started on this pull request; returns the rounds so far, this one included."""
    path = _attempts(card)
    data = _read_attempts(path)
    data["reworks"][str(int(pr))] = data["reworks"].get(str(int(pr)), 0) + 1
    _write_attempts(path, data)
    return data["reworks"][str(int(pr))]


def rework_rounds(card: int, pr: int) -> int:
    return _read_attempts(_attempts(card))["reworks"].get(str(int(pr)), 0)


DIED_LIMIT = 160


def note_death(card: int, reason: str) -> None:
    """A worker run that failed by name (its setup, its push, its finish): the first line of why, for the digest's
    "worker died: <reason>" and `lane.py status`. Cleared when a later run's session reaches its end (note_run)."""
    path = _attempts(card)
    data = _read_attempts(path)
    first = next((line.strip() for line in str(reason).splitlines() if line.strip()), "no reason given")
    data["died"] = first[:DIED_LIMIT]
    _write_attempts(path, data)


def died_info() -> dict:
    """card -> why its last worker run died, for every card whose last run died."""
    found = {}
    for path in _listing(common.state_dir("attempts")):
        data = _read_attempts(path)
        if data["died"]:
            found[int(path.stem)] = data["died"]
    return found


def park(card: int, reason: str, detail: str = "") -> bool:
    """Park the card for `reason` ('idle', 'reworks', 'secret', 'person', 'ready'), with the words a person reads
    in the digest (`detail`, for 'person' and 'ready'). Returns whether it was parked just now."""
    path = _attempts(card)
    data = _read_attempts(path)
    if data["parked"]:
        return False
    data["parked"], data["reason"], data["detail"] = True, reason, detail
    _write_attempts(path, data)
    return True


def parked_details() -> dict:
    """card -> why it is parked, for every parked card."""
    found = {}
    for path in _listing(common.state_dir("attempts")):
        data = _read_attempts(path)
        if data["parked"]:
            found[int(path.stem)] = data["reason"] or "idle"
    return found


def attempts(card: int) -> dict:
    """The card's attempts record as read: idle runs, rework rounds per pull request, parked, reason, detail."""
    return _read_attempts(_attempts(card))


def unpark(card: int) -> bool:
    """Remove the card's attempts record — unparked, both counts cleared. Returns whether there was one."""
    path = _attempts(card)
    was_parked = _read_attempts(path)["parked"]  # a record that cannot be read is named, not silently removed
    if not path.exists():
        return False
    if was_parked:
        _note_lifted(card)
    try:
        path.unlink()
    except OSError as exc:
        raise StateError(f"the attempts record {path} could not be removed: {exc}") from exc
    return True


def _read_cards(path: pathlib.Path) -> set:
    if not path.exists():
        return set()
    try:
        return {int(n) for n in json.loads(path.read_text())}
    except (OSError, ValueError, TypeError) as exc:
        raise StateError(f"{path} could not be read: {exc}") from exc


def _write_cards(path: pathlib.Path, cards) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(sorted(int(n) for n in cards)))
        os.replace(tmp, path)
    except OSError as exc:
        raise StateError(f"{path} could not be written: {exc}") from exc


def _note_lifted(card: int) -> None:
    """The lane itself lifted this card's park (`lane.py unpark`, or a new commit lifting an idle park)."""
    path = common.state_dir() / "unparked.json"
    _write_cards(path, _read_cards(path) | {int(card)})


def lifted_by_hand() -> list[int]:
    """Cards parked at the last check that are no longer parked, and that the lane did not unpark itself: their
    attempts record was edited or removed by hand. Updates the check's memory; the first check finds none."""
    seen_path, lifted_path = common.state_dir() / "parked-seen.json", common.state_dir() / "unparked.json"
    first = not seen_path.exists()
    seen, lifted, now = _read_cards(seen_path), _read_cards(lifted_path), set(parked_cards())
    _write_cards(seen_path, now)
    _write_cards(lifted_path, set())
    return [] if first else sorted(seen - now - lifted)


def parked_info() -> dict:
    """card -> (why it is parked, the detail a person reads), for every parked card."""
    found = {}
    for path in _listing(common.state_dir("attempts")):
        data = _read_attempts(path)
        if data["parked"]:
            found[int(path.stem)] = (data["reason"] or "idle", data["detail"])
    return found


def parked_cards() -> frozenset:
    """Cards the feeder must not start again until a person deletes their attempts record."""
    return frozenset(parked_details())
