# The lane as one state machine

F-0087, 2026-09-22. Fifty-three Bugs in thirty-six hours were, six times out of ten, the same
defect: three modules each folding the session registry their own way, each judging "finished",
"pushed", "held" and "reapable" from a different piece of evidence, and disagreeing. This page is
the one model they now share. The module is `asf/workers/lifecycle.py`; its invariants are
`tests/test_lifecycle.py`.

## The states

```
launched ─► running ─► ended(finished | failed: … | dead pid | stopped …)
                          │
                          ├─ finished ──► pushed ─► held(round n) ─► corrected ─► running …
                          │                 │            │
                          │                 │            └─ at the cap ─► adjudicate ─► running …
                          │                 └───────────► landed ─► reaped
                          └─ empty worktree ─────────────────────► reaped
```

| state | what it rests on |
| --- | --- |
| `launched` | a launch line in `sessions.jsonl` (`started` + `pid`), the pid alive, no commit on the branch yet |
| `running` | the pid alive, no `result` in the log's last run |
| `ended` | a `result` in the log's last run, or the pid gone; health records it once as the `ended` line with `end_reason` |
| `pushed` | `end_reason: finished` — written only when the result says ok **and** `origin/<branch>` holds the worktree's HEAD with nothing uncommitted (B-0051) |
| `held` | a `correction` on the run (red gate, conflict, unpushed work); `rounds` counted over every run of the item |
| `corrected` | a later run on the same item started at or after the correction |
| `adjudicate` | held at `ROUND_CAP` (3): the correction is `at_cap`, the feeder emits the ADJUDICATE row once; a second hold at the cap sets `operator_flagged` |
| `landed` | `harvested: <sha>` on the run |
| `reaped` | landed (or empty) and the worktree is gone — the only terminal state |

## The registry: runs, not fields

`sessions.jsonl` stays append-only. A line carrying `started` and `pid` **opens a run** of its
job; every other line for that job updates the latest run. Nothing folds across a launch line,
so a relaunch starts clean without the launcher writing nulls (B-0041). `lifecycle.fold` is the
only fold; `pool.load_sessions`, `harvest.read_sessions` and `harvest.sessions_by_branch` are
views of it (`latest`, `by_branch`). A worktree belongs to the run that recorded it last
(`by_worktree`), whatever the directory is named.

## Evidence, derived state, one recorded transition

`lifecycle.gather` reads the world — the log's last-run result, the pid, `origin/<branch>`, the
tree, HEAD — into an `Evidence`; `lifecycle.derive(run, evidence)` gives the state. Nothing
stores the state a second time. The one transition health *records* is `ended` (timestamp and
reason as judged then), so the feeder, the pool and spawn can ask `is_live` without git. A `dead
pid` verdict is revisited every tick while the run is not landed (B-0028).

`rc` is still written beside `end_reason` for the views; no judgement reads it.

## Who owns what

| question | asked by | answered by |
| --- | --- | --- |
| may this row launch into this worktree? | `spawn.make_worktree` | `may_launch`: refused only while the run owning the worktree is live; an ended run's worktree and branch are taken over, rebased onto the trunk (B-0025, B-0046, B-0048, B-0051); an orphan is refused |
| what `ended` line does this run get? | `health.health`, `step_health.handle_dead` | `judge`: ok + pushed → `finished`; ok + not pushed → `failed: not pushed: n uncommitted file(s), m unpushed commit(s)` and a hold (below); error → `failed[: signature]`; no result + pid gone → `dead pid`; a run with no branch is judged on the result alone |
| is this worktree reapable? | `health.health` | `reap_verdict`: landed (B-0049) · ended-not-finished and empty (B-0025) · finished, pushed, with commits, in the trunk (B-0019); never a live run, never an alive pid |
| may harvest gate this branch? | `harvest.run_product_harvest`, `run_harvest` | `eligible`: finished (hence pushed), not landed, not `harvest: pr` |
| what goes on a held run? | `harvest.hold_with_correction`, `health` (unpushed) | `hold`: the correction, the round over the item, the cap, the line to print |
| what is in flight, how many attempts, which corrections wait? | `step_wave`, the feeder | `inflight`, `attempts`, `corrections` |
| which branches may get a PR? | `step_prs.candidates` | `finished` and not `landed` |

## Rulings made here (D-0049: the factory decides, the operator is informed)

- A pushed branch waiting for harvest holds its item **busy** for the feeder but takes no
  session slot (`awaiting_harvest`): the branch is harvest's, not a session's. Before this the
  feeder relaunched every finished branch until harvest reached it, and the relaunch's live run
  hid the finished one from harvest (the dogfood tick showed it on the first run).
- A correction row runs on the branch the held run was on (`corrections[...]['branch']`), not
  on the item's default prefix — a held `spec/<id>` is corrected on `spec/<id>`.
- Reaping an empty worktree deletes its local branch too; a stray local branch with no worktree
  is reused by the next launch when it carries nothing and refused with the commit count when it
  does (B-0025's rule, now in spawn).
- A session's own REPORT says `pushed: yes <sha> | no — <why>`; `pushed: no` is a failed result
  at the source (`asf.workers.report`), before health measures the same thing against git.
- `asf new` and `asf stale` print their `record:` line on stderr: their stdout is a value a
  script reads (the minted id, the `--json` table).
- The record push retries its rebase up to `PUSH_RETRIES` (3) times while origin keeps moving
  between its fetch and its push; past that the refusal stands and the next run re-derives.

- A run that ends ok without pushing is **held**, not merely failed: its own work is the
  correction's input, the next session runs in the same worktree, and it counts as a round
  (else it would loop until the attempt limit).
- A correction is answered by any run on the item that started at or after it — health and
  harvest write corrections before the wave launches, so same-second is after.
- An orphan worktree (no run recorded it) is never taken over by a launch; it is the operator's.
- A run with no branch recorded (nothing to push) is judged on its result alone.
