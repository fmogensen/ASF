---
name: scorecard
description: "ASF: the SCORECARD: value shipped per week, its cost, where it went, and the causes the loop filed"
allowed-tools: Bash
---

Print the output below **verbatim** in a fenced code block and stop. Do not summarise, reorder or comment on it unless asked. The product is `--product` if you give one, else `$ASF_PRODUCT`, else the product whose repo or record holds the working directory, else `default_product` in `~/.ASF/config.yaml`; arguments after the command are passed through.

!`PATH="$HOME/.local/bin:$PATH"; ASF_BIN=$(command -v asf); if [ -n "$ASF_BIN" ]; then ASF_TABLES=box "$ASF_BIN" scorecard $ARGUMENTS 2>&1 || true; else echo "asf is not installed: bash tools/install.sh <product>"; fi`
