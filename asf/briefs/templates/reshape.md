## Your job: reshape {item_id}

Binding: the groom's approved split — {reason} — of this Task of `{plan_path}`, under the
approved spec `{spec_path}`. Add no scope. Change only how {item_id} is cut.

DELIVERABLE, on branch `{branch}` cut from `origin/{main}`:
- one Task per area, ids from `BACKLOG_ID_RANGE`, each carrying `split_from: {item_id}`, its
  `writes:` (the part of {item_id}'s `{writes}` in that area) and `stories:`. Every Story of
  {item_id} sits on at least one part;
- {item_id}'s section of the plan replaced by one section per part, each with Files, Steps, Gate
  and Acceptance (the spec's fenced tests, byte-identical);
- {item_id} marked `removed: split into <ids>`.
If a part cannot pass its own acceptance without another part, say so and leave {item_id} whole:
`NEEDS OPERATOR: {item_id} does not split along <area> — answer no on the split line`.

Final message: the pushed sha, the part ids with their writes, the coverage line.
