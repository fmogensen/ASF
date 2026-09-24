# Plan — asf backfill: a partly-built product's history, into the record

Spec: `docs/specs/backfill.md` (draft, 2026-09-24, revised after the first adopter's empirical
review, spec §1.5). Builds on: `docs/plans/fix-package.md` (release v0.1.3), its lane API in
`asf/harvest/lane.py` (`snapshot`, `state`, `advance`, the adoption transition T2, `Host`,
`landing_class`), `asf/evidence/review.py` (`newest`) and `asf/record/stage.py` (`run_writers`,
with `backfill` already first in `WRITERS`).

**When and how it is built.** Right after the package releases, and **on the package's new PR
lane**: each Task below is an ordinary lane branch (coder → PR_OPEN → REVIEW → GATE → MERGED),
so the backfill work is also the lane's first real load after the release. The inbox card is
groomed into a Feature first; `asf.record.plan_tasks.mint_plan_tasks` then mints one Task card per
`### Task N:` heading of this plan once it lands, and writes `after:` from each Task's line. No
card id is invented here.

Twelve Tasks, cut by the module that owns the new surface. Sizes: **S** about 2 h, **M** about
4–5 h, **L** about 7–8 h. Total about **51 h** (was 40 h; the grammar, the plan-heading index,
the grouped residue and the synthetic family fixture are the additions).

Wave order: **1 ‖ 2 ‖ 3 ‖ 10 → 4 → 5 → 6 ‖ 8 → 7 → 9 → 11 → 12**, then the adopter's
validation run (not a Task, §Validation).

## Decisions

| # | Question | Decided |
| --- | --- | --- |
| PD1 | How the work is cut | By owner module: conventions (1), approvals (2), ingest/closing (3), the branch grammar and plan-heading index (4), the pure rules (5), the read-only facts (6), the dry-run CLI and grouped tables (7), the legacy review (8), the apply (9), status (10), the synthetic fixture and hit rates (11), the guide (12). No two Tasks' `writes:` intersect, so the only ordering is `after:`. |
| PD2 | Why the grammar is its own Task | The first rules mapped 4.6 % of a real history mostly because branch names were not parsed (multi-Task names, parts, waves, trains, phase families) and plan slugs did not match card `legacy_id`s. `parse_branch` and the plan-file key/heading index are the two pieces every later rule stands on, both pure, both testable family by family (spec A0). |
| PD3 | Why the model types live with the grammar | `model.py` (the facts, `BranchShape`, the plan rows) is written in Task 4 so the grammar, the rules and the facts all build on one set of types, and Task 5's rules are tested against hand-built facts before any git or host code exists. |
| PD4 | The package's adoption API | The package plan names `lane.adopt` (T2 from an existing branch or PR, replacing `land_spec.adopt`). Backfill calls `lane.adopt(product, branch, pr, item, reason, landing_class=None)`; `landing_class='docs'` puts an open document PR on the docs lane (spec O7). If v0.1.3 ships adoption only inside `lane.advance`, Task 9 adds `lane.adopt` as the thin writer of that synthetic run, and `Host.close(pr, comment)` beside `Host.open`/`merge` if absent. That is why Task 9's `writes:` includes `asf/harvest/lane.py`: it adds those two and changes no transition. |
| PD5 | Where the PR list comes from | The package's one host reader (the `gh pr list --state all` inside `lane.facts`), paged to the end, with `body` in the fields (L3(b) reads batch PR bodies). No fifth reader. |
| PD6 | The record write path | `stage.run_writers(root, [('backfill', apply.write_record), ('ingest', …), ('index', …)])`, then `publish.publish_changes` with the one commit subject of spec §9. The record invariants run over backfill's own change; a refused card is printed, the rest commits (spec E18). |
| PD7 | Approval holds are per plan digest | A hold id is `backfill-<digest>/<class>`, so one grant covers exactly one reviewed table (spec §5). |
| PD8 | No LLM anywhere | Nothing in `asf/backfill/` imports `asf.workers.runtime` or spawns a session; spec A12 asserts it. The grouped residue table is the fallback. |
| PD9 | The hit-rate targets | The fixture targets (spec §8.2) are hard acceptance in Task 11. The real-history targets — merged mapping at least the rate the adopter's plan-file+heading prototype reached (baseline 4.6 %), open PRs decided at least 58 of 150, residue at most about 30 groups, no harmful action — are checked once by the adopter's own `--dry-run`, and their numbers stay out of this repo. |

---

### Task 1: the `backfill:` conventions block
writes: asf/conventions.py, asf/env.py, docs/products.example.yaml, tests/test_conventions.py
after: none
size: S (3 h)

Spec §2.1. `Conventions.backfill` as a typed field with defaults: `branch_prefixes: []`,
`branch_patterns: []`, `slug_aliases: {}`, `doc_kinds: [spec, plan]`, `history_removed: None`,
`report_paths: None` (→ `[<reports_dir>/**]` when a `reports_dir` is set), `batch_patterns: []`,
`merge_batch: None`, `untracked_patterns: []`, `legacy_review: None`, `stale_after: 7d`. Accessors
compile the patterns once. `env.validate_product_text` refuses a pattern that does not compile, an
extra branch pattern with none of the groups `task`/`plan`/`item`, a bad duration and a non-string
alias, naming the key and the line. The example product file carries a commented `backfill:`
block with generic values only.

**Acceptance**

```bash
python3 -m unittest -v tests.test_conventions
python3 tools/run_tests.py 2>&1 | tail -3
bash tools/check_generic.sh
```

- A product file without `backfill:` loads with the defaults; one with a full block round-trips.
- Each refusal above is its own test and names the key.

---

### Task 2: two approval classes, `adopt_foreign_pr` and `close_foreign_pr`
writes: asf/approvals.py, tests/test_approvals.py
after: none
size: S (2 h)

Spec §5. Two `ActionClass` rows (defaults `auto` and `groom`, `read_by=('backfill',)`), after
`merge_routine_pr` in `CLASSES`. `plan_hold(product, digest, cls, count)` over `refuse` with item
`backfill-<digest>`, and `plan_granted(product, digest, cls)` over `is_granted`. `format_matrix`
prints them; `check_doctor` accepts them.

**Acceptance**

```bash
python3 -m unittest -v tests.test_approvals
python3 tools/run_tests.py 2>&1 | tail -3
```

- `matrix()` lists both classes with their defaults; an override to `human-now` is honoured.
- `plan_hold` twice for one digest bumps one hold; a second digest makes a second hold.

---

### Task 3: ingest reads the typed `landed:` answer
writes: asf/record/ingest.py, asf/evidence/closing.py, tests/test_ingest.py, tests/test_closing.py
after: none
size: M (4 h)

Spec §3.4. For a Task, a Bug and a childless Feature: a typed `landed: <sha>` on the trunk
(`evidence.ancestor_of`) fills `closing.Ev.landed` and, when nothing else names the item,
`merged_sha`, with `green = green_after(sha)` (true without CI). A sha off the trunk adds the line
`landed <sha> is not on the trunk` and is otherwise ignored. `closing._landed` reads `ev.landed`.

**Acceptance**

```bash
python3 -m unittest -v tests.test_closing tests.test_ingest
python3 tools/run_tests.py 2>&1 | tail -3
python3 -m asf.cli check
```

- `landed:` on the trunk with green CI → Closed (`reconciled`); off the trunk → state kept, line added.
- Ingesting twice is a no-op (package I6).

---

### Task 4: the model, the branch grammar and the plan-heading index
writes: asf/backfill/__init__.py, asf/backfill/model.py, asf/backfill/grammar.py, tests/test_backfill_grammar.py
after: 1
size: M (4 h)

Spec §3.1.1 and S5. `model.py`: frozen dataclasses `PrFact` (with `body`, head commit date),
`TrunkCommit`, `CardFact` (with `title`, `links.plan`), `PlanFile(path, key, alias, tasks: {n:
title})`, `OpenFacts` (on_trunk, conflicts, ahead, unique commits with a product-path flag, files),
`Facts`, `BranchShape(family, plan, tasks, wave, kind, item, untracked, batch)`, and the plan rows
`CardRow`, `OpenRow`, `ResidueRow`, `ResidueGroup`, `Plan`. `grammar.py`, pure:

- `parse_branch(branch, conv) -> BranchShape`: prefix strip, then ASF id, document, one Task,
  part, several Tasks (`-t3-t6`), range (`-t3-6`), phase family, wave (`-wN`), batch/train
  (`merge_batch`), untracked, bare name — in that order, then the extra `branch_patterns`;
- `plan_key(path)`: file name without a leading `YYYY-MM-DD-` and `.md`;
- `heading_index(plan_files, cards)`: `(plan path, task n) → card id` where exactly one Task card
  has that `links.plan` and a normalised title equal to the heading (lower case, non-word
  characters removed, first 40 characters); `resolve_plan(plan, conv, plan_files)` via
  `slug_aliases`, the exact key, or a unique `<plan>-` key prefix for a phase family.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_grammar
python3 tools/run_tests.py 2>&1 | tail -3
```

- Spec A0: every family of spec §8.1 parses to its shape, one test per family, under both
  prefixes; `family` normalises numbers to `N`.
- Two cards with one title under one plan → no index entry (ambiguity is the rules' to report).

---

### Task 5: the pure rules and the plan
writes: asf/backfill/rules.py, tests/test_backfill_rules.py
after: 4
size: L (8 h)

Spec §3.1.2–§3.3, §4.1 and §5's digest, over hand-built `Facts`:

- `map_card(pr, facts, conv)` → K1 (with the Feature fall-through for a Task shape), K2 (title and
  branch tokens; body tokens returned as advisory), K3, K3a, K3b, K4, K5–K7, per task of a
  multi-Task branch;
- `landing_sha(pr, facts)` → L1–L5; `batch_members(facts, conv)` → L3(a) trunk commits and L3(b)
  merged batch PRs by message **or body**;
- `card_rows(facts, conv)` → C1–C8, with C4 requiring C1 evidence and document-only → C5;
- `open_rows(facts, conv)` → O1–O12, staleness from the head commit date, O10/O11 by the
  mapped / product-path test, O7 for document PRs, and the one-Task tie-break;
- `residue_groups(rows)` → grouped by `(family, reason, kind)`, sorted by count, three examples
  each, and the answer per group; untracked counted only;
- `plan(facts, conv)` → `Plan` with `digest` (sha256 of the sorted PR-action rows, 12 hex) and a
  summary carrying the per-`how` mapped counts.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_rules
python3 tools/run_tests.py 2>&1 | tail -3
```

- Spec A1–A5: each K, L, C and O row one named test; the three harmful cases of spec §1.5 each
  have a test that fails on the old rule.
- `plan` is deterministic and ignores input order; over facts in which every action has already
  happened it is empty (spec D7, proven purely).

---

### Task 6: the read-only facts
writes: asf/backfill/facts.py, tests/test_backfill_facts.py
after: 5
size: M (5 h)

Spec §2, S1–S8. `gather(product, now=None) -> Facts`: the paged PR list with bodies (PD5), the
first-parent trunk log with bodies, the plan files on the trunk with their Task headings, per open
PR the `merge-tree --write-tree` result, conflict flag, `git cherry` unique commits each with its
product-path flag, and the head commit's committer date (via `refs/pull/<n>/head` when the branch
is gone), the record cards, `lane.snapshot`, the approvals holds. No write of any kind.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_facts
python3 tools/run_tests.py 2>&1 | tail -3
```

- Over a `tests/gitfixture` repo and the fake `gh`: merge, squash and rebase PRs give their
  `mergeCommit`; a branch that merged the trunk into itself is `on_trunk`; a docs-only unique
  commit is flagged non-product; the head commit date is read, not `updatedAt`.
- The fake `gh` log shows only read calls; refs and the record are unchanged.

---

### Task 7: `asf backfill --dry-run` and the grouped tables
writes: asf/backfill/table.py, asf/backfill/cli.py, asf/cli.py, tests/test_backfill_cli.py
after: 5, 6
size: M (4 h)

Spec §6. `table.render(plan)` prints SUMMARY (with the per-`how` mapped counts and the untracked
count), CARDS, OPEN PRS and the **grouped** RESIDUE; `--residue-detail` lists every PR; `--json`
prints the plan with `residue_groups`. `asf backfill --product --dry-run --json --expect --now
--residue-detail`. The apply path exits 2 (`apply: Task 9`) until Task 9 lands. The command takes
the tick lock and exits 3 when a tick holds it.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_cli
python3 tools/run_tests.py 2>&1 | tail -3
bash tools/check_generic.sh
```

- Spec A6: `--dry-run` leaves the record tree hash, `sessions.jsonl`, the approvals ledger and
  the fake host state byte-identical.
- Every residue group has an answer; `--json` parses and equals `plan()`; `--expect` mismatch
  exits 1 with both digests.

---

### Task 8: an adopted PR honours the product's legacy review
writes: asf/evidence/review.py, tests/test_review.py
after: 1
size: S (2 h)

Spec §4.3. `review.newest` falls back to `conventions.backfill.legacy_review` only for a run whose
`lane.reason` starts with `adopted` and when the ASF pattern finds nothing; approved for the head
only when no later commit touches a path outside the review globs.

**Acceptance**

```bash
python3 -m unittest -v tests.test_review
python3 tools/run_tests.py 2>&1 | tail -3
```

- Adopted + approved file + no later code commit → approved for the head; with a later code
  commit → history. A non-adopted run never reads the legacy glob.

---

### Task 9: `asf backfill` applies the plan
writes: asf/backfill/apply.py, asf/harvest/lane.py, tests/test_backfill_apply.py
after: 2, 3, 7
size: L (8 h)

Spec §3.3 writes, §4 actions, §5, D8 and E16–E18. `apply.run(product, plan, expect=None)`:
refuse on an `--expect` mismatch and print the table; the record writer through
`stage.run_writers` (PD6), one commit, pushed, none when there are no card rows; then per class,
`approvals.level_of` → act when `auto` or granted, else `plan_hold` and skip. Close = one marker
comment, then `Host.close`; adopt = `lane.adopt(…, reason='adopted by backfill <digest>')`, with
`landing_class='docs'` for O7. Every action is idempotent and logged to
`state/<p>/logs/backfill-<date>.log`. `lane.py` only per PD4.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_apply
python3 tools/run_tests.py 2>&1 | tail -3
python3 -m asf.cli check
```

- Spec A7, A8, A9, A11, A12; the O7 PR has a docs-class lane record and no coder row.

---

### Task 10: the status `Record` cell counts what is delivered
writes: asf/views/status.py, tests/test_views.py
after: none
size: S (2 h)

Spec §6. `record_cell` appends `· <n> delivered`: live items whose derived state is Resolved or
Closed.

**Acceptance**

```bash
python3 -m unittest -v tests.test_views
python3 tools/run_tests.py 2>&1 | tail -3
```

- Two Resolved, one Closed and one removed Resolved item → `3 delivered`.

---

### Task 11: the synthetic family fixture, the end-to-end acceptance and the hit rates
writes: tests/backfill/**, tests/test_backfill_e2e.py
after: 8, 9, 10
size: L (7 h)

Spec §8.1–§8.3. The synthetic fixture product, **generic names only**, reproducing every
branch-name family of spec §8.1 under two prefixes, in merged and open PRs: a trunk with merge,
squash, rebase and batch commits, a merged batch PR listing members only in its body, a queue
base, dated and undated plan files with `### Task` headings, cards with and without `legacy_id`,
three `removed:` Features, untracked families, and the fake `gh`. The module drives `--dry-run`,
the apply, a second of each, and the package's in-process lane harness for the adopted PRs.

**Acceptance**

```bash
python3 -m unittest -v tests.test_backfill_e2e
python3 tools/run_tests.py 2>&1 | tail -3
bash tools/check_generic.sh
```

- Spec A2–A11, A13, A14, A15 end to end; A10 through the lane.
- Spec A16: the fixture column of spec §8.2 — every tracked family mapped (merged 100 %, open
  100 % decided), residue exactly the fixture's expected groups, untracked only counted, no
  harmful action.
- The module runs in under 60 s.

---

### Task 12: the guide
writes: docs/guide/operating.md, docs/guide/product-config.md, CHANGELOG.md
after: 11
size: S (2 h)

`operating.md` gains "Adopting a partly-built product": `asf migrate`, then `asf backfill
--dry-run`, read the residue groups, answer them (`slug_aliases`, `branch_patterns`,
`untracked_patterns`, a card's `links.prs`), re-run until the residue is what should stay, set
`feeder.hold`, apply, resolve the holds. `product-config.md` documents every `backfill:` key.

**Acceptance**

```bash
bash tools/check_generic.sh && bash tools/check_conventions.sh
python3 tools/run_tests.py 2>&1 | tail -3
```

- Every key of spec §2.1 is documented once, with its default.

---

## Validation on a real history (after Task 12, not a Task)

The first adopter runs `asf backfill --dry-run --json` on its own history, before any apply, and
records in the Feature's History (the record, not this repo):

- merged PRs mapped ≥ the rate its plan-file+heading prototype reached on the same history
  (the first rules: 4.6 % of 520);
- open PRs decided (closed, adopted or skipped) ≥ 58 of 150, expected well above;
- residue ≤ about 30 groups;
- no history Feature restored on documents alone, and no unmapped PR with unique product work
  closed.

A miss is a defect against the rules, filed as a Bug with the residue group that shows it, and
fixed in the software (a grammar family, a rule), never by editing the record by hand. The apply
waits for this run.

## Stories → Tasks

| Story (spec) | Tasks |
| --- | --- |
| dry-run tables from facts, writes nothing | 4, 5, 6, 7 |
| merged PRs and batch commits become evidence | 1, 3, 4, 5, 9 |
| history-removed Features restored | 3, 5, 9 |
| open PRs closed or adopted by rule | 5, 8, 9 |
| PR actions pass the approvals matrix | 2, 9 |
| idempotent, crash-recoverable | 5, 9, 11 |
| status shows delivered | 10 |
