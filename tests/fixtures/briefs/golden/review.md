Backlog item: T-0001 — Record every payment attempt

## What is already known (do not go looking for it)
Item: T-0001 — Record every payment attempt (task, state New, stage —)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: CODE → REVIEW — the coder pushed
Branch: `task/T-0001` (exists: no)
Head: abc1234 record the provider outcome
Spec: `docs/specs/checkout-resilience.md` (312 lines)
Plan: `docs/plans/checkout-resilience.md` (188 lines)
Review file to write: `docs/reviews/1-t-0001.md` (round 1)
Writes (the footprint this job may touch): app/checkout/attempts.py, tests/test_checkout.py
Tests named by the card: tests/test_checkout.py::test_attempt_row_per_try (new)
Stories of the Feature: S-0001 Hold the order and retry once
Sessions in flight: (none)
Specs live in `docs/specs`, plans in `docs/plans`, reviews in `docs/reviews`.
Branch prefixes: fix → `fix`, plan → `plan`, spec → `spec`, task → `task`.
The trunk is `main`; every commit is signed off (`git commit -s`).

### Description
One row per attempt: provider, outcome, latency, order id. Written whether the attempt succeeded
or failed, and never inside the provider client itself.

### Acceptance
- [ ] `tests/test_checkout.py::test_attempt_row_per_try` passes.

### The last report for this item
REPORT
status: partial
left out: the retry itself, F-0001 owns it

### Standing rules
- Commit with `git commit -s`; the sign-off is the record that you did this work.
- Never push to `main`, never force-push, never `--no-verify`, never open a pull request.
- Push your own branch before your turn ends — after every commit, and once at the end even if
  nothing changed.
- Keep a heartbeat: print a progress line as you go; a silent session is read as a dead one and
  relaunched on top of you.
- Finish with the typed REPORT below, as the last thing you print.
- Anything a human must decide or run: `NEEDS OPERATOR: <what> — <the command or the answer>`.
- Every commit subject names the item: `review(T-0001): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.

## Your job: review T-0001 — round 1

Read, in this order: the writer's report on `task/T-0001`, then the diff against `origin/main`,
then the plan `docs/plans/checkout-resilience.md` for the Task it claims to deliver.

THE VERDICT IS A TABLE. Write `docs/reviews/1-t-0001.md` with exactly this shape — one row per check, and no
row without evidence; a result with no evidence counts as `fail`:

| check | result | evidence |
| --- | --- | --- |
| scope: the diff stays inside `writes:` | pass \| fail | the file, or the one outside it |
| every Step of the Task is implemented | pass \| fail | the step → the code |
| acceptance tests byte-identical to the plan's | pass \| fail | file:line |
| those tests were run and are green | pass \| fail | the run's last line |
| the Gate commands are green | pass \| fail | each command's last line |
| no secret value printed, no background process, no skipped check | pass \| fail | what you looked at |

Then, on its own line, `verdict: approved` or `verdict: changes requested`, then the C list (each
one: file:line and the exact fix) and the I list (what you would change, but will not block on).

Round 1 finds everything: run the whole table before writing a single finding. In a later round,
anything already visible in round 1's diff is not a new C — record it under
`### Missed in round 1` as an I. Rounds are expensive; a round that finds what the last one could
have found is the failure mode this rule exists to stop.

Final message: the verdict line, the failing rows, the gate lines.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `task/T-0001`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started;
if `git status` shows a rebase in progress, finish it first). A lane branch is straight commits
on the trunk: never merge `origin/task/T-0001` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin task/T-0001`. If that is refused as
non-fast-forward, the rebase is why: stop there — do not merge, do not force — and write
`pushed: rebased <sha> — the factory publishes` in the report; the factory publishes a rebased
lane branch itself (B-0056). Never invent an id: a card id comes from `asf new` or the
`BACKLOG_ID_RANGE` this session was given, and a ruling is never a commit in this repo.

Anything a human must decide, answer or run is never guessed and never buried in a comment:
print `NEEDS OPERATOR: <what> — <the command or the answer needed>` on its own line, then carry on
with every part of the job that does not depend on it.

Finish with this, and nothing after it:

```
REPORT
item: T-0001
kind: review
status: done | partial | blocked
branch: task/T-0001
pushed: yes <the sha origin/task/T-0001 now points at> | rebased <sha> — the factory publishes | no — <why>
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
ruling: <adjudicate only — one paragraph: what was disputed, what now holds, what changes; else omit>
blocked_on: <adjudicate only — the id this item must wait for, or none>
writes: <adjudicate only — the corrected footprint, space-separated globs, or none>
superseded_by: <adjudicate only — the id that replaces this item, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
