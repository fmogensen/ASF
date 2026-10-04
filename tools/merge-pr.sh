#!/usr/bin/env bash
# tools/merge-pr.sh <pr> — THE way to merge an agent PR into a repo with no merge queue.
#
# A bare `gh pr merge --match-head-commit` only proves the PR head did not change; it says nothing
# about main having moved, so two PRs, each green on its own head, can break main together. This
# script merges only a head that (a) contains origin/main right now and (b) has the required
# checks green on that exact sha:
#   1. fetch; if the head lacks origin/main, `gh pr update-branch --rebase` (a merge of main if the
#      rebase is refused), then wait for the checks on the NEW head, polling with backoff
#   2. `gh pr merge --squash --delete-branch --match-head-commit <sha>`; if main moved in between,
#      go round again (at most MERGE_PR_ATTEMPTS, default 3, then exit non-zero)
#   0. before step 1, take a host-wide lock (mkdir lock dir with a pid file, stock macOS has no
#      flock): update -> wait -> merge is one critical section, so ten agents no longer rebase on
#      each other's merges and burn 2 CI jobs a round. A waiting PR neither rebases nor runs CI.
#   3. print the merged sha and what the caller does next
#
# Env: MERGE_PR_CHECKS     required check names, ';'-separated (default "tests (3.12);tests (3.13)")
#      MERGE_PR_ATTEMPTS   rounds before giving up (default 3)
#      MERGE_PR_POLL       first poll delay in seconds (default 10, doubles up to 120)
#      MERGE_PR_TIMEOUT    seconds to wait for checks per round (default 3600)
#      MERGE_PR_LOCK       0 disables the host-wide lock (tests)
#      MERGE_PR_LOCK_POLL  seconds between lock polls (default 15)
set -euo pipefail

pr="${1:-}"
case "$pr" in ''|*[!0-9]*) echo "usage: merge-pr.sh <pr-number>" >&2; exit 2 ;; esac

IFS=';' read -r -a required <<< "${MERGE_PR_CHECKS:-tests (3.12);tests (3.13)}"
attempts="${MERGE_PR_ATTEMPTS:-3}"
poll0="${MERGE_PR_POLL:-10}"
timeout_s="${MERGE_PR_TIMEOUT:-3600}"

head_sha() { gh pr view "$pr" --json headRefOid --jq .headRefOid; }

# prints green | red | pending for the required checks on <sha>
checks_state() {
  local sha="$1" rows name line status concl state="green"
  rows="$(gh api "repos/{owner}/{repo}/commits/$sha/check-runs" --paginate \
    --jq '.check_runs[] | [.name, .status, (.conclusion // "")] | @tsv')"
  for name in "${required[@]}"; do
    # the latest row wins when a check was re-run
    line="$(printf '%s\n' "$rows" | awk -F'\t' -v n="$name" '$1==n{l=$0} END{print l}')"
    if [ -z "$line" ]; then state="pending"; continue; fi
    status="$(printf '%s' "$line" | cut -f2)"; concl="$(printf '%s' "$line" | cut -f3)"
    if [ "$status" != "completed" ]; then
      [ "$state" = "red" ] || state="pending"
    elif [ "$concl" != "success" ]; then
      state="red"
    fi
  done
  echo "$state"
}

wait_green() {
  local sha="$1" delay="$poll0" waited=0 st
  while :; do
    st="$(checks_state "$sha")"
    case "$st" in
      green) return 0 ;;
      red) echo "merge-pr: required checks are red on ${sha:0:12}; refusing to merge" >&2; return 1 ;;
    esac
    if [ "$waited" -ge "$timeout_s" ]; then
      echo "merge-pr: checks still pending on ${sha:0:12} after ${timeout_s}s" >&2; return 1
    fi
    echo "merge-pr: waiting for ${required[*]} on ${sha:0:12} (${delay}s)"
    sleep "$delay"; waited=$((waited + delay))
    delay=$((delay * 2)); [ "$delay" -le 120 ] || delay=120
  done
}

contains_main() { git merge-base --is-ancestor origin/main "$1"; }

fetch() { git fetch -q origin main "refs/pull/$pr/head"; }

# ---- host-wide lock: mkdir is atomic everywhere. Stale = holder pid dead, or older than
# MERGE_PR_TIMEOUT + 600 s (a holder cannot legitimately outlive one round's wait).
lock="${XDG_CACHE_HOME:-$HOME/.cache}/asf/merge-pr.lock"
lock_poll="${MERGE_PR_LOCK_POLL:-15}"
have_lock=0

lock_stale() {
  local hp since now
  hp="$(cat "$lock/pid" 2>/dev/null || true)"
  if [ -z "$hp" ]; then
    # holder between mkdir and writing its pid, or died there: give it a minute
    [ -n "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; return
  fi
  kill -0 "$hp" 2>/dev/null || return 0
  # a live holder with no readable start time is never stale (a missing `since` once read as 0,
  # i.e. "ancient", and broke a lock its holder was still filling in)
  since="$(cat "$lock/since" 2>/dev/null || true)"
  case "$since" in ''|*[!0-9]*) return 1 ;; esac
  now="$(date +%s)"
  [ $((now - since)) -gt $((timeout_s + 600)) ]
}

lock_break() {
  local junk="$lock.stale.$$"
  mv "$lock" "$junk" 2>/dev/null || return 0
  rm -rf "$junk"
}

lock_release() {
  if [ "$have_lock" = 1 ] && [ "$(cat "$lock/pid" 2>/dev/null)" = "$$" ]; then
    rm -rf "$lock"
  fi
  have_lock=0
}

lock_acquire() {
  local last=0 now
  mkdir -p "$(dirname "$lock")"
  while :; do
    if mkdir "$lock" 2>/dev/null; then
      # pid last and by rename: once a waiter can read a pid, `since` and `pr` are already whole
      have_lock=1
      date +%s > "$lock/since"; echo "$pr" > "$lock/pr"
      echo "$$" > "$lock/pid.$$" && mv "$lock/pid.$$" "$lock/pid"
      return 0
    fi
    if lock_stale; then
      echo "merge-pr: breaking a stale merge lock (held by PR #$(cat "$lock/pr" 2>/dev/null || echo ?), pid $(cat "$lock/pid" 2>/dev/null || echo ?))"
      lock_break; continue
    fi
    now="$(date +%s)"
    if [ $((now - last)) -ge 60 ]; then
      echo "merge-pr: waiting for the merge lock (held by PR #$(cat "$lock/pr" 2>/dev/null || echo ?), pid $(cat "$lock/pid" 2>/dev/null || echo ?))"
      last="$now"
    fi
    sleep "$lock_poll"
  done
}

if [ "${MERGE_PR_LOCK:-1}" != 0 ]; then
  trap lock_release EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  lock_acquire
fi

for round in $(seq 1 "$attempts"); do
  fetch
  sha="$(head_sha)"
  if ! contains_main "$sha"; then
    echo "merge-pr: round $round: ${sha:0:12} is behind origin/main; updating the branch"
    gh pr update-branch "$pr" --rebase || gh pr update-branch "$pr"
    # the branch moves on the server a moment after the call: wait for a new head
    for _ in $(seq 1 30); do
      new="$(head_sha)"; [ "$new" != "$sha" ] && break; sleep "$poll0"
    done
    fetch
    sha="$(head_sha)"
  fi
  wait_green "$sha" || exit 1
  fetch
  if [ "$(head_sha)" != "$sha" ] || ! contains_main "$sha"; then
    echo "merge-pr: round $round: main or the head moved while waiting; again"
    continue
  fi
  if gh pr merge "$pr" --squash --delete-branch --match-head-commit "$sha"; then
    merged="$(gh pr view "$pr" --json mergeCommit --jq .mergeCommit.oid)"
    echo "merge-pr: merged PR #$pr as $merged"
    echo "next: git pull --ff-only (main checkout), then asf upgrade --wait"
    exit 0
  fi
  echo "merge-pr: round $round: merge refused (head or main moved); again"
done
echo "merge-pr: gave up after $attempts rounds: main kept moving under PR #$pr" >&2
exit 1
