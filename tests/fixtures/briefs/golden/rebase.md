Backlog item: T-0001 — Record every payment attempt

## What is already known (do not go looking for it)
Item: T-0001 — Record every payment attempt (task, state New, stage —)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: CONFLICT → REBASE — PR does not merge cleanly, no session on it
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

## Your job: make T-0001 merge again

`task/T-0001` no longer merges into `main`: PR does not merge cleanly, no session on it. Its review stands and no new review round
follows — so do not change its design, its scope, or anything the review already passed.

Merge `origin/main` into the branch — **merge, never rebase**: the review history is what makes
this branch approvable without another round. Resolve every conflict so BOTH sides survive: never
drop a change that came from `main`, and never drop one of this branch's. A generated file is
regenerated with its script, never hand-merged. A conflicting test file is merged so both sides'
cases still run. If a number, an id or a register row this branch books collides with one `main`
has taken since, re-number this branch's to the next free one and update every reference to it.

ALWAYS PUSH SOMETHING — if the merge turns out to be a no-op, push a report saying so. A session
that ends without a push is counted dead and relaunched on top of you.

GATE before the push: the branch's own gate commands and the touched tests. Paste each one's last
line in the report.

Final message: the pushed sha, the files resolved, the gate lines.

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
kind: rebase
status: done | partial | blocked
branch: task/T-0001
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
