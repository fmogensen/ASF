## Your job: write the plan for {item_id}

Binding: the approved spec `{spec_path}`. Read it in full first — every section, every fenced
acceptance test. The plan adds no scope the spec does not carry; where the spec is wrong, say so
in the plan's decisions block rather than quietly widening it.

{refine}DELIVERABLE: `{plan_path}` on branch `{branch}`, cut from `origin/{main}`. Create only that file.

TASK LINES (binding, machine-read — the record turns every `### Task N:` heading into a Task card
and the feeder launches coders from them). Directly under each heading, before anything else,
exactly three lines:

    stories: <the Story ids this Task delivers>
    writes: <the path globs this Task's diff may touch>
    after: <the Tasks whose landed code this one needs, as `Task 1, Task 2` — or `none`>

`writes:` is the same set as the Task's Files block. Two Tasks whose `writes:` intersect are run
one after the other, never together — so keep them disjoint or accept the queue. `after:` becomes
the card's `after:`: the Task waits until those have landed, and one with `none` starts at once.

Every Task carries, besides those three lines: **Files**, **Steps**, **Gate** (the exact commands),
**Acceptance** (the spec's fenced tests, copied byte for byte).

COVERAGE: the plan is approvable only when every Story of the Feature is on at least one Task's
`stories:` line. The Stories: {stories}
End the plan with the line `coverage: <covered>/<total> stories; uncovered: <ids or none>`.
Mint Task ids only from `BACKLOG_ID_RANGE`.

Final message: the pushed sha, the plan path, the Task count and the wave order, the coverage line.
