# Plan — asf backfill: a partly-built product's history, into the record

Spec: `docs/specs/backfill.md` (draft, 2026-09-24). Builds on: `docs/plans/fix-package.md`
(release v0.1.3), its lane API in `asf/harvest/lane.py` (`snapshot`, `state`, `advance`, the
adoption transition T2, `Host`, `landing_class`), `asf/evidence/review.py` (`newest`) and
`asf/record/stage.py` (`run_writers`, with `backfill` already first in `WRITERS`).

**When and how it is built.** Right after the package releases, and **on the package's new PR
lane**: each Task below is an ordinary lane branch (coder → PR_OPEN → REVIEW → GATE → MERGED),
so the backfill work is also the lane's first real load after the release. The inbox card is
groomed into a Feature first; `asf.record.plan_tasks.mint_plan_tasks` then mints one Task card per
`### Task N:` heading of this plan once it lands, and writes `after:` from each Task's line. No
card id is invented here.

Eleven Tasks, cut by the module that owns the new surface. Sizes: **S** about 2 h, **M** about
4 h, **L** about 7 h. Total about 40 h.

Wave order: **1 ‖ 2 ‖ 3 ‖ 9 → 4 → 5 ‖ 7 → 6 → 8 → 10 → 11**.

## Decisions

| # | Question | Decided |
| --- | --- | --- |
| PD1 | How the work is cut | By owner module: conventions (1), approvals (2), ingest/closing (3), the pure rules (4), the read-only facts (5), the dry-run CLI (6), the legacy review (7), the apply (8), status (9), the acceptance fixture (10), the guide (11). No two Tasks' `writes:` intersect, so the only ordering is `after:`. |
| PD2 | Why the model types live with the rules | `rules.py` is pure and is the heart of the spec; `facts.py` fills the types it defines. Putting `model.py` in Task 4 lets the rules be written and tested against hand-built facts before any git or host code exists (spec A1–A5 are pure). |
| PD3 | The package's adoption API name | The package plan names `lane.adopt` (T2 from an existing branch or PR, replacing `land_spec.adopt`). If v0.1.3 ships adoption only inside `lane.advance` (an existing PR on a branch with no run), Task 8 adds `lane.adopt(product, branch, pr, item, reason)` as the thin writer of that synthetic run, and `Host.close(pr, comment)` beside `Host.open`/`merge` if absent. That is why Task 8's `writes:` includes `asf/harvest/lane.py`: it touches the file only to add those two, never to change a transition. |
| PD4 | Where the PR list comes from | The package's one host reader (the `gh pr list --state all` inside `lane.facts`). Task 5 calls it with paging to the end instead of adding a fifth reader. If the package does not expose it as a function, Task 5 calls `GitHubHost` and says so in its PR. |
| PD5 | The record write path | `stage.run_writers(root, [('backfill', apply.write_record), ('ingest', …), ('index', …)])`, then `publish.publish_changes` with the one commit subject of spec §9. The record invariants (I1, I2, I3, I10, I11) run over backfill's own change; a refused card is printed, the rest commits (spec E18). |
| PD6 | Approval holds are per plan digest | A hold id is `backfill-<digest>/<class>` (`approvals.refuse` shape `<item>/<class>`), so one grant covers exactly one reviewed table (spec §5). |
| PD7 | No LLM anywhere | Nothing in `asf/backfill/` imports `asf.workers.runtime` or spawns a session; spec A12 asserts it. The residue table is the fallback. |

---

### Task 1: the `backfill:` conventions block
writes: asf/conventions.py, asf/env.py, docs/products.example.yaml, tests/test_conventions.py
after: none
size: S

Spec §2.1. `Conventions.backfill` as a typed field with defaults (`branch_patterns: []`,
`phase_plans: {}`, `history_removed: None`, `report_paths: None` → `[<reports_dir>/**]` when a
`reports_dir` is set, `batch_patterns: []`, `legacy_review: None`, `stale_after: 7d`), and
accessors `backfill_patterns()` (compiled, in order) and `backfill_stale_after_s()`.
`env.validate_product_text` refuses a pattern that does not compile, one with none of the groups
`task`/`plan`/`item`, a `phase` group without `phase_plans`, and a bad duration, naming the key
and the line. The example product file carries a commented `backfill:` block with generic
patterns only.

**Acceptance**

```bash
python3 -m unittest -v tests.test_conventions
python3 tools/run_tests.py 2>&1 | tail -3
bash tools/check_generic.sh
```

- A product file without `backfill:` loads with the defaults; one with a valid block round-trips.
- Each refusal above is its own test and names the key.

---

### Task 2: two approval classes, `adopt_foreign_pr` and `close_foreign_pr`
writes: asf/approvals.py, tests/test_approvals.py
after: none
size: S

Spec §5. Two `ActionClass` rows (defaults `auto` and `groom`, `read_by=('backfill',)`), placed
after `merge_routine_pr` in `CLASSES`. A helper `plan_hold(product, digest, cls, count)` that
calls `refuse` with item `backfill-<digest>` and returns the hold id, and `plan_granted(product,
digest, cls)` over `is_granted`. `format_matrix` prints them; `check_doctor` accepts them.

**Acceptance**

```bash
python3 -m unittest -v tests.test_approvals
python3 tools/run_tests.py 2>&1 | tail -3
```

- `matrix()` lists both classes with their defaults; an `approvals:` override to `human-now`
  is honoured; an unknown level is refused as today.
- `plan_hold` twice for one digest bumps one hold's count; a second digest makes a second hold.

---

### Task 3: ingest reads the typed `landed:` answer
writes: asf/record/ingest.py, asf/evidence/closing.py, tests/test_ingest.py, tests/test_closing.py
after: none
size: M

Spec §3.4. For a Task, a Bug and a childless Feature: a typed `landed: <sha>` that is on the
trunk (`evidence.ancestor_of`) fills `closing.Ev.landed` and, when no PR or commit names the item,
`merged_sha`, with `green = green_after(sha)` (true without CI). A sha not on the trunk adds the
evidence line `landed <sha> is not on the trunk` and is otherwise ignored. `closing._landed`
reads `ev.landed` too, so `landed`/`landed-green` fire as well as `reconciled`. The rule table's
docstring says so.

**Acceptance**

```bash
python3 -m unittest -v tests.test_closing tests.test_ingest
python3 tools/run_tests.py 2>&1 | tail -3
python3 -m asf.cli check
```

- A Task with `landed:` on the trunk and green CI derives Closed (`reconciled`); without CI data
  it derives per the product's CI rule; with a sha off the trunk it keeps its state and gains the
  line.
- Ingesting twice is a no-op (package I6).

---

### Task 4: the plan model and the pure rules
writes: asf/backfill/__init__.py, asf/backfill/model.py, asf/backfill/rules.py, tests/test_backfill_rules.py
after: 1
size: L

Spec §3.1–§3.3, §4.1 and the digest of §5. `model.py`: frozen dataclasses `PrFact`,
`TrunkCommit`, `CardFact`, `OpenFacts` (on_trunk, conflicts, ahead, files), `Facts` (all of them
plus `now`, `trunk`, `lane_branches`), and the plan rows `CardRow`, `OpenRow`, `ResidueRow`,
`Plan(digest, summary, cards, open, residue)`. `rules.py`, no I/O:

- `map_card(pr, facts, conv)` → K1–K7;
- `landing_sha(pr, facts)` → L1–L5, with `batch_members(facts, conv)`;
- `card_rows(facts, conv)` → C1–C8, folding several PRs per card;
- `open_rows(facts, conv)` → O1–O9 plus the one-Task tie-break;
- `plan(facts, conv)` → `Plan`, rows sorted, `digest` = sha256 of the sorted PR-action rows,
  12 hex.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_rules
python3 tools/run_tests.py 2>&1 | tail -3
```

- Spec A1–A5, each K, L, C and O row one named test over hand-built `Facts`.
- `plan(facts)` twice is equal; the digest ignores row order in the input.
- `plan` over facts in which every action has already happened (links typed, PRs closed,
  branches lane-owned) is empty (the idempotence of spec D7, proven purely).

---

### Task 5: the read-only facts
writes: asf/backfill/facts.py, tests/test_backfill_facts.py
after: 4
size: M

Spec §2, S1–S8. `gather(product, now=None) -> Facts`: the paged PR list (PD4), the first-parent
trunk log with bodies, per open PR the `merge-tree --write-tree` result and conflict flag and the
`git cherry` patch-id test (reading `refs/pull/<n>/head` when the branch is gone), the record
cards, `evidence.discover`'s plan aliases and Tasks, `lane.snapshot`, and the approvals holds.
No write of any kind.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_facts
python3 tools/run_tests.py 2>&1 | tail -3
```

- Over a `tests/gitfixture` repo and the package's fake `gh`: a merge, a squash and a rebase PR
  give their `mergeCommit`; a branch that merged the trunk into itself is `on_trunk` by tree
  equality; a conflicting branch is `conflicts`; a deleted head is read from `refs/pull`.
- The fake `gh` log shows only read calls; the repo's refs and the record are unchanged.

---

### Task 6: `asf backfill --dry-run` and the tables
writes: asf/backfill/table.py, asf/backfill/cli.py, asf/cli.py, tests/test_backfill_cli.py
after: 4, 5
size: M

Spec §6. `table.render(plan)` prints SUMMARY, CARDS, OPEN PRS and RESIDUE exactly as the spec
shows, with the residue's answer column generated from the row's reason; `--json` prints the
plan. `cli.register` adds `asf backfill --product --dry-run --json --expect --now`. The apply
path is a stub that exits 2 (`apply: Task 8`) until Task 8 lands. The command takes the tick lock
and refuses with exit 3 when a tick holds it (spec E15).

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_cli
python3 tools/run_tests.py 2>&1 | tail -3
bash tools/check_generic.sh
```

- Spec A6: `--dry-run` on the fixture leaves the record tree hash, `sessions.jsonl`, the approvals
  ledger and the fake host state byte-identical.
- The table's residue rows each carry an answer; `--json` parses and equals `plan()`.
- `--expect <other digest>` exits 1 and prints both digests.

---

### Task 7: an adopted PR honours the product's legacy review
writes: asf/evidence/review.py, tests/test_review.py
after: 1
size: S

Spec §4.3. `review.newest(product, branch, item)` falls back to `conventions.backfill.
legacy_review` only when the branch's owning run's `lane.reason` starts with `adopted` and the
ASF pattern finds nothing: the newest file matching `glob` on the head, `approved` when
`verdict_regex` matches and no later commit touches a path outside the review globs; its `head`
is the branch head in that case, else None (history).

**Acceptance**

```bash
python3 -m unittest -v tests.test_review
python3 tools/run_tests.py 2>&1 | tail -3
```

- Adopted run + matching approved file + no later code commit → `(n, 'approved', head)`.
- The same with a later code commit → `head` None. A non-adopted run never reads the legacy glob.

---

### Task 8: `asf backfill` applies the plan
writes: asf/backfill/apply.py, asf/harvest/lane.py, tests/test_backfill_apply.py
after: 2, 3, 6
size: L

Spec §3.3 writes, §4 actions, §5, D8 and E16–E18. `apply.run(product, plan, expect=None)`:

1. Refuse on `--expect` mismatch. Print the table.
2. Record: `write_record(root, plan)` sets the typed fields through `setfield.set_typed` and one
   History line per card; `stage.run_writers` runs it with ingest and index (PD5); one commit,
   pushed. An empty card set makes no commit.
3. PR actions, per class: `approvals.level_of`; `auto` or a granted `plan_hold` → act; otherwise
   `plan_hold` and skip. Close = one marker comment then `Host.close`; adopt =
   `lane.adopt(product, branch, pr, item, reason='adopted by backfill <digest>')`. Every action
   is idempotent (already closed, already lane-owned → skip) and is logged to
   `state/<p>/logs/backfill-<date>.log`.
4. `lane.py` is touched only per PD3.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_apply
python3 tools/run_tests.py 2>&1 | tail -3
python3 -m asf.cli check
```

- Spec A7: one record commit, `asf check` green, record invariants `[]`, markers on the closed
  PRs, `PR_OPEN` on the adopted ones.
- Spec A8: the second apply makes no commit and no host write.
- Spec A9: the hold, the grant, and a changed digest not using the old grant.
- Spec A11: a fault after the record commit; the rerun acts on the PRs and commits nothing.
- Spec A12: the runtime factory patched to raise is never reached.

---

### Task 9: the status `Record` cell counts what is delivered
writes: asf/views/status.py, tests/test_views.py
after: none
size: S

Spec §6. `record_cell` appends `· <n> delivered`: live (not removed) items whose derived state is
Resolved or Closed.

**Acceptance**

```bash
python3 -m unittest -v tests.test_views
python3 tools/run_tests.py 2>&1 | tail -3
```

- An index with two Resolved, one Closed and one removed Resolved item shows `3 delivered`.

---

### Task 10: the backfill fixture and the end-to-end acceptance
writes: tests/backfill/**, tests/test_backfill_e2e.py
after: 7, 8, 9
size: M

Spec §8. The fixture product (generic names only): a bare origin, a trunk with merge, squash,
rebase and batch commits, a queue base, legacy branches under two declared patterns, a record
with `legacy_id`s and three `removed:` Features, and the package's fake `gh` seeded with merged,
closed and one open PR per O row (fork and draft included). The module drives `asf backfill
--dry-run`, then `asf backfill`, then a second of each, then the package's in-process lane
harness for the adopted PRs.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_e2e
python3 tools/run_tests.py 2>&1 | tail -3
bash tools/check_generic.sh
```

- Spec A2–A11, A13, A14 and A15 end to end; A10 through the lane: PR_OPEN → REVIEW → GATE →
  MERGED with one review session, and REVIEW skipped for the legacy-approved head.
- The module runs in under 60 s.

---

### Task 11: the guide
writes: docs/guide/operating.md, docs/guide/product-config.md, CHANGELOG.md
after: 10
size: S

`operating.md` gains "Adopting a partly-built product": `asf migrate`, then `asf backfill
--dry-run`, read the residue, answer it, set `feeder.hold`, apply, resolve the holds, re-run.
`product-config.md` documents the `backfill:` keys. `CHANGELOG.md` names the feature under
unreleased.

**Acceptance**

```bash
bash tools/check_generic.sh && bash tools/check_conventions.sh
python3 tools/run_tests.py 2>&1 | tail -3
```

- Every key of spec §2.1 is documented once, with its default.

---

## Stories → Tasks

| Story (spec) | Tasks |
| --- | --- |
| dry-run tables from facts, writes nothing | 4, 5, 6 |
| merged PRs and batch commits become evidence | 1, 3, 4, 8 |
| history-removed Features restored | 3, 4, 8 |
| open PRs closed or adopted by rule | 4, 7, 8 |
| PR actions pass the approvals matrix | 2, 8 |
| idempotent, crash-recoverable | 4, 8, 10 |
| status shows delivered | 9 |
