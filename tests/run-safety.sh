#!/usr/bin/env bash
# Offline release checks. Keep test-* / test_* suites offline; paid evals live in evals/.
# Run every suite even after a failure, then fail the whole gate if any suite failed.
# Each suite gets SB_SUITE_TIMEOUT seconds (default 300); a suite still running then is
# killed with its child processes and counted as failed (exit 124), and the run goes on.
set -uo pipefail
cd "$(dirname "$0")/.."
# Windows (Git Bash): jq without CRLF and a working python3, exported to every suite.
# shellcheck source=../scripts/super-board-compat.sh
[ ! -f scripts/super-board-compat.sh ] || . scripts/super-board-compat.sh

for tool in bash python3 git jq node curl tar; do
  command -v "$tool" >/dev/null || { echo "Missing test dependency: $tool" >&2; exit 1; }
done

SUITE_LIMIT="${SB_SUITE_TIMEOUT:-300}"
case "$SUITE_LIMIT" in ''|*[!0-9]*) echo "SB_SUITE_TIMEOUT must be whole seconds" >&2; exit 64 ;; esac

# Stop a suite and everything it started. Suites run in their own process group
# (set -m below); Windows-native children (python.exe, node.exe) are outside MSYS
# process groups, so on Windows the process tree is ended with taskkill as well.
kill_tree() {
  local pid="$1" winpid=""
  if [ -n "${SB_WINDOWS:-}" ] && [ -r "/proc/$pid/winpid" ]; then
    winpid=$(cat "/proc/$pid/winpid" 2>/dev/null || true)
    [ -z "$winpid" ] || taskkill //F //T //PID "$winpid" >/dev/null 2>&1 || true
  fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  sleep 2
  kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

# run_limited <seconds> <cmd…>: portable timeout (macOS has no GNU timeout).
run_limited() {
  local limit="$1" pid wd rc marker
  shift
  marker=$(mktemp "${TMPDIR:-/tmp}/sb-suite-timeout.XXXXXX")
  rm -f "$marker"
  set -m
  "$@" </dev/null &
  pid=$!
  set +m
  (
    sleep "$limit" &
    sp=$!
    trap 'kill "$sp" 2>/dev/null; exit 0' TERM
    wait "$sp"
    : > "$marker"
    echo "::error::timed out after ${limit}s; stopping the suite" >&2
    kill_tree "$pid"
  ) &
  wd=$!
  wait "$pid"
  rc=$?
  kill -TERM "$wd" 2>/dev/null || true
  wait "$wd" 2>/dev/null || true
  if [ -e "$marker" ]; then rc=124; rm -f "$marker"; fi
  return "$rc"
}

shopt -s nullglob
suites=(tests/test-*.sh tests/test_*.py)
[ "${#suites[@]}" -gt 0 ] || { echo 'No safety suites found' >&2; exit 1; }
failed=()
for suite in "${suites[@]}"; do
  echo "::group::$suite"
  start=$SECONDS
  case "$suite" in
    *.sh) run_limited "$SUITE_LIMIT" bash "$suite" ;;
    *.py) run_limited "$SUITE_LIMIT" python3 "$suite" ;;
  esac
  rc=$?
  echo "[run-safety] $suite exit $rc in $((SECONDS - start))s"
  echo '::endgroup::'
  if [ "$rc" -ne 0 ]; then
    failed+=("$suite")
    echo "::error::$suite failed (exit $rc)"
  fi
done
if [ "${#failed[@]}" -gt 0 ]; then
  printf 'Failed suite: %s\n' "${failed[@]}" >&2
  exit 1
fi
echo "PASS: ${#suites[@]} safety suites"
