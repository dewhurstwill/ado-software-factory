#!/usr/bin/env python3
"""super-board-forge.py: one provider-neutral command for tickets, PRs and merges.

Scripts call neutral verbs here instead of `gh`. A backend per provider answers
each verb in the SAME JSON/text shapes the GitHub path always produced, so the
callers (merge policy, jq in wave-plan/deps, validators) do not care which
provider is behind it. Full contract: skills/super-board/references/providers.md.

    super-board-forge.py [--config <file>] [--repo <slug>] <verb> [args]

Provider: --config <file>, else $SB_CONFIG; key "provider" is "github" (default
when the key or the config is absent) or "azure-devops". The config is read once
(it may be a FIFO). Repo: --repo, else config repo.remote, else `git remote get-url
origin`; GitHub strips https://github.com/, git@github.com: and a trailing .git
to give owner/name. Verbs add `--repo` to `gh` only for a repo given by --repo or
the config (as the scripts always did); `repo` also falls back to origin.

Every verb answers in the same stdout shape, flags and exit codes on both providers; the full table (shapes,
the `gh` commands each replaces, the few honest differences) is providers.md. Ids are opaque strings.

  provider | repo [--explicit] | check | halt <reason> | resume | whoami | quota | repository | permission <login>
  ticket    view <N> | list [--state open|closed|all] [--label L]... [--limit N] | graph | history |
            create --title T --body-file F [--label L]... [--column C] | edit <N> [--title] [--body-file]
            [--add-label L]... [--remove-label L]... | close <N> --reason completed|not-planned |
            comment <N> --body-file F | comments <N> | comment-edit <N> <ID> --body-file F |
            claim <N> | release <N> [--force]
  pr        view <N> | head <N> | state <N> | body <N> | set-body <N> --body-file F | metadata <N> |
            diff <N> --meta F | list --state open|merged --limit N | refs --state open|merged --limit N |
            merged --since ISO --limit N | links <N> | approval <N> | checks <N> |
            create --base B --head H --title T --body-file F [--draft] [--label L]... [--closes <N>]... |
            ready <N> | comment <N> --body-file F | comments <N> | comment-edit <N> <ID> --body-file F |
            note <N> --file P [--line L [--end-line M]] [--side left|right] --body-file F [--head SHA] |
            notes <N> --notes-file F [--head SHA] | note-edit <N> <ID> --body-file F | threads <N> |
            thread-resolve <N> <thread id> | merge <N> --squash --delete-branch [--subject S]
            [--body-file F] --match-head SHA
  board     items | status <N> | item-id <N> | move <N> <Column> | ids | ensure [--check]
            (GitHub: super-board-card.sh; `ensure` is Azure DevOps only, GitHub uses super-board-setup.py)

Backends: GitHub = this file (the original verbs) plus super-board-forge-github.py (the lane verbs);
Azure DevOps = super-board-forge-ado.py (tracker) and super-board-forge-ado-pr.py (pull requests). On Azure DevOps
metadata and diff come from local git (the service has no diff); set-body moves the history, then visual, block
into an overflow thread past 4,000 characters and body puts it back (exit 65 if still too long); merge pins the
head, completes, then POLLS (exit 6 head moved, 5 conflicts, 7 branch policy, 65 other refusal, 69 outcome
unknown). Never trust lastMergeCommit before status is completed. Claims: GitHub = the assignee, verified by a
re-read; Azure DevOps = per-holder tags `sb:claimed-<6hex>`.

Exit codes: 0 ok · 3 not found (card/column/pull request) · 4 already claimed · 5/7 merge conflicts / branch policy
(Azure DevOps) · 6 head moved (merge, note) · 64 usage, or a malformed read (the read helper's) · 65 refused ·
66 config unreadable, unknown provider or a missing key · 69 provider or verb unavailable, a write failed, or a
create's follow-up failed · 79 required reads halted (the shared halt file). Other codes are the write's own `gh`.

Reads go through super-board-github-read.py (retry, halt file, SB_GITHUB_READ_STRICT);
writes run `gh` directly and are never retried.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('github_read', HERE / 'super-board-github-read.py')
github_read = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(github_read)
_spec = importlib.util.spec_from_file_location('forge_ado', HERE / 'super-board-forge-ado.py')
forge_ado = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(forge_ado)
_spec = importlib.util.spec_from_file_location('forge_ado_pr', HERE / 'super-board-forge-ado-pr.py')
forge_ado_pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(forge_ado_pr)
_spec = importlib.util.spec_from_file_location('forge_github', HERE / 'super-board-forge-github.py')
forge_github = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(forge_github)

EXIT_USAGE, EXIT_CONFIG, EXIT_UNAVAILABLE, EXIT_HALTED = 64, 66, 69, 79

DEPS_QUERY = '''
    query($owner:String!,$repo:String!,$endCursor:String){
      repository(owner:$owner,name:$repo){
        issues(states:OPEN,first:100,after:$endCursor){
          pageInfo{ hasNextPage endCursor }
          nodes{ number title body state labels(first:30){ nodes{ name } } comments(last:8){ nodes{ body } } }
        }
      }
    }'''

# Human approval's one PR read (super-board-approval.py): head, state, base, body and closing links
# in one paginated snapshot. Paginate the links too: an omitted newer request must never make an
# older approval authoritative.
APPROVAL_QUERY = '''query($owner:String!,$repo:String!,$pr:Int!,$endCursor:String){
          repository(owner:$owner,name:$repo){defaultBranchRef{name}
            pullRequest(number:$pr){headRefOid state baseRefName body
            closingIssuesReferences(first:100,after:$endCursor){
              pageInfo{hasNextPage endCursor} nodes{number repository{nameWithOwner}}
            }}}}'''
# GitHub's closing keywords: KEYWORD[:] #N or KEYWORD[:] owner/repo#N.
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+([\w.-]+/[\w.-]+)?#(\d+)\b", re.I)
GITHUB_LOGIN = re.compile(r'[A-Za-z0-9_-]+(?:\[bot\])?')
SHA40 = re.compile(r'[0-9a-f]{40}\Z')

PR_LIST_FIELDS = {
    'open': 'number,title,body,url,headRefName,files',
    'merged': 'number,title,body,url',
}


class Usage(Exception):
    pass


class Unavailable(Exception):
    pass


class ForgeError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


def parser(prog):
    return Parser(prog=f'super-board-forge {prog}', add_help=False)


def positive(text):
    if not re.fullmatch(r'[1-9][0-9]*', text):
        raise Usage(f'not a positive number: {text}')
    return text


class Backend:
    """Every verb is a method; the base class makes each one unavailable."""
    name = ''

    def __init__(self, config, repo):
        self.config = config
        self.flag_repo = repo

    def unavailable(self, verb):
        raise Unavailable(f'{self.name} provider: {verb} is not implemented yet')

    def resolve_repo(self):
        self.unavailable('repo')

    def check(self, argv): self.unavailable('check')
    def halt(self, argv): self.unavailable('halt')
    def resume(self, argv): self.unavailable('resume')
    def pr_head(self, argv): self.unavailable('pr head')
    def pr_state(self, argv): self.unavailable('pr state')
    def pr_metadata(self, argv): self.unavailable('pr metadata')
    def pr_diff(self, argv): self.unavailable('pr diff')
    def pr_body(self, argv): self.unavailable('pr body')
    def pr_set_body(self, argv): self.unavailable('pr set-body')
    def pr_merge(self, argv): self.unavailable('pr merge')
    def pr_list(self, argv): self.unavailable('pr list')
    def pr_refs(self, argv): self.unavailable('pr refs')
    def pr_merged(self, argv): self.unavailable('pr merged')
    def pr_threads(self, argv): self.unavailable('pr threads')
    def pr_create(self, argv): self.unavailable('pr create')
    def pr_ready(self, argv): self.unavailable('pr ready')
    def pr_comment(self, argv): self.unavailable('pr comment')
    def pr_comments(self, argv): self.unavailable('pr comments')
    def pr_comment_edit(self, argv): self.unavailable('pr comment-edit')
    def pr_links(self, argv): self.unavailable('pr links')
    def pr_view(self, argv): self.unavailable('pr view')
    def pr_checks(self, argv): self.unavailable('pr checks')
    def pr_note(self, argv): self.unavailable('pr note')
    def pr_notes(self, argv): self.unavailable('pr notes')
    def pr_note_edit(self, argv): self.unavailable('pr note-edit')
    def pr_thread_resolve(self, argv): self.unavailable('pr thread-resolve')
    def ticket_view(self, argv): self.unavailable('ticket view')
    def ticket_list(self, argv): self.unavailable('ticket list')
    def ticket_graph(self, argv): self.unavailable('ticket graph')
    def ticket_history(self, argv): self.unavailable('ticket history')
    def ticket_create(self, argv): self.unavailable('ticket create')
    def ticket_edit(self, argv): self.unavailable('ticket edit')
    def ticket_close(self, argv): self.unavailable('ticket close')
    def ticket_comment(self, argv): self.unavailable('ticket comment')
    def ticket_comments(self, argv): self.unavailable('ticket comments')
    def ticket_comment_edit(self, argv): self.unavailable('ticket comment-edit')
    def pr_approval(self, argv): self.unavailable('pr approval')
    def permission_verb(self, argv): self.unavailable('permission')
    def ticket_claim(self, argv): self.unavailable('ticket claim')
    def ticket_release(self, argv): self.unavailable('ticket release')
    def whoami_verb(self, argv): self.unavailable('whoami')
    def quota_verb(self, argv): self.unavailable('quota')
    def repository_verb(self, argv): self.unavailable('repository')
    def board(self, argv): self.unavailable('board ' + (argv[0] if argv else '<verb>'))


class AzureDevOpsBackend(forge_ado_pr.AdoPrVerbs, forge_ado.AdoVerbs, Backend):
    """Tracker verbs live in super-board-forge-ado.py, pull-request verbs in super-board-forge-ado-pr.py."""
    name = 'azure-devops'


class GitHubBackend(forge_github.GhVerbs, Backend):
    """Lane verbs (tickets, claims, PR comments/notes/checks, board, quota...) live in super-board-forge-github.py."""
    name = 'github'

    # ---- repo ------------------------------------------------------------
    @staticmethod
    def slug(value):
        value = (value or '').strip()
        value = re.sub(r'^(https?://github\.com/|git@github\.com:)', '', value)
        return re.sub(r'\.git$', '', value)

    def explicit_repo(self):
        """--repo, else config repo.remote; empty lets `gh` use the checkout."""
        remote = (self.config.get('repo') or {}).get('remote') or ''
        return self.slug(self.flag_repo or remote)

    def resolve_repo(self):
        found = self.explicit_repo()
        if found:
            return found
        path = (self.config.get('repo') or {}).get('path') or os.environ.get('SB_REPO_PATH') or '.'
        result = github_read.compat.run(['git', '-C', path, 'remote', 'get-url', 'origin'],
                                capture_output=True, text=True)
        return self.slug(result.stdout) if result.returncode == 0 else ''

    def repo_args(self):
        found = self.explicit_repo()
        return ['--repo', found] if found else []

    # ---- reads (retry, halt file, exit 79/64 via the read helper) ----------
    def read(self, args, kind, meta=None):
        out, _ = github_read.read(args, kind, meta)
        return out

    def emit(self, args, kind, meta=None):
        sys.stdout.write(self.read(args, kind, meta))

    # ---- verbs -----------------------------------------------------------
    def check(self, argv):
        github_read.check_halt()

    def halt(self, argv):
        p = parser('halt <reason>')
        p.add_argument('reason')
        github_read.halt(p.parse_args(argv).reason, 1)

    def resume(self, argv):
        # Exactly the helper's own recovery: its `--resume` replays the saved read (or, given
        # `--kind K [--meta F] -- <gh args>`, a replacement read of the same kind), then archives the halt.
        sys.stdout.flush()
        rc = subprocess.run([sys.executable, str(HERE / 'super-board-github-read.py'), '--resume', *argv]).returncode
        if rc:
            raise ForgeError('', rc)

    def pr_head(self, argv):
        p = parser('pr head <N>'); p.add_argument('number', type=positive)
        n = p.parse_args(argv).number
        self.emit(['pr', 'view', n, *self.repo_args(), '--json', 'headRefName,headRefOid',
                   '-q', '.headRefName + " " + .headRefOid'], 'head')

    def pr_state(self, argv):
        p = parser('pr state <N>'); p.add_argument('number', type=positive)
        n = p.parse_args(argv).number
        self.emit(['pr', 'view', n, *self.repo_args(), '--json', 'state,mergeCommit,headRefOid'], 'merge-state')

    def pr_metadata(self, argv):
        p = parser('pr metadata <N>'); p.add_argument('number', type=positive)
        n = p.parse_args(argv).number
        self.emit(['pr', 'view', n, *self.repo_args(), '--json',
                   'labels,files,additions,deletions,changedFiles,body'], 'metadata')

    def pr_diff(self, argv):
        p = parser('pr diff <N> --meta <file>'); p.add_argument('number', type=positive)
        p.add_argument('--meta')
        a = p.parse_args(argv)
        meta = json.loads(Path(a.meta).read_text(encoding='utf-8')) if a.meta else None
        self.emit(['pr', 'diff', a.number, *self.repo_args()], 'diff', meta)

    def pr_body(self, argv):
        p = parser('pr body <N>'); p.add_argument('number', type=positive)
        n = p.parse_args(argv).number
        # REST, not `gh pr view`: the core bucket is separate from the GraphQL one.
        # GH_REPO fills {owner}/{repo}; unset, gh uses the current checkout.
        found = self.explicit_repo()
        if found:
            os.environ['GH_REPO'] = found
        self.emit(['api', f'repos/{{owner}}/{{repo}}/pulls/{n}', '--jq',
                   '{headRefOid: .head.sha, body: (.body // "")}'], 'body')

    def pr_set_body(self, argv):
        p = parser('pr set-body <N> --body-file <f>'); p.add_argument('number', type=positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        payload = json.dumps({'body': Path(a.body_file).read_text(encoding='utf-8')}, ensure_ascii=False)
        found = self.explicit_repo()
        env = dict(os.environ)
        if found:
            env['GH_REPO'] = found
        rc = github_read.compat.run(['gh', 'api', '-X', 'PATCH', f'repos/{{owner}}/{{repo}}/pulls/{a.number}',
                             '--input', '-'], input=payload.encode('utf-8'), env=env).returncode
        if rc:
            raise ForgeError('gh pr body update failed', rc)

    def pr_merge(self, argv):
        p = parser('pr merge <N> --squash --delete-branch --match-head <sha>')
        p.add_argument('number', type=positive)
        p.add_argument('--squash', action='store_true')
        p.add_argument('--delete-branch', action='store_true')
        p.add_argument('--subject')
        p.add_argument('--body-file')
        p.add_argument('--match-head', required=True)
        a = p.parse_args(argv)
        if not (a.squash and a.delete_branch):
            raise Usage('pr merge needs --squash and --delete-branch')
        # A write: run directly, output and exit status pass straight through.
        cmd = ['gh', 'pr', 'merge', a.number, *self.repo_args(), '--squash', '--delete-branch']
        if a.subject:
            cmd += ['--subject', a.subject]
        if a.body_file:
            cmd += ['--body-file', a.body_file]
        cmd += ['--match-head-commit', a.match_head]
        sys.stdout.flush()
        rc = github_read.compat.run(cmd).returncode
        if rc:
            # 5, 6, 7 and 65 are the gate's own "definite refusal" codes (Azure DevOps `pr merge`);
            # a `gh` failure is ambiguous, so it must never look like one of them.
            raise ForgeError('', 1 if rc in (5, 6, 7, 65) else rc)

    def pr_list(self, argv):
        p = parser('pr list --state open|merged --limit N')
        p.add_argument('--state', required=True, choices=sorted(PR_LIST_FIELDS))
        p.add_argument('--limit', required=True, type=positive)
        a = p.parse_args(argv)
        self.emit(['pr', 'list', *self.repo_args(), '--state', a.state, '--limit', a.limit,
                   '--json', PR_LIST_FIELDS[a.state]], f'prs-{a.state}')

    def ticket_graph(self, argv):
        parser('ticket graph').parse_args(argv)
        repo = self.resolve_repo()
        if not re.fullmatch(r'[^/\s]+/[^/\s]+', repo):
            raise Usage('ticket graph needs a repo (--repo, config repo.remote, or an origin remote)')
        owner, name = repo.split('/')
        # GraphQL rather than `gh issue list`: the lanes' `blocked-by:` line lives in
        # a COMMENT, which the REST list cannot return.
        pages = json.loads(self.read(['api', 'graphql', '--paginate', '--slurp', '-f', 'query=' + DEPS_QUERY,
                                      '-F', f'owner={owner}', '-F', f'repo={name}'], 'issues'))
        print(json.dumps([n for page in pages for n in page['data']['repository']['issues']['nodes']]))

    # ---- human approval (super-board-approval.py) ---------------------------
    # The argv builders and the snapshot parser are shared with super-board-approval.py,
    # which runs the same commands through the same read helper and kinds.
    @staticmethod
    def approval_argv(repo, n):
        owner, name = repo.split('/')
        return ['api', 'graphql', '--paginate', '--slurp', '-f', f'query={APPROVAL_QUERY}',
                '-F', f'owner={owner}', '-F', f'repo={name}', '-F', f'pr={n}']

    @staticmethod
    def comments_argv(repo, n):
        return ['api', '--paginate', '--slurp', f'repos/{repo}/issues/{n}/comments?per_page=100']

    @staticmethod
    def permission_argv(repo, login):
        return ['api', f'repos/{repo}/collaborators/{login}/permission']

    @staticmethod
    def body_links(body, repo):
        """Same-repo issues a PR body closes; other-repo references are ignored."""
        return {int(n) for other, n in CLOSES.findall(body) if not other or other.lower() == repo}

    @classmethod
    def approval_snapshot(cls, pages, repo):
        """{state, headRefOid, closes} from the approval pages; repo is lower-case owner/name.
        GitHub fills closingIssuesReferences only for PRs into the default branch, so a PR into
        another base takes its body's closing keywords, read in this same head-pinned snapshot.
        Remote-repository links are ignored. ValueError when incomplete or changing."""
        snapshots, issues = set(), set()
        if not isinstance(pages, list) or not pages:
            raise ValueError('unreadable PR')
        for page in pages:
            if page.get('errors'):
                raise ValueError('incomplete PR response')
            repository = page['data']['repository']
            pr = repository['pullRequest']
            snapshots.add((pr['state'], pr['headRefOid'], pr['baseRefName'], pr['body'],
                           (repository['defaultBranchRef'] or {}).get('name')))
            links = pr['closingIssuesReferences']
            for issue in links['nodes']:
                if issue['repository']['nameWithOwner'].lower() == repo:
                    issues.add(issue['number'])
        if links['pageInfo']['hasNextPage'] or len(snapshots) != 1:
            raise ValueError('PR changed or issue links are incomplete')
        state, head, base, body, default = snapshots.pop()
        if not all(isinstance(v, str) for v in (state, head, base, body, default)) or not SHA40.fullmatch(head):
            raise ValueError('unreadable PR head, base or body')
        if base != default:
            issues |= cls.body_links(body, repo)
        return {'state': state, 'headRefOid': head, 'closes': sorted(issues)}

    def owner_name(self, verb):
        repo = self.resolve_repo()
        if not re.fullmatch(r'[^/\s]+/[^/\s]+', repo):
            raise Usage(f'{verb} needs a repo (--repo, config repo.remote, or an origin remote)')
        return repo.lower()

    def pr_approval(self, argv):
        p = parser('pr approval <N>'); p.add_argument('number', type=positive)
        n = p.parse_args(argv).number
        repo = self.owner_name('pr approval')
        pages = json.loads(self.read(self.approval_argv(repo, n), 'approval'))
        print(json.dumps(self.approval_snapshot(pages, repo)))

    def ticket_comments(self, argv):
        p = parser('ticket comments <N>'); p.add_argument('number', type=positive)
        n = p.parse_args(argv).number
        self.emit(self.comments_argv(self.owner_name('ticket comments'), n), 'comments')

    def permission_verb(self, argv):
        p = parser('permission <login>'); p.add_argument('login')
        login = p.parse_args(argv).login
        if not GITHUB_LOGIN.fullmatch(login):
            print(json.dumps({'permission': None}))
            return
        value = json.loads(self.read(self.permission_argv(self.owner_name('permission'), login), 'permission'))
        print(json.dumps({'permission': value.get('permission')}))


BACKENDS = {'github': GitHubBackend, 'azure-devops': AzureDevOpsBackend}

# (noun, verb) or (verb,) -> Backend method name. Adding a verb: a row here and a method.
DISPATCH = {
    ('check',): 'check',
    ('halt',): 'halt',
    ('resume',): 'resume',
    ('pr', 'head'): 'pr_head',
    ('pr', 'state'): 'pr_state',
    ('pr', 'metadata'): 'pr_metadata',
    ('pr', 'diff'): 'pr_diff',
    ('pr', 'body'): 'pr_body',
    ('pr', 'set-body'): 'pr_set_body',
    ('pr', 'merge'): 'pr_merge',
    ('pr', 'list'): 'pr_list',
    ('pr', 'refs'): 'pr_refs',
    ('pr', 'merged'): 'pr_merged',
    ('pr', 'threads'): 'pr_threads',
    ('pr', 'create'): 'pr_create',
    ('pr', 'ready'): 'pr_ready',
    ('pr', 'comment'): 'pr_comment',
    ('pr', 'comments'): 'pr_comments',
    ('pr', 'comment-edit'): 'pr_comment_edit',
    ('pr', 'links'): 'pr_links',
    ('pr', 'approval'): 'pr_approval',
    ('pr', 'view'): 'pr_view',
    ('pr', 'checks'): 'pr_checks',
    ('pr', 'note'): 'pr_note',
    ('pr', 'notes'): 'pr_notes',
    ('pr', 'note-edit'): 'pr_note_edit',
    ('pr', 'thread-resolve'): 'pr_thread_resolve',
    ('permission',): 'permission_verb',
    ('ticket', 'view'): 'ticket_view',
    ('ticket', 'list'): 'ticket_list',
    ('ticket', 'graph'): 'ticket_graph',
    ('ticket', 'history'): 'ticket_history',
    ('ticket', 'create'): 'ticket_create',
    ('ticket', 'edit'): 'ticket_edit',
    ('ticket', 'close'): 'ticket_close',
    ('ticket', 'comment'): 'ticket_comment',
    ('ticket', 'comments'): 'ticket_comments',
    ('ticket', 'comment-edit'): 'ticket_comment_edit',
    ('ticket', 'claim'): 'ticket_claim',
    ('ticket', 'release'): 'ticket_release',
    ('whoami',): 'whoami_verb',
    ('quota',): 'quota_verb',
    ('repository',): 'repository_verb',
}
forge_ado.bind(sys.modules[__name__])
forge_ado_pr.bind(sys.modules[__name__], forge_ado)
forge_github.bind(sys.modules[__name__])
LABEL = {'name': 'github-read'}  # prefix of halt/read messages; the ADO backend says azure-devops


def load_config(path):
    path = path or os.environ.get('SB_CONFIG') or ''
    if not path:
        return {}
    try:
        text = sys.stdin.read() if path == '-' else Path(path).read_text(encoding='utf-8')
        value = json.loads(text)  # once: the file may be a FIFO
    except (OSError, ValueError) as error:
        raise ForgeError(f'config unreadable: {path}: {error}', EXIT_CONFIG)
    if not isinstance(value, dict):
        raise ForgeError(f'config is not a JSON object: {path}', EXIT_CONFIG)
    return value


def run(argv):
    top = Parser(prog='super-board-forge', add_help=False)
    top.add_argument('--config')
    top.add_argument('--repo')
    top.add_argument('command', nargs=argparse.REMAINDER)
    args = top.parse_args(argv)
    command = args.command
    if not command:
        raise Usage('usage: super-board-forge.py [--config F] [--repo R] <verb> ...')
    config = load_config(args.config)
    provider = config.get('provider', 'github')
    if provider not in BACKENDS:
        raise ForgeError(f'unknown provider "{provider}" in config (expected github or azure-devops)', EXIT_CONFIG)
    LABEL['name'] = 'azure-devops' if provider == 'azure-devops' else 'github-read'
    backend = BACKENDS[provider](config, args.repo)
    if command[0] == 'provider':
        print(backend.name)
        return 0
    if command[0] == 'repo':
        if command[1:] not in ([], ['--explicit']):
            raise Usage('usage: repo [--explicit]')
        print(backend.explicit_repo() if command[1:] and provider == 'github' else backend.resolve_repo())
        return 0
    if command[0] == 'board':
        backend.board(command[1:])
        return 0
    key = tuple(command[:1]) if (command[0],) in DISPATCH else tuple(command[:2])
    method = DISPATCH.get(key)
    if method is None:
        raise Usage(f'unknown verb: {" ".join(command[:2])}')
    getattr(backend, method)(command[len(key):])
    return 0


def utf8_stdio():
    """Titles and comments carry emoji. A piped Windows stdout defaults to the ANSI code page,
    where printing one raises UnicodeEncodeError; force UTF-8 on every stream instead."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace' if stream is sys.stderr else 'strict', newline=None if stream is sys.stdin else '\n')
        except (AttributeError, ValueError):
            pass


def main():
    utf8_stdio()
    try:
        return run(sys.argv[1:])
    except github_read.ReadHalted as error:
        print(f'[{LABEL["name"]}] HALT: {error}. Saved at {github_read.halt_path()}', file=sys.stderr)
        return EXIT_HALTED
    except Usage as error:
        print(f'super-board-forge: {error}', file=sys.stderr)
        return EXIT_USAGE
    except Unavailable as error:
        print(f'super-board-forge: {error}', file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ForgeError as error:
        if str(error):
            print(f'super-board-forge: {error}', file=sys.stderr)
        return error.code
    except (ValueError, OSError) as error:
        print(f'[{LABEL["name"]}] {error}', file=sys.stderr)
        return EXIT_USAGE


if __name__ == '__main__':
    sys.exit(main())
