Backlog item: B-0001 — Checkout returns 500 when the payment provider times out

## What is already known (do not go looking for it)
Item: B-0001 — Checkout returns 500 when the payment provider times out (bug, state New, stage —)
Severity: S1
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: FIX → CORRECT — the harvest gate went red, round 1
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

## Your job: correct B-0001 — the harvest held `fix/B-0001`

The harvest gate held `fix/B-0001` and sent it back to you: your worktree is already on `fix/B-0001`, and the rebase onto `origin/main` was started for you — if `git status` shows a conflict it is still in place: resolve it (or, if the rebase finished cleanly, carry on), fix what the failure below names (a conflict is resolved so both sides survive), run the full suite, and push the same branch — never a new one, never a merge of `origin/fix/B-0001` or `origin/main` into it, never a force. A push refused as non-fast-forward is the rebase you were handed: stop there and report `pushed: rebased <sha> — the factory publishes`. Change nothing the failure does not ask for; paste the suite's last line in the report.

CORRECTION: the step failed with:
FAIL: test_red_gate

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `fix/B-0001`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started;
if `git status` shows a rebase in progress, finish it first). A lane branch is straight commits
on the trunk: never merge `origin/fix/B-0001` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin fix/B-0001`. If that is refused as
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
item: B-0001
kind: correct
status: done | partial | blocked
branch: fix/B-0001
pushed: yes <the sha origin/fix/B-0001 now points at> | rebased <sha> — the factory publishes | no — <why>
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
