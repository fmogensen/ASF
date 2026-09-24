---
name: doctor
description: "ASF: is this product's ASF install sound — one table"
allowed-tools: Bash
---

Print the output below **verbatim** in a fenced code block and stop. Do not summarise, reorder or comment on it unless asked. The product is `$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`; arguments after the command are passed through.

!`PATH="$HOME/.local/bin:$PATH"; ASF_BIN=$(command -v asf); if [ -n "$ASF_BIN" ]; then ASF_TABLES=box "$ASF_BIN" doctor $ARGUMENTS 2>&1 || true; else echo "asf is not installed: bash tools/install.sh <product>"; fi`
