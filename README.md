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
`lunate-worker.pem` and `lunate-reviewer.pem`, mode 600 in a directory mode 700. They are never printed,
copied into a repository, or passed to a model. Installation tokens are minted per run and are short-lived.

## How the loop runs

Three cron jobs and one detached driver. Each cron job is one script, takes its own lock
(`<state>/locks/<job>.lock`, a kernel `flock`, so two runs never overlap and a crash never leaves it held),
logs to `<state>/logs/<job>.log`, and exits non-zero when a read failed.

1. **Feeder** (`scripts/feed.py`, every 10 minutes). Reads the open issues of the milestone, every open pull
   request, the worker identity's pull requests in full, and the running-worker records, then starts:
   - **rework first**: a worker pull request whose reviewer requested changes *on its current head* goes back to
     a worker on the same branch, with the review's findings in the brief. It does not add to WIP — its card is
     already in flight;
   - **then new cards**, in order: the pins file (if any), then `kind/bug`, then cards that other open issues
     depend on (native GitHub dependency edges), then oldest first. A card waiting on an open issue is skipped;
     so is a card any open pull request (any author) already carries or closes.
   - **limits**: total WIP ≤ `wip_limit`, where WIP is the cards in flight — each open worker pull request plus
     each running worker, a worker and its own pull request counted once — and at most one card in flight per
     `area/*` label. A card without an area label has no area limit.
   - **parked cards**: a card whose worker runs ended with no new commit three times in a row (new card or
     rework) is parked. The feeder no longer starts or reworks it, its open pull request still counts toward
     WIP, and the run that parked it says so in a comment on the pull request (on the issue if there is none). A
     new commit resets the count. To give a parked card back to the lane, delete `<state>/attempts/<card>.json`.
2. **Worker** (`scripts/worker.py --issue N [--rework PR]`, started detached by the feeder). Cuts or continues
   the worktree `<state>/worktrees/issue-N` of the checkout on `wt/issue-N`, renders `templates/worker-brief.md`
   with the issue body, and runs one Pi session on the worker model. The session has no GitHub credential; the
   lane pushes. At the branch's first commit the pull request is opened **without** auto-merge (so the worker can
   name its `changes/<pr>.md` fragment); when the session ends with commits the branch is pushed again, the
   session's final report is posted on the pull request, and auto-merge is armed — only if the run was clean (exit
   0, not stalled, nothing uncommitted, a new commit, a real final message). Otherwise auto-merge is left off — and
   turned **off** before the push when a rework lands on a pull request that was armed — with a comment naming
   why, and the orchestrator takes over. The auto-merge state in the comment and the log is read back from
   GitHub, never assumed. Auto-merge is armed with the merge queue's own method, read from the base branch's
   rules (no queue rule is a refusal, not a guess). GitHub may record a different `mergeMethod` on the pull
   request than the one sent (it recorded `MERGE` on #813, armed with `SQUASH`); the log shows both, and what
   actually lands is checked on the merged commit's parent count. The final report is scanned before posting: anything token- or key-shaped (GitHub
   tokens, `sk-` keys, private-key blocks, AWS keys, JWTs, `key=…`/`token=…` assignments, long opaque strings, or
   the model credential itself) replaces it with a fixed "output withheld" note, and the worker log says so.
   Stopping a worker with `kill <pid>` (SIGTERM, also SIGHUP or SIGINT) kills its Pi session's process group on
   the way out. If the driver is SIGKILLed, its record names the session's group, and the next feeder or
   reviewer tick that finds the driver dead kills that session before clearing the record.
3. **Reviewer** (`scripts/review.py`, every 10 minutes). For each open worker pull request that is not a draft,
   has no running worker, has every *required* check green on its head, and has no review from
   `lunate-reviewer` at that head: runs a Pi session on the reviewer model (the config refuses one equal to the
   worker's) in a detached worktree at the head, with the diff and the issue in `templates/review-brief.md`. The
   session writes a JSON verdict; the lane re-reads the pull request and, only if it is still open at the same
   head, posts a real review — `APPROVE` or `REQUEST_CHANGES` with the findings — as `lunate-reviewer`. It then
   reads the review back, and if GitHub names any other author the run exits non-zero. A submitted review cannot
   be deleted, so a person has to dismiss it. A missing or malformed verdict posts nothing.
4. **Merging** is GitHub's: the ruleset (one approval, code-owner review for `adr/`, required checks, merge queue)
   plus the auto-merge the worker armed.
5. **Digest** (`scripts/digest.py`, daily). One short message on stdout and in `<state>/digest/latest.md`: open
   issues in the milestone, every open pull request with its state, the last 24 hours' merges, and pull requests
   open for more than a day with the reason they are stuck.

## Files

| File | What it does |
| --- | --- |
| `lane.json` | The one config: repo slug, milestone, base, WIP limit, App slugs, worker and reviewer models, labels, Pi harness settings, paths. |
| `scripts/common.py` | Config loading, the named errors, `gh`/`git`/GraphQL/REST calls that raise instead of returning empty, templates, logs, locks, the cron job wrapper. |
| `scripts/github.py` | Reads of GitHub's state: milestone cards with dependency edges, open pull requests, one pull request in full, required checks; the check and review verdicts. |
| `scripts/cards.py` | The feeder's pure decisions: order, pins, WIP and area limits, rework before new work. |
| `scripts/harness.py` | Runs one headless Pi session with a minimal environment, its own HOME and a stall watchdog; the secret-shape scan; the running-worker records, orphan clean-up and idle-run counts. |
| `scripts/feed.py` | Cron entry point: the feeder. |
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
- The model credential for Pi: `harness.credential_file` (a dotenv file) under `harness.credential_key`; it is
  exported to the session as `harness.credential_env` and never logged or put in argv.
- State: `~/.local/state/lunate-lane/`: `locks/`, `logs/`, `workers/` (one pid file per running worker, with
  its session's process group), `attempts/` (idle-run counts; a parked card's file is deleted to unpark it),
  `worktrees/`, `reviews/`, `scratch/` (each session's TMPDIR and its own `home/`), `sessions/` (Pi event logs),
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

- **Sessions run as the same macOS user as the lane.** A session has its own HOME and no GitHub credential in
  its environment, but it runs with `--approve` and the lane's uid. It could still read
  `~/.config/lunate-lane/app.json` and the App private keys by absolute path, mint a reviewer token and approve
  its own pull request. It could also read a push's environment (`ps -E`) while the push runs. The output scan
  and the review author read-back catch some misuse after the fact, but neither is a boundary. The fix is to run
  sessions as a separate macOS user that cannot read the keys or the lane's processes. That is deferred to a
  later round.
