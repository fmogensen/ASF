# T-0363 review round 1 — notes

- heartbeat script blocked by harness (brace/quote obfuscation detection) — known issue (memory:
  feedback_heartbeat_blocked), not retrying.
- origin/main has moved 55 commits past the branch's merge-base (384df7eba); diffed against
  merge-base instead of origin/main directly — diff is exactly writes: rules.py, file_bugs.py,
  test_file_bugs.py (210 insertions, 3 deletions). Matches plan footprint.
- Read plan Task T-0363 in docs/plans/replans/f-0069-cf8d63721a07.md (lines 32-107). All 7 Steps
  verified against diff — match.
- Tests run and green:
  - RuleViolationFieldTests + SecretAlertTests: 8 tests OK (matches writer report)
  - tests.test_file_bugs full: 52 tests OK (matches writer report)
  - tests.test_rules + tests.test_security_alerts: 49 tests OK (skipped=7)
  - check_generic.sh: clean
  - check_conventions.sh: clean
  - asf.cli check: exit 1, BUT verified identical output (1028 lines, same content, diff -q
    clean) at merge-base commit 384df7eba in a scratch worktree — pre-existing backlog-product
    lint failures (bare decision refs in tasks/*.md, closing-rule warnings in stories/*.md), zero
    overlap with writes:. Not caused by this diff.
- Still running: python3 tools/run_tests.py (backgrounded, long-running) — awaiting result.
- Next: once run_tests.py result is in, write docs/reviews/1-t-0363.md with the verdict table,
  C/I lists, and verdict block. No findings so far (diff is clean, matches plan exactly, byte-for-
  byte D10 assertion present as test_two_lines_with_neither_field_file_one_s2_bug_byte_for_byte).
