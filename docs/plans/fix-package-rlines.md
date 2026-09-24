# Fix package: R-lines to tests

The review checklist of `fix-package.md` §10 (R1–R26), checked by code (§11).
`tools/package_gate.py` reads this table. Every R-line must appear exactly once, with one of
three statuses:

- **test**: every test named must exist and pass in the gate's run.
- **pending**: owned by a stream that has not landed yet. The line is a gap until every test
  named passes. The stream then marks it `test`, renaming the ids here if its names differ.
- **n/a**: a process or live step that no suite test can witness. The reason is required, and
  the line names no test.

Test ids are `path::Class::test`. The tag waits for `python3 tools/package_gate.py` to exit 0.

| R-line | status | tests | why |
| --- | --- | --- | --- |
| R1 | test | `tests/test_lane.py::LaneRepo::test_r1_lane_state_lives_on_the_run_line` | lane stream |
| R2 | test | `tests/test_lane.py::LaneRepo::test_r2_the_in_process_pass_stops_at_the_gate_and_the_gate_pass_lands` | lane stream |
| R3 | test | `tests/test_lane.py::Overrides::test_r3_merging_intent_is_ours` | lane stream |
| R4 | test | `tests/test_lane.py::Overrides::test_r4_a_moved_head_in_any_open_state_is_pushed_again`, `tests/test_lane.py::Overrides::test_r4_review_currency_follows_the_head` | lane stream |
| R5 | test | `tests/test_lane.py::Overrides::test_r5_queued_merged_is_ours_rejected_waits`, `tests/test_lane.py::Overrides::test_r5_in_queue_counts_toward_the_merge_budget` | lane stream |
| R6 | test | `tests/test_lane.py::Overrides::test_r6_a_reopened_pr_leaves_stale` | lane stream |
| R7 | test | `tests/test_lane.py::LaneRepo::test_r7_a_docs_branch_the_gate_refuses_goes_back_to_a_starved_plan_session`, `tests/test_e2e_lane.py::DocsRedOnGatePR::test_r7_fault1_docs_pr_red_on_the_product_gate_never_merges`, `tests/test_e2e_lane.py::DocsRedOnGateFF::test_r7_fault1_docs_pr_red_on_the_product_gate_never_merges` | lane stream, W5 harness |
| R8 | test | `tests/test_lane.py::Overrides::test_r8_a_crash_after_the_push_is_closed_at_the_pushed_sha`, `tests/test_lane.py::Overrides::test_r8_a_crash_before_the_merge_is_gated_again`, `tests/test_e2e_lane.py::CrashAfterMergePR::test_r8_crash_between_merge_and_ledger_is_recoverable`, `tests/test_e2e_lane.py::CrashAfterMergeFF::test_r8_crash_between_merge_and_ledger_is_recoverable` | lane stream, W5 harness |
| R9 | test | `tests/test_invariants.py::R9SoftFailure::test_r9_a_check_that_raises_is_a_line_never_an_abort`, `tests/test_invariants.py::R9SoftFailure::test_r9_feeder_drops_the_violating_row_and_logs_it`, `tests/test_invariants.py::R9SoftFailure::test_r9_feeder_facts_that_fail_keep_every_row`, `tests/test_invariants.py::R9SoftFailure::test_r9_the_lane_facts_the_lane_cannot_give_yet_are_no_finding`, `tests/test_invariants.py::R9RecordStepInTheTick::test_r9_record_reverts_only_the_offending_path_and_files_one_bug`, `tests/test_tick_steps.py::WaveStepTests::test_r9_the_feeder_check_point_drops_a_violating_row_before_the_wave` | W4 |
| R10 | test | `tests/test_invariants.py::R10I4OneLaunchPerBranchOfItsKind::test_r10_the_review_launch_on_a_review_branch_is_legal`, `tests/test_invariants.py::R10I4OneLaunchPerBranchOfItsKind::test_r10_a_coder_on_a_branch_the_lane_holds_is_dropped`, `tests/test_invariants.py::R10I4OneLaunchPerBranchOfItsKind::test_r10_back_takes_a_correction_not_a_review`, `tests/test_invariants.py::R10I4OneLaunchPerBranchOfItsKind::test_r10_at_most_one_launching_row_per_branch`, `tests/test_invariants.py::R10I4OneLaunchPerBranchOfItsKind::test_r10_occupancy_answers_for_a_branch_the_lane_has_no_record_of` | W4 |
| R11 | test | `tests/test_record_stage.py::R11I2AfterRestamp::test_r11_i2_after_restamp` | W2 |
| R12 | test | `tests/test_invariants.py::R12I9IsAnEvent::test_r12_a_human_merge_is_an_event_not_a_violation`, `tests/test_invariants.py::R12I9IsAnEvent::test_r12_the_lane_report_logs_the_event_and_returns_no_finding`, `tests/test_e2e_lane.py::HumanMergePR::test_r12_human_merge_is_landed_not_foreign` | W4 (the event), W5 harness (the human merge lands) |
| R13 | test | `tests/test_invariants.py::R13TestsOnlyInvariants::test_r13_i6_i12_i13_are_never_tick_checks`, `tests/test_invariants.py::R13TestsOnlyInvariants::test_r13_i6_deep_only_rederives_a_copy_and_names_a_non_idempotent_field`, `tests/test_invariants.py::R13TestsOnlyInvariants::test_r13_i13_an_explicit_type_decides_the_minted_type`, `tests/test_metrics.py::ReleaseHonesty::test_i12_release_notes_list_only_resolved_or_closed_as_landed`, `tests/test_metrics.py::ReleaseHonesty::test_i12_the_check_names_an_open_item_listed_as_landed` | W4 |
| R14 | test | `tests/test_contracts.py::StubsImport::test_record_stage`, `tests/test_record_stage.py::Stage::test_r14_stage_captures_one_writers_paths_and_their_content_before_it`, `tests/test_record_stage.py::Stage::test_r14_one_writer_refused_the_other_writers_output_stands`, `tests/test_record_stage.py::Stage::test_r14_guarded_prints_and_keeps_the_finding_for_the_bug_filer`, `tests/test_record_stage.py::Stage::test_r14_a_created_path_refused_is_removed` | W0 contract, W2 |
| R15 | n/a |  | process: `integration/fix-package` was cut from 74497d9, which contains e284ee7 (`git merge-base --is-ancestor e284ee7 74497d9`) |
| R16 | test | `tests/test_lane.py::Occupancy::test_r16_the_feeder_reads_the_lane` | W1 and W3 built as one lane stream: the feeder reads the lane |
| R17 | test | `tests/test_contracts.py::ConventionKeysParse::test_the_keys_from_yaml`, `tests/test_contracts.py::ConventionKeysDefault::test_the_defaults`, `tests/test_contracts.py::WorkerPoolKeys::test_parse_and_defaults` | W0 |
| R18 | n/a |  | process: main is merged into `integration/fix-package`, never rebased onto it |
| R19 | test | `tests/test_e2e_lane.py::HappyPathPR::test_r19_s1_card_to_merge_resolves_the_feature`, `tests/test_e2e_lane.py::HappyPathFF::test_r19_s1_card_to_merge_resolves_the_feature`, `tests/test_e2e_lane.py::RedTrunkPR::test_r19_s2_red_trunk_waits_then_lands`, `tests/test_e2e_lane.py::RedTrunkFF::test_r19_s2_red_trunk_waits_then_lands`, `tests/test_e2e_lane.py::SquashMergePR::test_r19_s4_landed_task_closes_open_sibling_keeps_feature_open`, `tests/test_e2e_lane.py::SquashMergeFF::test_r19_s4_landed_task_closes_open_sibling_keeps_feature_open`, `tests/test_e2e_lane.py::PreexistingPRPR::test_r19_s5_open_pr_is_adopted_not_rebuilt`, `tests/test_e2e_lane.py::PreexistingPRFF::test_r19_s5_open_pr_is_adopted_not_rebuilt` | W5 harness, both landing modes |
| R20 | test | `tests/test_lifecycle.py::LaunchAndReapInvariants::test_an_ended_runs_worktree_under_a_differently_cased_home_is_reused`, `tests/test_lifecycle.py::LaunchAndReapInvariants::test_a_live_run_in_a_differently_cased_worktree_refuses_every_other_job`, `tests/test_invariants.py::I7OneSessionPerBranchAndWorktree::test_i7_the_worktree_key_is_the_case_folded_path` | case-insensitive paths. The other blind spots are covered elsewhere: load timeouts by a suite run under CPU load before the tag, real gh semantics by R21's live smoke, hand-made yaml and foreign hooks by the existing unit tests |
| R21 | n/a |  | live: `tools/smoke_isolated_session.sh` runs one real worker session with the isolated HOME before the tag. A suite has no account |
| R22 | test | `tests/test_snapshot.py::SnapshotTickTest::test_r22_a_tick_started_while_the_checkout_is_rewritten_imports_one_consistent_tree`, `tests/test_snapshot.py::SnapshotTickTest::test_r22_ticks_during_a_rewrite_loop_all_import_head` | W6 |
| R23 | test | `tests/test_contracts.py::WorkerPoolKeys::test_accounts_carry_the_keys_and_home_stays_a_path` | W0: `home` stays a path as v0.1.2 reads it, and `isolate_home` is a key v0.1.2 ignores |
| R24 | test | `tests/test_feeder.py::FeederHoldTest::test_features_held_waits_their_new_work_and_nothing_else`, `tests/test_feeder.py::FeederHoldTest::test_bugs_held_waits_the_fix` | e284ee7: `feeder.hold` |
| R25 | test | `tests/test_lane.py::LandingClass::test_r25_a_docs_only_trunk_move_does_not_regate_a_green_branch` | lane stream |
| R26 | n/a |  | cut for Saturday (§9 item 8): the status, tick-log, daily-stamp and init polish (W4), install.sh and RunAtLoad (W6), and card hygiene |
