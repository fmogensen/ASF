---
name: groom
description: "ASF: inbox -> cards, then write groom/<date>.md"
allowed-tools: Bash, Read, Edit
---

Print the output below **verbatim** in a fenced code block. The product is `$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`; arguments after the command are passed through. Then read `<backlog_dir>/groom/<today UTC>.md` (`backlog_dir` is in `~/.ASF/products/<product>.yaml`; the product is `--product` if given, else `$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`) and show its open questions (`→ answer: ____`) as one table: item, question, your recommended answer with a one-line reason. Wait for the operator's answers (`yes` / `no` / `rank 2` / `parent E-nnnn` / `S1` / `duplicate of B-nnnn` / `all as recommended`); write each into that line's `answer:` slot, run `asf groom --apply <same args>` and print its output verbatim. Never write `decided: true` anywhere except through an answer.

!`PATH="$HOME/.local/bin:$PATH"; if command -v asf >/dev/null 2>&1; then ASF_TABLES=box asf groom $ARGUMENTS 2>&1 || true; else echo "asf is not installed: pipx install -e <ASF checkout>"; fi`
