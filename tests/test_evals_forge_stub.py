#!/usr/bin/env python3
"""The paid evals' offline `gh` (evals/_stubs/bin/gh) answers every forge verb the evaluated skills use.

Lane instructions now run `python3 .claude/bin/super-board-forge.py --config <cfg> <verb>`; on GitHub the
forge sends its own `gh` argv (REST `api repos/...`, GraphQL). This seeds two eval cases exactly as their
seed.sh does (review-remembers, collect-duplicate), then drives the installed forge against the stub for
each verb and checks the output shape and the logs the graders read. No model, no network.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
STUBS = KIT / 'evals' / '_stubs' / 'bin'


def seeded(case):
    tmp = Path(tempfile.mkdtemp(prefix=f'sb-eval-{case}-'))
    home, ws = tmp / 'home', tmp / 'ws'
    home.mkdir()
    ws.mkdir()
    env = dict(os.environ, HOME=str(home), PATH=f'{STUBS}{os.pathsep}{os.environ["PATH"]}',
               SB_GITHUB_RETRY_DELAY='0', GIT_CONFIG_NOSYSTEM='1')
    env.pop('SB_CONFIG', None)
    env.pop('GH_REPO', None)
    r = subprocess.run(['bash', str(KIT / 'evals' / case / 'seed.sh'), str(ws)], cwd=ws, env=env,
                       capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(f'{case} seed failed: {r.stderr}')
    return tmp, ws, env


class Case(unittest.TestCase):
    case = None
    config = None

    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.ws, cls.env = seeded(cls.case)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def forge(self, *args, ok=True):
        r = subprocess.run([sys.executable, '.claude/bin/super-board-forge.py', '--config', self.config, *args],
                           cwd=self.ws, env=self.env, capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, f'forge {" ".join(args)}: {r.stderr}')
        return r

    def json(self, *args):
        return json.loads(self.forge(*args).stdout)

    def body(self, text):
        path = self.ws / '.git' / f'body-{abs(hash(text))}.md'
        path.write_text(text)
        return str(path)

    def log(self, name):
        path = self.ws / name
        return path.read_text() if path.exists() else ''


class ReviewRemembers(Case):
    """super-review / super-qa: the PR verbs, ticket reads and writes, claims and the board."""
    case = 'review-remembers'
    config = '.claude/super-board/configs/shop.json'

    def test_identity_and_quota(self):
        self.assertEqual(self.forge('provider').stdout.strip(), 'github')
        self.assertEqual(self.forge('repo').stdout.strip(), 'example/shop')
        self.assertEqual(self.json('whoami')['login'], 'super-board-bot')
        self.assertEqual(self.json('quota')['remaining'], 4988)
        repo = self.json('repository')
        self.assertEqual((repo['defaultBranch'], repo['empty'], repo['baseExists']), ('main', False, True))
        self.forge('check')

    def test_pr_reads(self):
        view = self.json('pr', 'view', '12')
        self.assertEqual((view['number'], view['state'], view['baseRefName']), (12, 'OPEN', 'main'))
        head = self.forge('pr', 'head', '12').stdout.split()
        self.assertEqual(head[0], 'issue-7-cart-discount')
        self.assertEqual(head[1], view['headRefOid'])
        self.assertEqual(self.json('pr', 'state', '12')['state'], 'OPEN')
        self.assertIn('Local tests:', self.json('pr', 'body', '12')['body'])
        meta = self.json('pr', 'metadata', '12')
        self.assertEqual(sorted(f['path'] for f in meta['files']), ['src/cart.py', 'tests/test_cart.py'])
        meta_file = self.ws / '.git' / 'meta.json'
        meta_file.write_text(json.dumps(meta))
        self.assertIn('def average_price', self.forge('pr', 'diff', '12', '--meta', str(meta_file)).stdout)
        self.assertEqual(self.json('pr', 'checks', '12'), [])
        self.assertEqual(self.json('pr', 'threads', '12'), [])
        self.assertEqual(self.json('pr', 'links', '12'), {'closes': [7]})
        self.assertEqual(self.json('pr', 'approval', '12')['closes'], [7])
        self.assertEqual([r['number'] for r in self.json('pr', 'refs', '--state', 'open', '--limit', '100')], [12])
        self.assertEqual(self.json('pr', 'list', '--state', 'open', '--limit', '10')[0]['headRefName'],
                         'issue-7-cart-discount')

    def test_prior_report_round_trip(self):
        # run.md -> Reviewer step 3b: find the report by marker, then edit it in place.
        pages = self.json('pr', 'comments', '12')
        report = [c for page in pages for c in page if '<!-- super-review:report -->' in c['body']][-1]
        self.assertIn('R2 [builder]', report['body'])
        self.forge('pr', 'comment-edit', '12', report['id'], '--body-file',
                   self.body('<!-- super-review:report -->\nRound: 2'))
        again = [c for page in self.json('pr', 'comments', '12') for c in page]
        self.assertTrue(any(c['id'] == report['id'] for c in again))
        new_id = self.forge('pr', 'comment', '12', '--body-file', self.body('[reviewer] [report] bounced')).stdout.strip()
        self.assertTrue(new_id.isdigit())
        self.assertIn('[reviewer] [report] bounced', [c['body'] for page in self.json('pr', 'comments', '12') for c in page])
        self.assertIn('[reviewer] [report] bounced', self.log('.git/gh-comments.md'))

    def test_notes_ready_create(self):
        head = self.json('pr', 'view', '12')['headRefOid']
        self.assertTrue(self.forge('pr', 'note', '12', '--file', 'src/cart.py', '--line', '7', '--head', head,
                                   '--body-file', self.body('Purpose: clamp')).stdout.strip())
        notes = self.ws / '.git' / 'notes.json'
        notes.write_text(json.dumps([{'file': 'src/cart.py', 'line': 2, 'body': 'guard'}, {'file': 'tests/test_cart.py', 'body': 'tests'}]))
        self.assertEqual([n['index'] for n in self.json('pr', 'notes', '12', '--notes-file', str(notes), '--head', head)], [0, 1])
        self.assertEqual(self.forge('pr', 'ready', '12').stdout.strip(), 'ready #12')
        out = self.forge('pr', 'create', '--draft', '--base', 'main', '--head', 'issue-7-cart-discount', '--title',
                         '🐛 [fix] cart: clamp', '--body-file', self.body('body'), '--closes', '7').stdout.split()
        self.assertTrue(out[0].startswith('https://github.com/example/shop/pull/') and out[-1].isdigit())
        self.assertIn('Closes #7', self.log('.git/gh-prs.md'))
        self.assertEqual(self.forge('pr', 'thread-resolve', '12', 'PRRT_eval0').stdout.strip(), 'resolved PRRT_eval0')

    def test_ticket_verbs_and_board(self):
        ticket = self.json('ticket', 'view', '7')
        self.assertEqual((ticket['number'], ticket['state']), (7, 'OPEN'))
        self.assertEqual(self.json('ticket', 'comments', '7'), [[]])
        self.assertTrue(self.forge('ticket', 'comment', '7', '--body-file', self.body('[reviewer] [report] ❌')).stdout.strip())
        self.forge('ticket', 'edit', '7', '--add-label', 'loop:in-review', '--remove-label', 'loop:in-qa')
        self.assertEqual(self.forge('ticket', 'claim', '7').stdout.strip(), 'claimed #7')
        self.assertEqual(self.json('ticket', 'view', '7')['assignees'], [])  # issue view is the fixture, not the state
        self.assertEqual(self.forge('ticket', 'release', '7').stdout.strip(), 'released #7 (super-board-bot)')
        self.assertEqual(self.forge('board', 'status', '7').stdout.strip(), 'Review')
        self.assertEqual(self.forge('board', 'move', '7', 'Ready').stdout.strip(), 'moved #7 → Ready')
        self.assertIn('opt_ready', self.log('.git/gh-calls.log'))
        self.assertEqual(self.json('board', 'items'), {'items': [], 'totalCount': 0})
        self.assertEqual(self.forge('ticket', 'close', '7', '--reason', 'completed').stdout.strip(), 'closed #7 (completed)')

    def test_merge_is_logged_never_sent(self):
        head = self.json('pr', 'view', '12')['headRefOid']
        self.forge('pr', 'merge', '12', '--squash', '--delete-branch', '--match-head', head)
        self.assertIn('pr merge 12', self.log('.gh-log'))


class CollectDuplicate(Case):
    """super-collect: the board snapshot, open tickets, merged PRs and the fingerprint dedupe."""
    case = 'collect-duplicate'
    config = '.claude/super-board/configs/shop.json'

    def test_board_and_tickets(self):
        board = self.json('board', 'items')
        self.assertEqual(board['totalCount'], 2)
        self.assertEqual({(i['content']['number'], i['status']) for i in board['items']}, {(301, 'Backlog'), (290, 'Backlog')})
        self.assertEqual([t['number'] for t in self.json('ticket', 'list', '--state', 'open', '--limit', '300')], [301, 290])
        self.assertEqual([t['number'] for t in self.json('ticket', 'list', '--label', 'bug')], [301])
        history = self.json('ticket', 'history')
        self.assertTrue(any('err|sentry|4411' in t['body'].split() for t in history))
        self.assertEqual(self.json('pr', 'list', '--state', 'merged', '--limit', '100'), [])
        graph = self.json('ticket', 'graph')
        self.assertEqual(sorted(t['number'] for t in graph), [290, 301])

    def test_seen_again_comment_is_graded(self):
        self.forge('ticket', 'comment', '301', '--body-file', self.body('Seen again: 22 events'))
        self.assertIn('Seen again', self.log('.git/gh-comments.md'))
        calls = self.log('.git/gh-calls.log')
        self.assertRegex(calls, r'issue comment 301|-X POST repos/[^ ]+/issues/301/comments')
        self.assertNotRegex(calls, r'issue create|-X POST repos/[^ ]+/issues --input')


if __name__ == '__main__':
    if not shutil.which('jq'):
        print('SKIP: test_evals_forge_stub.py needs jq')
        sys.exit(0)
    unittest.main()
