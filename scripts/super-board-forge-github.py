"""super-board-forge-github.py: the GitHub LANE verbs for super-board-forge.py.

Not a command. The forge loads it and mixes `GhVerbs` into `GitHubBackend` (after `bind(forge_module)`
hands over Usage/ForgeError/parser). The verbs the pull-request and tracker lanes need beyond what the
forge always had (pr head/state/metadata/diff/body/set-body/merge/list, ticket graph, approval reads)
live here, so a lane agent runs ONE set of commands on GitHub and on Azure DevOps.
Contract and shapes: skills/super-board/references/providers.md.

Reads go through super-board-github-read.py (three attempts, the shared halt file, exit 79/64) with a
`--kind`; writes run `gh` once and never retry. Where the GraphQL point budget matters (see
rate-limit-etiquette.md) the verb uses the REST form (`gh api repos/{owner}/{repo}/...`, the core
bucket); the ones with no REST form (review threads, draft -> ready, check required-ness) are 1-2 points.
`{owner}/{repo}` is filled by `gh` from GH_REPO, which the verbs set from --repo / the config.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

forge = None  # the forge module, set by bind()

HERE = Path(__file__).resolve().parent
# gh failures keep their own status, except the ones the callers read as a definite answer.
RESERVED = {3, 4, 5, 6, 7, 64, 65, 66, 69, 79}
EXIT_NOT_FOUND, EXIT_CLAIMED, EXIT_HEAD_MOVED, EXIT_FAILED = 3, 4, 6, 69
SHA40 = re.compile(r'[0-9a-f]{40}\Z')
TICKET_FIELDS = 'number,title,body,state,stateReason,labels,assignees,url'
CLOSING = r'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)'

THREADS_QUERY = '''query($owner:String!,$repo:String!,$pr:Int!,$endCursor:String){
  repository(owner:$owner,name:$repo){ pullRequest(number:$pr){
    reviewThreads(first:100,after:$endCursor){ pageInfo{ hasNextPage endCursor }
      nodes{ id isResolved isOutdated path line startLine originalLine diffSide
        comments(first:100){ nodes{ databaseId body createdAt author{ login __typename } } } } } } } }'''

CHECKS_QUERY = '''query($owner:String!,$repo:String!,$pr:Int!,$endCursor:String){
  repository(owner:$owner,name:$repo){ pullRequest(number:$pr){
    commits(last:1){ nodes{ commit{ oid statusCheckRollup{
      contexts(first:100,after:$endCursor){ pageInfo{ hasNextPage endCursor }
        nodes{ __typename
          ... on CheckRun{ name status conclusion isRequired(pullRequestNumber:$pr) }
          ... on StatusContext{ context state isRequired(pullRequestNumber:$pr) } } } } } } } } } }'''

RESOLVE_MUTATION = ('mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId})'
                    '{thread{isResolved}}}')
RATE_QUERY = 'query{rateLimit{limit remaining used resetAt}}'
HISTORY_QUERY = ('query($owner:String!,$repo:String!,$endCursor:String){repository(owner:$owner,name:$repo)'
                 '{issues(first:100,after:$endCursor,states:[OPEN,CLOSED]){totalCount pageInfo{hasNextPage endCursor} '
                 'nodes{number state body}}}}')

PR_VIEW_JQ = ('{number: .number, title: .title, url: .html_url, '
              'state: (if .merged then "MERGED" elif .state == "closed" then "CLOSED" else "OPEN" end), '
              'isDraft: (.draft // false), headRefName: .head.ref, headRefOid: .head.sha, baseRefName: .base.ref, '
              'mergeCommit: (if .merged then {oid: .merge_commit_sha} else null end), '
              'labels: [.labels[] | {name: .name}]}')


def bind(module):
    global forge
    forge = module


# ---- pure helpers -----------------------------------------------------------------------------
def login_key(login):
    """GraphQL logins omit `[bot]`, REST logins carry it: compare without it, case-insensitively."""
    return re.sub(r'\[bot\]$', '', (login or '').strip(), flags=re.I).casefold()


def ticket_shape(value, default_state='OPEN'):
    """The neutral ticket: {number,title,body,state,stateReason,labels:[{name}],assignees:[{login}],url}."""
    state = str(value.get('state') or default_state).upper()
    reason = None
    if state == 'CLOSED':
        given = str(value.get('stateReason') or '').upper()
        reason = given if given in ('COMPLETED', 'NOT_PLANNED') else 'COMPLETED'
    return {'number': value['number'], 'title': value.get('title') or '', 'body': value.get('body') or '',
            'state': state, 'stateReason': reason,
            'labels': [{'name': l['name']} for l in value.get('labels') or [] if isinstance(l, dict) and l.get('name')],
            'assignees': [{'login': a['login']} for a in value.get('assignees') or [] if isinstance(a, dict) and a.get('login')],
            'url': value.get('url') or ''}


def check_status(node):
    """pending | pass | fail | skipped for one check run or commit status."""
    if node.get('__typename') == 'StatusContext':
        state = str(node.get('state') or '').upper()
        return 'pass' if state == 'SUCCESS' else 'fail' if state in ('FAILURE', 'ERROR') else 'pending'
    if str(node.get('status') or '').upper() != 'COMPLETED':
        return 'pending'
    conclusion = str(node.get('conclusion') or '').upper()
    if conclusion in ('SUCCESS', 'NEUTRAL'):
        return 'pass'
    return 'skipped' if conclusion == 'SKIPPED' else 'fail'


def author_shape(author):
    author = author or {}
    return {'login': author.get('login') or '', 'type': 'Bot' if author.get('__typename') == 'Bot' else 'User'}


def side_of(text):
    value = (text or 'right').lower()
    if value not in ('left', 'right'):
        raise forge.Usage(f'--side must be left or right, got: {text}')
    return value


def note_spec(entry, index=None):
    """Validate one inline note (a `pr note` call or one entry of a `pr notes` file)."""
    where = f'note {index}: ' if index is not None else ''
    if not isinstance(entry, dict):
        raise forge.Usage(f'{where}a note is an object {{file, line?, end_line?, side?, body}}')
    path = entry.get('file')
    body = entry.get('body')
    if not isinstance(path, str) or not path.strip('/'):
        raise forge.Usage(f'{where}file is required')
    if not isinstance(body, str) or not body.strip():
        raise forge.Usage(f'{where}body is required')
    line, end = entry.get('line'), entry.get('end_line')
    if line is not None and (type(line) is not int or line < 1):
        raise forge.Usage(f'{where}line must be a positive number')
    if end is not None and (line is None or type(end) is not int or end < line):
        raise forge.Usage(f'{where}end_line needs a line and must not be before it')
    return {'file': path.replace('\\', '/').lstrip('/'), 'body': body, 'line': line,
            'end_line': end if end and end != line else None, 'side': side_of(entry.get('side'))}


def load_notes(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise forge.Usage(f'--notes-file unreadable: {error}')
    if not isinstance(value, list) or not value:
        raise forge.Usage('--notes-file must hold a non-empty JSON array of notes')
    return [note_spec(e, i) for i, e in enumerate(value)]


# ---- the verbs --------------------------------------------------------------------------------------
class GhVerbs:
    # ---- plumbing --------------------------------------------------------------------------------
    def pin_repo(self):
        """Let `gh` fill {owner}/{repo}: from --repo or the config, else its own checkout."""
        found = self.explicit_repo()
        if found:
            os.environ['GH_REPO'] = found

    def gh_run(self, argv, payload=None):
        """One `gh` write. (rc, stdout, stderr); never retried."""
        sys.stdout.flush()
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
        try:
            r = subprocess.run([shutil.which('gh') or 'gh', *argv], input=data, capture_output=True)
        except OSError as error:
            raise forge.ForgeError(f'gh could not run: {error}', EXIT_FAILED)
        return r.returncode, r.stdout.decode('utf-8', 'replace'), r.stderr.decode('utf-8', 'replace')

    def gh_write(self, argv, payload=None, what='gh write', tolerate=None):
        """stdout of a write that succeeded. A failure keeps gh's message and (unless it is one of the
        callers' own codes) gh's status; `tolerate` is a regex over stderr for an answer that is fine."""
        rc, out, err = self.gh_run(argv, payload)
        if rc and tolerate and re.search(tolerate, err + out):
            return ''
        if rc:
            sys.stderr.write(err if err.endswith('\n') or not err else err + '\n')
            raise forge.ForgeError(f'{what} failed', 1 if rc in RESERVED else rc)
        return out

    def api_json(self, path, kind, jq=None, paginate=False):
        args = ['api', *(['--paginate', '--slurp'] if paginate else []), path, *(['--jq', jq] if jq else [])]
        return json.loads(self.read(args, kind))

    def issue_path(self, n, tail=''):
        return f'repos/{{owner}}/{{repo}}/issues/{n}{tail}'

    def pr_path(self, n, tail=''):
        return f'repos/{{owner}}/{{repo}}/pulls/{n}{tail}'

    def current_head(self, n):
        return self.read(['api', self.pr_path(n), '--jq', '.head.sha'], 'scalar').strip()

    def require_head(self, n, wanted):
        """Exit 6 (head moved, nothing written) when the PR is no longer at the head the caller read."""
        if wanted and self.current_head(n) != wanted:
            raise forge.ForgeError(f'head moved: pull request #{n} is no longer at {wanted}; nothing was written',
                                   EXIT_HEAD_MOVED)

    # ---- repository, identity, quota --------------------------------------------------------------
    def repository_verb(self, argv):
        forge.parser('repository').parse_args(argv)
        self.pin_repo()
        info = self.api_json('repos/{owner}/{repo}', 'object', '{repository: .name, defaultBranch: .default_branch}')
        heads = self.api_json('repos/{owner}/{repo}/branches?per_page=1', 'array')
        base = self.config.get('base_branch') or None
        exists = None
        if base and heads:
            found = self.api_json('repos/{owner}/{repo}/git/matching-refs/heads/' + urllib.parse.quote(base, safe='/'), 'array')
            exists = any(r.get('ref') == f'refs/heads/{base}' for r in found if isinstance(r, dict))
        elif base:
            exists = False
        print(json.dumps({'repository': info['repository'], 'defaultBranch': info.get('defaultBranch') if heads else None,
                          'empty': not heads, 'base': base, 'baseExists': exists}))

    def whoami_verb(self, argv):
        forge.parser('whoami').parse_args(argv)
        print(json.dumps(self.whoami_value()))

    def whoami_value(self):
        # An installation (GitHub App) token has no /user: `gh` answers 403 and the read halts. Set
        # notifications.bot_identity to the bot's login when the run uses one.
        return self.api_json('user', 'object', '{login: .login, id: (.id | tostring)}')

    def quota_verb(self, argv):
        """{remaining, limit, reset, at, seen} = the GraphQL bucket (the one that stalls a board), plus both
        buckets as `graphql` and `core`. GraphQL numbers come from GraphQL's own rateLimit when it answers:
        `gh api rate_limit` misreports that bucket (see super-board-gh-guard.sh)."""
        forge.parser('quota').parse_args(argv)
        resources = json.loads(self.read(['api', 'rate_limit'], 'quota'))['resources']
        rate = {b: {k: resources[b].get(k) for k in ('limit', 'remaining', 'used', 'reset')} for b in ('graphql', 'core')}
        try:
            r = subprocess.run([shutil.which('gh') or 'gh', 'api', 'graphql', '-f', f'query={RATE_QUERY}'],
                               capture_output=True, timeout=30)
            node = json.loads(r.stdout.decode('utf-8', 'replace'))['data']['rateLimit'] if r.returncode == 0 else None
            reset = forge.forge_ado_pr.parse_time(node['resetAt']) if node else None
            if node and reset:
                rate['graphql'] = {'limit': node['limit'], 'remaining': node['remaining'], 'used': node['used'],
                                   'reset': int(reset.timestamp())}
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            pass  # the REST numbers stand
        print(json.dumps({'remaining': rate['graphql']['remaining'], 'limit': rate['graphql']['limit'],
                          'reset': rate['graphql']['reset'], 'at': int(time.time()), 'seen': True, **rate}))

    # ---- boards (super-board-card.sh is the cheap board CLI) --------------------------------------------
    def card_argv(self):
        project = self.config.get('project') or {}
        argv = [shutil.which('bash') or 'bash', str(HERE / 'super-board-card.sh')]
        for flag, value in (('--owner', project.get('owner')), ('--number', project.get('number')),
                            ('--repo', self.explicit_repo())):
            if value not in (None, ''):
                argv += [flag, str(value)]
        return argv

    def card_run(self, args, capture=False):
        # The forge consumed the config (it may be a FIFO): hand the card script flags, never SB_CONFIG.
        env = {k: v for k, v in os.environ.items() if k != 'SB_CONFIG'}
        sys.stdout.flush()
        return subprocess.run([*self.card_argv(), *args], env=env, capture_output=capture, text=capture,
                              encoding='utf-8' if capture else None)

    def board(self, argv):
        if not argv:
            raise forge.Usage('usage: board items|status <N>|item-id <N>|move <N> <Status>|ids')
        if argv[0] == 'ensure':
            raise forge.Unavailable('github provider: board ensure is not a forge verb. Use '
                                    '`python3 .claude/bin/super-board-setup.py check|fix`, or board-migrate --config <cfg> '
                                    '[--dry-run] for the Status options and labels (see providers.md, "Board setup")')
        if argv[0] not in ('items', 'status', 'item-id', 'move', 'ids'):
            raise forge.Usage(f'unknown board verb: {argv[0]}')
        rc = self.card_run(argv).returncode
        if rc:
            raise forge.ForgeError('', rc)

    def move_to(self, n, column):
        """Best effort for verbs that also place a card; (ok, message)."""
        r = self.card_run(['move', str(n), column], capture=True)
        return r.returncode == 0, (r.stderr or '').strip()

    # ---- tickets -------------------------------------------------------------------------------------------
    def ticket_view(self, argv):
        p = forge.parser('ticket view <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        text = self.read(['issue', 'view', n, *self.repo_args(), '--json', TICKET_FIELDS], 'issue')
        print(json.dumps(ticket_shape(json.loads(text))))

    def ticket_list(self, argv):
        p = forge.parser('ticket list [--state open|closed|all] [--label L]... [--limit N]')
        p.add_argument('--state', default='open', choices=['open', 'closed', 'all'])
        p.add_argument('--label', action='append', default=[])
        p.add_argument('--limit', default='30', type=forge.positive)
        a = p.parse_args(argv)
        args = ['issue', 'list', *self.repo_args(), '--state', a.state, '--limit', a.limit]
        for label in a.label:
            args += ['--label', label]
        text = self.read(args + ['--json', TICKET_FIELDS], 'issues-closed' if a.state == 'closed' else 'tickets')
        default = 'CLOSED' if a.state == 'closed' else 'OPEN'
        print(json.dumps([ticket_shape(v, default) for v in json.loads(text)]))

    def ticket_history(self, argv):
        forge.parser('ticket history').parse_args(argv)
        owner, name = self.owner_name('ticket history').split('/')
        pages = json.loads(self.read(['api', 'graphql', '--paginate', '--slurp', '-f', 'query=' + HISTORY_QUERY,
                                      '-F', f'owner={owner}', '-F', f'repo={name}'], 'issue-history'))
        print(json.dumps([n for page in pages for n in page['data']['repository']['issues']['nodes']]))

    def ticket_create(self, argv):
        p = forge.parser('ticket create --title T --body-file F [--label L]... [--column C]')
        p.add_argument('--title', required=True); p.add_argument('--body-file', required=True)
        p.add_argument('--label', action='append', default=[]); p.add_argument('--column')
        a = p.parse_args(argv)
        body = Path(a.body_file).read_text(encoding='utf-8')
        project = self.config.get('project') or {}
        if a.column:
            if not (project.get('owner') and project.get('number')):
                raise forge.ForgeError('ticket create --column needs config project.owner and project.number',
                                       forge.EXIT_CONFIG)
            ids = self.card_run(['ids'], capture=True)
            if ids.returncode:
                raise forge.ForgeError((ids.stderr or 'board ids failed').strip(), ids.returncode)
            if a.column not in (json.loads(ids.stdout).get('options') or {}):
                raise forge.ForgeError(f'column "{a.column}" is not on the board (nothing was created)', EXIT_NOT_FOUND)
        self.pin_repo()
        payload = {'title': a.title, 'body': body}
        labels = list(dict.fromkeys(a.label))
        if labels:
            payload['labels'] = labels
        lines = self.gh_write(['api', '-X', 'POST', 'repos/{owner}/{repo}/issues',
                               '--input', '-', '--jq', '.html_url, .number'], payload, 'ticket create').split()
        if len(lines) < 2 or not lines[-1].isdigit():
            raise forge.ForgeError('ticket create: the response carried no issue number', EXIT_FAILED)
        url, n = lines[0], lines[-1]
        failure = ''
        if a.column:
            _, _, err = self.gh_run(['project', 'item-add', str(project['number']), '--owner', str(project['owner']), '--url', url])
            ok, why = self.move_to(n, a.column)
            if not ok:
                failure = f'created #{n} but could not place it in "{a.column}": {why or err}'
        print(url)
        print(n)
        if failure:
            raise forge.ForgeError(failure, EXIT_FAILED)

    def ticket_edit(self, argv):
        p = forge.parser('ticket edit <N> [--title T] [--body-file F] [--add-label L]... [--remove-label L]...')
        p.add_argument('number', type=forge.positive); p.add_argument('--title'); p.add_argument('--body-file')
        p.add_argument('--add-label', action='append', default=[]); p.add_argument('--remove-label', action='append', default=[])
        a = p.parse_args(argv)
        self.pin_repo()
        change = {}
        if a.title is not None:
            change['title'] = a.title
        if a.body_file:
            change['body'] = Path(a.body_file).read_text(encoding='utf-8')
        if change:
            self.gh_write(['api', '-X', 'PATCH', self.issue_path(a.number), '--input', '-', '--jq', '.number'], change,
                          f'edit #{a.number}')
        if a.add_label:
            self.gh_write(['api', '-X', 'POST', self.issue_path(a.number, '/labels'), '--input', '-', '--jq', '.[0].name'],
                          {'labels': list(dict.fromkeys(a.add_label))}, f'label #{a.number}')
        for label in dict.fromkeys(a.remove_label):
            # a label that is not on the ticket is fine: removing it is idempotent
            self.gh_write(['api', '-X', 'DELETE', self.issue_path(a.number, '/labels/' + urllib.parse.quote(label, safe=''))],
                          None, f'unlabel #{a.number}', tolerate=r'HTTP 404|Label does not exist')

    def ticket_close(self, argv):
        p = forge.parser('ticket close <N> --reason completed|not-planned'); p.add_argument('number', type=forge.positive)
        p.add_argument('--reason', required=True, choices=['completed', 'not-planned'])
        a = p.parse_args(argv)
        self.pin_repo()
        self.gh_write(['api', '-X', 'PATCH', self.issue_path(a.number), '--input', '-', '--jq', '.state'],
                      {'state': 'closed', 'state_reason': a.reason.replace('-', '_')}, f'close #{a.number}')
        project = self.config.get('project') or {}
        if project.get('owner') and project.get('number'):
            ok, why = self.move_to(a.number, 'Done')
            if not ok:
                print(f'warn: closed #{a.number} but could not move its card to Done: {why}', file=sys.stderr)
        print(f'closed #{a.number} ({a.reason})')

    def ticket_comment(self, argv):
        p = forge.parser('ticket comment <N> --body-file F'); p.add_argument('number', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        self.pin_repo()
        out = self.gh_write(['api', '-X', 'POST', self.issue_path(a.number, '/comments'), '--input', '-', '--jq', '.id'],
                            {'body': Path(a.body_file).read_text(encoding='utf-8')}, f'comment on #{a.number}')
        print(out.strip())

    def ticket_comment_edit(self, argv):
        p = forge.parser('ticket comment-edit <N> <ID> --body-file F')
        p.add_argument('number', type=forge.positive); p.add_argument('comment', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        self.pin_repo()
        self.gh_write(['api', '-X', 'PATCH', f'repos/{{owner}}/{{repo}}/issues/comments/{a.comment}', '--input', '-', '--jq', '.id'],
                      {'body': Path(a.body_file).read_text(encoding='utf-8')}, f'edit comment {a.comment}')
        print(a.comment)

    # ---- claims: the assignee is the lock -------------------------------------------------------------------
    def bot_login(self):
        """notifications.bot_identity; `auto` or unset = the authenticated login."""
        notes = self.config.get('notifications') or {}
        value = str(notes.get('bot_identity') or self.config.get('bot_identity') or '').strip()
        if not value or value.lower() == 'auto':
            value = self.whoami_value()['login']
        return value

    def assignees(self, n):
        return self.api_json(self.issue_path(n), 'array', '[.assignees[].login]')

    def ticket_claim(self, argv):
        """Add the bot as assignee, then re-read and require EXACTLY [bot]: an add never fails when someone
        else already holds the card (issues take up to 10 assignees), so the add alone is not a mutex."""
        p = forge.parser('ticket claim <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        self.pin_repo()
        me = self.bot_login()
        current = self.assignees(n)
        others = [a for a in current if login_key(a) != login_key(me)]
        if others:
            raise forge.ForgeError(f'#{n} is already claimed by {", ".join(others)}', EXIT_CLAIMED)
        if not current:
            self.gh_write(['api', '-X', 'POST', self.issue_path(n, '/assignees'), '--input', '-', '--jq', '.number'],
                          {'assignees': [me]}, f'claim #{n}')
            after = self.assignees(n)
            if [login_key(a) for a in after] != [login_key(me)]:
                if after:  # someone else landed at the same moment: take our own assignee back
                    self.gh_run(['api', '-X', 'DELETE', self.issue_path(n, '/assignees'), '--input', '-'], {'assignees': [me]})
                    raise forge.ForgeError(f'#{n} was claimed by another holder at the same moment', EXIT_CLAIMED)
                raise forge.ForgeError(f'#{n}: GitHub did not accept {me} as an assignee (is it a collaborator?)', EXIT_FAILED)
        print(f'claimed #{n}')

    def ticket_release(self, argv):
        """Remove OUR assignee only. `--force` is accepted for parity with Azure DevOps and changes nothing:
        a GitHub assignee other than the bot is a person's, never a leaked claim."""
        p = forge.parser('ticket release <N> [--force]'); p.add_argument('number', type=forge.positive)
        p.add_argument('--force', action='store_true')
        n = p.parse_args(argv).number
        self.pin_repo()
        me = self.bot_login()
        mine = [a for a in self.assignees(n) if login_key(a) == login_key(me)]
        if mine:
            self.gh_write(['api', '-X', 'DELETE', self.issue_path(n, '/assignees'), '--input', '-', '--jq', '.number'],
                          {'assignees': mine}, f'release #{n}')
        print(f'released #{n}' + (f' ({", ".join(mine)})' if mine else ' (nothing held)'))

    # ---- pull requests: reads -----------------------------------------------------------------------------
    def pr_view(self, argv):
        p = forge.parser('pr view <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        self.pin_repo()
        self.emit(['api', self.pr_path(n), '--jq', PR_VIEW_JQ], 'pr-view')

    def pr_refs(self, argv):
        p = forge.parser('pr refs --state open|merged --limit N')
        p.add_argument('--state', required=True, choices=['open', 'merged'])
        p.add_argument('--limit', required=True, type=forge.positive)
        a = p.parse_args(argv)
        rows = json.loads(self.read(['pr', 'list', *self.repo_args(), '--state', a.state, '--limit', a.limit, '--json',
                                     'number,headRefName,baseRefName,headRefOid'], 'refs'))
        # a merged row with no recorded head is left out rather than guessed: a wrong "merged" deletes work
        print(json.dumps([r for r in rows if a.state == 'open' or r.get('headRefOid')]))

    def pr_merged(self, argv):
        p = forge.parser('pr merged --since <iso> --limit N')
        p.add_argument('--since', required=True); p.add_argument('--limit', required=True, type=forge.positive)
        a = p.parse_args(argv)
        since = forge.forge_ado_pr.parse_time(a.since)
        if since is None:
            raise forge.Usage(f'--since is not an ISO-8601 timestamp: {a.since}')
        rows = json.loads(self.read(['pr', 'list', *self.repo_args(), '--state', 'merged', '--limit', a.limit, '--json',
                                     'number,title,url,mergedAt,author'], 'array'))
        out = []
        for r in rows:
            merged = forge.forge_ado_pr.parse_time(r.get('mergedAt'))
            if merged is None or merged < since:
                continue
            author = r.get('author') or {}
            login = author.get('login') or ''
            bot = bool(author.get('is_bot')) or login.endswith('[bot]') or login.startswith('app/')
            out.append((merged, {'number': r['number'], 'title': r.get('title') or '', 'url': r.get('url') or '',
                                 'mergedAt': r['mergedAt'], 'author': {'login': login, 'type': 'Bot' if bot else 'User'}}))
        out.sort(key=lambda row: row[0], reverse=True)
        print(json.dumps([row for _, row in out[:int(a.limit)]]))

    def pr_links(self, argv):
        p = forge.parser('pr links <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        repo = self.owner_name('pr links')
        pages = json.loads(self.read(self.approval_argv(repo, n), 'approval'))
        print(json.dumps({'closes': self.approval_snapshot(pages, repo)['closes']}))

    def pr_comments(self, argv):
        """The PR conversation (issue comments), oldest first. Ids are opaque strings. File-anchored review
        comments are not here: `pr threads` lists them."""
        p = forge.parser('pr comments <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        pages = json.loads(self.read(self.comments_argv(self.owner_name('pr comments'), n), 'comments'))
        print(json.dumps([[dict(c, id=str(c['id'])) for c in page] for page in pages]))

    def pr_threads(self, argv):
        p = forge.parser('pr threads <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        owner, name = self.owner_name('pr threads').split('/')
        pages = json.loads(self.read(['api', 'graphql', '--paginate', '--slurp', '-f', 'query=' + THREADS_QUERY,
                                      '-F', f'owner={owner}', '-F', f'repo={name}', '-F', f'pr={n}'], 'threads'))
        out = []
        for page in pages:
            for t in page['data']['repository']['pullRequest']['reviewThreads']['nodes']:
                comments = [{'id': str(c['databaseId']), 'author': author_shape(c.get('author')), 'body': c.get('body') or '',
                             'createdAt': c.get('createdAt') or ''} for c in t['comments']['nodes']]
                if not comments:
                    continue
                line = t.get('line') or t.get('originalLine')
                start = t.get('startLine')
                out.append({'id': t['id'], 'path': t.get('path') or None, 'status': 'resolved' if t['isResolved'] else 'active',
                            'resolved': t['isResolved'], 'line': line, 'startLine': start if start and start != line else None,
                            'side': (t.get('diffSide') or '').lower() or None if line else None,
                            'outdated': bool(t.get('isOutdated')), 'comments': comments})
        print(json.dumps(out))

    def pr_checks(self, argv):
        """[{name, status: pending|pass|fail|skipped, required}] for the head: check runs AND commit statuses.
        `required` is GitHub's own branch-protection answer for this PR (isRequired)."""
        p = forge.parser('pr checks <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        owner, name = self.owner_name('pr checks').split('/')
        pages = json.loads(self.read(['api', 'graphql', '--paginate', '--slurp', '-f', 'query=' + CHECKS_QUERY,
                                      '-F', f'owner={owner}', '-F', f'repo={name}', '-F', f'pr={n}'], 'checks'))
        out = []
        for page in pages:
            rollup = page['data']['repository']['pullRequest']['commits']['nodes'][0]['commit']['statusCheckRollup']
            for node in (rollup or {}).get('contexts', {}).get('nodes', []):
                out.append({'name': node.get('name') or node.get('context') or '', 'status': check_status(node),
                            'required': bool(node.get('isRequired'))})
        print(json.dumps(out))

    # ---- pull requests: writes ----------------------------------------------------------------------------------
    def pr_create(self, argv):
        p = forge.parser('pr create --base B --head H --title T --body-file F [--draft] [--label L]... [--closes <ticket>]...')
        p.add_argument('--base', required=True); p.add_argument('--head', required=True)
        p.add_argument('--title', required=True); p.add_argument('--body-file', required=True)
        p.add_argument('--draft', action='store_true')
        p.add_argument('--label', action='append', default=[]); p.add_argument('--closes', action='append', default=[])
        a = p.parse_args(argv)
        body = Path(a.body_file).read_text(encoding='utf-8')
        for ticket in dict.fromkeys(t.lstrip('#') for t in a.closes):
            if not re.fullmatch(r'[1-9][0-9]*', ticket):
                raise forge.Usage(f'--closes needs a ticket number, got: {ticket}')
            # GitHub links a PR to the issue it closes through a closing keyword in the body
            if not re.search(rf'\b{CLOSING}:?\s+#{ticket}\b', body, re.I):
                body = body.rstrip('\n') + f'\n\nCloses #{ticket}\n'
        self.pin_repo()
        lines = self.gh_write(['api', '-X', 'POST', 'repos/{owner}/{repo}/pulls', '--input', '-', '--jq', '.html_url, .number'],
                              {'title': a.title, 'head': a.head, 'base': a.base, 'body': body, 'draft': a.draft},
                              'pull request create').split()
        if len(lines) < 2 or not lines[-1].isdigit():
            raise forge.ForgeError('pull request create: the response carried no pull request number', EXIT_FAILED)
        url, n = lines[0], lines[-1]
        failure = ''
        if a.label:
            rc, _, err = self.gh_run(['api', '-X', 'POST', self.issue_path(n, '/labels'), '--input', '-', '--jq', '.[0].name'],
                                     {'labels': list(dict.fromkeys(a.label))})
            if rc:
                failure = f'created pull request #{n} but could not label it: {err.strip()}'
        print(url)
        print(n)
        if failure:
            raise forge.ForgeError(failure, EXIT_FAILED)

    def pr_ready(self, argv):
        p = forge.parser('pr ready <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        # no REST form: draft -> ready is a GraphQL mutation; `gh pr ready` is idempotent on a ready PR
        self.gh_write(['pr', 'ready', n, *self.repo_args()], None, f'publish pull request #{n}')
        print(f'ready #{n}')

    def pr_comment(self, argv):
        p = forge.parser('pr comment <N> --body-file F'); p.add_argument('number', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        self.pin_repo()
        out = self.gh_write(['api', '-X', 'POST', self.issue_path(a.number, '/comments'), '--input', '-', '--jq', '.id'],
                            {'body': Path(a.body_file).read_text(encoding='utf-8')}, f'comment on pull request #{a.number}')
        print(out.strip())

    def pr_comment_edit(self, argv):
        p = forge.parser('pr comment-edit <N> <ID> --body-file F')
        p.add_argument('number', type=forge.positive); p.add_argument('comment', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        self.pin_repo()
        self.gh_write(['api', '-X', 'PATCH', f'repos/{{owner}}/{{repo}}/issues/comments/{a.comment}', '--input', '-', '--jq', '.id'],
                      {'body': Path(a.body_file).read_text(encoding='utf-8')}, f'edit comment {a.comment}')
        print(a.comment)

    # ---- inline review notes ---------------------------------------------------------------------------------------
    @staticmethod
    def note_payload(spec, head):
        payload = {'body': spec['body'], 'commit_id': head, 'path': spec['file']}
        if spec['line'] is None:
            payload['subject_type'] = 'file'   # a whole-file note: no fake line-1 anchor
            return payload
        payload.update(line=spec['end_line'] or spec['line'], side=spec['side'].upper())
        if spec['end_line']:
            payload.update(start_line=spec['line'], start_side=spec['side'].upper())
        return payload

    def pr_note(self, argv):
        """One inline note: a line, a line range (--line first, --end-line last) or, with no --line, a whole file."""
        p = forge.parser('pr note <N> --file <path> [--line N [--end-line M]] [--side left|right] --body-file F [--head <sha>]')
        p.add_argument('number', type=forge.positive); p.add_argument('--file', required=True)
        p.add_argument('--line', type=forge.positive); p.add_argument('--end-line', type=forge.positive)
        p.add_argument('--side'); p.add_argument('--body-file', required=True); p.add_argument('--head')
        a = p.parse_args(argv)
        spec = note_spec({'file': a.file, 'line': int(a.line) if a.line else None,
                          'end_line': int(a.end_line) if a.end_line else None, 'side': a.side,
                          'body': Path(a.body_file).read_text(encoding='utf-8')})
        if a.head and not SHA40.match(a.head):
            raise forge.Usage('--head must be the full 40-hex head sha')
        self.pin_repo()
        head = a.head or self.current_head(a.number)
        if a.head:
            self.require_head(a.number, a.head)
        out = self.gh_write(['api', '-X', 'POST', self.pr_path(a.number, '/comments'), '--input', '-', '--jq', '.id'],
                            self.note_payload(spec, head), f'note on pull request #{a.number}')
        print(out.strip())

    def pr_notes(self, argv):
        """Several notes from a JSON file: `[{file, line?, end_line?, side?, body}]`. Line notes go in ONE review
        (`event: COMMENT`, so reviewers get one notification); a whole-file note is its own comment (a review
        entry cannot carry `subject_type`). Prints `[{index, id}]`."""
        p = forge.parser('pr notes <N> --notes-file F [--head <sha>]')
        p.add_argument('number', type=forge.positive); p.add_argument('--notes-file', required=True); p.add_argument('--head')
        a = p.parse_args(argv)
        specs = load_notes(a.notes_file)
        if a.head and not SHA40.match(a.head):
            raise forge.Usage('--head must be the full 40-hex head sha')
        self.pin_repo()
        head = a.head or self.current_head(a.number)
        if a.head:
            self.require_head(a.number, a.head)
        ids = {}
        for i, spec in enumerate(specs):
            if spec['line'] is None:
                ids[i] = self.gh_write(['api', '-X', 'POST', self.pr_path(a.number, '/comments'), '--input', '-', '--jq', '.id'],
                                       self.note_payload(spec, head), f'note {i} on pull request #{a.number}').strip()
        line_notes = [(i, s) for i, s in enumerate(specs) if s['line'] is not None]
        if line_notes:
            comments = []
            for _, spec in line_notes:
                one = self.note_payload(spec, head)
                one.pop('commit_id')
                comments.append(one)
            review = self.gh_write(['api', '-X', 'POST', self.pr_path(a.number, '/reviews'), '--input', '-', '--jq', '.id'],
                                   {'commit_id': head, 'event': 'COMMENT', 'comments': comments},
                                   f'review of pull request #{a.number}').strip()
            listed = [c for page in json.loads(self.read(
                ['api', '--paginate', '--slurp', self.pr_path(a.number, f'/reviews/{review}/comments?per_page=100')], 'comments'))
                for c in page]
            for i, spec in line_notes:
                want = spec['end_line'] or spec['line']
                hit = next((c for c in listed if c.get('path') == spec['file'] and c.get('body') == spec['body']
                            and c.get('line') in (want, None)), None)
                if hit is not None:
                    listed.remove(hit)
                    ids[i] = str(hit['id'])
                else:
                    ids[i] = None  # written, id not read back: `pr threads` has it
        print(json.dumps([{'index': i, 'id': ids.get(i)} for i in range(len(specs))]))

    def pr_note_edit(self, argv):
        p = forge.parser('pr note-edit <N> <ID> --body-file F')
        p.add_argument('number', type=forge.positive); p.add_argument('comment', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        self.pin_repo()
        self.gh_write(['api', '-X', 'PATCH', f'repos/{{owner}}/{{repo}}/pulls/comments/{a.comment}', '--input', '-', '--jq', '.id'],
                      {'body': Path(a.body_file).read_text(encoding='utf-8')}, f'edit note {a.comment}')
        print(a.comment)

    def pr_thread_resolve(self, argv):
        p = forge.parser('pr thread-resolve <N> <thread id>')
        p.add_argument('number', type=forge.positive); p.add_argument('thread')
        a = p.parse_args(argv)
        if not re.fullmatch(r'[A-Za-z0-9_=-]+', a.thread):
            raise forge.Usage(f'not a review thread id: {a.thread}')
        # A write (GraphQL mutation): run once, never through the retrying read helper.
        self.gh_write(['api', 'graphql', '-f', 'query=' + RESOLVE_MUTATION, '-f', f'threadId={a.thread}', '--jq',
                       '.data.resolveReviewThread.thread.isResolved'], None, f'resolve thread {a.thread}')
        print(f'resolved {a.thread}')
