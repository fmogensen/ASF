Backlog item: F-0002 — Per-customer rate limits on the public API

## What is already known (do not go looking for it)
Item: F-0002 — Per-customer rate limits on the public API (feature, state Active, stage spec-review r4)
Feature: F-0001 — Checkout survives a failed payment provider
Epic: E-0001 — The storefront holds together under a bad day
Why this session exists: GROOM → ADJUDICATE — 2 groom questions no rule answers, oldest F-0002 (undecided 21d)
Branch: `groom/2026-09-22` (exists: no)
Head: abc1234 record the provider outcome
Spec: not in the record — its place is `docs/specs/f-0002.md`
Plan: not in the record — its place is `docs/plans/f-0002.md`
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

## Your job: rule on the groom questions, and only those

The groom file `groom/2026-09-22.md` is the day's list of `- [ ] <id> <title> — <why> → answer: ____`
lines a policy could not answer. The open questions it still carries, exactly as listed there:

- [ ] F-0002 Per-customer rate limits on the public API — no Stories → answer: ____
- [ ] B-0001 Checkout returns 500 when the payment provider times out — duplicate of B-0002? → answer: ____

FOR EACH ONE, one of two outcomes — never "noted", never a question back:
- an answer in the grammar: `yes`, `no`, `rank <n>`, `parent <id>`, `S1`, `S2`, `S3`, or
  `unblock <id>`, each with one sentence of why;
- or, when the question is not yours to answer, the literal `NEEDS OPERATOR: <the question> —
  <your recommendation>`.

FOUR THINGS ARE NEVER YOURS: licence, money, security, and anything that changes what the customer
sees. Those go to `NEEDS OPERATOR`, matching every other kind's rail.

Write every answer, and nothing else, to the answers file `~/.ASF/state/sample/groom/2026-09-22.answers` — one line per
question, in the groom file's own grammar, `adjudicator:` in place of `controller:`:

    - [ ] <id> <title> — <why> → answer: adjudicator: <word>

THE REPOSITORY IS NOT YOUR WORK. Do not commit, do not push, do not edit a card — not this
product's repository, not the record. The next tick reads the answers file and applies it there;
that is the only path an answer reaches a card by.

Final message: how many questions you answered, how many you sent to `NEEDS OPERATOR`, and the
path `~/.ASF/state/sample/groom/2026-09-22.answers` you wrote them to.

## The heartbeat, the marker, and the report

Print a progress line as you go — what you are doing, not that you are doing something. A session
whose output has gone quiet is read as stalled and may be relaunched on top of you, which throws
away everything you have not pushed. Never run a command in the background and never end your
turn waiting for one.

Run the gate in the foreground and wait for it. Your last act is `git push`. Never start a
background task you do not wait for. A result with uncommitted or unpushed work is a failed
session (B-0051) and comes back to you as a correction. Say so yourself in the report's
`pushed:` line: `pushed: no` is read as that failure at once.

Your branch is `groom/2026-09-22`, and it may already be on origin (`exists:` above — a held branch
comes back to its session, and the worktree was rebased onto `origin/main` before you started;
if `git status` shows a rebase in progress, finish it first). A lane branch is straight commits
on the trunk: never merge `origin/groom/2026-09-22` or `origin/main` into it, never force-push, never
recut it or open another branch. Push with `git push origin groom/2026-09-22`. If that is refused as
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
kind: groom
status: done | partial | blocked
branch: groom/2026-09-22
pushed: yes <the sha origin/groom/2026-09-22 now points at> | rebased <sha> — the factory publishes | no — <why>
commits: <sha> <subject> (one per line, or none)
tests: <what you ran — and its last line>
left out: <what and why, or none>
ruling: <adjudicate only — one paragraph: what was disputed, what now holds, what changes; else omit>
NEEDS OPERATOR: <only if something needs a human, else omit>
```
