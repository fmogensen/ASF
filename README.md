# ASF — Autonomous Software Factory

One record with typed intent and derived state. Rules as checks. Code decides what runs when; agents only write
and judge. Multi-product from day one. The factory builds itself through the same mechanism it offers you.

**Status: 0.1 in construction.** The factory's own record is
[`fmogensen/ASF-backlog`](https://github.com/fmogensen/ASF-backlog) — public, because the record is the demo.

- Code: `asf/` (the package), `plugin/` (the Claude Code plugin: skills, role agents, hooks), `rules/` (the core
  rule cards and their checks), `evals/` (the frozen eval set), `docs/` (specification), `sample/` (a tiny product
  for the quickstart).
- Operator configuration lives in `~/.ASF/` — one file per product; nothing product-specific lives in this repo.
- License: Apache-2.0.

## Install

Needs macOS or Linux, `git`, `gh`, `pipx` and Claude Code. Per product, once:

```bash
curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> -- --repo <dir> --record <dir> --scheduler launchd
```

The last two steps are the doctor and a dry tick run; then in that product's Claude Code session, `/plugin marketplace add ~/.ASF/plugin` and `/plugin install asf@asf`. A session whose working directory is the product's repo or its record needs no `ASF_PRODUCT`.

The operator's guide is [`docs/guide/`](docs/guide/): [getting started](docs/guide/getting-started.md),
[the product file](docs/guide/product-config.md), [the daily loop](docs/guide/operating.md),
[upgrading](docs/guide/upgrading.md), [troubleshooting](docs/guide/troubleshooting.md) and the
planned [connectors](docs/guide/connectors.md).

Not "ASF" the Apache Software Foundation.
