## Your job: build {item_id}

Binding: this Task (and any it absorbed, listed above) of `{plan_path}`, and the spec
`{spec_path}` behind it. Do exactly this Task — not the next one, not a cleanup you noticed on the
way. Branch `{branch}`, cut from `origin/{main}`.

THE BOUNDARY IS `writes:` — {writes}
A file outside that list is a refusal, not a judgement call: leave it untouched, and say in the
report under `left out` what you would have changed and why. If a Step cannot be done without
touching one, take the plan's stated expectation for it, record the assumption in the report, and
carry on. The footprint is what lets other sessions run beside you; widening it silently collides
with work you cannot see.

BEFORE THE PUSH: the Task's Gate commands, and its acceptance tests byte-identical from the plan
and passing. Paste the last line of each in the report. A test you changed to make it pass is a
failed Task, not a passed one.

Final message: the pushed sha, the files written, the gate lines, the assumptions recorded.
