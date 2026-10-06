"""asf.groom.sticky — G1: the digest that excludes groom's own written fields, the ledger's
re-ask window, the ``structural``/``rank_owner`` flags, and a replay pin over an anonymised
slice of groom answers (F-0101 §4): how many of them a sticky window would have held out of
the file, had it been on at the time."""
import datetime
import json
import os
import shutil
import tempfile
import unittest

from asf.env import Product
from asf.groom import sticky

HERE = os.path.dirname(os.path.abspath(__file__))
REPLAY_ANSWERS = os.path.join(HERE, 'fixtures', 'replay', 'groom_answers.json')
UTC = datetime.timezone.utc


class DigestTests(unittest.TestCase):
    def test_a_groom_written_field_does_not_change_the_digest(self):
        before = sticky.digest({'type': 'feature', 'title': 'x', 'decided': False}, 'body')
        after = sticky.digest({'type': 'feature', 'title': 'x', 'decided': True, 'rank': 3},
                              'body')
        self.assertEqual(before, after)

    def test_a_typed_fact_changes_the_digest(self):
        a = sticky.digest({'type': 'bug', 'title': 'x', 'count': 2}, 'body')
        b = sticky.digest({'type': 'bug', 'title': 'x', 'count': 3}, 'body')
        self.assertNotEqual(a, b)

    def test_history_is_excluded_but_the_rest_of_the_body_is_not(self):
        a = sticky.digest({'type': 'feature'}, 'intro\n## History\n- old line')
        b = sticky.digest({'type': 'feature'}, 'intro\n## History\n- a new line today')
        self.assertEqual(a, b)
        c = sticky.digest({'type': 'feature'}, 'a different intro\n## History\n- old line')
        self.assertNotEqual(a, c)

    def test_machine_fields_are_excluded(self):
        from asf.record.frontmatter import FrontmatterDict
        a = FrontmatterDict(type='feature', title='x')
        a.machine_keys = {'stage_since'}
        a['stage_since'] = '2026-09-01T00:00:00Z'
        b = FrontmatterDict(type='feature', title='x')
        b.machine_keys = {'stage_since'}
        b['stage_since'] = '2026-10-06T00:00:00Z'
        self.assertEqual(sticky.digest(a, 'body'), sticky.digest(b, 'body'))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def _canonical(self, iid, **meta):
        return {iid: {'meta': {'type': 'feature', 'title': 'Some card', **meta}, 'body': ''}}

    def test_round_trips_through_record_and_load(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'undecided3', 'adjudicator:groom-x')],
                              canonical)
        data = sticky.load_ledger(self.d)
        self.assertIn('F-0001|decide', data)

    def test_a_rule_answer_is_never_recorded(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'undecided3', 'rule:close_on_starvation')],
                              canonical)
        self.assertEqual(sticky.load_ledger(self.d), {})

    def test_sticky_within_the_window_on_an_unchanged_card(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'undecided3', 'operator')], canonical)
        now = datetime.datetime.now(UTC) + datetime.timedelta(days=3)
        self.assertTrue(sticky.is_sticky(self.d, 'F-0001', 'undecided3', canonical, 7, now))

    def test_not_sticky_once_the_card_changes(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'undecided3', 'operator')], canonical)
        changed = self._canonical('F-0001', title='A different title')
        now = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
        self.assertFalse(sticky.is_sticky(self.d, 'F-0001', 'undecided3', changed, 7, now))

    def test_not_sticky_past_the_window(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'undecided3', 'operator')], canonical)
        now = datetime.datetime.now(UTC) + datetime.timedelta(days=8)
        self.assertFalse(sticky.is_sticky(self.d, 'F-0001', 'undecided3', canonical, 7, now))

    def test_reask_days_off_is_never_sticky(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'undecided3', 'operator')], canonical)
        self.assertFalse(sticky.is_sticky(self.d, 'F-0001', 'undecided3', canonical, None))

    def test_section_group_collapses_the_decide_sections(self):
        for section in ('inbox', 'undecided_new', 'undecided3', 'undecided14'):
            self.assertEqual(sticky.section_group(section), 'decide')
        self.assertEqual(sticky.section_group('no_stories'), 'no_stories')
        self.assertEqual(sticky.section_group('dupes'), 'dupes')

    def test_answering_under_one_decide_section_holds_out_every_other(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'inbox', 'operator')], canonical)
        now = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
        self.assertTrue(sticky.is_sticky(self.d, 'F-0001', 'undecided3', canonical, 7, now))

    def test_filter_sticky_drops_only_open_lines_of_a_sticky_card(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'undecided3', 'operator')], canonical)
        now = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
        sections = {'undecided3': ['- [ ] F-0001 Some card — undecided 4d → answer: ____'],
                   'dupes': ['- [ ] F-0001 Some card — near-duplicate of F-0002 → answer: ____']}
        out, count = sticky.filter_sticky(sections, self.d, canonical, 7, now)
        self.assertEqual(count, 1)
        self.assertEqual(out['undecided3'], [])
        self.assertEqual(len(out['dupes']), 1)  # a different section's own question is untouched

    def test_filter_sticky_never_touches_an_already_answered_line(self):
        canonical = self._canonical('F-0001')
        sticky.record_answers(self.d, [('F-0001', 'undecided3', 'operator')], canonical)
        now = datetime.datetime.now(UTC) + datetime.timedelta(days=1)
        sections = {'undecided3': ['- [x] F-0001 Some card — undecided 4d → answer: yes']}
        out, count = sticky.filter_sticky(sections, self.d, canonical, 7, now)
        self.assertEqual(count, 0)
        self.assertEqual(len(out['undecided3']), 1)


class ReportLineTests(unittest.TestCase):
    def test_converts_an_open_question_line(self):
        line = '- [ ] F-0001 Lonely feature — no Stories → answer: ____'
        self.assertEqual(sticky.to_report_line(line),
                         '- F-0001 Lonely feature — no Stories (report)')

    def test_an_answered_line_is_unchanged(self):
        line = '- [x] F-0001 Lonely feature — no Stories → answer: (spoken for: CARD → SPEC)'
        self.assertEqual(sticky.to_report_line(line), line)

    def test_a_non_card_line_is_unchanged(self):
        self.assertEqual(sticky.to_report_line('(none)'), '(none)')


class FlagTests(unittest.TestCase):
    def test_structural_default_is_report(self):
        self.assertEqual(sticky.structural(Product('p', {})), 'report')
        self.assertEqual(sticky.structural(None), 'report')

    def test_structural_ask_override(self):
        p = Product('p', {'conventions': {'flags': {'groom': {'structural': 'ask'}}}})
        self.assertEqual(sticky.structural(p), 'ask')

    def test_structural_unknown_value_falls_back_to_report(self):
        p = Product('p', {'conventions': {'flags': {'groom': {'structural': 'bogus'}}}})
        self.assertEqual(sticky.structural(p), 'report')

    def test_rank_owner_default_is_code(self):
        self.assertEqual(sticky.rank_owner(Product('p', {})), 'code')
        self.assertEqual(sticky.rank_owner(None), 'code')

    def test_rank_owner_adjudicator_override(self):
        p = Product('p', {'conventions': {'flags': {'groom': {'rank_owner': 'adjudicator'}}}})
        self.assertEqual(sticky.rank_owner(p), 'adjudicator')

    def test_reask_days_default_and_off(self):
        self.assertEqual(sticky.reask_days(Product('p', {})), 7)
        p = Product('p', {'conventions': {'flags': {'groom': {'reask_days': 'off'}}}})
        self.assertIsNone(sticky.reask_days(p))

    def test_reask_days_override(self):
        p = Product('p', {'conventions': {'flags': {'groom': {'reask_days': 14}}}})
        self.assertEqual(sticky.reask_days(p), 14)


class ReplayPinTests(unittest.TestCase):
    """Replays ``tests/fixtures/replay/groom_answers.json`` — each entry one historical groom
    occurrence of a card under a section, in order — against a sticky window of 7 days on a
    card whose own facts never change across the fixture: a repeat occurrence of the same
    ``<id>|<group>`` inside the window is a question the sticky rule would have avoided; one
    past it, or under a different id, is not."""

    def setUp(self):
        with open(REPLAY_ANSWERS, encoding='utf-8') as f:
            self.entries = json.load(f)
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def _replay(self, days=7):
        canonical_by_id = {}
        avoided = []
        for e in sorted(self.entries, key=lambda x: (x['day'], x['id'])):
            iid = e['id']
            canonical = canonical_by_id.setdefault(
                iid, {iid: {'meta': {'type': 'feature', 'title': f'Card {iid}'}, 'body': ''}})
            now = datetime.datetime.strptime(e['day'], '%Y-%m-%d').replace(tzinfo=UTC)
            if sticky.is_sticky(self.d, iid, e['t'], canonical, days, now):
                avoided.append(e)
                continue
            sticky.record_answers(self.d, [(iid, e['t'], 'adjudicator:groom-x')], canonical,
                                  now=now)
        return avoided

    def test_fixture_shape(self):
        self.assertEqual(len(self.entries), 7)

    def test_three_occurrences_are_avoided(self):
        avoided = self._replay()
        self.assertEqual(len(avoided), 3)
        self.assertTrue(all(e['id'] == 'F-0001' for e in avoided))
        self.assertEqual([e['day'] for e in avoided],
                         ['2026-09-30', '2026-10-01', '2026-10-02'])

    def test_the_gap_past_the_window_is_not_avoided(self):
        avoided = self._replay()
        self.assertNotIn('2026-10-07', [e['day'] for e in avoided if e['id'] == 'B-0001'])

    def test_a_single_occurrence_is_never_avoided(self):
        avoided = self._replay()
        self.assertFalse(any(e['id'] == 'F-0003' for e in avoided))

    def test_turning_the_window_off_avoids_nothing(self):
        self.assertEqual(self._replay(days=None), [])


if __name__ == '__main__':
    unittest.main()
