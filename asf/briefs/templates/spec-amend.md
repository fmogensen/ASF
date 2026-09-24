## Your job: amend the spec for {item_id}

The card above is the scope — read it as the requirement; everything else is context, not the
source. Write the spec and nothing else: no plan, no code, no branch but this one. When it is
written and pushed, the tick sends it to the reviewer.

DELIVERABLE: `{spec_path}`, which **already exists on this branch**, amended in place.

READ IT FIRST, in full. Your job is the difference between what it says and what the card now
asks for — not a better spec. Change the sections that are wrong, add the sections that are
missing, and leave every section that still holds exactly as it stands, down to its wording: a
reviewer reads your diff, and a rewritten paragraph that says the same thing costs them the
same attention as a changed one. A Story the `## Stories` block already carries keeps its line
and its id. If, having read it, you believe the document should be replaced rather than
amended, do not replace it — write one line saying so at the top of your report and amend what
you can, because a rewrite is a decision the reviewer makes and not you.

SHAPE, in this order:
1. **Decisions** — what is in and what is out, every precondition the work depends on, and one
   row per choice you had to make (what was chosen, against what, why).
2. **The design** — what changes, where, and what it looks like when it is there.
3. **Acceptance tests** — by path, as fenced blocks that can be run. A test nobody can run is
   not acceptance.
4. **Records** — what the change writes down: the rows, the files, the events.
5. **`## Stories`** — REQUIRED, and last. One line per Story:
   `- S: <title> — acceptance: <the test that proves it>`.
   A spec without that block is not reviewable, because the plan that follows mints one Task per
   Story from it and the review checks coverage against it.

The Stories the record already holds for this Feature: {stories}
Mint any new Story id only from the range this session was given (`BACKLOG_ID_RANGE`); never
reuse an id the record already carries.

Final message: the pushed sha, the spec path, the sections written, the Story count.
