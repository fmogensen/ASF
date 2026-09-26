**NEXT** — 13 rows · 9 would launch

| Tier | Row | Item | Feature | Action |
|---|---|---|---|---|
| 0 | BUG → FIX | B-0001 | F-0002 | would launch fix-bug on fix/B-0001 |
| 0 | UNDECIDED → DECIDE | B-0004 | F-0002 | NEEDS DECISION |
| 1 | BUG → FIX | B-0002 | F-0004 | would launch fix-bug on fix/B-0002 |
| 2 | CONFLICT → REBASE | T-0007 | F-0002 | would launch rebase on task/T-0007 |
| 2 | PLAN → CODE | T-0001 | F-0002 | would launch task on worker/T-0001 |
| 2 | PLAN → CODE | T-0002 | F-0002 | WAITS ON T-0001 |
| 2 | PLAN → CODE | T-0003 | F-0002 | would launch task on worker/T-0003 |
| 2 | STALE → CLOSE | T-0006 | F-0007 | would launch close on task/T-0006 |
| 2 | CARD → SPEC | F-0001 | F-0001 | would launch spec on spec/F-0001 |
| 2 | STALEMATE → ADJUDICATE | F-0003 | F-0003 | would launch adjudicate on plan/F-0003 |
| 2 | STARVED → SPEC | F-0004 | F-0004 | would launch spec on spec/F-0004 |
| 2 | STARVED → PLAN | F-0005 | F-0005 | WAITS ON finish: 0 spec/plan in flight + 2 this wave, cap 2 (feeder.max_specs_in_flight) while 2 planned Features have Tasks to build: F-0002, F-0007 |
| 2 | UNDECIDED → DECIDE | F-0006 | F-0006 | NEEDS DECISION |
