#!/usr/bin/env bash
# tools/check_clients.sh — no new raw `gh`/`git` call site: every gh call belongs in asf/github.py,
# every git call in asf/gitops.py / asf/gitpush.py. Per-file counts of raw argv sites (and of broad
# `except Exception` lines) may only fall below tools/clients-baseline.txt, never rise; a line
# marked `# client-exempt: <reason>` is not counted. Once asf/gitpush.py declares
# `__gitpush_door__ = True`, every `gitpush.push(` call must also pass `guard=`.
# The rules and the counting live in tools/check_clients.py.
#
#   check_clients.sh                    check the tree (exit 1 on a rise)
#   check_clients.sh --write-baseline   rewrite the baseline (only ever to lower it)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
exec python3 "$HERE/check_clients.py" --root "$ROOT" "$@"
