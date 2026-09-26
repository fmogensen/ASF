Backlog item: F-0003 — Checkout says so when a payment is pending

## What is already known (do not go looking for it)
Item: F-0003 — Checkout says so when a payment is pending (feature, state Active, stage card)
Feature: F-0003 — Checkout says so when a payment is pending
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: DELIVERY → PLAN — 3 items in one delivery, no plan
Branch: `plan/F-0003` (exists: no)
Head: abc1234 record the provider outcome
Spec: not in the record — its place is `docs/specs/f-0003.md`
Plan: not in the record — its place is `docs/plans/f-0003.md`
Writes (the footprint this job may touch): app/checkout/pay.py, app/checkout/retry.py, tests/test_checkout.py
Tests named by the card: tests/test_checkout.py::test_pending_page_says_so (new)
Delivery: 3 items, in this order — F-0003, B-0001, S-0001
Sessions in flight: (none)
Specs live in `docs/specs`, plans in `docs/plans`, reviews in `docs/reviews`.
Branch prefixes: fix → `fix`, plan → `plan`, spec → `spec`, task → `task`.
The trunk is `main`; every commit is signed off (`git commit -s`).

### Description
A customer whose payment is pending sees a page that says so, instead of a blank confirmation.

### Acceptance
- [ ] `tests/test_checkout.py::test_pending_page_says_so` fails before the change and passes after.

### The items of this delivery
#### F-0003 — Checkout says so when a payment is pending (feature)
writes: app/checkout/pay.py, app/checkout/retry.py, tests/test_checkout.py
acceptance:
- [ ] `tests/test_checkout.py::test_pending_page_says_so` fails before the change and passes after.

#### B-0001 — Checkout returns 500 when the payment provider times out (bug)
writes: (none declared)
acceptance:
- [ ] `tests/test_checkout.py::test_timeout_is_pending_not_500` fails before the fix and passes after.
fix:
Catch the provider's timeout in `app/checkout/pay.py` and return the `pending` path already used
for a declined card, instead of letting the exception reach the handler. The retry itself is
F-0001's work, not this fix's.

Test: `tests/test_checkout.py::test_timeout_is_pending_not_500`.

#### S-0001 — Hold the order and retry once (story)
writes: (none declared)
acceptance:
- [ ] `tests/test_checkout.py::test_timeout_retries_fallback` passes.

### Where to look
`tests/test_checkout.py` (12 lines)
- test_attempt_row_per_try (def) L3-12
Read only these line ranges first.

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
- Every commit subject names the item: `task(F-0003): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.
- Never write a worker account name or a machine path into the product; refer to a lane as `lane-N`. Run the redaction check before you push.

## Your job: write the delivery plan for F-0003 — 3 items in one branch

Binding: the item blocks above, each one a decided card. There is no spec and there will be no
second document: this plan IS the spec-lite for every item in it. Add no scope no card carries,
and drop nothing a card's ## Acceptance asks for.

DELIVERABLE: `docs/plans/f-0003.md` on branch `plan/F-0003`, cut from `origin/main`. Create only that file.

ONE SECTION PER ITEM, in this order — F-0003, B-0001, S-0001 — and nothing between them:

    ### <id> — <title>
    writes: <the globs this item's diff may touch — within the card's own>
    acceptance: <a fenced block that can be run, and that fails today>
    steps: <what to change, in order>

The delivery's whole footprint is app/checkout/pay.py, app/checkout/retry.py, tests/test_checkout.py. A section that needs a file outside it is a section
that does not belong in this delivery: leave the item out, name it under `left out`, and it goes
back to its own lane while the rest land.

No Task cards: one session builds every item and each item closes by its own commit, so this job
mints no ids and needs no `BACKLOG_ID_RANGE`.

End the plan with `delivers: <ids, in build order>` and `budget: <n> items, <m> globs`.

Final message: the pushed sha, the plan path, the item count, the build order.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `plan/F-0003`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started;
if `git status` shows a rebase in progress, finish it first). A lane branch is straight commits
on the trunk: never merge `origin/plan/F-0003` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin plan/F-0003`. If that is refused as
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
item: F-0003
kind: delivery-plan
status: done | partial | blocked
branch: plan/F-0003
pushed: yes <the sha origin/plan/F-0003 now points at> | rebased <sha> — the factory publishes | no — <why>
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
