#!/usr/bin/env python3
"""Fixture answers for the offline `gh` stub (evals/_stubs/bin/gh), for the argv the forge's GitHub
backend sends (scripts/super-board-forge-github.py, super-board-card.sh): REST `api repos/...` reads
and writes, the GraphQL queries (board, review threads, checks, approval links, ticket history and
graph), and the enriched PR object `pr view` / `pr list` print. Not a command: the stub calls it as
`_gh_fixture.py <repo root> pr-json` or `_gh_fixture.py <repo root> api <gh args...>`.

Fixtures (all in <root>/.git/gh-fixture/, all optional): pr.json, diff.patch, issue.json, issues.json,
project-items.json, fields.json, graphql.json. Writes are recorded, never sent: ticket bodies to
.git/gh-created.md, comments and notes to .git/gh-comments.md, PRs to .git/gh-prs.md; state the
lanes read back (posted comments, assignees) lives in .git/gh-state.json.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
FX = ROOT / '.git' / 'gh-fixture'
STATE = ROOT / '.git' / 'gh-state.json'
BOT = 'super-board-bot'
ZERO = '0' * 40
DEFAULT_FIELDS = {'fields': [{'id': 'PVTSSF_status', 'name': 'Status', 'type': 'ProjectV2SingleSelectField',
                              'options': [{'id': 'opt_backlog', 'name': 'Backlog'}, {'id': 'opt_ready', 'name': 'Ready'},
                                          {'id': 'opt_progress', 'name': 'In progress'}, {'id': 'opt_done', 'name': 'Done'}]}]}


def load(name, default=None):
    path = FX / name
    if not path.is_file():
        return default
    return path.read_text() if name.endswith('.patch') else json.loads(path.read_text())


def state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {'comments': {}, 'assignees': {}, 'next_id': 5000}


def save(value):
    STATE.write_text(json.dumps(value))


def record(name, heading, body):
    with (ROOT / '.git' / name).open('a') as f:
        f.write(f'=== {heading}\n{body}\n\n')


def git(*args):
    r = subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


def head_of(branch):
    for ref in (f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'):
        sha = git('rev-parse', '--verify', '-q', ref)
        if re.fullmatch(r'[0-9a-f]{40}', sha):
            return sha
    return ZERO


def diff_files(patch):
    """[{path, additions, deletions}] in diff order, counted exactly as the read helper validates."""
    files, current = [], None
    old_left = new_left = 0
    for line in patch.split('\n'):
        if line.startswith('diff --git '):
            current = {'path': line.split(' b/', 1)[-1], 'additions': 0, 'deletions': 0}
            files.append(current)
        elif line.startswith('@@ '):
            m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            old_left, new_left = int(m[2] or 1), int(m[4] or 1)
        elif (old_left or new_left) and line and line[0] in '+- ':
            if line[0] != '+':
                old_left -= 1
            if line[0] != '-':
                new_left -= 1
            if line[0] == '+':
                current['additions'] += 1
            if line[0] == '-':
                current['deletions'] += 1
    return files


def pr_object():
    """pr.json as `gh pr view --json` would print it, with the fields the forge validates filled in."""
    pr = dict(load('pr.json', {}) or {})
    pr.setdefault('number', 1)
    pr.setdefault('title', '')
    pr.setdefault('body', '')
    pr.setdefault('state', 'OPEN')
    pr.setdefault('isDraft', False)
    pr.setdefault('baseRefName', 'main')
    pr.setdefault('headRefName', 'feature')
    pr.setdefault('labels', [])
    pr.setdefault('url', f'https://github.com/example/shop/pull/{pr["number"]}')
    if not re.fullmatch(r'[0-9a-f]{40}', str(pr.get('headRefOid') or '')):
        pr['headRefOid'] = head_of(pr['headRefName'])
    patch = load('diff.patch')
    if patch is not None:
        pr['files'] = diff_files(patch)
    pr.setdefault('files', [])
    pr['additions'] = sum(f.get('additions', 0) for f in pr['files'])
    pr['deletions'] = sum(f.get('deletions', 0) for f in pr['files'])
    pr['changedFiles'] = len(pr['files'])
    pr.setdefault('mergeCommit', None)
    return pr


def pr_rest(pr):
    return {'number': pr['number'], 'title': pr['title'], 'body': pr['body'], 'html_url': pr['url'],
            'state': 'closed' if pr['state'] != 'OPEN' else 'open', 'merged': pr['state'] == 'MERGED',
            'draft': bool(pr.get('isDraft')), 'head': {'ref': pr['headRefName'], 'sha': pr['headRefOid']},
            'base': {'ref': pr['baseRefName']}, 'labels': pr['labels'],
            'merge_commit_sha': (pr.get('mergeCommit') or {}).get('oid')}


def issues():
    found = list(load('issues.json', []) or [])
    one = load('issue.json')
    if one and not any(i.get('number') == one.get('number') for i in found):
        found.append(one)
    return found


def issue(n):
    for i in issues():
        if i.get('number') == n:
            return i
    if (FX / 'pr.json').is_file():  # the stub's long-standing fallback ticket
        return {'number': n, 'title': pr_object()['title'],
                'body': 'Cart discount. AC: discount percent is clamped to 0-100; an empty cart totals 0.',
                'labels': [], 'comments': []}
    return None


def comment_rest(c, index):
    author = (c.get('author') or c.get('user') or {}).get('login') or BOT
    when = c.get('createdAt') or c.get('created_at') or '2026-01-01T00:00:00Z'
    return {'id': c.get('id') if type(c.get('id')) is int else 1000 + index, 'body': c.get('body') or '',
            'user': {'login': author, 'type': 'Bot' if author.endswith('[bot]') else 'User'},
            'created_at': when, 'updated_at': c.get('updated_at') or when}


def comments_of(n, st):
    pr = load('pr.json')
    if pr and pr.get('number') == n:
        base = pr.get('comments') or []
    else:
        base = (issue(n) or {}).get('comments') or []
    fixture = [comment_rest(c, i) for i, c in enumerate(base)]
    return fixture + st['comments'].get(str(n), [])


def status_of(n):
    for item in (load('project-items.json', {}) or {}).get('items', []):
        if (item.get('content') or {}).get('number') == n:
            return item.get('status')
    pr = load('pr.json') or {}
    return 'QA' if pr.get('isDraft') else 'Review'


def item_node(item):
    content = item.get('content') or {}
    labels = item.get('labels') or content.get('labels') or []
    labels = [l if isinstance(l, str) else l.get('name') for l in labels]
    node = {'__typename': content.get('type') or 'Issue', 'title': content.get('title') or item.get('title') or ''}
    if node['__typename'] != 'DraftIssue':
        node.update(number=content.get('number'), url=content.get('url') or '',
                    repository={'nameWithOwner': content.get('repository') or 'example/shop'},
                    labels={'nodes': [{'name': l} for l in labels if l]})
    return {'id': item.get('id') or f'PVTI_{content.get("number")}',
            'fieldValueByName': {'name': item['status']} if item.get('status') else None, 'content': node}


def graphql(query, fields, slurp):
    def out(page):
        return [page] if slurp else page
    n = int(fields.get('pr') or fields.get('issue') or 0)
    if 'resolveReviewThread' in query:
        return {'data': {'resolveReviewThread': {'thread': {'isResolved': True}}}}
    if 'updateProjectV2ItemFieldValue' in query:
        return {'data': {'updateProjectV2ItemFieldValue': {'projectV2Item': {'id': fields.get('i', '')}}}}
    if 'repositoryOwner' in query and 'items(first' in query:
        items = (load('project-items.json', {}) or {}).get('items', [])
        nodes = [item_node(i) for i in items]
        return out({'data': {'repositoryOwner': {'projectV2': {'items': {
            'totalCount': len(nodes), 'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': nodes}}}}})
    if 'field(name:"Status")' in query:
        status = next(f for f in (load('fields.json') or DEFAULT_FIELDS)['fields'] if f.get('name') == 'Status')
        return {'data': {'repositoryOwner': {'projectV2': {'id': 'PVT_eval', 'field': {
            'id': status['id'], 'options': [{'id': o['id'], 'name': o['name']} for o in status['options']]}}}}}
    if 'issueOrPullRequest' in query:
        node = {'id': f'PVTI_{n}', 'project': {'number': 1, 'owner': {'login': 'example'}},
                'fieldValueByName': {'name': status_of(n)}}
        return {'data': {'repository': {'issueOrPullRequest': {'projectItems': {'nodes': [node]}}}}}
    if 'reviewThreads' in query:
        nodes = []
        for i, t in enumerate((load('pr.json') or {}).get('reviewThreads') or []):
            comments = t.get('comments') or []
            comments = comments.get('nodes', []) if isinstance(comments, dict) else comments
            nodes.append({'id': t.get('id') or f'PRRT_eval{i}', 'isResolved': bool(t.get('isResolved')),
                          'isOutdated': bool(t.get('isOutdated')), 'path': t.get('path'), 'line': t.get('line'),
                          'startLine': t.get('startLine'), 'originalLine': t.get('originalLine'),
                          'diffSide': t.get('diffSide') or 'RIGHT',
                          'comments': {'nodes': [{'databaseId': c.get('databaseId') or 2000 + j, 'body': c.get('body') or '',
                                                  'createdAt': c.get('createdAt') or '2026-01-01T00:00:00Z',
                                                  'author': {'login': (c.get('author') or {}).get('login') or BOT,
                                                             '__typename': 'User'}}
                                                 for j, c in enumerate(comments)]}})
        return out({'data': {'repository': {'pullRequest': {'reviewThreads': {
            'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': nodes}}}}})
    if 'statusCheckRollup' in query:
        return out({'data': {'repository': {'pullRequest': {'commits': {'nodes': [
            {'commit': {'oid': pr_object()['headRefOid'], 'statusCheckRollup': None}}]}}}}})
    if 'closingIssuesReferences' in query:
        pr = pr_object()
        closes = sorted({int(m) for m in re.findall(r'(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)', pr['body'])})
        return out({'data': {'repository': {'defaultBranchRef': {'name': 'main'}, 'pullRequest': {
            'headRefOid': pr['headRefOid'], 'state': pr['state'], 'baseRefName': pr['baseRefName'], 'body': pr['body'],
            'closingIssuesReferences': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                        'nodes': [{'number': c, 'repository': {'nameWithOwner': fields.get('owner', 'example') + '/' + fields.get('repo', 'shop')}} for c in closes]}}}}})
    if 'states:[OPEN,CLOSED]' in query:
        nodes = [{'number': i['number'], 'state': str(i.get('state') or 'OPEN').upper(), 'body': i.get('body') or ''}
                 for i in issues()]
        return out({'data': {'repository': {'issues': {'totalCount': len(nodes),
                    'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': nodes}}}})
    if 'issues(states:OPEN' in query:
        st = state()
        nodes = [{'number': i['number'], 'title': i.get('title') or '', 'body': i.get('body') or '', 'state': 'OPEN',
                  'labels': {'nodes': [{'name': l['name']} for l in i.get('labels') or []]},
                  'comments': {'nodes': [{'body': c['body']} for c in comments_of(i['number'], st)[-8:]]}}
                 for i in issues() if str(i.get('state') or 'OPEN').upper() == 'OPEN']
        return out({'data': {'repository': {'issues': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                                       'nodes': nodes}}}})
    return load('graphql.json', {})


def rest(method, path, payload, slurp):
    st = state()
    # gh fills {owner}/{repo} from GH_REPO (the forge sets it from the config), else the checkout
    path = path.replace('{owner}/{repo}', os.environ.get('GH_REPO') or 'example/shop')
    m = re.fullmatch(r'repos/([^/]+)/([^/?]+)(?:/(.*))?', path)
    tail = (m.group(3) or '') if m else None
    tail_path = (tail or '').split('?')[0]
    if path == 'user':
        return {'login': BOT, 'id': 4242}
    if tail is None:
        return {}
    if method == 'GET':
        if tail == '':
            return {'name': m.group(2), 'full_name': f'{m.group(1)}/{m.group(2)}', 'default_branch': 'main'}
        if tail_path == 'branches':
            return [{'name': 'main'}]
        if tail_path.startswith('git/matching-refs/heads/'):
            branch = tail_path[len('git/matching-refs/heads/'):]
            return [{'ref': f'refs/heads/{branch}'}] if head_of(branch) != ZERO else []
        if re.fullmatch(r'collaborators/[^/]+/permission', tail_path):
            return {'permission': 'write'}
        hit = re.fullmatch(r'pulls/(\d+)', tail_path)
        if hit:
            return pr_rest(pr_object())
        if re.fullmatch(r'pulls/\d+/(comments|reviews/\d+/comments)', tail_path):
            return []
        hit = re.fullmatch(r'issues/(\d+)/comments', tail_path)
        if hit:
            return comments_of(int(hit.group(1)), st)
        hit = re.fullmatch(r'issues/(\d+)', tail_path)
        if hit:
            n = int(hit.group(1))
            i = issue(n) or {'number': n, 'title': '', 'body': ''}
            return {'number': n, 'title': i.get('title') or '', 'body': i.get('body') or '',
                    'state': str(i.get('state') or 'open').lower(), 'labels': i.get('labels') or [],
                    'assignees': [{'login': a} for a in st['assignees'].get(str(n), [])]}
        return {}
    # writes: recorded, never sent
    st['next_id'] += 1
    new_id = st['next_id']
    body = (payload or {}).get('body') or ''
    result = {}
    hit = re.fullmatch(r'issues/(\d+)/comments', tail_path)
    if hit and method == 'POST':
        n = hit.group(1)
        record('gh-comments.md', f'#{n}', body)
        st['comments'].setdefault(n, []).append({'id': new_id, 'body': body, 'user': {'login': BOT, 'type': 'User'},
                                                 'created_at': '2026-12-01T00:00:00Z', 'updated_at': '2026-12-01T00:00:00Z'})
        result = {'id': new_id, 'body': body}
    elif re.fullmatch(r'(issues|pulls)/comments/\d+', tail_path) and method == 'PATCH':
        cid = int(tail_path.rsplit('/', 1)[1])
        record('gh-comments.md', f'edit {cid}', body)
        for page in st['comments'].values():
            for c in page:
                if c['id'] == cid:
                    c['body'] = body
        result = {'id': cid, 'body': body}
    elif tail_path == 'issues' and method == 'POST':
        n = 500 + sum(1 for line in (ROOT / '.git' / 'gh-created.md').read_text().splitlines() if line.startswith('=== ')) \
            if (ROOT / '.git' / 'gh-created.md').exists() else 500
        record('gh-created.md', f'#{n} {(payload or {}).get("title", "")}', body)
        result = {'number': n, 'html_url': f'https://github.com/{m.group(1)}/{m.group(2)}/issues/{n}'}
    elif tail_path == 'pulls' and method == 'POST':
        n = 900 + new_id % 100
        record('gh-prs.md', f'#{n} {(payload or {}).get("title", "")}', body)
        result = {'number': n, 'html_url': f'https://github.com/{m.group(1)}/{m.group(2)}/pull/{n}'}
    elif re.fullmatch(r'pulls/\d+/comments', tail_path):
        record('gh-comments.md', f'note #{tail_path.split("/")[1]} {(payload or {}).get("path", "")}', body)
        result = {'id': new_id}
    elif re.fullmatch(r'pulls/\d+/reviews', tail_path):
        for c in (payload or {}).get('comments') or []:
            record('gh-comments.md', f'note #{tail_path.split("/")[1]} {c.get("path", "")}', c.get('body') or '')
        result = {'id': new_id}
    elif re.fullmatch(r'issues/\d+/labels(/.*)?', tail_path):
        result = [{'name': l} for l in (payload or {}).get('labels') or []]
    elif re.fullmatch(r'issues/\d+/assignees', tail_path):
        n = tail_path.split('/')[1]
        held = st['assignees'].setdefault(n, [])
        for a in (payload or {}).get('assignees') or []:
            if method == 'POST' and a not in held:
                held.append(a)
            if method == 'DELETE' and a in held:
                held.remove(a)
        result = {'number': int(n), 'assignees': [{'login': a} for a in held]}
    elif re.fullmatch(r'issues/\d+', tail_path):
        result = {'number': int(tail_path.split('/')[1]), 'state': (payload or {}).get('state') or 'open'}
    save(st)
    return result


def emit(value, jq, slurp):
    if slurp:
        value = [value]
    text = json.dumps(value)
    if jq:
        sys.stdout.write(subprocess.run(['jq', '-r', jq], input=text, capture_output=True, text=True).stdout)
    else:
        print(text)


def main():
    if sys.argv[2] == 'pr-json':
        print(json.dumps(pr_object()))
        return
    args = sys.argv[3:]
    method, path, jq, slurp, has_input, fields = 'GET', None, None, False, False, {}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ('-X', '--method'):
            method = args[i + 1].upper(); i += 1
        elif a in ('--jq', '-q'):
            jq = args[i + 1]; i += 1
        elif a == '--slurp':
            slurp = True
        elif a == '--paginate':
            pass
        elif a == '--input':
            has_input = True; i += 1
        elif a in ('-f', '-F', '--raw-field', '--field'):
            key, _, value = args[i + 1].partition('='); fields[key] = value; i += 1
        elif path is None and not a.startswith('-'):
            path = a
        i += 1
    payload = None
    if has_input:
        try:
            payload = json.loads(sys.stdin.read() or 'null')
        except ValueError:
            payload = None
    if path == 'graphql':
        value = graphql(fields.get('query', ''), fields, slurp)
        emit(value, jq, False)
        return
    value = rest(method, path or '', payload, slurp)
    if method == 'DELETE' and re.search(r'/labels/', path or ''):
        return
    emit(value, jq, slurp)


if __name__ == '__main__':
    main()
