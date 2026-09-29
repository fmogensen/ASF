Backlog item: F-0001 — Checkout survives a failed payment provider

## What is already known (do not go looking for it)
Item: F-0001 — Checkout survives a failed payment provider (feature, state Active, stage plan-approved)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: RESHAPE → REPLAN — groom: move the export onto the new reader
Branch: `plan/F-0001-replan` (exists: no)
Head: abc1234 record the provider outcome
Spec: `docs/specs/checkout-resilience.md` (312 lines)
Plan: `docs/plans/checkout-resilience.md` (188 lines)
Writes (the footprint this job may touch): (none declared)
Tests named by the card: (none named)
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
- Keep a heartbeat: print a progress line as you go; a silent session is read as a dead one and
  relaunched on top of you.
- Finish with the typed REPORT below, as the last thing you print.
- Anything a human must decide or run: `NEEDS OPERATOR: <what> — <the command or the answer>`.
- Every commit subject names the item: `task(F-0001): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.
- Never write a worker account name or a machine path into the product; refer to a lane as `lane-N`. Run the redaction check before you push.

## Your job: re-plan F-0001's open Tasks

Binding: the groom's reshape decision on F-0001 — (none) — under the approved spec
`docs/specs/checkout-resilience.md` and the plan `docs/plans/checkout-resilience.md`. The decision is the scope: carry it out, add none.

The Feature's Tasks as the record holds them:
- T-0001 [New] Record every payment attempt; writes: app/checkout/attempts.py, tests/test_checkout.py; after: none

A landed Task is kept exactly as it is, and so is every commit already on a Task's branch: a
replan re-cuts the work still to do, it never reverts work done. An open Task you keep, rewrite
it; one the decision makes unnecessary, drop it; work the decision adds, add as new Tasks. Move
an `after:` wherever the decision moves a dependency — an `after:` on a card that will never
land (a removed card, another Feature's archived work) holds its Task for ever, so replace it
with the Task that now provides that surface, or `none`.

DELIVERABLE: `(none)` on branch `plan/F-0001-replan`, cut from `origin/main`. Create only that
file. Every commit subject names the card — `plan(F-0001): <what>`. The record reads it once it
lands and writes the cards itself — you write no card. Its format is binding, machine-read:

    replan: F-0001 (none)

    ### Task T-nnnn: <title>        (an open Task of F-0001, rewritten)
    stories: <Story ids>
    writes: <path globs, comma-separated>
    after: <Task ids, `new N` for the Nth new Task below, or `none`>
    <Files, Steps, Gate and Acceptance, as in any plan>

    ### Task new: <title>           (a Task the record mints under F-0001)
    <the same lines and sections>

    ### Drop T-nnnn: <why>          (an open Task this replan removes)

Every open Task above appears exactly once, as a `### Task` or a `### Drop` section. A Task's
`writes:` names every file its acceptance needs. Two Tasks whose `writes:` intersect run one
after the other.

If the decision cannot be carried out without adding scope the spec does not carry, write no
replan and say so: `NEEDS OPERATOR: F-0001 — <what the decision leaves open>`.

Final message: the pushed sha, the replan path, and per Task: rewritten, new or dropped.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `plan/F-0001-replan`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started
only if the branch needed it — trunk history on it, or a conflict; a branch behind the trunk is
fine: never rebase or update it just to catch up. If `git status` shows a rebase in progress,
finish it first). A lane branch is straight commits
on the trunk: never merge `origin/plan/F-0001-replan` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin plan/F-0001-replan`. If that is refused as
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
kind: replan
status: done | partial | blocked
branch: plan/F-0001-replan
pushed: yes <the sha origin/plan/F-0001-replan now points at> | rebased <sha> — the factory publishes | no — <why>
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
