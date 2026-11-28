#!/usr/bin/env python3
"""The onboarding checks (super-board-setup.py check/fix) on an `azure-devops` config.

Against the fake Azure DevOps: gh is not a required tool, a token is; the `ado` block is validated; the
board is read with `board ensure --check` and set up by `fix`; an empty Azure Repos repository is a
must-fix with the exact git commands; nothing writes the run's halt file. GitHub configs keep
needing gh (tests/test-setup.sh covers the rest of the GitHub checks, unmodified)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase  # noqa: E402

_spec = importlib.util.spec_from_file_location('sbsetup', ROOT / 'scripts/super-board-setup.py')
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)

SETUP = ROOT / 'scripts/super-board-setup.py'


class SetupCase(AdoCase):
    seven = True

    def setUp(self):
        super().setUp()
        self.proj = self.root / 'proj'
        (self.proj / '.claude/super-board/configs').mkdir(parents=True)
        self.config_value['repo'] = {'path': str(self.proj)}
        self.write_config()
        patch = mock.patch.dict(os.environ, {k: v for k, v in self.env.items()}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        self.fake_az = self.bin / 'az'

    def write_config(self, value=None):
        path = self.proj / '.claude/super-board/configs/ado.json'
        path.write_text(json.dumps(value or self.config_value))
        return path

    def check(self):
        with mock.patch.object(setup, 'have', lambda t: t != 'gh'):
            return setup.check(str(self.proj), str(ROOT))

    def report(self):
        return self.check()['ado']['.claude/super-board/configs/ado.json']


class ToolTests(SetupCase):
    def test_gh_is_not_required_on_azure_devops_but_still_is_on_github(self):
        names = [n['name'] for n in self.check()['needs']]
        self.assertNotIn('gh', names)
        github = self.proj / '.claude/super-board/configs/gh.json'
        github.write_text('{"provider":"github"}')
        self.assertIn('gh', [n['name'] for n in self.check()['needs']])

    def test_no_config_at_all_keeps_the_github_default(self):
        (self.proj / '.claude/super-board/configs/ado.json').unlink()
        self.assertIn('gh', [n['name'] for n in self.check()['needs']])


class ConfigTests(SetupCase):
    def test_a_good_block_has_no_errors(self):
        self.assertEqual(setup.validate_ado(self.config_value), [])

    def test_each_bad_key_is_named_and_nothing_is_called(self):
        self.config_value['ado'].update(organization='dev.azure.com/x', project=' ', team='', board=3,
                                        repository=None, approvers='me@x.com')
        self.write_config()
        report = self.report()
        text = '\n'.join(report['errors'])
        for key in ('ado.organization', 'ado.project', 'ado.team', 'ado.board', 'ado.repository', 'ado.approvers'):
            self.assertIn(key, text)
        self.assertEqual(self.fake.requests, [], 'a config that is wrong is reported before any call')

    def test_missing_ado_block(self):
        self.assertIn('"ado" block missing', setup.validate_ado({'provider': 'azure-devops'})[0])

    def test_approvers_must_be_strings(self):
        bad = dict(self.config_value, ado=dict(self.ado, approvers=['a@x.com', 3]))
        self.assertIn('ado.approvers', '\n'.join(setup.validate_ado(bad)))


class CredentialTests(SetupCase):
    def test_no_credentials_is_a_must_fix_with_no_network(self):
        with mock.patch.dict(os.environ, {'AZURE_DEVOPS_EXT_PAT': ''}):
            self.fake_az.write_text('#!/bin/sh\nexit 1\n')
            self.fake_az.chmod(0o755)
            report = self.report()
        self.assertIn('no Azure DevOps credentials', report['errors'][0])
        self.assertEqual(self.fake.requests, [])

    def test_the_project_env_file_counts(self):
        (self.proj / '.env').write_text('OTHER=1\nexport AZURE_DEVOPS_EXT_PAT="abc"\n')
        with mock.patch.dict(os.environ, {'AZURE_DEVOPS_EXT_PAT': ''}):
            self.assertEqual(setup.ado_token_source(str(self.proj), self.config_value), '.env')

    def test_a_working_az_login_counts_and_az_is_optional(self):
        self.fake_az.write_text('#!/bin/sh\necho token-from-az\n')
        self.fake_az.chmod(0o755)
        with mock.patch.dict(os.environ, {'AZURE_DEVOPS_EXT_PAT': ''}):
            self.assertEqual(setup.ado_token_source(str(self.proj), self.config_value), 'az login')
            self.fake_az.unlink()
            with mock.patch.object(setup.shutil, 'which', lambda t: None):
                self.assertEqual(setup.ado_token_source(str(self.proj), self.config_value), '')


class BoardAndRepositoryTests(SetupCase):
    def test_a_healthy_project_reports_no_azure_devops_problem(self):
        report = self.report()
        self.assertEqual(report['errors'], [])
        self.assertEqual(report['credentials'], 'environment')
        self.assertEqual(report['repository']['empty'], False)
        self.assertIn('Azure DevOps ready', '\n'.join(self.check()['ok']))
        self.assertEqual(self.fake.reqs('PUT'), [], 'a check never writes')

    def test_an_empty_repository_is_a_must_fix_with_the_exact_commands(self):
        self.fake.repo_heads = []
        self.config_value['base_branch'] = 'main'
        self.write_config()
        result = self.check()
        self.assertFalse(result['green'])
        (error,) = self.report()['errors']
        self.assertIn('git checkout -b main && git commit --allow-empty -m "Initial commit" && git push -u origin main', error)
        self.assertIn('cannot be opened until the base branch', error)

    def test_a_missing_base_branch_is_reported(self):
        self.config_value['base_branch'] = 'develop'
        self.write_config()
        self.assertIn('base branch "develop" does not exist', self.report()['errors'][0])

    def test_board_shape_is_checked_then_fixed_without_a_question(self):
        self.fake.columns = [dict(c) for c in self.fake.columns[:5]]  # a wrong board: five columns
        self.fake.columns[0]['name'] = 'To do'
        self.fake.items.clear()
        report = self.report()
        self.assertTrue(any('columns:' in line for line in report['board']), report)
        self.assertEqual(self.fake.reqs('PUT'), [])
        with mock.patch.object(setup, 'have', lambda t: t != 'gh'):
            result = setup.fix(str(self.proj), str(ROOT), helpers=False)
        self.assertTrue(any(f.startswith('Azure DevOps board set up') for f in result['fixed']), result['fixed'])
        self.assertEqual([c['name'] for c in self.fake.columns], ['Backlog', 'Ready', 'Building', 'QA', 'Review', 'Blocked', 'Done'])
        self.assertEqual(result['ado']['.claude/super-board/configs/ado.json']['errors'], [])

    def test_a_refused_board_change_is_not_forced(self):
        self.fake.columns = [dict(c) for c in self.fake.columns]
        self.fake.columns.insert(2, dict(self.fake.columns[2], id='extra', name='Extra'))
        self.fake.add('User Story', 'card', '', '', 'Extra')
        report = self.report()
        self.assertTrue(report['errors'])
        self.assertIn('refusing to remove column "Extra"', '\n'.join(report['errors']))

    def test_a_rejected_read_is_reported_and_never_halts_the_run(self):
        self.fake.inject.append({'match': lambda r: True, 'times': 99, 'status': 401, 'body': {}})
        report = self.report()
        self.assertTrue(any('cannot check the Azure DevOps board' in e for e in report['errors']), report)
        self.assertFalse(self.halt.exists(), 'a setup check must not write the halt file')

    def test_the_cli_prints_the_red_item_in_text(self):
        self.fake.repo_heads = []
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run([sys.executable, str(SETUP), 'check', '--root', str(self.proj), '--pack', str(ROOT), '--text'],
                           env=e, text=True, capture_output=True, cwd=self.proj, stdin=subprocess.DEVNULL, timeout=180)
        self.assertIn('has no branch', r.stdout)
        self.assertEqual(r.returncode, 1)


if __name__ == '__main__':
    unittest.main()
