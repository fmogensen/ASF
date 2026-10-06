#!/usr/bin/env bash
# tools/install-clean.sh — install from zero, as a new user would, and require a green doctor.
#
#   bash tools/install-clean.sh [ref] [-- <asf install flags>]
#
# Run on a clean machine (CI: `install-clean-linux` in an ubuntu container, `install-clean-macos`
# with a fresh HOME): no asf, no gh login, no Claude login. From this checkout as the package
# source (ASF_REPO_URL=file://<checkout>, pinned to [ref], default HEAD) it
#
# 1. runs the installer the way a Claude Code session would — no terminal, no --yes — and
#    requires the one NEEDS OPERATOR line, and no pipx run (F-0109);
# 2. runs it the way an operator's machine with no terminal does (--yes): the package, then
#    `asf install` on a fresh sample product (a repo with a hosted-looking origin, an empty
#    record);
# 3. runs `asf init` on that product again (idempotent) and `asf doctor`, which must exit 0 with
#    no RED row — a row that needs a login reads "not configured", never red.
#
# Every path it writes is under $HOME. It never removes anything outside INSTALL_CLEAN_DIR.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="$(git -C "$SRC" rev-parse HEAD)"
if [ $# -gt 0 ] && [ "$1" != "--" ]; then REF="$1"; shift; fi
if [ $# -gt 0 ] && [ "$1" = "--" ]; then shift; fi
PRODUCT="${INSTALL_CLEAN_PRODUCT:-example}"
WORK="${INSTALL_CLEAN_DIR:-$HOME/install-clean}"
export ASF_REPO_URL="file://$SRC"

say() { printf 'install-clean: %s\n' "$*"; }
fail() { printf 'install-clean: FAIL: %s\n' "$*" >&2; exit 1; }

command -v asf >/dev/null 2>&1 && fail "asf is already on PATH ($(command -v asf)) — not a clean machine"
[ -e "${ASF_HOME:-$HOME/.ASF}" ] && fail "${ASF_HOME:-$HOME/.ASF} already exists — not a clean machine"

git config --global user.name >/dev/null 2>&1 || git config --global user.name "install-clean"
git config --global user.email >/dev/null 2>&1 || git config --global user.email "install-clean@localhost"

# the sample product: a repo whose origin reads as a hosted one (unreachable, never pushed), and
# an empty record directory for `asf init` to lay down
rm -rf "$WORK"
mkdir -p "$WORK/product" "$WORK/record"
git -C "$WORK/product" init -q -b main
printf 'a sample product\n' > "$WORK/product/README"
git -C "$WORK/product" add README
git -C "$WORK/product" commit -qm "a sample product"
git -C "$WORK/product" remote add origin https://github.com/example-org/example-product.git
git -C "$WORK/record" init -q -b main
git -C "$WORK/record" commit -q --allow-empty -m "the record"

say "1/3 the default form with no terminal (a session): the package half is never attempted"
set +e
OUT="$(bash "$SRC/tools/install.sh" "$PRODUCT" "$REF" </dev/null 2>&1)"
RC=$?
set -e
printf '%s\n' "$OUT"
[ "$RC" -ne 0 ] || fail "the no-terminal default exited 0"
grep -q 'NEEDS OPERATOR: step 1 installs the package and needs a terminal' <<<"$OUT" \
  || fail "no NEEDS OPERATOR line for the package half"
command -v asf >/dev/null 2>&1 && fail "asf was installed without a terminal or --yes"

say "2/3 the install: package half and session half (--yes: no terminal here)"
bash "$SRC/tools/install.sh" "$PRODUCT" "$REF" --yes -- \
  --repo "$WORK/product" --record "$WORK/record" "$@" </dev/null \
  || fail "install.sh exited non-zero"
export PATH="$HOME/.local/bin:$PATH"
asf --version

say "3/3 asf init again, then asf doctor"
asf init --product "$PRODUCT"
set +e
asf doctor --product "$PRODUCT" | tee "$WORK/doctor.txt"
RC=${PIPESTATUS[0]}
set -e
RED="$(grep -E '^[^ ].*  RED  ' "$WORK/doctor.txt" || true)"
SCHED_RED="$(sed -n '/^== SCHEDULER/,$p' "$WORK/doctor.txt" | grep -E '^RED' || true)"
[ -z "$RED$SCHED_RED" ] || fail "RED doctor row(s):
$RED$SCHED_RED"
[ "$RC" -eq 0 ] || fail "asf doctor exited $RC"
grep -qE '^cli:gh +(ok|skip) ' "$WORK/doctor.txt" || fail "no cli:gh row read ok or not configured"
say "green: asf doctor --product $PRODUCT exited 0 with no RED row"
