"""Pull requests for the fake Azure DevOps (fake_ado_server.py): create/get/patch/list,
iterations and changes, threads, linked work items.

Behaviour is what was observed on a real org (see providers.md): a 4,001-character description
is a 400; completing needs `lastMergeSourceCommit` (400 without, 409 TF401192 when it is not the
current source head); a right commit answers 200 and STILL says active/queued, and the PR turns
`completed` on a LATER GET, when `lastMergeCommit` switches from the service's trial merge commit
to the squash commit. Test hooks: `complete_after_gets`, `never_complete`, `merge_outcome`.
"""
import copy
import hashlib
import re

ROUTE = re.compile(r'/Factory/_apis/git/repositories/[^/]+/pullrequests(?:/(\d+))?(/.*)?', re.I)


def sha(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()


def units(text):
    return len(text.encode('utf-16-le')) // 2


class PrStore:
    def __init__(self, user):
        self.user = user
        self.prs = {}
        self.threads = {}        # pr id -> [thread]
        self.changes = {}        # pr id -> [(changeType, '/path')]
        self.heads = {}          # branch -> current tip sha (what a completion is checked against)
        self.merged = {}         # pr id -> {'message': ..., 'sha': ...}
        self.deleted = set()     # source branches removed by a completion
        self.next_pr = 1
        self.next_thread = 1
        self.clock = 0
        self.complete_after_gets = 1   # GETs that still show active after the completion PATCH
        self.never_complete = False
        self.merge_outcome = None      # 'conflicts' | 'rejectedByPolicy' | 'failure' after the PATCH
        self.before_complete = None    # fn(store, pr), once: e.g. a push landing just before the completion
        self.project_id = 'proj-0001'  # the real PR payload carries repository.project.id; None = it does not
        self.policies = []             # policy evaluations of every PR: [{'status', 'type', 'name', 'blocking'}]
        self.unmet_policy = None       # message: the completion PATCH itself is refused with HTTP 403, as the
                                       # live service does for required reviewers (mergeStatus stays succeeded)

    # ---- test helpers -------------------------------------------------------------------------
    def tick(self):
        self.clock += 1
        return f'2026-11-27T10:{self.clock // 60:02d}:{self.clock % 60:02d}Z'

    def add(self, source, target='main', title='t', description='', head=None, draft=False, labels=(), work_items=()):
        n = self.next_pr
        self.next_pr += 1
        head = head or self.heads.get(source) or sha(f'{source}-{n}')
        self.heads[source] = head
        self.prs[n] = {
            'pullRequestId': n, 'status': 'active', 'isDraft': draft, 'title': title, 'description': description,
            'sourceRefName': f'refs/heads/{source}', 'targetRefName': f'refs/heads/{target}',
            'mergeStatus': 'queued', 'closedDate': None,
            'lastMergeSourceCommit': {'commitId': head}, 'lastMergeTargetCommit': {'commitId': sha('target')},
            'lastMergeCommit': None, 'labels': [{'id': sha(l), 'name': l, 'active': True} for l in labels],
            'workItemRefs': [{'id': str(w)} for w in work_items], 'createdBy': self.user,
            '_completing': None, '_gets_left': 0}
        self.threads[n] = []
        self.changes[n] = []
        return n

    def push(self, n, new_head):
        pr = self.prs[n]
        self.heads[pr['sourceRefName'][len('refs/heads/'):]] = new_head
        pr['lastMergeSourceCommit'] = {'commitId': new_head}

    def add_thread(self, n, comments, status='active', context=None):
        """context: a file anchor ({'filePath': ...}), as an inline review thread carries."""
        t = {'id': self.next_thread, 'status': status, 'comments': [], 'threadContext': context}
        self.next_thread += 1
        for i, c in enumerate(comments, 1):
            t['comments'].append(self.stamp(c, i))
        self.threads[n].append(t)
        return t

    def stamp(self, c, i):
        c = dict(c)
        c.setdefault('id', i)
        c.setdefault('author', self.user)
        c.setdefault('commentType', 'text')
        c.setdefault('publishedDate', self.tick())
        c.setdefault('lastUpdatedDate', c['publishedDate'])
        c.setdefault('lastContentUpdatedDate', c['publishedDate'])
        return c

    def reply(self, n, thread_id, comment):
        """A reply in an existing thread (the web UI's Reply)."""
        thread = next(t for t in self.threads[n] if t['id'] == thread_id)
        c = self.stamp(comment, max([x['id'] for x in thread['comments']] or [0]) + 1)
        thread['comments'].append(c)
        return c

    # ---- model ------------------------------------------------------------------------------------
    def render(self, pr, labels=False):
        """The single-PR GET (and the list) carry NO `labels` key on the real service; only the create
        response may, and `/labels` is the source of truth."""
        out = {k: copy.deepcopy(v) for k, v in pr.items() if not k.startswith('_') and (labels or k != 'labels')}
        if self.project_id:
            out['repository'] = {'id': 'repo-id', 'name': 'Factory', 'project': {'id': self.project_id}}
        return out

    def settle(self, pr):
        """What a GET sees: the trial merge appears, and a requested completion lands later."""
        if pr['status'] == 'active' and pr['mergeStatus'] == 'queued' and not pr['_completing']:
            pr['mergeStatus'] = 'succeeded'
            pr['lastMergeCommit'] = {'commitId': sha('trial-' + pr['lastMergeSourceCommit']['commitId'])}
        if pr['_completing'] and pr['status'] == 'active':
            if self.merge_outcome:
                pr['mergeStatus'] = self.merge_outcome
                pr['mergeFailureMessage'] = f'forced {self.merge_outcome}'
            elif not self.never_complete:
                if pr['_gets_left'] > 0:
                    pr['_gets_left'] -= 1
                else:
                    options = pr['_completing']
                    squash = sha('squash-' + pr['lastMergeSourceCommit']['commitId'] + options.get('mergeCommitMessage', ''))
                    pr.update(status='completed', mergeStatus='succeeded', closedDate=self.tick(),
                              lastMergeCommit={'commitId': squash})
                    self.merged[pr['pullRequestId']] = {'message': options.get('mergeCommitMessage'), 'sha': squash,
                                                        'options': options}
                    self.deleted.add(pr['sourceRefName'][len('refs/heads/'):])

    # ---- routes ------------------------------------------------------------------------------------
    def route(self, req):
        found = ROUTE.fullmatch(req['path'])
        if not found:
            return None
        n, tail = (int(found.group(1)) if found.group(1) else None), found.group(2) or ''
        m = req['method']
        if n is None:
            return self.collection(req) if tail == '' else (404, {}, {'message': 'no route'})
        pr = self.prs.get(n)
        if pr is None:
            return 404, {}, {'message': f'TF401180: The requested pull request was not found.'}
        if tail == '':
            if m == 'GET':
                self.settle(pr)
                return 200, {}, self.render(pr)
            if m == 'PATCH':
                return self.patch(pr, req)
        if tail == '/iterations' and m == 'GET':
            return 200, {}, {'count': 2, 'value': [{'id': 1}, {'id': 2}]}
        mt = re.fullmatch(r'/iterations/(\d+)/changes', tail)
        if mt and m == 'GET':
            entries = [{'changeType': kind, 'item': {'path': path}} for kind, path in self.changes[n]]
            entries.insert(0, {'changeType': 'add', 'item': {'path': '/src', 'isFolder': True}})
            return 200, {}, {'changeEntries': entries}
        if tail == '/labels' and m == 'GET':
            return 200, {}, {'count': len(pr['labels']), 'value': copy.deepcopy(pr['labels'])}
        if tail == '/workitems' and m == 'GET':
            return 200, {}, {'count': len(pr['workItemRefs']), 'value': [{'id': w['id'], 'url': 'http://fake/wi'} for w in pr['workItemRefs']]}
        if tail == '/threads':
            if m == 'GET':
                return 200, {}, {'count': len(self.threads[n]), 'value': copy.deepcopy(self.threads[n])}
            if m == 'POST':
                comment = (req['body'].get('comments') or [{}])[0]
                if not comment.get('content'):
                    return 400, {}, {'message': 'comment content is required'}
                t = self.add_thread(n, [{'content': comment['content']}], 'active', req['body'].get('threadContext'))
                return 200, {}, copy.deepcopy(t)
        mt = re.fullmatch(r'/threads/(\d+)', tail)
        if mt and m == 'PATCH':
            thread = next((t for t in self.threads[n] if t['id'] == int(mt.group(1))), None)
            if thread is None:
                return 404, {}, {'message': 'thread not found'}
            if 'status' in req['body']:
                thread['status'] = req['body']['status']  # observed: a string such as "fixed" is accepted
            return 200, {}, copy.deepcopy(thread)
        mt = re.fullmatch(r'/threads/(\d+)/comments/(\d+)', tail)
        if mt and m == 'PATCH':
            thread = next((t for t in self.threads[n] if t['id'] == int(mt.group(1))), None)
            comment = next((c for c in (thread or {}).get('comments', []) if c['id'] == int(mt.group(2))), None)
            if comment is None:
                return 404, {}, {'message': 'comment not found'}
            comment['content'] = req['body']['content']
            comment['lastUpdatedDate'] = comment['lastContentUpdatedDate'] = self.tick()
            return 200, {}, copy.deepcopy(comment)
        return 404, {}, {'message': f'no route {m} {req["path"]}'}

    def collection(self, req):
        if req['method'] == 'GET':
            wanted = req['query'].get('searchCriteria.status', ['active'])[0]
            top = int(req['query'].get('$top', ['100'])[0])
            rows = [self.render(p) for p in sorted(self.prs.values(), key=lambda p: -p['pullRequestId'])
                    if wanted == 'all' or p['status'] == wanted]
            return 200, {}, {'count': len(rows[:top]), 'value': rows[:top]}
        body = req['body'] or {}
        if not all(body.get(k) for k in ('sourceRefName', 'targetRefName', 'title')):
            return 400, {}, {'message': 'sourceRefName, targetRefName and title are required'}
        if units(body.get('description') or '') > 4000:
            return 400, {}, {'message': 'A description for a pull request must not be longer than 4000 characters.'}
        n = self.add(body['sourceRefName'][len('refs/heads/'):], body['targetRefName'][len('refs/heads/'):], body['title'],
                     body.get('description') or '', draft=bool(body.get('isDraft')),
                     labels=[l['name'] for l in body.get('labels') or []],
                     work_items=[w['id'] for w in body.get('workItemRefs') or []])
        return 201, {}, self.render(self.prs[n], labels=True)

    def patch(self, pr, req):
        if req['content_type'] != 'application/json':
            return 415, {}, {'message': 'unsupported media type'}
        body = req['body'] or {}
        if 'description' in body and units(body['description']) > 4000:
            return 400, {}, {'message': 'A description for a pull request must not be longer than 4000 characters.'}
        if body.get('status') == 'completed':
            if not (body.get('lastMergeSourceCommit') or {}).get('commitId'):
                return 400, {}, {'message': 'You must specify a valid LastMergeSourceCommit to perform this operation.'}
            if pr['isDraft']:
                return 400, {}, {'message': 'The pull request is a draft and cannot be completed.'}
            if self.before_complete:
                hook, self.before_complete = self.before_complete, None
                hook(self, pr)
            current = self.heads[pr['sourceRefName'][len('refs/heads/'):]]
            if body['lastMergeSourceCommit']['commitId'] != current:
                return 409, {}, {'message': 'TF401192: The source branch has been modified since the last merge attempt.'}
            if self.unmet_policy:
                return 403, {}, {'message': self.unmet_policy}
            pr['_completing'] = body.get('completionOptions') or {}
            pr['_gets_left'] = self.complete_after_gets
            pr['mergeStatus'] = 'queued'
            return 200, {}, self.render(pr)  # still active / queued
        if body.get('status') == 'abandoned':
            pr['status'] = 'abandoned'
        if 'description' in body:
            pr['description'] = body['description']
        if 'isDraft' in body:
            pr['isDraft'] = body['isDraft']
        if 'title' in body:
            pr['title'] = body['title']
        return 200, {}, self.render(pr)
