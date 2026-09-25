#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export ASF_GATE_MODULES="$(cat .b0119_modules.txt)"
python3 tools/run_tests.py > .b0119_test_output.log 2>&1
