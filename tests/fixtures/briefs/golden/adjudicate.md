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
  nothing changed. A Stop gate refuses your exit while your work is off origin.
- Print a progress line as you go; a silent session is read as a dead one and relaunched on top
  of you. The heartbeat is the runtime's: a beat that cannot start never ends your session.
- Finish with the typed REPORT below, as the last thing you print.
- Anything a human must decide or run: `NEEDS OPERATOR: <what you could not check or do> — ` then the exact command in backticks (read-only, harvest runs it itself — B-0042), or, with no command, the exact answer needed instead.
- Every commit subject names the item: `task(F-0002): <what>`. Harvest holds a branch whose commits do not name it; the id inside the branch name does not count.
- Never write a worker account name or a machine path into the product; refer to a lane as `lane-N`. Run the redaction check before you push.
- A push your pre-push hook refuses is not the end of your turn: it printed what is wrong and where — fix that, commit, and push again, then report `pushed: yes <sha>`. Only if the hook refuses the *same* thing a second time do you stop, and then the report reads `pushed: no — hook refused twice: <what it said>`.

## Your job: rule on F-0002, and end the loop

`spec/F-0002` is stuck: spec-review r4 >= r4: adjudicate, no further round. Rounds have run out; nobody rules. You rule, and your ruling ends
the loop — there is no round after yours.

Read, in full: the hold's own text above (the gate line, the conflict, the refusal), the newest
review `docs/reviews/4-f-0002.md` if there is one (quoted at the end of this brief when the factory keeps it off
the branch), the last report, and the document or code under dispute
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
turn waiting for one. The heartbeat belongs to the runtime, never to a loop you must keep alive:
if a HEARTBEAT command above cannot start (the sandbox refuses it), say so in one line and carry
on with the job — it is never a reason to stop, to end `needs input`, or a `NEEDS OPERATOR`.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `spec/F-0002`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started
only if the branch needed it — trunk history on it, or a conflict; a branch behind the trunk is
fine: never rebase or update it just to catch up. If `git status` shows a rebase in progress,
finish it first). A lane branch is straight commits
on the trunk: never merge `origin/spec/F-0002` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin spec/F-0002`. If that is refused as
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
item: F-0002
kind: adjudicate
status: done | partial | blocked
branch: spec/F-0002
pushed: yes <the sha origin/spec/F-0002 now points at> | rebased <sha> — the factory publishes | no — <why>
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
