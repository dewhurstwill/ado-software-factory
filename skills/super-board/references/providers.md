# Providers: one forge, one set of lane commands

Lane agents and scripts never call `gh` (or an Azure DevOps API) for tickets, boards, PRs or merges. They run
one command, `python3 .claude/bin/super-board-forge.py --config <config> <verb> ...`, which picks the backend
from the project config and answers every verb in ONE shape, with the same flags and exit codes on GitHub and
on Azure DevOps. A lane never branches on the provider. Below, `forge` stands for that command line.
Tests: `tests/test_forge_contract.py` runs one scenario list against both backends and fails when a stdout
shape drifts on either side; `test_forge_github.py` pins the exact `gh` argv; `test_forge_ado*.py` the service calls.

## Choosing the provider

- `--config <file>`, else `$SB_CONFIG`. Key `provider`: `"github"` (default when the key or the config is
  absent) or `"azure-devops"`. Anything else exits 66.
- The config is read once; it may be a FIFO. A caller that already consumed its config hands the forge a fresh
  copy (`--config <(printf '%s\n' "$CONFIG_JSON")`). Scripts pass `--config` whenever they have one.
- `--repo <slug>`, else config `repo.remote`, else `git remote get-url origin`. GitHub strips `https://github.com/`,
  `git@github.com:` and a trailing `.git` to give `owner/name`; `repo` prints it (`--explicit`: no origin fallback).
  Azure DevOps prints `<org>/<project>/<repository>`.
- A verb a backend does not have exits 69 (`<provider> provider: <verb> is not implemented yet`). Today only
  `board ensure` on GitHub (see "Setup-only").

## Rules for every verb

- **Ids are opaque strings.** Comment ids, note ids and thread ids are printed by one verb and handed back to
  another unchanged (`3.7` on Azure DevOps, `4242` or `PRRT_...` on GitHub); never parse or compare them across verbs.
- **Ticket and PR numbers** are the issue/PR number (GitHub) or the work item/PR id (Azure DevOps).
- **Body input** is always a file: `--body-file F`. Output is JSON (one object or array) unless the table says text.
- **`state` / `stateReason`** use GitHub's words (`OPEN`, `CLOSED`, `MERGED`; `COMPLETED`, `NOT_PLANNED`);
  `headRefOid` is a full 40-hex sha; `files` entries carry `path`; `labels` entries carry `name`.
- **Reads** go through `super-board-github-read.py` (GitHub) or the same retry in the Azure DevOps transport: three
  attempts, then the shared halt file and exit 79. **Writes** run once and are never retried; a write that failed
  with an unknown outcome is not replayed, re-read first.
- GitHub prefers the REST form (the separate core quota) over `gh issue`/`gh pr` (GraphQL points, see
  `rate-limit-etiquette.md`); the ones left on GraphQL are marked below.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 64 | usage error, or a malformed read (the read helper's: the valid form is quoted on stderr; the run continues). `SB_GITHUB_READ_STRICT=1` turns a malformed read into a halt (79) |
| 3 | not found: work item not on the board, column not on the board (also `ticket create --column` on a GitHub board: nothing created), pull request not found; `pr` via the gate: the PR is closed |
| 4 | `ticket claim`: already claimed (never steals), or the race was lost |
| 5, 7 | `pr merge` only: 5 conflicts, 7 branch policy rejects completion (`human-gate:` line on stdout). Azure DevOps only |
| 6 | the head moved: `pr merge` (TF401192) and `pr note` / `pr notes` with `--head` (both providers); nothing written |
| 65 | refused rather than guess or write: `board ensure` (a non-empty extra column would be deleted); Azure DevOps `pr set-body`/`pr create` (body over 4,000 characters after the overflow) and `pr merge` (draft, not active, mergeStatus conflicts / rejectedByPolicy / failure) |
| 66 | config unreadable, unknown `provider`, or a verb needs a config key it lacks (`ticket create --column` on GitHub needs `project.owner/number`) |
| 69 | provider or verb unavailable; Azure DevOps: no credentials; a write failed, or a follow-up of a create failed (the URL and number are STILL printed, then 69) |
| 79 | required reads halted: the shared halt file exists. Preserve work, stop dispatch, restart explicitly |
| other | a GitHub write's own `gh` status, passed through (a `gh` status that collides with a code above becomes 1) |

Exit 79 is shared by both providers: one halt file (the name says github; installs depend on it). `ado-quota.json` sits beside it.

## The lane operations

`forge <verb>`; `<N>` a ticket or PR number. "Replaces" lists the old `gh` commands.

### Identity, state, setup

| Operation | Verb | Stdout | Replaces |
|---|---|---|---|
| provider, repo | `provider` · `repo [--explicit]` | name · slug | `git remote get-url origin` parsing |
| is the run halted | `check` | nothing; 79 when halted | `super-board-github-read.py --check` |
| record an unknown write outcome | `halt <reason>` | nothing; records the halt, 79 | `super-board-github-read.py --halt` |
| clear a read halt | `resume` | nothing; 0 once the saved read passes and the halt file is archived; 79 when it still fails or the halt is not a replayable read | `super-board-github-read.py --resume` |
| who am I | `whoami` | `{login,id}` | `gh api user`, `gh auth status` |
| quota | `quota` | `{remaining,limit,reset,at,seen}`; GitHub adds `graphql` and `core` (`{limit,remaining,used,reset}`), `remaining` is the GraphQL bucket; Azure DevOps adds `cost`/`total_cost` when the service sends only a cost. `quota --throttle` / `--take-throttle` (Azure DevOps): `{throttled,throttled_at}` | `gh api rate_limit` (use `sb_gh_guard_check` / `sb_gh_guard_summary`, which are provider-aware, before a burst) |
| default branch, empty repo, base exists | `repository` | `{repository,defaultBranch,empty,base,baseExists}` (`defaultBranch` null when `empty`; `base` = config `base_branch`) | `gh repo view --json defaultBranchRef`, `gh api repos/{o}/{r}` |
| who may approve | `permission <login>` | `{permission}`: GitHub `admin\|maintain\|write\|triage\|read\|none`; Azure DevOps `approve` for `ado.approvers`, else `none` | `gh api repos/{o}/{r}/collaborators/<login>/permission` |

### Tickets

| Operation | Verb | Stdout | Replaces |
|---|---|---|---|
| read a ticket | `ticket view <N>` | `{number,title,body,state,stateReason,labels:[{name}],assignees:[{login}],url}` | `gh issue view N --json ...`, `gh api repos/{o}/{r}/issues/N` |
| list tickets | `ticket list [--state open\|closed\|all] [--label L]... [--limit N]` (default open, 30; every `--label` must match) | array of the same | `gh issue list --state --label --json ...`, `gh issue list -l source:qa` |
| open tickets with their last 8 comments (dependency graph) | `ticket graph` | `[{number,title,body,state,labels:{nodes:[{name}]},comments:{nodes:[{body}]}}]` | `gh api graphql` issues query |
| every ticket, any state (dedupe) | `ticket history` | `[{number,state,body}]` | `super-board-github-read.py --issue-history` |
| create | `ticket create --title T --body-file F [--label L]... [--column C]` | URL, then the number (last line). `--column` places the card (GitHub: needs `project.owner/number`; adds it to the project, then moves it). A failed placement still prints both, then exits 69 | `gh issue create`, `gh project item-add` + `card.sh move` |
| edit, label | `ticket edit <N> [--title T] [--body-file F] [--add-label L]... [--remove-label L]...` | nothing. Removing a label that is not there is fine | `gh issue edit`, `gh api POST/DELETE issues/N/labels` |
| close | `ticket close <N> --reason completed\|not-planned` | `closed #N (reason)`. Also moves the card to Done (GitHub: when `project` is configured; a failed move only warns) | `gh issue close`, `gh api PATCH issues/N -f state=closed -f state_reason=...` |
| comment | `ticket comment <N> --body-file F` | the comment id | `gh issue comment`, `gh api POST issues/N/comments` |
| read comments | `ticket comments <N>` | `[[{id,body,user:{login,type},created_at,updated_at}]]`, one page; Azure DevOps adds `version` | `gh issue view --json comments`, `gh api --paginate issues/N/comments` |
| edit a comment in place | `ticket comment-edit <N> <ID> --body-file F` | the id | `gh api -X PATCH issues/comments/ID` |
| claim | `ticket claim <N>` | `claimed #N`; 4 when someone else holds it (never steals) or the race was lost | `gh issue edit --add-assignee` + the re-read |
| release | `ticket release <N> [--force]` | `released #N (<who>)` or `released #N (nothing held)`; idempotent | `gh issue edit --remove-assignee`, `gh api DELETE issues/N/assignees` |
| read the claim | `ticket view <N>`: `assignees` non-empty = claimed | | `gh issue view --json assignees` |

**Claim.** GitHub: the claimant is `notifications.bot_identity`; `"auto"` or unset = the authenticated login
(`whoami`; an App installation token has no `/user`, so name the bot login). The verb reads the assignees, exits 4
unless none or only the bot is there, adds the bot, RE-READS and requires the list to be exactly `[bot]` (an add does
not fail when someone else holds the card: issues take up to 10 assignees), else removes its own assignee and exits 4.
`[bot]` suffixes and case are ignored when comparing. `--force` on `release` changes nothing on GitHub (a human
assignee is not a leaked claim). Azure DevOps: a per-holder tag, see below. Callers no longer skip the claim when
`bot_identity` is unset. `notifications.bot_identity` is GitHub-only: the Azure DevOps backend ignores it (the
claim is the holder tag, `assignees` shows the `whoami` login), `super-board-setup.py check` never flags it
missing or `"auto"` there, `super-board-status.py` shows the `whoami` login where GitHub shows the bot, and
`super-board-stop.sh` reads it only on its GitHub path.

### Board

| Operation | Verb | Stdout | Replaces |
|---|---|---|---|
| the whole board | `board items` | `{items:[{id,title,status,labels,content:{type,number,title,url,repository}}],totalCount}`; Azure DevOps adds `content.assignees` | `gh project item-list`, `card.sh items` |
| a card's column | `board status <N>` | column name; 3 not on the board | `card.sh status` |
| move a card | `board move <N> <Column>` | `moved #N → Column`; 3 unknown column | `card.sh move`, `gh project item-edit` |
| the card's id | `board item-id <N>` | the id (Azure DevOps: `N`) | `card.sh item-id` |
| ids | `board ids` | provider-specific shape (GitHub `{projectId,fieldId,options}`, Azure DevOps `{board,columnField,columns}`); no lane needs it | `card.sh ids` |
| make the board fit | `board ensure [--check]` | Azure DevOps only: one `changed:`/`needed:` line per change; 1 `--check` found changes; 65 refused | see "Setup-only" |

On GitHub the board verbs ARE `super-board-card.sh` (cached ids, 1 point a move); the forge hands it
`--owner/--number/--repo` from the config, never `SB_CONFIG`.

### Pull requests

| Operation | Verb | Stdout | Replaces |
|---|---|---|---|
| create | `pr create --base B --head H --title T --body-file F [--draft] [--label L]... [--closes <N>]...` | URL, then the number (last line). `--closes` links the ticket: GitHub appends `Closes #N` to the body when it has no closing keyword for it; Azure DevOps links the work item. A failed label still prints both, then 69 | `gh pr create`, `gh api POST pulls` |
| draft to ready | `pr ready <N>` | `ready #N`; idempotent | `gh pr ready` (GraphQL, no REST form) |
| the PR at a glance | `pr view <N>` | `{number,title,url,state,isDraft,headRefName,headRefOid,baseRefName,mergeCommit:{oid}\|null,labels:[{name}]}` | `gh pr view --json baseRefName,state,mergeCommit,headRefOid,...`, `gh api pulls/N` |
| branch and head | `pr head <N>` | `<branch> <40-hex sha>` | `gh pr view --json headRefName,headRefOid` |
| state | `pr state <N>` | `{state,headRefOid,mergeCommit}` (`mergeCommit` only once merged) | `gh pr view --json state,mergeCommit` |
| body, the head it belongs to | `pr body <N>` | `{headRefOid,body}` | `gh api pulls/N --jq .body` |
| write the body | `pr set-body <N> --body-file F` | nothing (use `super-board-pr-body.sh`, which rewrites one block) | `gh pr edit --body-file`, `gh api PATCH pulls/N` |
| files, counts, labels | `pr metadata <N>` | `{labels,files:[{path,additions,deletions}],additions,deletions,changedFiles,body}` | `gh pr view --json files,...`, `gh api pulls/N/files` |
| the diff | `pr diff <N> --meta <file>` (`--meta` from `pr metadata`, taken right before) | unified diff, validated against it | `gh pr diff` |
| CI and policy state | `pr checks <N>` | `[{name,status:pending\|pass\|fail\|skipped,required}]`; `[]` when there are none. GitHub: check runs AND commit statuses of the head, `required` = branch protection (1 GraphQL point); Azure DevOps: policy evaluations (`required` = blocking) | `gh pr checks`, `gh api commits/<sha>/check-runs` |
| open or merged PRs | `pr list --state open\|merged --limit N` | open `[{number,title,body,url,headRefName,files:[{path}]}]`; merged `[{number,title,body,url}]` | `gh pr list --json ...` |
| find a PR by branch | `pr refs --state open\|merged --limit N`, pick the `headRefName` | `[{number,headRefName,baseRefName,headRefOid}]` (merged rows only when they carry a head) | `gh pr list --search "head:..."`, `gh api pulls?head=` |
| recent merges | `pr merged --since ISO --limit N` | `[{number,title,url,mergedAt,author:{login,type}}]`, newest first | `gh pr list --state merged --json mergedAt,...` |
| tickets the PR closes | `pr links <N>` | `{closes:[numbers]}` | `closingIssuesReferences` |
| human approval inputs | `pr approval <N>` | `{state,headRefOid,closes}` (see "Human approval") | the approval GraphQL |
| merge | `pr merge <N> --squash --delete-branch [--subject S] [--body-file F] --match-head <sha>` | the provider's merge output (Azure DevOps: `merged #N as <sha>`); only through the merge gate | `gh pr merge` |

### Pull request comments, review notes and threads

| Operation | Verb | Stdout | Replaces |
|---|---|---|---|
| comment on the PR | `pr comment <N> --body-file F` | the comment id | `gh pr comment`, `gh api POST issues/N/comments` |
| read the conversation | `pr comments <N>` | `[[{id,body,user:{login,type},created_at,updated_at}]]`, oldest first, ids as strings. Azure DevOps lists file-thread comments here too (GitHub keeps them in `pr threads`); the Reviewer report is found by its marker | `gh pr view --json comments`, `gh api --paginate issues/N/comments` |
| edit a comment in place (the Reviewer report) | `pr comment-edit <N> <id> --body-file F` | the id | `gh api -X PATCH issues/comments/<id>` |
| one inline note | `pr note <N> --file <path> [--line L [--end-line M]] [--side left\|right] --body-file F [--head <sha>]` | the note's id. No `--line` = a whole-file note; `--line L --end-line M` = a range (L first, M last); `--side` default `right` (the new file), `left` = the deleted side. `--head` = the head you read: the PR moved, exit 6, nothing written | `gh api POST pulls/N/comments` (`subject_type`/`line`/`side`/`start_line`) |
| several notes at once | `pr notes <N> --notes-file F [--head <sha>]`, `F` = JSON `[{file,line?,end_line?,side?,body}]` | `[{index,id}]` (`id` null when written but not read back). GitHub: line notes in ONE review with `event: COMMENT`, whole-file notes as their own comments; Azure DevOps: one thread each | `gh api POST pulls/N/reviews` |
| edit a note in place | `pr note-edit <N> <id> --body-file F` | the id | `gh api -X PATCH pulls/comments/<id>` |
| read review threads | `pr threads <N>` | `[{id,path,status,resolved,line,startLine,side,outdated,comments:[{id,author:{login,type},body,createdAt}]}]`: file threads only on GitHub (`status` `active\|resolved`), every live thread on Azure DevOps (`path` null for a conversation thread; `status` its service word; `outdated` always false). `line` = the last line of the range, `startLine` only for a multi-line range, `side` `left\|right\|null` | the `reviewThreads` GraphQL, `gh api pulls/N/comments` |
| resolve a thread | `pr thread-resolve <N> <thread id>` | `resolved <id>` (GitHub `resolveReviewThread`; Azure DevOps status `fixed`) | `gh api graphql resolveReviewThread` |

Author notes ([pr-author-notes.md](pr-author-notes.md)): match existing notes by `path` and marker in `pr threads`,
only edit ones whose `author.login` is `whoami`'s login, `pr note-edit` the owned note, `pr note`/`pr notes` for new
ones with `--head` set, then read `pr threads` back. A note with an unknown outcome is read back, never replayed.

### Setup-only: stays provider-specific

Account administration is not lane work and is not wrapped: `gh auth login|status|refresh|switch`, the
second-account flow (`second-account.md`: collaborator invitations, `gh api users/<bot>`), `gh repo create|edit|delete`,
`gh project create|list|view|field-list|item-*` (onboarding), `gh label create|edit|delete`. Their Azure DevOps
counterparts are credentials (`AZURE_DEVOPS_EXT_PAT` or `az login`), the `ado` config block and `board ensure`.
`board ensure` has no GitHub form on the forge: onboarding runs `python3 .claude/bin/super-board-setup.py check|fix`
(the neutral entry: on Azure DevOps it runs `board ensure`; on GitHub the Status options and labels come from
`super-board-setup.py board-migrate --config <cfg> [--dry-run]`).

## Differences a caller may meet (everything else is identical)

- A ticket or PR that does not exist: Azure DevOps exits 3; GitHub's `Could not resolve to ...` is a permanent read
  error and halts (79). Do not probe numbers you did not get from a read.
- `ticket claim` on Azure DevOps is a tag, so two claimers on the SAME machine and checkout are not separated by it
  (the local inflight lockfiles cover that); on GitHub the assignee is the lock.
- Azure DevOps `pr metadata`/`pr diff` come from the local clone (no server diff) and serve open PRs only; a body
  over 4,000 characters overflows into a thread (see below).
- `pr checks`' `required` is branch protection on GitHub and blocking policy on Azure DevOps.
- Drafts: `pr create --draft` then `pr ready` on both. GitHub will not merge a draft; Azure DevOps refuses with 65.
- GitHub-only: the GraphQL point budget (price list in `rate-limit-etiquette.md`; `sb_gh_guard_check`), `gh` auth
  scopes, secondary rate limits. Azure DevOps-only: tags as claims, the 4,000-character description, pinned-commit
  completion, 429 throttling. Both stay behind the guard functions, which are provider-aware.


## Azure DevOps backend

Code: `super-board-forge-ado.py` (mixed into the forge's `AzureDevOpsBackend`). stdlib `urllib`
only. Tracker verbs: ticket, board, comment and claim; the verb table above is the contract, this is how the service
answers it. The pull-request verbs are in `super-board-forge-ado-pr.py` (see "Azure DevOps pull-request backend").

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
2. a line `AZURE_DEVOPS_EXT_PAT=...` in `<repo.path>/.env`, else in `<repo.path>/.env.local` (lane agents load no
   dotenv file; the first non-empty line wins). `super-board-env-check.sh AZURE_DEVOPS_EXT_PAT` (no `--file`) looks
   in `.env.local` then `.env` and says `present` for either, and `super-board-setup.py check` uses the same two
   files, so the three agree; name one file with `--file` to check only it.
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


### Service notes per verb

- `board ids` caches `{board, columnField, columns}`. `board items` runs WIQL for the board's types, reads in batches and
  takes the status from the board's own column field; cards with no column are skipped. `board move` writes the column
  field (an unknown column refreshes the cache once, then exits 3).
- `board ensure [--check]` prints one `changed:` (or `needed:`) line per change, else `board ok: ...`; exits 1 when
  `--check` found changes and 65 when it refused. See below.
- `ticket view/list`: `state` is the state category (Completed or Removed = CLOSED; Removed or tag `not-planned` =
  `NOT_PLANNED`, else `COMPLETED`); a claim tag shows as `assignees: [{login}]` (the `whoami` login) and is hidden from `labels`.
  `ticket list --label L` filters on the tags (every label must be there). `ticket graph` makes one comments call per
  open ticket: the cost driver. `ticket history` is every state.
- `ticket create` makes the work item type from `work_item_types` by label (a Bug body goes in Repro Steps), sets the
  body as Markdown and tags = labels. When the ticket was created but the move into `--column` failed, the URL and
  number are STILL printed (then a non-zero exit) so a filer can report "filed, not placed".
- `ticket edit`: `--add-label` is an `add` of only the missing tags (the service merges, no rev test); removing or setting is
  read, then `[test /rev, replace <exact list>]` (`remove` when empty), retried on a stale rev.
- `ticket close --reason not-planned` adds the tag `not-planned`, then moves the card to Done.
- `ticket comment` / `comments`: work item comments (Markdown); `comments` carries `version` (1 = never edited) and
  `created_at`/`updated_at` = `createdDate`/`modifiedDate`.
- `ticket claim` reads; a claim tag that is not ours: exit 4; ours: exit 0 (idempotent); else `add` ours, re-read and
  verify. If another holder's tag landed too, the lexicographically smallest claim tag wins and the loser takes its own tag
  back (rev-tested replace). `ticket release` removes OUR claim tag only; `--force` removes every claim tag (crash-recovery
  sweep). Idempotent.
- `whoami` = `connectionData`'s authenticated user; `permission` is `approve` for a unique name in `ado.approvers`
  (case-insensitive), else `none`, with no call (66 when `ado.approvers` is not a list of names).
- `quota`: the saved `ado-quota.json` plus `seen`; `--throttle` / `--take-throttle` read the 429s the transport recorded
  (`ado-throttle.json`); `--take-throttle` also resets the count, so a caller asking once per wave sees only that wave's.
- `repository`: `empty` = no branch at all (a NEW Azure Repos repository has none and a PR cannot be opened); `base` =
  config `base_branch`, `baseExists` whether it exists (null when unset). Exit 79 when the repository is not found.

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
one work item is exit 3. A read that halts records what it takes to replay it in the halt file's `operation`:
`{provider: "azure-devops", method, path (under the organization; no host, no credentials), body (a POST read's
JSON), validate (the validator's name), fixed, what}`. Writes are never retried, except a JSON-Patch whose `/rev` test the server
rejected as stale (409, 412, or a body naming VS403351 / a revision mismatch): it did not apply, so
re-read and retry, max 3. Halt messages say Azure DevOps.

**Recovery: `forge resume`.** It replays the saved read with the same validator and, only when that passes,
archives the halt file by renaming it `github-halt-<timestamp>.json` (never deleting it). A read that still fails
leaves the halt in place (79; a 404 on a non-fixed read is 3). A halt with no replayable read (`forge halt <reason>`,
an unresolved write outcome, or a record from an older pack) is refused with 79 on BOTH providers: reconcile the
remote outcome by hand, then archive the file yourself. On GitHub `resume` runs
`super-board-github-read.py --resume` with the same arguments, so `resume --kind <same-shape> [--meta F] -- <gh read
args>` still supplies a corrected replacement read; Azure DevOps takes no arguments.

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

### Service notes per verb

- `pr head`: `sourceRefName` without `refs/heads/` and `lastMergeSourceCommit`; `pr state`: `active`=OPEN,
  `completed`=MERGED, `abandoned`=CLOSED, `mergeCommit:{oid}` only when completed (69 when completed without one).
  `pr view` adds the title, URL, draft flag, base branch and the labels (from the labels endpoint).
- `pr metadata` / `pr diff`: local git (see "No server-side diff"); 69 on a fetch/head mismatch or a failed validation, 64 `pr diff` without `--meta`.
- `pr set-body`: 65 when still over 4,000 after the overflow, nothing written. `pr create`: 65 body too long, nothing created.
- `pr merge`: see "Merge: pinned commit, then poll"; exit 3 when not found.
- `pr list`: `files` come from the last iteration's changes, without the leading `/`. `pr refs`: ONE list call (`pr list` also
  reads every open PR's changed files); `merged` rows are only `completed` PRs that carry a recorded source head
  (`lastMergeSourceCommit`), anything else is left out rather than guessed. Callers: the cleanup hook, stop.
  `pr merged`: completed PRs with `closedDate` at or after `since`, newest first; `type` is `Bot` for a service identity
  (`[bot]`, `svc.` prefix, "Build Service"); 64 when `--since` is not ISO-8601. Caller: `collect_prs.py`.
- `pr links`: the PR's linked work items (never text parsing); `pr approval` = `pr state`'s state and head plus them.
- `pr comment` prints `<threadId>.<commentId>`; `pr comments` leaves out system comments, deleted ones and the overflow thread
  (`created_at` = `publishedDate`; `updated_at` = `lastContentUpdatedDate`, else `lastUpdatedDate`, which also moves when the
  text did not change); `pr comment-edit` takes that id (64 for a bad one).
- `pr threads`: every live thread; `path` = `threadContext.filePath` without the leading `/`; `resolved` is true for `fixed`,
  `closed`, `wontFix`, `byDesign`; `line`/`startLine`/`side` come from `rightFileStart/End` (or `leftFile...`).
- `pr note`: a thread with `threadContext` = `{filePath: "/<path>", rightFileStart: {line, offset: 1}, rightFileEnd: {line, offset: 1}}`
  (`leftFile...` for `--side left`; the path only for a whole-file note), status active. `pr notes` posts one thread each
  in order; when one fails it names the ones already written. `--head` is checked against the PR's current source commit first.
  `pr note-edit` is `pr comment-edit`. `pr thread-resolve` is `PATCH .../threads/{id}` `{"status":"fixed"}`.
- `pr checks`: `GET {project}/_apis/policy/evaluations?artifactId=vstfs:///CodeReview/CodeReviewId/{projectId}/{pullRequestId}&api-version=7.1-preview.1`
  (`projectId` from the PR's `repository.project.id`, else the project lookup). `status` queued|running = pending, approved =
  pass, rejected|broken = fail, notApplicable = skipped; `name` = the policy type (`<type>: <display name>` when it has one);
  `required` = `configuration.isBlocking`. No policies: `[]`.

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

## Ticket filers on Azure DevOps

`super-qa-file-bug.sh`, `super-review-file-refactor.sh` and `super-collect-file.sh` take the provider from
`--config` (the collect and review filers already did) or `$SB_CONFIG`; no config means GitHub, whose path is
unchanged. On `azure-devops` they never call `gh` and the GitHub knobs (`SUPER_QA_PROJECT_OWNER/TITLE`,
`SUPER_QA_TARGET_OPTION_NAME`, `SUPER_QA_REPO`) are ignored. The QA filer also takes `--column` (default
`Ready`, as the GitHub fallback chain lands a bug there on a seven-column board); the collect router passes
`Backlog`; the review filer picks Ready with acceptance criteria, else Backlog.

**Fingerprint evidence is visible text.** Azure DevOps strips HTML comments from a work item body and comment on
save, so `<!-- super-qa-meta ... -->`, `<!-- super-review-fingerprint: ... -->` and the collect comments cannot
carry anything. Instead each filer ends the body with a rule and plain lines:

- QA: a bullet list `- route: ...`, `- spec`, `- iteration`, `- area`, `- category`, `- priority`, `- type` and
  `- fingerprint: <fp>`;
- review: `super-review-fingerprint: <fp>` on its own line;
- collect (added above the child filer's own): `- super-collect-fingerprint: <fp>` and `- super-collect-source: <s>`.

The dedupe is the SAME match as GitHub: a whitespace-delimited token equal to the fingerprint in any ticket body,
read from `forge ticket history` (`[{number,state,body}]`, every state, body already decoded: the service stores
`&` and `"` as entities and the forge reverses that, so a fingerprint with those characters still matches). An
open hit gets a "Seen again" comment (`forge ticket comment`) and no new ticket; a closed-only hit is a
recurrence (collect). Why it is safe: nothing depends on hidden text; a light edit of the prose leaves the footer
line alone; the token boundary rule means `err|x|441` never hits `err|x|4411`; and if a person deletes the line
the worst case is one duplicate ticket, never a lost finding or a wrongly merged one. A ticket is on the board as
soon as it exists, so `forge ticket create --column` is the only placement step. Exit codes are the filers' own:
`71` filed but not placed (the number is still the last stdout line), `70` the column does not exist (nothing was
filed), `79` an unknown outcome (the run is halted).

## Rate limits on Azure DevOps

There is no quota endpoint; the transport saves `X-RateLimit-*` headers (`ado-quota.json`) and records every 429
(`ado-throttle.json`) and waits `Retry-After` itself. Consequently:

- `super-board-gh-guard.sh` (provider from `$SB_CONFIG`): `sb_gh_guard_check` never calls `gh` and never blocks
  without a reading; it halts on the shared halt file (79) and sleeps only when the saved `remaining` is at or
  below `SB_ADO_GUARD_MIN_REMAINING` (5) with `reset` (epoch seconds) in the future, at most 300 s. The per-worker
  call budget is unchanged. `sb_gh_guard_summary` prints `[gh-guard] azure-devops remaining=<n>/<limit> cost=<total>
  throttled=<429s>`; a worker appends the part after the tag to its PR handoff comment as
  `gh-quota-on-exit: azure-devops remaining=<n>/<limit> cost=<total> throttled=<429s>` (`?` for what the service
  never sent).
- `super-board-throttle.sh`: "the wave hit the hourly limit" means the service answered 429 during the wave, or a
  lane result names one (`HTTP 429`, `Azure DevOps throttled`, `too many requests`). It reads and resets the count
  with `forge quota --take-throttle` (`--dry-run` only reads it) and steps `max_workers` down on that and nothing else.

## Other scripts on Azure DevOps

- `super-board-stop.sh`: `forge pr refs` finds the PR of `issue-<N>-*`, `ticket comment` / `pr comment` leave the
  wrap-up, `ticket release` drops our claim (and `--force` when a claim tag that is not ours is still there),
  `ticket edit --remove-label` drops the `loop:*` labels.
- `super-board-status.py` loads the Azure DevOps backend in-process (no bash, no jq, no subprocess); a status
  read neither writes nor honours the halt file; a blocked card's reason is its newest comment.
- `hooks/cleanup-wt.py` asks the forge (`pr refs`, found in `.claude/bin` / `.agents/bin`, or `CLEANUP_WT_FORGE`).
  A PR counts as merged only when it is `completed` AND its recorded source head equals the local branch tip;
  an unreadable list keeps every remote branch and says so. It runs with a throwaway halt file.
- `super-board-setup.py check|fix`: `gh` is not required, a token is; the `ado` block is validated; the board is
  read with `board ensure --check` and set up by `fix`; an empty repository is a must-fix with the exact git
  commands; the routing labels are tags and need no creation. `check --config <file>` validates ONE config, saved or
  staged (what onboard runs before step 8 saves it): the `ado` block, token source, board shape and
  repository/base branch on Azure DevOps; verification readiness, host and old keys on GitHub.
- `super-board-skills-ready.py --hydrate --config <cfg>` reads a card's body and labels with `ticket view` on any
  non-GitHub provider; without `--config` (or on GitHub) it keeps the guarded `gh` reads. The legacy
  `super-build-dispatch.sh` does the same when `SB_CONFIG` names a config.
- `skills/super-collect/scripts/collect_prs.py`: `pr merged` + `pr threads`. The `github` source's open-issue read
  is `ticket list --state open --limit N`.
- `super-board-run.sh` (legacy `claude-p`) exits 78 on any provider but GitHub.


## GitHub backend

Code: the original verbs in `super-board-forge.py`, the lane verbs in `super-board-forge-github.py`. Reads use the read
helper's kinds (`issue`, `issues-closed`, `tickets`, `pr-view`, `refs`, `threads`, `checks`, `comments`, `approval`,
`permission`, `body`, `metadata`, `diff`, `merge-state`, `head`, `prs-open`, `prs-merged`, `issues`, `issue-history`, `quota`,
`object`, `array`, `scalar`); a payload of the wrong shape three times is exit 64 (the caller's malformed read), a failing
`gh` three times is the halt. Verbs add `--repo` to `gh` only for a repo given by `--repo` or the config, so a bare checkout
still lets `gh` pick its own; the REST verbs set `GH_REPO` for the `{owner}/{repo}` placeholders instead.

| Verb | `gh` it runs |
|---|---|
| `pr head` | `pr view N --json headRefName,headRefOid -q '.headRefName + " " + .headRefOid'` (read, `head`) |
| `pr state` | `pr view N --json state,mergeCommit,headRefOid` (`merge-state`) |
| `pr metadata` | `pr view N --json labels,files,additions,deletions,changedFiles,body` (`metadata`) |
| `pr diff N --meta F` | `pr diff N` (`diff`, checked against the metadata file) |
| `pr body` / `pr set-body` | REST `api repos/{o}/{r}/pulls/N --jq '{headRefOid: .head.sha, body: (.body // "")}'` / `api -X PATCH repos/{o}/{r}/pulls/N --input -` with stdin `{"body": ...}` |
| `pr merge` | `pr merge N --squash --delete-branch [--subject S] [--body-file F] --match-head-commit sha` (a `gh` exit of 5, 6, 7 or 65 is normalised to 1: those codes cannot come from GitHub) |
| `pr list --state open\|merged` | `pr list --state S --limit N --json number,title,body,url[,headRefName,files]` |
| `pr view` | REST `api repos/{o}/{r}/pulls/N --jq <object>` (`pr-view`) |
| `pr refs` / `pr merged` | `pr list --state S --limit N --json number,headRefName,baseRefName,headRefOid` / `--json number,title,url,mergedAt,author` |
| `pr links` / `pr approval` | `api graphql --paginate --slurp` approval query (`closingIssuesReferences`, plus the body's closing keywords when the base is not the default branch; incomplete link pages or a snapshot that changed between pages fail) |
| `pr create` | REST `api -X POST repos/{o}/{r}/pulls --input -` `{title,head,base,body,draft}`, then `POST issues/N/labels` |
| `pr ready` | `pr ready N` |
| `pr comment` / `pr comments` / `pr comment-edit` | REST `POST issues/N/comments` / `GET --paginate issues/N/comments?per_page=100` / `PATCH issues/comments/ID` |
| `pr note` | REST `POST pulls/N/comments` `{body,commit_id,path,line,side[,start_line,start_side]}`; no line: `subject_type: "file"` |
| `pr notes` | one `POST pulls/N/reviews` `{commit_id,event:"COMMENT",comments:[...]}` for the line notes, `GET pulls/N/reviews/ID/comments` to read the ids back; whole-file notes as `POST pulls/N/comments` |
| `pr note-edit` | REST `PATCH pulls/comments/ID` |
| `pr threads` | `api graphql --paginate --slurp` `reviewThreads(first:100)` with `comments(first:100)` (≈1 point; a thread with more than 100 comments is cut) |
| `pr checks` | `api graphql --paginate --slurp` `commits(last:1){statusCheckRollup{contexts(first:100){... isRequired(pullRequestNumber:N)}}}` |
| `pr thread-resolve` | `api graphql -f query=mutation{resolveReviewThread} -f threadId=ID` (a write, once) |
| `ticket view` | `issue view N --json number,title,body,state,stateReason,labels,assignees,url` (`issue`; 1 point, kept so callers' `gh issue view` stubs still match) |
| `ticket list` | `issue list --state S --limit N [--label L]... --json <same fields>` (`issues-closed` for closed, else `tickets`) |
| `ticket graph` / `ticket history` | `api graphql --paginate --slurp` open issues with their last 8 comments / all issues, `number state body` |
| `ticket create` | REST `POST repos/{o}/{r}/issues --input -` `{title,body,labels}`; `--column`: `board ids` first (exit 3 when unknown, nothing created), then `project item-add N --owner O --url U` and `board move` |
| `ticket edit` | REST `PATCH issues/N` `{title,body}`, `POST issues/N/labels` `{labels}`, `DELETE issues/N/labels/<label>` (a 404 is fine) |
| `ticket close` | REST `PATCH issues/N` `{state:"closed",state_reason:"completed"\|"not_planned"}`, then `board move N Done` when `project` is set (a failure only warns) |
| `ticket comment` / `comment-edit` | REST `POST issues/N/comments` `{body}` / `PATCH issues/comments/ID` |
| `ticket comments` | `api --paginate --slurp repos/{o}/{r}/issues/N/comments?per_page=100` (`comments`) |
| `ticket claim` / `release` | REST `GET issues/N` assignees, `POST issues/N/assignees` `{assignees:[bot]}` and the re-read / `DELETE issues/N/assignees` |
| `whoami` / `quota` / `repository` | `api user` / `api rate_limit` plus GraphQL's own `rateLimit` (REST misreports that bucket) / `api repos/{o}/{r}`, `branches?per_page=1`, `git/matching-refs/heads/<base>` |
| `permission` | `api repos/{o}/{r}/collaborators/<login>/permission` (`null` for a string that is not a login: no call) |
| `board ...` | `super-board-card.sh` (`--owner/--number/--repo` from the config) |
