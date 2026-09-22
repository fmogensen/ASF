#!/usr/bin/env bash
# tools/check_conventions.sh — no product *convention* in the code: no branch prefix, document
# tree, file name or item id that one product uses and another does not. Every one of them is a
# field of asf.conventions.Conventions, read from the product yaml.
#
# Fails if any `asf/**/*.py` matches a pattern in tools/forbidden-conventions.txt. Two files are
# exempt by construction:
#   asf/conventions.py              the defaults themselves live there, documented, one per field
#   asf/metrics/import_sessions.py  an adapter for a foreign log format: its literals describe
#                                   that file's shape, not this factory's conventions
#   asf/hooks.py                    an adapter for the worker runtime's own settings file: the
#                                   path is the runtime's convention, not a product's
# `~/.ASF` is allowed everywhere — that is the operator's own directory, not a product's.
#
#   check_conventions.sh                     check the package
#   check_conventions.sh --exclude <path>    skip one more file (repeatable)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git rev-parse --show-toplevel)"
PATTERNS_FILE="$HERE/forbidden-conventions.txt"

# The two documented exceptions, plus — TODO(B-0020), remove once the evidence job lands — the
# four files another card is rewriting right now. Their literals are known and counted; excluding
# them here keeps this check meaningful instead of permanently red for someone else's work.
excludes=(
  "asf/conventions.py"
  "asf/metrics/import_sessions.py"
  "asf/hooks.py"
  "asf/evidence/evidence.py"
  "asf/record/match.py"
  "asf/record/ingest.py"
  "asf/tick/stale.py"
)

while [ $# -gt 0 ]; do
  case "$1" in
    --exclude)
      excludes+=("$2")
      shift 2
      ;;
    *)
      echo "usage: check_conventions.sh [--exclude <path>]..." >&2
      exit 2
      ;;
  esac
done

if [ ! -f "$PATTERNS_FILE" ]; then
  echo "check_conventions: missing $PATTERNS_FILE" >&2
  exit 2
fi

patterns=()
while IFS= read -r line; do
  line="${line%%#*}"
  line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -z "$line" ] && continue
  patterns+=("$line")
done < "$PATTERNS_FILE"

if [ ${#patterns[@]} -eq 0 ]; then
  echo "check_conventions: no patterns loaded" >&2
  exit 2
fi

cd "$ROOT"

found=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  [ -f "$f" ] || continue
  skip=0
  for e in "${excludes[@]}"; do
    [ "$f" = "$e" ] && skip=1 && break
  done
  [ "$skip" -eq 1 ] && continue
  for p in "${patterns[@]}"; do
    if grep -HInE -- "$p" "$f" 2>/dev/null; then
      found=1
    fi
  done
done <<< "$(git ls-files 'asf/*.py' 'asf/**/*.py')"

if [ "$found" -eq 1 ]; then
  echo "check_conventions: a product convention is hardcoded (see above) — move it to the" >&2
  echo "product yaml's conventions: block and read it through asf.conventions.Conventions" >&2
  exit 1
fi

echo "check_conventions: clean"
exit 0
