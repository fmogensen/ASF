#!/usr/bin/env bash
# tools/check_record_writers.sh — one card writer: no module under asf/record/ opens a file for
# writing ('w', 'wb', 'w+', …) except asf/record/writer.py, whose writes are one os.replace each
# (a reader never sees a truncated card). An append ('a') is not a card write and is not counted.
#
#   check_record_writers.sh    exit 1 and print file:line for each in-place writer
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

hits="$(grep -rnE --include='*.py' "open\(.*['\"]w[bt+]?['\"]" asf/record/ \
        | grep -v '^asf/record/writer\.py:' || true)"
if [ -n "$hits" ]; then
  echo "check_record_writers: a record file written in place — use asf.record.writer:" >&2
  echo "$hits"
  exit 1
fi
echo "check_record_writers: ok"
