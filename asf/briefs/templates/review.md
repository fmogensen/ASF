## Your job: review {item_id} — round {next_round}

Read, in this order: the writer's report on `{branch}`, then the diff against `origin/{main}`,
then the plan `{plan_path}` for the Task it claims to deliver.

THE VERDICT IS A TABLE. Write `{review_path}` with exactly this shape — one row per check, and no
row without evidence; a result with no evidence counts as `fail`:

| check | result | evidence |
| --- | --- | --- |
| the diff stays inside `writes:` | pass \| fail | the file, or the one outside it |
| every Step of the Task is implemented | pass \| fail | the step → the code |
| the acceptance tests are byte-identical to the plan's | pass \| fail | file:line |
| those tests were run and are green | pass \| fail | the run's last line |
| the Gate commands are green | pass \| fail | each command's last line |
| no secret value printed, no background process, no skipped check | pass \| fail | what you looked at |{delivery_checks}

Then, on its own line, `verdict: approved` or `verdict: changes requested`, then the C list (each
one: file:line and the exact fix) and the I list (what you would change, but will not block on).

Round 1 finds everything: run the whole table before writing a single finding. In a later round,
anything already visible in round 1's diff is not a new C — record it under
`### Missed in round 1` as an I. Rounds are expensive; a round that finds what the last one could
have found is the failure mode this rule exists to stop.

THE REVIEW NEVER TOUCHES THE BRANCH. Write `{review_path}` in your worktree and leave it there:
never `git add` it, never commit it, never push — not the review, not anything else. A push to
`{branch}` moves its PR head, which cancels and restarts the PR's whole CI and unpins the merge
watcher. When you finish, the factory files the file off the branch, bound to the head you read
(`git rev-parse HEAD`). This overrides the push rule of the report section below: your REPORT
says `pushed: n/a — review left for the factory to file`, and `commits: none`.

Final message: the verdict line, the failing rows, the gate lines.
