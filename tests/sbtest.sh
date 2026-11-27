# sbtest.sh — sourced by the bash suites (not a suite itself):
#
#   . "$(cd "$(dirname "$0")" && pwd)/sbtest.sh"
#
# - super-board-compat.sh: jq without CRLF, a working python3 (no-op off Windows).
# - Host git settings that change a test repo are pinned through GIT_CONFIG_*
#   (Git for Windows defaults core.autocrlf=true; a host may sign commits or name
#   its default branch differently). Product hooks that drop GIT_* are unaffected.
# - fake_tool <dir> <name>: writes the fake tool <dir>/<name> from stdin, executable.
#   A Python fake (#!…python…) on Windows becomes a bash stub that runs the Python
#   body through python3, so bash callers get LF output and Windows Python callers
#   can start it (super-board-compat.py runs `#!` scripts). Elsewhere the body is
#   written as given.
# - sb_path <path>: one spelling of a path for comparisons (C:\x, C:/x and /c/x).

SBTEST_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../scripts/super-board-compat.sh
. "$SBTEST_ROOT/scripts/super-board-compat.sh"

sbtest_git_env() {
  local n="${GIT_CONFIG_COUNT:-0}" kv
  for kv in core.autocrlf=false core.safecrlf=false core.longpaths=true init.defaultBranch=main \
            commit.gpgsign=false tag.gpgsign=false user.name=super-board-test \
            user.email=super-board-test@example.invalid; do
    export "GIT_CONFIG_KEY_$n=${kv%%=*}" "GIT_CONFIG_VALUE_$n=${kv#*=}"
    n=$((n + 1))
  done
  export GIT_CONFIG_COUNT="$n"
}
[ -n "${SBTEST_GIT_ENV:-}" ] || { sbtest_git_env; export SBTEST_GIT_ENV=1; }
# Windows: Python fakes write LF like the tools they stand in for (tests/pysite).
sbtest_pysite() {
  [ -n "${SB_WINDOWS:-}" ] || return 0
  local site="$SBTEST_ROOT/tests/pysite"
  command -v cygpath >/dev/null 2>&1 && site=$(cygpath -w "$site")
  case ";${PYTHONPATH:-};" in *";$site;"*) return 0 ;; esac
  export PYTHONPATH="$site${PYTHONPATH:+;$PYTHONPATH}"
}
sbtest_pysite

fake_tool() {
  local dir="$1" name="$2" body first
  body=$(cat; printf x)
  body=${body%x}
  first=${body%%$'\n'*}
  mkdir -p "$dir"
  case "$first" in
    '#!'*python*)
      if [ -n "${SB_WINDOWS:-}" ]; then
        printf '%s' "$body" > "$dir/.$name.py"
        printf '#!/usr/bin/env bash\n. %q\npython3 %q "$@"\n' \
          "$SBTEST_ROOT/scripts/super-board-compat.sh" "$dir/.$name.py" > "$dir/$name"
        chmod +x "$dir/$name"
        return 0
      fi ;;
  esac
  printf '%s' "$body" > "$dir/$name"
  chmod +x "$dir/$name"
}

# sb_native <path>: the form a native Windows program receives for an MSYS path
# (C:/x/y, as MSYS converts arguments); the path itself elsewhere.
sb_native() {
  if [ -n "${SB_WINDOWS:-}" ] && command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

sb_path() {
  local p="$1"
  if [ -n "${SB_WINDOWS:-}" ] && command -v cygpath >/dev/null 2>&1; then
    p=$(cygpath -u "$p")
  fi
  printf '%s' "${p%/}"
}
