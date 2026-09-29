"""tests.test_unpushed_gate — F-0158 §2.2/§2.3: the built-in ``Stop`` hook refuses a factory
session's exit while its branch's work is off origin, bounded, and stands aside rather than trap
a session it cannot satisfy.

``GateTests`` is T2's fence, ``BoundTests`` is T3's. Each case runs over its own hermetic
``ASF_HOME`` (``mock.patch.object(env, 'ASF_HOME', home)``, the idiom
``tests/test_metrics.py``'s ``test_render_daily_with_a_product_computes_landing`` uses) and a
real git worktree from a copied template (``tests/gitfixture.Template``, the idiom
``tests/test_lifecycle.py``'s ``UNPUBLISHED`` uses) — the same evidence
:func:`asf.workers.lifecycle.unpublished` reads, so the gate is exercised as it runs, not as a
mock of it.
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod
from asf.workers import stopgate
from tests.gitfixture import Template
from tests.test_workers import Home, feature_row

PRODUCT = 'demo'
JOB = 'code-T-0522'
ITEM = 'T-0522'
BRANCH = 'worker/T-0522'


def _git(args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


def _build(root):
    """A bare origin and a clone on ``worker/T-0522``, pushed and clean."""
    origin, wt = os.path.join(root, 'origin.git'), os.path.join(root, 'wt')
    _git(['init', '-q', '--bare', '-b', 'main', origin], root)
    _git(['clone', '-q', origin, wt], root)
    for k, v in (('user.name', 'T'), ('user.email', 't@example.com'), ('commit.gpgsign', 'false')):
        _git(['config', k, v], wt)
    with open(os.path.join(wt, 'seed'), 'w') as f:
        f.write('base\n')
    _git(['add', '-A'], wt)
    _git(['commit', '-qm', 'seed'], wt)
    _git(['push', '-q', 'origin', 'HEAD:main'], wt)
    _git(['checkout', '-q', '-b', BRANCH], wt)
    _git(['push', '-q', '-u', 'origin', BRANCH], wt)


FIXTURE = Template(_build, prefix='stopgate_')


class GateHome(unittest.TestCase):
    """A hermetic ``ASF_HOME`` with one product, over a fresh copy of :data:`FIXTURE`."""

    EXTRA = ''

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='stopgate_home_')
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.object(env, 'ASF_HOME', self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        os.makedirs(os.path.join(env.ASF_HOME, 'state', PRODUCT))
        self.write_product()
        self.root = FIXTURE.fresh()
        self.wt = os.path.join(self.root, 'wt')

    def write_product(self, extra=None):
        with open(env.product_path(PRODUCT), 'w', encoding='utf-8') as f:
            f.write(f'product: {PRODUCT}\nmain: main\n'
                    f'{extra if extra is not None else self.EXTRA}')

    def write_session(self, job=JOB, **fields):
        rec = {'job': job, 'item': ITEM, 'branch': BRANCH, 'worktree': self.wt, 'kind': 'task',
              'started': '2026-09-29T00:00:00Z', 'pid': 1}
        rec.update(fields)
        with open(pool_mod.sessions_path(PRODUCT), 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.wt, check=True, capture_output=True,
                              text=True).stdout.strip()

    def write(self, *names):
        for name in names:
            with open(os.path.join(self.wt, name), 'w') as f:
                f.write('x\n')

    def call(self, job=JOB, product=PRODUCT, payload=None):
        """``(rc, what the runtime would feed back to the model)``."""
        environ = {}
        if job:
            environ['ASF_JOB'] = job
        if product:
            environ['ASF_PRODUCT'] = product
        out = io.StringIO()
        rc = stopgate.run_hook(json.dumps(payload or {}), environ, out=out)
        return rc, out.getvalue()


class GateTests(GateHome):
    """T2 fence: :func:`stopgate.run_hook` refuses a stop that would lose work, and lets one
    through that would not or that is not the gate's business."""

    def test_uncommitted_files_are_refused_and_named(self):
        self.write_session()
        self.write('a', 'b')
        rc, out = self.call()
        self.assertEqual(rc, 2)
        self.assertIn(BRANCH, out)
        self.assertIn('not pushed: 2 uncommitted file(s), 0 unpushed commit(s)', out)
        self.assertIn('git commit -s', out)
        self.assertIn(f'git push origin {BRANCH}', out)
        self.assertIn(ITEM, out)

    def test_committed_but_unpushed_does_not_say_git_add(self):
        self.write_session()
        self.write('c')
        self.git('add', '-A')
        self.git('commit', '-qm', 'local work')
        rc, out = self.call()
        self.assertEqual(rc, 2)
        self.assertNotIn('git add', out)
        self.assertIn(f'git push origin {BRANCH}', out)

    def test_a_clean_pushed_worktree_is_silent(self):
        self.write_session()
        self.assertEqual(self.call(), (0, ''))

    def test_a_clean_never_pushed_branch_is_refused(self):
        self.git('checkout', '-q', '-b', 'worker/T-0523')
        self.write_session(branch='worker/T-0523')
        rc, out = self.call()
        self.assertEqual(rc, 2)
        self.assertIn('git push origin worker/T-0523', out)

    def test_no_asf_job_lets_the_stop_through(self):
        self.write_session()
        self.write('a')
        self.assertEqual(self.call(job=None), (0, ''))

    def test_a_groom_run_lets_the_stop_through(self):
        self.write_session(kind='groom')
        self.write('a')
        self.assertEqual(self.call(), (0, ''))

    def test_a_run_on_main_lets_the_stop_through(self):
        self.write_session(branch='main')
        self.write('a')
        self.assertEqual(self.call(), (0, ''))

    def test_no_worktree_on_disk_lets_the_stop_through(self):
        self.write_session(worktree=os.path.join(self.root, 'nowhere'))
        self.assertEqual(self.call(), (0, ''))

    def test_an_unreadable_registry_lets_the_stop_through_and_names_the_hook(self):
        self.write_session()
        self.write('a')
        with mock.patch.object(pool_mod, 'load_sessions', side_effect=OSError('boom')):
            rc, out = self.call()
        self.assertEqual(rc, 0)
        self.assertIn('unpushed gate', out)


class BoundTests(GateHome):
    """T3 fence: the gate refuses at most ``conventions.stop_gate_rounds`` times, then stands
    aside rather than trap the session."""

    EXTRA = 'conventions:\n  stop_gate_rounds: 2\n'

    def test_bounded_then_stands_aside(self):
        self.write_session()
        self.write('a', 'b')
        rc1, out1 = self.call()
        rc2, out2 = self.call()
        rc3, out3 = self.call()
        self.assertEqual((rc1, rc2, rc3), (2, 2, 0))
        self.assertIn('stands aside', out3)
        self.assertEqual(stopgate.refusals(PRODUCT, JOB), 2)

    def test_stop_hook_active_at_the_limit_also_stands_aside(self):
        self.write_session()
        self.write('a')
        self.call()
        self.call()
        rc, out = self.call(payload={'stop_hook_active': True})
        self.assertEqual(rc, 0)
        self.assertIn('stands aside', out)

    def test_zero_rounds_disables_the_gate_and_writes_no_counter(self):
        self.write_product('conventions:\n  stop_gate_rounds: 0\n')
        self.write_session()
        self.write('a')
        rc, out = self.call()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(stopgate.counter_path(PRODUCT, JOB)))

    def test_a_counter_under_one_job_does_not_bound_another(self):
        self.write_session()
        self.write('a')
        self.call()
        self.call()
        self.assertEqual(stopgate.refusals(PRODUCT, JOB), 2)
        other = 'code-T-0999'
        self.write_session(job=other)
        rc, out = self.call(job=other)
        self.assertEqual(rc, 2)
        self.assertEqual(stopgate.refusals(PRODUCT, other), 1)


class SpawnClearTests(Home):
    """T3 fence: ``spawn`` launching a run under a job removes that job's counter file first."""

    def test_spawn_clears_the_jobs_counter(self):
        stopgate.bump('sample', 'f-0001')
        self.assertTrue(os.path.exists(stopgate.counter_path('sample', 'f-0001')))
        rt = runtime_mod.FakeRuntime([{'running': True, 'pid': 4242}])
        spawn_mod.spawn(self.product, feature_row('f-0001'), self.acct(), 'b',
                        runtime=rt, cfg=self.cfg)
        self.assertFalse(os.path.exists(stopgate.counter_path('sample', 'f-0001')))


if __name__ == '__main__':
    unittest.main()
