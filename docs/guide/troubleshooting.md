# Troubleshooting

A line that needs you always starts `NEEDS OPERATOR:` and names what to do. Grep a tick log for it:
`grep 'NEEDS OPERATOR' ~/.ASF/logs/tick-<product>-*.log`. Below is every family the code prints,
what it means and the one command that clears it; then the common stalls that print no such line.

## `NEEDS OPERATOR` lines

### The installer

| line | meaning | fix |
| --- | --- | --- |
| `install: NEEDS OPERATOR: pipx is not installed …` | step 1 cannot run | `brew install pipx` (or `python3 -m pip install --user pipx`) |
| `install: NEEDS OPERATOR: git is not installed` | step 1 cannot run | install git |
| `install: NEEDS OPERATOR: cannot read main's head from <url>` | no ref given and the repo is unreachable | pass a tag or sha, or check `ASF_REPO_URL` and the network |
| `install: NEEDS OPERATOR: asf is not on PATH after pipx install …` | pipx's bin dir is not on `PATH` | `pipx ensurepath`, open a new shell |
| `install: NEEDS OPERATOR: ~/.ASF/config.yaml is missing …` | no operator config | `cp docs/config.example.yaml ~/.ASF/config.yaml` and fill it in |
| `install: NEEDS OPERATOR: ~/.ASF/products/<p>.yaml is missing …` | no product file | `cp docs/products.example.yaml ~/.ASF/products/<p>.yaml`, fill it in |
| `install: NEEDS OPERATOR: N step(s) failed …` | a `FAILED step N` line above it | run that step's command to see why, fix it, rerun the installer |

### Configuration

| line | meaning | fix |
| --- | --- | --- |
| `NEEDS OPERATOR: <problem> — edit the file or run asf init --product <p>` | a config file does not load: an unknown key (`… is not a field of the product file`), a value of the wrong shape, no product file, no product named anywhere, an unknown approval class or level | edit the file at the line named; `asf doctor --product <p>` confirms |
| `NEEDS OPERATOR: no backlog dir for the record — asf init …` | `asf init` has no record directory | `asf init --product <p> --backlog <dir>` |
| `NEEDS OPERATOR: products/<p>.yaml declares no clocks …` | nothing would run the product | add a `clocks:` block ([product-config.md](product-config.md#clocks)), then `asf scheduler install --product <p>` |
| `NEEDS OPERATOR: step <s> is on no clock …` (doctor) | an ASF step no clock runs | add it to a clock's `steps:`, or declare it `off` under `steps:` |
| `NEEDS OPERATOR: products/<p>.yaml has no clock that ticks record …` (cutover) | `tools/cutover.sh` found no `record` clock | add one, then rerun the cutover |

### The scheduler

| line | meaning | fix |
| --- | --- | --- |
| `NEEDS OPERATOR: install the cron line above — crontab -e` | `scheduler.kind: cron`: ASF prints, you install | paste the printed lines with `crontab -e` |
| `NEEDS OPERATOR: clock <c>: every … cannot be expressed as a cron schedule …` | the interval does not map onto cron | pick an interval cron can express (a divisor of an hour, or of a day in hours), or install the printed command yourself |
| `NEEDS OPERATOR: scheduler kind '<k>' has no adapter …` | only `launchd` and `cron` have adapters | set `scheduler.kind`, or install the printed command on your own scheduler |

### Hooks and the redaction gate

| line | meaning | fix |
| --- | --- | --- |
| `NEEDS OPERATOR: asf is not on PATH — pipx install asf-factory` | `asf hooks install` cannot resolve `asf` | rerun the installer (it puts `asf` on `PATH`) |
| `NEEDS OPERATOR: <dir> is not a git repo — asf hooks install cannot place its hooks there` | `repo_dir` or `backlog_dir` is wrong | fix the path, then `asf hooks install --product <p>` |
| `NEEDS OPERATOR: <hook> is not asf's — add the line: "<asf>" redact --pre-commit\|--pre-push --product <p>` | a hook ASF did not write is in the way. Until it is fixed, `asf hooks install` also skips the worker accounts' approvals hook (a known issue) | add the line to that hook — see [below](#the-redaction-hooks) — then rerun `asf hooks install --product <p>` |
| `NEEDS OPERATOR: product <p> has no repo_dir …` | a rule card declares a Claude Code hook but there is no product repo to put it in | set `repo_dir` |

### Approvals

| line | meaning | fix |
| --- | --- | --- |
| `NEEDS OPERATOR: held <class> on <item> — <detail> — asf approvals resolve <item>/<class> granted\|done\|dropped` | a `human-now` action was refused; the item is parked; printed every tick until resolved | `asf approvals resolve <item>/<class> granted` (or `done`, `dropped`) |
| `NEEDS OPERATOR: <item> <class> — asf approvals resolve …` | the same, in a session's own output | the same |
| `[NEEDS OPERATOR: ]held file_bug on <signature> — widen approvals: file_bug …` | Bug filing is not `auto`, so found Bugs are only listed | set `approvals.file_bug: auto`, or file them yourself |

### Workers and the pool

| line | meaning | fix |
| --- | --- | --- |
| `NEEDS OPERATOR: no account under quota — wait for a window to reset, or add an account …` | every account is at or above its `stop` band, or its quota is unreadable | `asf workers quota --product <p>` shows which; wait, add an account, or fix `worker_pool.quota_command` |
| `NEEDS OPERATOR: worker_pool.models has no entry for <label> — add it to config.yaml` | a job's model label (`heavy` or `light`) is not mapped | add a `models:` block under `worker_pool:` with `heavy: <id>` and `light: <id>` |
| `NEEDS OPERATOR: <what> — <command or answer>` in a session's report | a worker session hit something only a person can do (a credential, a decision) and carried on with the rest | do what it names; for a reshape, answer the split line in the groom |

### The record

| line | meaning | fix |
| --- | --- | --- |
| `NEEDS OPERATOR: run asf schema-migrate — <detail>` (exit 3) | the record's schema and the package's disagree; writes are refused | `asf schema-migrate --product <p> --drain`; if the record is newer, reinstall the newer `asf` |
| `NEEDS OPERATOR: rule check <timed out\|failed>: R-nnnn — 3 runs in a row …` | a rule's check script is slow or broken — a factory problem, not a product Bug | fix the script, or raise `ASF_RULE_CHECK_TIMEOUT` in the tick job's environment ([rule cards](product-config.md#rule-cards)) |
| `NEEDS OPERATOR: <item> — <why>; approvals.<key> is not auto.` (groom digest) | a groom policy would have decided it, but its class is held | answer it in the groom (`/asf:groom`) |
| `NEEDS OPERATOR: <item> — <why>` (groom digest) | the adjudicate sessions for today are spent and the question is still open | answer it in the groom (`/asf:groom`) |

## The redaction hooks

**A freshly laid-down record reports its own pre-commit as foreign.** `asf init` gives a new record
a `.githooks/pre-commit` that runs `asf check`, and points `core.hooksPath` at it. That file is not
ASF's redaction hook, so `asf init` prints `git hooks NOT installed — NEEDS OPERATOR: …/.githooks/
pre-commit is not asf's` and the doctor's `redaction-hooks` row is red. Add the printed line to the
top of that hook (before the `asf check` part, ending in `|| exit 1`), then `asf hooks install
--product <p>` writes the missing `pre-push`. Commit the hook.

**An existing `pre-push` stops receiving refs.** `asf redact --pre-push` reads the whole of stdin.
Save stdin once and pipe a copy to each consumer — the example is in
[product-config.md](product-config.md#redaction-hooks).

**A push is refused with `redact: refused — N finding(s)`.** Each finding names a file and line, or a
commit message, and which list matched. Remove the name or secret and amend; if the match is a
false positive from your private list, fix `~/.ASF/redact-names.txt`.

## Common stalls

Nothing is red, yet nothing moves. These print a line, not a `NEEDS OPERATOR`.

**The tick is skipped.** `tick: another tick of <p> is running — skipped`: another clock holds the
product lock. Normal for a moment; if every run says it, a tick is stuck. `ps` for `asf.cli tick
--product <p>`, read that clock's log for the step it is in, and stop that process by its pid if it
is hung. A command-only clock prints `… holds the record — <what> skipped` instead; `tick: step <s>
is already running — skipped` means the previous run of that command step has not finished.

**Harvest looks idle.** `harvest: gate running (pid n, since …)`: the gate is still going in the
background; each tick waits for it. Its log is `~/.ASF/logs/harvest-<p>.log`. A gate past
`conventions.harvest.gate_timeout_s` (default 600 s) is killed and counted red.

**One job at a time.** `waits <job> <item> — quota cooldown — one job at a time`: every account with
room is in its cooldown band (default from 90 % of a usage window), so each takes a job only while
it runs none. It clears when the window resets. `asf workers quota --product <p>` shows the windows.

**A fair-share hold.** `waits <job> <item> — fair share: n of u usable slots across k products`: this
product already has its share of the shared pool. Its running sessions continue; new slots come as
they end or as other products go quiet. To change the split, lower another product's
`capacity.sessions` or add pool accounts; `asf capacity --all` shows each product's bound.

**Other waits in the wave.** `pool full` (every account at its `cap`), `reserved for S1` (a slot kept
for an S1 Bug), `held <class> (<level>)` (an approval hold — resolve it), `WAITS ON <item>` (an
`after:` predecessor or a running Task writing the same files), `waits batch — at ci capacity (n/m)`
(the merge queue waits for CI runs to finish).

**A plan waits to land in pull-request mode.** The Feature sits at `plan-draft` or `plan-review`,
NEXT shows `PUSHED → LAND` / `WAITS ON landing`, and no Task is minted. In pull-request landing a
plan is approved only when it is on `main` or its newest review file says `APPROVED`. Harvest
merges a docs-only plan branch itself once its PR's checks are green; look for its line in
`~/.ASF/logs/harvest-<p>.log`:

- `waiting <branch>: no open PR yet` — the `prs` step has not opened it; check `gh auth status`
  and `repo_slug`.
- `waiting <branch>: PR #n checks pending` — CI is still running on the PR.
- `waiting <branch>: PR #n not approved — no ASF review yet` (or `… reads <verdict>`) — the branch
  changes files outside `specs_dir`, `plans_dir` and `reviews_dir`, so it is merged like code and
  needs a review with `verdict: approved`. Keep a plan branch to documents only.
- `waiting <branch>: PR #n green — n merged this tick (capacity.batch.per_run)` — the merge budget
  for this harvest is spent; the next one merges it.
- `queued <branch>: PR #n in the merge queue` — GitHub's merge queue has it.
- `held <branch>: PR #n merge refused — …` — branch protection refuses the merge; merge it by hand
  or allow the method.
- `pr-lane <branch>` — the product has no PR host, so nothing merges it but you or your `batch`
  step.

**The tables do not change.** They read your record checkout. `git -C <backlog_dir> pull`.

**A step has no owner.** `tick: step <s> has no owner — declare it under steps in products/<p>.yaml`
and exit 2: typically `batch`. Add `batch: off` (or its command) under `steps:`.

**A branch is held with `commits do not name <ITEM>`.** Every commit subject on a lane branch must
name its item — `plan(F-0042): …`; the id in the branch name does not count. The session is sent
back to reword them; see [holds](operating.md#holds-and-back-to-its-session).

**Answers in the groom have no effect.** They were not committed and pushed, or they were written
into a day's file after the next day's daily already ran — see
[the groom](operating.md#the-groom-and-the-inbox).

## Retiring a pre-ASF scheduler

If the product was run by earlier scripts on their own scheduler, those jobs keep running until
the product's owner retires them — ASF never removes a job it did not install, and the doctor's
`scheduler` row stays `ok` while one runs.

Find them first — ASF only knows the jobs you name:

```bash
launchctl list | grep -v com.apple   # loaded launchd jobs; look for the old factory's labels
crontab -l                          # cron lines
asf scheduler list                  # ASF's own jobs plus scheduler.legacy_labels, with the
                                    # paths each job's arguments point at
```

Then declare them in `config.yaml` so the doctor can see them:

```yaml
scheduler:
  launchd_label: <the old job's label>    # the one job to retire
  legacy_labels: [<label or glob>, ...]   # listed in the doctor's SCHEDULER section
legacy_paths: [<dir of the old scripts>, ...]
```

What the doctor then reports:

- `scheduler  ok  pre-ASF job <label> still loaded — retire it with tools/cutover.sh`, until it is
  gone, then `pre-ASF job <label> retired`.
- Under `== SCHEDULER`, a line per old job, and `YELLOW <dir> still in use by <label>` for each
  `legacy_paths` directory a loaded job still points at.
- `one-factory RED` when a tool named like one in `legacy_paths` is on `PATH` or inside the product
  repo — two factories running the same product.

To retire them: `tools/cutover.sh <product> [--ref DIR] [--force] [--dry-run|--apply]`. It is a dry
run by default (the gate table and every step it would take); `--apply` boots out the job named by
`scheduler.launchd_label`, installs ASF's clocks, moves the `legacy_paths` directories aside and
records everything in `~/.ASF/state/<product>/retired/<date>/manifest.tsv`. It runs from an ASF
checkout (it is not part of the pipx install). Its gates:

| gate | refuses when | exit |
| --- | --- | --- |
| clocks | the product's `clocks:` do not render | 3 |
| 1 referenced dirs | a `legacy_paths` directory is still used by a loaded job other than the one being retired | 3 |
| 2 manifest complete | `asf tick --product <p> --manifest` has a step with no owner | 3 |
| (a) doctor and shadow-diff | `asf doctor` or `asf shadow-diff --ref DIR` is not clean (without `--ref` there is nothing to compare). `--force` overrides this gate only | 1 |
| 3 the job runs | the installed record job does not complete a run with exit 0 and a `tick: state` commit (or a no-change line) — the run is rolled back | 4 |

`--force` never bypasses gates 1–3. A product already cut over exits 0 and changes nothing.
`tools/rollback.sh <product> --apply` undoes a cutover from its manifest. Or retire the old jobs by
hand (`launchctl bootout gui/$(id -u)/<label>`, `crontab -e`) and rerun `asf doctor`.
