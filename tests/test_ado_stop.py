#!/usr/bin/env python3
"""super-board-stop.sh on an `azure-devops` config: it finds the pull request of an issue branch,
comments on the ticket and the PR, releases the claim and removes the loop:* labels, all through
the forge. A `gh` shim that fails and logs proves `gh` is never run."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase  # noqa: E402

STOP = ROOT / 'scripts/super-board-stop.sh'


class StopCase(AdoCase):
    def setUp(self):
        super().setUp()
        for name, body in (('gh', 'echo "$@" >> "$GH_SHIM_LOG"\nexit 1\n'), ('pgrep', 'exit 1\n')):
            shim = self.bin / name
            shim.write_text('#!/bin/sh\n' + body)
            shim.chmod(0o755)
        self.gh_log = self.root / 'gh.log'
        self.proj = self.root / 'proj'
        (self.proj / '.claude/super-board/configs').mkdir(parents=True)
        (self.proj / '.claude/super-board/inflight').mkdir()
        self.config_value['repo'] = {'path': str(self.proj)}
        (self.proj / '.claude/super-board/configs/test.json').write_text(json.dumps(self.config_value))
        self.env.update(GH_SHIM_LOG=str(self.gh_log), SB_CLAIM_HOLDER='abc123', SB_REPO_PATH=str(self.proj))

    def worker(self, tags, lane='build', pr=True):
        n = self.fake.add('User Story', 'card', 'body', tags, 'Building')
        (self.proj / f'.claude/super-board/inflight/{n}').write_text(f'PID=999999999\nLANE={lane}\nSTARTED=now\n')
        number = self.fake.pulls.add(f'issue-{n}-card', 'main') if pr else None
        return n, number

    def stop(self):
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run(['bash', str(STOP), 'test'], env=e, text=True, capture_output=True, cwd=self.proj,
                           stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def comments(self, n):
        return [c['body'] for c in json.loads(self.forge('ticket', 'comments', str(n)).stdout)[0]]

    def pr_comments(self, number):
        return [c['body'] for c in json.loads(self.forge('pr', 'comments', str(number)).stdout)[0]]

    def no_gh(self):
        self.assertFalse(self.gh_log.exists() and self.gh_log.read_text().strip(), 'gh must never be called')


class StopTests(StopCase):
    def test_comments_on_ticket_and_pr_and_releases_our_claim_and_labels(self):
        n, pr = self.worker('feature; loop:in-build; loop:in-qa; sb:claimed-abc123')
        self.stop()
        (ticket_comment,) = self.comments(n)
        self.assertIn('[orchestrator] [report] 🛑 stopped · build lane', ticket_comment)
        self.assertEqual(self.pr_comments(pr), [ticket_comment])
        self.assertEqual(self.fake.tags(n), ['feature'])
        self.assertFalse((self.proj / f'.claude/super-board/inflight/{n}').exists())
        self.no_gh()

    def test_a_claim_that_is_not_ours_is_swept_with_force(self):
        n, _ = self.worker('feature; sb:claimed-zzz999')
        r = self.stop()
        self.assertEqual(self.fake.tags(n), ['feature'])
        self.assertIn('swept a claim that was not ours', r.stdout)
        self.no_gh()

    def test_no_pull_request_still_comments_on_the_ticket(self):
        n, _ = self.worker('feature; sb:claimed-abc123', pr=False)
        self.stop()
        self.assertEqual(len(self.comments(n)), 1)
        self.assertEqual(self.fake.tags(n), ['feature'])
        self.no_gh()

    def test_the_pr_is_found_by_the_issue_branch_only(self):
        n, pr = self.worker('feature; sb:claimed-abc123')
        other = self.fake.pulls.add('issue-9999-other', 'main')
        self.stop()
        self.assertEqual(len(self.pr_comments(pr)), 1)
        self.assertEqual(self.pr_comments(other), [])

    def test_a_failed_comment_does_not_stop_the_wrap_up(self):
        n, _ = self.worker('feature; sb:claimed-abc123', pr=False)
        self.fake.inject.append({'match': lambda r: r['method'] == 'POST' and '/comments' in r['path'], 'times': 1,
                                 'status': 500, 'body': {'message': 'boom'}})
        r = self.stop()
        self.assertIn('failed to comment', r.stdout)
        self.assertEqual(self.fake.tags(n), ['feature'])


class LegacyDispatcherTests(StopCase):
    def test_the_claude_p_dispatcher_refuses_a_non_github_config_before_any_call(self):
        cfg = dict(self.config_value, worker_backend='claude-p')
        (self.proj / '.claude/super-board/configs/test.json').write_text(json.dumps(cfg))
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run(['bash', str(ROOT / 'scripts/super-board-run.sh'), 'test'], env=e, text=True,
                           capture_output=True, cwd=self.proj, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertIn('supports GitHub only', r.stderr)
        self.no_gh()
        self.assertEqual(self.fake.requests, [])


if __name__ == '__main__':
    unittest.main()
