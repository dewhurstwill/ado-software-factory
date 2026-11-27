#!/usr/bin/env bash
# Exercise aggregation with tiny fixtures; never recursively run the actual suite.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/sbtest.sh"  # compat shims, pinned git settings, fake_tool
PACK=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/tests"
cp "$PACK/tests/run-safety.sh" "$WORK/tests/"
fail() { echo "FAIL: $1" >&2; exit 1; }
if bash "$WORK/tests/run-safety.sh" >/dev/null 2>&1; then fail 'empty suite passed'; fi
printf 'exit 3\n' > "$WORK/tests/test-a-failure.sh"
printf 'echo reached\n' > "$WORK/tests/test-z-success.sh"
printf 'print("python reached")\n' > "$WORK/tests/test_python.py"
rc=0
out=$(bash "$WORK/tests/run-safety.sh" 2>&1) || rc=$?
[ "$rc" -eq 1 ] || fail "failed suite did not fail gate: $rc"
[[ "$out" == *reached* && "$out" == *'python reached'* ]] || fail 'failure hid later suites'
rm "$WORK/tests/test-a-failure.sh"
bash "$WORK/tests/run-safety.sh" >/dev/null || fail 'passing suites failed'

# A hung suite (and the child it started) is stopped at the limit, fails the gate,
# and the suites after it still run.
cat > "$WORK/tests/test-b-hang.sh" <<'SH'
sleep 600 &
echo started
wait
SH
rc=0
start=$SECONDS
out=$(SB_SUITE_TIMEOUT=3 bash "$WORK/tests/run-safety.sh" 2>&1) || rc=$?
[ "$rc" -eq 1 ] || fail "hung suite did not fail gate: $rc"
[ $((SECONDS - start)) -lt 60 ] || fail 'hung suite was not stopped at its limit'
[[ "$out" == *'test-b-hang.sh exit 124'* ]] || fail "hung suite not reported as a timeout: $out"
[[ "$out" == *reached* ]] || fail 'timeout hid later suites'
rm "$WORK/tests/test-b-hang.sh"
echo 'PASS: test-safety-runner.sh (empty, failure, continuation, success, timeout)'
