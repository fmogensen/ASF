# Connectors (planned)

> **Not yet available.** Nothing on this page is in the current release: there is no
> `connectors:` key, no `asf connect` and no `asf connectors` command. This page describes the
> design recorded in the factory's backlog so you can plan for it. Until it lands, use what
> exists today, listed at the end.

## The problem it solves

A product talks to external services that need auth — its code host and CI, a hosting platform, a
database — and the factory talks to one or more LLM runner accounts. Today:

- the doctor checks a fixed list of vendor CLIs, and only whether each is logged in;
- what a product *uses* a service for (CI, deploy, database) is not data anywhere, so the status
  rows need hand-set keys (`ci.runner_org`, `deploy_sha.workflow`) and the approval matrix cannot
  tell a deploy from a read;
- worker accounts are configured by hand in `worker_pool.accounts`.

## The design

A **connector** is one external service, declared once. Service connectors are per product (in
the product file); runner connectors are per machine (in `config.yaml`, shared by every product).

```yaml
connectors:
  <name>:                          # a free name: host, db, ci, …
    kind: cli | token | oauth
    template: <catalogue entry>    # optional: a shipped template fills the defaults
    check: [<argv>]                # prints the identity; exit 0 = authenticated
    login: [<argv>]                # what you run once, interactively
    secret: keychain:<service> | env:<VAR> | file:<path>   # never in a record or repo
    provides: [ci, deploy, db, preview, secrets]
    actions:                       # command patterns → approval class
      deploy:  {match: "<regex>", class: touch_production}
      migrate: {match: "<regex>", class: touch_customer_data}
    sessions: inherit | none       # whether worker sessions get this auth
```

A runner connector (`kind: runner`) names its runtime adapter, the account's isolated config dir,
its `check`, `login` and `quota` commands, the models it may run and its lane.

Planned pieces:

- **A catalogue** of templates for common services, shipped as data, not code — adding a service
  is a data change, and ASF's code still names no vendor.
- **`asf connect <name> --product <p>`** — guided, idempotent setup: is the tool installed, is it
  logged in (if not, the exact login command to run), verify the check, write the connector into
  the product file (with a backup), offer `sessions: inherit`. For a runner, adding a worker
  account becomes one command.
- **`asf connectors --product <p>`** — one table: connector, provides, identity,
  ok / expired / missing, last checked; runners also show their quota band and load.
- **The doctor** builds its rows from the product's connectors instead of a fixed CLI list; a
  required connector that is not authenticated is `RED`, with the command that fixes it.
- **Status rows** read the connector that `provides: deploy` (Prod) and `provides: ci` (Runners);
  the old keys stay as aliases.
- **Approvals**: a connector's `actions` feed the matrix, so a deploy through any connector is
  `touch_production` without per-vendor code.
- **Sessions**: default-deny. Workers get an isolated home, and only connectors marked
  `sessions: inherit` are provisioned into it, each with its least-privileged credential; declared
  per-connector environments keep production contexts away from workers unless the matrix grants
  that class.
- **Expiry**: the `health` step re-checks connectors (cached) and an expired login is one
  `NEEDS OPERATOR` line naming its login command; a runner whose login expired leaves the pool
  like an account at `stop`.
- **Leaks**: the redaction gate learns each connector's secret shape.

## Until then

- **Service logins**: log in to each CLI yourself; `asf doctor` shows `git` and `gh` (required) and
  a few common cloud and hosting CLIs (optional).
- **Deploy and CI facts**: `ci:` and `deploy_sha:` in the product file.
- **Guarding production actions**: `approval_signals:` in the product file adds your own
  `paths` and `commands` recognisers to a class.
- **Worker accounts**: `worker_pool.accounts` in `config.yaml`. Give each account a `config_dir`,
  and a `home` if its sessions must not share your CLI logins: a session's `HOME` is only
  isolated when the account sets `home`; otherwise it inherits every CLI login on this machine.
