## Your job: close the review on {item_id}

The binding list is the newest review on `{branch}` — `{review_path}`. Read it in full before you
change anything. Fix every C exactly as it specifies; apply the I's it leaves to you; never
redesign and never widen the scope: this branch is already reviewed, and a change the review did
not ask for buys another round.

Stay inside `writes:` — {writes}

FIRST, before any fix: merge `origin/{main}` into the branch, resolve, commit, push. That merge is
your first push and it is due immediately — a branch that has moved is how the tick knows you are
alive.

ALWAYS PUSH SOMETHING — even when the honest answer is "nothing to change". Then the push is a
report saying what you checked, why no change was needed, and the sha that already carries it.

Re-run the acceptance tests and the Gate, and append one line per C to the report: what it asked,
where it is now closed.

Final message: the pushed sha, one line per C, the gate lines.
