"""A PR's checks are its exact head's check runs from every event, and a red head is a correct round.

* :func:`asf.harvest.lane.pr_checks` with ``head`` joins the head's check runs
  (``commits/<sha>/check-runs``) to the PR rollup: a ``workflow_dispatch`` run the CI queue
  started on the head (never in the rollup) judges, newest per workflow and name;
* a PR in PR_OPEN/REVIEW whose exact head has a required check that failed goes BACK with a
  ``gate`` correction naming those checks (T5c) — never another review round or a rerun of the
  item's delivery job — and its NEXT row names them.
"""
import json
import unittest
from unittest import mock

from asf import env
from asf.feeder import render
from asf.feeder import rows as R
from asf.harvest import harvest, lane

HEAD = 'a' * 40
OTHER = 'b' * 40
PULL = 'https://github.com/o/p/actions/runs/100/job/{}'
DISPATCH = 'https://github.com/o/p/actions/runs/200/job/{}'

# the PR rollup: the pull_request run, cancelled by the CI queue
ROLLUP = [
    {'name': 'gate', 'bucket': 'cancel', 'link': PULL.format(1), 'workflow': 'ci',
     'startedAt': '2026-09-30T07:03:25Z'},
    {'name': 'gate-tests', 'bucket': 'cancel', 'link': PULL.format(2), 'workflow': 'ci',
     'startedAt': '2026-09-30T06:53:46Z'},
    {'name': 'p1-e2e', 'bucket': 'cancel', 'link': PULL.format(3), 'workflow': 'ci',
     'startedAt': '2026-09-30T07:06:38Z'},
]


def run(name, conclusion, job, at, link=DISPATCH, status='completed'):
    return {'name': name, 'status': status, 'conclusion': conclusion,
            'html_url': link.format(job), 'started_at': at}


# every event's runs on the head: the rollup's own and the dispatched ci.yml run after them
HEAD_RUNS = {'check_runs': [
    run('gate', 'cancelled', 1, '2026-09-30T07:03:25Z', link=PULL),
    run('gate', 'success', 11, '2026-09-30T07:29:56Z'),
    run('gate-tests', 'failure', 12, '2026-09-30T07:30:27Z'),
    run('p1-e2e', 'failure', 13, '2026-09-30T07:32:25Z'),
    run('m5-soak', 'cancelled', 14, '2026-09-30T07:30:28Z'),
]}


def fake_gh(rollup=ROLLUP, runs=HEAD_RUNS, calls=None):
    def gh(args):
        if calls is not None:
            calls.append(list(args))
        if args[:2] == ['pr', 'checks']:
            return 1, json.dumps(rollup), ''
        if args[0] == 'api' and '/check-runs' in args[1]:
            return 0, json.dumps(runs), ''
        return 1, '', 'unexpected'
    return gh


class HeadRuns(unittest.TestCase):
    def test_the_rollup_alone_never_sees_the_dispatched_run(self):
        with mock.patch.object(harvest, '_gh', side_effect=fake_gh()):
            state, detail, _ = lane.pr_checks('o/p', 902, ('gate', 'gate-tests', 'p1-e2e'))
        self.assertEqual((state, detail), ('red', 'gate, gate-tests, p1-e2e'))  # cancels only

    def test_a_dispatched_run_on_the_head_judges(self):
        calls = []
        with mock.patch.object(harvest, '_gh', side_effect=fake_gh(calls=calls)):
            state, detail, checks = lane.pr_checks('o/p', 902, ('gate', 'gate-tests', 'p1-e2e'),
                                                   head=HEAD)
            green, _, _ = lane.pr_checks('o/p', 902, ('gate',), head=HEAD)
        self.assertEqual((state, detail), ('red', 'gate-tests, p1-e2e'))
        self.assertEqual(green, 'green')  # the dispatched gate success replaced the cancel
        self.assertIn(['api', f'repos/o/p/commits/{HEAD}/check-runs?per_page=100'], calls)
        gate = [c for c in checks if c['name'] == 'gate']
        self.assertEqual([c['link'] for c in gate], [DISPATCH.format(11)])
        self.assertEqual(gate[0]['workflow'], 'ci')

    def test_a_pending_dispatched_run_is_pending(self):
        runs = {'check_runs': [run('gate', None, 11, '2026-09-30T07:29:56Z',
                                   status='in_progress')]}
        with mock.patch.object(harvest, '_gh', side_effect=fake_gh(runs=runs)):
            self.assertEqual(lane.pr_checks('o/p', 902, ('gate',), head=HEAD)[0], 'pending')

    def test_unreadable_runs_leave_the_rollup(self):
        with mock.patch.object(harvest, '_gh', side_effect=fake_gh(runs='nope')):
            self.assertEqual(lane.pr_checks('o/p', 902, ('gate',), head=HEAD)[:2],
                             ('red', 'gate'))

    def test_only_the_exact_head(self):
        self.assertEqual(lane.exact_head({'head': HEAD, 'pr': {'head': HEAD}}), HEAD)
        self.assertEqual(lane.exact_head({'head': HEAD, 'pr': {'number': 7}}), HEAD)
        self.assertIsNone(lane.exact_head({'head': HEAD, 'pr': {'head': OTHER}}))
        self.assertIsNone(lane.exact_head({'head': None}))


def github_host():
    product = env.Product('p', {'repo_slug': 'o/p',
                                'conventions': {'landing': 'pull-request',
                                                'landing_checks': ['gate', 'gate-tests',
                                                                   'p1-e2e']}})
    host = lane.GitHubHost(product)
    host.lane = mock.Mock(state_dir='/nonexistent', repo=None)
    return host


class HeadRed(unittest.TestCase):
    F = {'branch': 'cloud/T-0042', 'head': HEAD, 'pr': {'number': 902, 'head': HEAD},
         'class': lane.CODE, 'files': ['src/a.ts']}

    def test_the_failed_required_checks_are_named(self):
        host = github_host()
        with mock.patch.object(harvest, '_gh', side_effect=fake_gh()), \
                mock.patch.object(host, 'merge_required',
                                  return_value=(('gate', 'gate-tests', 'p1-e2e'), None)):
            red = host.head_red(self.F, 902)
        self.assertEqual(red['names'], ['gate-tests', 'p1-e2e'])

    def test_a_cancel_alone_is_no_correct_round(self):
        runs = {'check_runs': [run('gate', 'success', 11, '2026-09-30T07:29:56Z'),
                               run('gate-tests', 'cancelled', 12, '2026-09-30T07:30:27Z'),
                               run('p1-e2e', 'success', 13, '2026-09-30T07:32:25Z')]}
        host = github_host()
        with mock.patch.object(harvest, '_gh', side_effect=fake_gh(runs=runs)), \
                mock.patch.object(host, 'merge_required',
                                  return_value=(('gate', 'gate-tests', 'p1-e2e'), None)):
            self.assertIsNone(host.head_red(self.F, 902))

    def test_red_on_the_trunk_too_is_not_its_fault(self):
        host = github_host()
        with mock.patch.object(harvest, '_gh', side_effect=fake_gh()), \
                mock.patch.object(host, 'merge_required',
                                  return_value=(('gate', 'gate-tests', 'p1-e2e'), None)), \
                mock.patch.object(host, 'trunk_red', return_value={'p1-e2e': OTHER}):
            self.assertEqual(host.head_red(self.F, 902)['names'], ['gate-tests'])

    def test_another_head_is_never_read(self):
        host = github_host()
        f = dict(self.F, pr={'number': 902, 'head': OTHER})
        with mock.patch.object(harvest, '_gh', side_effect=AssertionError('no gh call')):
            self.assertIsNone(host.head_red(f, 902))


class RedHeadGoesBack(unittest.TestCase):
    RED = {'names': ['gate-tests', 'p1-e2e'], 'detail': 'gate-tests, p1-e2e', 'checks': []}

    def facts(self, **kw):
        f = {'branch': 'cloud/T-0042', 'item': 'T-0042', 'head': HEAD, 'mode': 'pr',
             'host': True, 'pr': {'number': 902, 'state': 'OPEN', 'head': HEAD},
             'review_required': True, 'review': {}, 'now': 0}
        f.update(kw)
        return f

    def test_t5c_a_red_head_goes_back_before_a_review_round(self):
        rec = {'state': lane.REVIEW, 'head': HEAD, 'pr': 902,
               'reason': 'round 6 wanted: 5-t-0042.md predates the head'}
        self.assertEqual(lane.next_state(rec, self.facts(checks_red=self.RED)),
                         (lane.BACK, 'kind=gate'))
        self.assertEqual(lane.next_state(dict(rec, state=lane.PR_OPEN),
                                         self.facts(checks_red=self.RED)),
                         (lane.BACK, 'kind=gate'))
        self.assertEqual(lane.next_state(rec, self.facts())[0], lane.REVIEW)

    def test_the_hold_names_the_failures(self):
        ln = lane.Lane.__new__(lane.Lane)
        ln.dry_run, ln.results, ln.out = False, {}, lambda *_: None
        ln.host = mock.Mock(slug='o/p')
        f = self.facts(checks_red=self.RED, prev={'state': lane.REVIEW}, correction=None)
        sent = []

        def back(lane_, f_, kind, text, files):
            sent.append((kind, text))
            f_['prev'] = {'state': lane.BACK}
            return 'held'
        with mock.patch.object(lane, 'send_back', side_effect=back), \
                mock.patch.object(lane, 'red_evidence', return_value='\nlog'):
            rec = ln.enter_back(f, 'kind=gate')
        self.assertEqual(sent, [('gate', 'PR #902 checks red: gate-tests, p1-e2e\nlog')])
        self.assertEqual(rec, {'state': lane.BACK})

    def test_t5d_a_pending_review_round_on_a_red_head_turns_into_the_gate_round(self):
        rec = {'state': lane.BACK, 'head': HEAD, 'pr': 902, 'reason': 'kind=review'}
        review = {'kind': 'review', 'text': '7-t-0042.md reads changes'}
        self.assertEqual(lane.next_state(rec, self.facts(checks_red=self.RED, correction=review)),
                         (lane.BACK, 'kind=gate'))
        self.assertEqual(lane.next_state(rec, self.facts(correction=review)),
                         (lane.BACK, 'kind=review'))
        gate = {'kind': 'gate', 'text': 'PR #902 checks red: gate-tests, p1-e2e'}
        self.assertEqual(lane.next_state(dict(rec, reason='kind=gate'),
                                         self.facts(checks_red=self.RED, correction=gate)),
                         (lane.BACK, 'kind=gate'))

    def test_t5d_the_gate_hold_replaces_the_pending_review_hold(self):
        ln = lane.Lane.__new__(lane.Lane)
        ln.dry_run, ln.results, ln.out = False, {}, lambda *_: None
        ln.host = mock.Mock(slug='o/p')
        f = self.facts(checks_red=self.RED, prev={'state': lane.BACK, 'reason': 'kind=review'},
                       correction={'kind': 'review', 'text': 'C list'})
        sent = []

        def back(lane_, f_, kind, text, files):
            sent.append(kind)
            return 'held'
        with mock.patch.object(lane, 'send_back', side_effect=back), \
                mock.patch.object(lane, 'red_evidence', return_value=''):
            ln.enter_back(f, 'kind=gate')
        self.assertEqual(sent, ['gate'])
        self.assertEqual(f['correction']['kind'], 'gate')

    def test_the_red_head_is_read_on_a_new_runs_first_pass_and_every_open_state(self):
        review = {'kind': 'review', 'text': 'C list'}
        self.assertTrue(lane.reads_red(None, True, None))      # a run that ended, no state yet
        self.assertFalse(lane.reads_red(None, False, None))    # a live session: nothing to read
        for st in (lane.PUSHED, lane.BACK, lane.PR_OPEN, lane.REVIEW):
            self.assertTrue(lane.reads_red(st, True, None), st)
            self.assertTrue(lane.reads_red(st, True, review), st)
        self.assertFalse(lane.reads_red(lane.BACK, True, {'kind': 'conflict'}))
        self.assertFalse(lane.reads_red(lane.MERGED, True, None))


class CorrectRowNamesTheFailures(unittest.TestCase):
    def row(self, correction):
        return R.Row(tier=2, kind=R.FIX_CORRECT, item_id='T-0042', feature_id='F-0116',
                     action=R.LAUNCH, brief_kind='correct', branch='cloud/T-0042',
                     reason='r', correction=correction)

    def test_a_red_checks_correction_names_them(self):
        cell = render.action_cell(self.row('PR #902 checks red: gate-tests, p1-e2e\n\ngate-tests: '
                                           'https://x\nFAIL'))
        self.assertEqual(cell, 'would launch correct on cloud/T-0042 — against the red check(s): '
                               'gate-tests, p1-e2e')

    def test_any_other_correction_is_unchanged(self):
        self.assertEqual(render.action_cell(self.row('conflict with main')),
                         'would launch correct on cloud/T-0042')


if __name__ == '__main__':
    unittest.main()
