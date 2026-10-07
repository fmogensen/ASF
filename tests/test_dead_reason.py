"""F-0266 — the regression replay of 2026-10-07: a cloud coder run whose commits named no item,
the lane's naming refusal held on it, the workflow run completing ``succeeded`` with no report
commit, and the health pass that ends it. Every read an operator has — the sync's tick-log line,
``cloud-sessions.json``'s ``why``, the run's ``dead_why``, the doctor's dead-sessions line and
the Dead row of ``asf sessions`` — names the refusal; and the before-picture is pinned, so no
later change can quietly restore the bare sentence."""
import time
import unittest
from unittest import mock

from asf.views import sessions as sessions_view
from asf.workers import actions
from asf.workers import cloudpid
from asf.workers import health as health_mod
from asf.workers import observe
from asf.workers import pool as pool_mod
from asf.workers import refusals

try:
    from tests.test_cloud import LogGh, _DeadSync
except ImportError:  # pragma: no cover - `discover -s tests`
    from test_cloud import LogGh, _DeadSync

NAMING = ('commits do not name T-44931: every commit subject on the branch names its item — '
          'the lane could not reword them: 1 commit')
BARE = 'run 500 ended succeeded without the report commit'


class Replay(_DeadSync):
    def replay(self):
        """``(sync line, status-file why, dead row, dead_why, doctor line)`` for the incident."""
        rec = self.launch()
        fake = LogGh()
        self.sync(fake)
        # the lane's naming refusal, held on the run (lifecycle.hold's correction shape)
        pool_mod.update_session(self.product, 'spec-1', correction={
            'kind': 'naming', 'text': NAMING,
            'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        fake.view_ = {'status': 'completed', 'conclusion': 'succeeded'}
        lines = []
        self.sync(fake, lines=lines)
        sync_line = next(l for l in lines if 'spec-1' in l and 'dead:' in l)
        status_why = cloudpid.why(rec['pid'])
        run = pool_mod.load_sessions(self.product)['spec-1']
        row = sessions_view.dead_why(run, self.product)
        real_gh = actions.Gh
        with mock.patch.object(actions, 'Gh', lambda product: real_gh(product, run=fake)):
            health_mod.health(self.product, fix=True, session_source=observe.FakeSource([]),
                              out=lambda s: None, items={})
        run = pool_mod.load_sessions(self.product)['spec-1']
        _ok, doctor = health_mod.dead_census_line(health_mod.dead_census(self.product))
        return sync_line, status_why, row, refusals.dead_reason(run, self.product), doctor, run

    def test_every_read_names_the_refusal(self):
        sync_line, status_why, row, dead_why, doctor, run = self.replay()
        for text in (sync_line, status_why, row, dead_why):
            self.assertIn(BARE + ' — last ASF refusal (naming', text)
            self.assertIn('commits do not name T-44931', text)
        self.assertTrue(run['dead_why'])
        self.assertIn('; refusals: naming 1', doctor)

    def test_the_before_picture(self):
        with mock.patch.object(refusals, 'last', return_value=None):
            sync_line, status_why, row, dead_why, doctor, run = self.replay()
        self.assertTrue(sync_line.endswith(f'dead: {BARE}'), sync_line)
        self.assertEqual(status_why, BARE)
        self.assertEqual(row, BARE)
        self.assertEqual(run['dead_why'], BARE)
        self.assertEqual(dead_why, BARE)
        self.assertNotIn('refusals', doctor)


if __name__ == '__main__':
    unittest.main()
