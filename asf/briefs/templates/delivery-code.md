## Your job: build the delivery {item_id} — {delivery_count} items, one branch, one gate

Binding: `{plan_path}`, section by section, in this order: {delivers}. Do those items and
nothing else — not the next card you notice, not a cleanup on the way. The order is the plan's
`after:` order: an item builds on what the one before it committed, so never start a later item
before the earlier one's commit is on the branch.

RESUME, NEVER RESTART. `{branch}` may already be on origin (`exists:` above): then its head is
the state of the delivery. Read `git log origin/{main}..{branch}` first — an item whose id a
commit subject there names is done; do not redo it, do not amend it. Carry on with the first item
no commit names, from that head. The report under `The last report for this item` above says
where the last session stopped.

ONE COMMIT PER ITEM, and the subject names that item's id — `<kind>(<id>): <what>`. A commit on
the trunk naming the id is what closes the card: an item no commit names lands nothing, whatever
its diff did. Never fold two items into one commit. Push after every item's commit: a session
that dies leaves the branch at its last pushed commit, and the next one continues from there.

THE BOUNDARY IS `writes:` — {writes}. That is the union of the items' footprints; a file outside
it is a refusal, not a judgement call.

PROVES — each item's block above lists the Story lines its tests tick (`proves:`). Every line
your tests prove carries a trailer in that item's own commit message, on its own line:

    Proves: <S-id> line <n> — <the test path that proves it>

`<n>` is the number in the block, not a line of the card file. The test path must exist on this
branch. An item that proves nothing is refused at the landing and handed straight back to you.

AN ITEM ALREADY ON THE TRUNK — its work landed on `origin/{main}` under another commit, before
the card existed or by another lane: no commit there names its id, so the card stays open. Verify
it against the item's section and run the test that covers it, then make that item's one commit
an empty, signed one — `git commit --allow-empty -s -m "<kind>(<id>): already landed in <sha> —
verified by <test>"`, with the item's `Proves:` trailers naming the test that already proves each
line — and push. That commit is the evidence the record needs, not a fabricated one; a report
that only says "already on main" closes nothing, and the lane relaunches this session.

An item you cannot finish: commit nothing for it — no half of it — name it under `left out`, and
carry on with the rest. A run that ends with items left says `status: partial`: no PR opens for
a partial delivery, and the same session comes back to this branch to finish. Only `status: done`
lands the branch — with every item, or with the ones under `left out` returned to their own
lane. A gate you cannot make green is the whole branch's problem: say so and stop.

BEFORE THE PUSH: the plan's acceptance block for every item you committed, byte-identical and
passing, and the product's gate once over the whole branch. Paste each last line in the report.{pre_push_check}

Final message: the pushed sha, one line per item (committed, left out, or already on the branch),
the gate lines.
