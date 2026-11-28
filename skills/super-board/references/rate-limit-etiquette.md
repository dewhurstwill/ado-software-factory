# Worker rate-limit etiquette

The dispatcher's `gh_rate_guard` only protects the dispatcher's own ticks. Workers (Builder / Tester / Reviewer) run as independent `claude -p` sessions and **share the same provider credential** — on GitHub the gh-auth token bucket, 5000 GraphQL points/hr for a PAT, 15000/hr for a GitHub App; on Azure DevOps the one PAT or `az login` identity the service throttles. The #381 worker storm (2026-05-21) drained the bucket because nothing in the worker contract told workers to watch the quota.

This file is the worker-side contract. Every worker MUST follow it. Lane steps call the forge (`python3 .claude/bin/super-board-forge.py --config <cfg> <verb>`, written `forge <verb>`; [providers.md](providers.md) has every verb) and the guard functions below, which are provider-aware; the provider-specific costs are in the two sections that follow.

## Price list (GitHub)

GraphQL is charged in points, not requests: a query's cost is the nodes it
*could* return, so one nested connection multiplies. Measured 2026-10-04 with
`rateLimit(dryRun:true){cost}` on gh 2.101 and a 131-card board, when a 3-card
wave spent ~2,850 of 5,000 points (~950 a card) and the board stalled an hour:

| Call | GraphQL points |
|---|---|
| `gh project item-list` | 101 per 100 cards — **~203** on this board |
| `gh project field-list` | **101** (it fetches 100 items it never prints) |
| `gh project view` | 2 |
| `gh issue view` · `gh pr view` · `gh pr list` · review-thread query | 1 |
| `gh issue comment` · `gh pr comment` · `gh issue edit` · `gh pr ready` | ~2 (lookup + mutation) |
| `forge board move` (`super-board-card.sh move`) | 1 (3 the first time an issue is seen) |
| `forge board status` · `board items` | 1 · ~1 per 100 cards |
| `gh api repos/...` (REST) | 0 — the separate 5,000/hr core bucket |

The rules that follow from it:

- **Cards go through `forge board`** — `move <issue> <Status>`,
  `status <issue>`, `items`. On GitHub it runs `.claude/bin/super-board-card.sh`, which caches the
  project, Status field and option ids and each issue's item id, shared across worktrees.
  `gh project item-list`, `field-list` and `view` belong to onboarding.
- **REST for everything REST can do** — the forge verbs already do this on GitHub; call the verb,
  never the `gh` command beside it (providers.md → "GitHub backend" has the full table):

  | Need | Forge verb | REST call it runs on GitHub |
  |---|---|---|
  | comment on an issue or PR | `ticket comment` · `pr comment` | `POST repos/{owner}/{repo}/issues/<N>/comments` |
  | edit a comment | `ticket comment-edit` · `pr comment-edit` | `PATCH repos/{owner}/{repo}/issues/comments/<id>` |
  | read comments | `ticket comments` · `pr comments` | `GET repos/{owner}/{repo}/issues/<N>/comments --paginate` |
  | add / remove a label | `ticket edit --add-label` · `--remove-label` | `POST issues/<N>/labels` · `DELETE issues/<N>/labels/<l>` |
  | read a PR (head sha, body, draft) | `pr view` · `pr body` | `GET repos/{owner}/{repo}/pulls/<N>` |
  | find a PR by branch | `pr refs`, pick the `headRefName` | (`gh pr list`, 1 point) |
  | close an issue | `ticket close --reason completed` | `PATCH repos/{owner}/{repo}/issues/<N>` `state=closed` |

  Review threads (`pr threads`, `pr thread-resolve`), CI (`pr checks`) and draft → ready
  (`pr ready`) have no REST form; they stay GraphQL at 1–2 points each.
- **One read per decision.** Read a thing when a step needs it; a `sleep` loop
  around `gh` spends a point every turn of the loop.

## Azure DevOps

There is no quota endpoint and no point budget. The forge's transport saves the `X-RateLimit-*`
response headers (`ado-quota.json`; the live service sends `X-RateLimit-Cost` but no
`Remaining`), records every 429 (`ado-throttle.json`) and waits `Retry-After` itself. So:

- `sb_gh_guard_check` never calls `gh` and never blocks without a reading; it halts on the shared
  halt file (79) and sleeps only when the saved `remaining` is at or below
  `SB_ADO_GUARD_MIN_REMAINING` (5) with the reset still in the future, at most 300 s.
- `sb_gh_guard_summary` prints the provider's line: `azure-devops remaining=<n>/<limit>
  cost=<total> throttled=<429s>` (`?` for what the service never sent).
- `super-board-throttle.sh` steps `max_workers` down on a 429 recorded during the wave (or a lane
  result naming one) and on nothing else.
- The per-worker call budget (§7) and the sub-agent cap (§4) are unchanged. One read per
  decision still holds: `ticket graph` makes one comments call per open ticket.

## 1. Source the guard at worker start

```bash
export SB_CONFIG=<cfg>     # the guard reads the provider from it, as the forge does
source scripts/super-board-gh-guard.sh
sb_gh_budget_init 150      # per-worker soft cap on gh calls
sb_gh_guard_check 200      # GitHub: sleep if GraphQL remaining < 200 (Azure DevOps: see above)
sb_gh_guard_summary        # log starting quota for the run manifest
```

## 2. Use `sb_gh_guard_check` before any burst

Call it before:

- Reading or resolving PR review threads (Builder rebuild, Tester rebuild, Reviewer).
- Spawning adversarial sub-agents.
- Final self-check verification on exit.

It's a no-op if quota is healthy. It sleeps to reset only if remaining is below threshold. Cheap to call.

## 3. Trust the move's exit code

The Worker self-check item "Card column move re-read and verified" previously required a full board read (`item-list --limit 500`) on every worker exit — ~200 points per worker per lane transition, for very little signal. **Rule:**

- Trust the exit code of `forge board move` (on GitHub it already re-reads stale ids and retries once; on Azure DevOps an unknown column refreshes the cache once, then exits 3).
- If it returned non-zero, re-try once with `sb_gh_guard_check 200` first.
- If still non-zero, write the halt comment and exit. Do not re-query the whole board.

## 4. Adversarial mode — sub-agent gh-call cap

When `truth_gate` triggers adversarial mode, each sub-agent (Code-grounder, Historian) MUST stay within `SB_GH_GUARD_SUBAGENT_BUDGET` (default 50) gh calls. The Reviewer passes this budget to each sub-agent via prompt:

> Adversarial sub-agent budget: ≤50 provider calls (`forge` or `gh`) total. Prefer `git blame` (local) over remote reads (`forge`, GitHub GraphQL). If you need more than 50 calls to reach a confidence score, return `confidence: "insufficient_data"` and let the Reviewer flag the card as 🛡 truth-check inconclusive — do NOT burn through quota.

## 5. Backoff on 403 / secondary rate limit (GitHub) and 429 (Azure DevOps)

GitHub: if a `gh` call returns 403 or a body containing `secondary rate limit`, do NOT retry immediately. Sleep 60s, then re-check with `sb_gh_guard_check 500` (stricter threshold) before resuming.

Azure DevOps: the forge already waited `Retry-After` on a 429 (a read counts it as one of its three attempts) and recorded it for the throttle step-down. Re-check with `sb_gh_guard_check 500` before the next burst; never retry a write.

## 6. Log remaining quota in your exit handoff comment

Every worker's PR handoff comment MUST include a final line:

```
gh-quota-on-exit: graphql=<n>/5000 rest=<n>/5000
```

On Azure DevOps the same line carries the provider's fields:
`gh-quota-on-exit: azure-devops remaining=<n>/<limit> cost=<total> throttled=<429s>`.

Use `sb_gh_guard_summary` to grab the snapshot (the text after its `[gh-guard]` tag). This gives the run manifest visibility into which lane is the heaviest consumer over time.

## 7. Per-worker hard cap

`sb_gh_budget_spend` decrements a per-worker call counter. Default 150. If exhausted, the helper returns non-zero — the worker should write a halt comment ("worker-budget exhausted, releasing claim") and exit gracefully. The orphan-scan and reaper will recover the lock; the dispatcher re-tries on next tick when quota has recovered.

---

Pointer: dispatcher-side guard lives in `super-board-run.sh::gh_rate_guard` (legacy `claude-p`, GitHub only); the workflow backend steps down with `super-board-throttle.sh`. The two together are defense in depth — dispatcher pauses before the next tick, worker pauses before its next burst.
