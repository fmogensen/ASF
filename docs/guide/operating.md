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
| `record` | in the tick's own clone of the record: CI backfill, derive state from git and CI evidence (`ingest`), mint Tasks from landed plans, file Bugs, roll up metrics and releases, rebuild `index.json`, apply groom answers, groom the inbox | `[record:<part>] 0.8s`, `file-bugs: 0 filed …` |
| `health` | reconcile the session ledger with what runs: end finished or dead sessions, hold unpushed work, reap worktrees, look for stalls | `ended`, `held`, `reaped`, `stall: none` |
| `wave` | say the open holds, then launch what the feeder says — the NEXT table, cut to capacity | `launched <job> <item> → <account> (<model>) pid n`, `waits …` |
| `prs` | pull-request landing: open a PR per finished branch, then PR hygiene | `prs: opened …`, `prs: landing is fast-forward …` |
| `harvest` | land finished branches, fast-forward or by merging their PRs (in the background) | `harvest: started in the background`, `landed <branch> → <sha>`, `waiting …`, `held <branch>: …` |
| `batch` | your own merge-queue script, if declared | `[command:batch] …` |
| `daily` | once a day: groom with yesterday's answers, stale items, Bugs, yesterday's rollup and release | `daily: <part> ok` |

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
| Prod | how far `main` is ahead of the last successful deploy (`deploy_sha.workflow`) |
| Agents | `n working`, then `f finished (awaiting harvest)` and `m dead` when there are any |
| Capacity | `sessions used/ceiling (bound by …)`, and CI when configured |
| Ready to launch | how many rows the NEXT table would launch, and the first |
| Quota 5h/7d | each account's windows, naming the band when not free |
| Cron | the scheduler's view of this product's jobs |
| Groom | the latest digest's counts (only with `approvals.groom: auto`) |

### `/asf:next` — NEXT

What the tick would start now, S1 first: `Tier | Row | Item | Feature | Action`.

- **Tier** 0 is an open S1 Bug, 1 an S2 Bug, 2 everything else by Feature rank. While an S1 Bug has
  no session, no tier-2 row is shown: Features wait for the incident.
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

**The groom** runs in every tick's record step: new and edited inbox cards are typed into cards
(the file moves to `<intake_dir>/done/`), and anything it cannot decide becomes a question in
`groom/<date>.md` of the record:

```
- [ ] F-0042 … → answer: ____
```

Answer with `/asf:groom`: it prints the groom, shows each open question with a recommended answer,
takes yours (`yes`, `no`, `rank 2`, `parent E-0003`, `S1`, `duplicate of B-0007`, or `all as
recommended`), writes them into the `answer:` slots and runs `asf groom --apply`. Or edit the file
in your checkout, push it, and the next tick applies the answers. A card becomes buildable only
when its answer makes it `decided: true` — that is what `NEEDS DECISION` rows wait for.

With `approvals.groom: auto` the groom also answers what its policies can (exact duplicates,
recurring Bugs), sends the rest to an adjudicate session, and writes a daily digest; only what is
left comes to you as `NEEDS OPERATOR` lines.
