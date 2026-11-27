#!/usr/bin/env python3
"""super-collect's `prs` source on an `azure-devops` config: merged PRs in the window with their
conversation comments and review threads (file path, resolved state), read through the forge's
`pr merged` and `pr threads` verbs. Also pins those two verbs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase  # noqa: E402

COLLECT_PRS = ROOT / 'skills/super-collect/scripts/collect_prs.py'
MARK = '<!-- super-review:report -->'
BOT = {'displayName': 'Project Collection Build Service (example)', 'uniqueName': 'svc.build@example.com', 'id': 'b'}
FILE = {'filePath': '/src/orders/intake.ts'}


class CollectCase(AdoCase):
    def setUp(self):
        super().setUp()
        self.pulls = self.fake.pulls
        self.env.update(SUPER_BOARD_BIN=str(ROOT / 'scripts'))

    def merged(self, branch, closed, title='t', by=None):
        n = self.pulls.add(branch, 'main', title=title)
        self.pulls.prs[n].update(status='completed', closedDate=closed, lastMergeCommit={'commitId': 'c' * 40})
        if by:
            self.pulls.prs[n]['createdBy'] = by
        return n

    def run_collect(self, *args):
        e = {k: v for k, v in self.env.items() if v is not None}
        return subprocess.run([sys.executable, str(COLLECT_PRS), '--config', self.cfg(), 'list', *args], env=e,
                              text=True, capture_output=True, cwd=self.root, stdin=subprocess.DEVNULL, timeout=120)


class VerbTests(CollectCase):
    def test_merged_window_and_shape(self):
        a = self.merged('feat-new', '2026-11-26T10:00:00.1234567Z', 'new', BOT)
        self.merged('feat-old', '2026-10-01T10:00:00Z', 'old')
        self.pulls.add('feat-open', 'main')
        out = self.js('pr', 'merged', '--since', '2026-11-01T00:00:00+00:00', '--limit', '50')
        self.assertEqual(out, [{'number': a, 'title': 'new', 'url': f'https://dev.azure.com/example/Factory/_git/Factory/pullrequest/{a}',
                                'mergedAt': '2026-11-26T10:00:00.1234567Z',
                                'author': {'login': 'svc.build@example.com', 'type': 'Bot'}}])

    def test_bad_since_is_a_usage_error(self):
        self.assertEqual(self.forge('pr', 'merged', '--since', 'yesterday', '--limit', '5').returncode, 64)

    def test_threads_carry_path_and_resolved_and_skip_system_and_overflow(self):
        n = self.merged('feat-a', '2026-11-26T10:00:00Z')
        self.pulls.add_thread(n, [{'content': 'general note'}])
        self.pulls.add_thread(n, [{'content': 'rename this'}, {'content': 'done', 'author': BOT}], status='fixed', context=FILE)
        self.pulls.add_thread(n, [{'content': 'nit'}], status='wontFix', context=FILE)
        self.pulls.add_thread(n, [{'content': 'still open'}], status='active', context=FILE)
        self.pulls.add_thread(n, [{'content': 'system said', 'commentType': 'system'}])
        self.pulls.add_thread(n, [{'content': '<!-- sb:overflow -->\nmoved'}])
        self.pulls.add_thread(n, [{'content': 'gone', 'isDeleted': True}])
        out = self.js('pr', 'threads', str(n))
        self.assertEqual([(t['path'], t['status'], t['resolved']) for t in out],
                         [(None, 'active', False), ('src/orders/intake.ts', 'fixed', True),
                          ('src/orders/intake.ts', 'wontFix', True), ('src/orders/intake.ts', 'active', False)])
        self.assertEqual(out[1]['comments'][1]['author'], {'login': 'svc.build@example.com', 'type': 'Bot'})
        self.assertEqual(out[1]['comments'][0]['author'], {'login': 'user@example.com', 'type': 'User'})

    def test_github_backend_leaves_them_unimplemented(self):
        cfg = self.root / 'gh.json'
        cfg.write_text('{"provider":"github"}')
        for args in (['pr', 'merged', '--since', '2026-01-01T00:00:00Z', '--limit', '1'], ['pr', 'threads', '1']):
            r = subprocess.run([sys.executable, str(ROOT / 'scripts/super-board-forge.py'), '--config', str(cfg), *args],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 69, args)


class CollectorTests(CollectCase):
    def test_lists_merged_prs_with_comments_threads_and_super_review_reports(self):
        n = self.merged('feat-a', '2026-11-26T10:00:00Z', 'Add intake', BOT)
        self.merged('feat-old', '2026-09-01T10:00:00Z', 'ancient')
        self.pulls.add_thread(n, [{'content': MARK + '\nSuper Review report'}])
        self.pulls.add_thread(n, [{'content': 'please rename'}], status='fixed', context=FILE)
        r = self.run_collect('--since', '2026-11-01')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out['source'], out['repo'], out['truncated'], out['superReviewReports']),
                         ('prs', 'example/Factory/Factory', False, 1))
        (pr,) = out['prs']
        self.assertEqual((pr['number'], pr['title'], pr['mergedAt'], pr['author']),
                         (n, 'Add intake', '2026-11-26T10:00:00Z', {'login': 'svc.build@example.com', 'bot': True}))
        self.assertEqual(pr['reviews'], [])
        self.assertEqual([c['superReview'] for c in pr['comments']], [True])
        self.assertEqual(pr['threads'], [{'path': 'src/orders/intake.ts', 'resolved': True,
                                          'comments': [{'author': {'login': 'user@example.com', 'bot': False},
                                                        'body': 'please rename', 'superReview': False}]}])

    def test_the_forge_failing_is_unavailable_exit_2(self):
        self.fake.inject.append({'match': lambda req: '/pullrequests' in req['path'], 'times': 99, 'status': 500, 'body': {}})
        r = self.run_collect('--since', '2026-11-01')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout)['status'], 'unavailable')

    def test_gh_is_never_run(self):
        gh = self.bin / 'gh'
        gh.write_text('#!/bin/sh\necho "$@" >> "$GH_SHIM_LOG"\nexit 1\n')
        gh.chmod(0o755)
        log = self.root / 'gh.log'
        self.env.update(GH_SHIM_LOG=str(log))
        self.merged('feat-a', '2026-11-26T10:00:00Z')
        self.assertEqual(self.run_collect('--since', '2026-11-01').returncode, 0)
        self.assertFalse(log.exists())


if __name__ == '__main__':
    unittest.main()
