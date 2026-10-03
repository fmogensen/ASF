# tools/ab-lib.sh — sourced by tools/ab-dry-run.sh and tools/ab-ci-queue.sh: names a venv.
#
# ab_venv <product> <arg> prints the venv dir <arg> names:
#   pin            the product's pin (<asf home>/state/<product>/install.json's venv)
#   <dir>          a venv directory
#   <name>         a venv under pipx's venvs dir ($PIPX_HOME/venvs, else pipx's own answer)
#   <sha>          the product's venv at that sha (asf-factory-<product>-<sha7>)
ab_venvs_root() {
  if [ -n "${PIPX_HOME:-}" ]; then echo "$PIPX_HOME/venvs"; return; fi
  pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "$HOME/.local/pipx/venvs"
}

ab_venv() {  # <product> <arg>
  local product=$1 arg=$2 root v
  root=$(ab_venvs_root)
  if [ "$arg" = pin ]; then
    v=$(python3 - "${ASF_HOME:-$HOME/.ASF}/state/$product/install.json" <<'PY' 2>/dev/null
import json, sys
print(json.load(open(sys.argv[1])).get('venv') or '')
PY
)
    [ -n "$v" ] || { echo "ab: $product has no pin" >&2; return 1; }
    case "$v" in /*) ;; *) v="$root/$v" ;; esac
  elif [ -d "$arg" ]; then v=$arg
  elif [ -d "$root/$arg" ]; then v="$root/$arg"
  elif [ -d "$root/asf-factory-$product-${arg:0:7}" ]; then v="$root/asf-factory-$product-${arg:0:7}"
  else echo "ab: no venv for '$arg'" >&2; return 1
  fi
  [ -x "$v/bin/python" ] || { echo "ab: $v has no bin/python" >&2; return 1; }
  (cd "$v" && pwd -P)
}
