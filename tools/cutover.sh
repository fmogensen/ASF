#!/usr/bin/env bash
# tools/cutover.sh <product> [--force] [--apply]
#
# Switches one product's factory to `asf` in one command. Dry-run by default (prints every step
# it would take, changes nothing); `--apply` performs them. Idempotent: a second `--apply` for a
# product that has already been cut over reports that and exits 0 without touching anything.
# Reversible: every file this moves or replaces lands under
# `~/.ASF/state/<product>/retired/<date>/` first — `tools/rollback.sh <product>` restores it.
#
# Gate (a): `asf doctor` and `asf shadow-diff` must both exit 0, or pass --force.
# (b): the operator's tick procedure file (config `operator.tick_file`) — step 0 calls
#      `asf tick --product <p>`, its tables come from `asf <view> --product <p>`.
# (c): the `/asf` plugin skills (config `operator.plugin_dir`) — each calls
#      `asf <command> --product <p>` and carries a stamp line.
# (d): the scheduler job (launchd today: config `scheduler.launchd_label`) — replaced by one
#      that runs `asf tick`; the old plist retired, not deleted.
# (e): the legacy tool directories (config `legacy_paths:`) — moved to `retired/<date>/`, not
#      deleted.
# (f): a `cutover` event recorded in the product's own metrics, then the doctor table.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ASF_HOME="${ASF_HOME:-$HOME/.ASF}"
DATE="$(date -u +%Y-%m-%d)"

PRODUCT=""
FORCE=0
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --apply) APPLY=1; shift ;;
    -h|--help) echo "usage: cutover.sh <product> [--force] [--apply]"; exit 0 ;;
    -*) echo "usage: cutover.sh <product> [--force] [--apply]" >&2; exit 2 ;;
    *)
      if [ -n "$PRODUCT" ]; then
        echo "usage: cutover.sh <product> [--force] [--apply]" >&2
        exit 2
      fi
      PRODUCT="$1"; shift ;;
  esac
done
if [ -z "$PRODUCT" ]; then
  echo "usage: cutover.sh <product> [--force] [--apply]" >&2
  exit 2
fi

MODE="dry-run"
[ "$APPLY" -eq 1 ] && MODE="apply"

run_asf() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ASF_HOME="$ASF_HOME" python3 -m asf.cli "$@"
}

cfg_get() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ASF_HOME="$ASF_HOME" python3 -c "
import os, sys
from asf import env
cfg = env.load_config()
v = cfg
for part in sys.argv[1].split('.'):
    v = (v or {}).get(part) if isinstance(v, dict) else None
print(os.path.expanduser(v) if isinstance(v, str) else (v if v is not None else ''))
" "$1" 2>/dev/null || true
}

cfg_list() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ASF_HOME="$ASF_HOME" python3 -c "
import os
from asf import env
cfg = env.load_config()
for p in (cfg.get('$1') or []):
    print(os.path.expanduser(p))
" 2>/dev/null || true
}

product_get() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ASF_HOME="$ASF_HOME" python3 -c "
import sys
from asf import env
p = env.load_product(sys.argv[1])
v = getattr(p, sys.argv[2], None)
print(v if v is not None else '')
" "$PRODUCT" "$1" 2>/dev/null || true
}

is_set() {
  [ -n "$1" ] && [ "$1" != "TODO" ]
}

STATE_DIR="$ASF_HOME/state/$PRODUCT"
RETIRED_DIR="$STATE_DIR/retired/$DATE"
MARKER="$STATE_DIR/cutover-done"
MANIFEST="$RETIRED_DIR/manifest.tsv"

record_manifest() {
  # kind, original path, retired-copy path, extra (scheduler label) — tools/rollback.sh's input
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "${4:-}" >> "$MANIFEST"
}

echo "== CUTOVER $PRODUCT ($MODE)"

if [ -f "$MARKER" ]; then
  echo "cutover: $PRODUCT already cut over on $(cat "$MARKER") — nothing to do"
  echo "(tools/rollback.sh $PRODUCT undoes it if you need to redo this)"
  exit 0
fi

# ---- (a) the gate: doctor + shadow-diff clean, or --force ------------------------------------

set +e
DOCTOR_OUT="$(run_asf doctor --product "$PRODUCT" 2>&1)"
DOCTOR_RC=$?
SHADOW_OUT="$(run_asf shadow-diff --product "$PRODUCT" 2>&1)"
SHADOW_RC=$?
set -e

echo "$DOCTOR_OUT"
echo
echo "== SHADOW-DIFF $PRODUCT"
echo "$SHADOW_OUT"
echo

GATE_CLEAN=1
[ "$DOCTOR_RC" -ne 0 ] && GATE_CLEAN=0
[ "$SHADOW_RC" -ne 0 ] && GATE_CLEAN=0

if [ "$GATE_CLEAN" -ne 1 ]; then
  if [ "$FORCE" -eq 1 ]; then
    echo "cutover: doctor and/or shadow-diff not clean — proceeding anyway (--force)"
  else
    echo "cutover: doctor and/or shadow-diff not clean — refusing (pass --force to override)" >&2
    exit 1
  fi
fi

FAILED=0
mkdir -p "$STATE_DIR"
[ "$APPLY" -eq 1 ] && mkdir -p "$RETIRED_DIR"

# ---- (b) the operator's tick procedure file ------------------------------------------------

TICK_FILE="$(cfg_get operator.tick_file)"
echo "== step 0: tick procedure file"
if ! is_set "$TICK_FILE"; then
  echo "  operator.tick_file not set in $ASF_HOME/config.yaml — skipping (an operator pass fills this in)"
elif [ ! -f "$TICK_FILE" ]; then
  echo "  operator.tick_file=$TICK_FILE not found — skipping"
else
  BLOCK_BEGIN="<!-- ASF:CUTOVER:BEGIN (do not edit between these markers; tools/cutover.sh owns it) -->"
  BLOCK_END="<!-- ASF:CUTOVER:END -->"
  BLOCK_BODY="Step 0: \`asf tick --product $PRODUCT\`. Tables: \`asf <view> --product $PRODUCT\` (roadmap, backlog, parity, prod, sessions, status)."
  if grep -qF "$BLOCK_BEGIN" "$TICK_FILE" 2>/dev/null; then
    CURRENT="$(sed -n "/$(printf '%s' "$BLOCK_BEGIN" | sed 's/[.[\*^$/]/\\&/g')/,/$(printf '%s' "$BLOCK_END" | sed 's/[.[\*^$/]/\\&/g')/p" "$TICK_FILE")"
    if [ "$CURRENT" = "$BLOCK_BEGIN
$BLOCK_BODY
$BLOCK_END" ]; then
      echo "  $TICK_FILE: already up to date"
    elif [ "$APPLY" -eq 1 ]; then
      cp "$TICK_FILE" "$RETIRED_DIR/$(basename "$TICK_FILE").bak"
      python3 - "$TICK_FILE" "$BLOCK_BEGIN" "$BLOCK_END" "$BLOCK_BODY" <<'PYEOF'
import sys
path, begin, end, body = sys.argv[1:5]
text = open(path, encoding='utf-8').read()
i, j = text.index(begin), text.index(end) + len(end)
open(path, 'w', encoding='utf-8').write(text[:i] + begin + "\n" + body + "\n" + end + text[j:])
PYEOF
      record_manifest tick_file "$TICK_FILE" "$RETIRED_DIR/$(basename "$TICK_FILE").bak"
      echo "  $TICK_FILE: updated (backup at $RETIRED_DIR/$(basename "$TICK_FILE").bak)"
    else
      echo "  would update $TICK_FILE: step 0 -> $BLOCK_BODY"
    fi
  else
    if [ "$APPLY" -eq 1 ]; then
      cp "$TICK_FILE" "$RETIRED_DIR/$(basename "$TICK_FILE").bak"
      { echo "$BLOCK_BEGIN"; echo "$BLOCK_BODY"; echo "$BLOCK_END"; echo; cat "$TICK_FILE"; } > "$TICK_FILE.new"
      mv "$TICK_FILE.new" "$TICK_FILE"
      record_manifest tick_file "$TICK_FILE" "$RETIRED_DIR/$(basename "$TICK_FILE").bak"
      echo "  $TICK_FILE: cutover block inserted (backup at $RETIRED_DIR/$(basename "$TICK_FILE").bak)"
    else
      echo "  would insert into $TICK_FILE: step 0 -> $BLOCK_BODY"
    fi
  fi
fi

# ---- (c) the /asf plugin skills ------------------------------------------------------------

PLUGIN_DIR="$(cfg_get operator.plugin_dir)"
echo "== /asf plugin skills"
if ! is_set "$PLUGIN_DIR"; then
  echo "  operator.plugin_dir not set in $ASF_HOME/config.yaml — skipping (an operator pass fills this in)"
elif [ ! -d "$PLUGIN_DIR" ]; then
  echo "  operator.plugin_dir=$PLUGIN_DIR not found — skipping"
else
  STAMP_NOTE="stamp line: \`asf --version\` / \`— generated by asf <command>@<sha> <HH:MM>\`"
  shopt -s nullglob
  for f in "$PLUGIN_DIR"/*; do
    [ -f "$f" ] || continue
    NAME="$(basename "$f")"
    CMD="${NAME%.*}"
    BLOCK_BEGIN="<!-- ASF:CUTOVER:BEGIN (do not edit between these markers; tools/cutover.sh owns it) -->"
    BLOCK_END="<!-- ASF:CUTOVER:END -->"
    BLOCK_BODY="This skill now runs \`asf $CMD --product $PRODUCT\` ($STAMP_NOTE)."
    if grep -qF "$BLOCK_BEGIN" "$f" 2>/dev/null; then
      echo "  $f: already up to date"
      continue
    fi
    if [ "$APPLY" -eq 1 ]; then
      cp "$f" "$RETIRED_DIR/$NAME.bak"
      { echo "$BLOCK_BEGIN"; echo "$BLOCK_BODY"; echo "$BLOCK_END"; echo; cat "$f"; } > "$f.new"
      mv "$f.new" "$f"
      record_manifest plugin_skill "$f" "$RETIRED_DIR/$NAME.bak"
      echo "  $f: cutover block inserted (backup at $RETIRED_DIR/$NAME.bak)"
    else
      echo "  would update $f: call \`asf $CMD --product $PRODUCT\`"
    fi
  done
  shopt -u nullglob
fi

# ---- (d) the scheduler job ------------------------------------------------------------------

LAUNCHD_LABEL="$(cfg_get scheduler.launchd_label)"
echo "== scheduler job"
if ! is_set "$LAUNCHD_LABEL"; then
  echo "  scheduler.launchd_label not set in $ASF_HOME/config.yaml — skipping"
else
  PLIST_PATH="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
  if [ "$APPLY" -eq 1 ]; then
    if [ ! -f "$PLIST_PATH" ]; then
      echo "  $PLIST_PATH not found — cannot retire it, leaving the scheduler untouched" >&2
      FAILED=1
    else
      launchctl unload "$PLIST_PATH" 2>/dev/null || true
      cp "$PLIST_PATH" "$RETIRED_DIR/$LAUNCHD_LABEL.plist"
      NEW_LABEL="${LAUNCHD_LABEL}.asf"
      cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$NEW_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>python3</string><string>-m</string><string>asf.cli</string><string>tick</string>
    <string>--product</string><string>$PRODUCT</string></array>
  <key>StartInterval</key><integer>600</integer>
</dict></plist>
PLIST
      launchctl load "$PLIST_PATH" 2>/dev/null || true
      record_manifest scheduler "$PLIST_PATH" "$RETIRED_DIR/$LAUNCHD_LABEL.plist" "$LAUNCHD_LABEL"
      echo "  $LAUNCHD_LABEL: retired to $RETIRED_DIR/$LAUNCHD_LABEL.plist; $PLIST_PATH now runs \`asf tick --product $PRODUCT\`"
    fi
  else
    echo "  would unload $LAUNCHD_LABEL, retire $PLIST_PATH to $RETIRED_DIR/, install a job there running \`asf tick --product $PRODUCT\`"
  fi
fi

# ---- (e) the legacy tool directories ---------------------------------------------------------

echo "== legacy tool directories"
while IFS= read -r dir; do
  [ -z "$dir" ] && continue
  # basename alone collides when two legacy_paths share a leaf name (e.g. two "tools" dirs);
  # the sanitized full path is unique and still legible in the retired dir.
  NAME="$(echo "$dir" | sed -e 's#^/##' -e 's#/#-#g')"
  if [ ! -d "$dir" ]; then
    echo "  $dir: not present (already retired, or never existed here)"
    continue
  fi
  if [ "$APPLY" -eq 1 ]; then
    mv "$dir" "$RETIRED_DIR/$NAME"
    record_manifest legacy_dir "$dir" "$RETIRED_DIR/$NAME"
    echo "  $dir: moved to $RETIRED_DIR/$NAME"
  else
    echo "  would move $dir to $RETIRED_DIR/$NAME"
  fi
done < <(cfg_list legacy_paths)

# ---- (f) metrics event + doctor table ---------------------------------------------------------

echo "== cutover event"
BACKLOG_DIR="$(product_get backlog_dir)"
if [ -z "$BACKLOG_DIR" ]; then
  echo "  no backlog_dir for $PRODUCT — cannot record the event"
elif [ "$APPLY" -eq 1 ]; then
  EVENTS_DIR="$BACKLOG_DIR/metrics/events"
  mkdir -p "$EVENTS_DIR"
  python3 -c "
import json, datetime
ev = {'ts': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
      'event': 'cutover', 'product': '$PRODUCT', 'mode': '$MODE'}
with open('$EVENTS_DIR/$DATE.jsonl', 'a', encoding='utf-8') as f:
    f.write(json.dumps(ev, sort_keys=True) + chr(10))
"
  echo "  recorded in $EVENTS_DIR/$DATE.jsonl"
  echo "$DATE" > "$MARKER"
else
  echo "  would record {event: cutover, product: $PRODUCT} in $BACKLOG_DIR/metrics/events/$DATE.jsonl"
fi

echo
run_asf doctor --product "$PRODUCT" || true

if [ "$FAILED" -eq 1 ]; then
  exit 1
fi
