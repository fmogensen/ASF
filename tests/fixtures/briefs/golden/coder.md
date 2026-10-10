Backlog item: T-0001 — Record every payment attempt

## What is already known (do not go looking for it)
Item: T-0001 — Record every payment attempt (task, state New, stage —)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: PLAN → CODE — plan approved, footprint free
Branch: `task/T-0001` (exists: no)
Head: abc1234 record the provider outcome
Spec: `docs/specs/checkout-resilience.md` (312 lines)
Plan: `docs/plans/checkout-resilience.md` (188 lines)
Writes (the footprint this job may touch): app/checkout/attempts.py, tests/test_checkout.py
Tests named by the card: tests/test_checkout.py::test_attempt_row_per_try (new)
Sessions in flight: (none)
Specs live in `docs/specs`, plans in `docs/plans`, reviews in `docs/reviews`.
Branch prefixes: fix → `fix`, plan → `plan`, spec → `spec`, task → `task`.
The trunk is `main`; every commit is signed off (`git commit -s`).

### Description
One row per attempt: provider, outcome, latency, order id. Written whether the attempt succeeded
or failed, and never inside the provider client itself.

### Acceptance
- [ ] `tests/test_checkout.py::test_attempt_row_per_try` passes.

### Where to look
`app/checkout/attempts.py` (41 lines)
- record_attempt (def) L10-18
- AttemptStore (class) L20-41
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
- Print a progress line as you go; a silent session is read as a dead one and relaunched on top
  of you. The heartbeat is the runtime's: a beat that cannot start never ends your session.
- Finish with the typed REPORT below, as the last thing you print.
- Anything a human must decide or run: `NEEDS OPERATOR: <what you could not check or do> — ` then the exact command in backticks (read-only, harvest runs it itself — B-0042), or, with no command, the exact answer needed instead.
- Every commit subject names the item: `task(T-0001): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.
- Never write a worker account name or a machine path into the product; refer to a lane as `lane-N`. Run the redaction check before you push.
- A push your pre-push hook refuses is not the end of your turn: it printed what is wrong and where — fix that, commit, and push again, then report `pushed: yes <sha>`. Only if the hook refuses the *same* thing a second time do you stop, and then the report reads `pushed: no — hook refused twice: <what it said>`.

## Your job: build T-0001

Binding: this Task (and any it absorbed, listed above) of `docs/plans/checkout-resilience.md`, and the spec
`docs/specs/checkout-resilience.md` behind it. Do exactly this Task — not the next one, not a cleanup you noticed on the
way. Branch `task/T-0001`, cut from `origin/main`.

THE BOUNDARY IS `writes:` — app/checkout/attempts.py, tests/test_checkout.py
A file outside that list is a refusal, not a judgement call: leave it untouched, and say in the
report under `left out` what you would have changed and why. If a Step cannot be done without
touching one, take the plan's stated expectation for it, record the assumption in the report, and
carry on. The footprint is what lets other sessions run beside you; widening it silently collides
with work you cannot see.

When the Task cannot be whole without such a file — a sibling test suite your change turns red, a
constant that belongs in another module — name every one, as a full repo path, on the REPORT's
`needs writes:` line and report `status: partial`. The factory widens `writes:` by those paths
and sends this run back to you; that line is how the footprint grows, never a question to a person.

BEFORE THE PUSH: the Task's Gate commands, and its acceptance tests byte-identical from the plan
and passing. Paste the last line of each in the report. A test you changed to make it pass is a
failed Task, not a passed one.

## Before the push

level: low (size class small)

| check | result | confidence | evidence |
| --- | --- | --- | --- |
| the diff stays inside the declared footprint | <pass\|fail> | <n/a\|high\|medium\|low> | 
| every changed function is reachable, and every caller it changed still compiles | <pass\|fail> | <n/a\|high\|medium\|low> | 
| the tests the change names were run, and their last line is green | <pass\|fail> | <n/a\|high\|medium\|low> | 
| no secret value, no host name and no account name is printed or committed | <pass\|fail> | <n/a\|high\|medium\|low> | 

The rows above with a command are run by your `pre-push` hook: a red one refuses the push and prints what failed. Paste the table, filled, in your report.

PROVES — the acceptance lines your tests tick: (this Task lists no Story — say so in the report)
Before the push, every line above that your tests now prove carries a trailer on its own line in
one of your commit messages:

    Proves: <S-id> line <n> — <the test path that proves it>

`<n>` is the number above, not a line of the card file. The test path must exist on this branch.
A Task that proves nothing is refused at the landing and handed straight back to you, so write
the trailer with the commit, not after it.

If the Task's work is ALREADY on `origin/main` under another commit (landed before the card
existed, or by another lane): verify it, run the test that covers it, then make one empty, signed
commit — `git commit --allow-empty -s -m "task(T-0001): already landed in <sha> — verified by
<test>"`, with the `Proves:` trailers naming the test that already proves each line — and push. A
report that only says "already on main" closes nothing, and the lane relaunches this session.

Final message: the pushed sha, the files written, the gate lines, the assumptions recorded.

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

Your branch is `task/T-0001`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started
only if the branch needed it — trunk history on it, or a conflict; a branch behind the trunk is
fine: never rebase or update it just to catch up. If `git status` shows a rebase in progress,
finish it first). A lane branch is straight commits
on the trunk: never merge `origin/task/T-0001` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin task/T-0001`. If that is refused as
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
item: T-0001
kind: coder
status: done | partial | blocked
branch: task/T-0001
pushed: yes <the sha origin/task/T-0001 now points at> | rebased <sha> — the factory publishes | no — <why>
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
