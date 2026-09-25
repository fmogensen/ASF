## Your job: write the spec and the plan for {item_id} — one document

This Feature is small (`size: s`): its spec and its plan are one document, written in one
session, with no separate review round. The card above is the scope — read it as the
requirement; everything else is context. Write the document and nothing else: no code.

DELIVERABLE, on branch `{branch}`, cut from `origin/{main}` — or, when `{branch}` is already on
origin (`exists:` above), on that branch as it stands in your worktree:
- `{plan_path}` — the one document: the spec and the plan together (the SHAPE below).
- `{spec_path}` — two lines only: the Feature's title as a heading, then
  `The spec and the plan are one document: {plan_path}.` (coders read the spec from the trunk,
  so it must be there; it carries no text of its own).
Create only those two files.

Every commit subject names the card — `plan({item_id}): <what>` — the lane refuses a branch whose
subjects do not name {item_id} as a token (the branch name does not count).

SHAPE of `{plan_path}`, in this order — short, the Feature is small:
1. **Decisions** — what is in and what is out, and one row per choice you made (chosen, against
   what, why).
2. **The design** — what changes, where, what it looks like when it is there.
3. **Acceptance tests** — by path, as fenced blocks that can be run.
4. **`## Stories`** — one line per Story: `- S: <title> — acceptance: <the test that proves it>`.
   The Stories the record already holds for this Feature: {stories}
5. **The Tasks** — TASK LINES (binding, machine-read: the record turns every `### Task N:`
   heading into a Task card and the feeder launches coders from them). Directly under each
   heading, before anything else, exactly three lines:

       stories: <the Story ids this Task delivers>
       writes: <the path globs this Task's diff may touch>
       after: <the Tasks whose landed code this one needs, as `Task 1, Task 2` — or `none`>

   Then **Files**, **Steps**, **Gate** (the exact commands) and **Acceptance** (the tests from 3,
   byte for byte). Prefer one or two Tasks: a Task whose diff stays small lands on CI and the gate
   without a review session.

End the document with the line `coverage: <covered>/<total> stories; uncovered: <ids or none>`.
Mint Story and Task ids only from the range this session was given (`BACKLOG_ID_RANGE`); never
reuse an id the record already carries.

Final message: the pushed sha, the two paths, the Story count, the Task count, the coverage line.
