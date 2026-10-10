# HANDOFF: g4g5 (code-over-LLM evaluation, G4 + G5)

Branch `feat/g4g5`, rebased on origin/main v0.1.169. Delete this file in the final PR.

## Spec
- G4: adopt `asf/shadow.py` (from the WIP commit ecc981aaa, branch `feat/code-over-llm-b`) as a decision log with off/shadow/on modes per decider and a countable "0 diffs over N". It must:
  - count DISTINCT cases;
  - record EVERY decision;
  - compare against the OUTCOME where possible;
  - report the statistical upper bound on the error rate at N.
- Deciders that close, remove or push stay manual-flip only. Add `asf deciders`, and put `facts` on the ledger.
- Cloud cost: price the cloud sessions, which are unpriced today, from the run's usage if available, else an estimate marked as such. First-pass green belongs to the ci-measure agent (#820); do not duplicate it.
- G5: in `asf/tune.py`, let groom, replan and plan be tunable kinds, and make the `cheap` tier real (`models.cheap_kinds` had no reader). No model names in code: labels come from `worker_pool.models`.
- Rules: generic, with no product, account or provider names. Every policy value is a config key in `asf/config_keys.py` and `docs/config.example.yaml`. Each new path is a no-op for a minimal product. Hermetic tests come first. One PR; merge with `gh pr merge <n> --squash --match-head-commit <sha>` after green. Never touch the live install `/Users/frank/Code/fmogensen/ASF`, `~/.ASF` config, or any product checkout.

## Done (untested)
- `asf/shadow.py`: the ledger is `state/<product>/shadow-<decider>.jsonl`, registered as `shadow-*.jsonl` in `asf/state/registry.py`.
  - `decide()` records every decision; an identical repeat on the same case is skipped.
  - `outcome()` records what happened later. `cases()`, `tally()` and `upper_bound()` give a Clopper-Pearson one-sided bound, with strata.
  - `DECIDERS` covers facts (closes), mechanical (pushes) and roots (removes); `auto_cutover()` is False for all three. `mode()` maps the flags to off/shadow/on.
  - `settle_closes()` and `settle()` settle a `landed` decision once it is older than `shadow.outcome_days`: diff if the card's `reopened:` marker comes after the decision, else same.
  - `asf deciders [--product] [--json]` is wired in `asf/cli.py`.
  - The daily step (`asf/tick/step_daily.py`) has a `deciders` part that calls `shadow.settle`.
  - Settings `shadow.confidence` (0.95), `shadow.outcome_days` (7), and `shadow.min_n` (a number or a map per decider).
- facts: `asf/facts/__init__.py:shadow` and `asf/facts/landing.py:shadow` call `asf.shadow.decide` on every decision. The landing path carries `code_says` and `item`; `item` is passed from `shadow_run` and `shadow_earlier`, or taken from the key when the key is a card id. `facts-disagree.jsonl` is unchanged.
- G5:
  - `asf/briefs/build.py`: `cheap_kinds(product)` reads `flags.models.cheap_kinds`, and `model_for` returns `cheap` for listed kinds. S1 rows are excluded, and a `conventions.models.<kind>` override still wins.
  - `asf/tune.py`:
    - New `tune.lookback_days.<kind>` (default `{plan: 7}`): a run counts toward a trial, watch or baseline only after it ended that many days ago.
    - `step()` loads runs since the oldest live trial or watch.
    - `candidates(..., cfg)` skips a cheaper label that maps to the same runtime model (`cheap` falling back to `light`).
    - Groom, replan and plan rows already reach `tune.place` through the wave with brief kinds, so no allow-list was needed. Verify this in a test.

## Left: exact next steps
1. Cloud cost in `asf/improve/measure.py:ended_runs`:
   - When `usd is None` and `cloud`, estimate `usd = minutes × the median $/min` of priced runs. Use the same (kind, model) first, then the kind, then all priced runs.
   - Add a `usd_estimated: bool = False` field to `Run`.
   - Add `unpriced_sessions` and `estimated_sessions` to `table()`.
   - Add a config key, e.g. `cost.cloud_estimate: rate|off`, default `rate`.
   - The cloud status file's `last_run` carries no usage. A `result` line in the job log, if one exists, still wins.
2. Register config keys in `asf/config_keys.py`: `shadow.confidence`, `shadow.outcome_days`, `shadow.min_n`, `shadow.min_n.*`, `tune.lookback_days.*`, and the cost key. Document them in `docs/config.example.yaml`, adding example tune bounds for groom, plan and replan (`[heavy, light, cheap]`). Mention `asf deciders` and `cheap_kinds` in `docs/guide/product-config.md`.
3. Write the tests:
   - `tests/test_shadow_ledger.py`. Do not reuse `tests/test_shadow.py`, which tests `asf/tick/shadow.py`. Cover: distinct cases, an identical repeat not written, agreements recorded, outcome beating incumbent, `upper_bound(0, 100)` ≈ 0.0295 and k>0 monotone, strata, `mode()` words including facts old/new, manual cutover, `settle_closes` reopened → diff, `cmd_deciders`.
   - Extend `tests/test_tune.py`: plan lookback holds a verdict until matured; the same-runtime cheap step is skipped; groom/plan/replan bounds trial.
   - Extend `tests/test_briefs*.py` (find `model_for` tests): `cheap_kinds` list and string forms, S1 excluded, override wins.
   - Extend `tests/test_facts.py` and `tests/test_facts_landing.py`: an agreement now writes a shadow decision.
   - Add a measure test for the cloud estimate.
4. Run: `python -m pytest -n 2 tests/test_shadow_ledger.py tests/test_tune.py tests/test_facts.py tests/test_facts_landing.py tests/test_config_keys.py tests/test_env.py tests/test_tick_steps.py tests/test_tick.py tests/test_release.py tests/test_scorecard.py` plus the briefs and measure tests (or the repo's own runner, `-j 2`). Run all `tools/check_*.sh`. The repo's genericity check runs in `bun run check`/CI.
5. Add a CHANGELOG notes line following the PR convention (see `.github/workflows` and #767). Remove this HANDOFF.md, open one PR, and merge on green with `--match-head-commit`.
6. Cleanup on the Mac: remove the worktree `/private/tmp/claude-501/asf-wt-code-over-llm-b` and the local branch `feat/code-over-llm-b`, which is now adopted. Its `merge_rules.py` was deferred per the evaluation. Then remove the `feat/g4g5` worktree and branch.
