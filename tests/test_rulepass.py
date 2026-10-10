"""tests.test_rulepass — the disputes code rules before any adjudicate session is spawned.

Pure, over built ``Dispute``s and a fake product with a ``docs/decisions/`` folder, the fixture
shape ``tests/test_precedent.py`` already uses (F-0300, S-78054). ``ScreenRuledTests`` and
``WaiversTests`` are a later Task's own: :func:`asf.tick.step_wave.screen`'s check and
:func:`asf.tick.step_wave.waivers`, both over ``rulepass.dispute`` mocked out — the dispute
table itself is proven above, pure.
"""
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf import env
from asf.evidence import rulepass as rp
from asf.evidence import rulings
from asf.feeder import rows as feeder_rows
from asf.tick import step_wave
from asf.workers import lifecycle
from asf.workers import pool as pool_mod


class _TempProduct(unittest.TestCase):
    """A fresh ``backlog_dir`` and ``docs/decisions/`` under a fresh ``tempfile.mkdtemp``, the
    fixture shape :mod:`tests.test_precedent` already uses."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rulepass_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._n = 0

    def product(self, flag='on', decisions_files=None):
        self._n += 1
        backlog = os.path.join(self.tmp, f'backlog{self._n}')
        repo = os.path.join(self.tmp, f'repo{self._n}')
        os.makedirs(backlog, exist_ok=True)
        os.makedirs(os.path.join(repo, 'docs', 'decisions'), exist_ok=True)
        for name, content in (decisions_files or {}).items():
            with open(os.path.join(repo, 'docs', 'decisions', name), 'w', encoding='utf-8') as f:
                f.write(content)
        flags = {'rule_pass': flag} if flag is not None else {}
        data = {'repo_dir': repo, 'backlog_dir': backlog, 'conventions': {'flags': flags}}
        return env.Product(f'rulepass-test-{self._n}', data)

    def write_card(self, product, item, history_lines=(), folder='tasks'):
        d = os.path.join(product.backlog_dir, folder)
        os.makedirs(d, exist_ok=True)
        body = '## History\n' + '\n'.join(history_lines) + '\n' if history_lines else ''
        with open(os.path.join(d, f'{item}.md'), 'w', encoding='utf-8') as f:
            f.write(f'---\nid: {item}\ntype: task\ntitle: "x"\nstate: New\n---\n\n{body}')


def _dispute(finding, entries, where='correction', item='T-0001', branch='worker/T-0001'):
    return rp.Dispute(item=item, branch=branch, finding=tuple(finding), entries=tuple(entries),
                      where=where)


class ClassifyTests(unittest.TestCase):
    def test_each_cosmetic_class(self):
        self.assertEqual(rp.classify('squash these three commits into one'), 'commit-shape')
        self.assertEqual(
            rp.classify("the docstring says the reader where it should say the reader's"),
            'wording')
        self.assertEqual(rp.classify('two blank lines between the defs'), 'formatting')

    def test_never_waive_beats_every_cosmetic_word(self):  # C6, P14
        texts = ['reword the licence header',
                 'fix the typo in the secret name',
                 'the wording of the customer-facing error',
                 'rename it — it is also wrong for `None`',
                 'rephrase it, and there is no test for the branch']
        for t in texts:
            self.assertEqual(rp.classify(t), '', t)
        self.assertEqual(rp.classify(None), '')  # also wrong for None


class RuleTests(_TempProduct):
    def test_a_two_item_all_cosmetic_list_is_ruled_cosmetic(self):  # C4, P11
        entries = (('C1', 'squash these commits into one over asf/flake.py'),
                   ('C2', 'two blank lines between the defs over asf/other.py'))
        d = _dispute(finding=('asf/flake.py', 'asf/other.py'), entries=entries)
        product = self.product()
        ruling = rp.rule(product, d, items={})
        self.assertEqual(ruling.rules, ('cosmetic', 'cosmetic'))
        sentences = [s for s in ruling.text.split('. ') if s]
        self.assertEqual(len(sentences), 2)
        self.assertIn('asf/flake.py', sentences[0])
        self.assertIn('asf/other.py', sentences[1])
        self.assertTrue(ruling.text.rstrip().endswith('.'))
        self.assertEqual(ruling.finding,
                         tuple(lifecycle.finding_of(lifecycle.REVIEW, '',
                                                    keys=['asf/flake.py', 'asf/other.py'])))

    def test_one_correctness_item_blocks_the_whole_dispute(self):  # C5
        entries = (('C1', 'squash these commits into one over asf/flake.py'),
                   ('C2', 'this raises an exception under load'))
        d = _dispute(finding=('asf/flake.py', 'asf/other.py'), entries=entries)
        product = self.product()
        self.assertIsNone(rp.rule(product, d, items={}))

    def test_a_standing_ruling_the_item_already_carries_is_precedent(self):
        product = self.product()
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'this point was already adjudicated; see the prior run.'),))
        with mock.patch.object(rp, 'standing',
                               return_value=[{'at': '2026-10-01 10:00',
                                              'job': 'adjudicate-t-0001', 'text': 'prior ruling.'}]):
            ruling = rp.rule(product, d, items={})
        self.assertEqual(ruling.rules, ('precedent',))
        self.assertEqual(len(ruling.cites), 1)
        self.assertIn('adjudicate-t-0001', ruling.cites[0])
        self.assertIn('2026-10-01 10:00', ruling.cites[0])

    def test_a_decision_the_register_confirms_accepted_is_precedent(self):
        product = self.product(decisions_files={
            'd-0117.md': '---\nid: D-0117\ntitle: "Queue retries are idempotent"\n'
                         'status: accepted\n---\nBody.\n'})
        d = _dispute(finding=('asf/queue.py',),
                     entries=(('C2', 'this repeats the point D-0117 already settled'),))
        ruling = rp.rule(product, d, items={})
        self.assertEqual(ruling.rules, ('precedent',))
        self.assertEqual(ruling.cites,
                         ('D-0117 — Queue retries are idempotent · docs/decisions/d-0117.md',))

    def test_a_superseded_or_absent_decision_is_not_precedent(self):
        superseded = self.product(decisions_files={
            'd-0117.md': '---\nid: D-0117\ntitle: "Queue retries are idempotent"\n'
                         'status: superseded\n---\nBody.\n'})
        d = _dispute(finding=('asf/queue.py',),
                     entries=(('C2', 'this repeats the point D-0117 already settled'),))
        self.assertIsNone(rp.rule(superseded, d, items={}))  # STALE_STATUS

        absent = self.product()
        self.assertIsNone(rp.rule(absent, d, items={}))

    def test_no_decision_and_no_cosmetic_class_is_not_ruled(self):  # Out: no lexical guess
        product = self.product()
        d = _dispute(finding=('asf/retry.py',),
                     entries=(('C3', 'the retry logic needs a second look'),))
        self.assertIsNone(rp.rule(product, d, items={}))


class HistoryLineTests(unittest.TestCase):
    def test_the_exact_shape_and_readback(self):  # P4
        ruling = rp.Ruling(rules=('cosmetic',),
                           text='C1 is overruled as cosmetic (commit-shape): a sentence.',
                           cites=(), finding=('asf/flake.py',), why='rule pass: cosmetic',
                           where='correction')
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        self.assertEqual(
            line,
            '- 2026-10-08 10:30 adjudicate (rule-pass): C1 is overruled as cosmetic '
            '(commit-shape): a sentence. [rule: cosmetic; finding: asf/flake.py] '
            '[precedent: none]')
        parsed = rulings.parse(f'## History\n{line}\n')
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['job'], 'rule-pass')
        self.assertIn('[precedent: none]', parsed[0]['text'])


class ReadBackTests(unittest.TestCase):
    def test_the_filed_line_is_read_back_by_the_landed_readers(self):  # P4, P5
        entries = (('C1', 'squash these commits into one over asf/flake.py'),
                   ('C2', 'two blank lines between the defs over asf/other.py'))
        d = _dispute(finding=('asf/flake.py', 'asf/other.py'), entries=entries)
        ruling = rp.rule(env.Product('x', {}), d, items={})
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        std = rulings.parse(f'## History\n{line}\n')

        body = ('## C\n'
                '1. squash these commits into one over asf/flake.py\n'
                '2. two blank lines between the defs over asf/other.py\n')
        self.assertEqual(rulings.covers(std, 'C1', 'squash these over asf/flake.py'), 'rule-pass')
        self.assertEqual(rulings.covers(std, 'C2', 'two blanks over asf/other.py'), 'rule-pass')
        self.assertEqual(rulings.reraised_only(body, std), ['rule-pass'])

        extra = body + '3. a new problem over asf/newfile.py\n'
        self.assertIsNone(rulings.reraised_only(extra, std))


class WaivedTests(_TempProduct):
    def test_finds_its_own_line_by_finding_key_and_not_a_different_key(self):  # C7
        product = self.product()
        entries = (('C1', 'squash these commits into one over asf/flake.py'),)
        d = _dispute(finding=('asf/flake.py',), entries=entries)
        ruling = rp.rule(product, d, items={})
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        self.write_card(product, 'T-0001', history_lines=[line])

        self.assertEqual(rp.waived(product, 'T-0001', ('asf/flake.py',)), '2026-10-08 10:30')
        self.assertEqual(rp.waived(product, 'T-0001', ('asf/other.py',)), '')


class EnabledTests(unittest.TestCase):
    def test_default_off_and_the_on_words(self):  # P10, C9
        self.assertFalse(rp.enabled(None))
        for value, want in (('off', False), ('', False), ('on', True), ('true', True),
                            ('yes', True), ('1', True), (True, True)):
            product = env.Product('x', {'conventions': {'flags': {'rule_pass': value}}})
            self.assertEqual(rp.enabled(product), want, value)
        self.assertFalse(rp.enabled(env.Product('x', {})))


class ApplyTests(unittest.TestCase):
    def test_flag_off_reads_and_writes_nothing(self):
        product = env.Product('x', {'conventions': {'flags': {'rule_pass': 'off'}}})
        row = object()
        with mock.patch.object(rp, 'dispute', side_effect=AssertionError('must not be called')):
            out = []
            self.assertIsNone(rp.apply(product, row, {}, out.append))
        self.assertEqual(out, [])

    def test_an_unreadable_review_returns_none_and_one_line_never_an_exception(self):
        product = env.Product('x', {'conventions': {'flags': {'rule_pass': 'on'}}})
        row = type('Row', (), {'item_id': 'T-0001', 'branch': 'worker/T-0001'})()
        out = []
        with mock.patch.object(rp, 'dispute', side_effect=OSError('review unreadable')):
            result = rp.apply(product, row, {}, out.append)
        self.assertIsNone(result)
        self.assertEqual(len(out), 1)
        self.assertIn('T-0001', out[0])


def _adjudicate_row(item_id='T-0001', branch='worker/T-0001', kind=feeder_rows.STALEMATE):
    return feeder_rows.Row(tier=2, kind=kind, item_id=item_id, feature_id='F-0001',
                           action=feeder_rows.LAUNCH, brief_kind='adjudicate', branch=branch,
                           reason='')


def _plan_code_row(item_id='T-0002', branch='worker/T-0002'):
    return feeder_rows.Row(tier=2, kind=feeder_rows.PLAN_CODE, item_id=item_id,
                           feature_id='F-0001', action=feeder_rows.LAUNCH, brief_kind='task',
                           branch=branch, reason='')


def _build(row, _bypass):
    return (pool_mod.Row(step_wave.job_name(row.brief_kind, row.item_id), row.item_id,
                        branch=row.branch),
            types.SimpleNamespace(text='brief', kind=row.brief_kind))


def _not_capped(_product, _row, wrow):
    """:func:`step_wave.relaunch_assessment`'s shape, patched in: never capped, never parked."""
    wrow.cause = ''
    return (None, None, None)


def _quiet_ctx():
    return types.SimpleNamespace(event=lambda *a, **k: None)


class ScreenRuledTests(_TempProduct):
    """S-78055: the check :func:`asf.tick.step_wave.screen` runs on an adjudicate row, between
    the operator's pause and the host hold — ``rulepass.dispute`` mocked out, ``rule`` and the
    card write real, so a cosmetic ruling is proven end to end without reading a real review."""

    def _events_ctx(self):
        events = []
        return types.SimpleNamespace(event=lambda kind, **f: events.append((kind, f))), events

    def _seed(self, product, item='T-0001'):
        self.write_card(product, item, history_lines=['- 2026-10-01 09:00 create: opened'])

    def _rulings(self, product, item='T-0001'):
        path = os.path.join(product.backlog_dir, 'tasks', f'{item}.md')
        with open(path, encoding='utf-8') as f:
            return rulings.parse(f.read())

    def test_all_cosmetic_dispute_rules_and_writes_once(self):
        product = self.product(flag='on')
        self._seed(product)
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'squash these commits into one over asf/flake.py'),))
        row = _adjudicate_row()
        ctx, events = self._events_ctx()
        lines = []
        with mock.patch.object(rp, 'dispute', return_value=d):
            screened = step_wave.screen(product, [row], {}, [], {}, 2, act=True,
                                        out=lines.append, build=_build, ctx=ctx,
                                        record_root=lambda: product.backlog_dir)
        self.assertEqual([s.kind for s in screened], [step_wave.RULED])
        self.assertFalse(screened[0].starts)
        after = self._rulings(product)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]['job'], 'rule-pass')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 'rule_pass')
        self.assertEqual(events[0][1]['item'], 'T-0001')
        self.assertEqual(len([l for l in lines if l.startswith('ruled')]), 1)

    def test_ruled_row_frees_its_seat_to_the_next_row(self):
        product = self.product(flag='on')
        self._seed(product)
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'squash these commits into one over asf/flake.py'),))
        ruled_row, code_row = _adjudicate_row(), _plan_code_row()
        ctx, _events = self._events_ctx()
        with mock.patch.object(rp, 'dispute', return_value=d), \
             mock.patch.object(step_wave, 'relaunch_assessment', side_effect=_not_capped):
            screened = step_wave.screen(product, [ruled_row, code_row], {}, [], {}, 1, act=True,
                                        out=lambda _l: None, build=_build, ctx=ctx,
                                        record_root=lambda: product.backlog_dir)
        kinds = {s.row.item_id: s for s in screened}
        self.assertEqual(kinds['T-0001'].kind, step_wave.RULED)
        self.assertFalse(kinds['T-0001'].starts)
        self.assertEqual(kinds['T-0002'].kind, step_wave.STARTS)
        self.assertTrue(kinds['T-0002'].starts)

    def test_preview_rules_quietly_and_writes_nothing(self):
        product = self.product(flag='on')
        self._seed(product)
        before = self._rulings(product)
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'squash these commits into one over asf/flake.py'),))
        row = _adjudicate_row()
        lines = []
        with mock.patch.object(rp, 'dispute', return_value=d):
            screened = step_wave.screen(product, [row], {}, [], {}, 2, act=False,
                                        out=lines.append)
        self.assertEqual([s.kind for s in screened], [step_wave.RULED])
        self.assertFalse(screened[0].starts)
        self.assertEqual(self._rulings(product), before)
        self.assertEqual(lines, [])

    def test_flag_off_starts_as_today(self):
        product = self.product(flag='off')
        row = _adjudicate_row()
        with mock.patch.object(rp, 'dispute', side_effect=AssertionError('must not be called')), \
             mock.patch.object(step_wave, 'relaunch_assessment', side_effect=_not_capped):
            screened = step_wave.screen(product, [row], {}, [], {}, 2, act=True,
                                        out=lambda _l: None, build=_build, ctx=_quiet_ctx())
        self.assertEqual(screened[0].kind, step_wave.STARTS)
        self.assertTrue(screened[0].starts)

    def test_one_correctness_item_blocks_the_ruling(self):
        product = self.product(flag='on')
        self._seed(product)
        before = self._rulings(product)
        d = _dispute(finding=('asf/flake.py', 'asf/other.py'),
                     entries=(('C1', 'squash these commits into one over asf/flake.py'),
                              ('C2', 'this raises an exception under load')))
        row = _adjudicate_row()
        with mock.patch.object(rp, 'dispute', return_value=d), \
             mock.patch.object(step_wave, 'relaunch_assessment', side_effect=_not_capped):
            screened = step_wave.screen(product, [row], {}, [], {}, 2, act=True,
                                        out=lambda _l: None, build=_build, ctx=_quiet_ctx(),
                                        record_root=lambda: product.backlog_dir)
        self.assertEqual(screened[0].kind, step_wave.STARTS)
        self.assertTrue(screened[0].starts)
        self.assertEqual(self._rulings(product), before)

    def test_bug_rows_and_capped_rows_are_never_disputed(self):
        product = self.product(flag='on')
        bug_row = _adjudicate_row(item_id='B-0001', branch='fix/B-0001')
        capped_row = _adjudicate_row(item_id='T-0003', branch='worker/T-0003')
        with mock.patch.object(rp, 'dispute', return_value=None) as m, \
             mock.patch.object(step_wave, 'relaunch_assessment', side_effect=_not_capped):
            screened = step_wave.screen(product, [bug_row, capped_row], {}, [], {}, 2, act=True,
                                        out=lambda _l: None, build=_build, ctx=_quiet_ctx())
        self.assertEqual(m.call_count, 2)
        self.assertEqual([s.kind for s in screened], [step_wave.STARTS, step_wave.STARTS])
        self.assertTrue(all(s.starts for s in screened))

    def test_pause_holds_before_the_rule_check_runs(self):
        product = self.product(flag='on')
        row = _adjudicate_row()
        paused = {'reason': 'maintenance', 'by': 'operator', 'at': '2026-10-08 10:00'}
        with mock.patch.object(rp, 'dispute', side_effect=AssertionError('must not be called')):
            screened = step_wave.screen(product, [row], {}, [], {}, 2, act=True,
                                        out=lambda _l: None, paused=paused, ctx=_quiet_ctx())
        self.assertEqual(screened[0].kind, step_wave.PAUSED)
        self.assertFalse(screened[0].starts)

    def test_host_hold_does_not_stop_the_rule_check(self):
        product = self.product(flag='on')
        self._seed(product)
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'squash these commits into one over asf/flake.py'),))
        row = _adjudicate_row()
        ctx, events = self._events_ctx()
        with mock.patch.object(rp, 'dispute', return_value=d):
            screened = step_wave.screen(product, [row], {}, [], {}, 2,
                                        host=(True, 'host pressure load 9/1'), act=True,
                                        out=lambda _l: None, build=_build, ctx=ctx,
                                        record_root=lambda: product.backlog_dir)
        self.assertEqual(screened[0].kind, step_wave.RULED)
        self.assertFalse(screened[0].starts)
        self.assertEqual(len(events), 1)

    def test_an_already_waived_finding_is_quiet_and_starts(self):
        product = self.product(flag='on')
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'squash these commits into one over asf/flake.py'),))
        ruling = rp.rule(product, d, items={})
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        self.write_card(product, 'T-0001', history_lines=[line])
        before = self._rulings(product)
        row = _adjudicate_row()
        ctx, events = self._events_ctx()
        lines = []
        with mock.patch.object(rp, 'dispute', return_value=d), \
             mock.patch.object(step_wave, 'relaunch_assessment', side_effect=_not_capped):
            screened = step_wave.screen(product, [row], {}, [], {}, 2, act=True,
                                        out=lines.append, build=_build, ctx=ctx,
                                        record_root=lambda: product.backlog_dir)
        self.assertEqual(screened[0].kind, step_wave.STARTS)
        self.assertTrue(screened[0].starts)
        self.assertEqual(self._rulings(product), before)
        self.assertEqual(events, [])
        self.assertEqual([l for l in lines if l.startswith('ruled')], [])

    def test_apply_raising_starts_the_row_and_logs_one_line(self):
        product = self.product(flag='on')
        held_row = feeder_rows.Row(tier=1, kind=feeder_rows.FIX_CORRECT, item_id='T-0009',
                                   feature_id='F-0001', action=feeder_rows.LAUNCH,
                                   brief_kind='correct', branch='worker/T-0009', reason='')
        wait_row = feeder_rows.Row(tier=1, kind=feeder_rows.PLAN_CODE, item_id='T-0010',
                                   feature_id='F-0001', action='WAITS ON T-0009',
                                   brief_kind='task', branch='', reason='')
        adjudicate_row = _adjudicate_row()
        held = {'T-0009': ('touch_amendable_set', 'human-now')}
        lines = []
        with mock.patch.object(rp, 'dispute', side_effect=RuntimeError('boom')), \
             mock.patch.object(step_wave, 'relaunch_assessment', side_effect=_not_capped):
            screened = step_wave.screen(product, [held_row, wait_row, adjudicate_row], {},
                                        [], held, 2, act=True, out=lines.append, build=_build,
                                        ctx=_quiet_ctx())
        kinds = {s.row.item_id: s for s in screened}
        self.assertEqual(kinds['T-0009'].kind, step_wave.HELD)
        self.assertEqual(kinds['T-0010'].kind, step_wave.WAITS)
        self.assertEqual(kinds['T-0001'].kind, step_wave.STARTS)
        self.assertTrue(kinds['T-0001'].starts)
        error_lines = [l for l in lines if 'could not rule' in l]
        self.assertEqual(len(error_lines), 1)
        self.assertIn('T-0001', error_lines[0])

    def test_wave_clock_never_files_a_ruling_so_the_row_starts(self):  # PD5
        product = self.product(flag='on')
        self._seed(product)
        before = self._rulings(product)
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'squash these commits into one over asf/flake.py'),))
        row = _adjudicate_row()
        lines = []
        with mock.patch.object(rp, 'dispute', return_value=d), \
             mock.patch.object(step_wave, 'relaunch_assessment', side_effect=_not_capped):
            screened = step_wave.screen(product, [row], {}, [], {}, 2, act=True,
                                        out=lines.append, build=_build, ctx=_quiet_ctx())
        self.assertEqual(screened[0].kind, step_wave.STARTS)
        self.assertTrue(screened[0].starts)
        self.assertEqual(self._rulings(product), before)
        self.assertEqual(len([l for l in lines if l.startswith('ruled')]), 1)


class WaiversTests(_TempProduct):
    """S-78056: :func:`asf.tick.step_wave.waivers` — the membership both feeder branches read,
    built over ``rulepass.dispute`` mocked out and ``rulepass.waived`` real, over a real card."""

    def test_waivers_matches_the_correction_finding_key(self):
        product = self.product(flag='on')
        d = _dispute(finding=('asf/flake.py',),
                     entries=(('C1', 'squash these commits into one over asf/flake.py'),))
        ruling = rp.rule(product, d, items={})
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        self.write_card(product, 'T-0001', history_lines=[line])
        index = {'T-0001': {'id': 'T-0001', 'type': 'task'}}
        with mock.patch.object(rp, 'dispute', return_value=d):
            out = step_wave.waivers(product, 'unused-root', index)
        self.assertEqual(out, {'T-0001': '2026-10-08 10:30'})

    def test_waivers_excludes_a_mismatched_finding_key(self):
        product = self.product(flag='on')
        card_dispute = _dispute(
            finding=('asf/other.py',),
            entries=(('C1', 'squash these commits into one over asf/other.py'),))
        ruling = rp.rule(product, card_dispute, items={})
        line = rp.history_line(ruling, now='2026-10-08T10:30:00')
        self.write_card(product, 'T-0001', history_lines=[line])
        current_dispute = _dispute(
            finding=('asf/flake.py',),
            entries=(('C1', 'squash these commits into one over asf/flake.py'),))
        index = {'T-0001': {'id': 'T-0001', 'type': 'task'}}
        with mock.patch.object(rp, 'dispute', return_value=current_dispute):
            out = step_wave.waivers(product, 'unused-root', index)
        self.assertEqual(out, {})

    def test_waivers_is_empty_with_the_flag_off_or_an_unreadable_review(self):
        index = {'T-0001': {'id': 'T-0001', 'type': 'task'}}
        product_off = self.product(flag='off')
        with mock.patch.object(rp, 'dispute', side_effect=AssertionError('must not be called')):
            self.assertEqual(step_wave.waivers(product_off, 'root', index), {})
        product_on = self.product(flag='on')
        with mock.patch.object(rp, 'dispute', side_effect=OSError('review unreadable')):
            self.assertEqual(step_wave.waivers(product_on, 'root', index), {})


if __name__ == '__main__':
    unittest.main()
