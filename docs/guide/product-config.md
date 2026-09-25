# The product file

`~/.ASF/products/<product>.yaml`, key by key, for the keys that change what the factory does. The
full annotated shape is [`docs/products.example.yaml`](../products.example.yaml); an unknown
top-level key is refused on load (`NEEDS OPERATOR: … is not a field of the product file`). Shared,
machine-wide settings live in `~/.ASF/config.yaml` ([`docs/config.example.yaml`](../config.example.yaml)).

The file is YAML, but read by a small built-in reader: nested block maps, `- item` lists, inline
`[a, b]` lists, scalars and `#` comments. **No inline maps**: `{a: 1}` is read as the string
`"{a: 1}"`, not a map — always write a map as an indented block. No anchors, no multi-line
strings. A key's shape is checked: `customer_paths` must be a list (`- apps/web/**` lines, or
`[apps/web/**]`), never a single string.

### Old keys, and where they went

A file written for an earlier release, or copied from an older product, may carry these. The
product file refuses an unknown key; the operator config keeps reading its deprecated ones, and
the doctor's `capacity` row names the two old capacity keys.

| old | new | notes |
| --- | --- | --- |
| `ci.deploy_workflow` | `deploy_sha.workflow` | read as an alias, last; see the Prod row note in [operating.md](operating.md#asfstatus--factory-status) |
| `conventions.harvest_gate`, `.branches_per_tick`, `.gate_timeout_s` | `conventions.harvest:` → `gate`, `branches_per_tick`, `gate_timeout_s` | both spellings are read |
| `conventions.test_command` | `ci.test_command` | both are read; the `conventions:` one wins |
| `conventions.ci_workflow`, `.ci_dev_job`, `.deploy_workflow` | `ci.workflow`, `ci.dev_job`, `deploy_sha.workflow` | both are read; the `conventions:` one wins |
| `conventions.task_heading` | — | not read; the evidence pass finds a plan's tasks by `## Task N` to `#### Task N` (or `T1`) headings |
| a clock running `asf tick --daily` | a clock with `steps: [daily]` | an installed job of the old form still reads back as the daily clock |
| `scheduler.interval_s` (config) | the product's `clocks:` | no longer read; the doctor flags it |
| `scheduler.provider` (config) | `scheduler.kind` | still read as a synonym |
| `feeder.capacity` (config) | `capacity.per_product.sessions` | still read |
| `worker_pool.reserve_for_s1` (config) | `capacity.reserve_for_s1` | still read |
| `worker_pool.quota_guard: {max_5h, max_7d}` (fractions) | `quota_guards.stop` (percent) | still read, as the stop for those windows; so is a flat `quota_guards: five_h: 95` |
| `worker_pool.accounts[].share` | `worker_pool.accounts[].cap` | `share` is not read |

## Repos and directories

| key | meaning |
| --- | --- |
| `product` | the product's name; matches the file name |
| `repo_slug` | `owner/name` on the PR host, for every `gh` call |
| `repo_dir` | the product's code checkout. Command steps run here; harvest fast-forwards it after a landing |
| `main` | the trunk branch (default `main`) |
| `backlog_dir` | your checkout of the record (see below) |

### The two copies of the record

The record exists twice on this machine, and they have different owners:

| copy | path | who writes it |
| --- | --- | --- |
| your checkout | `backlog_dir` | you: `asf inbox`, `asf new`, `asf set`, groom answers, hand edits |
| the tick's clone | `~/.ASF/state/<product>/record/` | the tick, and nothing else |

Every tick resets its clone hard to the record's `origin`, derives the state there, and ends with
one commit (`tick: state <ts>`) that it pushes to `origin` — rebasing onto `origin` and retrying if
it moved meanwhile. It reads your checkout once, for the `origin` URL, and never writes it.

So the two meet only at `origin`. What you write reaches the tick when you push it (`asf inbox`
and `asf new` commit and push for you). What the tick derived reaches your checkout when you
`git pull` there — and the tables (`asf status`, `/asf:backlog`, `asf next`, …) read your
checkout, so pull before you read them. Each table prints `record: <path>` first, and the board
and roadmap print when their `index.json` was generated.

## Conventions

`conventions:` holds every path and name the record and the tick need, so no tool hardcodes one.
The ones that matter most:

| key | default | meaning |
| --- | --- | --- |
| `specs_dir`, `plans_dir`, `reviews_dir` | `docs/specs`, `docs/plans`, `docs/reviews` | where spec, plan and review documents live in the product repo |
| `review_pattern` | `{reviews_dir}/{n}-{slug}.md` | a **code** review's file name (`{slug}` is the item id in lower case, `{n}` the round), read when harvest merges a PR. Spec and plan reviews use a fixed name — see below |
| `branch_prefixes` | `code: worker/`, `fix: fix/`, `spec: spec/`, `plan: plan/` | the branch each job kind pushes (`groom` too); `legacy: [..]` names old prefixes that are recognised, never minted |
| `intake_dir` | `inbox` | where `asf inbox` drops a card for the groom |
| `default_bug_epic` | none | the Epic a filed Bug is parented under; unset, the groom asks |
| `amendable_paths` | `[]` | globs that make a landing `merge_amendable_set` (see approvals) |
| `landing` | derived | `fast-forward` or `pull-request` — below |
| `harvest:` → `gate`, `branches_per_tick`, `gate_timeout_s` | `combined`, 12, 600 | how harvest gates: one gate over all eligible branches (bisecting on red) or `per-branch`; how many per run; seconds before a gate is killed and counted red |
| `prs_per_tick` | 6 | PRs the `prs` step opens per tick |
| `models:` → `<kind>: heavy\|light` | per kind | which of the two model labels a job kind runs on |

`ci.test_command` (under `ci:`, not `conventions:`) is the gate: the command a branch must pass
before it lands. Without one there is no test gate.

### Landing: fast-forward or pull-request

A worker session pushes its branch. Harvest (the tick's `harvest` step) decides how it reaches the
trunk, by `conventions.landing`:

- **`fast-forward`** — harvest rebases the finished branch onto `origin/<main>` in a throwaway
  worktree, runs the gate (`ci.test_command`), and pushes it to the trunk fast-forward only (never
  a force push), then deletes the branch. The `prs` step opens no PR (`prs: landing is
  fast-forward — harvest lands the branches`). A red gate holds the branch and sends it back to
  its session.
- **`pull-request`** — the `prs` step opens a PR for each finished branch, and on a PR host (a
  `repo_slug`, or an origin on a hosted repo) harvest merges those PRs itself. A PR is merged when
  all of these hold:
  - the approval matrix allows the landing (`merge_routine_pr`, or `merge_amendable_set`);
  - for a code, fix or task branch: the newest ASF review of its item on the branch — the
    `review_pattern` file with the highest round, `{slug}` being the item id in lower case — has a
    `verdict: approved` line, written after the branch's last change outside `reviews_dir`.
    With no such review — none yet, or only a round older than the head — harvest asks for one:
    the feeder launches a `PUSHED → REVIEW` session on the PR's branch (S1 first, then Bug
    fixes, within capacity, one per branch, no correction round spent), which writes the next
    round's file there. The feeder reads this off the open PRs and their branches, not off the
    session ledger, so a PR opened before any request was written gets its review too; and an
    item with an open code PR gets no second `BUG → FIX` or `PLAN → CODE` session — its PR is
    reviewed, or waits to land (`WAITS ON landing`) once its head is approved. `verdict: changes requested` sends the branch back to its writer as
    `FIX → CORRECT`; its next push asks for the next round. A **spec or plan branch that only changes files under `specs_dir`,
    `plans_dir` and `reviews_dir` needs no review**: merging it is what approves its document;
  - its checks are green (no checks at all counts as green);
  - **the trunk stays green** — see *Green checks are not a green trunk* below.

  Then `gh pr merge --squash --delete-branch` (then `--merge`, then `--rebase` if the repo refuses
  one), or `--auto` when the trunk has a GitHub merge queue. Hotfix and S1 branches go first;
  `capacity.batch.per_run` caps the merges per harvest and `capacity.batch.parallel` the PRs in the
  merge queue at once. A review asked for, or pending checks: `waiting <branch>: …` until the next tick. Red
  checks: the branch is held and sent back to its session. A PR someone else merged — a `batch`
  step, a person — is found merged and its session closed. A product with no PR host only gets the
  `pr-lane <branch>` mark and needs a person or its own `batch` step to merge.

Unset, `landing` is derived from `steps.batch`: `off` or unset means `fast-forward`; a command
means `pull-request`. Set `landing: pull-request` to get PRs without a `batch` step.

#### Green checks are not a green trunk

A product's CI is often path-filtered: its main gate job does not run on a docs-only PR, so a
spec or plan PR is "green" on a trivial check alone (a sign-off check, say) — and a rule that
runs only on the trunk can then reject what the document cites, turning the trunk red for every
branch after it. So harvest never merges a PR on its checks alone:

- **The local gate.** Every PR the rules above allow is gated on this machine first, the way
  fast-forward landing gates a branch: its head rebased onto `origin/<main>`, then the product's
  gate (`test_command`, and on asf's own repo its own checks). All the PRs mergeable in one
  harvest are stacked into **one** combined head and gated **once**, bisecting on red; the gate
  runs under the product's harvest lock, so never two at a time. Only a green head is merged.
  A red one goes back: a code PR to its session (a correction, as a red fast-forward gate); a
  docs-only spec or plan PR as a `STARVED → SPEC` / `STARVED → PLAN` session on its branch,
  whose brief ends with the failing gate line. When the trunk alone is red too, no PR is blamed:
  they wait for the next harvest.
- **Required checks.** The checks named in `conventions.landing_checks: [job names]` — else the
  ones the trunk's branch protection requires (read with `gh api`, cached for an hour) — must
  have *run* and passed. One that did not run (path-filtered: absent, or skipped) is not green.
  What harvest does then is `conventions.landing_checks_missing`:

  | value | meaning |
  | --- | --- |
  | `local-gate` (default) | the local gate stands in for it — it runs for every PR anyway |
  | `wait` | CI is the gate, not this machine: a PR whose required checks all ran and passed merges without a local gate; one missing a required check waits for it |

  It takes one value, or a map per landing class — `docs` (a docs-only spec/plan PR) and `code`
  (everything else), with `default:` for an unnamed class:

  ```yaml
  conventions:
    landing_checks: [build]
    landing_checks_missing: {docs: local-gate, code: wait}
    landing_checks_wait_min: 30
  ```

  A `wait` never waits for ever: a required check with no run on the same PR head after
  `landing_checks_wait_min` minutes (default 30) will not come — path filters never queue it —
  and the PR is gated locally instead. With no required check declared there is nothing to wait
  for, and the local gate runs.

**A product whose gate is heavy** — minutes long, gigabytes of memory, a whole monorepo build —
should set `landing_checks_missing: {docs: local-gate, code: wait}` (or plain `wait`) and name
its CI gate job in `landing_checks`: code PRs then merge on CI's run of that job, and only what
CI never gates (a docs PR its path filters skip) is gated here, one combined gate per harvest.

### What approves a spec or a plan

A Feature moves from spec to plan to code on evidence, never on a status someone typed. A spec (or
plan) counts as approved when either:

- the document is on `origin/<main>` — landing it is its approval (in fast-forward mode harvest's
  gate is the review); or
- the newest review file for it on the branch that carries the document carries the verdict
  `APPROVED` — the first of `APPROVED`, `CHANGES REQUESTED`, `BOUNCE`, `REVISE` anywhere in its
  text.

The review file for a spec or plan has a **fixed name**, whatever `review_pattern` says: in
`reviews_dir` on the document's branch, `<slug>-review-r<n>.md` (also accepted:
`spec-<slug>-review-r<n>.md`, `<slug>-spec-review-r<n>.md`, and the same with `plan`), `<n>` being
the round. `<slug>` is the branch name after its `spec`/`plan` prefix, matched case-sensitively —
the review on `spec/F-0042` is `docs/reviews/F-0042-review-r1.md`. (For a document the evidence
finds only on `main`, the slug is its file name without the leading `YYYY-MM-DD-` and `.md`; but
a document on `main` is approved already.) A spec review saved under the default `review_pattern`
(`docs/reviews/1-F-0042.md`) is never read, and never approves.

`review_pattern` and a `verdict: approved` line apply only to code PRs (above).

In pull-request mode, a spec or plan waits in its PR until harvest merges it. Meanwhile the NEXT
table shows it as `PUSHED → LAND` / `WAITS ON landing`, so no second session is started on it.

## Steps and clocks

### Steps

The tick has seven steps, always in this order: `record`, `health`, `wave`, `prs`, `harvest`,
`batch`, `daily`. `steps:` says who owns each:

```yaml
steps:
  health: asf                          # the default for every step ASF implements
  batch: "bash tools/merge-queue.sh"   # a command: yours, run by the tick
  daily: off                           # off: the tick skips it (another job runs it)
```

`asf tick --product <p> --manifest` prints the resolved table (step · owner · command). A step with
no owner — `batch` is the one ASF has no implementation for — is a refusal: the tick exits 2 before
running anything (`tick: step batch has no owner — declare it under steps …`). The check covers
the steps a run includes: a full `asf tick` with no `--steps`, or a clock whose `steps:` names
`batch` (`asf scheduler` refuses that clock). A product whose clocks never name `batch` is never
refused — but a hand-run `asf tick --product <p>` is. Declaring `batch: off` avoids both.

A **command step**:

- runs in `repo_dir` (the tick's own directory only when there is none), split shell-style with
  `~` expanded and no shell — wrap it in `bash -c '…'` if you need pipes;
- gets the tick's environment plus `ASF_PRODUCT` and the capacity the resolver computed:
  `ASF_CAPACITY_SESSIONS` always; `ASF_CAPACITY_CI` when a CI ceiling is configured;
  `ASF_CAPACITY_BATCH_PER_RUN`, `ASF_CAPACITY_BATCH_PARALLEL`, `ASF_CAPACITY_RUNNERS` from
  `capacity.batch`, each only when set — so your script's own default still wins where ASF has no
  opinion;
- may run `tick.step_timeout_s` seconds (in `config.yaml`, default 900); past that its whole
  process group is killed and it exits 124;
- has its output prefixed `[command:<step>]` in the tick log, and never overlaps itself: a second
  start while it runs prints `tick: step <s> is already running — skipped`.

A step that fails prints one line and the tick goes on; the tick exits 1. The exception is
`record`: when it fails nothing else runs (`tick: record failed — …; nothing else ran`), because
every later step would act on a stale board.

### The batch step

`batch` is an optional merge-queue script of your own, in your repo. On a PR host harvest already
merges the PRs itself (above), so most products declare `batch: off`; keep a script only for what
harvest does not do. ASF runs it as a command step and passes the batch shape from
`capacity.batch` (`per_run`, `parallel`, `runners`) as the environment above — the same
`per_run` and `parallel` also bound harvest's own merges. When a CI ceiling is configured and that
many CI runs are already in flight, the tick does not start it: `waits    batch — at ci capacity
(n/m)`. The ceiling gates only this step: a worker's PR push, a harvest merge onto the trunk and
a deploy dispatch start their runs regardless, and every `ci.workflow` run counts as in flight —
so the status row reads `ci 13 runs in flight (batch starts below 4 — batch waits)`. Declaring `batch` as a command also switches the default landing to `pull-request`.

### Clocks

`clocks:` is what runs when. Each entry becomes one scheduler job, labelled
`<label_prefix>.<product>.<name>` (prefix `asf` by default) and logging to
`~/.ASF/logs/tick-<product>-<name>.log`:

```yaml
clocks:
  record:
    steps: [record]
    every: 5m
  dispatch:
    steps: [health, wave, prs, harvest, batch]
    every: 10m
  daily:
    steps: [daily]
    at: "06:50"          # local time
  shadow:
    shadow: true         # record only, in a throwaway clone; never pushed
    every: 30m
```

Every clock needs `steps: [...]` or `shadow: true`, and exactly one of `every: <n><s|m|h|d>` (60 s
at least) or `at: "HH:MM"`. Names are lower-case letters, digits and `-`. `asf scheduler render`
prints the job definitions; `asf scheduler install` installs them and, on launchd, boots out this
product's jobs that are no longer a clock; `asf scheduler status` reads them back.

### Locks

Clocks of one product share the record clone, so they take turns:

- A clock with any ASF step holds the product lock (`~/.ASF/state/<p>/tick.lock`) for its whole
  run. An interval clock that finds it taken does nothing: `tick: another tick of <p> is running —
  skipped`. A clock that includes `daily` waits for it, up to 45 minutes.
- A clock of command steps only runs its commands outside that lock, and takes it briefly (waiting
  up to 10 minutes) around the parts that touch the record: `tick: another tick of <p> holds the
  record — <what> skipped` when it cannot.
- Harvest's gate can take many minutes, so the `harvest` step starts it as a background process
  under its own lock (`harvest.lock`) and returns. While it runs, each tick prints `harvest: gate
  running (pid …)`; the first tick after it ends prints what it landed or held.

`daily` also runs once per day at most — a stamp file remembers the date (`asf tick --daily`
forces it).

## Capacity

Ceilings on concurrent work, never a target. In `config.yaml`:

```yaml
capacity:
  total:              # across every product
    sessions: 6
    ci: 4
  per_product:        # the default for a product that declares none
    sessions: 2
    ci: 1
  reserve_for_s1:
    local: 1
    cloud: 1
```

and in the product file `capacity:` with `sessions`, `ci` and a `batch:` block (`per_run`,
`parallel`, `runners`).

A product's session ceiling is its own `capacity.sessions`, else `per_product.sessions`, else 4 —
then capped by `total.sessions` less what other products have in flight, then by its **fair
share**. Every product draws on one worker pool. The pool's usable slots right now are, per
account, its `cap` when free, 1 in cooldown, 0 at stop; with two or more products whose `wave` is
ASF's and on a clock, each gets `ceil(usable × weight / Σ weights)`, where a product's `capacity.weight` (default 1) is the operator's priority: a product at weight 3 beside one at weight 1 gets three quarters of the pool. A product over its share keeps its
running sessions — it just gets no new slot: `waits <job> <item> — fair share: n of u usable
slots across k products`.

The CI ceiling is the smaller of the product's own ceiling and `total.ci`. The product's own is
its `capacity.ci`, else the slots of its declared runner pool (`ci.pool`, the sum of every
runner's `slots` — see [the CI runner pool](ci-runner-pool.md)), else `per_product.ci`; with none
of them set there is none and no `gh` call is made. `asf capacity` names the source in its
`ci from` column: `product`, `ci.pool: heavy 12, light 7`, `product (overrides ci.pool 19)`,
`operator default` or `operator total`.

With a runner pool declared, every CI run ASF starts also goes through the product's **CI start
queue** (`ci.queue`), which starts a run only while each runner class it needs has enough free
runners — see [the CI start queue](ci-runner-pool.md#the-ci-start-queue).

**Quota bands** (`config.yaml`, percent of each usage window):

```yaml
quota_guards:
  stop:
    five_h: 95
    seven_d: 95
    seven_d_model: 95
  cooldown:
    five_h: 90
    seven_d: 90
    seven_d_model: 90
```

At or above `cooldown` in any window, an account takes one job at a time (it is picked only while
it runs nothing: `quota cooldown — one job at a time`). At or above `stop` it takes none; when no
account is under its stop the row says `NEEDS OPERATOR: no account under quota`. Running sessions
are never stopped — the bands gate launches only. The bands need `worker_pool.quota_command` (a
command, `{account}` substituted, printing one JSON line with `five_h_pct`, `seven_d_pct` and
optionally `seven_d_model_pct`). Without it every account reads 0 % and is always free; an account
whose command fails reads as `stop`.

**The 5h window as a budget.** Each launch carries an estimate of its share of the 5h window, per
kind and model family: a fixed table while history is thin (Opus spec/plan 10 %, Opus review 6 %,
any Sonnet 4 %), else the median `total_cost_usd` of that kind's recent runs over the dollars of a
whole window. That dollar value is `quota_guards.five_h_usd` when set, else estimated from the
wave's own `five_h_pct` readings (`~/.ASF/state/quota-samples.jsonl`) against what the account's
runs spent between two readings. An account takes a launch only while its reading, plus this
wave's launches on it, plus `running_allowance` (default 0.5) of each running session's estimate,
plus the launch stays under `stop.five_h`; else the row goes to another account or waits with
`headroom: <acct> would exceed 65% (now 51%, +10% committed, +10% this launch)`.

```yaml
quota_guards:
  five_h_usd: 40          # optional: the dollars of 100 % of a 5h window
  running_allowance: 0.5  # optional: the share of its estimate a running session still holds
```

A session that ends on the CLI's session/usage-limit message is `failed: quota-exhausted`: no
correction round, no hold, no attempt counted. Its account is stopped until the reset the message
names (`~/.ASF/state/quota-limits.json`; an hour when it names none, and `asf status` shows
`stop — resets 15:20`), and the item relaunches after the reset or on another account, in the
worktree its partial work sits in.

**Host guard** (`config.yaml`): a loaded host starts no new session and no landing gate.

```yaml
host_guards:
  load_per_core: 2.0   # the 15-minute load average over the core count
  swap_pct: 85         # swap in use, percent
```

At or over either (the defaults when the key is absent; 0 turns one off), the tick's wave step
builds no brief and starts nothing: each launching row prints `waits <job> <item> — held: host
pressure load 90/cores 12, swap 87%`, the step ends on `wave: held: …`, and a `host_pressure`
event lands in `metrics/events`. Running sessions are never stopped. A value the host does not
report (no swap reading, say) never holds. `ASF_HOST_READING="<load15> <cores> <swap_pct>"`
stands in for the host — to see what the tick would do at a given load.

The same guard holds the landing gate, which is the product's whole test suite on this host: over
either threshold the harvest starts none, prints `harvest: held: host pressure … — no gate started
this tick, retried next tick`, and each branch waits at `host-pressure` — unknown, never red, no
correction and no round, re-gated next tick. A PR merging on its external CI's checks alone runs no
suite here and is unaffected.

A product whose code PRs its external CI gates (`landing: pull-request` with
`conventions.landing_checks` named, or `landing_checks_missing` `wait` for code) gets one more
standing rule in every brief: run only the targeted checks locally and push — the full suite is
that CI's, and a PR merges only once it is green. Any other product's own gate is unchanged.

That rule is enforced, not only written: the product names its own full-suite and full-gate
commands in `conventions.full_suite_commands`, a list of regexes, and the approvals hook refuses a
session any Bash simple command one of them matches (split on `&&`, `||`, `;`, `|` and lines,
leading `VAR=value` dropped, heredoc bodies ignored), telling it to run only the targeted tests for
what it changed. The refusal is written to `approvals.jsonl` as a `refused-full-suite` line for the
audit trail only — no hold, nothing parked, the item keeps its slot. Match only the unscoped forms,
so a runner given specific paths stays allowed:

```yaml
conventions:
  full_suite_commands:
    - '^make test$'          # bare: refused; `make test T=tests/test_x.py` is allowed
    - '^make check\b'
```

A product without external CI ignores the key; a pattern that is not a regex is a red `doctor`
finding.

S1 reserve: while an S1 Bug is open, each lane keeps `reserve_for_s1` slots for `BUG → FIX` rows.

`asf capacity --product <p>` (or `--all`, `/asf:capacity`) prints the resolved numbers and which
term bounds each.

### Holding a class of new work

```yaml
feeder:
  hold: [features]    # or [bugs], or both; empty (the default) holds nothing
```

`feeder.hold` stops the feeder starting new work of a class while everything already in motion
carries on. With `features` held, the `CARD → SPEC`, `STARVED → SPEC`, `STARVED → PLAN` and
`PLAN → CODE` rows still show, as `WAITS ON hold: features`, and launch nothing; with `bugs`,
`BUG → FIX` waits the same way. Reviews, corrections (`FIX → CORRECT`), adjudicate and groom
sessions and landing are never held.

## Approvals

The approval matrix maps every action class to a level:

| level | effect |
| --- | --- |
| `auto` | the factory acts |
| `groom` | refused, and the session is told to finish another way |
| `human-now` | refused, and the session is told to finish another way |

A refusal never parks the item and never asks you anything — see below.

The classes, and their defaults when `approvals:` does not name them (`asf approvals` prints the
effective matrix and what recognises each class):

| class | covers | default |
| --- | --- | --- |
| `spend_money` | buying, subscribing, raising a paid tier or spend limit | human-now |
| `touch_production` | deploying, pushing to the trunk | human-now |
| `touch_security` | secrets, credentials, runtime hook/permission settings, git hooks | human-now |
| `touch_customer_data` | customer records, exports, production databases | human-now |
| `touch_legal` | licences, notices, terms, privacy texts | human-now |
| `new_epic` | opening a new Epic | human-now |
| `merge_amendable_set` | landing a branch that touches `conventions.amendable_paths` | human-now |
| `merge_routine_pr` | landing any other finished branch | auto |
| `file_bug` | filing or bumping a Bug | auto |
| `decide_feature` | the groom deciding an undecided Feature under a live Epic, by rule | human-now |
| `decide_bug` | the groom deciding an undecided Bug under a live Epic, by rule | human-now |

Where they bind: the approvals hook (a Claude Code `PreToolUse` hook in every worker account)
refuses a factory session's tool call whose class is not `auto` — only inside a factory session,
never your own; harvest checks the two `merge_*` classes before it lands a branch; the Bug filer
checks `file_bug`. A class with no built-in recogniser (`spend_money`, `touch_customer_data`)
matches only what you add yourself; the doctor's `approvals` row names such blind classes:

```yaml
approval_signals:
  touch_customer_data:
    paths: ['data/customers/*']
    commands: ['\bpsql\b.*\bprod\b']
```

What refused means. The hook blocks the call and tells the session plainly that it may not do
it, why, and what to do instead — `pushing to the trunk and deploying are the harvest's job … push
your own branch`, `the repo's git hooks are not yours to edit …` — and that it is not a question
for a person. Nothing is auto-granted: what the matrix refuses is exactly what it refused before.

- The refusal is recorded as a **hold** on `<item>/<class>`, for the audit trail only. It does
  **not** park the item: the wave launches or relaunches it as normal, and a relaunch's brief
  opens with `REFUSED LAST RUN`, naming what was refused and why, so the session does not repeat
  it. The tick prints one line, `approvals: <n> refused action(s) on <m> item(s) recorded — none
  parks its item`.
- An item refused the same class on its first run and on **2 relaunches in a row** becomes a
  question for the groom's adjudicator (`## Refused on repeat relaunches` in the groom file,
  with `approvals.groom: auto`), not for you. The adjudicator drops it (`no: <why>`), closes it
  (`close: <why>`) or has it reshaped (`reshape: <how>`). Only money, credentials or an action
  that cannot be undone (`spend_money`, `touch_security`, `touch_customer_data`) may go to `NEEDS
  OPERATOR` — and even then the item blocks no other work.

Only the harvest's two `merge_*` classes still park an item: the branch waits to land, the tick
prints `NEEDS OPERATOR: held <class> on <item> …` and the wave says `waits <job> <item> — held
<class> (<level>)`. Holds are listed and resolved with:

```bash
asf approvals list --product <p>                             # the open holds
asf approvals resolve <item>/<class> granted --product <p>   # let the factory do it
asf approvals resolve <item>/<class> done --product <p>      # you did it by hand
asf approvals resolve <item>/<class> dropped --product <p>   # it will not happen
```

`granted` stands for that item and class from then on — the one way to widen a single item
without widening the matrix.

Two more keys under `approvals:` are switches, not classes, and both are **off unless set**:

- `groom: auto` turns on the groom's policy pass, its adjudicate sessions and the daily digest.
  Unset, the groom still types inbox cards and asks its questions, but decides nothing itself, and
  the status table's Groom row reads `— (not configured: approvals.groom)`.
- `upgrade: auto` lets the tick run `asf upgrade` itself when the trunk's package is ahead of the
  install (only for ASF's own repo as a product).

### The groom's policies

With `groom: auto`, the groom answers an undecided card by code over facts before anyone is asked.
The policies run in this order, and a card's first answer is its only one, so a close always wins
over a decide:

| policy | answers | when |
| --- | --- | --- |
| `unblock_on_closed` | unblock | the card's blocker is Closed |
| `close_exact_duplicate` | close | a same-type, same-parent card's title overlaps by `groom.duplicate_overlap` |
| `close_superseded` | close, `superseded by <id>` | a decided card names it in `links.supersedes`, or shares its `legacy_id` |
| `decide_on_approved_doc` | decide | a Feature's spec or plan is approved on the trunk (`spec-approved`, `plan-approved`, `building`) |
| `decide_or_close_ci_red` | decide, or close `green since <sha>` | a `CI red: <job>: <step>` Bug: the job's latest trunk run failed within `groom.ci_red_days` (default 7), or passed since the Bug was filed |
| `decide_recurring_bug` | decide | an auto-filed Bug seen `groom.recurring_bug_count` times |
| `decide_by_approval` | decide | `decide_feature: auto` (resp. `decide_bug: auto`) and the Feature (Bug) sits under an open, decided Epic |
| `close_on_starvation` | close | undecided longer than `stage_limits.undecided_close` |

The CI facts are the trunk runs in the record's `metrics/ci` stream. The bound holds over every
policy and over the adjudicate session: a card whose title reads as money, security, production,
customer data or legal (or an Epic, for `new_epic`) is answered by nobody but you unless that class
is `auto`, and its line reads `____ (barred: approvals.<class>)`. An auto-filed Bug is exempt from
the title recognisers — its fix is still held by the hook. `groom.policies.<name>: off` turns one
policy off. What no policy answers stays an open question, for the adjudicate session or for you.

## Rule cards

Rules live in the record's `rules/` folder as `R-nnnn` cards. Each carries a `check:` script path
(the record's own script, else ASF's core script of the same name), or `enforced: false` with a
`reason:`. The contract: exit 0 is a pass; exit 1 with one line per place is a violation.

A rule card with `removed:` or `moved_to:` is **retired**: `asf rules check` skips it and its check
no longer binds the record. On any other card, `removed:` retires it the same way: the tables and
the wave leave it out, nothing is started on it, and a session whose item is removed is ended and
its worktree reaped rather than sent back. `moved_to:` retires rule cards only; on any other card
write `removed: <reason>` — the recipe is
[clearing a card](operating.md#clearing-a-card-that-does-not-belong).

Checks run in parallel, each with a timeout: `$ASF_RULE_CHECK_TIMEOUT` seconds, default 60. A check
that times out, crashes or exits anything but 0/1 is a **check failure**, never a violation: it
prints `rule check timed out: R-nnnn` (or `failed`), files no Bug, and shows in the doctor's
`rule-checks` row. Three runs in a row and the tick prints one `NEEDS OPERATOR: rule check …` line.
The timeout is read from the environment of the process that runs the checks — the scheduled tick's
job; `asf scheduler install` does not set it, so a raised value must go into the job's environment
by hand.

`asf rules check --product <p> [--verbose] [--json]` runs them against your checkout.

## Redaction hooks

The redaction gate keeps operator names and secrets out of every commit and push. It matches:
your worker account names, a private list at `~/.ASF/redact-names.txt` (one regex per line), the
scanned repo's own `tools/forbidden-names.txt`, built-in secret shapes, and the literal values of
secret-looking environment variables. A finding is printed as a location and a pattern source,
never the matched text.

`asf hooks install` writes a `pre-commit` and a `pre-push` into the real hooks directory (`git
rev-parse --git-path hooks`, which honours `core.hooksPath`) of `repo_dir` and `backlog_dir`. The
doctor's `redaction-hooks` row accepts any hook file that contains `asf redact --pre-commit` (or
`--pre-push`) in any spelling — a quoted path to `asf`, a bare `asf` on `PATH`, or `python3 -m
asf.redact`. A hook file already there that is not ASF's is never edited; the installer stops with
`NEEDS OPERATOR: <path> is not asf's — add the line: …`.

The foreign hook does not stop the rest: `asf hooks install` still writes the approvals hook into
every worker account and every git hook it can, then reports the foreign one and exits 2. doctor's
approvals-hook row is RED while any worker account lacks the approvals hook.

When a repo sets `core.hooksPath` to a tracked directory (a fresh record's `.githooks/`, for
example), the hooks ASF writes there are ordinary files in the working tree. **Commit and push
them**: every clone and worktree — each worker's included — reads its own copy of that
directory, so an uncommitted hook exists only in your checkout. Commit them yourself: a worker
session that writes `.githooks/*` is refused as `touch_security` (a built-in path, `human-now` by
default) and told the repo's git hooks are not its to edit; it finishes without them.

To add ASF's line to a hook you already have, run it first and stop on failure — do not `exec` it,
or the rest of your hook never runs:

```sh
# .git/hooks/pre-commit (or your hooks path)
asf redact --pre-commit --product <p> || exit 1
# … your existing checks …
```

`pre-push` needs care: git passes the pushed refs on stdin, and `asf redact --pre-push` reads all
of it. A hook that needs those lines too must save them first and feed each consumer its own copy:

```sh
#!/bin/sh
# .git/hooks/pre-push
refs="$(cat)"
printf '%s\n' "$refs" | asf redact --pre-push --product <p> || exit 1
printf '%s\n' "$refs" | your-existing-pre-push "$@" || exit 1
```

By hand: `asf redact --staged`, `--unpublished [REV]` or `--tree`, in the repo to scan.
