#!/usr/bin/env bash
# tools/check_generic.sh — no product, company or person name anywhere in this public repo,
# except the project's own name ("ASF"). Delegates to the one scanner, `asf.redact --tree`
# (F-0075, D11): every tracked file but LICENSE, matched against tools/forbidden-names.txt.
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

cd "$ROOT"

set +e
out="$(PYTHONPATH="$HERE/.." python3 -m asf.redact --tree --names "$PATTERNS_FILE" ${EXTRA_FILE:+--names "$EXTRA_FILE"} 2>&1)"
rc=$?
set -e

case "$rc" in
  0)
    echo "check_generic: clean"
    ;;
  1)
    # rc 1 is the scanner's "found", but a scanner that could not start (no module, no
    # interpreter) exits 1 too. Only its own trailer distinguishes the two, and reporting the
    # second as a finding hides the real reason behind a "see above" with nothing above it.
    if printf '%s\n' "$out" | grep -q '^redact: refused'; then
      # every finding line but the scanner's own trailer; check_generic prints its own below.
      printf '%s\n' "$out" | sed '$d'
      echo "check_generic: forbidden name found (see above) — this is a public repo; no product," >&2
      echo "company, person, vendor, account or host name may appear outside LICENSE" >&2
    else
      echo "check_generic: the scanner did not run — no name was checked" >&2
      printf '%s\n' "$out" >&2
      rc=2
    fi
    ;;
  *)
    printf '%s\n' "$out" >&2
    ;;
esac

exit "$rc"
