# HANDOFF — F-0247 sections A + C (config keys)

The card is F-0247 (`~/Code/fmogensen/ASF-backlog/features/F-0247.md`). Section B already landed in #772.
The build rules are in handbuild-rules: generic, every key registered in `asf/config_keys.py` and
documented in `docs/config.example.yaml`, hermetic tests, and a merge only with
`gh pr merge <n> --squash --match-head-commit <sha>` once the required checks are green on that exact head.

## Done
- **Slice 1: PR #821**, branch `feat/config-keys`, head `d31f73bd0`, rebased on v0.1.169 and pushed.
  - The previous head `c418ead` was green on tests (3.12/3.13) and notes. The new head needs CI again.
  - What it adds:
    - `config_keys.TYPED`, `value()`, `problems()` and the doctor warn row.
    - The top tunables are wired: round, relaunch and lifecycle caps, the headroom cost table and families, git, gh and setup timeouts, the PR list limit, the tick lock waits, the watchdog, the ci_queue timings and the flaky title.
    - `DEFAULT_WORKER_ENV = {}`.
    - `factory.git_identity` is registered.
    - The `cloud.*` keys are registered one by one.
    - Every registered key is documented.
  - Tests: `tests/test_config_keys.py` (including PinnedReader), `tests/test_tick.py::MinimalProductTick`, `tests/test_worker_env.py`.
- **Slice 2: this branch** `feat/config-keys-2`, WIP, cut from the OLD slice-1 head `c418ead`.
  - About 47 more keys are registered in `TYPED`, under "slice 2", and documented.
  - The wiring: each module gets a `TUNABLES` dict and `tunable(name)` at its end.
    - Modules: trunkmerge, ci_measure, ci_census, trunk_red, tick/file_bugs, tick/flaky, spawn, worktrees, upgrade, stale_ref, gh_limit, ci_pool (with ci_cancels and metrics), trunk_watch, harvest/transplant (with lane), tick/dry_run, tokens (with stall), progress, workers/seats, reservations, workers/answer, workers/judged, groom/policy, scheduler, metrics/metrics.
    - Default args that named a constant are now `None` and resolved in the body.
  - The `Tunables` test class in `tests/test_config_keys.py` checks every TUNABLES entry: its key is typed and its constant passes the check.

## Left: the exact next steps
1. Merge #821 once the required checks are green on `d31f73bd0`. If main has moved again, rebase it, run `tests.test_config_keys tests.test_tick tests.test_doctor`, and push again.
2. Rebase `feat/config-keys-2` onto the new main once #821 is merged. Slice 1's commit is squashed, so drop it: `git rebase --onto origin/main c418ead`.
3. Fix the two local failures in slice 2:
   - `tests.test_progress.LeafImportTests.test_no_asf_import_but_asf_env_and_asf_detach`: `progress.py`'s `tunable()` imports `asf.config_keys`. Either allow `asf.config_keys` in that leaf test (it imports only `os`, plus `asf.env` lazily), or read through `asf.env`.
   - `tests.test_scheduler` has 5 failures: the plist render, `status`, the installed-but-undeclared tests and the pause test. They are probably caused by wiring `MIN_EVERY_S`/`QUEUE_EVERY_S` (a module-level use, or a test that patches `scheduler.QUEUE_EVERY_S`). Check `git diff c418ead -- asf/scheduler.py`, and revert the scheduler wiring if it is not clean.
4. Run these modules with `-j 2`, as in `python3 -m unittest`:
   - test_config_keys, test_ci_cancels, test_ci_census, test_ci_measure, test_ci_pool, test_dry_run, test_dry_run_venv, test_file_bugs, test_flaky, test_gh_limit, test_harvest, test_lane, test_lane_off_tick, test_metrics, test_progress, test_reservations, test_scheduler, test_tokens, test_transplant, test_trunk_red, test_upgrade, test_workers, test_relaunch, test_groom*, test_tick, test_tick_steps.
   - Then all of `tools/check_*.sh`.
5. Open the PR. Its body needs a `What changed for you:` line, which the notes job requires. Merge on green with `--match-head-commit`. Then remove both worktrees (`/private/tmp/claude-501/asf-wt-config-keys*`) and both branches.

## Not done, by decision
- Card item 5 (premium models): already configurable as the product key `improve.premium_models`.
- `roles.py` shape limits: these validate shipped role files and are not tunables.
- `reserve.MAX_ATTEMPTS` and `step_wave.REPLANS`: internal loop bounds.
- `hermetic.DEFAULT_TRUNK`: callers pass the product's trunk.
- The `merge_queue` and `flake` values are already product keys.
- Product to inform: one that relied on the `VITEST_MAX_WORKERS=2` default must set `conventions.worker_env` once it moves past this release. Products pinned to an older version keep the old default.
