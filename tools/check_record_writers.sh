#!/usr/bin/env bash
# tools/check_record_writers.sh — one card writer: no module under asf/record/ opens a file for
# writing ('w', 'wb', 'w+', …) except asf/record/writer.py, whose writes are one os.replace each
# (a reader never sees a truncated card). Outside asf/record/, a card write is recognised by its
# path: open(<rec>['path'] or <rec>["path"], 'w'…) — the shape every record writer reads a card
# path in (a load_items record's 'path'); other files (reports, temp files) are not matched.
# asf/tick/flaky.py is exempt until its W5-PR5 rewrite lands and moves it onto the writer.
# An append ('a') is not a card write and is not counted.
#
#   check_record_writers.sh    exit 1 and print file:line for each in-place writer
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

hits="$({ grep -rnE --include='*.py' "open\(.*['\"]w[bt+]?['\"]" asf/record/ || true
          grep -rnE --include='*.py' "open\([A-Za-z_]*rec\[['\"]path['\"]\], *['\"]w[bt+]?['\"]" asf \
              | grep -v '^asf/record/' | grep -v '^asf/tick/flaky\.py:' || true
        } | grep -v '^asf/record/writer\.py:' || true)"
if [ -n "$hits" ]; then
  echo "check_record_writers: a record file written in place — use asf.record.writer:" >&2
  echo "$hits"
  exit 1
fi
echo "check_record_writers: ok"
