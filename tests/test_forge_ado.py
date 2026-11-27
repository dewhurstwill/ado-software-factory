#!/usr/bin/env python3
"""Offline contract for the Azure DevOps tracker backend of super-board-forge.py.

A local fake Azure DevOps (tests/fixtures/ado/fake_ado_server.py) answers over loopback;
no network, no real `az` (a shim shadows it)."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/ado'
sys.path.insert(0, str(FIXTURES))
import fake_ado_server as fake_mod  # noqa: E402

FORGE = ROOT / 'scripts/super-board-forge.py'
CARD = ROOT / 'scripts/super-board-card.sh'
PLAN = ROOT / 'scripts/super-board-wave-plan.sh'
DEPS = ROOT / 'scripts/super-board-deps.sh'
WEF = fake_mod.WEF
PAT = 'pat-secret-1234567890'
JSON_PATCH = 'application/json-patch+json'

_spec = importlib.util.spec_from_file_location('github_read', ROOT / 'scripts/super-board-github-read.py')
github_read = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(github_read)


def basic(token):
    return 'Basic ' + base64.b64encode(f':{token}'.encode()).decode()


class AdoCase(unittest.TestCase):
    seven = True

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = fake_mod.Fake(seven=self.seven)
        self.server = fake_mod.start(self.fake)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        az = self.bin / 'az'
        az.write_text('#!/bin/sh\nif [ -n "$FAKE_AZ_TOKEN" ]; then echo "$FAKE_AZ_TOKEN"; exit 0; fi\nexit 1\n')
        az.chmod(0o755)
        self.halt = self.root / 'state/halt.json'
        self.cache = self.root / 'cache'
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('SB_CONFIG', 'AZURE_DEVOPS_EXT_PAT', 'SB_REPO_PATH', 'GH_REPO', 'FAKE_AZ_TOKEN')}
        self.env.update(PATH=f'{self.bin}:{os.environ["PATH"]}', SB_ADO_BASE_URL=self.url,
                        AZURE_DEVOPS_EXT_PAT=PAT, SB_GITHUB_HALT_FILE=str(self.halt),
                        SB_CARD_CACHE_DIR=str(self.cache), SB_GITHUB_RETRY_DELAY='0', SB_ADO_SLEEP_SCALE='0')
        self.ado = {'organization': 'https://dev.azure.com/example', 'project': 'Factory', 'team': 'Factory Team',
                    'board': 'Stories', 'body_fields': {'Bug': 'Microsoft.VSTS.TCM.ReproSteps'}}
        self.config_value = {'provider': 'azure-devops', 'ado': self.ado, 'repo': {'path': str(self.root)}}
        self.config = self.root / 'cfg.json'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def cfg(self):
        self.config.write_text(json.dumps(self.config_value))
        return str(self.config)

    def forge(self, *args, env=None, cfg=True):
        e = dict(self.env, **(env or {}))
        e = {k: v for k, v in e.items() if v is not None}
        flags = ['--config', self.cfg()] if cfg else []
        return subprocess.run([sys.executable, str(FORGE), *flags, *args], env=e, text=True,
                              stdin=subprocess.DEVNULL, capture_output=True, cwd=self.root)

    def ok(self, *args, **kw):
        r = self.forge(*args, **kw)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout

    def js(self, *args, **kw):
        return json.loads(self.ok(*args, **kw))

    def body_file(self, text):
        path = self.root / 'body.md'
        path.write_text(text)
        return str(path)

    def seed(self):
        """Ready (1), a claimed Bug in Building (2), a QA-labelled story in Review (3)."""
        self.fake.items[1]['fields']['System.Tags'] = 'feature; probe'
        self.fake.items[1]['col_id'] = self.fake.columns[1]['id']  # Ready
        self.fake.items[1]['state'] = 'New'
        b = self.fake.add('Bug', 'crash on save', 'steps', 'bug; sb:claimed-abc123', 'Building')
        s = self.fake.add('User Story', 'qa the thing', 'body', 'qa', 'Review')
        return b, s


class BoardTests(AdoCase):
    def test_board_ids(self):
        out = self.js('board', 'ids')
        self.assertEqual(out, {'board': 'Stories', 'columnField': WEF, 'columns': fake_mod.COLUMNS_SEVEN})

    def test_board_items_shape_uses_wef_and_hides_claim(self):
        self.seed()
        # a card with no column on this board is skipped; BoardColumn (another board's) is ignored
        orphan = self.fake.add('User Story', 'nowhere', '', '')
        self.fake.items[orphan]['col_id'] = None
        out = self.js('board', 'items')
        self.assertEqual(out['totalCount'], 3)
        by = {i['content']['number']: i for i in out['items']}
        self.assertEqual(by[1], {'id': '1', 'title': '[sandbox probe] column move test', 'status': 'Ready',
                                 'labels': ['feature', 'probe'],
                                 'content': {'type': 'Issue', 'number': 1, 'title': '[sandbox probe] column move test',
                                             'url': 'https://dev.azure.com/example/Factory/_workitems/edit/1',
                                             'repository': 'example/Factory', 'assignees': []}})
        self.assertEqual(by[2]['status'], 'Building')
        self.assertEqual(by[2]['labels'], ['bug'], 'claim tags are never labels')
        self.assertEqual(by[2]['content']['assignees'], [{'login': 'user@example.com'}])
        self.assertNotIn(orphan, by)
        github_read.validate(json.dumps(out), 'items')
        wiql = self.fake.reqs('POST', 'wiql')[0]['body']['query']
        self.assertIn("('Bug', 'User Story')", wiql)
        batch = self.fake.reqs('POST', 'workitemsbatch')[0]['body']
        self.assertIn(WEF, batch['fields'])
        self.assertEqual(batch['errorPolicy'], 'omit')

    def test_status_item_id_and_missing_card(self):
        self.seed()
        self.assertEqual(self.ok('board', 'status', '3'), 'Review\n')
        self.assertEqual(self.ok('board', 'item-id', '3'), '3\n')
        orphan = self.fake.add('User Story', 'nowhere')
        self.fake.items[orphan]['col_id'] = None
        self.assertEqual(self.forge('board', 'status', str(orphan)).returncode, 3)
        self.assertEqual(self.forge('board', 'status', '999').returncode, 3)

    def test_move_request_shape(self):
        self.seed()
        r = self.ok('board', 'move', '1', 'Building')
        self.assertEqual(r, 'moved #1 → Building\n')
        patch = self.fake.reqs('PATCH', r'workitems/1$')[0]
        self.assertEqual(patch['content_type'], JSON_PATCH)
        self.assertEqual(patch['body'], [{'op': 'test', 'path': '/rev', 'value': 1},
                                         {'op': 'add', 'path': f'/fields/{WEF}', 'value': 'Building'}])
        self.assertEqual(self.fake.column_of(1), 'Building')
        self.assertEqual(self.fake.items[1]['state'], 'Active')
        self.assertTrue(patch['auth'])

    def test_move_unknown_column_exits_3_after_one_refresh(self):
        r = self.forge('board', 'move', '1', 'Nope')
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(len(self.fake.reqs('GET', 'boards/Stories$')), 2, 'cache miss re-reads the board once')
        self.assertEqual(self.fake.reqs('PATCH'), [])

    def test_move_to_current_column_is_not_written(self):
        self.seed()
        self.ok('board', 'move', '3', 'Review')
        self.assertEqual(self.fake.reqs('PATCH'), [])

    def test_move_retries_a_stale_rev(self):
        def other_writer(fake, item):
            item['rev'] += 1
        self.fake.pre_patch = other_writer
        self.ok('board', 'move', '1', 'Building')
        patches = self.fake.reqs('PATCH')
        self.assertEqual([p['body'][0]['value'] for p in patches], [1, 2])
        self.assertEqual(self.fake.column_of(1), 'Building')


class StaleRevTests(AdoCase):
    def test_stale_rev_status_variants_on_a_replace(self):
        for opts in ({}, {'stale_status': 409, 'stale_message': 'TF26071: changed by someone else'},
                     {'stale_status': 400, 'stale_message': 'VS403351: Test operation failed'}):
            self.fake.requests.clear()
            self.fake.items[1]['fields']['System.Tags'] = 'feature; probe'
            for k, v in opts.items():
                setattr(self.fake, k, v)

            def other_writer(fake, item):
                item['fields']['System.Tags'] += '; other'
                item['rev'] += 1
            self.fake.pre_patch = other_writer
            self.ok('ticket', 'edit', '1', '--remove-label', 'probe')
            self.assertEqual(self.fake.tags(1), ['feature', 'other'], opts)
            patches = self.fake.reqs('PATCH')
            self.assertEqual(len(patches), 2, opts)
            self.assertEqual(patches[0]['body'][0]['op'], 'test')
            self.assertEqual(patches[1]['body'][0]['value'], patches[0]['body'][0]['value'] + 1)
            self.assertEqual(patches[1]['body'][1]['op'], 'replace')
            self.fake.items[1]['rev'] = 1
            self.fake.stale_status, self.fake.stale_message = 412, 'VS403351: Test operation failed: revision mismatch'

    def test_stale_rev_gives_up_after_three(self):
        def always(fake, item):
            item['rev'] += 1
            fake.pre_patch = always
        self.fake.pre_patch = always
        r = self.forge('ticket', 'edit', '1', '--remove-label', 'probe')
        self.assertEqual(r.returncode, 69)
        self.assertEqual(len(self.fake.reqs('PATCH')), 3)

    def test_a_non_stale_write_failure_is_not_retried(self):
        self.fake.inject.append({'match': lambda r: r['method'] == 'PATCH', 'times': 5, 'status': 400,
                                 'body': {'message': 'TF401000: bad field'}})
        r = self.forge('ticket', 'edit', '1', '--title', 'x')
        self.assertEqual(r.returncode, 69)
        self.assertEqual(len(self.fake.reqs('PATCH')), 1)

    def test_simultaneous_identical_writes_both_succeed_and_different_ones_conflict(self):
        # service point 4: the rev test is not atomic with the save
        def rival_same(fake, item):
            item['fields']['System.Tags'] = 'feature'
            item['rev'] += 1
        self.fake.simultaneous = rival_same
        self.ok('ticket', 'edit', '1', '--remove-label', 'probe')
        self.assertEqual(self.fake.tags(1), ['feature'])
        self.assertEqual(len(self.fake.reqs('PATCH')), 1, 'identical result: 200 no-op, no retry')
        self.fake.requests.clear()
        self.fake.items[1]['fields']['System.Tags'] = 'feature; probe'

        def rival_other(fake, item):
            item['fields']['System.Tags'] = 'feature; probe; x'
            item['rev'] += 1
        self.fake.simultaneous = rival_other
        self.ok('ticket', 'edit', '1', '--remove-label', 'probe')
        self.assertEqual(len(self.fake.reqs('PATCH')), 2, '409 TF26071 is retried after a re-read')
        self.assertEqual(self.fake.tags(1), ['feature', 'x'])


class TagSemanticsTests(AdoCase):
    def test_remove_label_really_removes(self):
        self.ok('ticket', 'edit', '1', '--remove-label', 'probe')
        self.assertEqual(self.fake.tags(1), ['feature'])
        body = self.fake.reqs('PATCH')[0]['body']
        self.assertEqual(body, [{'op': 'test', 'path': '/rev', 'value': 1},
                                {'op': 'replace', 'path': '/fields/System.Tags', 'value': 'feature'}])

    def test_removing_the_last_tag_uses_remove(self):
        self.fake.items[1]['fields']['System.Tags'] = 'probe'
        self.ok('ticket', 'edit', '1', '--remove-label', 'probe')
        self.assertEqual(self.fake.tags(1), [])
        self.assertEqual(self.fake.reqs('PATCH')[0]['body'][1], {'op': 'remove', 'path': '/fields/System.Tags'})

    def test_add_label_sends_only_the_new_tags_without_a_rev_test(self):
        self.ok('ticket', 'edit', '1', '--add-label', 'extra', '--add-label', 'probe')
        self.assertEqual(self.fake.reqs('PATCH')[0]['body'],
                         [{'op': 'add', 'path': '/fields/System.Tags', 'value': 'extra'}])
        self.assertEqual(self.fake.tags(1), ['feature', 'probe', 'extra'])

    def test_add_label_makes_no_call_when_present(self):
        self.ok('ticket', 'edit', '1', '--add-label', 'PROBE')
        self.assertEqual(self.fake.reqs('PATCH'), [])

    def test_add_and_remove_together_is_one_rev_tested_replace(self):
        self.ok('ticket', 'edit', '1', '--add-label', 'bug', '--remove-label', 'probe')
        self.assertEqual(self.fake.tags(1), ['feature', 'bug'])
        self.assertEqual(self.fake.reqs('PATCH')[0]['body'][0]['op'], 'test')

    def test_close_not_planned_adds_only_its_tag(self):
        self.ok('ticket', 'close', '1', '--reason', 'not-planned')
        tag_patch = self.fake.reqs('PATCH')[0]['body']
        self.assertEqual(tag_patch, [{'op': 'add', 'path': '/fields/System.Tags', 'value': 'not-planned'}])
        self.assertEqual(self.fake.tags(1), ['feature', 'probe', 'not-planned'])


class ClaimTests(AdoCase):
    def claim(self, holder, n='1'):
        return self.forge('ticket', 'claim', n, env={'SB_CLAIM_HOLDER': holder})

    def claims(self, n=1):
        return [t for t in self.fake.tags(n) if t.startswith('sb:claimed')]

    def test_claim_adds_a_holder_tag_with_a_plain_merge_add(self):
        self.assertEqual(self.claim('aaaaaa').returncode, 0)
        self.assertEqual(self.fake.reqs('PATCH')[0]['body'],
                         [{'op': 'add', 'path': '/fields/System.Tags', 'value': 'sb:claimed-aaaaaa'}])
        self.assertFalse(any('AssignedTo' in op['path'] for op in self.fake.reqs('PATCH')[0]['body']))
        self.assertEqual(self.claims(), ['sb:claimed-aaaaaa'])

    def test_a_default_holder_is_stable_six_hex(self):
        self.ok('ticket', 'claim', '1', env={'SB_CLAIM_HOLDER': None})
        first = self.claims()
        self.assertRegex(first[0], r'^sb:claimed-[0-9a-f]{6}$')
        self.ok('ticket', 'release', '1', env={'SB_CLAIM_HOLDER': None})
        self.assertEqual(self.claims(), [])
        self.ok('ticket', 'claim', '1', env={'SB_CLAIM_HOLDER': None})
        self.assertEqual(self.claims(), first)

    def test_same_holder_reclaim_is_idempotent(self):
        self.assertEqual(self.claim('aaaaaa').returncode, 0)
        patches = len(self.fake.reqs('PATCH'))
        self.assertEqual(self.claim('aaaaaa').returncode, 0)
        self.assertEqual(len(self.fake.reqs('PATCH')), patches)

    def test_another_holders_claim_is_never_stolen(self):
        self.claim('bbbbbb')
        patches = len(self.fake.reqs('PATCH'))
        r = self.claim('aaaaaa')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(len(self.fake.reqs('PATCH')), patches)
        self.assertEqual(self.claims(), ['sb:claimed-bbbbbb'])

    def test_legacy_plain_claim_tag_counts_as_claimed(self):
        self.fake.items[1]['fields']['System.Tags'] = 'feature; sb:claimed'
        self.assertEqual(self.claim('aaaaaa').returncode, 4)

    def test_two_concurrent_claimers_with_different_holders(self):
        results = {}

        def run(h):
            results[h] = self.claim(h).returncode
        threads = [threading.Thread(target=run, args=(h,)) for h in ('aaaaaa', 'bbbbbb')]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results.values()), [0, 4], results)
        winner = [h for h, rc in results.items() if rc == 0][0]
        self.assertEqual(self.claims(), [f'sb:claimed-{winner}'])

    def test_simultaneous_adds_both_land_and_the_smaller_tag_wins(self):
        def rival(fake, item):  # the rival's add lands before ours, after our read
            item['fields']['System.Tags'] += '; sb:claimed-aaaaaa'
            item['rev'] += 1
        self.fake.pre_patch = rival
        r = self.claim('bbbbbb')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(self.claims(), ['sb:claimed-aaaaaa'], 'the loser took its own tag back')
        self.assertEqual(self.fake.tags(1), ['feature', 'probe', 'sb:claimed-aaaaaa'])
        withdraw = self.fake.reqs('PATCH')[-1]['body']
        self.assertEqual(withdraw[0]['op'], 'test')
        self.assertEqual(withdraw[1]['op'], 'replace')

    def test_simultaneous_adds_the_smaller_holder_keeps_its_claim(self):
        def rival(fake, item):
            item['fields']['System.Tags'] += '; sb:claimed-bbbbbb'
            item['rev'] += 1
        self.fake.pre_patch = rival
        self.assertEqual(self.claim('aaaaaa').returncode, 0)
        self.assertIn('sb:claimed-aaaaaa', self.fake.tags(1))

    def test_claim_survives_an_unrelated_concurrent_edit(self):
        def other(fake, item):
            item['fields']['System.Tags'] += '; other'
            item['rev'] += 1
        self.fake.pre_patch = other
        self.assertEqual(self.claim('aaaaaa').returncode, 0)
        self.assertEqual(self.fake.tags(1), ['feature', 'probe', 'other', 'sb:claimed-aaaaaa'])

    def test_release_removes_only_our_claim(self):
        self.fake.items[1]['fields']['System.Tags'] = 'feature; sb:claimed-bbbbbb'
        out = self.ok('ticket', 'release', '1', env={'SB_CLAIM_HOLDER': 'aaaaaa'})
        self.assertIn('nothing held', out)
        self.assertEqual(self.fake.reqs('PATCH'), [])
        self.claim('bbbbbb')  # already theirs: idempotent
        self.ok('ticket', 'release', '1', env={'SB_CLAIM_HOLDER': 'bbbbbb'})
        self.assertEqual(self.fake.tags(1), ['feature'])
        self.ok('ticket', 'release', '1', env={'SB_CLAIM_HOLDER': 'bbbbbb'})
        self.assertEqual(len(self.fake.reqs('PATCH')), 1, 'second release is a no-op')

    def test_release_force_reaps_every_claim(self):
        self.fake.items[1]['fields']['System.Tags'] = 'feature; sb:claimed-bbbbbb; sb:claimed; sb:claimed-cccccc'
        out = self.ok('ticket', 'release', '1', '--force', env={'SB_CLAIM_HOLDER': 'aaaaaa'})
        self.assertIn('sb:claimed-bbbbbb', out)
        self.assertEqual(self.fake.tags(1), ['feature'])

    def test_claim_tags_are_hidden_and_shown_as_assignees(self):
        self.claim('aaaaaa')
        view = self.js('ticket', 'view', '1')
        self.assertEqual(view['assignees'], [{'login': 'user@example.com'}])
        self.assertEqual(view['labels'], [{'name': 'feature'}, {'name': 'probe'}])
        self.fake.items[1]['col_id'] = self.fake.columns[1]['id']
        by = {i['content']['number']: i for i in self.js('board', 'items')['items']}
        self.assertEqual(by[1]['labels'], ['feature', 'probe'])
        self.assertEqual(by[1]['content']['assignees'], [{'login': 'user@example.com'}])
        self.ok('ticket', 'release', '1', env={'SB_CLAIM_HOLDER': 'aaaaaa'})
        self.assertEqual(self.js('ticket', 'view', '1')['assignees'], [])


class TicketTests(AdoCase):
    def test_view_and_states(self):
        view = self.js('ticket', 'view', '1')
        self.assertEqual(view, {'number': 1, 'title': '[sandbox probe] column move test',
                                'body': '\n## Problem\nProbe ticket.\n\n## Blocked by\n- None.\n',
                                'state': 'OPEN', 'stateReason': None,
                                'labels': [{'name': 'feature'}, {'name': 'probe'}], 'assignees': [],
                                'url': 'https://dev.azure.com/example/Factory/_workitems/edit/1'})
        github_read.validate(json.dumps(view), 'issue')
        self.assertEqual(self.forge('ticket', 'view', '999').returncode, 3)

    def test_close_reasons(self):
        a = self.fake.add('User Story', 'a')
        b = self.fake.add('User Story', 'b')
        self.ok('ticket', 'close', str(a), '--reason', 'completed')
        self.ok('ticket', 'close', str(b), '--reason', 'not-planned')
        self.assertEqual(self.fake.column_of(a), 'Done')
        self.assertEqual(self.fake.items[a]['state'], 'Closed')
        self.assertIn('not-planned', self.fake.tags(b))
        self.assertNotIn('not-planned', self.fake.tags(a))
        va, vb = self.js('ticket', 'view', str(a)), self.js('ticket', 'view', str(b))
        self.assertEqual((va['state'], va['stateReason']), ('CLOSED', 'COMPLETED'))
        self.assertEqual((vb['state'], vb['stateReason']), ('CLOSED', 'NOT_PLANNED'))
        removed = self.fake.add('User Story', 'c')
        self.fake.items[removed]['state'] = 'Removed'
        self.assertEqual(self.js('ticket', 'view', str(removed))['stateReason'], 'NOT_PLANNED')

    def test_list_states_and_limit(self):
        done = self.fake.add('User Story', 'done one')
        self.ok('ticket', 'close', str(done), '--reason', 'completed')
        open_list = self.js('ticket', 'list', '--state', 'open', '--limit', '10')
        self.assertEqual([t['number'] for t in open_list], [1])
        self.assertEqual([t['number'] for t in self.js('ticket', 'list', '--state', 'closed', '--limit', '10')], [done])
        self.assertEqual([t['number'] for t in self.js('ticket', 'list', '--state', 'all', '--limit', '10')], [done, 1])
        self.assertEqual(len(self.js('ticket', 'list', '--state', 'all', '--limit', '1')), 1)
        self.assertEqual(set(open_list[0]), {'number', 'title', 'body', 'state', 'stateReason', 'labels', 'assignees', 'url'})

    def test_history_includes_every_state(self):
        done = self.fake.add('User Story', 'done one', 'old body')
        self.ok('ticket', 'close', str(done), '--reason', 'completed')
        hist = self.js('ticket', 'history')
        self.assertEqual([(h['number'], h['state']) for h in hist], [(1, 'OPEN'), (done, 'CLOSED')])
        self.assertEqual(hist[1]['body'], 'old body')
        github_read.validate(json.dumps(hist), 'dedupe')

    def test_create_story_and_bug(self):
        text = '## Problem\nIt breaks.\n\n## Blocked by\n- #1\n'
        out = self.ok('ticket', 'create', '--title', 'T', '--body-file', self.body_file(text), '--label', 'feature').splitlines()
        n = int(out[-1])
        self.assertEqual(out[-2], f'https://dev.azure.com/example/Factory/_workitems/edit/{n}')
        post = self.fake.reqs('POST', r'workitems/\$')[0]
        self.assertEqual(post['path'], '/Factory/_apis/wit/workitems/$User Story')
        self.assertEqual(post['content_type'], JSON_PATCH)
        self.assertEqual(post['body'], [
            {'op': 'add', 'path': '/fields/System.Title', 'value': 'T'},
            {'op': 'add', 'path': '/fields/System.Description', 'value': text},
            {'op': 'add', 'path': '/fields/System.Tags', 'value': 'feature'},
            {'op': 'add', 'path': '/multilineFieldsFormat/System.Description', 'value': 'Markdown'}])
        out = self.ok('ticket', 'create', '--title', 'B', '--body-file', self.body_file('steps\n'),
                      '--label', 'bug', '--label', 'extra', '--column', 'Ready').splitlines()
        bug = int(out[-1])
        post = self.fake.reqs('POST', r'workitems/\$')[1]
        self.assertEqual(post['path'], '/Factory/_apis/wit/workitems/$Bug')
        self.assertEqual(post['body'][1]['path'], '/fields/Microsoft.VSTS.TCM.ReproSteps')
        self.assertEqual(post['body'][2]['value'], 'bug; extra')
        self.assertEqual(self.fake.column_of(bug), 'Ready')
        self.assertEqual(self.js('ticket', 'view', str(bug))['body'], 'steps\n')
        self.assertEqual(self.forge('ticket', 'create', '--title', 'x', '--body-file', self.body_file('b'),
                                    '--column', 'Nope').returncode, 3)
        self.assertEqual(len(self.fake.reqs('POST', r'workitems/\$')), 2, 'no work item is created for a bad column')

    def test_bug_body_field_defaults_to_repro_steps_and_explicit_config_wins(self):
        del self.ado['body_fields']
        n = int(self.ok('ticket', 'create', '--title', 'B', '--body-file', self.body_file('steps'),
                        '--label', 'bug').splitlines()[-1])
        post = self.fake.reqs('POST', r'workitems/\$')[0]['body']
        self.assertEqual(post[1]['path'], '/fields/Microsoft.VSTS.TCM.ReproSteps')
        self.assertEqual(post[-1], {'op': 'add', 'path': '/multilineFieldsFormat/Microsoft.VSTS.TCM.ReproSteps',
                                    'value': 'Markdown'})
        self.assertEqual(self.js('ticket', 'view', str(n))['body'], 'steps')
        self.ado['body_fields'] = {}
        self.ok('ticket', 'create', '--title', 'B2', '--body-file', self.body_file('x'), '--label', 'bug')
        self.assertEqual(self.fake.reqs('POST', r'workitems/\$')[1]['body'][1]['path'], '/fields/System.Description')

    def test_read_falls_back_to_description_when_the_type_field_is_empty(self):
        del self.ado['body_fields']
        old = self.fake.add('Bug', 'hand made', '', '')
        self.fake.items[old]['fields'] = {'System.Title': 'hand made', 'System.Description': 'in description'}
        self.assertEqual(self.js('ticket', 'view', str(old))['body'], 'in description')
        edit = self.fake.add('Bug', 'edit me', 'old', '')
        self.ok('ticket', 'edit', str(edit), '--body-file', self.body_file('new'))
        patch = self.fake.reqs('PATCH')[0]['body']
        self.assertIn({'op': 'add', 'path': '/fields/Microsoft.VSTS.TCM.ReproSteps', 'value': 'new'}, patch)

    def test_create_default_type_follows_feature(self):
        self.ado['work_item_types'] = {'feature': 'Bug'}
        self.ok('ticket', 'create', '--title', 'T', '--body-file', self.body_file('b'))
        self.assertTrue(self.fake.reqs('POST', r'workitems/\$')[0]['path'].endswith('$Bug'))

    def test_edit(self):
        self.ok('ticket', 'edit', '1', '--title', 'New title', '--body-file', self.body_file('new body'),
                '--add-label', 'bug', '--remove-label', 'probe')
        patch = self.fake.reqs('PATCH')[0]['body']
        self.assertEqual(patch[0], {'op': 'test', 'path': '/rev', 'value': 1})
        self.assertIn({'op': 'add', 'path': '/fields/System.Title', 'value': 'New title'}, patch)
        self.assertIn({'op': 'add', 'path': '/fields/System.Description', 'value': 'new body'}, patch)
        self.assertIn({'op': 'add', 'path': '/multilineFieldsFormat/System.Description', 'value': 'Markdown'}, patch)
        self.assertEqual(self.fake.tags(1), ['feature', 'bug'])
        self.assertEqual(self.fake.reqs('PATCH')[0]['content_type'], JSON_PATCH)

    def test_comments_round_trip(self):
        self.assertEqual(self.js('ticket', 'comments', '1'), [[]])
        cid = self.ok('ticket', 'comment', '1', '--body-file', self.body_file('first\nline two')).strip()
        post = self.fake.reqs('POST', 'comments')[0]
        self.assertEqual(post['query'], {'format': ['markdown'], 'api-version': ['7.1-preview.4']})
        self.assertEqual(post['body'], {'text': 'first\nline two'})
        self.ok('ticket', 'comment', '1', '--body-file', self.body_file('second'))
        self.ok('ticket', 'comment-edit', '1', cid, '--body-file', self.body_file('first, edited'))
        edit = self.fake.reqs('PATCH', 'comments')[0]
        self.assertTrue(edit['path'].endswith(f'/comments/{cid}'))
        self.assertEqual(edit['query']['format'], ['markdown'])
        pages = self.js('ticket', 'comments', '1')
        self.assertEqual(len(pages), 1)
        self.assertEqual([c['body'] for c in pages[0]], ['first, edited', 'second'])
        self.assertEqual(pages[0][0]['user'], {'login': 'user@example.com', 'type': 'User'})
        self.assertEqual(pages[0][0]['id'], int(cid))
        self.assertTrue(pages[0][0]['created_at'] and pages[0][0]['updated_at'])
        github_read.validate(json.dumps(pages), 'comments')

    def test_comments_follow_the_continuation_token(self):
        self.fake.page_size = 2
        for i in range(5):
            self.ok('ticket', 'comment', '1', '--body-file', self.body_file(f'c{i}'))
        pages = self.js('ticket', 'comments', '1')
        self.assertEqual([c['body'] for c in pages[0]], [f'c{i}' for i in range(5)])
        gets = self.fake.reqs('GET', 'comments')
        self.assertEqual(len(gets), 3)
        self.assertEqual(gets[0]['query']['$top'], ['200'])
        self.assertEqual(gets[0]['query']['order'], ['asc'])

    def test_graph_feeds_deps(self):
        two = self.fake.add('User Story', 'second', '## Blocked by\n- #1\n')
        three = self.fake.add('User Story', 'third', '## Blocked by\n- None.\n')
        self.ok('ticket', 'close', str(three), '--reason', 'completed')
        for i in range(10):
            self.ok('ticket', 'comment', '1', '--body-file', self.body_file(f'note {i}'))
        graph = self.js('ticket', 'graph')
        self.assertEqual([t['number'] for t in graph], [1, two], 'open tickets only')
        t1 = graph[0]
        self.assertEqual(set(t1), {'number', 'title', 'body', 'state', 'labels', 'comments'})
        self.assertEqual(t1['labels'], {'nodes': [{'name': 'feature'}, {'name': 'probe'}]})
        self.assertEqual([c['body'] for c in t1['comments']['nodes']], [f'note {i}' for i in range(2, 10)], 'last 8')
        self.assertEqual(len(self.fake.reqs('GET', 'comments')), 2, 'one comments call per open ticket')
        saved = self.root / 'graph.json'
        saved.write_text(json.dumps(graph))
        deps = json.loads(subprocess.run(['bash', str(DEPS), '--repo', 'x/y', '--from', str(saved)], text=True,
                                         capture_output=True, check=True, env=self.env).stdout)
        self.assertTrue(deps['1']['runnable'])
        self.assertEqual(deps[str(two)]['blockers'], [1])
        self.assertEqual(deps[str(two)]['openBlockers'], [1])
        self.assertFalse(deps[str(two)]['runnable'])


class DepsConfigTests(AdoCase):
    def test_deps_with_an_ado_config_needs_no_repo_flag(self):
        self.fake.add('User Story', 'second', '## Blocked by\n- #1\n')
        r = subprocess.run(['bash', str(DEPS), '--config', self.cfg()], env=self.env, text=True, capture_output=True,
                           cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        deps = json.loads(r.stdout)
        self.assertTrue(deps['1']['runnable'])
        self.assertEqual(deps['2']['openBlockers'], [1])

    def test_deps_still_requires_repo_for_github_without_a_config(self):
        r = subprocess.run(['bash', str(DEPS)], env=self.env, text=True, capture_output=True, cwd=self.root)
        self.assertEqual(r.returncode, 64)


class EnsureTests(AdoCase):
    seven = False

    def columns(self):
        return [(c['name'], c['columnType'], c['itemLimit'], c['stateMappings']) for c in self.fake.columns]

    def expect_seven(self):
        done = {'User Story': 'Closed', 'Bug': 'Closed'}
        new = {'User Story': 'New', 'Bug': 'New'}
        active = {'User Story': 'Active', 'Bug': 'Active'}
        self.assertEqual(self.columns(), [
            ('Backlog', 'incoming', 0, new), ('Ready', 'inProgress', 0, new), ('Building', 'inProgress', 0, active),
            ('QA', 'inProgress', 0, active), ('Review', 'inProgress', 0, active),
            ('Blocked', 'inProgress', 0, active), ('Done', 'outgoing', 0, done)])

    def test_default_agile_board_becomes_the_seven_columns_and_is_then_a_noop(self):
        before = {c['name']: c['id'] for c in self.fake.columns}
        out = self.ok('board', 'ensure').splitlines()
        self.assertIn('changed: bugsBehavior asTasks -> asRequirements', out)
        self.assertIn('changed: columns: rename "New" -> "Backlog"', out)
        self.assertIn('changed: columns: create "Ready"', out)
        self.assertTrue(all(l.startswith('changed: ') for l in out), out)
        self.expect_seven()
        ids = {c['name']: c['id'] for c in self.fake.columns}
        self.assertEqual((ids['Backlog'], ids['Building'], ids['Review'], ids['Done']),
                         (before['New'], before['Active'], before['Resolved'], before['Closed']),
                         'existing columns are renamed in place so cards keep their column')
        put = self.fake.reqs('PUT')[0]
        self.assertEqual(put['query'], {'api-version': ['7.1']})
        self.assertEqual([('id' in c) for c in put['body']], [True, False, True, False, True, False, True])
        self.assertEqual(self.fake.reqs('PATCH', 'teamsettings')[0]['body'], {'bugsBehavior': 'asRequirements'})
        writes = len(self.fake.reqs('PUT')) + len(self.fake.reqs('PATCH'))
        again = self.ok('board', 'ensure')
        self.assertEqual(again, 'board ok: seven columns, bugs on the board\n')
        self.assertEqual(len(self.fake.reqs('PUT')) + len(self.fake.reqs('PATCH')), writes, 'second run writes nothing')
        self.assertEqual(self.ok('board', 'ensure', '--check'), 'board ok: seven columns, bugs on the board\n')

    def test_check_reports_and_exits_1_without_writing(self):
        r = self.forge('board', 'ensure', '--check')
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('needed: bugsBehavior asTasks -> asRequirements', r.stdout)
        self.assertIn('needed: columns: create "Ready"', r.stdout)
        self.assertEqual(self.fake.reqs('PUT') + self.fake.reqs('PATCH'), [])
        self.assertEqual(self.fake.bugs, 'asTasks')

    def test_cards_survive_and_board_items_work_afterwards(self):
        self.fake.items[1]['col_id'] = self.fake.columns[1]['id']  # Active
        self.fake.items[1]['state'] = 'Active'
        self.ok('board', 'ensure')
        self.assertEqual(self.ok('board', 'status', '1'), 'Building\n')

    def test_refuses_to_delete_a_column_that_holds_cards(self):
        extra = [{'id': f'x{i}', 'name': n, 'columnType': 'inProgress', 'itemLimit': 5, 'isSplit': False,
                  'stateMappings': {'User Story': s, 'Bug': s}}
                 for i, (n, s) in enumerate([('M1', 'Active'), ('M2', 'Active'), ('M3', 'Active'), ('Extra', 'Resolved')])]
        self.fake.columns[2:2] = extra
        self.fake.bugs = 'asRequirements'
        for c in self.fake.columns:
            c['stateMappings'].setdefault('Bug', c['stateMappings']['User Story'])
        stuck = next(c['id'] for c in self.fake.columns if c['name'] == 'Resolved')
        self.fake.items[1]['col_id'] = stuck
        r = self.forge('board', 'ensure')
        self.assertEqual(r.returncode, 65, r.stderr)
        self.assertIn('"Resolved"', r.stderr)
        self.assertEqual(self.fake.reqs('PUT'), [])
        self.fake.items[1]['col_id'] = 'x0'
        out = self.ok('board', 'ensure')
        self.assertIn('columns: remove "Resolved"', out)
        self.assertEqual([c[0] for c in self.columns()], fake_mod.COLUMNS_SEVEN)


class TransportTests(AdoCase):
    def test_429_honours_retry_after_with_a_cap(self):
        self.fake.inject.append({'match': lambda r: 'boards' in r['path'], 'times': 1, 'status': 429,
                                 'headers': {'Retry-After': '7'}, 'body': {'message': 'slow down'}})
        r = self.forge('board', 'ids')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('waiting 7s', r.stderr)
        self.fake.inject.append({'match': lambda r: 'boards' in r['path'], 'times': 1, 'status': 429,
                                 'headers': {'Retry-After': '600'}, 'body': {}})
        self.cache.joinpath('x').mkdir(parents=True, exist_ok=True)
        r = self.forge('board', 'ids', env={'SB_CARD_CACHE_DIR': str(self.root / 'cache2')})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('waiting 60s', r.stderr)

    def test_401_halts_on_the_first_attempt(self):
        self.fake.token_ok = basic('other')
        r = self.forge('board', 'items')
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertTrue(self.halt.exists())
        self.assertIn('Azure DevOps', r.stderr)
        self.assertNotIn('GitHub', r.stderr)
        again = self.forge('check')
        self.assertEqual(again.returncode, 79)
        self.assertIn('Azure DevOps', again.stderr)
        self.assertEqual(len(self.fake.requests), 1, 'no request once halted')
        self.assertEqual(self.forge('board', 'items').returncode, 79)

    def test_404_on_a_fixed_resource_halts(self):
        self.fake.items.clear()
        self.ado['board'] = 'Missing'
        r = self.forge('board', 'ids')
        self.assertEqual(r.returncode, 79)
        self.assertEqual(len(self.fake.requests), 1)

    def test_three_500s_halt(self):
        self.fake.inject.append({'match': lambda r: True, 'times': 10, 'status': 500, 'body': {'message': 'boom'}})
        r = self.forge('board', 'ids')
        self.assertEqual(r.returncode, 79)
        self.assertEqual(len(self.fake.requests), 3)
        self.assertIn('retry 2/3', r.stderr)
        self.assertIn('retry 3/3', r.stderr)
        self.assertIn('3 failed attempts', json.loads(self.halt.read_text())['reason'])

    def test_a_shape_that_never_validates_halts(self):
        self.fake.inject.append({'match': lambda r: 'boards' in r['path'], 'times': 10, 'status': 200, 'body': {'oops': 1}})
        r = self.forge('board', 'ids')
        self.assertEqual(r.returncode, 79)
        self.assertEqual(len(self.fake.requests), 3)

    def test_a_flaky_read_recovers(self):
        self.fake.inject.append({'match': lambda r: 'boards' in r['path'], 'times': 2, 'status': 502, 'body': {}})
        self.assertEqual(self.forge('board', 'ids').returncode, 0)
        self.assertFalse(self.halt.exists())

    def test_halt_verb_records_and_check_reports(self):
        self.assertEqual(self.forge('halt', 'manual stop').returncode, 79)
        self.assertEqual(json.loads(self.halt.read_text())['reason'], 'manual stop')
        self.assertEqual(self.forge('check').returncode, 79)

    def test_writes_are_not_retried_on_server_errors(self):
        self.fake.inject.append({'match': lambda r: r['method'] == 'POST' and 'comments' in r['path'], 'times': 5,
                                 'status': 500, 'body': {'message': 'boom'}})
        r = self.forge('ticket', 'comment', '1', '--body-file', self.body_file('hi'))
        self.assertEqual(r.returncode, 69)
        self.assertEqual(len(self.fake.reqs('POST', 'comments')), 1)

    def test_quota_header_is_saved(self):
        self.assertEqual(self.js('quota'), {'remaining': None, 'limit': None, 'reset': None, 'at': None, 'seen': False})
        self.fake.extra_headers = {'X-RateLimit-Remaining': '7', 'X-RateLimit-Limit': '200', 'X-RateLimit-Reset': '1767000000'}
        self.ok('board', 'ids')
        saved = json.loads((self.halt.parent / 'ado-quota.json').read_text())
        self.assertEqual((saved['remaining'], saved['limit'], saved['reset']), (7, 200, 1767000000))
        self.assertIsInstance(saved['at'], int)
        out = self.js('quota')
        self.assertEqual((out['remaining'], out['seen']), (7, True))

    def test_quota_records_cost_when_there_is_no_remaining_header(self):
        self.fake.extra_headers = {'X-RateLimit-Cost': '3'}
        self.ok('board', 'ids')
        self.ok('whoami')
        out = self.js('quota')
        self.assertEqual((out['cost'], out['total_cost'], out['remaining']), (3, 6, None))

    def test_api_versions(self):
        self.seed()
        self.ok('board', 'items')
        self.ok('ticket', 'comments', '1')
        self.ok('whoami')
        versions = {(r['path'].split('/')[-1] if 'comments' in r['path'] else 'x'): r['query']['api-version'][0]
                    for r in self.fake.requests}
        self.assertEqual(versions['comments'], '7.1-preview.4')
        ident = self.fake.reqs('GET', 'connectionData')[0]
        self.assertEqual(ident['query']['api-version'], ['7.1-preview'])
        for r in self.fake.requests:
            if 'comments' not in r['path'] and 'connectionData' not in r['path']:
                self.assertEqual(r['query']['api-version'], ['7.1'], r['path'])

    def test_whoami_repo_provider(self):
        self.assertEqual(self.js('whoami'), {'login': 'user@example.com', 'id': '00000000-0000-0000-0000-000000000001'})
        self.assertEqual(self.ok('repo'), 'example/Factory/Factory\n')
        self.assertEqual(self.ok('repo', '--explicit'), 'example/Factory/Factory\n')
        self.ado['repository'] = 'Repo One'
        self.assertEqual(self.ok('repo'), 'example/Factory/Repo One\n')
        self.assertEqual(self.ok('provider'), 'azure-devops\n')

    def test_team_defaults_to_project_team_and_names_are_encoded(self):
        del self.ado['team']
        self.ok('board', 'ids')
        self.assertIn('/Factory/Factory Team/_apis/work/boards/Stories', self.fake.requests[0]['path'])

    def test_missing_ado_block_is_a_config_error(self):
        del self.config_value['ado']
        self.assertEqual(self.forge('board', 'ids').returncode, 66)

    def test_pr_verbs_are_still_unavailable(self):
        r = self.forge('pr', 'head', '1')
        self.assertEqual(r.returncode, 69)
        self.assertIn('azure-devops provider: pr head is not implemented yet', r.stderr)


class AuthTests(AdoCase):
    def sent(self):
        return {r['authorization'] for r in self.fake.requests}

    def assert_no_token_leak(self, token, *results):
        for r in results:
            self.assertNotIn(token, r.stdout + r.stderr)
        for path in list(self.root.rglob('*')):
            if path.is_file() and path.name not in ('.env', 'az', 'cfg.json'):
                self.assertNotIn(token, path.read_text(errors='replace'), str(path))

    def test_env_pat_wins_over_env_file_and_az(self):
        (self.root / '.env').write_text('AZURE_DEVOPS_EXT_PAT=from-file\n')
        r = self.forge('board', 'ids', env={'FAKE_AZ_TOKEN': 'from-az'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sent(), {basic(PAT)})
        self.assert_no_token_leak(PAT, r)

    def test_env_file_is_second(self):
        (self.root / '.env').write_text('OTHER=1\nexport AZURE_DEVOPS_EXT_PAT="from-file"\n')
        r = self.forge('board', 'ids', env={'AZURE_DEVOPS_EXT_PAT': None, 'FAKE_AZ_TOKEN': 'from-az'})
        self.assertEqual(self.sent(), {basic('from-file')})
        self.assert_no_token_leak('from-file', r)

    def test_az_token_is_a_bearer(self):
        r = self.forge('board', 'ids', env={'AZURE_DEVOPS_EXT_PAT': None, 'FAKE_AZ_TOKEN': 'az-token-xyz'})
        self.assertEqual(self.sent(), {'Bearer az-token-xyz'})
        self.assert_no_token_leak('az-token-xyz', r)

    def test_no_credentials_exits_69_naming_the_options(self):
        r = self.forge('board', 'ids', env={'AZURE_DEVOPS_EXT_PAT': None})
        self.assertEqual(r.returncode, 69)
        for needle in ('AZURE_DEVOPS_EXT_PAT', '.env', 'az login'):
            self.assertIn(needle, r.stderr)
        self.assertEqual(self.fake.requests, [])


class WavePlanTests(AdoCase):
    def test_wave_plan_end_to_end_against_the_fake(self):
        free = 1
        blocked = self.fake.add('User Story', 'blocked one', '## Blocked by\n- #1\n', 'feature', 'Ready')
        claimed = self.fake.add('User Story', 'claimed one', '## Blocked by\n- None.\n', 'feature; sb:claimed-abc123', 'Ready')
        review = self.fake.add('User Story', 'in review', '## Blocked by\n- None.\n', 'feature', 'Review')
        qa = self.fake.add('User Story', 'qa one', '## Blocked by\n- None.\n', 'qa', 'Backlog')
        done = self.fake.add('User Story', 'finished', '', '', 'Done')
        self.fake.items[free]['col_id'] = self.fake.columns[1]['id']
        self.fake.items[free]['fields']['System.Description'] = '## Blocked by\n- None.\n'
        cfg = self.root / 'wave.json'
        cfg.write_text(json.dumps({**self.config_value, 'version': 1, 'variant': 'full', 'max_workers': 0,
                                   'verify_commands': ['git diff --check']}))
        env = dict(self.env)
        r = subprocess.run(['bash', str(PLAN), '--config', str(cfg)], env=env, text=True, capture_output=True,
                           cwd=self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual([(c['number'], c['status'], c['lane']) for c in plan['cards']],
                         [(review, 'Review', 'review'), (free, 'Ready', 'build')])
        self.assertEqual(plan['sweep'], [])
        self.assertEqual(plan['stranded'], [])
        numbers = {c['number'] for c in plan['cards']}
        self.assertNotIn(claimed, numbers, 'a claimed card is not planned')
        self.assertNotIn(blocked, numbers, 'a card blocked by an open ticket is not planned')
        self.assertNotIn(done, numbers)
        # the planner reached ADO through card.sh and the forge; no GitHub call was possible (no gh on PATH shim)
        self.assertTrue(self.fake.reqs('POST', 'wiql'))

    def test_card_sh_hands_an_ado_config_to_the_forge(self):
        self.seed()
        r = subprocess.run(['bash', str(CARD), '--config', self.cfg(), 'move', '1', 'Building'], env=self.env,
                           text=True, capture_output=True, cwd=self.root)
        self.assertEqual((r.returncode, r.stdout), (0, 'moved #1 → Building\n'), r.stderr)
        self.assertEqual(self.fake.column_of(1), 'Building')


if __name__ == '__main__':
    unittest.main()
