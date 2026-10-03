replan: F-0211 cdcfd081dba8

The card's mechanism was never the work. The spec's finding is that all five clauses the customer
asked for landed in one commit — `f5816335c`, 2026-09-24, whose `Fixes:` trailer is this card's own
inbox slug — and that **no file under `asf/` changes** (O1, D9). What was left was three surfaces
pinned to a routing that was already live: the guide, the example file, and the one Task that is not
documentation at all — the pin that keeps both yaml forms a product file may actually hold.

The guide has landed. `T-0685` is on the trunk at `b9c8c10ef` ("the product-config guide documents
model routing, quoted from doctor itself"), and with it the surface the customer actually reported
against: `docs/guide/product-config.md:280-341` is now a `### models` subsection whose built-in
table is `asf doctor`'s own output quoted line for line, whose override block shows all three
shapes an operator may write (`:327-332`), and whose `default:` trap paragraph (`:334-338`) is the
single most useful sentence the spec argued for at D5.

**The groom's reshape cuts to Task 3, and Task 3 is the pin.** One Task rewritten, one dropped, none
minted. The decision only removes, so nothing here adds scope the spec does not carry.

| Task | what the cut does to it |
|---|---|
| T-0685 | **landed** — kept exactly as it is, at `b9c8c10ef`. Nothing in this replan touches it, and nothing reverts it |
| T-0686 | **dropped** — the example product file. The stale per-kind lists stay stale; §*Drop* measures what that costs and names the card it wants |
| T-0687 | **rewritten** — the pin, and now the whole of F-0211's open work. Its justification gets *stronger* for the cut: the three shapes it drives through a real product file are the three shapes the landed guide tells an operator to write |

**What the cut changes about the pin, in one sentence.** The plan justified T-0687 against the
card's own line — the flow-form map a customer would copy out of the backlog. T-0685 landed that
line into the guide, at `docs/guide/product-config.md:328`, beside a plain label (`:327`) and an
indented block map (`:329-331`). So the pin stops guarding a line in a card and starts guarding the
worked example the product ships to every operator. The Task's footprint, its fixtures and its four
methods are unchanged by that — the plan already wrote exactly these three shapes — but its Steps
now cite the landed text rather than a sibling Task's future one.

**Verified at this head (`bf45bb402`) rather than read out of the plan.** The plan's anchors into
`tests/test_briefs.py` have all shifted and the rewrite below carries the re-measured ones:

| What | Plan said | Holds now |
|---|---|---|
| `KindModelGrantTest` | `:984-1083` | **`:1012-1130`** |
| its `BUG` staticmethod | `:1006` | **`:1034`** |
| `CheapTierTests` | `:1104` | **`:1132`** |
| the import block, `from asf.env import Product` | `:24-28`, `:27` | unchanged — `:24-28`, `:27` |
| `build_mod = importlib.import_module('asf.briefs.build')` | `:32` | unchanged — `:32` |
| `product(**extra)` helper | `:65` | unchanged — `:65` |
| `MODEL_CLASSES`, `MODEL_TABLE['review']`, `DEFAULT_MODELS`, `item_class`, `model_for`, `model_table` | `:55`, `:75`, `:87`, `:204`, `:219`, `:235` | all unchanged |

And the behaviour the Task asserts, re-measured through the real reader on 2026-10-03, not reasoned
about — for each of the three shapes, `env.validate_product_text` returns `[]`, `env.loads` yields
the nested map, and `model_for` answers:

| fixture | parsed `conventions.models` | S2 Bug | S1 Bug | feature | `spec` |
|---|---|---|---|---|---|
| indented block, with `default: light`, no `feature:` | `{'review': {'S1': 'heavy', 'S2': 'light', 'default': 'light'}, 'spec': 'heavy'}` | `light` | `heavy` | **`light`** | `heavy` |
| the guide's flow form, naming `feature: heavy` | all five keys, nested | `light` | `heavy` | **`heavy`** | `heavy` |
| a plain string, `review: light` | `{'review': 'light'}` | `light` | **`light`** | `light` | `heavy` |

`MODEL_TABLE['review']` is `{'default': heavy, 'S1': heavy, 'S2': light, 'S3': light, 'task':
light}`, so the block form's `light` for a Feature review is the built-in's `heavy` overridden by a
`default:` the operator did not mean as a floor — the trap, asserted as behaviour. Baseline green
before any edit: `python3 -m unittest tests.test_briefs` → `Ran 121 tests in 0.152s … OK`;
`tests.test_briefs.KindModelGrantTest tests.test_briefs.CheapTierTests` → `Ran 20 tests … OK`;
`bash tools/check_generic.sh` → `check_generic: clean`.

**Nothing of T-0687 has landed, and nothing of it is reverted.** `grep -c
ModelMapThroughTheProductFile tests/test_briefs.py` is `0` at this head, and `tests/test_briefs.py`
carries no reference to the name `env` anywhere in its import block. The standing rule that a
replan never reverts landed work bites only on T-0685, which this replan leaves untouched.

**No `after:` dangles.** Every Task of this Feature was `after: none` — D8's argument was three
disjoint `writes:` sets in one wave — so the drop of T-0686 leaves no card waiting on it. T-0687's
`writes:` is `tests/test_briefs.py` alone, which intersects neither T-0685's three landed paths nor
T-0686's two dropped ones.

The plan's PD6, PD7, PD10, PD11, PD12, PD13 and PD14 bind this replan unchanged:
`check_conventions.sh` scans `asf/**/*.py` only and is a no-op guard that this Task stayed out of
`asf/` (PD6); no Gate runs a full-suite command (PD7); the `stories:` id below mints nothing —
`plan_tasks.STORY_ID_RE` is `\bS-\d{4}\b` (`asf/record/plan_tasks.py:29`), which a five-digit id
never matches, and `stories_of` (`:38`) filters again to canonical ids (PD10); no card id is
invented here (PD11); `writes:` is comma-separated and identical to its **Files** block (PD12); no
forbidden name, no person, vendor or host word, and no absolute machine path appears in this
document or in anything the Task writes (PD13); and nothing under `asf/` is in the footprint, which
is the card's spine (PD14).

---

### Task T-0687: a models map reaches model_for through a real product file — every shape the landed guide shows
stories: S-37402
writes: tests/test_briefs.py
after: none

The spec's I6 and `## The design` §5's `tests/test_briefs.py` half, and after the cut the whole of
F-0211's open work. This is the gap the spec's P11 names and the only one that was never
documentation: **all three shapes work today, and nothing keeps them working.** Every existing case
builds `product(conventions={'models': …})` as a Python dict (`tests/test_briefs.py:1070-1081`), so
the yaml subset reader and `env.validate_product_text` have never been asked whether the shape the
guide now ships is a shape a product file may hold. A change to either could take it away in
silence, and the operator's copied line would stop resolving with nothing red.

**What the cut changes.** The guide landed first (T-0685, `b9c8c10ef`), so the three shapes this
Task drives through the reader are no longer a line in a backlog card — they are
`docs/guide/product-config.md:327-332`, the block an operator copies:

| the guide's line | this Task's fixture |
|---|---|
| `coder: heavy` — a plain label, every class of the kind | `STRING`, as `review: light` |
| `review: {S1: heavy, S2: light, S3: light, feature: heavy, default: light}` | `FLOW` — the flow form, byte for byte |
| `fix-bug:` / `  S1: heavy` / `  default: light` — the indented map | `BLOCK`, as `review:` with `default: light` and no `feature:` |

D4 is why **both** map forms are asserted and not just one: they take different paths through the
yaml subset reader, the guide shows both, and P11 was a hand check rather than a guard. Each shape
is driven through `validate_product_text` **and** `env.loads` **and** `model_for`, so one test
covers the reader, the validator and the resolution.

Read **PD2** first — `from asf.briefs import build` returns the `build` *function*, not the module,
which is why `:32` asks for the module by name.

Nothing under `asf/` moves (O1, D9, PD14). One test module, one new class.

**Files**

- `tests/test_briefs.py` — a new `ModelMapThroughTheProductFileTests(unittest.TestCase)` after
  `KindModelGrantTest` (`:1012-1130`) and before `CheapTierTests` (`:1132`); and `from asf import
  env` added to the import block (`:24-28`)

**Steps**

1. Add `from asf import env` to the import block (`:24-28`). Leave `from asf.env import Product`
   (`:27`) exactly as it is — other classes in the module use it and the two names do not collide.
   Use the existing `build_mod` (`:32`) for `model_for`; do **not** add a second
   `importlib.import_module` of that module, and do **not** write `from asf.briefs import build`,
   which binds the entry-point function and makes every `build.DEFAULT_MODELS` an `AttributeError`
   (PD2). `build_mod.env` also resolves — the module is already reached that way at `:1227` — but
   the module-scope import is the clear form and is what this Task writes.
2. `ModelMapThroughTheProductFileTests`, a plain `unittest.TestCase`, with a docstring naming F-0211
   and P11: a `conventions.models` map reaches `model_for` through a *product file* — the yaml subset
   reader and the product validator, not a dict built in Python — and the shapes are the ones
   `docs/guide/product-config.md:327-332` tells an operator to write. Three class-level fixtures as
   tuples of lines, joined with `'\n'.join(...)` at use:

   ```python
       BLOCK = ('repo_slug: a/b', 'conventions:', '  models:', '    review:',
                '      S1: heavy', '      S2: light', '      default: light', '    spec: heavy')
       FLOW = ('repo_slug: a/b', 'conventions:', '  models:',
               '    review: {S1: heavy, S2: light, S3: light, feature: heavy, default: light}')
       STRING = ('repo_slug: a/b', 'conventions:', '  models:', '    review: light')
   ```

   Do **not** use the module's `product()` helper (`:65`) for these: it builds a `Product` from a
   dict, which is the very path this class exists to bypass. Build each one as `env.Product('x',
   env.loads(text))`. A small `_product(self, lines)` helper that joins the lines, asserts
   `env.validate_product_text(text) == []` and returns the `Product` keeps the four methods to their
   own point; reuse `KindModelGrantTest`'s `BUG = staticmethod(lambda sev: {'type': 'bug',
   'severity': sev})` idiom (`:1034`) rather than inlining that dict five times.
3. `test_the_block_form_validates_parses_and_resolves` — over `BLOCK`:
   `env.validate_product_text(text)` is `[]`; `env.loads(text)['conventions']['models']` is
   `{'review': {'S1': 'heavy', 'S2': 'light', 'default': 'light'}, 'spec': 'heavy'}` — assert the
   parsed **nested map itself**, not merely that it is a dict, because the nesting is the thing the
   reader could take away; then `model_for(p, 'review', BUG('S2'))` is `light` and `model_for(p,
   'review', BUG('S1'))` is `heavy`. All of it measured at this head.
4. `test_the_guides_flow_form_validates_parses_and_resolves` — the same three assertions over
   `FLOW`, whose map parses to all five keys including `feature`, plus `model_for(p, 'review',
   {'type': 'feature'})` is `heavy`. The flow form names `feature` explicitly, so it is the shape
   that keeps a Feature-level review heavy while making S2 cheap — which is the card's own ask,
   written as the guide writes it. Measured `heavy`.
5. `test_a_map_default_covers_a_class_the_map_does_not_name` — the trap asserted as **behaviour**,
   over `BLOCK`, which names no `feature:` and does carry `default: light`: `model_for(p, 'review',
   {'type': 'feature'})` is `light`, where the built-in row says `heavy`
   (`MODEL_TABLE['review']`, `asf/briefs/build.py:75`). Measured `light`. Assert in the same method
   that `model_for(p, 'spec')` is still `heavy` — `spec: heavy` in `BLOCK` is a string override
   agreeing with the built-in, and the point is that a map on one kind does not disturb another.
   **This is the assertion the landed guide's trap paragraph rests on**
   (`docs/guide/product-config.md:334-338`, which tells the operator in so many words that
   `review: {S2: light, default: light}` buys a light Feature review). Until this method exists, the
   most useful sentence in the guide is documentation of behaviour that no test holds in place.
6. `test_a_string_in_a_product_file_still_covers_every_class` — over `STRING`:
   `validate_product_text` is `[]`, the parsed value is the plain string `{'review': 'light'}`, and
   `model_for` answers `light` for `{'type': 'feature'}` **and** for `BUG('S1')`, where the built-in
   routes an S1 review to `heavy`. Measured both. This is clause 4 of the card — a plain string keeps
   working — now through a product file rather than a Python dict.
7. `KindModelGrantTest` (`:1012-1130`) and `CheapTierTests` (`:1132`) are **not edited** — nothing
   under `asf/` moves, so every one of their assertions must read exactly as it does today (O1, D9).
   If one of them goes red, the cause is in this Task's own diff and not in the product.
8. No fixture writes a file to disk and none needs a products directory: `env.loads` and
   `env.validate_product_text` are both over text. Keep it that way — a test that wrote a product
   yaml into a temp directory would be pinning `env.load_product`, which is a different function and
   not this card's.
9. The example product file is **out of this footprint**, and deliberately: T-0686 is dropped, so
   `docs/products.example.yaml` keeps its stale per-kind lists and its `review: light` worked
   example. Do not fix it here while passing — that is a second card's diff wearing this Task's id
   (§*Drop* names it). This Task writes one file.

**Gate**

```bash
python3 -m unittest -v tests.test_briefs.ModelMapThroughTheProductFileTests
```

```bash
python3 -m unittest -v tests.test_briefs.KindModelGrantTest tests.test_briefs.CheapTierTests
```

```bash
python3 -m unittest -v tests.test_briefs
```

```bash
bash tools/check_generic.sh && bash tools/check_conventions.sh
```

**Acceptance**

The spec's §3.3 fence, byte for byte:

```bash
python3 -m unittest tests.test_briefs -v
```

`ModelMapThroughTheProductFileTests`: for the indented block form, the guide's flow form and a plain
string, `validate_product_text` returns `[]`, `env.loads` yields the parsed value, and `model_for`
on the resulting `Product` answers `light` for an S2 Bug review and `heavy` for an S1; the flow form
answers `heavy` for a Feature review where the block form's `default: light` with no `feature:`
answers `light`, while `spec` keeps `heavy` in both; and the plain string covers every class
including S1. `KindModelGrantTest` and `CheapTierTests` stay green unchanged — nothing under `asf/`
moves (O1, D9).

Baseline to beat, measured at `bf45bb402` before any edit: `Ran 121 tests in 0.152s … OK` for
`tests.test_briefs`, and `Ran 20 tests … OK` for `KindModelGrantTest` and `CheapTierTests` together.
The four new methods are four more tests and no fewer.

The fences (PD6 — `check_generic` is the one that can see a test module, via `asf.redact --tree`
over every tracked file but LICENSE; `check_conventions` scans `asf/**/*.py` only and is here as the
cheap no-op that confirms this Task stayed out of `asf/`):

```bash
bash tools/check_generic.sh
bash tools/check_conventions.sh
```

---

### Drop T-0686: the example product file is the surface the cut removes — the guide already owns the map form, and what stays wrong here wants its own card

T-0686 was the spec's I5 and I7's example half: replace `docs/products.example.yaml:116-125` so the
block shows the map form and points at `asf doctor` instead of restating a per-kind routing, delete
the three per-kind label lists D2 argues are the thing that rots, and add two methods to
`DocumentedModelsTests` in `tests/test_env.py` — one asserting the map example is present, one
guarding against a reintroduced list.

**The cut is to the pin, and this Task is the third documentation surface.** The spec named three
surfaces plus one pin (D8). The first surface landed. The pin is the only open work that is not
prose about a routing already live and already quoted from the tool, and it is where the decision
cuts. The example file is prose about that routing.

**The guide now owns the map form, which is why dropping this costs less than it did when the plan
was written.** T-0686's own headline deliverable — "shows the map form the card asked for" — is
`docs/guide/product-config.md:328` today, inside a subsection whose built-in table is `asf doctor`'s
output asserted line for line and whose trap paragraph is spelled out. An operator looking for how
to write the setting has a correct, test-pinned answer in the guide. The example file is no longer
the only place that could have told them.

**What stays wrong, stated plainly rather than buried.** Measured at this head, 2026-10-03:
`docs/products.example.yaml:118-120` still carries the three label lists that predate `f5816335c` —

```
  #   heavy  judgement — spec, spec-amend, plan, review, adjudicate, groom, reshape
  #   light  code      — coder, fixer, fix-bug, correct
  #   cheap  clerical  — rebase, close, groom-clerk
```

— `:121` still says "Unset for a kind means the default above", and `:124-125` still offer
`review: light` as the block's only worked example. Two defects survive the drop:

1. **One line is factually wrong about a per-kind default.** `adjudicate` is listed under `heavy`;
   `DEFAULT_MODELS['adjudicate']` is `light` (measured). Every other kind on those three lines
   resolves to the label it is listed under, so this is the one outright error, and it is the error
   D2 and §3.2 predicted the guard would catch.
2. **The deeper one no per-kind claim can fix.** `review` under `heavy` and `correct` under `light`
   are each *true as a default* and *misleading as a routing*: `MODEL_TABLE['review']` is
   `{'default': heavy, 'S1': heavy, 'S2': light, 'S3': light, 'task': light}`, so a reader of this
   block still learns that every review runs heavy — which is the customer's original complaint,
   restated in the file the customer is told to copy. A per-kind list cannot express a per-class
   table. That is D2's whole argument, and the drop leaves it standing.

Neither is a regression and neither is new: both have been in the file since `f5816335c` on
2026-09-24, and the drop changes nothing about them except that this Feature stops planning to fix
them. The example file is documentation of a default, not a decision the factory reads — nothing
resolves a model label from it, `models:` is commented so no `Product` loaded from it carries a
`models` key at all (D3, PD8), and `env.validate_product_text` returns `[]` for the file as it
stands. So the cost is a reader misled about a default they can check with one command the guide now
points them at, not a session launched on the wrong model.

**It wants its own card, and the groom should mint one.** The work is small and fully specified
already — the replacement block is written out at `docs/specs/f-0211.md:187-205` and
`docs/plans/f-0211.md:300-318`, the guard's exact semantics are settled at PD5 (split the tail on
commas, assert `DEFAULT_MODELS[token] == label` for every token that is a kind, skip a token that is
not), and PD8 records that the whole replacement was applied in memory with every existing
assertion of `DocumentedModelsTests` and `test_products_example_parses_into_a_product` surviving it.
A future card can lift all of it verbatim. This replan mints no id for it: a replan writes no card,
and `NEEDS OPERATOR` below says what the groom is being asked for.

**Nothing of T-0686 has landed, so the drop reverts nothing.** Verified at this head rather than
read out of the record: `docs/products.example.yaml:116-125` is unchanged from the lines the plan
quoted, `grep -c 'the_example_shows_the_map_form' tests/test_env.py` is `0`, and
`DocumentedModelsTests` (`tests/test_env.py:31-49`) still holds exactly its two original methods.

**The drop dangles no `after:`.** No Task of this Feature ever named T-0686 — all three were
`after: none` by D8, which chose three disjoint `writes:` sets precisely so that a red in one held
neither other. T-0687 above stays `after: none`, and its `writes:` does not intersect the two paths
leaving the footprint (`docs/products.example.yaml`, `tests/test_env.py`). Both of those paths leave
F-0211 entirely.

NEEDS OPERATOR: F-0211 — the dropped T-0686's two defects in `docs/products.example.yaml:118-121`
survive this cut and no card now carries them. Mint one if they should be fixed: `asf new bug
"docs/products.example.yaml lists adjudicate under heavy, and its per-kind label lists cannot
express the per-class table"` — the replacement block and the guard are already written out at
`docs/specs/f-0211.md:187-205`, `docs/plans/f-0211.md:300-318` and PD5.
