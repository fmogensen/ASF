# ADR 0001 — The 2026-08 prototype is prior art, not a base

- **Status:** accepted
- **Date:** 2026-09-21
- **Decider:** the operator
- **Supersedes:** nothing
- **Source:** the ASF 0.1 plan (not part of this repository; cited below as `plan:<line>`),
  decisions 1 and 4
- **Evidence:** [`docs/research/prior-art-prototype.md`](../research/prior-art-prototype.md)

## Context

The 2026-08 prototype is a working agent factory: 446 commits over eight days (2026-08-20 →
08-27), a TypeScript chain runner with ten gates, a ten-directive constitution, 14 role
prompts, 5 chains, 30 frozen eval tasks, and **143 backlog findings — each one a real failure
mode of a factory, with its fix**. It is the most useful document about building this thing
that exists, and it is the obvious thing to fork.

We are not forking it.

## Decision

In the operator's words, from the plan:

> *"the previous ASF prototype may have useful elements but **start fresh**"* (plan:8)

> **Decision 4.** *"Fresh code; the 2026-08 prototype is **prior art to mine, not a
> base**."* (plan:29)

> *"The 2026-08 prototype repo (TypeScript runner, gates, frozen evals, CONSTITUTION) is
> prior art: **its ideas are already adopted as cards** (D-0180); **its code is not
> carried**."* (plan:16-17)

> **Not in scope:** *"re-using the prototype's code."* (plan:345)

So:

1. **No file, function or type crosses over.** Not the runner, not `core/gates.ts`, not the
   chains, not the role prompts, not the eval harness. This repository is fresh with no
   product data and no secrets in its history (plan:26).
2. **Ideas cross over as cards, each citing its source.** Every idea taken becomes a Feature
   card in the ASF backlog under the ASF 0.1 Epic, `decided: true`, with `source:` naming the old
   spec file **and** the finding id it rests on (plan:235-240). The operator's ranking stands:
   1 preamble, 2 grill/interrogator, 3 self-amendment policy, 4 typed envelopes, 5
   writes-boundary hold, 6 panels, 7 prompt-budget dimensions, 8 roles, 9 README shape.
3. **The 143 findings are a checklist, not a changelog.** They are clustered into 22 failure
   classes in the research note; a class the 0.1 spec does not answer is a **GAP** the spec
   must close before P2. Five classes are open today.
4. **The old repo is archived as prior art**, not deleted: tagged, made read-only, and linked
   from the research note (plan:169).

## Why not fork it

**The goal is different, and the difference is structural.** The old ASF is a *chain runner* —
a person types `asf run <chain> "<request>"` and a graph of phases executes. ASF 0.1 is a
*record with a tick over it*: items with derived state, a feeder, waves, harvest, groom,
metrics per product. The old framework's own findings say so in its own words — **b76**
*"ASF has no orchestrator. It has a pass, and a person acting as one"*; **b123** *"nothing has
ever entered this factory through a door… every one of its 131 items was written by a session
that already knew the codebase"*; **b113** *"the factory has produced exactly one `open` item
in its entire history"*. A fork inherits the shape those findings are about.

**Multi-product is not retrofittable there.** E06, the old repo's own tenancy epic, lists what
it would touch: *"everything scoped to a run is single-valued — `cwd`, `runsRoot`,
`promptsDir` and `agentsDir` are one string each; the trace is one SQLite file under one
`runsRoot`; `asf tend` reads one `backlog/`; and the roster is one `assignments` block in one
operator config."* ASF 0.1 is multi-product from day one (plan:28, 69-83). That is a different
spine, not a migration.

**The working code is elsewhere.** The factory that actually ships a product today — 130 tool
files, 305 tests, `backlog.py`, `evidence.py`, `metrics.py` — is the ~70 % that has to be made
generic (plan:12-15). Carrying a *second* implementation of the same ideas, in a second
language, would mean two things to reconcile instead of one to generalise. P4b's rule is one
factory, and `asf doctor` fails if a second copy of any factory tool is on PATH (plan:174-176).

**Its licence and history make a clean OSS repo harder, not easier.** This repository is
public and Apache-2.0 from the first commit (plan:26). The old repo is MIT (D1), carries
operator-specific config, run directories and traces, and 446 commits of history that
would need auditing rather than writing.

**The value was never the code.** It was eight days of a factory failing in public with the
reasons written down. That value transfers as the research note, the cards and the eval
discipline — and it transfers *better* when it is re-derived against ASF 0.1's own shape than
when it arrives as inherited code nobody chose.

## Consequences

- **P1's spec is written against the research note, not against the old repo.** The 22 failure
  classes are its acceptance checklist; the five GAPs are the sections it must contain.
- **Nothing in this repository may be justified by "the old ASF did it."** An idea is carried
  because a finding id, a test or a measured number says it worked — which is the column the
  research note's table is built around.
- **The old repo's language is not a precedent.** The plan leaves the implementation language
  to the spec writer, with a reason (plan:298-301); TypeScript matching the old ASF is an
  input, not an argument.
- **Attribution is honest.** `docs/research/prior-art-prototype.md` names the source of every
  idea, and the README credits the public upstream software-factory project the prototype credited
  (`docs/prior-art.md:5-12`).
- **The archive is a dependency of nothing.** Once ASF 0.1 ships, the old repo is read-only
  history; its index source is removed from the autopilot (plan:169).

## Alternatives considered

| Option | Why not |
|---|---|
| **Fork the prototype and generalise it** | Inherits the runner-not-a-record shape (b76, b123), single-tenancy (E06), an operator-specific history, and a second implementation to reconcile with the one that actually ships a product |
| **Port `core/` only, write the rest fresh** | `core/` is where the shape lives — the chain, the runner, the envelope, the gates. Porting it is forking it with extra steps |
| **Vendor `core/gates.ts` and `evals/`** | The gates are the best artefact in the old repo, and they are also the most coupled: they read `runs/<id>/`, `RunDir`, `RunContext`, `Bun.spawn`. Their *design rules* carry; their code does not. Recorded in the research note §(c) |
| **Start fresh and ignore the old repo** | Throws away 143 findings that cost real money to discover — 28.3 % of the old factory's total spend is documented in two of them alone (b111, b112). P0.5 exists to prevent this |
