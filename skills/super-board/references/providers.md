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
- `azure-devops` serves the tracker verbs and the pull-request verbs (below). A verb a backend does not
  have exits 69 (`<provider> provider: <verb> is not implemented yet`): the Azure DevOps-only `pr create|ready|
  comment|comments|comment-edit|links` on GitHub.

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
| 3 | Azure DevOps: work item not on the board, column not on the board, or pull request not found; `pr` via the gate: the PR is closed (abandoned) |
| 4 | `ticket claim`: already claimed (never steals) |
| 6 | Azure DevOps `pr merge`: the head moved (TF401192); nothing merged |
| 5, 7 | Azure DevOps `pr merge` only: 5 conflicts, 7 branch policy rejects completion (`human-gate:` line on stdout) |
| 65 | refused rather than guess or write: `board ensure` (a non-empty extra column would be deleted); Azure DevOps `pr set-body`/`pr create` (body over 4,000 characters after the overflow) and `pr merge` (draft, not active, mergeStatus conflicts / rejectedByPolicy / failure) |
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
| `pr approval <N>` | `{state, headRefOid, closes:[ticket numbers]}` (see "Human approval") | `api graphql --paginate --slurp -f query=<approval query> -F owner= -F repo= -F pr=N`; `closes` = same-repo `closingIssuesReferences`, plus the body's closing keywords when the base is not the default branch; incomplete link pages or a snapshot that changed between pages fail | approval |
| `ticket comments <N>` | `[[{id,body,user:{login,type},created_at,updated_at}]]` (the issue or PR conversation) | `api --paginate --slurp repos/{owner}/{repo}/issues/N/comments?per_page=100` | comments |
| `permission <login>` | `{permission}`: `admin`, `maintain`, `write`, `triage`, `read`, `none`; `null` for a string that is not a login (no call) | `api repos/{owner}/{repo}/collaborators/<login>/permission` | permission |
| `board <verb> ...` | reserved: `super-board-card.sh` hands an `azure-devops` config to this | none on GitHub (the card script owns its verbs) | |

Shape rules for any backend: the field names above are exact; `state`/`stateReason` use
GitHub's words (`OPEN`, `CLOSED`, `MERGED`, `COMPLETED`, `NOT_PLANNED`); `headRefOid` is a
full 40-hex sha; `files` entries carry `path`; `labels` entries carry `name`.

## Azure DevOps tracker backend

Code: `super-board-forge-ado.py` (mixed into the forge's `AzureDevOpsBackend`). stdlib `urllib`
only. Tracker verbs: ticket, board, comment and claim. The pull-request verbs are in `super-board-forge-ado-pr.py` (see "Azure DevOps pull-request backend").

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
| `ticket comments <N>` | `[[{id,body,user:{login,type},created_at,updated_at,version}]]`, `[[]]` when none. `created_at`/`updated_at` = `createdDate`/`modifiedDate`; `version` starts at 1 and grows on every edit | |
| `ticket comment-edit <N> <ID> --body-file F` | comment id | |
| `ticket claim <N>` | `claimed #N`. Reads; a claim tag that is not ours: exit 4; ours: exit 0 (idempotent); else `add` ours, re-read and verify. If another holder's tag landed too, the lexicographically smallest claim tag wins and the loser takes its own tag back (rev-tested replace) | 4 already claimed or lost the race |
| `ticket release <N> [--force]` | `released #N (<tags>)`; removes OUR claim tag only; `--force` removes every claim tag (crash-recovery sweep). Idempotent | |
| `whoami` | `{login,id}` | |
| `permission <login>` | `{permission}`: `"approve"` when `ado.approvers` lists the unique name (case-insensitive), else `"none"`. No call | 66 `ado.approvers` not a list of names |
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

## Azure DevOps pull-request backend

Code: `super-board-forge-ado-pr.py`. Repository = `ado.repository` (default: the project). Resource
`{org}/{project}/_apis/git/repositories/{repo}/pullrequests/{id}`, JSON bodies (`application/json`),
transport, auth, halting and the 409/412 rules as above. PR web URL: `{organization}/{project}/_git/{repository}/pullrequest/{id}`.
Config `repo.path` is the local clone used for `pr metadata` / `pr diff`.
Labels come from `GET …/pullrequests/{id}/labels` (`{count, value:[{id,name,active}]}`); the single-PR GET has no `labels` key
and is not trusted for them. A failed labels read is a failed required read (halt), never "no labels": merge policy matches on them.

| Verb | Stdout | Exit |
|---|---|---|
| `pr head <N>` | `<branch> <40-hex>`: `sourceRefName` without `refs/heads/`, `lastMergeSourceCommit` | 3 not found |
| `pr state <N>` | `{state, headRefOid, mergeCommit}`; `active`=OPEN, `completed`=MERGED, `abandoned`=CLOSED; `mergeCommit:{oid}` only when completed, else null | 69 completed without a merge commit |
| `pr metadata <N>` | `{labels, files:[{path,additions,deletions}], additions, deletions, changedFiles, body}` from local git; `body` is the logical body | 69 fetch/head mismatch/validation |
| `pr diff <N> --meta F` | unified diff from local git, validated against `F` | 64 no `--meta`; 69 |
| `pr body <N>` | `{headRefOid, body}`: the LOGICAL body (overflow put back) | |
| `pr set-body <N> --body-file F` | nothing | 65 still over 4,000 after the overflow; nothing written |
| `pr merge <N> --squash --delete-branch [--subject S] [--body-file F] --match-head <sha>` | `merged #N as <sha>`; on 7 also `human-gate: policy — Azure DevOps branch policy blocks completion: <mergeFailureMessage or "see the PR's policy list">` | 6 head moved (TF401192); 5 `mergeStatus` conflicts; 7 `rejectedByPolicy`; 65 draft, not active, `mergeStatus` failure or other definite refusal; 69 only when the outcome is unknown (poll timeout, transport failure after the PATCH) |
| `pr list --state open\|merged --limit N` | prs-open / prs-merged shapes (open: + `headRefName`, `files:[{path}]` without the leading `/`) | |
| `pr create --base B --head H --title T --body-file F [--draft] [--label L]... [--closes <ticket>]...` | URL, then the number (last line) | 65 body too long, nothing created |
| `pr ready <N>` | `ready #N` | |
| `pr comment <N> --body-file F` | `<threadId>.<commentId>` | |
| `pr comments <N>` | `[[{id:"<thread>.<comment>", body, user:{login,type:"User"}, created_at, updated_at}]]`, oldest first. `created_at` = `publishedDate`; `updated_at` = `lastContentUpdatedDate` (else `lastUpdatedDate`), because `lastUpdatedDate` also moves when nothing in the text changed | |
| `pr comment-edit <N> <thread>.<comment> --body-file F` | the id | 64 bad id |
| `pr links <N>` | `{closes:[<work item ids>]}` from the PR's linked work items | |
| `pr approval <N>` | `{state, headRefOid, closes}`: `pr state`'s state and head, `pr links`' work items (never text parsing, whatever the target branch) | 3 not found |

`pr list` `body` is the raw description: a moved history block shows as its stub line. `pr comments` leaves out
`system` comments, deleted ones and the overflow thread. `$top` is passed as `--limit`.

Observed live: create, head/state/links, metadata/diff, overflow round-trip, comment edit (HTML markers survive), list open/merged,
ready, the service reports a new source head about 2s after a push, the full gate (stale head exit 6, squash in ~3s, branch
deleted, re-run is a no-op), `Closes #N` closing the work item even with `transitionWorkItems:false`; `lastMergeSourceCommit`
stays set on a completed PR.

### Merge: pinned commit, then poll

The completion `PATCH` is `{status:"completed", lastMergeSourceCommit:{commitId:<match-head>}, completionOptions:
{mergeStrategy:"squash", deleteSourceBranch:true, transitionWorkItems:false, mergeCommitMessage:"<subject>\n\n<body>"}}`.
`lastMergeSourceCommit` is the `--match-head-commit` equivalent: missing = 400, not the current source head = 409
`TF401192` (exit 6, nothing merged). The service answers 200 and STILL says `active`/`queued`; the verb then GETs every
`SB_ADO_MERGE_POLL_INTERVAL` seconds (2) for up to `SB_ADO_MERGE_POLL_TIMEOUT` (90) until `status == completed`.
`mergeStatus` conflicts / rejectedByPolicy / failure (checked before the completion and while polling) are DEFINITE
refusals with their own codes (5, 7, 65; 7 also prints the `human-gate:` line); a draft exits 65 telling the caller to run
`pr ready` first. Only a timeout or a transport failure after the PATCH exits 69: the gate re-reads the state and halts
for reconciliation. The gate maps 5 to its exit 5 (rebase pass), 7 to its exit 7 (human-gate line, approval request, no halt),
65 to exit 1 with no halt; 6 and everything else take the re-read path. The GitHub backend normalises a `gh pr merge`
exit of 5, 6, 7 or 65 to 1, so none of those codes can come from GitHub. The completion is never re-sent. Without `--subject` the PR title is the squash subject (when a body
is given); with neither the service writes its own message. A `Closes #<work item id>` line in the squash message makes
the service close that work item and move its card to Done (repo setting, on by default): the factory's later move to Done
is then a no-op.

**Trial-commit trap:** `lastMergeCommit` is already set while the PR is active (the service's trial merge). It is the real
merge commit only once `status` is `completed`; `pr state` reports `mergeCommit` only then.

### No server-side diff

The service has no added/deleted line counts and no unified diff. `pr metadata` and `pr diff` run in `repo.path`:
`git fetch origin <source> <target>` (the clone's own credentials), the fetched source tip must equal
`lastMergeSourceCommit` (else one re-read, then exit 69: a push is still being processed), `merge-base(target, head)`,
then BOTH the `--numstat` and the diff come from that one `base..head` pair with `--no-renames` (a rename is a delete
plus an add; binary files count 0). The forge runs the read helper's `metadata` and `diff` validators on its own output
before printing. A completed PR's source branch is deleted, so these verbs serve open PRs only.

### The 4,000-character description cap

A description over 4,000 characters (UTF-16 units) is a 400. `pr set-body` and `pr create` keep callers unaware:
if the body fits it is the description; if not, the whole `<!-- sb:history -->` block, then (only if still too long)
the `<!-- sb:visual -->` block, moves into ONE PR thread whose first comment starts with `<!-- sb:overflow -->` (created
once, then edited in place). In the description the block keeps its own markers around
`_Moved to the first comment thread (Azure DevOps limits a description to 4,000 characters)._`. When nothing overflows any
more the thread's comment is reset to the marker plus a one-line note, never deleted. `pr body` / `pr metadata` read the
thread (only when the description carries the stub) and substitute the blocks back, so `super-board-pr-body.sh` always sees and
writes one complete body. Still over 4,000 after both moves: exit 65 naming the limit and the size, nothing written
(`super-board-pr-body.sh` passes the 65 on and does not halt). The thread is written before the description, and reset after it.
HTML comments (`<!-- sb:... -->`) survive in descriptions and thread comments (unlike work items).

## Human approval

`super-board-approval.py` alone decides whether a held merge (gate exit 7 or 8) has human approval.
It takes `--config` (provider; GitHub when absent) and keeps `--repo` for GitHub callers. GitHub runs
the same `gh` reads through the read helper with the same kinds (`approval`, `comments`,
`permission`; argv from the forge's GitHub backend). Azure DevOps loads the forge's backend in-process:
`pr approval`, `pr comments`, `ticket comments`, `whoami` and the `ado.approvers` check. A halt is
exit 79 on both; anything else unreadable is `approved: false`.

**Trust source.** GitHub: the commenter's current collaborator permission (`write`, `maintain`,
`admin`). Azure DevOps: the config allow-list `ado.approvers: ["user@example.com", ...]`, unique names
compared case-insensitively with the comment author's unique name (never the display name). Absent or
empty: nobody approves by comment, and a person merges held PRs in Azure DevOps. Why a list: the
config is committed and reviewed, so who may release a held merge is visible and auditable, and it
needs no extra token scope (group membership needs the Graph scope, which the pack does not ask for).

**Rules, the same on both** (Azure DevOps: `tests/test_approval_ado.py`; GitHub: `tests/test_approval.py`):

1. A request is the newest trusted 🙋 block (`Reason tag: …🙋` or an `approval-request:` line). It
   must carry exactly one `approval-request:` line pinning repo, PR, full head and the policy/steps
   hash. GitHub: the request's repo is `owner/name`; Azure DevOps: `<org>/<project>/<repository>`,
   lower-case.
2. Trusted requester: GitHub write+; Azure DevOps an approver or the token's own identity (the pack
   posts requests; its unique name comes from `connectionData`'s `Account`, else nobody).
3. Any newer trusted 🙋 block supersedes the request; blocks from anyone else are ignored. Two
   newest blocks at the same instant: hold.
4. Approval is a later comment that is exactly `done` (`.`/`!` allowed), from a `User` who may
   approve, in the same place as the request (the same work item, or the same PR). Azure DevOps
   work item comments may wrap that word in one `<div>` or `<p>` (the rich-text editor).
5. Edited comments count for nothing: an edited request holds, an edited `done` is skipped. GitHub
   and PR threads: `updated_at != created_at` (PR threads use `lastContentUpdatedDate`). Work items:
   also `version != 1` (or no version).
6. The PR must be open and at the head the request pins; the policy/steps must be unchanged.
7. Only the PR and the tickets it really links are read. Azure DevOps: the PR's own work item
   links, and PR ids and work item ids are separate spaces (a `done` on PR 1 never answers work
   item 1). File-anchored PR threads are ignored, like GitHub review comments.
8. Incomplete evidence never approves: unfinished link pages (GitHub), a repeated work item comment
   continuation token (Azure DevOps), unreadable timestamps. Azure DevOps also re-reads the PR after
   the comments and holds if its head, state or links moved.

**Solo identity (Azure DevOps).** One PAT often belongs to the human who also approves. An identity
that is both `whoami` and on `ado.approvers` MAY approve, as a solo GitHub owner whose own login runs
the pack may (`test_current_request_allows_solo_owner`). Not on the list, the pack's identity can post
requests but never approve. The mechanism cannot tell the person from an agent holding their token,
on either provider: lanes must never write `done` (block-template.md, "Trust boundary").

## Not behind the forge yet

The card verbs in `super-board-card.sh`, and the filer, lane, run and status scripts still call `gh`
directly. `super-board-preflight.sh` skips the GitHub quota guard on Azure DevOps.
