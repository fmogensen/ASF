## Your job: write the delivery plan for {item_id} — {delivery_count} items in one branch

Binding: the item blocks above, each one a decided card. There is no spec and there will be no
second document: this plan IS the spec-lite for every item in it. Add no scope no card carries,
and drop nothing a card's ## Acceptance asks for.

DELIVERABLE: `{plan_path}` on branch `{branch}`, cut from `origin/{main}`. Create only that file.

ONE SECTION PER ITEM, in this order — {delivers} — and nothing between them:

    ### <id> — <title>
    writes: <the globs this item's diff may touch — within the card's own>
    acceptance: <a fenced block that can be run, and that fails today>
    steps: <what to change, in order>

The delivery's whole footprint is {writes}. A section that needs a file outside it is a section
that does not belong in this delivery: leave the item out, name it under `left out`, and it goes
back to its own lane while the rest land.

No Task cards: one session builds every item and each item closes by its own commit, so this job
mints no ids and needs no `BACKLOG_ID_RANGE`.

End the plan with `delivers: <ids, in build order>` and `budget: <n> items, <m> globs`.

Final message: the pushed sha, the plan path, the item count, the build order.
