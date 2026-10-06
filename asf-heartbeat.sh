#!/bin/sh
secs=$1; ref=$2; sid=$3; last=
while :; do
  idx=$(mktemp) || exit 0
  GIT_INDEX_FILE=$idx git read-tree HEAD && GIT_INDEX_FILE=$idx git add -A
  tree=$(GIT_INDEX_FILE=$idx git write-tree)
  { echo "wip: heartbeat $(date -u +%Y-%m-%dT%H:%M:%SZ)"; echo; cat NOTES.asf.md 2>/dev/null
    echo; echo "ASF-Session: $sid"; } > "$idx.msg"
  c=$(git commit-tree "$tree" -p HEAD -F "$idx.msg")
  rm -f "$idx" "$idx.msg"
  if [ -n "$c" ]; then
    if [ -z "$last" ]; then lease=--force; else lease="--force-with-lease=$ref:$last"; fi
    if git push -q $lease origin "$c:$ref" 2>/dev/null; then last=$c
    elif seen=$(git ls-remote origin "$ref" 2>/dev/null); then
      [ "$(printf '%s' "$seen" | cut -f1)" = "$last" ] || exit 0
    fi
  fi
  sleep "$secs"
done
