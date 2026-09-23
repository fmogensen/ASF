Backlog item: F-0002 — Per-customer rate limits on the public API

## What is already known (do not go looking for it)
Item: F-0002 — Per-customer rate limits on the public API (feature, state Active, stage spec-review r4)
Feature: F-0002 — Per-customer rate limits on the public API
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: STALEMATE → ADJUDICATE — spec-review r4 >= r4: adjudicate, no further round
Branch: `spec/F-0002` (exists: no)
Head: abc1234 record the provider outcome
Spec: not in the record — its place is `docs/specs/f-0002.md`
Plan: not in the record — its place is `docs/plans/f-0002.md`
Review file to answer: `docs/reviews/4-f-0002.md` (round 4)
Writes (the footprint this job may touch): (none declared)
Tests named by the card: (none named)
Sessions in flight: (none)
Specs live in `docs/specs`, plans in `docs/plans`, reviews in `docs/reviews`.
Branch prefixes: fix → `fix`, plan → `plan`, spec → `spec`, task → `task`.
The trunk is `main`; every commit is signed off (`git commit -s`).

### Description
The public API has one global limit, so one heavy customer starves every other. Limit per
customer instead, with the limit itself a field on the customer record.

### Acceptance
- [ ] A customer over its own limit is throttled; every other customer is unaffected.

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

## Your job: rule on F-0002, and end the loop

`spec/F-0002` is stuck: spec-review r4 >= r4: adjudicate, no further round. Rounds have run out; nobody rules. You rule, and your ruling ends
the loop — there is no round after yours.

Read, in full: the hold's own text above (the gate line, the conflict, the refusal), the newest
review `docs/reviews/4-f-0002.md` if there is one, the last report, and the document or code under dispute
(`docs/specs/f-0002.md` / `docs/plans/f-0002.md` / the branch's diff against `origin/main` as the case requires).

FOR EACH OPEN FINDING OR HOLD, one of two outcomes — never "noted", never a question back:
- **upheld** — state the exact edit, then make it yourself on `spec/F-0002`, committed under the
  item's own subject (`fix(F-0002): …`, `spec(F-0002): …`) and pushed;
- **overruled** — one sentence on why, citing the line you checked.

Check every disputed anchor against the checkout yourself. Both sides' readings are claims; the
file is the fact. An overruled finding never comes back in a later round — say so in the ruling.

THE RULING GOES TO THE RECORD, AND THE FACTORY WRITES IT THERE: your ruling is the `ruling:`
line of the REPORT below — one paragraph: what was disputed, what now holds, what changes because
of it. The tick files it on F-0002's card as a `## History` line and closes the row. You never
commit a ruling, a review or a decision file to this repo, you never create a Decision card, and
you never write a decision id: ids are minted by `asf new`, not by a session — a `D-nnnn` you
made up is a defect, not a ruling (B-0054).

Your ruling's *mechanism* is the three fields `blocked_on`, `writes` and `superseded_by`; the
paragraph is its *explanation*. If the answer is "this waits for T-0025", the answer is
`blocked_on: T-0025` — not a sentence saying so. If the answer is "its footprint was wrong", the
answer is the corrected `writes:` line. A paragraph with no field behind it changes nothing, and
the loop you were asked to end restarts on the next tick.

FOUR THINGS ARE NEVER YOURS: licence, money, security, and anything that changes what the customer
sees. For those, leave the document as it is and write `NEEDS OPERATOR: <what> — <the question>`
with your one-line recommendation.

Final message: the REPORT, with `ruling:` filled in, the findings upheld and overruled named in it,
and `pushed:` for any edit you made.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `spec/F-0002`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started;
if `git status` shows a rebase in progress, finish it first). A lane branch is straight commits
on the trunk: never merge `origin/spec/F-0002` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin spec/F-0002`. If that is refused as
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
item: F-0002
kind: adjudicate
status: done | partial | blocked
branch: spec/F-0002
pushed: yes <the sha origin/spec/F-0002 now points at> | rebased <sha> — the factory publishes | no — <why>
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
ruling: <adjudicate only — one paragraph: what was disputed, what now holds, what changes; else omit>
blocked_on: <adjudicate only — the id this item must wait for, or none>
writes: <adjudicate only — the corrected footprint, space-separated globs, or none>
superseded_by: <adjudicate only — the id that replaces this item, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
