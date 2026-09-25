# docs/ — the specification

Three surfaces, one audience each: the root README makes the argument (evaluator); `plugin/skills` give the
procedure (operator); `docs/` holds the specification (contributor). A mechanism is specified in exactly one
place; the other two link to it. The one exception is `guide/`: the operator's end-to-end guide, which
describes what the code does today and links to the rest.

- [`guide/`](guide/) — the operator's guide: putting a product on ASF and running it day to day
  - [Getting started](guide/getting-started.md) — prerequisites, the config files, the installer, the plugin, the first doctor
  - [The product file](guide/product-config.md) — the product yaml key by key: landing, steps and clocks, capacity, approvals, rule cards, redaction hooks
  - [Operating](guide/operating.md) — the tick, the `/asf:*` tables, holds, `workers health --fix`, the groom and the inbox
  - [Upgrading](guide/upgrading.md) — releases, reinstalling at a tag, schema migrations, what is safe mid-run
  - [Troubleshooting](guide/troubleshooting.md) — every `NEEDS OPERATOR` line, and the common stalls
  - [The CI runner pool](guide/ci-runner-pool.md) — `ci.pool`: capability-only routing labels, the doctor's drift rows, `asf ci reconcile`, trials, the CI ceiling
  - [Connectors](guide/connectors.md) — planned, not yet available
- `specs/` — the design specs, one per release
- [`decisions/`](decisions/) — ADRs
  - [0001 — the 2026-08 prototype is prior art, not a base](decisions/0001-prior-art-not-base.md)
  - [0002 — CI routing labels name a capability, never a provider](decisions/0002-ci-routing-labels-are-capabilities.md)
- [`research/`](research/) — prior art and measurements
  - [Prior art — the 2026-08 prototype](research/prior-art-prototype.md) (five parts: specs and directives, findings, gates and evals, roles, what never shipped)
- `CONSTITUTION.md` — the directives every role runs on, each mapped to the rule cards that enforce it
