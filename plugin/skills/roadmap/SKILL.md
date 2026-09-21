---
name: roadmap
description: "ASF: show the ROADMAP table, one row per Epic"
allowed-tools: Bash
---

Print the output below **verbatim** in a fenced code block and stop. Do not summarise, reorder or comment on it unless asked. The product is `$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`; arguments after the command are passed through.

!`if command -v asf >/dev/null 2>&1; then asf roadmap $ARGUMENTS; else python3 -m asf.cli roadmap $ARGUMENTS; fi`
