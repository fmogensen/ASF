Backlog item: F-0003 — Checkout says so when a payment is pending

## What is already known (do not go looking for it)
Item: F-0003 — Checkout says so when a payment is pending (feature, state Active, stage card)
Feature: F-0003 — Checkout says so when a payment is pending
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: DELIVERY → CODE — delivery plan approved, footprint free
Branch: `worker/F-0003` (exists: no)
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
  nothing changed. A Stop gate refuses your exit while your work is off origin.
- Keep a heartbeat: print a progress line as you go; a silent session is read as a dead one and
  relaunched on top of you.
- Finish with the typed REPORT below, as the last thing you print.
- Anything a human must decide or run: `NEEDS OPERATOR: <what> — <the command or the answer>`.
- Every commit subject names the item: `task(F-0003): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.
- Never write a worker account name or a machine path into the product; refer to a lane as `lane-N`. Run the redaction check before you push.

## Your job: build the delivery F-0003 — 3 items, one branch, one gate

Binding: `docs/plans/f-0003.md`, section by section, in this order: F-0003, B-0001, S-0001. Do those items and
nothing else — not the next card you notice, not a cleanup on the way. The order is the plan's
`after:` order: an item builds on what the one before it committed, so never start a later item
before the earlier one's commit is on the branch.

RESUME, NEVER RESTART. `worker/F-0003` may already be on origin (`exists:` above): then its head is
the state of the delivery. Read `git log origin/main..worker/F-0003` first — an item whose id a
commit subject there names is done; do not redo it, do not amend it. Carry on with the first item
no commit names, from that head. The report under `The last report for this item` above says
where the last session stopped.

ONE COMMIT PER ITEM, and the subject names that item's id — `<kind>(<id>): <what>`. A commit on
the trunk naming the id is what closes the card: an item no commit names lands nothing, whatever
its diff did. Never fold two items into one commit. Push after every item's commit: a session
that dies leaves the branch at its last pushed commit, and the next one continues from there.

THE BOUNDARY IS `writes:` — app/checkout/pay.py, app/checkout/retry.py, tests/test_checkout.py. That is the union of the items' footprints; a file outside
it is a refusal, not a judgement call.

PROVES — each item's block above lists the Story lines its tests tick (`proves:`). Every line
your tests prove carries a trailer in that item's own commit message, on its own line:

    Proves: <S-id> line <n> — <the test path that proves it>

`<n>` is the number in the block, not a line of the card file. The test path must exist on this
branch. An item that proves nothing is refused at the landing and handed straight back to you.

AN ITEM ALREADY ON THE TRUNK — its work landed on `origin/main` under another commit, before
the card existed or by another lane: no commit there names its id, so the card stays open. Verify
it against the item's section and run the test that covers it, then make that item's one commit
an empty, signed one — `git commit --allow-empty -s -m "<kind>(<id>): already landed in <sha> —
verified by <test>"`, with the item's `Proves:` trailers naming the test that already proves each
line — and push. That commit is the evidence the record needs, not a fabricated one; a report
that only says "already on main" closes nothing, and the lane relaunches this session.

An item you cannot finish: commit nothing for it — no half of it — name it under `left out`, and
carry on with the rest. A run that ends with items left says `status: partial`: no PR opens for
a partial delivery, and the same session comes back to this branch to finish. Only `status: done`
lands the branch — with every item, or with the ones under `left out` returned to their own
lane. A gate you cannot make green is the whole branch's problem: say so and stop.

BEFORE THE PUSH: the plan's acceptance block for every item you committed, byte-identical and
passing, and the product's gate once over the whole branch. Paste each last line in the report.

Final message: the pushed sha, one line per item (committed, left out, or already on the branch),
the gate lines.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `worker/F-0003`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started
only if the branch needed it — trunk history on it, or a conflict; a branch behind the trunk is
fine: never rebase or update it just to catch up. If `git status` shows a rebase in progress,
finish it first). A lane branch is straight commits
on the trunk: never merge `origin/worker/F-0003` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin worker/F-0003`. If that is refused as
non-fast-forward, the rebase is why: stop there — do not merge, do not force — and write
`pushed: rebased <sha> — the factory publishes` in the report; the factory publishes a rebased
lane branch itself (B-0056). Publishing and landing are the factory's, never yours: commit, and
the lane publishes and lands the branch — never run `asf land` or any other `asf` command to
publish, and a refused push is never a `NEEDS OPERATOR`.
Never invent an id: a card id comes from `asf new` or the
`BACKLOG_ID_RANGE` this session was given, and a ruling is never a commit in this repo.

Anything a human must decide, answer or run is never guessed and never buried in a comment:
print `NEEDS OPERATOR: <what> — <the command or the answer needed>` on its own line, then carry on
with every part of the job that does not depend on it.

Finish with this, and nothing after it:

```
REPORT
item: F-0003
kind: delivery-code
status: done | partial | blocked
branch: worker/F-0003
pushed: yes <the sha origin/worker/F-0003 now points at> | rebased <sha> — the factory publishes | no — <why>
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
