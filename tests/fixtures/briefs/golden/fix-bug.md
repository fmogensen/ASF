Backlog item: B-0001 — Checkout returns 500 when the payment provider times out

## What is already known (do not go looking for it)
Item: B-0001 — Checkout returns 500 when the payment provider times out (bug, state New, stage —)
Severity: S1
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: BUG → FIX — S1 open, decided, no session — its ## Fix is the plan
Branch: `fix/B-0001` (exists: no)
Head: abc1234 record the provider outcome
Spec: `docs/specs/checkout-resilience.md` (312 lines)
Plan: `docs/plans/checkout-resilience.md` (188 lines)
Writes (the footprint this job may touch): (none declared)
Tests named by the card: tests/test_checkout.py::test_timeout_is_pending_not_500
Sessions in flight: (none)
Specs live in `docs/specs`, plans in `docs/plans`, reviews in `docs/reviews`.
Branch prefixes: fix → `fix`, plan → `plan`, spec → `spec`, task → `task`.
The trunk is `main`; every commit is signed off (`git commit -s`).

### Description
Every provider timeout since the last release returns a 500 and drops the basket. Eleven orders
in the last hour.

### Acceptance
- [ ] `tests/test_checkout.py::test_timeout_is_pending_not_500` fails before the fix and passes after.

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

## Your job: fix B-0001 — the incident lane

This is the incident lane, not the Feature ladder: **no spec, no plan, no review round**. The
card's `## Fix` IS the plan, and the test it names is the acceptance. Everything below is the
whole of the process.

THE FIX, from the card:
Catch the provider's timeout in `app/checkout/pay.py` and return the `pending` path already used
for a declined card, instead of letting the exception reach the handler. The retry itself is
F-0001's work, not this fix's.

Test: `tests/test_checkout.py::test_timeout_is_pending_not_500`.

THE TEST THAT CLOSES IT: tests/test_checkout.py::test_timeout_is_pending_not_500
Write that test FIRST and watch it fail, then make it pass. The harvest requires it by name: a
fix that lands without a test that failed before it is not a landed fix, it is a claim.

Branch `fix/B-0001`, cut from `origin/main` — the row names it; do not cut a second one.

Do the smallest change that makes the test pass. A refactor you noticed on the way, a second bug
you found next to this one, a rename that would be tidier: all of those go in the report, not in
this diff. Speed here is the point — the lane exists so an incident closes in hours while the
Feature work waits.

Every commit subject names the card — `fix(B-0001): <what>` — because a commit on the trunk that
names the id is what turns the card `Resolved`; a fix nobody can trace to the card does not exist.

If the fix is ALREADY on `origin/main` when you look (someone landed it before this session):
verify it against THE FIX above and run the test that covers it, then make one empty, signed commit
on your branch — `git commit --allow-empty -s -m "fix(B-0001): already landed in <sha> — verified
by <test name>"` — and push. That commit is the evidence the record needs; a branch pushed with no
commit closes nothing and the lane relaunches this session every wave.

Final message: the pushed sha, the test's name and the run's last line, what you changed.

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
item: B-0001
kind: fix-bug
status: done | partial | blocked
branch: fix/B-0001
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
