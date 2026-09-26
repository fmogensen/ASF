#!/usr/bin/env bash
# tools/install.sh — one product's ASF install from a pinned git ref, idempotent.
#
#   bash tools/install.sh <product> [ref]          # ref: a sha or tag; default: origin main's head
#   curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> [ref]
#
# A dev install (`pipx install -e <checkout>`) of the same package is replaced: one `asf` per machine.
# The factory's own clocks run a checkout directly, so the command on PATH is the pinned release.
#
# 1. waits (bounded) for a running tick's lock, then installs the factory as `asf` with pipx,
#    pinned to <ref> (reinstalls when the ref moves)
# 2. checks ~/.ASF/config.yaml and ~/.ASF/products/<product>.yaml exist (the operator's config)
# 3. installs the redaction hooks in the product's repos
# 4. installs the product's clocks (the scheduler runs the pinned install, not a checkout)
# 5. runs the doctor, and prints the two Claude Code lines that add the /asf:* plugin
# 6. offers the console's own allow list (B-0131) — shown in full, never written here: the
#    operator runs the install command themselves when ready
#
# Steps 1-2 abort at once (nothing after them can work). Steps 3, 4 and 6 never abort: a failure
# is recorded, the rest still runs (the doctor included), each failed step gets a summary line,
# and the exit status is non-zero when any step or the doctor failed.
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

# 1. the same lock a running tick holds for its whole run (asf.tick.tick.lock_path), held across
# the `pipx install --force` call itself, not just checked beforehand: `pipx install --force` is
# not atomic, and replacing the package under a running tick — or one that starts while the swap
# is in flight — tears it, half its modules old, half new (B-0135, an ImportError mid-groom).
# Bounded: a stuck tick asks the operator rather than hanging the install forever.
INSTALL_LOCK_WAIT_S="${ASF_INSTALL_LOCK_WAIT_S:-600}"
INSTALL_LOCK_POLL_S="${ASF_INSTALL_LOCK_POLL_S:-10}"
TICK_LOCK="$ASF_HOME/state/$PRODUCT/tick.lock"
mkdir -p "$(dirname "$TICK_LOCK")"
rc=0
python3 - "$TICK_LOCK" "$INSTALL_LOCK_WAIT_S" "$INSTALL_LOCK_POLL_S" "$REPO_URL" "$REF" <<'PY' || rc=$?
import fcntl, subprocess, sys, time
path, wait_s, poll_s, repo_url, ref = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4], sys.argv[5]
deadline = time.monotonic() + wait_s
f = open(path, 'a')
waited = False
while True:
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if not waited:
            print('install: a running tick holds the lock — waiting', file=sys.stderr)
            waited = True
        if time.monotonic() >= deadline:
            sys.exit(99)  # distinct from pipx's own exit code: the wait timed out, not the install
        time.sleep(poll_s)
# the lock is held from here through the install itself: a tick that starts now, or is already
# running, blocks on this same lock until the swap below is done and it is released
try:
    subprocess.run(['pipx', 'install', '--force', f'git+{repo_url}@{ref}'],
                    check=True, stdout=subprocess.DEVNULL)
except subprocess.CalledProcessError as e:
    sys.exit(e.returncode or 1)
finally:
    fcntl.flock(f, fcntl.LOCK_UN)
PY
if [ "$rc" -eq 99 ]; then
  die "a running tick of $PRODUCT still held its lock after ${INSTALL_LOCK_WAIT_S}s — wait for it to finish, then rerun"
elif [ "$rc" -ne 0 ]; then
  die "pipx install --force git+${REPO_URL}@${REF} failed (exit $rc) while holding $PRODUCT's tick lock"
fi
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
step "step 4: $BIN scheduler install --product $PRODUCT" "$BIN" scheduler install --product "$PRODUCT"

# 5. verify — runs whatever steps 3 and 4 did
step "step 5: $BIN doctor --product $PRODUCT" env ASF_TABLES=md "$BIN" doctor --product "$PRODUCT"

# 6. offer the console's own allow list — the operator confirms by running the install command
#    themselves; nothing is written by this script
echo
step "step 6: $BIN console-permissions offer --product $PRODUCT" \
  "$BIN" console-permissions offer --product "$PRODUCT"
say "run one to write it: $BIN console-permissions install --product $PRODUCT --scope user|repo"
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
