#!/usr/bin/env python3
"""super-board-status.py on an `azure-devops` config, against the fake Azure DevOps.

The snapshot is read in-process through the forge backend: the run uses a PATH with no bash and no
jq (only a failing, logging `gh` shim), and the halt file is neither written nor honoured."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase  # noqa: E402

STATUS = ROOT / 'scripts/super-board-status.py'


class StatusTests(AdoCase):
    def setUp(self):
        super().setUp()
        self.gh_log = self.root / 'gh.log'
        self.proj = self.root / 'proj'
        (self.proj / '.claude/super-board/configs').mkdir(parents=True)
        self.config_value['repo'] = {'path': str(self.proj)}
        (self.proj / '.claude/super-board/configs/test.json').write_text(json.dumps(self.config_value))
        self.shims = self.root / 'shims'
        self.shims.mkdir()
        gh = self.shims / 'gh'
        gh.write_text('#!/bin/sh\necho "$@" >> "$GH_SHIM_LOG"\nexit 1\n')
        gh.chmod(0o755)
        # no bash, no jq on PATH: only the shim directory
        self.env.update(PATH=str(self.shims), GH_SHIM_LOG=str(self.gh_log), SB_REPO_PATH=str(self.proj))

    def status(self):
        e = {k: v for k, v in self.env.items() if v is not None}
        return subprocess.run([sys.executable, str(STATUS), 'test'], env=e, text=True, capture_output=True,
                              cwd=self.proj, stdin=subprocess.DEVNULL, timeout=120)

    def test_kanban_from_the_board_and_the_blocked_reason_from_the_newest_comment(self):
        a = self.fake.add('User Story', 'ready card', '', 'feature', 'Ready')
        b = self.fake.add('Bug', 'building \x1b[2Kcard', '', 'bug', 'Building')
        c = self.fake.add('User Story', 'stuck card', '', 'feature', 'Blocked')
        self.fake.add_comment(c, '🔐 older reason: missing key')
        self.fake.add_comment(c, '🛡 Reason tag: waiting on #3')
        r = self.status()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        for needle in (f'#{a}', f'#{b}', f'#{c}', 'ready card', 'stuck card'):
            self.assertIn(needle, out)
        self.assertIn('building [2Kcard', out, 'control characters are stripped from titles')
        self.assertNotIn('\x1b', out)
        self.assertIn('dependency gate', out, 'the newest comment decides the reason (and entities are decoded)')
        self.assertNotIn('missing creds', out)
        self.assertFalse(self.gh_log.exists(), 'gh is never run')

    def test_a_failed_comment_read_skips_the_reason_but_not_the_snapshot(self):
        c = self.fake.add('User Story', 'stuck card', '', 'feature', 'Blocked')
        self.fake.add_comment(c, '🛡 reason')
        self.fake.inject.append({'match': lambda r: '/comments' in r['path'], 'times': 9, 'status': 500, 'body': {}})
        r = self.status()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('stuck card', r.stdout)
        self.assertFalse(self.halt.exists(), 'a status read never halts the run')

    def test_an_existing_halt_does_not_block_the_snapshot_and_is_left_alone(self):
        self.halt.parent.mkdir(parents=True, exist_ok=True)
        self.halt.write_text('{"reason":"earlier"}')
        self.fake.add('User Story', 'still visible', '', '', 'Ready')
        r = self.status()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('still visible', r.stdout)
        self.assertEqual(json.loads(self.halt.read_text()), {'reason': 'earlier'})

    def test_a_board_read_failure_is_exit_67(self):
        self.fake.inject.append({'match': lambda r: True, 'times': 99, 'status': 401, 'body': {}})
        r = self.status()
        self.assertEqual(r.returncode, 67)
        self.assertIn('Azure DevOps board read failed', r.stderr)
        self.assertFalse(self.halt.exists())

    def test_no_credentials_is_exit_67(self):
        self.env.pop('AZURE_DEVOPS_EXT_PAT')
        r = self.status()
        self.assertEqual(r.returncode, 67)
        self.assertIn('no Azure DevOps credentials', r.stderr)

    def test_unknown_provider_is_66(self):
        self.config_value['provider'] = 'jira'
        (self.proj / '.claude/super-board/configs/test.json').write_text(json.dumps(self.config_value))
        self.assertEqual(self.status().returncode, 66)


if __name__ == '__main__':
    unittest.main()
