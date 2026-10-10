---
name: console-feed
description: "ASF: the FACTORY STATUS table plus the tick deltas since the last call, when console.status_every says it is due (B-0121)"
allowed-tools: Bash
---

Print the output below **verbatim** in a fenced code block and stop. Do not summarise, reorder or comment on it unless asked. The product is `--product` if you give one, else `$ASF_PRODUCT`, else the product whose repo or record holds the working directory, else `default_product` in `~/.ASF/config.yaml`; arguments after the command are passed through.

!`PATH="$HOME/.local/bin:$PATH"; ASF_BIN=$(command -v asf); if [ -n "$ASF_BIN" ]; then ASF_TABLES=box "$ASF_BIN" console-feed $ARGUMENTS 2>&1 || true; else echo "asf is not installed: bash tools/install.sh <product>"; fi`
