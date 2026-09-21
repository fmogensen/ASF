#!/usr/bin/env bash
# tools/rollback.sh <product> [--apply]
#
# The inverse of tools/cutover.sh: restores every file cutover moved or rewrote, from the
# manifest cutover.sh wrote under `~/.ASF/state/<product>/retired/<date>/manifest.tsv`. Dry-run
# by default; `--apply` performs it. Refuses (exit 1) if the product was never cut over, or
# was already rolled back — nothing to invert.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ASF_HOME="${ASF_HOME:-$HOME/.ASF}"

PRODUCT=""
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    -h|--help) echo "usage: rollback.sh <product> [--apply]"; exit 0 ;;
    -*) echo "usage: rollback.sh <product> [--apply]" >&2; exit 2 ;;
    *)
      if [ -n "$PRODUCT" ]; then
        echo "usage: rollback.sh <product> [--apply]" >&2
        exit 2
      fi
      PRODUCT="$1"; shift ;;
  esac
done
if [ -z "$PRODUCT" ]; then
  echo "usage: rollback.sh <product> [--apply]" >&2
  exit 2
fi

MODE="dry-run"
[ "$APPLY" -eq 1 ] && MODE="apply"

STATE_DIR="$ASF_HOME/state/$PRODUCT"
MARKER="$STATE_DIR/cutover-done"

echo "== ROLLBACK $PRODUCT ($MODE)"

if [ ! -f "$MARKER" ]; then
  echo "rollback: $PRODUCT was not cut over (no $MARKER) — nothing to roll back" >&2
  exit 1
fi

DATE="$(cat "$MARKER")"
RETIRED_DIR="$STATE_DIR/retired/$DATE"
MANIFEST="$RETIRED_DIR/manifest.tsv"

if [ ! -f "$MANIFEST" ]; then
  echo "rollback: no manifest at $MANIFEST — cannot roll back safely" >&2
  exit 1
fi

while IFS=$'\t' read -r kind original retired extra; do
  [ -z "$kind" ] && continue
  case "$kind" in
    tick_file|plugin_skill)
      if [ "$APPLY" -eq 1 ]; then
        cp "$retired" "$original"
        echo "  $kind: restored $original from $retired"
      else
        echo "  would restore $original from $retired"
      fi
      ;;
    scheduler)
      LABEL="$extra"
      if [ "$APPLY" -eq 1 ]; then
        launchctl unload "$original" 2>/dev/null || true
        cp "$retired" "$original"
        launchctl load "$original" 2>/dev/null || true
        echo "  scheduler: restored $original ($LABEL) from $retired"
      else
        echo "  would restore $original ($LABEL) from $retired"
      fi
      ;;
    legacy_dir)
      if [ "$APPLY" -eq 1 ]; then
        if [ -e "$original" ]; then
          echo "  $original already exists — not overwriting, leaving $retired in place" >&2
        else
          mv "$retired" "$original"
          echo "  legacy_dir: restored $original from $retired"
        fi
      else
        echo "  would move $retired back to $original"
      fi
      ;;
    *)
      echo "  unknown manifest kind $kind for $original — skipping" >&2
      ;;
  esac
done < "$MANIFEST"

echo "== cutover event"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
BACKLOG_DIR="$(PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ASF_HOME="$ASF_HOME" python3 -c "
from asf import env
p = env.load_product('$PRODUCT')
print(p.backlog_dir or '')
" 2>/dev/null || true)"
if [ -n "$BACKLOG_DIR" ] && [ "$APPLY" -eq 1 ]; then
  EVENTS_DIR="$BACKLOG_DIR/metrics/events"
  mkdir -p "$EVENTS_DIR"
  TODAY="$(date -u +%Y-%m-%d)"
  python3 -c "
import json, datetime
ev = {'ts': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
      'event': 'rollback', 'product': '$PRODUCT', 'mode': '$MODE'}
with open('$EVENTS_DIR/$TODAY.jsonl', 'a', encoding='utf-8') as f:
    f.write(json.dumps(ev, sort_keys=True) + chr(10))
"
  echo "  recorded in $EVENTS_DIR/$TODAY.jsonl"
elif [ -n "$BACKLOG_DIR" ]; then
  echo "  would record {event: rollback, product: $PRODUCT} in $BACKLOG_DIR/metrics/events/"
fi

if [ "$APPLY" -eq 1 ]; then
  rm -f "$MARKER"
  echo
  echo "rollback: $PRODUCT is back on the old factory; re-run tools/cutover.sh $PRODUCT --apply when ready"
fi
