#!/usr/bin/env bash
# tools/check_factory_only.sh — the CI entry point for the factory-only merge rule
# (asf.factory_only). Reads the conventions this repository committed for its own CI to see
# (.asf/product.yaml, F-0253 — a GitHub runner cannot read the operator's ~/.ASF/products/), and
# runs with no install: PYTHONPATH is pointed at this checkout so `asf` is importable straight
# from source.
#
# Usage: check_factory_only.sh [args passed through to python3 -m asf.factory_only]
#
# With no arguments at all, and only on a pull request (GITHUB_BASE_REF set), the PR's own diff
# is computed here and passed on as --files; any argument from the caller is forwarded exactly as
# given, and none is supplied in that case. Env: FACTORY_ONLY_PRODUCT_FILE overrides the product
# file path (tests use this; CI never sets it).
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
PRODUCT_FILE="${FACTORY_ONLY_PRODUCT_FILE:-$ROOT/.asf/product.yaml}"

if [ ! -f "$PRODUCT_FILE" ]; then
  echo "check_factory_only: missing $PRODUCT_FILE" >&2
  exit 2
fi

args=("$@")

if [ ${#args[@]} -eq 0 ] && [ -n "${GITHUB_BASE_REF:-}" ]; then
  git -C "$ROOT" fetch --no-tags --depth=2 origin "${GITHUB_SHA:-HEAD}" 2>/dev/null || true
  if ! diff_out="$(git -C "$ROOT" diff --name-only HEAD^1 HEAD)"; then
    echo "check_factory_only: cannot read the diff HEAD^1..HEAD" >&2
    exit 2
  fi
  args=(--files)
  while IFS= read -r f; do
    [ -n "$f" ] && args+=("$f")
  done <<< "$diff_out"
fi

PYTHONPATH="$ROOT" exec python3 -m asf.factory_only --product-file "$PRODUCT_FILE" "${args[@]+"${args[@]}"}"
