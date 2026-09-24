"""asf.workers.report — the typed report: its schema, its reader, its renderer, its contract
(F-0025), and the one failure it declares at the source, ``pushed: no`` (F-0087, B-0024, B-0052)."""
import json
import unittest

from asf.briefs.build import KINDS
from asf.workers import report
from asf.workers import runtime as runtime_mod



def block(**over):
    """A fenced spec report, with ``over`` laid on a good one."""
    obj = json.loads(report.render('spec').split('\n', 1)[1].rsplit('\n```', 1)[0])
    obj.update(over)
    return '```json asf-report\n' + json.dumps(obj) + '\n```\n'


def rec(text):
    return {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text}


class SchemaTest(unittest.TestCase):
    def test_every_brief_kind_has_a_schema(self):
        self.assertEqual(sorted(report.KIND_FIELDS), sorted(KINDS))
        for kind in KINDS:
            self.assertTrue(report.schema(kind), kind)

    def test_every_field_declares_types_a_default_and_a_line(self):
        for kind in KINDS:
            for name, spec in report.schema(kind).items():
                with self.subTest(kind=kind, field=name):
                    types, _default, line = spec
                    self.assertTrue(line)
                    self.assertTrue(types)
                    named = [t in report._CHECK for t in types]
                    self.assertTrue(all(named) or not any(named), types)

    def test_a_kind_field_never_shadows_a_common_one(self):
        for kind in KINDS:
            self.assertEqual(set(report.KIND_FIELDS[kind]) & set(report.COMMON), set(), kind)

    def test_an_unknown_kind_refuses(self):
        with self.assertRaises(report.ReportError):
            report.schema('preflight')


class ParseTest(unittest.TestCase):
    def test_the_block_is_read_out_of_the_final_message(self):
        text = 'I wrote the spec.\n\nREPORT\nitem: X\n\n' + block(item='F-0001')
        self.assertEqual(report.typed(text, 'spec')['item'], 'F-0001')

    def test_the_last_block_wins(self):
        text = block(item='F-0001') + '\nthen I corrected it\n' + block(item='F-0002')
        self.assertEqual(report.typed(text, 'spec')['item'], 'F-0002')

    def test_a_longer_fence_closes_on_a_run_at_least_as_long(self):
        body = json.dumps(json.loads(block()[len('```json asf-report\n'):-5]))
        text = '````json asf-report\n' + body + '\n```\nstill inside\n````\n'
        self.assertEqual(report.fence(text), body + '\n```\nstill inside')

    def test_a_block_that_never_closes_is_no_block(self):
        self.assertIn('asf-report', report.check('spec', block()[:-5]))

    def test_no_block_is_a_named_failure(self):
        self.assertIn('asf-report', report.check('spec', 'I did the thing.'))
        self.assertIn('asf-report', report.check('spec', None))

    def test_broken_json_is_a_named_failure(self):
        msg = report.check('spec', '```json asf-report\n{"item":\n```\n')
        self.assertIn('not JSON', msg)
        self.assertIn('Expecting value', msg)

    def test_an_array_is_not_an_object(self):
        self.assertIn('not an object', report.check('spec', '```json asf-report\n[]\n```\n'))

    def test_an_unknown_key_is_refused(self):
        self.assertEqual(report.check('spec', block(plan='x')), 'unknown key(s) plan')

    def test_a_kindless_read_allows_unknown_keys_and_checks_only_the_common_ones(self):
        self.assertEqual(report.typed(block(plan='x'), None)['plan'], 'x')
        with self.assertRaises(report.ReportError):
            report.typed(block(status='finished'), None)

    def test_a_missing_required_key_is_refused(self):
        obj = json.loads(block()[len('```json asf-report\n'):-5])
        del obj['pushed']
        text = '```json asf-report\n' + json.dumps(obj) + '\n```\n'
        self.assertIn("'pushed'", report.check('spec', text))

    def test_a_bad_enum_is_refused(self):
        msg = report.check('spec', block(status='finished'))
        self.assertIn('done, partial, blocked', msg)
        self.assertIn("'finished'", msg)

    def test_a_bad_type_is_refused(self):
        self.assertIn("'commits' must be list", report.check('spec', block(commits='none')))
        self.assertIn("'round' must be int", report.check('review', report.render('review', round=True)))

    def test_the_three_cross_field_rules(self):
        cases = (
            (dict(pushed='yes', sha=None), "'sha'"),
            (dict(pushed='rebased', sha=''), "'sha'"),
            (dict(pushed='no', why=None), "'why'"),
            (dict(status='blocked', needs_operator=[], left_out=[]), 'needs_operator'),
        )
        for over, named in cases:
            with self.subTest(over=over):
                self.assertIn(named, report.check('spec', block(**over)))
        good = (dict(pushed='yes', sha='abc1234'), dict(pushed='rebased', sha='abc1234'),
                dict(pushed='no', sha=None, why='the suite is still running'),
                dict(status='blocked', needs_operator=['X — answer']),
                dict(status='blocked', left_out=['the whole thing — no access']))
        for over in good:
            with self.subTest(over=over):
                self.assertIsNone(report.check('spec', block(**over)))

    def test_an_adjudicate_ruling_may_not_be_empty(self):
        self.assertIn("'ruling'", report.check('adjudicate', report.render('adjudicate', ruling='  ')))

    def test_a_good_report_of_every_kind_parses(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                report.typed(report.render(kind), kind)
                self.assertIsNone(report.check(kind, report.render(kind)))


class RenderTest(unittest.TestCase):
    def test_render_round_trips_through_typed(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(report.typed(report.render(kind, item='F-0025'), kind)['item'],
                                 'F-0025')

    def test_render_refuses_a_field_the_kind_does_not_have(self):
        with self.assertRaises(report.ReportError):
            report.render('close', stories=[])

    def test_render_fills_every_required_field(self):
        for kind in KINDS:
            obj = report.typed(report.render(kind), kind)
            required = {n for n, (_t, d, _l) in report.schema(kind).items() if d is report.REQ}
            self.assertLessEqual(required, set(obj), kind)

    def test_render_of_a_non_pushing_or_blocked_report_is_still_valid(self):
        for over in (dict(pushed='no'), dict(status='blocked'), dict(status='partial', pushed='no')):
            self.assertIsNone(report.check('coder', report.render('coder', **over)), over)


class ContractTest(unittest.TestCase):
    def test_the_contract_names_every_field_of_the_kind(self):
        for kind in KINDS:
            text = report.contract(kind)
            for name, (_types, _default, line) in report.schema(kind).items():
                with self.subTest(kind=kind, field=name):
                    self.assertIn(f'"{name}"', text)
                    self.assertIn(line, text)

    def test_the_contract_is_the_shape_it_asks_for(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                shape = json.loads(report.fence(report.contract(kind)))
                given = json.loads(report.fence(report.render(kind)))
                filled = {k: given[k] if isinstance(v, str) else v for k, v in shape.items()}
                text = '```json asf-report\n' + json.dumps(filled) + '\n```\n'
                self.assertEqual(list(shape), list(report.schema(kind)))
                self.assertIsNone(report.check(kind, text))

    def test_the_common_fields_come_first_in_every_kind(self):
        heads = set()
        for kind in KINDS:
            lines = report.contract(kind).split('```json asf-report\n', 1)[1].splitlines()
            heads.add('\n'.join(lines[:1 + len(report.COMMON)]))
        self.assertEqual(len(heads), 1)


class ReadersTest(unittest.TestCase):
    """The card's second acceptance, stated as a contradiction: the JSON wins over the prose."""

    def test_the_json_wins_over_the_prose(self):
        text = ('REPORT\nitem: B-0001\nstatus: partial\npushed: no — waiting for the suite\n\n'
                + report.render('fix-bug', pushed='yes', sha='abc1234'))
        self.assertIsNone(report.failure(text))
        self.assertTrue(runtime_mod.result_ok(rec(text)))

    def test_and_the_other_way_round(self):
        text = ('REPORT\nitem: B-0001\nstatus: done\npushed: yes abc1234\n\n'
                + report.render('fix-bug', pushed='no', why='the suite is still running'))
        self.assertEqual(report.failure(text), report.UNPUSHED)
        self.assertEqual(runtime_mod.failure_reason(rec(text)), report.UNPUSHED)
        self.assertFalse(runtime_mod.result_ok(rec(text)))

    def test_the_ruling_is_read_off_the_json(self):
        text = ('REPORT\nitem: B-0001\nruling: the prose says this\n\n'
                + report.render('adjudicate', ruling='  the typed one  '))
        self.assertEqual(report.ruling(text), 'the typed one')
        self.assertEqual(report.ruling('REPORT\nruling: prose only\n'), '')
        self.assertEqual(report.ruling('no report at all'), '')

    def test_the_prose_reader_is_gone(self):
        for name in ('parse', 'unpushed', 'NO_RE'):
            self.assertFalse(hasattr(report, name), name)

    def test_a_malformed_report_is_not_a_failure_reason(self):
        for text in ('I did the thing.', '```json asf-report\n{"item":\n```\n'):
            self.assertIsNone(report.failure(text))
            self.assertTrue(runtime_mod.result_ok(rec(text)))

    def test_summary_is_one_line(self):
        done = report.summary(report.typed(report.render('coder'), 'coder'))
        unpushed = report.summary(report.typed(
            report.render('coder', status='partial', pushed='no', why='the suite\nis still running'),
            'coder'))
        blocked = report.summary(report.typed(
            report.render('coder', status='blocked', pushed='no', needs_operator=['X — answer']),
            'coder'))
        self.assertEqual(done, 'done')
        self.assertEqual(unpushed, 'partial — the suite is still running')
        self.assertEqual(blocked, 'blocked — needs operator: X — answer')
        for line in (done, unpushed, blocked):
            self.assertNotIn('\n', line)


class RulingFieldsTest(unittest.TestCase):
    """F-0090 D10 (PD5): the ruling's mechanism is three fields of the adjudicate report."""

    NONE = {'blocked_on': None, 'writes': None, 'superseded_by': None}

    def test_the_three_fields_are_read_off_the_object(self):
        text = report.render('adjudicate', ruling='it waits', blocked_on='T-0025',
                             writes=['asf/a.py', 'tests/test_a.py', 'docs/**'],
                             superseded_by='T-0030')
        self.assertEqual(report.ruling_fields(text),
                         {'blocked_on': 'T-0025', 'writes': ['asf/a.py', 'tests/test_a.py', 'docs/**'],
                          'superseded_by': 'T-0030'})

    def test_an_absent_field_is_none(self):
        self.assertEqual(report.ruling_fields(report.render('adjudicate', ruling='nothing')), self.NONE)
        self.assertEqual(report.ruling_fields('no report at all'), self.NONE)
        self.assertEqual(report.ruling_fields(report.render('coder')), self.NONE)

    def test_none_and_empty_are_no_claim(self):
        for value in ('none', 'None', 'n/a', '-', '—', ''):
            with self.subTest(value=value):
                text = report.render('adjudicate', ruling='r', blocked_on=value,
                                     writes=[value], superseded_by=value)
                self.assertEqual(report.ruling_fields(text), self.NONE)
        self.assertEqual(report.ruling_fields(report.render('adjudicate', ruling='r', writes=[])),
                         self.NONE)

    def test_a_field_the_prose_names_is_not_read(self):
        text = 'REPORT\nblocked_on: T-0001\n\n' + report.render('adjudicate', ruling='r')
        self.assertIsNone(report.ruling_fields(text)['blocked_on'])

    def test_a_report_quoting_a_cli_error_is_not_that_failure(self):
        text = ('the tool said: permission denied in this session\n\n'
                + report.render('coder', left_out=['tools/x.sh - permission denied in this session']))
        self.assertIsNone(runtime_mod.failure_reason(rec(text)))
        self.assertTrue(runtime_mod.result_ok(rec(text)))
        self.assertEqual(runtime_mod.failure_reason(rec('Error: permission denied')), 'permission')


if __name__ == '__main__':
    unittest.main()
