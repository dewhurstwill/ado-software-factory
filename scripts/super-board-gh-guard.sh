#!/usr/bin/env bash
# super-board-gh-guard.sh — worker-side rate-limit etiquette.
#
# Sourced by super-board workers (Builder / Tester / Reviewer) before any
# burst of gh calls. The dispatcher's gh_rate_guard only protects the
# dispatcher's own ticks; workers run as independent claude -p sessions and
# share the same gh-auth bucket. Without this guard, a single Reviewer in
# adversarial mode can drain the 5000/hr GraphQL bucket and the next tick
# of the dispatcher (and every other worker on the same machine) opens to
# "0 remaining".
#
# Usage from a worker:
#   source scripts/super-board-gh-guard.sh
#   sb_gh_guard_check 200     # sleep until reset if GraphQL remaining < 200
#   sb_gh_budget_spend 5      # decrement worker-local call budget; halt if 0
#   sb_gh_guard_summary       # log remaining quota; safe to call frequently
#
# Provider: GitHub (default) reads `gh api rate_limit`. On an azure-devops config ($SB_CONFIG, as
# for every forge call) nothing here calls `gh`: Azure DevOps has no quota endpoint, so the guard
# reads what the forge saved from X-RateLimit-* response headers (`forge quota`), never blocks when
# the service reported nothing, and sleeps only when the saved remaining is at or near zero with a
# reset still in the future (SB_ADO_GUARD_MIN_REMAINING, default 5; at most 300s). The summary line
# is `azure-devops remaining=<n>/<limit> cost=<total> throttled=<429s>`; workers append it as
# `gh-quota-on-exit: <that text>` exactly as they append the GitHub one.
#
# Windows: jq/python3 shims (bash only: workers may source this file from zsh).
if [ -n "${BASH_VERSION:-}" ]; then . "$(dirname "${BASH_SOURCE[0]}")/super-board-compat.sh"; fi

# Constants
SB_GH_GUARD_MIN_REMAINING_DEFAULT=200
SB_GH_GUARD_REST_MIN_REMAINING=500
SB_GH_GUARD_BUDGET_DEFAULT=150     # per-worker soft cap on gh calls
SB_GH_GUARD_SUBAGENT_BUDGET=50     # per adversarial-mode sub-agent cap
SB_GH_GUARD_STATE_FILE="${SB_GH_GUARD_STATE_FILE:-${TMPDIR:-/tmp}/super-board-gh-budget-$$}"

# This file's own path: BASH_SOURCE under bash, the %x prompt escape under zsh.
if [ -n "${BASH_SOURCE[0]:-}" ]; then _sb_guard_src="${BASH_SOURCE[0]}"; else eval '_sb_guard_src="${(%):-%x}"'; fi
SB_GITHUB_READ="$(cd "$(dirname "$_sb_guard_src")" && pwd)/super-board-github-read.py"; unset _sb_guard_src
sb_gh_required_read() { local kind="$1"; shift; python3 "$SB_GITHUB_READ" --kind "$kind" -- "$@"; }
sb_gh_halt_check() { python3 "$SB_GITHUB_READ" --check; }
SB_FORGE="${SB_GITHUB_READ%/*}/super-board-forge.py"

# Sets SB_GUARD_PROVIDER (github unless the config says azure-devops); cached for the shell.
sb_gh_set_provider() {
  [ -z "${SB_GUARD_PROVIDER:-}" ] || return 0
  SB_GUARD_PROVIDER=$(python3 "$SB_FORGE" provider 2>/dev/null | tr -d '\r') || SB_GUARD_PROVIDER=""
  [ -n "$SB_GUARD_PROVIDER" ] || SB_GUARD_PROVIDER=github
}

# Azure DevOps: the saved quota record; `{}` when the forge cannot answer. Never calls gh.
sb_ado_quota() { python3 "$SB_FORGE" quota 2>/dev/null || echo '{}'; }

sb_ado_guard_check() {
  python3 "$SB_FORGE" check || return $?   # 79 when the run is halted
  local q remaining reset now wait min="${SB_ADO_GUARD_MIN_REMAINING:-5}"
  q=$(sb_ado_quota)
  remaining=$(echo "$q" | jq -r '.remaining // empty' 2>/dev/null | tr -d '\r')
  reset=$(echo "$q" | jq -r '.reset // empty' 2>/dev/null | tr -d '\r')
  case "$remaining" in ''|*[!0-9.]*) return 0 ;; esac   # the service reported no remaining: never block
  remaining=${remaining%%.*}; [ -n "$remaining" ] || return 0
  [ "$remaining" -le "$min" ] || return 0
  case "$reset" in ''|*[!0-9.]*) return 0 ;; esac
  reset=${reset%%.*}; [ -n "$reset" ] || return 0
  now=$(date +%s)
  wait=$((reset - now + 2))
  [ "$wait" -gt 0 ] || return 0                         # the window already reset
  [ "$wait" -le 300 ] || wait=300
  echo "[gh-guard] Azure DevOps quota low: ${remaining} left (<=${min}); sleeping ${wait}s until the reset" >&2
  sleep "$wait"
}

sb_ado_guard_summary() {
  local q t
  q=$(sb_ado_quota); t=$(python3 "$SB_FORGE" quota --throttle 2>/dev/null || echo '{}')
  echo "[gh-guard] $(jq -rn --argjson q "$q" --argjson t "$t" '"azure-devops remaining=\($q.remaining // "?")/\($q.limit // "?") cost=\($q.total_cost // "?") throttled=\($t.throttled // 0)"' 2>/dev/null || echo 'azure-devops remaining=?/? cost=? throttled=?')"
}

# `gh api rate_limit` (REST) misreports the GraphQL bucket. Measured 2026-10-04,
# back to back on one token: REST said graphql used=2 remaining=4998 while
# GraphQL's own rateLimit said used=930 — and an hour earlier REST said 4999
# while the board was stalled at 0. So the GraphQL numbers come from GraphQL's
# rateLimit object (a rateLimit-only query is not charged), the core numbers
# from REST. Arg 1: the REST payload; prints it with .resources.graphql replaced
# when the GraphQL read succeeds, unchanged when it does not.
sb_gh_quota_merge() {
  local rest="$1" gql
  gql=$(gh api graphql -f query='query{rateLimit{limit remaining used resetAt}}' 2>/dev/null) || { echo "$rest"; return 0; }
  echo "$rest" | jq --argjson g "$gql" '
    ($g.data.rateLimit // null) as $r
    | if $r == null then . else
        .resources.graphql = { limit: $r.limit, remaining: $r.remaining, used: $r.used,
                               reset: ($r.resetAt | fromdateiso8601) } end' 2>/dev/null || echo "$rest"
}

sb_gh_guard_check() {
  # Sleep until GraphQL quota recovers. Also checks REST.
  # Arg 1: optional minimum-remaining threshold (default 200).
  local min="${1:-$SB_GH_GUARD_MIN_REMAINING_DEFAULT}"
  sb_gh_set_provider
  if [ "$SB_GUARD_PROVIDER" = azure-devops ]; then sb_ado_guard_check; return $?; fi
  local payload graphql_remaining graphql_reset rest_remaining now wait
  payload=$(sb_gh_required_read quota api rate_limit) || return $?
  payload=$(sb_gh_quota_merge "$payload")
  graphql_remaining=$(echo "$payload" | jq -r '.resources.graphql.remaining // 5000')
  rest_remaining=$(echo "$payload" | jq -r '.resources.core.remaining // 5000')

  if [ "$graphql_remaining" -lt "$min" ]; then
    graphql_reset=$(echo "$payload" | jq -r '.resources.graphql.reset // 0')
    now=$(date +%s)
    wait=$((graphql_reset - now + 10))
    [ "$wait" -lt 60 ] && wait=60
    [ "$wait" -gt 3600 ] && wait=3600
    echo "[gh-guard] GraphQL low: ${graphql_remaining} left (<${min}); sleeping ${wait}s" >&2
    sleep "$wait"
    return 0
  fi

  if [ "$rest_remaining" -lt "$SB_GH_GUARD_REST_MIN_REMAINING" ]; then
    echo "[gh-guard] REST low: ${rest_remaining} left — pausing 60s to let the token breathe" >&2
    sleep 60
  fi
}

sb_gh_guard_summary() {
  # One-line snapshot. Use in worker exit messages for trend tracking.
  sb_gh_set_provider
  if [ "$SB_GUARD_PROVIDER" = azure-devops ]; then sb_ado_guard_summary; return 0; fi
  local payload
  payload=$(sb_gh_quota_merge "$(gh api rate_limit 2>/dev/null || echo '{}')")
  echo "[gh-guard] $(echo "$payload" | jq -r '"graphql=\(.resources.graphql.remaining // "?")/\(.resources.graphql.limit // "?") rest=\(.resources.core.remaining // "?")/\(.resources.core.limit // "?")"')"
}

sb_gh_budget_init() {
  # Initialize per-worker budget. Call once at worker start.
  local budget="${1:-$SB_GH_GUARD_BUDGET_DEFAULT}"
  echo "$budget" > "$SB_GH_GUARD_STATE_FILE"
}

sb_gh_budget_spend() {
  # Decrement budget by N (default 1). If exhausted, halt the worker.
  local cost="${1:-1}" remaining
  [ -f "$SB_GH_GUARD_STATE_FILE" ] || sb_gh_budget_init
  remaining=$(cat "$SB_GH_GUARD_STATE_FILE")
  remaining=$((remaining - cost))
  echo "$remaining" > "$SB_GH_GUARD_STATE_FILE"
  if [ "$remaining" -le 0 ]; then
    echo "[gh-guard] worker gh-call budget exhausted — halting to protect shared quota" >&2
    return 73
  fi
}

sb_gh_budget_remaining() {
  [ -f "$SB_GH_GUARD_STATE_FILE" ] || sb_gh_budget_init
  cat "$SB_GH_GUARD_STATE_FILE"
}
