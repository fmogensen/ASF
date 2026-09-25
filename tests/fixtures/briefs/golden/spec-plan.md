Backlog item: F-0001 — Checkout survives a failed payment provider

## What is already known (do not go looking for it)
Item: F-0001 — Checkout survives a failed payment provider (feature, state Active, stage plan-approved)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: CARD → SPEC+PLAN — decided card, size s: spec and plan in one session
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
- Every commit subject names the item: `plan(F-0001): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.

## Your job: write the spec and the plan for F-0001 — one document

This Feature is small (`size: s`): its spec and its plan are one document, written in one
session, with no separate review round. The card above is the scope — read it as the
requirement; everything else is context. Write the document and nothing else: no code.

DELIVERABLE, on branch `plan/F-0001`, cut from `origin/main` — or, when `plan/F-0001` is already on
origin (`exists:` above), on that branch as it stands in your worktree:
- `docs/plans/checkout-resilience.md` — the one document: the spec and the plan together (the SHAPE below).
- `docs/specs/checkout-resilience.md` — two lines only: the Feature's title as a heading, then
  `The spec and the plan are one document: docs/plans/checkout-resilience.md.` (coders read the spec from the trunk,
  so it must be there; it carries no text of its own).
Create only those two files.

Every commit subject names the card — `plan(F-0001): <what>` — the lane refuses a branch whose
subjects do not name F-0001 as a token (the branch name does not count).

SHAPE of `docs/plans/checkout-resilience.md`, in this order — short, the Feature is small:
1. **Decisions** — what is in and what is out, and one row per choice you made (chosen, against
   what, why).
2. **The design** — what changes, where, what it looks like when it is there.
3. **Acceptance tests** — by path, as fenced blocks that can be run.
4. **`## Stories`** — one line per Story: `- S: <title> — acceptance: <the test that proves it>`.
   The Stories the record already holds for this Feature: S-0001 Hold the order and retry once
5. **The Tasks** — TASK LINES (binding, machine-read: the record turns every `### Task N:`
   heading into a Task card and the feeder launches coders from them). Directly under each
   heading, before anything else, exactly three lines:

       stories: <the Story ids this Task delivers>
       writes: <the path globs this Task's diff may touch>
       after: <the Tasks whose landed code this one needs, as `Task 1, Task 2` — or `none`>

   Then **Files**, **Steps**, **Gate** (the exact commands) and **Acceptance** (the tests from 3,
   byte for byte). Prefer one or two Tasks: a Task whose diff stays small lands on CI and the gate
   without a review session.

End the document with the line `coverage: <covered>/<total> stories; uncovered: <ids or none>`.
Mint Story and Task ids only from the range this session was given (`BACKLOG_ID_RANGE`); never
reuse an id the record already carries.

Final message: the pushed sha, the two paths, the Story count, the Task count, the coverage line.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `plan/F-0001`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started;
if `git status` shows a rebase in progress, finish it first). A lane branch is straight commits
on the trunk: never merge `origin/plan/F-0001` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin plan/F-0001`. If that is refused as
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
item: F-0001
kind: spec-plan
status: done | partial | blocked
branch: plan/F-0001
pushed: yes <the sha origin/plan/F-0001 now points at> | rebased <sha> — the factory publishes | no — <why>
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
needs writes: <coder/correct only — repo paths outside writes: that must change too, space-separated, or none>
ruling: <adjudicate only — one paragraph: what was disputed, what now holds, what changes; else omit>
blocked_on: <adjudicate only — the id this item must wait for, or none>
writes: <adjudicate only — the corrected footprint, space-separated globs, or none>
superseded_by: <adjudicate only — the id that replaces this item, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
