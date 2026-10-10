"""The kernel's three go-live gaps: parked is invisible everywhere, a launch carries the floor's
real brief, and a reviewer's ``VERDICT:`` lines round-trip through the review ledger."""
import json
import os
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.kernel import actions as A
from asf.kernel import briefs as KB
from asf.kernel import loop, status
from asf.kernel import ports as P
from asf.kernel.apply import NO_VERDICT
from asf.kernel.decide import decide
from asf.kernel.facts import read_facts

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORD = os.path.join(HERE, 'fixtures', 'briefs', 'record')
SPEC = '## Stories\n- S-0009: one\n  - it works\n'


class Parked(unittest.TestCase):

    def test_a_parked_feature_with_a_landed_spec_mints_nothing(self):
        for items in ([B.item('F-0001', priority='later')],
                      [B.item('E-0001', priority='later'), B.item('F-0001', parent='E-0001')]):
            plan = decide(B.facts(items, specs_landed={'F-0001': SPEC}), B.config())
            self.assertEqual(B.of(plan, A.MintStory), [])
            self.assertEqual(B.state(plan, 'F-0001'), State.PARKED)
        plan = decide(B.facts([B.item('F-0001')], specs_landed={'F-0001': SPEC}), B.config())
        self.assertEqual([m.story_id for m in B.of(plan, A.MintStory)], ['S-0009'])

    def test_the_open_pr_of_a_parked_task_gets_no_review_and_no_upkeep(self):
        items = [B.item('S-0001', priority='later'),
                 B.task('T-0001', parent='S-0001', state=State.REVIEW)]
        red = B.check(name='ci', conclusion='failure', run_id=9)
        for pr in (B.pr(7, 'T-0001'), B.pr(7, 'T-0001', behind=True, checks=[red])):
            plan = decide(B.facts(items, prs=[pr]), B.config())
            self.assertEqual(plan.actions, [])
            self.assertEqual(B.state(plan, 'T-0001'), State.PARKED)
        plan = decide(B.facts(items, prs=[B.pr(7, 'T-0001')], reviews=[B.review('T-0001')]),
                      B.config())
        self.assertEqual(B.of(plan, A.EnableAutoMerge), [])

    def test_a_stuck_parked_item_is_not_listed_and_blocks_no_one(self):
        items = [B.task('T-0001', state=State.STUCK, stuck=B.M.Stuck('red', 'ci'),
                        priority='later'),
                 B.task('T-0002', state=State.STUCK, stuck=B.M.Stuck('red', 'ci')),
                 B.task('T-0003', after=['T-0002'], priority='later')]
        facts = B.facts(items)
        plan = decide(facts, B.config())
        summary = loop.summarize(plan, facts)
        self.assertEqual([s['item'] for s in summary['stuck']], ['T-0002'])
        self.assertEqual(summary['stuck'][0]['blocked'], 0)
        self.assertEqual(summary['states'].get('parked'), 2)
        self.assertNotIn('T-0001', [m.item_id for m in B.of(plan, A.MarkStuck)])

    def test_parked_is_never_written_to_the_card(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.READY, priority='later')])
        loop.tick(env.Product('sample', {}), ports=F.ports(record=rec), config=B.config(),
                  state_dir=tempfile.mkdtemp(), out=lambda *_: None)
        self.assertEqual(rec.writes, [])

    def test_a_parked_stuck_drops_its_stuck_and_never_stores_parked(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.STUCK, stuck=B.M.Stuck('red', 'ci'),
                                   priority='later')])
        loop.tick(env.Product('sample', {}), ports=F.ports(record=rec), config=B.config(),
                  state_dir=tempfile.mkdtemp(), out=lambda *_: None)
        (iid, fields), = rec.writes
        self.assertEqual(iid, 'T-0001')
        self.assertEqual(set(fields.values()), {None})
        self.assertNotIn('parked', [str(v) for v in rec.fields['T-0001'].values()])

    def test_a_container_of_parked_children_is_parked_not_launched(self):
        items = [B.item('F-0001'), B.task('T-0001', parent='F-0001', priority='later')]
        plan = decide(B.facts(items), B.config())
        self.assertEqual(B.state(plan, 'F-0001'), State.PARKED)
        self.assertEqual(B.launched(plan), [])


def _product(**conv):
    conventions = {'specs_dir': 'docs/specs', 'plans_dir': 'docs/plans',
                   'reviews_dir': 'docs/reviews', 'review_pattern': 'docs/reviews/{n}-{slug}.md',
                   'pre_push_check': 'make fast-gate'}
    conventions.update(conv)
    return env.Product('sample', {'backlog_dir': RECORD, 'main': 'main',
                                  'repo_slug': 'acme/sample', 'conventions': conventions})


def _briefer(product):
    with open(os.path.join(RECORD, 'index.json'), encoding='utf-8') as f:
        index = json.load(f)

    def make(item, launch, findings=(), pr=None):
        return KB.build(product, launch, item, findings, pr, index=index,
                        repo_facts=lambda *_: None)
    return make


class RealBriefs(unittest.TestCase):

    def launch(self, items, prs=(), reviews=()):
        product = _product()
        rec = F.FakeRecord(items, reviews=reviews)
        sess = F.FakeSessions()
        ports = F.ports(record=rec, github=F.FakeGitHub(prs=prs), sessions=sess,
                        briefer=_briefer(product))
        loop.tick(product, ports=ports, config=B.config(), state_dir=tempfile.mkdtemp(),
                  out=lambda *_: None)
        return sess

    def test_a_build_gets_the_coder_brief_with_the_pre_push_check(self):
        sess = self.launch([B.task('T-0001', writes=['app/checkout/*.py'])])
        (kind, iid, _branch, text), = sess.launched
        self.assertEqual((kind, iid), ('build', 'T-0001'))
        self.assertTrue(text.startswith('Backlog item: T-0001'))
        self.assertIn('THE BOUNDARY IS `writes:`', text)
        self.assertIn('make fast-gate', text)
        self.assertIn('\nREPORT\nitem: T-0001\nkind: coder', text)

    def test_a_review_gets_the_review_brief_ending_in_the_verdict_lines(self):
        sess = self.launch([B.task('T-0001', state=State.REVIEW)],
                           prs=[B.pr(7, 'T-0001', head='h' * 40)])
        (kind, _iid, branch, text), = sess.launched
        self.assertEqual((kind, branch), ('review', 'worker/T-0001'))
        self.assertIn('## Your job: review T-0001', text)
        self.assertIn('kind: review', text)
        self.assertIn('#7, head `%s`' % ('h' * 40), text)
        self.assertTrue(text.rstrip().endswith(KB.VERDICT_RULE.splitlines()[-1]))
        self.assertIn('`VERDICT: approve` or `VERDICT: changes`', text)
        self.assertEqual(sess.meta, [{'pr': 7, 'tree': 'tree-1', 'change': ''}])

    def test_a_fix_round_is_a_correct_brief_naming_the_findings(self):
        sess = self.launch([B.task('T-0001', state=State.REVIEW)], prs=[B.pr(7, 'T-0001')],
                           reviews=[B.review('T-0001', verdict='changes',
                                             findings=['src/a.py:3 — guard the None'])])
        (kind, _iid, _branch, text), = sess.launched
        self.assertEqual(kind, 'build')
        self.assertIn('kind: correct', text)
        self.assertIn('src/a.py:3 — guard the None', text)
        self.assertIn('make fast-gate', text)

    def test_kinds_map_onto_the_floor(self):
        bug, task = B.item('B-0001'), B.task('T-0001')
        from asf.kernel.decide import rebase_finding_for
        self.assertTrue(KB.correction([rebase_finding_for(4)], None).startswith(
            'the PR conflicts with its base'))
        self.assertEqual(KB.brief_kind(A.Launch('build', 'B-0001', 'x'), bug, False), 'fix-bug')
        self.assertEqual(KB.brief_kind(A.Launch('build', 'T-0001', 'x'), task, False), 'coder')
        self.assertEqual(KB.brief_kind(A.Launch('build', 'T-0001', 'x'), task, True), 'correct')
        for k in ('review', 'spec', 'plan'):
            self.assertEqual(KB.brief_kind(A.Launch(k, 'T-0001', 'x'), task, False), k)


REPORT = ('REPORT\nitem: T-0001\nkind: review\nstatus: done\n```\n'
          'VERDICT: %s\nFINDINGS: src/a.py:3 — guard the None\n')


class LedgerRecord(F.FakeRecord):
    """The fake record with the real review ledger (``state/<p>/kernel-reviews.jsonl``)."""

    def __init__(self, items, state_dir):
        super().__init__(items)
        self.real = P.RealRecord(env.Product('sample', {'backlog_dir': state_dir}),
                                 state_dir=state_dir)

    def reviews(self):
        return self.real.reviews()

    def record_review(self, *a):
        self.real.record_review(*a)


class VerdictRoundTrip(unittest.TestCase):

    def setUp(self):
        self.state = tempfile.mkdtemp()

    def run_ticks(self, report):
        rec = LedgerRecord([B.task('T-0001', state=State.REVIEW)], self.state)
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001', tree='t1')])
        sess = F.FakeSessions([B.session('rv', 'T-0001', kind='review', alive=False, ended=True,
                                         result='report', report=report, pr=7, tree_sha='t1')])
        ports = F.ports(record=rec, github=gh, sessions=sess)
        tick = lambda: loop.tick(env.Product('sample', {}), ports=ports,  # noqa: E731
                                 config=B.config(), state_dir=self.state, out=lambda *_: None)
        tick()
        return rec, gh, sess, ports, tick

    def test_parse_verdict(self):
        self.assertEqual(KB.parse_verdict(REPORT % 'approve'),
                         ('approve', ['src/a.py:3 — guard the None']))
        self.assertEqual(KB.parse_verdict('VERDICT: changes\nFINDINGS: none\n'), ('changes', []))
        self.assertIsNone(KB.parse_verdict('verdict: approved (in prose)'))

    def test_an_approve_lands_the_item_on_the_next_tick(self):
        rec, gh, sess, ports, tick = self.run_ticks(REPORT % 'approve')
        with open(os.path.join(self.state, P.REVIEWS_FILE)) as f:
            row = json.loads(f.readline())
        self.assertEqual({k: row[k] for k in ('item', 'pr', 'tree_sha', 'verdict')},
                         {'item': 'T-0001', 'pr': 7, 'tree_sha': 't1', 'verdict': 'approve'})
        facts = read_facts(ports)
        self.assertEqual([(r.item_id, r.tree_sha, r.verdict) for r in facts.reviews],
                         [('T-0001', 't1', 'approve')])
        plan = decide(facts, B.config())
        self.assertEqual(B.state(plan, 'T-0001'), State.LANDING)
        tick()
        self.assertEqual(gh.calls, [('auto_merge', 7)])
        self.assertEqual(rec.fields['T-0001'][P.STATE], 'landing')

    def test_changes_send_the_findings_to_the_fix_round(self):
        rec, gh, sess, ports, tick = self.run_ticks(REPORT % 'changes')
        tick()
        (kind, _iid, _branch, text), = sess.launched
        self.assertEqual(kind, 'build')
        self.assertIn('src/a.py:3 — guard the None', text)

    def test_no_verdict_line_is_an_attempt_and_no_ledger_row(self):
        rec, *_ = self.run_ticks('REPORT\nstatus: done\n')
        self.assertEqual(rec.fields['T-0001'][P.ATTEMPTS], [NO_VERDICT])
        self.assertFalse(os.path.exists(os.path.join(self.state, P.REVIEWS_FILE)))


class ApprovalSurvivesUpdate(unittest.TestCase):

    def test_a_verdict_recorded_with_its_change_survives_a_branch_update(self):
        state = tempfile.mkdtemp()
        rec = LedgerRecord([B.task('T-0001', state=State.REVIEW)], state)
        gh = F.FakeGitHub(prs=[B.pr(7, 'T-0001', tree='t1', change_id='c1')])
        sess = F.FakeSessions([B.session('rv', 'T-0001', kind='review', alive=False, ended=True,
                                         result='report', report=REPORT % 'approve', pr=7,
                                         tree_sha='t1', change_id='c1')])
        ports = F.ports(record=rec, github=gh, sessions=sess)
        tick = lambda: loop.tick(env.Product('sample', {}), ports=ports,  # noqa: E731
                                 config=B.config(), state_dir=state, out=lambda *_: None)
        tick()
        with open(os.path.join(state, P.REVIEWS_FILE)) as f:
            self.assertEqual(json.loads(f.readline())['change_id'], 'c1')
        # GitHub's "update branch" merged trunk in: a new head tree, the same own change
        gh._prs = [B.pr(7, 'T-0001', tree='t2', head='head-2', change_id='c1', auto_merge=True)]
        sess._sessions = []
        tick()
        self.assertEqual(rec.fields['T-0001'][P.STATE], 'landing')
        self.assertEqual(sess.launched, [])
        # a new commit on the PR: a new change, a new review
        gh._prs = [B.pr(7, 'T-0001', tree='t3', head='head-3', change_id='c2', auto_merge=True)]
        tick()
        self.assertEqual(rec.fields['T-0001'][P.STATE], 'review')
        self.assertEqual([k for k, *_ in sess.launched], ['review'])


class FloorApprovals(unittest.TestCase):

    def test_an_old_approval_on_the_head_counts(self):
        repo = tempfile.mkdtemp()
        gh = P.RealGitHub(env.Product('sample', {'repo_slug': 'o/r', 'repo_dir': repo}))
        prs = [B.pr(7, 'T-0001', head='abcdef1234', tree='t1'),
               B.pr(8, 'T-0002', head='0123456789', tree='t2')]
        found = {'T-0001': {'verdict': 'approved', 'head': 'abcdef1'},
                 'T-0002': {'verdict': 'approved', 'head': 'fffffff'}}
        with mock.patch('asf.evidence.review.review_at',
                        side_effect=lambda repo, conv, ref, item, store=None: found[item]):
            got = gh.floor_approvals(prs)
        self.assertEqual(got, [B.M.Review('T-0001', 't1', 'approve', [])])


class StatusFromThePlan(unittest.TestCase):

    def test_status_reads_the_last_ticks_plan_and_live_decides_afresh(self):
        state = tempfile.mkdtemp()
        product = env.Product('sample', {})
        rec = F.FakeRecord([B.task('T-0001', state=State.STUCK, stuck=B.M.Stuck('red', 'ci'))])
        loop.tick(product, ports=F.ports(record=rec), config=B.config(), state_dir=state,
                  out=lambda *_: None)
        self.assertTrue(os.path.exists(os.path.join(state, loop.PLAN_FILE)))

        class NoGitHub(F.FakeGitHub):
            def prs(self):
                raise AssertionError('status read GitHub')
        ports = F.ports(record=rec, github=NoGitHub())
        text = status.status(product, ports=ports, config=B.config(), out=lambda *_: None,
                             state_dir=state)
        self.assertIn('| T-0001 | ci | 0 |', text)
        self.assertIn('| parked | 0 |', text)
        live = status.status(product, ports=ports, config=B.config(), out=lambda *_: None,
                             state_dir=state, live=True)
        self.assertIn('under one tick old', live)  # a plan this fresh is now's facts
        path = os.path.join(state, loop.PLAN_FILE)
        with open(path, encoding='utf-8') as f:
            plan = json.load(f)
        plan['at'] = '2026-01-01T00:00:00Z'  # older than one tick: --live reads afresh
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(plan, f)
        with self.assertRaises(AssertionError):
            status.status(product, ports=ports, config=B.config(), out=lambda *_: None,
                          state_dir=state, live=True)


if __name__ == '__main__':
    unittest.main()
