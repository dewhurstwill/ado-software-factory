#!/usr/bin/env python3
"""The three ticket filers on an `azure-devops` config, against the fake Azure DevOps.

super-qa-file-bug.sh, super-review-file-refactor.sh and super-collect-file.sh run unchanged
(no source edits) over loopback. The fake strips HTML comments from work item bodies and
comments on save (what the live service does), so the fingerprint tests prove nothing hidden is
relied on. A `gh` shim that fails and logs proves GitHub is never called."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase, WEF  # noqa: E402,F401

QA = ROOT / 'scripts/super-qa-file-bug.sh'
REVIEW = ROOT / 'scripts/super-review-file-refactor.sh'
COLLECT = ROOT / 'skills/super-collect/scripts/super-collect-file.sh'

ROWS = ['Error + stack', 'Request / trace ID', 'Sentry', 'PostHog replay', 'Logs', 'Screenshots', 'HAR / API sample',
        'Env + release', 'First / last seen', 'Users affected', 'Steps', 'Expected / actual']
BUG = '''## Problem
CSV upload reports success but no rows land in the imports table.

## Context
- **Where:** Imports page, `/imports`
  - a. Log in as the QA bot
  - b. Upload `fixtures/ten-rows.csv`
  - c. Click **Submit**

## Evidence
<details><summary>12 rows</summary>

| Row | Value |
|---|---|
''' + '\n'.join(f'| {r} | n/a — none |' for r in ROWS) + '''

</details>

## Fix
Make the import job reach a terminal state.

## Acceptance Criteria
- [ ] Uploaded rows are visible after submit

## Risk
🟢 **Low** · one handler.
'''
REFACTOR = '''## Problem
`OrderIntake` leaks three layers.

## Context
Seen while reviewing the checkout PR.

## Fix
Deepen the module behind one interface.
'''
AC = '\n## Acceptance Criteria\n- [ ] one interface\n'


class FilerCase(AdoCase):
    def setUp(self):
        super().setUp()
        gh = self.bin / 'gh'
        gh.write_bytes(b'#!/bin/sh\necho "$@" >> "$GH_SHIM_LOG"\nexit 1\n')
        gh.chmod(0o755)
        self.gh_log = self.root / 'gh.log'
        self.env.update(GH_SHIM_LOG=str(self.gh_log), SB_COLLECT_STATE_DIR=str(self.root / 'collect'),
                        SUPER_BOARD_BIN=str(ROOT / 'scripts'))
        self.fake.items[1]['fields']['System.Tags'] = 'feature'

    def run_script(self, script, *args, env=None):
        e = {k: v for k, v in dict(self.env, **(env or {})).items() if v is not None}
        return subprocess.run(['bash', str(script), *args], env=e, text=True, capture_output=True, cwd=self.root,
                              stdin=subprocess.DEVNULL, timeout=120)

    def no_gh_calls(self):
        self.assertFalse(self.gh_log.exists() and self.gh_log.read_text(encoding='utf-8').strip(),
                         'gh must never be called')

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return str(path)

    def new_tickets(self, before=1):
        return sorted(n for n in self.fake.items if n > before)

    def body_of(self, n):
        return json.loads(self.forge('ticket', 'view', str(n)).stdout)['body']

    def seen_comments(self, n):
        return [c['body'] for c in json.loads(self.forge('ticket', 'comments', str(n)).stdout)[0]]


class QaFilerTests(FilerCase):
    def file(self, *extra, body=BUG, title='CSV rows vanish', env=None):
        return self.run_script(QA, '--config', self.cfg(), '--title', title, '--body-file', self.write('bug.md', body),
                               '--route', '/imports', '--category', 'functional', *extra, env=env)

    def test_files_a_bug_in_ready_with_a_visible_fingerprint(self):
        r = self.file('--spec', 'imports.spec.ts', '--iter', '2', '--area', 'imports', '--suggested-skill', 'super-build')
        self.assertEqual(r.returncode, 0, r.stderr)
        n = int(r.stdout.strip().splitlines()[-1])
        item = self.fake.items[n]
        self.assertEqual(item['type'], 'Bug', 'kind bug is a Bug work item')
        self.assertEqual(self.fake.column_of(n), 'Ready')
        self.assertEqual(self.fake.tags(n), ['bug', 'source:qa', 'priority:medium', 'area:imports', 'qa:functional',
                                             'skill:super-build'])
        body = self.body_of(n)
        self.assertIn('## Board summary', body)
        self.assertNotIn('<!--', body)
        self.assertIn('- fingerprint: bug|/imports|functional|csv-rows-vanish', body)
        self.assertIn('- iteration: 2', body)
        self.assertEqual(item['fields']['System.Title'], '🐛 [bug] imports: CSV rows vanish')
        self.assertIn('## Blocked by\n\n- None.', body)
        self.no_gh_calls()

    def test_hidden_markers_would_not_survive(self):
        # the property the design rests on: a ticket written with an HTML comment loses it on save
        path = self.write('hidden.md', 'text\n<!-- fingerprint: x|y -->\n')
        n = int(self.ok('ticket', 'create', '--title', 'T', '--body-file', path).splitlines()[-1])
        self.assertNotIn('x|y', self.body_of(n))

    def test_same_fingerprint_comments_seen_again_and_files_nothing(self):
        first = self.file('--iter', '1')
        n = int(first.stdout.strip())
        again = self.file('--iter', '3')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout.strip(), str(n))
        self.assertEqual(self.new_tickets(), [n], 'no second ticket')
        comments = self.seen_comments(n)
        self.assertEqual(len(comments), 1)
        self.assertTrue(comments[0].startswith('Seen again on iteration 3.'))
        self.no_gh_calls()

    def test_dedupe_survives_a_light_edit_of_the_prose(self):
        n = int(self.file().stdout.strip())
        body = self.body_of(n).replace('CSV upload reports success', 'The CSV upload says it worked').replace('Fix', 'Remedy')
        self.ok('ticket', 'edit', str(n), '--body-file', self.write('edited.md', body))
        again = self.file()
        self.assertEqual(again.stdout.strip(), str(n))
        self.assertEqual(self.new_tickets(), [n])

    def test_fingerprint_with_entities_still_matches(self):
        fp = 'imports|"quote"&more|x'
        n = int(self.file('--fingerprint', fp).stdout.strip())
        self.assertIn('&amp;', self.fake_raw(n), 'the service stores entities; the forge decodes them')
        again = self.file('--fingerprint', fp)
        self.assertEqual(again.stdout.strip(), str(n))
        self.assertEqual(self.new_tickets(), [n])

    def fake_raw(self, n):
        return self.fake.render(self.fake.items[n])['fields']['Microsoft.VSTS.TCM.ReproSteps']

    def test_a_longer_token_is_not_a_hit(self):
        n = int(self.file('--fingerprint', 'err|sentry|4411').stdout.strip())
        other = int(self.file('--fingerprint', 'err|sentry|441').stdout.strip())
        self.assertNotEqual(n, other)

    def test_a_closed_ticket_does_not_dedupe(self):
        n = int(self.file().stdout.strip())
        self.ok('ticket', 'close', str(n), '--reason', 'completed')
        again = int(self.file().stdout.strip())
        self.assertNotEqual(again, n)
        self.assertEqual(self.seen_comments(n), [])

    def test_unknown_column_files_nothing(self):
        r = self.file('--column', 'Nope')
        self.assertEqual(r.returncode, 70, r.stderr)
        self.assertEqual(self.new_tickets(), [])
        self.assertEqual(r.stdout.strip(), '')

    def test_filed_but_not_placed_is_71_with_the_number(self):
        placed = {'done': False}

        def match(req):
            if req['method'] == 'PATCH' and '/wit/workitems/' in req['path'] and not placed['done']:
                placed['done'] = True
                return True
            return False
        self.fake.inject.append({'match': match, 'times': 1, 'status': 500, 'body': {'message': 'boom'}})
        r = self.file()
        self.assertEqual(r.returncode, 71, r.stderr)
        n = int(r.stdout.strip().splitlines()[-1])
        self.assertIn(n, self.fake.items)
        self.assertIn('manual move', r.stderr)

    def test_creation_failure_halts(self):
        self.fake.inject.append({'match': lambda req: req['method'] == 'POST' and req['path'].endswith('/workitems/$Bug'),
                                 'times': 1, 'status': 500, 'body': {'message': 'boom'}})
        r = self.file()
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertEqual(self.new_tickets(), [])
        self.assertTrue(self.halt.exists())

    def test_provider_comes_from_sb_config(self):
        r = self.run_script(QA, '--title', 'T', '--body-file', self.write('bug.md', BUG), '--route', '/imports',
                            env={'SB_CONFIG': self.cfg()})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.new_tickets()), 1)

    def test_check_body_makes_no_calls(self):
        r = self.run_script(QA, '--config', self.cfg(), '--title', 'T', '--body-file', self.write('b.md', BUG), '--check-body')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fake.requests, [])


class ReviewFilerTests(FilerCase):
    def file(self, body=REFACTOR, fp='order-intake|leaky', *extra):
        return self.run_script(REVIEW, '--config', self.cfg(), '--title', 'Leaky intake', '--body-file',
                               self.write('r.md', body), '--fingerprint', fp, '--files', 'src/a.ts,src/b.ts',
                               '--pr', '12', *extra)

    def test_a_note_lands_in_backlog_with_the_visible_stamp(self):
        r = self.file()
        self.assertEqual(r.returncode, 0, r.stderr)
        n = int(r.stdout.strip())
        self.assertEqual(self.fake.column_of(n), 'Backlog')
        self.assertEqual(self.fake.tags(n), ['refactor', 'source:review', 'strength:worth-exploring'])
        body = self.body_of(n)
        self.assertIn('super-review-fingerprint: order-intake|leaky', body)
        self.assertIn('## Blocked by\n\n- None.', body)
        self.assertNotIn('<!--', body)
        self.assertEqual(self.fake.items[n]['fields']['System.Title'], '♻️ [refactor] order-intake: Leaky intake')
        self.no_gh_calls()

    def test_acceptance_criteria_land_in_ready(self):
        n = int(self.file(REFACTOR + AC).stdout.strip())
        self.assertEqual(self.fake.column_of(n), 'Ready')

    def test_repeat_comments_and_files_nothing(self):
        n = int(self.file().stdout.strip())
        again = self.file()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout.strip(), str(n))
        self.assertEqual(self.new_tickets(), [n])
        self.assertTrue(self.seen_comments(n)[0].startswith('Seen again during review of #12.'))
        self.no_gh_calls()

    def test_missing_section_is_refused(self):
        r = self.file('## Problem\nx\n')
        self.assertEqual(r.returncode, 66)
        self.assertEqual(self.new_tickets(), [])

    def test_unknown_column_is_70(self):
        r = self.file(REFACTOR, 'a|b', '--column', 'Nope')
        self.assertEqual(r.returncode, 70, r.stderr)
        self.assertEqual(self.new_tickets(), [])


class CollectFilerTests(FilerCase):
    def collect(self, *args, body=BUG, title='Imports fail', typ='bug', source='sentry', fp='err|sentry|441'):
        return self.run_script(COLLECT, '--config', self.cfg(), '--type', typ, '--source', source, '--title', title,
                               '--body-file', self.write('c.md', body), '--fingerprint', fp, *args)

    def test_dry_run_plans_without_writing(self):
        r = self.collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'would-file|bug|Imports fail|Backlog')
        self.assertEqual(self.fake.reqs('POST', r'/workitems/\$'), [])

    def test_files_a_bug_into_backlog_with_visible_lines_and_tags(self):
        r = self.collect('--yes', '--label', 'needs-triage')
        self.assertEqual(r.returncode, 0, r.stderr)
        n = int(r.stdout.strip().splitlines()[-1])
        self.assertEqual(self.fake.column_of(n), 'Backlog')
        self.assertEqual(set(self.fake.tags(n)),
                         {'bug', 'source:qa', 'priority:medium', 'source:collect', 'collect:sentry', 'needs-triage'})
        body = self.body_of(n)
        self.assertIn('- super-collect-fingerprint: err|sentry|441', body)
        self.assertIn('- super-collect-source: sentry', body)
        self.assertNotIn('<!--', body)
        self.no_gh_calls()

    def test_second_run_is_a_duplicate_and_comments_seen_again(self):
        n = int(self.collect('--yes').stdout.strip().splitlines()[-1])
        plan = self.collect()
        self.assertEqual(plan.stdout.strip(), f'duplicate|#{n}|bug|Imports fail')
        again = self.collect('--yes', body=BUG + '\nmore evidence\n')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout.strip(), str(n))
        self.assertEqual(self.new_tickets(), [n])
        self.assertTrue(self.seen_comments(n)[0].startswith('Seen again by super-collect (sentry).'))
        self.no_gh_calls()

    def test_closed_hit_is_a_recurrence(self):
        n = int(self.collect('--yes').stdout.strip().splitlines()[-1])
        self.ok('ticket', 'close', str(n), '--reason', 'completed')
        self.assertEqual(self.collect().stdout.strip(), f'recurrence|#{n}|bug|Imports fail|Backlog')
        m = int(self.collect('--yes').stdout.strip().splitlines()[-1])
        self.assertNotEqual(m, n)
        self.assertIn(f'Recurrence: this fingerprint was last filed as #{n}', self.body_of(m))

    def test_refactor_goes_through_the_review_filer(self):
        r = self.collect('--yes', typ='refactor', source='architecture', body=REFACTOR, fp='arch|order|leaky', title='Leaky')
        self.assertEqual(r.returncode, 0, r.stderr)
        n = int(r.stdout.strip().splitlines()[-1])
        self.assertEqual(self.fake.column_of(n), 'Backlog')
        self.assertIn('super-review-fingerprint: arch|order|leaky', self.body_of(n))
        self.assertIn('- super-collect-fingerprint: arch|order|leaky', self.body_of(n))

    def test_a_board_without_backlog_refuses(self):
        self.fake.columns[0]['name'] = 'Inbox'
        self.cache.mkdir(exist_ok=True)
        r = self.collect('--yes')
        self.assertEqual(r.returncode, 65, r.stderr)
        self.assertEqual(self.new_tickets(), [])

    def test_adopt_places_a_card_that_has_no_column(self):
        n = self.fake.add('User Story', 'user-filed', 'body', '')
        self.fake.items[n]['col_id'] = None
        self.assertEqual(self.run_script(COLLECT, '--config', self.cfg(), '--adopt', str(n), '--type', 'bug').stdout.strip(),
                         f'would-adopt|#{n}|bug|Backlog')
        r = self.run_script(COLLECT, '--config', self.cfg(), '--adopt', str(n), '--type', 'bug', '--yes')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(n))
        self.assertEqual(self.fake.column_of(n), 'Backlog')
        self.assertIn('source:collect', self.fake.tags(n))
        self.no_gh_calls()

    def test_adopt_never_resets_a_card_already_on_the_board(self):
        n = self.fake.add('User Story', 'live', 'body', '', 'Building')
        r = self.run_script(COLLECT, '--config', self.cfg(), '--adopt', str(n), '--type', 'feature', '--yes')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fake.column_of(n), 'Building')
        self.assertEqual(self.fake.tags(n), [])


if __name__ == '__main__':
    unittest.main()
