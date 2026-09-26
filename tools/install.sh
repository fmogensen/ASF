#!/usr/bin/env bash
# tools/install.sh — one product's ASF install from a pinned git ref, idempotent.
#
#   bash tools/install.sh <product> [ref]          # ref: a sha or tag; default: origin main's head
#   curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> [ref]
#
# A dev install (`pipx install -e <checkout>`) of the same package is replaced: one `asf` per machine.
# The factory's own clocks run a checkout directly, so the command on PATH is the pinned release.
#
# 1. installs the factory as `asf` with pipx, pinned to <ref> (reinstalls when the ref moves)
# 2. checks ~/.ASF/config.yaml and ~/.ASF/products/<product>.yaml exist (the operator's config)
# 3. installs the redaction hooks in the product's repos
# 4. installs the product's clocks (the scheduler runs the pinned install, not a checkout), then
#    reads every declared clock back: a bootstrap can fail silently and leave one merely absent
#    from `launchctl list` (B-0136), so a clock still not loaded gets one retried bootstrap before
#    the step fails, naming the label
# 5. runs the doctor, and prints the two Claude Code lines that add the /asf:* plugin
#
# Steps 1-2 abort at once (nothing after them can work). Steps 3 and 4 never abort: a failure is
# recorded, the rest still runs (the doctor included), each failed step gets a summary line, and
# the exit status is non-zero when any step or the doctor failed.
set -euo pipefail

REPO_URL="${ASF_REPO_URL:-https://github.com/fmogensen/ASF.git}"
PRODUCT="${1:?usage: install.sh <product> [ref]}"
REF="${2:-}"
ASF_HOME="${ASF_HOME:-$HOME/.ASF}"
BIN="asf"

say() { printf 'install: %s\n' "$*"; }
die() { printf 'install: NEEDS OPERATOR: %s\n' "$*" >&2; exit 2; }

command -v pipx >/dev/null 2>&1 || die "pipx is not installed — brew install pipx (or python3 -m pip install --user pipx)"
command -v git >/dev/null 2>&1 || die "git is not installed"

if [ -z "$REF" ]; then
  REF="$(git ls-remote "$REPO_URL" refs/heads/main | cut -f1)"
  [ -n "$REF" ] || die "cannot read main's head from $REPO_URL"
fi
say "product $PRODUCT, ref ${REF:0:12} from $REPO_URL"

# 1. the pinned install — pipx keeps it in its own venv; --force moves it to the new ref
pipx install --force "git+${REPO_URL}@${REF}" >/dev/null
# every run of this script is an install by hand (the tick's auto-upgrade runs pipx itself):
# `asf release-readiness` counts these lines against the factory's stability
mkdir -p "$ASF_HOME/logs" 2>/dev/null && \
  printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PRODUCT" "$REF" >> "$ASF_HOME/logs/install.log" || true
export PATH="$HOME/.local/bin:$PATH"
command -v "$BIN" >/dev/null 2>&1 || die "$BIN is not on PATH after pipx install — run: pipx ensurepath"
say "$("$BIN" --version)"

# 2. the operator's config — never written here (it names the operator's repos and accounts)
[ -f "$ASF_HOME/config.yaml" ] || die "$ASF_HOME/config.yaml is missing — start from docs/config.example.yaml"
[ -f "$ASF_HOME/products/$PRODUCT.yaml" ] || die "$ASF_HOME/products/$PRODUCT.yaml is missing — start from docs/products.example.yaml, then: $BIN init --product $PRODUCT"

# 3. redaction hooks, 4. the product's clocks — both idempotent; a failure is recorded, not fatal
FAILED=()
step() {  # step <label> <command...>: run it; on failure record "<label> (exit N)" and go on
  local label="$1"; shift
  local src=0
  "$@" || src=$?
  [ "$src" -eq 0 ] || FAILED+=("$label (exit $src)")
}
step "step 3: $BIN hooks install --product $PRODUCT" "$BIN" hooks install --product "$PRODUCT"

scheduler_install_verified() {  # install, then read every declared clock back; retry once
  "$BIN" scheduler install --product "$PRODUCT"
  local rc=$?
  [ "$rc" -eq 0 ] || return "$rc"

  local out
  out="$("$BIN" scheduler status --product "$PRODUCT" 2>&1)"
  rc=$?
  [ "$rc" -eq 0 ] && return 0

  say "clock check: $(printf '%s\n' "$out" | grep 'not loaded' | tr '\n' ' ')— retrying the bootstrap once"
  "$BIN" scheduler install --product "$PRODUCT" >/dev/null 2>&1

  out="$("$BIN" scheduler status --product "$PRODUCT" 2>&1)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    local missing
    missing="$(printf '%s\n' "$out" | grep 'not loaded' | awk '{print $1}' | paste -sd, -)"
    printf 'install: NEEDS OPERATOR: clock(s) still not loaded after retrying the bootstrap: %s\n' \
      "${missing:-see step 4 output above}" >&2
  fi
  return "$rc"
}
step "step 4: $BIN scheduler install --product $PRODUCT" scheduler_install_verified

# 5. verify — runs whatever steps 3 and 4 did
step "step 5: $BIN doctor --product $PRODUCT" env ASF_TABLES=md "$BIN" doctor --product "$PRODUCT"
cat <<EOF

install: in the Claude Code session for $PRODUCT, add the plugin once:
  /plugin marketplace add fmogensen/ASF
  /plugin install asf@asf
install: and start that session with ASF_PRODUCT=$PRODUCT so /asf:* reads this product.
EOF
if [ "${#FAILED[@]}" -eq 0 ]; then
  say "done"
  exit 0
fi
for f in "${FAILED[@]}"; do
  printf 'install: FAILED %s\n' "$f" >&2
done
printf 'install: NEEDS OPERATOR: %d step(s) failed, the install is incomplete; fix them and re-run\n' "${#FAILED[@]}" >&2
exit 1
