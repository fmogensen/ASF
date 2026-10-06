# Handoff: g1g3 (G1 stop re-asking, G3 loop guard) — for a cloud session

Spec: G1 + G3 of the code-over-LLM evaluation (operator 2026-10-06). Generic, configurable, no product names, ASF never special-cased.

## Done (commit f75bebbfe)
- asf/workers/loops.py: readers for flags relaunch_cap (2), loop_cap (3); new relaunch_same_report (2), relaunch_daily_cap (6/24 h); report digest (shas masked); same-report park; daily cap; counterfactual ledger replay.
- asf/workers/relaunch.py: assess() takes the guard settings; loop_key per park so one loop = one alarm.

## Left
1. G3 wiring: step_wave passes caps/guard, stamps report_key on each run, prints one `ALARM loop` line per new loop key; pass loop_cap into lifecycle.hold from health.py and lane.py; register flags in conventions.KNOWN_FLAGS; document in docs/products.example.yaml.
2. G1: "Features without Stories" / "Stories without Tasks" become report lines (flag groom.structural); re-ask only when the card digest changes or after groom.reask_days (7); record adjudicator/operator answers in a small state file; digest excludes fields + History lines the answer writes; groom.rank_owner: code|adjudicator (default code: adjudicator `rank <n>` answers not applied).
3. Evidence for tests (from the replay): 51 loop series of ≥4 sessions (468 sessions) in 7 days; correct sessions loop on moving heads with one report (B-0178: 13 sessions/7 heads/1 report; T-0641: 13/8/2); repeated parks (114, 187) are alarm noise. Build anonymised fixtures for these shapes and pin sessions avoided.
4. Hermetic tests (fail first), `python3 tools/run_tests.py` targeted modules, all tools/check_*.sh, a CHANGELOG notes line per the release workflow, one PR, merge only with `gh pr merge <n> --squash --match-head-commit <sha>` when required checks are green on that exact head; then delete the branch.
