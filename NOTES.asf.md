# T-0591 notes

## Status
Continuing a stalled run. Prior session (per CONTINUE block) reported:
- All 14 PathFilteredRequiredCheck tests passing.
- Full tests.test_ci_pool + tests.test_lane green (264 tests).
- docs/guide/product-config.md and docs/products.example.yaml edited for the
  path-filtered provenance clause.
- It then ran `tests.test_ci_pool tests.test_lane tests.test_env tests.test_readme`
  and was killed (exit 137, OOM/timeout) before finishing.

## What's uncommitted right now
Need to check `git status`/`git diff` in worktree to see what the stalled run
left behind before redoing any work.

## Next
1. Inspect current diff state vs the described progress.
2. Re-run the gate test modules individually (avoid running all 4 heavy modules
   at once again — likely what triggered exit 137) to confirm green.
3. Run tools/check_generic.sh, tools/check_conventions.sh,
   tools/run_tests.py --touched origin/main --shards 2.
4. Commit with Proves: trailers, sign off, push worker/T-0591.

## Open questions
None yet — investigating current state.
