Backlog item: none (T-0050 is not in the index)

## What is already known (do not go looking for it)
Item: T-0050 — (not known here) (?, state New, stage —)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: —
Why this session exists: RESHAPE → PLAN — groom: split asf/feeder | asf/harvest
Branch: `plan/T-0050` (exists: no)
Head: abc1234 record the provider outcome
Spec: not in the record — its place is `docs/specs/t-0050.md`
Plan: not in the record — its place is `docs/plans/t-0050.md`
Writes (the footprint this job may touch): (none declared)
Tests named by the card: (none named)
Sessions in flight: (none)
Specs live in `docs/specs`, plans in `docs/plans`, reviews in `docs/reviews`.
Branch prefixes: fix → `fix`, plan → `plan`, spec → `spec`, task → `task`.
The trunk is `main`; every commit is signed off (`git commit -s`).

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

## Your job: reshape T-0050

Binding: the groom's approved split — groom: split asf/feeder | asf/harvest — of this Task of `docs/plans/t-0050.md`, under the
approved spec `docs/specs/t-0050.md`. Add no scope. Change only how T-0050 is cut.

DELIVERABLE, on branch `plan/T-0050` cut from `origin/main`:
- one Task per area, ids from `BACKLOG_ID_RANGE`, each carrying `split_from: T-0050`, its
  `writes:` (the part of T-0050's `(none declared)` in that area) and `stories:`. Every Story of
  T-0050 sits on at least one part;
- T-0050's section of the plan replaced by one section per part, each with Files, Steps, Gate
  and Acceptance (the spec's fenced tests, byte-identical);
- T-0050 marked `removed: split into <ids>`.
If a part cannot pass its own acceptance without another part, say so and leave T-0050 whole:
`NEEDS OPERATOR: T-0050 does not split along <area> — answer no on the split line`.

Final message: the pushed sha, the part ids with their writes, the coverage line.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `plan/T-0050`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started;
if `git status` shows a rebase in progress, finish it first). A lane branch is straight commits
on the trunk: never merge `origin/plan/T-0050` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin plan/T-0050`. If that is refused as
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
item: T-0050
kind: reshape
status: done | partial | blocked
branch: plan/T-0050
pushed: yes <the sha origin/plan/T-0050 now points at> | rebased <sha> — the factory publishes | no — <why>
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
ruling: <adjudicate only — one paragraph: what was disputed, what now holds, what changes; else omit>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
