#!/usr/bin/env bash
# scripts/super-board-compat.sh: a pure pass-through off Windows, and on Windows (forced
# here with SB_COMPAT_FORCE_WINDOWS, so every OS runs it) jq/python3 shims that drop
# CRLF, keep exit status and -e, read <(…) arguments, and skip a broken python3.
# scripts/super-board-compat.py: tool() resolution and its Windows rules.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
CR=$(printf '\r')

# 1 — off Windows nothing is defined (run in a clean bash without inherited shims).
case "${OSTYPE:-}" in msys*|cygwin*|win32*) ON_WINDOWS=1 ;; *) ON_WINDOWS= ;; esac
if [ -z "$ON_WINDOWS" ]; then
  out=$(env -u SB_WINDOWS -u SB_PYTHON -u SB_JQ_MODE bash --norc -c '
    unset -f jq python3 _sb_native sb_exec_python 2>/dev/null
    . "$1"; type -t jq; type -t python3; echo "w=$SB_WINDOWS"' _ "$ROOT/scripts/super-board-compat.sh")
  [ "$out" = "$(printf 'file\nfile\nw=')" ] || fail "compat must not wrap jq/python3 off Windows: $out"
fi

# 2 — Windows shims, with native tools that behave like Windows ones: a jq without
#     --binary that writes CRLF, a Store-stub python3 that fails, a python that works.
mkdir -p "$T/bin"
REAL_JQ=$(type -P jq); REAL_PY=${SB_PYTHON:-$(type -P python3)}  # paths, never an inherited shim
cat > "$T/bin/jq" <<SH
#!/usr/bin/env bash
for a in "\$@"; do [ "\$a" = -b ] && { echo 'unknown option -b' >&2; exit 2; }; done
set -o pipefail
"$REAL_JQ" "\$@" | sed 's/\$/\r/'
SH
printf '#!/usr/bin/env bash\necho "Python was not found; run without arguments to install from the Microsoft Store" >&2\nexit 9009\n' > "$T/bin/python3"
cat > "$T/bin/python" <<SH
#!/usr/bin/env bash
exec "$REAL_PY" "\$@"
SH
chmod +x "$T/bin/"*
run_win() {  # <script>: run in a fresh bash with the Windows half forced
  env -u SB_PYTHON -u SB_JQ_MODE -u SB_WINDOWS SB_COMPAT_FORCE_WINDOWS=1 PATH="$T/bin:$PATH" \
    bash --norc -c "unset -f jq python3 _sb_native sb_exec_python 2>/dev/null; . '$ROOT/scripts/super-board-compat.sh'; $1"
}
[ "$(run_win 'echo $SB_JQ_MODE')" = strip ] || fail 'a jq without -b must use strip mode'
v=$(run_win 'jq -r .a <<<"{\"a\":\"x\"}"')
[ "$v" = x ] || fail "jq value kept a CR: $(printf '%s' "$v" | od -c | head -2)"
case "$(run_win 'printf "a\nb\n" | jq -R .')" in *"$CR"*) fail 'jq -R lines kept CR' ;; esac
rc=0; run_win 'set -e; jq -e .a <<<"{\"a\":false}" >/dev/null; echo unreachable' >/dev/null || rc=$?
[ "$rc" -eq 1 ] || fail "jq -e false must fail under set -e with 1, got $rc"
rc=0; run_win 'set -euo pipefail; x=$(jq . <<<"{bad"); echo unreachable' >/dev/null 2>&1 || rc=$?
[ "$rc" -ne 0 ] || fail 'jq parse error must fail $(…) under set -e'
out=$(run_win 'if jq -e .a <<<"{\"a\":null}" >/dev/null; then echo yes; else echo "no $?"; fi')
[ "$out" = "no 1" ] || fail "jq -e in if: $out"
[ "$(run_win 'jq -r .k <(echo "{\"k\":\"fd\"}")')" = fd ] || fail 'jq must read a <(…) argument'
ls "${TMPDIR:-/tmp}"/super-board-fd.* >/dev/null 2>&1 && fail 'process-substitution temp copy left behind'

py=$(run_win 'echo "$SB_PYTHON"')
[ -n "$py" ] && [ "$py" != "$T/bin/python3" ] || fail "python3 resolution must skip the failing stub: $py"
v=$(run_win "python3 -c 'print(\"a\\r\")'")
[ "$v" = a ] || fail 'python3 output kept a CR'
rc=0; run_win "python3 -c 'import sys; sys.exit(7)'" || rc=$?
[ "$rc" -eq 7 ] || fail "python3 exit status lost: $rc"
[ "$(run_win 'python3 - x <<<"import sys; print(sys.argv[1])"')" = x ] || fail 'python3 heredoc stdin'
[ "$(run_win 'python3 -c "import sys; print(open(sys.argv[1]).read().strip())" <(echo viafd)')" = viafd ] \
  || fail 'python3 must read a <(…) argument'
[ "$(run_win 'f() { sb_exec_python -c "print(3)"; echo unreachable; }; f')" = 3 ] || fail 'sb_exec_python must end the shell'
# Exported: a child bash inherits the shims without sourcing anything.
[ "$(run_win 'bash -c "type -t jq"')" = function ] || fail 'child bash must inherit the jq shim'
# No working Python at all: a clear message, exit 127.
mkdir -p "$T/nopy"; cp "$T/bin/python3" "$T/nopy/python3"; cp "$T/bin/python3" "$T/nopy/python"
err=$(env -u SB_PYTHON -u SB_JQ_MODE SB_COMPAT_FORCE_WINDOWS=1 PATH="$T/nopy:/usr/bin:/bin" \
  bash --norc -c "unset -f python3; . '$ROOT/scripts/super-board-compat.sh'; python3 -c 1" 2>&1) && fail 'no python must fail'
case "$err" in *'no working Python 3'*) ;; *) fail "missing-python message: $err" ;; esac

# 3 — super-board-compat.py
PYC="$ROOT/scripts/super-board-compat.py"
printf '#!/bin/sh\necho hi\n' > "$T/bin/sbfake"; chmod +x "$T/bin/sbfake"
out=$(PATH="$T/bin:$PATH" "$REAL_PY" "$PYC" which sbfake)
if [ -n "$ON_WINDOWS" ]; then  # Windows: Git Bash runs the #! script: "<bash.exe> C:\…\sbfake"
  case "$out" in *bash*sbfake) ;; *) fail "which must find a PATH tool: $out" ;; esac
else
  [ "$out" = "$T/bin/sbfake" ] || fail "which must find a PATH tool: $out"
fi
rc=0; python3 "$PYC" which sb-no-such-tool 2>/dev/null || rc=$?
[ "$rc" -eq 1 ] || fail 'which must fail for a missing tool'
python3 - "$PYC" "$T" <<'PY' || fail 'compat.py Windows rules'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('c', sys.argv[1]); c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
t = sys.argv[2]
d = os.path.join(t, 'win'); os.makedirs(d)
def w(name, body):
    with open(os.path.join(d, name), 'w') as f: f.write(body)
w('pyfake', '#!/usr/bin/env python3\nprint(1)\n'); w('shfake', '#!/bin/sh\necho 1\n'); w('nodefake', '#!/usr/bin/env node\n')
w('both', '#!/bin/sh\n'); w('both.cmd', '@echo off\n'); w('exe.exe', ''); w('exe', '#!/bin/sh\n'); w('plain', 'no shebang\n')
c.WINDOWS = True
os.environ['SB_BASH'] = sys.executable  # any existing file stands in for Git Bash
os.environ['PATH'] = d
assert c.resolve('pyfake') == [sys.executable, os.path.join(d, 'pyfake')], c.resolve('pyfake')
assert c.resolve('shfake') == [sys.executable, os.path.join(d, 'shfake')]
assert c.resolve('nodefake')[1:3] == ['-c', 'exec "$0" "$@"']
assert c.resolve('both') == [sys.executable, os.path.join(d, 'both')], 'a #! script beats its .cmd twin'
assert c.resolve('exe') == [os.path.join(d, 'exe.exe')], '.exe beats a script'
assert c.resolve('plain') is None and c.resolve('nothing') is None
assert c.resolve('bash') == [sys.executable], 'bash is Git Bash, never System32'
assert c.resolve(os.path.join(d, 'pyfake'))[0] == sys.executable
# An MSYS program gets every word quoted (it globs and brace-expands bare words itself).
assert c.command_line([sys.executable, '{"a":1}']) == [sys.executable, '{"a":1}'], 'a native program keeps the list'
line = c.command_line(['C:/Git/usr/bin/bash.exe', 'x.sh', '{"a":1,"b":2}', 'c:\\d\\', 'p q'])
assert line == '"C:/Git/usr/bin/bash.exe" "x.sh" "{\\"a\\":1,\\"b\\":2}" "c:\\\\d\\\\" "p q"', line
try:
    c.tool('nothing'); raise SystemExit('tool() must raise')
except c.ToolMissing as e:
    assert isinstance(e, FileNotFoundError) and 'nothing' in str(e)
PY
echo 'PASS: test-compat.sh (pass-through off Windows; jq/python3 shims; tool resolution)'
