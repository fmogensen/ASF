---
kind: spec
title: asf backfill — reconcile a partly-built product's history into its record
status: draft, 2026-09-24
builds-on: docs/plans/fix-package.md (the PR-lane state machine, §2 and §9)
---
# asf backfill — a partly-built product's history, into the record

Adopting ASF on a product that was already partly built is the normal case, not the exception.
Such a product arrives with years or weeks of merged pull requests, merge-queue batch commits and
open branches that no ASF run made. `asf migrate` creates the cards from the product's plans and
goals, but it does not read the history, so the record starts out wrong in three ways:

1. **Shipped work is not delivered.** Work that landed before the record existed either has no
   evidence (its Tasks sit New or Active for ever) or was typed `removed:` by the adopter's own
   migration ("done before the backlog existed"), which hides it. The factory counts none of it.
2. **Open pre-ASF PRs are orphans.** Nothing in ASF opened them, so the lane never advances
   them: they are never reviewed, gated, merged or closed, and the feeder may launch a second
   session for the same Task.
3. **What cannot be matched is invisible.** Hand merges and hotfixes without a pattern are
   nowhere, so the operator cannot answer them.

The first adopting product showed all three at scale: about 500 merged PRs, about 150 batch
commits, about 100 Features typed `removed:` for history, and about 140 open pre-ASF PRs (about
105 with clean unmerged work, about 35 conflicting, about 10 already on the trunk, several that
add only a writer report).

`asf backfill` is the factory feature that reconciles this, once and then idempotently. It is
code over facts: every decision below is a rule over git, the PR host and the record. No LLM
reads a PR. What no rule decides goes into one code-generated residue table for the operator.

```
asf backfill --product <p> --dry-run     # the classification table; writes nothing
asf backfill --product <p>               # the same table, then one record commit + the PR actions
asf backfill --product <p> --json        # the plan as JSON (for tests and the views)
asf backfill --product <p> --expect <digest>   # refuse unless the plan is still the one reviewed
```

---

## 1. Decisions

### 1.1 Preconditions

- **The fix package (v0.1.3) is installed.** Backfill is built on the package's PR-lane state
  machine (`asf/harvest/lane.py`), its lane state on the run line in `sessions.jsonl` (plan §9.1),
  its adoption transition (T2 from an existing PR, `lane.adopt`), its one review reader
  (`asf/evidence/review.py`) and its staged record writers (`asf/record/stage.py`, where
  `WRITERS` already names `backfill` first).
- **`asf migrate` has run.** Backfill never mints a card. A merged PR whose plan Task has no card
  is residue (`no-card`), and the remedy is `asf migrate`, then a second backfill.
- **The tick is paused for the product** while backfill applies (it takes the product's tick lock,
  `tick.Locks`, and refuses when a tick holds it). The operator is advised, not forced, to set
  `feeder.hold: [features, bugs]` for the first tick after an apply, as the package rollout does.

### 1.2 In

- A `backfill:` block in the product's `conventions` (§2.1).
- `asf/backfill/` (new): `model.py` (the fact and plan types), `grammar.py` (the branch grammar
  and the plan-file/heading index, §3.1.1), `facts.py` (read-only gathering),
  `rules.py` (the pure classifier), `table.py` (rendering), `apply.py` (the record writer and the
  PR actions), `cli.py`.
- Two approval classes in `asf/approvals.py`: `adopt_foreign_pr` and `close_foreign_pr` (§5).
- Ingest reads the typed `landed:` answer into the closing rules (§3.4). Today `closing.Ev.landed`
  exists and the `reconciled` rule reads it, but `ingest` never fills it.
- `review.newest` honours a product's `backfill.legacy_review` for an adopted PR (§4.3).
- The status `Record` cell shows the delivered count (§6).

### 1.3 Out

- Minting cards (that is `asf migrate`).
- Deleting any branch, force-pushing, rewriting a PR's commits, or reopening a PR.
- An LLM mapper. A later card may add an *advisory* suggestion column to the residue table; it
  never decides a row.
- Stories. A Story closes only by its own rules (its Tasks, or the plan's matrix, or the
  product's "done = tests green" test evidence). Backfill writes nothing to a Story.
- Bugs. A pre-ASF hotfix is residue unless a card names it.

### 1.4 The choices

| # | Question | Decided |
| --- | --- | --- |
| D1 | What backfill writes into the record | **Typed answers only**: `links.prs`, `links.branches`, `landed: <sha>`, and the removal of a history `removed:`, plus one History line per card. Every state and stage is then derived by `ingest` through the closing rules, in the same commit. Backfill never writes a machine key. This is "fix the software, not the state": the history becomes evidence, and the existing derivation does the rest. |
| D2 | Where the mapping comes from | From facts the product already has: the cards' `links` and `legacy_id`, the **plan files on the trunk and their `### Task N` headings** (the key that maps most of a real history, §3.1 K3b), and the product's `backfill.branch_patterns`. An E/F/T/B id token in the PR **title or branch** is a mapping; one in the PR body is advisory only (bodies cite other items). No fuzzy matching: two candidates is residue (`ambiguous`), never a guess. |
| D3 | Rule order for an open PR | lane-owned → on-trunk → ineligible (fork, draft) → report-only → untracked → card closed or removed → document PR → mapped and mergeable → mapped and conflicting → stale-conflict → residue (§4.1). Lane-owned comes first so backfill never touches a PR a run speaks for. Report-only is decided **before** adoption, so a report that happens to sit on a mapped branch is closed, not reviewed. |
| D4 | What "adopt" means | The package's T2: `lane.adopt(product, branch, pr, item, reason)` writes a synthetic run whose `lane` field is `PR_OPEN`. From there the PR takes the normal lifecycle (REVIEW → GATE → MERGED, or BACK) and every merge goes through the merge classes as today. Backfill itself never merges. |
| D5 | A conflicting but fresh mapped PR | Adopted too. The lane's gate sends it BACK `kind=conflict`, and the feeder's correction rebases it, which is exactly what the lane is for. A conflict whose head commit is older than `backfill.stale_after` is closed only under D9 (O10). |
| D6 | How the operator approves the PR actions | Through the approvals matrix, one hold per (plan digest, class), not one per PR (§5). The record commit needs no approval: it writes only evidence, and it is one revertable commit. |
| D7 | Idempotence | The plan is a pure function of the facts and the record. After an apply, every fact the plan acted on is in the record (typed links, `landed:`), in the lane (a run owns the adopted branch) or on the host (the PR is closed, with a marker comment), so the next plan is empty. An empty plan makes no commit and no host call. |
| D8 | Order of the two writes | The record commit first (pushed), then the PR actions. A crash between them is recovered by the next run: the record rows are no-ops, and the PR actions are recomputed from the host. |
| D9 | Closing is conservative | A PR is closed only when its work is provably on the trunk (O2), provably a report (O4), or stale **and** either mapped to a card that will re-plan it or carrying no commit that touches a product path (O10). Stale unique work on no card is residue (`stale-unique`), never closed: a closed orphan is lost work. |
| D10 | The residue is sized in groups | The operator reads one row per **pattern group** (the branch's normalised family and the reason), with a count and up to three example PRs, not one row per PR. Untracked families (`backfill.untracked_patterns`: hotfixes, CI and research branches) are counted in the summary and never listed. |

### 1.5 What the first adopter's history showed

The first version of these rules was run by the first adopting product over its real history,
before any code: about 4.6 % of its 520 merged PRs mapped, and 58 of its 150 open PRs. The misses
had causes a rule can fix, and this spec adopts each:

| Cause | Share | Fixed by |
| --- | --- | --- |
| Task branches whose plan slug has no card `legacy_id` (aliases, renamed plans, phase-numbered plans) | the largest | K3b: the plan **file** (date prefix stripped) and its `### Task N` heading, matched to the Task card by `links.plan` and title |
| Branch names the grammar did not parse: several Tasks in one name (`…-t3-t6`), letter parts (`…-t2a`), wave branches (`…-w8`), trains | large | §3.1.1 branch grammar |
| A `links.prs` hit on a Feature (a document PR number re-used as the Feature's link) swallowing a Task-shaped branch | small, wrong | K1 falls through to K3 for a Task-shaped branch |
| Id tokens in PR bodies naming other items | wrong maps | K2 counts title and branch only |
| Batch/train PRs whose members are listed only in the PR body | misses | L3 reads a merged batch PR's body |
| Hotfix, CI and research branches | noise in the table | `untracked_patterns`: counted, not listed |
| C4 restoring a Feature on a document PR alone; the stale-conflict rule closing unmapped unique work, and reading staleness from `updatedAt` (bumped by bots and labels) | harmful | C4 needs C1; O10/O11 as D9; staleness from the head commit date |
| An open document PR adopted as code | wrong lane | O7: the document lane |

The acceptance (§8) reproduces each family on a synthetic fixture; the real history is the
validation run (§8.2).

---

## 2. Data sources

Everything is read once per run, before any decision. `facts.py` is the only module that
touches git or the host. `rules.py` is pure.

| # | Source | Read with | What it gives |
| --- | --- | --- | --- |
| S1 | Every PR, all states | the package's single host reader (`gh pr list --state all`, paged to the end; GraphQL when over the page limit) | number, head and base branch, head repository (fork?), draft, state, `mergedAt`, `mergeCommit`, `closedAt`, `updatedAt`, title, body, head sha, changed files |
| S2 | The trunk's first-parent history | `git log --first-parent --format=… origin/<trunk>` | per commit: sha, date, subject, body, parents. Merge commits (`Merge pull request #n …`), squash commits (`… (#n)`) and batch commits (§3.2) |
| S3 | Per open PR: is it already on the trunk, does it merge | `git merge-tree --write-tree origin/<trunk> <head>` (the result tree and conflict flag), `git cherry origin/<trunk> <head>` (patch ids) | `on_trunk`, `conflicts`, commits ahead; per unique commit (`+` in `git cherry`) whether it touches a product path (outside the docs roots, `report_paths` and the review globs); the **head commit's committer date** (staleness, O10). Computed locally: the host's lazy `mergeable` is never trusted |
| S4 | The record | `record.core.load_items` | per card: id, type, parent, title, `legacy_id`, `links` (incl. `links.plan`), `removed`, `landed`, derived state |
| S5 | The product's plans | `git ls-tree` of `plans_dir` on the trunk, and each file's `#{2,4} Task <n>[a-z]?[:.] <title>` headings (the `plan_tasks` parser) | per plan file: its path, its **key** (file name without a leading `YYYY-MM-DD-` and `.md`), its H1 alias (as `evidence.discover` reads it), its Tasks `n → heading title` |
| S6 | The lane | `lane.snapshot(product)` | which branches a run already owns, in which state |
| S7 | Reviews on a PR head | `review.newest` (with §4.3's legacy fallback) | approved or not, for which head |
| S8 | The approvals ledger | `approvals.holds` | whether this plan's hold is granted |

### 2.1 The `backfill:` conventions

```yaml
conventions:
  backfill:
    branch_prefixes: ['old/', 'wt-']   # stripped before the grammar of §3.1.1 reads the rest
    branch_patterns: []          # optional extra regexes with named groups, tried after the grammar
    slug_aliases:                # a branch's {plan} → a plan-file key, where the two differ
      sample-billing: billing-v2
      p3: phase-3-hardening      # a phase-numbered branch family → its plan-file key
    doc_kinds: [spec, plan]      # leading words that make a branch a document branch
    history_removed: '^(done|shipped) before the backlog'     # which `removed:` texts are history
    report_paths: []             # globs of writer reports; default: [conventions.reports_dir/**]
    batch_patterns: ['^Merge train', '^batch:']   # a trunk commit message naming member PRs
    merge_batch: '(^|/)(batch|train)[-/]'         # a head branch that is a batch/train PR
    untracked_patterns: ['hotfix', '^ci-', '^research-', '^infra-']   # counted, not listed
    legacy_review:               # honour an old APPROVED review on an adopted PR (§4.3)
      glob: 'reviews/*-{branch_tail}.md'
      verdict_regex: '^\*\*Verdict:\*\*\s*APPROVED'
    stale_after: 7d              # a conflicting PR whose head commit is older than this is stale
```

- Every pattern (in `branch_patterns`, `untracked_patterns`, `merge_batch`) is matched against
  the branch **after** its `branch_prefixes` prefix is stripped; `batch_patterns` against a
  commit or PR message.
- An extra `branch_patterns` entry's named groups: `plan`, `task` (one or more, `-`-separated),
  `wave`, `kind`, `item`. The grammar of §3.1.1 is tried first; a pattern is for a family the
  grammar does not know.
- Unset `backfill:` → the grammar still parses branches (with no prefix to strip), K1/K2/K3b
  still map, and nothing else is inferred.
- Validation (in `env.validate_product_text`, like the package's keys): every pattern compiles;
  an extra branch pattern has at least one of `task`, `plan`, `item`; `stale_after` parses as a
  duration; every `slug_aliases` value is a string.

---

## 3. Merged history → evidence

### 3.1 Mapping a branch to a card (`rules.map_card`)

#### 3.1.1 The branch grammar (`rules.parse_branch`)

A head branch is first stripped of a `branch_prefixes` prefix, then read by one grammar into a
`BranchShape(family, plan, tasks, wave, kind, item)`. `family` is the normalised name with every
number replaced by `N` (`sample-tN`, `pN-tN`, `delta-wN`): the residue's group key.

| Shape | Example (after the prefix) | Parsed |
| --- | --- | --- |
| ASF id | `T-0135`, `f-0012-retry` | `item=T-0135` |
| document | `spec-billing`, `plan-billing` | `kind=spec|plan`, `plan=billing` (`doc_kinds`) |
| one Task | `billing-t3` | `plan=billing`, `tasks=[3]` |
| a Task part | `billing-t2a` | `tasks=[2a]` (part of Task 2 when no card has `2a`) |
| several Tasks | `billing-t3-t6` | `tasks=[3, 6]` |
| a Task range | `billing-t3-6` | `tasks=[3, 4, 5, 6]` |
| phase-numbered | `p3-t12` | `plan=p3`, `tasks=[12]` (resolved by `slug_aliases`, or by a plan file whose key starts `p3-`) |
| a wave | `delta-w8` | `plan=delta`, `wave=8`: no Task; maps only by K1/K2, else residue group `wave` |
| a batch/train | matches `merge_batch` | a batch PR: its members are read by L3, it maps to no card itself |
| untracked | matches `untracked_patterns` | counted in the summary, never mapped, never listed |
| anything else | `misc-cleanup` | `plan=<name>`, no task: K4 may map it as a document or Feature key, else residue |

#### 3.1.2 The mapping rules

First match wins. The result is `(card ids, how)` or a residue reason. A branch with several
Tasks maps to several cards; the PR is evidence on each.

| # | Fact | Maps to | `how` |
| --- | --- | --- | --- |
| K1 | The PR number is in a card's `links.prs`, or its head branch in a card's `links.branches` — **except** when that card is a Feature and the branch has Task shape (`tasks` non-empty): then fall through to K3 | that card | `link` |
| K2 | The PR **title** or the **branch** carries exactly one E/F/T/B id token of a card (`evidence.id_tokens`). Tokens in the body are listed as `advisory` in the row and never map | that card | `id` |
| K3 | Task shape, and a Task card has `legacy_id == <plan>/T<task>` (case-insensitive) | that Task (per task) | `legacy` |
| K3a | As K3 for a part `T2a` with no `…/T2a` card: the `…/T2` card | that Task, as one part | `legacy-part` |
| K3b | Task shape, no K3 hit: the plan file whose key equals `slug_aliases[plan]` or `plan` (or, for a phase family, the one plan file whose key starts `<plan>-`), its `### Task <task>` heading, and the **one** Task card whose `links.plan` is that file and whose title equals the heading (both normalised: lower case, non-word characters removed, first 40 characters). Parts fall back to Task `<n>` as in K3a | that Task (per task) | `plan-heading` |
| K4 | Document shape (`kind`) or a bare name: the Feature whose `legacy_id` is `plan`, or whose `links.spec`/`links.plan` file has that key | that Feature | `doc` |
| K5 | Two or more cards satisfy the first matching K row (per task) | none: residue `ambiguous` | — |
| K6 | Task shape, the plan file and its heading exist, but no card has them | none: residue `no-card` | — |
| K6b | Task shape, no plan file has the key | none: residue `no-plan-file` (answer: a `slug_aliases` entry) | — |
| K7 | Nothing matched | none: residue `unmapped` | — |

### 3.2 A merged PR's landing sha (`rules.landing_sha`)

| # | Fact | Landing sha | Date |
| --- | --- | --- | --- |
| L1 | Merged, base is the trunk | the PR's `mergeCommit` (a merge, squash or the last rebase commit) | `mergedAt` |
| L2 | Merged into another base (a queue or stacked branch), and that merge commit is an ancestor of the trunk | the first first-parent trunk commit that contains it | that commit's date |
| L3 | Named as a member of a **batch or train**: (a) a first-parent trunk commit whose message matches a `batch_patterns` entry, or names two or more PR numbers (`#<n>`) of PRs whose base was not the trunk or that the host reports closed unmerged; or (b) a **merged batch PR** (head matches `merge_batch`) that names its members as `#<n>` in its merge message **or its PR body**. Only S1 PR numbers count as members, and a member must not itself be merged into the trunk (L1 wins) | (a) the batch commit; (b) the batch PR's landing sha by L1/L2 | its date |
| L4 | Merged into another base that never reached the trunk | none: residue `not-on-trunk` | — |
| L5 | Closed unmerged and named by no batch | not landed; nothing is written | — |

### 3.3 Card rows (`rules.card_rows`)

One row per card that any merged PR, batch member or on-trunk open PR maps to. Several PRs on one
card are folded: all go into `links.prs`, and the **newest** landing sha is `landed:`.

| # | Card and evidence | Typed write | Derived after ingest (closing rule) |
| --- | --- | --- | --- |
| C1 | Task, one or more landed PRs, no open PR mapped to it, not already carrying them | `links.prs += [n…]`, `links.branches += [b…]`, `landed: <newest sha>` | Resolved (`landed`), or Closed (`reconciled`/`landed-green`) when CI was green at or after the sha, or the product has no CI |
| C2 | Task, landed parts (K3a) and a part still open | `links.*` for all parts; **no** `landed:` | Active (`in-flight`); the open part is adopted (§4) and lands the rest |
| C3 | Feature via K4 (a spec/plan document PR) | `links.prs += [n]` only | as today: a document on the trunk makes the Feature `documented`, never landed (package I10) |
| C4 | Feature or Task typed `removed:` whose text matches `backfill.history_removed`, with **C1 evidence** (a landed code PR) on it or on a descendant Task. A document PR (C3) alone never restores: a spec on the trunk says what was intended, not what shipped | drop `removed:`; the C1 writes (and any C3 links); History `backfill: restored from removed "<text>"; landed <date> via #n…` | from its children, or childless: `landed:`. Descendants still `removed:` with no evidence of their own stay removed, so a restored Feature never re-opens un-evidenced work for the feeder |
| C5 | Typed `removed:` matching `history_removed`, with no C1 evidence anywhere under it (none at all, or document PRs only) | the C3 links only, when there are document PRs; `removed:` stays | stays removed; counted in the table as `kept removed (docs only)` or `kept removed (no evidence)` |
| C6 | Typed `removed:` with any other text | none, ever | a human decision; untouched |
| C7 | The card already carries every PR and the `landed:` this row would write | none | no-op (idempotence) |
| C8 | Closed card that the evidence would move | none | `closing.sticky` already holds Closed; the row shows `no-op (Closed)` |

### 3.4 Ingest reads the typed `landed:`

A typed `landed: <sha>` on a Task, Bug or childless Feature is read as that item's
`merged_sha` when nothing stronger names it, and as `Ev.landed`, with `green = green_after(sha)`
(or true when the product has no CI). So `reconciled` and `landed` fire from the typed answer.
A `landed:` sha that is not on the trunk is ignored and says so in the evidence lines
(`landed <sha> is not on the trunk`). `check` keeps validating only its shape.

---

## 4. Open pre-ASF PRs → adopt or close

### 4.1 The rules (`rules.open_rows`), first match wins

| # | Fact | Action | Close reason / note | Approval class |
| --- | --- | --- | --- | --- |
| O1 | The lane owns the branch (`lane.snapshot` has it), or it is under one of ASF's own non-legacy `branch_prefixes` | **skip** | the lane decides it, including its T11 on-trunk case | — |
| O2 | No commits ahead of the trunk, or the merge-tree result equals the trunk's tree, or every commit's patch id is on the trunk | **close** | `already on the trunk at <sha>` (the trunk commit carrying the patch, else the trunk head). If mapped, the card gets C1 evidence with that sha | `close_foreign_pr` |
| O3 | Head repository is a fork, or the PR is a draft | **residue** | `fork` / `draft`: never adopted, never closed by rule | — |
| O4 | Every changed file matches `report_paths` | **close** | `report-only: <files>` | `close_foreign_pr` |
| O5 | The branch matches `untracked_patterns` | **none** | counted as `untracked` in the summary, not listed | — |
| O6 | Mapped (K1–K4) to a card that is removed (not history), or Closed/Resolved and O2 did not hold | **close** if removed; **residue** `card-done-unlanded` if done | `card <id> removed: <text>` | `close_foreign_pr` |
| O7 | Document shape, or every unique commit touches only docs roots (`lane.landing_class == docs`), mapped to an open Feature | **adopt as a document** | `lane.adopt(…, landing_class='docs')`: the package's docs lane (review policy `lane.review.docs`), never a coder or a code review; a conflict goes BACK to the docs correction route (package R7) | `adopt_foreign_pr` |
| O8 | Mapped to an open card, merges cleanly | **adopt** | `lane.adopt` → PR_OPEN; the card gets `links.prs`/`links.branches` (Active) | `adopt_foreign_pr` |
| O9 | Mapped to an open card, conflicts, head commit date within `stale_after` | **adopt** | the lane's gate sends it BACK `kind=conflict` (D5) | `adopt_foreign_pr` |
| O10 | Conflicts, head commit date older than `stale_after`, **and** (mapped, or no unique commit touches a product path) | **close** | `conflicts with the trunk; head commit <date>`; if mapped: `card <id> stays <state> and is re-planned by the feeder`; if unmapped: `no unique product change` | `close_foreign_pr` |
| O11 | Conflicts, stale, unmapped, and a unique commit touches a product path | **residue** `stale-unique` | never closed by rule (D9) | — |
| O12 | Anything else (unmapped and clean, or unmapped and fresh) | **residue** | the K reason | — |

Staleness is always the **head commit's committer date** (S3), never the PR's `updatedAt`, which
a label, a bot comment or a CI re-run moves.

Two open PRs on one branch cannot exist on the host. Two open PRs mapped to one Task (for example
a retry on a new branch): the one with the newest head commit takes O8/O9, and the others become
O10 with reason `superseded by #<n>` when they conflict, else residue `duplicate of #<n>`.

A closed PR gets one comment before it is closed:

```
Closed by asf backfill: <reason>.
Evidence: <trunk sha | card id | files>.
Reopen it to undo; the next backfill will classify it again.
<!-- asf-backfill:<digest> -->
```

The marker makes the close recognisable; the branch is never deleted.

### 4.2 After adoption

From `PR_OPEN` the lane owns the PR (package §2): T3 asks for a review of the head (code class),
T4 on approval → GATE, T9 BACK on red or conflict, T10/T11 MERGED. The Task derives Resolved from
the MERGED fact like any lane PR. The adopted run is synthetic (no session, no pid), as
`land_spec.adopt` does today, so `lifecycle.occupancy` sees the item busy and the feeder starts no
second coder (package I4).

### 4.3 Legacy reviews

With `backfill.legacy_review` set, `review.newest(product, branch, item)` falls back, **only for
a run whose `lane.reason` starts with `adopted`**, to the newest file matching `glob` on the PR
head (`{branch_tail}` is the head branch after its prefix). It counts as `approved` for the head
when `verdict_regex` matches its text **and** no commit after the one that added the review file
touches a path outside the review globs. Otherwise the file is history, and T3 asks for an ASF
review as for any PR.

---

## 5. Approvals interplay

Two classes join `approvals.CLASSES`:

| class | covers | default | read by |
| --- | --- | --- | --- |
| `adopt_foreign_pr` | handing an open PR that ASF did not open to the PR lane | `auto` | `backfill` |
| `close_foreign_pr` | closing an open PR that ASF did not open, with a reason | `groom` | `backfill` |

- Adoption is `auto` by default because it merges nothing: every merge after it is still decided
  by `merge_routine_pr` / `merge_amendable_set` at the lane's gate. Closing is visible to other
  people and is `groom` by default. An operator may set either to any level in `approvals:`.
- **One hold per (plan digest, class), not per PR.** The plan digest is the sha256 of the sorted
  PR-action rows (`pr, action, reason`), shortened to 12 hex. For a class whose level is not
  `auto`, `apply` records `approvals.refuse(product, item='backfill-<digest>', cls, level,
  job='backfill', tool='asf backfill', detail='<n> PRs — asf backfill --dry-run')` and performs
  none of that class's actions. `human-now` prints the usual `NEEDS OPERATOR … asf approvals
  resolve backfill-<digest>/<class> granted` line; `groom` lists it for the groom.
- `asf approvals resolve backfill-<digest>/close_foreign_pr granted`, then `asf backfill` again:
  if the recomputed digest is the same, the actions run; if the facts moved (a PR was pushed, a
  new PR opened), the digest differs, a new hold is raised and the old one is never used. A
  grant covers exactly the table that was reviewed.
- `dropped` on a hold: that class's actions are skipped for this digest; the PRs stay in the
  table as `held (dropped)`.
- Records are not an approval class: the record commit (evidence only) always applies.
- The hook side is unchanged: backfill runs in the operator's process, not in a session, so the
  `PreToolUse` hook is not involved; the classes are read by `backfill` directly through
  `approvals.level_of` (which fails closed to `human-now` on a bad matrix).

---

## 6. Output

`--dry-run` prints, and the apply prints again before acting:

```
BACKFILL <product>   plan <digest>   trunk <sha>   <date>

SUMMARY  merged PRs 40 · mapped 33 (82 %: link 2 · id 3 · legacy 6 · plan-heading 19 · doc 3)
         batch members 6 · untracked 4 · residue 3 PRs in 2 groups
         cards: Tasks → landed 27 · Features restored 4 (kept removed: docs only 1, no evidence 1)
         open PRs 14: close on-trunk 2 · adopt code 5 · adopt docs 1 · close stale-conflict 2
                      close report-only 1 · skip (lane) 1 · untracked 1 · residue 1 group

CARDS    id       type     before                  after (derived)        evidence
         T-0002   task     New                     Resolved (landed)      #12 squash 3f2a9c1 2026-08-02 (plan-heading)
         F-0003   feature  removed (history)       Resolved (children)    restored; 6/6 Tasks landed
         ...

OPEN PRS pr    branch               rule  action      card     reason                              approval
         #51   old/sample-t4        O10   close       T-0009   conflicts; head commit 2026-08-30   groom (held)
         #52   old/sample-a-t1      O8    adopt       T-0007   merges cleanly                      auto
         #53   old/plan-sample      O7    adopt-docs  F-0004   document PR                         auto
         ...

RESIDUE  group (family · reason)        kind    count  examples          to answer it
         delta-wN · wave                merged  2      #20 #21           a card's links.prs, or leave
         sample-b-tN · no-plan-file     open    1      #55               slug_aliases: {sample-b: <plan key>}
```

The residue table is code-generated from the rows and **grouped** by `(family, reason, kind)`
(§3.1.1): one line per group, sorted by count descending, with at most three example PR numbers
and the typed answer that would map the whole group on the next run (a `slug_aliases` or
`branch_patterns` entry for a family, `links.prs` or `legacy_id` for a single PR). No free text is
invented. `--residue-detail` prints every PR of every group.

`--json` prints the same plan: `{digest, trunk, summary, cards[], open[], residue[]}`.

After an apply the status `Record` cell reads `<n> open · <n> Active · <n> blocked · <n> no rule
· <n> delivered`, where delivered counts live items whose derived state is Resolved or Closed.

---

## 7. Edge cases

| # | Case | Handling |
| --- | --- | --- |
| E1 | Squash merge (the merge sha is not a descendant of the branch head) | L1 uses the host's `mergeCommit`, never ancestry |
| E2 | Rebase merge (several trunk commits) | `mergeCommit` is the last of them; `landed:` is that sha |
| E3 | PR merged into a queue branch that landed as a batch | L2, or L3 when the host shows the member closed unmerged |
| E4 | A batch commit names a PR number that is not a PR (an issue) | ignored: only S1 numbers are members |
| E5 | A split Task (`t2a`, `t2b`) whose card is `T2` | K3a; C1 when every part is landed, C2 while a part is open. A part that is neither landed nor open does not hold the Task back: the record cannot know it existed |
| E6 | One Task, several PRs over time (closed, then a new one) | all in `links.prs`; the newest landing sha is `landed:` |
| E7 | An ASF id token and a pattern disagree | K2 wins over K3; the row says both |
| E8 | A legacy prefix equals one of ASF's own prefixes | O1 decides by the lane: a run owns it → skip; otherwise the legacy rules |
| E9 | The host reports `mergeable: UNKNOWN` | ignored; S3 computes it locally |
| E10 | A branch that merges trunk into itself | the merge-tree tree comparison (O2) sees through it; patch ids alone would not |
| E11 | Head branch deleted, PR open | S3 reads the PR head via `refs/pull/<n>/head`; adoption is refused (the lane needs the branch): residue `branch-gone` |
| E12 | A card was restored (C4) but the Feature has un-evidenced Tasks that are not removed | they keep their typed state and become ordinary feeder candidates; the table counts them as `launchable after apply` so the operator can set `feeder.hold` first |
| E13 | History Features whose `removed:` text differs slightly | only `history_removed` decides; the table lists the distinct removed texts it did not match, with counts, so the operator can widen the regex |
| E14 | 500+ PRs | S1 pages to the end; the run is one pass; nothing is fetched per PR except S3 (local git) |
| E15 | A tick holds the lock | refuse with `a tick is running for <p>; retry after it` and exit 3, as `schema.require` does |
| E16 | Crash after the record commit, before the PR actions | the next run: every card row is C7 (no-op), the PR actions are recomputed; a PR already closed with the marker is not in S1's open set; an adopted branch is O1 |
| E17 | Crash in the middle of the PR actions | the same: each action is idempotent (close an already-closed PR: skipped; adopt a branch the lane owns: O1) |
| E18 | The record check refuses the backfill writer's change | `stage.run_writers` puts the offending cards back (package R14); the table prints them as `refused (<invariant>)` and the rest commits |
| E19 | No `backfill:` block at all | K1, K2 and K3b still map (the grammar needs no configuration); O1–O4 (O4 with `reports_dir`) and O10/O11 still apply; everything else is residue |
| E21 | A branch naming several Tasks, some landed elsewhere | the PR is evidence on each Task; a Task already carrying another landed PR folds both (E6) |
| E22 | A wave branch (`…-wN`) | no Task: K1/K2 only, else residue group `wave`; a closed or stale one follows O10/O11 like any other |
| E23 | A batch PR still open | it is adopted or closed by its own rules; its members are not landed until it is (L3 needs it merged) |
| E24 | A title id token and a branch id token disagree | K5 `ambiguous`: the operator decides |
| E20 | Dates and time zones | all UTC; `--now <iso>` fixes the clock for tests |

---

## 8. Acceptance tests

### 8.1 The synthetic fixture

A fixture product in `tests/backfill/`, **synthetic and generic** (the repo is public: no name,
path, branch or number from any real product). It reproduces every branch-name family the first
adopter's history showed, each under two prefixes (`old/`, `wt-`), in both merged and open PRs:

| Family | Fixture branches (examples) | Expected |
| --- | --- | --- |
| Task with a `legacy_id` card | `old/alpha-t1` | K3 |
| Task, plan renamed, no `legacy_id`, dated plan file | `old/beta-t2` with `plans/2026-01-05-beta.md` `### Task 2: …` | K3b |
| Task via `slug_aliases` | `old/gam-t1` → `gamma-v2` | K3b |
| phase-numbered | `wt-p3-t12` with `plans/2026-02-01-p3-hardening.md` | K3b (prefix key) |
| several Tasks | `old/beta-t3-t5` | K3b on T3 and T5 |
| Task part | `old/alpha-t2a`, `old/alpha-t2b` | K3a / K3b part, C1 when both land, C2 while one is open |
| wave | `wt-delta-w4` | residue group `delta-wN · wave` |
| document | `old/spec-beta`, `old/plan-beta` | K4 → C3; open → O7 |
| ASF id in the title / branch / body only | `old/T-0005`, title `F-0002: …`, body `see T-0009` | K2, K2, advisory only |
| a `links.prs` hit on a Feature from a Task branch | `old/beta-t4` whose PR number the Feature links | falls through to K3b |
| batch commit on the trunk | `batch: #31 #32 #33` | L3(a) |
| batch PR with members in its body only | `old/train-3` merged, body lists `#34 #35` | L3(b) |
| untracked | `old/hotfix-login`, `wt-ci-cache`, `old/research-x` | counted, not listed |
| unmapped | `old/misc-cleanup` | residue group |
| stale conflict, mapped / unmapped docs-only / unmapped unique | three open PRs, head commits 30 days old | O10 close, O10 close, O11 residue `stale-unique` |
| report-only | `wt-report-3` touching only `reports/` | O4 |

Plus: a queue base, merge/squash/rebase merges, a record with three `removed:` Features (history
text with code evidence, history text with document evidence only, another reason), and the
package's fake `gh` (`tests/e2e/fakes/gh`) seeded with merged, closed and open PRs.

### 8.2 Hit-rate targets and the real-history validation

Measured by `asf backfill --dry-run --json` (the summary's `mapped`, `open` and `residue_groups`):

| Target | Fixture | First adopter's real history (validation) |
| --- | --- | --- |
| merged PRs mapped | 100 % of the tracked families | ≥ the rate the adopter's plan-file+heading prototype reached on the same history, plus the K1/K2/K3 hits (baseline of the first rules: 4.6 %) |
| open PRs decided (closed, adopted or skipped) | 100 % of the tracked families | ≥ 58 of 150 (the first rules), expected well above it |
| residue size | exactly the fixture's residue groups | ≤ about 30 groups |
| harmful actions | none | no history Feature restored on documents alone; no unmapped PR with unique product work closed |

The real-history column is checked **once, by the adopter**, running `asf backfill --dry-run` on
its own history after the code lands and before any apply; its numbers go into the Feature's
History, never into this public repo.

### 8.3 The tests

| # | Test | Asserts |
| --- | --- | --- |
| A0 | Branch grammar | every family of §8.1 parses to its `BranchShape`, pure |
| A1 | `rules` K table | each K row, one assertion each, pure (no git, no host); K1's fall-through; K2 ignoring body tokens |
| A2 | Landing sha | a merge commit, a squash and a rebase PR each give L1's sha; a queue-base PR gives L2; a batch commit naming three members (two closed unmerged) gives L3(a) for all three; a merged batch PR listing members only in its body gives L3(b); a queue branch never landed gives residue `not-on-trunk` |
| A3 | Card rows | C1–C8 each: the typed write, and the state after `ingest` |
| A4 | History restore | the history-removed Feature with code evidence is restored and derives Resolved from its landed Tasks; the one with document evidence only stays removed (C5, links written); the one with no evidence stays removed; the other-reason one is untouched; an un-evidenced removed Task under a restored Feature stays removed |
| A5 | Open PR rules | one fixture PR per O row, and the tie-break for two PRs on one Task; a stale PR whose `updatedAt` is fresh but head commit old is stale; the open document PR is adopted with `landing_class=docs` |
| A6 | `--dry-run` writes nothing | the record tree hash, `sessions.jsonl`, the approvals ledger and the fake host state are byte-identical before and after |
| A7 | Apply | exactly one new record commit; `asf check` passes; `invariants.run(scope='record')` is `[]`; the closed PRs carry the marker comment; the adopted PRs have a lane record `PR_OPEN` |
| A8 | Idempotent | a second `--dry-run` prints an empty plan (digest of the empty plan), and a second apply makes no commit and no host write |
| A9 | Approvals | with `close_foreign_pr: human-now`, apply closes nothing and records one hold `backfill-<digest>/close_foreign_pr`; after `resolve … granted` the next apply closes them; after a new push to one PR the digest changes and the grant is not used |
| A10 | Adopted PR walks the lane | through the package's e2e harness: PR_OPEN → REVIEW (one review session) → GATE → MERGED, the Task Resolved; with `legacy_review` approved and no later code commit, REVIEW is skipped; with a later code commit, it is not |
| A11 | Crash recovery | a fault injected after the record commit: the rerun performs the PR actions and makes no second record commit |
| A12 | No LLM | no worker runtime is constructed during a backfill run (the runtime factory is patched to raise) |
| A13 | Residue | fork, draft, unmapped, ambiguous, no-card, no-plan-file, wave, stale-unique, card-done-unlanded, branch-gone and not-on-trunk each appear, **grouped by family and reason**, with a count, examples and the answer column; untracked families appear only as a summary count |
| A16 | Hit rates | §8.2's fixture column, read from `--json` |
| A14 | Status | the `Record` cell counts `delivered` after the apply |
| A15 | Generic | `tools/check_generic.sh` passes over the fixture and the docs |

The card's acceptance maps onto these: the dry-run lists the mapped Tasks/Features with PR
evidence and the open-PR decisions (A3, A5); the apply commit passes `asf check` (A7); the status
`Record` shows the delivered counts (A14).

---

## 9. Records

- The record commit subject: `backfill(<product>): <n> Tasks landed, <n> Features restored,
  <n> PRs linked — plan <digest>`. It names no item (it is not a fix commit), so it does not
  count toward a release.
- Every changed card gets one History line: `<date> backfill <digest>: <what> (<evidence>)`.
- Each PR action is logged to the tick log format under `state/<p>/logs/backfill-<date>.log`,
  one line per action, with the host's answer.

## Stories

- S: `asf backfill --dry-run` prints the card, open-PR and residue tables from facts alone, and writes nothing.
- S: Merged pre-ASF PRs and batch commits become typed evidence, and ingest derives the Tasks landed.
- S: History-removed Features with evidence are restored to their derived landed state.
- S: Open pre-ASF PRs are closed with a reason, or adopted into the PR lane, by rule.
- S: The PR actions pass the approvals matrix, one hold per plan digest and class.
- S: A second run is a no-op, and a crash between the writes is recovered.
- S: The status Record cell shows the delivered count.
