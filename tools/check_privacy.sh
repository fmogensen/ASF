#!/usr/bin/env bash
# tools/check_privacy.sh — the privacy sweep: no operator home path, e-mail address or private
# link in a tracked file (tools/check_privacy.py; exemptions in tools/privacy-allow.txt). A
# finding names the file, the line and the kind, never the matched text.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/check_privacy.py" --root "$(cd "$HERE/.." && pwd)" "$@"
