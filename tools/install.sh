#!/usr/bin/env bash
# tools/install.sh — one product's ASF install: pins a release with pipx, then hands off to
# `asf install` for everything else.
#
#   bash tools/install.sh <product> [ref] [--package-only|--no-package] [--yes] [-- <asf install flags>]
#   curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- <product> [ref] [flags] [-- <asf install flags>]
#
# ref: a branch head or a sha (ASF_REF, or a second argument); with neither given, the newest
# `v<major>.<minor>.<patch>` tag on the remote (`git ls-remote --tags`) is used, and the tag
# chosen is printed — never a silent fall back to a branch. A dev install
# (`pipx install -e <checkout>`) of the same package is replaced: one `asf` per machine. The
# factory's own clocks run a checkout directly, so the command on PATH is the pinned release.
#
# Two halves, two owners (F-0109):
#
# 1. the package half — the OPERATOR's, at a terminal: waits (bounded) for a running tick's
#    lock, then installs the factory as `asf` with pipx, pinned to <ref> (reinstalls when the ref
#    moves), and appends one line to <ASF_HOME>/logs/install.log. A Claude Code session in auto
#    mode is refused a network package install by its own runtime, and cannot grant itself the
#    rule that would allow it — so this half needs a person. `--package-only` runs just this.
# 2. the session half — anyone's, a session included: once `asf` is on PATH, this script hands
#    off to `asf install --product <product>` with whatever flags followed `--`, so the config,
#    the record, the account, the hooks, the clocks, the plugin and the doctor each run once, in
#    Python, with one exit status — the one this script carries back. `--no-package` runs just
#    this (no pipx, no network, no lock wait); with a [ref] it first checks that the `asf` on
#    PATH is that ref.
#
# Neither flag: both halves. With no terminal and no `--yes` (ASF_INSTALL_YES=1), the package
# half is never attempted silently: the one operator command is printed (NEEDS OPERATOR), the
# session half runs when `asf` is already there, and the exit is non-zero. `--yes` is the
# standing answer for a machine with no terminal (CI): it runs the package half and answers
# `asf install`'s hooks question yes. It is never the default.
set -euo pipefail

# The whole script is one compound command, read in full before its first command runs: bash
# otherwise reads a script as it goes, and this one waits up to ten minutes on a tick's lock
# while it is run from a checkout other sessions fast-forward — the rewrite resumed it at a stale
# byte offset mid-line (`line 84: the: command not found`) and the steps after it failed.
{

REPO_URL="${ASF_REPO_URL:-https://github.com/fmogensen/ASF.git}"
USAGE='usage: install.sh <product> [ref] [--package-only|--no-package] [--yes] [-- <asf install flags>]'
PRODUCT=""
REF="${ASF_REF:-}"
REF_ARG=""
MODE=both
YES="${ASF_INSTALL_YES:-}"
PASS=()
usage() { printf '%s\n' "$USAGE" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --) shift; PASS=("$@"); break ;;
    --package-only) [ "$MODE" = no-package ] && usage; MODE=package-only ;;
    --no-package) [ "$MODE" = package-only ] && usage; MODE=no-package ;;
    --yes) YES=1 ;;
    -h|--help) printf '%s\n' "$USAGE"; exit 0 ;;
    -*) usage ;;
    *)
      if [ -z "$PRODUCT" ]; then PRODUCT="$1"
      elif [ -z "$REF_ARG" ]; then REF_ARG="$1"; REF="$1"
      else usage
      fi ;;
  esac
  shift
done
[ -n "$PRODUCT" ] || usage
ASF_HOME="${ASF_HOME:-$HOME/.ASF}"
BIN="asf"

say() { printf 'install: %s\n' "$*"; }
die() { printf 'install: NEEDS OPERATOR: %s\n' "$*" >&2; exit 2; }

# the two halves as commands, spelled for whoever must run them
REF_WORD="${REF:+ $REF}"
OPERATOR_CMD="bash tools/install.sh $PRODUCT$REF_WORD --package-only"
OPERATOR_CURL="curl -fsSL https://raw.githubusercontent.com/fmogensen/ASF/main/tools/install.sh | bash -s -- $PRODUCT$REF_WORD --package-only"
SESSION_CMD="bash tools/install.sh $PRODUCT$REF_WORD --no-package"

# a controlling terminal — what a person at a keyboard has and a session's tool call does not
has_tty() { ( exec </dev/tty ) 2>/dev/null; }

OUTSTANDING=""
if [ -z "$YES" ] && ! has_tty; then
  if [ "$MODE" = package-only ]; then
    die "step 1 installs the package and needs a terminal — the operator runs: $OPERATOR_CMD (or: $OPERATOR_CURL)"
  fi
  if [ "$MODE" = both ]; then
    say "NEEDS OPERATOR: step 1 installs the package and needs a terminal — run:"
    say "  $OPERATOR_CMD"
    say "then this session can finish with: $SESSION_CMD"
    MODE=no-package
    OUTSTANDING=1
  fi
fi

command -v git >/dev/null 2>&1 || die "git is not installed"

if [ "$MODE" != no-package ]; then
command -v pipx >/dev/null 2>&1 || die "pipx is not installed — brew install pipx (or python3 -m pip install --user pipx)"

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

if [ "$MODE" = package-only ]; then
  say "package half done: $("$BIN" --version 2>/dev/null || echo "$BIN")"
  say "the rest can run from the product's own Claude Code session: bash tools/install.sh $PRODUCT $REF --no-package"
  exit 0
fi
fi  # the package half

# the session half: `asf` must already be there, and be the ref asked for
export PATH="$HOME/.local/bin:$PATH"
if [ "$MODE" = no-package ]; then
  command -v "$BIN" >/dev/null 2>&1 || die "asf is not installed — the operator runs: $OPERATOR_CMD"
  if [ -n "$REF" ]; then
    VERSION_LINE="$("$BIN" --version 2>/dev/null || true)"
    python3 - "$REF" "$VERSION_LINE" <<'REF_CHECK' || die "asf is ${VERSION_LINE:-unknown}, not $REF — the operator runs: $OPERATOR_CMD"
import re, sys
ref, line = sys.argv[1].strip(), sys.argv[2].strip()
m = re.match(r"v?(\S+)(?:\s+\((\w+)\))?", line)
if not m:
    sys.exit(1)
release, commit = m.group(1), (m.group(2) or "")
if ref.lstrip("v") == release.split("+")[0] and "+" not in release:
    sys.exit(0)
n = min(len(ref), len(commit))
sys.exit(0 if commit and n >= 7 and ref[:n].lower() == commit[:n].lower() else 1)
REF_CHECK
  fi
fi

if [ -n "$YES" ]; then
  PASS+=(--yes)
fi

# 2. everything else — the config, the record, the account, the hooks, the clocks, the plugin,
# the doctor — is `asf install`'s own; its exit status is this script's
if [ -z "$OUTSTANDING" ]; then
  exec "$BIN" install --product "$PRODUCT" ${PASS[@]+"${PASS[@]}"}
fi
rc=0
"$BIN" install --product "$PRODUCT" ${PASS[@]+"${PASS[@]}"} || rc=$?
say "OUTSTANDING: step 1 (the package) was not run here — the operator runs: $OPERATOR_CMD"
exit $(( rc ? rc : 2 ))
}
