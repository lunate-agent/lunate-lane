# lunate-lane

The Lunate delivery lane: one loop, in one sentence.

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

## First step

`scripts/identity.py` mints an installation token for either App. `tests/test_identity.py` mints a token for
each and makes one harmless read, printing only success or the error — never a token.
