# T-0528 review round 1 — working notes

Heartbeat: the brief's background git-push heartbeat (force-push to refs/asf/hb/review-t-0528)
is refused by this sandbox's permission classifier (same refusal the coder session hit and
recorded in its own report's "Assumptions"). Not started; no workaround attempted. Progress is
reported via visible output instead.

## Status of the six-row verdict table

1. diff stays inside writes: — PASS. All 9 changed files match the brief's writes: list exactly
   (asf/tick/step_health.py, asf/workers/health.py, asf/workers/lifecycle.py, asf/workers/stall.py,
   docs/guide/operating.md, tests/test_ended_run_liveness.py, tests/test_lifecycle.py,
   tests/test_readme.py, tests/test_workers.py).
2. every Step of the Task is implemented — PASS, checked Task 1 (health.py: own_process,
   QUIESCE_GRACE_S, quiet_for, settle_quiesced, stopped_pids, not_alive, wiring at the right
   point, docstring paragraph at :22, QuiescedLiveRunTests with all enumerated cases), Task 2
   (lifecycle.py judge guard above landing but below `landing=lands(run)` per PD13, docstring
   paragraph, classify's `or reason` fallback, health.py:632 PD1a fix, health.py:5 bullet,
   JudgementInvariants new tests, SessionStateInvariants PD1b test, TestHealth new tests), and
   Task 3 (stall.classify rewrite matching D8/D9, step_health.py docstring, operating.md rows,
   TestStall new cases, SessionLivenessProseTests). All verified line-by-line against the plan
   docs/plans/f-0160.md. No gaps found.
3. acceptance tests byte-identical to plan's — the plan doesn't give literal test-code blocks
   for this card (unlike some plans); it gives gate *commands* and prose-described cases. All
   gate commands used below are copied verbatim from the plan. Test cases match every clause
   enumerated in Steps 10/11 (Task1), 6-10 (Task2), 7-8 (Task3). PASS.
4. tests run and green — ran tests.test_ended_run_liveness.QuiescedLiveRunTests (11 OK),
   tests.test_ended_run_liveness + tests.test_workers.TestHealth (53 OK), and the big combined
   tests.test_lifecycle tests.test_workers tests.test_cloud tests.test_views tests.test_tick
   tests.test_readme (722 OK). PASS.
5. Gate commands green — check_conventions.sh clean, check_generic.sh clean. `python3
   tools/run_tests.py` (the full-suite gate every Task's Gate block ends with) is running in the
   background (task b0jf9851t) — PENDING, waiting on it before closing the row.
6. no secret printed / no background process / no skipped check — grepped diff for
   secret/password/token-print patterns: none. No skipped checks found. PASS (pending final
   confirmation once full suite output is in).

## Next
- Read task b0jf9851t's output once it completes; if red, check whether it's the same
  pre-existing baseline reds the coder's report already named (test_workers /
  test_sample_product non-F-0160 cases) — if so still PASS per the coder's own verified baseline
  comparison, note under I list, not C.
- Write docs/reviews/1-t-0528.md with the verdict table, C list (expect empty or very small —
  review found no defects so far), I list, and verdict block with head 2b3af7350b0710bc610fd1a7d47de9e5df8b9179.
- Never git add/commit/push this review file.
