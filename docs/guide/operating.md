# Operating a product

The factory runs itself on its clocks. Your part of the day: read the tables, answer the groom,
resolve what is held, and feed the inbox. Every table is also a CLI command; the `/asf:*` skill
prints the same output inside Claude Code.

Before you read a table, `git pull` in your record checkout (`backlog_dir`): the tick writes to
its own clone and pushes, and the tables read your checkout (see
[the two copies](product-config.md#the-two-copies-of-the-record)).

## The tick

Each clock runs `asf tick --product <p> --steps <its steps>`. Its log is
`~/.ASF/logs/tick-<product>-<clock>.log`. Run one by hand the same way; `--manifest` shows who owns
each step without running anything.

| step | what it does | typical lines |
| --- | --- | --- |
| `record` | in the tick's own clone of the record: CI backfill, derive state from git and CI evidence (`ingest`), mint Tasks from landed plans, file Bugs, roll up metrics and releases, rebuild `index.json`; with `approvals.groom: auto` also apply the adjudicator's answers and groom the inbox | `[record:<part>] 0.8s`, `file-bugs: 0 filed …` |
| `health` | reconcile the session ledger with what runs: end finished or dead sessions, hold unpushed work, reap worktrees, look for stalls | `ended`, `held`, `reaped`, `stall: none` |
| `wave` | say the open holds, then launch what the feeder says — the NEXT table, cut to capacity | `launched <job> <item> → <account> (<model>) pid n`, `waits …` |
| `prs` | pull-request landing: open a PR per finished branch, then PR hygiene | `prs: opened …`, `prs: landing is fast-forward …` |
| `harvest` | land finished branches, fast-forward or by merging their PRs (in the background) | `harvest: started in the background`, `landed <branch> → <sha>`, `waiting …`, `held <branch>: …` |
| `batch` | your own merge-queue script, if declared | `[command:batch] …` |
| `daily` | once a day: `groom --apply` (the previous groom file's answers, the inbox, today's questions), stale items, Bugs, yesterday's rollup and release | `daily: <part> ok` |

Each step ends with `[step:<name>] <seconds>s`; the tick ends with `tick: state committed and
pushed`, `tick: total …`, then a summary: **IN FLIGHT** (the sessions running now) and **DONE
since** the last tick on this clock (sessions that ended, with their result). A step that failed
printed `[step:<name>] FAILED <why>`; a failed record step prints `RECORD STALE — <why>`.

## The tables

### `/asf:status` — FACTORY STATUS

One row per metric. A row that has nothing to read says which key would fill it: `— (not
configured: <key>)`.

| row | reads |
| --- | --- |
| Runners | the CI runner pool (`ci.runner_org`) |
| Prod | how far `main` is ahead of the last successful deploy — see the note below |
| Agents | `n working`, then `f finished (awaiting harvest)` and `m dead` when there are any |
| Capacity | `sessions used/ceiling (bound by …)`, and CI when configured |
| Ready to launch | how many rows the NEXT table would launch, and the first |
| Quota 5h/7d | each account's windows, naming the band when not free |
| Cron | the scheduler's view of this product's jobs |
| Groom | the latest digest's counts (only with `approvals.groom: auto`) |

**Prod always reads `— (not configured: ci.deploy_workflow)` today.** The row reads
`ci.deploy_workflow`, but the product file refuses that key under `ci:` (it is not one of `ci`'s
fields), so no config can fill it. The deploy workflow belongs in `deploy_sha.workflow`, which the
evidence pass (the prod sha behind a Feature's `on-prod`) already reads; the pending fix is for
the Prod row to read it too.

To keep the table in front of you, type `/loop 5m /asf:status` in each product's Claude Code
session: it reprints the status every five minutes until you stop it.

### `/asf:next` — NEXT

What the tick would start now, S1 first: `Tier | Row | Item | Feature | Action`.

- **Tier** 0 is an open S1 Bug, 1 an S2 Bug, 2 everything else by Feature rank. While an S1 Bug has
  no session, no tier-2 row is shown: Features wait for the incident. If that Bug does not belong
  to this product, retire it rather than wait — see
  [clearing a card](#clearing-a-card-that-does-not-belong).
- **Row** is `STATE → ACTION`: `BUG → FIX`, `FIX → CORRECT` (a held branch back to a session),
  `STALEMATE → ADJUDICATE`, `CARD → SPEC`, `STARVED → SPEC`/`PLAN`, `PLAN → CODE`, `CONFLICT →
  REBASE`, `STALE → CLOSE`, `RESHAPE → PLAN`, `UNDECIDED → DECIDE`, `GROOM → ADJUDICATE`, and
  `PUSHED → LAND` (a spec or plan whose work is pushed and waits to land — nothing to launch).
- **Action** is `would launch <kind> on <branch>`, or why not: `WAITS ON <item>` (an `after:`
  predecessor, or a running Task that writes the same files), `WAITS ON landing` (pushed, a PR
  open or harvest pending), `WAITS ON a free slot`, `NEEDS
  DECISION` (the card is not `decided: true` yet — answer it in the groom), `PARKED …`, `ON TRUNK
  <sha>` (its work is already on the trunk).

`asf next --json` gives the same rows for scripts; `--capacity n` asks "what if I had n slots".

### `/asf:sessions` — SESSIONS

Groups, counted in the header (`n working · n finished (awaiting harvest) · n dead · n ended · n
other`; the finished group only appears when it has rows):

| group | meaning |
| --- | --- |
| **Working** | on the ledger with no end, and its process is alive |
| **Finished** | its process exited after writing a success result; `health` has not recorded the end yet, and harvest lands it next |
| **Dead** | on the ledger with no end, its process is gone, and it wrote no success result |
| **Other** | a session on one of your pool accounts that is not this product's — another product's, or one ASF did not start |
| **Ended** | sessions that ended yesterday or today, with their result |

Neither a finished nor a dead session holds a slot. The next `health` step records each as one of:

- `finished` — the session reported success and its branch is on `origin`; harvest takes it next;
- `failed: not pushed: …` — it said done but left work uncommitted or unpushed; held and sent back;
- `dead pid` — the process died without writing a result; it gets one cold retry
  (`<job>-correction`), and a second death holds the item like a red gate.

To record them now instead of at the next tick: `asf workers health --product <p>`.

### `/asf:backlog` — BOARD

One table per Epic, one row per Feature: `Stage | Spec | Plan | Tasks | PRs | Blocked on | Age |
Cost`. The stage ladder is derived, never typed: `card → spec-draft → spec-review rN →
spec-approved → plan-draft → plan-review rN → plan-approved → building c/t → landed → on-prod`
(`on-prod` only for a product that configures a deploy). The header counts specs, plans and Tasks
across the board and says when `index.json` was generated.

### `/asf:roadmap` — ROADMAP

One row per Epic by rank: `State | On prod | Next | Blocked | Spend / budget`. **Next** names up to
four Features that are Active or plan-approved, with their PRs. An Epic's state follows its
children; it is `Closed` only when you type `closed: true` on it.

### `/asf:capacity` — CAPACITY

One row per product (`--all` for every product): `sessions` (the effective ceiling), `in flight`,
`free`, `bound by` (`product`, `operator default`, `operator total` or `fair share`), then the same
for CI runs, and the batch shape. `?` is unknown — not configured, or unreadable.

### `/asf:doctor`

Is the install sound — see [getting-started.md](getting-started.md#5-the-first-asf-doctor). Run it
after any config change.

`/asf:parity` (one row per Story) and `/asf:prod` (deploy state and what shipped) complete the set.

## Holds, and "back to its session"

When harvest cannot land a branch — its gate is red, its rebase conflicts on a file ASF does not
own, or its session said done without pushing — the branch is **held**:

```
held worker/T-0042: <why> — back to its session (round 1)
```

The item gets a correction. The next wave shows it as `FIX → CORRECT`, and launches a session in
the **same worktree** with the failing output, to fix it in place rather than start over. Rounds
count per item. After the third hold the row becomes `STALEMATE → ADJUDICATE`: a heavier session
rules on the item instead of a fourth attempt, and any further hold says `— adjudicate pending`.

Other holds you will see:

- `held <branch>: <class> (<level>) — <file>` — an approval class stops the landing; resolve it
  with `asf approvals resolve` ([product-config.md](product-config.md#approvals)).
- `parked <branch>: ended empty 2 times …` — a Task whose sessions wrote nothing twice. Check
  whether its work is already on the trunk, then close the Task, reshape its plan, or release it:
  `asf unpark <item> --why "<reason>" --product <p>`.
- `held <branch>: ruling belongs in the record` — an adjudicate ruling was committed to the product
  repo instead of reported.
- `held <branch>: commits do not name <ITEM>: every commit subject on the branch names its item …`
  — harvest lands a branch only when every commit subject names the item as a token; an id that
  appears only in the branch name does not count. Every brief states the rule as
  `<kind>(<ITEM>): <what>` — `spec(F-0042): …`, `plan(F-0042): …`, `fix(B-0007): …`,
  `task(T-0101): …` — and the session is sent back to reword its commits. Commit to a lane branch
  by hand the same way.
- `held <branch>: merge commit on a lane branch …` — a lane branch must be straight commits on the
  trunk; the session is sent back to rebase.

A held branch is not a failed step. Nothing is lost: the worktree and branch stay until the item
lands or is closed.

## `asf workers health --fix`

`asf workers health --product <p>` reconciles the ledger and lists every worktree and lane branch
with a verdict. It is not read-only: it records ended sessions and holds exactly as the tick's
`health` step does. With `--fix` (which the tick always uses) it also removes, and only removes:

1. **Worktrees under `~/.ASF/state/<p>/worktrees/`** that pass the reap rule and whose process is
   dead:
   - its branch was landed by harvest (the landed sha is on the ledger) — `reaped <job> (landed
     <sha>)`;
   - its session ended without finishing, and the worktree has no uncommitted changes and no
     commit that `origin/<main>` lacks — `reaped <job> (empty)`;
   - its session finished, the tree is clean, every commit is pushed, and the branch's commits are
     already in `origin/<main>`.

   A reaped worktree that was landed or empty takes its local branch with it, and releases the
   job's reserved id range.
2. **Local lane branches** in `repo_dir` that no worktree holds and whose work is on the trunk, or
   that the lane landed — `pruned <branch> (…)`.

It never removes a worktree whose process is alive, one with uncommitted changes, unpushed
commits or commits not on the trunk, or a live session's worktree. It never deletes a lane branch
with work the trunk lacks — that one is listed as `stray` and kept for you. Without `--fix`, the
same candidates are listed as `reapable` and `stale`.

`asf workers stall` lists live sessions that went silent; `asf workers quota` shows each account's
band; `asf workers sessions` every agent session on the machine and whose it is.

## The groom and the inbox

**The inbox** is how anything enters the record — an idea, a bug, a request:

```bash
asf inbox --title "Export to CSV" --body-file notes.md --product <p>   # optional: --parent E-0003
```

It writes `<intake_dir>/<slug>.md` in your record checkout, prints its path, and commits and pushes
it. You can also drop a `.md` file into the intake folder and push it. The body may carry
`type:`, `parent:`, `severity:`, `writes:` or `stories:` lines; the type is derived from the
card's shape when it does not say.

**The groom** types new and edited inbox cards into cards (the file moves to
`<intake_dir>/done/`) and turns anything it cannot decide into a question in `groom/<date>.md` of
the record:

```
- [ ] F-0042 … → answer: ____
```

When it runs:

- **Once a day, always**: the `daily` step runs `asf groom --apply` in the tick's clone. `--apply`
  applies the answers in the newest groom file dated **before today** (only that one), then grooms
  the inbox and writes today's file. The clone is reset to `origin` first, so it sees your answers
  only if they were committed and pushed.
- **Every tick, only with `approvals.groom: auto`**: the record step grooms the inbox, adds new
  questions to today's file, and applies the adjudicate session's answers (a
  `~/.ASF/state/<p>/groom/<date>.answers` file). Without `groom: auto`, neither happens between
  dailies.

Answering, step by step:

1. Pull your record checkout (`backlog_dir`).
2. `/asf:groom` (or `asf groom --product <p>`) runs the groom **in your checkout**: it writes
   today's file there, shows each open question with a recommended answer, takes yours (`yes`,
   `no`, `rank 2`, `parent E-0003`, `S1`, `duplicate of B-0007`, or `all as recommended`) and
   writes them into the `answer:` slots. It then runs `asf groom --apply`, which applies the
   *previous* day's file in your checkout — not the one you just answered.
3. **Commit and push the checkout.** Neither `asf groom` nor the skill commits or pushes, and the
   tick never reads your checkout.
4. The next day's `daily` step applies those answers in its clone and pushes the result. A card
   becomes buildable only when its answer makes it `decided: true` — that is what `NEEDS
   DECISION` rows wait for.

Answer a day's file before the next day's daily runs: after that, `--apply` reads a newer file.

With `approvals.groom: auto` the groom also answers what its policies can (exact duplicates,
recurring Bugs), sends the rest to an adjudicate session, and writes a daily digest; only what is
left comes to you as `NEEDS OPERATOR` lines.

**The daily stamp.** The `daily` step runs at most once a day, remembered in
`~/.ASF/state/<p>/daily.stamp` as the **local** date. A stamp written today by something other
than the daily clock — a hand-run `asf tick --daily`, a copied state directory — makes the
scheduled daily print `tick: step daily already ran today`. Delete the file to clear it, or force
a run: `asf tick --product <p> --steps daily --daily`.

## Your checkout drifts from the tick's clone

Only `asf inbox` and `asf new` commit and push what they write. `asf groom`, `asf groom --apply`,
`asf set` and your own edits change your checkout and stop there — the tick, working in its clone
from `origin`, never sees them. Commit and push after each, and pull before you edit: the tick
pushes a `tick: state` commit every run, so an unpulled checkout is always behind.

## Clearing a card that does not belong

A card that is not this product's work — a Bug filed against the wrong product, a Feature nobody
wants — is retired with `removed:`, never deleted:

1. Pull your record checkout.
2. In the card's typed block (above the `# ---- machine ----` line), add
   `removed: <why, in a few words>`. `asf set` cannot write this field; edit the file.
3. Run `asf index` in the checkout, then commit and push the card and `index.json`.

On the next tick the card leaves the tables and the wave: nothing is started on it, and a session
already on it is ended and its worktree reaped rather than sent back. An S1 Bug retired this way
releases the tier-2 freeze. (`moved_to:` retires rule cards only; use `removed:` for everything
else.)

## Safety: what a worker session can reach

A worker session is launched with **the tick's whole environment**: ASF removes only git-hook and
caller-identity variables, and adds the job's own (`ASF_PRODUCT`, `ASF_JOB`, …) and the account's
`CLAUDE_CONFIG_DIR`. It keeps your `HOME` unless the account sets `home:`, so it also has every
CLI login that lives under `HOME` or in your keychain — code host, cloud, hosting, database,
payments.

So a tick you run by hand from your shell passes that shell's exported secrets and all your logins
to every session it launches — including, say, a payments CLI whose active context is live. A
scheduled tick passes the smaller environment of its job, but still your `HOME`.

What limits this today:

- Give each account in `worker_pool.accounts` its own `home:` directory, holding only the logins a
  worker should have ([config.example.yaml](../config.example.yaml)).
- Keep the CLIs a worker can reach on test-mode or non-production contexts.
- Map what must never happen unattended with `approval_signals` (commands and paths) at
  `human-now` ([approvals](product-config.md#approvals)).
- Run ticks from the scheduler, not from a shell with secrets exported.

The planned [connectors](connectors.md) replace this with default-deny scoping.
