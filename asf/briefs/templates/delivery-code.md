## Your job: build the delivery {item_id} — {delivery_count} items, one branch, one gate

Binding: `{plan_path}`, section by section, in this order: {delivers}. Do those items and
nothing else — not the next card you notice, not a cleanup on the way.

ONE COMMIT PER ITEM, and the subject names that item's id — `<kind>(<id>): <what>`. A commit on
the trunk naming the id is what closes the card: an item no commit names lands nothing, whatever
its diff did. Never fold two items into one commit.

THE BOUNDARY IS `writes:` — {writes}. That is the union of the items' footprints; a file outside
it is a refusal, not a judgement call.

An item you cannot finish: commit nothing for it — no half of it — name it under `left out`, and
carry on with the rest. The branch lands without it and it returns to its own lane. A gate you
cannot make green is the whole branch's problem: say so and stop.

BEFORE THE PUSH: the plan's acceptance block for every item you committed, byte-identical and
passing, and the product's gate once over the whole branch. Paste each last line in the report.

Final message: the pushed sha, one line per item (committed or left out), the gate lines.
