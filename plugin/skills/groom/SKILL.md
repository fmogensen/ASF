---
name: groom
description: "ASF: run the groom — type inbox cards, list the operator's questions; --apply takes the answers"
allowed-tools: Bash, Read, Edit
---

Print the output below **verbatim** in a fenced code block. Then read `<backlog_dir>/groom/<today UTC>.md` (`backlog_dir` is in `~/.ASF/products/<product>.yaml`; the product is `--product` if given, else `$ASF_PRODUCT`, else `default_product` in `~/.ASF/config.yaml`) and show its open questions (`→ answer: ____`) as one table: item, question, your recommended answer with a one-line reason. Wait for the operator's answers (`yes` / `no` / `rank 2` / `parent E-0001` / `S1` / `duplicate of B-0038` / `all as recommended`); write each into that line's `answer:` slot, run `asf groom --apply <same args>` and print its output verbatim. Never write `decided: true` anywhere except through an answer. Arguments after the command are passed through.

!`PATH="$HOME/.local/bin:$PATH"; if command -v asf >/dev/null 2>&1; then asf groom $ARGUMENTS 2>&1; else echo "asf is not installed: pipx install -e <ASF checkout>"; fi`