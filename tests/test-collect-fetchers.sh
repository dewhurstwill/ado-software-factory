#!/usr/bin/env bash
# Runs the super-collect fetcher unit tests (Sentry, PostHog, PRs). Stubbed HTTP, no network.
#   bash tests/test-collect-fetchers.sh
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/sbtest.sh"  # compat shims, pinned git settings, fake_tool
exec python3 "$(cd "$(dirname "$0")" && pwd)/test_collect_fetchers.py"
