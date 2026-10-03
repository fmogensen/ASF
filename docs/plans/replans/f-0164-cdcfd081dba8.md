replan: F-0164 cdcfd081dba8

**The groom's reshape cuts F-0164 to Task 3, and Task 3 is the guard.** One Task rewritten, none
minted, none dropped by name — the record holds exactly one open card for this Feature, `T-0756`,
and the cut moves it off the plan's Task 1 (the measurement) onto the plan's Task 3 (publish only a
branch this factory could have minted). The plan's Tasks 1 and 2 were never minted as cards of
their own, so this replan removes no card: it narrows the one that exists, and two of the eight
paths in its `writes:` leave the footprint with the work that is cut.

| Task | what the cut does to it |
|---|---|
| T-0756 | **rewritten** — from *"the health pass says what its publishes cost"* to the lane-prefix guard, and now the whole of F-0164's open work. Its `writes:` drops from the plan's three-Task union (eight paths) to Task 3's six; `asf/gitpush.py` and `tests/test_gitpush.py` leave the footprint entirely |

Nothing of F-0164 has landed, so nothing here reverts anything. Verified at this head rather than
read out of the record: `grep -rn 'launch_prefixes\|LAUNCH_KINDS\|publish_timing_line\|
publish_timeouts\|timed_out_after\|timeout_line\|PUBLISH_TIMEOUT' asf/ tests/` returns only
`asf/invariants.py:472`'s unrelated `LANE_LAUNCH_KINDS` (the launching *row kinds* each lane state
allows — a different thing from a branch kind, and the one name a later grep will collide with) and
one unrelated harvest test. `lifecycle.publish` still takes no `prefixes`, `run_steps` still
returns `None`, and `asf/gitpush.py` still formats its killed-push line inline.

## What the cut takes with it, stated plainly

The card asked for a proof and three remedies. The first remedy — publish concurrently — landed
before this Feature was planned (`5ec15c34e`, `PUBLISH_WORKERS = 3`). The cut keeps the third and
drops the proof and the second:

- **the measured incident is the part that is kept.** Of the 426.6 s step the card measured, 240 s
  was two timed-out pushes of **one** branch under no lane prefix — a hand-made `worktree-<label>-…`
  stray that was never the factory's. The kept Task refuses that branch in `publish`'s first guard,
  before any push and before any rebase, so that 240 s is not spent at all. The rest of that step
  was real publishes of real branches, which the landed pool already overlaps three at a time.
- **the split is still not measured** (the plan's Task 1, S-39700). The factory will still know what
  the whole step cost and nothing about what its publishes cost, and the next long step will be
  hand-sampled off a live ledger again. The spec's D1 argued the measurement was what a later
  decision to reorder the tick's steps would have to rest on; after this cut there is no such
  number, and that decision stays undecidable. Nothing regresses — this is a number that does not
  exist today either.
- **a lane branch whose hook outlives `git.push_timeout_s` still republishes forever** (the plan's
  Task 2, S-39701). This is the Feature title's second clause, and the cut leaves it standing for
  every branch the guard *does* accept: `republish_steps` publishes an ended-unpushed run's
  worktree on every pass, nothing counts the timeouts, `push_failure` classifies the timeout text
  as neither hook nor network (P5), and the run falls to the plain `UNPUSHED` hold telling the next
  session to push a branch the factory just failed to push. After the cut, the factory's answer to
  a slow hook on a lane branch is still 120 s a tick, for ever, with no hold and no next action.
  `NEEDS OPERATOR` below asks the groom for the card.

So the cut is defensible on the measurement the card itself carries — it keeps the remedy that
recovers the 240 s and drops the two that do not — and it is honest only if the second clause of the
Feature's own title is read as *not fixed*. S-39700 and S-39701 are uncovered by this replan;
coverage after it is 1/3 stories.

## Verified at this head (`875df70c5`), not read out of the plan

`asf/workers/health.py` has not moved since the plan measured it at `c26a365ed` — every anchor P1,
P4, P9 and P12 cite is at the line the plan gives. `asf/conventions.py` and
`asf/workers/lifecycle.py` have moved a long way, and every line below is re-read here:

| What | Plan / spec said | Holds now |
|---|---|---|
| `DEFAULT_BRANCH_PREFIXES` | `:94-101` | **`asf/conventions.py:100-108`** |
| `RECOGNISED_KINDS = ('direct',)` | `:107` | **`:113`** |
| `prefix`, `branch`, `legacy_prefixes`, `kinds`, `all_prefixes`, `branch_kind` | `:954-1016`, `:972-986`, `:978-985`, `:1004-1016` | **`:1032-1038`, `:1040-1042`, `:1044-1048`, `:1050-1054`, `:1056-1063`, `:1082`** |
| the fence's own header — why a prefix literal may live only here | `:9-16` | **`:1-9`**, and `tools/check_conventions.sh`'s four exempt files are exactly the four PD15 names |
| `publish`'s signature | `asf/workers/lifecycle.py:1571` | **`:1689`** (docstring to `:1731`) |
| `publish`'s one pre-push refusal, `is not a lane branch` | `:1614-1618` | **`:1732-1733`** — one writer in the package, one reader in the suite (`tests/test_lifecycle.py:1467`) |
| `spawn`'s two `publish` callers, which get no `prefixes` | `:671`, `:980` | **`asf/workers/spawn.py:745`** (`_rebase_onto_trunk`, `:720`) and **`:1060`** (`_publish_fresh_branch`, `:1054`) |
| `publish_gap`'s `lifecycle.publish` call | `health.py:411-413` | unchanged — **`:411-413`**, inside `publish_gap` `:389-418` |
| `refusal_text`, `republish_steps` and its refusal tail | `:498`, `:511-551` | unchanged — **`:498-508`**, **`:511-551`**, the tail **`:539-551`** |
| `_lane_branches`'s defensive read of `all_prefixes` | `:577` | unchanged — **`:577`**, `hasattr(product.conventions, 'all_prefixes')`; `prune_branches` at `:604` |
| the groom branch every groom and groom-clerk session is launched on | `asf/feeder/rows.py:1463` | **`:1628`**, `branch=branch_for(product, 'groom', date)` |
| the correction row's `'fix'`/`'task'` fallback | `:736` | **`:786-787`**, `kind = 'fix' if item['type'] == 'bug' else 'task'` |
| the real carrier branches PD3 is about | `tests/test_invariants_in_feeder.py:29-33` | **`:31-36`** — `cloud/tinkerer-mode-t1`, `cloud/tinkerer-mode-t2`, `cloud/voice-parity-t2`, `cloud/plan-F-0111`; `plan_carrier` is `asf/feeder/rows.py:1026`, read at `asf/reservations.py:129-131` |
| `UnpushedAfterARebaseTest`, the real-git publish fixture | `tests/test_lifecycle.py:1380-1424` | **`:1380-1423`**; its branches are `fix/B-9999` and `fix/B-9998`, `test_b0056_publish_never_targets_the_trunk` at **`:1464`** |
| `tests/test_health_publish_steps.py` has no harness | imports `threading`, `unittest`, `asf.workers.health` | unchanged — **`:6-9`**, one class (`RunStepsTest`, `:19`) |
| the `Home` harness import form | `tests/test_ended_run_liveness.py:29-30` | unchanged — `sys.path.insert(0, os.path.dirname(__file__))`, `from test_workers import Home` |
| the republish precedent to copy | `tests/test_workers.py:1991-2015` | unchanged — `TestHealth` at `:1598`, its `spawn`/`commit`/`land` helpers `:1599-1616`, `feature_row` `:49-50` |

**The prefix machinery, re-measured live at this head** (through `Conventions.from_mapping`, which
is how a product's yaml reaches it — `Conventions()` and `from_mapping({})` both default
`branch_prefixes` to a copy of `DEFAULT_BRANCH_PREFIXES`, so *a product naming nothing* still names
every default kind):

| product | `all_prefixes()` | `launch_prefixes()` as the Task defines it | `branch_kind('spec/f-0164')` |
|---|---|---|---|
| names nothing | `('cloud/direct-', 'worker/', 'plan/', 'spec/', 'fix/')` | `('cloud/direct-', 'worker/', 'groom/', 'plan/', 'spec/', 'task/', 'fix/')` | `spec` |
| `{code: feature/, fix: bugfix/}` | `('cloud/direct-', 'feature/', 'bugfix/')` | `('cloud/direct-', 'feature/', 'bugfix/', 'groom/', 'plan/', 'spec/', 'task/')` | **`None`** |
| `{code: feature/, legacy: [old/]}` | `('cloud/direct-', 'feature/', 'old/')` | `('cloud/direct-', 'feature/', 'groom/', 'plan/', 'spec/', 'task/', 'fix/', 'old/')` | `None` |

P8 holds exactly as the spec measured it — the second row is the regression D8 exists to prevent,
and it is why `all_prefixes()` may not simply be widened. PD2 holds too: `prefix('groom')` is
`groom/` and `prefix('task')` is `task/`, neither kind is in `DEFAULT_BRANCH_PREFIXES` or
`RECOGNISED_KINDS`, `branch('groom', '2026-09-30')` is `groom/2026-09-30`, and
`branch_kind('groom/2026-09-30')` is `None` for all three products. Note the second row carries no
`worker/`: a product that renames `code:` to `feature/` does not get `worker/` published, because
`worker/` is not a branch this factory would mint for *that* product — which is the definition
working, and PD3's `legacy:` answer for a branch left over from before a rename.

**PD4's probe, re-run at this head.** A read-only spy wrapped `lifecycle.publish`, recorded its
branch and its caller, and ran `tests.test_workers` (178 tests, `OK`, 54 s on this host — the plan
measured 260 s; host variance, not a change in shape). Fourteen calls, twelve distinct:
`publish_gap` is handed `spec/again`, `spec/carried`, `spec/dirty`, `spec/done`, `spec/pingpong`,
`spec/rebased`, `spec/refused`, `spec/stuck` — all eight under `spec/` — and `spawn`'s two callers
are handed `spec/cloudy` (`_publish_fresh_branch`) and `fix/B-copy`, `fix/B-merge`,
`spec/correct-f-0037` (`_rebase_onto_trunk`), which get no `prefixes` at all. The `Home` product
(`tests/test_workers.py:93-95`) declares no `conventions`, so its `launch_prefixes()` is the first
row of the table above and every one of those eight passes the guard. **The guard reddens nothing
in the suite**, measured, and `publish_gap` is still covered only by `TestHealth`: `grep -rn
'republish_steps\|publish_gap' tests/` is empty.

**The guard's one real cost, carried forward from PD3 with its anchors re-read.** A session's branch
is not always minted by `Conventions.branch`: `asf/feeder/rows.py:621` and `asf/reservations.py:131`
prefer a carrier — the branch a Feature's own ingest evidence says its document sits on — and the
four real ones quoted in `tests/test_invariants_in_feeder.py:31-36` are under `cloud/` but not under
`cloud/direct-`, so the guard refuses them. The cost of that refusal is a visible stalled publish
that prints once and leaves the run's own correction standing, never a deleted branch and never a
lost commit; the session's own push is untouched, because `spawn.push_allow`
(`asf/workers/spawn.py:217-228`) appends the session's own branch to `ASF_PUSH_ALLOW` whatever its
prefix. The operator's two answers are named in `launch_prefixes()`'s docstring (Step 2), and a
`legacy:` prefix hands `prune_branches` nothing it does not already have — `all_prefixes()` already
extends `legacy_prefixes()` (`asf/conventions.py:1062`).

**One shape the plan's PD2 did not reach, measured here, and deliberately not widened for.** Beside
`Conventions.branch`, there is a second minting path: `spawn.branch_for(product, row)` is
`row.branch or f'{product.branch_prefix(row.kind)}/{row.job}'` (`asf/workers/spawn.py:153-154`), and
`pool.Row.kind` defaults to `ACTION_KIND.get(action, action.lower() or 'task')` —
`{'FIX': 'fix-bug', 'SPEC': 'spec', 'PLAN': 'plan', 'BUILD': 'task', 'TASK': 'task'}`,
`asf/workers/pool.py:121`, `:136`. That fallback could mint `fix-bug/<job>` or
`<action.lower()>/<job>`, neither of which `launch_prefixes()` holds. It is unreachable from the
tick: `branch` is a **required** field of the feeder's `Row` (`asf/feeder/rows.py:209-217`), every
launch row sets it from `branch_for`/a carrier, and `step_wave.worker_row` passes it straight
through as `branch=row.branch or None` (`asf/tick/step_wave.py:531-534`). So the fallback serves
hand-built `pool.Row`s — tests, and a direct spawn — and `LAUNCH_KINDS` stays the two kinds a
`branch_for` call site actually passes. Step 1's comment is where a reader is told that, so the
next person to add a kind to `branch_for` adds it there too.

**The neighbouring guard this card does not touch.** `spawn.spawn_refusal`
(`asf/workers/spawn.py:201-214`) already asks *"is this a factory branch"* through
`_factory_branch` (`:185-191`), which reads `branch_kind` — and therefore carries P8's blind spot
too: for a product naming only some kinds it answers `None` for that product's own `spec/…` branch.
It fires only for a foreign-PR adoption row (`_foreign`, `:194-198`), it refuses a *launch* rather
than a push, and `tests/test_session_push_guard.py` pins it (7 tests, `OK`, 2 s at this head). It is
out of this footprint and out of this card: a second reader rewritten to the new list would be scope
the spec does not carry. It is the better home for `launch_prefixes` once this lands, and it wants
its own card.

**Baselines measured in this worktree before any edit**, so a coder can tell a red of their own from
a red that was already there: `python3 -m unittest -q tests.test_conventions
tests.test_health_publish_steps` → `Ran 55 tests in 33.700s … OK`; `tests.test_lifecycle` → `Ran 209
tests in 60.163s … OK`; `tests.test_workers` → `Ran 178 tests in 54.194s … OK`;
`tests.test_session_push_guard` → `Ran 7 tests … OK`; `bash tools/check_generic.sh` →
`check_generic: clean`; `python3 -m asf.cli redact --unpublished` → `redact: clean`, rc 0;
`python3 -m asf.conventions --forbidden | wc -l` → `13`. `bash tools/check_conventions.sh` could not
be run from this session either (the host refused the command, as PD15 recorded); it is in the Gate
and the coder runs it.

## What binds, and what leaves with the cut

Binding on the Task below, unchanged: the spec's **P7**, **P8**, **P9**, **D8** and its `## Out`
(`all_prefixes()` is not widened, `prune_branches` does not learn to see a stray, `spawn`'s two
callers get no `prefixes`); the plan's **PD1** (no line number in the spec is usable as written —
the table above carries this head's), **PD2** (the enumeration is completed, not shipped as the spec
wrote it), **PD3** (the carrier cost, accepted with the answer named), **PD4** (the guard reddens
nothing — re-measured above), **PD13** (the stray's fixture, and the one word that may not be
written down), **PD15** (the list may live only in `asf/conventions.py`), **PD16** (the `stories:`
line below mints no Story link: `plan_tasks.STORY_ID_RE` is `\bS-\d{4}\b`,
`asf/record/plan_tasks.py:29`, which a five-digit id never matches) and **PD17** (no id is minted
here).

**PD12 is binding on this Task now, not on a dropped one.** It put the module's first `Home` import
in the plan's Task 2; with that Task cut, `StrayBranchTests` is what adds it, and PD12's harness
form and its `mock.patch.object(lifecycle, 'publish', …)` + `pub.call_count` idiom are what Step 6
copies.

Leaving with the cut: **PD5**, **PD6**, **PD7**, **PD8**, **PD9**, **PD10**, **PD11** and **PD14** —
every one of them is about `run_steps`'s timing rows, the timeout's words, the counter, the cap or
the park. **D1**, **D2**, **D3**, **D4**, **D5**, **D6** and **D7** go with them. A coder reading
the plan must not implement them: `run_steps` keeps returning `None`, `asf/gitpush.py` is not
touched, `RUN_FIELDS` gains nothing, and `push_retry`, `RETRY_CLASSES` and `OUTCOME_CLASSES` stay
exactly as they are.

**No `after:` dangles.** T-0756 was `after: none` and stays `after: none`; it is the Feature's only
card, so no Task waits on a dropped one, and no `writes:` intersects another's.

---

### Task T-0756: the factory publishes only a branch it could have minted
stories: S-39702
writes: asf/conventions.py, asf/workers/lifecycle.py, asf/workers/health.py, tests/test_conventions.py, tests/test_lifecycle.py, tests/test_health_publish_steps.py
after: none

The spec's last two `## In` bullets and `## The design` §5, and after the cut the whole of F-0164's
open work: `conventions.launch_prefixes()`, `publish(..., prefixes=())` refusing in the guard that
already exists, and `publish_gap` passing the list. No push is attempted, so the refusal is free —
the stray that burned 240 s of the step the card measured costs nothing, prints once, and leaves the
run's own correction saying what it said.

**Read PD2 first, and do not ship the spec's enumeration as written**: it omits `groom/` and
`task/`, which this factory mints, and the guard would refuse every groom session's branch on every
product — the card's own defect, inverted. Read **PD3** (the carrier shape the guard does refuse,
and the operator's answer), **PD4** (the guard reddens nothing — re-measured at this head) and
**PD15** (why the list may live only in `asf/conventions.py`) too.

This Task does not touch `run_steps`, `asf/gitpush.py`, `RUN_FIELDS`, `push_retry` or the
publish-timeout count: that work is cut, and its two files have left this footprint.

**Files**

- `asf/conventions.py` — `LAUNCH_KINDS` beside `RECOGNISED_KINDS` (`:113`); `launch_prefixes()`
  beside `all_prefixes()` (`:1056-1063`). `all_prefixes`, `kinds`, `prefix`, `branch_kind` and
  `legacy_prefixes` are **not** touched (D8, P8, P9)
- `asf/workers/lifecycle.py` — `publish`'s signature (`:1689`) gains `prefixes=()`; its first guard
  (`:1732-1733`) gains the prefix test; the refusal's tail words become a module constant both
  sides read; the docstring gains the sentence PD3 names. `spawn`'s two callers
  (`asf/workers/spawn.py:745`, `:1060`) are not edited
- `asf/workers/health.py` — `publish_gap`'s `lifecycle.publish` call (`:411-413`) passes
  `prefixes=`; `republish_steps`'s refusal tail (`:539-551`) leaves a pending `UNPUSHED` correction
  alone when the refusal is this one
- `tests/test_conventions.py` — `LaunchPrefixesTests`, after `BranchTests` (`:107-135`) and before
  `PathTests` (`:137`)
- `tests/test_lifecycle.py` — `PublishLanePrefixTests`, over `UnpushedAfterARebaseTest`'s real-git
  fixture (`:1380-1423`), which is what `test_b0056_publish_never_targets_the_trunk` (`:1464`) uses
- `tests/test_health_publish_steps.py` — `StrayBranchTests`, and the module's first `Home` import
  (PD12)

**Steps**

1. `LAUNCH_KINDS` in `asf/conventions.py`, beside `RECOGNISED_KINDS` (`:113`), with a `#:` comment
   naming what it is and how it was read: *the kinds this factory mints a branch for that
   `DEFAULT_BRANCH_PREFIXES` does not name* — `('groom', 'task')`, from `asf/feeder/rows.py:1628`
   (every groom and groom-clerk session's branch) and `:786-787` (a correction row's fallback). The
   comment says the one thing a reader must know before editing either place: a kind added to
   `branch_for` and not to this tuple is a branch the factory can mint and will then refuse to
   publish. Measured, so the comment is true: `prefix('groom')` is `groom/`, `prefix('task')` is
   `task/`, and `branch_kind('groom/2026-09-30')` is `None` for every product in the table above.
   A tuple of kind *names* adds no pattern to the fence — `forbidden_patterns()`
   (`asf/conventions.py:434-450`) walks `DEFAULT_BRANCH_PREFIXES`'s values and module-level
   `DEFAULT_*` **strings** only — so do not name it `DEFAULT_…` and do not write a prefix in it.
2. `launch_prefixes()` with the spec's docstring byte for byte, plus PD3's sentence on the two
   answers for a product whose branches carry a shape it never declared (declare it as
   `branch_prefixes: {legacy: [cloud/]}`, or as a named kind of its own — the form
   `tests/test_conventions.py:108-111` already covers with `batch: m-`; a `legacy:` prefix hands
   `prune_branches` nothing new, because `all_prefixes()` already extends `legacy_prefixes()` at
   `:1062`). The list is `DEFAULT_BRANCH_PREFIXES`'s kinds less `legacy`, ∪ `RECOGNISED_KINDS`, ∪
   `LAUNCH_KINDS`, ∪ the kinds the product's own `branch_prefixes` names less `legacy` — each
   resolved through `prefix(kind)`, never a literal (PD15) — plus `legacy_prefixes()`,
   deduplicated and longest first, exactly the shape `all_prefixes()` returns (`:1056-1063`). Guard
   the mapping the way `kinds()` does (`isinstance(self.branch_prefixes, dict)`, `:1053`).
3. `publish` takes `prefixes=()` and refuses in the guard already there (`:1732-1733`):

   ```python
       if not branch or branch == main or (prefixes and not any(branch.startswith(p) for p in prefixes)):
           return False, f'publish refused: {branch or "no branch"} {NOT_A_LANE_BRANCH}'
   ```

   The **existing words**, before any push, before any rebase, before the archive, before
   `refguard.refusal`. `NOT_A_LANE_BRANCH = 'is not a lane branch'` is a module constant beside
   `publish`, so the one writer of those words and Step 5's reader of them cannot drift apart;
   nothing else in the package or the suite matches that text today (`tests/test_lifecycle.py:1467`
   asserts the substring and stays green). Empty `prefixes` means no such check, which is what
   leaves `spawn`'s two callers and every existing publish test untouched (PD4). Extend `publish`'s
   docstring with one sentence: which branches the list holds, and that a branch outside it is
   refused free, before any push, because the hook's run is what a publish costs.
4. `publish_gap` (`:411-413`) passes `prefixes=product.conventions.launch_prefixes()`, read
   defensively the way `_lane_branches` reads `all_prefixes` (`:577`) — `hasattr`, with `()` when a
   product object carries no real `Conventions`, so an empty list means the guard simply does not
   fire. This one call site covers all three publish sites of the pass (P1): the dead-pid re-judge,
   the live judgement and `republish_steps` all go through `publish_gap`.
5. `republish_steps`'s refusal tail (`:539-551`): a not-a-lane-branch refusal is recorded on
   `publish_refused` — so it prints once and not every tick — and the run's pending `UNPUSHED`
   correction is **left byte for byte as it was**, because `refusal_text`'s tail (*"the factory
   publishes, never a push of your own"*, `:507-508`) is false for a branch the factory has just
   declined to publish. Recognise it with `lifecycle.NOT_A_LANE_BRANCH in line` rather than
   `endswith` — `publish_gap` may return the refusal joined behind a `commit_leftovers` line
   (`'; '.join(lines + [line])`, `:415` and `:418`). Every other refusal keeps `refusal_text` as it
   is today, including the `redact:` one `tests/test_workers.py:1991-2015` pins.
6. The three test classes the acceptance describes, plus the two cases PD2 exists for.
   `StrayBranchTests` brings the module's first harness: `sys.path.insert(0,
   os.path.dirname(__file__))` then `from test_workers import Home`, the form
   `tests/test_ended_run_liveness.py:29-30` uses and the only one that loads under `python3 -m
   unittest tests.test_health_publish_steps` (PD12); `RunStepsTest`'s five existing cases
   (PD14) are not edited. Its fixture is a run with a real worktree on a branch under no launch
   prefix — `pool_mod.Row('stray-run', 'F-0001', kind='spec', branch='worktree-x-hotfix-v3')`, so
   `spawn` mints the worktree with `worktree add -b` and git, the ledger and the guard all agree.
   `spawn_refusal` does not refuse it: its branch test fires only for a foreign-PR row (`_foreign`,
   `asf/workers/spawn.py:194-198`), and this row's item is a Feature. Do **not** mock
   `lifecycle.publish` in this class — the guard under test is inside it; the proof that no push ran
   is that the refusal arrives with no remote ref and no rebase. **The label in the card's own
   Description is a worker-account name and is written nowhere** (PD13): `x` is the placeholder in a
   branch name, `worktree-<label>-…` the form in prose, and `python3 -m asf.cli redact
   --unpublished` is in the Gate.
7. Run `tests.test_workers` and `tests.test_lane` before and after. The probe above measured that
   nothing there goes red; if something does, that fixture's branch is under no launch prefix, and
   the fix is to rename the fixture branch to a minted shape — **never** to weaken the guard or to
   widen `LAUNCH_KINDS` for a name no `branch_for` call site passes. `tests/test_workers.py` is not
   in this Task's `writes:`: a rename there is a `needs writes:` line in the REPORT, not a quiet
   edit. `python3 -m asf.conventions --forbidden | wc -l` must still be `13`.

**Gate**

```bash
python3 -m unittest tests.test_conventions tests.test_health_publish_steps -v
python3 -m unittest tests.test_lifecycle tests.test_workers tests.test_lane -v
python3 -m unittest tests.test_session_push_guard -v
bash tools/check_generic.sh
bash tools/check_conventions.sh
python3 -m asf.cli redact --unpublished
```

`tools/check_conventions.sh` is load-bearing for this Task, not routine: it is the fence that fails
if a branch-prefix literal reaches any file but the four it exempts (PD15), and
`asf/conventions.py` is the only one of those this Task writes. `tests.test_session_push_guard` is
in the Gate as the cheap proof that `spawn`'s own factory-branch guard and `push_allow` — the other
two readers of this product's prefixes — were left alone (7 tests, 2 s).

**Acceptance**

```bash
python3 -m unittest tests.test_conventions.LaunchPrefixesTests -v
python3 -m unittest tests.test_lifecycle.PublishLanePrefixTests -v
python3 -m unittest tests.test_health_publish_steps.StrayBranchTests -v
```

`LaunchPrefixesTests` — for a product naming nothing, `launch_prefixes()` holds `worker/`, `fix/`,
`spec/`, `plan/` and `cloud/direct-`; for one whose yaml says `{code: feature/, fix: bugfix/}` it
holds `feature/`, `bugfix/`, `spec/`, `plan/`, `cloud/direct-` — the P8 case, where `all_prefixes()`
holds three and `branch_kind('spec/f-0164')` is `None`; a product with `legacy: [old/]` keeps `old/`;
the list is longest-first and deduplicated; and `all_prefixes()` returns exactly what it returns
today for all three (the pruner is untouched, D8). Build each product with
`Conventions.from_mapping`, not `Conventions(...)`: the table of measured values above is through
`from_mapping`, and the bare constructor takes the mapping as the whole of `branch_prefixes`.

`PublishLanePrefixTests` — over the same real-git fixture the existing publish tests use:
`publish(wt, 'worktree-x-hotfix-v3', prefixes=<launch_prefixes>)` is refused with `is not a lane
branch` and `ls-remote` shows no such ref (nothing was pushed, and no rebase ran); the same call with
`prefixes=()` publishes, so `spawn`'s callers are unaffected; a `spec/…` branch of a product naming
only `code:`/`fix:` **is** published (the regression D8 exists to prevent); `publish(wt, 'main', …)`
is refused as it is today.

`StrayBranchTests` — an ended unpushed run on a branch under no lane prefix: the pass records the
refusal on `publish_refused`, leaves the run's pending `UNPUSHED` correction byte-for-byte as it was
(no `refusal_text` rewrite), prints the line once, and prints nothing on the next pass; a run on a
lane branch in the same pass is published normally.

Beyond the spec's own words, and required by PD2: `launch_prefixes()` holds `groom/` and `task/` for
a product naming nothing, and a run on `groom/<date>` is published, not refused.

Baselines to beat, measured at `875df70c5` before any edit: `Ran 55 tests … OK` for
`tests.test_conventions` and `tests.test_health_publish_steps` together, `Ran 209 tests … OK` for
`tests.test_lifecycle`, `Ran 178 tests … OK` for `tests.test_workers`, `check_generic: clean`,
`redact: clean`. The new classes are more tests and no fewer.

---

coverage after the cut: 1/3 stories (S-39702); uncovered: S-39700, S-39701

NEEDS OPERATOR: F-0164 — the cut leaves the Feature's own title half-answered and no card now
carries it. A lane branch whose `pre-push` hook outlives `git.push_timeout_s` is still republished
every pass, for ever, at 120 s a tick, with no count, no cap, no park and no next action (spec P4,
P5; the plan's Task 2, S-39701), and the split of the health step is still unmeasured (S-39700).
Mint one or both if they should be fixed: `asf new bug "health republishes a branch whose pre-push
hook outlives git.push_timeout_s for ever — no count, no cap, no next action"` and `asf new feature
"the health pass says what its publishes cost"`. Both are fully written out already and can be
lifted verbatim — the design at `docs/specs/f-0164.md:147-308`, the Steps at
`docs/plans/f-0164.md:89-211` (the measurement) and `:215-376` (the count, the cap and the park),
with PD5–PD12 and PD14 carrying every anchor and every compile-level correction a coder needs.
