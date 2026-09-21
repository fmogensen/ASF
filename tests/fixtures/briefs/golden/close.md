Backlog item: T-0001 — Record every payment attempt

## What is already known (do not go looking for it)
Item: T-0001 — Record every payment attempt (task, state New, stage —)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: STALE → CLOSE — PR closed unmerged, branch left behind
Branch: `task/T-0001` (exists: no)
Head: abc1234 record the provider outcome
Spec: `docs/specs/checkout-resilience.md` (312 lines)
Plan: `docs/plans/checkout-resilience.md` (188 lines)
Writes (the footprint this job may touch): app/checkout/attempts.py, tests/test_checkout.py
Tests named by the card: tests/test_checkout.py::test_attempt_row_per_try
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

## Your job: close T-0001 out

It is stale: PR closed unmerged, branch left behind. No session is moving branch `task/T-0001`.

This is one line of work, and no more than one. Write the single line that says **why** it is
closed — what was attempted, and what ended it — and put that line in your report. Do not revive
the work, do not re-open the diff, do not delete anyone's branch, do not touch `main`.

If the work should in fact continue, that is a recommendation, not a decision you take here: say
so in the report in one line and stop. If closing it needs a human — a customer promise, money, a
credential — print `NEEDS OPERATOR:` with the question instead of guessing.

Final message: the one line, and whether the item should be re-opened.

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
kind: close
status: done | partial | blocked
branch: task/T-0001
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
