"""A document whose work is pushed and waiting to land is not starved; a run that finished and
exited is not dead.

Seen live: a plan session finished, pushed its branch and exited normally; before health wrote
its ``ended`` line (or after harvest handed the branch to the PR lane) the feeder read the Feature
as "plan-draft, no session" and the next wave launched a second plan session on the same branch.
The status view meanwhile called the finished runs "dead", because their pids were gone."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf.env import Product
from asf.feeder import rows
from asf.workers import lifecycle


def gone_pid():
    p = subprocess.Popen(['true'])
    p.wait()
    return p.pid


def product():
    return Product('sample', {'conventions': {
        'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}})


def index(stage='plan-draft'):
    return {'F-0001': {'id': 'F-0001', 'type': 'feature', 'stage': stage, 'state': 'New',
                       'decided': True}}


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pushed_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, 'sessions.jsonl')

    def log(self, name, result=None):
        path = os.path.join(self.tmp, f'{name}.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            if result is not None:
                f.write(json.dumps(result) + '\n')
        return path

    def write(self, *lines):
        with open(self.path, 'a', encoding='utf-8') as f:
            for line in lines:
                f.write(json.dumps(line) + '\n')

    def plan_run(self, pid, result=None, **extra):
        return dict({'job': 'plan-f-0001', 'item': 'F-0001', 'kind': 'plan', 'pid': pid,
                     'account': 'w1', 'branch': 'plan/F-0001', 'started': '2026-09-24T05:26:25Z',
                     'log': self.log('plan-f-0001', result)}, **extra)

    def feeder_rows(self, open_branches=None):
        return rows.candidates(index(), product(), lifecycle.inflight(self.path),
                               busy=lifecycle.awaiting_harvest(self.path),
                               unlanded=lifecycle.unlanded(self.path),
                               open_branches=open_branches)


OK = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done'}


class PushedPlanIsNotStarved(Registry):
    def test_a_pushed_plan_branch_with_an_open_pr_is_not_starved(self):
        self.write(self.plan_run(gone_pid(), OK),
                   {'job': 'plan-f-0001', 'ended': '2026-09-24T05:44:00Z',
                    'end_reason': 'finished'},
                   {'job': 'plan-f-0001', 'harvest': 'pr'})
        got = self.feeder_rows()
        self.assertEqual([(r.kind, r.item_id) for r in got], [(rows.PUSHED_LAND, 'F-0001')])
        self.assertFalse(got[0].launches)
        self.assertIn('plan pushed, PR open, waiting to land', got[0].action)

    def test_an_open_pr_on_the_plan_branch_alone_is_not_starved(self):
        got = self.feeder_rows(open_branches={'plan/F-0001'})
        self.assertEqual([(r.kind, r.launches) for r in got], [(rows.PUSHED_LAND, False)])

    def test_a_finished_unharvested_plan_run_is_not_starved(self):
        # exited on a success result; health has not recorded the end yet
        self.write(self.plan_run(gone_pid(), OK))
        got = self.feeder_rows()
        self.assertFalse([r for r in got if r.launches])
        self.assertNotIn(rows.STARVED_PLAN, [r.kind for r in got])

    def test_a_finished_recorded_plan_run_is_not_starved(self):
        self.write(self.plan_run(gone_pid(), OK),
                   {'job': 'plan-f-0001', 'ended': '2026-09-24T05:44:00Z',
                    'end_reason': 'finished'})
        self.assertFalse([r for r in self.feeder_rows() if r.launches])

    def test_a_truly_idle_plan_draft_is_starved(self):
        got = self.feeder_rows()
        self.assertEqual([(r.kind, r.item_id, r.launches) for r in got],
                         [(rows.STARVED_PLAN, 'F-0001', True)])
        self.assertEqual(got[0].reason, 'plan-draft, no session')

    def test_a_landed_plan_run_does_not_hold_the_doc(self):
        self.write(self.plan_run(gone_pid(), OK),
                   {'job': 'plan-f-0001', 'ended': '2026-09-24T05:44:00Z',
                    'end_reason': 'finished'},
                   {'job': 'plan-f-0001', 'harvest': 'pr', 'harvested': 'abc123'})
        self.assertEqual([r.kind for r in self.feeder_rows()], [rows.STARVED_PLAN])

    def test_a_pushed_spec_does_not_hold_the_plan(self):
        self.write(dict(self.plan_run(gone_pid(), OK), job='spec-f-0001', kind='spec',
                        branch='spec/F-0001'),
                   {'job': 'spec-f-0001', 'ended': '2026-09-24T05:44:00Z',
                    'end_reason': 'finished'},
                   {'job': 'spec-f-0001', 'harvest': 'pr'})
        self.assertEqual([r.kind for r in self.feeder_rows()], [rows.STARVED_PLAN])

    def test_a_dead_plan_run_leaves_the_plan_starved(self):
        self.write(self.plan_run(gone_pid(), None))
        self.assertEqual([r.kind for r in self.feeder_rows()], [rows.STARVED_PLAN])


class FinishedIsNotDead(Registry):
    def test_a_success_result_with_an_exited_pid_is_finished_not_dead(self):
        self.write(self.plan_run(gone_pid(), OK))
        run = lifecycle.latest(self.path)['plan-f-0001']
        self.assertTrue(lifecycle.finished_unrecorded(run))
        self.assertFalse(lifecycle.occupies(run))   # still no load: the dead-run rule holds
        self.assertEqual(lifecycle.inflight(self.path), [])

    def test_no_result_with_an_exited_pid_is_dead(self):
        self.write(self.plan_run(gone_pid(), None))
        run = lifecycle.latest(self.path)['plan-f-0001']
        self.assertFalse(lifecycle.finished_unrecorded(run))
        self.assertFalse(lifecycle.occupies(run))

    def test_a_failed_result_with_an_exited_pid_is_not_finished(self):
        self.write(self.plan_run(gone_pid(), dict(OK, is_error=True)))
        self.assertFalse(lifecycle.finished_unrecorded(lifecycle.latest(self.path)['plan-f-0001']))

    def test_a_live_pid_is_not_finished(self):
        self.write(self.plan_run(os.getpid(), OK))
        self.assertFalse(lifecycle.finished_unrecorded(lifecycle.latest(self.path)['plan-f-0001']))


if __name__ == '__main__':
    unittest.main()
