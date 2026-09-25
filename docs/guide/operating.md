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
printed `[step:<name>] FAILED <why>`; a failed record step prints `RECORD STALE — <why>`. Last of
all, a digest of what this tick did: `TICK — record ok, health ok, …` then the non-zero counters
(`launches 2, merges 1, relaunches 1`), or `nothing launched, merged or stalled`.

`asf watch --product <p>` tails that digest as it lands, tick after tick, without running one
itself — read the console instead of a clock's log when a clock (not you) is the one ticking.

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
write new files with `deploy_sha.workflow` (or `deploy_sha.prod.workflow`).

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
(`~/.ASF/state/homes/<account>`), holding only what `home_seed` lists and a `.gitconfig` with your
`user.name` and `user.email` — none of your CLI logins. `isolate_home: false` gives a session your
`HOME` back; `asf doctor`'s `worker env` row is red while any account does.

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
