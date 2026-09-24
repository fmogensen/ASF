# Upgrading ASF

## Releases

ASF is released from its own trunk, by the same rollup it runs for any product that deploys
nothing:

- A release is a git tag `v<major>.<minor>.<patch>` on a trunk commit. At most one is cut per UTC
  day, and only when a commit naming a record item landed since the last one.
- `major.minor` is the release line (the lowest-ranked open Epic whose title carries a version);
  the patch is that line's highest so far plus one.
- Each release gets a GitHub Release and an entry at the top of [`CHANGELOG.md`](../../CHANGELOG.md)
  listing the Features landed and the Bugs fixed.
- The tag is the version. The package's own `__version__` is not rewritten per release; `asf
  --version` prints the release you run and its commit instead:
  - an install of a tag prints that tag, e.g. `asf v0.1.1 (47bab2d)`;
  - a checkout prints its nearest release tag and the commits past it, e.g.
    `asf v0.1.1+42 (20a8082)`;
  - an install of a bare sha or of `main` prints the static base version, e.g.
    `asf 0.1.0 (a3480e0)`. To tell which release that is, compare the sha with the tags:
    `git ls-remote --tags <ASF repo> | grep a3480e0`.

## Upgrading

Upgrade by running the installer again with the new ref:

```bash
bash tools/install.sh <product> v0.1.1     # a tag
bash tools/install.sh <product> 7a5881d    # or any commit sha
bash tools/install.sh <product>            # or main's current head
```

It reinstalls the pinned package with `pipx install --force`, re-applies the hooks and the clocks
(both idempotent) and ends with the doctor. With several products, run it once per product — the
package is shared, so the first run upgrades `asf` for all of them, and each later run
re-applies that product's hooks and clocks.

Then check:

```bash
asf --version                  # the release and commit you now run
asf upgrade --skip-pipx        # one row per product: record schema, package schema, action
asf doctor --product <p>
```

`asf upgrade` without `--skip-pipx` also runs `pipx upgrade asf-factory`. A pinned install keeps
the spec it was installed with, so move a pin with the installer, not with `asf upgrade`.

## Schema migrations

The record carries a schema version (`index.json`'s `schema_version`). When a release raises it,
every command that writes the record refuses until the record is migrated:

```
NEEDS OPERATOR: run asf schema-migrate — <detail>
```

(exit 3, nothing written). Reads keep working. Migrate:

```bash
asf schema-migrate --product <p> --drain     # or --all for every product
```

It migrates the tick's record clone, one commit per step (`migrate: schema a → b`), pushes it, and
sets `schema_version` in `config.yaml`. Without `--drain` it refuses while sessions are in flight;
with it, it waits for them (`--drain-timeout`, default 3600 s) and then migrates. A session
counts as in flight while its process lives and it has not yet written a success result. Migrations are
forward-only; to roll back, revert that commit and reinstall the older ref. A record newer than
the package is never migrated down — `asf upgrade` says to install the newer `asf`.

## What is safe while sessions run

| action | effect on running work |
| --- | --- |
| reinstalling the package | worker sessions are their own processes and keep running; the next tick runs the new code |
| `asf hooks install` | idempotent; it only adds entries that are missing |
| `asf scheduler install` | on launchd it boots out and reloads each tick job, which ends a tick that is running at that moment. Nothing is lost — the next tick resets its clone and derives again — but install between ticks when you can |
| `asf schema-migrate` | refuses while sessions are in flight; `--drain` waits for them |
| editing `config.yaml` or a product file | read fresh by every command and tick; run `asf doctor` after |

A tick that was killed mid-run leaves nothing half-done in the record: its commit is made only at
the end, and the next tick starts from `origin` again.
