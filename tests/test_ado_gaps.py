#!/usr/bin/env python3
"""The last script gaps on Azure DevOps, one scenario per item (offline: the fake Azure DevOps server and a
PATH-stub `gh`), plus the GitHub argv where a GitHub path was touched.

1 skills-ready --hydrate reads body and labels through the forge        4 bot_identity is GitHub-only
2 super-build-dispatch.sh reads the ticket through the forge             5 config-schema allow-list comment
3 the token is read from .env, then .env.local                           6 `forge resume` (both providers)
7 super-board-setup.py check --config <staged config>"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase, PAT, basic  # noqa: E402

GH_STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['GH_LOG'], 'a') as f:
    f.write(json.dumps(args) + '\n')
mode = open(os.environ['GH_MODE']).read().strip() if os.path.exists(os.environ['GH_MODE']) else 'ok'
if mode == 'denied':
    sys.stderr.write('HTTP 401: Bad credentials\n')
    sys.exit(1)
if args[:2] == ['api', 'rate_limit']:
    print(json.dumps({'resources': {'graphql': {'remaining': 5000, 'reset': 0}, 'core': {'remaining': 5000, 'reset': 0}}}))
elif args[:2] == ['issue', 'view']:
    fields = args[args.index('--json') + 1]
    if fields == 'labels':
        print(json.dumps({'labels': [{'name': 'bug'}]}))
    else:
        print(json.dumps({'number': int(args[2]), 'title': 't', 'body': 'b', 'state': 'OPEN', 'stateReason': None,
                          'labels': [{'name': 'bug'}], 'assignees': [], 'url': 'u'}))
else:
    sys.exit('unexpected gh call: ' + ' '.join(args))
'''


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Gaps(AdoCase):
    def setUp(self):
        super().setUp()
        self.proj = self.root / 'proj'
        (self.proj / '.claude/super-board/configs').mkdir(parents=True)
        self.config_value['repo'] = {'path': str(self.proj)}
        self.gh_log = self.root / 'gh.log'
        self.gh_mode = self.root / 'gh.mode'
        gh = self.bin / 'gh'
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.env.update(GH_LOG=str(self.gh_log), GH_MODE=str(self.gh_mode), SB_REPO_PATH=str(self.proj))
        patch = mock.patch.dict(os.environ, {k: v for k, v in self.env.items()}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)

    def gh_calls(self):
        return [json.loads(l) for l in self.gh_log.read_text().splitlines()] if self.gh_log.exists() else []

    def github_config(self):
        path = self.root / 'gh-cfg.json'
        path.write_text(json.dumps({'provider': 'github', 'repo': {'path': str(self.proj)}}))
        return str(path)

    def run_forge(self, config, *args, env=None):
        e = {k: v for k, v in dict(self.env, **(env or {})).items() if v is not None}
        return subprocess.run([sys.executable, str(ROOT / 'scripts/super-board-forge.py'), '--config', config, *args],
                              env=e, text=True, capture_output=True, cwd=self.proj, stdin=subprocess.DEVNULL)


class HydrateTests(Gaps):
    """1. skills-ready --hydrate."""

    def test_azure_devops_reads_body_and_labels_through_the_forge_in_one_read(self):
        self.fake.items[1]['fields']['System.Tags'] = 'bug; extra'
        ready = load('ready', 'scripts/super-board-skills-ready.py')
        cards = ready.hydrate([{'number': 1}], str(self.proj), self.cfg())
        self.assertEqual(cards[0]['labels'], [{'name': 'bug'}, {'name': 'extra'}])
        self.assertIsInstance(cards[0]['body'], str)
        self.assertEqual(len(self.fake.reqs('GET', r'/workitems/1$')), 1, 'body and labels share one read')
        self.assertEqual(self.gh_calls(), [], 'gh is never run')

    def test_cli_takes_config_and_a_missing_config_file_is_a_usage_error(self):
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run([sys.executable, str(ROOT / 'scripts/super-board-skills-ready.py'), '--root', str(self.proj),
                            '--host', 'claude', '--cards', '-', '--hydrate', '--config', str(self.root / 'nope.json')],
                           input='[{"number":1}]', env=e, text=True, capture_output=True)
        self.assertEqual(r.returncode, 64)
        self.assertIn('config unreadable', r.stderr)

    def test_github_argv_is_unchanged_with_and_without_a_config(self):
        ready = load('ready', 'scripts/super-board-skills-ready.py')
        for config in (None, self.github_config()):
            self.gh_log.unlink() if self.gh_log.exists() else None
            card = ready.hydrate([{'number': 5}], str(self.proj), config)[0]
            self.assertEqual(card['labels'], [{'name': 'bug'}])
            self.assertEqual(self.gh_calls(), [['issue', 'view', '5', '--json', 'number,title,body'],
                                               ['issue', 'view', '5', '--json', 'labels']])

    def test_callers_pass_the_config(self):
        for rel in ('scripts/super-board-lane.sh', 'scripts/super-board-codex-wave.sh'):
            self.assertIn('hydrate(', (ROOT / rel).read_text(encoding='utf-8'))
            self.assertRegex((ROOT / rel).read_text(encoding='utf-8'), r'hydrate\(\[?card[s]?\]?, root, str\(config_path\)\)')
        self.assertIn('--cards - --config ${input.configPath} --hydrate', (ROOT / 'workflows/super-board-wave.js').read_text(encoding='utf-8'))


class DispatchTests(Gaps):
    """2. super-build-dispatch.sh (it stops at exit 10: no worker skills in the empty home)."""

    def setUp(self):
        super().setUp()
        subprocess.run(['git', 'init', '-q', str(self.proj)], check=True)
        subprocess.run(['git', '-C', str(self.proj), '-c', 'user.name=F', '-c', 'user.email=f@example.com', 'commit',
                        '--allow-empty', '-qm', 'x'], check=True)
        bindir = self.proj / '.claude/bin'
        bindir.mkdir(parents=True, exist_ok=True)
        for name in ('super-board-github-read.py', 'super-board-skills-ready.py', 'super-board-forge.py',
                     'super-board-forge-ado.py', 'super-board-forge-ado-pr.py', 'super-board-forge-github.py',
                     'super-board-compat.py'):  # install.sh ships it beside them; it finds gh/az on Windows
            shutil.copy2(ROOT / 'scripts' / name, bindir / name)
        self.home = self.root / 'home'
        self.home.mkdir()
        branch = subprocess.check_output(['git', '-C', str(self.proj), 'branch', '--show-current'], text=True).strip()
        self.env.update(REPO_DIR=str(self.proj), BASE_BRANCH=branch, SUPER_BOARD_SKILLS_HOME=str(self.home))

    def dispatch(self, **env):
        e = {k: v for k, v in dict(self.env, **env).items() if v is not None}
        e.pop('SB_CONFIG', None) if 'SB_CONFIG' not in env else None
        return subprocess.run(['bash', str(ROOT / 'skills/super-build/scripts/super-build-dispatch.sh'), '1'],
                              env=e, text=True, capture_output=True, cwd=self.proj, stdin=subprocess.DEVNULL)

    def test_with_a_config_the_ticket_is_read_through_the_forge(self):
        r = self.dispatch(SB_CONFIG=self.cfg())
        self.assertEqual(r.returncode, 10, r.stderr)  # the card was built and reached skills-ready
        self.assertTrue(self.fake.reqs('GET', r'/workitems/1$'))
        self.assertEqual(self.gh_calls(), [])

    def test_a_forge_failure_is_reported_with_its_exit_code(self):
        r = self.dispatch(SB_CONFIG=self.cfg(), AZURE_DEVOPS_EXT_PAT=None, SB_REPO_PATH=str(self.root / 'none'))
        self.assertEqual(r.returncode, 69, r.stderr)
        self.assertIn('forge ticket view #1 failed', r.stderr)

    def test_without_a_config_the_github_argv_is_exactly_what_it_was(self):
        r = self.dispatch()
        self.assertEqual(r.returncode, 10, r.stderr)
        self.assertEqual(self.gh_calls()[:2], [['issue', 'view', '1', '--json', 'number,title,body'],
                                               ['issue', 'view', '1', '--json', 'labels']])


class TokenFileTests(Gaps):
    """3. .env, then .env.local."""

    def setUp(self):
        super().setUp()
        self.env.pop('AZURE_DEVOPS_EXT_PAT')
        os.environ.pop('AZURE_DEVOPS_EXT_PAT', None)

    def auths(self):
        return {r['authorization'] for r in self.fake.requests}

    def test_the_forge_falls_back_to_env_local(self):
        (self.proj / '.env.local').write_text('AZURE_DEVOPS_EXT_PAT="pat-from-local-1234"\n')
        self.fake.token_ok = basic('pat-from-local-1234')
        self.js('whoami')
        self.assertEqual(self.auths(), {basic('pat-from-local-1234')})

    def test_env_wins_over_env_local_and_an_empty_line_falls_through(self):
        (self.proj / '.env').write_text('AZURE_DEVOPS_EXT_PAT=pat-from-env-1234\n')
        (self.proj / '.env.local').write_text('AZURE_DEVOPS_EXT_PAT=pat-from-local-1234\n')
        self.fake.token_ok = basic('pat-from-env-1234')
        self.js('whoami')
        self.assertEqual(self.auths(), {basic('pat-from-env-1234')})
        (self.proj / '.env').write_text('AZURE_DEVOPS_EXT_PAT=\n')
        self.fake.requests.clear()
        self.fake.token_ok = basic('pat-from-local-1234')
        self.js('whoami')
        self.assertEqual(self.auths(), {basic('pat-from-local-1234')})

    def test_setup_and_the_forge_agree_on_where_the_token_is(self):
        setup = load('sbsetup', 'scripts/super-board-setup.py')
        cfg = self.config_value
        self.assertEqual(setup.ado_token_source(str(self.proj), cfg), '' if not shutil.which('az') else
                         setup.ado_token_source(str(self.proj), cfg))
        (self.proj / '.env.local').write_text('AZURE_DEVOPS_EXT_PAT=x1\n')
        self.assertEqual(setup.ado_token_source(str(self.proj), cfg), '.env.local')
        (self.proj / '.env').write_text('export AZURE_DEVOPS_EXT_PAT=x2\n')
        self.assertEqual(setup.ado_token_source(str(self.proj), cfg), '.env')

    def test_env_check_finds_the_key_in_either_file(self):
        check = ROOT / 'scripts/super-board-env-check.sh'
        (self.proj / '.env.local').write_text('OTHER=1\n')
        (self.proj / '.env').write_text('AZURE_DEVOPS_EXT_PAT=secretvalue\n')
        r = subprocess.run(['bash', str(check), 'AZURE_DEVOPS_EXT_PAT'], cwd=self.proj, text=True, capture_output=True)
        self.assertEqual((r.returncode, r.stdout), (0, 'AZURE_DEVOPS_EXT_PAT present\n'))
        self.assertNotIn('secretvalue', r.stdout + r.stderr)
        r = subprocess.run(['bash', str(check), '--file', '.env.local', 'AZURE_DEVOPS_EXT_PAT'], cwd=self.proj,
                           text=True, capture_output=True)
        self.assertEqual((r.returncode, r.stdout), (1, 'AZURE_DEVOPS_EXT_PAT missing\n'), '--file names exactly one file')


class IdentityTests(Gaps):
    """4. bot_identity is GitHub-only."""

    def test_setup_never_flags_a_missing_or_auto_bot_identity_on_azure_devops(self):
        setup = load('sbsetup', 'scripts/super-board-setup.py')
        for notifications in (None, {'channel': 'session'}, {'bot_identity': 'auto'}):
            cfg = dict(self.config_value)
            if notifications is not None:
                cfg['notifications'] = notifications
            path = self.proj / '.claude/super-board/configs/ado.json'
            path.write_text(json.dumps(cfg))
            with mock.patch.object(setup, 'have', lambda t: t != 'gh'):
                res = setup.check(str(self.proj), str(ROOT))
            report = res['ado']['.claude/super-board/configs/ado.json']
            self.assertEqual(report['errors'], [], notifications)
            self.assertNotIn('bot', json.dumps(report).lower())
            self.assertNotIn('bot', json.dumps(res['configs']).lower(), 'no migration proposes a bot_identity either')

    def test_status_shows_the_whoami_login_not_bot_identity(self):
        self.config_value['notifications'] = {'bot_identity': 'someone-else[bot]'}
        (self.proj / '.claude/super-board/configs/test.json').write_text(json.dumps(self.config_value))
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run([sys.executable, str(ROOT / 'scripts/super-board-status.py'), 'test'], env=e, text=True,
                           capture_output=True, cwd=self.proj, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('claim: user@example.com', r.stdout)
        self.assertNotIn('someone-else', r.stdout)

    def test_status_falls_back_to_a_question_mark_when_whoami_cannot_be_read(self):
        (self.proj / '.claude/super-board/configs/test.json').write_text(json.dumps(self.config_value))
        self.fake.inject.append({'match': lambda r: r['path'] == '/_apis/connectionData', 'times': 99, 'status': 500, 'body': {}})
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run([sys.executable, str(ROOT / 'scripts/super-board-status.py'), 'test'], env=e, text=True,
                           capture_output=True, cwd=self.proj, stdin=subprocess.DEVNULL, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('claim: ?', r.stdout)
        self.assertFalse(self.halt.exists())

    def test_the_schema_and_stop_say_so(self):
        schema = (ROOT / 'skills/super-board/references/config-schema.json').read_text(encoding='utf-8')
        self.assertIn('GITHUB ONLY. On `provider: "azure-devops"` this key is ignored', schema)
        self.assertIn('GitHub only', (ROOT / 'scripts/super-board-stop.sh').read_text(encoding='utf-8'))


class AllowListTests(Gaps):
    """5. the schema comment matches run-workflow.md."""

    def test_no_gh_pr_merge_rule_anywhere_the_allow_list_is_described(self):
        schema = (ROOT / 'skills/super-board/references/config-schema.json').read_text(encoding='utf-8')
        workflow = (ROOT / 'skills/super-board/references/run-workflow.md').read_text(encoding='utf-8')
        self.assertNotIn('Bash(gh pr merge', schema)
        self.assertNotIn('Bash(gh pr merge', workflow)
        self.assertIn('Bash(bash .claude/bin/super-board-merge-gate.sh:*)', schema)
        self.assertIn('Bash(bash .claude/bin/super-board-merge-gate.sh:*)', workflow)


class ResumeTests(Gaps):
    """6. forge resume."""

    def archived(self):
        return sorted(self.halt.parent.glob('github-halt-*.json'))

    def test_azure_devops_get_read_halt_replays_and_archives(self):
        self.fake.inject.append({'match': lambda r: r['path'].endswith('/workitems/1'), 'times': 1, 'status': 401, 'body': {}})
        r = self.forge('ticket', 'view', '1')
        self.assertEqual(r.returncode, 79, r.stderr)
        record = json.loads(self.halt.read_text())
        op = record['operation']
        self.assertEqual((op['provider'], op['method'], op['validate'], op['fixed']), ('azure-devops', 'GET', 'v_item', False))
        self.assertTrue(op['path'].startswith('/Factory/_apis/wit/workitems/1?'), op['path'])
        self.assertNotIn(PAT, self.halt.read_text())
        self.assertNotIn('127.0.0.1', self.halt.read_text(), 'no host in the record')
        r = self.forge('resume')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.halt.exists())
        self.assertEqual(len(self.archived()), 1)
        self.assertEqual(json.loads(self.archived()[0].read_text())['reason'], record['reason'])
        self.assertEqual(self.forge('check').returncode, 0)
        self.assertEqual(self.forge('resume').returncode, 0, 'nothing halted: nothing to do')

    def test_azure_devops_post_read_halt_keeps_its_body_and_replays(self):
        self.fake.inject.append({'match': lambda r: r['method'] == 'POST' and 'wiql' in r['path'], 'times': 1, 'status': 403, 'body': {}})
        self.assertEqual(self.forge('board', 'items').returncode, 79)
        op = json.loads(self.halt.read_text())['operation']
        self.assertEqual((op['method'], op['validate']), ('POST', 'v_wiql'))
        self.assertIn('query', op['body'])
        self.assertEqual(self.forge('resume').returncode, 0)
        self.assertEqual(len(self.archived()), 1)
        self.assertEqual(self.forge('board', 'items').returncode, 0)

    def test_a_pull_request_validator_is_found_too(self):
        n = self.fake.pulls.add('issue-1-x', 'main')
        self.fake.inject.append({'match': lambda r: f'/pullrequests/{n}' in r['path'].lower() and r['method'] == 'GET',
                                 'times': 1, 'status': 401, 'body': {}})
        self.assertEqual(self.forge('pr', 'view', str(n)).returncode, 79)
        self.assertEqual(json.loads(self.halt.read_text())['operation']['validate'], 'v_pr')
        self.assertEqual(self.forge('resume').returncode, 0)

    def test_a_replay_that_still_fails_leaves_the_halt_untouched(self):
        self.fake.inject.append({'match': lambda r: r['path'].endswith('/workitems/1'), 'times': 99, 'status': 401, 'body': {}})
        self.assertEqual(self.forge('ticket', 'view', '1').returncode, 79)
        before = self.halt.read_text()
        r = self.forge('resume')
        self.assertEqual(r.returncode, 79)
        self.assertEqual(self.halt.read_text(), before)
        self.assertEqual(self.archived(), [])

    def test_an_unresolved_write_halt_is_never_auto_resumed_on_either_provider(self):
        for config in (self.cfg(), self.github_config()):
            self.assertEqual(self.run_forge(config, 'halt', 'move outcome unknown').returncode, 79)
            r = self.run_forge(config, 'resume')
            self.assertEqual(r.returncode, 79, r.stderr)
            self.assertIn('manual outcome reconciliation', r.stderr)
            self.assertTrue(self.halt.exists())
            self.assertEqual(self.archived(), [])
            self.halt.unlink()

    def test_a_tampered_record_is_refused_not_followed(self):
        for op in ({'provider': 'azure-devops', 'method': 'GET', 'path': 'http://evil.example/x', 'validate': None},
                   {'provider': 'azure-devops', 'method': 'DELETE', 'path': '/x', 'validate': None},
                   {'provider': 'azure-devops', 'method': 'GET', 'path': '/Factory/_apis/x', 'validate': '__import__'}):
            self.halt.parent.mkdir(parents=True, exist_ok=True)
            self.halt.write_text(json.dumps({'reason': 'x', 'operation': op}))
            self.assertEqual(self.forge('resume').returncode, 79, op)
            self.assertTrue(self.halt.exists())
        self.assertEqual(self.fake.requests, [])

    def test_github_resume_is_the_helpers_resume_with_the_same_gh_argv(self):
        config = self.github_config()
        self.gh_mode.write_text('denied')
        self.assertEqual(self.run_forge(config, 'ticket', 'view', '5').returncode, 79)
        self.assertTrue(self.halt.exists())
        self.gh_log.unlink()
        self.gh_mode.write_text('ok')
        r = self.run_forge(config, 'resume')
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.gh_calls()
        self.assertEqual([c[:2] for c in calls], [['issue', 'view'], ['api', 'rate_limit']])
        self.assertEqual(calls[0][:4], ['issue', 'view', '5', '--json'])
        self.assertEqual(len(self.archived()), 1)
        # the helper itself still does the same on its own
        self.gh_mode.write_text('denied')
        self.assertEqual(self.run_forge(config, 'ticket', 'view', '5').returncode, 79)
        self.gh_mode.write_text('ok')
        self.gh_log.unlink()
        h = subprocess.run([sys.executable, str(ROOT / 'scripts/super-board-github-read.py'), '--resume'],
                           env={k: v for k, v in self.env.items() if v is not None}, cwd=self.proj, text=True, capture_output=True)
        self.assertEqual(h.returncode, 0, h.stderr)
        self.assertEqual([c[:2] for c in self.gh_calls()], [['issue', 'view'], ['api', 'rate_limit']])

    def test_github_resume_failure_passes_the_helpers_exit_and_keeps_the_halt(self):
        config = self.github_config()
        self.gh_mode.write_text('denied')
        self.assertEqual(self.run_forge(config, 'ticket', 'view', '5').returncode, 79)
        self.assertEqual(self.run_forge(config, 'resume').returncode, 79)
        self.assertTrue(self.halt.exists())

    def test_the_helper_refuses_an_azure_devops_record_cleanly(self):
        self.fake.inject.append({'match': lambda r: r['path'].endswith('/workitems/1'), 'times': 1, 'status': 401, 'body': {}})
        self.assertEqual(self.forge('ticket', 'view', '1').returncode, 79)
        h = subprocess.run([sys.executable, str(ROOT / 'scripts/super-board-github-read.py'), '--resume'],
                           env={k: v for k, v in self.env.items() if v is not None}, cwd=self.proj, text=True, capture_output=True)
        self.assertEqual(h.returncode, 79)
        self.assertNotIn('Traceback', h.stderr)
        self.assertTrue(self.halt.exists())

    def test_the_references_use_forge_resume(self):
        for rel in ('skills/super-board/references/run-workflow.md', 'skills/super-board/references/run.md',
                    'skills/super-board/references/providers.md'):
            self.assertIn('resume', (ROOT / rel).read_text(encoding='utf-8'))
        self.assertIn('super-board-forge.py --config <config> resume',
                      (ROOT / 'skills/super-board/references/run-workflow.md').read_text(encoding='utf-8'))


class StagedConfigTests(Gaps):
    """7. super-board-setup.py check --config <file>."""

    SETUP = ROOT / 'scripts/super-board-setup.py'

    def staged(self, value=None):
        path = self.root / 'onboard-staged.json'
        path.write_text(json.dumps(value or self.config_value))
        return str(path)

    def run_check(self, path, *extra):
        e = {k: v for k, v in self.env.items() if v is not None}
        return subprocess.run([sys.executable, str(self.SETUP), 'check', '--config', path, '--root', str(self.proj), *extra],
                              env=e, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=180)

    def test_a_good_staged_azure_devops_config_is_green_with_no_saved_config(self):
        self.assertEqual(list((self.proj / '.claude/super-board/configs').iterdir()), [])
        r = self.run_check(self.staged())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        res = json.loads(r.stdout)
        self.assertTrue(res['green'])
        self.assertEqual(res['provider'], 'azure-devops')
        (report,) = res['ado'].values()
        self.assertEqual((report['errors'], report['credentials']), ([], 'environment'))
        self.assertTrue(self.fake.requests, 'the board and the repository were read')
        self.assertEqual([r for r in self.fake.requests if r['method'] != 'GET' and 'wiql' not in r['path']], [], 'nothing written')

    def test_a_bad_block_a_missing_token_and_a_board_in_need_are_red_lines(self):
        bad = dict(self.config_value, ado=dict(self.ado, organization='nope'))
        r = self.run_check(self.staged(bad), '--text')
        self.assertEqual(r.returncode, 1)
        self.assertIn('ado.organization', r.stdout)
        # no token anywhere
        e = dict(self.env, AZURE_DEVOPS_EXT_PAT='', FAKE_AZ_TOKEN='')
        r = subprocess.run([sys.executable, str(self.SETUP), 'check', '--config', self.staged(), '--root', str(self.proj)],
                           env=e, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=180)
        self.assertEqual(r.returncode, 1)
        self.assertIn('no Azure DevOps credentials', json.dumps(json.loads(r.stdout)['ado']))

    def test_the_board_check_runs_on_the_staged_file(self):
        fake = self.fake
        fake.seven = False
        # a default Agile board: needs changes (the fake's default columns)
        self.server.shutdown()
        self.server.server_close()
        import fake_ado_server as fake_mod
        self.fake = fake_mod.Fake(seven=False)
        self.server = fake_mod.start(self.fake)
        self.env['SB_ADO_BASE_URL'] = f'http://127.0.0.1:{self.server.server_port}'
        r = self.run_check(self.staged())
        self.assertEqual(r.returncode, 1, r.stdout)
        (report,) = json.loads(r.stdout)['ado'].values()
        self.assertTrue(report['board'] and 'board' in report['fix'])

    def test_an_unreadable_config_and_the_default_github_checks(self):
        r = self.run_check(str(self.root / 'missing.json'))
        self.assertEqual(r.returncode, 1)
        self.assertIn('unreadable', json.loads(r.stdout)['error'])
        gh = self.run_check(self.github_config())
        res = json.loads(gh.stdout)
        self.assertEqual(res['provider'], 'github')
        self.assertEqual(res['ado'], {})
        self.assertIn(self.github_config(), res['verification'], 'a GitHub config gets the per-config checks check already runs')

    def test_without_config_the_no_argument_check_is_what_it_was(self):
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run([sys.executable, str(self.SETUP), 'check', '--root', str(self.proj), '--pack', str(ROOT)],
                           env=e, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=180)
        res = json.loads(r.stdout)
        self.assertIn('helper_readiness', res)
        self.assertIn('missing', res)

    def test_onboard_uses_it(self):
        text = (ROOT / 'skills/super-board/references/onboard.md').read_text(encoding='utf-8')
        self.assertIn('super-board-setup.py check --config <staged config> --text', text)


if __name__ == '__main__':
    unittest.main()
