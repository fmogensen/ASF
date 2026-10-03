#!/usr/bin/env bash
# tools/hook-smoke.sh <product> [dir] — do a product's hooks run its pinned venv?
#
# Replays, through the CLI dispatcher (asf.dispatch) and from a directory of the product, the three
# calls a session's hooks make: a PreToolUse payload (`asf hook approvals`), a Stop payload
# (`asf hook unpushed`) and a pre-push ref list (`asf redact --pre-push --product <p>`, a deletion
# line: nothing to scan). Each runs with no ASF_JOB (so every hook lets it through and changes
# nothing) and no ASF_PRODUCT (so the product is resolved the hard way, from the directory, where
# the call does not name it). The dispatcher's trace names the CLI that answered; the script prints
# one line per call and exits 0 only when every call exits 0 from the product's pinned venv.
#
# Then the same pre-push once per agent home (<asf home>/state/homes/<agent>): a worker session
# runs with HOME=<that home>, and its git hooks call "$HOME/.local/bin/asf" — so the replay runs
# under that HOME through that path, and fails when the home's link is missing or dangling.
#
#   dir                the directory to run from (default: the first of the product's state
#                      worktrees, else its repo_dir)
# Env: ASF_HOME        the asf home (default ~/.ASF)
#      ASF_DISPATCHER  the dispatcher (default $HOME/.local/bin/asf)
set -uo pipefail

product="${1:-}"
[ -n "$product" ] || { echo "usage: hook-smoke.sh <product> [dir]" >&2; exit 2; }
asf_home="${ASF_HOME:-$HOME/.ASF}"
dispatcher="${ASF_DISPATCHER:-$HOME/.local/bin/asf}"
record="$asf_home/state/$product/install.json"

dir="${2:-}"
if [ -z "$dir" ]; then
  for d in "$asf_home/state/$product/worktrees"/*/; do
    [ -d "$d" ] && { dir="${d%/}"; break; }
  done
fi
if [ -z "$dir" ]; then
  dir=$(sed -n 's/^repo_dir:[[:space:]]*//p' "$asf_home/products/$product.yaml" 2>/dev/null |
        sed 's/[[:space:]]#.*//; s/^["'"'"']//; s/["'"'"'][[:space:]]*$//' | head -n 1)
  dir="${dir/#\~/$HOME}"
fi
[ -d "$dir" ] || { echo "hook-smoke: no directory for product $product (pass one)" >&2; exit 2; }
[ -x "$dispatcher" ] || { echo "hook-smoke: no dispatcher at $dispatcher" >&2; exit 2; }

venv=$(python3 - "$record" "${PIPX_HOME:-$HOME/.local/pipx}/venvs" <<'PY' 2>/dev/null
import json, os, sys
venv = json.load(open(sys.argv[1])).get('venv') or ''
print(venv if os.path.isabs(venv) else os.path.join(sys.argv[2], venv))
PY
)
[ -n "$venv" ] || { echo "hook-smoke: $product has no pin ($record)" >&2; exit 1; }
venv=$(cd "$venv" 2>/dev/null && pwd -P || echo "$venv")

zero=0000000000000000000000000000000000000000
fail=0
run_home="$HOME"; run_cli="$dispatcher"
replay() {  # <label> <stdin> <args...> — runs $run_cli under HOME=$run_home
  local label=$1 input=$2; shift 2
  local err rc cli
  if [ ! -x "$run_cli" ]; then
    echo "FAIL  $label  no asf at $run_cli"
    fail=1
    return
  fi
  err=$(cd "$dir" && printf '%s\n' "$input" |
        env -u ASF_JOB -u ASF_PRODUCT HOME="$run_home" ASF_DISPATCH_TRACE=1 ASF_HOME="$asf_home" \
          "$run_cli" "$@" 2>&1 >/dev/null)
  rc=$?
  cli=$(printf '%s\n' "$err" | sed -n 's/^asf-dispatch: .* cli=//p' | head -n 1)
  local real
  real=$(cd "$(dirname "$cli")" 2>/dev/null && pwd -P)
  if [ "$rc" = 0 ] && [ "${real#"$venv"/}" != "$real" ]; then
    echo "ok    $label  rc=$rc  $cli"
  else
    echo "FAIL  $label  rc=$rc  ${cli:-none}  (pin: $venv)"
    printf '%s\n' "$err" | sed 's/^/      /'
    fail=1
  fi
}

replay PreToolUse \
  '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"true"}}' \
  hook approvals
replay Stop '{"hook_event_name":"Stop","stop_hook_active":false}' hook unpushed
replay pre-push "refs/heads/hook-smoke $zero refs/heads/hook-smoke $zero" \
  redact --pre-push --product "$product"

for h in "$asf_home/state/homes"/*/; do
  [ -d "$h" ] || continue
  h=${h%/}
  run_home="$h"; run_cli="$h/.local/bin/asf"
  replay "pre-push@home:$(basename "$h")" \
    "refs/heads/hook-smoke $zero refs/heads/hook-smoke $zero" \
    redact --pre-push --product "$product"
done
exit $fail
