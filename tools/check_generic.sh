#!/usr/bin/env bash
# tools/check_generic.sh — no product, company or person name anywhere in this public repo,
# except the project's own name ("ASF"). Fails if any tracked file other than LICENSE matches a
# pattern in tools/forbidden-names.txt (one extended regex per line, case-insensitive).
#
# An operator may keep a private extension list (anything specific to their own deployment) and
# pass it with --extra <file>; it is never committed here.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git rev-parse --show-toplevel)"
PATTERNS_FILE="$HERE/forbidden-names.txt"
EXTRA_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --extra)
      EXTRA_FILE="$2"
      shift 2
      ;;
    *)
      echo "usage: check_generic.sh [--extra <file>]" >&2
      exit 2
      ;;
  esac
done

if [ ! -f "$PATTERNS_FILE" ]; then
  echo "check_generic: missing $PATTERNS_FILE" >&2
  exit 2
fi

patterns=()
while IFS= read -r line; do
  line="${line%%#*}"
  line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -z "$line" ] && continue
  patterns+=("$line")
done < "$PATTERNS_FILE"

if [ -n "$EXTRA_FILE" ] && [ -f "$EXTRA_FILE" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    patterns+=("$line")
  done < "$EXTRA_FILE"
fi

if [ ${#patterns[@]} -eq 0 ]; then
  echo "check_generic: no patterns loaded" >&2
  exit 2
fi

regex="$(IFS='|'; echo "${patterns[*]}")"

cd "$ROOT"
files="$(git ls-files | grep -v '^LICENSE$' || true)"

found=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  [ -f "$f" ] || continue
  if grep -HInE "$regex" -- "$f" 2>/dev/null; then
    found=1
  fi
done <<< "$files"

if [ "$found" -eq 1 ]; then
  echo "check_generic: forbidden name found (see above) — this is a public repo; no product," >&2
  echo "company, person, vendor, account or host name may appear outside LICENSE" >&2
  exit 1
fi

echo "check_generic: clean"
exit 0
