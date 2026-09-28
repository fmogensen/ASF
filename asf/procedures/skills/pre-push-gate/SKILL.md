---
name: pre-push-gate
description: The coder's gate — what to run, in what order, and what a red one means.
allowed-tools: Read, Bash
---

Before the push, every acceptance line your tests now prove carries a trailer on its own line in
one of your commit messages:

    Proves: <S-id> line <n> — <the test path that proves it>

`<n>` is the number above, not a line of the card file. The test path must exist on this branch.
A Task that proves nothing is refused at the landing and handed straight back to you, so write
the trailer with the commit, not after it.

Paste the last line of each gate command in the report. A test you changed to make it pass is a
failed Task, not a passed one.
