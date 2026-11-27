#!/usr/bin/env bash
# super-board-stop.sh — graceful shutdown of an in-flight super-board run.
#
# What it does:
#   1. Find every in-flight worker (lock files under .claude/super-board/inflight/).
#      Stop any Codex wave and its lane process groups before comments/releases.
#   2. For each one, post a "stopped mid-flight" comment on the issue AND its PR
#      (with last commit, lane, and resume hint) — so context survives the kill.
#   3. Release the GitHub assignee mutex on each claimed issue. (Azure DevOps config: the same
#      steps through super-board-forge.py: `ticket comment`, `pr comment`, `ticket release` --
#      with --force when our release leaves a claim tag behind -- and `ticket edit` for the loop
#      labels. No `gh` is run.)
#   4. SIGTERM → 1s → SIGKILL the worker PIDs.
#   5. Kill the legacy dispatcher loop (super-board-run.sh).
#   6. Remove in-flight locks. Leave worktrees in place (Builder can resume).
#   7. Print summary + resume command.
#
# What it does NOT do:
#   - Wait for workers to reach a clean stopping point (claude -p has no SIGTERM
#     handler that flushes a partial commit). Unsaved in-memory work can be lost;
#     uncommitted files remain in the preserved worktree for inspection.
#   - Touch worktrees. Leaving them lets the next worker pick up faster.
#   - Touch branches or PRs. State lives on the GitHub Project board.
#
# Resume:
#   super-board run <slug>     — same command. The board is the state. Cards sit
#                                in whichever column they were in. Workers re-claim
#                                from the "stopped" comment context.
#
# Usage:
#   scripts/super-board-stop.sh [<config-slug>]
#
# Exit codes:
#   0  clean stop (or nothing to stop)
#   64 missing arg + no .claude/super-board/active
#   66 config not found

set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/super-board-compat.sh"  # Windows: jq/python3 shims; no-op elsewhere

# ───────────────────────────── args + paths ─────────────────────────────
CONFIG_SLUG="${1:-}"
if [ -z "$CONFIG_SLUG" ]; then
  if [ -f .claude/super-board/active ]; then
    CONFIG_SLUG=$(cat .claude/super-board/active)
  else
    echo "usage: $0 <config-slug>  (or set .claude/super-board/active)" >&2
    exit 64
  fi
fi

CONFIG_PATH=".claude/super-board/configs/${CONFIG_SLUG}.json"
if [ ! -f "$CONFIG_PATH" ]; then
  echo "config not found: $CONFIG_PATH" >&2
  exit 66
fi

# ───────────────────────────── config read ─────────────────────────────
# GitHub only: the assignee to remove. Azure DevOps claims with a tag and never reads it.
BOT_LOGIN=$(jq -r '.notifications.bot_identity // .bot_identity // ""' "$CONFIG_PATH")
PROVIDER=$(jq -r '.provider // "github"' "$CONFIG_PATH" | tr -d '\r')
FORGE_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/super-board-forge.py"
forge() { python3 "$FORGE_PY" --config "$CONFIG_PATH" "$@"; }
INFLIGHT_DIR=".claude/super-board/inflight"
RUN_DATE=$(date +%Y-%m-%d)
RUN_MANIFEST="docs/super-board/runs/${RUN_DATE}-${CONFIG_SLUG}.md"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ───────────────────────────── helpers ─────────────────────────────
log() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"
  [ -f "$RUN_MANIFEST" ] && printf '[%s] STOP %s\n' "$(date +%H:%M:%S)" "$*" >> "$RUN_MANIFEST" || true
}

# Source a lock file. Supports both v1.3.0+ (PID=/LANE=/STARTED= assignments)
# and pre-v1.3.0 (single-line PID) formats.
read_lock() {
  local lock="$INFLIGHT_DIR/$1"
  PID=""; LANE=""; STARTED=""
  [ -f "$lock" ] || return 1
  if grep -q '^PID=' "$lock" 2>/dev/null; then
    # shellcheck disable=SC1090
    . "$lock" 2>/dev/null || true
  else
    PID=$(cat "$lock" 2>/dev/null || echo "")
  fi
  return 0
}

# Look up the PR number for an issue's branch (if any). One gh call.
pr_for_issue() {
  local issue="$1"
  if [ "$PROVIDER" = azure-devops ]; then
    forge pr refs --state open --limit 200 2>/dev/null \
      | jq -r --arg p "issue-${issue}-" '.[] | select(.headRefName | startswith($p)) | .number' 2>/dev/null | tr -d '\r' | head -1
    return 0
  fi
  gh pr list --state open --json number,headRefName \
    --jq ".[] | select(.headRefName | startswith(\"issue-${issue}-\")) | .number" 2>/dev/null | head -1
}

# Latest commit on the issue's branch (local or remote). Best-effort.
last_commit_on_branch() {
  local issue="$1" branch
  branch=$(git branch -a --list "*issue-${issue}-*" 2>/dev/null | head -1 | sed 's|^[* ]*||;s|^remotes/[^/]*/||')
  [ -z "$branch" ] && { echo "(no branch yet)"; return; }
  git log -1 --format='%h %s' "$branch" 2>/dev/null || echo "(branch found but no commits)"
}

post_stop_comment() {
  local issue="$1" lane="$2" pid="$3" pr commit body
  commit=$(last_commit_on_branch "$issue")
  pr=$(pr_for_issue "$issue" || echo "")

  # Comment format: writing-standard.md § 4.
  body="[orchestrator] [report] 🛑 stopped · ${lane:-unknown} lane
Did: \`super-board stop\` ended worker PID ${pid:-unknown} at ${TS}
✅ Done: resume point is the last pushed commit — ${commit}
❌ Not done: interrupted work may be uncommitted; inspect the preserved worktree
Next: preserve edits before \`super-board run ${CONFIG_SLUG}\`; dirty or legacy worktrees need inspection"

  if [ "$PROVIDER" = azure-devops ]; then
    local bodyfile; bodyfile=$(mktemp)
    printf '%s\n' "$body" > "$bodyfile"
    forge ticket comment "$issue" --body-file "$bodyfile" >/dev/null 2>&1 \
      && log "  💬 ticket comment posted on #${issue}" \
      || log "  ⚠ failed to comment on ticket #${issue} (continuing)"
    if [ -n "$pr" ]; then
      forge pr comment "$pr" --body-file "$bodyfile" >/dev/null 2>&1 \
        && log "  💬 PR comment posted on #${pr}" \
        || log "  ⚠ failed to comment on PR #${pr} (continuing)"
    fi
    rm -f "$bodyfile"
    return 0
  fi

  gh issue comment "$issue" --body "$body" >/dev/null 2>&1 \
    && log "  💬 issue comment posted on #${issue}" \
    || log "  ⚠ failed to comment on issue #${issue} (continuing)"

  if [ -n "$pr" ]; then
    gh pr comment "$pr" --body "$body" >/dev/null 2>&1 \
      && log "  💬 PR comment posted on #${pr}" \
      || log "  ⚠ failed to comment on PR #${pr} (continuing)"
  fi
}

release_claim() {
  local issue="$1"
  if [ "$PROVIDER" = azure-devops ]; then
    # The claim is a tag, not an assignee: release OURS; if a claim tag is still there (another
    # holder id wrote it, e.g. a crashed run under a different SB_CLAIM_HOLDER) sweep it with --force.
    forge ticket release "$issue" >/dev/null 2>&1 || log "  ⚠ could not release the claim on #${issue} (continuing)"
    if [ "$(forge ticket view "$issue" 2>/dev/null | jq -r '(.assignees // []) | length' 2>/dev/null | tr -d '\r')" -gt 0 ] 2>/dev/null; then
      forge ticket release "$issue" --force >/dev/null 2>&1 && log "  🔓 swept a claim that was not ours on #${issue}" \
        || log "  ⚠ a claim is still on #${issue} (continuing)"
    fi
    local label
    for label in loop:in-build loop:in-qa loop:in-review; do
      forge ticket edit "$issue" --remove-label "$label" >/dev/null 2>&1 || true
    done
    log "  🔓 released claim + lane labels on #${issue}"
    return 0
  fi
  [ -z "$BOT_LOGIN" ] && return 0
  gh issue edit "$issue" --remove-assignee "$BOT_LOGIN" >/dev/null 2>&1 || true
  for label in loop:in-build loop:in-qa loop:in-review; do
    gh issue edit "$issue" --remove-label "$label" >/dev/null 2>&1 || true
  done
  log "  🔓 released assignee + lane labels on #${issue}"
}

kill_pid() {
  local pid="$1" name="${2:-process}" grace="${3:-1}" waited=0
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    log "  ⊘ ${name} pid=${pid:-empty} already dead"
    return 0
  fi
  kill -TERM "$pid" 2>/dev/null || true
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$grace" ]; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    log "  ☠ ${name} pid=${pid} SIGKILLed (didn't respond to SIGTERM)"
  else
    log "  ☠ ${name} pid=${pid} SIGTERMed"
  fi
}

# The Codex wave marker holds the PID the wave's Python wrote. On Windows that is a
# Windows PID, which bash's kill -0 (MSYS PIDs) cannot see, and ps has no -o: check,
# identify and end it with Windows tools there (see super-board-run.sh, PID trap).
wave_alive() {
  if [ -n "${SB_WINDOWS:-}" ]; then
    tasklist //FI "PID eq $1" //FO CSV //NH 2>/dev/null | grep -q "\"$1\""
  else
    kill -0 "$1" 2>/dev/null
  fi
}
wave_args() {
  if [ -n "${SB_WINDOWS:-}" ]; then
    powershell.exe -NoProfile -NonInteractive -Command \
      "(Get-CimInstance Win32_Process -Filter 'ProcessId=$1').CommandLine" 2>/dev/null
  else
    ps -p "$1" -o args= 2>/dev/null
  fi
}
# Windows has no SIGTERM for another process: end the wave's whole tree (its lanes
# included) at once, then wait for it to be gone.
stop_wave() {
  local pid="$1" grace="$2" waited=0
  if [ -z "${SB_WINDOWS:-}" ]; then kill_pid "$pid" "codex-wave" "$grace"; return; fi
  if ! wave_alive "$pid"; then log "  ⊘ codex-wave pid=${pid} already dead"; return 0; fi
  taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
  while wave_alive "$pid" && [ "$waited" -lt "$grace" ]; do sleep 1; waited=$((waited + 1)); done
  if wave_alive "$pid"; then log "  ⚠ codex-wave pid=${pid} still running after taskkill"; else log "  ☠ codex-wave pid=${pid} ended (process tree)"; fi
}

# ───────────────────────────── main ─────────────────────────────
echo "════════════════════════════════════════════════════════"
echo "  super-board stop · ${CONFIG_SLUG}"
echo "════════════════════════════════════════════════════════"

# 1. Inventory in-flight workers BEFORE killing anything.
WORKERS=()
if [ -d "$INFLIGHT_DIR" ]; then
  for lock in "$INFLIGHT_DIR"/*; do
    [ -f "$lock" ] || continue
    issue=$(basename "$lock")
    # Wave/merge control markers are not GitHub issue numbers.
    case "$issue" in ''|*[!0-9]*) continue ;; esac
    read_lock "$issue"
    WORKERS+=("${issue}|${LANE:-unknown}|${PID:-}")
  done
fi

DISPATCHER_PIDS=$(pgrep -f 'super-board-run\.sh' 2>/dev/null || true)
ORPHAN_WORKERS=$(pgrep -f 'claude -p .*super-board' 2>/dev/null || true)
CODEX_WAVE_MARKER=".claude/super-board/codex-wave.pid"
CODEX_WAVE_PID=""
if [ -f "$CODEX_WAVE_MARKER" ]; then
  CODEX_WAVE_PID=$(cat "$CODEX_WAVE_MARKER")
  case "$CODEX_WAVE_PID" in ''|*[!0-9]*)
    log "🛑 invalid Codex wave PID marker; preserve claims and inspect $CODEX_WAVE_MARKER"
    exit 1 ;;
  esac
  if wave_alive "$CODEX_WAVE_PID"; then
    # Refuse a reused PID, or an unavailable process identity, rather than
    # signal an unrelated process and release a still-running wave's claims.
    if ! wave_args "$CODEX_WAVE_PID" | grep -q 'super-board-codex-wave'; then
      log "🛑 cannot identify Codex wave PID $CODEX_WAVE_PID; preserve claims and inspect $CODEX_WAVE_MARKER"
      exit 1
    fi
  fi
fi

if [ "${#WORKERS[@]}" -eq 0 ] && [ -z "$DISPATCHER_PIDS" ] && [ -z "$ORPHAN_WORKERS" ] && [ -z "$CODEX_WAVE_PID" ]; then
  echo "  ✓ nothing to stop — no dispatcher, no in-flight workers, no orphans"
  echo "════════════════════════════════════════════════════════"
  exit 0
fi

# Stop new Codex dispatch first. Its TERM handler cancels lane process groups
# and reaps them (up to five seconds), before any claim can be released.
if [ -n "$CODEX_WAVE_PID" ]; then
  log "🛑 stopping Codex wave and its lane workers"
  stop_wave "$CODEX_WAVE_PID" 7
fi

# 2. Per-worker wrap-up: comment, release claim, kill.
if [ "${#WORKERS[@]}" -gt 0 ]; then
  log "📋 ${#WORKERS[@]} in-flight worker(s) to wrap up"
  for entry in "${WORKERS[@]}"; do
    IFS='|' read -r issue lane pid <<< "$entry"
    log ""
    log "─── #${issue} (lane=${lane}, pid=${pid:-unknown}) ───"
    post_stop_comment "$issue" "$lane" "$pid"
    release_claim "$issue"
    kill_pid "$pid" "worker(${lane}/#${issue})"
  done
fi

# 3. Sweep any untracked claude -p super-board workers.
if [ -n "$ORPHAN_WORKERS" ]; then
  log ""
  log "🧹 sweeping untracked claude -p super-board workers"
  for pid in $ORPHAN_WORKERS; do
    kill_pid "$pid" "orphan-worker"
  done
fi

# 4. Kill the dispatcher loop.
if [ -n "$DISPATCHER_PIDS" ]; then
  log ""
  log "🛑 stopping dispatcher loop"
  for pid in $DISPATCHER_PIDS; do
    kill_pid "$pid" "dispatcher"
  done
fi

# 5. Clear in-flight locks (PIDs are dead now).
if [ -d "$INFLIGHT_DIR" ] && [ -n "$(ls -A "$INFLIGHT_DIR" 2>/dev/null)" ]; then
  for entry in "${WORKERS[@]}"; do
    IFS='|' read -r issue lane pid <<< "$entry"
    rm -f "$INFLIGHT_DIR/$issue"
  done
  log ""
  log "🧽 cleared $INFLIGHT_DIR"
fi
[ -z "$CODEX_WAVE_PID" ] || rm -f "$INFLIGHT_DIR/workflow-wave.lock" "$CODEX_WAVE_MARKER"

# 6. Summary.
DISP_COUNT=$(echo "$DISPATCHER_PIDS" | tr ' ' '\n' | grep -c . || true)
[ -z "$CODEX_WAVE_PID" ] || DISP_COUNT=$((DISP_COUNT + 1))
echo ""
echo "════════════════════════════════════════════════════════"
echo "  ✅ stopped: ${#WORKERS[@]} worker(s), ${DISP_COUNT} dispatcher(s)"
echo "  📝 wrap-up comments posted on each in-flight issue + PR"
echo "  ▶  Resume: super-board run ${CONFIG_SLUG}"
echo "════════════════════════════════════════════════════════"
