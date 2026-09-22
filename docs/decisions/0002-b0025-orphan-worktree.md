---
id: D-7250
type: decision
title: B-0025 is closed by the orphan rule; its health and spawn halves are main's
# ---- machine ----
schema_version: 1
state: New
stage_since: 2026-09-22T00:00:00Z
updated: 2026-09-22T00:00:00Z
---
## Context

`fix/B-0025` was held 34 times on a rebase conflict, not on a reviewer's finding: while it sat,
B-0049 landed a `worktree_empty` reap in `health` and B-0051 landed a "reuse an ended session's
worktree" rule in `spawn`, so both halves of the branch's fix arrived on `main` by another route
and every rebase re-collided with them. The dispute to settle was therefore not *whether* the
branch was right but *what of it is left* once `main` already does the work — and the answer is
one case neither B-0049 nor B-0051 covers: a worktree with **no session line at all**.

## Decision

1. **The branch's `health` half is overruled.** Its `unused_worktree`-driven reap duplicates
   B-0049's rule already on `main` (`asf/workers/health.py`, `worktree_empty` and the
   `end_reason != 'finished'` arm). `main`'s wording stands; the branch's is dropped. This does
   not come back in a later round.
2. **The branch's `spawn` half is upheld, narrowed to the orphan.** B-0051's block already
   reuses the worktree of a session that *ended*. It does not cover `s is None` — the launch
   died between `make_worktree` and the ledger write — and for that case `spawn` still raised
   `worktree already exists` while `health` kept the tree forever, because `pushed()` can never
   clear an orphan that has no pushed branch. That is B-0025's headline scenario and it is now
   closed in both places: `health` reaps an empty orphan, `spawn` discards one and cuts the job
   afresh.
3. **The `alive=` parameter the branch threaded through `spawn`/`make_worktree` is removed.** It
   survived *outside* the conflict markers, so any resolution that took `main`'s side left
   `make_worktree(..., alive=alive)` calling a three-argument function — a `TypeError` on every
   spawn. This is the most likely reason the branch kept coming back held.
4. **`discard_worktree` keeps a branch that exists on origin.** Deleting the local ref would
   strand pushed work that `make_worktree`'s own reuse path is about to check out again.

## Consequences

- A launch that dies before its ledger line no longer strands its job: `health` lists the tree
  `reapable`/`reaped` as `empty orphan` (branch included, or the next `worktree add -b` still
  fails), and `spawn` clears it rather than refusing.
- An orphan that *holds* anything — uncommitted changes or commits ahead of the trunk — is still
  kept and still refused, with what it holds named in the error. Nothing is deleted unexamined.
- "Empty" for the orphan rule means **never committed to** (`has_commits`, B-0019's reflog
  check), not `in_trunk`. A landed orphan is contained in the trunk too, so `worktree_empty`
  alone would have relabelled it `empty orphan` and shadowed the existing `orphan` reap.
- `test_orphan_worktree` chained three stages, and its middle one asserted the bug — an empty
  orphan `keep`-ed on every pass. It is split rather than deleted: the committed stages stay.
- `spawn()` and `make_worktree()` are back to `main`'s signatures; no caller passes `alive`.
- The review loop on `fix/B-0025` ends here. Findings 1 is overruled for good; a later round may
  not re-open it.

## Links

- B-0025 — *A session that ended with nothing committed leaves a worktree that blocks every
  relaunch* (S2, F-0015, E-0001)
- Supersedes the branch's own `merge(B-0025)` resolution (`b218efe`)
- Rests on B-0049 (`cb0cbb7`) and B-0051 (`2380a19`), which carry the two halves this ruling
  declines to duplicate
- Review: `docs/reviews/1-b-0025.md` — not in the record; the hold reason was `conflict`, and no
  review file was ever written
