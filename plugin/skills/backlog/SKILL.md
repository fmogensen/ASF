---
name: backlog
description: "ASF: the BOARD table: one row per Feature, grouped by Epic"
allowed-tools: Bash
---

Print the output below **verbatim** in a fenced code block and stop. Do not summarise, reorder or comment on it unless asked. The product is `$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`; arguments after the command are passed through.

!`PATH="$HOME/.local/bin:$PATH"; if command -v asf >/dev/null 2>&1; then ASF_TABLES=box asf backlog $ARGUMENTS 2>&1 || true; else echo "asf is not installed: pipx install -e <ASF checkout>"; fi`
