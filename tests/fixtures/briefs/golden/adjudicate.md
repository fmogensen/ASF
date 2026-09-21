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
Review file for this round: `docs/reviews/5-f-0002.md` (round 5)
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

`spec/F-0002` is stuck: spec-review r4 >= r4: adjudicate, no further round. The reviewer keeps returning changes and the fixer keeps answering
that the findings are false positives. Nobody rules. You rule, and your ruling ends the branch's
review loop — there is no round after yours.

Read, in full: the newest review `docs/reviews/5-f-0002.md`, the fixer's last report, and the document under
review (`docs/specs/f-0002.md` / `docs/plans/f-0002.md` as the dispute requires).

FOR EACH OPEN FINDING, one of two outcomes — never "noted", never a question back:
- **upheld** — state the exact edit, then make it yourself;
- **overruled** — one sentence on why the reviewer is wrong, citing the line you checked.

Check every disputed anchor against the checkout yourself. The reviewer's reading and the fixer's
are both claims; the file is the fact. An overruled finding never comes back in a later round —
write that into the ruling.

WRITE THE RULING AS A DECISION CARD BODY, ready to be filed as a record: `## Context` (what was
disputed, in two sentences), `## Decision` (what now holds), `## Consequences` (what changes
because of it), `## Links` (back to F-0002 and to the review). Mint its id from
`BACKLOG_ID_RANGE`.

FOUR THINGS ARE NEVER YOURS: licence, money, security, and anything that changes what the customer
sees. For those, leave the document as it is and write `NEEDS OPERATOR: <what> — <the question>`
with your one-line recommendation.

Final message: the findings upheld, the findings overruled, the Decision id, the pushed sha.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

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
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
