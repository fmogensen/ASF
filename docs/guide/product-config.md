# The product file

`~/.ASF/products/<product>.yaml`, key by key, for the keys that change what the factory does. The
full annotated shape is [`docs/products.example.yaml`](../products.example.yaml); an unknown
top-level key is refused on load (`NEEDS OPERATOR: … is not a field of the product file`). Shared,
machine-wide settings live in `~/.ASF/config.yaml` ([`docs/config.example.yaml`](../config.example.yaml)).

The file is YAML, but read by a small built-in reader: nested block maps, `- item` lists, inline
`[a, b]` lists, scalars and `#` comments. **No inline maps**: `{a: 1}` is read as the string
`"{a: 1}"`, not a map — always write a map as an indented block. No anchors, no multi-line
strings.

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
| `review_pattern` | `{reviews_dir}/{n}-{slug}.md` | a review file's name; `{n}` is the round |
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
    `verdict: approved` line. A **spec or plan branch that only changes files under `specs_dir`,
    `plans_dir` and `reviews_dir` needs no review**: merging it is what approves its document;
  - its checks are green (no checks at all counts as green).

  Then `gh pr merge --squash --delete-branch` (then `--merge`, then `--rebase` if the repo refuses
  one), or `--auto` when the trunk has a GitHub merge queue. Hotfix and S1 branches go first;
  `capacity.batch.per_run` caps the merges per harvest and `capacity.batch.parallel` the PRs in the
  merge queue at once. No review, or pending checks: `waiting <branch>: …` until the next tick. Red
  checks: the branch is held and sent back to its session. A PR someone else merged — a `batch`
  step, a person — is found merged and its session closed. A product with no PR host only gets the
  `pr-lane <branch>` mark and needs a person or its own `batch` step to merge.

Unset, `landing` is derived from `steps.batch`: `off` or unset means `fast-forward`; a command
means `pull-request`. Set `landing: pull-request` to get PRs without a `batch` step.

### What approves a spec or a plan

A Feature moves from spec to plan to code on evidence, never on a status someone typed. A spec (or
plan) counts as approved when either:

- the document is on `origin/<main>` — landing it is its approval (in fast-forward mode harvest's
  gate is the review); or
- the newest review file for it — in `reviews_dir` on the branch that carries the document, matched
  by `review_pattern` — carries the verdict `APPROVED` (the first of `APPROVED`, `CHANGES
  REQUESTED`, `BOUNCE`, `REVISE` in its text).

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
running anything (`tick: step batch has no owner — declare it under steps …`). Declare it, even as
`off`.

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
(n/m)`. Declaring `batch` as a command also switches the default landing to `pull-request`.

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
ASF's and on a clock, each gets `ceil(usable / products)`. A product over its share keeps its
running sessions — it just gets no new slot: `waits <job> <item> — fair share: n of u usable
slots across k products`.

The CI ceiling is the smaller of the product's `ci` (or `per_product.ci`) and `total.ci`; with
neither set there is none and no `gh` call is made.

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

S1 reserve: while an S1 Bug is open, each lane keeps `reserve_for_s1` slots for `BUG → FIX` rows.

`asf capacity --product <p>` (or `--all`, `/asf:capacity`) prints the resolved numbers and which
term bounds each.

## Approvals

The approval matrix maps every action class to a level:

| level | effect |
| --- | --- |
| `auto` | the factory acts |
| `groom` | refused; the item waits for the next groom |
| `human-now` | refused; every tick prints a `NEEDS OPERATOR: held …` line until a person resolves it |

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

A refused action becomes a **hold** on `<item>/<class>`, and the item is parked — the wave will not
start it (`waits <job> <item> — held <class> (<level>)`). To release it:

```bash
asf approvals list --product <p>                             # the open holds
asf approvals resolve <item>/<class> granted --product <p>   # let the factory do it
asf approvals resolve <item>/<class> done --product <p>      # you did it by hand
asf approvals resolve <item>/<class> dropped --product <p>   # it will not happen
```

`granted` stands for that item and class from then on.

Two more keys under `approvals:` are switches, not classes, and both are **off unless set**:

- `groom: auto` turns on the groom's policy pass, its adjudicate sessions and the daily digest.
  Unset, the groom still types inbox cards and asks its questions, but decides nothing itself, and
  the status table's Groom row reads `— (not configured: approvals.groom)`.
- `upgrade: auto` lets the tick run `asf upgrade` itself when the trunk's package is ahead of the
  install (only for ASF's own repo as a product).

## Rule cards

Rules live in the record's `rules/` folder as `R-nnnn` cards. Each carries a `check:` script path
(the record's own script, else ASF's core script of the same name), or `enforced: false` with a
`reason:`. The contract: exit 0 is a pass; exit 1 with one line per place is a violation.

A rule card with `removed:` or `moved_to:` is **retired**: `asf rules check` skips it and its check
no longer binds the record. On any other card, `removed:` retires it the same way: the tables and
the wave leave it out, nothing is started on it, and a session whose item is removed is ended and
its worktree reaped rather than sent back. (`moved_to:` retires rule cards only.)

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
