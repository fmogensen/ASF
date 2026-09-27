# The core rules

The rules every factory session is held to, shipped with asf. One card per rule, `R-NNNN.md`,
and for a hooked card one shim, `rNNNN.sh`, that runs the rule's hook module.

## The core band: `R-0001` to `R-0099`

Core cards take their ids from `R-0001` to `R-0099`, and only from there. A product's record
mints `R-` ids from its own counter; without a reserved band, the first product to mint
`R-0001` would shadow the core card of that id in every table that joins the two. A product
record's own rules take ids from `R-0100` up.

## The card

```markdown
---
id: R-0001
type: rule
title: a stage names its files
check: tools/checks/r0001.sh
hook: [PreToolUse]
---
Why the rule exists, and what to do instead.
```

- `id` — in the core band; its check-script form (`R-0001` → `r0001`) names the shim and the
  module, `rules/r0001.sh` and `asf.rules.hooks.r0001`.
- `type` — always `rule`.
- `title` — the rule in a few words; a refusal names it.
- `check` — the basename of the rule's check script, the half a product runs on its own clock.
- `hook` — the runtime events the rule's hook fires on, a subset of `PreToolUse`,
  `PostToolUse` and `Stop`. A card without it declares no hook.

## The two halves

- **The hook half binds from this directory.** `asf hooks install` reads every card's `hook:`
  line here and writes the hook into every worker session's settings; the shim runs the rule's
  module, which answers allow or refuse on each tool call.
- **The check half binds only through a product's own record.** Nothing reads a core card's
  `check:` line to run it: the script it names is not shipped here. A product that wants a core
  rule's check on its clock adds its own one-line `R-` card, in its own band, whose `check:`
  names the same basename; the check runner then resolves that basename against the product's
  record first and this directory second.

A file in this directory is in the amendable set: no factory session edits it. A change is
proposed, and a person approves and merges it.
