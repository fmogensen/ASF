# ADR 0002 — CI routing labels name a capability, never a provider

- **Status:** accepted
- **Date:** 2026-09-25
- **Decider:** the operator
- **Supersedes:** nothing
- **Guide:** [the CI runner pool](../guide/ci-runner-pool.md)

## Context

Self-hosted CI runners are matched to jobs by labels: a job runs on a runner that carries every
label in its `runs-on`. On one fleet every job asked for `[self-hosted, <provider>, heavy]`. When
heavy boxes from a second provider were added, they were labelled `<provider2>-heavy` and sat idle
for days while 170 jobs queued — no job asked for their labels, and nothing reported it. The fix
was hand edits, which put the first provider's name on the second provider's boxes so the jobs
would take them: the label no longer said anything true.

## Decision

1. A routing label describes **capability only**: `heavy`, `light`, and later others (`gpu`,
   `docker`). It is the runner's declared `role`.
2. **Provider and size are inventory**, declared in the product file's `ci.pool`, and never appear
   in a `runs-on`.
3. A runner may carry one **informational** label, `provider-<name>`. The doctor flags it, and
   any label that is not a declared role, in a `runs-on`.
4. `ci.pool` is the single source of truth for runner labels. `asf ci reconcile` sets exactly the
   host defaults, the role and the provider label, and strips the rest — adds before removes,
   never removing a label a current `runs-on` still needs.

## Consequences

- Adding a box from any provider is one `ci.pool` line and one reconcile: every job that asks for
  its role can use it.
- Moving an existing product costs one workflow change (every `runs-on` to a role); until it
  lands, the reconcile keeps the old labels and says why.
- The same pool feeds the planned `ci.provider: vm`, where there are no host labels at all, only
  roles and slots.
