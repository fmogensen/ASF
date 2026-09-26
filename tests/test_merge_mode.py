"""``conventions.merge: auto | manual`` — who clicks merge on a green, reviewed PR.

Under ``auto`` the lane merges every open PR on the trunk whose required checks are green and
whose factory review approved its head; the merge-time approval holds are ``auto`` for the
product; a PR no factory item made gets a review session first. Under ``manual`` nothing changes:
the holds stay as the matrix sets them. Red checks never merge, in either mode.
"""
import json
import importlib
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from asf import approvals, doctor, env
from asf.conventions import Conventions
from asf.feeder import rows as feeder_rows
from asf.harvest import deploy, harvest, lane
from asf.views import status
from asf.workers import lifecycle

from tests.test_lane import LaneFixture, sh

build = importlib.import_module('asf.briefs.build')  # the package attribute is a function

HEAD = 'a' * 40


class RecordingHost(lane.GitHubHost):
    """The real ``check_gate`` over a mocked ``gh``; ``merge`` recorded, never run."""

    def __init__(self, product, ln):
        super().__init__(product, ln)
        self.merges = []

    def slots(self):
        return None, None

    def has_queue(self):
        return False

    def merge(self, branch, pr, subject=None):
        self.merges.append((branch, pr))
        return 'c' * 40, 'squash'

    def cancel_ci(self, branch):
        return 0


def pr_product(merge=None, **conv):
    c = {'landing': 'pull-request', 'landing_checks': ['gate'], 'landing_checks_missing': 'wait',
         'amendable_paths': ['.githooks/*', '.github/workflows/*']}
    if merge is not None:
        c['merge'] = merge
    c.update(conv)
    return env.Product('p', {'repo_slug': 'o/p', 'main': 'main', 'conventions': c})


class Switch(unittest.TestCase):
    def test_default_is_manual_and_auto_is_read_from_conventions_merge(self):
        self.assertFalse(Conventions.from_mapping({}).merge_auto())
        self.assertEqual(Conventions.from_mapping({}).merge, 'manual')
        self.assertTrue(Conventions.from_mapping({'merge': 'auto'}).merge_auto())
        self.assertTrue(Conventions.from_mapping({'merge': 'Auto '}).merge_auto())
        self.assertFalse(Conventions.from_mapping({'merge': 'manual'}).merge_auto())

    def test_a_product_file_with_conventions_merge_auto_loads_as_auto(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write(textwrap.dedent("""\
                repo_slug: acme/sample
                repo_dir: /tmp/sample
                main: main
                conventions:
                  landing: pull-request
                  merge: auto
                """))
            with mock.patch.object(env, 'ASF_HOME', home):
                product = env.load_product('sample')
        self.assertTrue(product.conventions.merge_auto())
        self.assertTrue(approvals.merge_auto(product))
        self.assertEqual(status.merge_cell(product), 'auto')
        self.assertEqual(status.merge_cell(pr_product()), 'manual')

    def test_the_doctor_is_red_on_any_other_value_and_it_reads_as_manual(self):
        product = pr_product(merge='sometimes')
        self.assertFalse(product.conventions.merge_auto())
        (finding,) = doctor.check_convention_shapes(product)
        self.assertFalse(finding[0])
        self.assertIn('conventions.merge must be one of auto, manual', finding[1])
        self.assertEqual(doctor.check_convention_shapes(pr_product(merge='auto')), [])

    def test_auto_makes_the_merge_holds_auto_and_manual_keeps_them(self):
        self.assertEqual(approvals.merge_level(pr_product(), 'merge_amendable_set'), 'human-now')
        self.assertEqual(approvals.merge_level(pr_product('auto'), 'merge_amendable_set'), 'auto')
        # a session's own write to the amendable set is no merge: auto never widens it
        self.assertEqual(approvals.merge_level(pr_product('auto'), 'touch_amendable_set'),
                         'human-now')


class Gate(unittest.TestCase):
    """The detached harvest's half over a GATE entry, ``gh`` mocked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='merge_mode_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patch = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def run_gate(self, product, checks, item='T-0001'):
        lines = []
        ln = lane.Lane(product, self.tmp, out=lines.append)
        ln.host = RecordingHost(product, ln)
        f = {'branch': 'fix/hook', 'item': item, 'kind': 'fix', 'head': HEAD, 'run': None,
             'prev': {'state': lane.GATE, 'head': HEAD, 'pr': 7, 'reason': ''},
             'files': ['.githooks/pre-push', '.github/workflows/ci.yml'], 'class': lane.CODE}
        with mock.patch.object(harvest, '_gh', return_value=(0, json.dumps(checks), '')), \
                mock.patch.object(lane, 'has_adjudicate_commit', return_value=False), \
                mock.patch.object(lane, 'send_back',
                                  side_effect=lambda ln_, f_, *a: ln_.results.update(
                                      {f_['branch']: 'back'})):
            ready = lane.precheck(ln, [f])
            if ready:
                lane.gate_set(ln, ready)
        return ln, lines

    GREEN = [{'name': 'gate', 'bucket': 'pass'}]
    RED = [{'name': 'gate', 'bucket': 'fail'}]

    def test_auto_merges_a_green_approved_pr_touching_the_amendable_set(self):
        ln, lines = self.run_gate(pr_product('auto'), self.GREEN)
        self.assertEqual(ln.host.merges, [('fix/hook', 7)])
        self.assertEqual(ln.results, {'fix/hook': 'landed'}, lines)
        (line,) = [x for x in lines if x.startswith('merge: auto')]
        self.assertIn('PR #7', line)
        self.assertIn('.githooks/pre-push', line)
        self.assertIn('.github/workflows/ci.yml', line)
        self.assertEqual(approvals.open_holds(pr_product('auto')), [])

    def test_manual_holds_the_amendable_merge_for_the_operator(self):
        product = pr_product()
        ln, lines = self.run_gate(product, self.GREEN)
        self.assertEqual(ln.host.merges, [])
        self.assertEqual(ln.results, {'fix/hook': 'held'}, lines)
        (hold,) = approvals.open_holds(product)
        self.assertEqual((hold['class'], hold['level']), ('merge_amendable_set', 'human-now'))

    def test_switching_to_auto_releases_the_hold_a_manual_pass_left(self):
        self.run_gate(pr_product(), self.GREEN)
        self.assertEqual(len(approvals.open_holds(pr_product())), 1)
        ln, lines = self.run_gate(pr_product('auto'), self.GREEN)
        self.assertEqual(ln.host.merges, [('fix/hook', 7)])
        self.assertEqual(approvals.open_holds(pr_product('auto')), [])

    def test_red_required_checks_never_merge_under_auto(self):
        ln, lines = self.run_gate(pr_product('auto'), self.RED)
        self.assertEqual(ln.host.merges, [])
        self.assertEqual(ln.results, {'fix/hook': 'back'}, lines)
        self.assertFalse([x for x in lines if x.startswith('merge: auto')])

    def test_pending_checks_never_merge_under_auto(self):
        ln, _lines = self.run_gate(pr_product('auto'), [{'name': 'gate', 'bucket': 'pending'}])
        self.assertEqual(ln.host.merges, [])


class DirectLaneRequiredChecks(unittest.TestCase):
    """2026-09-26: a direct-lane PR (``cloud/direct-*``) reached MERGING with a deploy-required
    suite red — ``gate-tests`` read as "red but not required" because only ``landing_checks``
    judged it. A direct-lane code PR goes through the same required set as any code PR (the
    landing checks plus ``deploy_sha.prod``'s required jobs), and the checks are read again
    right before MERGING: a snapshot that went red while the pass ran never merges."""

    BRANCH = 'cloud/direct-F-0001'
    DEPLOY = {'prod': {'mode': 'manual', 'workflow': 'd.yml',
                       'required_jobs_from': {'file': 'scripts/merge.sh', 'var': 'REQUIRED'}}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='direct_req_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patch = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def product(self):
        return env.Product('p', {'repo_slug': 'o/p', 'main': 'main', 'deploy_sha': self.DEPLOY,
                                 'conventions': {'landing': 'pull-request', 'merge': 'auto',
                                                 'landing_checks': ['gate'],
                                                 'landing_checks_missing': 'wait'}})

    def run_gate(self, answers, read_from=lambda *_a: ['gate', 'gate-tests']):
        """``answers``: the successive ``gh pr checks`` lists (the last one repeats)."""
        product = self.product()
        self.assertEqual(product.conventions.branch_kind(self.BRANCH), 'direct')
        lines = []
        ln = lane.Lane(product, self.tmp, out=lines.append)
        ln.host = RecordingHost(product, ln)
        f = {'branch': self.BRANCH, 'item': 'F-0001', 'kind': 'direct', 'head': HEAD, 'run': None,
             'prev': {'state': lane.GATE, 'head': HEAD, 'pr': 7, 'reason': ''},
             'files': ['src/a.ts', 'src/a.test.ts'], 'class': lane.CODE}
        answers = list(answers)

        def gh(*_a, **_k):
            return 0, json.dumps(answers.pop(0) if len(answers) > 1 else answers[0]), ''

        with mock.patch.object(harvest, '_gh', side_effect=gh), \
                mock.patch.object(deploy, '_read_from', side_effect=read_from), \
                mock.patch.object(lane, 'has_adjudicate_commit', return_value=False), \
                mock.patch.object(lane, 'send_back',
                                  side_effect=lambda ln_, f_, *a: ln_.results.update(
                                      {f_['branch']: 'back'})):
            ready = lane.precheck(ln, [f])
            if ready:
                lane.gate_set(ln, ready)
        return ln, lines

    GREEN = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'pass'},
             {'name': 'site', 'bucket': 'fail'}]
    RED = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'gate-tests', 'bucket': 'fail'},
           {'name': 'site', 'bucket': 'fail'}]

    def test_a_red_deploy_required_check_sends_a_direct_pr_back(self):
        ln, lines = self.run_gate([self.RED])
        self.assertEqual(ln.host.merges, [])
        self.assertEqual(ln.results, {self.BRANCH: 'back'}, lines)
        self.assertFalse([x for x in lines if 'MERGING' in x], lines)
        (info,) = [x for x in lines if 'not required' in x]
        self.assertIn('site', info)
        self.assertNotIn('gate-tests', info)

    def test_the_green_direct_pr_merges(self):
        ln, lines = self.run_gate([self.GREEN])
        self.assertEqual(ln.host.merges, [(self.BRANCH, 7)], lines)

    def test_a_required_check_red_by_merge_time_never_enters_merging(self):
        ln, lines = self.run_gate([self.GREEN, self.RED])
        self.assertEqual(ln.host.merges, [])
        self.assertFalse([x for x in lines if 'MERGING' in x], lines)
        self.assertTrue([x for x in lines if 'gate-tests' in x and 'red' in x], lines)
        self.assertNotEqual(ln.results.get(self.BRANCH), 'landed')

    def test_required_jobs_from_unreadable_at_the_head_is_read_at_the_trunk(self):
        # the file reads at the trunk but not at the PR head (say, not fetched yet): the
        # landing checks alone never stand in for the deploy-required jobs
        ln, lines = self.run_gate(
            [self.RED], read_from=lambda _p, sha, *_a: None if sha == HEAD
            else ['gate', 'gate-tests'])
        self.assertEqual(ln.host.merges, [])
        self.assertEqual(ln.results, {self.BRANCH: 'back'}, lines)

    def test_required_jobs_from_unreadable_anywhere_holds(self):
        ln, lines = self.run_gate([self.GREEN], read_from=lambda *_a: None)
        self.assertEqual(ln.host.merges, [])
        self.assertTrue([x for x in lines if 'required checks unknown' in x], lines)


class ForeignPR(LaneFixture):
    """An open PR on the trunk no factory item made (a person, the product's own session)."""

    def product(self, merge='manual'):
        conv = {'test_command': f'{sys.executable} -m unittest discover -s checks -p test_*.py',
                'reviews_dir': 'reviews', 'landing': 'pull-request', 'merge': merge,
                'lane': {'review': {'code': 'none'}},
                'branch_prefixes': {'code': 'worker/', 'plan': 'plan/', 'spec': 'spec/'}}
        return env.Product('sample', {'repo_dir': self.repo, 'main': 'main', 'repo_slug': 'o/p',
                                      'conventions': conv})

    def run_lane(self, product, prs):
        ln = lane.Lane(product, self.state_dir, out=lambda *_: None, items={},
                       now=time.time())
        ln.host.prs = lambda: prs
        lane.lane_pass(product, self.state_dir, items={}, lane=ln)
        return ln

    PRS = {'hook-fix': {'number': 12, 'state': 'OPEN', 'base': 'main', 'title': 'fix the hook'}}

    def test_auto_adopts_it_and_asks_the_lane_for_a_review_session(self):
        self.push_lane('hook-fix', {'.githooks/pre-push': '#!/bin/sh\n'}, 'fix the pre-push hook')
        self.run_lane(self.product('auto'), self.PRS)
        rec = self.lane_of('hook-fix')
        self.assertEqual((rec['state'], rec['item'], rec['pr']), (lane.REVIEW, 'PR-0012', 12))
        occ = lifecycle.occupancy(os.path.join(self.state_dir, 'sessions.jsonl'))
        self.assertEqual(occ['review']['PR-0012']['branch'], 'hook-fix')
        (row,) = [r for r in feeder_rows.candidates({}, self.product('auto'), [], occupancy=occ)
                  if r.item_id == 'PR-0012']
        self.assertEqual((row.kind, row.brief_kind, row.branch, row.launches),
                         (feeder_rows.PUSHED_REVIEW, 'review', 'hook-fix', True))
        self.assertIn('opened outside the factory', row.reason)
        text = build.foreign_review_text('review', {'item_id': 'PR-0012', 'branch': 'hook-fix',
                                                    'review_path': 'reviews/1-pr-0012.md'})
        self.assertIn('gh pr view 12', text)
        self.assertEqual(build.foreign_review_text('review', {'item_id': 'T-0001'}), '')

    def test_an_approved_review_of_its_head_takes_it_to_the_gate(self):
        self.push_lane('hook-fix', {'.githooks/pre-push': '#!/bin/sh\n'}, 'fix the pre-push hook')
        self.run_lane(self.product('auto'), self.PRS)
        self.push_lane_on('hook-fix', {'reviews/1-pr-0012.md': 'verdict: approved\n'},
                          'review(PR-0012): round 1 — approved')
        self.run_lane(self.product('auto'), self.PRS)
        self.assertEqual(self.lane_of('hook-fix')['state'], lane.GATE)

    def test_manual_never_touches_it(self):
        self.push_lane('hook-fix', {'.githooks/pre-push': '#!/bin/sh\n'}, 'fix the pre-push hook')
        self.run_lane(self.product('manual'), self.PRS)
        self.assertEqual(self.lane_of('hook-fix'), {})

    def push_lane_on(self, branch, files, subject):
        """Add a commit on top of ``branch`` as a review session would."""
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        sh(['git', 'checkout', '-q', '-B', branch, f'origin/{branch}'], cwd=self.worker)
        for rel, text in files.items():
            self.write(self.worker, rel, text)
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', subject], cwd=self.worker, env_=self.ident)
        sh(['git', 'push', '-q', 'origin', branch], cwd=self.worker)


if __name__ == '__main__':
    unittest.main()
