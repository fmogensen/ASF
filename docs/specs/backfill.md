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
- `asf/backfill/` (new): `model.py` (the fact and plan types), `facts.py` (read-only gathering),
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
| D2 | Where the mapping comes from | From the product's own conventions: `backfill.branch_patterns` (regexes with named groups) and the cards' `legacy_id`. An ASF id token in the PR title, body or branch wins over a pattern. No fuzzy matching: two candidates is residue (`ambiguous`), never a guess. |
| D3 | Rule order for an open PR | lane-owned → on-trunk → ineligible (fork, draft) → report-only → card closed or removed → mapped and mergeable → mapped and conflicting → stale-conflict → residue (§4.1). Lane-owned comes first so backfill never touches a PR a run speaks for. Report-only is decided **before** adoption, so a report that happens to sit on a mapped branch is closed, not reviewed. |
| D4 | What "adopt" means | The package's T2: `lane.adopt(product, branch, pr, item, reason)` writes a synthetic run whose `lane` field is `PR_OPEN`. From there the PR takes the normal lifecycle (REVIEW → GATE → MERGED, or BACK) and every merge goes through the merge classes as today. Backfill itself never merges. |
| D5 | A conflicting but fresh mapped PR | Adopted too. The lane's gate sends it BACK `kind=conflict`, and the feeder's correction rebases it, which is exactly what the lane is for. Only a conflict older than `backfill.stale_after` is closed. |
| D6 | How the operator approves the PR actions | Through the approvals matrix, one hold per (plan digest, class), not one per PR (§5). The record commit needs no approval: it writes only evidence, and it is one revertable commit. |
| D7 | Idempotence | The plan is a pure function of the facts and the record. After an apply, every fact the plan acted on is in the record (typed links, `landed:`), in the lane (a run owns the adopted branch) or on the host (the PR is closed, with a marker comment), so the next plan is empty. An empty plan makes no commit and no host call. |
| D8 | Order of the two writes | The record commit first (pushed), then the PR actions. A crash between them is recovered by the next run: the record rows are no-ops, and the PR actions are recomputed from the host. |

---

## 2. Data sources

Everything is read once per run, before any decision. `facts.py` is the only module that
touches git or the host. `rules.py` is pure.

| # | Source | Read with | What it gives |
| --- | --- | --- | --- |
| S1 | Every PR, all states | the package's single host reader (`gh pr list --state all`, paged to the end; GraphQL when over the page limit) | number, head and base branch, head repository (fork?), draft, state, `mergedAt`, `mergeCommit`, `closedAt`, `updatedAt`, title, body, head sha, changed files |
| S2 | The trunk's first-parent history | `git log --first-parent --format=… origin/<trunk>` | per commit: sha, date, subject, body, parents. Merge commits (`Merge pull request #n …`), squash commits (`… (#n)`) and batch commits (§3.2) |
| S3 | Per open PR: is it already on the trunk, does it merge | `git merge-tree --write-tree origin/<trunk> <head>` (the result tree and conflict flag), `git cherry origin/<trunk> <head>` (patch ids) | `on_trunk`, `conflicts`, commits ahead. Computed locally: the host's lazy `mergeable` is never trusted |
| S4 | The record | `record.core.load_items` | per card: id, type, parent, `legacy_id`, `links`, `removed`, `landed`, derived state |
| S5 | The product's plans | `evidence.discover` (as `ingest` uses it) | per plan: its alias (the H1 alias), its Tasks `T<n>` |
| S6 | The lane | `lane.snapshot(product)` | which branches a run already owns, in which state |
| S7 | Reviews on a PR head | `review.newest` (with §4.3's legacy fallback) | approved or not, for which head |
| S8 | The approvals ledger | `approvals.holds` | whether this plan's hold is granted |

### 2.1 The `backfill:` conventions

```yaml
conventions:
  backfill:
    branch_patterns:             # ordered; first match wins; named groups decide the target
      - '^old/(?P<plan>[a-z0-9-]+?)-t(?P<task>\d+[a-z]?)$'     # a plan Task
      - '^old/p(?P<phase>\d+)-t(?P<task>\d+[a-z]?)$'            # a phase-numbered plan Task
      - '^old/(?P<kind>spec|plan)-(?P<plan>[a-z0-9-]+)$'        # a spec/plan document branch
      - '^wt-(?P<plan>[a-z0-9-]+?)-t(?P<task>\d+[a-z]?)$'
    phase_plans: {'6': 'plan-alias-of-phase-6'}   # a {phase} group → a plan alias
    history_removed: '^(done|shipped) before the backlog'     # which `removed:` texts are history
    report_paths: []             # globs of writer reports; default: [conventions.reports_dir/**]
    batch_patterns: []           # extra regexes over a trunk commit message naming member PRs
    legacy_review:               # honour an old APPROVED review on an adopted PR (§4.3)
      glob: 'reviews/*-{branch_tail}.md'
      verdict_regex: '^\*\*Verdict:\*\*\s*APPROVED'
    stale_after: 7d              # a conflicting PR untouched this long is closed
```

- A pattern's named groups: `task` with `plan` or `phase` → a Task by `legacy_id =
  <plan alias>/T<task>` (the `TASK_LEGACY_RE` shape `ingest` already reads); `plan` alone (with
  or without `kind`) → the Feature whose `legacy_id` is that alias; `item` → that card id.
- Unset `backfill:` → only ASF id tokens and existing `links` map; every other PR is residue.
  Nothing is inferred from a branch name the product did not declare.
- Validation (in `env.validate_product_text`, like the package's keys): every pattern compiles
  and has at least one of the groups `task`, `plan`, `item`; `stale_after` parses as a duration;
  a `phase` group needs `phase_plans`.

---

## 3. Merged history → evidence

### 3.1 Mapping a branch to a card (`rules.map_card`)

First match wins. The result is `(card id, how)` or a residue reason.

| # | Fact | Maps to | `how` |
| --- | --- | --- | --- |
| K1 | The PR number is in a card's `links.prs`, or its head branch in a card's `links.branches` | that card | `link` |
| K2 | The PR title, body or head branch carries exactly one ASF id token of an open or closed card (`evidence.id_tokens`) | that card | `id` |
| K3 | The head branch matches a `branch_patterns` entry with `task`: the Task card whose `legacy_id` is `<alias>/T<task>` (case-insensitive on `T`) | that Task | `pattern` |
| K3a | As K3 but the task carries a letter suffix (`T2a`) and no card has that `legacy_id`: the card `<alias>/T2` | that Task, as one part | `pattern-part` |
| K4 | The head branch matches a pattern with `plan` and no `task` | the Feature whose `legacy_id` is the alias | `pattern-doc` |
| K5 | Two or more cards satisfy the first matching K row | none: residue `ambiguous (<ids>)` | — |
| K6 | A pattern matched, the plan has the Task, but no card has that `legacy_id` | none: residue `no-card <alias>/T<n>` | — |
| K7 | Nothing matched | none: residue `unmapped` | — |

### 3.2 A merged PR's landing sha (`rules.landing_sha`)

| # | Fact | Landing sha | Date |
| --- | --- | --- | --- |
| L1 | Merged, base is the trunk | the PR's `mergeCommit` (a merge, squash or the last rebase commit) | `mergedAt` |
| L2 | Merged into another base (a queue or stacked branch), and that merge commit is an ancestor of the trunk | the first first-parent trunk commit that contains it | that commit's date |
| L3 | Named as a member by a **batch commit** on the trunk: a first-parent commit whose message names two or more PR numbers (`#<n>`) of PRs whose base was not the trunk or which the host reports closed unmerged, or which matches a `batch_patterns` entry | the batch commit | its date |
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
| C4 | Feature or Task typed `removed:` whose text matches `backfill.history_removed`, with C1/C3 evidence on it or on a descendant | drop `removed:`; the C1/C3 writes; History `backfill: restored from removed "<text>"; landed <date> via #n…` | from its children, or childless: `landed:`. Descendants still `removed:` with no evidence of their own stay removed, so a restored Feature never re-opens un-evidenced work for the feeder |
| C5 | Typed `removed:` matching `history_removed`, no evidence anywhere under it | none | stays removed; counted in the table |
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
| O5 | Mapped (K1–K4) to a card that is removed (not history), or Closed/Resolved and O1 did not hold | **close** if removed; **residue** `card-done-unlanded` if done | `card <id> removed: <text>` | `close_foreign_pr` |
| O6 | Mapped to an open card, merges cleanly | **adopt** | `lane.adopt` → PR_OPEN; the card gets `links.prs`/`links.branches` (Active) | `adopt_foreign_pr` |
| O7 | Mapped to an open card, conflicts, `updatedAt` within `stale_after` | **adopt** | the lane's gate sends it BACK `kind=conflict` (D5) | `adopt_foreign_pr` |
| O8 | Conflicts, `updatedAt` older than `stale_after` (mapped or not) | **close** | `conflicts with the trunk, untouched since <date>`; if mapped: `card <id> stays <state> and is re-planned by the feeder` | `close_foreign_pr` |
| O9 | Anything else (unmapped and clean, or unmapped and fresh) | **residue** | the K reason | — |

Two open PRs on one branch cannot exist on the host. Two open PRs mapped to one Task (for example
a retry on a new branch): the newest by `updatedAt` takes O6/O7, and the others become O8 with
reason `superseded by #<n>` when they conflict, else residue `duplicate of #<n>`.

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

SUMMARY  merged PRs 40 · mapped 33 · batch members 6 · residue 7
         cards: Tasks → landed 27 · Features restored 4 (kept removed 1) · no-op 2
         open PRs 14: close on-trunk 2 · adopt 6 · close stale-conflict 2 · close report-only 1
                      skip (lane) 1 · residue 2

CARDS    id       type     before                  after (derived)        evidence
         T-0002   task     New                     Resolved (landed)      #12 squash 3f2a9c1 2026-08-02
         F-0003   feature  removed (history)       Resolved (children)    restored; 6/6 Tasks landed
         ...

OPEN PRS pr    branch               rule  action   card     reason                          approval
         #51   old/plan-sample      O8    close    F-0004   conflicts, untouched since …     groom (held)
         #52   old/sample-a-t1      O6    adopt    T-0007   merges cleanly                   auto
         ...

RESIDUE  kind    pr    branch            date        reason        to answer it
         merged  #9    hotfix-sample     2026-05-02  unmapped      asf set <id> links.prs=[9]
         open    #55   old/x-t2a         2026-09-21  ambiguous     T-0011, T-0012: type legacy_id on one
         ...
```

The residue table is code-generated from the rows, sorted by kind, then date. Its last column is
the typed answer that would map the row on the next run (`links.prs`, `legacy_id`, or a new
`branch_patterns` entry). No free text is invented.

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
| E10 | A branch that merges trunk into itself | the merge-tree tree comparison (O1) sees through it; patch ids alone would not |
| E11 | Head branch deleted, PR open | S3 reads the PR head via `refs/pull/<n>/head`; adoption is refused (the lane needs the branch): residue `branch-gone` |
| E12 | A card was restored (C4) but the Feature has un-evidenced Tasks that are not removed | they keep their typed state and become ordinary feeder candidates; the table counts them as `launchable after apply` so the operator can set `feeder.hold` first |
| E13 | History Features whose `removed:` text differs slightly | only `history_removed` decides; the table lists the distinct removed texts it did not match, with counts, so the operator can widen the regex |
| E14 | 500+ PRs | S1 pages to the end; the run is one pass; nothing is fetched per PR except S3 (local git) |
| E15 | A tick holds the lock | refuse with `a tick is running for <p>; retry after it` and exit 3, as `schema.require` does |
| E16 | Crash after the record commit, before the PR actions | the next run: every card row is C7 (no-op), the PR actions are recomputed; a PR already closed with the marker is not in S1's open set; an adopted branch is O1 |
| E17 | Crash in the middle of the PR actions | the same: each action is idempotent (close an already-closed PR: skipped; adopt a branch the lane owns: O1) |
| E18 | The record check refuses the backfill writer's change | `stage.run_writers` puts the offending cards back (package R14); the table prints them as `refused (<invariant>)` and the rest commits |
| E19 | No `backfill:` block at all | only K1/K2 map; O1, O2, O3, O8 and O4 (with `reports_dir`) still apply; everything else is residue |
| E20 | Dates and time zones | all UTC; `--now <iso>` fixes the clock for tests |

---

## 8. Acceptance tests

A fixture product (generic names only) in `tests/backfill/`: a bare origin with a trunk, legacy
branches under two declared patterns, one queue base, a record with Tasks carrying `legacy_id`s,
three Features typed `removed:` (two with the history text, one with another reason), and the
package's fake `gh` (`tests/e2e/fakes/gh`) seeded with merged, closed and open PRs.

| # | Test | Asserts |
| --- | --- | --- |
| A1 | `rules` K table | each K row, one assertion each, pure (no git, no host) |
| A2 | Landing sha | a merge commit, a squash and a rebase PR each give L1's sha; a queue-base PR gives L2; a batch commit naming three members (two closed unmerged) gives L3 for all three; a queue branch never landed gives residue `not-on-trunk` |
| A3 | Card rows | C1–C8 each: the typed write, and the state after `ingest` |
| A4 | History restore | the history-removed Feature with evidence is restored and derives Resolved from its landed Tasks; the one with no evidence stays removed; the other-reason one is untouched; an un-evidenced removed Task under a restored Feature stays removed |
| A5 | Open PR rules | one fixture PR per O row, and the tie-break for two PRs on one Task |
| A6 | `--dry-run` writes nothing | the record tree hash, `sessions.jsonl`, the approvals ledger and the fake host state are byte-identical before and after |
| A7 | Apply | exactly one new record commit; `asf check` passes; `invariants.run(scope='record')` is `[]`; the closed PRs carry the marker comment; the adopted PRs have a lane record `PR_OPEN` |
| A8 | Idempotent | a second `--dry-run` prints an empty plan (digest of the empty plan), and a second apply makes no commit and no host write |
| A9 | Approvals | with `close_foreign_pr: human-now`, apply closes nothing and records one hold `backfill-<digest>/close_foreign_pr`; after `resolve … granted` the next apply closes them; after a new push to one PR the digest changes and the grant is not used |
| A10 | Adopted PR walks the lane | through the package's e2e harness: PR_OPEN → REVIEW (one review session) → GATE → MERGED, the Task Resolved; with `legacy_review` approved and no later code commit, REVIEW is skipped; with a later code commit, it is not |
| A11 | Crash recovery | a fault injected after the record commit: the rerun performs the PR actions and makes no second record commit |
| A12 | No LLM | no worker runtime is constructed during a backfill run (the runtime factory is patched to raise) |
| A13 | Residue | fork, draft, unmapped, ambiguous, no-card, card-done-unlanded, branch-gone and not-on-trunk each appear with their answer column |
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
