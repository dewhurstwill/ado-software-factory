# Providers: the forge verb contract

Scripts never call `gh` for tickets, PRs or merges. They call
`.claude/bin/super-board-forge.py`, which picks a backend from the project config and
answers every verb in one set of shapes. Callers (merge policy, the jq in
`super-board-wave-plan.sh` and `super-board-deps.sh`, validators) read those shapes and
do not know which provider answered.

## Choosing the provider

- `--config <file>`, else `$SB_CONFIG`. Key `provider`: `"github"` (default when the key or
  the config is absent) or `"azure-devops"`. Anything else exits 66.
- The config is read once; it may be a FIFO. A caller that already consumed its config
  hands the forge a fresh copy (`--config <(printf '%s\n' "$CONFIG_JSON")`).
- Scripts pass `--config` to the forge whenever they have one.
- `azure-devops` serves the tracker verbs (below). Its pull-request verbs (`pr ...`) still exit 69
  (`azure-devops provider: <verb> is not implemented yet`) until that slice lands.

## Repo

`--repo <slug>`, else config `repo.remote`, else `git remote get-url origin`. GitHub strips
`https://github.com/`, `git@github.com:` and a trailing `.git` to give `owner/name`.
Verbs add `--repo` to `gh` only for a repo given by `--repo` or the config, so a bare
checkout still lets `gh` pick its own. `repo --explicit` prints that same value without the
origin fallback (possibly empty); `repo` alone falls back to origin.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 64 | usage error, or a malformed read (the read helper's: valid form is quoted on stderr; the run continues). `SB_GITHUB_READ_STRICT=1` turns a malformed read into a halt (79) |
| 3 | Azure DevOps: work item not on the board, or column not on the board |
| 4 | `ticket claim`: already claimed (never steals) |
| 65 | `board ensure` refused rather than guess (a non-empty extra column would be deleted) |
| 66 | config unreadable, or unknown `provider` |
| 69 | provider or verb unavailable; Azure DevOps: no credentials, or a write failed |
| 79 | required reads halted: the shared halt file exists. Preserve work, stop dispatch, restart explicitly |
| other | a write's own `gh` status, passed through (writes are never retried) |

Exit 79 is shared: both providers use one halt file (the name says github; it is not renamed
because installs depend on it). `ado-quota.json` sits beside it.

## Verbs

Usage: `super-board-forge.py [--config F] [--repo R] <verb> [args]`. Reads go through
`super-board-github-read.py` with the kind shown (retry, halt file, exit 79/64). Writes run `gh`
directly.

| Verb | Stdout | GitHub backend (`gh` argv; `--repo` as above) | Kind |
|---|---|---|---|
| `provider` | provider name | none | |
| `repo [--explicit]` | `owner/name` | none (git for the origin fallback) | |
| `check` | nothing; 79 when halted | helper `check_halt()` | |
| `halt <reason>` | nothing; records halt, 79 | helper `halt()` | |
| `pr head <N>` | `<headRefName> <40-hex sha>` | `pr view N --json headRefName,headRefOid -q '.headRefName + " " + .headRefOid'` | head |
| `pr state <N>` | `{state, headRefOid, mergeCommit}` | `pr view N --json state,mergeCommit,headRefOid` | merge-state |
| `pr metadata <N>` | `{labels, files, additions, deletions, changedFiles, body}` | `pr view N --json labels,files,additions,deletions,changedFiles,body` | metadata |
| `pr diff <N> --meta <file>` | unified diff; checked against the metadata file | `pr diff N` | diff |
| `pr body <N>` | `{headRefOid, body}` | `api repos/{owner}/{repo}/pulls/N --jq '{headRefOid: .head.sha, body: (.body // "")}'`, `GH_REPO` set from the repo | body |
| `pr set-body <N> --body-file <f>` | nothing | write: `api -X PATCH repos/{owner}/{repo}/pulls/N --input -`, stdin `{"body": <file text>}` | |
| `pr merge <N> --squash --delete-branch [--subject S] [--body-file F] --match-head <sha>` | the provider's merge output | write: `pr merge N --squash --delete-branch [--subject S] [--body-file F] --match-head-commit sha` | |
| `pr list --state open --limit N` | `[{number,title,body,url,headRefName,files:[{path}]}]` | `pr list --state open --limit N --json number,title,body,url,headRefName,files` | prs-open |
| `pr list --state merged --limit N` | `[{number,title,body,url}]` | `pr list --state merged --limit N --json number,title,body,url` | prs-merged |
| `ticket view <N>` | `{number, title, body}` | `issue view N --json number,title,body` | issue |
| `ticket list --state closed --limit N` | `[{number,title,body,stateReason}]` | `issue list --state closed --limit N --json number,title,body,stateReason` | issues-closed |
| `ticket graph` | array of every open issue: `{number,title,body,state,labels:{nodes:[{name}]},comments:{nodes:[{body}]}}` (last 8 comments) | `api graphql --paginate --slurp` (open issues, 100 a page), pages flattened | issues |
| `board <verb> ...` | reserved: `super-board-card.sh` hands an `azure-devops` config to this | none on GitHub (the card script owns its verbs) | |

Shape rules for any backend: the field names above are exact; `state`/`stateReason` use
GitHub's words (`OPEN`, `CLOSED`, `MERGED`, `COMPLETED`, `NOT_PLANNED`); `headRefOid` is a
full 40-hex sha; `files` entries carry `path`; `labels` entries carry `name`.

## Azure DevOps tracker backend

Code: `super-board-forge-ado.py` (mixed into the forge's `AzureDevOpsBackend`). stdlib `urllib`
only. Tracker only: ticket, board, comment and claim verbs. No `pr` verbs yet.

### Config

```
"provider": "azure-devops",
"ado": { "organization": "https://dev.azure.com/<org>", "project": "<name>", "team": "<name>",
         "board": "Stories", "repository": "<repo>",
         "work_item_types": {"feature":"User Story","bug":"Bug","qa":"User Story"},
         "body_fields": {"Bug":"Microsoft.VSTS.TCM.ReproSteps"} }
```

Defaults: team `<project> Team`, board `Stories`, repository = project, the types and `body_fields`
shown (body default `System.Description`, except Bug: `Microsoft.VSTS.TCM.ReproSteps`). `project.owner/number` are unused. Env
`SB_ADO_BASE_URL` replaces the organization URL for transport (tests point it at a fake server).
`repo` prints `<org>/<project>/<repository>`.

### Auth (first that exists; a token is never printed or logged)

1. env `AZURE_DEVOPS_EXT_PAT`, sent as `Authorization: Basic base64(":" + PAT)`.
2. a line `AZURE_DEVOPS_EXT_PAT=...` in `<repo.path>/.env` (lane agents load no `.env`).
3. `az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798` as a Bearer token.

None: exit 69 naming the three options.

### Mapping

| GitHub idea | Azure DevOps |
|---|---|
| issue number | work item id |
| project board / Status column | one team's Kanban board (`ado.board`); the seven factory columns |
| label | work item tag (`System.Tags`, `; `-joined). Adding = `add` of only the missing tags (the service merges; no rev test). Removing or setting = read, then `[test /rev, replace <exact list>]` (`remove` when empty), retried on a stale rev |
| assignee = claim | tag `sb:claimed-<holder>` (never `System.AssignedTo`); any tag `sb:claimed` or `sb:claimed-*` counts as claimed, is hidden from `labels` and shown as `assignees: [{login}]` (the `whoami` login) |
| open / closed | state category: Completed or Removed = CLOSED; Removed or tag `not-planned` = `NOT_PLANNED`, else `COMPLETED` |
| issue comment | work item comment (Markdown) |
| `gh api rate_limit` | none: `X-RateLimit-*` headers are saved to `ado-quota.json`; `quota` prints it plus `seen` |

### Verbs

| Verb | Stdout | Exit |
|---|---|---|
| `board ids` | `{board, columnField, columns:[names]}` (cached) | |
| `board items` | `{items:[{id,title,status,labels:[],content:{type:"Issue",number,title,url,repository:"<org>/<project>",assignees}}],totalCount}`; WIQL for the board's types, batch reads, status from the board's own column field; cards with no column skipped | |
| `board status <N>` | column name | 3 not on board |
| `board item-id <N>` | `N` | |
| `board move <N> <Status>` | `moved #N → Status` | 3 unknown column (cache miss refreshes once) |
| `board ensure [--check]` | one `changed:` (or `needed:`) line per change, else `board ok: ...` | 1 `--check` found changes; 65 refused |
| `ticket view <N>` | `{number,title,body,state,stateReason,labels:[{name}],assignees:[{login}],url}` | 3 not found |
| `ticket list [--state open\|closed\|all] [--limit N]` | array of the same | |
| `ticket graph` | open tickets in the GitHub graph shape, each with its last 8 comments. One comments call per open ticket: the cost driver | |
| `ticket history` | `[{number,state,body}]`, every state | |
| `ticket create --title T --body-file F [--label L]... [--column C]` | URL, then the number (last line) | 3 unknown column |
| `ticket edit <N> [--title] [--body-file] [--add-label L]... [--remove-label L]...` | | |
| `ticket close <N> --reason completed\|not-planned` | `closed #N (reason)`; moves to Done; not-planned adds tag `not-planned` | |
| `ticket comment <N> --body-file F` | comment id | |
| `ticket comments <N>` | `[[{id,body,user:{login,type},created_at,updated_at}]]`, `[[]]` when none | |
| `ticket comment-edit <N> <ID> --body-file F` | comment id | |
| `ticket claim <N>` | `claimed #N`. Reads; a claim tag that is not ours: exit 4; ours: exit 0 (idempotent); else `add` ours, re-read and verify. If another holder's tag landed too, the lexicographically smallest claim tag wins and the loser takes its own tag back (rev-tested replace) | 4 already claimed or lost the race |
| `ticket release <N> [--force]` | `released #N (<tags>)`; removes OUR claim tag only; `--force` removes every claim tag (crash-recovery sweep). Idempotent | |
| `whoami` | `{login,id}` | |
| `quota` | saved `ado-quota.json` plus `seen`; when the service sends only `X-RateLimit-Cost`, `cost` (last call) and `total_cost` (running) | |
| `check`, `halt <reason>`, `provider`, `repo` | as above | 79 when halted |

The holder is a stable 6-hex id: first 6 hex of sha256 over hostname, login user and the main
checkout's absolute path; env `SB_CLAIM_HOLDER` overrides it. It never carries a per-run nonce, so
the organisation's tag pool stays small. Limit: two claimers with the SAME holder id (same machine
and checkout) are not separated by the tag; the local inflight lockfiles cover that case.

`ticket create` makes the work item type from `work_item_types` by label, sets the body Markdown
(`multilineFieldsFormat`), tags = labels. `board ensure` makes Bugs appear on the board
(`bugsBehavior: asRequirements`), sets exactly Backlog, Ready, Building, QA, Review, Blocked, Done
(`itemLimit` 0), reuses existing column ids so cards keep their column (same name first, then the
ends, then an equal state mapping, then Review, Building, QA, Blocked, Ready), and maps each type:
Backlog = its incoming state; Ready = that state if the board allows it in progress, else the first
InProgress-category state; Building/QA/Review/Blocked = the first InProgress-category state (workflow
order) the board allows; Done = the outgoing state. It refuses (65) if a non-empty column would be
deleted or emptiness cannot be checked.

### Transport and halting

Base `{org}/{project}/_apis/...`; team routes `{org}/{project}/{team}/_apis/...` (names URL-encoded).
`api-version=7.1`, comments `7.1-preview.4`, identity `7.1-preview`.
Reads: three attempts, backoff `SB_GITHUB_RETRY_DELAY`, `429` waits `Retry-After` (capped 60s, counts
as an attempt). 400, 401, 403, 203, a redirect and a 404 on a fixed resource (board, states, team
settings) halt on the first attempt; three failures or three malformed payloads halt (79). A 404 on
one work item is exit 3. Writes are never retried, except a JSON-Patch whose `/rev` test the server
rejected as stale (409, 412, or a body naming VS403351 / a revision mismatch): it did not apply, so
re-read and retry, max 3. Halt messages say Azure DevOps.

### Gotchas (observed on a real org)

- Never claim by `System.AssignedTo`: the Agile process auto-assigns the acting user when an item
  first moves to an Active state, and people assign stories to themselves.
- `add` on `System.Tags` MERGES (it never removes a tag); `replace` sets the exact list (`""` and
  `remove` give null). A write that changes nothing returns 200 with the rev unchanged.
- The rev test is not atomic with the save. Two simultaneous patches from one revision: with
  different values one gets 200 and the other 409 `TF26071` (treated like 412 `VS403351`: it did not
  apply); with IDENTICAL values both get 200. So a claim cannot rest on one shared value: hence
  per-holder tags and the verify step.
- A Bug's body field defaults to `Microsoft.VSTS.TCM.ReproSteps` (the form shows Repro Steps, not
  Description); an explicit `ado.body_fields` (even `{}`) wins. Reads fall back to `System.Description`
  when the type's field is empty.
- HTML comments in a Markdown work item body are stripped on save: keep no hidden markers there.
- The column field is per board (`WEF_<32hex>_Kanban.Column`): discovered and cached, never
  hardcoded. `System.BoardColumn` is read-only and can show another team's board.
- Moving a card writes the column field; the server sets `System.State` from the column mapping.
  An unknown column is `TF401320`.
- A new Agile project has `bugsBehavior: asTasks`: a Bug never appears in a column.
- There is no up-front quota endpoint; only `X-RateLimit-*` response headers. The live service sent
  `X-RateLimit-Cost` but no `X-RateLimit-Remaining`.
- `allowedMappings.InProgress` lists states alphabetically, not in workflow order; use the states endpoint.
- WIQL returns ids only and truncates at 20,000.

## Not behind the forge yet

`super-board-approval.py` (approval comments, closing-issue links), the card verbs in
`super-board-card.sh`, and the filer, lane, run and status scripts still call `gh` directly.
