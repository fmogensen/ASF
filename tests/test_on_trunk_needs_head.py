"""An on-trunk lane record carries the head the trunk holds (round E #24).

A product's lane wrote ``MERGED method=on-trunk`` records with ``head: null`` for many items on
2026-09-28 (T-0006, T-0059, …): a finished run whose branch was gone from origin and that had no
lane record yet was taken as landed on its word (``gone_merged = True``), and the record named
the trunk's tip of the moment as its sha. Such a record now needs a head proven an ancestor of the
trunk — the record's own, else the sha the run's REPORT says it pushed — and carries it."""
import json
import os
import subprocess

from asf.harvest import lane
from tests.test_lane import LaneFixture, sh


def report(sha):
    return (f'```\nREPORT\nitem: T-0001\nkind: coder\nstatus: done\nbranch: worker/T-0001\n'
            f'pushed: yes {sha}\ncommits: one\ntests: none\nleft out: none\n'
            f'needs writes: none\nproves: none\n```')


class OnTrunkNeedsHead(LaneFixture):

    def ledger(self, log_text=None):
        p = subprocess.Popen(['true'])
        p.wait()
        log = os.path.join(self.state_dir, 'coder-t-0001.jsonl')
        if log_text is not None:
            with open(log, 'w') as f:
                f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
                f.write(json.dumps({'type': 'result', 'subtype': 'success',
                                    'result': log_text}) + '\n')
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'a') as f:
            f.write(json.dumps({'job': 'coder-t-0001', 'item': 'T-0001', 'kind': 'coder',
                                'branch': 'worker/T-0001', 'pid': p.pid, 'log': log,
                                'started': '2026-09-28T00:00:00Z'}) + '\n')
            f.write(json.dumps({'job': 'coder-t-0001', 'ended': '2026-09-28T00:05:00Z',
                                'end_reason': 'finished', 'rc': 0}) + '\n')

    def run_lane(self):
        lines = []
        lane.lane_pass(self.product(), self.state_dir, out=lines.append)
        return lines

    def landed_then_gone(self):
        """``worker/T-0001`` pushed, fast-forwarded onto main, then deleted: its head sha."""
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        head = sh(['git', 'rev-parse', 'worker/T-0001'], cwd=self.origin).stdout.strip()
        sh(['git', 'push', '-q', 'origin', f'{head}:refs/heads/main'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', '--delete', 'worker/T-0001'], cwd=self.worker)
        sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=self.repo)
        return head

    def test_a_gone_branch_with_no_record_and_no_head_is_never_on_trunk(self):
        self.push_main({'other.txt': 'x\n'}, 'chore: the trunk moves on')
        self.ledger()
        self.run_lane()
        rec = self.lane_of('worker/T-0001')
        self.assertNotEqual(rec.get('state'), lane.MERGED, rec)

    def test_a_reported_head_the_trunk_holds_lands_with_that_head(self):
        head = self.landed_then_gone()
        self.push_main({'other.txt': 'x\n'}, 'chore: the trunk moves on')
        self.ledger(report(head[:12]))
        self.run_lane()
        rec = self.lane_of('worker/T-0001')
        self.assertEqual((rec.get('state'), rec.get('method')), (lane.MERGED, 'on-trunk'), rec)
        self.assertEqual(rec.get('head'), head)

    def test_a_reported_head_the_trunk_lacks_is_not_landed(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        head = sh(['git', 'rev-parse', 'worker/T-0001'], cwd=self.origin).stdout.strip()
        sh(['git', 'push', '-q', 'origin', '--delete', 'worker/T-0001'], cwd=self.worker)
        self.ledger(report(head))
        self.run_lane()
        self.assertNotEqual(self.lane_of('worker/T-0001').get('state'), lane.MERGED)

    def test_enter_merged_refuses_an_on_trunk_with_no_head(self):
        ln = lane.Lane(self.product(), self.state_dir, out=lambda *_: None, items={})
        ln.trunk_sha = self.origin_main()
        f = {'branch': 'worker/T-0009', 'item': 'T-0009', 'head': None, 'run': None,
             'prev': {}}
        self.assertIsNone(ln.enter_merged(f, 'method=on-trunk'))
        self.assertEqual(self.lane_of('worker/T-0009'), {})


if __name__ == '__main__':
    import unittest
    unittest.main()
