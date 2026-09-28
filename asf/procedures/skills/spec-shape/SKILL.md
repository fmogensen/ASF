---
name: spec-shape
description: The spec's shape — Decisions, the design, acceptance, records, Stories.
allowed-tools: Read, Grep, Glob
---

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
