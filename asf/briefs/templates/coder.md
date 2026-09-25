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

When the Task cannot be whole without such a file — a sibling test suite your change turns red, a
constant that belongs in another module — name every one, as a full repo path, on the REPORT's
`needs writes:` line and report `status: partial`. The factory widens `writes:` by those paths
and sends this run back to you; that line is how the footprint grows, never a question to a person.

{gate_before_push} Paste the last line of each in the report. A test you changed to make it pass is a
failed Task, not a passed one.

Final message: the pushed sha, the files written, the gate lines, the assumptions recorded.
