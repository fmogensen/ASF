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
  nothing changed. A Stop gate refuses your exit while your work is off origin.
- Print a progress line as you go; a silent session is read as a dead one and relaunched on top
  of you. The heartbeat is the runtime's: a beat that cannot start never ends your session.
- Finish with the typed REPORT below, as the last thing you print.
- Anything a human must decide or run: `NEEDS OPERATOR: <what you could not check or do> — ` then the exact command in backticks (read-only, harvest runs it itself — B-0042), or, with no command, the exact answer needed instead.
- Every commit subject names the item: `plan(F-0001): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.
- Never write a worker account name or a machine path into the product; refer to a lane as `lane-N`. Run the redaction check before you push.
- A push your pre-push hook refuses is not the end of your turn: it printed what is wrong and where — fix that, commit, and push again, then report `pushed: yes <sha>`. Only if the hook refuses the *same* thing a second time do you stop, and then the report reads `pushed: no — hook refused twice: <what it said>`.

## Your job: write the plan for F-0001

Binding: the approved spec `docs/specs/checkout-resilience.md`. Read it in full first — every section, every fenced
acceptance test. The plan adds no scope the spec does not carry; where the spec is wrong, say so
in the plan's decisions block rather than quietly widening it.

DELIVERABLE: `docs/plans/checkout-resilience.md` on branch `plan/F-0001`, cut from `origin/main`. Create only that file.

Every commit subject names the card — `plan(F-0001): <what>` — the lane refuses a branch whose
subjects do not name F-0001 as a token (the branch name does not count).

TASK LINES (binding, machine-read — the record turns every `### Task N:` heading into a Task card
and the feeder launches coders from them). Directly under each heading, before anything else,
exactly three lines:

    stories: <the Story ids this Task delivers>
    writes: <the path globs this Task's diff may touch>
    after: <the Tasks whose landed code this one needs, as `Task 1, Task 2` — or `none`>

`writes:` is the same set as the Task's Files block. Two Tasks whose `writes:` intersect are run
one after the other, never together — so keep them disjoint or accept the queue. `after:` becomes
the card's `after:`: the Task waits until those have landed, and one with `none` starts at once.

Every Task carries, besides those three lines: **Files**, **Steps**, **Gate** (the exact commands),
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
turn waiting for one. The heartbeat belongs to the runtime, never to a loop you must keep alive:
if a HEARTBEAT command above cannot start (the sandbox refuses it), say so in one line and carry
on with the job — it is never a reason to stop, to end `needs input`, or a `NEEDS OPERATOR`.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `plan/F-0001`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started
only if the branch needed it — trunk history on it, or a conflict; a branch behind the trunk is
fine: never rebase or update it just to catch up. If `git status` shows a rebase in progress,
finish it first). A lane branch is straight commits
on the trunk: never merge `origin/plan/F-0001` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin plan/F-0001`. If that is refused as
non-fast-forward, the rebase is why: stop there — do not merge, do not force — and write
`pushed: rebased <sha> — the factory publishes` in the report; the factory publishes a rebased
lane branch itself (B-0056). Publishing and landing are the factory's, never yours: commit, and
the lane publishes and lands the branch — never run `asf land` or any other `asf` command to
publish, and a refused push is never a `NEEDS OPERATOR`.
Never invent an id: a card id comes from `asf new` or the
`BACKLOG_ID_RANGE` this session was given, and a ruling is never a commit in this repo.

Anything a human must decide, answer or run is never guessed and never buried in a comment: print
a line of the form `NEEDS OPERATOR: <what you could not check or do> — ` then the exact command in
backticks, copy-pasteable, never paraphrased (B-0042: a read-only one, harvest runs it itself and
attaches the output, so the park already answers it when a person opens it) — or, when there is
no command, only a decision, the exact answer needed instead. Then carry on with every part of
the job that does not depend on it.

Finish with this, and nothing after it:

```
REPORT
item: F-0001
kind: plan
status: done | partial | blocked
branch: plan/F-0001
pushed: yes <the sha origin/plan/F-0001 now points at> | rebased <sha> — the factory publishes | no — <why>
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
needs writes: <coder/correct only — repo paths outside writes: that must change too, space-separated, or none>
proves: <code only — the Proves: trailers you wrote, one per line; or none — <why>>
ruling: <adjudicate only — one paragraph: what was disputed, what now holds, what changes; else omit>
blocked_on: <adjudicate only — the id this item must wait for, or none>
writes: <adjudicate only — the corrected footprint, space-separated globs, or none>
superseded_by: <adjudicate only — the id that replaces this item, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
