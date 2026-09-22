---
id: D-7850
type: decision
title: B-0016 is accepted as it stands; the review loop on fix/B-0016 is closed and does not reopen
superseded_in_part_by: D-7850, D-7851
---
> **Final ruling: [`docs/decisions/D-7851.md`](../decisions/D-7851.md).** The loop reopened a fourth
> time after D-7850 closed it, with the worktree left mid-`rebase` replaying `6fcb8df` again.
> D-7851 affirms D-7850 in substance entirely — no finding, the fix stands — and changes two
> things: the handover moves from the local branch `fix/B-0016-clean` to a formula over origin refs
> (a remedy only visible inside one worktree is why nothing happened), and **adjudication is
> refused for this item from here**. B-0016 is not disputed; it is blocked on the operator. Read
> D-7851 for what now holds.

> **Filed as [`docs/decisions/D-7850.md`](../decisions/D-7850.md).** That card mints this ruling's
> id and affirms everything below about the finding (there is none) and the fix (it stands). It
> **overrules one thing**: the `-s ours` merge `59e8ebf` recorded below as the way the divergence
> "stays closed … not by a force-push". It does not stay closed — the merge drags the superseded
> `6fcb8df` into `origin/main..HEAD`, and harvest's rebase flattens merges and replays it, which is
> the `conflict in asf/workers/spawn.py` hold. Read D-7850 for what now holds.

## Context
`fix/B-0016` was routed to adjudication as a reviewer/fixer dispute after three holds, but there
is no dispute on the record: `docs/reviews/1-b-0016.md` has never existed on any branch
(`git log --all -- docs/reviews/1-b-0016.md` returns nothing), and neither has
`docs/specs/b-0016.md` or `docs/plans/b-0016.md`. What actually held the branch three times was
mechanical, not substantive — a diverged branch that could only be pushed by forcing, which the
standing rules forbid — and, at the time this ruling was written, a half-finished interactive
rebase left in the worktree that was replaying the superseded pre-`lifecycle` variant of the fix
(`6fcb8df`) on top of the current one (`4690ba0`) and would have regressed `asf/workers/spawn.py`.

## Decision
- **Open review findings: none.** None is upheld and none is overruled, because none was ever
  written down. Any finding raised against round 1 after this ruling is out of time: it does not
  reopen this loop and must be filed as its own item against `main`.
- **The fix holds as committed.** Checked against the checkout, not against either party's claim:
  `make_worktree` (`asf/workers/spawn.py:164-215`) runs the entire add path — fetch, takeover,
  held-branch reuse, fresh branch — inside `_with_repo_lock` (`asf/workers/spawn.py:143-161`),
  which takes an exclusive `fcntl.flock` on `.git/asf-worktree.lock` per repo
  (`_repo_lock_path`, `asf/workers/spawn.py:139-140`) and retries the closure once when the error
  names `lock config file`. That is exactly the "serialise worktree creation per repo (a lock dir
  or a per-repo mutex) and retry once" the item asks for.
  `tests/test_workers.py::test_b0016_concurrent_worktree_adds_all_succeed` drives eight
  concurrent `make_worktree` calls against one repo and asserts every one of them lands.
- **The prior ruling's third consequence is overruled as factually wrong.** `f8bfe5b` recorded a
  "known limit": that after a partial failure leaving a branch created but no worktree, the
  retry's `worktree add -b` would fail with "already exists". It does not. On the second call the
  reaped-branch arm (`asf/workers/spawn.py:204-211`) sees a local branch with no worktree, finds
  it carries nothing (`rev-list --count origin/main..branch` is `0`), deletes it and remakes it.
  `add` is safe to call twice, as `_with_repo_lock`'s docstring requires. This is now pinned by
  `tests/test_workers.py::test_b0016_retry_runs_the_add_path_again_over_a_half_made_worktree`.
- **Gate.** The full suite passes with the caller identity unset:
  `env -u ASF_PRODUCT -u ASF_JOB -u BACKLOG_ID_RANGE python3 -m unittest discover -s tests` →
  `Ran 801 tests in 406.203s … OK (skipped=13)`, plus `tools/check_generic.sh` → `clean`.
- **The divergence stays closed by the merge already in history, not by a force-push.** `59e8ebf`
  merged `origin/fix/B-0016` with `-s ours`: the tree remains `4690ba0`'s and the branch is a
  descendant of what origin holds, so the push is a fast-forward. The stray rebase was aborted
  rather than continued, for the reason in Context.

## Consequences
- `fix/B-0016` leaves review and goes to merge. No further review round is opened for it; a
  reviewer returning changes on round 1 after this is to be refused by reference to this card.
- The retry path gains a regression test, so the behaviour the prior ruling mis-recorded as a
  limit is now asserted rather than assumed.
- The `-s ours` merge means `6fcb8df`'s tree is deliberately not in the result. Anyone reading
  `asf/workers/spawn.py` history should take `4690ba0` as the fix; `6fcb8df` is the same fix
  written before `lifecycle.py` landed and is superseded, not lost.
- A worker session's own `ASF_PRODUCT` leaks into the test process and fails tests in
  `test_evidence`, `test_ingest` and `test_migrate` — `asf.env.ConfigError: no product config at
  …/products/asf.yaml`, raised at `asf/env.py:383` from `asf/record/ingest.py:225`. Reproduced
  here: `python3 -m unittest tests.test_migrate` is `FAILED (errors=4)` with the session's
  identity set and `OK` without it; the whole suite is 21 failures/errors the same way. That is
  not B-0016's defect and is not fixed here — it needs its own bug, so that a worker's gate run
  is hermetic the way `asf/hermetic.py` makes the gates. The gate for this branch was therefore
  run with the caller identity unset.

## Links
- Item: B-0016 — Parallel spawns race on .git/config when creating worktrees (F-0015, E-0001)
- Review: `docs/reviews/1-b-0016.md` (round 1 — never written; nothing to answer)
- Supersedes: the ruling recorded in `f8bfe5b`, whose third consequence is overruled above
- Commits: `4690ba0` (the fix on `main`), `59e8ebf` (the `-s ours` merge), `6fcb8df` (superseded)
