#!/usr/bin/env python3
"""Offline contract for the Azure DevOps pull-request verbs of super-board-forge.py.

The fake Azure DevOps (tests/fixtures/ado/fake_ado_server.py + fake_ado_pr.py) answers over
loopback; metadata and diff come from a real local git repository with a bare origin."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_forge_ado import AdoCase, github_read  # noqa: E402

_spec = importlib.util.spec_from_file_location('forge_ado_pr', ROOT / 'scripts/super-board-forge-ado-pr.py')
pr_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr_mod)

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
           'GIT_TERMINAL_PROMPT': '0'}


def put_file(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(text.encode('utf-8') if isinstance(text, str) else text)


def block(name, inner):
    return f'<!-- sb:{name} -->\n{inner}\n<!-- /sb:{name} -->'


def history(rows):
    body = '\n'.join(f'| builder | done | 10:{i % 60:02d} | row {i} ' + 'x' * 40 + ' |' for i in range(rows))
    return block('history', '<details><summary>Iteration history</summary>\n\n' + body + '\n\n</details>')


def sample_body(rows=100, problem='Probe.', visual='shot'):
    return '\n\n'.join([block('status', '> [!NOTE]\n> in review'), block('problem', '## Problem\n' + problem),
                        history(rows), block('visual', visual), block('risk', 'low')]) + '\n'


class GitMixin:
    """work: a clone with `origin` a bare repo; branch main with a few files."""

    def git(self, *args, cwd=None, check=True):
        r = subprocess.run(['git', *args], cwd=str(cwd or self.work), capture_output=True, text=True,
                           env=dict(os.environ, **GIT_ENV))
        if check and r.returncode:
            raise AssertionError(f'git {args}: {r.stderr}')
        return r.stdout.strip()

    def init_repo(self):
        self.origin = self.root / 'origin.git'
        self.work = self.root / 'work'
        self.work.mkdir()
        subprocess.run(['git', 'init', '-q', '--bare', str(self.origin)], check=True)
        self.git('init', '-q')
        self.git('symbolic-ref', 'HEAD', 'refs/heads/main')
        self.git('remote', 'add', 'origin', str(self.origin))
        self.config_value['repo']['path'] = str(self.work)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')


class PrCase(AdoCase, GitMixin):
    def setUp(self):
        super().setUp()
        self.pulls = self.fake.pulls
        self.env.update(SB_ADO_MERGE_POLL_INTERVAL='0', SB_ADO_MERGE_POLL_TIMEOUT='5')

    def put(self, text):
        path = self.root / 'in.md'
        put_file(path, text)
        return str(path)

    def requests(self, method, pattern):
        return self.fake.reqs(method, pattern)

    def threads_of(self, n):
        return self.pulls.threads[n]


class PureTests(unittest.TestCase):
    def test_units_count_utf16(self):
        self.assertEqual(pr_mod.units('abc'), 3)
        self.assertEqual(pr_mod.units('\U0001F600'), 2)

    def test_split_join_round_trip(self):
        for rows in (100, 150):
            body = sample_body(rows)
            description, moved = pr_mod.split_body(body)
            self.assertLessEqual(pr_mod.units(description), 4000)
            self.assertEqual(pr_mod.join_body(description, pr_mod.overflow_text(moved)), body)
        small = sample_body(3)
        self.assertEqual(pr_mod.split_body(small), (small, {}))

    def test_only_what_is_needed_moves(self):
        description, moved = pr_mod.split_body(sample_body(100))
        self.assertEqual(list(moved), ['history'])
        self.assertIn('<!-- sb:visual -->\nshot\n<!-- /sb:visual -->', description)

    def test_visual_moves_second(self):
        body = sample_body(60, problem='p' * 1200, visual='v' * 2600)
        description, moved = pr_mod.split_body(body)
        self.assertEqual(list(moved), ['history', 'visual'])
        self.assertEqual(pr_mod.join_body(description, pr_mod.overflow_text(moved)), body)

    def test_still_too_long(self):
        with self.assertRaises(pr_mod.TooLong) as caught:
            pr_mod.split_body(sample_body(100, problem='p' * 4500))
        self.assertGreater(caught.exception.size, 4000)


class ReadVerbTests(PrCase):
    def test_head_state_for_each_status(self):
        n = self.pulls.add('feat', head='a' * 40)
        self.assertEqual(self.ok('pr', 'head', str(n)).strip(), 'feat ' + 'a' * 40)
        state = self.js('pr', 'state', str(n))
        self.assertEqual(state, {'state': 'OPEN', 'headRefOid': 'a' * 40, 'mergeCommit': None})
        # the trial merge commit is set by now and must never be reported as a merge
        self.assertTrue(self.pulls.prs[n]['lastMergeCommit'])
        self.assertIsNone(self.js('pr', 'state', str(n))['mergeCommit'])
        self.pulls.prs[n]['status'] = 'abandoned'
        self.assertEqual(self.js('pr', 'state', str(n))['state'], 'CLOSED')
        self.pulls.prs[n].update(status='completed', lastMergeCommit={'commitId': 'b' * 40})
        self.assertEqual(self.js('pr', 'state', str(n)),
                         {'state': 'MERGED', 'headRefOid': 'a' * 40, 'mergeCommit': {'oid': 'b' * 40}})
        self.pulls.prs[n]['lastMergeCommit'] = None
        self.assertEqual(self.forge('pr', 'state', str(n)).returncode, 69)

    def test_unknown_pr_is_exit_3(self):
        self.assertEqual(self.forge('pr', 'head', '99').returncode, 3)
        self.assertEqual(self.forge('pr', 'head', 'x').returncode, 64)

    def test_get_url_and_auth(self):
        n = self.pulls.add('feat')
        self.ok('pr', 'head', str(n))
        request = self.requests('GET', r'/pullrequests/1$')[0]
        self.assertEqual(request['path'], '/Factory/_apis/git/repositories/Factory/pullrequests/1')
        self.assertEqual(request['query']['api-version'], ['7.1'])
        self.assertTrue(request['auth'])

    def test_list_open_and_merged(self):
        a = self.pulls.add('issue-1-x', title='open one', description='Closes #1')
        self.pulls.changes[a] = [('edit', '/src/x.txt'), ('add', '/docs/y.md')]
        b = self.pulls.add('issue-2-y', title='done one', description='Closes #2')
        self.pulls.prs[b]['status'] = 'completed'
        opened = self.js('pr', 'list', '--state', 'open', '--limit', '5')
        self.assertEqual(opened, [{'number': a, 'title': 'open one', 'body': 'Closes #1',
                                   'url': 'https://dev.azure.com/example/Factory/_git/Factory/pullrequest/1',
                                   'headRefName': 'issue-1-x', 'files': [{'path': 'src/x.txt'}, {'path': 'docs/y.md'}]}])
        merged = self.js('pr', 'list', '--state', 'merged', '--limit', '5')
        self.assertEqual(merged, [{'number': b, 'title': 'done one', 'body': 'Closes #2',
                                   'url': 'https://dev.azure.com/example/Factory/_git/Factory/pullrequest/2'}])
        listing = self.requests('GET', r'/pullrequests$')[0]
        self.assertEqual(listing['query']['searchCriteria.status'], ['active'])
        self.assertEqual(listing['query']['$top'], ['5'])
        for entry in opened:  # the validators the forge's callers read through
            github_read.validate(json.dumps([entry]), 'prs-open')
        github_read.validate(json.dumps(merged), 'prs-merged')
        self.assertEqual(self.forge('pr', 'list', '--state', 'all', '--limit', '1').returncode, 64)

    def test_labels_come_from_the_labels_endpoint(self):
        n = self.pulls.add('feat', labels=['payments'])
        self.assertNotIn('labels', self.js('pr', 'state', str(n)))  # (the GET has none; metadata must not trust it)
        self.assertNotIn('labels', self.fake_get(n))

    def fake_get(self, n):
        self.pulls.settle(self.pulls.prs[n])
        return self.pulls.render(self.pulls.prs[n])

    def test_links(self):
        n = self.pulls.add('feat', work_items=[12, 3])
        self.assertEqual(self.js('pr', 'links', str(n)), {'closes': [3, 12]})
        self.assertEqual(self.js('pr', 'links', str(self.pulls.add('other'))), {'closes': []})


class CommentTests(PrCase):
    def test_comment_create_list_edit(self):
        n = self.pulls.add('feat')
        out = self.ok('pr', 'comment', str(n), '--body-file', self.put('<!-- super-review:report -->\nfirst')).strip()
        self.assertEqual(out, '1.1')
        sent = self.requests('POST', r'/threads$')[0]['body']
        self.assertEqual(sent, {'comments': [{'parentCommentId': 0, 'content': '<!-- super-review:report -->\nfirst',
                                              'commentType': 1}], 'status': 1})
        self.assertEqual(self.ok('pr', 'comment', str(n), '--body-file', self.put('second')).strip(), '2.1')
        # a system thread, a deleted comment and the overflow thread are not comments
        self.pulls.add_thread(n, [{'content': 'Policy status', 'commentType': 'system'}])
        self.pulls.add_thread(n, [{'content': pr_mod.OVERFLOW_MARK + '\nmoved'}])
        self.pulls.threads[n][0]['comments'].append({'id': 2, 'content': 'reply', 'commentType': 'text',
                                                     'publishedDate': '2026-11-27T12:00:00Z',
                                                     'lastUpdatedDate': '2026-11-27T12:00:00Z', 'author': fake_user()})
        self.pulls.threads[n][1]['comments'].append({'id': 2, 'content': 'gone', 'isDeleted': True, 'commentType': 'text',
                                                     'publishedDate': '2026-11-27T12:01:00Z', 'author': fake_user()})
        pages = self.js('pr', 'comments', str(n))
        self.assertEqual(len(pages), 1)
        rows = pages[0]
        self.assertEqual([r['id'] for r in rows], ['1.1', '2.1', '1.2'])
        self.assertEqual(rows[0]['body'], '<!-- super-review:report -->\nfirst')
        self.assertEqual(rows[0]['user'], {'login': 'user@example.com', 'type': 'User'})
        self.assertEqual(self.ok('pr', 'comment-edit', str(n), '1.1', '--body-file', self.put('edited')).strip(), '1.1')
        patch = self.requests('PATCH', r'/threads/1/comments/1$')[0]
        self.assertEqual(patch['body'], {'content': 'edited'})
        self.assertEqual(self.js('pr', 'comments', str(n))[0][0]['body'], 'edited')
        self.assertEqual(self.forge('pr', 'comment-edit', str(n), 'x', '--body-file', self.put('z')).returncode, 64)


def fake_user():
    sys.path.insert(0, str(ROOT / 'tests/fixtures/ado'))
    import fake_ado_server
    return fake_ado_server.USER


class BodyTests(PrCase):
    def test_small_body_is_the_description(self):
        n = self.pulls.add('feat', description='old')
        body = sample_body(3)
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(body))
        self.assertEqual(self.pulls.prs[n]['description'], body)
        self.assertEqual(self.threads_of(n), [])
        sent = self.requests('PATCH', r'/pullrequests/1$')[0]
        self.assertEqual(sent['body'], {'description': body})
        self.assertEqual(sent['content_type'], 'application/json')
        out = self.js('pr', 'body', str(n))
        self.assertEqual(out, {'headRefOid': self.pulls.prs[n]['lastMergeSourceCommit']['commitId'], 'body': body})
        github_read.validate(json.dumps(out), 'body')
        self.assertEqual(self.requests('GET', r'/threads$'), [], 'no thread read when nothing was moved')

    def test_overflow_round_trip(self):
        n = self.pulls.add('feat')
        body = sample_body(100)
        self.assertGreater(pr_mod.units(body), 5500)
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(body))
        description = self.pulls.prs[n]['description']
        self.assertLessEqual(pr_mod.units(description), 4000)
        self.assertIn(pr_mod.stub('history'), description)
        self.assertNotIn('row 5 ', description)
        self.assertEqual(len(self.threads_of(n)), 1)
        first = self.threads_of(n)[0]['comments'][0]['content']
        self.assertTrue(first.startswith('<!-- sb:overflow -->\n'))
        self.assertIn('row 5 ', first)
        self.assertEqual(self.js('pr', 'body', str(n))['body'], body)
        # a second write edits the same comment in place; it never opens a second thread
        longer = sample_body(101)
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(longer))
        self.assertEqual(len(self.threads_of(n)), 1)
        self.assertEqual(len(self.requests('POST', r'/threads$')), 1)
        self.assertEqual(len(self.requests('PATCH', r'/threads/1/comments/1$')), 1)
        self.assertEqual(self.js('pr', 'body', str(n))['body'], longer)
        # same content again: no thread write
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(longer))
        self.assertEqual(len(self.requests('PATCH', r'/threads/1/comments/1$')), 1)
        # shrinking back puts the full body in the description and leaves a one-line note
        small = sample_body(3)
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(small))
        self.assertEqual(self.pulls.prs[n]['description'], small)
        self.assertEqual(len(self.threads_of(n)), 1)
        self.assertEqual(self.threads_of(n)[0]['comments'][0]['content'], pr_mod.OVERFLOW_QUIET)
        self.assertEqual(self.js('pr', 'body', str(n))['body'], small)
        # and it overflows again into the SAME thread
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(body))
        self.assertEqual(len(self.threads_of(n)), 1)
        self.assertEqual(self.js('pr', 'body', str(n))['body'], body)

    def test_visual_moves_when_history_is_not_enough(self):
        n = self.pulls.add('feat')
        body = sample_body(60, problem='p' * 1200, visual='v' * 2600)
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(body))
        description = self.pulls.prs[n]['description']
        self.assertLessEqual(pr_mod.units(description), 4000)
        self.assertIn(pr_mod.stub('visual'), description)
        self.assertEqual(self.js('pr', 'body', str(n))['body'], body)

    def test_still_too_long_exits_65_and_writes_nothing(self):
        n = self.pulls.add('feat', description='keep')
        r = self.forge('pr', 'set-body', str(n), '--body-file', self.put(sample_body(100, problem='p' * 4500)))
        self.assertEqual(r.returncode, 65, r.stderr)
        self.assertIn('4000', r.stderr)
        self.assertRegex(r.stderr, r'\d{4,} characters')
        self.assertEqual(self.pulls.prs[n]['description'], 'keep')
        self.assertEqual(self.requests('PATCH', r'.'), [])
        self.assertEqual(self.requests('POST', r'.'), [])

    def test_description_write_error_is_exit_69(self):
        n = self.pulls.add('feat')
        self.fake.inject.append({'match': lambda r: r['method'] == 'PATCH', 'times': 1, 'status': 400,
                                 'body': {'message': 'A description for a pull request must not be longer than 4000 characters.'}})
        r = self.forge('pr', 'set-body', str(n), '--body-file', self.put('x'))
        self.assertEqual(r.returncode, 69)
        self.assertIn('4000 characters', r.stderr)


class CreateTests(PrCase):
    def test_create_sends_the_documented_payload(self):
        out = self.ok('pr', 'create', '--base', 'main', '--head', 'issue-1-probe', '--title', 'feat: probe',
                      '--body-file', self.put('body text'), '--draft', '--label', 'feature', '--label', 'qa',
                      '--closes', '1', '--closes', '#7').splitlines()
        self.assertEqual(out, ['https://dev.azure.com/example/Factory/_git/Factory/pullrequest/1', '1'])
        sent = self.requests('POST', r'/pullrequests$')[0]
        self.assertEqual(sent['body'], {
            'sourceRefName': 'refs/heads/issue-1-probe', 'targetRefName': 'refs/heads/main', 'title': 'feat: probe',
            'description': 'body text', 'isDraft': True, 'labels': [{'name': 'feature'}, {'name': 'qa'}],
            'workItemRefs': [{'id': '1'}, {'id': '7'}]})
        self.assertEqual(self.js('pr', 'links', '1'), {'closes': [1, 7]})
        self.assertTrue(self.pulls.prs[1]['isDraft'])
        self.ok('pr', 'ready', '1')
        self.assertEqual(self.requests('PATCH', r'/pullrequests/1$')[0]['body'], {'isDraft': False})
        self.assertFalse(self.pulls.prs[1]['isDraft'])

    def test_create_with_overflow_makes_one_thread(self):
        body = sample_body(100)
        out = self.ok('pr', 'create', '--base', 'main', '--head', 'feat', '--title', 't', '--body-file', self.put(body))
        n = int(out.splitlines()[-1])
        self.assertLessEqual(pr_mod.units(self.pulls.prs[n]['description']), 4000)
        self.assertEqual(len(self.threads_of(n)), 1)
        self.assertEqual(self.js('pr', 'body', str(n))['body'], body)

    def test_create_too_long_posts_nothing(self):
        r = self.forge('pr', 'create', '--base', 'main', '--head', 'feat', '--title', 't',
                       '--body-file', self.put(sample_body(100, problem='p' * 4500)))
        self.assertEqual(r.returncode, 65)
        self.assertEqual(self.requests('POST', r'.'), [])

    def test_create_bad_closes(self):
        r = self.forge('pr', 'create', '--base', 'main', '--head', 'f', '--title', 't', '--body-file', self.put('x'),
                       '--closes', 'abc')
        self.assertEqual(r.returncode, 64)


class LaneVerbTests(PrCase):
    """The verbs a lane agent shares with GitHub: pr view, checks, inline notes, thread resolve, threads."""
    HEAD = 'd' * 40

    def pr(self, **kw):
        return self.pulls.add('feat', 'main', title='the title', head=self.HEAD, **kw)

    def test_pr_view_is_one_neutral_object(self):
        n = self.pr(labels=['bug'])
        view = self.js('pr', 'view', str(n))
        self.assertEqual(view, {'number': n, 'title': 'the title', 'url': f'https://dev.azure.com/example/Factory/_git/Factory/pullrequest/{n}',
                                'state': 'OPEN', 'isDraft': False, 'headRefName': 'feat', 'headRefOid': self.HEAD,
                                'baseRefName': 'main', 'mergeCommit': None, 'labels': [{'name': 'bug'}]})
        self.pulls.prs[n].update(status='completed', lastMergeCommit={'commitId': 'e' * 40})
        done = self.js('pr', 'view', str(n))
        self.assertEqual((done['state'], done['mergeCommit']), ('MERGED', {'oid': 'e' * 40}))
        self.assertEqual(self.forge('pr', 'view', '99').returncode, 3)

    def test_checks_map_policy_evaluations(self):
        n = self.pr()
        self.assertEqual(self.js('pr', 'checks', str(n)), [], 'no policies: an empty list, not an error')
        self.pulls.policies = [{'status': 'approved', 'type': 'Minimum number of reviewers', 'blocking': True},
                               {'status': 'running', 'type': 'Build', 'name': 'ci', 'blocking': True},
                               {'status': 'rejected', 'type': 'Comment requirements', 'blocking': False},
                               {'status': 'queued', 'type': 'Build', 'name': 'lint', 'blocking': False},
                               {'status': 'notApplicable', 'type': 'Work item linking', 'blocking': True}]
        self.assertEqual(self.js('pr', 'checks', str(n)), [
            {'name': 'Minimum number of reviewers', 'status': 'pass', 'required': True},
            {'name': 'Build: ci', 'status': 'pending', 'required': True},
            {'name': 'Comment requirements', 'status': 'fail', 'required': False},
            {'name': 'Build: lint', 'status': 'pending', 'required': False},
            {'name': 'Work item linking', 'status': 'skipped', 'required': True}])
        req = self.requests('GET', r'/policy/evaluations')[0]
        self.assertEqual(req['query']['artifactId'], [f'vstfs:///CodeReview/CodeReviewId/proj-0001/{n}'])
        self.assertEqual(req['query']['api-version'], ['7.1-preview.1'])

    def test_checks_find_the_project_id_when_the_pull_request_has_none(self):
        n = self.pr()
        self.pulls.project_id = None
        self.assertEqual(self.js('pr', 'checks', str(n)), [])
        self.assertEqual(len(self.requests('GET', r'/_apis/projects/Factory$')), 1)

    def note(self, n, *args, body='**Purpose** the note'):
        return self.forge('pr', 'note', str(n), *args, '--body-file', self.put(body))

    def test_a_line_note_is_a_file_anchored_thread(self):
        n = self.pr()
        r = self.note(n, '--file', 'src/a.ts', '--line', '7')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, '1.1\n')
        sent = self.requests('POST', r'/threads$')[0]['body']
        self.assertEqual(sent['threadContext'], {'filePath': '/src/a.ts', 'rightFileStart': {'line': 7, 'offset': 1},
                                                 'rightFileEnd': {'line': 7, 'offset': 1}})
        self.assertEqual(sent['comments'][0]['content'], '**Purpose** the note')

    def test_range_left_side_and_whole_file_notes(self):
        n = self.pr()
        self.note(n, '--file', '/a.ts', '--line', '3', '--end-line', '9', '--side', 'left')
        self.note(n, '--file', 'b.ts')
        first, second = [r['body']['threadContext'] for r in self.requests('POST', r'/threads$')]
        self.assertEqual(first, {'filePath': '/a.ts', 'leftFileStart': {'line': 3, 'offset': 1}, 'leftFileEnd': {'line': 9, 'offset': 1}})
        self.assertEqual(second, {'filePath': '/b.ts'}, 'a whole-file note has the path only')

    def test_note_refuses_a_moved_head_and_bad_input(self):
        n = self.pr()
        r = self.note(n, '--file', 'a.ts', '--line', '1', '--head', 'f' * 40)
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertEqual(self.requests('POST', r'/threads$'), [])
        self.assertEqual(self.note(n, '--file', 'a.ts', '--line', '1', '--head', self.HEAD).returncode, 0)
        self.assertEqual(self.note(n, '--file', 'a.ts', '--end-line', '4').returncode, 64)
        self.assertEqual(self.note(n, '--file', 'a.ts', '--line', '5', '--end-line', '2').returncode, 64)
        self.assertEqual(self.note(n, '--file', 'a.ts', '--line', '1', '--side', 'up').returncode, 64)
        self.assertEqual(self.note(n, '--file', 'a.ts', '--line', '1', body='  ').returncode, 64)

    def test_notes_batch_posts_one_thread_each_in_order(self):
        n = self.pr()
        notes = [{'file': 'a.ts', 'line': 2, 'body': 'one'}, {'file': 'b.ts', 'body': 'two'},
                 {'file': 'c.ts', 'line': 1, 'end_line': 3, 'side': 'right', 'body': 'three'}]
        path = self.root / 'notes.json'
        path.write_text(json.dumps(notes))
        out = self.js('pr', 'notes', str(n), '--notes-file', str(path), '--head', self.HEAD)
        self.assertEqual(out, [{'index': 0, 'id': '1.1'}, {'index': 1, 'id': '2.1'}, {'index': 2, 'id': '3.1'}])
        self.assertEqual([t['comments'][0]['content'] for t in self.threads_of(n)], ['one', 'two', 'three'])
        path.write_text('[]')
        self.assertEqual(self.forge('pr', 'notes', str(n), '--notes-file', str(path)).returncode, 64)

    def test_threads_carry_ids_and_the_anchor_and_note_edit_changes_one(self):
        n = self.pr()
        self.note(n, '--file', 'src/a.ts', '--line', '4', '--end-line', '6')
        self.pulls.add_thread(n, [{'content': 'general'}])
        threads = self.js('pr', 'threads', str(n))
        self.assertEqual((threads[0]['path'], threads[0]['line'], threads[0]['startLine'], threads[0]['side'], threads[0]['outdated']),
                         ('src/a.ts', 6, 4, 'right', False))
        self.assertEqual((threads[1]['path'], threads[1]['line'], threads[1]['side']), (None, None, None))
        self.assertEqual(threads[0]['comments'][0]['id'], '1.1')
        r = self.forge('pr', 'note-edit', str(n), '1.1', '--body-file', self.put('edited'))
        self.assertEqual((r.returncode, r.stdout), (0, '1.1\n'))
        self.assertEqual(self.js('pr', 'threads', str(n))[0]['comments'][0]['body'], 'edited')

    def test_thread_resolve_patches_the_thread_status(self):
        n = self.pr()
        self.note(n, '--file', 'a.ts', '--line', '1')
        r = self.forge('pr', 'thread-resolve', str(n), '1')
        self.assertEqual((r.returncode, r.stdout), (0, 'resolved 1\n'))
        patch = self.requests('PATCH', r'/threads/1$')[0]
        self.assertEqual(patch['body'], {'status': 'fixed'})
        self.assertTrue(self.js('pr', 'threads', str(n))[0]['resolved'])
        self.assertEqual(self.forge('pr', 'thread-resolve', str(n), '99').returncode, 69)
        self.assertEqual(self.forge('pr', 'thread-resolve', str(n), 'abc').returncode, 64)


class MergeTests(PrCase):
    HEAD = 'c' * 40

    def merge(self, n=1, *extra, head=None, env=None):
        return self.forge('pr', 'merge', str(n), '--squash', '--delete-branch', *extra,
                          '--match-head', head or self.HEAD, env=env)

    def test_pinned_completion_then_poll(self):
        n = self.pulls.add('feat', title='the title', head=self.HEAD)
        msg = self.put('- first bullet\nCloses #1\n')
        r = self.merge(n, '--subject', 'S: subject', '--body-file', msg)
        self.assertEqual(r.returncode, 0, r.stderr)
        patches = self.requests('PATCH', r'/pullrequests/1$')
        self.assertEqual(len(patches), 1, 'the completion is sent exactly once')
        self.assertEqual(patches[0]['body'], {
            'status': 'completed', 'lastMergeSourceCommit': {'commitId': self.HEAD},
            'completionOptions': {'mergeStrategy': 'squash', 'deleteSourceBranch': True, 'transitionWorkItems': False,
                                  'mergeCommitMessage': 'S: subject\n\n- first bullet\nCloses #1'}})
        self.assertGreaterEqual(len(self.requests('GET', r'/pullrequests/1$')), 3, 'one precheck, then polled GETs')
        done = self.pulls.merged[n]
        self.assertEqual(done['message'], 'S: subject\n\n- first bullet\nCloses #1')
        self.assertIn(done['sha'], r.stdout)
        state = self.js('pr', 'state', str(n))
        self.assertEqual(state, {'state': 'MERGED', 'headRefOid': self.HEAD, 'mergeCommit': {'oid': done['sha']}})
        self.assertNotEqual(done['sha'], self.pulls.prs[n]['lastMergeSourceCommit']['commitId'])
        self.assertIn('feat', self.pulls.deleted)

    def test_no_message_sends_none_and_body_only_uses_the_title(self):
        n = self.pulls.add('feat', title='the title', head=self.HEAD)
        self.assertEqual(self.merge(n).returncode, 0)
        self.assertNotIn('mergeCommitMessage', self.requests('PATCH', r'/pullrequests/1$')[0]['body']['completionOptions'])
        m = self.pulls.add('feat2', title='T2', head=self.HEAD)
        self.assertEqual(self.merge(m, '--body-file', self.put('only body\n')).returncode, 0)
        self.assertEqual(self.pulls.merged[m]['message'], 'T2\n\nonly body')

    def test_trial_commit_is_not_a_merge_while_active(self):
        n = self.pulls.add('feat', head=self.HEAD)
        self.pulls.never_complete = True
        r = self.merge(n, env={'SB_ADO_MERGE_POLL_TIMEOUT': '0.3', 'SB_ADO_MERGE_POLL_INTERVAL': '0.05'})
        self.assertEqual(r.returncode, 69)
        self.assertIn('not confirmed', r.stderr)
        self.assertIn('not retried', r.stderr)
        self.assertEqual(len(self.requests('PATCH', r'/pullrequests/1$')), 1)
        self.assertTrue(self.pulls.prs[n]['lastMergeCommit'], 'the service filled in its trial merge')
        state = self.js('pr', 'state', str(n))
        self.assertEqual(state['state'], 'OPEN')
        self.assertIsNone(state['mergeCommit'])

    def test_head_moved_is_409_and_exit_6(self):
        n = self.pulls.add('feat', head=self.HEAD)
        r = self.merge(n, head='d' * 40)
        self.assertEqual(r.returncode, 6, r.stderr)
        self.assertIn('head moved', r.stderr)
        self.assertEqual(self.pulls.merged, {})
        self.assertEqual(self.pulls.prs[n]['status'], 'active')

    def test_policy_and_conflict_rejections_name_the_status(self):
        for outcome in ('conflicts', 'rejectedByPolicy', 'failure'):
            self.pulls.merge_outcome = outcome
            n = self.pulls.add(f'feat-{outcome}', head=self.HEAD)
            r = self.merge(n)
            self.assertEqual(r.returncode, {'conflicts': 5, 'rejectedByPolicy': 7, 'failure': 65}[outcome], (outcome, r.stderr))
            self.assertIn(outcome, r.stderr)
            if outcome == 'rejectedByPolicy':
                self.assertEqual(r.stdout.strip(), 'human-gate: policy — Azure DevOps branch policy blocks completion: forced rejectedByPolicy')
            else:
                self.assertEqual(r.stdout, '')
            self.assertNotIn(n, self.pulls.merged)
            self.assertEqual(self.pulls.prs[n]['status'], 'active')
        self.pulls.merge_outcome = None

    def test_prechecks_send_nothing(self):
        n = self.pulls.add('a', head=self.HEAD)
        self.pulls.prs[n]['mergeStatus'] = 'conflicts'
        r = self.merge(n)
        self.assertEqual(r.returncode, 5)
        self.assertIn('conflicts', r.stderr)
        p = self.pulls.add('pol', head=self.HEAD)
        self.pulls.prs[p]['mergeStatus'] = 'rejectedByPolicy'
        r = self.merge(p)
        self.assertEqual(r.returncode, 7)
        self.assertEqual(r.stdout.strip(), "human-gate: policy — Azure DevOps branch policy blocks completion: see the PR's policy list")
        d = self.pulls.add('b', head=self.HEAD, draft=True)
        r = self.merge(d)
        self.assertEqual(r.returncode, 65)
        self.assertIn(f'pr ready {d}', r.stderr)
        z = self.pulls.add('c', head=self.HEAD)
        self.pulls.prs[z]['status'] = 'abandoned'
        self.assertEqual(self.merge(z).returncode, 65)
        self.assertEqual(self.requests('PATCH', r'.'), [], 'no completion was sent')
        self.assertEqual(self.forge('pr', 'merge', '1', '--squash', '--delete-branch', '--match-head', 'abc').returncode, 64)
        self.assertEqual(self.forge('pr', 'merge', '1', '--squash', '--match-head', self.HEAD).returncode, 64)

    def test_completion_without_the_pin_is_refused_by_the_service(self):
        # the fake mirrors the observed 400; the verb always sends the pin
        n = self.pulls.add('feat', head=self.HEAD)
        status, _, _ = self.http('PATCH', f'/Factory/_apis/git/repositories/Factory/pullrequests/{n}?api-version=7.1',
                                 {'status': 'completed'})
        self.assertEqual(status, 400)

    def http(self, method, path, body):
        import urllib.request
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method=method,
                                     headers={'Authorization': 'Basic x', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read(), None
        except urllib.error.HTTPError as error:
            return error.code, error.read(), None


class MetadataTests(PrCase):
    def build(self):
        """main: a.txt, b.txt, old.txt, img.bin; feat edits a, adds c, deletes b, renames old, changes the
        binary, adds a file with a space; main then moves on with an unrelated commit."""
        self.init_repo()
        a = ''.join(f'line {i}\n' for i in range(1, 11))
        put_file(self.work / 'a.txt', a)
        put_file(self.work / 'b.txt', 'one\ntwo\nthree\n')
        put_file(self.work / 'old.txt', ''.join(f'keep {i}\n' for i in range(20)))
        put_file(self.work / 'img.bin', b'\x00\x01\x02binary\x00')
        self.commit('base')
        self.git('push', '-q', 'origin', 'main')
        self.git('checkout', '-q', '-b', 'feat')
        put_file(self.work / 'a.txt', a.replace('line 3\n', 'line three\nextra\n'))
        put_file(self.work / 'c.txt', 'new\nfile\n')
        (self.work / 'b.txt').unlink()
        (self.work / 'old.txt').rename(self.work / 'renamed.txt')
        put_file(self.work / 'img.bin', b'\x00\x09\x09other\x00\x00')
        put_file(self.work / 'dir/with space.txt', 'spaced\n')
        self.head = self.commit('feat')
        self.git('push', '-q', 'origin', 'feat')
        self.git('checkout', '-q', 'main')
        put_file(self.work / 'unrelated.txt', 'main moved on\n')
        self.commit('main moves')
        self.git('push', '-q', 'origin', 'main')
        # the local clone has not seen origin's state beyond what it pushed; drop local refs the
        # verb must fetch itself
        self.git('update-ref', '-d', 'refs/remotes/origin/feat')
        return self.pulls.add('feat', 'main', head=self.head, labels=['feature', 'qa'], description='the body')

    def test_metadata_and_diff_pass_the_read_validators(self):
        n = self.build()
        meta_text = self.ok('pr', 'metadata', str(n))
        meta = json.loads(meta_text)
        github_read.validate(meta_text, 'metadata')
        files = {f['path']: (f['additions'], f['deletions']) for f in meta['files']}
        self.assertEqual(files, {'a.txt': (2, 1), 'b.txt': (0, 3), 'c.txt': (2, 0), 'old.txt': (0, 20),
                                 'renamed.txt': (20, 0), 'img.bin': (0, 0), 'dir/with space.txt': (1, 0)})
        self.assertEqual((meta['additions'], meta['deletions'], meta['changedFiles']), (25, 24, 7))
        self.assertEqual(meta['labels'], [{'name': 'feature'}, {'name': 'qa'}])
        self.assertEqual(len(self.requests('GET', r'/pullRequests/1/labels$|/pullrequests/1/labels$')), 1)
        self.assertEqual(meta['body'], 'the body')
        self.assertNotIn('unrelated.txt', files, 'the diff is against the merge base, not the moved target')
        meta_path = self.root / 'meta.json'
        meta_path.write_text(meta_text, encoding='utf-8')
        r = self.forge('pr', 'diff', str(n), '--meta', str(meta_path))
        self.assertEqual(r.returncode, 0, r.stderr)
        github_read.validate(r.stdout, 'diff', meta)
        self.assertEqual(r.stdout.count('diff --git '), 7)
        self.assertIn('Binary files', r.stdout)
        self.assertNotIn('rename from', r.stdout)
        self.assertNotIn('unrelated', r.stdout)
        self.assertEqual(self.forge('pr', 'diff', str(n)).returncode, 64)

    def test_diff_against_stale_metadata_is_refused(self):
        n = self.build()
        meta = json.loads(self.ok('pr', 'metadata', str(n)))
        meta['additions'] += 1
        path = self.root / 'meta.json'
        path.write_text(json.dumps(meta), encoding='utf-8')
        r = self.forge('pr', 'diff', str(n), '--meta', str(path))
        self.assertEqual(r.returncode, 69)
        self.assertIn('failed validation', r.stderr)

    def test_overflowed_body_is_whole_in_metadata(self):
        n = self.build()
        body = sample_body(100)
        self.ok('pr', 'set-body', str(n), '--body-file', self.put(body))
        self.assertEqual(self.js('pr', 'metadata', str(n))['body'], body)

    def test_empty_pr(self):
        self.init_repo()
        put_file(self.work / 'a.txt', 'x\n')
        sha = self.commit('base')
        self.git('push', '-q', 'origin', 'main')
        self.git('push', '-q', 'origin', 'main:refs/heads/empty')
        n = self.pulls.add('empty', 'main', head=sha)
        text = self.ok('pr', 'metadata', str(n))
        meta = json.loads(text)
        self.assertEqual((meta['files'], meta['additions'], meta['deletions'], meta['changedFiles']), ([], 0, 0, 0))
        path = self.root / 'meta.json'
        path.write_text(text, encoding='utf-8')
        r = self.forge('pr', 'diff', str(n), '--meta', str(path))
        self.assertEqual((r.returncode, r.stdout), (0, ''))

    def test_head_that_moved_between_read_and_fetch_fails(self):
        n = self.build()
        self.pulls.prs[n]['lastMergeSourceCommit'] = {'commitId': 'e' * 40}  # the API is behind the branch
        r = self.forge('pr', 'metadata', str(n))
        self.assertEqual(r.returncode, 69)
        self.assertIn('source commit', r.stderr)
        self.assertEqual(len(self.requests('GET', r'/pullrequests/1$')), 2, 're-read once, then fail')

    def test_unreadable_labels_fail_the_read_never_mean_no_labels(self):
        n = self.build()
        self.fake.inject.append({'match': lambda r: r['path'].endswith('/labels'), 'times': 9, 'status': 500, 'body': {}})
        r = self.forge('pr', 'metadata', str(n))
        self.assertEqual(r.returncode, 79, r.stderr)
        self.assertTrue(self.halt.exists())

    def test_missing_branch_fails_cleanly(self):
        n = self.build()
        self.pulls.prs[n]['sourceRefName'] = 'refs/heads/nope'
        r = self.forge('pr', 'metadata', str(n))
        self.assertEqual(r.returncode, 69)
        self.assertIn('git fetch', r.stderr)


if __name__ == '__main__':
    unittest.main()
