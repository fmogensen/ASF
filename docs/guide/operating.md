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

The wave runs right after `record` (`tick.wave_first`, default on), so health, the groom and the
record's bookkeeping never delay a launch (a new inbox card has the groom run first on that tick,
so it is launched by the same tick); the tick logs `tick: wave latency <s>s (tick start → wave
start)`, `asf status` shows it as **Wave latency**, and past `conventions.watchdog.wave_latency`
(default 2 min) it is a `watchdog: BREACH wave_latency` line. `record` itself runs only what the
wave decides from (ingest, plan-tasks, replan, slice, index); its tail — backfill, plan-order,
file-bugs, rollup — runs as `record-tail` before the harvest. When health ends, holds or corrects
a run, the wave runs a second time after the groom, so the seat it freed is still this tick's. Each tail part, and any step but
`record`, `wave` and `daily`, can run on every n-th tick only: `tick.every_n` in `config.yaml`
(default: the four tail parts every 3rd tick) or `clocks.<name>.every_n` in the product file; a
skipped one prints `tick: <name> deferred — …` and still runs at least every
`tick.deferred_max_age_s` (default 3600).

Each step prints `[step:<name>] start owner=<asf|command> pid=<n> at=<time>` as it begins and
`[step:<name>] <seconds>s ok=<yes|no> owner=… pid=… at=…` as it ends, both flushed at once — so
`tail -f` on the log names the step the tick is inside right now, not only the ones it has
finished. A step the tick *skipped* (`off`, already ran today, at CI capacity, already running)
prints neither. The tick then ends with `tick: state committed and
pushed`, `tick: total …`, then a summary: **IN FLIGHT** (the sessions running now) and **DONE
since** the last tick on this clock (sessions that ended, with their result). A step that failed
printed `[step:<name>] FAILED <why>`; a failed record step prints `RECORD STALE — <why>`. Last of
all, a digest of what this tick did: `TICK — record ok, health ok, …` then the non-zero counters
(`launches 2, merges 1, relaunches 1`), or `nothing launched, merged or stalled`.

`asf watch --product <p>` tails that digest as it lands, tick after tick, without running one
itself — read the console instead of a clock's log when a clock (not you) is the one ticking.

`asf doctor` reads that start line too: the SCHEDULER row for each clock shows `step: <name> running
<age> (<owner>, pid <n>)` while its tick is in a step, so you can see which step is running
without opening the log.

## `asf watch`

`asf watch --product <p>` prints each tick's digest as it is written to the ticks stream
(`metrics/ticks/<day>.jsonl` in the tick's own clone), the same two lines the tick itself ends
with. It never clones, fetches or runs anything — read-only, safe to leave open in a console the
operator is not using for anything else, `ctrl-c` to stop. `--poll <seconds>` sets how often it
checks for a new line (default 5s).

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

**Prod reads `deploy_sha.workflow`**: the deploy workflow whose newest successful run is prod,
the same key the evidence pass reads for a Feature's `on-prod`. `conventions.deploy_workflow` and
`ci.deploy_workflow` are read as aliases of it, so a file written for the old hint still loads;
write new files with `deploy_sha.workflow` (or `deploy_sha.prod.workflow`). With none of these
set, Prod reads `trunk is production — no deploy configured (B-0077)`: a package, library or
tool that deploys nothing, not a missing key.

**Runners with no self-hosted pool**: with neither `ci.runner_org` nor `ci.pool` declared,
Runners reads `hosted — no self-hosted ci.pool or ci.runner_org declared` — hosted CI (GitHub's
own runners) has no pool to read, not a missing key.

**Deploys are set per environment** with `deploy_sha.dev.mode` and `deploy_sha.prod.mode`:

```yaml
deploy_sha:
  dev:
    mode: ci            # auto | manual | ci — ci: the product's CI deploys dev, ASF observes
  prod:
    mode: auto          # auto | manual (the default)
    workflow: deploy-prod.yml
    # from: dev         # promote the sha dev runs, instead of the newest green trunk sha
```

`auto` has the tick dispatch the environment's `workflow` for its candidate — the newest green
`ci.workflow` run on the trunk that the environment lacks (for prod with `from: dev`, the sha dev
runs, once its own CI run is green). `manual` dispatches nothing, and the Prod row, `asf prod` and
every tick name the green sha that waits on a hand dispatch. For every environment a running
deploy, a sha whose deploy already failed (never retried) and a red trunk each hold the dispatch,
and the line says which. The old `deploy_sha.auto: true` still reads as `prod.mode: auto`;
`asf doctor`'s `deploy` row names it as deprecated and shows the modes it resolved.

**Named deploy targets** sit beside dev and prod under `deploy_sha.targets` — a marketing site, a
docs site, anything with its own deploy:

```yaml
deploy_sha:
  targets:
    site:
      mode: manual              # auto | manual (the default) | ci (its own workflow deploys it)
      workflow: site-deploy.yml # what auto dispatches; its newest success is the deployed sha
      paths: [apps/site/**]     # behind only by trunk commits touching these
      # source: vercel          # or read the deployed sha from Vercel (project, scope)
```

Each target gets its own `deploy <name>:` line in every tick, the Prod row and `asf prod`: the sha
it runs, how many relevant commits it is behind (all of them without `paths`) and its mode. A
manual target that is behind says so loudly — `MANUAL: site is 15 relevant commits behind — waits
on a hand dispatch of site-deploy.yml`. The same holds apply: a running deploy, a failed sha and a
red trunk. `asf prod`'s **ON PROD — check these** table lists, per target, the customer-visible PRs
merged to the trunk but not yet on it (`NOT LIVE`), then the last ones it shipped — so a page
merged but never deployed to the site shows up there, not as "no change".

A deploy made from the provider's CLI carries no commit sha, so the line reads `site sha unknown`.
The deployer names it once the deploy is done — `asf deploy record site <sha>` — and the line counts
from it until a newer deployment appears without one. A deploy ASF dispatches records its own sha.

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
for CI runs, and the batch shape. `?` is unknown — not configured, or unreadable. `ci from` names where the CI
ceiling came from — `capacity.ci`, the declared runner pool's slots, or the operator's defaults.

### `/asf:doctor`

Is the install sound — see [getting-started.md](getting-started.md#5-the-first-asf-doctor). Run it
after any config change.

A product with a declared runner pool (`ci.pool`) also gets `ci pool` rows: stranded runners,
`runs-on` sets no runner satisfies, role drift, provider labels in `runs-on`, and missing or
offline runners. `asf ci reconcile` plans the fix. See [the CI runner pool](ci-runner-pool.md).

`/asf:parity` (one row per Story) and `/asf:prod` (deploy state and what shipped) complete the set.

## The trunk ruleset: one door to the trunk

Under `merge: queue` the merge queue is the only way onto the trunk, and the host enforces it.
Just before the queue fast-forwards the trunk to a green batch sha, it posts the commit status
`asf/queue` = success on that exact sha (`conventions.ci.queue_status` renames the context;
the description names the batch, the link is the batch's CI run). A repository ruleset on the
trunk requires that status:

- target: the trunk branch (`refs/heads/main`); enforcement `active`; **no bypass actors**
- rules: `deletion`, `non_fast_forward` (no force-push), `required_status_checks` with the one
  context `asf/queue` (`strict_required_status_checks_policy: false`; no pull-request rule)

Every session and worker pushes as the same GitHub user, so a bypass actor would protect
nothing: the status is the key. A `gh pr merge`, the web merge button or a hand push makes a sha
nobody posted `asf/queue` on, and GitHub refuses it (`Required status check "asf/queue" is
expected`). The queue's own push carries it and goes through.

`asf doctor` shows a `trunk ruleset` row: green names the ruleset id and both calls below; red
when no active ruleset on the trunk requires the status (it names a disabled one to enable).
The `queue bypass` row counts only trunk commits from after that ruleset was created (or after
`merge_queue.watch_since`, an ISO date), so the merges that made the ruleset necessary do not
keep it red.

**Landing through `merge-pr.sh`: `asf ruleset`.** A product whose green PRs land through
`tools/merge-pr.sh` (no merge queue) gets the same server-side door from `asf ruleset install`:
ruleset `asf trunk` on the trunk, no bypass actors, `deletion` and `non_fast_forward` refused,
`conventions.landing_checks` required on the merged head (`strict_required_status_checks_policy:
true`) and a `pull_request` rule with 0 approvals, so the token's `gh pr merge` is the only way on
and a hand push is refused. It creates or updates the ruleset by name, so a second run changes
nothing. `asf ruleset install --dry-run` prints the exact API call and payload and the diff
against what the host has, and writes nothing; `asf ruleset status` reads it. `asf ruleset
break-glass --off` deletes the ruleset with a loud line and a `break-glass.log` entry in the
product's state directory; `--on` installs it again. Applying it to a repository is an operator
move, never a tick's.

**Trunk stall.** When the trunk has not moved for more than `conventions.ci.trunk_stall_hours`
(default 4) while landings wait — a batch in the merge queue, or an `asf land` request — every
tick logs one `trunk watch: STALL` line and `asf status` / `asf doctor` show a red `Trunk stall`
row naming what waits. A batch whose cancelled CI run the start queue can neither re-run nor
replace (made on a workflow the trunk has changed since, or STUCK) never holds the queue: the
start queue drops it from the line and the merge queue cuts its members again on the trunk's tip
on its next pass — a new batch sha, a fresh run.

**Stale merge refs first.** A `pull_request` run tests the PR merged with the trunk as it was
when the run was created, and a re-run replays that same merge ref. A red PR head — an `asf land`
request or a factory PR — whose failing run was created before the trunk's current tip arrived is
therefore never kept red, never re-run and never sent to a correct round: ASF closes and reopens
the PR for a fresh run on today's trunk (no commit on the branch; the newest run per check is
what counts), once per head and tip. Only a red on the fresh merge ref is a verdict
(`asf.stale_ref`). An `asf land` request marked red before the trunk moved is read again.

**Trunk red.** An attested trunk push skips the heavy jobs, so a check that breaks on the trunk
itself shows only on the landings. The same required check red (after flake/infra triage) on two
unrelated landings — no PR in common, neither diff touching a file the failing logs name — is
trunk red, suspected: `asf status` and `asf doctor` show `TRUNK RED: <check> (seen on #a, #b)`,
no batch member is blamed and no PR goes to a correct round. When that holds, or the stall alarm
fires (after the fresh runs above have shown), the trunk's full workflow is dispatched on its tip once per tip (`gh workflow run <wf>
--ref <trunk>`: a `workflow_dispatch` run, never the attested skip). Red there confirms it — one
`trunk watch: TRUNK RED` line and one S1 fix card in the intake (check, test, log tail, first red
sha; an open card with signature `trunk-red <check>` is linked instead); green clears it and the
landings' reds are their own. As a safety net the full workflow runs at least every
`conventions.ci.trunk_full_every_hours` (default 6, `0` turns it off), counting scheduled and
dispatched runs (`asf.trunk_red`).

**Break-glass.** Only when the queue itself cannot land and the trunk must move now — the queue
is broken and its fix has to land, or a production incident needs a hotfix the queue cannot
carry — and only with the operator told. Disable, land the one change, re-enable at once:

```sh
gh api -X PUT repos/<owner>/<repo>/rulesets/<id> -f enforcement=disabled   # open the trunk
gh api -X PUT repos/<owner>/<repo>/rulesets/<id> -f enforcement=active     # close it again
```

`asf doctor` prints the exact pair with the id filled in. Until the ruleset is active again the
doctor row is red, and `queue bypass` lists every commit that landed outside the queue.

## The factory-only rule: a hand branch into the trunk is refused

**The factory-only rule: a hand branch into the trunk is refused.** The ruleset above decides
*how* a change reaches the trunk; this decides *whose* changes may. With
`conventions.merge.factory_only: true` a CI check refuses a pull request into the trunk whose head
branch is under none of the product's `branch_prefixes`. Three escapes stand: a release tag (no
pull request), a PR touching nothing but `merge.bot_paths` (default `CHANGELOG.md`, the file the
release bot writes), and a PR into any branch but the trunk.

A runner cannot read `~/.ASF/products/<product>.yaml`, so turning the rule on takes two committed
files, not one config key:

1. `.asf/product.yaml` — `main`, the factory `branch_prefixes`, and
   `conventions.merge: {factory_only: true, bot_paths: [CHANGELOG.md]}`. Nothing else: no
   `repo_slug`, no `repo_dir`, no account. This file is the rule's only input.
2. One step in the workflow job whose check is already required — for this repository the `tests`
   job, whose `tests (3.12)` / `tests (3.13)` names `tools/merge-pr.sh` will not land without:

```yaml
      - name: the factory-only merge rule (a hand branch into main is refused)
        run: bash tools/check_factory_only.sh
```

Put it in an *already required* job. A job of its own is a check nobody requires until someone
adds its name to `conventions.landing_checks` and to the trunk ruleset — and those live outside
the repository, which is the hole this rule would otherwise still have.

`conventions.merge.require_item_id: true` adds the second half: a head under a factory prefix must
also be named after a card (`worker/T-0123`, a suffix allowed). Without it `fix/` is a factory
prefix and `fix/typo` passes, which is a four-character way round the rule. Leave it off for a
product whose branches are named for the work rather than for a card (`worker/add-login`).

The rule comes off the way it went on: `factory_only: false` in the committed file, one line, one
pull request — which the rule itself lets through, because that PR is on a factory branch.

## Merging an agent PR: `tools/merge-pr.sh <pr>`

This is THE way to merge an agent PR; a bare `gh pr merge` is not allowed. The repo has no merge
queue and no branch protection, and `--match-head-commit` only proves the PR head is unchanged, not
that main is: two PRs, each green on its own head, merged one after the other have broken main
together. The script merges only a head that contains `origin/main` at that moment and has the
required checks (`tests (3.12)`, `tests (3.13)`) green on that exact sha. A head behind main is
updated (`gh pr update-branch --rebase`, a merge of main if the rebase is refused) and the checks
are awaited again on the new head, polling with backoff. If main moves again in between it goes
round again, three rounds at most, then exits non-zero. Red checks refuse. On success it prints
the merged sha; then `git pull --ff-only` in the main checkout and `asf upgrade --wait`.
`MERGE_PR_CHECKS`, `MERGE_PR_ATTEMPTS`, `MERGE_PR_POLL` and `MERGE_PR_TIMEOUT` tune it.

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

- `held <branch>: <class> (<level>) — <file>` — a `merge_*` approval class stops the landing;
  resolve it with `asf approvals resolve` ([product-config.md](product-config.md#approvals)).
  A session's own refused action (a trunk push, a git-hook edit) holds nothing: the item is
  relaunched with the refusal in its brief, and a repeat goes to the groom's adjudicator.
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

A hold records the head it judged. When the correct session launches and `origin/<branch>` has
moved past that head (another push, a later round), its brief gains a `BRANCH MOVED` section: the
real head, the commits and files since, and the instruction to judge each point of the correction
against the current head rather than redo what the new commits already answer. A rebase onto a
newer trunk alone does not count as a move.

### A session asked a question: `asf answer`, not `asf correct`

A session that stops with a question shows as `INPUT <job> needs input — <question>` in the tick
log (and a `blocked` park when it had nothing to land). Answer it with

```
asf answer <job|item> --text "<answer>" --product <p>     # or --file <path>
```

The answer is kept in `~/.ASF/state/<p>/operator-answers.jsonl`; every later brief of the item
quotes it under `OPERATOR ANSWER`, so the next relaunch of the job that asked gets it. It is no
correction round: no round is spent, nothing is refused while the session still runs, and the
question's own park (`blocked`, or a relaunch cap on a question) is released so the item
relaunches. Use `asf correct` only when the work itself needs another round.

### A wrong landing: `asf reset`, not `asf correct`

`asf correct <item> --why …` asks for one more round on work that has not landed. When the ledger
says an item landed and it did not — a PR merged that carried another card's work, a run closed
on a commit that is not the item's — the row sits on NEEDS DECISION and no correction reaches it.
`asf reset <item> --why "<reason>" --product <p>` voids that claim: a reset line naming its
`(pr, head)` in the session ledger, the run's `harvested` cleared, a History line on the card.
The item starts over; a *different* PR of it lands normally, while the voided PR's merge never
counts again, and a session that reports the voided sha again is parked
(`claims voided landing <sha7>`) instead of closing the card. A live session refuses the reset.
`asf reset --undo <item> --why "<reason>"` takes the newest void back.

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
`parent:`, `severity:`, `signature:`, `writes:` or `stories:` lines. The type is always derived
from the card's shape, and a `type:` line is not read: a card becomes a **Bug** when it carries a
`signature:` line (a defect with a signature), a **Task** when it names `writes:`, an **Epic** when it lists
Features, a **Story** under a Feature `parent:`, otherwise a **Feature**. To file a Bug, give it a `signature:` and a
`severity:`.

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

`conventions.flags.groom_rules` adds rules the groom step runs every tick, whether or not a
question was asked: one open Bug per invariant finding's cause and one open scorecard card per
cause class (the younger ones close as duplicates of the oldest, unless work on them began), and
one verify Task for a Feature whose open Tasks' `writes:` the trunk already covers — never a
close (guide/product-config.md). `asf reopen` undoes a close.

**The daily stamp.** The `daily` step runs at most once a day, remembered in
`~/.ASF/state/<p>/daily.stamp` as the **local** date. A stamp written today by something other
than the daily clock — a hand-run `asf tick --daily`, a copied state directory — makes the
scheduled daily print `tick: step daily already ran today`. Delete the file to clear it, or force
a run: `asf tick --product <p> --steps daily --daily`.

## Your checkout drifts from the tick's clone

Every console command that writes the record commits and pushes what *it* wrote — `asf inbox`,
`asf new`, `asf set`, `asf groom` and `asf groom --apply` — and nothing you changed by hand rides
along. Your own edits stop in your checkout: the tick, working in its clone from `origin`, never
sees them, so commit and push after each. Pull before you edit — the tick pushes a `tick: state`
commit every run, so an unpulled checkout is always behind.

## Widening a Task's footprint

A push is refused because the diff names a file outside the Task's `writes:`. Widening it is one
command, and `asf set` publishes the card itself (above):

    asf set T-0338 writes+=asf/tick/widen_footprint.py

`writes:` and `after:` are the two list fields: `+=PATH` adds, `-=PATH` removes, `=[a, b]`
replaces, and `+=[a, b]` takes several at once. A path the footprint already covers — named, or
under one of its globs — is not added twice, and a `-=` that would remove nothing is refused
rather than reported as done. A Task never ends with an empty `writes:`.

Two refusals are the record protecting itself, not a bug:

- `I3: writes: intersects Active task T-0412's writes:` — another Active Task holds that path. One
  of the two has to finish first; the card is left exactly as it was.
- An approvals-protected path — it goes to its approval class, not into a footprint.

Most widenings are not yours to make. When a coder session's REPORT names `needs writes:`, or its
branch goes red on a file outside the footprint, the tick widens the Task itself and sends the same
session back as a correction with no round spent. The command above is for the card in front of
you — a plan that under-scoped a Task you are about to start, or a footprint you are narrowing.

## The direct lane, small Features, and comparing them

A Feature's route is a typed field:

- `asf set F-x lane=direct` — one session builds it end to end on `cloud/direct-F-x`
  (`branch_prefixes.direct`): the feeder's one row is `DIRECT → BUILD`, never a spec or plan
  row. Its first commit's body is the "what and how" note the PR description carries; every
  subject is `feat(F-x): …`. The lane lands it like any code PR — CI, the gate, the
  customer-content check and auto-merge — without a review round, and the trunk commit naming
  F-x marks the Feature landed. `lane=full` (or no field) is the full pipeline.
- `asf set F-x size=s` — on the full lane, spec and plan are one session (`CARD → SPEC+PLAN`, one
  document with Task lines), and a Task whose diff is under `review.skip_under_lines` (80) skips
  the review session.
- `asf set F-x ab_pair=<name>` on one direct and one full Feature makes them a pair.
  `asf scorecard --by-lane [--days n]` prints per lane the Features landed, median lead time,
  $ per Feature, sessions, repair sessions and CI minutes per Feature, then one row per pair with
  the delta; the daily scorecard line carries the 7-day lane split. `asf doctor` (row `ab pairs`)
  and `asf status` (row `A/B pairs`) warn when a pair's two Features touch the same files.

## Program metrics and targets

`asf scorecard --json [--window 7d|36h|since=<iso>] [--all]` adds a `program` row: `idle_hours`
(launch gaps over 2 h; `--all` counts every product's launches), `offline_ticks`,
`waste_by_class` (each run classed once: failed, loop, superseded, nothing),
`mechanical_only_corrects`, `max_runs_job_head`, `heavy_share`, `cardless_heavy_reviews`,
`reshape`, `infra_ended`, and over the board `rows_waiting_on_item`, `top_roots` and
`reviews_held_after`. Keys whose producers land later read `null`.
`asf scorecard --check docs/program/targets.yaml [--window …]` reads each target (`key`, `op`
of `==`/`<=`/`<`/`>=`/`>` or `measured`, `value`) against that row and exits 1 listing every
missed one; a key the row could not read misses any numeric target.

## Clearing a card that does not belong

A card that is not this product's work — a Bug filed against the wrong product, a Feature nobody
wants, an item that landed by hand outside the factory — is retired with `removed:`, never
deleted, and never by a hand edit:

    asf retire <item> --why "<reason>" [--landed <pr|sha|url>...]

`<item>` is a card id, or a note in the intake directory no groom has typed yet (its file name,
with or without `.md`, or its path). A card gets `removed: retired: <reason> (landed …)`, a
`landed:` sha when a ref is one, and a History line; a note moves to `inbox/done/` with the same
words in its header. Either way it goes through the `asf set` publish path (one signed-off commit
with `index.json`, retried when another lands meanwhile, then pushed), and the groom never mints
it again — not from a note, and not from the same title or signature filed a second time.
`asf set <id> removed=<reason>` writes the bare field.

A delivery member the lead's PR does not build (an adjudication struck it) is taken out of the
delivery, not retired — retiring a member is refused while its lead still lists it:

    asf undeliver <task> --why "<reason>"

It drops `<task>` from its lead's `delivers:` (the whole list when only the lead is left), deletes
the member's `delivered_by:`, and writes `undelivered from <lead>: <reason>` into both cards'
History, in one record commit with `index.json`. The member goes back to its own lane on the next
tick instead of `WAITS ON delivery <lead>`. It is refused for a card with no `delivered_by:`; a
lead already Closed or retired is cleaned all the same.

On the next tick the card leaves the tables and the wave: nothing is started on it, and a session
already on it is ended and its worktree reaped rather than sent back. An S1 Bug retired this way
releases the tier-2 freeze. (`moved_to:` retires rule cards only; use `removed:` for everything
else.)

## The CI runner pool

A product whose CI runs on self-hosted runners declares them in `ci.pool` of its product file.
The doctor's `ci pool` rows say when the host drifts from it; `asf ci reconcile --product <p>`
prints the plan (runner, current labels, target labels, action) and `--apply` writes it, adds
before removes. Jobs ask for a role (`heavy`, `light`), never a provider. The whole procedure,
including moving an existing product over, is in [the CI runner pool](ci-runner-pool.md).

## Console permissions

The operator's own console — the orchestrator session, not a worker account's — is a Claude Code
session too, and under its default auto mode every one of the factory's own maintenance commands
hits the safety classifier's prompt: `bash tools/install.sh`, `asf approvals resolve`, a
`launchctl` pause/resume of a clock, a push of a lane branch, `git worktree` cleanup. An approval
given in chat does not persist — the next session asks again — and the session cannot add its own
allow rule (that is refused as self-modification). Left unfixed, fixes land on `main` and are
never installed (B-0131).

`tools/install.sh` ends by printing the allow list the console needs (`asf console-permissions
offer --product <p>`) and telling you the command that writes it — it writes nothing itself:

```
allow  Bash(asf:*)
allow  Bash(bash tools/install.sh:*)
allow  Bash(launchctl bootout gui/*/asf.*)
allow  Bash(launchctl bootstrap gui/*)
allow  Bash(git worktree:*)
allow  Bash(git push origin <prefix>*)      # one per lane branch prefix this product declares
deny   Bash(git push --force* origin <main>)  # this product's own trunk, never a literal name
```

Run `asf console-permissions install --product <p> --scope user` to write it into your own
user-level Claude Code settings (`~/.claude/settings.json`, every product's console), or
`--scope repo` for the product repo's own `.claude/settings.json` — Claude Code reads rules from
both, so either is enough. The merge is idempotent and keeps every unrelated key. `asf doctor`'s
`console permissions` row is red while neither file carries every rule, and names the ones
missing.

## Safety: what a worker session can reach

A worker session starts from **an allow-list, not the tick's environment**: `PATH`, `LANG`,
`LC_*`, `TERM`, `TMPDIR`, `USER`, `SHELL`, the names you list in `worker_pool.env_passthrough`, and
the job's own variables (`ASF_PRODUCT`, `ASF_JOB`, …, the account's `CLAUDE_CONFIG_DIR`). A secret
exported in the shell that ran the tick does not reach it. Its `HOME` is its account's own
(`~/.ASF/state/homes/<account>`), holding only what `home_seed` lists and a `.gitconfig` naming the
agent it commits as — `asf worker <asf-worker@localhost>` by default, never your own `user.name`
or `user.email`, so a session's commits are never filed under your name. `GIT_CONFIG_*` in the
session's own environment carries the same pair and is what actually binds it: it outranks this
file, a `home_seed`ed `~/.gitconfig` and even a repo-local `user.name` a product's
`worktree_setup` sets. `worker_pool.accounts[].identity: {name, email}` overrides it for one
account — the code host's `noreply` form, say. `isolate_home: false` gives a session your `HOME`
back, your own identity included; `asf doctor`'s `worker env` row is red while any account does.

### Credentials: `auth_env`

A HOME of its own has no login in it, and on macOS the runtime's own login sits in your login
keychain, which a session finds through your `HOME` — so an isolated session fails with "Not
logged in", and a `git push` over HTTPS finds no credential either. Each account gets its
credentials explicitly, as environment variables read from files at every launch:

```yaml
worker_pool:
  accounts:
    - name: acct-a
      auth_env:
        CLAUDE_CODE_OAUTH_TOKEN: ~/.ASF/secrets/acct-a.token   # the runtime's login
        GH_TOKEN: ~/.ASF/secrets/acct-a.gh                     # git push over HTTPS, and gh
```

The runtime token is per account — one `CLAUDE_CODE_OAUTH_TOKEN` covers every product that
account works on. GitHub access is per **product**: products can live under different GitHub
owners, and a fine-grained token covers one owner only, so the same account's `GH_TOKEN` can't
serve two products under different owners. A product names its own GitHub token under its own
`products/<name>.yaml`, in `conventions.auth_env`:

```yaml
conventions:
  auth_env:
    GH_TOKEN: ~/.ASF/secrets/<product>.gh   # this product's own owner
```

At every launch, the worker environment is the account's `auth_env` merged with the product's —
the product's value wins for a variable both name. Put under `conventions:` (not a top-level
key), an older `asf` — which keeps unknown `conventions` keys but rejects an unknown top-level
one — still loads the file (rollback rule R23).

Create the files once per account:

```sh
mkdir -p ~/.ASF/secrets && chmod 700 ~/.ASF/secrets
# the runtime's long-lived token: authorize as that account in the browser it opens,
# then copy the token it prints
claude setup-token
pbpaste > ~/.ASF/secrets/acct-a.token && chmod 600 ~/.ASF/secrets/acct-a.token
# a fine-grained GitHub token: https://github.com/settings/personal-access-tokens/new —
# repository access: the product repo only; permissions: Contents and Pull requests read/write
pbpaste > ~/.ASF/secrets/acct-a.gh && chmod 600 ~/.ASF/secrets/acct-a.gh
```

What ASF does with them:

- Each file's content (whitespace stripped) becomes that variable in **that account's sessions
  only** — and in its `worktree_setup` command — merged with the product's own `auth_env`, the
  product's value winning for a variable both name. Never in the tick, never in another
  account's, never in another product's.
- A missing, unreadable or empty file refuses the launch with `NEEDS OPERATOR`, naming the file and
  how to create it — the same rule for an account's file and a product's. Nothing is made first: no
  worktree, no ledger line.
- With `GH_TOKEN`, git in the session gets, through `GIT_CONFIG_*` variables, an HTTPS credential
  helper for `https://github.com` that echoes the token from the session's own environment, after
  resetting every other helper for that host (the system keychain helper included). The token is
  never written to a file, a config or a remote URL. Nothing else from your `~/.gitconfig` is
  used.
- Values are never logged: the session record and the brief hold none, and a `worktree_setup`
  command's output is written to its log with each value replaced by `[redacted:<VARIABLE>]`. The
  redaction gate (`asf redact`, the pre-commit and pre-push hooks) searches for every account's and
  every product's `auth_env` value, whatever the variable is called.
- `asf doctor`: the `worker env` row is red when an isolated account has no `auth_env` for the
  runtime's login variable (it names `claude setup-token`); the `worker secrets` row lists each
  account's and the product's own `auth_env` file's presence by variable name only (a product's
  labelled `product:<name>:<VAR>`), red when one is missing.

`tools/smoke_isolated_session.sh <product> [account]` launches one real session this way and checks
it authenticates, works and pushes.

### What this is, and what it is not

HOME and environment isolation under **your own OS user** stops *accidental* credential use: a
session no longer inherits your shell's secrets or your CLI logins, and a tool that looks for a
login under `HOME` finds none. It is **not a hard boundary**. The session runs as you, so a process
in it can still read your keychain or any file you can read by its absolute path. A hard boundary
needs each worker account to run as a separate OS user (its own keychain, its own file
permissions); that is future work.

Until then, also:

- Keep the CLIs a worker can reach on test-mode or non-production contexts.
- Map what must never happen unattended with `approval_signals` (commands and paths) at
  `human-now` ([approvals](product-config.md#approvals)).
- Scope each `auth_env` token to the least it needs (one repo, push and PRs only).

## Token economy

The model a session runs on is routed per brief kind and item class
([product-config.md](product-config.md#models--the-model-a-session-runs-on)): a cardless PR's
review (`PR-<n>`), a reshape and a replan run `light`; `conventions.models.<kind>: heavy` puts a
kind back on the heavy model. Mapping `worker_pool.models.cheap` to the pool's smallest model is a
host-wide `config.yaml` edit every product reads at once — make it inside a planned move window.

Discovery — a session reading its way to what the runner already knew — is most of what a run
costs. Two things in this factory push back on that:

- **The preamble's "Where to look" section.** Every brief already carries the card, the state and
  the footprint generated, never searched for (`asf.briefs.preamble`); it also carries, for each
  file the item's `writes:` names that exists on the trunk, that file's top-level functions and
  classes with their line ranges — computed from the checkout, not from the session opening the
  file itself. It is capped and trimmed like the rest of the preamble, so it is never why a brief
  goes over budget.
- **Roles** (`asf/roles/*.md`, `asf roles`) carry a session's identity and doctrine, separately
  from the model and access an operator configures for it. One of them, `locator`, is a read-only
  finder — file:line locations with a short excerpt, never a whole file — but it ships unbound
  (`asf roles` lists it with its reason): nothing in this factory yet launches a role as a
  separate, tool-restricted sub-agent a worker session can call. Until that launch path exists,
  the preamble's closing line under "Where to look" says to read only the line ranges it names,
  not to reach for a locator that is not there.

The planned [connectors](connectors.md) replace this with default-deny scoping per service.
