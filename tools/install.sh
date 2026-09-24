#!/usr/bin/env bash
# tools/install.sh — one product's ASF install from a pinned git ref, idempotent.
#
#   bash tools/install.sh <product> [ref]          # ref: a sha or tag; default: origin main's head
#   curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> [ref]
#
# ASF_SUFFIX=-live: only on a machine that also holds a dev install (`pipx install -e <checkout>` is
# `asf`); the pinned install then lives beside it as `asf-live`. Everyone else gets plain `asf`.
#
# 1. installs the factory as `asf` with pipx, pinned to <ref> (reinstalls when the ref moves)
# 2. checks ~/.ASF/config.yaml and ~/.ASF/products/<product>.yaml exist (the operator's config)
# 3. installs the redaction hooks in the product's repos
# 4. installs the product's clocks (the scheduler runs the pinned asf-live, not a checkout)
# 5. runs the doctor, and prints the two Claude Code lines that add the /asf:* plugin
set -euo pipefail

REPO_URL="${ASF_REPO_URL:-https://github.com/fmogensen/ASF.git}"
PRODUCT="${1:?usage: install.sh <product> [ref]}"
REF="${2:-}"
ASF_HOME="${ASF_HOME:-$HOME/.ASF}"
SUFFIX="${ASF_SUFFIX:-}"
BIN="asf${SUFFIX}"

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
pipx install --force ${SUFFIX:+--suffix="$SUFFIX"} "git+${REPO_URL}@${REF}" >/dev/null
export PATH="$HOME/.local/bin:$PATH"
command -v "$BIN" >/dev/null 2>&1 || die "$BIN is not on PATH after pipx install — run: pipx ensurepath"
say "$("$BIN" --version)"

# 2. the operator's config — never written here (it names the operator's repos and accounts)
[ -f "$ASF_HOME/config.yaml" ] || die "$ASF_HOME/config.yaml is missing — start from docs/config.example.yaml"
[ -f "$ASF_HOME/products/$PRODUCT.yaml" ] || die "$ASF_HOME/products/$PRODUCT.yaml is missing — start from docs/products.example.yaml, then: $BIN init --product $PRODUCT"

# 3. redaction hooks, 4. the product's clocks — both idempotent
"$BIN" hooks install --product "$PRODUCT"
"$BIN" scheduler install --product "$PRODUCT"

# 5. verify
set +e
ASF_TABLES=md "$BIN" doctor --product "$PRODUCT"
rc=$?
set -e
cat <<EOF

install: in the Claude Code session for $PRODUCT, add the plugin once:
  /plugin marketplace add fmogensen/ASF
  /plugin install asf@asf
install: and start that session with ASF_PRODUCT=$PRODUCT so /asf:* reads this product.
EOF
exit $rc
