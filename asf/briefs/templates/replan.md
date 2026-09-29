## Your job: re-plan {item_id}'s open Tasks

Binding: the groom's reshape decision on {item_id} — {reshape} — under the approved spec
`{spec_path}` and the plan `{plan_path}`. The decision is the scope: carry it out, add none.

The Feature's Tasks as the record holds them:
{feature_tasks}

A landed Task is kept exactly as it is, and so is every commit already on a Task's branch: a
replan re-cuts the work still to do, it never reverts work done. An open Task you keep, rewrite
it; one the decision makes unnecessary, drop it; work the decision adds, add as new Tasks. Move
an `after:` wherever the decision moves a dependency — an `after:` on a card that will never
land (a removed card, another Feature's archived work) holds its Task for ever, so replace it
with the Task that now provides that surface, or `none`.

DELIVERABLE: `{replan_path}` on branch `{branch}`, cut from `origin/{main}`. Create only that
file. Every commit subject names the card — `plan({item_id}): <what>`. The record reads it once it
lands and writes the cards itself — you write no card. Its format is binding, machine-read:

    replan: {item_id} {reshape_digest}

    ### Task T-nnnn: <title>        (an open Task of {item_id}, rewritten)
    stories: <Story ids>
    writes: <path globs, comma-separated>
    after: <Task ids, `new N` for the Nth new Task below, or `none`>
    <Files, Steps, Gate and Acceptance, as in any plan>

    ### Task new: <title>           (a Task the record mints under {item_id})
    <the same lines and sections>

    ### Drop T-nnnn: <why>          (an open Task this replan removes)

Every open Task above appears exactly once, as a `### Task` or a `### Drop` section. A Task's
`writes:` names every file its acceptance needs. Two Tasks whose `writes:` intersect run one
after the other.

If the decision cannot be carried out without adding scope the spec does not carry, write no
replan and say so: `NEEDS OPERATOR: {item_id} — <what the decision leaves open>`.

Final message: the pushed sha, the replan path, and per Task: rewritten, new or dropped.
