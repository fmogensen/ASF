# ADR 0003 — A Task's planned first review is not repair

- **Status:** accepted
- **Date:** 2026-10-06
- **Decider:** the operator (the release gate's repair-load item)
- **Supersedes:** nothing
- **Code:** `asf/scorecard/score.py` (`repair_flags`), read by `asf scorecard` and `asf release-readiness`

## Context

The release gate's criterion 2 holds repair sessions per landed Feature at or under
`release.max_repair_per_feature` (3). The scorecard counted every `review` session as repair. In
the factory's own 7-day window that was about 107 review sessions against 16 correct and 3
adjudicate: most of the "repair load" was the one review every Task gets by design, so the ratio
measured the pipeline's shape rather than rework.

## Decision

1. An item's **first `review` session** (by time, over the whole sessions stream) is planned work,
   not repair.
2. A `review` is repair from that item's **second review on**, or when its job names round 2 or
   later (`-r2`, the stream's `round`).
3. Every other repair kind stays repair: `correct`, `adjudicate`, `rebase`, `remerge`, `relaunch`,
   `bounce`, `revise`, `hotfix`, and the pre-review kinds already counted (`rereview`,
   `prereview`, `precheck`).
4. The scorecard, its per-Feature rows, its "where the days went" ranking and
   `asf release-readiness` read the one function, so they always agree; the gate's evidence cell
   names the repair sessions by kind.

## Consequences

The ratio falls by roughly one session per reviewed Task, with no change in what the factory does.
Real rework (send-backs, corrections, rebases) still counts in full and is what the gate now holds
under 3. A session stream that lacks the first review of an item (trimmed history) counts that
item's earliest remaining review as its first.
