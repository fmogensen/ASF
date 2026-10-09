# Security checks

ASF reads two feeds a code host already keeps — open secret-scanning alerts and open Dependabot
alerts — as one of its own rules, `R-0009`, and files every open one as a Bug. No alert's own
*value* is ever read, cached or printed: not a secret, not a machine address, not an account
name. Only counts, kinds and an age ever reach a line this factory prints, holds or files.

## `conventions.security.alerts`

```yaml
conventions:
  security:
    alerts:
      max_age_h: 24   # default
```

| key | default | what it is |
|---|---|---|
| `alerts.max_age_h` | 24 | how long a read of the host's own feeds may age before an unread feed is itself a violation (not a pass) |

`security.paths` (which of a diff's files are sensitive, read by the precheck pass) and
`security.ports` (an exposed-port probe) are validated the same way but are a different Feature's
concern — `security.ports` in particular is validated and documented but nothing reads it yet.
Both are documented key by key in `docs/products.example.yaml`.

## What `R-0009` files

`asf security alerts --check` is the rule's own check: it prints one line per place on stdout and
exits 1 when there is one, 0 when there is none — the contract `asf rules check` reads. `asf
security alerts` with no `--check` prints the same lines as a human report and exits 0.

Three kinds of line, each carrying the two optional trailing fields the filer reads
(`sev=S1|S2|S3`, `sig=<key>`) so each becomes its own Bug instead of folding into one Bug per
rule:

| finding | severity | signature | files as |
|---|---|---|---|
| an open secret-scanning alert | `S1` | `secret-<repo>-<n>` | its own Bug, `<repo>#<n>` and its kind as evidence |
| an open Dependabot alert | `S2` | `dep-<repo>-<n>` | its own Bug, the package and severity as evidence |
| the feeds could not be read, and the last good read is stale or absent | `S2` | `feed-stale` | one Bug naming how long ago the last read was (or that there has never been one) |

A read that fails behind a still-fresh cache reports nothing — the cache's alerts are already
filed, and reporting them again would refile them. A read that succeeds rewrites the cache so the
next failure has something fresh to fall back on.

## Adopting it

A product adopts a core check by giving its own record a rule card of the same shape, naming the
core script by basename — the `hook:` half of a card binds from the core checkout on `asf hooks
install`; the `check:` half binds only through a card in the product's own record, because an
installed `asf` ships `asf/**` only. Nothing elsewhere reads this product's record to find it.

`tools/checks/r0009.sh` — the one-line shim a product's own repo carries:

```sh
#!/usr/bin/env bash
# R-0009 — the host's own secret- and dependency-scanning alerts, read as this factory's Bugs.
# contract: asf rules check calls this; exit 0 pass, exit 1 with one line per place.
set -euo pipefail
exec "${ASF:-asf}" security alerts --check
```

And the one card in the product's own record, `R-0009.md` (or any id — `rules.py`'s `partition`
reads `type: rule` and `check:` off whichever card names the script):

```yaml
---
id: R-0009
type: rule
title: the host's own secret- and dependency-scanning alerts are read every tick
check: tools/checks/r0009.sh
---
the code host's secret-scanning and dependency alerts are read every tick; every open alert is a
Bug keyed on the alert; a feed that could not be read is a violation, not a pass.
```

No `hook:` — a `PreToolUse` hook cannot see a feed; this rule only ever runs as a check.

## The doctor's `security` row

`asf doctor` reads the product's own record and the feed cache only — it never spends a host
round trip of its own. The row stays red until both are true: the record carries the card above,
and the cache is no older than `security.alerts.max_age_h`.

| row | what it means | fix |
|---|---|---|
| `security` RED, "not adopted" | no rule card in the record names `r0009.sh` | add the card above |
| `security` RED, "never been read" | the feeds have no cache yet | run `asf security alerts` once, or wait for the next tick |
| `security` RED, "last read `<n>`h ago" | the cache is older than `max_age_h` | the next successful read refreshes it; a host outage past the window is loud on purpose |
| `security` green, `alerts <n>h · R-0009 adopted` | the card exists and the cache is fresh | nothing to do |

This row is informational (not required): a product that has adopted nothing is loud about it,
but the row never fails `asf doctor`'s own exit code on its own.
