"""The Definition of Ready (Stage 1, part 1b; 2026-10-10: 36 of 38 cards carried an empty
Acceptance, and builds started on cards that named no test, no file or a dangling blocker).

- A Task or Bug becomes Ready only when its card names a test (Acceptance, or a test module on
  the trunk or in its writes in its Gate block), its writes exist on the trunk or are declared new,
  every ``after:`` id is on the record and not parked, and its parent exists and is not Done.
- Otherwise it stays New with ``dor: <missing>`` (an idle reason, never LIMBO) and the kernel
  launches one ``groom-fill`` session for it, at most ``kernel.dor.fill_per_tick`` a tick, after
  finishing work, and at most ``kernel.dor.max_fills`` a card.
- The session returns a structured verdict code validates (a block missing a required field is
  rejected whole); code applies it: superseded retires the card, fill/reshape write acceptance and
  writes, and the next tick judges the card afresh.
"""
import os
import subprocess
import tempfile
import unittest

from asf import env
from asf.kernel import actions as A
from asf.kernel import briefs, dor, loop, settings
from asf.kernel import ports as P
from asf.kernel.apply import apply
from asf.kernel.decide import IDLE_REASONS, decide

try:
    from kernel import builders as B
    from kernel import fakes as F
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F

State = B.State

TRUNK = frozenset({'asf/a.py', 'asf/b.py', 'tests/test_a.py', 'asf/pkg/c.py'})

GOOD_BODY = '## Description\nwork\n\n## Acceptance\n- [ ] a holds — tests/test_a.py::ATests\n'


def ready_task(iid='T-0001', **kw):
    kw.setdefault('parent', 'F-0001')
    kw.setdefault('writes', ['asf/a.py', 'tests/test_a.py'])
    kw.setdefault('body', GOOD_BODY)
    return B.task(iid, **kw)


def world(*tasks, **kw):
    return B.facts([B.item('F-0001', rank=1)] + list(tasks), trunk_files=TRUNK, **kw)


def cfg(**kw):
    kw.setdefault('dor', True)
    return B.config(**kw)


class Checks(unittest.TestCase):

    def gaps(self, it, items=None, trunk=TRUNK, parked=()):
        items = items or {'F-0001': B.item('F-0001'), it.id: it}
        return dor.missing(it, items, trunk, parked)

    def test_a_full_card_passes(self):
        self.assertEqual(self.gaps(ready_task()), [])

    def test_i_an_empty_acceptance_fails(self):
        it = ready_task(body='## Acceptance\n- [ ] \n')
        self.assertEqual(self.gaps(it), ['no test named in Acceptance or Gate'])

    def test_i_a_dotted_module_in_acceptance_counts(self):
        it = ready_task(body='## Acceptance\n- [ ] python3 -m unittest tests.test_zz.Zed\n')
        self.assertEqual(self.gaps(it), [])

    def test_i_a_gate_block_counts_when_its_module_is_on_trunk_or_in_writes(self):
        gate = ('**Gate**\n\n```sh\npython3 -m unittest -v tests.test_%s\n'
                'python3 tools/run_tests.py\n```\n\n## Acceptance\n- [ ] \n')
        self.assertEqual(self.gaps(ready_task(body=gate % 'a')), [])
        self.assertEqual(self.gaps(ready_task(body=gate % 'new', writes=['asf/a.py', 'tests/test_new.py'],
                                              creates=['tests/test_new.py'])), [])
        self.assertEqual(self.gaps(ready_task(body=gate % 'gone')),
                         ['no test named in Acceptance or Gate'])

    def test_ii_writes_empty_or_absent_from_trunk_fail_unless_declared_new(self):
        self.assertEqual(self.gaps(ready_task(writes=[])), ['writes: empty'])
        self.assertEqual(self.gaps(ready_task(writes=['asf/zz.py'])),
                         ['writes not on trunk: asf/zz.py'])
        self.assertEqual(self.gaps(ready_task(writes=['asf/zz.py'], creates=['asf/zz.py'])), [])
        self.assertEqual(self.gaps(ready_task(writes=['asf/pkg/**', 'asf/pkg/'])), [])
        self.assertEqual(self.gaps(ready_task(writes=['asf/zz.py']), trunk=None), [],
                         'trunk unread: only the emptiness is checked')

    def test_iii_after_ids_must_be_on_the_record_and_not_parked(self):
        it = ready_task(after=['T-0404'])
        self.assertEqual(self.gaps(it), ['after: not on the record: T-0404'])
        other = B.task('T-0002', priority='later')
        it = ready_task(after=['T-0002'])
        items = {'F-0001': B.item('F-0001'), 'T-0001': it, 'T-0002': other}
        self.assertEqual(self.gaps(it, items, parked={'T-0002'}), ['after: parked: T-0002'])

    def test_iv_parent_must_exist_and_not_be_done(self):
        self.assertEqual(self.gaps(ready_task(parent='F-0404')),
                         ['parent F-0404 not on the record'])
        it = ready_task()
        items = {'F-0001': B.item('F-0001', state=State.DONE), 'T-0001': it}
        self.assertEqual(self.gaps(it, items), ['parent F-0001 is Done'])
        self.assertEqual(self.gaps(ready_task(parent=None)), ['no parent'])
        bug = B.item('B-0001', writes=['asf/a.py'], body=GOOD_BODY)
        self.assertEqual(self.gaps(bug), [], 'a machine-filed Bug may have no parent')


class Decide(unittest.TestCase):

    def test_a_ready_card_builds(self):
        plan = decide(world(ready_task()), cfg())
        self.assertEqual(B.launched(plan), ['T-0001'])
        self.assertEqual(plan.dor, {})

    def test_an_unready_card_stays_new_with_its_reason_and_gets_a_groom_fill(self):
        plan = decide(world(ready_task(writes=[])), cfg())
        self.assertEqual(B.state(plan, 'T-0001'), State.NEW)
        self.assertEqual(plan.dor, {'T-0001': 'dor: writes: empty'})
        self.assertIn('dor: writes: empty', plan.notes['T-0001'])
        launches = B.of(plan, A.Launch)
        self.assertEqual([(a.kind, a.item_id, a.branch) for a in launches],
                         [('groom-fill', 'T-0001', 'groom-fill/T-0001')])
        self.assertEqual(launches[0].findings, ['dor: writes: empty'])
        self.assertNotIn('T-0001', plan.limbo)

    def test_off_by_default(self):
        plan = decide(world(ready_task(writes=[])), B.config())
        self.assertEqual(B.launched(plan), ['T-0001'])
        self.assertIsNone(plan.dor)

    def test_groom_fills_are_capped_per_tick_and_come_after_finishing_work_before_builds(self):
        tasks = [ready_task('T-%04d' % n, writes=[]) for n in range(1, 6)]
        tasks.append(B.task('T-0009', state=State.REVIEW, parent='F-0001'))
        f = world(*tasks, prs=[B.pr(9, 'T-0009')])
        plan = decide(f, cfg(dor_fill_per_tick=3, max_sessions=10))
        kinds = [(a.kind, a.item_id) for a in B.of(plan, A.Launch)]
        self.assertEqual(kinds[0], ('review', 'T-0009'))
        self.assertEqual([k for k, _i in kinds[1:]], ['groom-fill'] * 3)
        plan = decide(f, cfg(dor_fill_per_tick=3, max_sessions=1))
        self.assertEqual([(a.kind, a.item_id) for a in B.of(plan, A.Launch)],
                         [('review', 'T-0009')])
        f = world(ready_task('T-0001', writes=[]), ready_task('T-0002'))
        plan = decide(f, cfg(max_sessions=1))
        self.assertEqual([(a.kind, a.item_id) for a in B.of(plan, A.Launch)],
                         [('groom-fill', 'T-0001')], 'a groom-fill is not starved by builds')

    def test_the_idle_alarm_names_the_hold(self):
        plan = decide(world(ready_task(writes=[], dor_fills=2)), cfg(dor_max_fills=3,
                                                                     dor_fill_per_tick=0))
        self.assertEqual(plan.idle['reasons'], [(IDLE_REASONS['dor'], 1)])

    def test_a_live_groom_fill_holds_the_card_new_and_nothing_else_launches(self):
        s = B.session('groom-fill-t-0001', 'T-0001', kind='groom-fill', alive=True)
        plan = decide(world(ready_task(writes=[]), sessions=[s]), cfg())
        self.assertEqual(B.state(plan, 'T-0001'), State.NEW)
        self.assertEqual(B.of(plan, A.Launch), [])
        self.assertNotIn('T-0001', plan.limbo)

    def test_an_ended_groom_fill_is_no_attempt_and_waits_a_tick(self):
        s = B.session('groom-fill-t-0001', 'T-0001', kind='groom-fill', alive=False,
                      ended=True, result='report')
        plan = decide(world(ready_task(writes=[]), sessions=[s]), cfg())
        self.assertEqual(B.state(plan, 'T-0001'), State.NEW)
        self.assertEqual(B.launched(plan), [])
        self.assertEqual([type(a) for a in plan.actions], [A.EndSession])

    def test_spent_fills_make_it_stuck_on_the_operator_judged_afresh(self):
        plan = decide(world(ready_task(writes=[], dor_fills=2)), cfg())
        self.assertEqual(B.state(plan, 'T-0001'), State.STUCK)
        self.assertEqual(B.stuck(plan, 'T-0001').owner, 'operator')
        self.assertTrue(B.stuck(plan, 'T-0001').reason.startswith('dor: writes: empty'))
        recorded = ready_task(state=State.STUCK, dor_fills=2,
                              stuck=B.M.Stuck('dor: writes: empty — 2', 'operator'))
        plan = decide(world(recorded), cfg())
        self.assertEqual(B.launched(plan), ['T-0001'], 'mended by hand: it starts')

    def test_a_fix_round_on_an_open_pr_is_never_held(self):
        it = ready_task(writes=[], state=State.REVIEW)
        f = world(it, prs=[B.pr(3, 'T-0001')], reviews=[B.review('T-0001', verdict='changes')])
        plan = decide(f, cfg())
        self.assertEqual([(a.kind, a.item_id) for a in B.of(plan, A.Launch)],
                         [('build', 'T-0001')])


FILL = """REPORT
status: done

GROOM-FILL
verdict: fill
acceptance: ["a holds — tests/test_a.py::ATests"]
writes: ["asf/a.py", "tests/test_a.py", "asf/new.py (new)"]
risk_raise: none
reason: the plan names them
"""


class Verdict(unittest.TestCase):

    def test_a_fill_parses_and_marks_the_new_paths(self):
        v, why = dor.parse_verdict(FILL)
        self.assertEqual(why, '')
        self.assertEqual((v.verdict, v.acceptance, v.writes, v.creates),
                         ('fill', ['a holds — tests/test_a.py::ATests'],
                          ['asf/a.py', 'tests/test_a.py', 'asf/new.py'], ['asf/new.py']))

    def test_a_missing_required_field_rejects_the_block(self):
        cases = {
            'verdict: maybe': 'verdict',
            'reason: ': 'reason',
            'risk_raise: low': 'risk_raise',
            'acceptance: []': 'acceptance',
            'acceptance: ["it works"]': 'names no test',
            'writes: ': 'writes',
        }
        for line, why in cases.items():
            key = line.split(':')[0]
            text = '\n'.join(line if ln.startswith(key + ':') else ln
                             for ln in FILL.splitlines())
            v, got = dor.parse_verdict(text)
            self.assertIsNone(v, line)
            self.assertIn(why, got, line)
        self.assertEqual(dor.parse_verdict('REPORT\nstatus: done\n'), (None, 'no GROOM-FILL block'))

    def test_superseded_needs_an_id_or_sha(self):
        base = 'GROOM-FILL\nverdict: superseded\nrisk_raise: none\nreason: landed in #12\n'
        self.assertIn('superseded_by', dor.parse_verdict(base)[1])
        self.assertEqual(dor.parse_verdict(base + 'superseded_by: t-0009')[0].superseded_by,
                         'T-0009')
        self.assertEqual(dor.parse_verdict(base + 'superseded_by: ABC1234')[0].superseded_by,
                         'abc1234')

    def test_proceed_needs_only_the_reason(self):
        v, _ = dor.parse_verdict('```\nGROOM-FILL\nverdict: proceed\nrisk_raise: none\n'
                                 'reason: ready as is\n```\n')
        self.assertEqual(v.verdict, 'proceed')


def _end(report, item=None):
    item = item or ready_task(writes=[])
    s = B.session('groom-fill-t-0001', 'T-0001', kind='groom-fill', alive=False, ended=True,
                  result='report', report=report)
    rec = F.FakeRecord([B.item('F-0001', rank=1), item])
    sess = F.FakeSessions([s])
    facts = world(item, sessions=[s])
    apply(decide(facts, cfg()), facts, F.ports(record=rec, sessions=sess), log=lambda *_: None)
    return rec, sess


class Apply(unittest.TestCase):

    def test_the_launch_counts_a_fill_and_leaves_the_card_new(self):
        rec = F.FakeRecord([B.item('F-0001', rank=1), ready_task(writes=[])])
        sess = F.FakeSessions()
        facts = world(ready_task(writes=[]))
        apply(decide(facts, cfg()), facts, F.ports(record=rec, sessions=sess),
              log=lambda *_: None)
        self.assertEqual([(k, i) for k, i, _b, _t in sess.launched], [('groom-fill', 'T-0001')])
        self.assertEqual(rec.fields['T-0001'][P.DOR_FILLS], 1)
        self.assertEqual(rec.fields['T-0001'][P.STATE], 'new')

    def test_a_fill_writes_the_card_and_the_next_tick_builds(self):
        rec, sess = _end(FILL)
        self.assertEqual(rec.filled, [('T-0001', ['a holds — tests/test_a.py::ATests'],
                                       ['asf/a.py', 'tests/test_a.py', 'asf/new.py'],
                                       ['asf/new.py'])])
        items = rec.items()
        plan = decide(B.facts(list(items.values()), trunk_files=TRUNK), cfg())
        self.assertEqual(B.launched(plan), ['T-0001'])

    def test_superseded_retires_the_card(self):
        rec, _ = _end('GROOM-FILL\nverdict: superseded\nsuperseded_by: T-0009\n'
                      'risk_raise: none\nreason: T-0009 does it\n')
        self.assertEqual(rec.superseded, [('T-0001', 'T-0009')])

    def test_a_rejected_verdict_is_a_note_and_writes_nothing(self):
        rec, _ = _end('GROOM-FILL\nverdict: fill\nrisk_raise: none\nreason: x\n')
        self.assertEqual(rec.filled, [])
        self.assertTrue(any('groom-fill verdict rejected: acceptance' in n
                            for n in rec.fields['T-0001'][P.NOTES]))

    def test_risk_raise_high_is_a_note(self):
        rec, _ = _end('GROOM-FILL\nverdict: proceed\nrisk_raise: high\nreason: touches CI\n')
        self.assertTrue(any('raised risk high' in n for n in rec.fields['T-0001'][P.NOTES]))


class Brief(unittest.TestCase):

    def test_the_brief_is_short_names_the_gap_and_the_schema_on_the_light_model(self):
        product = env.Product('sample', {'main': 'main'})
        launch = A.Launch('groom-fill', 'T-0001', 'groom-fill/T-0001', ['dor: writes: empty'])
        b = briefs.build(product, launch, ready_task(writes=[]), launch.findings)
        self.assertEqual(b.kind, 'groom-fill')
        self.assertEqual(b.model, settings.LIGHT_MODEL)
        self.assertIn('What is missing: writes: empty', b.text)
        self.assertIn(dor.VERDICT_SCHEMA, b.text)
        self.assertLess(len(b.text), 4000)


def _git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


class RealRecordWrites(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, 'tasks'))
        for iid, extra in (('T-0001', ''), ('T-0009', '')):
            with open(os.path.join(self.root, 'tasks', iid + '.md'), 'w') as f:
                f.write('---\nid: %s\ntype: task\ntitle: one\nparent: F-0001\n%s---\n'
                        '## Description\nwork\n\n## Acceptance\n- [ ] \n\n## History\n'
                        '- 2026-10-10: created\n' % (iid, extra))
        self.product = env.Product('sample', {'backlog_dir': self.root})

    def rec(self):
        return P.RealRecord(self.product, state_dir=tempfile.mkdtemp())

    def test_fill_card_writes_writes_creates_and_acceptance(self):
        self.rec().fill_card('T-0001', ['a holds — tests/test_a.py::A'],
                             ['asf/a.py', 'asf/n.py'], ['asf/n.py'], 'fill: from the plan')
        it = self.rec().items()['T-0001']
        self.assertEqual(it.writes, ['asf/a.py', 'asf/n.py'])
        self.assertEqual(it.creates, ['asf/n.py'])
        self.assertEqual(dor.acceptance_lines(it.body), ['a holds — tests/test_a.py::A'])
        self.assertIn('groom-fill: acceptance and writes filled', it.body)

    def test_supersede_retires_with_superseded_by_on_both_cards(self):
        self.rec().supersede('T-0001', 'T-0009', 'T-0009 covers it')
        with open(os.path.join(self.root, 'tasks', 'T-0001.md')) as f:
            loser = f.read()
        with open(os.path.join(self.root, 'tasks', 'T-0009.md')) as f:
            winner = f.read()
        self.assertIn('superseded_by: T-0009', loser)
        self.assertIn('removed: "retired: superseded by T-0009', loser.replace("'", '"'))
        self.assertIn('supersedes: [T-0001]', winner)
        it = self.rec().items()['T-0001']
        self.assertEqual(it.state, State.DONE, 'retired: reads as Done')

    def test_trunk_files_reads_origin_main(self):
        repo = tempfile.mkdtemp()
        _git(repo, 'init', '-q', '-b', 'main')
        with open(os.path.join(repo, 'a.py'), 'w') as f:
            f.write('x\n')
        _git(repo, 'add', '.')
        _git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'a')
        _git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        product = env.Product('sample', {'backlog_dir': self.root, 'repo_dir': repo,
                                         'main': 'main'})
        self.assertEqual(P.RealRecord(product, state_dir=repo).trunk_files(),
                         frozenset({'a.py'}))


class StopGate(unittest.TestCase):

    def test_a_groom_fill_session_is_never_held_for_a_push(self):
        from asf.workers import lifecycle
        self.assertFalse(lifecycle.lands({'kind': dor.GROOM_FILL, 'branch': 'groom-fill/T-1'}))


class VerdictOffTheLog(unittest.TestCase):

    def test_the_verdict_is_read_off_the_last_result_that_holds_it(self):
        import json
        log = os.path.join(tempfile.mkdtemp(), 'run.jsonl')
        with open(log, 'w') as f:
            for text in (FILL, 'The stop hook refused; I will not fabricate a commit.'):
                f.write(json.dumps({'type': 'result', 'result': text}) + '\n')
        got = P.report_result(log, lambda t: dor.block(t) is not None)
        self.assertEqual(dor.parse_verdict(got['result'])[0].verdict, 'fill')


class TickLine(unittest.TestCase):

    def test_the_tick_logs_the_hold_by_gap(self):
        held = {'T-1': 'dor: writes: empty; no parent', 'T-2': 'dor: writes: empty',
                'T-3': 'dor: writes not on trunk: a.py'}
        self.assertEqual(loop.dor_line(held), 'DOR: 3 held New — writes: empty 2, '
                         'no parent 1, writes not on trunk 1')
        self.assertEqual(loop.dor_line({}), '')


class Settings(unittest.TestCase):

    def test_the_dor_block_and_its_defaults(self):
        k = settings.read(None)
        self.assertEqual(k['dor'], {'enabled': False, 'fill_per_tick': 3, 'max_fills': 2})
        self.assertEqual(k['models']['groom-fill'], settings.LIGHT_MODEL)
        self.assertEqual(settings.read({'dor': {'enabled': True}})['dor']['enabled'], True)


if __name__ == '__main__':
    unittest.main()
