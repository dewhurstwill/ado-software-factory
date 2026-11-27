#!/usr/bin/env bash
# Runs the super-collect custom-source tests (onboard's "➕ Add another source"). No network.
#   bash tests/test-collect-custom.sh
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/sbtest.sh"  # compat shims, pinned git settings, fake_tool
exec python3 "$(cd "$(dirname "$0")" && pwd)/test_collect_custom.py"
