"""Pin the Lunate build the Lunate harness runs: a named commit on main, published inside the session sandbox.

    .venv/bin/python scripts/lane.py lunate-pin <commit>

The commit must be on `origin/main`. It is checked out detached under `<state>/tools/src/`, and
`dotnet publish` runs there under the session sandbox (never outside it: a build runs the branch's own MSBuild
code), with the session's own HOME and no model key. The binary is copied to
`<state>/tools/lunate-main-<sha12>/lunate`, `--version` must name the commit, and lane.json gets
`lunate_binary`, `lunate_commit` and `lunate_sha256`. The harness refuses a Lunate session whose binary's hash does
not match (`harness.verify_lunate`).

Nothing updates the pin by itself. Running this is the deliberate step, and the lane.json change goes through a
lane pull request like any other.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

from scripts import common, harness
from scripts.common import LaneError

RID = "osx-arm64"
PROJECT = "src/Lunate.Coding/Lunate.Coding.csproj"
PUBLISH_TIMEOUT = 1800
JOB = "lunate-pin"


class PinError(LaneError):
    """The build was not pinned; lane.json is unchanged."""


def publish_argv(out: pathlib.Path) -> list:
    """The release workflow's publish (`release.yml`, the osx-arm64 leg), with MSBuild's node reuse off."""
    return ["dotnet", "publish", PROJECT, "-c", "Release", "-r", RID, "--self-contained", "true",
            "-p:PublishSingleFile=true", "-p:PublishReadyToRun=true", "-o", str(out), "--nologo", "-nodeReuse:false"]


def resolve(cfg: common.Config, commit: str) -> str:
    """The full sha of `commit`, which must be on `origin/<base>`."""
    with common.checkout_lock():
        common.git(cfg.checkout, "fetch", "-q", "origin", cfg.base)
    try:
        sha = common.git(cfg.checkout, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    except LaneError as exc:
        raise PinError(f"{commit} is not a commit in {cfg.checkout}") from exc
    on_main = subprocess.run(["git", "-C", str(cfg.checkout), "merge-base", "--is-ancestor", sha,
                              f"origin/{cfg.base}"], capture_output=True, timeout=60)
    if on_main.returncode != 0:
        raise PinError(f"{sha[:12]} is not on origin/{cfg.base}; only a commit on {cfg.base} is pinned")
    return sha


def write_pin(config_path: pathlib.Path, binary: str, sha: str, digest: str) -> None:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PinError(f"{config_path} could not be read: {exc}") from exc
    data["harness"].update(lunate_binary=binary, lunate_commit=sha, lunate_sha256=digest)
    config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def drop_source(cfg: common.Config, src: pathlib.Path) -> None:
    """Remove the build's own detached worktree: its directory (inside the state dir, full of build output git would
    refuse to remove without forcing), then git's record of it."""
    if src.exists():
        shutil.rmtree(common.inside_state(src, "tools"), ignore_errors=True)
    common.git(cfg.checkout, "worktree", "prune")


def pin(cfg: common.Config, commit: str, config_path: pathlib.Path = common.CONFIG_PATH, *,
        runner=subprocess.run) -> dict:
    sha = resolve(cfg, commit)
    short = sha[:12]
    tools = cfg.state / "tools"
    src, scratch = tools / "src" / f"lunate-{short}", tools / "scratch" / f"lunate-{short}"
    dest = tools / f"lunate-main-{short}" / "lunate"
    log_path = cfg.state / "logs" / f"{JOB}-{short}.log"
    for path in (src.parent, scratch.parent, dest.parent, log_path.parent):
        path.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir()
    with common.checkout_lock():
        drop_source(cfg, src)
        common.git(cfg.checkout, "worktree", "add", "-q", "--detach", str(src), sha)
    try:
        env = harness.session_env(cfg, scratch)
        env.pop(cfg.harness["credential_env"], None)  # a build needs no model key
        profile = harness.write_profile(cfg, scratch, worktree=src)
        out = src / "artifacts" / "lane-publish"
        common.log(JOB, f"publishing {short} in the sandbox; build log {log_path}")
        with log_path.open("w", encoding="utf-8") as log:
            try:
                done = runner(harness.sandboxed(publish_argv(out), profile), cwd=str(src), env=env, stdout=log,
                              stderr=subprocess.STDOUT, timeout=PUBLISH_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                raise PinError(f"dotnet publish did not finish in {PUBLISH_TIMEOUT}s; see {log_path}") from exc
        if done.returncode != 0:
            raise PinError(f"dotnet publish failed (exit {done.returncode}); see {log_path}")
        built = out / "lunate"
        if not built.is_file():
            raise PinError(f"dotnet publish left no {built}")
        shutil.copy2(built, dest)
        dest.chmod(0o755)
        version = runner(harness.sandboxed([str(dest), "--version"], profile), env=env, capture_output=True,
                         text=True, timeout=60)
        reported = (version.stdout or "").strip()
        if version.returncode != 0 or sha[:8] not in reported:
            raise PinError(f"the built binary reports {reported!r} (exit {version.returncode}), not commit {short}")
    finally:
        with common.checkout_lock():
            drop_source(cfg, src)
        shutil.rmtree(scratch, ignore_errors=True)
    digest = harness.file_sha256(dest)
    home = str(pathlib.Path.home())
    shown = "~" + str(dest)[len(home):] if str(dest).startswith(home + "/") else str(dest)
    write_pin(config_path, shown, sha, digest)
    common.log(JOB, f"pinned lunate {reported} at {shown}, sha256 {digest}")
    return {"commit": sha, "binary": shown, "sha256": digest, "version": reported}
