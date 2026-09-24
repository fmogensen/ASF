# ASF plugin for Claude Code

The factory's tables in the console. **Generated from the CLI** — `asf plugin build` writes one
skill per operator view from the command's own help; `asf plugin check` (run by the tests and CI)
fails when the tree drifts. Never edit `skills/*/SKILL.md` by hand.

| Skill | Prints |
| --- | --- |
| `/asf:status` | the FACTORY STATUS table |
| `/asf:next` | the NEXT table: what the tick would start, S1 first |
| `/asf:backlog` | the BOARD table: one row per Feature, grouped by Epic |
| `/asf:roadmap` | the ROADMAP table: one row per Epic |
| `/asf:parity` | the PARITY table: one row per Story |
| `/asf:prod` | the PROD table: deploy state and what just shipped |
| `/asf:sessions` | the SESSIONS table |
| `/asf:capacity` | the CAPACITY table: sessions and CI runs per product |
| `/asf:doctor` | is this product's install sound — one table |
| `/asf:groom` | the groom: inbox → cards, the operator's questions, `--apply` with the answers |

Every skill passes its arguments through (`/asf:status --product <p>`); without `--product` the
CLI uses `$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`.

## Install

The repository is its own marketplace (`.claude-plugin/marketplace.json` at the root):

```
/plugin marketplace add <owner>/ASF        # or a local path: /plugin marketplace add ~/Code/<owner>/ASF
/plugin install asf@asf
```

Requires `asf` on `PATH` (`pipx install -e <checkout>` puts it in `~/.local/bin`, which the skills
add themselves).

## Names from a first-generation factory

A product's earlier console may have had tables the generic factory does not ship: a *goals*
table is the ROADMAP (an Epic is a goal); a *health* table is `/asf:doctor` (the install) plus
`asf workers health` (the sessions); *ci* and *performance* tables are sections of STATUS, shown
when the product's `ci:` config provides the data; an *experiments* table is a product feature,
not a factory table. There are no aliases.
