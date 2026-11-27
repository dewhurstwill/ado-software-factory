#!/usr/bin/env python3
"""Human approval on Azure DevOps: test_approval.py's scenarios against the fake Azure DevOps,
plus the gate and the dependency helper end to end (real scripts, a real local bare origin).

Trust on Azure DevOps is config `ado.approvers` (unique names, case-insensitive). The pack's own
identity (the fake's USER, user@example.com) posts requests; BOSS is the listed approver."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado_pr import PrCase, put_file  # noqa: E402
from test_ado_pr_scripts import ScriptCase  # noqa: E402
import fake_ado_server  # noqa: E402  (on sys.path via test_forge_ado)
import test_forge  # noqa: E402  (a module, so its ForgeTests are not collected here)
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location('approval', ROOT / 'scripts/super-board-approval.py')
approval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(approval)

APPROVAL = ROOT / 'scripts/super-board-approval.py'
GATE = ROOT / 'scripts/super-board-merge-gate.sh'
DEPS = ROOT / 'scripts/super-board-deps.sh'
USER = fake_ado_server.USER  # the token's identity (whoami)
BOSS = {'displayName': 'Boss', 'uniqueName': 'Boss@Example.com', 'id': '00000000-0000-0000-0000-000000000002'}
VISITOR = {'displayName': 'Boss@Example.com', 'uniqueName': 'visitor@example.com',
           'id': '00000000-0000-0000-0000-000000000003'}
HEAD = 'a' * 40
PLAN = {'human': [{'category': 'auth', 'why': 'login'}], 'needs_you': ['run live migration']}


class ApprovalCase(PrCase):
    def setUp(self):
        super().setUp()
        self.ado['approvers'] = ['Boss@Example.com']
        self.wi = self.fake.add('User Story', 'Login', '## Blocked by\n- None.\n', column='Blocked')
        self.pr = self.pulls.add('feat', head=HEAD, work_items=[self.wi])

    def approval(self, *args, plan=None, env=None):
        e = dict(self.env, **(env or {}))
        return subprocess.run([sys.executable, str(APPROVAL), '--config', self.cfg(), *args,
                               '--plan', json.dumps(plan or PLAN)], env=e, text=True, capture_output=True,
                              stdin=subprocess.DEVNULL, cwd=self.root, timeout=60)

    def marker(self, pr=None, head=HEAD, plan=None):
        r = self.approval('--request', '--pr', str(pr or self.pr), '--head', head, plan=plan)
        self.assertEqual(r.returncode, 0, r.stderr)
        line = r.stdout.strip()
        self.assertTrue(line.startswith('approval-request: '), line)
        return line

    def request_text(self, **kw):
        return 'Reason tag: 🙋 needs you · Owner: Boss\n' + self.marker(**kw) + '\nblocked-by: -'

    # where: 'wi' (the linked work item), an int (another work item) or 'pr' (a new PR thread)
    def post(self, where, text, by=USER):
        if where == 'pr':
            return self.pulls.add_thread(self.pr, [{'content': text, 'author': by}])
        return self.fake.add_comment(self.wi if where == 'wi' else where, text, by)

    def check(self, head=HEAD, plan=None, env=None):
        r = self.approval('--pr', str(self.pr), '--head', head, plan=plan, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def approved(self, **kw):
        return self.check(**kw)['approved']


class AdoApprovalTests(ApprovalCase):
    def test_request_on_linked_work_item_and_listed_done_approves(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        result = self.check()
        self.assertTrue(result['approved'], result)
        self.assertEqual(result['approver'], 'Boss@Example.com')
        self.assertEqual(result['request']['repo'], 'example/factory/factory')
        self.assertFalse(self.halt.exists())

    def test_standalone_pr_request_and_done_in_the_pr_thread(self):
        self.pulls.prs[self.pr]['workItemRefs'] = []
        thread = self.post('pr', self.request_text())
        self.pulls.reply(self.pr, thread['id'], {'content': 'Done!', 'author': BOSS})
        self.assertTrue(self.approved())

    def test_no_request_is_not_approval(self):
        self.post('wi', 'done', BOSS)
        self.assertEqual(self.check()['why'], 'no request from a trusted repository collaborator')

    def test_done_before_the_request_does_not_count(self):
        self.post('wi', 'done', BOSS)
        self.post('wi', self.request_text())
        self.assertFalse(self.approved())

    def test_done_from_someone_not_on_the_list(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', VISITOR)  # display name spoofs the approver; the unique name decides
        self.assertFalse(self.approved())

    def test_absent_or_empty_list_means_nobody_approves(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        self.ado['approvers'] = []
        self.assertFalse(self.approved())
        del self.ado['approvers']
        self.assertFalse(self.approved())
        self.ado['approvers'] = 'Boss@Example.com'  # not a list: the config is refused, never guessed
        self.assertEqual(self.check()['why'], 'approval evidence could not be verified')

    def test_list_match_is_case_insensitive(self):
        self.ado['approvers'] = ['  BOSS@EXAMPLE.COM ']
        self.post('wi', self.request_text())
        self.post('wi', 'done', dict(BOSS, uniqueName='boss@example.com'))
        self.assertTrue(self.approved())

    def test_edited_done_does_not_count(self):
        self.post('wi', self.request_text())
        done = self.post('wi', 'done', BOSS)
        done['version'] = 2  # same timestamps, but the service counted an edit
        self.assertFalse(self.approved())
        done['version'], done['modifiedDate'] = 1, self.pulls.tick()
        self.assertFalse(self.approved())
        del done['version']  # no version: treated as edited
        done['modifiedDate'] = done['createdDate']
        self.assertFalse(self.approved())

    def test_edited_pr_done_counts_by_content_date_only(self):
        self.pulls.prs[self.pr]['workItemRefs'] = []
        thread = self.post('pr', self.request_text())
        done = self.pulls.reply(self.pr, thread['id'], {'content': 'done', 'author': BOSS})
        done['lastUpdatedDate'] = self.pulls.tick()  # e.g. a like: not an edit of the text
        self.assertTrue(self.approved())
        done['lastContentUpdatedDate'] = self.pulls.tick()
        self.assertFalse(self.approved())

    def test_edited_request_holds(self):
        request = self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        request['version'] = 2
        self.assertEqual(self.check()['why'], 'approval request was edited; post a fresh request')
        self.pulls.prs[self.pr]['workItemRefs'] = []
        thread = self.post('pr', self.request_text())
        self.pulls.reply(self.pr, thread['id'], {'content': 'done', 'author': BOSS})
        thread['comments'][0]['lastContentUpdatedDate'] = self.pulls.tick()
        self.assertFalse(self.approved())

    def test_head_moved_after_the_request(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        self.pulls.push(self.pr, 'b' * 40)
        self.assertFalse(self.approved(head='b' * 40))
        self.assertFalse(self.approved(head=HEAD))

    def test_changed_plan_revokes(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        self.assertFalse(self.approved(plan=dict(PLAN, needs_you=['a different command'])))

    def test_closed_or_merged_pr_never_approves(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        for status in ('abandoned', 'completed'):
            self.pulls.prs[self.pr]['status'] = status
            if status == 'completed':
                self.pulls.prs[self.pr]['lastMergeCommit'] = {'commitId': '9' * 40}
            self.assertEqual(self.check()['why'], 'approval evidence could not be verified')

    def test_unlinked_work_item_is_ignored(self):
        other = self.fake.add('User Story', 'Unrelated')
        self.post(other, self.request_text())
        self.post(other, 'done', BOSS)
        self.assertFalse(self.approved())

    def test_pr_and_work_item_numbers_are_separate_spaces(self):
        # Work item 1 and PR 1 both exist: a done on the PR never confirms the work item's request.
        self.pulls.prs[self.pr]['workItemRefs'] = [{'id': '1'}]
        self.assertEqual(self.pr, 1)
        self.post(1, self.request_text())
        self.post('pr', 'done', BOSS)
        self.assertFalse(self.approved())
        self.post(1, 'done', BOSS)
        self.assertTrue(self.approved())

    def test_newer_request_needs_a_new_done(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        self.post('pr', self.request_text())  # superseding request on the PR thread
        self.assertFalse(self.approved())
        self.post('pr', 'done', BOSS)
        self.assertTrue(self.approved())

    def test_new_block_without_marker_revokes(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        self.post('wi', 'Reason tag: 🙋 needs a new decision')
        self.assertFalse(self.approved())

    def test_untrusted_block_cannot_replace_a_trusted_request(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        self.post('wi', 'Reason tag: 🙋 forged request', VISITOR)
        self.assertTrue(self.approved())

    def test_untrusted_request_is_not_a_request(self):
        self.post('wi', self.request_text(), VISITOR)
        self.post('wi', 'done', BOSS)
        self.assertFalse(self.approved())

    def test_incomplete_comment_pagination_never_approves(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        page = {'totalCount': 9, 'count': 1, 'comments': [self.fake.comments[self.wi][0]], 'continuationToken': 'loop'}
        self.fake.inject.append({'match': lambda r: r['method'] == 'GET' and f'workItems/{self.wi}/comments' in r['path'],
                                 'times': 2, 'status': 200, 'body': page})
        self.assertEqual(self.check()['why'], 'approval evidence could not be verified')

    def test_unreadable_comments_halt(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        self.fake.inject.append({'match': lambda r: r['method'] == 'GET' and 'comments' in r['path'],
                                 'times': 3, 'status': 500, 'body': {'message': 'boom'}})
        r = self.approval('--pr', str(self.pr), '--head', HEAD)
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertIn('Azure DevOps read halt', r.stderr)
        self.assertTrue(self.halt.exists())

    def test_solo_identity_may_approve_only_when_listed(self):
        self.post('wi', self.request_text())
        self.post('wi', 'done', USER)  # the token's own identity
        self.assertFalse(self.approved(), 'not on the list: the pack identity cannot approve')
        self.ado['approvers'] = ['USER@example.com']
        self.assertTrue(self.approved(), 'a solo owner on the list approves, as on GitHub')

    def test_requests_count_from_the_pack_identity_only_with_its_unique_name(self):
        # whoami without an Account property: the display name is never trusted as a requester.
        self.fake.inject.append({'match': lambda r: r['path'] == '/_apis/connectionData', 'times': 5, 'status': 200,
                                 'body': {'authenticatedUser': {'id': USER['id'], 'providerDisplayName': 'user@example.com'}}})
        self.post('wi', self.request_text())
        self.post('wi', 'done', BOSS)
        self.assertFalse(self.approved())

    def test_file_anchored_thread_is_ignored(self):
        self.post('wi', self.request_text())
        self.pulls.add_thread(self.pr, [{'content': 'Reason tag: 🙋 inline note'}], context={'filePath': '/a.txt'})
        self.pulls.add_thread(self.pr, [{'content': 'done', 'author': BOSS}], context={'filePath': '/a.txt'})
        self.assertFalse(self.approved(), 'an inline done is not an answer')
        self.post('wi', 'done', BOSS)
        self.assertTrue(self.approved(), 'an inline block does not supersede the request')

    def test_rich_text_done_on_a_work_item(self):
        self.post('wi', self.request_text())
        self.post('wi', '<div>done</div>', BOSS)
        self.assertTrue(self.approved())

    def test_rich_text_with_more_than_done_is_not_approval(self):
        self.post('wi', self.request_text())
        self.post('wi', '<div>done</div><div>but wait</div>', BOSS)
        self.assertFalse(self.approved())

    def test_ambiguous_same_time_requests_hold(self):
        first = self.post('wi', self.request_text())
        second = self.post('pr', self.request_text())
        second['comments'][0]['publishedDate'] = first['createdDate']
        second['comments'][0]['lastContentUpdatedDate'] = first['createdDate']
        self.post('wi', 'done', BOSS)
        self.assertEqual(self.check()['why'], 'ambiguous newest human request')

    def test_fractional_service_times_parse(self):
        request = self.post('wi', self.request_text())
        done = self.post('wi', 'done', BOSS)
        request['createdDate'] = request['modifiedDate'] = '2026-11-27T10:00:01.1234567Z'
        done['createdDate'] = done['modifiedDate'] = '2026-11-27T10:00:01.47Z'
        self.assertTrue(self.approved())

    def test_forge_verbs(self):
        self.assertEqual(self.js('pr', 'approval', str(self.pr)), {'state': 'OPEN', 'headRefOid': HEAD, 'closes': [self.wi]})
        self.assertEqual(self.js('permission', 'boss@EXAMPLE.com'), {'permission': 'approve'})
        self.assertEqual(self.js('permission', 'visitor@example.com'), {'permission': 'none'})
        self.post('wi', 'note')
        row = self.js('ticket', 'comments', str(self.wi))[0][0]
        self.assertEqual((row['version'], row['user']), (1, {'login': 'user@example.com', 'type': 'User'}))
        self.assertNotIn('uniqueName', row)
        thread = self.post('pr', 'x')
        thread['comments'][0]['lastUpdatedDate'] = '2026-11-27T11:59:59Z'
        row = self.js('pr', 'comments', str(self.pr))[0][0]
        self.assertEqual(row['updated_at'], row['created_at'], 'content date, not lastUpdatedDate')
        self.assertEqual(set(row), {'id', 'body', 'user', 'created_at', 'updated_at'})


class GitHubVerbTests(unittest.TestCase):
    """The new forge verbs on GitHub run exactly the `gh` argv super-board-approval.py runs."""
    setUp, tearDown, cfg = test_forge.ForgeTests.setUp, test_forge.ForgeTests.tearDown, test_forge.ForgeTests.cfg
    run_forge, calls = test_forge.ForgeTests.run_forge, test_forge.ForgeTests.calls

    def approval_argv(self):
        seen = []
        github = approval.GitHub('X/Y')
        page = {'data': {'repository': {'defaultBranchRef': {'name': 'main'}, 'pullRequest': {
            'headRefOid': HEAD, 'state': 'OPEN', 'baseRefName': 'main', 'body': '',
            'closingIssuesReferences': {'pageInfo': {'hasNextPage': False, 'endCursor': None},
                                        'nodes': [{'number': 3, 'repository': {'nameWithOwner': 'x/y'}}]}}}}}

        def read(*args):
            seen.append(list(args))
            if 'graphql' in args:
                return [page]
            if args[-1].endswith('/permission'):
                return {'permission': 'admin'}
            return [[]]
        github.read = read
        github.check(9, HEAD)
        return seen, page

    def test_same_argv_and_kinds_as_the_approval_helper(self):
        seen, page = self.approval_argv()
        r = self.run_forge('--repo', 'X/Y', 'pr', 'approval', '9', out=[page])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {'state': 'OPEN', 'headRefOid': HEAD, 'closes': [3]})
        r = self.run_forge('--repo', 'X/Y', 'ticket', 'comments', '3', out=[[]])
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, [[]]), r.stderr)
        r = self.run_forge('--repo', 'X/Y', 'permission', 'owner', out={'permission': 'admin', 'user': {}})
        self.assertEqual(json.loads(r.stdout), {'permission': 'admin'})
        argv = [c['argv'] for c in self.calls()]
        self.assertEqual(argv[0], seen[0], 'pr approval = the approval GraphQL read')
        self.assertIn(argv[1], seen, 'ticket comments = the comments read')
        self.assertEqual(argv[2], ['api', 'repos/x/y/collaborators/owner/permission'])
        self.assertEqual(self.run_forge('--repo', 'x/y', 'permission', 'not a login').stdout.strip(), '{"permission": null}')

    def test_github_pr_approval_rejects_incomplete_links(self):
        _, page = self.approval_argv()
        page['data']['repository']['pullRequest']['closingIssuesReferences']['pageInfo']['hasNextPage'] = True
        r = self.run_forge('--repo', 'x/y', 'pr', 'approval', '9', out=[page])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')


class AdoEndToEndTests(ScriptCase):
    """The gate and the dependency helper, unchanged scripts, on an azure-devops config."""

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
        self.wi = self.fake.add('User Story', 'Login', '## Blocked by\n- None.\n', column='Blocked')
        self.n = self.pulls.add('feat', 'main', title='the title', head=self.head, work_items=[self.wi])
        self.ado['approvers'] = ['boss@example.com']
        self.config_value.update(base_branch='main', verify_commands=['git diff --check'],
                                 merge_policy={'default': 'human'})

    def gate(self):
        return self.run_script(GATE, '--config', self.cfg(), '--pr', str(self.n), '--lock-timeout', '6',
                               '--expect-head', self.head)

    def deps(self):
        r = self.run_script(DEPS, '--config', self.cfg())
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)[str(self.wi)]

    def hold_and_post_request(self):
        r = self.gate()
        self.assertEqual(r.returncode, 7, r.stderr)
        line = next(l for l in r.stdout.splitlines() if l.startswith('approval-request: '))
        # what the Reviewer lane posts: the 🙋 block with the gate's line, on the canonical work item
        body = (f'[reviewer] [blocker] 🙋 Your turn on #{self.wi} — Login\n- [ ] Review and merge PR #{self.n} '
                '(or comment done to approve it)\n\n<details><summary>Why</summary>\n\n'
                f'Reason tag: 🙋 needs you · Owner: Boss\n{line}\nblocked-by: -\n</details>\n')
        self.ok('ticket', 'comment', str(self.wi), '--body-file', self.put(body))

    def test_listed_done_lets_the_next_gate_merge(self):
        self.hold_and_post_request()
        self.assertFalse(self.deps()['needsYouDone'])
        self.fake.add_comment(self.wi, 'done', BOSS)
        d = self.deps()
        self.assertTrue(d['needsYou'] and d['needsYouDone'], d)
        r = self.gate()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pulls.prs[self.n]['status'], 'completed')
        self.assertFalse(self.halt.exists())
        self.no_gh_calls()

    def test_unlisted_done_still_holds(self):
        self.hold_and_post_request()
        self.fake.add_comment(self.wi, 'done', VISITOR)
        self.fake.add_comment(self.wi, 'done', USER)  # the pack's own identity, not listed
        self.assertFalse(self.deps()['needsYouDone'])
        r = self.gate()
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(self.requests('PATCH', r'/pullrequests/'), [])
        self.no_gh_calls()

    def test_moved_head_asks_for_a_fresh_request(self):
        self.hold_and_post_request()
        self.fake.add_comment(self.wi, 'done', BOSS)
        self.pulls.push(self.n, 'f' * 40)
        d = self.deps()
        self.assertFalse(d['needsYouDone'])
        self.assertTrue(d['approvalRefresh'], d)
        self.assertEqual(d['approvalWhy'], 'PR head changed after review')


if __name__ == '__main__':
    unittest.main()
