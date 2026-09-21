Backlog item: F-0001 — Checkout survives a failed payment provider

## What is already known (do not go looking for it)
Item: F-0001 — Checkout survives a failed payment provider (feature, state Active, stage plan-approved)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: STARVED → PLAN — spec approved, no plan
Branch: `plan/F-0001` (exists: no)
Head: abc1234 record the provider outcome
Spec: `docs/specs/checkout-resilience.md` (312 lines)
Plan: `docs/plans/checkout-resilience.md` (188 lines)
Writes (the footprint this job may touch): (none declared)
Tests named by the card: (none named)
Stories of the Feature: S-0001 Hold the order and retry once
Sessions in flight: (none)
Specs live in `docs/specs`, plans in `docs/plans`, reviews in `docs/reviews`.
Branch prefixes: fix → `fix`, plan → `plan`, spec → `spec`, task → `task`.
The trunk is `main`; every commit is signed off (`git commit -s`).

### Description
When the payment provider times out, the checkout must hold the order, retry once with a second
provider, and tell the customer what happened — instead of dropping the basket and returning a
500 with no record of the attempt.

The attempt itself is the thing that is missing today: nothing is written down when a provider
fails, so nobody can tell a timeout from a decline, and support answers every such ticket by
guessing.

### Acceptance
- [ ] A provider timeout holds the order in `pending` and retries once against the fallback.
- [ ] Every attempt is recorded: provider, outcome, latency, and the order it belongs to.
- [ ] The customer sees the retry, not a 500.

### Links the card names
- spec: docs/specs/checkout-resilience.md
- the incident that filed this: B-0001

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

## Your job: write the plan for F-0001

Binding: the approved spec `docs/specs/checkout-resilience.md`. Read it in full first — every section, every fenced
acceptance test. The plan adds no scope the spec does not carry; where the spec is wrong, say so
in the plan's decisions block rather than quietly widening it.

DELIVERABLE: `docs/plans/checkout-resilience.md` on branch `plan/F-0001`, cut from `origin/main`. Create only that file.

TASK LINES (binding, machine-read — the record turns every `### Task N:` heading into a Task card
and the feeder launches coders from them). Directly under each heading, before anything else,
exactly two lines:

    stories: <the Story ids this Task delivers>
    writes: <the path globs this Task's diff may touch>

`writes:` is the same set as the Task's Files block. Two Tasks whose `writes:` intersect are run
one after the other, never together — so keep them disjoint or accept the queue.

Every Task carries, besides those two lines: **Files**, **Steps**, **Gate** (the exact commands),
**Acceptance** (the spec's fenced tests, copied byte for byte).

COVERAGE: the plan is approvable only when every Story of the Feature is on at least one Task's
`stories:` line. The Stories: S-0001 Hold the order and retry once
End the plan with the line `coverage: <covered>/<total> stories; uncovered: <ids or none>`.
Mint Task ids only from `BACKLOG_ID_RANGE`.

Final message: the pushed sha, the plan path, the Task count and the wave order, the coverage line.

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
item: F-0001
kind: plan
status: done | partial | blocked
branch: plan/F-0001
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
