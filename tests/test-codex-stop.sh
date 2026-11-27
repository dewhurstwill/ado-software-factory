#!/usr/bin/env bash
# Stop integration uses local dummy processes and gh/ps/pgrep stubs only.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/sbtest.sh"  # compat shims, pinned git settings, fake_tool
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d)
wave_pid=""
trap 'if [ -n "$wave_pid" ]; then kill "$wave_pid" 2>/dev/null || true; wait "$wave_pid" 2>/dev/null || true; fi; rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/.claude/super-board/configs" "$TMP/.claude/super-board/inflight/merge.lock"
printf '{"bot_identity":"bot"}\n' > "$TMP/.claude/super-board/configs/test.json"
printf 'SLUG=test\nSTARTED=now\n' > "$TMP/.claude/super-board/inflight/workflow-wave.lock"
printf 'PID=999999999\nLANE=build\nSTARTED=now\n' > "$TMP/.claude/super-board/inflight/12"
export STOP_TRACE="$TMP/trace"
# Windows has no SIGTERM for another process, so the wave's TERM handler below cannot
# run there: stop ends the wave's process tree instead. The gh stub then proves the
# same order there: the wave is gone before the first claim is touched.
cat > "$TMP/bin/gh" <<'SH'
#!/usr/bin/env bash
if [ -n "${STOP_WAVE_WINPID:-}" ] && ! grep -q '^wave-' "$STOP_TRACE" 2>/dev/null; then
  if tasklist //FI "PID eq $STOP_WAVE_WINPID" //FO CSV //NH | grep -q "\"$STOP_WAVE_WINPID\""; then
    echo wave-alive >> "$STOP_TRACE"
  else
    echo wave-stopped >> "$STOP_TRACE"
  fi
fi
printf 'gh %s\n' "$*" >> "$STOP_TRACE"
SH
cat > "$TMP/bin/pgrep" <<'SH'
#!/usr/bin/env bash
exit 1
SH
cat > "$TMP/bin/ps" <<'SH'
#!/usr/bin/env bash
echo "python3 - /kit/scripts/super-board-codex-wave.sh"
SH
chmod +x "$TMP/bin/"*
# argv names the wave script, as the real wave's does (Windows reads the command line).
python3 - "$TMP" /kit/scripts/super-board-codex-wave.sh <<'PY' &
import os, pathlib, signal, sys, time
root = pathlib.Path(sys.argv[1])
def stop(*_):
    with (root / 'trace').open('a') as f:
        f.write('wave-stopped\n')
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
(root / '.claude/super-board/codex-wave.pid').write_text(str(os.getpid()) + '\n')
deadline = time.time() + 120  # never outlive a failed test
while time.time() < deadline:
    time.sleep(.05)
PY
wave_pid=$!
for attempt in {1..100}; do
  [ -f "$TMP/.claude/super-board/codex-wave.pid" ] && break
  sleep .02
done
[ -z "${SB_WINDOWS:-}" ] || export STOP_WAVE_WINPID="$(cat "$TMP/.claude/super-board/codex-wave.pid")"
(cd "$TMP" && PATH="$TMP/bin:$PATH" bash "$ROOT/scripts/super-board-stop.sh" test > stop.log 2>&1) || { echo "FAIL: stop exited non-zero"; cat "$TMP/stop.log"; exit 1; }
wait "$wave_pid" || [ -n "${SB_WINDOWS:-}" ]  # Windows: taskkill /F ends it with status 1
wave_pid=""
[ "$(head -1 "$STOP_TRACE")" = wave-stopped ] || { echo "FAIL: claims released before wave stopped"; cat "$STOP_TRACE" "$TMP/stop.log"; exit 1; }
grep -q '^gh issue comment 12 ' "$STOP_TRACE" || { echo 'FAIL: numeric issue not wrapped up'; exit 1; }
if grep -q 'workflow-wave.lock\|merge.lock' "$STOP_TRACE"; then
  echo 'FAIL: control marker treated as an issue'; exit 1
fi
[ -d "$TMP/.claude/super-board/inflight/merge.lock" ] || { echo 'FAIL: merge lock removed'; exit 1; }
[ ! -e "$TMP/.claude/super-board/codex-wave.pid" ] || { echo 'FAIL: Codex marker retained'; exit 1; }

# A live reused PID with a different command must not be signalled or released.
# This shell is alive and is not a wave (its Windows PID on Windows, as a wave writes).
if [ -n "${SB_WINDOWS:-}" ]; then cat "/proc/$$/winpid"; else printf '%s\n' "$$"; fi > "$TMP/.claude/super-board/codex-wave.pid"
unset STOP_WAVE_WINPID
printf 'PID=999999999\nLANE=qa\n' > "$TMP/.claude/super-board/inflight/13"
printf '#!/usr/bin/env bash\necho unrelated-process\n' > "$TMP/bin/ps"
: > "$STOP_TRACE"
rc=0
(cd "$TMP" && PATH="$TMP/bin:$PATH" bash "$ROOT/scripts/super-board-stop.sh" test > stop.log) || rc=$?
[ "$rc" -eq 1 ] || { echo 'FAIL: reused PID not refused'; exit 1; }
[ ! -s "$STOP_TRACE" ] || { echo 'FAIL: reused PID released claims'; exit 1; }
[ -f "$TMP/.claude/super-board/inflight/13" ] || { echo 'FAIL: reused PID removed worker lock'; exit 1; }
echo 'PASS: test-codex-stop.sh (stop order, control markers, reused PID)'
