# ASF consolidated fix package: one branch, one release

Status: plan, 2026-09-24. Based on ASF `565fee1` (v0.1.2) and the record at `d259d08`.
Inputs: `inventory.md` in this scratchpad (the 52 hotfixes, the open cards, the root causes).
Nothing in the code or the record was changed to write this.

---

## 1. Summary

### What is broken at the root

- **The PR lane has no state machine.** A lane branch's life (pushed → PR → review → gate →
  merged | back) is spread over about 20 functions in `asf/harvest/harvest.py` (`land_ff`,
  `land_combined`/`land_set`/`land_group`, `land_pr`/`land_ready`/`gate_prs`/`merge_ready`,
  `request_review`, `close_merged`, `land_already`, `archive_superseded`), plus
  `asf/tick/step_prs.py` (opens PRs), `asf/tick/land_spec.py` (adopts branches) and
  `asf/harvest/pr_hygiene.py` (stale PRs). Each stage was added only after a stall showed it was
  missing (a3480e0 → 9658b54 → fc743e8 → cd53c4d). Fast-forward mode and pull-request mode are
  two separate code paths with two separate gates.
- **Several sources answer "is this item busy?", and they disagree.** The feeder
  (`asf/feeder/rows.py candidates`) combines `lifecycle.inflight`, `awaiting_harvest`,
  `unlanded` (with its `harvest: 'pr'` marker), `corrections` (in which `REVIEW_WANTED` stands in
  for a review request) and `step_wave.open_pr_branches` (a cached `prs.json`). These sources
  update at different moments. In the gaps between them, rows are duplicated (a second session)
  or held forever.
- **Record writers do not validate what they write.** Ingest (`_ingest_fields`), the widen pass
  (`tick/widen_footprint.py`), groom/set and derived backlinks (`record/core.py
  backlinks_lines`) each rewrite parts of a card. None of them checks its output against
  `asf check` before committing, so the first sign of a bad write is a record that refuses every
  commit (ccffe94, 7554942, 1fe49c1 → 6f91f78).
- **Facts are read from text, not from what changed.** Three review readers disagree:
  `harvest.review_verdict` uses `conventions.review_pattern` (`{reviews_dir}/{n}-{slug}.md`),
  `evidence.rx_review` hard-codes `<slug>-review-r<n>.md`, and `pr_hygiene.parse_verdict` is a
  third parser. There are four `gh pr list` readers (`PrLane.__init__`, `step_prs.has_open_pr`,
  `pr_hygiene.fetch_open_prs`, `evidence.pr_list`). Plan approval and landing were derived from
  commit subjects (9ec5d63 → f733645 → 8460f4e).
- **A worker session inherits the whole machine.** `workers/runtime.py build_env` →
  `hermetic.build` removes only git-hook and caller-identity variables from `os.environ` and
  keeps the operator's `HOME`: every CLI login, keychain credential and live cloud or payments
  context (`docs/guide/operating.md` "Safety"). No card owns the fix.

### What the package delivers (release: the next patch after the newest tag at merge time —
v0.1.6 as of this revision, main having since tagged v0.1.3 through v0.1.5; see §6)

1. One PR-lane state machine in a new `asf/harvest/lane.py` with its own append-only ledger
   (`lanes.jsonl`). Both landing modes use it, with a single gate and a single review reader
   (`asf/evidence/review.py`).
2. One answer to "is this item busy?", `lifecycle.occupancy()`. The feeder reads it and nothing
   else.
3. A registry of executable invariants, `asf/invariants.py`. The tick runs it before every
   record commit, after the feeder and after harvest. `asf check --invariants` and the tests
   assert the same checks.
4. An end-to-end harness, `tests/test_e2e_lane.py`: a sample product in PR mode with a fake `gh`
   and scripted sessions, driven from card to merge.
5. Worker environment isolation, done first: an allow-listed environment and a per-account home
   by default.
6. Card hygiene: 14 items that are fixed but still open get closed, and the package closes 17
   more by evidence.

---

## 2. The PR-lane lifecycle as one state machine

The owner is **`asf/harvest/lane.py`** (new). `harvest.run_product_harvest` shrinks to: fetch,
list lane branches, call `lane.advance(product, branch, facts)` for each branch, then run one
gate over every branch in `GATE`. No other module writes a lane state. Everything else reads it
through `lane.state(branch)`, `lane.busy_items()` and `lane.snapshot()`.

**Ledger.** `state/<product>/lanes.jsonl` is append-only. There is one line per transition:
`{branch, item, head, state, fact, pr, at, reason}`. A branch's state is its last line.
`sessions.jsonl` keeps runs, and its format is unchanged. Lane transitions that finish a run
still write `harvested:` or `correction:` on the run through `lifecycle.hold` and
`mark_session`, so a v0.1.2 binary reading the same state directory still works (§6 rollback).

**Facts.** Each tick gathers facts once, in `lane.facts(product)`:

- one `gh pr list --state all` (this replaces the four readers above);
- one `git ls-remote`;
- the run per branch (`lifecycle.by_branch`);
- the review file per branch head (`review.newest(branch, item)`);
- the gate cache.

Every transition is a pure function of these facts: `lane.next_state(prev, facts) -> (state,
reason)`. It has unit tests with no git and no `gh`.

### States and transitions

| # | from → to | trigger fact | owner (single writer) | ledger / record writes |
| --- | --- | --- | --- | --- |
| T1 | (run) → **PUSHED** | health's `ended finished` line (`lifecycle.judge`): result ok, `origin/<branch>` == worktree HEAD | `workers/health.py` writes the run's `ended` line; `lane.advance` sees it | lanes: `PUSHED head=<sha>`. record: none |
| T2 | PUSHED → **PR_OPEN** | PR mode: `gh pr create` succeeded, or an open PR with `headRefName == branch` already exists (the pre-existing-PR case, adoption). FF mode: immediately, with `pr=null` (the branch is its own "PR") | `lane.py` (`step_prs.run` is removed; `land_spec.adopt` becomes `lane.adopt`) | lanes: `PR_OPEN pr=<n>\|null`. record: ingest links the PR |
| T3 | PR_OPEN → **REVIEW** | the review policy for the landing class (`conventions.lane.review: {docs: none, code: required}`) requires a review, and `review.newest()` has no file whose round reviewed this head | `lane.py` | lanes: `REVIEW round=<n> wanted`. The feeder emits `PUSHED → REVIEW` from the lane state, not from a fake `REVIEW_WANTED` correction |
| T4 | REVIEW → **GATE** | the current review for the head reads `verdict: approved`, or the policy is `none` (docs, or FF mode without a review requirement) | `lane.py` | lanes: `GATE`. record: the approval stage comes from `review.py`, the same reader |
| T5 | REVIEW → **BACK** | the current review reads `changes…` | `lane.py` → `lifecycle.hold(kind='review')` | lanes: `BACK kind=review`. run: `correction`, `rounds+1` |
| T6 | GATE → **WAITING_CI** | `landing_checks_missing: wait` for this class, and required checks are pending or absent for less than `landing_checks_wait_min` | `lane.py` | lanes: `WAITING_CI since=<first-seen>` (this replaces `landing-missing-since.json`) |
| T7 | WAITING_CI → GATE | the required checks pass (→ merge on CI), or the wait expired (→ local gate) | `lane.py` | lanes: `GATE via=ci\|local` |
| T8 | GATE → **WAITING** | the trunk alone is red (`TrunkRed`); no merge budget left (`PrLane.slots`); deferred ("green alone, one merges ahead") | `lane.py` | lanes: `WAITING reason=trunk-red\|budget\|deferred`. Never a correction, never a round |
| T9 | GATE → **BACK** | the branch is red alone on a green trunk, conflicts, a footprint rule decided `reshape`, or a pre-push hook refused (with the hook's output) | `lane.py` → `lifecycle.hold(kind=gate\|conflict\|footprint\|hook)` | lanes: `BACK kind=…`. run: `correction`. At `ROUND_CAP` the feeder gives STALEMATE → ADJUDICATE |
| T10 | GATE → **MERGED** | the combined head is green (or its CI passed) and the merge succeeded. FF: `push_ff` and the sha is on `origin/<trunk>`. PR: `gh pr merge` (squash first) or `--auto` into a merge queue, then observed merged | `lane.py` via the host adapter | lanes: `MERGED sha=<merge sha> method=ff\|squash\|merge\|rebase\|queue`. run: `harvested=<sha>`. record: ingest derives landed from **this** fact (item ↔ merge sha), never from ancestry or a subject line. That is how the squash case works |
| T11 | any open state → MERGED | the PR was found merged outside the lane (a human, a merge queue, a product batch step), or the branch's diff is already on the trunk (`already_on_trunk`) | `lane.py` | as T10, `method=external\|on-trunk` |
| T12 | any open state → **STALE** | the PR was closed unmerged; the branch is gone with no merge; the item is removed or done (`lifecycle.closed_state`); the item is superseded; or the head has not changed and no session has held it for longer than `lane.stale_after` | `lane.py` (absorbs `pr_hygiene.classify`/`close_row` and `archive_superseded`) | lanes: `STALE reason=…`. run: `harvested=superseded` when archived. record: the feeder's `STALE → CLOSE` row only when unlanded work exists |
| T13 | BACK → PUSHED | the correcting session ended `finished` with a new head | health, then `lane.advance` | lanes: `PUSHED head=<new>` |
| T14 | MERGED/STALE → REAPED | the worktree has been removed (`lifecycle.reap_verdict`) | `workers/health.py` | lanes: terminal |

Every state except REAPED has an outgoing transition with a time bound. WAITING and
WAITING_CI are re-decided every tick. Any state other than MERGED/STALE/REAPED that is older
than `lane.stale_after` is reported by invariant **I8** (§3).

### One path for both landing modes

`lane.py` uses a small host interface, and `landing(product)` only picks the adapter:

```
class Host:            # asf/harvest/lane.py
    def open(branch) -> pr | None      # FF: None (T2 passes at once)
    def status(branch) -> PrFacts      # FF: {checks: none, merged: on-trunk?}
    def merge(branch, pr) -> (sha, method) | refusal   # FF: push_ff; PR: gh pr merge / --auto
class FastForwardHost(Host) ...
class GitHubHost(Host) ...
```

- **One gate:** `lane.gate_set(entries)`. It builds a combined head, bisects on the red modules,
  confirms in full, and checks the trunk once when something is red. This is today's
  `confirmed_group` logic, used by both modes. It replaces `land_ff`, `land_combined`,
  `land_set`, `land_group` and `gate_prs`. `harvest_gate: per-branch` becomes a set of size 1.
- **The gate is cached** by `(branch head, trunk code fingerprint)`. The fingerprint is the tree
  hash of `origin/<trunk>` with `conventions.doc_paths` and the docs dirs removed. A trunk that
  moved only by docs commits does not re-gate a green branch. That closes the "re-gates when
  main moved only by docs" card and removes the "moved again on retry" livelock.
- **Docs vs code** is decided by one rule: `lane.landing_class(files)`. The docs roots are
  `specs_dir`, `plans_dir`, `reviews_dir` plus `conventions.doc_paths`. This replaces
  `is_docs_branch`, `is_inert` and `docs_only` (harvest) and `evidence.docs_only`/`doc_dirs`.
- **`conventions.shared_paths`** (lockfiles) are excluded from footprint overlap in the feeder
  (W3). In the lane they are serialised at merge: at most one branch per tick touching a shared
  path enters `gate_set`, and the others get WAITING `reason=shared-path`.

### Scattered rules this replaces

| today | where | becomes |
| --- | --- | --- |
| `harvest: 'pr'` run marker | `harvest.run_product_harvest`, `lifecycle.eligible`, `lifecycle.unlanded` (`PR_WAIT`) | lane state PR_OPEN.. |
| PR opening, `has_open_pr`, `candidates` | `tick/step_prs.py` | T2 |
| `land_spec.adopt`, `why_not_as_is`, `APPROVED → LAND` row | `tick/land_spec.py`, `rows.land_spec_row` | `lane.adopt` (T2 from an existing branch/PR) |
| `request_review` writing a `REVIEW_WANTED` *correction* | `harvest.py` | T3 (a state, not a correction; no round is spent) |
| `review_verdict`, `review_round`, `review_is_current`; `rx_review`/`newest_review`/`verdict_of`; `parse_verdict`/`branch_verdict` | harvest, evidence, pr_hygiene | `asf/evidence/review.py`: one contract, `conventions.review_pattern`, for spec, plan and code (closes the spec-plan-approval card) |
| `land_pr` → `land_ready` → `gate_prs` → `merge_ready`; `land_ff`; `land_combined`/`land_set`/`land_group` | harvest | T4–T10, `gate_set`, `Host.merge` |
| `missing_since`/`forget_missing` (`landing-missing-since.json`) | harvest | WAITING_CI ledger line |
| `send_back` docs/code split (`LANDING_GATE` vs `hold_with_correction`) | harvest | T9 with `kind`. The feeder routes by the branch kind |
| `close_merged`, `land_already`, `merged_pr` | harvest | T11 |
| `archive_superseded`, `pr_hygiene.classify`/`close_row` | harvest, pr_hygiene | T12 |
| `step_wave.open_pr_branches` (cached `prs.json`), `rows._waiting_doc` | wave, feeder | `lane.busy_items()` inside `lifecycle.occupancy()` |

---

## 3. Invariants with checks

These live in the new `asf/invariants.py`: `INVARIANTS = [Invariant(id, scope, check)]`. Each
`check(ctx) -> [Finding]` is pure over the facts it is given.

They run at three points in the tick (`asf/tick/tick.py`):

- **record**: over the staged tree before `commit_and_push`. A violation refuses the write of the
  offending writer, not the whole commit. The rest of the tick commits, and one Bug is filed per
  invariant id (not one per tick), through `tick/file_bugs.py`.
- **feeder**: over `plan_rows` output before the wave. A violating row is dropped and logged as
  `INVARIANT <id>: <row>`.
- **lane**: after harvest.

`asf check --invariants` runs them all read-only against the record and state directory.
`tests/test_invariants.py` has one positive case and one negative case per invariant. The e2e
harness asserts `invariants.run(ctx) == []` after every tick.

| id | invariant (broken today by) | check | scope |
| --- | --- | --- | --- |
| I1 | **Never strip machine keys.** A writer may drop only the derivable keys `stage`, `evidence`, `blocked`, `blocked_by_open`. Any other key present before is present after (ccffe94 lost `schema_version`) | diff the `frontmatter.machine_keys` of each card before and after, per writer | record |
| I2 | Every card carries the record's `schema_version` (`schema.record_version`) | compare the stamp per card | record |
| I3 | **Never write intersecting `writes:`.** No two open Tasks' `writes:` intersect (`core.writes_intersect`), and every writer (widen, set, plan_tasks) is refused before commit, not after (7554942) | the existing pair check in `check.py` (the `writes_intersect` loop), lifted into I3 and run over the staged tree | record |
| I4 | **No launch row for a busy item**: an open PR, a live run, or a lane state in PUSHED..MERGED except BACK (e74b41b, cd53c4d) | `{r.item_id for r in rows if r.launches} ∩ occupancy.busy == ∅` | feeder |
| I5 | **No code row without the spec (and plan) on the trunk.** PLAN → CODE / BUG → FIX for a Feature's Task requires `origin/<trunk>:<spec path>` to exist (f733645, 8460f4e) | `git cat-file -e` batch over the rows' Features | feeder |
| I6 | **Every derived field can be re-derived.** Ingesting twice is a no-op, and stripping every card's machine block and re-ingesting gives the same `state`/`stage`/`evidence` (except `updated`, `stage_since`) | run `ingest` into a temp copy and compare | record (tests, and `asf check --invariants --deep`; too slow for every tick, run daily) |
| I7 | One session per branch and per worktree, with paths case-folded (2fdbe0e) | group the occupying runs by `lifecycle.path_key(worktree)` and by branch | feeder |
| I8 | **Every lane branch is in exactly one state, and no open state is older than `lane.stale_after` without a reason**. This makes silent waits visible | fold `lanes.jsonl` | lane |
| I9 | **One controller per product.** One tick lock holder, and no foreign merger (a PR merged by `method=external` while a lane owned it is reported) | `tick.Locks` and T11 lines | lane |
| I10 | A Feature is Resolved only when all its Tasks are closed, and a merged docs PR never marks a Feature landed (9ec5d63) | walk the index | record |
| I11 | Derived text (Backlinks, Children) never carries a token the redaction gate refuses (the derived-backlinks card) | `redact.scan` over the derived sections of the staged cards | record |
| I12 | Release notes list under "landed" only items whose derived state is Resolved or Closed. The v0.1.2 `CHANGELOG.md` lists F-0026 (plan-approved), F-0039 (2/7) and F-0095 (1/4) as landed, because `metrics.release_items` counts any merged PR that links the item | the item state for every id in the note's landed sections | record (release) |
| I13 | An explicit `type:` line in an inbox card decides the minted type (the intake type card) | intake result type == declared type | record |

---

## 4. End-to-end harness

**Module:** `tests/test_e2e_lane.py`. It drives the real `python -m asf.cli tick` as a
subprocess under `hermetic.build` with a temporary `ASF_HOME`, like `tests/test_sample_product.py`.
It has no network, no account and no agent. The time budget is under 90 s, so it runs in the
ordinary suite (`tools/run_tests.py`).

**Fixture layout:**

```
tests/e2e/
  product/                    # the sample product in PR mode (generic names only)
    product.yaml              # landing: pull-request, review_pattern, doc_paths, shared_paths,
                              # lane.review {docs: none, code: required}, test command
    config.yaml               # worker_pool.backend: e2e (the ScriptedRuntime), one account, home: <tmp>
    repo/  src/ tests/ specs/ plans/ reviews/ uv.lock (a shared path)
    record/ epics/E-0001.md features/F-0001.md (decided card) index.json
  scripts/                    # one JSON per brief kind; a step = {writes:{path:text}, commit, push, report}
    spec.json spec-review.json plan.json plan-review.json coder.json review.json correct.json
  fakes/
    gh                        # executable, first on PATH: a Python gh over fakes/state.json —
                              # pr create/list/view/checks/merge (--squash|--merge|--rebase|--auto),
                              # api graphql mergeQueue, api branch protection; the merge performs
                              # a real squash commit on the bare origin
    session.py                # ScriptedRuntime(FakeRuntime): runs the script step in the job's
                              # worktree (write, git commit, git push), then writes REPORT + result
  factory.py                  # Factory: setup(tmp) → bare origins; tick(); gh(...) to move the
                              # forge (CI done, a human merge, a close); snapshot() → Snapshot
```

`Snapshot` holds `ledger` (folded `sessions.jsonl` plus `lanes.jsonl`), `record` (`index.json`
state/stage/evidence per id) and `rows` (`asf next --json`). Every test runs
`assert_tick(snap, lanes=…, record=…, rows=…, launched=…)` and `invariants.run() == []` after
every tick.

**Scenarios** (each also runs as a subTest under `landing: fast-forward`, so both modes are
tested through the same machine):

1. **Happy path**, card → merge, about 12 ticks: CARD → SPEC → spec PR (docs, no review) →
   MERGED → `spec-approved` → PLAN → plan PR → MERGED → Tasks minted with `after:` → coder →
   PUSHED → PR_OPEN → REVIEW (a review session launched once) → GATE → MERGED (squash) → Task
   Resolved → Feature Resolved. It asserts no tick launches two sessions for one item (I4).
2. **Red trunk:** a direct push turns the trunk red. The PR in GATE goes to WAITING
   `trunk-red`: no correction, no round, no Bug against the branch. The trunk is fixed and the
   PR merges the next tick.
3. **Footprint widening:** the coder's REPORT says `needs writes: src/b.py`. The widen pass
   widens → correct → merge. A variant: the path overlaps an open sibling's `writes:`, so the
   Task waits and the record never holds intersecting writes (I3). A second widening →
   RESHAPE.
4. **Squash merge:** the merge sha is not a descendant of the branch head. The run is
   `harvested=<squash sha>`, the Task is Resolved by the lane fact, and a Feature with an open
   Task stays open (I10).
5. **Pre-existing PR:** `fakes/gh` already has an open PR on `feature/f-0001-t1` before the
   first tick. The lane adopts it at T2 (no second PR, no second coder), reviews it and merges
   it.
6. **Docs-only trunk move:** a green branch in GATE is not gated again when only `specs/` moved.
   The gate cache hits, and the harness counts gate invocations.
7. **Record integrity:** a card with an unknown machine key and a `schema_version` survives 12
   ticks byte-identical in those keys (I1, I2).

---

## 5. Workstreams

Each workstream owns its files, and the file sets do not overlap. Only W0 touches the shared
contracts (config keys, interface stubs). The docs pass is W0's closing commit. This is what
stops a repeat of today's 10-and-10 churn on `harvest.py` and `rows.py`.

### W6: Worker and process isolation (**safety first**, merges first). Size M, about 6 h

- **Scope:**
  - `hermetic.build(..., mode='worker')` builds an allow-listed environment (`PATH`, `LANG`,
    `LC_*`, `TERM`, `TMPDIR`, `USER`, `SHELL`, plus `worker_pool.env_passthrough`) instead of
    `os.environ` minus a deny-list.
  - `HOME` defaults to a per-account home under the state directory (`state/homes/<account>`),
    seeded only with what `worker_pool.accounts[].home_seed` lists. It is never the operator's
    `HOME` unless the account says `home: inherit`.
  - The doctor gains a red row, `worker env`: any account on `home: inherit`, or any passthrough
    variable that matches a credential pattern.
  - The worktree setup command (`conventions.worktree_setup`) runs in every fresh worktree in
    `spawn.py`, under the worker environment.
  - The scheduled tick runs from a snapshot (a `git worktree` of the last release tag, or the
    trunk sha, under `state/snapshot/<sha>`) and never from the live dev checkout.
    `RunAtLoad: False` for calendar clocks.
  - The `asf init` pre-commit is recognised as ASF's own (not foreign).
  - `install.sh` step 1 is guarded, and step 4 under cron is reported as expected.
- **Owns:** `asf/hermetic.py`, `asf/workers/runtime.py`, `asf/workers/spawn.py`,
  `asf/workers/githooks.py`, `asf/doctor.py`, `asf/scheduler.py`, `asf/init.py`,
  `tools/install.sh`, `tests/test_hermetic.py`, `test_session_identity.py`, `test_doctor.py`,
  `test_scheduler.py`, `test_install.py`, `test_env.py`.
- **Depends on:** W0's keys.
- **Accept:**
  - A worker spawned from a tick with `FAKE_SECRET=x` and an operator `HOME` sees neither, and
    the test asserts the child's `env`.
  - The gate environment (`harvest.gate_env`) stays deny-list, since a product's tests may need
    the machine's tools.
  - A tick started while the checkout is mid-rebase imports from the snapshot.

### W1: The PR-lane state machine. Size L, about 12 h

- **Scope:** §2 in full.
  - `lane.py` (facts, `next_state`, `advance`, `gate_set`, `Host` adapters, adopt, stale).
  - `evidence/review.py`, the one review reader.
  - `harvest.py` reduced to its entry point, rebase/resolve and `product_gate`.
  - A bootstrap on first run: `lanes.jsonl` is seeded from `sessions.jsonl` and the open PRs
    (every `harvest: 'pr'` run → PR_OPEN). It is idempotent and needs no hand edit.
  - Pre-push hook refusals are carried as `kind=hook` with the hook's output.
- **Owns:** `asf/harvest/harvest.py`, `asf/harvest/lane.py`, `asf/harvest/pr_hygiene.py`,
  `asf/evidence/review.py`, `asf/tick/step_prs.py`, `asf/tick/land_spec.py`,
  `asf/tick/step_harvest.py`, `tests/test_harvest.py`, `tests/test_lane.py` (new, the pure
  `next_state` table), `test_pr_hygiene.py`, `test_land_spec.py`.
- **Depends on:** W0 (the stub API and keys).
- **Accept:**
  - Every row of the §2 table has a `next_state` unit test.
  - The e2e scenarios 1, 2, 4, 5 and 6 pass in both modes.
  - `harvest.py` has fewer than 900 lines, with no `land_ff`/`land_pr`/`gate_prs` left.

### W2: Record integrity and evidence. Size M, about 7 h

- **Scope:**
  - Ingest merges into the machine block and never rebuilds it (I1).
  - `evidence.py` calls `review.py` for spec/plan approval, and derives landed from the lane's
    MERGED fact (item ↔ merge sha), not from subjects.
  - Date-prefixed plans mint Tasks (F-0123).
  - Derived Backlinks/Children are passed through `redact`.
  - The intake honours `type:`.
  - The widen pass validates `writes:` against I3 before its commit.
  - The flow-style YAML map in the config reader is parsed or refused.
- **Owns:** `asf/record/ingest.py`, `frontmatter.py`, `core.py`, `setfield.py`,
  `plan_tasks.py`, `asf/evidence/evidence.py`, `closing.py`, `asf/tick/widen_footprint.py`,
  `asf/feeder/widen.py`, `asf/groom/inbox.py`, `asf/env.py` (the config reader), and the tests
  `test_ingest.py`, `test_evidence.py`, `test_closing.py`, `test_widen.py`,
  `test_doc_lane_landing.py`, `test_plan_tasks.py`, `test_inbox_shape.py`,
  `test_schema_version.py`.
- **Depends on:** W0, and W1's `review.py` signature (a stub from W0).
- **Accept:**
  - I1, I2, I3, I10, I11 and I13 pass on a copy of the real ASF record and the sample.
  - e2e scenarios 3 and 7 pass.

### W3: Feeder and occupancy. Size M, about 7 h

- **Scope:**
  - `lifecycle.occupancy(path, lanes, alive) -> {busy, waiting_landing, corrections}` is the one
    "latest run per item" answer. It folds `inflight`, `awaiting_harvest`, `unlanded` and
    `lane.busy_items()`.
  - `rows.candidates` takes `occupancy` in place of the five separate arguments.
  - `PUSHED → REVIEW` and `PUSHED → LAND` rows come from lane states.
  - `footprint.overlaps` ignores `conventions.shared_paths`.
  - `lifecycle.outcome_class` classifies a hook refusal and a transport error on push (B-0097)
    as their own classes: a retry with no round spent.
  - The DONE table credits only a run with commits of its own.
- **Owns:** `asf/feeder/rows.py`, `tiers.py`, `footprint.py`, `asf/workers/lifecycle.py`,
  `asf/workers/health.py`, `asf/workers/report.py`, `asf/tick/step_wave.py`,
  `asf/workers/wave.py`, `asf/tick/summary.py`, `tests/test_feeder.py`, `test_lifecycle.py`,
  `test_workers.py`, `test_pushed_not_starved.py`, `test_dead_run_load.py`,
  `test_tick_summary.py`.
- **Depends on:** W0 (`lane.busy_items` stub). It is integrated after W1.
- **Accept:**
  - I4, I5 and I7 hold in every e2e tick.
  - `rows.py` has no reference to `open_branches`, `unlanded`, `REVIEW_WANTED` or `LAND_SPEC`.

### W4: Invariants, `asf check`, tick wiring, release honesty. Size M, about 5 h

- **Scope:**
  - `asf/invariants.py` (I1–I13 registry).
  - `check.py` runs it (`--invariants`, `--deep`).
  - The tick's three check points, and filing one Bug per invariant id.
  - `metrics.release_items`/`render_release` split each note into "landed" (Resolved/Closed)
    and "progress" (I12).
  - The tick prints a start line per step (the tick-logs card).
  - `steps.daily_due` compares the stamp's owner as well as its date, and `asf init` writes
    `approvals.groom: auto` (the groom-commits card, items 2–3).
  - The status Prod row reads `ci.deploy_sha_workflow`.
- **Owns:** `asf/invariants.py`, `asf/record/check.py`, `asf/tick/tick.py`,
  `asf/tick/steps.py`, `asf/tick/file_bugs.py`, `asf/cli.py`, `asf/metrics/metrics.py`,
  `asf/views/status.py`, `tests/test_invariants.py`, `test_metrics.py`, `test_tick.py`,
  `test_tick_steps.py`, `test_views.py`.
- **Depends on:** W2 (the ingest dry-run hook), and the W1/W3 read APIs (stubs).
- **Accept:**
  - `asf check --invariants` on a copy of the real record lists exactly the known violations
    before the package and none after.
  - A seeded I3 violation is refused before commit, and the rest of the tick commits.

### W5: The end-to-end harness. Size M-L, about 8 h

- **Scope:** §4.
  - Start on day 0 against the *current* code. Scenarios that fail today are marked
    `expectedFailure` with the card they prove, and each workstream flips its own scenarios to
    hard.
  - The fake `gh` becomes the one `gh` stub for the suite (it replaces
    `test_harvest.fake_gh`-style patching over time, not in this package).
- **Owns:** `tests/e2e/**`, `tests/test_e2e_lane.py`, `tests/gitfixture.py` (additions only).
- **Depends on:** nothing to start. Green needs all the other workstreams.
- **Accept:**
  - All seven scenarios pass in both modes, in under 90 s, on the integration branch.

### W0: Contracts and integration (the integrator). Size S, about 3 h total

- **Opening commit:**
  - New config keys in `asf/schema.py`, `asf/conventions.py` and
    `docs/products.example.yaml`: `doc_paths`, `shared_paths`, `lane.review`,
    `lane.stale_after`, `worktree_setup`, `worker_pool.env_passthrough`, `home_seed`.
  - Stub modules with final signatures and `NotImplementedError` bodies: `lane.py`
    (`state`, `busy_items`, `snapshot`), `review.py` (`newest`) and `invariants.py` (the
    registry).
- **Closing commit:** `docs/guide/*` updated once against the merged code: `operating.md`
  Safety and groom, `troubleshooting.md` PR lane, `product-config.md` new keys.
- **Owns:** those files and `CHANGELOG.md`.

---

## 6. Sequencing and delivery

- **Integration branch:** `integration/fix-package-0.1.3`, cut from `main` at `565fee1`. It
  must match none of `conventions.branch_prefixes`, so ASF's own harvest ignores it. Each
  workstream works in its own worktree on `integration/fix-package-0.1.3-wN` and merges into the
  integration branch with `--no-ff`.
- **Where the work runs:** the factory is the thing being changed and the lane is the broken
  part, so these workstreams run as operator-directed sessions on the integration branch, not
  through ASF's own pool.
- **Hotfix freeze on `main`** from the cut to the release. The only exception is an S1 that
  stops the factory. That hotfix names its card and is forward-ported onto the integration
  branch within the hour. The integration branch rebases on `main` once a day.

| order | when | merge | gate at that point |
| --- | --- | --- | --- |
| 1 | Thu 24 evening | W0 opening, then W5 skeleton (xfails recorded) | full suite green |
| 2 | Fri 25 AM | **W6** (safety first) | suite, `check_generic.sh`, e2e (xfails unchanged) |
| 3 | Fri 25 | W2 | suite, e2e scenarios 3 and 7 hard |
| 4 | Fri 25 PM | W1 | suite, e2e 1, 2, 4, 5 and 6 hard in both modes |
| 5 | Sat 26 AM | W3 | suite, e2e fully hard |
| 6 | Sat 26 | W4, then W0 closing (docs, CHANGELOG) | the package gate below |

W1, W2, W3 and W6 are developed in parallel from Friday morning, because their files do not
overlap. The order above is the merge order only.

### Package gate (all of these must pass before the tag)

1. `python3 tools/run_tests.py` is green, plus `tools/check_generic.sh` and
   `tools/check_conventions.sh`. No product names or customer paths appear anywhere: the
   fixtures use `sample`-style names only.
2. `tests/test_e2e_lane.py`: all scenarios, both modes, no `expectedFailure` left.
3. Invariants on real data:
   - `asf check --invariants --deep` on a fresh copy of the ASF record (`ASF-backlog`), and on a
     copy of each live product's record and state directory taken under the operator's
     `ASF_HOME`. These copies are never committed.
   - Then `asf tick --dry-run` against each copy. It must print a lane state for every open
     branch and PR, no `INVARIANT` line, and no launch row for an item with an open PR.
   - A second dry-run must print the same transitions (idempotence).
4. The ledger bootstrap is run against a copy of the real `sessions.jsonl` (every `harvest: 'pr'`
   run maps to one lane state). Then the v0.1.2 binary is run against the same state directory
   to prove rollback works.

### Release

`metrics.next_tag` takes its line from `roadmap_line(items)`: the lowest-ranked open Epic whose
title carries a version. That is **E-0001 "ASF 0.1 — a working factory"** (rank 1, Active), so
the line is 0.1 and the release is **the next patch after the newest `v0.1.x` tag on `main` at
merge time** — the same "highest so far plus one" rule `asf upgrade` documents
(`docs/guide/upgrading.md`). The plan was written against `565fee1` (v0.1.2); `main` has since
cut v0.1.3, v0.1.4 and v0.1.5 with unrelated hotfixes, so as of this revision the package release
is **v0.1.6**. A later hotfix before the merge moves this by the same rule — never hand-set. A
`v0.2.0-rc` is not possible for two reasons:

- `SEMVER_TAG_RE` (`^v(\d+)\.(\d+)\.(\d+)$`) has no pre-release form.
- Reaching 0.2 would mean closing E-0001 by hand, which the "fix the software, not the state"
  principle forbids.

The package is fixes on the 0.1 line. v0.2.0 comes when E-0001 closes by derivation.

The release is cut by the normal `release_min_interval` path on the merge commit of
`integration/fix-package-0.1.3` into `main`. That is a single `--no-ff` merge, so the release
is one sha.

### Rollout (the customer upgrades once)

1. Pause the product's clock: `asf` scheduler off (the doctor shows it).
2. Run `bash tools/install.sh <product> <the release tag — v0.1.6 as of this revision>`. It is
   pinned and idempotent.
3. Run `asf doctor`. The new `worker env` row must be green: the account homes are created and
   seeded by the installer's step 3.
4. Run `asf check --invariants`, then `asf tick --dry-run`. Read the lane table: every in-flight
   PR must show a state.
5. Resume the clock. The first real tick writes the `lanes.jsonl` bootstrap.

**Rollback:** `install.sh <product> <tag>` with the newest tag on `main` from before the package
merged — as of this revision, `v0.1.5`. Not the package's own preceding minor release (v0.1.2): that release
refuses the top-level `feeder:` key, and the upgrade's first hour runs with `feeder.hold`. Every
tag from v0.1.3 on already reads `feeder.hold` (`e284ee7`, in `main` since before v0.1.3), so the
newest pre-package tag is always a safe rollback target — recompute it the same way the release
version is computed, never hand-set. `sessions.jsonl` is unchanged in format, and `lanes.jsonl` is
ignored by every one of them.

---

## 7. Card hygiene

### Already fixed in code: close now, citing the sha

Close these through `asf set` or a groom `done` move, which commit and push since 354e2f6. This
is the record's normal write path, not a hand edit.

| card / item | fixed by |
| --- | --- |
| inbox `native-pr-landing-merge-an-asf-opened-pr-when-approved-and-green-…` | 9658b54 (+ fc743e8, cd53c4d) |
| inbox `briefs-spec-plan-templates-don-t-state-the-commit-subject-rule-…` | 1e51864 |
| inbox `feeder-a-pushed-plan-spec-awaiting-its-pr-is-relaunched-as-starved-…` | e74b41b |
| inbox `check-bare-decision-reference-fires-inside-fenced-code-…` | 1fe49c1 |
| inbox `native-landing-a-merged-spec-plan-pr-marks-its-feature-landed-resolved-…` | 9ec5d63 |
| inbox `a-removed-or-moved-feature-still-shows-a-derived-stage-…` | dbb478e |
| inbox `asf-groom-asf-set-write-the-checkout-and-never-push-…` | 354e2f6 |
| inbox `status-a-session-that-exited-after-a-done-report-shows-as-dead` | e74b41b |
| B-0096 | 65f7dc7 |
| B-0099 | abfd93a |
| B-0100 | 9214303 |
| B-0101 | 9214303 |
| F-0124 | 6f91f78 |
| F-0126 | d82d0f8 |

The package closes these as well:

- F-0123: its Task minting is verified by W2 and e2e scenario 1.
- The groom-commits card: item 1 is fixed by 354e2f6, and items 2–3 by W4.

### Closed by the package (each commit names its card, so closing happens by evidence)

- **W1:**
  - `spec-plan-approval-ignores-review-pattern-…`
  - `harvest-re-gates-a-green-code-branch-when-main-moved-only-by-docs-commits`
  - `conventions-doc-paths-…`
  - `conventions-shared-paths-lock-files-…` (with W3)
  - `a-push-refused-by-the-repo-s-pre-push-hook-…` (with W3)
  - F-0125, the adoption part only (a pre-existing PR is adopted). The legacy review form stays
    open.
- **W2:**
  - `derived-backlinks-copy-a-protected-name-…`
  - `config-reader-a-yaml-flow-style-map-…`
  - `intake-silently-ignores-an-explicit-type-line-…`
- **W3:**
  - B-0097 (network blip on push)
  - `done-table-credits-landed-sha-to-a-run-with-no-commits-of-its-own`
  - F-0098's remaining acceptance (dead vs finished) is re-checked and closed if met.
- **W4:**
  - `groom-commits-its-own-output-…` (items 2–3)
  - `tick-logs-stay-empty-while-steps-run-…`
  - `status-the-prod-row-reads-ci-deploy-workflow-…`, and B-0093 if it is the same defect
- **W6:**
  - `a-product-declares-a-worktree-setup-command-…`
  - `the-factory-s-own-clock-imports-from-the-live-dev-checkout-…`
  - `scheduler-install-runs-the-daily-clock-at-load-hours-…`
  - `asf-init-writes-a-pre-commit-that-doctor-then-calls-foreign-…`
  - `install-sh-step-1-failure-exits-with-pipx-s-code-…`
  - `install-sh-reports-step-4-failed-for-scheduler-kind-cron-…`
  - The worker-env part of `connectors-declare-each-external-service-…`: default deny. The card
    stays open, narrowed to per-service connectors.

**Rule going forward:** a fix commit names its card or item in its subject or a `Fixes:`
trailer. A hotfix without one is refused by the `check_conventions` gate. Otherwise the record
keeps lagging the code: 14 items are open today only because their fixes named no card.

---

## 8. Out of scope (deferred until after Sat 2026-09-26)

These are features, or fixes that do not touch the lane, the feeder, record integrity or
isolation:

- `predictive-capacity-place-a-launch-on-the-account-that-can-finish-it-…`
- `pause-one-product-…`
- `approvals-decide-feature-auto-…`
- `asf-collects-trunk-ci-runs-itself-…`
- `classify-every-cancelled-ci-job-…`
- `asf-config-check-migrate-an-old-or-hand-made-product-yaml-…`
- `the-landing-gate-refuses-a-branch-that-adds-a-debug-toggle-marker` (a new gate rule)
- The per-service connectors, beyond W6's default deny.
- F-0105: gating on CI by default. The WAITING_CI state already supports it; only the default
  stays local.
- F-0104 and F-0112: pinned live installs, and self-upgrade with rollback. W6's snapshot tick
  covers ASF's own clock only.
- B-0087, B-0088, B-0090, B-0092, B-0098 (views, digest, console rules), and F-0117/F-0118/F-0122
  (command center, statusLine, tables reading the tick clone).
- Decided features not yet landed: F-0039, F-0040, F-0041, F-0053, F-0026 (except I3), F-0063,
  F-0054 (except W3's two new outcome classes), F-0093, F-0028, F-0101.
- Approvals text-matching (the 06bcc00 → 0be71f7 class): it is not a lane defect today, and a
  structural replacement is a feature (F-0062/F-0065).

---

## 9. OVERRIDES after the independent review (these win over §1–§8)

1. NO lanes.jsonl. Lane state lives ON THE RUN LINE in sessions.jsonl: mark_session(job, lane={state, head, pr, at, reason}). lifecycle.fold/by_branch give "last run naming a branch owns it". Adoption writes a synthetic run (as land_spec.adopt does). No bootstrap. v0.1.2 ignores the extra field (rollback safe).
2. Lane pass IN-PROCESS before the wave: transitions the feeder reads (PUSHED, PR_OPEN, REVIEW, BACK, STALE, head-moved) are computed in the tick before step wave. Only GATE outcomes (MERGED/BACK/WAITING) run in the detached harvest. Before any external merge write an intent `lane: MERGING pr=n`; a later T11 with a prior MERGING is our own merge, not foreign.
3. Added transitions: any open state with head != recorded head → PUSHED(new head); QUEUED state for merge queues (queue-rejected → BACK or WAITING; counts toward in-queue budget); a reopened PR goes STALE → PR_OPEN.
4. Invariants fail SOFT: record violation → revert only the offending paths (git checkout -- paths), commit the rest, one Bug per (invariant, path); feeder violation → drop row + log; lane → report only. Never abort the tick. I4 reworded: at most one launching row per branch, and its kind must match the lane state. I2 runs after ingest's restamp. I9 is an event, not a violation. I6/I12/I13 are tests, not tick checks.
5. Workstreams: W1 and W3 are ONE stream (lane + feeder occupancy). Config parser keys (env_passthrough, home_seed, doc_paths, shared_paths, lane.*) all in W0 (asf/env.py, conventions, schema). Merge main INTO the integration branch (no rebase).
6. Harness: ticks in-process, harvest inline (no subprocess tick); scenarios 1, 2, 4, 5 in both modes; 3, 6, 7 as unit tests.
7. Release gate adds: one LIVE worker session on one product with the isolated home before tagging; rollout uses feeder.hold: [features, bugs] for the first hour; new config keys are accepted by v0.1.2 or live where it ignores them (no rollback config edit).
8. Cut for Saturday: gate fingerprint cache (instead: skip re-gate when the trunk moved docs-only via landing_class), W4 status/tick-log/daily-stamp/init polish, W6 install.sh/RunAtLoad items, §7 card hygiene (any day).
9. Added from customer feedback: groom "For you" holds only human-now actions (card inbox/groom-digest-for-you-fills-…): near-duplicates of templated Tasks decided by rule; deciding a card crosses no approval class; housekeeping in its own section; no derived Backlinks on removed cards. Goes with W4.

Base: current origin/main (includes e284ee7 and 74497d9). Integration branch: integration/fix-package.
ASF's repo is PUBLIC: never copy any customer's paths, identifiers or record content into code/tests/docs/commits.

## 10. REVIEW COMPLIANCE CHECKLIST (binding; the package gate verifies every line)

Every workstream's final report must say, per applicable line, "done: <file:function / test name>" or "n/a".
A second independent review (Fable) checks this list against the integration branch before tagging.

State machine (W1+W3)
- [ ] R1  Lane state on the run line in sessions.jsonl; no lanes.jsonl; no bootstrap step.
- [ ] R2  Feeder-visible transitions computed in-process BEFORE the wave; only gate outcomes in the detached harvest.
- [ ] R3  `lane: MERGING pr=n` intent written before every external merge; a merge seen later with a prior MERGING is ours (never "foreign").
- [ ] R4  Head moved in any open state (coder continued, human push, force-push) → PUSHED with the new head; review currency follows the head.
- [ ] R5  QUEUED state for merge queues; queue-rejected → BACK or WAITING; in-queue counts toward the merge budget.
- [ ] R6  Reopened PR: STALE → PR_OPEN.
- [ ] R7  Docs-branch BACK routes to a STARVED → SPEC/PLAN correction (the routing lives in the same stream as the lane).
- [ ] R8  Tick crash between any external side effect and its ledger write is recoverable on the next tick (test it).

Invariants (W4)
- [ ] R9  Soft failure: record → revert only the offending paths, commit the rest, one Bug per (invariant, path); feeder → drop row + log; lane → report. The tick never aborts on an invariant.
- [ ] R10 I4 = at most one launching row per branch, kind matching the lane state (the REVIEW launch is legal).
- [ ] R11 I2 runs after ingest's restamp (a migrated record never blocks).
- [ ] R12 I9 is an event, not a violation (a human merge in PR mode is legitimate).
- [ ] R13 I6 daily/deep only; I12, I13 are tests, not tick checks.
- [ ] R14 I1 per-writer refusal: the record step is restructured to stage/validate per writer — W2 and W4 agree the interface in W0's stub first (it's larger than 5 h; plan for it).

Workstreams / integration (all)
- [ ] R15 Branch cut from a base including e284ee7 (done: 74497d9).
- [ ] R16 W1 and W3 built as ONE stream.
- [ ] R17 All new config keys parsed in W0 (asf/env.py/conventions/schema), none in W6/W2.
- [ ] R18 main is merged INTO integration/fix-package (never rebased).

Harness (W5)
- [ ] R19 Ticks in-process, harvest inline; scenarios 1, 2, 4, 5 in both modes; 3, 6, 7 as unit tests; module < 90 s.
- [ ] R20 Known blind spots covered elsewhere: load timeouts (a suite run under CPU load before tag), case-insensitive paths (unit test), real gh semantics (the live smoke below), hand-made yaml / foreign hooks (existing unit tests).

Rollout (W6 + gate)
- [ ] R21 LIVE smoke before tag: one real worker session on one product with the isolated HOME authenticates, works, pushes.
- [ ] R22 W6 snapshot tick is live before the package merges into main (torn-tree protection).
- [ ] R23 New config keys don't break a v0.1.2 rollback (placed where v0.1.2 ignores them, or v0.1.2-compatible) — verified by running v0.1.2 against a product file with the new keys.
- [ ] R24 Rollout brake: feeder.hold: [features, bugs] for the first hour after upgrade.

Scope (cut unless time remains)
- [ ] R25 Gate fingerprint cache replaced by "skip re-gate when the trunk moved docs-only" (landing_class).
- [ ] R26 Polish items cut: status/tick-log/daily-stamp/init (W4), install.sh/RunAtLoad (W6), card hygiene (any day).

## 11. CODE OVER LLM (operator, binding — overrides §10's "second independent review")

- The review checklist (§10) is verified by CODE, not by an LLM review. Every R-line maps to a named test
  (e.g. tests/test_lane.py::test_r3_merging_intent_is_ours). A new tools/package_gate.py runs: the full suite,
  both lints, tests/test_e2e_lane.py, and a table of R1..R26 → test ids, failing if any R-line has no passing
  test (or an explicit, reasoned n/a in a checked-in list). The tag waits for package_gate.py exit 0.
  An LLM review may be run as advisory input only; it never gates.
- Releases and card closing by rule, not by memory: tools/check_conventions.sh refuses a fix/hotfix commit
  whose subject or trailer names no item (Fixes: <id>), so the release cadence (which needs a commit naming an
  item) and the record both follow the code. Hotfix commits already on main are credited once by a small
  scripted pass, not by hand.
- The rollback target (the last release containing feeder.hold) is ensured by that rule cutting a release,
  not by an operator or agent remembering to tag.
