"""PUSHED → LAND needs a branch origin has (round E #23).

A product's T-0654 (2026-10-05): a New Task with no remote ref, no PR and no session read as
"PUSHED → LAND, its branch pushed, waiting to land" — a finished groom run named it, and the run
line alone said "pushed". It never got its coder. A run-line wait now holds only a branch origin
has a head for; unknown (origin cannot be asked) changes nothing."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from asf.tick import step_wave
from asf.workers import lifecycle

ITEM = 'T-0654'


class _Ledger(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, 'sessions.jsonl')

    def add_run(self, job, kind, branch):
        with open(self.path, 'a') as f:
            for ln in ({'job': job, 'item': ITEM, 'kind': kind, 'branch': branch, 'pid': 1,
                        'started': '2026-10-04T10:00:00Z'},
                       {'job': job, 'ended': '2026-10-04T10:10:00Z', 'end_reason': 'finished'}):
                f.write(json.dumps(ln) + '\n')

    def occ(self, on_origin=None):
        return lifecycle.occupancy(self.path, alive=lambda *_a, **_k: False, on_origin=on_origin)


class GroomRunTests(_Ledger):
    """The root cause: the groom run of 10-04 carried the first card it filed as its item."""

    INDEX = {'items': {
        'F-0109': {'id': 'F-0109', 'type': 'feature', 'decided': True, 'state': 'Active',
                   'rank': 1, 'stage': 'building 0/1', 'children': [ITEM]},
        ITEM: {'id': ITEM, 'type': 'task', 'parent': 'F-0109', 'state': 'New',
               'writes': ['src/one.py']}}}

    def test_a_finished_groom_run_never_puts_its_item_in_a_wait(self):
        for kind in ('groom', 'groom-clerk', 'close'):
            with self.subTest(kind=kind):
                open(self.path, 'w').close()
                self.add_run(f'{kind}-2026-10-04', kind, f'groom/{kind}')
                occ = self.occ()
                self.assertNotIn(ITEM, occ['waiting_landing'])
                self.assertNotIn(ITEM, lifecycle.unlanded(self.path))
                self.assertNotIn(ITEM, lifecycle.awaiting_harvest(self.path))

    def test_its_item_gets_its_coder(self):
        self.add_run('groom-2026-10-04', 'groom', 'groom/2026-10-04')
        from asf.feeder import rows
        from tests.test_feeder import product
        rs = rows.candidates(self.INDEX, product(), [], occupancy=self.occ())
        acts = {(r.kind, r.item_id): r.action for r in rs}
        self.assertEqual(acts.get((rows.PLAN_CODE, ITEM)), rows.LAUNCH, acts)
        self.assertNotIn((rows.PUSHED_LAND, ITEM), acts)


class PushedNeedsOriginTests(_Ledger):

    def setUp(self):
        super().setUp()
        self.add_run('coder-t-0654', 'coder', 'cloud/T-0654')

    def test_a_branch_origin_lacks_is_not_waiting_to_land(self):
        occ = self.occ(lambda branches: set())
        self.assertNotIn(ITEM, occ['waiting_landing'])
        self.assertNotIn('cloud/T-0654', occ['branches'])

    def test_a_branch_origin_has_still_waits(self):
        occ = self.occ(lambda branches: set(branches))
        self.assertEqual(occ['waiting_landing'][ITEM], lifecycle.PUSHED_WAIT)

    def test_unknown_changes_nothing(self):
        self.assertIn(ITEM, self.occ(lambda branches: None)['waiting_landing'])
        self.assertIn(ITEM, self.occ(None)['waiting_landing'])

    def test_the_question_names_only_run_line_waits(self):
        asked = []
        self.occ(lambda branches: asked.extend(branches) or set(branches))
        self.assertEqual(asked, ['cloud/T-0654'])


def git(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True)


class OnOriginTests(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        origin, repo = os.path.join(self.d, 'origin.git'), os.path.join(self.d, 'repo')
        git('init', '-q', '--bare', origin, cwd=self.d)
        git('init', '-q', '-b', 'main', repo, cwd=self.d)
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
            '-m', 'root', cwd=repo)
        git('remote', 'add', 'origin', origin, cwd=repo)
        git('push', '-q', 'origin', 'main', 'main:cloud/T-0001', cwd=repo)
        self.product = SimpleNamespace(repo_dir=repo)

    def test_names_exactly_the_branches_origin_has(self):
        got = step_wave.on_origin(self.product, ['cloud/T-0001', 'cloud/T-0654'])
        self.assertEqual(got, {'cloud/T-0001'})

    def test_no_repo_is_unknown(self):
        self.assertIsNone(step_wave.on_origin(SimpleNamespace(repo_dir=None), ['x']))


if __name__ == '__main__':
    unittest.main()
