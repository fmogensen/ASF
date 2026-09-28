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

## 1. The flags

Everything ASF knows about you and your products lives under `~/.ASF` (`$ASF_HOME` overrides it).
Nothing product-specific lives in the ASF repo. `asf install` writes it all from flags — there is
no file to copy by hand:

| flag | what it does | default |
| --- | --- | --- |
| `--repo` | the product's code repo | the current directory; asked once |
| `--repo-url` | cloned into `--repo` only when that directory does not exist | — |
| `--record` | the record's directory (the backlog: one markdown card per work item) | the product file's `backlog_dir`; asked once |
| `--record-url` | cloned into `--record` only when that directory does not exist | — |
| `--scheduler` | the clock adapter: `launchd` (installed end to end) or `cron` (ASF prints the lines, you add them) or `none` | `launchd`; asked once |
| `--account` | one worker account, `NAME[:CONFIG_DIR]`; repeatable | detected under `<ASF_HOME>/accounts/` |
| `--fake-workers` | `worker_pool.backend: fake`, and no account | off |
| `--console-permissions` | writes the console's own allow list at this scope (`user` or `repo`) instead of only offering it | offered, not written |
| `--allow-checkout` | configure from a checkout or editable install | refused otherwise |
| `--yes` | never prompt; a missing flag with no default is refused | off (a missing flag is asked for on a tty) |

`~/.ASF/config.yaml` is written once, from these flags: `default_product`, `scheduler.kind`,
`worker_pool.backend|models|accounts`, `capacity.total.sessions`. It is never rewritten after
that — edit it by hand, or see [product-config.md](product-config.md) for every key it does not
write. `worker_pool.models` (the `heavy` and `light` model ids) and `capacity` are left for you to
fill in; `worker_pool.accounts`' `home` — the session's `HOME`; without it a session inherits
every CLI login you have (see [Safety](operating.md#safety-what-a-worker-session-can-reach)) — is
also yours to add.

| key | what it resolves |
| --- | --- |
| `default_product` | the product `asf` targets when no `--product`, no `$ASF_PRODUCT` and no product whose repo or record holds the working directory answers |

`~/.ASF/products/<product>.yaml` describes the product: `product`, `repo_slug` (for `gh`; not
needed with `ci: none`), `repo_dir`, `main`, `backlog_dir`, `ci.test_command` (the gate branches
must pass before they land), `steps` and `clocks`. `asf install` writes it via `asf init`, filling
what `--repo`/`--repo-url` and `--record`/`--record-url` gave it and leaving a `# TODO` on every
key it could not — it never overwrites an existing file, it prints the diff it would have made.
[product-config.md](product-config.md) goes through the keys one by one.

## 2. Run the installer

```bash
curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> [ref] -- <asf install flags>
```

or, from a checkout of the ASF repo, `bash tools/install.sh <product> [ref] -- <asf install
flags>`. `ref` is a release tag (`v0.1.1`) or a commit sha; without one the installer pins the
newest release tag. `ASF_REPO_URL` points it at a fork or mirror. It installs the pinned package
as `asf`, then hands off to `asf install --product <product> <asf install flags>` — the config,
the record, the account, the hooks, the clocks, the plugin and the doctor, each run once.

The bootstrap pins the release with `pipx install --force`, replacing any dev install
(`pipx install -e`) — one `asf` per machine — then hands off to `asf install`'s own twelve steps:
the host and this `asf`, `~/.ASF/config.yaml`, the repos, the record's adopt, the account, the
hooks, the clocks, the plugin, the console permissions offer, the doctor, and a dry tick run —
each printing one `step N: <label>: <detail>` line. Steps 1–5 abort the run when they fail; steps
6–12 record a failure and keep going, so a red doctor or a missing account still leaves the rest
installed. `NEEDS OPERATOR: …` lines name what only you can do — see
[troubleshooting.md](troubleshooting.md). The whole run is idempotent: rerun it after fixing a
`FAILED` line.

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

A session started in the product's repo or its record needs no `ASF_PRODUCT`; set it only for a
session started outside both. Every skill passes its arguments through, and chooses the product
the way the CLI does: `--product` if you give one (`/asf:status --product other`), else
`$ASF_PRODUCT`, else the product whose repo or record holds the working directory, else
`default_product` in `~/.ASF/config.yaml`. The skills run `asf` from `PATH` (they add
`~/.local/bin`).

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
| `scheduler` | **always `ok`** — it only reports, in its detail, whether a pre-ASF launchd job named by `scheduler.launchd_label` is still loaded (`… still loaded — retire it with tools/cutover.sh`). An old job still running does not turn it red; read the detail. ASF's own jobs are checked in the `SCHEDULER` section below |
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
