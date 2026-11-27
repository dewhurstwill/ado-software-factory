#!/usr/bin/env python3
"""End to end: the merge gate, the PR-body writer and preflight on an `azure-devops` config.

The scripts run unchanged against the fake Azure DevOps (loopback) and a real local bare origin.
A `gh` shim that fails and logs proves no GitHub call is made."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado_pr import PrCase, put_file, sample_body, pr_mod  # noqa: E402

GATE = ROOT / 'scripts/super-board-merge-gate.sh'
PRBODY = ROOT / 'scripts/super-board-pr-body.sh'
PREFLIGHT = ROOT / 'scripts/super-board-preflight.sh'
AC = '## Acceptance Criteria\n- [ ] it works\n'


class ScriptCase(PrCase):
    def setUp(self):
        super().setUp()
        gh = self.bin / 'gh'
        gh.write_bytes(b'#!/bin/sh\necho "$@" >> "$GH_SHIM_LOG"\nexit 1\n')
        gh.chmod(0o755)
        self.gh_log = self.root / 'gh.log'
        self.env.update(GH_SHIM_LOG=str(self.gh_log), PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        SB_ADO_MERGE_POLL_INTERVAL='0.01', GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')

    def run_script(self, script, *args, env=None, stdin=None):
        e = dict(self.env, **(env or {}))
        e = {k: v for k, v in e.items() if v is not None}
        return subprocess.run(['bash', str(script), *args], env=e, text=True, capture_output=True, cwd=self.root,
                              stdin=subprocess.DEVNULL, timeout=120)

    def no_gh_calls(self):
        self.assertFalse(self.gh_log.exists() and self.gh_log.read_text(encoding='utf-8').strip(), 'gh must never be called')


class GateTests(ScriptCase):
    def setUp(self):
        super().setUp()
        self.init_repo()
        put_file(self.work / 'a.txt', 'base\n')
        self.commit('base')
        self.git('push', '-q', 'origin', 'main')
        self.git('checkout', '-q', '-b', 'feat')
        put_file(self.work / 'a.txt', 'base\nchange\n')
        self.head = self.commit('feat')
        self.git('push', '-q', 'origin', 'feat')
        self.git('checkout', '-q', 'main')
        self.git('branch', '-q', '-D', 'feat')
        self.n = self.pulls.add('feat', 'main', title='the title', head=self.head)
        self.config_value.update(base_branch='main', verify_commands=['git diff --check'])

    def gate(self, *args, verify=None):
        if verify is not None:
            self.config_value['verify_commands'] = verify
        return self.run_script(GATE, '--config', self.cfg(), '--pr', str(self.n), '--lock-timeout', '6', *args)

    def lock(self):
        return self.work / '.claude/super-board/inflight/merge.lock'

    def test_merges_with_the_pinned_head_and_the_squash_message(self):
        msg = self.root / 'msg.md'
        put_file(msg, '- did it\nCloses #1\n')
        r = self.gate('--expect-head', self.head, '--subject', 'S: subject', '--body-file', str(msg))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f'merged {self.head}', r.stderr)
        completion = self.requests('PATCH', r'/pullrequests/1$')[-1]['body']
        self.assertEqual(completion['lastMergeSourceCommit'], {'commitId': self.head})
        self.assertEqual(self.pulls.merged[self.n]['message'], 'S: subject\n\n- did it\nCloses #1')
        self.assertEqual(self.pulls.prs[self.n]['status'], 'completed')
        self.assertFalse(self.lock().exists(), 'the lock is released')
        self.no_gh_calls()
        self.assertNotIn('GitHub', r.stderr)

    def test_head_moved_after_review_exits_6(self):
        r = self.gate('--expect-head', 'deadbeef')
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertEqual(self.requests('PATCH', r'.'), [])

    def test_head_moved_during_the_gate_exits_6(self):
        # a push lands after the gate read the head: the service refuses the pinned completion (TF401192)
        self.pulls.before_complete = lambda store, pr: store.push(self.n, 'f' * 40)
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertIn('head moved', r.stderr)
        self.assertEqual(self.pulls.merged, {})
        self.assertEqual(len(self.requests('PATCH', r'/pullrequests/1$')), 1)

    def test_abandoned_exits_3(self):
        self.pulls.prs[self.n]['status'] = 'abandoned'
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.requests('PATCH', r'.'), [])

    def test_already_completed_is_a_no_op(self):
        self.pulls.prs[self.n].update(status='completed', lastMergeCommit={'commitId': '9' * 40})
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('already merged', r.stderr)
        self.assertEqual(self.requests('PATCH', r'.'), [])

    def test_failing_verification_exits_2(self):
        r = self.gate('--expect-head', self.head, verify=['false'])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.requests('PATCH', r'.'), [])
        self.assertFalse(self.lock().exists())

    def test_dry_run_merges_nothing(self):
        r = self.gate('--expect-head', self.head, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('dry run: would squash-merge', r.stderr)
        self.assertEqual(self.requests('PATCH', r'.'), [])
        self.assertEqual(self.pulls.prs[self.n]['status'], 'active')

    def test_payments_label_forces_a_human_gate(self):
        self.pulls.prs[self.n]['labels'] = [{'id': 'x', 'name': 'payments', 'active': True}]
        self.config_value['merge_policy'] = {'always_human': {'money': {'labels': ['payments']}}}
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertIn('human-gate: money', r.stdout)
        self.assertEqual(self.requests('PATCH', r'.'), [])

    def test_a_pr_touching_the_board_config_folder_always_waits_for_a_human(self):
        # board-config is on by default and survives a custom always_human: that file holds the merge
        # policy and the approver list, so an agent's PR must not be able to change either.
        self.git('checkout', '-q', '-b', 'cfg', 'main')
        put_file(self.work / '.claude/super-board/configs/app.json', '{"merge_policy":{"default":"auto"}}\n')
        head = self.commit('cfg')
        self.git('push', '-q', 'origin', 'cfg')
        self.git('checkout', '-q', 'main')
        n = self.pulls.add('cfg', 'main', title='config', head=head)
        for policy in (None, {'always_human': {}}):
            if policy is not None:
                self.config_value['merge_policy'] = policy
            r = self.run_script(GATE, '--config', self.cfg(), '--pr', str(n), '--lock-timeout', '6', '--expect-head', head)
            self.assertEqual(r.returncode, 7, (policy, r.stderr))
            self.assertIn('human-gate: board-config — path .claude/super-board/configs/app.json', r.stdout)
            self.assertEqual(self.pulls.prs[n]['status'], 'active')
        self.no_gh_calls()

    def test_branch_policy_refusal_exits_7_without_a_halt(self):
        self.pulls.merge_outcome = 'rejectedByPolicy'
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertIn('human-gate: policy — Azure DevOps branch policy blocks completion: forced rejectedByPolicy', r.stdout)
        self.assertFalse(self.halt.exists(), 'a definite refusal is not an unconfirmed merge')
        self.assertEqual(self.pulls.prs[self.n]['status'], 'active')
        self.assertFalse(self.lock().exists())
        self.no_gh_calls()

    def test_unmet_reviewer_policy_is_a_403_on_completion_and_exits_7(self):
        # Seen live: required reviewers are enforced on the completion request itself.
        self.pulls.unmet_policy = ('The pull request needs a minimum number of approvals (1) from other users '
                                   'before it can be completed.')
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertIn('human-gate: policy — Azure DevOps will not let this identity complete the pull request: '
                      'The pull request needs a minimum number of approvals (1)', r.stdout)
        self.assertFalse(self.halt.exists(), 'a refused completion merged nothing; it must not stop the run')
        self.assertEqual(self.pulls.prs[self.n]['status'], 'active')
        self.assertFalse(self.lock().exists())
        self.no_gh_calls()

    def test_conflicts_exit_5_and_other_refusals_exit_1_without_a_halt(self):
        self.pulls.merge_outcome = 'conflicts'
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertFalse(self.halt.exists())
        self.pulls.merge_outcome = 'failure'
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertFalse(self.halt.exists())

    def test_draft_refusal_exits_1_without_a_halt(self):
        self.pulls.prs[self.n]['isDraft'] = True
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('pr ready', r.stderr)
        self.assertFalse(self.halt.exists())
        self.assertEqual(self.pulls.prs[self.n]['status'], 'active')

    def test_unconfirmed_completion_halts_for_reconciliation(self):
        self.pulls.never_complete = True
        r = self.run_script(GATE, '--config', self.cfg(), '--pr', str(self.n), '--lock-timeout', '6',
                            '--expect-head', self.head, env={'SB_ADO_MERGE_POLL_TIMEOUT': '0.3'})
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertTrue(self.halt.exists())
        self.assertIn('Azure DevOps has not confirmed a merge', self.halt.read_text(encoding='utf-8'))
        self.assertEqual(len(self.requests('PATCH', r'/pullrequests/1$')), 1, 'never retried')

    def test_human_policy_holds_and_prints_a_pinned_request(self):
        # Approval is read through the forge (tests/test_approval_ado.py); with no request yet the gate holds.
        self.config_value['merge_policy'] = {'default': 'human'}
        r = self.gate('--expect-head', self.head)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertIn('human-gate:', r.stdout)
        request = json.loads(next(l for l in r.stdout.splitlines() if l.startswith('approval-request: '))[18:])
        self.assertEqual((request['repo'], request['pr'], request['head']), ('example/factory/factory', self.n, self.head))
        self.assertEqual(self.requests('PATCH', r'.'), [])
        self.assertFalse(self.halt.exists())
        self.no_gh_calls()


class PrBodyTests(ScriptCase):
    def setUp(self):
        super().setUp()
        self.head = 'a' * 40
        self.n = self.pulls.add('feat', head=self.head)

    def rewrite(self, block_name, text, *extra, head=None):
        return self.run_script(PRBODY, '--config', self.cfg(), '--pr', str(self.n), '--block', block_name,
                               '--expect-head', head or self.head, '--body-file', self.put(text), *extra)

    def test_rewrites_one_block_and_leaves_the_rest(self):
        body = sample_body(3)
        self.pulls.prs[self.n]['description'] = body
        r = self.rewrite('status', '> [!NOTE]\n> merged soon')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'updated'), r.stderr)
        new = self.pulls.prs[self.n]['description']
        self.assertIn('> merged soon', new)
        self.assertEqual(new.rstrip('\n'), body.replace('> in review', '> merged soon').rstrip('\n'))  # the writer ends a body with one newline
        again = self.rewrite('status', '> [!NOTE]\n> merged soon')
        self.assertEqual(again.stdout.strip(), 'unchanged')
        self.no_gh_calls()

    def test_head_moved_exits_6(self):
        self.pulls.prs[self.n]['description'] = sample_body(3)
        r = self.rewrite('status', 'x', head='b' * 40)
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertEqual(self.requests('PATCH', r'.'), [])

    def test_overflowing_history_survives_a_block_rewrite(self):
        body = sample_body(100)
        n = self.n
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(body))
        self.assertEqual(len(self.threads_of(n)), 1)
        r = self.rewrite('status', '> [!NOTE]\n> merged soon')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'updated'), r.stderr)
        logical = self.js('pr', 'body', str(n))['body']
        self.assertEqual(logical.rstrip('\n'), body.replace('> in review', '> merged soon').rstrip('\n'))
        self.assertLessEqual(pr_mod.units(self.pulls.prs[n]['description']), 4000)
        self.assertEqual(len(self.threads_of(n)), 1, 'one overflow thread, edited in place')
        # appending a history row keeps the fold and the thread
        row = self.root / 'row.md'
        put_file(row, '| builder | done | 11:11 | new row |\n')
        r = self.run_script(PRBODY, '--config', self.cfg(), '--pr', str(n), '--block', 'history',
                            '--expect-head', self.head, '--append-file', str(row))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('new row', self.js('pr', 'body', str(n))['body'])
        self.assertEqual(len(self.threads_of(n)), 1)
        self.no_gh_calls()

    def test_a_body_that_cannot_fit_exits_65_and_writes_nothing(self):
        self.pulls.prs[self.n]['description'] = sample_body(3)
        r = self.rewrite('problem', 'p' * 4500)
        self.assertEqual(r.returncode, 65, r.stderr)
        self.assertIn('4000', r.stderr)
        self.assertFalse(self.halt.exists(), 'refused before any write: nothing to reconcile')
        self.assertEqual(self.requests('PATCH', r'.'), [])


class PreflightTests(ScriptCase):
    def setUp(self):
        super().setUp()
        self.fake.items[1]['fields']['System.Description'] = 'Do the thing.\n\n' + AC
        self.fake.items[1]['fields']['System.Title'] = 'Export invoices as CSV'
        self.config_file = self.cfg()
        self.env['SB_CONFIG'] = self.config_file

    def preflight(self, *args):
        return self.run_script(PREFLIGHT, '--repo', 'ignored/ignored', *args)

    def verdict(self, *args):
        r = self.preflight('--issues', '1', *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.no_gh_calls()
        return json.loads(r.stdout)['1']

    def test_proceeds_when_nothing_overlaps(self):
        v = self.verdict()
        self.assertEqual((v['verdict'], v['tag']), ('proceed', 'clean'), v)

    def test_a_merged_pr_that_closes_it_holds(self):
        n = self.pulls.add('issue-1-x', title='CSV export', description='## Problem\nCloses #1')
        self.pulls.prs[n]['status'] = 'completed'
        v = self.verdict()
        self.assertEqual((v['verdict'], v['tag']), ('hold', 'done'), v)
        self.assertIn(f'PR #{n} merged and closes #1', v['evidence'])

    def test_an_open_pr_on_the_same_files_sequences(self):
        n = self.pulls.add('issue-9-other', title='Unrelated banner', description='Closes #9')
        self.pulls.changes[n] = [('edit', '/src/export.ts')]
        v = self.verdict('--files', 'src/export.ts')
        self.assertEqual((v['verdict'], v['tag']), ('sequence', 'overlap'), v)
        self.assertEqual(v['conflictWith'], [n])
        self.assertEqual(v['blockedBy'], [9])

    def test_an_unclear_card_holds_and_closed_tickets_are_read(self):
        self.fake.items[1]['fields']['System.Description'] = 'No criteria.'
        v = self.verdict()
        self.assertEqual((v['verdict'], v['tag']), ('hold', 'unclear'), v)
        calls = [r['path'] for r in self.fake.requests]
        self.assertTrue(any('/wit/wiql' in c for c in calls), 'ticket list --state closed ran')
        self.assertTrue(any('/pullrequests' in c for c in calls))


if __name__ == '__main__':
    unittest.main()
