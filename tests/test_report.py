"""asf.workers.report — the typed REPORT a session ends with, read back (F-0025), and the prose
fallback a handful of callers still read off the brief's own REPORT block (F-0087, class "worker
behaviour": B-0024, B-0052; F-0025 replan, PD-READERS)."""
import importlib
import json
import unittest

from asf.workers import report

build_mod = importlib.import_module('asf.briefs.build')  # the package attribute is a function

RULED = """REPORT
item: B-0001
kind: adjudicate
status: done
branch: fix/B-0001
pushed: yes abc1234
commits: none
tests: none
left out: none
ruling: the hold was an add/add conflict, not a finding; the fix stands
  and the reviewer's point about the retry is overruled
```
"""

DONE = """I fixed it.

REPORT
item: B-0001
kind: fix-bug
status: done
branch: fix/B-0001
pushed: yes 1a2b3c4
commits: 1a2b3c4 fix(B-0001): return 0 on empty
tests: python3 -m unittest — OK
left out: none
"""

WAITING = """I'll stop here and wait for the background test-suite task to finish; I'll continue
with the commit and push once it reports back.

REPORT
item: B-0001
kind: fix-bug
status: partial
branch: fix/B-0001
pushed: no — the suite is still running in the background
commits: none
tests: python3 -m unittest (background)
left out: the push
"""


def _wrap(body):
    """A text whose only fenced block is a bare ``asf-report`` JSON object, ``body`` (a dict)."""
    return f'```json {report.FENCE_TOKEN}\n{json.dumps(body)}\n```'


class SchemaTest(unittest.TestCase):
    def test_every_brief_kind_has_a_schema(self):
        self.assertEqual(sorted(report.KIND_FIELDS), sorted(build_mod.KINDS))
        for k in build_mod.KINDS:
            report.schema(k)  # does not raise

    def test_every_field_declares_types_a_default_and_a_line(self):
        for k in build_mod.KINDS:
            for key, value in report.schema(k).items():
                self.assertEqual(len(value), 3, key)
                types, _default, instruction = value
                self.assertTrue(instruction, (k, key))
                is_types = all(t in report._CHECK for t in types)
                is_enum = all(t not in report._CHECK for t in types)
                self.assertTrue(is_types or is_enum, (k, key, types))

    def test_a_kind_field_never_shadows_a_common_one(self):
        for k in build_mod.KINDS:
            self.assertEqual(set(report.KIND_FIELDS[k]) & set(report.COMMON), set(), k)

    def test_an_unknown_kind_refuses(self):
        with self.assertRaises(report.ReportError):
            report.schema('preflight')

    def test_the_new_kinds_fields_match_their_templates_final_message(self):
        """PD-KINDS: each of the seven kinds landed since the approved spec's table was written
        is derived one for one off its own template's ``Final message:`` line."""
        expect = {
            'spec-amend': ('path you wrote', 'sections', 'story count'),
            'groom-clerk': ('answered', 'operator'),
            'spec-plan': ('path you wrote', 'story', 'task', 'coverage'),
            'direct': ('files written',),
            'delivery-plan': ('plan path', 'build order'),
            'delivery-code': ('one line per item',),
            'replan': ('replan path', 'task'),
        }
        for k, words in expect.items():
            instructions = ' '.join(instr.lower() for _types, _default, instr
                                    in report.KIND_FIELDS[k].values())
            for word in words:
                self.assertIn(word, instructions, (k, word))


class ParseTest(unittest.TestCase):
    def test_the_block_is_read_out_of_the_final_message(self):
        text = 'some prose\n\n' + report.render('spec', item='S-0001')
        obj = report.typed(text, 'spec')
        self.assertEqual(obj['item'], 'S-0001')

    def test_the_last_block_wins(self):
        text = report.render('spec', item='A') + '\n\n' + report.render('spec', item='B')
        self.assertEqual(report.typed(text, 'spec')['item'], 'B')

    def test_no_block_is_a_named_failure(self):
        why = report.check('spec', 'I did the thing.')
        self.assertIn(report.FENCE_TOKEN, why)

    def test_broken_json_is_a_named_failure(self):
        text = '```json asf-report\n{"item":\n```'
        why = report.check('spec', text)
        self.assertIn('json', why.lower())

    def test_the_block_must_be_an_object(self):
        text = '```json asf-report\n[1, 2]\n```'
        why = report.check('spec', text)
        self.assertIn('object', why)

    def test_an_unknown_key_is_refused(self):
        obj = json.loads(report.fence(report.render('spec')))
        obj['plan'] = 'docs/plans/x.md'
        why = report.check('spec', _wrap(obj))
        self.assertIn('plan', why)

    def test_a_missing_required_key_is_refused(self):
        obj = json.loads(report.fence(report.render('spec')))
        del obj['pushed']
        why = report.check('spec', _wrap(obj))
        self.assertIn('pushed', why)

    def test_a_bad_enum_is_refused(self):
        obj = json.loads(report.fence(report.render('spec')))
        obj['status'] = 'finished'
        why = report.check('spec', _wrap(obj))
        self.assertIn('done, partial, blocked', why)

    def test_the_three_cross_field_rules(self):
        base = json.loads(report.fence(report.render('spec')))

        yes_no_sha = dict(base, pushed='yes')
        yes_no_sha.pop('sha', None)
        why = report.check('spec', _wrap(yes_no_sha))
        self.assertIn('sha', why)

        no_no_why = dict(base, pushed='no')
        no_no_why.pop('why', None)
        why = report.check('spec', _wrap(no_no_why))
        self.assertIn('why', why)

        blocked_empty = dict(base, status='blocked', needs_operator=[], left_out=[])
        why = report.check('spec', _wrap(blocked_empty))
        self.assertIn('blocked', why)

        satisfied = dict(base, pushed='yes', sha='abc1234')
        self.assertIsNone(report.check('spec', _wrap(satisfied)))
        satisfied_no = dict(base, pushed='no', why='the suite is still running')
        satisfied_no.pop('sha', None)
        self.assertIsNone(report.check('spec', _wrap(satisfied_no)))
        satisfied_blocked = dict(base, status='blocked', left_out=['the migration — DB unreachable'])
        self.assertIsNone(report.check('spec', _wrap(satisfied_blocked)))

    def test_a_good_report_of_every_kind_parses(self):
        for k in build_mod.KINDS:
            text = report.render(k)
            report.typed(text, k)  # does not raise
            self.assertIsNone(report.check(k, text))


class RenderTest(unittest.TestCase):
    def test_render_round_trips_through_typed(self):
        for k in build_mod.KINDS:
            obj = report.typed(report.render(k, item='F-0025'), k)
            self.assertEqual(obj['item'], 'F-0025')

    def test_render_refuses_a_field_the_kind_does_not_have(self):
        with self.assertRaises(report.ReportError):
            report.render('close', stories=[])

    def test_render_fills_every_required_field(self):
        for k in build_mod.KINDS:
            obj = report.typed(report.render(k), k)
            required = {key for key, (_t, default, _i) in report.schema(k).items()
                        if default is report.REQ}
            self.assertTrue(required.issubset(set(obj)), k)


class ContractTest(unittest.TestCase):
    def test_the_contract_names_every_field_of_the_kind(self):
        for k in build_mod.KINDS:
            text = report.contract(k)
            for key, (_types, _default, instruction) in report.schema(k).items():
                self.assertIn(key, text, (k, key))
                self.assertIn(instruction, text, (k, key))

    def test_the_contract_is_the_shape_it_asks_for(self):
        for k in build_mod.KINDS:
            body = report.fence(report.contract(k))
            rendered = json.loads(report.fence(report.render(k)))
            for key in report.schema(k):
                body = body.replace(f'"<<{key}>>"', json.dumps(rendered.get(key)))
            wrapped = f'```{report.FENCE_TOKEN}\n{body}\n```'
            self.assertIsNone(report.check(k, wrapped), k)

    def test_the_common_fields_come_first_in_every_kind(self):
        prefix = None
        for k in build_mod.KINDS:
            lines = report.fence(report.contract(k)).splitlines()
            first = lines[1:1 + len(report.COMMON)]
            if prefix is None:
                prefix = first
            else:
                self.assertEqual(first, prefix, k)


class ReadersTest(unittest.TestCase):
    """The card's second acceptance, stated as a contradiction: every reader reads the typed
    object, never the prose."""

    def test_the_json_wins_over_the_prose(self):
        # runtime.failure_reason/result_ok are not in this card's footprint (writes:) — Task 3
        # (T-0112) is what repoints runtime.py onto report.failure; this exercises report.py's
        # own reading of the contradiction directly.
        text = ('REPORT\nstatus: partial\npushed: no — waiting for the suite\n\n' +
                report.render('fix-bug', status='done', pushed='yes', sha='abc1234'))
        self.assertIsNone(report.failure(text))

    def test_and_the_other_way_round(self):
        text = ('REPORT\nstatus: done\npushed: yes abc1234\n\n' +
                report.render('fix-bug', pushed='no', why='the suite is still running'))
        self.assertEqual(report.failure(text), report.UNPUSHED)

    def test_the_ruling_is_read_off_the_json(self):
        text = ('REPORT\nruling: the prose ruling\n\n' +
                report.render('adjudicate', ruling='the typed ruling'))
        self.assertEqual(report.ruling(text), 'the typed ruling')

    def test_the_prose_reader_is_gone(self):
        self.assertFalse(hasattr(report, 'parse'))
        self.assertFalse(hasattr(report, 'unpushed'))

    def test_a_malformed_report_is_not_a_failure_reason(self):
        text = 'I did the work, no block at all.'
        self.assertIsNone(report.failure(text))

    def test_summary_is_one_line(self):
        done = json.loads(report.fence(report.render('coder')))
        self.assertEqual(report.summary(done).count('\n'), 0)
        no = json.loads(report.fence(
            report.render('coder', pushed='no', why='the suite is still running')))
        s = report.summary(no)
        self.assertEqual(s.count('\n'), 0)
        self.assertIn('the suite is still running', s)
        blocked = json.loads(report.fence(
            report.render('coder', status='blocked', needs_operator=['rotate the key'])))
        s = report.summary(blocked)
        self.assertEqual(s.count('\n'), 0)
        self.assertIn('rotate the key', s)


class RulingFieldsTest(unittest.TestCase):
    """F-0090 D10 / F-0025 replan PD5: the ruling's mechanism is three typed fields, read off the
    last REPORT's typed object — never the prose (restates the old prose-level assertions
    against the object)."""

    def test_the_three_fields_are_read(self):
        text = report.render('adjudicate', ruling='it waits', blocked_on='T-0025',
                             writes=['asf/a.py', 'tests/test_a.py', 'docs/**'],
                             superseded_by='T-0030')
        self.assertEqual(report.ruling_fields(text),
                         {'blocked_on': 'T-0025', 'writes': ['asf/a.py', 'tests/test_a.py', 'docs/**'],
                          'superseded_by': 'T-0030'})

    def test_an_absent_field_is_none(self):
        text = report.render('adjudicate', ruling='it stands')
        self.assertEqual(report.ruling_fields(text),
                         {'blocked_on': None, 'writes': None, 'superseded_by': None})

    def test_an_explicit_null_is_also_none(self):
        obj = json.loads(report.fence(report.render('adjudicate', ruling='it stands')))
        obj.update(blocked_on=None, writes=None, superseded_by=None)
        text = _wrap(obj)
        self.assertEqual(report.ruling_fields(text),
                         {'blocked_on': None, 'writes': None, 'superseded_by': None})

    def test_no_report_at_all_is_no_claim(self):
        none = {'blocked_on': None, 'writes': None, 'superseded_by': None}
        self.assertEqual(report.ruling_fields('no report at all'), none)


class FailureAtTheSourceTest(unittest.TestCase):
    def test_pushed_no_is_unpushed_work(self):
        self.assertEqual(report.failure(report.render('fix-bug', pushed='no', why='reason')),
                         report.UNPUSHED)
        self.assertIsNone(report.failure(report.render('fix-bug')))
        self.assertIsNone(report.failure('done'))  # the fake runtime's results carry no report
        self.assertIsNone(report.failure(''))



# ---- the prose fallback (PD-READERS): unchanged behavior, read off `_prose` -------------------

class ProseTests(unittest.TestCase):
    def test_reads_every_field_of_the_last_block(self):
        rep = report._prose('REPORT\nitem: X\n\n' + DONE)
        self.assertEqual(rep['item'], 'B-0001')
        self.assertEqual(rep['status'], 'done')
        self.assertEqual(rep['pushed'], 'yes 1a2b3c4')
        self.assertEqual(rep['left out'], 'none')
        self.assertEqual(report._prose('no report here'), {})

    def test_a_multi_line_field_and_a_fence_end(self):
        text = 'REPORT\nitem: T-0001\ncommits: aaa one\nbbb two\ntests: ok\n```\nnot: a field\n'
        rep = report._prose(text)
        self.assertEqual(rep['commits'], 'aaa one\nbbb two')
        self.assertNotIn('not', rep)

    def test_proves_trailers_are_one_field_not_folded_into_left_out(self):
        """F-0040 P6: each `Proves:` trailer line collides, case-insensitively, with the
        `proves:` field name itself — a naive parse would restart the field on every line."""
        text = ('REPORT\nitem: T-0195\nstatus: done\nleft out: none\n'
                'proves: Proves: S-18750 line 1 — tests/test_proves.py::ParseTests::test_trailer\n'
                'Proves: S-18750 line 2 — tests/test_proves.py::ParseTests::test_duplicates\n'
                'ruling: not this kind\n```\n')
        rep = report._prose(text)
        self.assertEqual(rep['left out'], 'none')
        self.assertEqual(rep['proves'],
                         'Proves: S-18750 line 1 — tests/test_proves.py::ParseTests::test_trailer\n'
                         'Proves: S-18750 line 2 — tests/test_proves.py::ParseTests::test_duplicates')
        self.assertEqual(rep['ruling'], 'not this kind')


class NeedsInputTests(unittest.TestCase):
    def test_the_first_needs_operator_line_of_two(self):
        text = ('NEEDS OPERATOR: rotate the deploy key — run tools/rotate.sh\n'
                'NEEDS OPERATOR: approve the migration — run asf migrate --apply\n'
                'REPORT\nitem: B-0001\nstatus: done\n')
        self.assertEqual(report.needs_input(text),
                          'rotate the deploy key — run tools/rotate.sh')

    def test_a_needs_operator_line_outside_any_report_block_is_still_found(self):
        text = ('I did the work.\nNEEDS OPERATOR: confirm the rollback — run tools/rollback.sh\n'
                '\nREPORT\nitem: B-0001\nstatus: done\nbranch: fix/B-0001\n')
        self.assertEqual(report.needs_input(text), 'confirm the rollback — run tools/rollback.sh')

    def test_a_blocked_report_with_no_operator_line_gives_its_left_out(self):
        text = ('REPORT\nitem: B-0001\nstatus: blocked\nbranch: fix/B-0001\n'
                'left out: the migration — the prod DB is unreachable from this host\n')
        self.assertEqual(report.needs_input(text),
                          'the migration — the prod DB is unreachable from this host')

    def test_a_blocked_report_with_no_left_out_gives_the_first_line_of_the_text(self):
        text = ('The prod DB is unreachable from this host.\n\n'
                'REPORT\nitem: B-0001\nstatus: blocked\nbranch: fix/B-0001\nleft out: none\n')
        self.assertEqual(report.needs_input(text), 'The prod DB is unreachable from this host.')

    def test_none_for_a_done_report(self):
        self.assertIsNone(report.needs_input(DONE))

    def test_none_for_a_report_that_merely_mentions_waiting_for_ci(self):
        text = ('REPORT\nitem: B-0001\nstatus: partial\nbranch: fix/B-0001\n'
                'left out: the push — waiting for CI to go green\n')
        self.assertIsNone(report.needs_input(text))

    def test_none_for_text_with_no_report_at_all(self):
        self.assertIsNone(report.needs_input('I did the work and pushed it.'))

    def test_none_for_empty_and_none_text(self):
        self.assertIsNone(report.needs_input(''))
        self.assertIsNone(report.needs_input(None))

    def test_unfinished_still_pairs_partial_and_blocked(self):
        self.assertEqual(report.UNFINISHED, ('partial', 'blocked'))


class OperatorCommandTests(unittest.TestCase):
    """B-0042: the exact command a NEEDS OPERATOR question names, backtick-fenced."""

    def test_the_backtick_command_is_extracted(self):
        self.assertEqual(report.operator_command('rotate the key — `tools/rotate.sh`'),
                         'tools/rotate.sh')

    def test_the_last_of_several_backtick_spans_wins(self):
        q = 'check `git log -1` against `git status --short`'
        self.assertEqual(report.operator_command(q), 'git status --short')

    def test_empty_for_a_question_with_no_command(self):
        self.assertEqual(report.operator_command('approve the migration before it runs'), '')

    def test_empty_for_none_and_empty(self):
        self.assertEqual(report.operator_command(''), '')
        self.assertEqual(report.operator_command(None), '')


class RebasedTests(unittest.TestCase):
    def test_the_sha_is_read_off_the_prose_pushed_line(self):
        text = 'REPORT\nitem: B-0001\npushed: rebased abc1234def — the factory publishes\n'
        self.assertEqual(report.rebased(text), 'abc1234def')

    def test_empty_for_a_plain_push(self):
        self.assertEqual(report.rebased('REPORT\nitem: B-0001\npushed: yes abc1234\n'), '')


class FootprintClaimTests(unittest.TestCase):
    def test_needs_writes_names_paths(self):
        text = 'REPORT\nstatus: partial\nneeds writes: a.py b.py\n'
        self.assertEqual(report.footprint_claim(text), ('needs writes', ['a.py', 'b.py']))

    def test_a_done_pushed_report_claims_nothing(self):
        text = 'REPORT\nstatus: done\npushed: yes abc1234\nneeds writes: a.py\n'
        self.assertEqual(report.footprint_claim(text), (None, []))


class NoSplitTests(unittest.TestCase):
    def test_the_answer_is_read(self):
        text = "NO SPLIT: T-0100 does not split along area — one gate covers both halves"
        self.assertEqual(report.no_split(text),
                         'T-0100 does not split along area — one gate covers both halves')

    def test_empty_for_a_normal_report(self):
        self.assertEqual(report.no_split(DONE), '')


if __name__ == '__main__':
    unittest.main()
