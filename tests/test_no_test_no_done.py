"""S6 — "no test, no done".

A Story is done only when the ingest has recorded ``proved line N — <test>`` for every acceptance
line, or the line is deferred by a decision the register holds; a Feature is done only when every
child — its Tasks, its Stories and those Stories' Tasks — is. Task closure is necessary, never
sufficient. One class per defect of the 2026-10-04 defect log (19:58, 20:30, 20:40) plus the
F-0106/F-0108 deadlock, the plan-tasks decision check and the one-shot audit.
"""
import contextlib
import io
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf import proves
from asf.evidence import closing
from asf.record import decisions, frontmatter, ingest, plan_tasks
from asf.record import reopen as reopen_mod

FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'decision': 'decisions'}
EMPTY_EV = {'features': {}, 'stories': {}, 'prod_sha': None, 'dev_sha': None,
            'checked': set(), 'main_sha': None, 'merged': {}, 'branches': [], 'ids': {}}
PLAIN_BODY = ("## Description\n\n## Acceptance\n- [ ] \n\n## History\n- 2026-01-01: created\n\n"
              "## Children\n\n## Backlinks\n")


def story_body(bullets, history=()):
    """A Story body: ``bullets`` as (ticked, text), ``history`` as extra History lines."""
    acc = '\n'.join(f"- [{'x' if t else ' '}] {text}" for t, text in bullets)
    hist = '\n'.join(['- 2026-01-01: created'] + list(history))
    return (f"## Description\n\n## Acceptance\n{acc}\n\n## History\n{hist}\n\n"
            "## Children\n\n## Backlinks\n")


def proved(n, task='T-0351', test='tests/test_x.py'):
    return f"- 2026-10-04 17:55 ingest: proved line {n} — {task} ({test})"


def machine(state='New', *extra):
    return ('schema_version: 1', f'state: {state}', 'stage_since: 2026-01-01T00:00:00Z') + extra \
        + ('updated: 2026-01-01T00:00:00Z',)


def landed(*ids):
    return {i: {'branches': [], 'open_prs': [], 'commit': f"{i[-1]}" * 40, 'pr': None,
                'green': True} for i in ids}


class RecordCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='s6_')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for f in FOLDERS:
            os.makedirs(os.path.join(self.root, f))

    def write(self, id_, type_, parent=None, typed=(), state='New', body=PLAIN_BODY, extra=()):
        lines = [f'id: {id_}', f'type: {type_}', f'title: {id_} title']
        if parent:
            lines.append(f'parent: {parent}')
        lines += list(typed) + ['# ---- machine ----'] + list(machine(state, *extra))
        rel = f'{FOLDER_OF[type_]}/{id_}.md'
        with open(os.path.join(self.root, rel), 'w', encoding='utf-8') as f:
            f.write('---\n' + '\n'.join(lines) + '\n---\n' + body)
        return rel

    def decision(self, id_):
        return self.write(id_, 'decision', typed=('decided_by: operator', 'date: 2026-10-01'),
                          body="## Context\n\n## Decision\nDefer it.\n\n## History\n- 2026-10-01: created\n")

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as f:
            return frontmatter.parse(f.read(), path=rel)

    def state(self, rel):
        return self.read(rel)[0]['state']

    def ingest(self, ev):
        with mock.patch.object(ingest.evidence, 'load', return_value=ev), \
                contextlib.redirect_stdout(io.StringIO()):
            return ingest.cmd_ingest(types.SimpleNamespace(fresh=False, product=None), self.root)

    def snapshot(self):
        out = {}
        for f in FOLDERS:
            for name in sorted(os.listdir(os.path.join(self.root, f))):
                with open(os.path.join(self.root, f, name), encoding='utf-8') as fh:
                    out[name] = fh.read()
        return out


class TheRuleIsPure(unittest.TestCase):
    """closing.RULES: the story done rules read `unproved`; an unproved Story is Active."""

    def test_tasks_done_and_a_line_unproved_is_active_not_resolved(self):
        c = closing.state_of('story', closing.Ev(children=(closing.CLOSED,), in_prod=True,
                                                 unproved=(3,)))
        self.assertEqual((c.state, c.rule), (closing.ACTIVE, 'unproved'))

    def test_every_line_proved_closes_as_before(self):
        c = closing.state_of('story', closing.Ev(children=(closing.CLOSED,), in_prod=True))
        self.assertEqual((c.state, c.rule), (closing.CLOSED, 'tasks-closed'))
        c = closing.state_of('story', closing.Ev(children=(closing.RESOLVED,)))
        self.assertEqual((c.state, c.rule), (closing.RESOLVED, 'tasks-resolved'))

    def test_a_typed_landing_does_not_close_an_unproved_story(self):
        c = closing.state_of('story', closing.Ev(landed='a' * 40, green=True, unproved=(1,)))
        self.assertNotEqual(c.state, closing.CLOSED)

    def test_a_feature_with_an_open_child_story_is_not_resolved(self):
        c = closing.state_of('feature', closing.Ev(
            children=(closing.CLOSED, closing.CLOSED, closing.CLOSED, closing.NEW)))
        self.assertNotIn(c.state, (closing.RESOLVED, closing.CLOSED))
        c = closing.state_of('feature', closing.Ev(
            children=(closing.CLOSED, closing.NEW), spec_on_main=True))
        self.assertEqual((c.state, c.rule), (closing.ACTIVE, 'documented'))


class StoryNeedsEveryLineProved(RecordCase):
    """2026-10-04 19:58: S-1158 Resolved by `tasks-resolved` because T-0351 (Closed) lists it,
    though the ingest proved only lines 1–2; line 3 was never proved."""

    def s1158(self, line3='team service_principal', history=(proved(1), proved(2))):
        self.write('E-0001', 'epic')
        self.write('F-0106', 'feature', parent='E-0001')
        s = self.write('S-1158', 'story', parent='F-0106', body=story_body(
            [(True, 'org members'), (True, 'team members'), (False, line3)], history))
        self.write('T-0351', 'task', parent='F-0106', typed=('stories: [S-1158]',))
        return s

    def ev(self):
        return dict(EMPTY_EV, ci=True, ids=landed('T-0351'))

    def test_the_story_stays_active_and_names_the_unproved_line(self):
        s = self.s1158()
        self.assertEqual(self.ingest(self.ev()), 0)
        m = self.read(s)[0]
        self.assertEqual(m['state'], 'Active')
        self.assertEqual(m['evidence'][-1], 'rule: unproved')
        self.assertIn('unproved line 3 — team service_principal (no proof)',
                      m['evidence'])
        self.assertEqual(self.state('tasks/T-0351.md'), 'Closed')   # the Task is done; the Story is not

    def test_every_line_proved_closes_it(self):
        s = self.s1158(history=(proved(1), proved(2), proved(3)))
        self.ingest(self.ev())
        self.assertEqual(self.state(s), 'Closed')

    def test_a_line_deferred_by_a_registered_decision_counts(self):
        self.decision('D-0007')
        s = self.s1158(line3='team service_principal — deferred by D-0007')
        self.ingest(self.ev())
        self.assertEqual(self.state(s), 'Closed')

    def test_a_deferral_to_a_decision_the_register_lacks_does_not(self):
        s = self.s1158(line3='team service_principal — deferred by D-0099')
        self.ingest(self.ev())
        m = self.read(s)[0]
        self.assertEqual(m['state'], 'Active')
        self.assertTrue(any('deferred by D-0099, not in the decision register' in l
                            for l in m['evidence']), m['evidence'])

    def test_a_second_ingest_writes_nothing(self):
        self.s1158()
        self.ingest(self.ev())
        before = self.snapshot()
        self.ingest(self.ev())
        self.assertEqual(self.snapshot(), before)


class PartialClaimIsNotAProof(RecordCase):
    """F-0257 D8/D9: a qualified (``only``/``partial``/``half``/…) ``Proves:`` claim is refused,
    never counted — the Story's evidence says so, while the Story is Active, and ticks nothing."""

    def fixture(self):
        self.write('E-0001', 'epic')
        self.write('F-0001', 'feature', parent='E-0001')
        s = self.write('S-0001', 'story', parent='F-0001',
                       body=story_body([(False, 'line one'), (False, 'line two')]))
        self.write('T-0001', 'task', parent='F-0001', state='Closed',
                   typed=('stories: [S-0001]',))
        return s

    def refused_entry(self):
        return {'line': 2, 'test': 'tests/test_x.py', 'task': 'T-0001', 'pr': 7,
                'sha': 'a' * 40, 'source': 'pr', 'why': 'partial claim: "only"'}

    def ev(self):
        return dict(EMPTY_EV, ci=True, ids=landed('T-0001'), proves={},
                    proves_refused={'S-0001': [self.refused_entry()]})

    def test_the_line_stays_unticked_with_no_history_entry(self):
        s = self.fixture()
        self.ingest(self.ev())
        _meta, body = self.read(s)
        self.assertNotIn(2, proves.proved_lines(body))
        self.assertFalse(proves.is_ticked(body, 2))

    def test_the_refusal_is_said_while_the_story_is_active(self):
        s = self.fixture()
        self.ingest(self.ev())
        meta, _body = self.read(s)
        self.assertEqual(meta['state'], 'Active')
        self.assertIn('claim refused, line 2 not proved — partial claim: "only" — '
                      'T-0001, PR #7 (tests/test_x.py)', meta['evidence'])

    def test_unproved_of_gives_line_2_the_refusal_and_line_1_no_proof(self):
        s = self.fixture()
        _meta, body = self.read(s)
        out = ingest.unproved_of('S-0001', {'body': body}, (), self.ev())
        self.assertEqual([(n, why) for n, _t, why in out],
                         [(1, 'no proof'), (2, 'partial claim: "only"')])
        self.assertEqual(tuple(n for n, _t, _w in out), (1, 2))

    def test_a_story_whose_only_claim_is_refused_does_not_close(self):
        s = self.fixture()
        self.ingest(self.ev())
        self.assertNotEqual(self.state(s), 'Closed')
        self.assertNotIn(self.state('features/F-0001.md'), ('Resolved', 'Closed'))

    def test_the_same_claim_counted_instead_ticks_and_says_no_refusal(self):
        s = self.fixture()
        entry = dict(self.refused_entry())
        del entry['why']
        self.ingest(dict(EMPTY_EV, ci=True, ids=landed('T-0001'),
                         proves={'S-0001': [entry]}))
        meta, body = self.read(s)
        self.assertIn(2, proves.proved_lines(body))
        self.assertTrue(proves.is_ticked(body, 2))
        self.assertFalse(any(l.startswith('claim refused') for l in meta['evidence']))

    def test_no_proves_refused_key_reads_as_today(self):
        s = self.fixture()
        self.ingest(dict(EMPTY_EV, ci=True, ids=landed('T-0001')))
        meta, _body = self.read(s)
        self.assertFalse(any(l.startswith('claim refused') for l in meta['evidence']))


class FeatureCountsItsStories(RecordCase):
    """2026-10-04 20:30: F-0106 stayed Resolved with S-1158 New — `children-resolved` counted
    the Feature's Tasks (3/3 Closed) and ignored its Stories."""

    def ev(self, *tasks):
        # F-0106 had its spec and plan on the trunk: the matched path, whose children were Tasks
        return dict(EMPTY_EV, ci=True, ids=landed(*tasks), features={'f-0106': {
            'alias': None, 'spec': 'origin/main:docs/specs/f-0106.md', 'spec_branch': None,
            'spec_on_main': True, 'spec_review': None, 'plan': 'origin/main:docs/plans/f-0106.md',
            'plan_branch': None, 'plan_on_main': True, 'plan_review': None, 'tasks': {},
            'prs': []}})

    def test_an_open_story_keeps_the_feature_open(self):
        self.write('E-0001', 'epic')
        f = self.write('F-0106', 'feature', parent='E-0001')
        for t in ('T-0001', 'T-0002', 'T-0003'):
            self.write(t, 'task', parent='F-0106')
        self.write('S-1158', 'story', parent='F-0106',
                   body=story_body([(False, 'team service_principal')]))
        self.ingest(self.ev('T-0001', 'T-0002', 'T-0003'))
        self.assertEqual(self.state('stories/S-1158.md'), 'New')
        self.assertNotIn(self.state(f), ('Resolved', 'Closed'))

    def test_the_feature_closes_once_its_story_is_proved(self):
        self.write('E-0001', 'epic')
        f = self.write('F-0106', 'feature', parent='E-0001')
        self.write('T-0001', 'task', parent='F-0106', typed=('stories: [S-1158]',))
        self.write('S-1158', 'story', parent='F-0106',
                   body=story_body([(True, 'it works')], (proved(1, 'T-0001'),)))
        self.ingest(self.ev('T-0001'))
        self.assertEqual(self.state('stories/S-1158.md'), 'Closed')
        self.assertEqual(self.state(f), 'Closed')


class NewTaskUnderAStoryReopensTheFeature(RecordCase):
    """The F-0106/F-0108 deadlock: the Feature read Resolved while new Tasks under its Stories
    (T-0604, T-0605…) were New — no feeder row is built for a done Feature, so they could never
    launch, and `asf reopen` refused on the re-derived Resolved. A Feature's children are its
    Stories' Tasks too: a New one re-derives it to Active, on the building ladder."""

    def test_the_feature_derives_active_and_building_so_the_new_task_can_launch(self):
        from asf.feeder import rows
        self.write('E-0001', 'epic')
        f = self.write('F-0108', 'feature', parent='E-0001', typed=('decided: true',),
                       state='Resolved', extra=('stage: landed',))
        self.write('S-0086', 'story', parent='F-0108', state='Resolved',
                   body=story_body([(True, 'signed DPIA gate')], (proved(1, 'T-0001'),)))
        self.write('T-0001', 'task', parent='F-0108', typed=('stories: [S-0086]',), state='Closed')
        self.write('T-0604', 'task', parent='S-0086', typed=('decided: true',))
        ev = dict(EMPTY_EV, ci=True, ids=landed('T-0001'), features={'f-0108': {
            'alias': None, 'spec': 'origin/main:docs/specs/f-0108.md', 'spec_branch': None,
            'spec_on_main': True, 'spec_review': None, 'plan': 'origin/main:docs/plans/f-0108.md',
            'plan_branch': None, 'plan_on_main': True, 'plan_review': None, 'tasks': {},
            'prs': []}})
        self.ingest(ev)
        m = self.read(f)[0]
        self.assertEqual(m['state'], 'Active')
        self.assertTrue(rows.is_open(m))
        self.assertTrue(m['stage'].startswith('building'), m['stage'])
        self.assertIn(m['stage'].split(' ')[0], rows.BUILD_STAGES)
        self.assertEqual(self.state('tasks/T-0604.md'), 'New')   # not descended onto: still to build


class ReopenAStoryReopensItsFeature(RecordCase):
    """A Story's reopen re-derives its Feature in the same pass: the Feature's done stood on it."""

    def test_both_reopen_in_one_pass(self):
        self.write('E-0001', 'epic')
        f = self.write('F-0106', 'feature', parent='E-0001', state='Closed')
        s = self.write('S-1158', 'story', parent='F-0106', state='Closed', body=story_body(
            [(True, 'org members'), (False, 'team service_principal')], (proved(1),)))
        self.write('T-0351', 'task', parent='F-0106', typed=('stories: [S-1158]',), state='Closed')
        ev = dict(EMPTY_EV, ci=True, ids=landed('T-0351'))
        with mock.patch.object(ingest.evidence, 'load', return_value=ev), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = reopen_mod.cmd_reopen(types.SimpleNamespace(
                id='S-1158', reason='line 3 unproved', product=None), self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state(s), 'Active')
        self.assertNotIn(self.state(f), ('Resolved', 'Closed'))
        self.assertIn('F-0106: reopened — state Closed → ', out.getvalue())
        self.assertIn('its Story S-1158 reopened', self.read(f)[1])
        # and the next plain ingest keeps both open
        self.ingest(ev)
        self.assertEqual(self.state(s), 'Active')
        self.assertNotIn(self.state(f), ('Resolved', 'Closed'))


class ReCreditAndUntick(RecordCase):
    """2026-10-04 20:40: a tick on an already-[x] bullet wrote no `proved line` entry, so a
    hand-ticked line could never be credited; and there was no way to take a tick back."""

    def tree(self, ticked=True, state='New', history=()):
        self.write('E-0001', 'epic')
        self.write('F-0001', 'feature', parent='E-0001')
        self.write('T-0001', 'task', parent='F-0001', typed=('stories: [S-0001]',))
        return self.write('S-0001', 'story', parent='F-0001', state=state,
                          body=story_body([(ticked, 'hand ticked')], history))

    def ev(self):
        return dict(EMPTY_EV, ci=True, ids=landed('T-0001'), proves={'S-0001': [
            {'line': 1, 'test': 'tests/test_t.py', 'task': 'T-0001', 'pr': 12, 'sha': 'a' * 40,
             'source': 'pr'}]})

    def test_a_landed_claim_on_a_hand_ticked_line_writes_its_proved_entry_once(self):
        s = self.tree()
        self.ingest(self.ev())
        body = self.read(s)[1]
        self.assertEqual(body.count('ingest: proved line 1 — T-0001, PR #12 (tests/test_t.py)'), 1)
        self.assertEqual(self.state(s), 'Closed')
        self.ingest(self.ev())
        self.assertEqual(self.read(s)[1].count('ingest: proved line 1'), 1)

    def test_proves_reads_untick_as_cancelling_a_proof(self):
        body = story_body([(False, 'x')], (proved(1), '- 2026-10-04 21:00 untick: line 1 — op'))
        self.assertEqual(proves.proved_lines(body), set())
        self.assertEqual(proves.unproved(body)[0][0], 1)

    def test_untick_flips_the_bullet_records_it_and_reopens_a_closed_story(self):
        s = self.tree(state='Closed', history=())
        with mock.patch.object(ingest.evidence, 'load', return_value=dict(
                EMPTY_EV, ci=True, ids=landed('T-0001'))), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = reopen_mod.cmd_untick(types.SimpleNamespace(
                id='S-0001', line=1, reason='no test', product=None), self.root)
        self.assertEqual(rc, 0)
        meta, body = self.read(s)
        self.assertIn('- [ ] hand ticked', body)
        self.assertIn('untick: line 1 — no test', body)
        self.assertEqual(meta['state'], 'Active')

    def test_untick_refuses_a_line_neither_ticked_nor_proved(self):
        self.tree(ticked=False)
        with contextlib.redirect_stderr(io.StringIO()):
            rc = reopen_mod.cmd_untick(types.SimpleNamespace(
                id='S-0001', line=1, reason=None, product=None), self.root)
        self.assertEqual(rc, 2)

    def test_the_parser_takes_story_and_line(self):
        from asf.cli import build_parser
        args = build_parser().parse_args(['untick', 'S-0001', '3', '--reason', 'r'])
        self.assertEqual((args.command, args.id, args.line, args.reason),
                         ('untick', 'S-0001', 3, 'r'))


class ValidateReadsAParentStory(RecordCase):
    """proves.validate: a Task's `parent: S-xxxx` is one of its Stories."""

    def test_a_claim_on_the_parent_story_is_good(self):
        self.write('S-0001', 'story', body=story_body([(False, 'a'), (False, 'b')]))
        items = {'S-0001': {'type': 'story', 'folder': 'stories', 'id': 'S-0001'}}
        claims = proves.parse('Proves: S-0001 line 2 — tests/test_b.py')
        good, problems = proves.validate(claims, {'parent': 'S-0001'}, items, self.root)
        self.assertEqual((len(good), problems), (1, []))
        good, problems = proves.validate(claims, {'parent': 'F-0001'}, items, self.root)
        self.assertEqual(good, [])
        self.assertTrue(problems[0].startswith("not this Task's"))


class PlanTasksRefusesAnUnknownDecision(RecordCase):
    """A plan whose Task cites a decision the register lacks mints nothing."""

    PLAN = ("# Plan F-0001\n\n### Task 1: the reader\nstories: S-0001\nper {d}, line 3 is deferred\n"
            "writes: asf/reader.py\n\n### Task 2: the table\nwrites: asf/table.py\n"
            "an example only: `D-0042`\n")

    def mint(self, d):
        self.write('E-0001', 'epic')
        self.write('F-0001', 'feature', parent='E-0001')
        self.write('S-0001', 'story', parent='F-0001')
        lines = []
        ev = {'features': {'f-0001': {'plan': 'origin/main:docs/plans/f-0001.md',
                                      'plan_on_main': True, 'spec_on_main': True}}}
        made = plan_tasks.mint_plan_tasks(self.root, None, ev, out=lines.append,
                                          read_ref=lambda ref: self.PLAN.format(d=d))
        return made, lines

    def test_an_unknown_decision_is_refused_and_named(self):
        made, lines = self.mint('D-0099')
        self.assertEqual(made, [])
        self.assertTrue(any('D-0099' in l and 'not in the register' in l for l in lines), lines)
        self.assertNotIn('D-0042', ' '.join(lines))   # a code span is an example, not a citation

    def test_a_registered_decision_mints(self):
        self.decision('D-0007')
        made, _lines = self.mint('D-0007')
        self.assertEqual(len(made), 2)

    def test_the_products_docs_decisions_are_in_the_register(self):
        docs = os.path.join(self.root, 'repo', 'docs', 'decisions')
        os.makedirs(docs)
        for name in ('0001-prior-art.md', 'D-8050.md'):
            with open(os.path.join(docs, name), 'w', encoding='utf-8') as f:
                f.write('# x\n')
        self.assertEqual(decisions.docs_ids(os.path.join(self.root, 'repo')), {'D-0001', 'D-8050'})


class AuditProofs(RecordCase):
    """`asf audit-proofs`: every Resolved or Closed Story with an unproved line; read-only
    unless --apply, which reopens each (and its Feature)."""

    def fixture(self):
        self.write('E-0001', 'epic')
        self.write('F-0001', 'feature', parent='E-0001', state='Closed')
        self.decision('D-0007')
        self.write('T-0001', 'task', parent='F-0001', state='Closed',
                   typed=('stories: [S-0001, S-0002, S-0003, S-0004, S-0005]',))
        self.write('S-0001', 'story', parent='F-0001', state='Closed',
                   body=story_body([(True, 'proved'), (True, 'hand ticked')], (proved(1),)))
        self.write('S-0002', 'story', parent='F-0001', state='Resolved',
                   body=story_body([(True, 'proved')], (proved(1),)))
        self.write('S-0003', 'story', parent='F-0001', state='Resolved',
                   body=story_body([(False, 'later — deferred by D-0007')]))
        self.write('S-0004', 'story', parent='F-0001', state='Resolved',
                   body=story_body([(False, 'later — deferred by D-0099')]))
        self.write('S-0005', 'story', parent='F-0001', state='New',
                   body=story_body([(False, 'not done, not claimed')]))

    def run_audit(self, apply=False):
        before = self.snapshot()
        with mock.patch.object(ingest.evidence, 'load', return_value=dict(
                EMPTY_EV, ci=True, ids=landed('T-0001'))), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = reopen_mod.cmd_audit_proofs(types.SimpleNamespace(apply=apply, product=None),
                                             self.root)
        return rc, out.getvalue(), before

    def test_it_lists_only_the_done_stories_with_an_unproved_line_and_writes_nothing(self):
        self.fixture()
        rc, text, before = self.run_audit()
        self.assertEqual(rc, 0)
        self.assertIn('| S-0001 | Closed | F-0001 | 2: hand ticked (no proof) |', text)
        self.assertIn('| S-0004 | Resolved | F-0001 | 1: later — deferred by D-0099 '
                      '(deferred by D-0099, not in the decision register) |', text)
        for sid in ('S-0002', 'S-0003', 'S-0005'):
            self.assertNotIn(f'| {sid} |', text)
        self.assertIn('2 Resolved/Closed Story(ies)', text)
        self.assertEqual(self.snapshot(), before)

    def test_apply_reopens_them_and_their_feature(self):
        self.fixture()
        rc, text, _before = self.run_audit(apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.state('stories/S-0001.md'), 'Active')
        self.assertEqual(self.state('stories/S-0004.md'), 'Active')
        self.assertEqual(self.state('stories/S-0002.md'), 'Resolved')
        self.assertNotIn(self.state('features/F-0001.md'), ('Resolved', 'Closed'))
        self.assertIn('S-0001: reopened — state Closed → Active', text)

    def test_apply_records_a_pending_replan_on_each_reopened_feature(self):
        """A product's audit (2026-10-05): --apply reopened Stories and created no work — a
        Feature replan starts only from the Feature card's ``reshape:``, and groom only asks for
        a Story no Task lists. Each Feature it reopened gets one pending replan naming the
        reopened Stories and their unproved lines; the next tick queues it."""
        from asf.record import replan
        self.fixture()
        rc, text, _before = self.run_audit(apply=True)
        self.assertEqual(rc, 0)
        meta = self.read('features/F-0001.md')[0]
        how = meta.get('reshape') or ''
        self.assertIn('S-0001 line 2', how)
        self.assertIn('S-0004 line 1', how)
        self.assertNotIn('S-0002', how)
        self.assertTrue(replan.pending(dict(meta, id='F-0001', type='feature')))
        self.assertIn('F-0001: replan pending', text)
        # a second audit with nothing new to reopen leaves the decision as it stands
        rc, _text, before = self.run_audit(apply=True)
        self.assertEqual(self.read('features/F-0001.md')[0].get('reshape'), how)

    def test_apply_keeps_a_pending_reshape_and_adds_the_audit_to_it(self):
        self.fixture()
        frontmatter.write_typed(os.path.join(self.root, 'features/F-0001.md'),
                                {'reshape': 'split the billing Task'})
        self.run_audit(apply=True)
        how = self.read('features/F-0001.md')[0].get('reshape') or ''
        self.assertTrue(how.startswith('split the billing Task'))
        self.assertIn('S-0001 line 2', how)


class InlineProvenBy(unittest.TestCase):
    """A ticked bullet that says ``— proven by <path>`` is proved when the path is a file in the
    product checkout (and a quoted test name, when given, is in it); an absence never is."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='s6_repo_')
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.put('packages/x/src/foo.test.ts', "it('refuses a stranger', () => {})\n")
        self.put('apps/web/e2e/bar.spec.ts', "test('signs in', async () => {})\n")
        self.put('scripts/check.sh', '#!/bin/sh\n')

    def put(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def why(self, text, ticked=True, history=(), register=()):
        out = proves.unproved(story_body([(ticked, text)], history), register, repo_dir=self.repo)
        return out[0][2] if out else None

    def test_an_existing_file_proves_the_line(self):
        self.assertIsNone(self.why('refuses — proven by packages/x/src/foo.test.ts'))
        self.assertIsNone(self.why('runs — proven by scripts/check.sh'))
        self.assertIsNone(self.why('signs in — proven by `apps/web/e2e/bar.spec.ts:210`'))
        self.assertIsNone(self.why('signs in proven by apps/web/e2e/bar.spec.ts:210.'))

    def test_a_missing_path_does_not(self):
        self.assertEqual(self.why('x — proven by packages/x/src/gone.test.ts'),
                         'path missing: packages/x/src/gone.test.ts')

    def test_a_path_outside_the_checkout_does_not(self):
        self.assertTrue(self.why('x — proven by ../../etc/hosts').startswith('path missing'))

    def test_a_quoted_name_must_be_in_the_file(self):
        self.assertIsNone(self.why('x — proven by packages/x/src/foo.test.ts ("refuses a stranger")'))
        self.assertIsNone(self.why('x — proven by packages/x/src/foo.test.ts "refuses a stranger"'))
        self.assertEqual(self.why('x — proven by packages/x/src/foo.test.ts ("admits a stranger")'),
                         'test name not found: "admits a stranger" in packages/x/src/foo.test.ts')

    def test_several_paths_count_when_one_exists(self):
        for sep in (', ', ' and ', '; '):
            self.assertIsNone(self.why(f'x — proven by packages/x/gone.test.ts{sep}'
                                       'packages/x/src/foo.test.ts'), sep)
        self.assertEqual(self.why('x — proven by a/gone.test.ts, b/gone.test.ts'),
                         'path missing: a/gone.test.ts, b/gone.test.ts')

    def test_an_absence_is_never_a_proof(self):
        self.assertEqual(self.why('no secret logged — proven by the absence of `secret` in '
                                  'packages/x/src/foo.test.ts'), 'no proof')

    def test_an_unticked_bullet_is_not_proved_by_its_citation(self):
        self.assertEqual(self.why('x — proven by packages/x/src/foo.test.ts', ticked=False),
                         'no proof')

    def test_no_checkout_proves_nothing_inline(self):
        out = proves.unproved(story_body([(True, 'x — proven by packages/x/src/foo.test.ts')]))
        self.assertTrue(out[0][2].startswith('path missing'))

    def test_proved_line_and_deferral_still_count(self):
        self.assertIsNone(self.why('plain claim', history=(proved(1),)))
        self.assertIsNone(self.why('later — deferred by D-0007', ticked=False, register={'D-0007'}))
        self.assertEqual(self.why('plain claim'), 'no proof')

    def test_the_parser_reads_paths_names_and_stops_at_prose(self):
        self.assertEqual(
            proves.inline_proofs('x — proven by a/b.test.ts ("one"), c/d.spec.ts:12 and '
                                 '.github/workflows/ci.yml (the `drill` step)'),
            [('a/b.test.ts', 'one'), ('c/d.spec.ts', None), ('.github/workflows/ci.yml', None)])
        self.assertEqual(proves.inline_proofs('x — proven by the absence of it'), [])
        self.assertEqual(proves.inline_proofs('x — no test found'), [])


class InlineProofCloses(StoryNeedsEveryLineProved):
    """The ingest and the audit read the same inline proof: a done Story whose open line cites an
    existing test closes; one citing a missing file stays Active and says why."""

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.root, 'checkout')
        os.makedirs(os.path.join(self.repo, 'tests'))
        with open(os.path.join(self.repo, 'tests', 'team.test.ts'), 'w', encoding='utf-8') as f:
            f.write("it('team service_principal', () => {})\n")
        patcher = mock.patch.object(decisions, 'repo_dir', return_value=self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_inline_proof_closes_the_story(self):
        s = self.s1158(history=(proved(1), proved(2)))
        self.write('S-1158', 'story', parent='F-0106', body=story_body(
            [(True, 'org members'), (True, 'team members'),
             (True, 'team service_principal — proven by tests/team.test.ts')],
            (proved(1), proved(2))))
        self.ingest(self.ev())
        self.assertEqual(self.state(s), 'Closed')

    def test_a_missing_inline_path_keeps_it_active_and_says_so(self):
        s = self.s1158()
        self.write('S-1158', 'story', parent='F-0106', body=story_body(
            [(True, 'org members'), (True, 'team members'),
             (True, 'svc — proven by tests/gone.test.ts')], (proved(1), proved(2))))
        self.ingest(self.ev())
        m = self.read(s)[0]
        self.assertEqual(m['state'], 'Active')
        self.assertIn('unproved line 3 — svc — proven by tests/gone.test.ts '
                      '(path missing: tests/gone.test.ts)', m['evidence'])

    def test_the_audit_reads_the_same_proof(self):
        self.write('E-0001', 'epic')
        self.write('F-0001', 'feature', parent='E-0001', state='Closed')
        self.write('S-0001', 'story', parent='F-0001', state='Closed', body=story_body(
            [(True, 'a — proven by tests/team.test.ts ("team service_principal")')]))
        self.write('S-0002', 'story', parent='F-0001', state='Closed', body=story_body(
            [(True, 'b — proven by tests/team.test.ts ("someone else")')]))
        found = reopen_mod.audit(reopen_mod._canonical(self.root), set(), self.repo)
        self.assertEqual([(sid, [w for _n, _t, w in lines]) for sid, _s, _f, lines in found],
                         [('S-0002', ['test name not found: "someone else" in tests/team.test.ts'])])


if __name__ == '__main__':
    unittest.main()
