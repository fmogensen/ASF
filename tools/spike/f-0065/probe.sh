#!/usr/bin/env bash
# tools/spike/f-0065/probe.sh <role> — F-0065 Task 1 (§2.1). The instrument's reading end: one
# script that asks the probes named for <role> (coder, reviewer or prober) and prints exactly one
# line per probe:
#
#   PROBE <id> <ok|refused|error> <at most one line, no secret value>
#
# `refused` is the fence saying no; `error` is the probe itself broken (a missing binary, no
# fixture, no credential) and is never read as a finding. This script's exit status is always 0 —
# a probe that aborts the script loses every row after it, and a lost row is indistinguishable
# from a passing one in the log.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# The real product's own checkout — derived from where this script lives, never from cwd, so
# P04-P06 reach the real remote whether this runs on the host or inside a session whose cwd is
# the fixture worktree (§2.4).
REAL_REPO="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || true)"

# Same formula as fixture.sh — the two must agree, and neither reads the other's output to find it.
ASF_HOME="${ASF_HOME:-$HOME/.ASF}"
FIXTURE_DIR="${ASF_HOME}/state/spike/f-0065/fixture"

# ---- small helpers --------------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

# One line, trimmed, capped, and stripped of any string shaped like a GitHub token — belt and
# braces over what the probes already never print (§4).
oneline() {
  printf '%s' "$1" \
    | tr '\n\r\t' '   ' \
    | sed -E 's/gh[pousr]_[A-Za-z0-9_]{20,}/[redacted]/g; s/github_pat_[A-Za-z0-9_]{20,}/[redacted]/g' \
    | tr -s ' ' \
    | cut -c1-200
}

emit() {
  local id="$1" verdict="$2" msg="$3"
  msg="$(oneline "$msg")"
  printf 'PROBE %s %s %s\n' "$id" "$verdict" "${msg:-(no detail)}"
}

# Whether a failure's own text reads like the fence itself saying no, rather than the probe being
# broken — an OS permission refusal, or a network path that would not resolve or connect. This is
# a defensive fallback for the sandboxed runs later Tasks measure (P12: this build has no sandbox
# flag today, so in Task 1's unsandboxed run every probe below is expected to read `ok` or, where a
# tool or credential this host lacks is missing, `error` — never `refused`).
refusal_signature() {
  printf '%s' "$1" | grep -qiE \
    'permission denied|operation not permitted|not permitted|read-only file system|\bEPERM\b|\bEACCES\b|sandbox|could.?n.?t resolve host|connection refused|network is unreachable|ssl certificate problem|ssl connect error|name or service not known'
}

# ---- the probes (§2.1) -----------------------------------------------------------------------

probe_P00() {  # control: write outside every grant
  local f="$HOME/.f0065-control" err rc
  err=$( { : > "$f"; } 2>&1 )
  rc=$?
  if [ $rc -eq 0 ]; then
    rm -f "$f"
    emit P00 ok "wrote and removed $HOME/.f0065-control"
  elif refusal_signature "$err"; then
    emit P00 refused "$err"
  else
    emit P00 error "$err"
  fi
  return 0
}

probe_P01() {  # control: egress to a host no grant names
  if ! have curl; then
    emit P01 error "no curl binary"
    return 0
  fi
  local out rc
  out=$(curl -sS --max-time 8 -o /dev/null -w '%{http_code}' https://example.com 2>&1)
  rc=$?
  if [ $rc -eq 0 ] && [ "$out" = "200" ]; then
    emit P01 ok "TLS to example.com: HTTP $out"
  elif [ $rc -eq 0 ]; then
    emit P01 error "unexpected HTTP $out from example.com"
  elif refusal_signature "$out"; then
    emit P01 refused "curl rc=$rc: $out"
  else
    emit P01 error "curl rc=$rc: $out"
  fi
  return 0
}

probe_P02() {  # a write inside cwd: edit a tracked file in the fixture worktree
  local repo="$FIXTURE_DIR/repo" err rc
  if [ ! -d "$repo/.git" ]; then
    emit P02 error "no fixture repo at $repo (run fixture.sh)"
    return 0
  fi
  err=$( { printf 'probe P02 %s\n' "$(date +%s)" >> "$repo/README.md"; } 2>&1 )
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P02 ok "edited the fixture's tracked README.md"
  elif refusal_signature "$err"; then
    emit P02 refused "$err"
  else
    emit P02 error "$err"
  fi
  return 0
}

probe_P03() {  # a write to the gitdir and object store, plus exec of the hook dir
  local repo="$FIXTURE_DIR/repo" out rc
  if [ ! -d "$repo/.git" ]; then
    emit P03 error "no fixture repo at $repo (run fixture.sh)"
    return 0
  fi
  out=$(git -C "$repo" commit -s --allow-empty -m 'f0065 probe P03' 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P03 ok "committed $(git -C "$repo" rev-parse --short HEAD 2>/dev/null)"
  elif refusal_signature "$out"; then
    emit P03 refused "$out"
  else
    emit P03 error "$out"
  fi
  return 0
}

probe_P04() {  # DNS + TLS to the real code host, a read of <repo>/.git
  if [ -z "$REAL_REPO" ]; then
    emit P04 error "could not find the real product's checkout from $HERE"
    return 0
  fi
  local out rc
  out=$(git -C "$REAL_REPO" ls-remote --exit-code origin HEAD 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P04 ok "ls-remote reached origin"
  elif refusal_signature "$out"; then
    emit P04 refused "$out"
  else
    emit P04 error "$out"
  fi
  return 0
}

probe_P05() {  # the same, plus the credential helper's own shell exec reading $GH_TOKEN
  if [ -z "$REAL_REPO" ]; then
    emit P05 error "could not find the real product's checkout from $HERE"
    return 0
  fi
  local out rc
  out=$(git -C "$REAL_REPO" push --dry-run origin 'HEAD:refs/heads/f0065-probe-scratch' 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P05 ok "push --dry-run negotiated with origin"
  elif refusal_signature "$out"; then
    emit P05 refused "$out"
  else
    emit P05 error "$out"
  fi
  return 0
}

probe_P06() {  # TLS to the API domain, a read of the CLI's config under the session HOME
  if ! have gh; then
    emit P06 error "no gh binary"
    return 0
  fi
  local out rc
  out=$(gh api rate_limit 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P06 ok "gh api rate_limit reached"
  elif refusal_signature "$out"; then
    emit P06 refused "$out"
  else
    emit P06 error "$out"
  fi
  return 0
}

probe_P07() {  # exec of the factory CLI, a read of $ASF_HOME
  if ! have asf; then
    emit P07 error "no asf binary on PATH"
    return 0
  fi
  local out rc
  out=$(asf doctor 2>&1)
  rc=$?
  # `asf doctor` returns 1 on a red row (a health finding, not a refusal) and 0 on all-green: both
  # mean the CLI ran and read $ASF_HOME. Only a refusal-shaped failure or a crash is a probe result
  # other than ok.
  if [ $rc -eq 0 ] || [ $rc -eq 1 ]; then
    emit P07 ok "asf doctor ran (rc=$rc)"
  elif refusal_signature "$out"; then
    emit P07 refused "$out"
  else
    emit P07 error "asf doctor rc=$rc: $out"
  fi
  return 0
}

probe_P08() {  # a Bash call the approvals matrix classifies, so the hook writes its ledger row
  if ! have asf; then
    emit P08 error "no asf binary on PATH"
    return 0
  fi
  # This command is never executed — asf hook approvals only pattern-matches the string below
  # (asf/approvals.py:_pushes_trunk) to classify it as touch_production, which is not auto: the
  # hook refuses it and appends the refusal to the product's ledger (D5: no probe writes at the
  # far end — nothing here runs a real push).
  local payload out rc
  payload='{"tool_name":"Bash","hook_event_name":"PreToolUse","tool_input":{"command":"git push origin HEAD:main"}}'
  out=$(printf '%s' "$payload" | asf hook approvals 2>&1)
  rc=$?
  if printf '%s' "$out" | grep -q '^approvals hook error:'; then
    # the hook's own ledger write failed (asf/approvals.py:run_hook's except branch) — this is
    # the failure mode §2.1 calls the worst one: the fence refusing the hook's own write.
    if refusal_signature "$out"; then
      emit P08 refused "$out"
    else
      emit P08 error "$out"
    fi
  elif [ "$rc" = "2" ]; then
    emit P08 ok "the hook classified the call and wrote its ledger row (refused touch_production)"
  elif [ "$rc" = "0" ]; then
    emit P08 error "asf hook approvals classified nothing (no ASF_JOB in the environment, or the matrix let it through)"
  else
    emit P08 error "asf hook approvals rc=$rc: $out"
  fi
  return 0
}

probe_P09() {  # exec, a TMPDIR write, a git fixture with its own hooksPath
  local script="$FIXTURE_DIR/test.sh" out rc
  if [ ! -x "$script" ]; then
    emit P09 error "no fixture test command at $script (run fixture.sh)"
    return 0
  fi
  out=$(bash "$script" 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P09 ok "$out"
  elif refusal_signature "$out"; then
    emit P09 refused "$out"
  else
    emit P09 error "$out"
  fi
  return 0
}

probe_P10() {  # TLS to the package registry, a write to the package cache, a write in the worktree
  local dir="$FIXTURE_DIR/package"
  if [ ! -f "$dir/package.json" ]; then
    emit P10 error "no fixture package manifest at $dir (run fixture.sh)"
    return 0
  fi
  if ! have npm; then
    emit P10 error "no npm binary"
    return 0
  fi
  local out rc
  out=$( (cd "$dir" && npm install --no-audit --no-fund) 2>&1 )
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P10 ok "npm install reached the registry and wrote node_modules"
  elif refusal_signature "$out"; then
    emit P10 refused "$out"
  else
    emit P10 error "$out"
  fi
  return 0
}

probe_P11() {  # exec of the engine from its cache, its own child processes, its own inner sandbox
  local dir="$FIXTURE_DIR/browser"
  if [ ! -f "$dir/browser.js" ] || [ ! -d "$dir/node_modules/playwright" ]; then
    emit P11 error "no fixture browser engine at $dir (run fixture.sh)"
    return 0
  fi
  if ! have node; then
    emit P11 error "no node binary"
    return 0
  fi
  local out rc
  out=$( (cd "$dir" && node browser.js) 2>&1 )
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P11 ok "$out"
  elif refusal_signature "$out"; then
    emit P11 refused "$out"
  else
    emit P11 error "$out"
  fi
  return 0
}

probe_P12() {  # a loopback bind and a loopback connect
  local script="$FIXTURE_DIR/server/server.js"
  if [ ! -f "$script" ]; then
    emit P12 error "no fixture dev server at $script (run fixture.sh)"
    return 0
  fi
  if ! have node; then
    emit P12 error "no node binary"
    return 0
  fi
  local out pid port tries body rc
  out="$(mktemp "${TMPDIR:-/tmp}/f0065-server-out.XXXXXX")"
  node "$script" > "$out" 2>&1 &
  pid=$!
  port=""
  tries=0
  while [ $tries -lt 50 ]; do
    port=$(grep -o '^PORT [0-9]*' "$out" 2>/dev/null | awk '{print $2}')
    [ -n "$port" ] && break
    sleep 0.1
    tries=$((tries + 1))
  done
  if [ -z "$port" ]; then
    kill "$pid" >/dev/null 2>&1 || true
    emit P12 error "server never printed its port: $(cat "$out" 2>/dev/null)"
    rm -f "$out"
    return 0
  fi
  body=$(curl -sS --max-time 8 "http://127.0.0.1:${port}/f0065" 2>&1)
  rc=$?
  kill "$pid" >/dev/null 2>&1 || true
  wait "$pid" 2>/dev/null || true
  rm -f "$out"
  if [ $rc -eq 0 ] && [ "$body" = "f0065 ok" ]; then
    emit P12 ok "bound loopback :$port and fetched it"
  elif refusal_signature "$body"; then
    emit P12 refused "$body"
  else
    emit P12 error "curl rc=$rc: $body"
  fi
  return 0
}

probe_P13() {  # a read under <state>/<product>/briefs, a write at the one path the brief names
  local brief="$FIXTURE_DIR/briefs/f0065-prober.brief.md" sidefile err rc
  if [ ! -f "$brief" ]; then
    emit P13 error "no fixture brief at $brief (run fixture.sh)"
    return 0
  fi
  sidefile=$(sed -n 's/^SIDE_FILE: //p' "$brief" | head -1)
  if [ -z "$sidefile" ]; then
    emit P13 error "the fixture brief names no SIDE_FILE"
    return 0
  fi
  err=$( { mkdir -p "$(dirname "$sidefile")" && printf 'f0065 prober reading %s\n' "$(date +%s)" > "$sidefile"; } 2>&1 )
  rc=$?
  if [ $rc -eq 0 ]; then
    emit P13 ok "wrote the reading the brief named"
  elif refusal_signature "$err"; then
    emit P13 refused "$err"
  else
    emit P13 error "$err"
  fi
  return 0
}

# ---- role dispatch (§2.1's role column) ------------------------------------------------------

role_probes() {
  case "$1" in
    coder)    printf '%s\n' P00 P01 P02 P03 P04 P05 P06 P07 P08 P09 P10 P11 P12 ;;
    reviewer) printf '%s\n' P00 P01 P02 P03 P04 P05 P06 P07 P08 P09 ;;
    prober)   printf '%s\n' P00 P01 P07 P08 P13 ;;
    *) return 1 ;;
  esac
}

role="${1:-}"
if [ -z "$role" ]; then
  echo "usage: probe.sh <coder|reviewer|prober>" >&2
  exit 2
fi
ids="$(role_probes "$role")" || {
  echo "probe.sh: unknown role '$role' — want coder, reviewer or prober" >&2
  exit 2
}

while IFS= read -r id; do
  [ -n "$id" ] || continue
  if ! "probe_$id"; then
    # A probe function that exits non-zero without having emitted its own line is the one thing
    # this script must never let through silently: it still gets a row, so no row is ever lost.
    emit "$id" error "the probe function itself failed unexpectedly"
  fi
done <<< "$ids"

exit 0
