---
name: doctor
description: "ASF: is this product's install sound — config, scheduler, checkout, record, CI — one table"
allowed-tools: Bash
---

Print the output below **verbatim** in a fenced code block and stop. Arguments after the command are passed through (`--product <p>`).

!`PATH="$HOME/.local/bin:$PATH"; if command -v asf >/dev/null 2>&1; then asf doctor $ARGUMENTS 2>&1; else echo "asf is not installed: pipx install -e <ASF checkout>"; fi`