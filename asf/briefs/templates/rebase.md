## Your job: make {item_id} merge again

`{branch}` no longer merges into `{main}`: {reason}. Its review stands and no new review round
follows — so do not change its design, its scope, or anything the review already passed.

Merge `origin/{main}` into the branch — **merge, never rebase**: the review history is what makes
this branch approvable without another round. Resolve every conflict so BOTH sides survive: never
drop a change that came from `{main}`, and never drop one of this branch's. A generated file is
regenerated with its script, never hand-merged. A conflicting test file is merged so both sides'
cases still run. If a number, an id or a register row this branch books collides with one `{main}`
has taken since, re-number this branch's to the next free one and update every reference to it.

ALWAYS PUSH SOMETHING — if the merge turns out to be a no-op, push a report saying so. A session
that ends without a push is counted dead and relaunched on top of you.

GATE before the push: the branch's own gate commands and the touched tests. Paste each one's last
line in the report.

Final message: the pushed sha, the files resolved, the gate lines.
