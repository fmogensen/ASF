"""PR hygiene is a read-only view over the lane (:mod:`asf.harvest.pr_hygiene`).

What it once classified by itself — a stale unreviewed PR, a conflicting approved one — is the
lane's transitions now (T12 STALE, T9 BACK ``kind=conflict``); the view lists them."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from asf import env
from asf.harvest import pr_hygiene


class HygieneView(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='hygiene_')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.product = env.Product('sample', {'conventions': {}})

    def run_(self, job, item, branch, state, reason='', **extra):
        lane = {'state': state, 'head': 'a' * 40, 'pr': extra.pop('pr', None),
                'at': '2026-09-21T00:00:00Z', 'reason': reason, 'item': item, **extra}
        with open(os.path.join(self.dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'item': item, 'branch': branch, 'pid': None,
                                'started': '2026-09-21T00:00:00Z', 'lane': lane}) + '\n')

    def test_stale_and_conflicting_lane_branches_are_the_rows(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'STALE', 'PR #4 closed unmerged', pr=4)
        self.run_('b', 'T-0002', 'worker/T-0002', 'BACK', 'kind=conflict', pr=5)
        self.run_('c', 'T-0003', 'worker/T-0003', 'BACK', 'kind=gate')
        self.run_('d', 'T-0004', 'worker/T-0004', 'MERGED', 'method=squash', sha='b' * 40)
        self.run_('e', 'T-0005', 'worker/T-0005', 'REVIEW', 'round 1 wanted')
        found = pr_hygiene.rows(self.product, self.dir)
        self.assertEqual([(r['kind'], r['branch']) for r in found],
                         [(pr_hygiene.STALE_CLOSE, 'worker/T-0001'),
                          (pr_hygiene.CONFLICT_REBASE, 'worker/T-0002')])
        self.assertEqual(pr_hygiene.render(found[0]),
                         'STALE → CLOSE  worker/T-0001 (T-0001) PR #4 — PR #4 closed unmerged')

    def test_main_prints_the_rows_and_the_lanes(self):
        self.run_('a', 'T-0001', 'worker/T-0001', 'STALE', 'branch gone', pr=4)
        with mock.patch.object(env, 'load_product', lambda name=None: self.product), \
                mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.dir):
            for argv, want in (([], 'STALE → CLOSE  worker/T-0001 (T-0001) PR #4 — branch gone'),
                               (['--lanes'], '4')):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(pr_hygiene.main(argv), 0)
                self.assertEqual(buf.getvalue().strip(), want)


if __name__ == '__main__':
    unittest.main()
