# lunate-lane

**The tooling that drives Lunate's AI workers.** It is not part of the product: it is what decides which
card a worker picks up, opens the pull request as the `lunate-worker` identity, has `lunate-reviewer` review it,
and lets GitHub's merge queue land it. License: MIT.

The lane is one loop, in one sentence.

**card → worker → PR → CI → review → auto-merge**, and Daniel steps in only when something fails.

This repository holds the lane's scripts and their tests. Nothing else. Runtime state — locks, snapshots,
markers, logs — lives in `~/.local/state/lunate-lane`, never here. Cron jobs run only from this repository's
main checkout; changes happen in worktrees and reach main only after Daniel has reviewed them.

## The design rules

1. **An unreadable input is never a pass.** Every read failure is reported and stops the decision. This is the
   first rule because the old lane got it wrong: `lunate-pr-ready.py`'s `sh()` returned `""` on any exception,
   the changed-file list became empty, and the docs-only branch's `all([])` was `True` — so a gate that could
   not read the repository reported "no fragment required". A gate that fails open is worse than no gate.
2. **GitHub is the state machine.** Draft/ready, review state, merge-queue position and required checks are
   already modelled; the lane does not keep a second copy in labels or markers.
3. **The reviewer's verdict is a real PR review** from the `lunate-reviewer` identity — approve or request
   changes. Never a label, never a comment that only the lane understands.
4. **One identity authors, another reviews.** The author is `lunate-worker`; nobody approves their own work,
   and Daniel's personal token is used by neither.
5. **"Done" means merged and running.** A branch is written, not done.
6. **One immutable record of what ran.** History is never rewritten, not even for a rename.

## Credentials

`~/.config/lunate-lane/app.json` holds the App ids and key paths; the private keys are
`lunate-worker.pem` and `lunate-reviewer.pem`, mode 600 in a directory mode 700. The model key is
`~/.config/lunate-lane/env`, mode 600 (see Configuration). They are never printed,
copied into a repository, or passed to a model. Installation tokens are minted per run and are short-lived.

## How the loop runs

Three cron jobs and one detached driver. Each cron job is one script, takes its own lock
(`<state>/locks/<job>.lock`, a kernel `flock`, so two runs never overlap and a crash never leaves it held),
logs to `<state>/logs/<job>.log`, and exits non-zero when a read failed.

1. **Feeder** (`scripts/feed.py`, every 10 minutes). Reads the open issues of the milestone, every open pull
   request, the worker identity's pull requests in full, and the running-worker records, then starts:
   - **rework first**: a worker pull request whose reviewer requested changes *on its current head* goes back to
     a worker on the same branch, with the review's findings in the brief. So does a **stuck** one — ready (not a
     draft) and red on a required check, `CONFLICTING` with main, or approved but removed from the merge queue
     (neither queued nor armed, and the queue's latest removal was of the current head — one from before the last
     push does not count; when a person turned auto-merge off last, that is their decision and not a rework) — with the failing check names and the tail
     of each failing job's log, the conflicting files, or the queue's reason in the brief. A failure that is the
     runner's fault — every failing run cancelled, timed out or failed to start with no failing step (the
     `ci-ok` roll-up failing only because such legs did counts too) — is not a rework: the feeder reruns the
     failed jobs once for that head (`gh run rerun <id> --failed`, recorded in `<state>/reruns/`), and only a
     second such failure on the same head, or any real failing step, goes back to a worker. Conflicts are resolved
     by merging `origin/main` into the branch, never by rebasing; the lane never force-pushes (a rewritten branch
     is refused). Then the named tests, the push, and a new review (the ruleset dismisses the stale approval). A
     rework does not add to WIP — its card is already in flight;
   - **then new cards**, in order: the pins file (if any), then `kind/bug`, then cards that other open issues
     depend on (native GitHub dependency edges), then oldest first. A card waiting on an open issue is skipped;
     so is a card any open pull request (any author) already carries or closes.
   - **limits**: total WIP ≤ `wip_limit`, where WIP is the cards in flight — each open worker pull request plus
     each running worker, a worker and its own pull request counted once — and at most one card in flight per
     `area/*` label. A card without an area label has no area limit.
   - **parked cards**: a card is parked when its worker runs ended with no new commit three times in a row (new
     card or rework), or when one pull request has gone back to a worker three times, whatever the causes — the
     feeder parks it instead of starting a fourth rework. The feeder no longer starts or reworks a parked card, its
     open pull request still counts toward WIP, the run that parked it says so once in a comment on the pull
     request (on the issue if there is none), and the digest lists it. A new commit resets the idle-run count (and
     lifts an idle park) but not the rework rounds. To give a parked card back to the lane, delete
     `<state>/attempts/<card>.json`; that clears both counts.
   - **re-arming**: a worker pull request that is ready, approved at its head, green, not conflicting, neither
     queued nor armed — the state a clean finish leaves when arming fails after it was marked ready — is armed
     again with the queue's method and read back. Not when a person turned auto-merge off last.
   - **tidying, after the launches** (`scripts/cleanup.py`): a card whose lane pull request (`wt/issue-N`) is merged
     or closed, with none open and no worker running, loses its worktree, its local branch and its scratch
     directory (a merged one first has its merge commit's parent count logged — 1 is the squash the queue should
     make) — but only when nothing would be lost: no uncommitted or untracked change in the worktree, and every
     commit on the branch on an origin ref or a pull request's head as GitHub has it. Nothing is forced
     (`git worktree remove`, the branch deleted only at the tip that was checked). A card that fails the check
     is kept, logged, and listed in the digest under "Kept by cleanup" for a person. Logs over 5 MB are rotated (three generations kept), Pi event logs older than 14 days are deleted, and
     review sessions left by a killed review run are cleared. A cleanup that fails fails the tick by name, but only
     after the launches.
2. **Worker** (`scripts/worker.py --issue N [--rework PR]`, started detached by the feeder). Cuts or continues
   the worktree `<state>/worktrees/issue-N` of the checkout on `wt/issue-N`, renders `templates/worker-brief.md`
   with the issue body, and runs one Pi session on the worker model. The session has no GitHub credential; the
   lane pushes. At the branch's first commit the pull request is opened **as a draft**, without auto-merge (so
   the worker can name its `changes/<pr>.md` fragment, and Lunate's CI skips its heavy legs on work still being
   written); when the session ends with commits the branch is pushed again, the session's final report is posted
   on the pull request, and — only if the run was clean (exit 0, not stalled, nothing uncommitted, a new commit, a
   real final message) — the pull request is marked ready and then auto-merge is armed. Otherwise it stays a draft
   with auto-merge off, with a comment naming why, and the orchestrator takes over. A rework returns its pull
   request to draft, with auto-merge off, before its session starts, and is made ready again only at its own clean
   finish. The draft and auto-merge state in the comment and the log are read back from
   GitHub, never assumed. Auto-merge is armed with the merge queue's own method, read from the base branch's
   rules (no queue rule is a refusal, not a guess). GitHub may record a different `mergeMethod` on the pull
   request than the one sent (it recorded `MERGE` on #813, armed with `SQUASH`); the log shows both, and what
   actually lands is checked on the merged commit's parent count. The final report is scanned before posting: anything token- or key-shaped (GitHub
   tokens, `sk-` keys, private-key blocks, AWS keys, JWTs, `key=…`/`token=…` assignments, long opaque strings, or
   the model credential itself) replaces it with a fixed "output withheld" note, and the worker log says so.
   **Before every push** the diff's added lines (against `origin/main`), the pull request's title and its body
   are scanned for the shapes that are secrets by construction (private keys, GitHub tokens, `sk-`/`oc_` keys,
   AWS keys, JWTs) and the model credential. A match pushes nothing, parks the card, comments once naming the kind
   and where it was found, and logs the same — never the value. (The looser assignment and opaque-string rules
   apply to the report only: on a diff they would fire on ordinary code such as `cancellationToken = …`.)
   Stopping a worker with `kill <pid>` (SIGTERM, also SIGHUP or SIGINT) kills its Pi session's process group on
   the way out. If the driver is SIGKILLed, its record names the session's group, and the next feeder or
   reviewer tick that finds the driver dead kills that session before clearing the record.
3. **Reviewer** (`scripts/review.py`, every 10 minutes). For each open worker pull request that is not a draft,
   has no running worker, has every *required* check green on its head, has no review from
   `lunate-reviewer` at that head, and is not held back by an *older*, ready (not draft) open pull request (any author) that changes
   the same OpenSpec capability (`openspec/specs/<cap>/…` or a change's `specs/<cap>/…` delta) — the older one
   lands first: runs a Pi session on a reviewer model — `models.reviewer.full` (`opencode-go/gpt-6-luna`) for any
   pull request touching `src/`, `tests/`, `openspec/` or `.github/` (or whose file list is empty), and
   `models.reviewer.light` (`opencode-go/glm-5.3-flash`) for docs-only; the config refuses either one equal to the
   worker's, and the posted review names the model it ran on — in a detached worktree at the head, with the diff and the issue in `templates/review-brief.md`. The
   brief also carries the other open pull requests on the same capabilities and the worker's session report: the
   reviewer compares the spec delta with main's spec and with theirs, and checks the worker's **Intent
   decisions** (the grill-me questions it answered itself). The worker's brief says a conflict in
   `openspec/specs/**` is resolved by reverting the archive commit, merging `origin/main` and re-archiving, never
   by hand. The
   session writes a JSON verdict; the lane re-reads the pull request and, only if it is still open at the same
   head, posts a real review — `APPROVE` or `REQUEST_CHANGES` with the findings — as `lunate-reviewer`. It then
   reads the review back, and if GitHub names any other author the run exits non-zero. A review run records
   itself in `<state>/reviewing/<pr>.json` with its session's group; if it is `kill -9`ed, the next review or
   feeder tick kills that exact session and removes its worktree, as for workers. A submitted review cannot
   be deleted, so a person has to dismiss it. A missing or malformed verdict posts nothing.
4. **Merging** is GitHub's: the ruleset (one approval, code-owner review for `adr/`, required checks, merge queue)
   plus the auto-merge the worker armed.
5. **Digest** (`scripts/digest.py`, daily). One short message on stdout and in `<state>/digest/latest.md`: open
   issues in the milestone; WIP against its limit, with what is in flight (as the feeder counts it); every open
   pull request with its state; what is stuck and why (a worker pull request that is red, conflicting or out of
   the merge queue, and any pull request open for more than a day); the parked cards and why; and what merged
   since yesterday, with titles.

## Files

| File | What it does |
| --- | --- |
| `lane.json` | The one config: repo slug, milestone, base, WIP limit, App slugs, the worker model and the full and light reviewer models, labels, Pi harness settings, paths. |
| `scripts/common.py` | Config loading, the named errors, `gh`/`git`/GraphQL/REST calls that raise instead of returning empty, templates, logs, locks, the cron job wrapper. |
| `scripts/github.py` | Reads of GitHub's state: milestone cards with dependency edges, open pull requests, one pull request in full, required checks; the check and review verdicts. |
| `scripts/cards.py` | The feeder's pure decisions: order, pins, WIP and area limits, rework before new work. |
| `scripts/harness.py` | Runs one headless Pi session under `sandbox-exec` with a minimal environment, its own HOME and a stall watchdog; the running-worker records, orphan clean-up, idle-run and rework counts. |
| `scripts/secret_scan.py` | The secret shapes, shared by the report scan and the scan before every push. |
| `scripts/feed.py` | Cron entry point: the feeder. |
| `scripts/cleanup.py` | The feeder's tidying: finished cards' worktrees and branches, log rotation, dead review runs. |
| `scripts/worker.py` | The detached driver for one card's worker session. |
| `scripts/review.py` | Cron entry point: reviewer dispatch. |
| `scripts/digest.py` | Cron entry point: the daily digest. |
| `scripts/dispatch_card.py` | A card becomes (or reuses) its pull request: reads the issue, checks the branch, writes the body. |
| `scripts/open_pr.py` | Pushes and opens a pull request as `lunate-worker`, optionally arms auto-merge, comments as the worker. |
| `scripts/identity.py` | Mints installation tokens for either App. |
| `templates/worker-brief.md` | The worker's fixed brief: OpenSpec lifecycle, archive last, the changelog fragment, run only the named tests. |
| `templates/review-brief.md` | The reviewer's brief and the verdict file format. |

## Configuration

`lane.json` is checked in. Runtime inputs outside the repository:

- `~/.config/lunate-lane/app.json` — App and installation ids and key paths (see Credentials).
- `~/.config/lunate-lane/pins.json` — optional, `{"cards": [812, 790]}`: these cards first, in this order. Pins
  change the order only, never the limits. Absent means no pins; a malformed file stops the feeder.
- The model credential for Pi: `~/.config/lunate-lane/env` (`harness.credential_file`), a dotenv file holding
  `OPENCODE_GO_API_KEY=…` (`harness.credential_key`). It must be mode 600 — any other mode is refused and the
  session does not start. It is exported to the session as `harness.credential_env` and never logged or put in
  argv. (The old lane's `~/.hermes/.env` is no longer read.)
- State: `~/.local/state/lunate-lane/`: `locks/`, `logs/`, `workers/` (one pid file per running worker, with
  its session's process group), `attempts/` (idle-run counts; a parked card's file is deleted to unpark it),
  `worktrees/`, `reviews/`, `reviewing/` (one record per running review), `scratch/` (each session's TMPDIR and its own `home/`), `sessions/` (Pi event logs),
  `digest/`.
- The session's HOME: `<scratch>/home`, wiped and reseeded before every session. It gets copies of
  `~/.pi/agent/settings.json`, `models-store.json` and `bin/fd` (not `auth.json`; the credential is passed in the
  environment), and a `.gitconfig` holding only the checkout's `user.name` and `user.email`. `NUGET_PACKAGES`
  points at the real `~/.nuget/packages` cache.

## Tests

    cd ~/dev/lunate-lane
    .venv/bin/python -m pip install -r requirements.txt   # once, or after requirements.txt changes
    .venv/bin/python -m unittest discover -s tests -v

Every decision has a unit test without network: ordering, pins, WIP and area limits, rework before new work,
review skipping, the re-check before posting, and the error paths (a failed read stops the decision). The
worker tests use real temporary git repositories. `tests/test_identity.py` is the one live test: it mints a token
for each App and makes one harmless read, and skips when `app.json` is absent.

Each cron job also has a safe manual mode: `scripts/feed.py --dry-run` and `scripts/review.py --dry-run` say what
they would start, and start nothing.

## Installing the crons (for the orchestrator)

Not installed by this repository. Run them from the main checkout only, with this repository's `.venv`. Add to
the orchestrating user's crontab (`crontab -e`):

    PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
    LANE=/Users/danielwallner/dev/lunate-lane
    */10 * * * *  cd $LANE && .venv/bin/python scripts/feed.py   >> $HOME/.local/state/lunate-lane/logs/cron.out 2>&1
    5-59/10 * * * * cd $LANE && .venv/bin/python scripts/review.py >> $HOME/.local/state/lunate-lane/logs/cron.out 2>&1
    0 7 * * *     cd $LANE && .venv/bin/python scripts/digest.py

`PATH` must reach `gh`, `git` and `pi`. The digest prints its message on stdout, so whatever runs that line
delivers it (cron mail, or the orchestrator's own scheduler capturing stdout). Before the first real tick, run
`feed.py --dry-run` and `review.py --dry-run` by hand and read what they would do. Then retire the old jobs in
`~/.hermes` that feed, review or label the same repository, so two lanes never drive it at once.

## Known limitations

- **The sandbox is the v1 boundary; a separate macOS user is the follow-up.** Every Pi session — worker and
  reviewer — runs under `sandbox-exec` with a profile (`harness.sandbox_profile`) that:
  - denies **reading** `~/.config/lunate-lane` (the App keys, `app.json`, the model key, the push's askpass
    files), `~/.hermes`, `~/.ssh`, `~/.config/gh` and `~/Library/Keychains`;
  - denies **writing** everything the unsandboxed lane later runs or trusts: the lane's own code (this checkout
    and the main one), the whole state directory except the session's own scratch and worktree (so no worker,
    attempts, review or rerun record, no other session's files, not the push mirror), the checkout's
    `.git/config`, `.git/hooks` and per-worktree `config.worktree`, and the user's `~/.gitconfig` and
    `~/.config/git`;
  - denies process-info calls on any process outside the session's own sandbox.

  The lane's own git never trusts what a session could have influenced anyway: every git it runs has
  `core.fsmonitor`, `core.hooksPath` and the credential helper neutralised and runs without system or global
  config. Every path the lane deletes is resolved and refused unless it lies inside the state directory, and a
  review record must name exactly its own `reviews/pr-<n>`.

  What it does not cover:
  - `sysctl(KERN_PROCARGS2)` is not mediated by the sandbox on this macOS, so a session can still read the
    argv and environment of any process of the same user. The lane therefore keeps secrets out of both: the
    push's token reaches git through a `GIT_ASKPASS` helper that reads a mode-600 file in a denied directory,
    and answers only a prompt for exactly `https://x-access-token@github.com`. The push itself goes from a
    lane-private bare mirror (`<state>/push-mirror.git`, whose config is checked) to the explicit URL
    `https://github.com/lunate-agent/lunate.git`, with system and global config off and hooks pointing nowhere,
    so nothing a session writes into the checkout's config (a `pushurl`, an `insteadOf`) decides where the
    token goes. The model key is in the session's own environment by design.
  - Filters and diff drivers named by a branch's `.gitattributes` still run in the lane's git when the
    checkout's own `.git/config` defines them; sessions cannot write that file, and the lane's git ignores the
    global and system config where a planted one would otherwise sit.
  - The login keychain's *service* (securityd) is reachable over Mach IPC even though its files are denied;
    sessions get no git credential helper (see below), so nothing in them asks it for a GitHub credential.
  - `sandbox-exec` is deprecated by Apple and the profile language is undocumented; a macOS update could change
    what it enforces. `tests/test_harness.py` checks the profile against the real `sandbox-exec` on every run.

  The session still runs with `--approve` as the lane's uid. Running sessions as a separate macOS user that
  cannot read the keys, write the lane, or see the lane's processes remains the follow-up.
