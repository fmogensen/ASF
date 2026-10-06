#!/bin/sh
# F-0092 — mint the nine Stories this spec-amend derived.
#
# Written by the spec-amend session asf/spec-amend-f-0092@20261006T162808Z, which ran in a
# cloud container with no record: no ~/.ASF, no backlog_dir, so `asf new` could not run
# there. The ids below are this session's claimed block (S:50350-50399). Run this on the
# factory host, from the record checkout, in this order: mint_id takes the next free
# number of the claimed block, so the ids it prints are exactly the ones already written
# into docs/specs/f-0092.md's ## Stories.
#
# Each command prints its id. Check each printed id against the comment above it.
set -e
export BACKLOG_ID_RANGE='S:50350-50399,T:50350-50399,B:50350-50399'
export ASF_SESSION='asf/spec-amend-f-0092@20261006T162808Z'

# expect: S-50350
asf new story \
  --parent F-0092 \
  --title '`conventions.budget` — three sessions or $10 and the two run caps, per product, one place for the defaults' \
  --acceptance '`Conventions()` gives `budget == {'"'"'sessions'"'"': 3, '"'"'usd'"'"': 10, '"'"'run_minutes'"'"': 180, '"'"'run_turns'"'"': 600}` — proven by tests/test_conventions.py' \
  --acceptance '`from_mapping({'"'"'budget'"'"': {'"'"'sessions'"'"': 5, '"'"'run_minutes'"'"': '"'"'off'"'"'}})` overrides those two keys and leaves the other two at their defaults — proven by tests/test_conventions.py' \
  --acceptance '`budget_for(conv, '"'"'usd'"'"')` reads a key the product did not name — proven by tests/test_conventions.py' \
  --acceptance 'A `budget:` that is a string is `{}` through `map_of`, is named by `shape_findings()` and does not raise — proven by tests/test_conventions.py' \
  --acceptance '`'"'"'budget'"'"'` is in `MAP_CONVENTIONS` — proven by tests/test_conventions.py' \
  --acceptance '`tools/check_conventions.sh` passes: the four numbers appear nowhere else under `asf/` — proven by tests/test_conventions.py'

# expect: S-50351
asf new story \
  --parent F-0092 \
  --title '`asf/budget.py` — the item verdict, the per-item override and the one line it prints' \
  --acceptance '`budget.of` resolves default then product then item, and `budget_sessions:` / `budget_usd:` on the card each win on their own — proven by tests/test_budget.py' \
  --acceptance 'An Epic gets `Budget()` whatever its `budget_usd` — proven by tests/test_budget.py' \
  --acceptance '`budget.spent` over `cost: {sessions: 9, usd: 13.53}` with the default budget is over on `sessions`, and over `{sessions: 2, usd: 16.59}` is over on `usd` — proven by tests/test_budget.py' \
  --acceptance '`{sessions: 3, usd: 1}` is over — the third session is the last one the budget buys — proven by tests/test_budget.py' \
  --acceptance '`{sessions: 2, usd: None}` is not over, and a card with no `cost:` block is not over — proven by tests/test_budget.py' \
  --acceptance '`sessions: off` leaves only the money measure — proven by tests/test_budget.py' \
  --acceptance '`budget.line('"'"'T-0021'"'"', s)` is exactly `OVER BUDGET T-0021 — 9/3 sessions, $13.53/$10`, and prints `$—` in place of a cap that is off — proven by tests/test_budget.py'

# expect: S-50352
asf new story \
  --parent F-0092 \
  --title 'The rollup writes the budget and the verdict into the card'"'"'s `cost:` block' \
  --acceptance '`write_costs` over a record with one Task at 9 sessions / $13.53 writes `cost.budget_sessions: 3`, `cost.budget_usd: 10` and `cost.over_budget: sessions` beside the figures it already wrote' \
  --acceptance 'The machine block'"'"'s key order is unchanged by those three keys' \
  --acceptance 'With `conventions.budget.sessions: 12` the same record writes `budget_sessions: 12` and no `over_budget`' \
  --acceptance 'A card raised to `budget_sessions: 12` by hand loses its `cost.over_budget` key on the next run' \
  --acceptance 'An Epic gets neither budget key and keeps its `spend_usd` / `budget_usd` behaviour, `cmd_rollup`'"'"'s `budget:` lines byte-identical'

# expect: S-50353
asf new story \
  --parent F-0092 \
  --title 'The feeder launches no work row for an item whose budget is spent' \
  --acceptance 'Over the matrix of rows that can speak for one item — `PLAN → CODE`, `FIX → CORRECT` at 1 and 3 rounds, `CARD → SPEC`, `CARD → SPEC+PLAN`, `DIRECT → BUILD`, `STARVED → SPEC`, `STARVED → PLAN`, `BUG → FIX`, `STALEMATE → ADJUDICATE` — and over both measures, no row of the over-budget item launches' \
  --acceptance 'Exactly one `OVER BUDGET` row is emitted for that item, with `waits_on == '"'"'budget'"'"'`' \
  --acceptance 'The same matrix with the budget raised on the card launches as it does today' \
  --acceptance 'A `PUSHED → REVIEW`, a `CONFLICT → REBASE` and a `STALE → CLOSE` row still launch for an over-budget item' \
  --acceptance 'A `PUSHED → LAND` / `ON TRUNK` row is passed through unchanged' \
  --acceptance 'A sibling Task under its own budget is untouched'

# expect: S-50354
asf new story \
  --parent F-0092 \
  --title 'The tick prints one `OVER BUDGET` line, and I14 drops a row that got past the gate' \
  --acceptance '`asf tick --steps record,wave` over a fixture whose Task is over budget prints exactly one `OVER BUDGET T-0021 — 9/3 sessions, $13.53/$10` line, printed whole, with no `waits` prefix' \
  --acceptance 'That tick launches nothing for the over-budget Task and launches its sibling' \
  --acceptance 'The budget invariant over a hand-made plan carrying a launching `PLAN → CODE` row for that item returns one `Finding`' \
  --acceptance '`feeder_gate` drops that row and prints its `INVARIANT <n>: <row> — budget spent: …` line' \
  --acceptance 'The same plan with the finishing kinds, or with an under-budget item, gives no finding'

# expect: S-50355
asf new story \
  --parent F-0092 \
  --title 'The groom asks for a ruling and the `budget <n> [$<usd>]` answer raises it' \
  --acceptance '`groom_over_budget_section` asks one line per open over-budget card, naming both measures and the three answers — proven by tests/test_groom.py' \
  --acceptance 'It asks nothing for a card that is under its budget, closed, reshaped or already carrying the raise — proven by tests/test_groom.py' \
  --acceptance '`budget 12` on that line writes `budget_sessions: 12`, one History line and one `groom_answer` event — proven by tests/test_groom.py' \
  --acceptance '`budget 12 $25` writes both fields — proven by tests/test_groom.py' \
  --acceptance 'A second `--apply` of the same file changes nothing — proven by tests/test_groom.py' \
  --acceptance '`no: not worth more` closes the card as any `no` does — proven by tests/test_groom.py' \
  --acceptance 'An unparseable `budget soon` is skipped with the "not an answer the grammar knows" line naming `budget <n> [$<usd>]` — proven by tests/test_groom.py' \
  --acceptance 'With `approvals.groom: auto` the adjudicate session'"'"'s `adjudicator: budget 12` is applied and attributed — proven by tests/test_groom.py' \
  --acceptance 'The section is rendered only when it has lines, and `_line_sections` attributes its lines to `over_budget` — proven by tests/test_groom.py'

# expect: S-50356
asf new story \
  --parent F-0092 \
  --title 'A run over its wall clock or its turn count is ended and recorded `run cap`' \
  --acceptance '`budget.run_over`: 214 minutes over 180 is `('"'"'run_minutes'"'"', 214, 180)`, 180 exactly is not over, and 601 turns over 600 is `('"'"'run_turns'"'"', 601, 600)` — proven by tests/test_budget.py' \
  --acceptance 'The wall clock is named before the turns when both are over, a `None` elapsed judges the turns alone, and `off` on both is never over — proven by tests/test_budget.py' \
  --acceptance '`budget.run_caps` reads the product'"'"'s `run_minutes` / `run_turns` — proven by tests/test_budget.py' \
  --acceptance '`tokens.meter` counts `turns` per run and resets them at an `init` boundary, and `run_turns` is not in `tokens.DIMENSIONS` — proven by tests/test_tokens.py' \
  --acceptance '`stall.capped` over a live session whose `started` is 4 hours ago stops it, appends a result whose `asf.run_cap` names `run_minutes`, marks the registry line and prints `CAP   <job>  run_minutes 240 over 180 (<kind>)` — proven by tests/test_tokens.py' \
  --acceptance '`runtime.failure_reason` of that record is `run cap`, and still `token cap` for an `asf.cap` one — proven by tests/test_budget.py' \
  --acceptance '`lifecycle.outcome_class('"'"'failed: run cap'"'"')` is `run cap`, not `other`, and both cap classes are in `OUTCOME_CLASSES` — proven by tests/test_lifecycle.py' \
  --acceptance 'A run that already has a result is untouched by the run cap, and `stop=False` still judges only — proven by tests/test_tokens.py' \
  --acceptance 'The tokens table'"'"'s `capped` cell counts a run-capped session'

# expect: S-50357
asf new story \
  --parent F-0092 \
  --title 'The health step is the caller: a capped run is ended the same tick' \
  --acceptance '`step_health.run` calls `stall.capped(product)` before `health(fix=True)`, in that recorded order' \
  --acceptance 'One tick over a live session past its wall clock prints the `CAP` line, then health'"'"'s `ended` line for the same job with `end_reason: failed: run cap`' \
  --acceptance 'The `sessions` stream carries that `result`' \
  --acceptance 'A tick with nothing live prints `cap: none` and is otherwise unchanged'

# expect: S-50358
asf new story \
  --parent F-0092 \
  --title '`asf backlog` shows the over-budget items and sorts by them' \
  --acceptance '`board.render` puts `· over: T-0021` in the Cost cell of the Feature whose subtree holds it, and `· over: 2` for two' \
  --acceptance 'A Feature with no over-budget item in its subtree gets nothing in that cell' \
  --acceptance '`asf backlog --sort budget` puts the Feature with over-budget items first within its Epic, then by spend' \
  --acceptance '`--sort rank` is byte-identical to today'"'"'s table'
