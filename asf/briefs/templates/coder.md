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
failed Task, not a passed one.{pre_push_check}{rubric}

PROVES — the acceptance lines your tests tick: {proves}
Before the push, every line above that your tests now prove carries a trailer on its own line in
one of your commit messages:

    Proves: <S-id> line <n> — <the test path that proves it>

`<n>` is the number above, not a line of the card file. The test path must exist on this branch.
A Task that proves nothing is refused at the landing and handed straight back to you, so write
the trailer with the commit, not after it.

If the Task's work is ALREADY on `origin/{main}` under another commit (landed before the card
existed, or by another lane): verify it, run the test that covers it, then make one empty, signed
commit — `git commit --allow-empty -s -m "task({item_id}): already landed in <sha> — verified by
<test>"`, with the `Proves:` trailers naming the test that already proves each line — and push. A
report that only says "already on main" closes nothing, and the lane relaunches this session.

Final message: the pushed sha, the files written, the gate lines, the assumptions recorded.
