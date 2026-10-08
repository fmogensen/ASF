#!/bin/sh
# F-0129 — mint the six Stories this spec declares.
#
# Written by the adjudicate session asf/adjudicate-f-0129@20261008T004209Z, ruling on the
# STALEMATE (NO STORIES -> SPEC-AMEND, 3 rounds) this item hit. Three prior spec-amend sessions
# (almost certainly also cloud sessions: no ~/.ASF, no backlog_dir, no `asf` on PATH) could
# declare these Stories in docs/specs/f-0129.md's `## Stories` section — the one thing a cloud
# session can do (asf/briefs/build.py's STORIES_FIRST fallback) — but could never mint a card
# for one: `asf new` has no record to write to there. That is why NO STORIES kept firing: the
# Feature's `has_stories` check (asf/feeder/rows.py) reads the record's own children, never a
# spec's prose, and a cloud session cannot add one. The ids below are this session's claimed
# block (S:69554-69603). Run this on the factory host, from the record checkout, in this exact
# order: mint_id takes the next free number of the claimed block, so the ids it prints are
# exactly the ones already written into docs/specs/f-0129.md's `## Stories`.
#
# Each command prints its id. Check each printed id against the comment above it.
set -e
export BACKLOG_ID_RANGE='S:69554-69603,T:69550-69599,B:69550-69599'
export ASF_SESSION='asf/adjudicate-f-0129@20261008T004209Z'

# expect: S-69554
asf new story \
  --parent F-0129 \
  --title '`asf config check` names every product-file key the schema does not know as written' \
  --acceptance '`asf config check --product P` prints one row per key the schema does not know as written, each row carrying that key'"'"'s 1-based line in the file' \
  --acceptance 'each row names its kind — `renamed`, `retired`, `no home`, `shape` or `duplicate`' \
  --acceptance 'a key the rename table holds reports the current dotted key it maps to' \
  --acceptance 'a key with no home in the schema reports `no home in the product schema` with its note, and names the command that files a card for it' \
  --acceptance 'a close match the rename table does not hold is printed as a best-effort suggestion (`customer_path` → `customer_paths`) and is never acted on' \
  --acceptance '`asf config check` exits 1 only when the file does not load as it stands — `env.product_problems`'"'"' errors non-empty — and 0 for a file that loads carrying warnings or advisories alone' \
  --acceptance 'a file whose only finding is an old `conventions.*` spelling exits 0, since `conventions:` keeps the keys it does not recognise' \
  --acceptance '`--json` prints the same rows as objects, one per row the table printed' \
  --acceptance '`asf config check` writes no file and no backup anywhere'

# expect: S-69555
asf new story \
  --parent F-0129 \
  --title '`asf config migrate` applies the known renames and keeps every unknown key' \
  --acceptance 'a key the rename table holds is rewritten to its current key in place, and moved into its current section, the section created when it is absent' \
  --acceptance 'an unknown key'"'"'s whole block is moved under the parking block with a comment naming the key and why it is parked, and the comment lines that sat above it are kept' \
  --acceptance 'the migrated file loads through `env.load_product`, and every parked leaf value is still readable in it' \
  --acceptance 'a run that changes something writes `<file>.bak.<UTC stamp>` beside the file, and never overwrites a backup already there' \
  --acceptance 'a second run writes neither the file nor a second backup, asserted on the filesystem rather than on the output' \
  --acceptance 'every byte the migration does not move is unchanged, the file'"'"'s comments included'

# expect: S-69556
asf new story \
  --parent F-0129 \
  --title 'a migration never loses a value and never changes what the factory already does' \
  --acceptance 'a rewrite whose text would not load is refused, leaving the file and its backup untouched' \
  --acceptance 'a rewrite that would drop a leaf value is refused, leaving the file untouched' \
  --acceptance 'a rewrite that would change a value the factory already reads is refused, while a key that was ignored before and is read after is allowed through' \
  --acceptance 'a rename whose target key is already set is reported and left where it is, and the rest of the file is still migrated' \
  --acceptance 'a duplicate key is reported with both its lines and left alone, since the reader keeps the last value and moving either could change what loads' \
  --acceptance 'a value of the wrong shape is reported with its line and the shape wanted, and is never coerced'

# expect: S-69557
asf new story \
  --parent F-0129 \
  --title 'the key list is derived from the schema, and the guide'"'"'s table cannot drift from the code'"'"'s' \
  --acceptance 'the current-key list is read from `env.PRODUCT_FIELDS` and `env.NESTED_FIELDS` at call time, so a field added to the schema is understood with no edit to the new module' \
  --acceptance 'a field removed from the schema stops being offered as a rename target' \
  --acceptance 'the guide'"'"'s product-file rename table and the code'"'"'s rename and retirement tables hold the same rows, asserted in both directions' \
  --acceptance 'the operator-config rows and the clock-shape row stay out of the machine-checked product-file table'

# expect: S-69558
asf new story \
  --parent F-0129 \
  --title 'every place a product file'"'"'s load failure surfaces names the command that explains it' \
  --acceptance 'the `NEEDS OPERATOR` line printed for a `ConfigError` names `asf config check --product <p>`' \
  --acceptance 'that output stays exactly one line on stdout, begins `NEEDS OPERATOR: `, still carries `asf init --product <p>`, and exits 2 — proven by tests/test_cli.py' \
  --acceptance 'the doctor'"'"'s `config` row detail ends in `asf config check --product <p>` when the product file will not load' \
  --acceptance '`asf upgrade`'"'"'s per-product row carries both the `ConfigError` it already printed and `asf config check --product <p>`' \
  --acceptance 'an advisory doctor row counts the parked keys and the old-but-still-read spellings the file carries, and never changes `asf doctor`'"'"'s exit code'

# expect: S-69559
asf new story \
  --parent F-0129 \
  --title 'a wrong-shaped or unrecognised key is a named finding, never a silent ignore and never a crash' \
  --acceptance '`ci:` and `deploy_sha:` each carry a declared shape — a map, or the bare word `none` — so a list-shaped block is an error at load naming its line, instead of a section every reader silently skips' \
  --acceptance '`ci: none` and `deploy_sha: none` still load, as `sample/product.yaml` and `tests/e2e/product/product.yaml` both write them' \
  --acceptance 'an unknown product-file key is reported with its line and never refuses the load — proven by tests/test_env.py' \
  --acceptance 'the doctor'"'"'s `product` row names each unknown key as a `warn` row and never goes red — proven by tests/test_doctor_pin.py' \
  --acceptance 'the prod view renders a list-shaped and an empty `customer_paths` without crashing — proven by tests/test_prod_view.py' \
  --acceptance '`docs/products.example.yaml` validates with no problems — proven by tests/test_env.py'
