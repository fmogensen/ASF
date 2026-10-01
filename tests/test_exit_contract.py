"""tests.test_exit_contract — the session exit contract.

(a) A factory session cannot end dirty or unpushed: the built-in ``unpushed`` Stop hook
(:mod:`asf.workers.stopgate`) is wired into every worker account's settings
(:data:`asf.hooks.ACCOUNT_HOOKS`, at install and at each launch), refuses the stop with the commit,
the product's pre-push check and ONE push, and — bounded — lets the third stop through with an
``unpushed`` defect on the run's ledger line.

(b) The lane writes the trailers, not the session: on publish a factory branch's own commits get
the sign-off and the product's ``commit.trailers`` (:meth:`asf.harvest.lane.Lane.normalise_commits`)
— trunk commits never, trees unchanged.
"""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from asf import env, hooks
from asf.harvest import lane
from asf.workers import pool as pool_mod
from asf.workers import stopgate
from tests import test_lane
from tests.test_lane import LaneFixture, rec, sh
from tests.test_unpushed_gate import BRANCH, JOB, PRODUCT, GateHome


class StopRefusals(GateHome):
    """(a) the stop is refused while work is off origin, then released with a defect."""

    EXTRA = 'conventions:\n  pre_push_check: make lint\n'

    def ledger(self):
        return pool_mod.load_sessions(PRODUCT).get(JOB) or {}

    def test_a_dirty_worktree_is_refused_at_stop(self):
        self.write_session()
        self.write('a.txt')
        rc, out = self.call()
        self.assertEqual(rc, 2)
        self.assertIn('REFUSED', out)
        self.assertIn('git add -A && git commit', out)
        self.assertIn('make lint', out)                       # the pre-push check, by kind
        self.assertLess(out.index('make lint'), out.index(f'git push origin {BRANCH}'))
        self.assertIn('ONE push', out)
        self.assertIn('trailers itself', out)

    def test_an_unpushed_commit_is_refused(self):
        self.write_session()
        self.write('a.txt')
        self.git('add', '-A')
        self.git('commit', '-qm', 'task(T-0522): the work')
        rc, out = self.call()
        self.assertEqual(rc, 2)
        self.assertNotIn('git add -A', out)
        self.assertIn(f'git push origin {BRANCH}', out)

    def test_the_third_stop_is_released_with_an_unpushed_defect_recorded(self):
        self.write_session()
        self.write('a.txt')
        rcs = [self.call()[0] for _ in range(2)]
        self.assertEqual(rcs, [2, 2])
        self.assertNotIn('defect', self.ledger())
        rc, out = self.call()
        self.assertEqual(rc, 0)
        self.assertIn('STOOD ASIDE', out)
        got = self.ledger()
        self.assertEqual(got.get('stop_gate'), 'released')
        self.assertEqual(got.get('stop_refusals'), 2)
        self.assertTrue(got.get('defect', '').startswith('unpushed:'), got.get('defect'))
        self.assertIn(BRANCH, got['defect'])

    def test_a_clean_pushed_session_stops(self):
        self.write_session()
        self.write('a.txt')
        self.git('add', '-A')
        self.git('commit', '-qm', 'task(T-0522): the work')
        self.git('push', '-q', 'origin', BRANCH)
        rc, out = self.call()
        self.assertEqual((rc, out), (0, ''))
        self.assertEqual(stopgate.refusals(PRODUCT, JOB), 0)
        self.assertNotIn('defect', self.ledger())

    def test_a_doc_kind_names_its_own_steps(self):
        self.write_product('conventions:\n  pre_push_check:\n    code: make lint\n'
                           '    spec: [make spec-lint]\n')
        product = env.load_product(PRODUCT)
        self.assertEqual(stopgate.pre_push_commands(product, 'spec'), ['make spec-lint'])
        self.assertEqual(stopgate.pre_push_commands(product, 'task'), ['make lint'])
        self.assertEqual(stopgate.pre_push_commands(product, 'plan'), [])


class StopHookWired(unittest.TestCase):
    """(a) the gate is wired: install and every launch put ``asf hook unpushed`` on ``Stop`` in
    each worker account's own settings, and ``asf hook unpushed`` runs the gate."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='exit_hooks_')
        self.addCleanup(shutil.rmtree, self.base, True)
        self.asf = os.path.join(self.base, 'bin', 'asf')
        os.makedirs(os.path.dirname(self.asf))
        with open(self.asf, 'w') as f:
            f.write('#!/bin/sh\n')
        os.chmod(self.asf, 0o755)

    def account(self):
        return pool_mod.Account('w1', cap=1, config_dir=os.path.join(self.base, 'w1'))

    def settings(self):
        with open(os.path.join(self.base, 'w1', 'settings.json'), encoding='utf-8') as f:
            return json.load(f)

    def test_a_launch_writes_the_stop_gate_into_the_accounts_settings(self):
        acct = self.account()
        self.assertTrue(hooks.ensure_account_hooks(acct, which=lambda _n: self.asf))
        got = self.settings()['hooks']
        self.assertEqual(got['Stop'], [{'hooks': [{'type': 'command',
                                                   'command': f'{self.asf} hook unpushed'}]}])
        self.assertEqual(got['PreToolUse'][0]['hooks'][0]['command'], f'{self.asf} hook approvals')
        # idempotent: a second launch adds nothing
        hooks.ensure_account_hooks(acct, which=lambda _n: self.asf)
        self.assertEqual(self.settings()['hooks'], got)

    def test_no_asf_on_path_writes_nothing(self):
        self.assertFalse(hooks.ensure_account_hooks(self.account(), which=lambda _n: None))
        self.assertFalse(os.path.exists(os.path.join(self.base, 'w1', 'settings.json')))

    def test_asf_hook_unpushed_runs_the_stop_gate(self):
        with mock.patch.object(stopgate, 'run_hook', return_value=2) as gate, \
                mock.patch('sys.stdin') as stdin:
            stdin.read.return_value = '{}'
            rc = hooks.cmd_hook(SimpleNamespace(name='unpushed', product=None))
        self.assertEqual(rc, 2)
        gate.assert_called_once()


class TrailersOnPublish(LaneFixture):
    """(b) the lane adds the sign-off and the product's trailers to the branch's own commits on
    publish — never a trunk commit, the trees unchanged, once."""

    _N = test_lane.NamingRepair
    B, AUTHOR = _N.B, _N.AUTHOR
    push_commits, tip, log = _N.push_commits, _N.tip, _N.log

    def setUp(self):
        super().setUp()
        self.lines = []

    def product(self, **conventions):
        conventions.setdefault('commit', {'signoff': True, 'trailers': {'Refs': '{item}'}})
        return super().product(**conventions)

    def facts(self, old):
        return {'branch': self.B, 'kind': 'code', 'item': 'T-0001', 'head': old,
                'prev': {}, 'run': {'job': 'coder-t-0001'}}

    def test_trailers_are_added_to_own_commits_only_with_an_unchanged_tree(self):
        self.push_main({'t.txt': 't\n'}, 'fix(ci): a trunk commit with no sign-off')
        trunk = self.origin_main()
        old = self.push_commits([('task(T-0001): the door', {'a.txt': 'a\n'}),
                                 ('task(T-0001): the hinge\n\nSigned-off-by: Ada <ada@x>',
                                  {'b.txt': 'b\n'})])
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        f = self.facts(old)
        self.assertTrue(ln.normalise_commits(f))
        new = self.tip()
        self.assertNotEqual(new, old)
        self.assertEqual(f['head'], new)
        bodies = [b for b in self.log('%B%x00', new).split('\x00') if b.strip()]
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            self.assertEqual(body.count('Signed-off-by: Ada <ada@x>'), 1, body)
            self.assertIn('Refs: T-0001', body)
            self.assertIn('the body stays', body)
        # trees, authors, committers, dates and subjects untouched; the trunk commit too
        self.assertEqual(self.log('%T %an %ae %ad %cn %ce %cd %s', new),
                         self.log('%T %an %ae %ad %cn %ce %cd %s', old))
        self.assertEqual(self.origin_main(), trunk)
        self.assertEqual(sh(['git', 'merge-base', 'main', new], cwd=self.origin).stdout.strip(),
                         trunk)
        self.assertEqual(sh(['git', 'log', '-1', '--format=%B', trunk],
                            cwd=self.origin).stdout.strip(),
                         'fix(ci): a trunk commit with no sign-off')
        same, why = lane.trees_identical(self.repo, old, new)
        self.assertTrue(same, why)
        # once: the next pass, the head recorded, or nothing left to add — nothing pushed
        self.assertFalse(ln.normalise_commits(dict(self.facts(new), prev=rec(lane.PUSHED,
                                                                              head=new))))
        self.assertFalse(ln.normalise_commits(self.facts(new)))
        self.assertEqual(self.tip(), new)

    def test_a_live_session_or_a_foreign_branch_is_never_rewritten(self):
        old = self.push_commits([('task(T-0001): the door', {'a.txt': 'a\n'})])
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        ln = lane.Lane(self.product(), self.state_dir, out=self.lines.append)
        self.assertFalse(ln.normalise_commits(dict(self.facts(old), live=True)))
        self.assertFalse(ln.normalise_commits(dict(self.facts(old), kind=None)))
        self.assertEqual(self.tip(), old)

    def test_no_trailer_convention_reads_nothing(self):
        old = self.push_commits([('task(T-0001): the door', {'a.txt': 'a\n'})])
        ln = lane.Lane(self.product(commit={}), self.state_dir, out=self.lines.append)
        with mock.patch.object(lane, 'normalise_branch') as nb:
            self.assertFalse(ln.normalise_commits(self.facts(old)))
        nb.assert_not_called()
        self.assertEqual(self.tip(), old)


if __name__ == '__main__':
    unittest.main()
