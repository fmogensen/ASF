# Changelog

One entry per released version, newest first.

## v0.1.24 — 2026-09-26

### Features landed

- F-0028 Prompt budget: four token dimensions itemised, never summed, with a cap per job
- F-0078 The tick ends with two tables: in flight, and done since the last tick

### In progress

- F-0100 The savings pass: the tick proposes the next cheapest win from its own numbers
- F-0112 Products update themselves to a new ASF release: upgrade: auto in the tick, with rollback on a red doctor
- F-0113 An S1 silently holds all Feature work, and NEXT plans from different inputs than the tick

### Improvements and hotfixes

- fix(evidence): naming_ids' docstrings name no product branch prefix
- fix(ci_queue): escalate trunk relief to in-progress runs whose queued jobs compete for main's runners
- feat(ci): ci.reserve keeps N runners free of a PR-only label, for the trunk
- feat(record): asf reopen — the supported way to correct a falsely derived closing

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.24"`

## v0.1.23 — 2026-09-26

### Bugs fixed

- B-0123 A failed daily run is never retried until the next day

### In progress

- F-0112 Products update themselves to a new ASF release: upgrade: auto in the tick, with rollback on a red doctor

### Improvements and hotfixes

- fix(ci-queue): trunk relief acts on a required trunk job queued past the wait, whatever the PR runs' creation time
- fix(deploy): a required-jobs candidate scan considers in-progress runs too
- fix(record): a stale Backlinks section on a card the commit never touches heals itself
- fix(pool): the local lane is every account not role: cloud — worker accounts launch with the cloud lane on

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.23"`

## v0.1.22 — 2026-09-26

### In progress

- F-0116 A session commits under its own identity, never the operator's

### Improvements and hotfixes

- feat(workers): the worktree reaper — ended sessions' pushed worktrees go, with a cap
- fix(workers): the worktree reaper counts a commit lost only when its patch is on neither origin nor the trunk
- perf(tick): fold the session registry once per content; one ls-remote per health pass
- fix(upgrade): a pending sha already contained in the installed build is treated as installed
- fix(lane): a draft PR parks the branch — no merge, no review/correction/adjudicate, no rebase
- fix(workers): a run's cloud-lane marker no longer collides with the harvest lane record
- fix(cloud): claude-remote's routine body is a short pointer; the brief rides a ref
- fix(feeder): a decided S1/S2 Bug the feeder skips is a WAITS row that says why

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.22"`

## v0.1.21 — 2026-09-26

### Bugs fixed

- B-0128 An adjudicator's 'no further sessions' ruling does not stick: the lane re-adjudicates the item

### Improvements and hotfixes

- feat(cloud): cloud.default puts the cloud lane first; asf cloud doctor
- feat(cloud): runtime claude-remote — a worker session as a claude.ai routine run
- fix(capacity): demand is the product's ready rows, not the S1-cut plan

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.21"`

## v0.1.20 — 2026-09-26

### Features landed

- F-0098 'dead' sessions are nearly always finished ones not yet recorded: say 'ended, awaiting tick', free the slot at once

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push

### Improvements and hotfixes

- fix(capacity): a partner's claim is what its wave recorded — a held partner lends nothing as its sessions end
- fix(deploy): a no-candidate line names the tip's still-running CI run; tick and view pinned to agree

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.20"`

## v0.1.19 — 2026-09-26

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push

### In progress

- F-0093 Model routing is argued from minutes-per-landing, and the pool gets a cheap tier

### Improvements and hotfixes

- fix(wave): an S1 fix passes the host load hold — one at a time, never past memory pressure
- fix(upgrade): the floor drains for a pending install — bounded wait, no new harvest, a loud cap
- test(upgrade): the drain test replaces the drain's own sleep, never time.sleep
- fix(lane): a path-filtered required check is satisfied once its workflow completed beside a success

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.19"`

## v0.1.18 — 2026-09-26

### Bugs fixed

- B-0131 The operator console cannot install fixes or run the factory's own commands under auto mode

### In progress

- F-0039 Same-session correction: review findings go back to the writer's own session; a fixer only for a dead one

### Improvements and hotfixes

- fix(lane): checks read again right before MERGING; required_jobs_from falls back to the trunk
- fix(host): a load spike that has ended holds nothing — the guard needs the 1- and 15-minute loads both over

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.18"`

## v0.1.17 — 2026-09-26

### Bugs fixed

- B-0002 `asf new` cannot type the fields a card needs, so every card must be rewritten after minting
- B-0129 A PR check that also fails on the trunk is charged to the branch: corrections and adjudications burn on flaky trunk jobs

### Improvements and hotfixes

- fix(status): the Capacity row's CI queue clause is computed live, as asf ci queue computes it
- fix(hooks): a worker's commit-msg signs the commit off under commit.signoff
- fix(lane): a red DCO check is repaired by the lane — the unsigned commits signed off, trees unchanged
- fix(lane): a branch rewrite touches only the branch's own commits; no factory write reaches the trunk
- fix(harvest): a branch at the trunk tip is landed only when its item's work is on the trunk
- fix(ci-queue): a job counts once, in its runner's class; a starved PR starts on half its jobs
- fix(ci-queue): the ceiling holds batch starts only; dry-run is read from the product file
- fix(lane): a red CI check hands back its link and failing lines; adjudicate sees the hold
- fix(deploy): a skipped required job is never green; a run-level success never stands in for its jobs
- fix(lane): auto-merge needs every required check success on the head

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.17"`

## v0.1.16 — 2026-09-25

### Bugs fixed

- B-0067 A coder's branch is task/<id> but harvest scans the conventions' prefixes only: finished Task branches are never landed

### Improvements and hotfixes

- fix(hooks): a worker's commit-msg names its item — <kind>(<ID>): when the subject lacks the id
- fix(lane): a naming refusal is reworded by the lane itself — no session, no round, never adjudicate
- feat(lane): lane/size/ab_pair Feature fields, the direct branch lands without a review round
- feat(feeder): DIRECT → BUILD for a direct Feature, CARD → SPEC+PLAN for a small one
- feat(scorecard): asf scorecard --by-lane — direct vs full, pooled and per ab_pair
- feat(lane): the direct brief's pre-push tests are targeted; the full suite is the landing gate's
- fix(feeder): a Feature in a lane experiment (ab_pair) is never held by the finish-first cap
- fix(feeder): a Feature in a lane experiment (ab_pair) is not starved by the capacity cut
- feat(file-bugs): one counted Bug per flaky e2e test from the CI logs
- fix(ci-queue): trunk and S1 starts reserve nothing; one stable median estimate
- fix(deploy): the prod candidate is green on its required jobs, not the run-level conclusion
- fix(tests): the git fixture's template copy skips lock files a background maintenance run creates

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.16"`

## v0.1.15 — 2026-09-25

### Features landed

- F-0031 The approval matrix as code

### Bugs fixed

- B-0125 Approvals hook errors look like refusals: an intermittent crash refuses ordinary Bash
- B-0127 Every asf worker runs the full suite that harvest's gate already runs: 6 sessions = 6 full suites on the host

### Improvements and hotfixes

- feat(ci-queue): trunk starvation relief — cancel queued runs ahead of a starved trunk run
- fix(lane): a hosted origin's archive and delete refs go through the host API, never a push
- fix(ci-queue): the queue view says view only; DRY RUN names the dry-run mode alone
- fix(ci-queue): expected jobs are peak concurrency, trunk runs skip the ci ceiling, one in-flight count
- fix(lane): only an origin that is a hosted repo takes the API path for ref archive and delete
- fix(status): the Runners row counts busy per class from the queue's own live runner read

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.15"`

## v0.1.14 — 2026-09-25

### Features landed

- F-0078 The tick ends with two tables: in flight, and done since the last tick

### Bugs fixed

- B-0132 The record pre-commit refuses every commit on pre-existing errors in untouched files (asf set blocked)

### Improvements and hotfixes

- fix(host): the memory guard judges the kernel's memory pressure where the host reports it, not sticky swap
- fix(redact): the pre-push scan refreshes origin first and trusts the remote sha
- fix(upgrade): batch auto-upgrades — one install per upgrade.min_interval_min, urgent heads excepted
- feat(ci-pool): runner class — a class:<name> label per pool runner, counted by doctor, grouped by capacity
- fix(ci-pool): the runner class label is class-<name>
- docs(ci-pool): the docstring names the class-<name> label
- fix(status): the ci clause names the batch gate, not a breached cap
- fix(deploy): a target with no provider sha says sha unknown; asf deploy record names it
- fix(scorecard): a landed Feature whose landing commits prod runs counts as on prod
- fix(scorecard): a children-resolved Feature is on prod by its descendants' newest landing commit
- fix(deploy): a view shows the customer-content refusal, never 'the next tick dispatches'
- feat(ci-queue): one CI start queue per product, admitted by free runners per class
- fix(upgrade): the upgrade's owner keeps running its steps while the install waits for a gap

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.14"`

## v0.1.13 — 2026-09-25

### Bugs fixed

- B-0104 briefs: spec/plan templates don't state the commit-subject rule harvest enforces

### Improvements and hotfixes

- feat(release): asf release-readiness — the framework release as a computed gate
- fix(workers): every local worker session gets host-load caps — VITEST_MAX_WORKERS=2 by default
- feat(reaper): expire archive/* and retired-prefix heads on origin; doctor counts unowned heads
- fix(release): name the install script without its path — the conventions check bans the literal
- fix(upgrade): a deferred upgrade marks itself pending so other ticks stop starting and a gap comes
- feat(cloud): a cloud lane — worker sessions run as Claude Code cloud sessions, off the host
- fix(retention): hosted branch deletes go through the host API, never a push that runs product hooks
- fix(cloud): refuse runtime claude-cloud at config check — it cannot launch
- feat(cloud): runtime actions — worker sessions as CI jobs on the product's runners
- feat(amendable): a !glob entry in amendable_paths excludes a subtree from the set

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.13"`

## v0.1.12 — 2026-09-25

### Features landed

- F-0145 asf status: value-shipped metric (Features landed/day, lead times)

### Bugs fixed

- B-0092 Groom digest says '55 for you' for items the adjudicator already ruled; removals are not surfaced
- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push
- B-0120 Invariant I10 refused a write to features/F-0095.md

### Improvements and hotfixes

- feat(deploy): named deploy targets beside dev and prod — a site is tracked, flagged and deployable
- fix(wave): launch first, then the lane pass's ref pushes; each push logs its duration
- fix(publish): the factory rebases a clean worktree onto a remote that moved ahead, then pushes
- feat(ci): ASF owns the CI runner pool — declared inventory, drift doctor, safe reconcile
- fix(approvals): a hold dropped by a person is never re-asked for the same subject
- fix(widen): a done-and-pushed run's stale footprint claim is dropped, not re-asked
- feat(gate): customer-content marker gate — no internal note lands on or deploys to a customer page
- feat(review): end-user review pass — a diff touching customer pages is read as the customer
- feat(merge): conventions.merge auto|manual — the lane merges green, reviewed PRs itself
- feat(scorecard): the facts and the numbers — per landed Feature and per week
- feat(scorecard): diagnosis as code — rank where cost goes, name the causes over threshold
- feat(scorecard): the loop — weekly snapshots, one card per cause, verify after landing
- feat(scorecard): asf scorecard, the Value row in asf status, and the daily step runs the loop

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.12"`

## v0.1.11 — 2026-09-25

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push
- B-0111 Inbox intake drops headers after an unknown key; a Bug card became a Feature
- B-0114 native landing: a merged spec/plan PR marks its Feature landed/Resolved, so no coder ever launches
- B-0119 tick logs stay empty while steps run; a live tick looks the same as a hung one

### Improvements and hotfixes

- feat(deploy): deploy mode per environment — dev auto|manual|ci, prod auto|manual
- fix(set): writes: and after: are settable list fields — =, += and -= forms
- fix(check): the record pre-commit regenerates and stages the index.json a hand edit leaves stale
- fix(deploy): a view's prod line says the next tick dispatches, never that it is dispatching
- fix(lane): a hook refusal naming paths outside writes: widens the Task, no round spent
- feat(feeder): finish before you start — Task rows first, new specs capped

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.11"`

## v0.1.10 — 2026-09-25

### In progress

- B-0104 briefs: spec/plan templates don't state the commit-subject rule harvest enforces
- F-0023 The grill: a request is interrogated before it is spent on
- F-0029 Roles: one file per role, five sections, model and access from config
- F-0035 Thin controller: every read loop moves to the tick, and a rule flags scriptable chores
- F-0099 Groom every tick: intake, policy pass and questions run on each tick, not once a day
- F-0100 The savings pass: the tick proposes the next cheapest win from its own numbers

### Improvements and hotfixes

- fix(harvest): a merged PR cancels its CI runs still going; a pending remote run shows its age
- fix(lane): an orphan lane branch is closed or picked back up, never left open
- fix(lane): ref-only pushes leave from a clean trunk checkout; a refused one is loud and retried
- fix(upgrade): reinstall the pin at the trunk head, so auto-upgrade actually upgrades
- fix(deploy): the tick dispatches a green trunk to prod when deploy_sha.auto is on; otherwise prod says what it waits on
- fix(version): a git install of a sha names its release from the describe its build stamped
- fix(upgrade): the defer rule matches tick processes, not shells that mention a tick
- fix(pool): a row waiting on seats at cap says so, never NEEDS OPERATOR
- fix(upgrade): name the install script without its path, so check_conventions passes
- fix(hooks): the pre-commit no longer runs the full suite on every worker commit

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.10"`

## v0.1.9 — 2026-09-25

### Features landed

- F-0076 Session identity: every asf worker is distinguishable from every other, and from sessions that are not asf's
- F-0090 Adjudication is the last resort: the cheap causes are ruled out before Opus is spent

### Bugs fixed

- B-0055 CI red on main since 8c25fa7: the hermetic step exports ASF_PRODUCT into a suite whose home has no such product
- B-0114 native landing: a merged spec/plan PR marks its Feature landed/Resolved, so no coder ever launches

### In progress

- F-0007 P1 — the 0.1 specification
- F-0047 The factory's self-improvement loop: measure, diagnose, act, verify, revert, reflect
- F-0061 Hooks as rule enforcement inside every worker session
- F-0092 An item has a budget: three sessions or $10, then it stops and says why
- F-0097 asf status says nothing about Bugs and Features: add a Bugs row and a Features row

### Improvements and hotfixes

- fix(harvest): a red trunk holds PR landing, never blames the PR
- feat(approvals): an external-CI product's full suite is refused in the hook
- fix(briefs): a coder on an external-CI product leaves the Gate to remote CI
- fix(quota): the wave places launches by 5h headroom; a session limit is quota-exhausted, not a failure

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.9"`

## v0.1.8 — 2026-09-25

### Features landed

- F-0090 Adjudication is the last resort: the cheap causes are ruled out before Opus is spent

### Bugs fixed

- B-0001 `asf init` does not exist, so a new record has to be laid down by hand
- B-0087 The console operator is never told what a tick did: no per-tick digest, no asf watch
- B-0090 The operator's console rules live in assistant memory, not code: the plugin ships no hooks enforcing them
- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push
- B-0099 A removed or moved rule card still runs its check and files S2 'rule violated' Bugs
- B-0101 file-bugs mints Bugs with an empty Acceptance and no statement of what is wrong
- B-0109 Full test suites run locally in parallel and exhaust the host (load 99, swap 95%)
- B-0110 Harvest re-gates a green code branch when main moved only by docs commits
- B-0115 scheduler install runs the daily clock at load, hours outside its declared time

### In progress

- B-0097 A network blip on push scores finished work as 'failed: not pushed' and costs a correction round
- F-0001 P0a — the two repositories and the licence
- F-0029 Roles: one file per role, five sections, model and access from config
- F-0091 asf improve: the self-improvement pass is a tick step, not something someone remembers to ask for
- F-0093 Model routing is argued from minutes-per-landing, and the pool gets a cheap tier
- F-0100 The savings pass: the tick proposes the next cheapest win from its own numbers
- F-0123 record: a date-prefixed plan file mints no Tasks after it lands
- F-0125 Onboarding: adopt a product's in-flight PRs (asf adopt-pr + legacy review form) so native landing drains them

### Improvements and hotfixes

- contracts(fix-package): config keys and stub APIs for the lane, review, invariants, occupancy
- contracts(fix-package): isolate_home replaces home: inherit; record stage/validate stub; rollback note
- docs(fix-package): the package plan, review overrides and compliance checklist
- fix(workers,scheduler): isolated worker env and HOME; the clock ticks from a snapshot
- fix(workers,doctor): an isolated session gets its credentials from auth_env files, never HOME or the keychain
- test(e2e): the PR-lane harness — in-process ticks over a fake PR host, both landing modes
- fix(run_tests): expected failures are green, unexpected successes are red
- fix(record): every writer staged and validated; ingest merges the machine block (R14, I1-I3, I10, I11)
- fix(env): a flow-style map or list is parsed, or refused with its key and line
- fix(groom): an explicit type: line decides the intake type (I13)
- fix(conventions): a map-valued convention in another shape is its default plus a doctor finding
- fix(doctor): a map-valued convention in another shape fails loud, with its key and line
- fix(briefs): the session model by kind and item class, in code — S2/S3 and Task reviews run light
- fix(invariants): the registry completed; three soft check points in the tick (R9-R13)
- fix(doctor): one brief of every kind rendered as a config smoke test
- fix(groom): For you holds only human-now approval actions
- fix(status): the Prod row reads deploy_sha.workflow, its old names as aliases
- fix(metrics): release notes list as landed only Resolved or Closed items (I12)
- tools(package_gate): the review checklist verified by code (§11)
- docs(fix-package): the lane stream's R-lines map to its landed tests
- fix(invariants): I3 judges only a written writes:, and the lane adopts only a card's branch
- tools(package_gate): R21 is pending-live until a recorded smoke; R15 and R18 checked by git
- feat(auth_env): product-level GitHub tokens (conventions.auth_env)
- feat(tick): asf tick --dry-run — the rollout's read-only rehearsal
- task(R21): the live smoke — isolated session, auth_env, one push, ALL PASS
- hotfix(dry-run): the state copy leaves out the worker worktrees and never follows a symlink
- hotfix(doctor): one-factory knows the factory's own source repo when asf runs from its install
- hotfix(workers): an isolated session carries the factory's ASF_HOME
- fix(smoke): the live smoke runs the approvals hook in the session's own environment
- fix(tests): the suite removes the temp homes and fixture copies it makes
- … and 14 more

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.8"`

## v0.1.7 — 2026-09-24

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push

### In progress

- F-0024 Self-amendment policy: the factory proposes, humans approve and merge
- F-0091 asf improve: the self-improvement pass is a tick step, not something someone remembers to ask for
- F-0097 asf status says nothing about Bugs and Features: add a Bugs row and a Features row
- F-0100 The savings pass: the tick proposes the next cheapest win from its own numbers

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.7"`

## v0.1.6 — 2026-09-24

### Features landed

- F-0093 Model routing is argued from minutes-per-landing, and the pool gets a cheap tier

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push

### Improvements and hotfixes

- hotfix(tick): hold new launches under host pressure; briefs keep full suites for CI

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.6"`

## v0.1.5 — 2026-09-24

### Features landed

- F-0090 Adjudication is the last resort: the cheap causes are ruled out before Opus is spent

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.5"`

## v0.1.4 — 2026-09-24

### Features landed

- F-0090 Adjudication is the last resort: the cheap causes are ruled out before Opus is spent

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.4"`

## v0.1.3 — 2026-09-24

### Features landed

- F-0100 The savings pass: the tick proposes the next cheapest win from its own numbers

### Improvements and hotfixes

- hotfix(metrics,status): trunk releases every release_min_interval, and status shows the version
- hotfix(feeder): an open code PR gets its review from the branch, not the ledger; feeder.hold
- hotfix(check): a residue (no closing rule sees the item) warns, it never refuses a commit

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.3"`

## v0.1.2 — 2026-09-24

### Features landed

- F-0002 P0b — the card-evaluation pass for multi-product wording
- F-0024 Self-amendment policy: the factory proposes, humans approve and merge
- F-0025 Typed envelopes: every job ends with a machine-readable report beside its prose
- F-0026 The writes boundary holds a branch; it never silently reverts
- F-0028 Prompt budget: four token dimensions itemised, never summed, with a cap per job
- F-0029 Roles: one file per role, five sections, model and access from config
- F-0030 The README carries the argument, the mental model and the manual on one page
- F-0033 Dogfood end to end in CI
- F-0035 Thin controller: every read loop moves to the tick, and a rule flags scriptable chores
- F-0038 A factory-only CI class: a push touching only factory code runs lint and the factory tests
- F-0039 Same-session correction: review findings go back to the writer's own session; a fixer only for a dead one
- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0041 Small-Task trains: size class from the footprint, one train per lane, one CI run and one review per train
- F-0043 CI waste targets on the scorecard, and a week over target files a Bug
- F-0044 The retro's first line: features on production per week, and cost per feature
- F-0046 The conflicts pass: supersession enforced, same-subject rule and decision pairs into the groom
- F-0053 Reviews return checks: every review ends in a machine-readable check table the tick reads
- F-0078 The tick ends with two tables: in flight, and done since the last tick
- F-0080 Closing is derived and total: a definition of done per type, reconciliation for work that predates it, nothing re-emitted
- F-0085 The groom answers itself (D-0049): rules as code, an adjudicate session for the rest, a digest to the operator
- F-0090 Adjudication is the last resort: the cheap causes are ruled out before Opus is spent
- F-0091 asf improve: the self-improvement pass is a tick step, not something someone remembers to ask for
- F-0093 Model routing is argued from minutes-per-landing, and the pool gets a cheap tier
- F-0095 The wave relaunches coders on Tasks that end 'empty branch: nothing to land'
- F-0096 The wave runs dry: 73 undecided cards and nothing moves them toward a spec
- F-0097 asf status says nothing about Bugs and Features: add a Bugs row and a Features row
- F-0099 Groom every tick: intake, policy pass and questions run on each tick, not once a day
- F-0100 The savings pass: the tick proposes the next cheapest win from its own numbers
- F-0101 Corrections of mechanical failures run on Opus: 25% of all spend goes to correct/adjudicate/groom sessions
- F-0102 Deliveries: one plan, one agent, one gate for several small Features and Bug fixes across the backlog
- F-0103 The tick files Bugs from its own session outcomes and a stalled wave

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push

### Improvements and hotfixes

- hotfix(install): one generic installer from a pinned git ref; skills run the pinned asf-live
- hotfix(install): the command is plain asf; ASF_SUFFIX=-live only beside a dev install
- hotfix(install): drop the -live name — one asf per machine, the pinned release
- hotfix(release): semantic versions from the roadmap, with release notes
- hotfix(rules): a rule card carrying removed: or moved_to: is retired — its check no longer runs
- hotfix(tick): a command-only clock runs its commands outside the product lock
- hotfix(install): steps 3-4 never abort the install; --version and the doctor stamp name asf's commit
- fix(harvest): a branch red alone on a green trunk is its own red, not foreign; keep the red output
- hotfix(workers): a session that wrote its REPORT is not failed by a CLI error text its prose quotes
- capacity: bound each product's session ceiling by its fair share of the usable pool
- fix(file-bugs): a rule check that times out is a check failure, not a violation
- workers: a dead or ended run holds no seat; a removed or done item's run is never held
- hotfix(rules): a rule check may run 60 s by default, not 10
- hotfix(approvals): reading core.hooksPath is not touching security; setting or unsetting it is
- hotfix(harvest): a docs-only spec/plan branch in the PR lane is merged by harvest itself
- hotfix(schema): the drain reads the registry's own fold, not a parser of its own
- hotfix(feeder): pushed spec/plan work is not starved; a finished run is not dead
- hotfix(harvest): harvest lands every PR-lane PR itself, no product merge-queue glue
- hotfix(briefs): every brief states the commit-subject rule harvest enforces
- docs(guide): the operator's guide — getting started, the product file, the daily loop, upgrading, troubleshooting, connectors (planned)
- docs(guide): corrections from the first customer review
- hotfix(hooks,doctor): a foreign git hook never skips the approvals hook; a live pre-ASF job turns doctor red
- docs(guide): the hooks install no longer skips the approvals hook on a foreign git hook
- hotfix(check): a decision id inside fenced code is quoted code, not a bare reference
- hotfix(feeder): a Task with no writes: waits instead of launching a coder that can only block
- hotfix(record): a merged spec/plan is spec/plan-approved, never the Feature's landing
- hotfix(record): a removed or moved Feature gets no derived stage and no Tasks from its plan
- hotfix(harvest): green alone, red together lands one at a time, never held whole
- hotfix(version): asf --version names the release it runs, not the static base version
- hotfix(capacity): capacity.weight splits the pool by the operator's priority
- … and 14 more

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.2"`

## v0.1.1 — 2026-09-24

### Features landed

- F-0002 P0b — the card-evaluation pass for multi-product wording
- F-0013 P3 wave 5 — the feeder and the tick
- F-0014 P3 wave 6 — the CLI and the plugin
- F-0022 The preamble: every brief opens with facts the runner already knows
- F-0023 The grill: a request is interrogated before it is spent on
- F-0024 Self-amendment policy: the factory proposes, humans approve and merge
- F-0025 Typed envelopes: every job ends with a machine-readable report beside its prose
- F-0028 Prompt budget: four token dimensions itemised, never summed, with a cap per job
- F-0031 The approval matrix as code
- F-0039 Same-session correction: review findings go back to the writer's own session; a fixer only for a dead one
- F-0073 Everything enters through the inbox; the type is derived from the card's shape
- F-0080 Closing is derived and total: a definition of done per type, reconciliation for work that predates it, nothing re-emitted
- F-0082 Quota: a cooldown band before the stop — one new job per account from 90 %, none from 95 %
- F-0086 The groom proposes merges, splits and batches of Stories — shape decides delivery speed
- F-0090 Adjudication is the last resort: the cheap causes are ruled out before Opus is spent
- F-0091 asf improve: the self-improvement pass is a tick step, not something someone remembers to ask for
- F-0095 The wave relaunches coders on Tasks that end 'empty branch: nothing to land'
- F-0098 'dead' sessions are nearly always finished ones not yet recorded: say 'ended, awaiting tick', free the slot at once
- F-0099 Groom every tick: intake, policy pass and questions run on each tick, not once a day
- F-0101 Corrections of mechanical failures run on Opus: 25% of all spend goes to correct/adjudicate/groom sessions

### Bugs fixed

- B-0025 A session that ended with nothing committed leaves a worktree that blocks every relaunch
- B-0069 Killing a session's process does not stop the run: the client dies, the work keeps pushing
- B-0080 after: holds the coder row only — correction and adjudicate launch anyway, on Opus
- B-0082 A timed-out gate is counted as a failed round and sends the item to Opus
- B-0083 A failed record step does not stop the tick: the wave acts on a stale board
- B-0084 A typed field is written by hand, so an unparseable card is only caught a tick later
- B-0086 The factory runs an installed copy of itself and never notices the trunk has moved
- B-0089 A new S1/S2 Bug waits up to 24h for the daily groom before BUG → FIX can take it
- B-0091 asf new / asf inbox never commit or push: a card filed from the console never reaches the tick
- B-0095 The PROD view crashes on a product whose `customer_paths` is the documented list

### Improvements and hotfixes

- fix(asf): a groom session can write its answers, and is judged on its result, not a branch
- fix(asf): the tick applies an adjudicator's answers when it first sees them
- fix(asf): an older day's groom answers never land on top of a newer day's
- fix(asf): an inbox card intake cannot type is put to the adjudicator, not left for a hand edit
- fix(asf): the groom's suppression sees the answers it just applied
- hotfix(groom): intake and the policy pass run every tick, edited cards are read again, and new questions get a same-day adjudicator
- hotfix(order): a plan's stated order reaches its Task cards as after:
- hotfix(tick): the gate never blocks the wave, output is line-buffered, steps are timed, sessions are detached
- hotfix(record): backlinks from one token scan per item, and each record part timed
- hotfix(record): evidence reads ancestry from one rev-list, not a merge-base fork per question
- hotfix(harvest): bisect on the red modules only, confirm in full, skip a trunk red
- hotfix(harvest): a docs-only branch is never held for a red test, and the diff is the footprint when writes are absent
- hotfix(tests): test_cutover reaches each script state once per class, and gate 3 never sleeps

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.1"`

## v0.1.0 — 2026-09-23

### Features landed

- F-0002 P0b — the card-evaluation pass for multi-product wording
- F-0009 P3 wave 1 — the record
- F-0011 P3 wave 3 — metrics
- F-0012 P3 wave 4 — rules
- F-0031 The approval matrix as code
- F-0071 S1 lane: critical factory bugs pre-empt Feature work — tiers, reserved capacity, no ladder, a clock
- F-0073 Everything enters through the inbox; the type is derived from the card's shape
- F-0074 Generic by construction: no product convention in code, proven by a second product in CI
- F-0075 Redaction is a gate on every write path: operator names and secrets never reach a record or a public repo
- F-0076 Session identity: every asf worker is distinguishable from every other, and from sessions that are not asf's
- F-0078 The tick ends with two tables: in flight, and done since the last tick
- F-0079 Capacity is configuration: session workers and CI, per product and per operator
- F-0082 Quota: a cooldown band before the stop — one new job per account from 90 %, none from 95 %
- F-0083 Scheduler clocks are configuration: each job group on its own interval, per product
- F-0085 The groom answers itself (D-0049): rules as code, an adjudicate session for the rest, a digest to the operator
- F-0086 The groom proposes merges, splits and batches of Stories — shape decides delivery speed
- F-0087 Failure-class invariants: one property test per class of the first 53 Bugs, and the whole-loop dogfood tick in CI
- F-0089 No run ends unlanded: the end-of-run check happens inside the session, not in the next one

### Bugs fixed

- B-0001 `asf init` does not exist, so a new record has to be laid down by hand
- B-0002 `asf new` cannot type the fields a card needs, so every card must be rewritten after minting
- B-0003 The operator's product file for this product does not match the schema the config reader reads
- B-0004 Nothing carries a schema version, although the ruling requires it from 0.1
- B-0005 `check` does not validate the record's layout, only its items
- B-0006 There is no command that shows the record: no status, roadmap, backlog, doctor or tick
- B-0007 Minting in a worktree needs an id range the tool gives no way to allocate
- B-0008 A tick that staged everything in a shared checkout committed in-progress worker content
- B-0009 A hand harvest reaped a worktree before its fast-forward had succeeded
- B-0010 A harvest reaped a worktree whose job was still running
- B-0011 Tests run under the pre-commit hook inherit the git environment and operate on the real repository
- B-0012 A fixture test reads the job's id range from the environment and fails in any ranged worktree
- B-0013 The live tick writes into the operator checkout and never commits or pushes
- B-0015 `asf new bug` mints no severity, signature or found_in, and `check` does not miss them
- B-0019 Health reaps a live job whose branch has no commits yet (reads "no commits" as "merged")
- B-0020 Evidence is blind to any product but the first: branch prefixes hardcoded, id tokens in commits ignored
- B-0021 Core rules have no check scripts in the ASF repo (142 unenforced)
- B-0023 Launch events write operator account names into the (public) record
- B-0024 An unmapped model label launches a session that dies at once, and the result reads as success
- B-0025 A session that ended with nothing committed leaves a worktree that blocks every relaunch
- B-0026 Tier ordering by id lets already-attempted Bugs starve newer ones; no attempt limit
- B-0027 Nothing lands a lane branch on a product without a merge queue: harvest reads the wrong repo, prefix and key, and is not a step
- B-0028 A corrected or late-finishing session stays 'dead pid'; harvest never lands its branch
- B-0029 prs step offers landed branches to gh and fails every tick; it ignores the landing convention
- B-0030 A record push refused because origin moved is discarded; the tick's appended events are lost
- B-0031 Harvest gates every eligible branch in one tick; a tick runs 25 minutes and CI races the landings — cap the branches per tick (interim)
- B-0032 A red harvest gate or a rebase conflict files a Bug or holds silently — it must go back to the branch's session with the failing output (D-0048, part a)
- B-0033 The harvest gate inherits the tick's ASF_PRODUCT; ASF's own suite reads the live product and goes red
- B-0036 A landing never reaches the checkout the scheduler runs; the factory keeps running the code from before its own fix
- B-0038 CI main red: harvest fixture depends on the host's init.defaultBranch
- B-0039 Corrections resume the same runtime session (--resume) instead of relaunching cold (D-0048, part b)
- B-0040 Harvest gates once per tick on the combined head, bisecting on red (replaces the interim cap)
- B-0041 A relaunched job inherits the previous run's ended/failed fields; health skips it, the feeder re-emits it, harvest never lands it
- B-0042 The scheduler's checkout is fast-forwarded only after a lane landing; a fix pushed any other way leaves the factory on old code
- B-0043 The test suite reads the operator's live ~/.ASF; the harvest gate goes red on any operator config change
- B-0044 A record rebase conflicts on machine-owned content when a hand commit touched the same card; the tick's push is still refused
- B-0045 A refused product file crashes every command and the tick with a traceback instead of one NEEDS OPERATOR line
- B-0046 A correction row cannot spawn: spawn creates a new branch, but the held branch already exists
- B-0047 asf plugin check resolves the plugin directory from the package, not the checkout; CI is red on a plain install
- B-0048 An adjudicate row cannot spawn on a held branch, and rounds climb past the cap — held branches loop forever
- B-0049 Health never reaps a landed branch's worktree: harvest rebased the tip and deleted the remote branch, so 'pushed' fails forever
- B-0050 Record commands use the cwd as the record: /asf:groom from a product repo grooms nothing and writes groom/ into the product repo
- B-0051 A session that ends 'finished' without pushing blocks its item forever: health says finished, harvest sees nothing, spawn refuses the worktree
- B-0052 Sessions background the test suite and return before it ends — no commit, no push; the brief must forbid it
- B-0053 CI runs on every worker-branch push: red notifications for in-progress work and wasted minutes; the gate is harvest's
- B-0054 Adjudicate sessions commit invented rulings to the code repo, never close the row, and every push re-fires the stale branch workflow
- B-0055 CI red on main since 8c25fa7: the hermetic step exports ASF_PRODUCT into a suite whose home has no such product
- B-0056 A session whose branch is already on origin merges its stale remote: nothing in the factory publishes a rebased lane branch, and no session may force
- B-0057 A lane branch whose content is already on the trunk, or whose item is closed, is held for ever: harvest counts commits, not content
- B-0058 A blocked Bug still gets its FIX row and a blocked item its CORRECT or ADJUDICATE row: the feeder reads blocked for Features only
- B-0059 A spec landing commit resolves the Feature it names, and a spec on main reads spec-draft: the evidence does not know the lane's own conventions
- B-0060 A landed plan never becomes Task cards: the Feature sits at plan-approved with nothing for the feeder to launch
- B-0061 A branch whose run ended other than finished is never landed by content nor archived: harvest reads the run's verdict before the content
- B-0062 A session that died twice asks the operator every tick instead of being held: the one dead end in the lane that is not a correction
- B-0063 Stale local lane branches pile up in the scheduler's checkout: health deletes a branch only with its worktree
- B-0064 The adjudicate brief still asks the session to mint a Decision id and commit a ruling file; the ruling has no home a session can reach
- B-0065 A card the groom removed keeps its branch on origin for ever: harvest reads only live cards, so superseded never fires
- B-0066 Archiving a superseded branch fires its stale workflow: the archive push re-runs the pre-B-0053 tests.yml and mails a red run
- B-0067 A coder's branch is task/<id> but harvest scans the conventions' prefixes only: finished Task branches are never landed
- B-0068 The suite runs serially: 324 s per gate on ten idle cores — shard the test modules across processes
- B-0071 test_cutover builds an installation per test: 172 s of a 318 s suite in one module — build each module's fixture once
- B-0072 The harvest gate has no timeout: one branch whose test recurses through the pre-commit hook hangs the tick, and every tick after it
- B-0073 A test that commits triggers the pre-commit hook, which runs the suite, which commits: the gate recurses and the lane stops
- B-0074 A landed Task stays Active: its plan's 'branch exists' line outranks the commit that landed it
- B-0075 Workers still background the test suite and end without pushing; prose cannot stop it
- B-0076 A finished run with an empty branch blocks every task that waits on it — for ever
- B-0077 A product with no deploy can never close a Feature: Closed demands a prod sha that does not exist
- B-0078 A Feature that lands by a commit stops at Resolved: ingest drops the state it just derived
- B-0079 A branch ahead of the trunk whose run failed is orphaned: harvest never looks at it again
- B-0081 The redaction scanner reads GIT_CONFIG_KEY_0 as a secret and flags ordinary text
- B-0085 A tick step waits on a model session, so health and harvest stop for its whole duration
- B-0086 The factory runs an installed copy of itself and never notices the trunk has moved

### Improvements and hotfixes

- ASF — Autonomous Software Factory: license, README, docs layout
- asf: package skeleton + env.py (operator config reader)
- asf.record: frontmatter, match, and the item database (new/check/index/ingest)
- asf.evidence + asf.rules: git/gh derived state and the rule-check runner
- asf.metrics: streams, rollup, releases, cost
- asf.harvest: land worker branches, PR hygiene
- asf.tick + asf.groom + asf.cli: migrate, stale, file-bugs, groom, and the CLI
- tools/check_generic.sh: fail the build on a forbidden name
- docs: config.example.yaml and products.example.yaml
- fix check_generic.sh false positives: platform names, self-referential test
- docs(research): prior art — the 2026-08 prototype (five parts) + ADR 0001
- asf(cutover): doctor, tools/cutover.sh + rollback.sh — the P4/P4b cutover kit
- asf(shadow): tick --shadow, shadow-diff, and the six asf views
- cutover: --ref for the shadow-diff gate; test quoted YAML keys
- docs(research): jev decision model — a cheap judge between code and sessions
- asf(tick): stateless clone; deterministic bug ids
- asf(tick): ensure_clone + push — a clone that commits as the factory identity and pushes
- asf(new): bug takes --severity/--signature/--found-in; check flags a bug without severity
- asf(plugin): the /asf:* skills over the asf CLI, plus tools/asf wrapper
- asf(cli): wire tick --steps/--manifest/--daily and new bug --severity; step owners are asf | command | off (no legacy vocabulary); docs: the constitution
- asf(scheduler): a job definition that can actually run
- asf(doctor): a scheduler section — is the factory running, not just installed
- asf(cutover): three gates, a split clock, and a rollback that reaches the jobs
- asf(package): pyproject (asf-factory, stdlib only, version 0.1.0 from asf/__init__), CI installs with pipx and runs the suite
- asf(feeder): footprint — the writes: overlap gate, glob-aware
- asf(feeder): rows and tiers — BUG → FIX S1 lane, stalemate gate, footprint waits
- asf(feeder): render, incident clock, asf next register(); golden tests
- asf(workers): runtime (claude_code + fake) and quota source/guards
- asf(workers): pool pick rule, S1 reserve, spawn and launch wave
- asf(workers): health reap rule, stall check, correct_once hook
- … and 39 more

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.0"`
