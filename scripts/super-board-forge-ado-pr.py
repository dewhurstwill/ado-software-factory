"""super-board-forge-ado-pr.py: the Azure DevOps PULL REQUEST verbs for super-board-forge.py.

Not a command. The forge loads it and mixes `AdoPrVerbs` into `AzureDevOpsBackend`
(before the tracker's `AdoVerbs`, whose transport `read()/write()/written()` it reuses).
Each verb answers in the shape the GitHub backend does, so merge gate, pr-body writer,
preflight and merge policy run unchanged. Contract: skills/super-board/references/providers.md.

Three things Azure Repos does not give us, handled here:
  - no added/deleted line counts and no unified diff: `pr metadata` / `pr diff` come from
    local git (fetch, merge-base, one `--no-renames` base..head pair for both);
  - a 4,000-character description cap: the history, then the visual, block overflows into
    one dedicated PR thread (first line `<!-- sb:overflow -->`) and `pr body` puts it back;
  - completion is asynchronous and `lastMergeCommit` is already set while the PR is active
    (the service's trial merge): `pr merge` pins the head, then POLLS for `completed`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

forge = None  # the forge module, set by bind()
ado = None    # super-board-forge-ado.py, set by bind()

CAP = 4000
OVERFLOW_MARK = '<!-- sb:overflow -->'
OVERFLOW_QUIET = OVERFLOW_MARK + '\n_Nothing is moved out of the description at the moment._'
MOVED = '_Moved to the first comment thread (Azure DevOps limits a description to 4,000 characters)._'
MOVABLE = ('history', 'visual')  # moved in this order, only as far as needed
SHA = re.compile(r'[0-9a-f]{40}')
STATE_OF = {'active': 'OPEN', 'completed': 'MERGED', 'abandoned': 'CLOSED'}
LIST_STATUS = {'open': 'active', 'merged': 'completed'}
BAD_MERGE_STATUS = ('conflicts', 'rejectedByPolicy', 'failure')
EXIT_CONFLICT, EXIT_HEAD_MOVED, EXIT_POLICY, EXIT_REFUSED, EXIT_FAILED = 5, 6, 7, 65, 69


def bind(forge_module, ado_module):
    global forge, ado
    forge, ado = forge_module, ado_module


# ---- pure helpers -----------------------------------------------------------------------
def units(text):
    """Length as the service counts it: UTF-16 code units (an emoji is 2)."""
    return len(text.encode('utf-16-le')) // 2


def branch_of(ref):
    return ref[len('refs/heads/'):] if ref.startswith('refs/heads/') else ref


def block_span(text, name):
    opening, closing = f'<!-- sb:{name} -->', f'<!-- /sb:{name} -->'
    i = text.find(opening)
    if i < 0:
        return None
    j = text.find(closing, i + len(opening))
    return None if j < 0 else (i, j + len(closing))


def stub(name):
    return f'<!-- sb:{name} -->\n{MOVED}\n<!-- /sb:{name} -->'


class TooLong(Exception):
    def __init__(self, size):
        super().__init__(size)
        self.size = size


def split_body(body):
    """(description, moved): moved maps block name -> its full text, in MOVABLE order."""
    if units(body) <= CAP:
        return body, {}
    description, moved = body, {}
    for name in MOVABLE:
        span = block_span(description, name)
        if span is None or description[span[0] + len(f'<!-- sb:{name} -->'):span[1] - len(f'<!-- /sb:{name} -->')].strip() == MOVED:
            continue
        moved[name] = description[span[0]:span[1]]
        description = description[:span[0]] + stub(name) + description[span[1]:]
        if units(description) <= CAP:
            return description, moved
    raise TooLong(units(description))


def join_body(description, overflow):
    """Undo split_body: put each moved block back in place of its stub."""
    out = description
    for name in MOVABLE:
        moved = block_span(overflow, name)
        here = block_span(out, name)
        if moved is None or here is None:
            continue
        inner = out[here[0] + len(f'<!-- sb:{name} -->'):here[1] - len(f'<!-- /sb:{name} -->')]
        if inner.strip() == MOVED:
            out = out[:here[0]] + overflow[moved[0]:moved[1]] + out[here[1]:]
    return out


def overflow_text(moved):
    return OVERFLOW_MARK + '\n' + '\n\n'.join(moved.values())


def v_pr(value):
    ado.need(isinstance(value, dict) and type(value.get('pullRequestId')) is int
             and value.get('status') in STATE_OF
             and isinstance(value.get('sourceRefName'), str) and value['sourceRefName'].startswith('refs/heads/')
             and isinstance(value.get('targetRefName'), str) and value['targetRefName'].startswith('refs/heads/'),
             'pull request malformed')
    commit = (value.get('lastMergeSourceCommit') or {}).get('commitId')
    ado.need(isinstance(commit, str) and SHA.fullmatch(commit) is not None, 'pull request source commit missing')


def v_pr_list(value):
    ado.need(isinstance(value, dict) and isinstance(value.get('value'), list), 'pull request list missing')
    ado.need(all(isinstance(p, dict) and type(p.get('pullRequestId')) is int and isinstance(p.get('sourceRefName'), str)
                 for p in value['value']), 'pull request list malformed')


def v_threads(value):
    ado.need(isinstance(value, dict) and isinstance(value.get('value'), list), 'threads missing')
    ado.need(all(isinstance(t, dict) and type(t.get('id')) is int and isinstance(t.get('comments'), list)
                 and all(isinstance(c, dict) and type(c.get('id')) is int for c in t['comments'])
                 for t in value['value']), 'thread fields missing')


def v_labels(value):
    ado.need(isinstance(value, dict) and isinstance(value.get('value'), list)
             and all(isinstance(l, dict) and isinstance(l.get('name'), str) for l in value['value']), 'labels malformed')


def v_iterations(value):
    ado.need(isinstance(value, dict) and isinstance(value.get('value'), list)
             and all(isinstance(i, dict) and type(i.get('id')) is int for i in value['value']), 'iterations malformed')


def v_changes(value):
    ado.need(isinstance(value, dict) and isinstance(value.get('changeEntries'), list), 'changes missing')


def v_workitems(value):
    ado.need(isinstance(value, dict) and isinstance(value.get('value'), list)
             and all(isinstance(w, dict) and re.fullmatch(r'[0-9]+', str(w.get('id', ''))) for w in value['value']),
             'linked work items malformed')


def comment_text(comment):
    return comment.get('content') or ''


def is_system(comment):
    return str(comment.get('commentType') or '').lower() == 'system'


def live(comment):
    return not comment.get('isDeleted') and not is_system(comment)


# ---- the verbs --------------------------------------------------------------------------------
class AdoPrVerbs:
    # ---- urls ----------------------------------------------------------------------------------
    def pr_api(self, n=None, tail='', extra=''):
        base = f'{self.proj_api}/git/repositories/{ado.q(self.repository)}/pullrequests'
        path = f'/{n}' if n else ''
        return f'{base}{path}{tail}?{extra}api-version={ado.API}'

    def pr_web(self, n):
        return f'{self.web_org}/{ado.q(self.project)}/_git/{ado.q(self.repository)}/pullrequest/{n}'

    def repo_dir(self):
        return Path((self.config.get('repo') or {}).get('path') or os.environ.get('SB_REPO_PATH') or '.')

    # ---- reads ----------------------------------------------------------------------------------
    def get_pr(self, n):
        return self.read('GET', self.pr_api(n), validate=v_pr, fixed=False, what=f'pull request #{n}')

    def get_threads(self, n):
        return self.read('GET', self.pr_api(n, '/threads'), validate=v_threads, fixed=False,
                         what=f'threads of pull request #{n}')['value']

    @staticmethod
    def head_of(pr):
        return pr['lastMergeSourceCommit']['commitId']

    def find_overflow(self, n):
        for thread in self.get_threads(n):
            first = next((c for c in thread['comments'] if not c.get('isDeleted')), None)
            if first is not None and not is_system(first) and comment_text(first).startswith(OVERFLOW_MARK):
                return {'thread': thread['id'], 'comment': first['id'], 'content': comment_text(first)}
        return None

    def logical_body(self, pr):
        description = pr.get('description') or ''
        if MOVED not in description:
            return description
        found = self.find_overflow(pr['pullRequestId'])
        return join_body(description, found['content']) if found else description

    # ---- writes -----------------------------------------------------------------------------------
    def patch_pr(self, n, body, what):
        status, parsed, text = self.write('PATCH', self.pr_api(n), body, what)
        return self.written(status, parsed, text, what)

    def post_thread(self, n, content):
        body = {'comments': [{'parentCommentId': 0, 'content': content, 'commentType': 1}], 'status': 1}
        status, parsed, text = self.write('POST', self.pr_api(n, '/threads'), body, f'comment on pull request #{n}')
        made = self.written(status, parsed, text, f'comment on pull request #{n}')
        comments = made.get('comments') or []
        if type(made.get('id')) is not int or not comments or type(comments[0].get('id')) is not int:
            raise forge.ForgeError(f'comment on pull request #{n}: the response carried no thread or comment id', EXIT_FAILED)
        return made['id'], comments[0]['id']

    def edit_comment(self, n, thread, comment, content):
        what = f'edit comment {thread}.{comment} on pull request #{n}'
        status, parsed, text = self.write('PATCH', self.pr_api(n, f'/threads/{thread}/comments/{comment}'),
                                          {'content': content}, what)
        self.written(status, parsed, text, what)

    def write_overflow(self, n, content):
        found = self.find_overflow(n)
        if found is None:
            self.post_thread(n, content)
        elif found['content'] != content:
            self.edit_comment(n, found['thread'], found['comment'], content)

    @staticmethod
    def too_long(error):
        return forge.ForgeError(f'the pull request body is {error.size} characters after moving the history and visual '
                                f'blocks out; Azure DevOps allows {CAP} in a description. Shorten the body; nothing was written.',
                                EXIT_REFUSED)

    # ---- head, state ---------------------------------------------------------------------------------
    def pr_head(self, argv):
        p = forge.parser('pr head <N>'); p.add_argument('number', type=forge.positive)
        pr = self.get_pr(p.parse_args(argv).number)
        print(f'{branch_of(pr["sourceRefName"])} {self.head_of(pr)}')

    def pr_state(self, argv):
        p = forge.parser('pr state <N>'); p.add_argument('number', type=forge.positive)
        pr = self.get_pr(p.parse_args(argv).number)
        merge = None
        if pr['status'] == 'completed':
            oid = (pr.get('lastMergeCommit') or {}).get('commitId')
            if not (isinstance(oid, str) and SHA.fullmatch(oid)):
                raise forge.ForgeError('pull request is completed but the service gave no merge commit', EXIT_FAILED)
            merge = {'oid': oid}  # lastMergeCommit is only the merge commit once completed; before, it is a trial
        print(json.dumps({'state': STATE_OF[pr['status']], 'headRefOid': self.head_of(pr), 'mergeCommit': merge}))

    # ---- metadata and diff: local git ----------------------------------------------------------------
    def git(self, *args):
        exe = shutil.which('git')
        if not exe:
            raise forge.ForgeError('git was not found on PATH', EXIT_FAILED)
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        try:
            r = subprocess.run([exe, '-C', str(self.repo_dir()), '-c', 'color.ui=never', *args],
                               capture_output=True, timeout=600, env=env)
        except (OSError, subprocess.SubprocessError) as error:
            raise forge.ForgeError(f'git {args[0]} could not run: {error}', EXIT_FAILED)
        return r.returncode, r.stdout, r.stderr.decode('utf-8', 'replace').strip()

    def git_ok(self, *args):
        rc, out, err = self.git(*args)
        if rc:
            raise forge.ForgeError(f'git {" ".join(args[:2])} failed in {self.repo_dir()}: {err[:300]}', EXIT_FAILED)
        return out.decode('utf-8', 'replace').strip()

    def local_change(self, n):
        """(pr, base, head): the PR, the merge-base with its target, and the source head, all
        from one fetched state. A head that moved between the API read and the fetch is re-read once."""
        for attempt in (1, 2):
            pr = self.get_pr(n)
            src, tgt, head = branch_of(pr['sourceRefName']), branch_of(pr['targetRefName']), self.head_of(pr)
            self.git_ok('fetch', '--quiet', 'origin', f'+refs/heads/{src}:refs/remotes/origin/{src}',
                        f'+refs/heads/{tgt}:refs/remotes/origin/{tgt}')
            tip = self.git_ok('rev-parse', '--verify', f'refs/remotes/origin/{src}^{{commit}}')
            if tip == head:
                break
        else:
            raise forge.ForgeError(f'pull request #{n}: branch {src} is at {tip} but the service reports {head} as its '
                                   'source commit (a push is still being processed); try again shortly', EXIT_FAILED)
        base = self.git_ok('merge-base', f'refs/remotes/origin/{tgt}', head)
        return pr, base, head

    DIFF_FLAGS = ('--no-renames', '--no-ext-diff', '--no-textconv', '--no-color')

    def numstat(self, base, head):
        rc, out, err = self.git('diff', '--numstat', '-z', *self.DIFF_FLAGS, base, head, '--')
        if rc:
            raise forge.ForgeError(f'git diff --numstat failed: {err[:300]}', EXIT_FAILED)
        files = []
        for record in out.decode('utf-8', 'replace').split('\0'):
            if not record:
                continue
            added, removed, path = record.split('\t', 2)
            files.append({'path': path,  # git prints forward slashes on every platform
                          'additions': int(added) if added.isdigit() else 0,  # '-' = binary
                          'deletions': int(removed) if removed.isdigit() else 0})
        return files

    def pr_metadata(self, argv):
        p = forge.parser('pr metadata <N>'); p.add_argument('number', type=forge.positive)
        meta, _ = self.build_metadata(p.parse_args(argv).number)
        print(json.dumps(meta))

    def pr_labels(self, n):
        """From the labels endpoint: the single-PR GET carries no `labels` key. A failed read is a
        failed read (halts like any required read), never "no labels": merge policy matches on them."""
        value = self.read('GET', self.pr_api(n, '/labels'), validate=v_labels, fixed=False,
                          what=f'labels of pull request #{n}')
        return [{'name': l['name']} for l in value['value'] if l.get('name') and l.get('active') is not False]

    def build_metadata(self, n):
        pr, base, head = self.local_change(n)
        files = self.numstat(base, head)
        meta = {'labels': self.pr_labels(n),
                'files': files, 'additions': sum(f['additions'] for f in files),
                'deletions': sum(f['deletions'] for f in files), 'changedFiles': len(files),
                'body': self.logical_body(pr)}
        self.self_check(json.dumps(meta), 'metadata')
        return meta, (base, head)

    @staticmethod
    def self_check(text, kind, meta=None):
        try:
            forge.github_read.validate(text, kind, meta)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
            raise forge.ForgeError(f'azure-devops: computed {kind} failed validation: {error}', EXIT_FAILED)

    def pr_diff(self, argv):
        p = forge.parser('pr diff <N> --meta <file>'); p.add_argument('number', type=forge.positive)
        p.add_argument('--meta')
        a = p.parse_args(argv)
        if not a.meta:
            raise forge.Usage('pr diff needs --meta <file> (from pr metadata, taken right before)')
        meta = json.loads(Path(a.meta).read_text(encoding='utf-8'))
        _, base, head = self.local_change(a.number)
        rc, out, err = self.git('diff', '-U3', *self.DIFF_FLAGS, base, head, '--')
        if rc:
            raise forge.ForgeError(f'git diff failed: {err[:300]}', EXIT_FAILED)
        text = out.decode('utf-8', 'replace')
        self.self_check(text, 'diff', meta)
        sys.stdout.write(text)

    # ---- body ---------------------------------------------------------------------------------------------
    def pr_body(self, argv):
        p = forge.parser('pr body <N>'); p.add_argument('number', type=forge.positive)
        pr = self.get_pr(p.parse_args(argv).number)
        print(json.dumps({'headRefOid': self.head_of(pr), 'body': self.logical_body(pr)}))

    def pr_set_body(self, argv):
        p = forge.parser('pr set-body <N> --body-file <f>'); p.add_argument('number', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        n = a.number
        body = Path(a.body_file).read_text(encoding='utf-8')
        try:
            description, moved = split_body(body)
        except TooLong as error:
            raise self.too_long(error)
        if moved:  # content first: a failure after it loses nothing
            self.write_overflow(n, overflow_text(moved))
            self.patch_pr(n, {'description': description}, f'update the description of pull request #{n}')
            return
        before = self.get_pr(n).get('description') or ''
        self.patch_pr(n, {'description': description}, f'update the description of pull request #{n}')
        if MOVED in before:  # shrunk back: keep the thread, say so
            found = self.find_overflow(n)
            if found and found['content'] != OVERFLOW_QUIET:
                self.edit_comment(n, found['thread'], found['comment'], OVERFLOW_QUIET)

    # ---- create, ready ----------------------------------------------------------------------------------------
    def pr_create(self, argv):
        p = forge.parser('pr create --base B --head H --title T --body-file F [--draft] [--label L]... [--closes <ticket>]...')
        p.add_argument('--base', required=True); p.add_argument('--head', required=True)
        p.add_argument('--title', required=True); p.add_argument('--body-file', required=True)
        p.add_argument('--draft', action='store_true')
        p.add_argument('--label', action='append', default=[]); p.add_argument('--closes', action='append', default=[])
        a = p.parse_args(argv)
        body = Path(a.body_file).read_text(encoding='utf-8')
        try:
            description, moved = split_body(body)
        except TooLong as error:
            raise self.too_long(error)
        refs = []
        for ticket in a.closes:
            number = ticket.lstrip('#')
            if not re.fullmatch(r'[1-9][0-9]*', number):
                raise forge.Usage(f'--closes needs a work item number, got: {ticket}')
            if {'id': number} not in refs:
                refs.append({'id': number})
        payload = {'sourceRefName': 'refs/heads/' + branch_of(a.head), 'targetRefName': 'refs/heads/' + branch_of(a.base),
                   'title': a.title, 'description': description, 'isDraft': a.draft,
                   'labels': [{'name': l} for l in dict.fromkeys(a.label)], 'workItemRefs': refs}
        status, parsed, text = self.write('POST', self.pr_api(), payload, 'pull request create')
        made = self.written(status, parsed, text, 'pull request create')
        n = made.get('pullRequestId')
        if type(n) is not int:
            raise forge.ForgeError('pull request create: the response carried no pull request id', EXIT_FAILED)
        if moved:
            try:
                self.post_thread(n, overflow_text(moved))
            except forge.ForgeError as error:
                print(f'created pull request #{n} but could not move the overflow into a thread: {error}', file=sys.stderr)
                raise
        print(self.pr_web(n))
        print(n)

    def pr_ready(self, argv):
        p = forge.parser('pr ready <N>'); p.add_argument('number', type=forge.positive)
        n = p.parse_args(argv).number
        self.patch_pr(n, {'isDraft': False}, f'publish pull request #{n}')
        print(f'ready #{n}')

    # ---- merge ------------------------------------------------------------------------------------------------------
    def pr_merge(self, argv):
        p = forge.parser('pr merge <N> --squash --delete-branch --match-head <sha>')
        p.add_argument('number', type=forge.positive)
        p.add_argument('--squash', action='store_true'); p.add_argument('--delete-branch', action='store_true')
        p.add_argument('--subject'); p.add_argument('--body-file'); p.add_argument('--match-head', required=True)
        a = p.parse_args(argv)
        if not (a.squash and a.delete_branch):
            raise forge.Usage('pr merge needs --squash and --delete-branch')
        if not SHA.fullmatch(a.match_head):
            raise forge.Usage('--match-head must be the full 40-hex source commit (the service pins the merge to it)')
        n = a.number
        pr = self.get_pr(n)
        if pr['status'] != 'active':
            raise forge.ForgeError(f'pull request #{n} is {pr["status"]}; nothing to complete', EXIT_REFUSED)
        if pr.get('isDraft'):
            raise forge.ForgeError(f'pull request #{n} is a draft and cannot be completed; run `pr ready {n}` first', EXIT_REFUSED)
        if pr.get('mergeStatus') in BAD_MERGE_STATUS:
            raise self.refusal(pr, n, 'cannot be completed')
        options = {'mergeStrategy': 'squash', 'deleteSourceBranch': True, 'transitionWorkItems': False}
        body = Path(a.body_file).read_text(encoding='utf-8').strip('\n') if a.body_file else ''
        if a.subject or body:
            options['mergeCommitMessage'] = (a.subject or pr.get('title') or '') + (f'\n\n{body}' if body else '')
        status, parsed, text = self.write('PATCH', self.pr_api(n),
                                          {'status': 'completed', 'lastMergeSourceCommit': {'commitId': a.match_head},
                                           'completionOptions': options}, f'complete pull request #{n}')
        if status == 409 and 'TF401192' in text:
            raise forge.ForgeError(f'head moved: the source branch of pull request #{n} is no longer {a.match_head} '
                                   '(TF401192); nothing was merged', EXIT_HEAD_MOVED)
        if 400 <= status < 500 and status not in (401, 408, 429):
            # The service answered and merged nothing. Seen live: a branch policy that is not
            # met (required reviewers, a required build) is refused HERE with HTTP 403
            # "The pull request needs a minimum number of approvals (1) from other users
            # before it can be completed." -- mergeStatus stays "succeeded", so the
            # rejectedByPolicy status below never appears for it. A 403 is a human's call
            # either way (policy, or this identity may not complete the PR).
            message = ((parsed or {}).get('message') if isinstance(parsed, dict) else None) or text[:300]
            if status == 403:
                print(f'human-gate: policy — Azure DevOps will not let this identity complete the pull request: {message}')
                raise forge.ForgeError(f'Azure DevOps refused to complete pull request #{n} (HTTP 403): {message}', EXIT_POLICY)
            raise forge.ForgeError(f'Azure DevOps refused to complete pull request #{n} (HTTP {status}): {message}', EXIT_REFUSED)
        self.written(status, parsed, text, f'complete pull request #{n}')
        self.await_completion(n)

    @staticmethod
    def refusal(pr, n, what):
        """A definite refusal (nothing merged) with its own exit code: 5 conflicts, 7 branch policy
        (also a `human-gate:` line on stdout, as the gate's policy hold prints), 65 anything else."""
        status = pr['mergeStatus']
        detail = pr.get('mergeFailureMessage') or ''
        if status == 'rejectedByPolicy':
            why = detail or "see the PR's policy list"
            print(f'human-gate: policy — Azure DevOps branch policy blocks completion: {why}')
        code = {'conflicts': EXIT_CONFLICT, 'rejectedByPolicy': EXIT_POLICY}.get(status, EXIT_REFUSED)
        return forge.ForgeError(f'pull request #{n} {what}: mergeStatus {status}' + (f': {detail}' if detail else ''), code)

    def await_completion(self, n):
        """The completion PATCH answers 200 and still says active/queued; poll until `completed`.
        Never re-sends the completion."""
        interval = max(0.0, float(os.environ.get('SB_ADO_MERGE_POLL_INTERVAL', '2')))
        deadline = time.monotonic() + float(os.environ.get('SB_ADO_MERGE_POLL_TIMEOUT', '90'))
        while True:
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            pr = self.get_pr(n)
            if pr['status'] == 'completed':
                oid = (pr.get('lastMergeCommit') or {}).get('commitId')
                if isinstance(oid, str) and SHA.fullmatch(oid):
                    print(f'merged #{n} as {oid}')
                    return
            elif pr['status'] == 'abandoned':
                raise forge.ForgeError(f'pull request #{n} was abandoned while it was being completed', EXIT_FAILED)
            elif pr.get('mergeStatus') in BAD_MERGE_STATUS:
                raise self.refusal(pr, n, 'was not completed')
            if time.monotonic() >= deadline:
                raise forge.ForgeError(f'pull request #{n}: completion not confirmed within '
                                       f'{os.environ.get("SB_ADO_MERGE_POLL_TIMEOUT", "90")}s; outcome unknown, not retried', EXIT_FAILED)

    # ---- list ------------------------------------------------------------------------------------------------------------
    def changed_paths(self, n):
        iterations = self.read('GET', self.pr_api(n, '/iterations'), validate=v_iterations, fixed=False,
                               what=f'iterations of pull request #{n}')['value']
        if not iterations:
            return []
        last = max(i['id'] for i in iterations)
        paths, skip = [], 0
        while True:
            value = self.read('GET', self.pr_api(n, f'/iterations/{last}/changes', f'$top=2000&$skip={skip}&'),
                              validate=v_changes, fixed=False, what=f'changes of pull request #{n}')
            for entry in value['changeEntries']:
                item = entry.get('item') or {}
                if item.get('path') and not item.get('isFolder'):
                    paths.append(item['path'].lstrip('/'))
            more = value.get('nextSkip')
            if not more or more <= skip:
                return list(dict.fromkeys(paths))
            skip = more

    def pr_list(self, argv):
        p = forge.parser('pr list --state open|merged --limit N')
        p.add_argument('--state', required=True, choices=sorted(LIST_STATUS))
        p.add_argument('--limit', required=True, type=forge.positive)
        a = p.parse_args(argv)
        value = self.read('GET', self.pr_api(extra=f'searchCriteria.status={LIST_STATUS[a.state]}&$top={a.limit}&'),
                          validate=v_pr_list, what='pull request list')
        out = []
        for pr in value['value'][:int(a.limit)]:
            n = pr['pullRequestId']
            entry = {'number': n, 'title': pr.get('title') or '', 'body': pr.get('description') or '', 'url': self.pr_web(n)}
            if a.state == 'open':
                entry['headRefName'] = branch_of(pr['sourceRefName'])
                entry['files'] = [{'path': path} for path in self.changed_paths(n)]
            out.append(entry)
        print(json.dumps(out))

    # ---- comments, links ---------------------------------------------------------------------------------------------------
    def pr_comment(self, argv):
        p = forge.parser('pr comment <N> --body-file F'); p.add_argument('number', type=forge.positive)
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        thread, comment = self.post_thread(a.number, Path(a.body_file).read_text(encoding='utf-8'))
        print(f'{thread}.{comment}')

    def pr_comment_rows(self, n):
        """The `pr comments` rows plus what approval needs: `uniqueName` (never the display name),
        `inline` (the thread is anchored to a file, like a GitHub review comment) and
        `contentUpdated` (lastContentUpdatedDate, else lastUpdatedDate, else ''). `updated_at` is the
        content edit time: lastUpdatedDate also moves for things that are not an edit of the text."""
        rows = []
        for thread in self.get_threads(n):
            first = next((c for c in thread['comments'] if not c.get('isDeleted')), None)
            if first is not None and not is_system(first) and comment_text(first).startswith(OVERFLOW_MARK):
                continue
            for c in thread['comments']:
                if not live(c):
                    continue
                author = c.get('author') or {}
                content = c.get('lastContentUpdatedDate') or c.get('lastUpdatedDate') or ''
                rows.append((c.get('publishedDate') or '', thread['id'], c['id'],
                             {'id': f'{thread["id"]}.{c["id"]}', 'body': comment_text(c),
                              'user': {'login': author.get('uniqueName') or author.get('displayName') or '', 'type': 'User'},
                              'created_at': c.get('publishedDate') or '',
                              'updated_at': content or c.get('publishedDate') or '',
                              'uniqueName': author.get('uniqueName') or '',
                              'inline': bool(thread.get('threadContext')), 'contentUpdated': content}))
        rows.sort(key=lambda r: r[:3])
        return [r[3] for r in rows]

    def pr_comments(self, argv):
        p = forge.parser('pr comments <N>'); p.add_argument('number', type=forge.positive)
        private = ('uniqueName', 'inline', 'contentUpdated')
        rows = self.pr_comment_rows(p.parse_args(argv).number)
        print(json.dumps([[{k: v for k, v in r.items() if k not in private} for r in rows]]))

    def pr_comment_edit(self, argv):
        p = forge.parser('pr comment-edit <N> <threadId>.<commentId> --body-file F')
        p.add_argument('number', type=forge.positive); p.add_argument('comment')
        p.add_argument('--body-file', required=True)
        a = p.parse_args(argv)
        found = re.fullmatch(r'([0-9]+)\.([0-9]+)', a.comment)
        if not found:
            raise forge.Usage(f'comment id must be <threadId>.<commentId>, got: {a.comment}')
        self.edit_comment(a.number, found[1], found[2], Path(a.body_file).read_text(encoding='utf-8'))
        print(a.comment)

    def linked_work_items(self, n):
        value = self.read('GET', self.pr_api(n, '/workitems'), validate=v_workitems, fixed=False,
                          what=f'work items of pull request #{n}')
        return sorted({int(w['id']) for w in value['value']})

    def pr_links(self, argv):
        p = forge.parser('pr links <N>'); p.add_argument('number', type=forge.positive)
        print(json.dumps({'closes': self.linked_work_items(p.parse_args(argv).number)}))

    def approval_snapshot(self, n):
        """{state, headRefOid, closes}: the PR, then the service's own work item links (no text parsing,
        whatever the target branch)."""
        pr = self.get_pr(n)
        return {'state': STATE_OF[pr['status']], 'headRefOid': self.head_of(pr), 'closes': self.linked_work_items(n)}

    def pr_approval(self, argv):
        p = forge.parser('pr approval <N>'); p.add_argument('number', type=forge.positive)
        print(json.dumps(self.approval_snapshot(int(p.parse_args(argv).number))))
