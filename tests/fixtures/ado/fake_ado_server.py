"""A local fake Azure DevOps Services (loopback, threaded) for the ADO backend tests.

Holds one team board, work items and comments in memory, seeded from the sanitized
captures beside it, and records every request. Only the routes the forge calls exist (pull requests: fake_ado_pr.py).
Behaviour mirrors what was observed against a real org: the column field is a per-board
WEF_* name, the server derives System.State from the column's mapping, writing
System.BoardColumn is refused, an unknown column is TF401320, a failed /rev test is
refused with a stale-revision error.
"""
import copy
import json
import re
import threading
import urllib.parse
import uuid
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from runtime_http_server import LoopbackHTTPServer  # noqa: E402
from fake_ado_pr import PrStore  # noqa: E402


class ThreadedServer(ThreadingMixIn, LoopbackHTTPServer):
    daemon_threads = True

WEF = 'WEF_0123456789ABCDEF0123456789ABCDEF_Kanban.Column'
USER = {'displayName': 'Example User', 'uniqueName': 'user@example.com', 'id': '00000000-0000-0000-0000-000000000001'}
STATES = {
    'User Story': [('New', 'Proposed'), ('Active', 'InProgress'), ('Resolved', 'Resolved'), ('Closed', 'Completed'),
                   ('Removed', 'Removed')],
    'Bug': [('New', 'Proposed'), ('Active', 'InProgress'), ('Resolved', 'Resolved'), ('Closed', 'Completed')],
}
COLUMNS_SEVEN = ['Backlog', 'Ready', 'Building', 'QA', 'Review', 'Blocked', 'Done']


LONG_TEXT = ('System.Description', 'Microsoft.VSTS.TCM.ReproSteps')


def stored(text):
    """What the live service hands back for a Markdown long-text field or work item comment:
    `&` and `"` as entities, characters outside the BMP (most emoji) as numeric references.
    `<`/`>` and BMP characters such as the em dash come back as written. Titles are untouched.
    Measured: `"x" & <b>t</b> 🙋 —` reads back `&quot;x&quot; &amp; <b>t</b> &#128587; —`.
    HTML comments (`<!-- ... -->`) are removed entirely (observed on a real org)."""
    if not isinstance(text, str):
        return text
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)  # observed: HTML comments are stripped on save
    text = text.replace('&', '&amp;').replace('"', '&quot;')
    return ''.join(f'&#{ord(ch)};' if ord(ch) > 0xFFFF else ch for ch in text)


def stored_comment(c):
    return dict(c, text=stored(c['text'])) if isinstance(c, dict) and 'text' in c else c


def load(name):
    return json.loads((HERE / name).read_text())


class Fake:
    def __init__(self, seven=False, bugs_on_board=None):
        self.lock = threading.Lock()
        self.requests = []
        self.inject = []          # {'match': fn(req)->bool, 'times': n, 'status':, 'headers':, 'body':}
        self.simultaneous = None  # fn(fake, item): a rival save lands after our rev test passed
        self.pre_patch = None     # fn(fake, item) run under the lock before the /rev test
        self.stale_status = 412
        self.stale_message = 'VS403351: Test operation failed: revision mismatch'
        self.token_ok = None      # exact Authorization value required, or None for "any"
        self.extra_headers = {}
        self.page_size = 200
        self.bugs = 'asRequirements' if (bugs_on_board if bugs_on_board is not None else seven) else 'asTasks'
        self.columns = copy.deepcopy(load('columns-seven.json' if seven else 'columns-default.json')['value'])
        for c in self.columns:
            if self.bugs == 'asTasks':
                c['stateMappings'].pop('Bug', None)
        self.pulls = PrStore(USER)  # pull requests: fake_ado_pr.py
        self.repo_heads = ['main']  # branches of the repository; [] = a brand new, empty Azure Repos repository
        self.items = {}
        self.comments = {}
        self.next_id = 2
        self.next_comment = 1
        self.items[1] = self.make(1, 'User Story', '[sandbox probe] column move test',
                                  load('work-item.json')['fields']['System.Description'], 'feature; probe')

    # ---- model ----------------------------------------------------------------------
    def col_for_state(self, typ, state):
        return next((c for c in self.columns if c['stateMappings'].get(typ) == state), None)

    def make(self, n, typ, title, body='', tags='', column=None, state=None):
        state = state or 'New'
        col = next((c for c in self.columns if c['name'] == column), None) if column else self.col_for_state(typ, state)
        if column and col:
            state = col['stateMappings'].get(typ, state)
        return {'id': n, 'rev': 1, 'type': typ, 'state': state, 'col_id': col['id'] if col else None,
                'fields': {'System.Title': title, 'System.Tags': tags,
                           'System.Description' if typ != 'Bug' else 'Microsoft.VSTS.TCM.ReproSteps': body}}

    def add(self, typ, title, body='', tags='', column=None):
        n = self.next_id
        self.next_id += 1
        self.items[n] = self.make(n, typ, title, body, tags, column)
        return n

    def board_types(self):
        return ['User Story', 'Bug'] if self.bugs == 'asRequirements' else ['User Story']

    def col_name(self, item):
        col = next((c for c in self.columns if c['id'] == item['col_id']), None)
        return col['name'] if col else None

    def render(self, item, fields=None):
        f = dict(item['fields'])
        f.update({'System.State': item['state'], 'System.WorkItemType': item['type'],
                  'System.TeamProject': 'Factory', 'System.BoardColumn': 'Other board column'})
        name = self.col_name(item)
        if name is not None and item['type'] in self.board_types():
            f[WEF] = name
        f = {k: (stored(v) if k in LONG_TEXT else v) for k, v in f.items() if v is not None}
        if fields:
            f = {k: v for k, v in f.items() if k in fields}
        return {'id': item['id'], 'rev': item['rev'], 'fields': f,
                'url': f'http://fake/_apis/wit/workItems/{item["id"]}'}

    def board_json(self):
        types = self.board_types()
        allowed = {'Incoming': {t: ['New'] for t in types},
                   'InProgress': {t: ['Active', 'New', 'Resolved'] for t in types},
                   'Outgoing': {t: ['Closed'] for t in types}}
        return {'id': 'board-id', 'name': 'Stories', 'revision': 1, 'isValid': True, 'canEdit': True,
                'columns': self.columns, 'fields': {'columnField': {'referenceName': WEF},
                                                    'doneField': {'referenceName': WEF + '.Done'},
                                                    'rowField': {'referenceName': 'System.Kanban.Lane'}},
                'allowedMappings': allowed}

    def add_comment(self, n, text, by=None, fmt='markdown'):
        """A work item comment; one clock with the PR threads, so times order across both."""
        at = self.pulls.tick()
        c = {'id': self.next_comment, 'version': 1, 'text': text, 'format': fmt,
             'createdDate': at, 'modifiedDate': at, 'createdBy': by or USER}
        self.next_comment += 1
        self.comments.setdefault(n, []).append(c)
        return c

    # ---- helpers for tests ---------------------------------------------------------
    def reqs(self, method=None, pattern=None):
        return [r for r in self.requests if (method is None or r['method'] == method)
                and (pattern is None or re.search(pattern, r['path']))]

    def tags(self, n):
        return [t.strip() for t in (self.items[n]['fields'].get('System.Tags') or '').split(';') if t.strip()]

    def column_of(self, n):
        return self.col_name(self.items[n])


class Handler(BaseHTTPRequestHandler):
    server_version = 'FakeAdo'

    def log_message(self, *args):
        pass

    def handle_any(self):
        fake = self.server.fake
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        req = {'method': self.command, 'path': path, 'query': urllib.parse.parse_qs(parsed.query),
               'content_type': self.headers.get('Content-Type'), 'body': body,
               'auth': self.headers.get('Authorization') is not None,
               'authorization': self.headers.get('Authorization')}
        with fake.lock:
            fake.requests.append(req)
            status, headers, payload = self.route(fake, req)
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for k, v in {**fake.extra_headers, **headers}.items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = do_PUT = handle_any

    # ---- routing ------------------------------------------------------------------------
    def route(self, fake, req):
        for rule in list(fake.inject):
            if rule['match'](req):
                rule['times'] -= 1
                if rule['times'] <= 0:
                    fake.inject.remove(rule)
                return rule['status'], rule.get('headers', {}), rule.get('body', {})
        auth = req['authorization']
        if not auth or (fake.token_ok and auth != fake.token_ok):
            return 401, {}, {'message': 'TF400813: unauthorized'}
        p, m, q = req['path'], req['method'], req['query']
        pulled = fake.pulls.route(req)
        if pulled:
            return pulled
        if p == '/_apis/projects/Factory' and m == 'GET':
            return 200, {}, {'id': 'proj-0001', 'name': 'Factory'}
        if p == '/Factory/_apis/policy/evaluations' and m == 'GET':
            # observed: `{count:0,value:[]}` on a repository with no policies; the artifact id carries the
            # project id and the pull request id
            found = re.fullmatch(r'vstfs:///CodeReview/CodeReviewId/proj-0001/(\d+)', q.get('artifactId', [''])[0])
            if not found or int(found.group(1)) not in fake.pulls.prs:
                return 400, {}, {'message': 'bad artifactId'}
            rows = [{'evaluationId': 'e%d' % i, 'status': e['status'],
                     'configuration': {'isBlocking': e['blocking'], 'type': {'displayName': e['type']},
                                       'settings': {'displayName': e['name']} if e.get('name') else {}}}
                    for i, e in enumerate(fake.pulls.policies)]
            return 200, {}, {'count': len(rows), 'value': rows}
        mr = re.fullmatch(r'/Factory/_apis/git/repositories/Factory(/refs)?', p)
        if mr and m == 'GET':
            if not mr.group(1):
                out = {'id': 'repo-id', 'name': 'Factory', 'size': 0 if not fake.repo_heads else 4096}
                if fake.repo_heads:
                    out['defaultBranch'] = 'refs/heads/' + fake.repo_heads[0]
                return 200, {}, out
            wanted = 'refs/' + q.get('filter', [''])[0]
            rows = [{'name': 'refs/heads/' + b, 'objectId': 'a' * 40} for b in fake.repo_heads
                    if ('refs/heads/' + b).startswith(wanted)]
            return 200, {}, {'count': len(rows), 'value': rows[:int(q.get('$top', ['1000'])[0])]}
        if p == '/_apis/connectionData':
            return 200, {}, {'authenticatedUser': {
                'id': USER['id'], 'providerDisplayName': 'Example User',
                'properties': {'Account': {'$type': 'System.String', '$value': 'user@example.com'}}}}
        team = '/Factory/Factory Team/_apis/work'
        if p == f'{team}/boards/Stories' and m == 'GET':
            return 200, {}, fake.board_json()
        if p == f'{team}/boards/Stories/columns' and m == 'PUT':
            return self.put_columns(fake, req['body'])
        if p == f'{team}/teamsettings':
            if m == 'PATCH':
                fake.bugs = req['body'].get('bugsBehavior', fake.bugs)
                if fake.bugs == 'asRequirements':
                    for c in fake.columns:
                        c['stateMappings'].setdefault('Bug', c['stateMappings'].get('User Story'))
            return 200, {}, {'bugsBehavior': fake.bugs, 'workingDays': []}
        mt = re.fullmatch(r'/Factory/_apis/wit/workitemtypes/(.+)/states', p)
        if mt:
            if mt.group(1) not in STATES:
                return 404, {}, {'message': 'TF26027: type not found'}
            return 200, {}, {'count': len(STATES[mt.group(1)]),
                             'value': [{'name': n, 'category': c} for n, c in STATES[mt.group(1)]]}
        if p == '/Factory/_apis/wit/wiql' and m == 'POST':
            return self.wiql(fake, req['body']['query'])
        if p == '/Factory/_apis/wit/workitemsbatch' and m == 'POST':
            b = req['body']
            return 200, {}, {'count': len(b['ids']), 'value': [
                fake.render(fake.items[i], b.get('fields')) if i in fake.items else None for i in b['ids']]}
        mt = re.fullmatch(r'/Factory/_apis/wit/workitems/\$(.+)', p)
        if mt and m == 'POST':
            return self.create(fake, mt.group(1), req['body'])
        mt = re.fullmatch(r'/Factory/_apis/wit/workitems/(\d+)', p)
        if mt:
            item = fake.items.get(int(mt.group(1)))
            if item is None:
                return 404, {}, {'message': f'TF401232: Work item {mt.group(1)} does not exist'}
            if m == 'GET':
                fields = q.get('fields', [''])[0].split(',') if 'fields' in q else None
                return 200, {}, fake.render(item, fields)
            if m == 'PATCH':
                return self.patch(fake, item, req)
        mt = re.fullmatch(r'/Factory/_apis/wit/workItems/(\d+)/comments(?:/(\d+))?', p)
        if mt:
            return self.comments(fake, req, int(mt.group(1)), mt.group(2))
        return 404, {}, {'message': f'no route {m} {p}'}

    def put_columns(self, fake, body):
        if not isinstance(body, list) or not body or body[0]['columnType'] != 'incoming' or body[-1]['columnType'] != 'outgoing':
            return 400, {}, {'message': 'first column must be incoming and last outgoing'}
        types = fake.board_types()
        keep = {c['id'] for c in body if c.get('id')}
        for item in fake.items.values():
            if item['col_id'] and item['col_id'] not in keep:
                return 400, {}, {'message': 'TF400000: cannot remove a column that holds cards'}
        out = []
        for c in body:
            if not all(t in c.get('stateMappings', {}) for t in types):
                return 400, {}, {'message': 'every column needs a state mapping for every work item type'}
            out.append({**{k: v for k, v in c.items()}, 'id': c.get('id') or str(uuid.uuid4()),
                        'itemLimit': c.get('itemLimit', 0)})
        fake.columns = out
        return 200, {}, {'count': len(out), 'value': out}

    def wiql(self, fake, query):
        found = re.search(r'IN \((.*?)\)', query)
        types = [t.replace("''", "'") for t in re.findall(r"'((?:[^']|'')*)'", found.group(1))] if found else list(STATES)
        ids = [i for i, it in fake.items.items() if it['type'] in types]
        m = re.search(r"\[(WEF_[^\]]+)\] = '(.*?)'", query)
        if m:
            ids = [i for i in ids if fake.col_name(fake.items[i]) == m.group(2)]
        ids.sort(reverse='DESC' in query)
        return 200, {}, {'queryType': 'flat', 'workItems': [{'id': i, 'url': f'http://fake/{i}'} for i in ids]}

    def create(self, fake, typ, ops):
        if typ not in STATES:
            return 400, {}, {'message': 'TF401227: unknown work item type'}
        n = fake.next_id
        fake.next_id += 1
        item = fake.make(n, typ, '')
        item['fields'] = {}
        formats = {}
        for op in ops:
            kind, _, name = op['path'].lstrip('/').partition('/')
            if kind == 'fields':
                item['fields'][name] = op['value']
            elif kind == 'multilineFieldsFormat':
                formats[name] = op['value'].lower()
        fake.items[n] = item
        out = fake.render(item)
        out['multilineFieldsFormat'] = formats
        return 201, {}, out

    @staticmethod
    def tag_list(value):
        return [t.strip() for t in (value or '').split(';') if t.strip()]

    def tags_after(self, current, op):
        """Observed service semantics: `add` MERGES the given tags into the set (never removes),
        `replace` sets the exact list ('' -> null), `remove` clears."""
        have = self.tag_list(current)
        if op['op'] == 'add':
            have += [t for t in self.tag_list(op['value']) if t.lower() not in [h.lower() for h in have]]
        elif op['op'] == 'replace':
            have = self.tag_list(op['value'])
        elif op['op'] == 'remove':
            have = []
        return '; '.join(have) or None

    def patch(self, fake, item, req):
        if req['content_type'] != 'application/json-patch+json':
            return 400, {}, {'message': 'unsupported media type'}
        if fake.pre_patch:
            hook, fake.pre_patch = fake.pre_patch, None
            hook(fake, item)
        ops = req['body']
        if ops and ops[0].get('path') == '/rev' and ops[0]['op'] == 'test' and ops[0]['value'] != item['rev']:
            return fake.stale_status, {}, {'message': fake.stale_message}
        staged = copy.deepcopy(item)
        for op in ops:
            if op['op'] == 'test':
                continue
            kind, _, name = op['path'].lstrip('/').partition('/')
            if kind == 'multilineFieldsFormat':
                continue
            if name == WEF:
                col = next((c for c in fake.columns if c['name'] == op['value']), None)
                if col is None:
                    return 400, {}, {'message': 'TF401320: Rule Error for field State. Error code: InvalidNotEmpty'}
                staged['col_id'] = col['id']
                staged['state'] = col['stateMappings'].get(item['type'], staged['state'])
            elif name == 'System.BoardColumn':
                return 400, {}, {'message': 'TF401326: field System.BoardColumn is ReadOnly'}
            elif name == 'System.Tags':
                staged['fields']['System.Tags'] = self.tags_after(staged['fields'].get('System.Tags'), op)
            else:
                staged['fields'][name] = op['value']
        if fake.simultaneous:
            # Another writer's save lands between our rev test and our save (the test is not
            # atomic with the save): identical result = a no-op 200, a different one = 409.
            hook, fake.simultaneous = fake.simultaneous, None
            hook(fake, item)
            current = fake.items[item['id']]
            if {k: v for k, v in staged.items() if k != 'rev'} != {k: v for k, v in current.items() if k != 'rev'}:
                return 409, {}, {'message': 'TF26071: This work item has been changed by someone else since you opened it.'}
            return 200, {}, fake.render(current)
        if {k: v for k, v in staged.items() if k != 'rev'} == {k: v for k, v in item.items() if k != 'rev'}:
            return 200, {}, fake.render(item)  # a write that changes nothing creates no revision
        staged['rev'] += 1
        fake.items[item['id']] = staged
        return 200, {}, fake.render(staged)

    def comments(self, fake, req, n, cid):
        if n not in fake.items:
            return 404, {}, {'message': 'work item not found'}
        store = fake.comments.setdefault(n, [])
        m = req['method']
        if m == 'POST':
            return 200, {}, stored_comment(fake.add_comment(n, req['body']['text']))
        if m == 'PATCH':
            c = next((c for c in store if c['id'] == int(cid)), None)
            if c is None:
                return 404, {}, {'message': 'comment not found'}
            c['text'], c['version'] = req['body']['text'], c['version'] + 1
            c['modifiedDate'] = fake.pulls.tick()
            return 200, {}, stored_comment(c)
        start = int(req['query'].get('continuationToken', ['0'])[0])
        size = min(int(req['query'].get('$top', ['200'])[0]), fake.page_size)
        page = store[start:start + size]
        out = {'totalCount': len(store), 'count': len(page), 'comments': [stored_comment(c) for c in page]}
        if start + size < len(store):
            out['continuationToken'] = str(start + size)
        return 200, {}, out


def start(fake):
    server = ThreadedServer(('127.0.0.1', 0), Handler)
    server.fake = fake
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
