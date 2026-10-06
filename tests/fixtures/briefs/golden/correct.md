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
Tests named by the card: tests/test_checkout.py::test_timeout_is_pending_not_500 (new)
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
  nothing changed. A Stop gate refuses your exit while your work is off origin.
- Keep a heartbeat: print a progress line as you go; a silent session is read as a dead one and
  relaunched on top of you.
- Finish with the typed REPORT below, as the last thing you print.
- Anything a human must decide or run: `NEEDS OPERATOR: <what> — <the command or the answer>`.
- Every commit subject names the item: `task(B-0001): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.
- Never write a worker account name or a machine path into the product; refer to a lane as `lane-N`. Run the redaction check before you push.
- A push your pre-push hook refuses is not the end of your turn: it printed what is wrong and where — fix that, commit, and push again, then report `pushed: yes <sha>`. Only if the hook refuses the *same* thing a second time do you stop, and then the report reads `pushed: no — hook refused twice: <what it said>`.

## Your job: correct B-0001 — the harvest held `fix/B-0001`

The harvest gate held `fix/B-0001` and sent it back to you: your worktree is already on `fix/B-0001`, and a rebase onto `origin/main` was started for you if the branch needed one — if `git status` shows a conflict it is still in place: resolve it (otherwise carry on; a branch merely behind the trunk is never rebased just to catch up), fix what the failure below names (a conflict is resolved so both sides survive), run the targeted tests for the files you changed, and push the same branch — never a new one, never a merge of `origin/fix/B-0001` or `origin/main` into it, never a force. A push refused as non-fast-forward is the rebase you were handed: stop there and report `pushed: rebased <sha> — the factory publishes`. Change nothing the failure does not ask for; paste the tests' last line in the report.

THE BOUNDARY IS `writes:` — (none declared)
When the failure below says the footprint was widened, the paths it added are inside that list now: change them as the failure asks. A file still outside it that must change goes, as a full repo path, on the REPORT's `needs writes:` line with `status: partial` — never edited, never a question to a person.

CORRECTION: the step failed with:
FAIL: test_red_gate

ONE PUSH: this is a correction round. Answer every point above in this one session, commit as you go, and push once — `git push` is your last act, never a push per fix. Each push starts the product's CI again and cancels the run before it; a second push in this session is recorded as a defect of the run.

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
comes back to its session, and the worktree was rebased onto `origin/main` before you started
only if the branch needed it — trunk history on it, or a conflict; a branch behind the trunk is
fine: never rebase or update it just to catch up. If `git status` shows a rebase in progress,
finish it first). A lane branch is straight commits
on the trunk: never merge `origin/fix/B-0001` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin fix/B-0001`. If that is refused as
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
item: B-0001
kind: correct
status: done | partial | blocked
branch: fix/B-0001
pushed: yes <the sha origin/fix/B-0001 now points at> | rebased <sha> — the factory publishes | no — <why>
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
