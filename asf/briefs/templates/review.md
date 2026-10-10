## Your job: review {item_id} — round {next_round}

Read, in this order: {read_order}

THE VERDICT IS A TABLE. Write `{review_path}` with exactly this shape — one row per check, and no
row without evidence; a result with no evidence counts as `fail`:

{checklist}{delivery_checks}

Then the C list (each one: file:line and the exact fix) and the I list (what you would change,
but will not block on). End the review with THE VERDICT BLOCK — exactly one, fenced, these three
keys and nothing else; the factory reads this block, not your prose:

```verdict
verdict: approved
head: <the 40-hex sha `git rev-parse HEAD` printed — the code you read>
asks: []
```

`verdict:` is `approved` or `changes`; `asks:` lists the C ids you block on (`[C1, C2]`), `[]`
when there are none — an `approved` that asks for anything reads as `changes`. A block naming
another head is a verdict on other code: it does not count for this branch.

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

Final message: the verdict block, the failing rows, the gate lines.
