## Your job: build {item_id} end to end — the direct lane

This Feature takes the direct lane: one session, one branch, one PR. There is no separate spec,
plan or review round — you read the card, decide what and how, build it, test it and push it. The
card above is the scope: its Description is the requirement and its Acceptance is what done
means. Everything else is context.

ONE BRANCH: `{branch}`, cut from `origin/{main}` — or, when it is already on origin (`exists:`
above), that branch as it stands in your worktree. Never a second branch, never a second PR.

Every commit subject names the Feature — `feat({item_id}): <what>` — the lane refuses a branch
whose subjects do not name {item_id} as a token (the branch name does not count). When it merges,
the commit on `{main}` naming {item_id} is what marks the Feature landed.

IN THIS ORDER:
1. **What and how** — before any code, write the note the PR description carries: the body of
   your FIRST commit on the branch (the lane copies it into the PR it opens). Keep it short:
   - What: the change in two or three sentences, what is in and what is out.
   - How: the files you will touch and the shape of the change, one line each.
   - Tests: the tests that prove each Acceptance line, by path.
   Commit it as `feat({item_id}): what and how` (an empty commit with `--allow-empty` is fine)
   and push it.
2. **The code** — the smallest change that meets every Acceptance line. Follow the product's
   conventions above (its directories, its trunk, its standing rules); no cleanup on the way.
3. **The tests** — a test per Acceptance line, next to the code it covers. The product's test
   command is `{test_command}`.

{gate_before_push_direct} Paste the last line of each in the report. A test you changed to make
it pass is a failed Feature, not a passed one. CI, the gate, the customer-content check and the
merge are the lane's — you push, it lands.

Final message: the pushed sha, the files written, the test lines, what the note says was left out.
