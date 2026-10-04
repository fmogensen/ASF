"""Ids are taken by a push (asf.record.idclaim): a create-only ref per block on the record repo's
origin, so two writers on two hosts — or a cloud session with no env and no asf CLI — never mint
the same id. Hermetic: a temp bare origin and clones of it, nothing leaves the temp dir."""
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from asf import env
from asf.record import idcheck, idclaim, ids, plan_tasks
from asf.workers import cloud, runtime as runtime_mod, spawn

from tests.test_plan_tasks import FOLDERS, write_item


def git(*args, cwd=None):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True,
                          stdin=subprocess.DEVNULL).stdout.strip()


class Origin:
    def __init__(self, tc):
        self.tmp = tempfile.mkdtemp(prefix='idclaim_')
        tc.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bare = os.path.join(self.tmp, 'origin.git')
        git('init', '-q', '--bare', self.bare)

    def clone(self, name):
        path = os.path.join(self.tmp, name)
        git('clone', '-q', self.bare, path)
        return path

    def refs(self):
        out = git('for-each-ref', '--format=%(refname)', idclaim.REF_NS, cwd=self.bare)
        return sorted(out.split()) if out else []


class ClaimByPush(unittest.TestCase):
    def setUp(self):
        self.o = Origin(self)

    def test_concurrent_claims_never_collide(self):
        clones = [self.o.clone(f'c{i}') for i in range(4)]
        got, errors = [], []

        def worker(i):
            try:
                got.append(idclaim.claim(clones[i % 4], {'T': 10, 'S': 10}, f'job-{i}',
                                         start=5000))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for p in 'TS':
            spans = sorted(b[p] for b in got)
            self.assertEqual(len(spans), 8)
            for (lo1, hi1), (lo2, _hi2) in zip(spans, spans[1:]):
                self.assertLess(hi1, lo2, f'{p} blocks overlap: {spans}')
        self.assertEqual(len(self.o.refs()), 16)

    def test_a_refused_push_moves_to_the_next_number(self):
        a, b = self.o.clone('a'), self.o.clone('b')
        self.assertEqual(idclaim.claim(b, {'T': 50}, 'job-b', start=5000), {'T': (5000, 5049)})
        pushes = []
        real_push, real_fetch = idclaim._push_create, idclaim.fetch
        stale = [True]

        def push(repo, remote, shas):
            ok = real_push(repo, remote, shas)
            pushes.append((sorted(shas), ok))
            return ok

        def fetch(repo, remote='origin'):  # a's first read is stale: it has not seen b's block
            if stale[0]:
                stale[0] = False
                return None
            return real_fetch(repo, remote)

        with mock.patch.object(idclaim, '_push_create', push), \
                mock.patch.object(idclaim, 'fetch', fetch):
            got = idclaim.claim(a, {'T': 50}, 'job-a', start=5000)
        self.assertEqual(pushes[0], (['refs/asf/ids/T-5000'], False))  # origin refused the create
        self.assertEqual(got, {'T': (5050, 5099)})
        msg = git('log', '-1', '--format=%B', 'refs/asf/ids/T-5050', cwd=self.o.bare)
        self.assertIn('claimant: job-a', msg)
        self.assertIn('range: T:5050-5099', msg)

    def test_an_existing_ref_is_never_overwritten(self):
        a = self.o.clone('a')
        idclaim.claim(a, {'B': 1}, 'first', start=7)
        before = git('rev-parse', 'refs/asf/ids/B-0007', cwd=self.o.bare)
        sha = idclaim._commit(a, 'B', 7, 7, 'second')
        self.assertFalse(idclaim._push_create(a, 'origin', {'refs/asf/ids/B-0007': sha}))
        self.assertEqual(git('rev-parse', 'refs/asf/ids/B-0007', cwd=self.o.bare), before)

    def test_an_overlapping_block_under_another_name_is_given_up(self):
        a, b = self.o.clone('a'), self.o.clone('b')
        idclaim.claim(b, {'T': 50}, 'low-floor', start=5000)          # T:5000-5049
        got = idclaim.claim(a, {'T': 10}, 'high-floor', floors={'T': 0}, start=5000)
        self.assertEqual(got, {'T': (5050, 5059)})
        # a writer whose floor put it inside b's block under another name yields and goes above
        with mock.patch.object(idclaim, 'top', side_effect=[5009, 5059]):
            got = idclaim.claim(a, {'T': 5}, 'stale', start=5000)
        self.assertEqual(got, {'T': (5060, 5064)})

    def test_asf_new_claims_its_single_id_by_push(self):
        a = self.o.clone('a')
        for f in FOLDERS:
            os.makedirs(os.path.join(a, f))
        write_item(a, 'T-0004', 'task', 'old')
        b = self.o.clone('b')
        idclaim.claim(b, {'T': 1}, 'other host', floors={'T': 4})     # T-0005 taken elsewhere
        self.assertEqual(ids.mint_id(a, {}, 'task', claim=True), 'T-0006')
        self.assertIn('refs/asf/ids/T-0006', self.o.refs())


class LaunchClaimsItsBlock(unittest.TestCase):
    """Two hosts (two state dirs, two clones of one record origin) reserve a block each."""

    def setUp(self):
        self.o = Origin(self)
        self.home = tempfile.mkdtemp(prefix='idclaim_home_')
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        patcher = mock.patch.object(env, 'state_dir',
                                    lambda p: os.makedirs(os.path.join(self.home, p.name),
                                                          exist_ok=True)
                                    or os.path.join(self.home, p.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def product(self, name):
        return env.Product(name, {'repo_dir': self.o.tmp, 'repo_slug': 'x/y', 'main': 'main',
                                  'backlog_dir': self.o.clone(f'backlog-{name}')})

    def test_two_hosts_never_get_the_same_block(self):
        r1 = spawn.reserve_id_range(self.product('host1'), 'plan-f-0001')
        r2 = spawn.reserve_id_range(self.product('host2'), 'plan-f-0002')
        self.assertEqual(r1, 'S:5000-5049,T:5000-5049,B:5000-5049')
        self.assertEqual(r2, 'S:5050-5099,T:5050-5099,B:5050-5099')  # old code: S:5000-5049 again
        self.assertIn('refs/asf/ids/T-5050', self.o.refs())

    def test_claims_off_keeps_the_local_block(self):
        p = env.Product('host3', {'repo_dir': self.o.tmp, 'repo_slug': 'x/y', 'main': 'main',
                                  'backlog_dir': self.o.clone('b3'),
                                  'conventions': {'flags': {'id_claim': 'off'}}})
        self.assertEqual(spawn.reserve_id_range(p, 'j'), 'S:5000-5049,T:5000-5049,B:5000-5049')
        self.assertEqual(self.o.refs(), [])


class CloudBriefCarriesTheRange(unittest.TestCase):
    def test_the_brief_names_the_claimed_block(self):
        j = runtime_mod.Job('sample', 'plan-f-0001', '/wt', '/b.md', 'opus',
                            env={'BACKLOG_ID_RANGE': 'S:5000-5049,T:5000-5049',
                                 'ASF_SESSION': 'sid-1'},
                            branch='plan/f-0001', base='main')
        text = cloud.cloud_brief('Plan it.\n', j)
        self.assertIn('BACKLOG_ID_RANGE=S:5000-5049,T:5000-5049', text)
        from asf.workers import remote
        text = cloud.cloud_brief('Plan it.\n', j, setting=remote.setting_lines(j))
        self.assertIn('BACKLOG_ID_RANGE=S:5000-5049,T:5000-5049', text)


PLAN = """# Plan F-0001

### Task 1: the reader
stories: S-0001, {story}
writes: asf/record/reader.py

### Task 2: the table
stories: S-0001
writes: asf/views/table.py
"""

DUP_PLAN = """# Plan F-0001

### Task 1: the reader
stories: S-0001
writes: asf/record/reader.py

### Task 2: the reader again
stories: S-0001
writes: asf/record/reader.py
"""


class VerifyOnLand(unittest.TestCase):
    def setUp(self):
        self.o = Origin(self)
        self.root = self.o.clone('record')
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'F-0001', 'feature', 'The reader', parent='E-0001')
        write_item(self.root, 'S-0001', 'story', 'Read it', parent='F-0001')
        idclaim.claim(self.o.clone('host'), {'S': 50, 'T': 50}, 'plan-f-0001', start=5000)
        self.lines = []

    def mint(self, text):
        ev = {'features': {'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md',
                                      'plan_on_main': True, 'spec_on_main': True}}}
        return plan_tasks.mint_plan_tasks(self.root, None, ev, out=self.lines.append,
                                          read_ref=lambda ref: text)

    def test_an_id_outside_the_sessions_block_is_refused(self):
        made = self.mint(PLAN.format(story='S-0900'))
        self.assertEqual(made, [])
        self.assertIn('S-0900 is outside', '\n'.join(self.lines))
        self.assertIn('S:5000-5049', '\n'.join(self.lines))

    def test_an_id_inside_the_sessions_block_lands(self):
        self.assertEqual(len(self.mint(PLAN.format(story='S-5003'))), 2)

    def test_a_heading_reusing_a_record_id_is_refused(self):
        text = PLAN.format(story='S-0001') + '\n## S-0001: something else entirely\n'
        self.assertEqual(self.mint(text), [])
        self.assertIn('S-0001 already exists in the record', '\n'.join(self.lines))

    def test_a_content_duplicate_task_is_flagged_naming_the_first(self):
        made = self.mint(DUP_PLAN)
        self.assertEqual(len(made), 1)
        self.assertIn(f'duplicates {made[0]}', '\n'.join(self.lines))

    def test_a_duplicate_of_an_open_task_is_found(self):
        canonical = {'T-0007': {'meta': {'type': 'task', 'parent': 'F-0001', 'state': 'New',
                                         'stories': ['S-0001'], 'writes': ['a.py']}}}
        self.assertEqual(idcheck.duplicate_task(canonical, 'F-0001', ['S-0001'], ['a.py']),
                         'T-0007')
        self.assertIsNone(idcheck.duplicate_task(canonical, 'F-0001', ['S-0001'], ['b.py']))


if __name__ == '__main__':
    unittest.main()
