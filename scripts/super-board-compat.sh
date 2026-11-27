# super-board-compat.sh — sourced near the top of every super-board bash script.
#
#   . "$(dirname "${BASH_SOURCE[0]}")/super-board-compat.sh"
#
# On macOS and Linux it changes nothing: no function is defined and python3/jq
# are the real commands. On Windows (Git Bash / MSYS2 / Cygwin) it smooths over
# what native Windows programs do differently, so the scripts above it can stay
# plain bash:
#
#   SB_WINDOWS   exported, 1 on Windows and empty elsewhere. Python sees it too.
#   jq           native jq.exe writes CRLF, so every $(jq -r …) value would end in
#                \r. Runs jq with --binary (-b) when this jq has it, else strips
#                \r from its stdout. Exit status, -e and stderr are unchanged.
#   python3      a normal Windows install has `python` and `py`, often no
#                `python3`, or only the Microsoft Store stub that prints a hint and
#                fails. Resolves the first of python3, python, `py -3` that really
#                runs, to that interpreter's own path (SB_PYTHON, exported), and
#                strips the \r a Windows Python writes for every \n on a pipe.
#   <(…)         both shims copy a /dev/fd/N argument (process substitution, which
#                only MSYS programs can open) to a temp file for the native program.
#   PYTHONUTF8   exported 1: Python's files and pipes default to UTF-8, not the
#                ANSI code page.
#   MSYS         exported with winsymlinks:nativestrict (`ln -s` makes a real link or
#                fails, instead of silently copying) and noglob (a Git Bash started by
#                Python must not glob/brace-expand its arguments: a JSON card argument
#                {"a":1,"b":2} would split in two).
#   sb_exec_python  use instead of `exec python3 …`: exec cannot run a function.
#   SB_BASH      exported Windows path of this bash, for Python that runs a
#                shell-script tool (see super-board-compat.py).
#
# Functions are exported, so a child bash (a test suite, a fake tool) inherits
# them even before it sources this file itself. Bash 3.2 safe; no GNU flags.

case "${OSTYPE:-}" in
  msys*|cygwin*|win32*) SB_WINDOWS=1 ;;
  darwin*|linux*) SB_WINDOWS= ;;
  *) case "$(uname -s 2>/dev/null)" in
       MINGW*|MSYS*|CYGWIN*) SB_WINDOWS=1 ;;
       *) SB_WINDOWS= ;;
     esac ;;
esac
# tests/test-compat.sh runs the Windows half on any OS.
[ -z "${SB_COMPAT_FORCE_WINDOWS:-}" ] || SB_WINDOWS=1
export SB_WINDOWS

if [ -z "$SB_WINDOWS" ]; then
  sb_exec_python() { exec python3 "$@"; }
  # Nothing else to do here: python3 and jq are the real commands.
  return 0 2>/dev/null || exit 0
fi

# _sb_native <strip:0|1> <cmd…>: run a native Windows program from bash.
#  - An argument /dev/fd/N (bash process substitution, <(…)) is a pipe only MSYS
#    programs can open; it is copied to a temp file the native program can read.
#  - strip=1 removes the \r a native program writes before every \n on a pipe.
#    A terminal gets the output untouched, so prompts are not held back.
# Exit status is the program's own, so -e, `if` and pipefail behave as before.
_sb_native() {
  local strip="$1" a t rc=0
  local args=() tmps=()
  shift
  for a in "$@"; do
    case "$a" in
      /dev/fd/*|/proc/self/fd/*)
        t=$(mktemp "${TMPDIR:-/tmp}/super-board-fd.XXXXXX") || return 66
        cat "$a" > "$t" || { rm -f "$t"; return 66; }
        tmps+=("$t"); args+=("$t") ;;
      *) args+=("$a") ;;
    esac
  done
  if [ "$strip" = 0 ] || [ -t 1 ]; then
    "${args[@]}" || rc=$?
  else
    case $- in *e*) set +e; "${args[@]}" | tr -d '\r'; rc=${PIPESTATUS[0]}; set -e ;;
               *) "${args[@]}" | tr -d '\r'; rc=${PIPESTATUS[0]} ;; esac
  fi
  [ "${#tmps[@]}" -eq 0 ] || rm -f "${tmps[@]}"
  return "$rc"
}

if [ -z "${SB_JQ_MODE:-}" ]; then
  # jq 1.6+ on Windows has --binary (-b): it writes LF instead of CRLF.
  if printf '1\n' | command jq -b . >/dev/null 2>&1; then SB_JQ_MODE=binary; else SB_JQ_MODE=strip; fi
  export SB_JQ_MODE
fi
if [ "$SB_JQ_MODE" = binary ]; then
  jq() { _sb_native 0 command jq -b "$@"; }
else
  jq() { _sb_native 1 command jq "$@"; }
fi

_sb_find_python() {
  local cand exe
  for cand in python3 python 'py -3'; do
    # shellcheck disable=SC2086 # 'py -3' is two words on purpose
    exe=$(command $cand -c 'import sys; sys.stdout.write(sys.executable)' 2>/dev/null) || continue
    [ -n "$exe" ] || continue
    if command -v cygpath >/dev/null 2>&1; then exe=$(cygpath -u "$exe"); fi
    [ -f "$exe" ] || continue
    printf '%s' "$exe"
    return 0
  done
  return 1
}
if [ -z "${SB_PYTHON:-}" ] || [ ! -f "$SB_PYTHON" ]; then
  SB_PYTHON=$(_sb_find_python) || SB_PYTHON=
  export SB_PYTHON
fi
if [ -n "$SB_PYTHON" ]; then
  python3() { _sb_native 1 "$SB_PYTHON" "$@"; }
else
  python3() {
    echo "super-board: no working Python 3 found (tried python3, python, py -3). Install it from https://www.python.org/downloads/ and open a new terminal." >&2
    return 127
  }
fi
sb_exec_python() { python3 "$@"; exit $?; }

if [ -z "${SB_BASH:-}" ] && command -v cygpath >/dev/null 2>&1; then
  SB_BASH=$(cygpath -w "${BASH:-/usr/bin/bash}" 2>/dev/null) || SB_BASH=
  export SB_BASH
fi
# MSYS `ln -s` silently copies when it cannot make a link; a real link or a failure
# lets install.sh tell the two apart (it copies, and says so, when linking fails).
case " ${MSYS:-} " in *winsymlinks*) ;; *) MSYS="${MSYS:+$MSYS }winsymlinks:nativestrict" ;; esac
case " ${MSYS:-} " in *glob*) ;; *) MSYS="$MSYS noglob" ;; esac
export MSYS
# Python and other native programs write text files with UTF-8 only when told.
export PYTHONUTF8="${PYTHONUTF8:-1}"

export -f _sb_native jq python3 sb_exec_python
