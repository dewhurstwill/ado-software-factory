#!/usr/bin/env python3
"""The worker rate-limit guard and the wave throttle on an `azure-devops` config.

Azure DevOps has no quota endpoint: the forge saves the X-RateLimit-* headers it sees and records
every 429. The guard reads that file (never `gh`); the throttle steps `max_workers` down only when
a 429 was recorded. A `gh` shim that fails and logs proves GitHub is never asked."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase  # noqa: E402

GUARD = ROOT / 'scripts/super-board-gh-guard.sh'
THROTTLE = ROOT / 'scripts/super-board-throttle.sh'


class GuardCase(AdoCase):
    def setUp(self):
        super().setUp()
        self.gh_log = self.root / 'gh.log'
        self.sleep_log = self.root / 'sleep.log'
        for name, body in (('gh', 'echo "$@" >> "$GH_SHIM_LOG"\nexit 1\n'), ('sleep', 'echo "$@" >> "$SLEEP_LOG"\n')):
            shim = self.bin / name
            shim.write_text('#!/bin/sh\n' + body)
            shim.chmod(0o755)
        self.env.update(GH_SHIM_LOG=str(self.gh_log), SLEEP_LOG=str(self.sleep_log), SB_CONFIG=self.cfg(),
                        SB_GH_GUARD_STATE_FILE=str(self.root / 'budget'))
        self.quota_file = self.halt.parent / 'ado-quota.json'

    def save_quota(self, **fields):
        self.quota_file.parent.mkdir(parents=True, exist_ok=True)
        self.quota_file.write_text(json.dumps(dict({'remaining': None, 'limit': None, 'reset': None, 'at': 1}, **fields)))

    def bash(self, snippet, env=None):
        e = {k: v for k, v in dict(self.env, **(env or {})).items() if v is not None}
        return subprocess.run(['bash', '-c', f'. "{GUARD}"; {snippet}'], env=e, text=True, capture_output=True,
                              cwd=self.root, stdin=subprocess.DEVNULL, timeout=60)

    def no_gh(self):
        self.assertFalse(self.gh_log.exists() and self.gh_log.read_text().strip(), 'gh must never be called')

    def slept(self):
        return self.sleep_log.read_text().split() if self.sleep_log.exists() else []

    def throttle_once(self):
        """One 429 on a read; the forge waits Retry-After (scaled to 0) and the retry succeeds."""
        self.fake.inject.append({'match': lambda r: r['path'].endswith('/connectionData'), 'times': 1, 'status': 429,
                                 'headers': {'Retry-After': '1'}, 'body': {}})
        self.ok('whoami')


class GuardTests(GuardCase):
    def test_check_never_calls_gh_and_never_blocks_without_a_reading(self):
        r = self.bash('sb_gh_guard_check 200; echo rc=$?')
        self.assertEqual(r.stdout.strip(), 'rc=0', r.stderr)
        self.assertEqual(self.slept(), [])
        self.no_gh()

    def test_healthy_remaining_does_not_sleep(self):
        self.save_quota(remaining=900, limit=1000, reset=int(time.time()) + 100)
        self.assertEqual(self.bash('sb_gh_guard_check 200; echo rc=$?').stdout.strip(), 'rc=0')
        self.assertEqual(self.slept(), [])

    def test_cost_only_record_never_blocks(self):
        self.save_quota(cost=1, total_cost=999)
        self.assertEqual(self.bash('sb_gh_guard_check 200; echo rc=$?').stdout.strip(), 'rc=0')
        self.assertEqual(self.slept(), [])

    def test_exhausted_with_a_future_reset_sleeps_until_it(self):
        self.save_quota(remaining=0, limit=1000, reset=int(time.time()) + 20)
        r = self.bash('sb_gh_guard_check 200; echo rc=$?')
        self.assertEqual(r.stdout.strip(), 'rc=0', r.stderr)
        (wait,) = self.slept()
        self.assertTrue(18 <= int(wait) <= 24, wait)
        self.assertIn('Azure DevOps quota low', r.stderr)
        self.no_gh()

    def test_the_sleep_is_capped(self):
        self.save_quota(remaining=1, limit=1000, reset=int(time.time()) + 100000)
        self.bash('sb_gh_guard_check 200')
        self.assertEqual(self.slept(), ['300'])

    def test_a_reset_in_the_past_does_not_sleep(self):
        self.save_quota(remaining=0, limit=1000, reset=int(time.time()) - 60)
        self.bash('sb_gh_guard_check 200')
        self.assertEqual(self.slept(), [])

    def test_halted_run_is_79(self):
        self.halt.parent.mkdir(parents=True, exist_ok=True)
        self.halt.write_text('{"reason":"x"}')
        r = self.bash('sb_gh_guard_check 200; echo rc=$?')
        self.assertEqual(r.stdout.strip().splitlines()[-1], 'rc=79', r.stderr)
        self.no_gh()

    def test_summary_prints_the_azure_devops_line(self):
        self.save_quota(remaining=410, limit=1000, cost=2, total_cost=57)
        r = self.bash('sb_gh_guard_summary')
        self.assertEqual(r.stdout.strip(), '[gh-guard] azure-devops remaining=410/1000 cost=57 throttled=0', r.stderr)
        self.no_gh()

    def test_summary_without_any_header_and_after_a_429(self):
        self.assertEqual(self.bash('sb_gh_guard_summary').stdout.strip(),
                         '[gh-guard] azure-devops remaining=?/? cost=? throttled=0')
        self.throttle_once()
        self.assertTrue(self.bash('sb_gh_guard_summary').stdout.strip().endswith('throttled=1'))
        self.no_gh()

    def test_the_call_budget_still_works(self):
        r = self.bash('sb_gh_budget_init 3; sb_gh_budget_spend 2; echo left=$(sb_gh_budget_remaining); '
                      'sb_gh_budget_spend 2; echo rc=$?')
        self.assertEqual(r.stdout.split(), ['left=1', 'rc=73'])

    def test_github_config_still_asks_gh(self):
        cfg = self.root / 'gh.json'
        cfg.write_text('{"provider":"github"}')
        (self.bin / 'gh').write_text('#!/bin/sh\necho "$@" >> "$GH_SHIM_LOG"\n'
                                     'echo \'{"resources":{"graphql":{"limit":5000,"remaining":4000},"core":{"limit":5000,"remaining":4000}}}\'\n')
        r = self.bash('sb_gh_guard_summary', env={'SB_CONFIG': str(cfg)})
        self.assertIn('graphql=4000/5000 rest=4000/5000', r.stdout)
        self.assertTrue(self.gh_log.read_text().strip())


class ThrottleTests(GuardCase):
    def setUp(self):
        super().setUp()
        self.config_value['max_workers'] = 3
        self.cfg()  # written once: the script rewrites max_workers in place

    def throttle(self, *args):
        e = {k: v for k, v in self.env.items() if v is not None}
        r = subprocess.run(['bash', str(THROTTLE), '--config', str(self.config), *args], env=e, text=True,
                           capture_output=True, cwd=self.root, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def workers(self):
        return json.loads(self.config.read_text())['max_workers']

    def test_a_healthy_wave_does_not_step_down(self):
        out = self.throttle()
        self.assertEqual((out['hit'], out['from'], out['to'], out['wrote']), (False, 3, 3, False))
        self.assertEqual(self.workers(), 3)
        self.no_gh()

    def test_a_429_steps_down_once_and_is_consumed(self):
        self.throttle_once()
        out = self.throttle()
        self.assertTrue(out['hit'])
        self.assertIn('429', out['why'])
        self.assertEqual((out['from'], out['to'], out['wrote']), (3, 2, True))
        self.assertEqual(self.workers(), 2)
        again = self.throttle()  # the next wave saw no 429
        self.assertFalse(again['hit'])
        self.assertEqual(self.workers(), 2)
        self.no_gh()

    def test_dry_run_reports_without_writing_or_consuming(self):
        self.throttle_once()
        self.assertTrue(self.throttle('--dry-run')['hit'])
        self.assertEqual(self.workers(), 3)
        self.assertTrue(self.throttle()['hit'], 'the dry run left the count in place')

    def test_a_lane_result_naming_a_429_is_a_hit(self):
        results = self.root / 'results.json'
        results.write_text('[{"number":7,"detail":"Azure DevOps throttled a required read (HTTP 429)"}]')
        out = self.throttle('--results', str(results))
        self.assertTrue(out['hit'])
        results.write_text('[{"number":7,"detail":"merged"}]')
        self.assertFalse(self.throttle('--results', str(results))['hit'])

    def test_floor_is_one(self):
        self.config_value['max_workers'] = 1
        self.cfg()
        self.throttle_once()
        out = self.throttle()
        self.assertEqual((out['hit'], out['to'], out['wrote']), (True, 1, False))


if __name__ == '__main__':
    unittest.main()
