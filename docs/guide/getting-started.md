# Getting started

Put one software product on ASF: two config files, one installer run, the plugin, and a green
`asf doctor`. Every command below exists in `asf --help`; every key is read by the code.

## Prerequisites

- macOS or Linux, `git`, `pipx`, and Claude Code (the worker runtime).
- `gh`, logged in (`gh auth status`) — unless the product has no PR host (`ci: none`, or `provider: none` under `ci:`).
- The product's code repo checked out on this machine, with an `origin`.
- A second git repo for the product's **record** (the backlog: one markdown card per work item),
  checked out, with an `origin`. It can start empty; `asf init` lays it out.
- One or more worker accounts: a Claude Code login each, ideally in its own config directory.

## 1. The config files

Everything ASF knows about you and your products lives under `~/.ASF` (`$ASF_HOME` overrides it).
Nothing product-specific lives in the ASF repo.

```bash
mkdir -p ~/.ASF/products
cp docs/config.example.yaml   ~/.ASF/config.yaml               # once per machine
cp docs/products.example.yaml ~/.ASF/products/<product>.yaml   # once per product
```

`~/.ASF/config.yaml` is shared by every product. Fill in at least:

| key | what to put there |
| --- | --- |
| `default_product` | the product `asf` targets when no `--product` and no `$ASF_PRODUCT` is given |
| `scheduler.kind` | `launchd` (default; installed end to end) or `cron` (ASF prints the lines, you add them) |
| `worker_pool.accounts` | one entry per worker account: `name`, `cap` (concurrent sessions), `config_dir` |
| `worker_pool.models` | the two model labels ASF uses, `heavy` and `light`, mapped to real model ids |
| `worker_pool.quota_command` | optional: prints an account's usage windows; without it the quota bands never apply |
| `capacity` | totals across products, and the per-product default |

`~/.ASF/products/<product>.yaml` describes one product. The keys that must be right before the
first run: `product`, `repo_slug` (for `gh`; not needed with `ci: none`), `repo_dir`,
`main`, `backlog_dir`, `ci.test_command` (the gate branches must pass before they land), `steps`
and `clocks`. [product-config.md](product-config.md) goes through them key by key.

If you would rather have ASF discover the repo facts, `asf init --product <product> --repo <dir>
--backlog <dir>` writes a product file with a `# TODO` on every key it could not fill — but only
when no file exists yet; it never overwrites one, it prints the diff it would have made. It needs
`asf` on `PATH`, so on a new machine copy the example first and run the installer.

## 2. Run the installer

From a checkout of the ASF repo:

```bash
bash tools/install.sh <product> [ref]
```

or without a checkout, the `curl … | bash -s -- <product> [ref]` line in the root README. `ref` is a
release tag (`v0.1.1`) or a commit sha; without one the installer pins `main`'s current head.
`ASF_REPO_URL` points it at a fork or mirror.

What it does, in order:

1. `pipx install --force` of the package, pinned to `ref`, as the command `asf`. A dev install
   (`pipx install -e`) is replaced: one `asf` per machine. It prints `install: asf <version> (<sha>)`.
2. Checks that `~/.ASF/config.yaml` and `~/.ASF/products/<product>.yaml` exist. It never writes them.
3. `asf hooks install --product <product>`:
   - a git `pre-commit` and `pre-push` in both `repo_dir` and `backlog_dir`, each running
     `asf redact` (the redaction gate, see [product-config.md](product-config.md#redaction-hooks));
   - the approvals hook in every worker account's Claude Code settings;
   - the Claude Code hooks any rule card declares, in the product repo's `.claude/settings.json`.
4. `asf scheduler install --product <product>`: one scheduler job per clock in the product file.
5. `asf doctor --product <product>`, then the two plugin lines below.

Steps 1–2 abort at once. Steps 3–5 never abort: each failure is recorded, the rest still runs.

### Reading its output

| line | meaning |
| --- | --- |
| `install: product <p>, ref <sha12> from <url>` | what is being installed |
| `install: asf 0.1.0 (<sha>)` | the install landed; the sha is the commit you are running |
| `install: NEEDS OPERATOR: …` then exit 2 | step 1 or 2 could not run — the line says what to install or copy |
| `install: FAILED step N: <command> (exit K)` | that step failed; run the command yourself to see why |
| `install: NEEDS OPERATOR: N step(s) failed …` then exit 1 | fix each `FAILED` line and run the installer again — it is idempotent |
| `install: done` | every step and the doctor passed |

With `scheduler.kind: cron`, step 4 always reports `FAILED … (exit 3)`: ASF cannot edit your
crontab, so it prints the lines and exits 3. Add the printed lines with `crontab -e`; the rest of
the install is fine. Any other kind has no adapter and prints the command each job must run.

## 3. Adopt the record

If the record was not adopted yet, run once:

```bash
asf init --product <product>
```

An ASF-shaped record (item folders plus `index.json`) is adopted as it is. Anything else gets the
layout: `epics/ features/ stories/ tasks/ bugs/ decisions/ rules/`, the intake folder, `groom/`,
`releases/`, `metrics/`, a `README.md`, an empty `index.json` and a `.githooks/pre-commit` that runs
`asf check`. Commit and push that layout yourself. A freshly laid-down record's own pre-commit is
not ASF's redaction hook, so the redaction gate reports it as foreign — see
[troubleshooting.md](troubleshooting.md#the-redaction-hooks).

## 4. Add the `/asf:*` plugin

In the Claude Code session you run the product from:

```
/plugin marketplace add <owner>/ASF        # or a local checkout path
/plugin install asf@asf
```

Start that session with `ASF_PRODUCT=<product>` in its environment. Every skill passes its
arguments through, and chooses the product the way the CLI does: `--product` if you give one
(`/asf:status --product other`), else `$ASF_PRODUCT`, else `default_product` in
`~/.ASF/config.yaml`. The skills run `asf` from `PATH` (they add `~/.local/bin`).

## 5. The first `asf doctor`

```bash
asf doctor --product <product>        # or /asf:doctor
```

One table, then a `SCHEDULER` section. Each row is `ok`, `RED` (required and failing) or `skip`
(optional, or an advisory finding). Exit 1 when any required row or the scheduler section is red.

| row | what it checks |
| --- | --- |
| `config` | both files parse; `repo_dir`, `backlog_dir` (and `repo_slug` with a PR host) are set |
| `repo`, `backlog` | each is a directory and a git work tree |
| `scheduler` | a pre-ASF launchd job named by `scheduler.launchd_label`, if any, is retired |
| `cli:<tool>` | `git` and `gh` are required (`gh` only with a PR host); the rest are optional |
| `one-factory` | no copy of an old tool from `legacy_paths:` is on `PATH` or in the product repo |
| `approvals` | the approval matrix loads; notes classes left to their default or unrecognisable |
| `redaction-hooks` | a `pre-commit` and `pre-push` running `asf redact` in both repos |
| `drift` | only for ASF's own repo: is the install behind the trunk |
| `rule-checks` | rule checks that timed out or crashed on the last run |
| `capacity`, `token-caps` | advisory: oversubscription, deprecated keys, uncapped token dimensions |

Under `== SCHEDULER`, each loaded job has one line (`state`, `runs`, `last-exit`, `last-run`, the
last log line). A job that exited non-zero, or never ran in two intervals, is `RED`. On launchd,
no loaded job at all is `RED` — nothing ticks the product. Below it, one `NEEDS OPERATOR: step <s>
is on no clock` line per ASF step no clock runs.

When the doctor is green, the factory runs on its clocks. [operating.md](operating.md) is the
daily loop.
