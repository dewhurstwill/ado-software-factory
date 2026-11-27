<div align="center">

<img src="docs/assets/logo.svg" width="88" height="88" alt="super-board kanban logo">

<h1>super-board</h1>

<p><strong>Add tasks, walk away, get merged PRs with proof.</strong></p>

<p>Stop babysitting coding agents: cards you drop in Ready come back as tested, reviewed PRs.</p>

<p>9 skills (5 you type, 4 the board runs) · 9 commands · 6 guard hooks</p>

<p>
<img alt="Version" src="https://img.shields.io/badge/version-3.3.0-1f883d?style=flat-square">
<img alt="Host" src="https://img.shields.io/badge/Claude%20Code-skills-d97757?style=flat-square">
<img alt="License" src="https://img.shields.io/badge/license-MIT-0969da?style=flat-square">
</p>

<p><a href="https://erictechpro.github.io/super-board/"><strong>🌐 Live site: erictechpro.github.io/super-board</strong></a></p>

<p>⭐ Star this repo if it helps · <a href="https://www.skool.com/erictech">Join the builders using this → weekly live calls, setup help</a> · <a href="https://youtu.be/aggJvNZxfKA">▶ Watch the walkthrough</a></p>

<p><a href="#install">Install</a> · <a href="#quick-start">Quick start</a> · <a href="#how-it-works">How it works</a> · <a href="#skills">Skills</a> · <a href="#commands">Commands</a> · <a href="#guards--hooks">Guards</a></p>

</div>

<p align="center"><a href="https://erictechpro.github.io/super-board/skill-map/"><img src="docs/assets/skill-map.png" alt="The super-board skill map: open the interactive version" width="100%"></a></p>
<p align="center"><sub>The skill map. Click it for the live version: zoom, pan, open any node.</sub></p>

## Install

Before you start:

- [ ] Claude Code, and `gh` with board access: `gh auth refresh -s project,read:project,repo`
- [ ] Dynamic workflows on in `/config` ([backends](docs/super-board/README.md#backends))
- [ ] `verify_commands` set in your config, so the merge gate can prove the build ([setup notes](docs/super-board/README.md#setup-notes))

In your project folder:

```bash
curl -fsSL https://raw.githubusercontent.com/EricTechPro/super-board/main/get.sh | bash
```

Or as a Claude Code plugin:

```
/plugin marketplace add EricTechPro/super-board
/plugin install super-board@super-board
```

> [!NOTE]
> The plugin ships the skills only. Run `/super-board:super-board onboard` and its 🔍 Checks step adds the guard hooks, scripts and workflows. Needs Claude Code, `gh`, `jq`, bash 3.2+ and Python 3.9+; the installer checks.

### Providers: GitHub or Azure DevOps

The board, tickets and pull requests live on one provider per project, set by `provider` in the
project config. Absent means `github`, so existing installs do not change.

| | GitHub (default) | Azure DevOps |
|---|---|---|
| Tickets | Issues | Azure Boards work items (User Story, Bug) |
| Board | Projects v2, Status field | One team's Kanban board, seven columns |
| Labels | Labels | Tags |
| Claim | Assignee | Tag `sb:claimed-<holder>` |
| Pull requests | GitHub PRs | Azure Repos PRs |
| Sign-in | `gh auth login` | A PAT (`AZURE_DEVOPS_EXT_PAT`, in the environment or the project's `.env`) or `az login` |

Azure DevOps support is being added in this fork and is not complete:

| Part | Azure DevOps status |
|---|---|
| Board and tickets: read, move, create, label, comment, claim, wave planning | Works, checked against a live organisation |
| Board setup (`board ensure`: seven columns, bugs on the board) | Works, checked live |
| Pull requests: open, comment, read the diff, rewrite PR body blocks | Works, checked live |
| Merge gate: pinned to the reviewed commit, squash, branch policies respected | Works, checked live |
| Approving a held merge with a `done` comment | Works, checked live. Only sign-ins listed in `ado.approvers` count |
| Status view, ticket filers (bug, refactor, collect), stop, cleanup hook, rate-limit guard | Works; status and bug filer checked live |
| Onboarding checks (`super-board-setup.py`): token, board shape, empty repository | Works against the test server; not yet run live |
| Skill instructions (what lane agents are told to run) and onboarding questions | One provider-neutral set: every lane step is a forge verb (`tests/test_prose_provider_neutral.py` guards it); onboarding has an Azure DevOps path. Not yet run end to end by a live lane agent |

To try it, install from this fork and add the block below to
`.claude/super-board/configs/<slug>.json`:

```bash
curl -fsSL https://raw.githubusercontent.com/dewhurstwill/ado-software-factory/main/get.sh | SUPER_BOARD_REPO=dewhurstwill/ado-software-factory bash
```

```json
{
  "provider": "azure-devops",
  "ado": {
    "organization": "https://dev.azure.com/<org>",
    "project": "<project>",
    "team": "<project> Team",
    "board": "Stories",
    "repository": "<repo>",
    "approvers": ["you@example.com"]
  }
}
```

`approvers` lists who may release a merge the gate holds (money, auth, schema, big diffs) by
replying `done`; leave it empty and a person merges held PRs in Azure DevOps instead.

The PAT needs **Work Items: Read & write** and **Code: Read & write**, and its owner must be a
team administrator to set up the board columns. The Agile, Scrum and Basic processes are
supported through the board's own column-to-state mapping; only Agile has been run live.
An Azure DevOps PR description holds 4,000 characters, so a long PR body keeps its history and
screenshots in the PR's first comment thread. A branch policy that needs a reviewer or a build is
respected: the gate reports the PR as waiting for a person instead of merging.
Verb contract, mapping and the service behaviour this relies on:
[providers.md](skills/super-board/references/providers.md).

### Platforms: macOS, Linux, Windows

| Platform | Shell | Status |
|---|---|---|
| macOS | bash 3.2+ | Every offline suite runs in CI |
| Linux | bash | Every offline suite runs in CI |
| Windows | Git Bash (the shell Claude Code uses on Windows) | CI job added and advisory; not yet green |

Windows needs Git for Windows, Python 3.9+, `jq` and Node.js on `PATH`. WSL counts as Linux.

### Security

Two security skills come with every install, and they stay separate:

| Skill | Runs | Covers |
|---|---|---|
| [`pr-security-review`](skills/pr-security-review/README.md) | **On every PR**, inside the QA lane. Each finding fails QA and goes back to Build. | The PR's diff against its base: high-confidence, exploitable bugs only. From Anthropic's [claude-code-security-review](https://github.com/anthropics/claude-code-security-review). |
| `security-audit` | **Only when you ask** (`/security-audit`). The board never runs it. | The whole repository, in depth. From Cloudflare's [security-audit-skill](https://github.com/cloudflare/security-audit-skill), fetched at install with `npx skills`. |

`--no-helper-skills` skips the `security-audit` download; `/super-board onboard` adds it later.
Claude Code's built-in `/security-review` is left as it is.

Running from Codex or OpenCode? See [supported hosts](docs/hosts.md) for tested versions, sign-in and gaps.

Optional post-run learning can delegate an already installed `calibrate` skill after
verified selected work completes. It applies only to explicitly permitted existing
canonical skill files; the package bundles no private skill. See
[post-run calibration](skills/super-board/references/calibration.md).

## Quick start

### 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/EricTechPro/super-board/main/get.sh | bash
```

### 2. Onboard

```
/super-board onboard
```

Checks · GitHub · Board · Branch · AGENTS.md · Policies · Bug sources · Review. <kbd>Enter</kbd> takes the recommended answer.

### 3. Run

```
/super-board run my-app
```

Add cards to `Ready`. The board drains on its own.

## How it works

**Which path?** Clear idea, foggy idea or bugs: [the routing rule](docs/super-board/routing.md) says what to run, and when 1–5 tickets go to `/implement-spec` instead of the board.

<p align="center"><a href="https://youtu.be/aggJvNZxfKA"><img src="https://i.ytimg.com/vi/aggJvNZxfKA/maxresdefault.jpg" alt="Watch I Built a Self-Improving Software Factory by Eric Tech on YouTube" width="100%"></a></p>
<p align="center"><a href="https://youtu.be/aggJvNZxfKA"><strong>▶ Watch on YouTube: I Built a Self-Improving Software Factory</strong></a></p>

## Skills

**9 skills (5 you type, 4 the board runs).** Uses [Matt Pocock skills](https://github.com/mattpocock/skills), ponytail, impeccable, humanizer, [Anthropic's security review](https://github.com/anthropics/claude-code-security-review) and [Cloudflare's security-audit](https://github.com/cloudflare/security-audit-skill).

<!-- skills:start -->
**You type — 5 skills**

| Skill | What it does |
|---|---|
| [`/super-board`](skills/super-board/README.md) | Orchestrator: plans waves, runs Build → QA → Review, merges when green. |
| [`/super-collect`](skills/super-collect/README.md) | Finds problems, verifies them, writes a findings doc for /to-spec and /to-tickets. |
| [`/ui-refine-loop`](skills/ui-refine-loop/README.md) | Critique → refine loop that polishes one page or component. |
| [`/visual`](skills/visual/README.md) | One HTML page: branch recap, plan, or codebase map with diagrams. |
| [`/git-sync`](skills/git-sync/README.md) | Commit per logical change, merge-pull, push without force, report. |

**The board runs — 4 skills**

| Skill | What it does |
|---|---|
| [`/super-build`](skills/super-build/README.md) | Builder lane: worktree, smallest safe change, tests, draft PR. |
| [`/super-qa`](skills/super-qa/README.md) | Tester lane: evidence, test-gap check, screenshots, or bounce to Build. |
| [`/super-review`](skills/super-review/README.md) | Reviewer lane: own hypotheses, remembers prior findings, merge gate. |
| [`/pr-security-review`](skills/pr-security-review/README.md) | Security pass on every PR diff; super-qa runs it, findings fail QA. |
<!-- skills:end -->

Each name links to that skill's README. Adding a skill? Write its `SKILL.md` and `README.md`, add a line to `skills/families.json`, and this table updates itself.

## Commands

```
/super-board onboard          # 8-step setup, resumes
/super-board lint             # vague ACs?
/super-board status           # read-only snapshot
/super-board run <slug>       # drain the board
/super-board stop             # stop workers
/super-collect [source]       # find problems → findings doc → /to-spec
/ui-refine-loop <route>       # polish one page
/visual [recap|plan|path]     # one HTML page
/git-sync                     # commit, pull, push
```

Flags: `run --low|--high` · `run --codex[=<model>]` · `super-collect --since 30d`

## Guards & hooks

Run automatically once installed. Python stdlib, JSON in, JSON out.

| | |
| --- | --- |
| [guard-worktree-path](hooks/guard-worktree-path.py) | Blocks `git worktree add` outside `.claude/worktrees/` |
| [guard-secrets](hooks/guard-secrets.py) | Blocks reading or piping dotenv files, SSH keys and credential files; key names via `super-board-env-check.sh` |
| [guard-key-literals](hooks/guard-key-literals.py) | Blocks a live-looking API key written into a file; flags one already there |
| [guard-delete-outside](hooks/guard-delete-outside.py) | Blocks `rm`, `find -delete` and `git clean` aimed outside the project, `~` or `/` |
| [guard-protected-push](hooks/guard-protected-push.py) | Opt-in (`onboard` asks, or `install.sh --protect-main`): blocks direct and force pushes to main/master/base |
| [README sync](scripts/super-board-readme-sync.py) | Regenerates the skill table; pre-commit and PostToolUse hooks keep it fresh |
| [cleanup-wt](hooks/cleanup-wt.py) | Removes merged worktrees and branches after each merge and at session start, with a recovery file |
| [merge gate](scripts/super-board-merge-gate.sh) | Merges only after the current base plus your non-empty project `verify_commands` pass (missing checks halt setup), pinned to the reviewed commit; applies `merge_policy` and runs allowed DB migrations |

## Models

A cheap router grades each card easy, medium or hard first; the run flag picks the model for each grade. With `--codex`, every lane runs on Codex GPT models instead of Claude.

| Command | Router | Easy card | Medium card | Hard card |
| --- | --- | --- | --- | --- |
| `/super-board run` | Haiku 4.5 | Sonnet 5.5 | Opus 5.5 | session model |
| `/super-board run --low` | Haiku 4.5 | Haiku 4.5 | Sonnet 5.5 | Opus 5.5 |
| `/super-board run --high` | Sonnet 5.5 | Opus 5.5 | Opus 5.5 | Opus 5.5 |
| `/super-board run --codex` | gpt-6-luna | gpt-6.1-sol | gpt-6.1-sol | gpt-6-astra |
| `/super-board run --codex --low` | gpt-6-luna | gpt-6-luna | gpt-6.1-sol | gpt-6.1-sol |
| `/super-board run --codex --high` | gpt-6-luna | gpt-6-astra | gpt-6-astra | gpt-6-astra |

`/super-board run --codex=<model>` pins one model for every card and skips the router.

Run inside Codex? plain `/super-board run` switches to the Codex ladder by itself. Codex has no
`/super-board` command: it finds skills in `.agents/skills/` (symlinks followed), so link
`.claude/skills/super-board` there and type `$super-board run`.

## Writing standard

Commits, tickets, PR bodies and comments all follow [one writing standard](https://erictechpro.github.io/super-board/writing-standard/), in the pack and in every project it is installed into.

<details>
<summary><strong>Upgrading from 2.x</strong></summary>

1. Re-run the install line.
2. Run `/super-board onboard`; step 1 upgrades for you and keeps a backup.
3. Try `board-migrate --dry-run` on a copy of your board first.

[Full notes](RELEASE-NOTES.md#upgrading-from-2x)

</details>

## Credits

MIT © Eric Tech ([LICENSE](LICENSE)). Built on [obra/superpowers](https://github.com/obra/superpowers), [mattpocock/skills](https://github.com/mattpocock/skills), [BuilderIO/skills](https://github.com/BuilderIO/skills) and [tt-a1i/archify](https://github.com/tt-a1i/archify). `pr-security-review` is ported from [anthropics/claude-code-security-review](https://github.com/anthropics/claude-code-security-review) (MIT © 2025 Anthropic, [notice](skills/pr-security-review/LICENSE)); `security-audit` is [cloudflare/security-audit-skill](https://github.com/cloudflare/security-audit-skill) (MIT © 2025-2026 Cloudflare, Inc.), installed from its own repo, not bundled.

---

<p align="center">
<a href="https://erictechpro.github.io/super-board/">Live site</a> ·
<a href="https://erictechpro.github.io/super-board/skill-map/">Skill map</a> ·
<a href="https://erictechpro.github.io/super-board/writing-standard/">Writing standard</a> ·
<a href="https://erictechpro.github.io/super-board/onboarding/">Setup simulator</a> ·
<a href="https://erictechpro.github.io/super-board/impeccable/">Impeccable map</a> ·
<a href="https://erictechpro.github.io/super-board/pstack/">pstack map</a> ·
<a href="https://erictechpro.github.io/super-board/security-audit/">Security audit map</a> ·
<a href="RELEASE-NOTES.md">Release notes</a> ·
<a href="RELEASING.md">Release checks</a> ·
<a href="https://youtu.be/nX_bGyIOFM4">YouTube walkthrough</a>
</p>
<p align="center"><sub>Made by Eric Tech</sub></p>

### Worker skill availability

Setup and dispatch resolve real, nonempty `SKILL.md` files for the actual
Claude or Codex host. Missing required skills halt before claims or workers;
lockfiles and arbitrary plugin caches do not count. The shared registry in
`scripts/super-board-skills-ready.py` owns lane dependencies, overrides and the
two declared ponytail fallbacks. The QA lane requires the bundled
`pr-security-review`; Cloudflare's `security-audit` is an optional QA entry,
so a missing copy never halts a run. Setup's full helper union may be red while a
selected card is runnable. Read-only checks never install; explicit onboard
repair rechecks files after installation.

The CLI accepts `--home <fixture-home>` and `--catalog <active-skills.json>`.
Launchers accept the same inventory through `SUPER_BOARD_SKILLS_HOME` and
`SUPER_BOARD_SKILLS_CATALOG`; neither changes the actual host. Catalog shape:
`{"host":"codex","skills":[{"name":"vendor:skill","path":"/exact/SKILL.md",
"enabled":true,"scope":"project","project_root":"/exact/project"}]}`.
User entries use `scope:"user"`. Restart explicitly after repairing missing
skills; readiness certifies availability, not their content or external tools.
