#!/usr/bin/env python3
"""hooks/cleanup-wt.py on an `azure-devops` config: open and merged PRs come from the forge
(`pr refs`), a PR counts as merged only when it is completed AND its recorded source head equals
the local branch tip, and an unreachable service keeps remote branches. A `gh` shim that fails and
logs proves `gh` is never run."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase  # noqa: E402
from test_forge_ado_pr import GitMixin, put_file  # noqa: E402

HOOK = ROOT / 'hooks/cleanup-wt.py'
FORGE = ROOT / 'scripts/super-board-forge.py'


class CleanupCase(AdoCase, GitMixin):
    def setUp(self):
        super().setUp()
        self.pulls = self.fake.pulls
        self.init_repo()
        put_file(self.work / 'base.txt', 'base\n')
        self.commit('base')
        self.git('push', '-q', 'origin', 'main')
        self.git('remote', 'set-head', 'origin', 'main')
        (self.work / '.git/info/exclude').write_text('.claude/\n')
        sb = self.work / '.claude/super-board'
        (sb / 'configs').mkdir(parents=True)
        (sb / 'configs/test.json').write_text(json.dumps(self.config_value))
        (sb / 'active').write_text('test\n')
        gh = self.bin / 'gh'
        gh.write_text('#!/bin/sh\necho "$@" >> "$GH_SHIM_LOG"\nexit 1\n')
        gh.chmod(0o755)
        self.gh_log = self.root / 'gh.log'
        self.env.update(GH_SHIM_LOG=str(self.gh_log), CLEANUP_WT_GH=str(gh), CLEANUP_WT_FORGE=str(FORGE),
                        SB_GITHUB_RETRY_DELAY='0')
        self.env.update({'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
                         'GIT_COMMITTER_EMAIL': 't@t'})

    def branch(self, name, files=1, push=True):
        """A branch with commits that are NOT in main (no git check can call it merged)."""
        self.git('checkout', '-q', '-b', name, 'main')
        for i in range(files):
            put_file(self.work / f'{name}-{i}.txt', f'{name} {i}\n')
            tip = self.commit(f'{name} {i}')
        if push:
            self.git('push', '-q', 'origin', name)
        self.git('checkout', '-q', 'main')
        return tip

    def completed(self, name, head):
        n = self.pulls.add(name, 'main', head=head)
        self.pulls.prs[n]['status'] = 'completed'
        self.pulls.prs[n]['lastMergeCommit'] = {'commitId': 'c' * 40}
        return n

    def hook(self, *args):
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run([sys.executable, str(HOOK), '--base', 'main', *args], env=e, text=True, capture_output=True,
                           cwd=self.work, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def local(self):
        return set(self.git('for-each-ref', 'refs/heads', '--format=%(refname:short)').split())

    def remote(self):
        self.git('fetch', '-q', '--prune', 'origin')
        return set(self.git('for-each-ref', 'refs/remotes/origin', '--format=%(refname:short)').split())

    def no_gh(self):
        self.assertFalse(self.gh_log.exists() and self.gh_log.read_text().strip(), 'gh must never be called')


class PrRefsVerbTests(CleanupCase):
    def test_refs_shapes_and_completed_only(self):
        a = self.pulls.add('feat-open', 'main', head='a' * 40)
        b = self.completed('feat-done', 'b' * 40)
        c = self.completed('feat-nohead', 'd' * 40)
        self.pulls.prs[c]['lastMergeSourceCommit'] = None
        d = self.pulls.add('feat-aband', 'main', head='e' * 40)
        self.pulls.prs[d]['status'] = 'abandoned'
        self.assertEqual(self.js('pr', 'refs', '--state', 'open', '--limit', '50'),
                         [{'number': a, 'headRefName': 'feat-open', 'baseRefName': 'main', 'headRefOid': 'a' * 40}])
        self.assertEqual(self.js('pr', 'refs', '--state', 'merged', '--limit', '50'),
                         [{'number': b, 'headRefName': 'feat-done', 'baseRefName': 'main', 'headRefOid': 'b' * 40}])
        self.assertEqual(len(self.fake.reqs('GET', r'/pullrequests$')), 2, 'one list call per state, no per-PR reads')

    def test_github_backend_leaves_it_unimplemented(self):
        cfg = self.root / 'gh.json'
        cfg.write_text('{"provider":"github"}')
        r = subprocess.run([sys.executable, str(FORGE), '--config', str(cfg), 'pr', 'refs', '--state', 'open',
                            '--limit', '5'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 69)


class HookTests(CleanupCase):
    def test_removes_only_what_the_service_confirms_merged_at_this_exact_tip(self):
        merged_tip = self.branch('feat-merged')
        moved_tip = self.branch('feat-moved')
        active_tip = self.branch('feat-active')
        aband_tip = self.branch('feat-aband')
        self.branch('feat-nopr')
        self.branch('feat-nohead')
        self.completed('feat-merged', merged_tip)
        self.completed('feat-moved', 'f' * 40)                              # completed, but at another head
        self.pulls.add('feat-active', 'main', head=active_tip)              # still open
        n = self.pulls.add('feat-aband', 'main', head=aband_tip)
        self.pulls.prs[n]['status'] = 'abandoned'
        gone = self.completed('feat-nohead', 'a' * 40)
        self.pulls.prs[gone]['lastMergeSourceCommit'] = None
        r = self.hook('--apply')
        self.assertEqual(self.local(), {'main', 'feat-moved', 'feat-active', 'feat-aband', 'feat-nopr', 'feat-nohead'})
        self.assertIn('PR completed on Azure DevOps', r.stdout)
        self.assertEqual(self.remote() - {'origin/main', 'origin'}, {
            'origin/feat-moved', 'origin/feat-active', 'origin/feat-aband', 'origin/feat-nopr', 'origin/feat-nohead'},
            'the merged remote branch goes too')
        self.no_gh()

    def test_a_completed_pr_whose_branch_gained_commits_since_is_kept(self):
        self.branch('feat-more', files=2)
        recorded = self.git('rev-parse', 'feat-more~1')   # the head the PR completed at
        self.completed('feat-more', recorded)
        self.hook('--apply')
        self.assertIn('feat-more', self.local(), 'tip != recorded head: work after the merge must survive')
        self.assertIn('origin/feat-more', self.remote())

    def test_open_pr_naming_the_branch_as_base_keeps_it(self):
        tip = self.branch('feat-base')
        self.completed('feat-base', tip)
        self.pulls.add('feat-child', 'feat-base', head='9' * 40)
        self.hook('--apply')
        self.assertIn('feat-base', self.local())

    def test_unreachable_service_keeps_remote_branches_and_says_so(self):
        tip = self.branch('feat-merged')
        self.completed('feat-merged', tip)
        self.fake.inject.append({'match': lambda req: '/pullrequests' in req['path'], 'times': 99, 'status': 500, 'body': {}})
        r = self.hook('--apply', '--remote-only')
        self.assertIn('Azure DevOps unavailable', r.stdout)
        self.assertIn('remote branches are kept', r.stdout)
        self.assertIn('origin/feat-merged', self.remote())
        self.no_gh()

    def test_an_unreadable_merged_list_never_reads_as_nothing_merged(self):
        tip = self.branch('feat-merged')
        self.completed('feat-merged', tip)
        self.fake.inject.append({'match': lambda req: 'searchCriteria.status=completed' in json.dumps(req['query'])
                                 or req['query'].get('searchCriteria.status') == ['completed'],
                                 'times': 99, 'status': 500, 'body': {}})
        r = self.hook('--apply')
        self.assertIn('feat-merged', self.local(), 'no confirmation, no deletion')
        self.assertIn('remote branches are kept', r.stdout)

    def test_the_service_halt_file_is_neither_honoured_nor_written(self):
        tip = self.branch('feat-merged')
        self.completed('feat-merged', tip)
        self.fake.inject.append({'match': lambda req: '/pullrequests' in req['path'], 'times': 99, 'status': 500, 'body': {}})
        self.hook('--apply')
        self.assertFalse(self.halt.exists(), 'a cleanup hook must not halt a run')

    def test_without_an_azure_devops_config_the_github_path_is_unchanged(self):
        (self.work / '.claude/super-board/configs/test.json').write_text('{"provider":"github"}')
        tip = self.branch('feat-merged')
        self.completed('feat-merged', tip)
        r = self.hook('--apply')   # the gh shim fails: "gh unavailable", nothing remote is touched
        self.assertIn('gh unavailable', r.stdout)
        self.assertIn('feat-merged', self.local())
        self.assertTrue(self.gh_log.read_text().strip())


if __name__ == '__main__':
    unittest.main()
