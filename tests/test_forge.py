#!/usr/bin/env python3
"""Offline contract for super-board-forge.py: provider and repo resolution, the GitHub
verbs' exact `gh` argv and output pass-through, writes never retried, halt (79)."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE = ROOT / 'scripts/super-board-forge.py'

HEAD = 'a' * 40
META = {'labels': [{'name': 'bug'}], 'files': [{'path': 'f'}], 'additions': 1, 'deletions': 0,
        'changedFiles': 1, 'body': 'b'}
DIFF = 'diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+x\n'
STATE = {'state': 'OPEN', 'headRefOid': HEAD, 'mergeCommit': None}
PR = {'number': 1, 'title': 't', 'body': 'b', 'url': 'u', 'headRefName': 'h', 'files': [{'path': 'f'}]}
PAGES = [{'data': {'repository': {'issues': {'pageInfo': {'hasNextPage': True, 'endCursor': 'c'}, 'nodes': [
    {'number': 1, 'title': 't', 'body': '', 'state': 'OPEN', 'labels': {'nodes': []}, 'comments': {'nodes': []}}]}}}},
    {'data': {'repository': {'issues': {'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': [
        {'number': 2, 'title': 'u', 'body': '', 'state': 'OPEN', 'labels': {'nodes': []}, 'comments': {'nodes': []}}]}}}}]


class ForgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / 'calls.jsonl'
        self.halt = self.root / 'halt.json'
        gh = self.root / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json, os, sys
stdin = '' if sys.stdin.isatty() else sys.stdin.read()
with open(os.environ['FORGE_LOG'], 'a') as f:
    f.write(json.dumps({'argv': sys.argv[1:], 'stdin': stdin, 'gh_repo': os.environ.get('GH_REPO')}) + '\\n')
sys.stdout.write(os.environ.get('FORGE_OUT', ''))
print(os.environ.get('FORGE_ERR', ''), file=sys.stderr)
sys.exit(int(os.environ.get('FORGE_RC', '0')))
''')
        gh.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if k not in ('SB_CONFIG', 'GH_REPO', 'SB_REPO_PATH')}
        self.env.update(PATH=f'{self.root}:{os.environ["PATH"]}', FORGE_LOG=str(self.log),
                        SB_GITHUB_HALT_FILE=str(self.halt), SB_GITHUB_RETRY_DELAY='0')
        self.config = self.root / 'cfg.json'

    def tearDown(self):
        self.temp.cleanup()

    def cfg(self, **value):
        self.config.write_text(json.dumps(value))
        return str(self.config)

    def run_forge(self, *args, out='', rc=0, err='', cfg=None, env=None):
        e = dict(self.env, FORGE_OUT=out if isinstance(out, str) else json.dumps(out), FORGE_RC=str(rc), FORGE_ERR=err)
        e.update(env or {})
        flags = ['--config', cfg] if cfg else []
        return subprocess.run(['python3', str(FORGE), *flags, *args], env=e, text=True, stdin=subprocess.DEVNULL,
                              capture_output=True, cwd=self.root)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    # ---- provider ---------------------------------------------------------
    def test_provider_defaults_to_github(self):
        self.assertEqual(self.run_forge('provider').stdout, 'github\n')
        self.assertEqual(self.run_forge('provider', cfg=self.cfg(project={})).stdout, 'github\n')
        self.assertEqual(self.run_forge('provider', cfg=self.cfg(provider='github')).stdout, 'github\n')

    def test_provider_from_sb_config_env(self):
        r = self.run_forge('provider', env={'SB_CONFIG': self.cfg(provider='azure-devops')})
        self.assertEqual(r.stdout, 'azure-devops\n')

    def test_config_may_be_a_fifo_read_once(self):
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        writer = threading.Thread(target=lambda: fifo.write_text('{"provider":"azure-devops"}'))
        writer.start()
        r = self.run_forge('provider', cfg=str(fifo))
        writer.join()
        self.assertEqual(r.stdout, 'azure-devops\n')

    def test_github_board_ensure_is_setup_not_a_forge_verb(self):
        # Every other verb is implemented on both providers (tests/test_forge_contract.py runs one scenario
        # list against both). The one that stays provider-specific points at the setup script and never runs gh.
        r = self.run_forge('board', 'ensure', cfg=self.cfg(provider='github'))
        self.assertEqual(r.returncode, 69, r.stderr)
        self.assertIn('super-board-setup.py', r.stderr)
        self.assertEqual(self.calls(), [], 'no gh call for board ensure')

    def test_unknown_provider_exits_66(self):
        r = self.run_forge('provider', cfg=self.cfg(provider='gitlab'))
        self.assertEqual(r.returncode, 66)
        self.assertIn('gitlab', r.stderr)
        self.assertEqual(self.run_forge('provider', cfg=str(self.root / 'missing.json')).returncode, 66)

    def test_unknown_verb_and_usage_exit_64(self):
        self.assertEqual(self.run_forge('pr', 'nope').returncode, 64)
        self.assertEqual(self.run_forge('pr', 'head', 'x').returncode, 64)
        self.assertEqual(self.run_forge().returncode, 64)

    # ---- repo -------------------------------------------------------------
    def test_repo_from_flag_config_and_origin(self):
        r = self.run_forge('--repo', 'https://github.com/o/n.git', 'repo', cfg=self.cfg(repo={'remote': 'a/b'}))
        self.assertEqual(r.stdout, 'o/n\n', 'the flag wins')
        for remote in ('https://github.com/o/n.git', 'http://github.com/o/n', 'git@github.com:o/n.git', 'o/n'):
            self.assertEqual(self.run_forge('repo', cfg=self.cfg(repo={'remote': remote})).stdout, 'o/n\n', remote)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'remote', 'add', 'origin', 'git@github.com:x/y.git'], check=True)
        self.assertEqual(self.run_forge('repo').stdout, 'x/y\n')
        explicit = self.run_forge('repo', '--explicit')
        self.assertEqual((explicit.returncode, explicit.stdout), (0, '\n'), 'no origin fallback when explicit')

    # ---- reads: argv and pass-through ---------------------------------------
    def test_pr_head(self):
        r = self.run_forge('--repo', 'o/n', 'pr', 'head', '7', out=f'feat {HEAD}\n')
        self.assertEqual(r.stdout, f'feat {HEAD}\n')
        self.assertEqual(self.calls()[0]['argv'], ['pr', 'view', '7', '--repo', 'o/n', '--json',
                         'headRefName,headRefOid', '-q', '.headRefName + " " + .headRefOid'])

    def test_no_repo_means_no_repo_flag(self):
        self.run_forge('pr', 'head', '7', out=f'feat {HEAD}')
        self.assertNotIn('--repo', self.calls()[0]['argv'])

    def test_pr_state_metadata_diff(self):
        cfg = self.cfg(repo={'remote': 'https://github.com/o/n.git'})
        r = self.run_forge('pr', 'state', '7', out=STATE, cfg=cfg)
        self.assertEqual(json.loads(r.stdout), STATE)
        r = self.run_forge('pr', 'metadata', '7', out=META, cfg=cfg)
        self.assertEqual(json.loads(r.stdout), META)
        meta = self.root / 'meta.json'
        meta.write_text(json.dumps(META))
        r = self.run_forge('pr', 'diff', '7', '--meta', str(meta), out=DIFF, cfg=cfg)
        self.assertEqual(r.stdout, DIFF)
        self.assertEqual([c['argv'] for c in self.calls()], [
            ['pr', 'view', '7', '--repo', 'o/n', '--json', 'state,mergeCommit,headRefOid'],
            ['pr', 'view', '7', '--repo', 'o/n', '--json', 'labels,files,additions,deletions,changedFiles,body'],
            ['pr', 'diff', '7', '--repo', 'o/n']])

    def test_diff_without_meta_is_a_malformed_read(self):
        r = self.run_forge('pr', 'diff', '7', out=DIFF)
        self.assertEqual(r.returncode, 64)
        self.assertEqual(self.calls(), [])

    def test_pr_body_uses_rest_with_gh_repo(self):
        r = self.run_forge('--repo', 'o/n', 'pr', 'body', '7', out={'headRefOid': HEAD, 'body': 'x'})
        self.assertEqual(json.loads(r.stdout), {'headRefOid': HEAD, 'body': 'x'})
        call = self.calls()[0]
        self.assertEqual(call['argv'], ['api', 'repos/{owner}/{repo}/pulls/7', '--jq',
                                        '{headRefOid: .head.sha, body: (.body // "")}'])
        self.assertEqual(call['gh_repo'], 'o/n')

    def test_pr_list_and_tickets(self):
        r = self.run_forge('--repo', 'o/n', 'pr', 'list', '--state', 'open', '--limit', '100', out=[PR])
        self.assertEqual(json.loads(r.stdout), [PR])
        merged = {k: PR[k] for k in ('number', 'title', 'body', 'url')}
        self.run_forge('--repo', 'o/n', 'pr', 'list', '--state', 'merged', '--limit', '100', out=[merged])
        issue = {'number': 3, 'title': 't', 'body': 'b'}
        self.run_forge('--repo', 'o/n', 'ticket', 'view', '3', out=issue)
        closed = dict(issue, stateReason='COMPLETED')
        self.run_forge('--repo', 'o/n', 'ticket', 'list', '--state', 'closed', '--limit', '100', out=[closed])
        self.assertEqual([c['argv'] for c in self.calls()], [
            ['pr', 'list', '--repo', 'o/n', '--state', 'open', '--limit', '100', '--json',
             'number,title,body,url,headRefName,files'],
            ['pr', 'list', '--repo', 'o/n', '--state', 'merged', '--limit', '100', '--json', 'number,title,body,url'],
            ['issue', 'view', '3', '--repo', 'o/n', '--json', 'number,title,body,state,stateReason,labels,assignees,url'],
            ['issue', 'list', '--repo', 'o/n', '--state', 'closed', '--limit', '100', '--json',
             'number,title,body,state,stateReason,labels,assignees,url']])

    def test_ticket_graph_flattens_pages(self):
        r = self.run_forge('--repo', 'o/n', 'ticket', 'graph', out=PAGES)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([n['number'] for n in json.loads(r.stdout)], [1, 2])
        argv = self.calls()[0]['argv']
        self.assertEqual(argv[:4], ['api', 'graphql', '--paginate', '--slurp'])
        self.assertEqual(argv[-4:], ['-F', 'owner=o', '-F', 'repo=n'])
        self.assertTrue(argv[5].startswith('query=') and 'issues(states:OPEN,first:100' in argv[5])

    # ---- writes -----------------------------------------------------------
    def test_pr_set_body_patches_once_with_json_stdin(self):
        body = self.root / 'body.md'
        body.write_text('line one\n"quoted" é\n')
        r = self.run_forge('--repo', 'o/n', 'pr', 'set-body', '7', '--body-file', str(body))
        self.assertEqual(r.returncode, 0, r.stderr)
        (call,) = self.calls()
        self.assertEqual(call['argv'], ['api', '-X', 'PATCH', 'repos/{owner}/{repo}/pulls/7', '--input', '-'])
        self.assertEqual(json.loads(call['stdin']), {'body': 'line one\n"quoted" é\n'})
        self.assertEqual(call['gh_repo'], 'o/n')

    def test_write_failure_is_not_retried_and_keeps_gh_status(self):
        body = self.root / 'body.md'
        body.write_text('x')
        r = self.run_forge('pr', 'set-body', '7', '--body-file', str(body), rc=3)
        self.assertEqual(r.returncode, 3)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(self.halt.exists(), 'a failed write does not halt by itself')

    def test_gh_merge_failure_never_looks_like_a_definite_refusal(self):
        # 5/6/7/65 are the merge gate's refusal codes (Azure DevOps); a gh failure is ambiguous.
        for rc, want in ((1, 1), (2, 2), (4, 4), (5, 1), (6, 1), (7, 1), (65, 1)):
            r = self.run_forge('pr', 'merge', '7', '--squash', '--delete-branch', '--match-head', HEAD, rc=rc)
            self.assertEqual(r.returncode, want, rc)

    def test_pr_merge_argv_and_status(self):
        r = self.run_forge('--repo', 'o/n', 'pr', 'merge', '7', '--squash', '--delete-branch', '--subject', 'S s',
                           '--body-file', 'm.md', '--match-head', HEAD, out='merged', rc=0)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, 'merged')
        r = self.run_forge('pr', 'merge', '7', '--squash', '--delete-branch', '--match-head', HEAD, rc=1)
        self.assertEqual(r.returncode, 1)
        self.assertEqual([c['argv'] for c in self.calls()], [
            ['pr', 'merge', '7', '--repo', 'o/n', '--squash', '--delete-branch', '--subject', 'S s',
             '--body-file', 'm.md', '--match-head-commit', HEAD],
            ['pr', 'merge', '7', '--squash', '--delete-branch', '--match-head-commit', HEAD]])
        self.assertEqual(self.run_forge('pr', 'merge', '7', '--match-head', HEAD).returncode, 64)

    # ---- halt -------------------------------------------------------------
    def test_failed_reads_halt_with_79_and_stop_further_reads(self):
        r = self.run_forge('pr', 'head', '7', rc=1, err='HTTP 503')
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(len(self.calls()), 3)
        self.assertTrue(self.halt.exists())
        again = self.run_forge('pr', 'state', '7', out=STATE)
        self.assertEqual(again.returncode, 79)
        self.assertEqual(len(self.calls()), 3, 'halted run must not read again')
        self.assertEqual(self.run_forge('check').returncode, 79)

    def test_check_and_halt_verbs(self):
        self.assertEqual(self.run_forge('check').returncode, 0)
        r = self.run_forge('halt', 'merge outcome unknown')
        self.assertEqual(r.returncode, 79)
        self.assertIn('merge outcome unknown', json.loads(self.halt.read_text())['reason'])
        self.assertEqual(self.run_forge('check').returncode, 79)

    def test_malformed_read_exits_64_unless_strict(self):
        r = self.run_forge('pr', 'head', '7', out='nonsense')
        self.assertEqual(r.returncode, 64, r.stderr)
        self.assertFalse(self.halt.exists())
        r = self.run_forge('pr', 'head', '7', out='nonsense', env={'SB_GITHUB_READ_STRICT': '1'})
        self.assertEqual(r.returncode, 79)


if __name__ == '__main__':
    unittest.main()
