"""A release, as one deterministic sequence (lunate-lane#21).

    .venv/bin/python scripts/lane.py release 0.5.0      # record the intent and advance as far as it can
    .venv/bin/python scripts/lane.py release --status   # where it is and what it waits on

A maintainer decides that a version ships; the orchestrator relays it as `lane.py release <version>`. No model
session decides or edits anything on this path. Each step reads GitHub, does one thing, and reads it back; a step
that is done is skipped, so the run continues from the first one not done and running it again is always safe.
The feeder tick advances an open release every 10 minutes, so nothing waits in a terminal while CI runs.

    preflight  the milestone is empty, main is green, the version is newer, the repository has what a release needs
    pr         `release/<version>`, `chore(release): <version>`, by lunate-worker: `changelog-assemble --release`
               in the session sandbox, ready and NOT armed
    approval   the one human gate: the maintainer approves the release pull request at its head
    merge      the release job enqueues it (the feeder never does: `github.release_owned`)
    tag        `v<version>` on the merged commit, as lunate-worker; release.yml publishes it
    published  the Release workflow on the tag succeeded and the release carries its assets and SHA256SUMS
    channels   stable only: `chore/<version>-channels` filled by `scripts/fill-release-hashes`, approved by the
               reviewer identity once its diff touches only the channel manifests and the README, then enqueued
    mirror     stable only: the Homebrew tap and the Scoop bucket get main's formula and manifest
    smoke      smoke.yml on the tag, dispatched once and waited for
    close      stable only: the milestone is closed, the next one created, and the feeder pointed at it

The intent lives in `<state>/release.json` (the version, who asked, when the smoke was dispatched, the last word);
everything else is read from GitHub. A finished release moves to `<state>/releases/<version>.json`.
"""

from __future__ import annotations

import base64
import dataclasses
import fnmatch
import json
import pathlib
import re
import subprocess
import time

from scripts import common, github, harness, identity, open_pr
from scripts.common import LaneError, StateError

JOB = "release"
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$")
PROPS_VERSION = re.compile(r"<Version>\s*([^<\s]+)\s*</Version>")
RELEASE_FILES = ("CHANGELOG.md", "Directory.Build.props", "changes/*.md")
CHANNEL_FILES = ("packaging/homebrew/lunate.rb", "packaging/scoop/lunate.json", "packaging/winget/*.yaml", "README.md")
ASSETS = ("SHA256SUMS",)  # the release is read back with these at least, and ASSET_COUNT in all
ASSET_COUNT = 6
MIRRORS = (("lunate-agent/homebrew-tap", "packaging/homebrew/lunate.rb", "Formula/lunate.rb"),
           ("lunate-agent/scoop-bucket", "packaging/scoop/lunate.json", "bucket/lunate.json"))
PYTHON = "/usr/bin/python3"


class Blocked(LaneError):
    """The release cannot go on without a person: said by name, every tick, until it is fixed."""


@dataclasses.dataclass(frozen=True)
class Version:
    text: str
    major: int
    minor: int
    patch: int
    rc: int | None

    @property
    def base(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def stable(self) -> bool:
        return self.rc is None

    @property
    def tag(self) -> str:
        return f"v{self.text}"

    @property
    def key(self) -> tuple:
        return (self.major, self.minor, self.patch, 1 if self.rc is None else 0, self.rc or 0)

    @property
    def next_milestone(self) -> str:
        return f"{self.major}.{self.minor + 1}.0"


def parse(text: str) -> Version:
    match = VERSION.match((text or "").strip().removeprefix("v"))
    if not match:
        raise LaneError(f"{text!r} is not a version: X.Y.Z or X.Y.Z-rc.N")
    major, minor, patch, rc = match.groups()
    return Version(match.group(0), int(major), int(minor), int(patch), int(rc) if rc else None)


# --- the intent ---------------------------------------------------------------------------------------------------

def _record() -> pathlib.Path:
    return common.state_dir() / "release.json"


def intent() -> dict | None:
    path = _record()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        parse(data["version"])
        return data
    except (OSError, ValueError, KeyError, TypeError, LaneError) as exc:
        raise StateError(f"the release record {path} could not be read: {exc}") from exc


def _write(data: dict) -> None:
    path = _record()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        tmp.replace(path)
    except OSError as exc:
        raise StateError(f"the release record {path} could not be written: {exc}") from exc


def start(version: str, by: str, allow_open: bool = False) -> dict:
    """Record the intent to release `version`. Refused while another release is open."""
    v = parse(version)
    current = intent()
    if current and current["version"] != v.text:
        raise LaneError(f"release {current['version']} is still open — finish it, or `lane.py release --abandon`")
    data = current or {"version": v.text, "by": by, "started": time.time(), "allow_open": bool(allow_open)}
    if allow_open:
        data["allow_open"] = True
    _write(data)
    return data


def abandon(reason: str) -> str:
    current = intent()
    if not current:
        return "no release is open"
    _finish(current, f"abandoned: {reason}")
    return f"release {current['version']} abandoned — nothing on GitHub was undone"


def _finish(data: dict, outcome: str) -> None:
    done = common.state_dir("releases") / f"{data['version']}.json"
    try:
        done.write_text(json.dumps({**data, "finished": time.time(), "outcome": outcome}, indent=1, sort_keys=True))
        _record().unlink()
    except OSError as exc:
        raise StateError(f"the release record could not be moved to {done}: {exc}") from exc


# --- GitHub, through gh (reads) and the App identities (writes) ---------------------------------------------------

def _api(path: str):
    return common.gh_json("api", path)


def _worker() -> str:
    return open_pr._worker_token()


def _reviewer() -> str:
    try:
        return identity.installation_token("reviewer")
    except identity.IdentityError as exc:
        raise LaneError(f"the reviewer identity could not mint a token: {exc}") from exc


def _pull_for(cfg: common.Config, branch: str) -> dict | None:
    """The newest pull request on `branch` in any state (REST), or None."""
    pulls = _api(f"repos/{cfg.slug}/pulls?state=all&head={cfg.owner}:{branch}&per_page=10")
    if not isinstance(pulls, list):
        raise LaneError(f"the pull requests for {branch!r} came back as {type(pulls).__name__}, not a list")
    pulls = [p for p in pulls if (p.get("head") or {}).get("ref") == branch]
    open_ = [p for p in pulls if p.get("state") == "open"]
    merged = [p for p in pulls if p.get("merged_at")]
    return (open_ or merged or [None])[0]


def _main_props(cfg: common.Config, ref: str) -> str:
    text = common.git(cfg.checkout, "show", f"{ref}:Directory.Build.props")
    match = PROPS_VERSION.search(text)
    if not match:
        raise LaneError(f"Directory.Build.props at {ref} has no <Version>")
    return match.group(1)


def _has(cfg: common.Config, ref: str, path: str, needle: str = "") -> bool:
    try:
        text = common.git(cfg.checkout, "show", f"{ref}:{path}")
    except common.GitError:
        return False
    return needle in text


def _milestone(cfg: common.Config, title: str) -> dict | None:
    found = [m for m in _api(f"repos/{cfg.slug}/milestones?state=all&per_page=100") if m.get("title") == title]
    return found[0] if found else None


def _ci_ok_on(cfg: common.Config, sha: str) -> str:
    runs = _api(f"repos/{cfg.slug}/commits/{sha}/check-runs?check_name={cfg.aggregate_check}&per_page=50")
    runs = sorted(runs.get("check_runs") or [], key=lambda r: r.get("started_at") or "")
    if not runs:
        return "missing"
    last = runs[-1]
    if last.get("status") != "completed":
        return "pending"
    return "green" if last.get("conclusion") == "success" else "failing"


def _tag_sha(cfg: common.Config, tag: str) -> str | None:
    refs = _api(f"repos/{cfg.slug}/git/matching-refs/tags/{tag}")
    exact = [r for r in refs if r.get("ref") == f"refs/tags/{tag}"]
    if not exact:
        return None
    obj = exact[0]["object"]
    if obj.get("type") == "tag":  # an annotated tag: its commit
        obj = _api(f"repos/{cfg.slug}/git/tags/{obj['sha']}")["object"]
    return str(obj["sha"])


def _workflow_runs(cfg: common.Config, workflow: str, query: str) -> list:
    data = _api(f"repos/{cfg.slug}/actions/workflows/{workflow}/runs?{query}&per_page=20")
    return sorted(data.get("workflow_runs") or [], key=lambda r: r.get("created_at") or "")


# --- a worktree, one sandboxed script, one commit ------------------------------------------------------------------

def _changed(cfg: common.Config, worktree: pathlib.Path) -> list[str]:
    """Every path the script changed, deleted or added, against the worktree's HEAD (no porcelain columns to
    misread: the lane's git output is stripped)."""
    tracked = common.wt_git(cfg, worktree, "diff", "--name-only", "HEAD")
    untracked = common.wt_git(cfg, worktree, "ls-files", "--others", "--exclude-standard")
    return sorted({line.strip() for line in (tracked + "\n" + untracked).splitlines() if line.strip()})


def _only(paths: list[str], allowed: tuple) -> list[str]:
    return [p for p in paths if not any(fnmatch.fnmatch(p, pattern) for pattern in allowed)]


def _sandboxed_run(cfg: common.Config, worktree: pathlib.Path, scratch: pathlib.Path, branch: str,
                   argv: list[str]) -> str:
    """Run one of the repository's own scripts in the session sandbox: it can write the worktree and its scratch,
    nothing else; no credential in its environment."""
    profile = harness.write_profile(cfg, scratch, worktree=worktree, branch=branch)
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"PATH": harness.session_path(cfg), "HOME": str(home), "TMPDIR": str(scratch), "LC_ALL": "C.UTF-8"}
    try:
        done = subprocess.run(harness.sandboxed(argv, profile), cwd=str(worktree), env=env, capture_output=True,
                              text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Blocked(f"{' '.join(argv[1:3])} could not be run ({exc.__class__.__name__})") from exc
    if done.returncode != 0:
        tail = (done.stderr + done.stdout).strip()[-600:]
        raise Blocked(f"`{' '.join(argv[1:])}` failed ({harness.describe_rc(done.returncode)}): {tail or 'no output'}")
    return done.stdout


def _cut(cfg: common.Config, branch: str, name: str, subject: str) -> pathlib.Path | None:
    """A fresh worktree on a new local `branch` from origin/<base>, or None when the branch already holds this
    step's own commit (`subject`) on the current base: an earlier attempt committed and only its pull request is
    left to open. Git never forces a removal here: a worktree a failed attempt left is kept for a person, and a
    stale local branch is deleted only at its checked tip, only when it is this step's own and never reached origin."""
    repo, path = cfg.checkout, common.state_dir("worktrees") / name
    with common.checkout_lock():
        common.git(repo, "fetch", "-q", "--prune", "origin")
        base = common.git(repo, "rev-parse", f"origin/{cfg.base}").strip()
        on_origin = bool(common.git(repo, "ls-remote", "--heads", "origin", branch).strip())
        ref = f"refs/heads/{branch}"
        tip = common.git(repo, "for-each-ref", "--format=%(objectname)", ref).strip()
        if path.exists():
            raise Blocked(f"an earlier attempt's worktree {path} is still there — look at it, remove it "
                          f"(`git -C {repo} worktree remove {path}`), and the next advance retries")
        if tip == base and not on_origin:  # an attempt that cut it and failed before committing: cut again
            common.git(repo, "update-ref", "-d", ref, tip)
        elif tip:
            own = common.git(repo, "log", "-1", "--format=%s", tip).strip() == subject
            if own and common.git(repo, "rev-parse", f"{tip}^").strip() == base:
                return None
            if not own or on_origin:
                raise Blocked(f"{branch!r} at {tip[:9]} is not this step's own commit on origin/{cfg.base}"
                              + (" and is on origin" if on_origin else "") + " — a person looks at it")
            common.git(repo, "update-ref", "-d", ref, tip)  # its own commit on an old base: cut again
        elif on_origin:
            raise Blocked(f"origin has {branch!r} but no pull request — delete the branch or open its pull request")
        common.git(repo, "worktree", "add", "--no-track", "-b", branch, str(path), f"origin/{cfg.base}")
    return path


def _drop(cfg: common.Config, path: pathlib.Path) -> None:
    """Remove a worktree whose work is committed — never forced: git refuses a dirty one, and says so."""
    with common.checkout_lock():
        if path.exists():
            common.git(cfg.checkout, "worktree", "remove", str(path))


def _kept(exc: Exception, path: pathlib.Path) -> Blocked:
    return Blocked(f"{exc} (its worktree {path} is kept for a look; remove it and the next advance retries)")


def _commit(cfg: common.Config, worktree: pathlib.Path, message: str) -> str:
    who = harness._git_identity(cfg)
    config = []
    for key in ("name", "email"):
        if key in who:
            config += ["-c", f"user.{key}={who[key]}"]
    common.wt_git(cfg, worktree, "add", "-A")
    common.wt_git(cfg, worktree, *config, "commit", "-q", "-m", message)
    return common.wt_git(cfg, worktree, "rev-parse", "HEAD").strip()


# --- the steps ----------------------------------------------------------------------------------------------------
# Each returns None when it is done (the next one runs), or what it waits on; a Blocked needs a person.

def step_preflight(cfg: common.Config, v: Version, data: dict) -> str | None:
    if _pull_for(cfg, f"release/{v.text}"):
        return None  # past it: the release pull request exists
    with common.checkout_lock():
        common.git(cfg.checkout, "fetch", "-q", "origin", cfg.base)
    ref = f"origin/{cfg.base}"
    current = parse(_main_props(cfg, ref))
    if v.key <= current.key:
        raise Blocked(f"{v.text} is not newer than {current.text}, the <Version> on {cfg.base}")
    if not _has(cfg, ref, "scripts/changelog-assemble", "--release"):
        raise Blocked(f"{cfg.base}'s scripts/changelog-assemble has no --release mode yet (lunate#958)")
    milestone = _milestone(cfg, v.base)
    if milestone is None:
        raise Blocked(f"there is no milestone {v.base!r} in {cfg.slug}")
    if int(milestone.get("open_issues") or 0) and not data.get("allow_open"):
        raise Blocked(f"milestone {v.base} still has {milestone['open_issues']} open issue(s) — finish them, move "
                      f"them, or `lane.py release {v.text} --allow-open`")
    sha = common.git(cfg.checkout, "rev-parse", ref).strip()
    state = _ci_ok_on(cfg, sha)
    if state == "pending":
        return f"{cfg.aggregate_check} is still running on {cfg.base} at {sha[:9]}"
    if state != "green":
        raise Blocked(f"{cfg.base} is not green at {sha[:9]} ({cfg.aggregate_check} {state})")
    return None


def release_body(v: Version, changelog: str) -> str:
    section = _section(changelog, v.text)
    return (f"The {v.text} release pull request, opened by the lane (`lane.py release {v.text}`, lunate-lane#21).\n\n"
            "**Approving it is the release.** Once the maintainer approves this head, the lane enqueues it, tags "
            f"`{v.tag}` on the merged commit, waits for the Release workflow, fills the channels"
            + (", mirrors the tap and the bucket, runs the smoke and closes the milestone." if v.stable else
               " (not for a prerelease) and runs the smoke.")
            + "\n\nBefore approving:\n\n"
            "- [ ] the changelog below reads right — it ships exactly as written\n"
            "- [ ] the terminal matrix (`docs/dev/distribution.md`, *Pre-release checks*)\n"
            "- [ ] README, guide and SECURITY re-read against the code (step 0)\n\n"
            f"## Changelog (as it ships)\n\n{section or '(the promoted section could not be read)'}\n")


def _section(changelog: str, version: str) -> str:
    lines, out, inside = changelog.splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line.startswith(f"## [{version}]")
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def step_pr(cfg: common.Config, v: Version, data: dict) -> str | None:
    branch = f"release/{v.text}"
    if _pull_for(cfg, branch):
        return None
    worktree = _cut(cfg, branch, f"release-{v.text}", f"chore(release): {v.text}")
    if worktree is not None:
        scratch = common.state_dir("scratch", f"release-{v.text}")
        try:
            _sandboxed_run(cfg, worktree, scratch, branch,
                           [PYTHON, "-I", "scripts/changelog-assemble", "--release", v.text])
            _sandboxed_run(cfg, worktree, scratch, branch, [PYTHON, "-I", "scripts/changelog-assemble", "--check"])
            changed = _changed(cfg, worktree)
            stray = _only(changed, RELEASE_FILES)
            if stray or "CHANGELOG.md" not in changed or "Directory.Build.props" not in changed:
                raise Blocked(f"changelog-assemble --release changed {', '.join(changed) or 'nothing'}; a release "
                              "edits CHANGELOG.md and Directory.Build.props and deletes fragments, nothing else")
            props = PROPS_VERSION.search((worktree / "Directory.Build.props").read_text())
            if not props or props.group(1) != v.text:
                raise Blocked(f"Directory.Build.props does not read {v.text} after changelog-assemble --release")
            _commit(cfg, worktree, f"chore(release): {v.text}")
        except LaneError as exc:
            raise _kept(exc, worktree) from exc
        _drop(cfg, worktree)  # committed: the pull request is opened from the branch, not the worktree
    body = release_body(v, common.git(cfg.checkout, "show", f"{branch}:CHANGELOG.md"))
    done = open_pr.open_pr(cfg.checkout, branch, f"chore(release): {v.text}", body, cfg.base, arm=False,
                           ready_only=True)
    common.log(JOB, f"{v.text}: opened the release pull request #{done['number']} — waits on the maintainer's approval")
    return f"the maintainer's approval of #{done['number']}"


def _approved_by(p: github.Pull, login: str) -> bool:
    """The latest review `login` left on the current head is an approval."""
    mine = [r for r in p.reviews if r.author == login and r.commit == p.head and r.state in ("APPROVED",
                                                                                                "CHANGES_REQUESTED")]
    return bool(mine) and mine[-1].state == "APPROVED"


def _land(cfg: common.Config, number: int, approver: str, what: str) -> str | None:
    """Open, green and approved by `approver` at its head → enqueued; merged → None (done); else what it waits on."""
    p = github.pull(cfg, number)
    if p.state == "MERGED":
        return None
    if p.state != "OPEN":
        raise Blocked(f"{what} #{number} was closed without merging — reopen it, or `lane.py release --abandon`")
    if p.author != cfg.worker_app:
        raise Blocked(f"{what} #{number} is authored by {p.author!r}, not {cfg.worker_app}")
    if p.in_queue:
        return f"#{number} in the merge queue"
    if p.ejection is not None and p.ejection.kind == "real":
        raise Blocked(f"the merge queue removed {what} #{number}: {p.ejection.why}")
    checks = github.check_state(p, github.required_checks(cfg))
    if checks == "failing":
        raise Blocked(f"{what} #{number} is red on a required check at {p.head[:9]}")
    if not _approved_by(p, approver):
        return f"@{approver}'s approval of #{number} at {p.head[:9]}"
    if checks == "pending":
        return f"the required checks on #{number}"
    if p.mergeable != "MERGEABLE":
        return f"GitHub to read #{number} as mergeable (it reads {p.mergeable or 'unknown'})"
    entry = open_pr.enqueue(p.number, p.node_id, p.head)
    common.log(JOB, f"enqueued {what} #{number} at {p.head[:9]} — {entry['state']}, position {entry['position']}")
    return f"#{number} in the merge queue"


def step_merge(cfg: common.Config, v: Version, data: dict) -> str | None:
    pull = _pull_for(cfg, f"release/{v.text}")
    if pull is None:
        raise Blocked(f"the release pull request for {v.text} is gone")
    return _land(cfg, int(pull["number"]), cfg.parked_assignee, "the release pull request")


def step_tag(cfg: common.Config, v: Version, data: dict) -> str | None:
    pull = _pull_for(cfg, f"release/{v.text}")
    merged = str((pull or {}).get("merge_commit_sha") or "")
    if not (pull or {}).get("merged_at") or not merged:
        raise Blocked(f"the release pull request for {v.text} reads as not merged")
    existing = _tag_sha(cfg, v.tag)
    if existing:
        if existing != merged:
            raise Blocked(f"{v.tag} already exists at {existing[:9]}, not at the release's merged commit {merged[:9]}")
        return None
    with common.checkout_lock():
        common.git(cfg.checkout, "fetch", "-q", "origin", cfg.base)
    if not _on_base(cfg, merged):
        raise Blocked(f"the release's merged commit {merged[:9]} is not on origin/{cfg.base}")
    if parse(_main_props(cfg, merged)).text != v.text:
        raise Blocked(f"Directory.Build.props at {merged[:9]} does not read {v.text}")
    common.rest(_worker(), "POST", f"/repos/{cfg.slug}/git/refs", {"ref": f"refs/tags/{v.tag}", "sha": merged})
    if _tag_sha(cfg, v.tag) != merged:
        raise LaneError(f"{v.tag} does not read back at {merged[:9]}")
    common.log(JOB, f"{v.text}: tagged {v.tag} at {merged[:9]}")
    return "the Release workflow on the tag"


def _on_base(cfg: common.Config, sha: str) -> bool:
    try:
        common.git(cfg.checkout, "merge-base", "--is-ancestor", sha, f"origin/{cfg.base}")
        return True
    except common.GitError:
        return False


def step_published(cfg: common.Config, v: Version, data: dict) -> str | None:
    runs = [r for r in _workflow_runs(cfg, "release.yml", f"event=push&branch={v.tag}")]
    if not runs:
        return f"the Release workflow to start on {v.tag}"
    run = runs[-1]
    if run.get("status") != "completed":
        return f"the Release workflow run {run['id']} on {v.tag}"
    if run.get("conclusion") != "success":
        raise Blocked(f"the Release workflow on {v.tag} ended {run.get('conclusion')}: {run.get('html_url')} — "
                      "re-run the tag run (`gh run rerun <id>`), and the next tick reads it again")
    release = _api(f"repos/{cfg.slug}/releases/tags/{v.tag}")
    names = sorted(a.get("name") for a in release.get("assets") or [])
    if release.get("draft") or any(a not in names for a in ASSETS) or len(names) < ASSET_COUNT:
        raise Blocked(f"the {v.tag} release reads as {'a draft' if release.get('draft') else 'published'} with "
                      f"{len(names)} asset(s) ({', '.join(names) or 'none'}); it needs {ASSET_COUNT} and SHA256SUMS")
    return None


def channels_check(changed: list[str]) -> list[str]:
    return _only(changed, CHANNEL_FILES)


def step_channels(cfg: common.Config, v: Version, data: dict) -> str | None:
    if not v.stable:
        return None  # a prerelease stays off the stable channels
    branch = f"chore/{v.text}-channels"
    pull = _pull_for(cfg, branch)
    if pull is None:
        with common.checkout_lock():
            common.git(cfg.checkout, "fetch", "-q", "origin", cfg.base)
        if not _has(cfg, f"origin/{cfg.base}", "scripts/fill-release-hashes"):
            raise Blocked(f"{cfg.base} has no scripts/fill-release-hashes yet (lunate#961)")
        subject = f"chore: fill {v.text} release hashes and the README release line"
        worktree = _cut(cfg, branch, f"channels-{v.text}", subject)
        if worktree is not None:
            scratch = common.state_dir("scratch", f"channels-{v.text}")
            try:
                sums = scratch / "SHA256SUMS"
                sums.unlink(missing_ok=True)
                common.gh("release", "download", v.tag, "-R", cfg.slug, "-p", "SHA256SUMS", "--dir", str(scratch))
                _sandboxed_run(cfg, worktree, scratch, branch,
                               [PYTHON, "-I", "scripts/fill-release-hashes", v.tag, "--sums", str(sums)])
                _sandboxed_run(cfg, worktree, scratch, branch, [PYTHON, "-I", "scripts/docs-sync", "--write"])
                changed = _changed(cfg, worktree)
                if not changed:
                    raise Blocked(f"fill-release-hashes changed nothing for {v.tag}")
                stray = channels_check(changed)
                if stray:
                    raise Blocked(f"the channel fill changed {', '.join(stray)} — only the channel manifests and the "
                                  "README release line may change")
                _commit(cfg, worktree, subject)
            except LaneError as exc:
                raise _kept(exc, worktree) from exc
            _drop(cfg, worktree)
        changed = [line for line in common.git(cfg.checkout, "diff", "--name-only", f"origin/{cfg.base}...{branch}"
                                               ).splitlines() if line.strip()]
        body = (f"The {v.tag} channel manifests and the README release line, filled by the lane from the "
                f"release's SHA256SUMS (`scripts/fill-release-hashes {v.tag}`, lunate-lane#21). Changed: "
                + ", ".join(f"`{p}`" for p in changed) + ".\n\nNo changelog fragment: release bookkeeping.\n")
        done = open_pr.open_pr(cfg.checkout, branch, f"chore: fill {v.text} release hashes", body, cfg.base,
                               arm=False, ready_only=True)
        common.log(JOB, f"{v.text}: opened the channels pull request #{done['number']}")
        return f"the required checks on #{done['number']}"
    number = int(pull["number"])
    if not pull.get("merged_at"):
        _approve_channels(cfg, v, number)
    return _land(cfg, number, cfg.reviewer_app, "the channels pull request")


def _approve_channels(cfg: common.Config, v: Version, number: int) -> None:
    """The reviewer identity approves the channels pull request once it is green and its files are only the
    channel manifests and the README: a check of the diff's shape, not a model's judgement."""
    p = github.pull(cfg, number)
    if p.state != "OPEN" or _approved_by(p, cfg.reviewer_app):
        return
    if github.check_state(p, github.required_checks(cfg)) != "green":
        return
    stray = channels_check(list(p.files or ()))
    if stray:
        raise Blocked(f"the channels pull request #{number} changes {', '.join(stray)} — not approved")
    token = _reviewer()
    review = common.rest(token, "POST", f"/repos/{cfg.slug}/pulls/{number}/reviews", {
        "commit_id": p.head, "event": "APPROVE",
        "body": (f"Release bookkeeping for {v.tag}: the diff touches only the channel manifests and the README "
                 "release line, and the required checks are green. Approved by the release job (lunate-lane#21).")})
    if not review.get("id"):
        raise LaneError(f"the approval of #{number} came back without an id")
    common.log(JOB, f"{v.text}: approved the channels pull request #{number} at {p.head[:9]}")


def step_mirror(cfg: common.Config, v: Version, data: dict) -> str | None:
    if not v.stable:
        return None
    with common.checkout_lock():
        common.git(cfg.checkout, "fetch", "-q", "origin", cfg.base)
    token = None
    for repo, source, target in MIRRORS:
        want = common.git(cfg.checkout, "show", f"origin/{cfg.base}:{source}")
        if v.text not in want:
            raise Blocked(f"{source} on {cfg.base} does not name {v.text} — the channels pull request has not landed")
        try:
            current = _api(f"repos/{repo}/contents/{target}")
        except common.GhError as exc:
            raise Blocked(f"{repo}/{target} could not be read: {exc}") from exc
        if base64.b64decode(current.get("content") or "").decode() == want:
            continue
        token = token or _worker()
        try:
            common.rest(token, "PUT", f"/repos/{repo}/contents/{target}", {
                "message": f"lunate {v.text}", "sha": current["sha"],
                "content": base64.b64encode(want.encode()).decode()})
        except common.ApiError as exc:
            raise Blocked(f"{repo} could not be updated as {cfg.worker_app} ({exc}) — install the worker App on "
                          f"{repo} with contents write, and the next tick mirrors it") from exc
        common.log(JOB, f"{v.text}: mirrored {source} to {repo}/{target}")
    return None


def step_smoke(cfg: common.Config, v: Version, data: dict) -> str | None:
    dispatched = data.get("smoke_dispatched")
    if not dispatched:
        common.gh("workflow", "run", "smoke.yml", "-R", cfg.slug, "--ref", cfg.base, "-f", f"tag={v.tag}")
        data["smoke_dispatched"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
        _write(data)
        common.log(JOB, f"{v.text}: dispatched smoke.yml for {v.tag}")
        return f"smoke.yml on {v.tag}"
    runs = [r for r in _workflow_runs(cfg, "smoke.yml", "event=workflow_dispatch")
            if (r.get("created_at") or "") >= dispatched]
    if not runs:
        return f"smoke.yml to start on {v.tag}"
    run = runs[0]
    if run.get("status") != "completed":
        return f"smoke.yml run {run['id']} on {v.tag}"
    if run.get("conclusion") != "success":
        raise Blocked(f"smoke.yml on {v.tag} ended {run.get('conclusion')}: {run.get('html_url')} — fix it and "
                      f"`lane.py release {v.text} --resmoke`")
    return None


def step_close(cfg: common.Config, v: Version, data: dict) -> str | None:
    if not v.stable:
        return None
    token = None
    shipped = _milestone(cfg, v.base)
    if shipped and shipped.get("state") == "open":
        if int(shipped.get("open_issues") or 0):
            raise Blocked(f"milestone {v.base} gained {shipped['open_issues']} open issue(s) during the release — "
                          "move them, and the next tick closes it")
        token = _worker()
        common.rest(token, "PATCH", f"/repos/{cfg.slug}/milestones/{shipped['number']}", {"state": "closed"})
        common.log(JOB, f"{v.text}: closed milestone {v.base}")
    if _milestone(cfg, v.next_milestone) is None:
        token = token or _worker()
        common.rest(token, "POST", f"/repos/{cfg.slug}/milestones", {"title": v.next_milestone})
        common.log(JOB, f"{v.text}: created milestone {v.next_milestone}")
    if common.config().milestone != v.next_milestone:
        common.set_milestone(v.next_milestone)
        common.log(JOB, f"{v.text}: the feeder now works milestone {v.next_milestone}")
    return None


STEPS = (("preflight", step_preflight), ("pr", step_pr), ("merge", step_merge), ("tag", step_tag),
         ("published", step_published), ("channels", step_channels), ("mirror", step_mirror),
         ("smoke", step_smoke), ("close", step_close))


def advance(cfg: common.Config) -> str:
    """Run the steps in order until one waits or blocks. Returns one line: where it stands. A Blocked is recorded
    and raised, so the tick that hit it fails by name."""
    data = intent()
    if data is None:
        return "no release is open"
    v = parse(data["version"])
    for name, step in STEPS:
        try:
            waiting = step(cfg, v, data)
        except Blocked as exc:
            data["last"] = f"{name}: blocked — {exc}"
            _write(data)
            raise
        if waiting:
            data["last"] = f"{name}: waits on {waiting}"
            _write(data)
            return f"release {v.text} — {data['last']}"
    _finish(data, "released")
    common.log(JOB, f"{v.text} is released")
    return f"release {v.text} — done"


def tick(cfg: common.Config, job: str) -> None:
    """The feeder's call: advance an open release under its own lock; a release run by hand right now wins."""
    if intent() is None:
        return
    try:
        with common.job_lock(JOB):
            common.log(job, advance(cfg))
    except common.LockBusy:
        common.log(job, "a release run holds its lock — the next tick advances it")


def status() -> str:
    data = intent()
    if data is None:
        return "no release is open"
    return f"release {data['version']} (asked by {data.get('by') or '?'}) — {data.get('last') or 'not advanced yet'}"
