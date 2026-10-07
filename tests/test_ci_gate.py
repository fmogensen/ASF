"""``conventions.harvest.gate_where: ci`` — the fast-forward gate run on the product's own CI
instead of a throwaway worktree on this host (:mod:`asf.ci_gate`).

``Switch`` is the convention alone: the new key parses independently of the taken
``harvest.gate``, a value outside the two words is a shape finding and never a silent default,
and ``ci_gate``'s own knobs merge over their defaults like every other map convention.
``Cut`` drives the gate itself, on a fast-forward lane: the combined head is pushed once as a
gate ref, every member waits on CI, and the clause this card is named for — ``H.product_gate`` is
never called — holds even while a conflicting branch still goes back exactly as it does today.
``HostPressure`` is B-0109's other half: a product gating on CI is let through host pressure
without the host ever being read.
"""
import json
import os
import re
import unittest
from unittest import mock

from asf import ci_gate, conventions, env
from asf.harvest import harvest, lane
from asf.workers import host, lifecycle

from tests.test_lane import LaneFixture, sh


class Switch(unittest.TestCase):
    def test_gate_where_defaults_to_local_and_nothing_else_moves(self):
        self.assertEqual(conventions.Conventions().gate_where, 'local')
        self.assertEqual(conventions.Conventions.from_mapping({'harvest': {}}),
                         conventions.Conventions())

    def test_gate_and_gate_where_parse_independently(self):
        c = conventions.Conventions.from_mapping(
            {'harvest': {'gate': 'per-branch', 'gate_where': 'ci'}})
        self.assertEqual((c.harvest_gate, c.gate_where), ('per-branch', 'ci'))
        self.assertEqual(c.shape_findings(), [])

    def test_an_unrecognised_value_is_a_shape_finding_never_a_silent_default(self):
        c = conventions.Conventions.from_mapping({'harvest': {'gate_where': 'remote'}})
        self.assertEqual(c.gate_where, 'local')
        findings = dict(c.shape_findings())
        self.assertIn('harvest.gate_where', findings)
        self.assertIn('local', findings['harvest.gate_where'])
        self.assertIn('ci', findings['harvest.gate_where'])

    def test_ci_gate_is_a_map_convention_and_a_scalar_block_is_misshapen(self):
        self.assertIn('ci_gate', conventions.MAP_CONVENTIONS)
        bad = conventions.Conventions.from_mapping({'ci_gate': 'yes'})
        self.assertEqual([k for k, _ in bad.shape_findings()], ['ci_gate'])
        self.assertEqual(ci_gate.settings(bad), ci_gate.DEFAULTS)

    def test_settings_over_a_partial_map_keeps_the_other_defaults(self):
        c = conventions.Conventions.from_mapping({'ci_gate': {'run_wait_min': 5}})
        s = ci_gate.settings(c)
        self.assertEqual((s['run_wait_min'], s['ref_prefix'], s['timeout_min']),
                         (5, ci_gate.DEFAULTS['ref_prefix'], ci_gate.DEFAULTS['timeout_min']))

    def test_enabled_reads_gate_where_ci_case_insensitively(self):
        self.assertFalse(ci_gate.enabled(conventions.Conventions()))
        self.assertTrue(ci_gate.enabled(conventions.Conventions.from_mapping(
            {'harvest': {'gate_where': ' CI '}})))


class GateFixture(LaneFixture):
    """:class:`tests.test_lane.LaneFixture`'s bare origin, product checkout and worker clone —
    every test here turns ``harvest.gate_where: ci`` on."""

    def ci_product(self, **harvest_):
        harvest_.setdefault('gate_where', 'ci')
        return self.product(harvest=harvest_)

    def run_of(self, branch):
        return lifecycle.by_branch(os.path.join(self.state_dir, 'sessions.jsonl')).get(branch) or {}


class Cut(GateFixture):
    def test_the_combined_head_is_pushed_as_a_gate_ref_and_the_set_waits_on_ci(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        product = self.ci_product()
        before = self.origin_main()
        lines = []
        with mock.patch.object(
                harvest, 'product_gate',
                side_effect=AssertionError('H.product_gate must not run under a ci gate')):
            results = harvest.run_product_harvest(product, self.state_dir, out=lines.append)
        self.assertEqual(results, {'worker/T-0001': 'waiting'}, lines)
        self.assertEqual(self.origin_main(), before, 'nothing lands until CI judges the gate ref')
        run = self.lane_of('worker/T-0001')
        self.assertEqual(run['state'], lane.WAITING_CI)
        self.assertIn('cut, waiting for CI', run['reason'])
        state = ci_gate.load(self.state_dir)
        gate = state['gates']['code']
        self.assertTrue(gate['ref'].startswith('gate/'), gate['ref'])
        self.assertEqual([m['branch'] for m in gate['members']], ['worker/T-0001'])
        remote = sh(['git', 'ls-remote', self.origin, 'refs/heads/' + gate['ref']]).stdout.split()
        self.assertEqual(remote[0], gate['sha'])

    def test_a_conflicting_branch_still_goes_back_on_the_same_tick(self):
        """Today's conflict handling (:func:`asf.harvest.lane.send_back`) is unchanged: a branch
        that only conflicts with another member stacked ahead of it, never with the trunk alone,
        is rebased onto the trunk and pushed on the spot — the same outcome, the same lines,
        whichever side of the switch cut the combined head (:func:`asf.harvest.lane.combined_head`
        calls the same ``hold`` → ``send_back`` either way)."""

        def scenario(product):
            self.push_lane('worker/T-0001', {'shared.txt': 'one\n'}, 'feat(T-0001): one')
            self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
            self.push_lane('worker/T-0002', {'shared.txt': 'two\n'}, 'feat(T-0002): two')
            self.session('coder-t-0002', 'T-0002', 'worker/T-0002')
            lines = []
            results = harvest.run_product_harvest(product, self.state_dir, out=lines.append)
            # the sha9s differ between the two fresh repos the two runs build; the words don't
            shape = [re.sub(r'\b[0-9a-f]{7,40}\b', '<sha>', ln) for ln in lines
                    if ln.startswith(('held ', 'rebased '))]
            return results, shape

        today_results, today_lines = scenario(self.product())
        self.setUp()   # a fresh origin/repo/worker/state_dir for the ci-gated rerun
        with mock.patch.object(
                harvest, 'product_gate',
                side_effect=AssertionError('H.product_gate must not run under a ci gate')):
            ci_results, ci_lines = scenario(self.ci_product())
        self.assertEqual(today_lines, ci_lines)
        self.assertEqual(today_results['worker/T-0002'], ci_results['worker/T-0002'])
        self.assertNotEqual(ci_results['worker/T-0002'], 'landed')  # today's own outcome, re-asserted
        self.assertEqual(ci_results['worker/T-0001'], 'waiting')
        state = ci_gate.load(self.state_dir)
        self.assertEqual([m['branch'] for m in state['gates']['code']['members']], ['worker/T-0001'])

    def test_dry_run_pushes_nothing_and_writes_nothing(self):
        self.push_lane('worker/T-0001', {'a.txt': 'a\n'}, 'feat(T-0001): a')
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        product = self.ci_product()
        before = self.origin_main()
        with mock.patch.object(
                harvest, 'product_gate',
                side_effect=AssertionError('H.product_gate must not run under a ci gate')):
            harvest.run_product_harvest(product, self.state_dir, dry_run=True,
                                        out=lambda *_a: None)
        self.assertEqual(self.origin_main(), before)
        self.assertFalse(os.path.exists(ci_gate.path(self.state_dir)))
        refs = sh(['git', 'ls-remote', '--heads', self.origin]).stdout
        self.assertNotIn('gate/', refs)


class HostPressure(GateFixture):
    """B-0109 no longer holds a gate that starts no suite on this host (D8)."""

    def test_a_ci_gated_entry_passes_through_without_reading_the_host(self):
        product = self.ci_product()
        ln = lane.Lane(product, self.state_dir, out=lambda *_a: None)
        ready = [{'branch': 'worker/T-0001', 'how': 'local'}]
        with mock.patch.dict(os.environ, {host.READING_ENV: '90 12 87'}), \
                mock.patch.object(env, 'load_config',
                                  side_effect=AssertionError('the host must not be read')):
            out = lane.held_by_host(ln, ready)
        self.assertEqual(out, ready)

    def test_gate_where_local_still_holds_under_pressure(self):
        product = self.product()  # gate_where defaults to local
        lines = []
        ln = lane.Lane(product, self.state_dir, out=lines.append)
        ready = [{'branch': 'worker/T-0001', 'how': 'local'}]
        with mock.patch.dict(os.environ, {host.READING_ENV: '90 12 87'}):
            out = lane.held_by_host(ln, ready)
        self.assertEqual(out, [])
        self.assertTrue(any('host pressure' in ln for ln in lines), lines)
