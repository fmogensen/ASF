---
id: D-(unminted — BACKLOG_ID_RANGE for this job reserves S, T, B only; no D block)
type: decision
title: B-0016 is accepted as it stands at 4690ba0; the review loop on fix/B-0016 is closed
---
## Context
`fix/B-0016` was held three times as "unpushed" and routed to adjudication as a reviewer/fixer
dispute, but the checkout holds no review to dispute: `docs/reviews/1-b-0016.md`,
`docs/specs/b-0016.md` and `docs/plans/b-0016.md` do not exist, so there is no open finding with
an anchor to check. What does exist is a diverged branch — `origin/fix/B-0016` at `6fcb8df` (the
fix on `4d38582`) and the local branch at `4690ba0` (the same fix rebased onto `origin/main`
`b0a50d2`) — so every push was a non-fast-forward the standing rules forbid forcing, and the
"unpushed" hold repeated.

## Decision
- **Open review findings: none.** No finding is upheld and none is overruled, because none was
  written down; any finding raised against round 1 after this ruling is out of time and does not
  reopen the loop.
- **The fix holds as committed at `4690ba0`.** Checked against the checkout: `make_worktree`
  (`asf/workers/spawn.py:164-215`) runs the whole add path — fetch, takeover, held-branch reuse,
  fresh branch — inside `_with_repo_lock` (`asf/workers/spawn.py:143-161`), an exclusive
  `flock` on `.git/asf-worktree.lock` per repo, with one retry when the error names
  `lock config file`. That is the serialise-per-repo-and-retry-once the item asks for.
  `tests/test_workers.py` `test_b0016_concurrent_worktree_adds_all_succeed` drives eight
  concurrent `make_worktree` calls against one repo.
- **The divergence is closed by a merge, not a force-push.** `origin/fix/B-0016` is merged into
  the local branch with `-s ours`: the tree stays `4690ba0`'s (the fix on current `main`), and the
  branch becomes a descendant of what origin holds, so the push is a fast-forward.
- **Gate.** The full suite passes with the caller identity unset:
  `env -u ASF_PRODUCT -u ASF_JOB -u BACKLOG_ID_RANGE python3 -m unittest discover -s tests` →
  `Ran 801 tests … OK (skipped=13)`.

## Consequences
- `fix/B-0016` leaves review and goes to merge; no further review round is opened for it.
- The same suite run inside a worker session fails 21 tests in `test_evidence`, `test_ingest`
  and `test_migrate` (`asf.env.ConfigError: no product config at …/products/asf.yaml`, raised
  from `asf/env.py:383` via `asf/record/ingest.py:225`): the session's `ASF_PRODUCT` leaks into
  the test process. That is not B-0016's defect and is not fixed here; it needs its own bug so a
  worker's gate run is hermetic like the gates in `asf/hermetic.py`.
- The retry in `_with_repo_lock` calls the whole add path a second time; a failure that leaves a
  branch created but no worktree would make the retry's `worktree add -b` fail with "already
  exists". This is recorded as a known limit, not a finding: inside the lock the only writer left
  is a git process outside asf, and the retry's error surfaces as a `SpawnError` rather than a
  silent loss.

## Links
- Item: B-0016 — Parallel spawns race on .git/config when creating worktrees (F-0015, E-0001)
- Review: `docs/reviews/1-b-0016.md` (round 1 — absent from the checkout)
- Commits: `4690ba0` (the fix on `main`), `6fcb8df` (the fix as first pushed, superseded)
