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
cp docs/config.example.yaml ~/.ASF/config.yaml                 # first product only; fill in accounts
cp docs/products.example.yaml ~/.ASF/products/<product>.yaml   # fill in repo_dir, backlog_dir
curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> [sha|tag]
```

The installer pins the factory as `asf`, installs the product's redaction hooks and clocks, and ends with
`asf doctor`. In that product's Claude Code session: `/plugin marketplace add fmogensen/ASF`, then
`/plugin install asf@asf`, with `ASF_PRODUCT=<product>` set. Rerun the installer with a new ref to upgrade.

The operator's guide is [`docs/guide/`](docs/guide/): [getting started](docs/guide/getting-started.md),
[the product file](docs/guide/product-config.md), [the daily loop](docs/guide/operating.md),
[upgrading](docs/guide/upgrading.md), [troubleshooting](docs/guide/troubleshooting.md) and the
planned [connectors](docs/guide/connectors.md).

Not "ASF" the Apache Software Foundation.
