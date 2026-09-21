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
Review file for this round: `docs/reviews/1-t-0001.md` (round 1)
Writes (the footprint this job may touch): app/checkout/attempts.py, tests/test_checkout.py
Tests named by the card: tests/test_checkout.py::test_attempt_row_per_try
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
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
