# Handoff: g4g5 (G4 shadow decision ledger, G5 cheap tier/tune) — for a cloud session

Spec: G4 + G5 of the code-over-LLM evaluation (operator 2026-10-06). Generic, configurable, no product names, ASF never special-cased.

## Done (WIP commit)
- asf/shadow.py: every decision recorded (state/<p>/shadow-<decider>.jsonl), N = distinct cases, outcome beats incumbent (daily check after shadow.outcome_days=7: diff if reopened), one-sided 95% upper bound (~3/N at 0 diffs), off/shadow/on per decider; facts/mechanical/roots manual-flip only.
- `asf deciders` (+ --json).
- facts.shadow and facts.landing.shadow write every decision.
- G5: model_for reads models.cheap_kinds (S1 excluded, conventions.models.<kind> override wins); tune.lookback_days.<kind> (plan 7); tune skips a cheaper step mapping to the same model.

## Left
1. Verify tune.step() does not use `state` before it is loaded.
2. Cloud cost: estimate spend for unpriced cloud runs in improve/measure.ended_runs, marked estimated (no usage data on cloud runs).
3. Register shadow.*, tune.lookback_days.*, the cost key in asf/config_keys.py; document in docs/config.example.yaml incl. tune bounds for groom/replan/plan.
4. Hermetic tests (fail first), targeted modules, all tools/check_*.sh, CHANGELOG notes line, one PR, merge only with `gh pr merge <n> --squash --match-head-commit <sha>` when green on that head; delete the branch. First-pass green is NOT yours (#820 builds it).
