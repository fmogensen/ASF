# Changelog

One entry per released version, newest first.

## v0.1.177 — 2026-10-06

### Bugs fixed

- Fewer red PR runs. Workers run the product's pre-push check and every touched test module before their one push, cloud sessions do the same, the merge queue stops re-batching a culprit after a deterministic red, and a test the trunk itself fails is read as trunk red instead of sending every PR to a correct round (#834)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.177"`

## v0.1.176 — 2026-10-06

### Features landed

- `asf release-readiness --gate preview` prints the five-point preview gate (and `release.gate: preview | 1.0`, default 1.0, picks the gate in the product file); `bash tools/quickstart.sh` now ends with a first landed Task on the stub runtime; the release workflow can cut a `v<x.y.z>-<label>` pre-release once the preview gate is green (#832)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.176"`

## v0.1.175 — 2026-10-06

### Bugs fixed

- Release readiness, versioning and the stale/gate policies read only the product's own config — no repository is special, and a minimal product (one account, no cloud, no queue, hosted CI, no quota reader, no prices) passes doctor, release-readiness and a full tick with n/a for what does not apply (#823)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.175"`

## v0.1.174 — 2026-10-06

### Bugs fixed

- PR runs stop going red for reasons the PR didn't cause. The release-notes check runs the trunk's tooling, a PR head is reopened for a fresh merge ref at most once, and a red that reproduced on two merge refs is never blindly re-run (#830)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.174"`

## v0.1.173 — 2026-10-06

### Features landed

- `asf upgrade --wait-s` now really ends within its bound and never blocks launches, and `asf retire <item> --why "…"` retires a hand-landed card or inbox note without hand-editing the record (#822)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.173"`

## v0.1.172 — 2026-10-06

### Features landed

- `asf status` has a `CI reds` row (reds 24h by class: real · flaky · ours, plus replay / tooling / check when non-zero), `asf scorecard` shows the red PR runs per class with a 7-day trend plus run-window CI targets (first pass ≥ 0.9 over 50 PRs, 0 runner-class reds in 100 runs, last 20 trunk runs green), and release-readiness gains criterion 12 "PR CI healthy"; criteria 3/4/6/10 no longer misread cancelled runs, release commits or missing data (#820)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.172"`

## v0.1.171 — 2026-10-06

### Features landed

- The scorecard names the cause (#818)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.171"`

## v0.1.170 — 2026-10-06

### Features landed

- The merge queue no longer re-runs a deterministic (registers / pre-cut) red and no longer drops a stacked chain on a re-run the host refused; `asf correct` targets the item's open lane; globs in one folder with different literal tails no longer collide; an auth error on launch (local or cloud) takes the account out of the pool at once with one ALARM (#824)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.170"`

## v0.1.169 — 2026-10-06

### Features landed

- The doctor's SCHEDULER row reads the start line — one bounded reader, one field, one YELLOW (#809)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.169"`

## v0.1.168 — 2026-10-06

### Features landed

- The rail — one variable, one refusal at the one resolution git owns (#804)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.168"`

## v0.1.167 — 2026-10-06

### Features landed

- The hook derives the index it cannot find (#816)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.167"`

## v0.1.166 — 2026-10-06

### Features landed

- One grace, one predicate, one index — and the stop reading them instead of its own arithmetic (#815)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.166"`

## v0.1.165 — 2026-10-06

### Features landed

- One retirement rule — a moved card is retired to the index reader too (#810)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.165"`

## v0.1.164 — 2026-10-06

### Features landed

- The wave raises one line per held Epic, on a busy tick too, and records it (#811)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.164"`

## v0.1.163 — 2026-10-06

### Features landed

- What shipped, and what it cost over its whole life (#803)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.163"`

## v0.1.162 — 2026-10-06

### Features landed

- The check names the card that holds the name, and no index entry copies it (#792)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.162"`

## v0.1.161 — 2026-10-06

### Features landed

- Every cancelled job carries why it was cancelled, written at import (#808)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.161"`

## v0.1.160 — 2026-10-06

### Features landed

- An S1 card left on a question says so, every tick, naming its file (#807)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.160"`

## v0.1.159 — 2026-10-06

### Features landed

- An absent `steps.batch` is off, both refusals go, and the five pinned expectations are corrected (#806)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.159"`

## v0.1.158 — 2026-10-06

### Features landed

- The brief names the push-free gate, and says a refused push ends no turn (#796)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.158"`

## v0.1.157 — 2026-10-06

### Features landed

- The gate's third route, and the four skips that speak (#802)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.157"`

## v0.1.156 — 2026-10-06

### Features landed

- An `S1:` title is a severity, and a body error line is the signature the card was asked for (#797)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.156"`

## v0.1.155 — 2026-10-06

### Features landed

- The row and the `NEEDS OPERATOR` line name the globs being dragged to the console (#795)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.155"`

## v0.1.154 — 2026-10-06

### Features landed

- The pass reaches its own watermark, buys nothing twice, says what it did — and one function parses the facts (#794)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.154"`

## v0.1.153 — 2026-10-06

### Features landed

- The help names the forms, and the guide stops being wrong about what `asf set` does (#791)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.153"`

## v0.1.152 — 2026-10-06

### Features landed

- `asf doctor` has a `connectors` row naming the active implementation of each kind (forge, ci, runtime, scheduler, quota, secrets) and where it was chosen, RED when a configured one cannot be found; docs/guide/writing-a-connector.md explains how to write one (#779)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.152"`

## v0.1.151 — 2026-10-06

### Features landed

- The row whose seat went, and the hand launch that claims before it spawns (#784)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.151"`

## v0.1.150 — 2026-10-06

### Features landed

- The clocks are installed through the `scheduler` connector, and Linux gets one: `connectors.scheduler: systemd` (or `scheduler.kind: systemd`) writes systemd user timers; launchd stays the default with no change in behaviour (#778)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.150"`

## v0.1.149 — 2026-10-06

### Features landed

- `git config` is judged by git's own grammar — every read form passes, every set form of the hooks path holds (#790)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.149"`

## v0.1.148 — 2026-10-06

### Bugs fixed

- Round F — non-amendable corrections launch, drain-first never blocks an in-flight batch, 0-min CI timeouts are no timeout, one CI box list with a doctor heartbeat row (#785)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.148"`

## v0.1.147 — 2026-10-06

### Features landed

- Worker sessions now start through the `runtime` connector — `connectors.runtime` in config.yaml (`worker_pool.backend` still works); the Claude Code CLI stays the default, local and cloud, with no change in behaviour (#777)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.147"`

## v0.1.146 — 2026-10-06

### Features landed

- The class written on the ledger, and the key the card verifies itself on left exactly where it is (#789)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.146"`

## v0.1.145 — 2026-10-06

### Features landed

- CI reads and actions (run lists, reruns, run reads, the CI start queue and runner pool) now go through the `ci` connector — `connectors.ci` in config.yaml; GitHub Actions stays the default with no change in behaviour (#776)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.145"`

## v0.1.144 — 2026-10-06

### Features landed

- A tick now launches sessions within seconds of starting — the wave runs right after the record's fast parts, and health, the groom and the record's bookkeeping run after it; `asf status` shows the wave latency and the watchdog alarms when it passes 2 min (#782)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.144"`

## v0.1.143 — 2026-10-06

### Features landed

- ASF now has connectors — `connectors.<kind>` in config.yaml picks the implementation for each external service (forge, quota and secrets in this release step), with GitHub, no quota command and plain files as the unchanged defaults (#770)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.143"`

## v0.1.142 — 2026-10-06

### Features landed

- A tick no longer blocks on the lane. The pre-push check on a branch the lane rebuilt runs as a background job that a later pass reads, one lane pass stops at a time budget, and the merge queue lands green batches from its own one-minute job instead of waiting for the next tick (#786)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.142"`

## v0.1.141 — 2026-10-06

### Bugs fixed

- The README now has Quick start, Configuration and Upgrade sections; a wedged account-manager lock is configured under `account_lock.*` (off unless `account_lock.path` is set) and the rollup's extra `gh` PATH under `operator.path_prefix` (#772)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.141"`

## v0.1.140 — 2026-10-06

### Features landed

- `asf scorecard` now shows eight throughput metrics (seat use, first-pass CI, false closes, time-to-detect, cloud outcomes, merge wait, runner use, quota burn) with a 7-day trend and alarms, and `asf release-readiness` gains "floor clean" and "seats used" and stops counting a Task's planned first review as repair (#774)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.140"`

## v0.1.139 — 2026-10-06

### Bugs fixed

- A test run inside a worker session can no longer rewrite a worker account's live settings.json, even when it runs an old branch's tests (#783)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.139"`

## v0.1.138 — 2026-10-06

### Features landed

- ASF now acts on stale leftovers instead of only flagging them — a PR the lane holds STALE past 3 days is archived as `archive/pr-<n>` and closed, a Task stuck far behind the trunk is re-planned from the trunk, and removed or closed items no longer show rows in `asf next`. Every action is logged to `stale-acts.jsonl`. On ASF's own repo it acts by default; any other product only prints dry-run lines until you set `conventions.stale.act: true`. Run it by hand with `asf stale --act [--dry-run]` (#781)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.138"`

## v0.1.137 — 2026-10-06

### Bugs fixed

- Round E — moved rulings, loud review filing, race-safe record commit, shared-path aging, origin-checked waits, on-trunk heads, opt-in factory-only merge (#780)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.137"`

## v0.1.136 — 2026-10-06

### Features landed

- A worker session that stops making progress is detected in about 10 minutes, not after the 240-minute cloud timeout, and is continued from its branch head and notes in the same tick (#775)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.136"`

## v0.1.135 — 2026-10-06

### Features landed

- A launch never re-pushes a branch whose CI run is in flight (#773)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.135"`

## v0.1.134 — 2026-10-06

### Features landed

- The snapshot pool is bounded by count, not only by age (#567)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.134"`

## v0.1.133 — 2026-10-06

### Features landed

- ASF can now tune itself — per session kind it tries a cheaper model or a different seat count within your bounds, keeps what measurably helps, reverts what hurts, and shows every change in `asf tune history` and `asf status` (off until you set `tune.enabled`) (#771)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.133"`

## v0.1.132 — 2026-10-06

### Bugs fixed

- Pre-cut check, row blame, leftover-run reap, priority runs, cut-short checks, tip-on-main evidence, groom no on a delivery lead (#768)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.132"`

## v0.1.131 — 2026-10-06

### Features landed

- Every ASF update now has a version number (this one is 0.1.131) with readable release notes in CHANGELOG.md, and ASF shows that number wherever it says what a product runs (#767)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.131"`

## v0.1.130 — 2026-10-06

### Bugs fixed

- A failed or rate-limited check on a cloud session no longer marks it as lost; ASF waits and looks again (#747)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.130"`

## v0.1.129 — 2026-10-06

### Features landed

- An approval class can now be recognised by the areas of the product it touches, and the doctor stops calling such a class blind (#496)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.129"`

## v0.1.128 — 2026-10-06

### Features landed

- The same automatic bug is filed at most once per day, and a source can ask for a slower pace (#494)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.128"`

## v0.1.127 — 2026-10-06

### Features landed

- The tick log now shows when each step starts as well as when it ends, and who ran it (#491)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.127"`

## v0.1.126 — 2026-10-06

### Features landed

- A new asf review-checks command checks a review's table before a person or session reads it, and refuses an incomplete one (#487)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.126"`

## v0.1.125 — 2026-10-06

### Features landed

- A rule that has been replaced by a newer one no longer runs, and no longer shows up as a gap (#484)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.125"`

## v0.1.124 — 2026-10-06

### Bugs fixed

- Several stalls that left worker seats idle are gone: approved work with nothing to correct now lands, and replans are no longer blocked (#764)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.124"`

## v0.1.123 — 2026-10-06

### Bugs fixed

- Cancelled CI runs are now actually stopped, first runs always get a verdict, and the chain alarm is honest (#765)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.123"`

## v0.1.122 — 2026-10-06

### Bugs fixed

- A correction round is now briefed on the branch's real latest state, and the new asf answer command replies to a question without starting a round (#763)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.122"`

## v0.1.121 — 2026-10-05

### Bugs fixed

- A product move now drains its queued batches first, then holds, and every exit restarts whatever it paused (#762)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.121"`

## v0.1.120 — 2026-10-05

### Bugs fixed

- Deferred Stories are no longer re-decided, list fields in cards parse correctly, and asf set can change a parent (#761)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.120"`

## v0.1.119 — 2026-10-05

### Bugs fixed

- A live merge batch now survives everything except a real verdict on its own code (#760)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.119"`

## v0.1.118 — 2026-10-05

### Bugs fixed

- A ruling at the round cap is now one code session, closing counts every Task beneath an item, and a lead's file scope covers its whole delivery (#759)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.118"`

## v0.1.117 — 2026-10-04

### Bugs fixed

- A cloud relaunch no longer mistakes the previous run's result for its own (#758)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.117"`

## v0.1.116 — 2026-10-04

### Bugs fixed

- A parent and its children can no longer close each other by proving one another (#757)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.116"`

## v0.1.115 — 2026-10-04

### Features landed

- Item ids are now handed out through one shared push, so local and cloud sessions never collide (#756)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.115"`

## v0.1.114 — 2026-10-04

### Bugs fixed

- The stop gate no longer counts empty commits or an untouched review ruling as unpushed work (#755)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.114"`

## v0.1.113 — 2026-10-04

### Bugs fixed

- A cancelled required check on a merge batch is no longer a verdict; it is re-run once, then the batch is cut again (#754)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.113"`

## v0.1.112 — 2026-10-04

### Bugs fixed

- A pushed branch now gets its PR in the same pass, even while the CI queue is holding (#753)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.112"`

## v0.1.111 — 2026-10-04

### Features landed

- A ticked acceptance line is now proven when it says "proven by" a file that exists (#752)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.111"`

## v0.1.110 — 2026-10-04

### Features landed

- A Story now needs every acceptance line proved before it counts as done, and Features count their Stories (#750)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.110"`

## v0.1.109 — 2026-10-04

### Features landed

- A new watchdog tracks how long items sit in a state, and the status Ready count uses the same filter as the wave (#751)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.109"`

## v0.1.108 — 2026-10-04

### Bugs fixed

- Green merge chains now land first, priority only orders the next cut, and one tested tree lands once (#749)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.108"`

## v0.1.107 — 2026-10-04

### Features landed

- A reconciled item now closes through the ordinary closing rules (#723)
- Trunk-red, stale-ref, flaky-test and tick checks now read GitHub through one shared client (#734)
- The verdict now looks at what the working tree holds before asking what the remote has (#424)
- Picking a worker from the pool is now race-safe and sees every product's claims (#402)
- A log that ends in a result is no longer mistaken for a dead session (#732)

### Bugs fixed

- An older cooldown setting name is still honoured with a warning, and more config keys are registered (#746)
- The session registry is cheaper to read, and a tick that runs too long now names its step and exits (#748)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.107"`

## v0.1.106 — 2026-10-04

### Features landed

- The groomer no longer treats a shared path as proof that two cards overlap (#392)
- Every card is now written through the safe writer, and bug filing collapses id lists (#739)
- New cloud modes (overflow, local, primary, off) with a visible fallback, plus automatic session capacity (#745)
- CI flight, doctor, upgrade, security alerts, capacity and cloud actions now share one GitHub client (#735)
- Doctor now warns about a config setting that no code reads (#741)

### Bugs fixed

- The plan-Task heading reader accepts more heading shapes, and a plan that yields zero Tasks is flagged (#744)
- A spec and plan sharing one job name no longer swap lane records every tick (#742)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.106"`

## v0.1.105 — 2026-10-04

### Features landed

- The measure page now counts a relaunch's cost per session (#740)
- asf set can now change a local-only switch and take several ids in one commit (#743)
- A Task that only changes documents no longer gets a review row (#726)
- Copies, merges, naming and hook refusals are now settled by code; reviews raised only by those are skipped (#729)

### Bugs fixed

- A PR's first CI run is never cut by the factory, and every PR it opens gets a run (#738)
- Hook-refused publishes are classified once and the worktree is restored after a refused publish (#737)
- A commit that only covers an item can no longer close it over the item's own PR (#736)
- Pushed work is never silent: an open Task or Bug with an open PR always gets a NEXT row (#733)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.105"`

## v0.1.104 — 2026-10-04

### Features landed

- The landing fact now runs in shadow beside the workers' deciders, with a replay command (#730)
- A card is now parsed once per version of its text, which speeds up the tick (#493)
- A mechanical pass ahead of a review now counts as a repair (#445)
- A ruling that says ready now publishes a branch the session could not push (#389)
- Groom rules now remove duplicate findings and report coverage (#728)
- Cards are now written atomically, so a reader never sees a half-written card (#727)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.104"`

## v0.1.103 — 2026-10-04

### Features landed

- A models map in a real product file now reaches model routing in every documented shape (#725)
- A verdict that changed now reaches the live event stream (#481)
- New asf ruleset commands install, show and break-glass the trunk's required checks (#703)
- Every push now passes a branch guard keyed on the repository (#710)
- The wave's launch path now reads shared repository facts instead of its own copy (#485)
- asf check now fails a supersession that dangles, loops or is written only once (#483)

### Bugs fixed

- Child processes given their own home no longer inherit the caller's config, and tests guard the operator's files (#724)
- A run with local commits is never parked as having nothing to land, and a declared transplant is published (#721)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.103"`

## v0.1.102 — 2026-10-04

### Features landed

- The planner is now told what is held before it books work (#449)

### Bugs fixed

- An unverified landing with unknown coverage now waits instead of being reset (#722)
- An operator ruling filed since the last review now lifts the "card unchanged" park (#720)
- New check that a close needs a landing fact, in report-only mode and off by default (#717)
- A footprint wait never names a closed owner, and a finished card in play holds no footprint (#713)
- A move now drains before shutting down, tests every clock's command, and keeps the host clock on the pin (#716)
- End-to-end tests use a fixed host reading and a quieter, more reliable git template (#719)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.102"`

## v0.1.101 — 2026-10-04

### Features landed

- Landings are now stamped on the card, with a migrate command and a reopen that clears the stamp (#709)
- Git reads now return a clear result, so an unknown answer never closes anything (#706)
- Merge-queue land requests and rebuilds now go through the shared state store (#712)
- An item only the console can move no longer orders anything in the feeder (#708)
- A new facts layer reports known or unknown values and logs disagreements with the old logic (#704)
- Cardless PR reviews, reshapes and replans now run on the light model, and doctor warns when cheap falls back (#689)
- Dependency roots are now decided by code (#699)
- New asf reset command voids a wrong landing claim, with an undo (#684)
- The scorecard now reports program metrics in its JSON output and check mode (#680)
- Not-pushed work is now published by code, behind a switch that is off by default (#700)
- New asf scheduler commands install, pause, resume and remove the host clock for the network probe (#701)
- A single GitHub client now serves the public GitHub calls (#690)
- A log-only network probe now records which layer fails each minute, with a doctor row (#695)
- Specs and plans are now started just in time, metered against the build cap (#687)
- Mechanical causes are now settled by code before a correction, behind a switch that is off by default (#692)
- Reviews can now end in a structured verdict block, and the stop gate refuses a review without one (#696)
- New state store: atomic, versioned, with per-file locks, a registry, a reaper and corrupt-file quarantine (#686)
- Recorded GitHub behaviours now back the contract tests (#691)
- Flaky timing tests now read one fixed clock (#685)
- One reachability probe per tick; an offline upgrade is skipped rather than failed (#679)
- Doctor now checks a pinned install by what really runs, and a product check command was added (#677)
- Each product now gets its own venv per version, with an upgrade command that switches safely and can roll back offline (#674)
- The asf command now runs the pinned venv of whichever product it acts on (#673)
- More scenario tests now cover close paths and refusals (#711)
- Scenario tests now cover every close path against every host behaviour (#693)

### Bugs fixed

- A typed landing now closes only by reconciliation, and a reset void holds against the ingest (#718)
- The A/B dry run no longer copies caches, refuses under 10 GB free, and cleans up when killed (#714)
- One new switch is now known, and a test memo no longer leaks between trunk-red tests (#715)
- A document lane no longer lands the Task (#705)
- A merge lock held by a live process is never treated as stale (#707)
- An unknown answer never closes, and the hook smoke test now runs from the repo (#688)
- Merges now take a host-wide lock, and a waiting PR no longer rebases or runs CI (#702)
- A product test's push into a scratch repo no longer triggers the session's push guard (#698)
- A run cancelled for relief is now re-run instead of dropped (#694)
- An attested trunk run now counts a required job its path filter skipped (#697)
- A repeating spawn failure now says so, and the retire guard compares content after archiving (#683)
- Bug filing now makes one Bug per cause, not per path (#682)
- A review of a pushed branch no longer waits on a dependency (#681)
- A pinned clock keeps its tools, every agent home keeps its asf, and a move smoke-tests before resuming (#678)
- A pinned product's clocks now run from its own venv (#676)
- An unknown product-file key is now a warning, not a refusal (#675)
- Put-aside work now says so, and the build cap counts only live work (#670)
- A closed item that no rule closes any more now reopens as new (#672)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.101"`

## v0.1.100 — 2026-10-03

### Bugs fixed

- A closed parent no longer closes unbuilt work, and a packed writes entry reads the same before and after a set (#667)
- A red on a stale merge ref now gets a fresh run, and a red trunk is confirmed by a full run and owned (#669)
- Approved content held only by git mechanics is now moved onto the trunk without a session (#668)
- A stale open run no longer blocks asf correct (#664)

### In progress

- Planned: replan cut to the session's own identity and the check (#671)
- Planned: replan cut to the lane-prefix guard (#666)
- Planned: replan cut to the writer and the reader (#665)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.100"`

## v0.1.99 — 2026-10-03

### In progress

- Planned: replan trimmed one work set to two Tasks, one already landed (#663)
- Planned: replan trimmed the retirement-rule work to its first Task (#660)
- Planned: replan rewrote one Task whole and dropped its guide paragraph (#662)
- Planned: replan kept one combined Task and dropped the action cell and guides (#661)
- Planned: replan rewrote one Task onto the memo and dropped the lease half (#659)
- Planned: replan kept the optional step and dropped the doctor guard and guide (#658)
- Planned: replan re-cut one card over the check and the index entry (#657)
- Planned: replan kept one Task on unmoved anchors and dropped the guide half (#656)
- Planned: three Tasks for a PR deadline with send-back, reaping and a churn cap (#655)
- Planned: replan cut to the switch and the verdict that uses it (#654)
- Planned: replan rewrote one Task to the pass alone (#653)
- Planned: replan cut to the store and what fills it (#652)
- Planned: replan cut to the pin and dropped the example product file (#651)
- Planned: replan kept the reader and doctor row and dropped the upgrade half (#650)
- Planned: replan cut to the collecting pass (#649)
- Planned: replan cut to three Tasks and dropped the product threshold (#648)
- Planned: replan merged two Tasks into one and dropped the drain window and two views (#647)
- Planned: replan cut to the reader and the launch (#646)
- Planned: replan re-cut one Task onto the landed gate and dropped another (#645)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.99"`

## v0.1.98 — 2026-10-03

### In progress

- Planned: replan cut to Task 1, with one Task rewritten and one landed Task kept (#644)
- Planned: replan cut to Task 1 and dropped the reshape brief's second mode (#643)
- Planned: replan dropped a Task whose work had already landed (#642)
- Planned: once opened, a PR has no deadline; bound every open lane state and reap what cannot land (#641)
- Planned: replan kept the default and the S1 bypass as one Task (#640)
- Planned: replan cut backfill to what the repository can say (#639)
- Planned: replan cut to the read-only half and dropped the conversation Task (#638)
- Planned: replan cut to measurement and dropped the admission gate (#637)
- Planned: replan re-cut four open Tasks to discovery and measurement (#636)
- Planned: replan cut to the seam and dropped two Tasks (#635)
- Planned: replan cut to the groom's own commit and the gate a new product gets (#634)
- Planned: replan cut to one connector kind end to end, split into six Tasks (#633)
- Planned: replan cut to the two expression forms the workflow uses (#632)
- Planned: replan cut to the one feeder row (#631)
- Planned: replan re-cut to the daily prune (#630)
- Planned: replan cut to the classification alone (#629)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.98"`

## v0.1.97 — 2026-10-03

### Features landed

- New merge-pr.sh script is the one way to merge an agent PR (#618)
- A live install checkout off main now shows red in doctor, warns each tick, and refuses upgrade without an explicit ref (#619)
- asf correct now takes a reason and an optional PR number to ask for one correction round with instructions (#617)

### Bugs fixed

- Parity work is never hidden any more: over-limit items, unverified landings and console-only members now show (#626)
- A conflict hold worded as another batch's, when the branch conflicts with the trunk, now gets a rebuild (#627)
- A trunk conflict behind a batch now goes to the lane rebuild, and an untriaged landing-gate hold is gated again (#625)
- The pre-push check now tests the branch merged with trunk, and asf correct at the cap files an operator ruling (#624)
- An asf land verdict now judges only the PR's exact current head, and runner loss counts as infrastructure (#623)
- asf land now admits a finished head whose CI never started a required job (#622)
- Infrastructure reds are re-run, never corrected; a priority batch gets runners; asf land PRs are never factory work (#621)
- asf land now admits a head whose own CI skipped a required job by path (#620)
- A red batch's culprit is now named from the failing job's log and sent back alone while the rest are cut again (#616)
- The install end-to-end test no longer rewrites settings, and test git repos never auto-clean (#615)

### In progress

- Planned: replan cut to putting the 32 steps on a clock (#628)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.97"`

## v0.1.96 — 2026-10-02

### Features landed

- asf land can now go first in line, and a red-check correction brief names the step, tests and local reproduction (#612)
- A trunk version marked as attested now reads its skipped required jobs as green (#611)
- A queue status now appears on the landed commit, and doctor reads the trunk rules (#608)
- Main now has an attestation step: a green batch is marked before the trunk moves (#607)
- CI batches are now admitted by free heavy capacity, and relief cancels only on a saturated class (#606)
- One door to main: asf land, a trunk watch and a runner-class doctor check (#604)
- A red required job is now re-run once before any correction round, to tell flakes from defects (#603)
- The pre-push check now depends on the brief kind, and a cancelled check is never red (#602)

### Bugs fixed

- A rewritten Task now keeps the file paths the factory widened it onto (#614)
- A conflicting land request is red at once, and conflicting members never share a batch (#613)
- A stale or stuck batch is rebuilt instead of waited out, and a trunk stall alarm was added (#610)
- Each module now attests once (#609)
- The stop gate is now wired in, a released stop counts as an unpushed defect, and the lane writes the trailers (#605)
- A question needing input with no question is no longer asked, and shared writes are append-only (#601)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.96"`

## v0.1.95 — 2026-10-01

### Bugs fixed

- Adjudicator rulings now bind later reviews, and one head takes at most two correction rounds (#600)
- A session's report commit no longer lands its item (#599)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.95"`

## v0.1.94 — 2026-09-30

### Features landed

- A started CI run is now counted once, and the page explains what a bracketed key means (#598)
- The CI demand estimate now names the labels, and the queue holds the run (#597)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.94"`

## v0.1.93 — 2026-09-30

### Features landed

- A card's acceptance now runs through the operator's own groom command (#594)
- A runner that cannot take a job is no longer counted as free for it (#596)
- The operator's page now describes the digest that actually exists (#595)
- The loop-guard status row now prints the guard's own clock and threshold, and is silent where it never fires (#585)

### In progress

- Planned: three Tasks so the supply learns what a runner can take and the ledger counts a started run once (#591)
- Planned: the Stale row reads the measured cadence (#590)
- Planned: a publish counts as progress, and every hold passes the guard the trunk (#589)
- Planned: four branches on the record and a WAITS row that names what holds an item (#587)
- Planned: the row tells a clean exit from a death and counts from the exit (#588)
- Planned: three Tasks for the retirement rule, the card's acceptance and the page (#586)
- Planned: three Tasks so cancels name their cause and the readers print it (#592)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.93"`

## v0.1.92 — 2026-09-30

### Bugs fixed

- One GitHub query per tick now fetches the checks for every open PR's head (#576)

### In progress

- Planned: a refused landing shows as a send-back row naming its own cause (#584)
- Planned: the lane counts what a branch carried, and the DONE table credits only that (#583)
- Planned: a runner counts as free only if it can take the jobs, and a started run counts once (#582)
- Planned: the Stale row judges the tick's measured cadence and names an upgrade hold (#581)
- Planned: a factory publish counts as progress for the loop guard (#580)
- Planned: a decided S1 or S2 Bug is never silent in NEXT (#579)
- Planned: a cleanly exited session counts as finished, not dead (#578)
- Planned: three remaining gaps on a card that landed without naming it (#577)
- Planned: credit a landing to the run that earned it, not to another item (#575)
- Planned: the loop-guard row prints its own clock (#574)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.92"`

## v0.1.91 — 2026-09-30

### Features landed

- The CI plan now stops keeping a busy runner free, and tells how long a hold lasts (#573)
- ASF-prefixed runner labels are now reserved: a tier is placed, never declared, and an unroutable label is a red row (#566)

### Bugs fixed

- Pure git work no longer launches a session (#570)
- Parks can now be scoped and undone with unpark (#569)
- A red exact head is now also read on a new run's first pass (#568)

### In progress

- Planned: two Tasks so every push takes the clock of its own kind (#572)
- Planned: two Tasks so the retirement rule lands first, then the word that says it (#571)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.91"`

## v0.1.90 — 2026-09-30

### Features landed

- An inbox card's cleaned-up title is now checked against its raw file name (#562)
- The launcher now refreshes itself from the snapshot it just made (#561)
- One table now says which source dominates tick time and names the cut (#543)
- The CI queue's wait limits now follow the measured median and can never be shortened below it (#558)
- Batch parallelism and runners are now measured when a product declares neither (#557)

### Bugs fixed

- A red exact head is now read in more lane states and replaces a pending review round (#563)
- Landing evidence must now be the item's own commit, never one merged in from the trunk (#560)

### In progress

- Planned: the record hook derives the index it cannot find, and the guide stops ordering that step (#564)
- Planned: three Tasks so a slow-hook publish is measured, capped and parked (#565)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.90"`

## v0.1.89 — 2026-09-30

### Features landed

- ASF can now discover the CI runner pool from the host, tier it by measured speed, and avoid flapping (#534)

### Bugs fixed

- A PR's checks now count its exact head's runs from every event, and a red head is a correct round (#556)

### In progress

- Planned: the CI queue status measures its preconditions and acceptance strings (#555)
- Planned: the launcher refreshes itself, the pool is bounded by count, and doctor reports both (#554)
- Planned: intake refuses a bad type line clearly, then the guide describes it (#553)
- Planned: six Tasks to backfill a partly built product's history into the record (#552)
- Planned: a retired Feature's stage is the retirement, and moved cards leave the tables (#551)
- Planned: a publish gets 900 seconds and a plain push keeps 120, with the seconds shown (#550)
- Planned: the Record row splits its count into work, resolved and records (#549)
- Planned: a protected name stays on the card that holds it (#548)
- Planned: a timed-out publish is parked instead of pushed forever (#547)
- Planned: the record hook rebuilds a missing index and the guide drops the manual step (#546)
- Planned: the mint gate learns the plan header and its skips now speak (#545)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.89"`

## v0.1.88 — 2026-09-30

### Bugs fixed

- Heavy CI that never started is now started, a conflicting PR goes back, and a docs PR is not labelled (#542)

### In progress

- Planned: the status snapshot clock reaches the launcher copy, the pool and the row (#541)
- Planned: a header line is never a title, and refusals name the line that would settle the card (#540)
- Planned: backfill turns merged history into each card's typed answer (#539)
- Planned: the Record row splits its count into work, resolved and records (#538)
- Planned: a protected name stays on the card that holds it (#537)
- Planned: four Tasks for the status snapshot, status line, setting and row (#536)
- Planned: six Tasks for the command center, read-only (#535)
- Planned: the mint gate learns the plan header and the plan briefs ask for the heading (#533)
- Planned: four Tasks so a session commits under its own identity (#532)
- Planned: five Tasks so the worker's allow list comes from the gate's own registry (#531)
- Planned: the CI queue plan was updated after the trunk moved under it (#530)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.88"`

## v0.1.87 — 2026-09-30

### Features landed

- The cloud default now uses a short exception list instead of an allow list (#529)
- Every cancelled CI run now gets exactly one recorded cause (#528)
- While any runner label is unresolved, none is removed and doctor says which (#526)
- The product-config guide now documents model routing, quoted from doctor (#514)
- The runner-label reader now asks the host for its variables and says when it cannot (#513)
- A card now reads the usage record the factory already writes (#518)
- ASF now measures each runner's median time, ratio and green rate per job kind and publishes a baseline (#517)

### In progress

- Planned: each tick leaves a status snapshot that the session's status line reads (#527)
- Planned: the worker's allow list is the gate's own registry (#524)
- Planned: six Tasks for running CI on any machine (#523)
- Planned: six Tasks for the end-user review gate (#522)
- Planned: a command center with one read-only table and live stream over every product (#525)
- Planned: a session commits under its own identity, never the operator's (#521)
- Planned: three Tasks for the cloud default, its exception list and the bypass (#520)
- Planned: four Tasks so a refused reword is remembered (#519)
- Planned: the CI queue stops keeping a busy runner free and holds starts that cannot finish first (#512)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.87"`

## v0.1.86 — 2026-09-30

### Features landed

- A test now proves a skipped heavy CI check never merges a PR that has no label (#506)
- The factory now makes one push per correction round and runs review before heavy CI (#500)

### Bugs fixed

- Work can now widen inside its Feature's scope, work already on the trunk is closed, and asf park was added (#516)
- A GitHub rate limit is now treated as unknown rather than real state, so the factory pauses and budgets calls (#515)

### In progress

- Planned: CI on any machine reachable over ssh (#511)
- Planned: an end-user review gate over landing and deploy, with operator sign-off on legal text (#509)
- Planned: the cloud becomes the default place sessions run, with a short exception list (#510)
- Planned: a refused naming reword is remembered so it is not retried every tick (#508)
- Planned: three Tasks to find which superseded runs were waste (#507)
- Planned: a refused push is remembered, earns a skip, and has a hold so it is never a dead end (#501)
- Planned: a read cache and eight-wide fetching to speed up the wave step (#499)
- Planned: three Tasks for pruning the shared build cache and reporting its size (#505)
- Planned: three Tasks to document, exemplify and pin per-class model routing (#504)
- Planned: nine Tasks so ASF discovers runners, measures them and places jobs itself (#503)
- Planned: two Tasks so the runner-label reader resolves host variables and holds what it cannot judge (#502)
- Planned: three Tasks for the CI artifact quota, prune and doctor rows (#498)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.86"`

## v0.1.85 — 2026-09-30

### Features landed

- The flaky-test pass now asks for one workflow's runs over a window that only moves past what it has read (#492)

### Bugs fixed

- The status table's Parked row now names every item a park holds (#495)

### In progress

- Planned: three Tasks for the runner-label reader: freeze, evaluator, then repo variables and guide (#497)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.85"`

## v0.1.84 — 2026-09-30

### Bugs fixed

- A row handed the same state twice now parks instead of relaunching, and every launch is recorded (#488)
- Briefs now name the product's pre-push check as required, and targeted test runs pass the full-suite guard (#486)
- Every cancelled CI run now gets one logged cause, job timeouts are named, and orphaned heads are re-run (#482)

### In progress

- Planned: every cancelled CI run gets a cause, told apart by its own head pair (#480)
- Planned: a refused publish is remembered so health stops re-running the pre-push hook on untouched branches (#479)
- Planned: the shared build cache is pruned past 7 days or 10 GB, with its size in doctor (#477)
- Planned: per-class model routing gets documentation and a pinned product-file path (#476)
- Planned: ASF reads the runner pool off the host, measures speed, and places and paces jobs (#475)
- Planned: six Tasks for a second round of tick speed-ups (#478)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.84"`

## v0.1.83 — 2026-09-30

### Features landed

- Doctor now names the install each clock runs, and goes red when it is not pinned and merged (#467)
- A CI job's time limit is now read from the workflow file (#466)

### In progress

- Planned: two Tasks for the debug-toggle landing check, including a doctor row describing it (#474)
- Planned: two more decisions on long Story ids and truncated card descriptions (#473)
- Planned: a runner label is judged against the host's own variables, and an unresolved one holds every removal (#472)
- Planned: the runner-label reader resolves expression forms over the repo's variables, and removes nothing it cannot resolve (#468)
- Planned: the factory keeps its own CI artifact storage under quota with daily and early pruning (#471)
- Planned: the wave step gets a read cache and eight-wide fetching (#470)
- Planned: three Tasks for pausing a single product while the tick keeps recording (#469)
- Planned: the flaky-test pass asks one workflow for its runs and git is asked once per pass (#465)
- Planned: the landing gate refuses a branch that adds a debug toggle, with a per-line waiver that needs a reason (#464)
- Planned: three Tasks for an optional native PR landing step with a doctor row and guide (#462)
- Planned: capacity reads every usage window at each account's own scale (#463)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.83"`

## v0.1.82 — 2026-09-30

### Bugs fixed

- B-0080 after: holds the coder row only — correction and adjudicate launch anyway, on Opus

### In progress

- F-0094 A decided Epic with no Features never starts: no row breaks an Epic into Features
- F-0104 Products tick on a pinned live install; `asf upgrade` moves it to the merged head
- F-0105 The landing gate runs 15–25 min on the operator's machine next to 8 sessions: gate on CI instead
- F-0106 Plans cut Tasks already on trunk, and depend on ids that were never minted: coders end with an empty branch
- F-0131 Classify every cancelled CI job as timeout, runner loss or failure, and list jobs near their limit
- F-0133 Groom commits its own output; a foreign daily stamp never skips the native daily; new products groom by default
- F-0135 Native PR landing: merge an ASF-opened PR when approved and green, so PR products need no merge-queue script
- F-0137 Pause one product: the tick keeps recording and harvesting but launches nothing
- F-0144 asf set cannot widen a Task's writes: footprint
- F-0207 Pending upgrade starves: every owner tick defers because another owner tick is running
- F-0229 Lane reaches MERGING with PR #None (plan/T-0189); I9 events repeat every tick
- F-0232 Route a Task whose writes: touch the amendable set to the console at plan time, not a worker launch

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.82"`

## v0.1.81 — 2026-09-29

### Features landed

- F-0095 The wave relaunches coders on Tasks that end 'empty branch: nothing to land'

### Bugs fixed

- B-0051 A session that ends 'finished' without pushing blocks its item forever: health says finished, harvest sees nothing, spawn refuses the worktree

### In progress

- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0051 The production verification probe: a real customer journey after every deploy
- F-0094 A decided Epic with no Features never starts: no row breaks an Epic into Features
- F-0104 Products tick on a pinned live install; `asf upgrade` moves it to the merged head
- F-0105 The landing gate runs 15–25 min on the operator's machine next to 8 sessions: gate on CI instead
- F-0106 Plans cut Tasks already on trunk, and depend on ids that were never minted: coders end with an empty branch
- F-0111 hooks install from asf-live tells the operator to wire the dev install's path into the product's git hooks
- F-0126 feeder: a Task with no writes: (migrated pre-ASF plan) is launched and can only block
- F-0131 Classify every cancelled CI job as timeout, runner loss or failure, and list jobs near their limit
- F-0133 Groom commits its own output; a foreign daily stamp never skips the native daily; new products groom by default
- F-0160 Health records a session ended at its first result while the process keeps working
- F-0207 Pending upgrade starves: every owner tick defers because another owner tick is running
- F-0208 Plan preflight across open branches: refuse a reserved range another open branch already holds
- F-0218 asf unpark releases one parked job per call
- F-0224 Repair sessions 'review' take 17 % of session spend
- F-0227 An ended worker run whose pid stays alive blocks its item forever (T-0196)
- F-0229 Lane reaches MERGING with PR #None (plan/T-0189); I9 events repeat every tick
- F-0231 No command releases a stale correction; F-1129 holds one for a never-pushed branch
- F-0232 Route a Task whose writes: touch the amendable set to the console at plan time, not a worker launch
- F-0233 Stories/Features of a deploying product never close: _in_prod needs checked.txt, which nothing writes

### Improvements and hotfixes

- fix(ingest): an applied replan carries the Tasks a removed survivor orphaned (#433)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.81"`

## v0.1.80 — 2026-09-29

### Bugs fixed

- B-0051 A session that ends 'finished' without pushing blocks its item forever: health says finished, harvest sees nothing, spawn refuses the worktree
- B-0058 A blocked Bug still gets its FIX row and a blocked item its CORRECT or ADJUDICATE row: the feeder reads blocked for Features only

### In progress

- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0060 A mechanical correctness pass before the human-standard review
- F-0190 Deploy relevance counts test-only commits as undeployed changes
- F-0209 Plans get writes: right; lane widens writes: on refusal instead of dying
- F-0212 The approvals hook holds a read-only 'git config core.hooksPath' as a security write
- F-0218 asf unpark releases one parked job per call
- F-0223 Sessions die with 'failed' 6.5 times a week
- F-0224 Repair sessions 'review' take 17 % of session spend
- F-0227 An ended worker run whose pid stays alive blocks its item forever (T-0196)
- F-0231 No command releases a stale correction; F-1129 holds one for a never-pushed branch

### Improvements and hotfixes

- fix(ingest): a removed survivor never closes a Feature over the Tasks merged into it (#426)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.80"`

## v0.1.79 — 2026-09-29

### Bugs fixed

- B-0019 Health reaps a live job whose branch has no commits yet (reads "no commits" as "merged")
- B-0141 An upgrade that cannot install still parks every product's ticks for 30 minutes

### In progress

- F-0190 Deploy relevance counts test-only commits as undeployed changes
- F-0200 Intake: an S1 card stuck on a signature question sits silently; the 'S1:' title prefix is ignored
- F-0201 Landed spec/plan branches keep their worker worktree
- F-0203 Lane rebases a PR onto a moved main mid-CI, restarting its required jobs (S1 #858 superseded 4x)
- F-0204 merge_skipped: a missing non-Actions required check counts as path-filtered
- F-0206 Orphaned dead session row (B-1381, unknown account) never reaped for 5 h
- F-0207 Pending upgrade starves: every owner tick defers because another owner tick is running
- F-0208 Plan preflight across open branches: refuse a reserved range another open branch already holds
- F-0209 Plans get writes: right; lane widens writes: on refusal instead of dying
- F-0212 The approvals hook holds a read-only 'git config core.hooksPath' as a security write
- F-0218 asf unpark releases one parked job per call
- F-0223 Sessions die with 'failed' 6.5 times a week

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.79"`

## v0.1.78 — 2026-09-29

### Bugs fixed

- B-0019 Health reaps a live job whose branch has no commits yet (reads "no commits" as "merged")

### In progress

- F-0170 32 pre-existing I3 writes: overlaps after the invariants audit; groom on no clock
- F-0176 B-0123 loop: 'died without a result' while both logs end in a ruling; ready branch never published
- F-0186 conventions.doc_paths: let a product declare extra document folders so approved spec branches land without a session
- F-0187 conventions.shared_paths: lock files overlap every Task — serialise them at merge, not in the feeder
- F-0189 Cross-product seat overcommit: two waves launch on one account at once (6 of cap 4)
- F-0190 Deploy relevance counts test-only commits as undeployed changes
- F-0200 Intake: an S1 card stuck on a signature question sits silently; the 'S1:' title prefix is ignored
- F-0201 Landed spec/plan branches keep their worker worktree
- F-0203 Lane rebases a PR onto a moved main mid-CI, restarting its required jobs (S1 #858 superseded 4x)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.78"`

## v0.1.77 — 2026-09-29

### Features landed

- F-0078 The tick ends with two tables: in flight, and done since the last tick
- F-0173 A ruling should survive a lane rebase (same patch-ids, new head)

### Bugs fixed

- B-0123 A failed daily run is never retried until the next day

### In progress

- F-0170 32 pre-existing I3 writes: overlaps after the invariants audit; groom on no clock
- F-0175 ASF collects trunk CI runs itself — CI facts go stale when a product's legacy collector is retired
- F-0176 B-0123 loop: 'died without a result' while both logs end in a ruling; ready branch never published
- F-0185 Connectors: declare each external service (CI, deploy, db, …) once per product — asf connect sets up auth, doctor/status/approvals/sessions all read it
- F-0186 conventions.doc_paths: let a product declare extra document folders so approved spec branches land without a session
- F-0187 conventions.shared_paths: lock files overlap every Task — serialise them at merge, not in the feeder
- F-0188 coverage: a product declares references; generic reference coverage (generalises matrix_path + asf parity)
- F-0189 Cross-product seat overcommit: two waves launch on one account at once (6 of cap 4)
- F-0234 Sessions die with 'dead pid' 5.5 times a week

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.77"`

## v0.1.76 — 2026-09-29

### Features landed

- F-0157 Sessions die with 'failed: empty branch' 12 times a week

### Bugs fixed

- B-0059 A spec landing commit resolves the Feature it names, and a spec on main reads spec-draft: the evidence does not know the lane's own conventions
- B-0114 native landing: a merged spec/plan PR marks its Feature landed/Resolved, so no coder ever launches
- B-0123 A failed daily run is never retried until the next day
- B-0128 An adjudicator's 'no further sessions' ruling does not stick: the lane re-adjudicates the item
- B-0149 CI-red cards count cancelled runs and fixed history

### In progress

- B-0147 Invariant I10 refused a write to features/F-0093.md
- B-0150 doctor ci-pool row flags ASF's own ci_pool.reserve label as undeclared
- F-0023 The grill: a request is interrogated before it is spent on
- F-0158 Sessions die with 'failed: not pushed' 72 times a week
- F-0169 CI gate:--- test_tick_steps: FAILED (rc N) is red 3.5 times a week
- F-0170 32 pre-existing I3 writes: overlaps after the invariants audit; groom on no clock
- F-0173 A ruling should survive a lane rebase (same patch-ids, new head)
- F-0175 ASF collects trunk CI runs itself — CI facts go stale when a product's legacy collector is retired
- F-0176 B-0123 loop: 'died without a result' while both logs end in a ruling; ready branch never published
- F-0185 Connectors: declare each external service (CI, deploy, db, …) once per product — asf connect sets up auth, doctor/status/approvals/sessions all read it
- F-0186 conventions.doc_paths: let a product declare extra document folders so approved spec branches land without a session
- F-0187 conventions.shared_paths: lock files overlap every Task — serialise them at merge, not in the feeder
- F-0188 coverage: a product declares references; generic reference coverage (generalises matrix_path + asf parity)
- F-0234 Sessions die with 'dead pid' 5.5 times a week

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.76"`

## v0.1.75 — 2026-09-29

### Features landed

- F-0098 'dead' sessions are nearly always finished ones not yet recorded: say 'ended, awaiting tick', free the slot at once
- F-0159 CI gate:--- test_dry_run: FAILED (rc N) is red 6 times a week

### In progress

- F-0021 P5 — launch
- F-0030 The README carries the argument, the mental model and the manual on one page
- F-0060 A mechanical correctness pass before the human-standard review
- F-0068 Spike: one Feature through a hosted agent runtime, compared with the local pool
- F-0069 Security checks by code: a review pass on sensitive paths, secret and dependency scanning as rules
- F-0120 Adopting a record: flag cards that belong to another product and move or remove them in bulk (asf move)
- F-0125 Onboarding: adopt a product's in-flight PRs (asf adopt-pr + legacy review form) so native landing drains them
- F-0126 feeder: a Task with no writes: (migrated pre-ASF plan) is launched and can only block
- F-0157 Sessions die with 'failed: empty branch' 12 times a week
- F-0158 Sessions die with 'failed: not pushed' 72 times a week
- F-0234 Sessions die with 'dead pid' 5.5 times a week

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.75"`

## v0.1.74 — 2026-09-29

### Features landed

- F-0082 Quota: a cooldown band before the stop — one new job per account from 90 %, none from 95 %
- F-0090 Adjudication is the last resort: the cheap causes are ruled out before Opus is spent

### In progress

- F-0120 Adopting a record: flag cards that belong to another product and move or remove them in bulk (asf move)
- F-0158 Sessions die with 'failed: not pushed' 72 times a week
- F-0162 asf upgrade writes the pending marker before checking the ref exists
- F-0163 asf set cannot change a bug's severity (needed to downgrade an S1)
- F-0167 asf status blocks on quota_command: reads uncached per account, 60 s timeout each
- F-0234 Sessions die with 'dead pid' 5.5 times a week
- F-0235 Sessions die with 'failed: hook refused' 19.5 times a week

### Improvements and hotfixes

- fix(publish): a worker-account name in unpublished commits is rewritten to lane-N, not refused forever (#340)
- fix(feeder): a REPLAN row waits on any of the Feature's Tasks with a PR in flight (#349)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.74"`

## v0.1.73 — 2026-09-29

### Features landed

- F-0150 Scorecard loop: a card whose number got worse is reverted, and adoptions are Decisions with both readings
- F-0156 711 branches carry no open PR

### In progress

- F-0157 Sessions die with 'failed: empty branch' 12 times a week

### Improvements and hotfixes

- fix(scheduler): a durable clock pause; upgrade never reloads a clock it did not unload (#332)
- fix(tick): asf tick --dry-run is side-effect free, structurally (#336)
- fix(review): reviews never touch the PR branch — filed off it, bound to the head they reviewed (#337)
- fix(briefs): a Task whose work is already on the trunk has a way to close — the empty commit naming its id (#334)
- fix(feeder): a Task-led delivery with no writes gets its RESHAPE re-cut, not a wait no session ends (#338)
- fix(lane): a merged or closed PR never yields a review, landing or correction row (#339)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.73"`

## v0.1.72 — 2026-09-29

### Features landed

- F-0098 'dead' sessions are nearly always finished ones not yet recorded: say 'ended, awaiting tick', free the slot at once

### Bugs fixed

- B-0028 A corrected or late-finishing session stays 'dead pid'; harvest never lands its branch

### In progress

- F-0035 Thin controller: every read loop moves to the tick, and a rule flags scriptable chores
- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0150 Scorecard loop: a card whose number got worse is reverted, and adoptions are Decisions with both readings
- F-0156 711 branches carry no open PR
- F-0158 Sessions die with 'failed: not pushed' 72 times a week
- F-0159 CI gate:--- test_dry_run: FAILED (rc N) is red 6 times a week
- F-0160 Health records a session ended at its first result while the process keeps working
- F-0162 asf upgrade writes the pending marker before checking the ref exists
- F-0163 asf set cannot change a bug's severity (needed to downgrade an S1)
- F-0169 CI gate:--- test_tick_steps: FAILED (rc N) is red 3.5 times a week
- F-0235 Sessions die with 'failed: hook refused' 19.5 times a week

### Improvements and hotfixes

- fix(ci-queue): a refused re-run says why, is bounded, and falls back to a fresh run (#321)
- fix(spawn): rebase a held branch onto the trunk at launch only when it needs it (#329)
- fix(record): a PR closed unmerged resets its Task to ready and clears its rounds (#331)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.72"`

## v0.1.71 — 2026-09-29

### In progress

- F-0122 The /asf:* tables read backlog_dir, which the tick never pulls — views go stale
- F-0148 Scorecard: a total across products and the week-over-week delta, as the daily rollup's first line
- F-0150 Scorecard loop: a card whose number got worse is reverted, and adoptions are Decisions with both readings
- F-0155 asf next/status read backlog_dir while the tick acts on its own clone — NEXT lags the tick
- F-0156 711 branches carry no open PR
- F-0157 Sessions die with 'failed: empty branch' 12 times a week

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.71"`

## v0.1.70 — 2026-09-28

### Features landed

- F-0031 The approval matrix as code
- F-0098 'dead' sessions are nearly always finished ones not yet recorded: say 'ended, awaiting tick', free the slot at once

### Bugs fixed

- B-0056 A session whose branch is already on origin merges its stale remote: nothing in the factory publishes a rebased lane branch, and no session may force
- B-0075 Workers still background the test suite and end without pushing; prose cannot stop it
- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push
- B-0128 An adjudicator's 'no further sessions' ruling does not stick: the lane re-adjudicates the item
- B-0149 CI-red cards count cancelled runs and fixed history

### In progress

- F-0001 P0a — the two repositories and the licence
- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0094 A decided Epic with no Features never starts: no row breaks an Epic into Features
- F-0122 The /asf:* tables read backlog_dir, which the tick never pulls — views go stale
- F-0126 feeder: a Task with no writes: (migrated pre-ASF plan) is launched and can only block
- F-0143 A pre-commit hook pointing at the /x/asf test fixture path broke review-b-0111's commit
- F-0144 asf set cannot widen a Task's writes: footprint
- F-0148 Scorecard: a total across products and the week-over-week delta, as the daily rollup's first line
- F-0149 Scorecard attribution: session spend matched to no Feature, and CI red counted per job instead of per root cause
- F-0150 Scorecard loop: a card whose number got worse is reverted, and adoptions are Decisions with both readings
- F-0152 Adjudicate brief names review round 1 instead of the newest round
- F-0155 asf next/status read backlog_dir while the tick acts on its own clone — NEXT lags the tick
- F-0156 711 branches carry no open PR
- F-0157 Sessions die with 'failed: empty branch' 12 times a week
- F-0158 Sessions die with 'failed: not pushed' 72 times a week
- F-0159 CI gate:--- test_dry_run: FAILED (rc N) is red 6 times a week
- F-0160 Health records a session ended at its first result while the process keeps working

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.70"`

## v0.1.69 — 2026-09-28

### Features landed

- F-0107 asf install: one command from zero to a ticking factory (from a release tag, doctor green)
- F-0127 A product declares a worktree setup command; the spawner runs it in every fresh worktree

### In progress

- F-0110 /asf:* in a product's own session shows another product: resolve the product from the working directory
- F-0126 feeder: a Task with no writes: (migrated pre-ASF plan) is launched and can only block
- F-0143 A pre-commit hook pointing at the /x/asf test fixture path broke review-b-0111's commit

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.69"`

## v0.1.68 — 2026-09-28

### In progress

- F-0126 feeder: a Task with no writes: (migrated pre-ASF plan) is launched and can only block
- F-0127 A product declares a worktree setup command; the spawner runs it in every fresh worktree

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.68"`

## v0.1.67 — 2026-09-28

### Bugs fixed

- B-0050 Record commands use the cwd as the record: /asf:groom from a product repo grooms nothing and writes groom/ into the product repo
- B-0095 The PROD view crashes on a product whose `customer_paths` is the documented list
- B-0119 tick logs stay empty while steps run; a live tick looks the same as a hung one

### In progress

- F-0107 asf install: one command from zero to a ticking factory (from a release tag, doctor green)
- F-0119 asf pr-hygiene --lanes: a machine-readable listing product rule checks can call
- F-0120 Adopting a record: flag cards that belong to another product and move or remove them in bulk (asf move)
- F-0122 The /asf:* tables read backlog_dir, which the tick never pulls — views go stale
- F-0125 Onboarding: adopt a product's in-flight PRs (asf adopt-pr + legacy review form) so native landing drains them
- F-0126 feeder: a Task with no writes: (migrated pre-ASF plan) is launched and can only block
- F-0127 A product declares a worktree setup command; the spawner runs it in every fresh worktree
- F-0128 approvals: decide_feature: auto — ASF decides a product's Features itself (operator instruction)
- F-0129 asf config check/migrate: an old or hand-made product yaml maps onto the schema without a hand rewrite
- F-0142 tick: a start/end line per step (owner, pid, time) and doctor's SCHEDULER shows the current step
- F-0143 A pre-commit hook pointing at the /x/asf test fixture path broke review-b-0111's commit
- F-0144 asf set cannot widen a Task's writes: footprint
- F-0148 Scorecard: a total across products and the week-over-week delta, as the daily rollup's first line

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.67"`

## v0.1.66 — 2026-09-28

### In progress

- F-0107 asf install: one command from zero to a ticking factory (from a release tag, doctor green)
- F-0111 hooks install from asf-live tells the operator to wire the dev install's path into the product's git hooks
- F-0113 An S1 silently holds all Feature work, and NEXT plans from different inputs than the tick
- F-0114 Upgrades never break other installs: automatic, versioned, rollback-safe migrations with an upgrade test from every release
- F-0119 asf pr-hygiene --lanes: a machine-readable listing product rule checks can call
- F-0120 Adopting a record: flag cards that belong to another product and move or remove them in bulk (asf move)
- F-0121 asf hooks install chains an existing git hook instead of refusing (stdin shared, one command)
- F-0122 The /asf:* tables read backlog_dir, which the tick never pulls — views go stale
- F-0125 Onboarding: adopt a product's in-flight PRs (asf adopt-pr + legacy review form) so native landing drains them
- F-0126 feeder: a Task with no writes: (migrated pre-ASF plan) is launched and can only block

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.66"`

## v0.1.65 — 2026-09-28

### Bugs fixed

- B-0136 An install can leave a product's tick clock unloaded; status just omits it

### In progress

- F-0107 asf install: one command from zero to a ticking factory (from a release tag, doctor green)
- F-0111 hooks install from asf-live tells the operator to wire the dev install's path into the product's git hooks
- F-0112 Products update themselves to a new ASF release: upgrade: auto in the tick, with rollback on a red doctor
- F-0113 An S1 silently holds all Feature work, and NEXT plans from different inputs than the tick
- F-0114 Upgrades never break other installs: automatic, versioned, rollback-safe migrations with an upgrade test from every release
- F-0119 asf pr-hygiene --lanes: a machine-readable listing product rule checks can call
- F-0120 Adopting a record: flag cards that belong to another product and move or remove them in bulk (asf move)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.65"`

## v0.1.64 — 2026-09-28

### In progress

- F-0070 A CI step-silence rule: a job with no new step for ten minutes is stalled, even under budget
- F-0107 asf install: one command from zero to a ticking factory (from a release tag, doctor green)
- F-0108 Release ASF 0.1 for feedback: general installer, first-user docs, release notes, a feedback channel
- F-0110 /asf:* in a product's own session shows another product: resolve the product from the working directory
- F-0113 An S1 silently holds all Feature work, and NEXT plans from different inputs than the tick

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.64"`

## v0.1.63 — 2026-09-28

### Bugs fixed

- B-0047 asf plugin check resolves the plugin directory from the package, not the checkout; CI is red on a plain install

### In progress

- F-0070 A CI step-silence rule: a job with no new step for ten minutes is stalled, even under budget
- F-0107 asf install: one command from zero to a ticking factory (from a release tag, doctor green)
- F-0108 Release ASF 0.1 for feedback: general installer, first-user docs, release notes, a feedback channel
- F-0110 /asf:* in a product's own session shows another product: resolve the product from the working directory
- F-0111 hooks install from asf-live tells the operator to wire the dev install's path into the product's git hooks
- F-0112 Products update themselves to a new ASF release: upgrade: auto in the tick, with rollback on a red doctor

### Improvements and hotfixes

- ci_queue: a rerun skipped for a workflow change starts fresh instead of dropping (#241)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.63"`

## v0.1.62 — 2026-09-28

### In progress

- F-0068 Spike: one Feature through a hosted agent runtime, compared with the local pool
- F-0070 A CI step-silence rule: a job with no new step for ten minutes is stalled, even under budget
- F-0084 The command surface reads as one product: names, one voice, and "writes" marked
- F-0107 asf install: one command from zero to a ticking factory (from a release tag, doctor green)
- F-0108 Release ASF 0.1 for feedback: general installer, first-user docs, release notes, a feedback channel
- F-0109 install.sh: the operating session cannot run it, it leaves the pre-ASF clocks running, and it installs hooks without the human-now approval
- F-0110 /asf:* in a product's own session shows another product: resolve the product from the working directory

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.62"`

## v0.1.61 — 2026-09-28

### Bugs fixed

- B-0047 asf plugin check resolves the plugin directory from the package, not the checkout; CI is red on a plain install

### In progress

- F-0050 The product improvement loop: the factory proposes product changes from the product's own measurements
- F-0066 A progress heartbeat: no progress for twenty minutes is a stall, not only silence
- F-0068 Spike: one Feature through a hosted agent runtime, compared with the local pool
- F-0069 Security checks by code: a review pass on sensitive paths, secret and dependency scanning as rules
- F-0084 The command surface reads as one product: names, one voice, and "writes" marked
- F-0107 asf install: one command from zero to a ticking factory (from a release tag, doctor green)

### Improvements and hotfixes

- fix(capacity): CI free from idle self-hosted runners, not whole runs in flight (#222)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.61"`

## v0.1.60 — 2026-09-28

### Bugs fixed

- B-0090 The operator's console rules live in assistant memory, not code: the plugin ships no hooks enforcing them
- B-0128 An adjudicator's 'no further sessions' ruling does not stick: the lane re-adjudicates the item
- B-0151 Lane branches collide on sequential numbers (migrations, bands); ASF should reserve them

### In progress

- F-0051 The production verification probe: a real customer journey after every deploy
- F-0065 Spike: a sandboxed shell for worker sessions
- F-0066 A progress heartbeat: no progress for twenty minutes is a stall, not only silence
- F-0069 Security checks by code: a review pass on sensitive paths, secret and dependency scanning as rules
- F-0070 A CI step-silence rule: a job with no new step for ten minutes is stalled, even under budget

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.60"`

## v0.1.59 — 2026-09-28

### In progress

- F-0050 The product improvement loop: the factory proposes product changes from the product's own measurements
- F-0051 The production verification probe: a real customer journey after every deploy
- F-0060 A mechanical correctness pass before the human-standard review
- F-0062 The launch path: skills as procedures, role agents with restricted tools, per-job caps, deny rules
- F-0063 Relaunch from the branch, not from zero
- F-0065 Spike: a sandboxed shell for worker sessions
- F-0066 A progress heartbeat: no progress for twenty minutes is a stall, not only silence
- F-0069 Security checks by code: a review pass on sensitive paths, secret and dependency scanning as rules
- F-0070 A CI step-silence rule: a job with no new step for ten minutes is stalled, even under budget

### Improvements and hotfixes

- fix(ci-queue): a PR run ranks by the record ids its title carries (#203)
- fix(deploy): name the ignore-file-excludes-traced-files cause on a failed deploy (#204)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.59"`

## v0.1.58 — 2026-09-28

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push

### In progress

- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0053 Reviews return checks: every review ends in a machine-readable check table the tick reads
- F-0063 Relaunch from the branch, not from zero
- F-0066 A progress heartbeat: no progress for twenty minutes is a stall, not only silence

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.58"`

## v0.1.57 — 2026-09-28

### In progress

- F-0053 Reviews return checks: every review ends in a machine-readable check table the tick reads
- F-0063 Relaunch from the branch, not from zero
- F-0120 Adopting a record: flag cards that belong to another product and move or remove them in bulk (asf move)

### Improvements and hotfixes

- fix(reviews): a pipe in the evidence or a qualified result is a row, not a fault (#197)
- fix(env): a bare command resolves the product whose checkout holds the cwd (#198)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.57"`

## v0.1.56 — 2026-09-28

### In progress

- F-0062 The launch path: skills as procedures, role agents with restricted tools, per-job caps, deny rules
- F-0067 Clean floor, re-scoped: the sweep, the registry and the tool repository

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.56"`

## v0.1.55 — 2026-09-28

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push

### In progress

- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0053 Reviews return checks: every review ends in a machine-readable check table the tick reads
- F-0056 Memory to code, first pass: what a session has to remember becomes a script, a row or a rule
- F-0062 The launch path: skills as procedures, role agents with restricted tools, per-job caps, deny rules
- F-0063 Relaunch from the branch, not from zero
- F-0066 A progress heartbeat: no progress for twenty minutes is a stall, not only silence

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.55"`

## v0.1.54 — 2026-09-27

### Features landed

- F-0222 Detect a wedged account-manager usage lock and (opt-in) reclaim it

### Improvements and hotfixes

- perf(ingest): one rev-list of the deploy answers every in-prod question
- review(PR-0180): round 1 — approved; one rev-list answers every in-prod question
- perf(health): the factory's publishes of independent runs go at once

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.54"`

## v0.1.53 — 2026-09-27

### Features landed

- F-0098 'dead' sessions are nearly always finished ones not yet recorded: say 'ended, awaiting tick', free the slot at once

### In progress

- F-0042 Credentials as a rule: a daily check that every signed-in tool is valid for three more days
- F-0052 Budget enforcement per Epic
- F-0063 Relaunch from the branch, not from zero
- F-0067 Clean floor, re-scoped: the sweep, the registry and the tool repository

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.53"`

## v0.1.52 — 2026-09-27

### Features landed

- F-0075 Redaction is a gate on every write path: operator names and secrets never reach a record or a public repo
- F-0093 Model routing is argued from minutes-per-landing, and the pool gets a cheap tier

### In progress

- F-0042 Credentials as a rule: a daily check that every signed-in tool is valid for three more days
- F-0046 The conflicts pass: supersession enforced, same-subject rule and decision pairs into the groom
- F-0053 Reviews return checks: every review ends in a machine-readable check table the tick reads
- F-0055 A frozen eval set for the factory's judging tools
- F-0056 Memory to code, first pass: what a session has to remember becomes a script, a row or a rule
- F-0060 A mechanical correctness pass before the human-standard review
- F-0061 Hooks as rule enforcement inside every worker session
- F-0062 The launch path: skills as procedures, role agents with restricted tools, per-job caps, deny rules
- F-0064 Events at the source: every tool appends its own event, and the human log is rendered from the streams
- F-0065 Spike: a sandboxed shell for worker sessions
- F-0066 A progress heartbeat: no progress for twenty minutes is a stall, not only silence
- F-0069 Security checks by code: a review pass on sensitive paths, secret and dependency scanning as rules

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.52"`

## v0.1.51 — 2026-09-27

### In progress

- F-0024 Self-amendment policy: the factory proposes, humans approve and merge
- F-0046 The conflicts pass: supersession enforced, same-subject rule and decision pairs into the groom
- F-0052 Budget enforcement per Epic
- F-0055 A frozen eval set for the factory's judging tools
- F-0064 Events at the source: every tool appends its own event, and the human log is rendered from the streams
- F-0065 Spike: a sandboxed shell for worker sessions

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.51"`

## v0.1.50 — 2026-09-27

### Features landed

- F-0022 The preamble: every brief opens with facts the runner already knows
- F-0075 Redaction is a gate on every write path: operator names and secrets never reach a record or a public repo

### In progress

- F-0052 Budget enforcement per Epic
- F-0053 Reviews return checks: every review ends in a machine-readable check table the tick reads
- F-0061 Hooks as rule enforcement inside every worker session
- F-0062 The launch path: skills as procedures, role agents with restricted tools, per-job caps, deny rules
- F-0063 Relaunch from the branch, not from zero

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.50"`

## v0.1.49 — 2026-09-27

### In progress

- F-0052 Budget enforcement per Epic
- F-0055 A frozen eval set for the factory's judging tools
- F-0056 Memory to code, first pass: what a session has to remember becomes a script, a row or a rule

### Improvements and hotfixes

- fix(tick): the wave's overlays keep the index reader's retired cards (#151)
- fix(publish): a superseded branch commit is one the trunk holds whole (#154)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.49"`

## v0.1.48 — 2026-09-27

### In progress

- B-0147 Invariant I10 refused a write to features/F-0093.md
- F-0052 Budget enforcement per Epic
- F-0053 Reviews return checks: every review ends in a machine-readable check table the tick reads
- F-0054 Failure classification before any relaunch: credential, quota, transport or work
- F-0055 A frozen eval set for the factory's judging tools
- F-0056 Memory to code, first pass: what a session has to remember becomes a script, a row or a rule
- F-0061 Hooks as rule enforcement inside every worker session
- F-0092 An item has a budget: three sessions or $10, then it stops and says why
- F-0097 asf status says nothing about Bugs and Features: add a Bugs row and a Features row
- F-0117 Command center: follow every product's factory live and talk to each product's orchestrator; each product keeps its own controller

### Improvements and hotfixes

- fix(ingest): deploy_sha.prod.mode auto is its own prod tick (#146)
- fix(feeder): the index reader keeps a removed done card for after: and blockedBy (#149)

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.48"`

## v0.1.47 — 2026-09-27

### In progress

- F-0021 P5 — launch
- F-0046 The conflicts pass: supersession enforced, same-subject rule and decision pairs into the groom
- F-0060 A mechanical correctness pass before the human-standard review

### Improvements and hotfixes

- feat(delivery): the Feature is the delivery unit — `conventions.delivery: feature`
- refactor(feeder): branch_rows names its held set once

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.47"`

## v0.1.46 — 2026-09-27

### In progress

- F-0021 P5 — launch
- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0042 Credentials as a rule: a daily check that every signed-in tool is valid for three more days
- F-0046 The conflicts pass: supersession enforced, same-subject rule and decision pairs into the groom
- F-0052 Budget enforcement per Epic
- F-0053 Reviews return checks: every review ends in a machine-readable check table the tick reads
- F-0054 Failure classification before any relaunch: credential, quota, transport or work
- F-0060 A mechanical correctness pass before the human-standard review
- F-0092 An item has a budget: three sessions or $10, then it stops and says why

### Improvements and hotfixes

- feat(lane): merge: queue — the serialized merge queue lands only a gated sha

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.46"`

## v0.1.45 — 2026-09-27

### In progress

- F-0013 P3 wave 5 — the feeder and the tick
- F-0060 A mechanical correctness pass before the human-standard review
- F-0102 Deliveries: one plan, one agent, one gate for several small Features and Bug fixes across the backlog

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.45"`

## v0.1.44 — 2026-09-27

### Features landed

- F-0033 Dogfood end to end in CI
- F-0093 Model routing is argued from minutes-per-landing, and the pool gets a cheap tier

### In progress

- F-0013 P3 wave 5 — the feeder and the tick
- F-0024 Self-amendment policy: the factory proposes, humans approve and merge

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.44"`

## v0.1.43 — 2026-09-27

### Features landed

- F-0195 Feeder: finish planned Features before starting new specs

### Bugs fixed

- B-0114 native landing: a merged spec/plan PR marks its Feature landed/Resolved, so no coder ever launches

### In progress

- F-0010 P3 wave 2 — evidence and ingest
- F-0033 Dogfood end to end in CI
- F-0038 A factory-only CI class: a push touching only factory code runs lint and the factory tests
- F-0092 An item has a budget: three sessions or $10, then it stops and says why
- F-0094 A decided Epic with no Features never starts: no row breaks an Epic into Features
- F-0100 The savings pass: the tick proposes the next cheapest win from its own numbers
- F-0111 hooks install from asf-live tells the operator to wire the dev install's path into the product's git hooks

### Improvements and hotfixes

- fix(install): read the whole script before running it; a pre-existing doctor RED warns
- test(tick_steps): the fake planners take plan_rows' bandwidth

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.43"`

## v0.1.42 — 2026-09-27

### Features landed

- F-0075 Redaction is a gate on every write path: operator names and secrets never reach a record or a public repo

### Bugs fixed

- B-0087 The console operator is never told what a tick did: no per-tick digest, no asf watch
- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push
- B-0124 Status shows a 7-hour-old snapshot as current when every tick fails offline

### In progress

- F-0014 P3 wave 6 — the CLI and the plugin
- F-0021 P5 — launch
- F-0029 Roles: one file per role, five sections, model and access from config
- F-0037 Questions in batches: a non-blocking question goes to the groom; only a blocked launch or merge interrupts
- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0080 Closing is derived and total: a definition of done per type, reconciliation for work that predates it, nothing re-emitted
- F-0092 An item has a budget: three sessions or $10, then it stops and says why
- F-0094 A decided Epic with no Features never starts: no row breaks an Epic into Features
- F-0099 Groom every tick: intake, policy pass and questions run on each tick, not once a day

### Improvements and hotfixes

- test(quota_stale): the limit resets hours ahead, not at a fixed 8am

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.42"`

## v0.1.41 — 2026-09-27

### Bugs fixed

- B-0141 An upgrade that cannot install still parks every product's ticks for 30 minutes
- B-0146 The TICK summary says every step is ok while the tick exits 1

### In progress

- F-0010 P3 wave 2 — evidence and ingest
- F-0013 P3 wave 5 — the feeder and the tick
- F-0014 P3 wave 6 — the CLI and the plugin
- F-0038 A factory-only CI class: a push touching only factory code runs lint and the factory tests
- F-0080 Closing is derived and total: a definition of done per type, reconciliation for work that predates it, nothing re-emitted
- F-0092 An item has a budget: three sessions or $10, then it stops and says why
- F-0099 Groom every tick: intake, policy pass and questions run on each tick, not once a day
- F-0102 Deliveries: one plan, one agent, one gate for several small Features and Bug fixes across the backlog

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.41"`

## v0.1.40 — 2026-09-27

### Bugs fixed

- B-0140 A push refused by the repo's pre-push hook loops as 'unpushed work' — carry the hook's output into the correction and route by it
- B-0142 Branch held by an external (non-factory) worktree reports NEEDS OPERATOR every tick; should be a WAITS
- B-0143 Local worker sessions can't authenticate git pushes (keychain unavailable); route via factory credentials

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.40"`

## v0.1.39 — 2026-09-27

### Bugs fixed

- B-0058 A blocked Bug still gets its FIX row and a blocked item its CORRECT or ADJUDICATE row: the feeder reads blocked for Features only
- B-0062 A session that died twice asks the operator every tick instead of being held: the one dead end in the lane that is not a correction

### In progress

- F-0102 Deliveries: one plan, one agent, one gate for several small Features and Bug fixes across the backlog

### Improvements and hotfixes

- fix(lane): a refused merge asks the host whether the PR conflicts
- fix(quota): a stale reading at a stop holds to its reset; a limit death stops its account at once
- fix(ci_queue): the queue never cancels a trunk push run

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.39"`

## v0.1.38 — 2026-09-27

### In progress

- F-0003 P0c — seed this record with the factory itself
- F-0010 P3 wave 2 — evidence and ingest
- F-0013 P3 wave 5 — the feeder and the tick
- F-0014 P3 wave 6 — the CLI and the plugin
- F-0092 An item has a budget: three sessions or $10, then it stops and says why

### Improvements and hotfixes

- fix(lane): a merge the host refuses for conflicts goes BACK to be rebased, never waits
- fix(quota): a reading older than stale_after_min is stale — status says so, the wave ignores it
- fix(ci_queue): the queue's pass opens the held PRs the line admits

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.38"`

## v0.1.37 — 2026-09-27

### Features landed

- F-0075 Redaction is a gate on every write path: operator names and secrets never reach a record or a public repo

### In progress

- F-0024 Self-amendment policy: the factory proposes, humans approve and merge
- F-0037 Questions in batches: a non-blocking question goes to the groom; only a blocked launch or merge interrupts
- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it

### Improvements and hotfixes

- fix(feeder): judge the launch-time invariants before the cut, not after it
- fix(harvest): the capped gate takes the least recently gated first, so no branch starves
- feat(ci_queue): within a tier, the entry more feeder rows wait on goes first
- docs(ci_queue): reflow the order paragraph

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.37"`

## v0.1.36 — 2026-09-27

### In progress

- F-0010 P3 wave 2 — evidence and ingest
- F-0025 Typed envelopes: every job ends with a machine-readable report beside its prose
- F-0029 Roles: one file per role, five sections, model and access from config
- F-0092 An item has a budget: three sessions or $10, then it stops and says why

### Improvements and hotfixes

- fix(ci_queue): never re-run a run whose workflow changed on the trunk since

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.36"`

## v0.1.35 — 2026-09-27

### In progress

- F-0075 Redaction is a gate on every write path: operator names and secrets never reach a record or a public repo
- F-0113 An S1 silently holds all Feature work, and NEXT plans from different inputs than the tick

### Improvements and hotfixes

- fix(ci_queue): name no branch convention in the order's docstrings
- fix(ci_queue): a held re-run asks at its item's priority in the record now
- fix(ci_queue): runners busy without a job, and a broken trunk reservation escalates at once

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.35"`

## v0.1.34 — 2026-09-27

### In progress

- F-0003 P0c — seed this record with the factory itself
- F-0075 Redaction is a gate on every write path: operator names and secrets never reach a record or a public repo

### Improvements and hotfixes

- feat(lifecycle, feeder, briefs): adjudicate later and cheaper
- fix(briefs): an external-CI brief names the full-suite commands it must not run
- fix(tests): pin the clock in the on-prod ingest idempotence check

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.34"`

## v0.1.33 — 2026-09-26

### In progress

- F-0035 Thin controller: every read loop moves to the tick, and a rule flags scriptable chores

### Improvements and hotfixes

- fix(ci_queue): backfill — an entry that fits beside the head's claim starts
- fix(lane): a naming reword never costs a session

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.33"`

## v0.1.32 — 2026-09-26

### Features landed

- F-0090 Adjudication is the last resort: the cheap causes are ruled out before Opus is spent

### In progress

- F-0001 P0a — the two repositories and the licence
- F-0003 P0c — seed this record with the factory itself
- F-0102 Deliveries: one plan, one agent, one gate for several small Features and Bug fixes across the backlog

### Improvements and hotfixes

- fix(ci_queue, wave): the line drops what can no longer start; the local lane keeps to the share
- fix(ci_queue): the queue's pass runs on its own clock, every minute

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.32"`

## v0.1.31 — 2026-09-26

### In progress

- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0101 Corrections of mechanical failures run on Opus: 25% of all spend goes to correct/adjudicate/groom sessions

### Improvements and hotfixes

- fix(gitpush): ref-only factory pushes skip the product hook; every push has a timeout

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.31"`

## v0.1.30 — 2026-09-26

### In progress

- F-0040 Story-to-test gate: a Task's pull request must tick an acceptance line with the test that proves it
- F-0041 Small-Task trains: size class from the footprint, one train per lane, one CI run and one review per train
- F-0044 The retro's first line: features on production per week, and cost per feature
- F-0071 S1 lane: critical factory bugs pre-empt Feature work — tiers, reserved capacity, no ladder, a clock
- F-0103 The tick files Bugs from its own session outcomes and a stalled wave

### Improvements and hotfixes

- fix(ci_queue): head guard — the head of the line starts after head_wait_max_min at the head

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.30"`

## v0.1.29 — 2026-09-26

### In progress

- F-0030 The README carries the argument, the mental model and the manual on one page
- F-0161 Wave launch costs ~55 s per session: 5 launches make most of a 242 s wave

### Improvements and hotfixes

- fix(workers): reaped worktrees leave git at once and the disk in the background
- ci_queue: relief serves an S1 fix PR run like the trunk run
- ci_queue: S1 relief covers older runs ahead of it; a superseded S1 run hands its re-runs to the newer run
- fix(ci_queue): cancel a branch push run a PR run on the same sha covers
- ci_queue: relief is label-aware — a run is in a starved job's way only on runners carrying all its labels

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.29"`

## v0.1.28 — 2026-09-26

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push
- B-0135 An install during a running tick tears it: groom ImportError from a half-swapped package

### In progress

- F-0161 Wave launch costs ~55 s per session: 5 launches make most of a 242 s wave

### Improvements and hotfixes

- fix(lifecycle): an unpark resets the loop guard; a copies hold counts correct launches only

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.28"`

## v0.1.27 — 2026-09-26

### Bugs fixed

- B-0094 19% of sessions end 'not pushed': finished work is lost to a missing commit or push

### In progress

- F-0091 asf improve: the self-improvement pass is a tick step, not something someone remembers to ask for

### Improvements and hotfixes

- fix(upgrade): the drain counts only this install's ASF home — a test suite's ticks never hold the floor
- fix(upgrade): a killed asf upgrade --wait never keeps the ticks parked

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.27"`

## v0.1.26 — 2026-09-26

### Bugs fixed

- B-0056 A session whose branch is already on origin merges its stale remote: nothing in the factory publishes a rebased lane branch, and no session may force
- B-0123 A failed daily run is never retried until the next day

### In progress

- F-0111 hooks install from asf-live tells the operator to wire the dev install's path into the product's git hooks

### Improvements and hotfixes

- fix(lane): a ruling stands on the head it was launched on, not only the sha it names
- fix(lane): a check the CI queue cancelled to re-run waits, never sends the PR back
- fix(report): a note after superseded_by is no superseded claim
- fix(ci-queue): relief never cancels a run whose PR changes CI config
- fix(lane): a trunk-copies conflict is a rebase for a correct session — no round, never adjudicate
- fix(publish): a rebase off trunk copies is published, the old tip archived

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.26"`

## v0.1.25 — 2026-09-26

### Bugs fixed

- B-0128 An adjudicator's 'no further sessions' ruling does not stick: the lane re-adjudicates the item
- B-0136 An install can leave a product's tick clock unloaded; status just omits it

### Improvements and hotfixes

- fix(pool): a full wait names the full seats; quota and the share read the session-limit stop

### Upgrade

`pipx install --force "git+https://github.com/fmogensen/ASF.git@v0.1.25"`

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
