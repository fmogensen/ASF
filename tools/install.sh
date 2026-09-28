#!/usr/bin/env bash
# tools/install.sh — one product's ASF install: pins a release with pipx, then hands off to
# `asf install` for everything else.
#
#   bash tools/install.sh <product> [ref] [-- <asf install flags>]
#   curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> [ref] [-- <asf install flags>]
#
# ref: a branch head or a sha (ASF_REF, or a second argument); with neither given, the newest
# `v<major>.<minor>.<patch>` tag on the remote (`git ls-remote --tags`) is used, and the tag
# chosen is printed — never a silent fall back to a branch. A dev install
# (`pipx install -e <checkout>`) of the same package is replaced: one `asf` per machine. The
# factory's own clocks run a checkout directly, so the command on PATH is the pinned release.
#
# 1. waits (bounded) for a running tick's lock, then installs the factory as `asf` with pipx,
#    pinned to <ref> (reinstalls when the ref moves), and appends one line to
#    <ASF_HOME>/logs/install.log
# 2. hands off: once `asf` is on PATH, this script `exec`s `asf install --product <product>` with
#    whatever flags followed `--`, so the config, the record, the account, the hooks, the clocks,
#    the plugin and the doctor each run once, in Python, with one exit status — the one this
#    script carries back
set -euo pipefail

# The whole script is one compound command, read in full before its first command runs: bash
# otherwise reads a script as it goes, and this one waits up to ten minutes on a tick's lock
# while it is run from a checkout other sessions fast-forward — the rewrite resumed it at a stale
# byte offset mid-line (`line 84: the: command not found`) and the steps after it failed.
{

REPO_URL="${ASF_REPO_URL:-https://github.com/fmogensen/ASF.git}"
PRODUCT="${1:?usage: install.sh <product> [ref] [-- <asf install flags>]}"
shift
REF="${ASF_REF:-}"
if [ $# -gt 0 ] && [ "$1" != "--" ]; then
  REF="$1"
  shift
fi
if [ $# -gt 0 ] && [ "$1" = "--" ]; then
  shift
fi
ASF_HOME="${ASF_HOME:-$HOME/.ASF}"
BIN="asf"

say() { printf 'install: %s\n' "$*"; }
die() { printf 'install: NEEDS OPERATOR: %s\n' "$*" >&2; exit 2; }

command -v pipx >/dev/null 2>&1 || die "pipx is not installed — brew install pipx (or python3 -m pip install --user pipx)"
command -v git >/dev/null 2>&1 || die "git is not installed"

if [ -z "$REF" ]; then
  TAG_REFS="$(git ls-remote --tags --refs "$REPO_URL" 'v*')"
  REF="$(python3 - "$TAG_REFS" <<'RESOLVE_TAG'
import re, sys
pattern = re.compile(r"v\d+\.\d+\.\d+")
names = []
for line in sys.argv[1].splitlines():
    line = line.strip()
    if not line:
        continue
    name = line.rsplit(None, 1)[-1].rsplit("refs/tags/", 1)[-1]
    if pattern.fullmatch(name):
        names.append(name)
if names:
    print(max(names, key=lambda n: tuple(int(g) for g in re.findall(r"\d+", n))))
RESOLVE_TAG
)"
  [ -n "$REF" ] || die "no v<major>.<minor>.<patch> tag found on $REPO_URL — pass one: install.sh $PRODUCT <ref>"
  say "product $PRODUCT, release $REF from $REPO_URL"
else
  say "product $PRODUCT, ref ${REF:0:12} from $REPO_URL"
fi

# 1. the same lock a clock with an asf step holds for its whole run (asf.tick.tick.lock_path,
# asf.tick.tick.Locks.held — a command-only clock takes it only briefly, around each record
# part, and is not the case this fix protects), held across the `pipx install --force` call
# itself, not just checked beforehand: `pipx install --force` is
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

say "/asf:* resolves the product from the working directory — a session started in"
say "$PRODUCT's repo or its record needs no ASF_PRODUCT."
say "set ASF_PRODUCT=$PRODUCT only for a session that runs outside both."

# 2. everything else — the config, the record, the account, the hooks, the clocks, the plugin,
# the doctor — is `asf install`'s own; its exit status is this script's
exec "$BIN" install --product "$PRODUCT" "$@"
}
