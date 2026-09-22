#!/usr/bin/env bash
# tools/cutover.sh <product> [--ref DIR] [--force] [--dry-run|--apply]
#
# Switches one product's factory to `asf` in one command. Dry-run by default (prints the gate
# table and every step it would take, changes nothing); `--apply` performs them. Idempotent: a
# second `--apply` for a product that has already been cut over reports that and exits 0 without
# touching anything. Reversible: every file this moves or replaces, every job it installs and
# every job it boots out is written to `~/.ASF/state/<product>/retired/<date>/manifest.tsv` —
# `tools/rollback.sh <product> --apply` inverts exactly that list.
#
# ---- the gates ------------------------------------------------------------------------------
#
# Installing must leave a working factory or refuse. Three numbered gates stand in the way; each
# prints one line and exits 3 (gate 3 exits 4, having rolled back). `--force` does NOT bypass
# gates 1-3 — it only overrides the shadow-diff/doctor gate (a), which is a comparison against
# the old tools, not a statement about whether the new factory can run.
#
#   gate 1 referenced dirs   — no `legacy_paths` directory is still referenced by a loaded job.
#                              Moving a directory out from under a running job stops dispatch
#                              silently: the clock still fires, the script is no longer there.
#   gate 2 manifest complete — `asf tick --product <p> --manifest` exits 0, i.e. every step of
#                              the tick has an owner. A tick that carries only some of the work
#                              is a factory that looks alive and does a fraction of its job.
#   gate 3 the job runs      — after installing, the job must actually complete once: runs >= 1,
#                              last exit 0, and the record's origin gained a `tick: state`
#                              commit (or the log says the tick found no change). Otherwise this
#                              run is rolled back and the cutover exits 4 having changed nothing.
#
# Gate 1 is evaluated twice: once up front over every loaded job except the one this run retires
# itself (`scheduler.launchd_label`), and again per directory at the moment of retiring it, by
# which time that job has been booted out. A directory is retired only if its own check passes.
# A job whose label the operator has declared in `legacy_steps:` is excused: that is the
# operator saying "this step is still the old factory's, on purpose".
#
# ---- what it changes --------------------------------------------------------------------------
# (a) the gate: `asf doctor` and `asf shadow-diff --ref DIR` must both exit 0, or pass --force.
#     DIR is the reference tables/index.json the pre-asf tools produced for the same minute the
#     shadow tick ran; without --ref, shadow-diff has nothing to compare against.
# (b) the operator's tick procedure file (config `operator.tick_file`).
# (c) the `/asf` plugin skills (config `operator.plugin_dir`).
# (d) the scheduler jobs — the clock is split in three, because one job doing everything means
#     one failing step takes the whole factory down: `record` (steps record), `dispatch` (steps
#     health,wave,prs,batch, installed only when the manifest owns all four) and `daily`. The
#     old job (config `scheduler.launchd_label`) is booted out and its plist retired, not
#     deleted. The new ones come from `asf scheduler`, never from a plist written here by hand.
# (e) the legacy tool directories (config `legacy_paths:`) — moved to `retired/<date>/`.
# (f) a `cutover` event recorded in the product's own metrics, then the doctor table.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ASF_HOME="${ASF_HOME:-$HOME/.ASF}"
DATE="$(date -u +%Y-%m-%d)"

USAGE="usage: cutover.sh <product> [--ref DIR] [--force] [--dry-run|--apply]"

PRODUCT=""
REF=""
FORCE=0
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      [ $# -ge 2 ] || { echo "$USAGE" >&2; exit 2; }
      REF="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    -h|--help) echo "$USAGE"; exit 0 ;;
    -*) echo "$USAGE" >&2; exit 2 ;;
    *)
      if [ -n "$PRODUCT" ]; then echo "$USAGE" >&2; exit 2; fi
      PRODUCT="$1"; shift ;;
  esac
done
if [ -z "$PRODUCT" ]; then echo "$USAGE" >&2; exit 2; fi

MODE="dry-run"
[ "$APPLY" -eq 1 ] && MODE="apply"

# The `asf` entry point. Defaults to this checkout; an operator with `asf` on PATH, or a test
# with a stub standing in for a subcommand, overrides it with $ASF_CMD (word-split on purpose).
ASF_CMD_DEFAULT="python3 -m asf.cli"

run_asf() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ASF_HOME="$ASF_HOME" \
    ${ASF_CMD:-$ASF_CMD_DEFAULT} "$@"
}

# `asf scheduler ...`, falling back to the module entry point on a checkout whose cli.py does
# not have the subcommand wired yet. Same code either way.
SCHEDULER_VIA_CLI=0
if run_asf scheduler --help >/dev/null 2>&1; then SCHEDULER_VIA_CLI=1; fi

run_scheduler() {
  if [ "$SCHEDULER_VIA_CLI" -eq 1 ]; then
    run_asf scheduler "$@"
  else
    PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ASF_HOME="$ASF_HOME" \
      python3 -m asf.scheduler "$@"
  fi
}

py() {
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" ASF_HOME="$ASF_HOME" python3 "$@"
}

cfg_get() {
  py -c "
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
  py -c "
import os, sys
from asf import env
cfg = env.load_config()
v = cfg
for part in sys.argv[1].split('.'):
    v = (v or {}).get(part) if isinstance(v, dict) else None
for p in (v or []):
    print(os.path.expanduser(p) if isinstance(p, str) else p)
" "$1" 2>/dev/null || true
}

product_get() {
  py -c "
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
  # kind, original path, retired-copy path ('-' when there is none), extra (scheduler label).
  # tools/rollback.sh reads exactly this back.
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "${3:--}" "${4:-}" >> "$MANIFEST"
}

echo "== CUTOVER $PRODUCT ($MODE)"

if [ -f "$MARKER" ]; then
  echo "cutover: $PRODUCT already cut over on $(cat "$MARKER") — nothing to do"
  echo "(tools/rollback.sh $PRODUCT undoes it if you need to redo this)"
  exit 0
fi

LAUNCHD_LABEL="$(cfg_get scheduler.launchd_label)"
JOB_TIMEOUT="$(cfg_get cutover.job_timeout_s)"
[ -n "$JOB_TIMEOUT" ] || JOB_TIMEOUT=90
BACKLOG_DIR="$(product_get backlog_dir)"

# The declared clocks (products/<p>.yaml's `clocks:`), one row per clock: name, label, log,
# comma-joined steps. A product with no clocks: block (or any other refused clock) stops here,
# before gate 1 or gate 2 touches anything (D5).
set +e
CLOCKS_OUT="$(run_scheduler render --product "$PRODUCT" --json 2>&1)"
CLOCKS_RC=$?
set -e
if [ "$CLOCKS_RC" -ne 0 ]; then
  echo "$CLOCKS_OUT" >&2
  exit 3
fi
CLOCKS_TSV="$(printf '%s' "$CLOCKS_OUT" | py -c '
import json, sys
for j in json.load(sys.stdin):
    print("\t".join([j["clock"], j["label"], j["log"], ",".join(j.get("steps") or [])]))
')"

# ---- gate 1: is any legacy_paths dir still referenced by a loaded job? -------------------------

LEGACY_DIRS="$(cfg_list legacy_paths)"
LEGACY_STEPS="$(cfg_list legacy_steps)"

# Prints one refusal line per offending directory; empty output means the gate passes. $1 is a
# label to exempt (the job this run retires itself), '' for none.
referencing_jobs() {
  local exempt="$1"
  run_scheduler list --product "$PRODUCT" --json 2>/dev/null | py -c '
import fnmatch, json, os, sys
exempt, dirs_blob, excused_blob = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    jobs = json.load(sys.stdin)
except ValueError:
    jobs = []
excused = [p for p in excused_blob.splitlines() if p.strip()]
for raw in dirs_blob.splitlines():
    directory = raw.strip()
    if not directory:
        continue
    target = os.path.normpath(os.path.expanduser(directory))
    for job in jobs:
        label = job.get("label", "")
        if label == exempt or any(fnmatch.fnmatch(label, p) for p in excused):
            continue
        for path in job.get("paths") or []:
            if path == target or path.startswith(target + os.sep):
                print(f"cutover: {directory} is still used by loaded job {label} ({path}) "
                      "— retire the job first or declare the step under legacy_steps")
                break
' "$exempt" "$LEGACY_DIRS" "$LEGACY_STEPS" 2>/dev/null || true
}

GATE1_LINES="$(referencing_jobs "$LAUNCHD_LABEL")"
if [ -z "$GATE1_LINES" ]; then
  GATE1_RESULT="pass"
  GATE1_LINE="no legacy_paths directory is referenced by a loaded job"
else
  GATE1_RESULT="REFUSE"
  GATE1_LINE="$(printf '%s' "$GATE1_LINES" | head -1)"
fi

# ---- gate 2: does the tick manifest own every step? -------------------------------------------

set +e
MANIFEST_OUT="$(run_asf tick --product "$PRODUCT" --manifest 2>&1)"
MANIFEST_RC=$?
set -e

if [ "$MANIFEST_RC" -eq 0 ]; then
  GATE2_RESULT="pass"
  GATE2_LINE="every tick step has an owner"
else
  GATE2_RESULT="REFUSE"
  GATE2_LINE="$(printf '%s\n' "$MANIFEST_OUT" | grep -i -m1 'no owner' || true)"
  if [ -z "$GATE2_LINE" ]; then
    GATE2_LINE="$(printf '%s\n' "$MANIFEST_OUT" | grep -v '^[[:space:]]*$' | tail -1)"
  fi
  GATE2_LINE="cutover: $GATE2_LINE"
fi

echo
echo "== GATES $PRODUCT"
echo "gate · result · line"
echo "1 referenced-dirs · $GATE1_RESULT · $GATE1_LINE"
echo "2 manifest · $GATE2_RESULT · $GATE2_LINE"
if [ "$APPLY" -eq 1 ]; then
  echo "3 installed-job-runs · pending · checked after the job is installed"
else
  echo "3 installed-job-runs · skip · only --apply installs a job to check"
fi
echo

if [ "$GATE1_RESULT" = "REFUSE" ]; then
  printf '%s\n' "$GATE1_LINES" >&2
  exit 3
fi
if [ "$GATE2_RESULT" = "REFUSE" ]; then
  echo "$GATE2_LINE" >&2
  exit 3
fi

# ---- (a) the gate: doctor + shadow-diff clean, or --force ------------------------------------

set +e
DOCTOR_OUT="$(run_asf doctor --product "$PRODUCT" 2>&1)"
DOCTOR_RC=$?
SHADOW_ARGS=(shadow-diff --product "$PRODUCT")
[ -n "$REF" ] && SHADOW_ARGS+=(--ref "$REF")
SHADOW_OUT="$(run_asf "${SHADOW_ARGS[@]}" 2>&1)"
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
if [ "$APPLY" -eq 1 ]; then
  mkdir -p "$RETIRED_DIR"
  # The marker goes down before the first change, not after the last: from here on this run owns
  # a retired dir and a manifest, and `tools/rollback.sh` must be able to find them even if the
  # run dies halfway through.
  echo "$DATE" > "$MARKER"
fi

# Gate 3's baseline: where the record's origin stood before this run installed anything. It has
# to be read now — the job may complete its first run the moment it is bootstrapped.
git -C "$BACKLOG_DIR" fetch -q origin 2>/dev/null || true
ORIGIN_BASE="$(git -C "$BACKLOG_DIR" rev-parse FETCH_HEAD 2>/dev/null || true)"

rollback_and_exit() {
  echo "$1" >&2
  bash "$HERE/rollback.sh" "$PRODUCT" --apply >&2 || true
  exit 4
}

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
      py - "$TICK_FILE" "$BLOCK_BEGIN" "$BLOCK_END" "$BLOCK_BODY" <<'PYEOF'
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

# ---- (d) the scheduler jobs: retire the old one, split the clock in three ----------------------

echo "== scheduler jobs"

# Does the manifest own every one of these steps? A step the manifest never mentions has no
# owner in the new factory, so the old job that does own it stays loaded.
manifest_owns() {
  printf '%s\n' "$MANIFEST_OUT" | py -c '
import sys
wanted = [s for s in sys.argv[1].split(",") if s]
seen = set()
for line in sys.stdin:
    parts = [p.strip() for p in line.replace("·", "|").split("|")]
    if parts and parts[0]:
        seen.add(parts[0].split()[0])
sys.exit(0 if all(w in seen for w in wanted) else 1)
' "$1"
}

if ! is_set "$LAUNCHD_LABEL"; then
  echo "  scheduler.launchd_label not set in $ASF_HOME/config.yaml — no old job to retire"
else
  OLD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
  if [ "$APPLY" -eq 1 ]; then
    if [ ! -f "$OLD_PLIST" ]; then
      echo "  $LAUNCHD_LABEL: no plist at $OLD_PLIST — nothing to retire"
    else
      cp "$OLD_PLIST" "$RETIRED_DIR/$LAUNCHD_LABEL.plist"
      launchctl bootout "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null || true
      rm -f "$OLD_PLIST"
      record_manifest scheduler_retire "$OLD_PLIST" "$RETIRED_DIR/$LAUNCHD_LABEL.plist" "$LAUNCHD_LABEL"
      echo "  $LAUNCHD_LABEL: booted out, plist retired to $RETIRED_DIR/$LAUNCHD_LABEL.plist"
    fi
  else
    echo "  would boot out $LAUNCHD_LABEL and retire $OLD_PLIST to $RETIRED_DIR/"
  fi
fi

INSTALLED_LABELS=""
RECORD_CLOCK=""
RECORD_LABEL_RENDERED=""
RECORD_LOG=""

install_job() {  # clock, label
  local clock="$1" label="$2"
  if [ "$APPLY" -eq 1 ]; then
    run_scheduler install --product "$PRODUCT" --clock "$clock" | sed 's/^/  /'
    record_manifest scheduler_install "$HOME/Library/LaunchAgents/$label.plist" "-" "$label"
    INSTALLED_LABELS="$INSTALLED_LABELS $label"
    echo "  $label: installed (clock $clock)"
  else
    echo "  would install $label (clock $clock)"
  fi
}

while IFS=$'\t' read -r clock label log steps; do
  [ -z "$clock" ] && continue
  if manifest_owns "$steps"; then
    install_job "$clock" "$label"
  else
    echo "  $clock ($steps): the manifest does not own these steps — leaving the legacy job loaded"
  fi
  if [ -z "$RECORD_CLOCK" ]; then
    case ",$steps," in
      *,record,*) RECORD_CLOCK="$clock"; RECORD_LABEL_RENDERED="$label"; RECORD_LOG="$log" ;;
    esac
  fi
done <<< "$CLOCKS_TSV"

# ---- gate 3: the installed job must actually complete once ------------------------------------

job_completed() {
  run_scheduler status --product "$PRODUCT" --clock "$RECORD_CLOCK" --json 2>/dev/null | py -c '
import json, sys
try:
    info = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
sys.exit(0 if info.get("loaded") and (info.get("runs") or 0) >= 1
         and info.get("last_exit") == 0 else 1)
' 2>/dev/null
}

record_advanced() {
  [ -n "$BACKLOG_DIR" ] || return 1
  if [ -f "$RECORD_LOG" ] && grep -q 'no change' "$RECORD_LOG"; then return 0; fi
  git -C "$BACKLOG_DIR" fetch -q origin 2>/dev/null || return 1
  local head subject
  head="$(git -C "$BACKLOG_DIR" rev-parse FETCH_HEAD 2>/dev/null || true)"
  [ -n "$head" ] && [ "$head" != "$ORIGIN_BASE" ] || return 1
  subject="$(git -C "$BACKLOG_DIR" log -1 --format=%s FETCH_HEAD 2>/dev/null || true)"
  case "$subject" in "tick: state"*) return 0 ;; esac
  return 1
}

if [ "$APPLY" -eq 1 ]; then
  if [ -z "$RECORD_CLOCK" ]; then
    rollback_and_exit "NEEDS OPERATOR: products/$PRODUCT.yaml has no clock that ticks record — add one (see docs/products.example.yaml)"
  fi
  RECORD_LABEL="$RECORD_LABEL_RENDERED"
  echo "== gate 3: waiting up to ${JOB_TIMEOUT}s for $RECORD_LABEL to complete a run"
  DEADLINE=$(( $(date +%s) + JOB_TIMEOUT ))
  GATE3_OK=0
  while :; do
    if job_completed && record_advanced; then GATE3_OK=1; break; fi
    [ "$(date +%s)" -ge "$DEADLINE" ] && break
    sleep 3
  done
  if [ "$GATE3_OK" -ne 1 ]; then
    rollback_and_exit "cutover: installed job did not complete — rolled back"
  fi
  echo "  $RECORD_LABEL: completed a run, the record's origin has it"
fi

# ---- (e) the legacy tool directories ---------------------------------------------------------
#
# Re-checked per directory: the old job has been booted out by now, so a directory that gate 1
# excused for it is free to move — and one that some *other* loaded job still points at is not,
# no matter what the up-front gate said.

echo "== legacy tool directories"
STILL_REFERENCED="$(referencing_jobs '')"
while IFS= read -r dir; do
  [ -z "$dir" ] && continue
  # basename alone collides when two legacy_paths share a leaf name (e.g. two "tools" dirs);
  # the sanitized full path is unique and still legible in the retired dir.
  NAME="$(echo "$dir" | sed -e 's#^/##' -e 's#/#-#g')"
  if [ ! -d "$dir" ]; then
    echo "  $dir: not present (already retired, or never existed here)"
    continue
  fi
  BLOCKER="$(printf '%s\n' "$STILL_REFERENCED" | grep -F "cutover: $dir is still used" | head -1 || true)"
  if [ -n "$BLOCKER" ]; then
    echo "  $BLOCKER"
    echo "  $dir: left in place (gate 1)"
    continue
  fi
  if [ "$APPLY" -eq 1 ]; then
    mv "$dir" "$RETIRED_DIR/$NAME"
    record_manifest legacy_dir "$dir" "$RETIRED_DIR/$NAME"
    echo "  $dir: moved to $RETIRED_DIR/$NAME"
  else
    echo "  would move $dir to $RETIRED_DIR/$NAME"
  fi
done <<< "$LEGACY_DIRS"

# ---- (f) metrics event + doctor table ---------------------------------------------------------

echo "== cutover event"
if [ -z "$BACKLOG_DIR" ]; then
  echo "  no backlog_dir for $PRODUCT — cannot record the event"
elif [ "$APPLY" -eq 1 ]; then
  EVENTS_DIR="$BACKLOG_DIR/metrics/events"
  mkdir -p "$EVENTS_DIR"
  py -c "
import json, datetime
ev = {'ts': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
      'event': 'cutover', 'product': '$PRODUCT', 'mode': '$MODE'}
with open('$EVENTS_DIR/$DATE.jsonl', 'a', encoding='utf-8') as f:
    f.write(json.dumps(ev, sort_keys=True) + chr(10))
"
  echo "  recorded in $EVENTS_DIR/$DATE.jsonl"
else
  echo "  would record {event: cutover, product: $PRODUCT} in $BACKLOG_DIR/metrics/events/$DATE.jsonl"
fi

echo
run_asf doctor --product "$PRODUCT" || true

if [ "$FAILED" -eq 1 ]; then
  exit 1
fi
