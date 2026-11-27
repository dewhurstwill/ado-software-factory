#!/usr/bin/env bash
# Offline release checks. Keep test-* / test_* suites offline; paid evals live in evals/.
# Run every suite even after a failure, then fail the whole gate if any suite failed.
# Each suite gets SB_SUITE_TIMEOUT seconds (default 300); a suite still running then is
# killed with its child processes and counted as failed (exit 124), and the run goes on.
set -uo pipefail
cd "$(dirname "$0")/.."
# Windows (Git Bash): jq without CRLF and a working python3, exported to every suite,
# plus the pinned test git settings and LF-writing Python fakes (tests/sbtest.sh).
# shellcheck source=sbtest.sh
if [ -f tests/sbtest.sh ]; then . tests/sbtest.sh
elif [ -f scripts/super-board-compat.sh ]; then . scripts/super-board-compat.sh
fi

for tool in bash python3 git jq node curl tar; do
  command -v "$tool" >/dev/null || { echo "Missing test dependency: $tool" >&2; exit 1; }
done

SUITE_LIMIT="${SB_SUITE_TIMEOUT:-300}"
case "$SUITE_LIMIT" in ''|*[!0-9]*) echo "SB_SUITE_TIMEOUT must be whole seconds" >&2; exit 64 ;; esac

# Stop a suite and everything it started. Suites run in their own process group
# (set -m below); Windows-native children (python.exe, node.exe) are outside MSYS
# process groups, so on Windows the process tree is ended with taskkill as well.
kill_tree() {
  local pid="$1" tree p w
  if [ -n "${SB_WINDOWS:-}" ]; then
    # MSYS ps: PID PPID PGID WINPID … (a status letter may lead). Walk PPID links from
    # the suite down, then end each process by MSYS PID and by Windows PID.
    tree=$(ps 2>/dev/null | awk -v root="$pid" '
      NR > 1 { if ($1 !~ /^[0-9]+$/) { $1 = ""; $0 = $0 } ; pp[$1] = $2; wp[$1] = $4 }
      END { keep[root] = 1; changed = 1
            while (changed) { changed = 0
              for (p in pp) if (!(p in keep) && (pp[p] in keep)) { keep[p] = 1; changed = 1 } }
            for (p in keep) print p, wp[p] }')
    while read -r p w; do
      [ -n "$p" ] || continue
      kill -KILL "$p" 2>/dev/null || true
      [ -z "$w" ] || taskkill //F //T //PID "$w" >/dev/null 2>&1 || true
    done <<EOF
$tree
EOF
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
  # The watchdog writes nothing to the suite's output, so a sleep it leaves behind can
  # never hold a $(…) or pipe open, and it is not waited for once the suite is done
  # (a signal can be slow to reach a waiting MSYS bash).
  (
    trap 'kill "$sp" 2>/dev/null; exit 0' TERM
    sleep "$limit" &
    sp=$!
    wait "$sp"
    : > "$marker"
    kill_tree "$pid"
  ) </dev/null >/dev/null 2>&1 &
  wd=$!
  wait "$pid"
  rc=$?
  kill -TERM "$wd" 2>/dev/null || true
  if [ -e "$marker" ]; then
    rc=124; rm -f "$marker"
    echo "::error::timed out after ${limit}s; stopped the suite" >&2
  fi
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
