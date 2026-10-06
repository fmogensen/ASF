# ASF — Autonomous Software Factory

One record with typed intent and derived state. Rules as checks. Code decides what runs when; agents only write
and judge. Multi-product from day one. The factory builds itself through the same mechanism it offers you.

**Status: 0.1.0-preview** — a pre-release for feedback: read the
[release notes](docs/RELEASE-NOTES.md) and the [known issues](docs/KNOWN-ISSUES.md) first, and tell
us what broke ([Feedback](#feedback)). The factory's own record is
[`fmogensen/ASF-backlog`](https://github.com/fmogensen/ASF-backlog) — public, because the record is the demo.

- Code: `asf/` (the package), `plugin/` (the Claude Code plugin: skills, role agents, hooks), `rules/` (the core
  rule cards and their checks), `evals/` (the frozen eval set), `docs/` (specification), `sample/` (a tiny product
  for the quickstart).
- Operator configuration lives in `~/.ASF/` — one file per product; nothing product-specific lives in this repo.
- License: Apache-2.0 ([`LICENSE`](LICENSE), [`NOTICE`](NOTICE)).

## Install

Needs macOS or Linux, `git`, `gh`, `pipx` and Claude Code. Per product, once:

```bash
curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> -- --repo <dir> --record <dir>
```

The clock is launchd on macOS and cron on Linux unless you pass `--scheduler launchd|cron|none`.

The last two steps are the doctor and a dry tick run; then in that product's Claude Code session, `/plugin marketplace add ~/.ASF/plugin` and `/plugin install asf@asf`. A session whose working directory is the product's repo or its record needs no `ASF_PRODUCT`.

The operator's guide is [`docs/guide/`](docs/guide/): [getting started](docs/guide/getting-started.md),
[the product file](docs/guide/product-config.md), [the daily loop](docs/guide/operating.md),
[upgrading](docs/guide/upgrading.md), [troubleshooting](docs/guide/troubleshooting.md) and the
planned [connectors](docs/guide/connectors.md).

## Quick start

Try it first on the bundled sample product. From a clone, with `git` and Python 3 only (no account,
no network, no `gh`), this materialises [`sample/`](sample/) in a temporary directory and walks it
through `asf init`, `asf next`, one `asf tick`, the ticks that land a first Task (a stub runtime
plays each session's work, so no agent runs), `asf doctor`, `asf roadmap` and `asf scorecard`:

```bash
git clone https://github.com/fmogensen/ASF.git && cd ASF
bash tools/quickstart.sh
```

Then your own product, with one Claude Code account, no cloud lane and GitHub-hosted CI. Run the
Install line above with `--account <name>[:<config dir>]` added (`asf install --help` lists every
flag). It ends on `asf doctor` and a dry tick. After that:

```bash
asf doctor --product <product>             # one table; every row green or naming its fix
asf next --product <product>               # what the next tick would start, and why
asf tick --product <product> --dry-run     # a tick on a throwaway copy: no push, no PR, no session
```

The clock you chose with `--scheduler` runs the real ticks. Ideas, bugs and requests enter through
`asf inbox` ([the daily loop](docs/guide/operating.md#the-groom-and-the-inbox)).

## Configuration

Everything machine- or product-specific lives in `~/.ASF` (`$ASF_HOME` moves it), never in this
repo:

- `~/.ASF/config.yaml` — shared by every product on the machine. `asf install` writes it once:
  `default_product`, `scheduler.kind`, `worker_pool.backend`, `worker_pool.models` and
  `worker_pool.accounts` (one entry is enough), `capacity.total.sessions`. Annotated:
  [`docs/config.example.yaml`](docs/config.example.yaml).
- `~/.ASF/products/<product>.yaml` — one file per product. The minimum is `product`, `repo_slug`,
  `repo_dir`, `main`, `backlog_dir` and `ci.test_command`, plus the `steps` and `clocks` that
  `asf init` writes. Annotated: [`docs/products.example.yaml`](docs/products.example.yaml); key by
  key: [the product file](docs/guide/product-config.md).

Every other key has a default, and the optional lanes (cloud sessions, a merge queue, extra
accounts) stay off until you set them. `asf doctor` names any key no code reads.

## Upgrade

```bash
asf upgrade --to <version>        # a release, tag or commit; without --to, main's head
asf --version
asf doctor --product <product>
```

One `asf upgrade` upgrades the package for every product on the machine and ends on a schema
table. When a release raises the record's schema, commands that write the record refuse until you
run `asf schema-migrate --product <product> --drain` — forward-only, one commit per step
([schema migrations](docs/guide/upgrading.md#schema-migrations)). `upgrade: auto` under a product's
`approvals:` lets the tick run `asf upgrade` itself — today only for ASF's own repo as a product
([the product file](docs/guide/product-config.md#approvals)). Rolling back, and what is safe while
sessions run: [upgrading](docs/guide/upgrading.md).

## Feedback

Feedback goes to this repository's GitHub Issues, through one of three forms: a **bug report**, an
**install problem** or a **feature request** (blank issues are off). Each asks for what the intake
needs and never for a name, an account or a path. Before filing, check the
[known issues](docs/KNOWN-ISSUES.md); run `asf doctor` and paste its red rows into an install
problem.

Not "ASF" the Apache Software Foundation.
