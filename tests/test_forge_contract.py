#!/usr/bin/env python3
"""One scenario list, two providers: the forge verbs a lane agent uses answer in the SAME shape.

Each scenario runs the same `super-board-forge.py` arguments against the GitHub backend (a scripted `gh`)
and the Azure DevOps backend (the fake service), then asserts the same exit status and the same stdout shape
key-for-key (leaf values and list lengths do not matter; the keys at every depth do). A key a provider adds on
top of the contract (the union documented in providers.md) is named in `extra` and dropped before comparing.
This is what lets the lane prose be provider-neutral: if a verb's shape drifts on one side, this fails.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado_pr import PrCase  # noqa: E402
from test_forge_github import GhCase, BoardTests, gh_issue  # noqa: E402

HEAD = 'd' * 40


def shape(value, extra=()):
    """The keys at every depth. A list is the merged shape of its elements; scalars (incl. null) are 'v'."""
    if isinstance(value, dict):
        return {k: shape(v, extra) for k, v in sorted(value.items()) if k not in extra}
    if isinstance(value, list):
        merged = {}
        for item in value:
            s = shape(item, extra)
            if isinstance(s, dict):
                merged.update(s)
            else:
                merged = s
        return [merged] if value else []
    return 'v'


def compatible(a, b):
    """Same keys at every depth; an empty list matches any list (no sample to compare)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(compatible(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return not a or not b or compatible(a[0], b[0])
    return a == b == 'v'


def gh_row(**kw):
    row = {'id': 4242, 'body': 'hello', 'user': {'login': 'user@example.com', 'type': 'User'},
           'created_at': '2026-11-01T00:00:00Z', 'updated_at': '2026-11-01T00:00:00Z'}
    row.update(kw)
    return row


class Contract(PrCase):
    seven = True

    def setUp(self):
        super().setUp()
        self.gh = GhCase()
        self.gh.setUp()
        self.addCleanup(self.gh.tearDown)
        self.config_value['base_branch'] = 'main'

    def pr(self, **kw):
        return self.pulls.add('feat', 'main', title='the title', head=HEAD, **kw)

    def both(self, args, gh_rules, kind='json', extra=(), rc=0, ado_rc=None, config=None):
        """Run `args` on both providers; returns (github stdout, azure devops stdout), compared per `kind`:
        json (same shape), exact (same text), line (one non-empty line), url-number (a URL line, then digits),
        empty (nothing), prefix:<text> (same start)."""
        self.gh.rules(*gh_rules)
        g = self.gh.forge(*args, **(config or {}))
        a = self.forge(*args)
        self.assertEqual(g.returncode, rc, ('github', args, g.stderr))
        self.assertEqual(a.returncode, rc if ado_rc is None else ado_rc, ('azure-devops', args, a.stderr))
        if rc:
            return g.stdout, a.stdout
        if kind == 'json':
            gs, as_ = shape(json.loads(g.stdout), extra), shape(json.loads(a.stdout), extra)
            self.assertTrue(compatible(gs, as_), f'{args}: github {gs} vs azure-devops {as_}')
        elif kind == 'exact':
            self.assertEqual(g.stdout, a.stdout, args)
        elif kind == 'line':
            for out in (g.stdout, a.stdout):
                self.assertRegex(out, r'^\S+\n$', args)
        elif kind == 'url-number':
            for out in (g.stdout, a.stdout):
                self.assertRegex(out, r'^\S+\n[0-9]+\n$', args)
        elif kind == 'empty':
            self.assertEqual((g.stdout, a.stdout), ('', ''), args)
        elif kind.startswith('prefix:'):
            for out in (g.stdout, a.stdout):
                self.assertTrue(out.startswith(kind[7:]), (args, out))
        return g.stdout, a.stdout

    # ---- tickets ------------------------------------------------------------------------------------
    def test_ticket_reads(self):
        self.fake.add('User Story', 'done', '', 'qa')
        self.ok('ticket', 'close', '2', '--reason', 'completed')
        issue = gh_issue(1, labels=[{'name': 'feature'}])
        self.both(['ticket', 'view', '1'], [{'match': ['issue view'], 'out': issue}])
        self.both(['ticket', 'list', '--state', 'open', '--limit', '5'], [{'match': ['issue list'], 'out': [issue]}])
        self.both(['ticket', 'list', '--state', 'closed', '--limit', '5'],
                  [{'match': ['issue list'], 'out': [gh_issue(2, state='CLOSED', stateReason='COMPLETED')]}])
        self.both(['ticket', 'list', '--label', 'feature'], [{'match': ['issue list'], 'out': [issue]}])
        self.fake.add_comment(1, 'hello')
        self.both(['ticket', 'comments', '1'], [{'match': ['--paginate', 'issues/1/comments'], 'out': [[gh_row()]]}], extra=('version',))

    def test_ticket_writes(self):
        p = self.root / 'body.md'
        p.write_text('body\n')
        self.fake.add_comment(1, 'to edit')
        self.both(['ticket', 'create', '--title', 't', '--body-file', str(p), '--label', 'feature'],
                  [{'match': ['POST', '/issues'], 'out': 'https://github.com/o/n/issues/9\n9\n'}], kind='url-number')
        self.both(['ticket', 'comment', '1', '--body-file', str(p)], [{'match': ['POST', 'issues/1/comments'], 'out': '4242\n'}], kind='line')
        self.both(['ticket', 'comment-edit', '1', '1', '--body-file', str(p)], [{'match': ['PATCH', 'issues/comments/1'], 'out': '1'}], kind='exact')
        self.both(['ticket', 'edit', '1', '--title', 'n', '--add-label', 'x', '--remove-label', 'probe'],
                  [{'match': ['api'], 'out': ''}], kind='empty')
        self.both(['ticket', 'close', '1', '--reason', 'not-planned'], [{'match': ['PATCH', 'issues/1'], 'out': 'closed'}], kind='exact')

    def test_claim_release_and_a_held_card(self):
        cfg = {'notifications': {'bot_identity': 'me'}}
        rules = [{'match': ['--jq', '[.assignees[].login]'], 'outs': [[], ['me'], ['me'], []]},
                 {'match': ['POST', 'assignees'], 'out': '1'}, {'match': ['DELETE', 'assignees'], 'out': '1'}]
        self.both(['ticket', 'claim', '1'], rules, kind='exact', config=cfg)
        self.both(['ticket', 'release', '1'], rules, kind='prefix:released #1 (', config=cfg)
        # held by someone else: both exit 4, never steal
        self.fake.items[1]['fields']['System.Tags'] = 'feature; sb:claimed-zzzzzz'
        self.both(['ticket', 'claim', '1'], [{'match': ['--jq', '[.assignees[].login]'], 'out': ['someone']}], rc=4, config=cfg)

    def test_identity_quota_repository(self):
        self.both(['whoami'], [{'match': ['api', 'user'], 'out': {'login': 'user@example.com', 'id': '1'}}])
        rest = {'resources': {'graphql': {'limit': 5000, 'remaining': 4000, 'used': 1, 'reset': 100},
                              'core': {'limit': 5000, 'remaining': 4000, 'used': 1, 'reset': 100}}}
        # the union: GitHub adds both buckets; Azure DevOps adds cost/total_cost when the service sends only a cost
        self.both(['quota'], [{'match': ['api', 'rate_limit'], 'out': rest}, {'match': ['graphql'], 'rc': 1}],
                  extra=('graphql', 'core', 'cost', 'total_cost'))
        self.both(['repository'], [{'match': ['--jq', 'defaultBranch'], 'out': {'repository': 'Factory', 'defaultBranch': 'main'}},
                                   {'match': ['branches?per_page=1'], 'out': [{'name': 'main'}]},
                                   {'match': ['matching-refs'], 'out': [{'ref': 'refs/heads/main'}]}], config={'base_branch': 'main'})

    # ---- pull requests -------------------------------------------------------------------------------
    def test_pr_reads(self):
        n = self.pr(labels=['bug'])
        view = {'number': n, 'title': 'the title', 'url': 'u', 'state': 'OPEN', 'isDraft': False, 'headRefName': 'feat',
                'headRefOid': HEAD, 'baseRefName': 'main', 'mergeCommit': None, 'labels': [{'name': 'bug'}]}
        self.both(['pr', 'view', str(n)], [{'match': ['--jq', 'mergeCommit'], 'out': view}])
        self.both(['pr', 'head', str(n)], [{'match': ['pr view'], 'out': f'feat {HEAD}\n'}], kind='exact', extra=())
        self.both(['pr', 'state', str(n)], [{'match': ['pr view'], 'out': {'state': 'OPEN', 'headRefOid': HEAD, 'mergeCommit': None}}])
        self.both(['pr', 'body', str(n)], [{'match': ['--jq', 'headRefOid'], 'out': {'headRefOid': HEAD, 'body': 'b'}}])
        refs = [{'number': n, 'headRefName': 'feat', 'baseRefName': 'main', 'headRefOid': HEAD}]
        self.both(['pr', 'refs', '--state', 'open', '--limit', '5'], [{'match': ['pr list', 'headRefOid'], 'out': refs}])
        self.pulls.prs[n].update(status='completed', closedDate='2026-11-26T10:00:00Z', lastMergeCommit={'commitId': 'e' * 40})
        self.both(['pr', 'refs', '--state', 'merged', '--limit', '5'], [{'match': ['pr list', 'headRefOid'], 'out': refs}])
        merged = [{'number': n, 'title': 't', 'url': 'u', 'mergedAt': '2026-11-26T10:00:00Z', 'author': {'login': 'a', 'is_bot': False}}]
        self.both(['pr', 'merged', '--since', '2026-11-01T00:00:00Z', '--limit', '5'], [{'match': ['pr list', 'mergedAt'], 'out': merged}])

    def test_pr_links_and_checks(self):
        n = self.pr(work_items=[1])
        page = {'data': {'repository': {'defaultBranchRef': {'name': 'main'}, 'pullRequest': {
            'headRefOid': HEAD, 'state': 'OPEN', 'baseRefName': 'main', 'body': '',
            'closingIssuesReferences': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                        'nodes': [{'number': 1, 'repository': {'nameWithOwner': 'example/factory'}}]}}}}}
        self.both(['pr', 'links', str(n)], [{'match': ['graphql', 'closingIssuesReferences'], 'out': [page]}],
                  config={'repo': {'remote': 'example/factory'}})
        self.pulls.policies = [{'status': 'approved', 'type': 'Build', 'name': 'ci', 'blocking': True}]
        run = {'__typename': 'CheckRun', 'name': 'ci', 'status': 'COMPLETED', 'conclusion': 'SUCCESS', 'isRequired': True}
        checks = [{'data': {'repository': {'pullRequest': {'commits': {'nodes': [{'commit': {'oid': HEAD, 'statusCheckRollup': {
            'contexts': {'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': [run]}}}}]}}}}}]
        self.both(['pr', 'checks', str(n)], [{'match': ['graphql', 'statusCheckRollup'], 'out': checks}])

    def test_pr_writes_and_comments(self):
        n = self.pr(draft=True)
        p = self.root / 'body.md'
        p.write_text('body\n')
        self.both(['pr', 'create', '--base', 'main', '--head', 'feat2', '--title', 't', '--body-file', str(p), '--draft',
                   '--label', 'bug', '--closes', '1'],
                  [{'match': ['POST', '/pulls'], 'out': 'https://github.com/o/n/pull/5\n5\n'}, {'match': ['labels'], 'out': 'bug'}],
                  kind='url-number')
        self.both(['pr', 'ready', str(n)], [{'match': ['pr ready'], 'out': ''}], kind='exact')
        self.both(['pr', 'comment', str(n), '--body-file', str(p)], [{'match': ['POST', 'issues/1/comments'], 'out': '4242\n'}], kind='line')
        self.both(['pr', 'comments', str(n)], [{'match': ['--paginate', 'issues/1/comments'], 'out': [[gh_row(id=4242)]]}], extra=('version',))
        gh_id = '4242'
        ado_id = self.ok('pr', 'comment', str(n), '--body-file', str(p)).strip()
        self.assertRegex(ado_id, r'^\d+\.\d+$', 'opaque to the caller, but what `pr comment-edit` takes back')
        self.gh.rules({'match': ['PATCH', 'issues/comments/4242'], 'out': '4242'})
        g = self.gh.forge('pr', 'comment-edit', str(n), gh_id, '--body-file', str(p))
        a = self.forge('pr', 'comment-edit', str(n), ado_id, '--body-file', str(p))
        self.assertEqual((g.returncode, a.returncode, g.stdout, a.stdout), (0, 0, gh_id + '\n', ado_id + '\n'))

    def test_inline_notes_threads_and_resolve(self):
        n = self.pr()
        p = self.root / 'note.md'
        p.write_text('**Purpose** x\n')
        head_rule = {'match': ['--jq', '.head.sha'], 'out': HEAD + '\n'}
        self.both(['pr', 'note', str(n), '--file', 'src/a.ts', '--line', '3', '--end-line', '5', '--head', HEAD, '--body-file', str(p)],
                  [head_rule, {'match': ['POST', 'pulls/1/comments'], 'out': '501\n'}], kind='line')
        self.both(['pr', 'note', str(n), '--file', 'src/b.ts', '--body-file', str(p)],
                  [head_rule, {'match': ['POST', 'pulls/1/comments'], 'out': '502\n'}], kind='line')
        notes = self.root / 'notes.json'
        notes.write_text(json.dumps([{'file': 'a.ts', 'line': 2, 'body': 'one'}, {'file': 'b.ts', 'body': 'two'}]))
        listed = [[{'id': 901, 'path': 'a.ts', 'body': 'one', 'line': 2, 'user': {'login': 'u'}, 'created_at': 'c', 'updated_at': 'c'}]]
        self.both(['pr', 'notes', str(n), '--notes-file', str(notes), '--head', HEAD],
                  [head_rule, {'match': ['POST', 'pulls/1/comments'], 'out': '902\n'}, {'match': ['POST', 'pulls/1/reviews'], 'out': '88\n'},
                   {'match': ['--paginate', 'reviews/88/comments'], 'out': listed}])
        # a moved head: both exit 6 and write nothing
        before = len(self.requests('POST', r'/threads$'))
        self.both(['pr', 'note', str(n), '--file', 'a.ts', '--line', '1', '--head', 'f' * 40, '--body-file', str(p)],
                  [{'match': ['--jq', '.head.sha'], 'out': HEAD + '\n'}], rc=6)
        self.assertEqual(len(self.requests('POST', r'/threads$')), before)
        thread = lambda i, resolved: {'id': f'PRRT_{i}', 'isResolved': resolved, 'isOutdated': False, 'path': 'src/a.ts', 'line': 5,
                                      'startLine': 3, 'originalLine': 5, 'diffSide': 'RIGHT', 'comments': {'nodes': [
                                          {'databaseId': 501, 'body': 'x', 'createdAt': '2026-11-01T00:00:00Z',
                                           'author': {'login': 'user@example.com', '__typename': 'User'}}]}}
        pages = [{'data': {'repository': {'pullRequest': {'reviewThreads': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                                                           'nodes': [thread(1, False)]}}}}}]
        g, a = self.both(['pr', 'threads', str(n)], [{'match': ['graphql', 'reviewThreads'], 'out': pages}])
        self.assertEqual(json.loads(a)[0]['line'], 5, 'the range ends on its last line')
        self.assertEqual(json.loads(g)[0]['startLine'], 3)
        ado_thread = json.loads(a)[0]
        # the id is opaque: each provider takes back what it gave out (`<thread>.<comment>` on Azure DevOps)
        g = self.gh.forge('pr', 'note-edit', str(n), '501', '--body-file', str(p))
        self.gh.rules({'match': ['PATCH', 'pulls/comments/501'], 'out': '501'})
        g = self.gh.forge('pr', 'note-edit', str(n), '501', '--body-file', str(p))
        a = self.forge('pr', 'note-edit', str(n), ado_thread['comments'][0]['id'], '--body-file', str(p))
        self.assertEqual((g.returncode, a.returncode, g.stdout, a.stdout),
                         (0, 0, '501\n', ado_thread['comments'][0]['id'] + '\n'))
        self.both(['pr', 'thread-resolve', str(n), '1'], [{'match': ['graphql', 'resolveReviewThread'], 'out': 'true\n'}],
                  kind='exact')

    # ---- board -----------------------------------------------------------------------------------------------
    def test_board_verbs(self):
        self.seed()
        project = {'project': {'owner': 'acme', 'number': 3}}
        cards = BoardTests.card_rules(self)
        items = {'data': {'repositoryOwner': {'projectV2': {'items': {'totalCount': 1, 'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': [
            {'id': 'ITEM1', 'fieldValueByName': {'name': 'Ready'},
             'content': {'__typename': 'Issue', 'number': 1, 'title': 't', 'url': 'u', 'repository': {'nameWithOwner': 'o/n'},
                         'labels': {'nodes': [{'name': 'feature'}]}}}]}}}}}
        self.both(['board', 'items'], [{'match': ['graphql', 'items(first:100'], 'out': [items]}, *cards],
                  extra=('assignees',), config=project)
        self.both(['board', 'status', '1'], cards, kind='exact', config=project)
        self.both(['board', 'move', '1', 'Done'], cards, kind='prefix:moved #1', config=project)
        self.both(['board', 'move', '1', 'Nowhere'], cards, rc=3, config=project)


class EveryVerbIsImplementedOnBothProviders(unittest.TestCase):
    def test_no_verb_is_left_unimplemented(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('forge_under_test', ROOT / 'scripts/super-board-forge.py')
        forge = importlib.util.module_from_spec(spec)
        sys.modules['forge_under_test'] = forge
        spec.loader.exec_module(forge)
        for provider, backend in forge.BACKENDS.items():
            for verb, method in forge.DISPATCH.items():
                self.assertIsNot(getattr(backend, method), getattr(forge.Backend, method),
                                 f'{provider} leaves "{" ".join(verb)}" unimplemented')


if __name__ == '__main__':
    unittest.main()
