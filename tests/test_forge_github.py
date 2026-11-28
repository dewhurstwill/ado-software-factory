#!/usr/bin/env python3
"""Offline contract for the GitHub LANE verbs of super-board-forge.py (super-board-forge-github.py).

A scripted `gh` (tests/fixtures/fake_gh.py) on PATH records every call; each test asserts the exact `gh`
argv and stdin, the stdout shape, and that a write runs once while a read goes through the retry/halt helper.
The same verbs against Azure DevOps, and the shape comparison between the two, are tests/test_forge_contract.py."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE = ROOT / 'scripts/super-board-forge.py'
FAKE_GH = ROOT / 'tests/fixtures/fake_gh.py'
HEAD = 'a' * 40
NEW_HEAD = 'b' * 40


def gh_issue(n=7, **kw):
    value = {'number': n, 'title': 'T', 'body': 'B', 'state': 'OPEN', 'stateReason': '',
             'labels': [{'id': 'x', 'name': 'bug', 'color': 'ff0000'}],
             'assignees': [{'id': 'y', 'login': 'octo', 'name': 'O', 'isBot': False}], 'url': f'https://github.com/o/n/issues/{n}'}
    value.update(kw)
    return value


class GhCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        shutil.copy(FAKE_GH, gh)
        gh.write_text(gh.read_text(encoding='utf-8').replace('#!/usr/bin/env python3', f'#!{sys.executable}', 1), encoding='utf-8')
        gh.chmod(0o755)
        self.log = self.root / 'calls.jsonl'
        self.rules_file = self.root / 'rules.json'
        self.halt = self.root / 'halt.json'
        self.env = {k: v for k, v in os.environ.items() if k not in ('SB_CONFIG', 'GH_REPO', 'SB_REPO_PATH', 'BUILD_LOOP_OWNER', 'BUILD_LOOP_PROJECT')}
        self.env.update(PATH=f'{self.bin}{os.pathsep}{os.environ["PATH"]}', FAKE_GH_LOG=str(self.log),
                        FAKE_GH_RULES=str(self.rules_file), SB_GITHUB_HALT_FILE=str(self.halt), SB_GITHUB_RETRY_DELAY='0',
                        SB_CARD_CACHE_DIR=str(self.root / 'cache'))
        self.rules_file.write_text('[]')
        self.config_value = {'repo': {'remote': 'https://github.com/o/n.git'}}
        self.cfg_path = self.root / 'cfg.json'

    def tearDown(self):
        self.temp.cleanup()

    def rules(self, *rules):
        self.rules_file.write_text(json.dumps(list(rules)))

    def forge(self, *args, env=None, **config):
        self.cfg_path.write_text(json.dumps(dict(self.config_value, **config)))
        e = dict(self.env, **(env or {}))
        return subprocess.run([sys.executable, str(FORGE), '--config', str(self.cfg_path), *args], env=e, text=True,
                              stdin=subprocess.DEVNULL, capture_output=True, cwd=self.root)

    def ok(self, *args, **kw):
        r = self.forge(*args, **kw)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout

    def js(self, *args, **kw):
        return json.loads(self.ok(*args, **kw))

    def calls(self):
        return [json.loads(l) for l in self.log.read_text(encoding='utf-8').splitlines()] if self.log.exists() else []

    def argvs(self):
        return [c['argv'] for c in self.calls()]

    def put(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return str(path)


class TicketTests(GhCase):
    def test_view_and_list_give_the_neutral_ticket(self):
        self.rules({'match': ['issue view'], 'out': gh_issue()},
                   {'match': ['issue list'], 'out': [gh_issue(3, state='CLOSED', stateReason='NOT_PLANNED'), gh_issue(4, state='CLOSED', stateReason=None)]})
        want = {'number': 7, 'title': 'T', 'body': 'B', 'state': 'OPEN', 'stateReason': None, 'labels': [{'name': 'bug'}],
                'assignees': [{'login': 'octo'}], 'url': 'https://github.com/o/n/issues/7'}
        self.assertEqual(self.js('ticket', 'view', '7'), want)
        closed = self.js('ticket', 'list', '--state', 'closed', '--label', 'a', '--label', 'b', '--limit', '5')
        self.assertEqual([(t['number'], t['stateReason']) for t in closed], [(3, 'NOT_PLANNED'), (4, 'COMPLETED')])
        self.js('ticket', 'list')
        self.assertEqual(self.argvs()[1], ['issue', 'list', '--repo', 'o/n', '--state', 'closed', '--limit', '5', '--label', 'a',
                                           '--label', 'b', '--json', 'number,title,body,state,stateReason,labels,assignees,url'])
        self.assertEqual(self.argvs()[2][:7], ['issue', 'list', '--repo', 'o/n', '--state', 'open', '--limit'])
        self.assertEqual(self.argvs()[2][7], '30', 'defaults match Azure DevOps: open, 30')

    def test_history_flattens_the_pages(self):
        node = {'number': 1, 'state': 'OPEN', 'body': 'x'}
        page = {'data': {'repository': {'issues': {'totalCount': 1, 'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                                    'nodes': [node]}}}}
        self.rules({'match': ['graphql', 'states:[OPEN,CLOSED]'], 'out': [page]})
        self.assertEqual(self.js('ticket', 'history'), [node])

    def test_create_posts_once_and_prints_url_then_number(self):
        self.rules({'match': ['api', '-X POST', 'repos/{owner}/{repo}/issues', '--jq'], 'out': 'https://github.com/o/n/issues/9\n9\n'})
        r = self.ok('ticket', 'create', '--title', 'A "title" é', '--body-file', self.put('b.md', 'body\n'),
                    '--label', 'bug', '--label', 'qa')
        self.assertEqual(r, 'https://github.com/o/n/issues/9\n9\n')
        (call,) = self.calls()
        self.assertEqual(call['argv'][:4], ['api', '-X', 'POST', 'repos/{owner}/{repo}/issues'])
        self.assertEqual(json.loads(call['stdin']), {'title': 'A "title" é', 'body': 'body\n', 'labels': ['bug', 'qa']})
        self.assertEqual(call['gh_repo'], 'o/n')

    def test_a_failed_create_is_not_retried(self):
        self.rules({'match': ['api'], 'rc': 1, 'err': 'HTTP 502'})
        r = self.forge('ticket', 'create', '--title', 't', '--body-file', self.put('b.md', 'x'))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(self.halt.exists())

    def test_edit_title_body_and_labels(self):
        self.rules({'match': ['api'], 'out': ''}, )
        self.ok('ticket', 'edit', '5', '--title', 'new', '--body-file', self.put('b.md', 'nb'), '--add-label', 'a',
                '--add-label', 'b', '--remove-label', 'loop:in-build')
        calls = self.calls()
        self.assertEqual([c['argv'][:4] for c in calls], [
            ['api', '-X', 'PATCH', 'repos/{owner}/{repo}/issues/5'], ['api', '-X', 'POST', 'repos/{owner}/{repo}/issues/5/labels'],
            ['api', '-X', 'DELETE', 'repos/{owner}/{repo}/issues/5/labels/loop%3Ain-build']])
        self.assertEqual(json.loads(calls[0]['stdin']), {'title': 'new', 'body': 'nb'})
        self.assertEqual(json.loads(calls[1]['stdin']), {'labels': ['a', 'b']})

    def test_removing_a_label_that_is_not_there_is_fine(self):
        self.rules({'match': ['DELETE'], 'rc': 1, 'err': 'gh: Label does not exist (HTTP 404)'})
        self.assertEqual(self.forge('ticket', 'edit', '5', '--remove-label', 'gone').returncode, 0)
        self.rules({'match': ['DELETE'], 'rc': 1, 'err': 'HTTP 500'})
        self.assertEqual(self.forge('ticket', 'edit', '5', '--remove-label', 'gone').returncode, 1)

    def test_close_reasons_and_the_card_goes_to_done(self):
        self.rules({'match': ['PATCH', 'issues/5'], 'out': 'closed'})
        self.assertEqual(self.ok('ticket', 'close', '5', '--reason', 'not-planned'), 'closed #5 (not-planned)\n')
        self.assertEqual(json.loads(self.calls()[0]['stdin']), {'state': 'closed', 'state_reason': 'not_planned'})
        self.ok('ticket', 'close', '5', '--reason', 'completed')
        self.assertEqual(json.loads(self.calls()[1]['stdin'])['state_reason'], 'completed')
        self.assertEqual(self.forge('ticket', 'close', '5', '--reason', 'later').returncode, 64)
        self.assertEqual(len(self.calls()), 2, 'no project in the config: no card move')

    def test_comment_edit_and_comments(self):
        self.rules({'match': ['POST', 'issues/5/comments'], 'out': '4242\n'}, {'match': ['PATCH', 'issues/comments/4242'], 'out': '4242'},
                   {'match': ['--paginate', 'issues/5/comments'],
                    'out': [[{'id': 4242, 'body': 'b', 'user': {'login': 'u', 'type': 'User'}, 'created_at': 'c', 'updated_at': 'c'}]]})
        self.assertEqual(self.ok('ticket', 'comment', '5', '--body-file', self.put('c.md', 'hi')), '4242\n')
        self.assertEqual(self.ok('ticket', 'comment-edit', '5', '4242', '--body-file', self.put('c.md', 'edited')), '4242\n')
        self.assertEqual(json.loads(self.calls()[1]['stdin']), {'body': 'edited'})
        self.assertEqual(self.js('ticket', 'comments', '5')[0][0]['id'], 4242)
        self.assertEqual(self.forge('ticket', 'comment-edit', '5', 'x', '--body-file', self.put('c.md', 'e')).returncode, 64)


class ClaimTests(GhCase):
    def claim_rules(self, before, after, post_rc=0):
        self.rules({'match': ['--jq', '[.assignees[].login]'], 'outs': [before, after]},
                   {'match': ['POST', 'issues/5/assignees'], 'out': '5', 'rc': post_rc},
                   {'match': ['DELETE', 'issues/5/assignees'], 'out': '5'})

    def test_claim_adds_the_bot_then_verifies_exactly_it(self):
        self.claim_rules([], ['super-board-bot[bot]'])
        out = self.ok('ticket', 'claim', '5', notifications={'bot_identity': 'super-board-bot[bot]'})
        self.assertEqual(out, 'claimed #5\n')
        add = self.calls()[1]
        self.assertEqual(add['argv'][:4], ['api', '-X', 'POST', 'repos/{owner}/{repo}/issues/5/assignees'])
        self.assertEqual(json.loads(add['stdin']), {'assignees': ['super-board-bot[bot]']})
        self.assertEqual(len(self.calls()), 3, 'read, one write, re-read')

    def test_somebody_elses_claim_is_never_stolen(self):
        self.claim_rules(['someone'], ['someone'])
        r = self.forge('ticket', 'claim', '5', notifications={'bot_identity': 'me'})
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(len(self.calls()), 1, 'no write when the card is held')

    def test_a_race_lost_takes_our_assignee_back(self):
        self.claim_rules([], ['someone', 'me'])
        r = self.forge('ticket', 'claim', '5', notifications={'bot_identity': 'me'})
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(json.loads(self.calls()[-1]['stdin']), {'assignees': ['me']})
        self.assertEqual(self.calls()[-1]['argv'][1:3], ['-X', 'DELETE'])

    def test_claim_is_idempotent_and_an_unaccepted_assignee_is_a_failure(self):
        self.claim_rules(['Me'], ['Me'])
        self.assertEqual(self.ok('ticket', 'claim', '5', notifications={'bot_identity': 'me'}), 'claimed #5\n')
        self.assertEqual(len(self.calls()), 1)
        self.claim_rules([], [])
        self.assertEqual(self.forge('ticket', 'claim', '5', notifications={'bot_identity': 'me'}).returncode, 69)

    def test_auto_and_unset_resolve_to_the_authenticated_login(self):
        self.rules({'match': ['api', 'user'], 'out': {'login': 'octo', 'id': '1'}},
                   {'match': ['--jq', '[.assignees[].login]'], 'outs': [[], ['octo']]},
                   {'match': ['POST', 'assignees'], 'out': '5'})
        self.ok('ticket', 'claim', '5', notifications={'bot_identity': 'auto'})
        self.assertEqual(json.loads(self.calls()[2]['stdin']), {'assignees': ['octo']})

    def test_release_removes_only_our_assignee_and_is_idempotent(self):
        self.rules({'match': ['--jq', '[.assignees[].login]'], 'outs': [['me', 'human'], []]},
                   {'match': ['DELETE', 'assignees'], 'out': '5'})
        self.assertEqual(self.ok('ticket', 'release', '5', '--force', notifications={'bot_identity': 'me'}), 'released #5 (me)\n')
        self.assertEqual(json.loads(self.calls()[1]['stdin']), {'assignees': ['me']})
        self.assertEqual(self.ok('ticket', 'release', '5', notifications={'bot_identity': 'me'}), 'released #5 (nothing held)\n')
        self.assertEqual(len(self.calls()), 3)


class IdentityTests(GhCase):
    def test_whoami_quota_repository(self):
        rest = {'resources': {'graphql': {'limit': 5000, 'remaining': 4000, 'used': 1000, 'reset': 100},
                              'core': {'limit': 5000, 'remaining': 4900, 'used': 100, 'reset': 200}}}
        gql = {'data': {'rateLimit': {'limit': 5000, 'remaining': 120, 'used': 4880, 'resetAt': '2030-01-01T00:00:00Z'}}}
        self.rules({'match': ['api', 'user'], 'out': {'login': 'octo', 'id': '7'}},
                   {'match': ['api', 'rate_limit'], 'out': rest}, {'match': ['graphql', 'rateLimit'], 'out': gql},
                   {'match': ['repos/{owner}/{repo}', '--jq', 'defaultBranch'], 'out': {'repository': 'n', 'defaultBranch': 'main'}},
                   {'match': ['branches?per_page=1'], 'out': [{'name': 'main'}]},
                   {'match': ['matching-refs/heads/dev'], 'out': [{'ref': 'refs/heads/dev'}, {'ref': 'refs/heads/dev2'}]})
        self.assertEqual(self.js('whoami'), {'login': 'octo', 'id': '7'})
        q = self.js('quota')
        self.assertEqual((q['remaining'], q['limit'], q['seen']), (120, 5000, True), 'the top level is the GraphQL bucket')
        self.assertEqual(q['graphql']['remaining'], 120, "GraphQL's own rateLimit wins over the REST report")
        self.assertEqual(q['core']['remaining'], 4900)
        self.assertEqual(q['reset'], 1893456000)
        self.assertEqual(self.js('repository', base_branch='dev'), {'repository': 'n', 'defaultBranch': 'main', 'empty': False,
                                                                     'base': 'dev', 'baseExists': True})

    def test_an_empty_repository_has_no_default_branch_and_no_base(self):
        self.rules({'match': ['--jq', 'defaultBranch'], 'out': {'repository': 'n', 'defaultBranch': 'main'}},
                   {'match': ['branches?per_page=1'], 'out': []})
        self.assertEqual(self.js('repository', base_branch='main'),
                         {'repository': 'n', 'defaultBranch': None, 'empty': True, 'base': 'main', 'baseExists': False})
        self.assertEqual(self.js('repository')['baseExists'], None)


class PrReadTests(GhCase):
    def test_view_refs_merged_links(self):
        view = {'number': 3, 'title': 't', 'url': 'u', 'state': 'OPEN', 'isDraft': True, 'headRefName': 'f', 'headRefOid': HEAD,
                'baseRefName': 'main', 'mergeCommit': None, 'labels': [{'name': 'bug'}]}
        refs = [{'number': 1, 'headRefName': 'a', 'baseRefName': 'main', 'headRefOid': HEAD},
                {'number': 2, 'headRefName': 'b', 'baseRefName': 'main', 'headRefOid': ''}]
        merged = [{'number': 9, 'title': 'new', 'url': 'u9', 'mergedAt': '2026-11-02T10:00:00Z', 'author': {'login': 'dependabot[bot]', 'is_bot': True}},
                  {'number': 8, 'title': 'old', 'url': 'u8', 'mergedAt': '2026-01-02T10:00:00Z', 'author': {'login': 'h'}},
                  {'number': 10, 'title': 'newer', 'url': 'u10', 'mergedAt': '2026-11-03T10:00:00Z', 'author': {'login': 'h', 'is_bot': False}}]
        page = {'data': {'repository': {'defaultBranchRef': {'name': 'main'}, 'pullRequest': {
            'headRefOid': HEAD, 'state': 'OPEN', 'baseRefName': 'main', 'body': '',
            'closingIssuesReferences': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                        'nodes': [{'number': 12, 'repository': {'nameWithOwner': 'o/n'}}]}}}}}
        self.rules({'match': ['api', 'pulls/3', '--jq', 'mergeCommit'], 'out': view},
                   {'match': ['pr list', 'headRefOid'], 'out': refs}, {'match': ['pr list', 'mergedAt'], 'out': merged},
                   {'match': ['graphql', 'closingIssuesReferences'], 'out': [page]})
        self.assertEqual(self.js('pr', 'view', '3'), view)
        self.assertEqual(self.calls()[0]['gh_repo'], 'o/n')
        self.assertEqual(self.js('pr', 'refs', '--state', 'open', '--limit', '10'), refs)
        self.assertEqual(self.js('pr', 'refs', '--state', 'merged', '--limit', '10'), refs[:1], 'a merged row with no head is left out')
        out = self.js('pr', 'merged', '--since', '2026-11-01T00:00:00Z', '--limit', '10')
        self.assertEqual([(r['number'], r['author']) for r in out], [(10, {'login': 'h', 'type': 'User'}),
                                                                      (9, {'login': 'dependabot[bot]', 'type': 'Bot'})])
        self.assertEqual(self.js('pr', 'links', '3'), {'closes': [12]})
        self.assertEqual(self.forge('pr', 'merged', '--since', 'yesterday', '--limit', '1').returncode, 64)
        self.assertEqual(self.argvs()[1], ['pr', 'list', '--repo', 'o/n', '--state', 'open', '--limit', '10', '--json',
                                           'number,headRefName,baseRefName,headRefOid'])

    def test_comments_are_the_conversation_with_string_ids(self):
        row = {'id': 11, 'body': 'b', 'user': {'login': 'u', 'type': 'User'}, 'created_at': 'c', 'updated_at': 'c'}
        self.rules({'match': ['--paginate', 'issues/3/comments'], 'out': [[row]]})
        self.assertEqual(self.js('pr', 'comments', '3'), [[dict(row, id='11')]])

    def test_threads_are_reshaped_with_ids_anchors_and_authors(self):
        def thread(i, resolved, path, line, start, side, outdated=False):
            return {'id': f'PRRT_{i}', 'isResolved': resolved, 'isOutdated': outdated, 'path': path, 'line': line,
                    'startLine': start, 'originalLine': line, 'diffSide': side,
                    'comments': {'nodes': [{'databaseId': i * 10, 'body': f'c{i}', 'createdAt': '2026-11-01T00:00:00Z',
                                            'author': {'login': 'copilot', '__typename': 'Bot'}}]}}
        pages = [{'data': {'repository': {'pullRequest': {'reviewThreads': {'pageInfo': {'hasNextPage': True, 'endCursor': 'c'},
                                                                           'nodes': [thread(1, False, 'a.ts', 5, 3, 'RIGHT')]}}}}},
                 {'data': {'repository': {'pullRequest': {'reviewThreads': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                                                           'nodes': [thread(2, True, 'b.ts', None, None, None, True)]}}}}}]
        self.rules({'match': ['graphql', 'reviewThreads'], 'out': pages})
        out = self.js('pr', 'threads', '3')
        self.assertEqual(out[0], {'id': 'PRRT_1', 'path': 'a.ts', 'status': 'active', 'resolved': False, 'line': 5, 'startLine': 3,
                                  'side': 'right', 'outdated': False,
                                  'comments': [{'id': '10', 'author': {'login': 'copilot', 'type': 'Bot'}, 'body': 'c1',
                                                'createdAt': '2026-11-01T00:00:00Z'}]})
        self.assertEqual((out[1]['resolved'], out[1]['status'], out[1]['outdated'], out[1]['line'], out[1]['side']),
                         (True, 'resolved', True, None, None))
        argv = self.argvs()[0]
        self.assertEqual(argv[:4], ['api', 'graphql', '--paginate', '--slurp'])
        self.assertEqual(argv[-6:], ['-F', 'owner=o', '-F', 'repo=n', '-F', 'pr=3'])

    def test_checks_cover_check_runs_and_statuses_with_required(self):
        def page(nodes, more=False):
            return {'data': {'repository': {'pullRequest': {'commits': {'nodes': [{'commit': {'oid': HEAD, 'statusCheckRollup': {
                'contexts': {'pageInfo': {'hasNextPage': more, 'endCursor': 'x'}, 'nodes': nodes}}}}]}}}}}
        run = lambda name, status, conclusion, req: {'__typename': 'CheckRun', 'name': name, 'status': status, 'conclusion': conclusion, 'isRequired': req}
        pages = [page([run('build', 'COMPLETED', 'SUCCESS', True), run('lint', 'IN_PROGRESS', None, False)], True),
                 page([run('e2e', 'COMPLETED', 'FAILURE', True), run('docs', 'COMPLETED', 'SKIPPED', False),
                       {'__typename': 'StatusContext', 'context': 'ci/legacy', 'state': 'PENDING', 'isRequired': False},
                       run('flaky', 'COMPLETED', 'CANCELLED', False)])]
        self.rules({'match': ['graphql', 'statusCheckRollup'], 'out': pages})
        self.assertEqual(self.js('pr', 'checks', '3'), [
            {'name': 'build', 'status': 'pass', 'required': True}, {'name': 'lint', 'status': 'pending', 'required': False},
            {'name': 'e2e', 'status': 'fail', 'required': True}, {'name': 'docs', 'status': 'skipped', 'required': False},
            {'name': 'ci/legacy', 'status': 'pending', 'required': False}, {'name': 'flaky', 'status': 'fail', 'required': False}])
        self.rules({'match': ['graphql'], 'out': [{'data': {'repository': {'pullRequest': {'commits': {'nodes': [
            {'commit': {'oid': HEAD, 'statusCheckRollup': None}}]}}}}}]})
        self.assertEqual(self.js('pr', 'checks', '3'), [], 'no checks at all: an empty list')

    def test_a_new_read_halts_with_79_after_three_failures(self):
        self.rules({'match': ['api'], 'rc': 1, 'err': 'HTTP 503'})
        r = self.forge('pr', 'view', '3')
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(len(self.calls()), 3)
        self.assertTrue(self.halt.exists())
        self.assertEqual(self.forge('pr', 'checks', '3').returncode, 79)
        self.assertEqual(len(self.calls()), 3, 'a halted run reads nothing more')
        self.assertEqual(self.forge('ticket', 'claim', '5').returncode, 79, 'claim reads first: halted')


class PrWriteTests(GhCase):
    def test_create_draft_with_labels_and_a_closing_keyword(self):
        self.rules({'match': ['POST', 'repos/{owner}/{repo}/pulls', '--jq'], 'out': 'https://github.com/o/n/pull/4\n4\n'},
                   {'match': ['POST', 'issues/4/labels'], 'out': 'bug'})
        out = self.ok('pr', 'create', '--base', 'main', '--head', 'loop/issue-9', '--title', 'T', '--body-file', self.put('b.md', 'body'),
                      '--draft', '--label', 'bug', '--closes', '9', '--closes', '#9')
        self.assertEqual(out, 'https://github.com/o/n/pull/4\n4\n')
        create, label = self.calls()
        self.assertEqual(json.loads(create['stdin']), {'title': 'T', 'head': 'loop/issue-9', 'base': 'main', 'draft': True,
                                                       'body': 'body\n\nCloses #9\n'})
        self.assertEqual(json.loads(label['stdin']), {'labels': ['bug']})
        # a body that already closes the ticket is left alone
        self.log.unlink()
        self.ok('pr', 'create', '--base', 'main', '--head', 'h', '--title', 'T', '--closes', '9',
                '--body-file', self.put('b.md', 'fixes #9 already'))
        self.assertEqual(json.loads(self.calls()[0]['stdin'])['body'], 'fixes #9 already')
        self.assertEqual(self.forge('pr', 'create', '--base', 'm', '--head', 'h', '--title', 'T', '--closes', 'x',
                                    '--body-file', self.put('b.md', 'b')).returncode, 64)

    def test_a_label_failure_still_prints_the_pr_and_fails(self):
        self.rules({'match': ['POST', '/pulls'], 'out': 'u\n4\n'}, {'match': ['labels'], 'rc': 1, 'err': 'HTTP 403'})
        r = self.forge('pr', 'create', '--base', 'main', '--head', 'h', '--title', 'T', '--label', 'x',
                       '--body-file', self.put('b.md', 'b'))
        self.assertEqual((r.returncode, r.stdout), (69, 'u\n4\n'))

    def test_ready_comment_edit(self):
        self.rules({'match': ['pr ready'], 'out': ''}, {'match': ['POST', 'issues/4/comments'], 'out': '77\n'},
                   {'match': ['PATCH', 'issues/comments/77'], 'out': '77'})
        self.assertEqual(self.ok('pr', 'ready', '4'), 'ready #4\n')
        self.assertEqual(self.argvs()[0], ['pr', 'ready', '4', '--repo', 'o/n'])
        self.assertEqual(self.ok('pr', 'comment', '4', '--body-file', self.put('c.md', 'x')), '77\n')
        self.assertEqual(self.ok('pr', 'comment-edit', '4', '77', '--body-file', self.put('c.md', 'y')), '77\n')

    def head_rule(self, sha=HEAD):
        return {'match': ['pulls/4', '--jq', '.head.sha'], 'out': sha + '\n'}

    def test_a_line_note_is_a_review_comment_at_the_current_head(self):
        self.rules(self.head_rule(), {'match': ['POST', 'pulls/4/comments'], 'out': '501\n'})
        self.assertEqual(self.ok('pr', 'note', '4', '--file', '/src/a.ts', '--line', '7', '--body-file', self.put('n.md', 'why')), '501\n')
        post = self.calls()[1]
        self.assertEqual(json.loads(post['stdin']), {'body': 'why', 'commit_id': HEAD, 'path': 'src/a.ts', 'line': 7, 'side': 'RIGHT'})

    def test_range_left_side_and_whole_file_notes(self):
        self.rules(self.head_rule(), {'match': ['POST', 'pulls/4/comments'], 'out': '1\n'})
        self.ok('pr', 'note', '4', '--file', 'a.ts', '--line', '3', '--end-line', '9', '--side', 'left', '--head', HEAD,
                '--body-file', self.put('n.md', 'r'))
        self.ok('pr', 'note', '4', '--file', 'b.ts', '--body-file', self.put('n.md', 'f'))
        sent = [json.loads(c['stdin']) for c in self.calls() if c['stdin']]
        self.assertEqual(sent[0], {'body': 'r', 'commit_id': HEAD, 'path': 'a.ts', 'line': 9, 'side': 'LEFT',
                                   'start_line': 3, 'start_side': 'LEFT'})
        self.assertEqual(sent[1], {'body': 'f', 'commit_id': HEAD, 'path': 'b.ts', 'subject_type': 'file'})

    def test_note_with_a_moved_head_writes_nothing(self):
        self.rules(self.head_rule(NEW_HEAD), {'match': ['POST'], 'out': '1'})
        r = self.forge('pr', 'note', '4', '--file', 'a.ts', '--line', '1', '--head', HEAD, '--body-file', self.put('n.md', 'x'))
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertEqual(len(self.calls()), 1)
        for bad in (['--line', '1', '--side', 'up'], ['--end-line', '3'], ['--line', '4', '--end-line', '2']):
            self.assertEqual(self.forge('pr', 'note', '4', '--file', 'a.ts', *bad, '--body-file', self.put('n.md', 'x')).returncode, 64, bad)

    def test_a_note_write_is_never_retried(self):
        self.rules(self.head_rule(), {'match': ['POST'], 'rc': 1, 'err': 'HTTP 502'})
        r = self.forge('pr', 'note', '4', '--file', 'a.ts', '--line', '1', '--body-file', self.put('n.md', 'x'))
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.calls()), 2, 'one head read, one write')

    def test_notes_batch_puts_line_notes_in_one_review_and_files_apart(self):
        notes = [{'file': 'a.ts', 'line': 2, 'body': 'one'}, {'file': 'b.ts', 'body': 'whole file'},
                 {'file': 'c.ts', 'line': 1, 'end_line': 3, 'side': 'right', 'body': 'three'}]
        listed = [[{'id': 903, 'path': 'c.ts', 'body': 'three', 'line': 3, 'user': {'login': 'u'}, 'created_at': 'c', 'updated_at': 'c'},
                   {'id': 901, 'path': 'a.ts', 'body': 'one', 'line': 2, 'user': {'login': 'u'}, 'created_at': 'c', 'updated_at': 'c'}]]
        self.rules(self.head_rule(), {'match': ['POST', 'pulls/4/comments'], 'out': '902\n'},
                   {'match': ['POST', 'pulls/4/reviews'], 'out': '88\n'}, {'match': ['--paginate', 'pulls/4/reviews/88/comments'], 'out': listed})
        out = self.js('pr', 'notes', '4', '--notes-file', self.put('n.json', json.dumps(notes)), '--head', HEAD)
        self.assertEqual(out, [{'index': 0, 'id': '901'}, {'index': 1, 'id': '902'}, {'index': 2, 'id': '903'}])
        review = next(json.loads(c['stdin']) for c in self.calls() if c['argv'][3:4] == ['repos/{owner}/{repo}/pulls/4/reviews'])
        self.assertEqual(review['event'], 'COMMENT')
        self.assertEqual(review['commit_id'], HEAD)
        self.assertEqual(review['comments'], [{'body': 'one', 'path': 'a.ts', 'line': 2, 'side': 'RIGHT'},
                                              {'body': 'three', 'path': 'c.ts', 'line': 3, 'side': 'RIGHT', 'start_line': 1, 'start_side': 'RIGHT'}])
        self.assertEqual(sum(1 for c in self.calls() if c['argv'][3:4] == ['repos/{owner}/{repo}/pulls/4/reviews']), 1)

    def test_note_edit_and_thread_resolve(self):
        self.rules({'match': ['PATCH', 'pulls/comments/501'], 'out': '501'},
                   {'match': ['graphql', 'resolveReviewThread'], 'out': 'true\n'})
        self.assertEqual(self.ok('pr', 'note-edit', '4', '501', '--body-file', self.put('n.md', 'new')), '501\n')
        self.assertEqual(json.loads(self.calls()[0]['stdin']), {'body': 'new'})
        self.assertEqual(self.ok('pr', 'thread-resolve', '4', 'PRRT_abc'), 'resolved PRRT_abc\n')
        argv = self.calls()[1]['argv']
        self.assertEqual(argv[:2], ['api', 'graphql'])
        self.assertIn('threadId=PRRT_abc', argv)
        self.assertTrue(any(a.startswith('query=mutation') for a in argv))
        self.assertEqual(self.forge('pr', 'thread-resolve', '4', 'PR RT').returncode, 64)


class BoardTests(GhCase):
    PROJECT = {'project': {'owner': 'acme', 'number': 3}}

    def card_rules(self):
        ids = {'data': {'repositoryOwner': {'projectV2': {'id': 'P1', 'field': {'id': 'F1', 'options': [
            {'id': 'o1', 'name': 'Ready'}, {'id': 'o2', 'name': 'Done'}]}}}}}
        node = {'data': {'repository': {'issueOrPullRequest': {'projectItems': {'nodes': [
            {'id': 'ITEM9', 'project': {'number': 3, 'owner': {'login': 'acme'}}, 'fieldValueByName': {'name': 'Ready'}}]}}}}}
        return [{'match': ['graphql', 'projectV2(number:$number){ id'], 'out': ids},
                {'match': ['graphql', 'issueOrPullRequest'], 'out': node},
                {'match': ['graphql', 'updateProjectV2ItemFieldValue'], 'out': {'data': {}}}]

    def test_board_verbs_are_the_card_script(self):
        self.rules(*self.card_rules())
        self.assertEqual(self.ok('board', 'status', '9', **self.PROJECT), 'Ready\n')
        self.assertEqual(self.ok('board', 'move', '9', 'Done', **self.PROJECT), 'moved #9 → Done\n')
        self.assertEqual(self.ok('board', 'item-id', '9', **self.PROJECT), 'ITEM9\n')
        ids = self.js('board', 'ids', **self.PROJECT)
        self.assertEqual(ids['options'], {'Ready': 'o1', 'Done': 'o2'})
        r = self.forge('board', 'move', '9', 'Nowhere', **self.PROJECT)
        self.assertEqual(r.returncode, 3, r.stderr)

    def test_board_ensure_is_setup(self):
        r = self.forge('board', 'ensure', **self.PROJECT)
        self.assertEqual(r.returncode, 69)
        self.assertIn('super-board-setup.py', r.stderr)
        self.assertEqual(self.forge('board', 'nope').returncode, 64)

    def test_close_moves_the_card_and_a_move_failure_only_warns(self):
        self.rules(*self.card_rules(), {'match': ['PATCH', 'issues/9'], 'out': 'closed'})
        r = self.forge('ticket', 'close', '9', '--reason', 'completed', **self.PROJECT)
        self.assertEqual((r.returncode, r.stdout), (0, 'closed #9 (completed)\n'))
        self.assertTrue(any('updateProjectV2ItemFieldValue' in ' '.join(a) for a in self.argvs()))
        self.rules({'match': ['PATCH', 'issues/9'], 'out': 'closed'})  # the board cannot be read now
        r = self.forge('ticket', 'close', '9', '--reason', 'completed', **self.PROJECT)
        self.assertEqual(r.returncode, 0)
        self.assertIn('could not move its card to Done', r.stderr)

    def test_create_with_a_column_adds_the_card_and_moves_it(self):
        self.rules(*self.card_rules(), {'match': ['POST', '/issues', '--jq'], 'out': 'https://github.com/o/n/issues/9\n9\n'},
                   {'match': ['project item-add'], 'out': ''})
        out = self.ok('ticket', 'create', '--title', 't', '--body-file', self.put('b.md', 'b'), '--column', 'Ready', **self.PROJECT)
        self.assertEqual(out, 'https://github.com/o/n/issues/9\n9\n')
        add = next(a for a in self.argvs() if a[:2] == ['project', 'item-add'])
        self.assertEqual(add, ['project', 'item-add', '3', '--owner', 'acme', '--url', 'https://github.com/o/n/issues/9'])
        # an unknown column creates nothing
        self.log.unlink()
        r = self.forge('ticket', 'create', '--title', 't', '--body-file', self.put('b.md', 'b'), '--column', 'Nowhere', **self.PROJECT)
        self.assertEqual(r.returncode, 3)
        self.assertFalse(any(a[:2] == ['api', '-X'] for a in self.argvs()), 'nothing was created')
        self.assertEqual(self.forge('ticket', 'create', '--title', 't', '--body-file', self.put('b.md', 'b'), '--column', 'Ready').returncode, 66)


if __name__ == '__main__':
    unittest.main()
