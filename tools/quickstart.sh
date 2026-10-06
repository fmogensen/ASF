#!/usr/bin/env bash
# tools/quickstart.sh — the sample product, start to finish, in twenty minutes.
#
#   bash tools/quickstart.sh [<dir>]
#
# Materialises `sample/` into <dir> (a fresh `mktemp -d` by default), points a fresh operator
# home at it, and runs it through `asf init`, `asf next`, one `asf tick`, the ticks that land a
# first Task (the stub runtime replaying `sample/first_task.json` does each session's work), `asf
# doctor`, `asf roadmap` and `asf scorecard` — the same walk `tests/test_sample_product.py`'s `setUpClass`
# takes, in the form a stranger with a clone and nothing else can run. stdlib and `git` only, no
# account, no network beyond the loopback the script makes of itself, and no `gh` — the sample is
# `ci: {provider: none}`, `deploy_sha: none`, and its workers are the `fake` runtime replaying
# `sample/fake_script.json`. Every path it writes is under <dir>; it never removes <dir> itself.
set -euo pipefail

TOTAL=10
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STARTED="$(date +%s)"

die() {
  printf 'quickstart: %s\n' "$1" >&2
  exit 2
}

if [ "$#" -gt 1 ]; then
  die "usage: bash tools/quickstart.sh [<dir>]"
fi

DIR="${1:-}"
if [ -z "$DIR" ]; then
  DIR="$(mktemp -d)"
else
  mkdir -p "$DIR"
fi
DIR="$(cd "$DIR" && pwd)"
echo "$DIR"

step() {
  printf 'quickstart: %s/%s %s\n' "$1" "$TOTAL" "$2"
}

open_line() {
  printf '  open: %s\n' "$1"
}

# ---- Step 1 — check the host ------------------------------------------------------------

step 1 'checking the host'
command -v git >/dev/null 2>&1 || die "git is not installed — install it and re-run"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed — install it and re-run"
ASF_BIN=(asf)
if ! command -v asf >/dev/null 2>&1; then
  ASF_BIN=(python3 -m asf.cli)
  export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
fi
if ! ASF_VERSION="$("${ASF_BIN[@]}" --version 2>&1)"; then
  die "\`${ASF_BIN[*]} --version\` failed — check the install: $ASF_VERSION"
fi
echo "  $ASF_VERSION"

# ---- Step 2 — materialise the sample product --------------------------------------------

step 2 'copying the sample product'
SAMPLE_DIR="$DIR/sample"
cp -R "$ROOT/sample" "$SAMPLE_DIR"
REPO="$SAMPLE_DIR/repo"
BACKLOG="$SAMPLE_DIR/backlog"

publish() {
  # A tree becomes a git repo whose `main` is pushed to a new bare origin — the shape
  # `tests/test_sample_product.py`'s `_publish` makes.
  local tree="$1" origin="$2"
  git init -q --bare -b main "$origin"
  git -C "$tree" init -q -b main
  git -C "$tree" config user.email 'sample@example.com'
  git -C "$tree" config user.name 'sample'
  git -C "$tree" add -A
  git -C "$tree" commit -q -m 'sample'
  git -C "$tree" remote add origin "$origin"
  git -C "$tree" push -q -u origin main
  git -C "$tree" remote set-head origin main
}
publish "$REPO" "$DIR/repo.git"
publish "$BACKLOG" "$DIR/backlog.git"
open_line "$SAMPLE_DIR/README.md"

# ---- Step 3 — write the operator home ----------------------------------------------------

step 3 'writing the operator home'
ASF_HOME="$DIR/.ASF"
mkdir -p "$ASF_HOME/products"
sed -e "s#@REPO@#$REPO#g" -e "s#@BACKLOG@#$BACKLOG#g" \
  "$SAMPLE_DIR/product.yaml" > "$ASF_HOME/products/sample.yaml"
sed -e "s#@SAMPLE@#$SAMPLE_DIR#g" "$SAMPLE_DIR/config.yaml" > "$ASF_HOME/config.yaml"
export ASF_HOME
# The reader's own `~` and `~/.ASF` are read for nothing and written for nothing from here on:
# every `asf` call below runs under this temp HOME instead (PD6).
export HOME="$DIR/home"
mkdir -p "$HOME"
# scratch too: a background harvest keeps its gate checkout under TMPDIR
export TMPDIR="$DIR/tmp"
mkdir -p "$TMPDIR"
export GIT_AUTHOR_NAME='sample' GIT_AUTHOR_EMAIL='sample@example.com'
export GIT_COMMITTER_NAME='sample' GIT_COMMITTER_EMAIL='sample@example.com'
open_line "$ASF_HOME/products/sample.yaml"

# ---- Step 4 — adopt the product ------------------------------------------------------------

step 4 'adopting the product (asf init)'
"${ASF_BIN[@]}" init --product sample
# B-0131: a real install ends with the operator writing the console's own allow list — this
# script stands in for that operator, the same way the test's fixture does, so `asf doctor`'s
# console-permissions row is green at Step 7.
"${ASF_BIN[@]}" console-permissions install --product sample --scope user
open_line "$BACKLOG/index.json"

# ---- Step 5 — what the tick would do -------------------------------------------------------

step 5 'previewing the tick (asf next)'
"${ASF_BIN[@]}" next --product sample
echo '  (the S1 Bug above is what the tick launches next)'

# ---- Step 6 — run the tick ------------------------------------------------------------------

step 6 'running one tick (asf tick)'
"${ASF_BIN[@]}" tick --product sample
open_line "$ASF_HOME/state/sample/record"

# ---- Step 7 — tick until a first Task lands ---------------------------------------------------

step 7 'ticking until a first Task lands (asf tick, the stub runtime does the work)'
# From here each session commits and pushes what `sample/first_task.json` scripts for it, so
# the loop runs card → spec → plan → Task → landed with no agent. The stub spends nothing on this
# host, so the host guard is off for this walk. Each tick waits for the harvest it started.
sed -i.bak -e "s#$SAMPLE_DIR/fake_script.json#$SAMPLE_DIR/first_task.json#" "$ASF_HOME/config.yaml"
rm -f "$ASF_HOME/config.yaml.bak"
printf 'host_guards:\n  load_per_core: 0\n  swap_pct: 0\n' >> "$ASF_HOME/config.yaml"
landed_task() {
  python3 - "$BACKLOG/index.json" <<'PY'
import json, sys
items = json.load(open(sys.argv[1], encoding='utf-8')).get('items') or {}
done = sorted(k for k, v in items.items() if v.get('type') == 'task' and v.get('state') == 'Closed')
print(done[0] if done else '')
PY
}
LANDED=''
for _ in $(seq 1 "${QUICKSTART_MAX_TICKS:-16}"); do
  OUT="$("${ASF_BIN[@]}" tick --product sample --fresh)"
  printf '%s\n' "$OUT" | grep -E '^(launched|landed|held) ' | sed 's/^/  /' || true
  HPID="$(printf '%s\n' "$OUT" | sed -n 's/.*harvest: started in the background (pid \([0-9][0-9]*\)).*/\1/p' | head -1)"
  if [ -n "$HPID" ]; then
    while kill -0 "$HPID" 2>/dev/null; do sleep 0.5; done
  fi
  LANDED="$(landed_task)"
  [ -n "$LANDED" ] && break
done
if [ -z "$LANDED" ]; then
  echo 'quickstart: no Task landed — read the tick lines above' >&2
  exit 4
fi
echo "quickstart: first Task landed: $LANDED"

# ---- Step 8 — check the install --------------------------------------------------------------

step 8 'checking the install (asf doctor)'
set +e
DOCTOR_OUT="$("${ASF_BIN[@]}" doctor --product sample)"
DOCTOR_RC=$?
set -e
echo "$DOCTOR_OUT"
if [ "$DOCTOR_RC" -ne 0 ]; then
  echo 'quickstart: doctor is red — the row and the line that fixes it:' >&2
  printf '%s\n' "$DOCTOR_OUT" | grep '  RED  ' >&2 || true
  exit 3
fi

# ---- Step 9 — the record's two views ---------------------------------------------------------

step 9 'reading the record (asf roadmap, asf scorecard)'
"${ASF_BIN[@]}" roadmap --product sample
"${ASF_BIN[@]}" scorecard --product sample
# the sample's `steps: {daily: off}` means the tick wrote no ROADMAP.md/SCORECARD.md pages — the
# two views above are the whole of what there is to read, not a file left unpointed-at.
echo '  the sample runs no daily step, so these two views are not also written as pages'

# ---- Step 10 — what to read next --------------------------------------------------------------

step 10 'what to read next'
open_line "$SAMPLE_DIR/repo"
open_line "$ASF_HOME/state/sample/record"
open_line "$ASF_HOME/state/sample/worktrees"
open_line "$ROOT/docs/guide/getting-started.md (for your own product)"

ELAPSED=$(( $(date +%s) - STARTED ))
echo "quickstart: done in ${ELAPSED}s"
