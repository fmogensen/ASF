"""tests.test_precedent — the in-repo precedent an adjudicator reads before it parks (F-0262).

Reproduces a product's T-42278 (2026-10-06): the session rightly reverted a raw credential read,
then parked the Task on the operator over a question the repo's own decisions and job-queue
pattern already answered.
"""
import os
import shutil
import tempfile
import types
import unittest

from asf.evidence import precedent
from asf.evidence import rulings
from asf.record import decisions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _TempProduct(unittest.TestCase):
    """A ``docs/decisions/`` of its own, under a fresh ``tempfile.mkdtemp``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='precedent_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def product_with(self, files):
        """A product whose ``docs/decisions/`` holds exactly ``{name: content}``."""
        root = tempfile.mkdtemp(dir=self.tmp)
        folder = os.path.join(root, decisions.DOCS_DIR)
        os.makedirs(folder, exist_ok=True)
        for name, content in files.items():
            with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
                f.write(content)
        return types.SimpleNamespace(repo_dir=root)


class _ReportFixture(unittest.TestCase):
    """A minimal adjudicate REPORT block carrying the given lines."""

    def report(self, *lines):
        return ('REPORT\nitem: T-0001\nkind: adjudicate\nstatus: done\n' + '\n'.join(lines)
                + '\n```\n')


class RegisterTests(_TempProduct):
    """`entries` reads both decision file shapes, the record's cards, and never raises."""

    def test_an_adr_with_no_front_matter_is_read_by_its_h1(self):
        # P3: frontmatter.parse raises on exactly this file shape
        rows = precedent.entries(self.product_with(
            {'0002-ci-routing-labels-are-capabilities.md':
                '# ADR 0002 — CI routing labels name a capability, never a provider\n\n'
                '- **Status:** accepted\n'}))
        self.assertEqual([(r['id'], r['status']) for r in rows], [('D-0002', 'accepted')])
        self.assertIn('CI routing labels', rows[0]['title'])
        self.assertEqual(rows[0]['path'],
                         os.path.join(decisions.DOCS_DIR,
                                      '0002-ci-routing-labels-are-capabilities.md'))

    def test_a_record_card_is_read_by_its_front_matter_title(self):
        rows = precedent.entries(self.product_with(
            {'D-8050.md': '---\nid: D-8050\ntype: decision\ntitle: "A rebased harvest is not '
                          'unpushed"\nstate: New\n---\n\n## Statement\n'}))
        self.assertEqual(rows[0]['id'], 'D-8050')
        self.assertEqual(rows[0]['title'], 'A rebased harvest is not unpushed')

    def test_this_repos_own_decisions_dir_reads_whole(self):
        # the fixture is this checkout: four files, two shapes, four titles, no exception
        rows = precedent.entries(types.SimpleNamespace(repo_dir=ROOT))
        self.assertEqual(sorted(r['id'] for r in rows),
                         ['D-0001', 'D-0002', 'D-0003', 'D-8050'])
        self.assertTrue(all(r['title'] for r in rows), rows)

    def test_the_records_own_decision_cards_are_listed_beside_the_files(self):
        items = {'D-0100': {'id': 'D-0100', 'type': 'decision', 'title': 'a card', 'state': 'New'},
                 'T-0001': {'id': 'T-0001', 'type': 'task', 'title': 'not a decision'}}
        rows = precedent.entries(self.product_with({}), items)
        self.assertEqual([r['id'] for r in rows], ['D-0100'])

    def test_a_retired_decision_card_is_not_precedent(self):
        items = {'D-0100': {'id': 'D-0100', 'type': 'decision', 'title': 'x', 'removed': 'folded'}}
        self.assertEqual(precedent.entries(self.product_with({}), items), [])

    def test_a_stale_status_is_printed_not_dropped(self):   # C5
        rows = precedent.entries(self.product_with(
            {'0009-old.md': '# ADR 0009 — the old way\n\n- **Status:** superseded by ADR 0011\n'}))
        self.assertEqual(rows[0]['status'], 'superseded')

    def test_no_dir_no_product_no_repo_dir_is_an_empty_register_never_an_error(self):
        for p in (None, types.SimpleNamespace(repo_dir=None),
                  types.SimpleNamespace(repo_dir=os.path.join(self.tmp, 'nonexistent'))):
            self.assertEqual(precedent.entries(p), [])

    def test_an_unreadable_file_is_skipped_and_the_rest_are_read(self):
        product = self.product_with(
            {'0002-good.md': '# ADR 0002 — a good one\n\n- **Status:** accepted\n'})
        folder = os.path.join(product.repo_dir, decisions.DOCS_DIR)
        # a directory named *.md: unreadable as a file, and never raised
        os.mkdir(os.path.join(folder, 'x.md'))
        rows = precedent.entries(product)
        self.assertEqual([r['id'] for r in rows], ['D-0002'])

    def test_the_path_is_record_relative_and_carries_no_machine_path(self):
        product = self.product_with(
            {'0002-good.md': '# ADR 0002 — a good one\n\n- **Status:** accepted\n'})
        rows = precedent.entries(product)
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row['path'].startswith(decisions.DOCS_DIR), row['path'])
            self.assertNotIn(self.tmp, row['path'])


class BriefSectionTests(unittest.TestCase):
    def test_the_section_names_the_decisions_the_count_and_both_searches(self):
        rows = [{'id': 'D-0003', 'title': "A Task's planned first review is not repair",
                 'status': 'accepted',
                 'path': os.path.join(decisions.DOCS_DIR,
                                      '0003-a-planned-first-review-is-not-repair.md')}]
        text = precedent.brief_section(rows)
        self.assertIn('IN-REPO PRECEDENT', text)
        self.assertIn('1 question(s)', text)
        self.assertIn("- D-0003 — A Task's planned first review is not repair (accepted) · "
                     + rows[0]['path'], text)
        self.assertIn('the pattern already in the code', text)
        self.assertIn('`precedent:`', text)

    def test_an_empty_register_is_an_empty_section(self):
        self.assertEqual(precedent.brief_section([]), '')

    def test_the_list_is_bounded_and_says_how_many_it_did_not_print(self):
        rows = [{'id': f'D-{n:04d}', 'title': f't{n}', 'status': 'accepted', 'path': 'p'}
                for n in range(1, 31)]
        text = precedent.brief_section(rows)
        self.assertEqual(text.count('\n- '), precedent.BRIEF_MAX)
        self.assertIn(f'{30 - precedent.BRIEF_MAX} older decision(s) not listed', text)

    def test_the_newest_id_is_first(self):
        items = {'D-0002': {'id': 'D-0002', 'type': 'decision', 'title': 'old', 'state': 'accepted'},
                 'D-8050': {'id': 'D-8050', 'type': 'decision', 'title': 'new', 'state': 'New'}}
        rows = precedent.entries(None, items)
        self.assertEqual([r['id'] for r in rows], ['D-8050', 'D-0002'])
        text = precedent.brief_section(rows)
        self.assertLess(text.index('D-8050'), text.index('D-0002'))


class ClaimTests(_ReportFixture):
    def test_a_citation_is_named_by_path_by_id_and_by_adr_number(self):
        for value, want in (('D-0004, workers/jobs/claim.py', 'D-0004'),
                            ('ADR 0002', 'ADR 0002'),
                            ('the pattern in asf/record/decisions.py', 'decisions.py')):
            self.assertIn(want, precedent.named(self.report(f'precedent: {value}')))

    def test_none_with_where_it_looked_names_nothing(self):
        text = self.report('precedent: none — docs/decisions/ (7 entries), grep -rn getenv')
        self.assertEqual(precedent.named(text), '')
        self.assertTrue(precedent.claim(text))

    def test_an_absent_field_is_an_empty_claim(self):
        self.assertEqual(precedent.claim(self.report('ruling: it stands')), '')

    def test_the_three_park_notes_are_three_different_facts(self):
        self.assertIn('precedent: D-0004',
                      precedent.park_note(self.report('precedent: D-0004')))
        self.assertIn('no in-repo precedent',
                      precedent.park_note(self.report('precedent: none — I looked in docs/')))
        self.assertIn('names no precedent', precedent.park_note(self.report('ruling: x')))

    def test_the_history_note_is_a_bracketed_clause_or_nothing(self):
        self.assertEqual(precedent.history_note(self.report('precedent: D-0004')),
                         ' [precedent: D-0004]')
        self.assertEqual(precedent.history_note(self.report('precedent: none — looked')), '')
        self.assertEqual(precedent.history_note(self.report('ruling: x')), '')

    def test_a_history_note_appended_to_a_ruling_line_still_parses_as_a_ruling(self):
        # P12: the citation must survive into rulings.standing, or it is not precedent
        line = (f'- 2026-10-06 14:02 adjudicate (adjudicate-t-0001): upheld; the call goes '
                f'through the job{precedent.history_note(self.report("precedent: D-0004"))}')
        parsed = rulings.parse(f'## History\n{line}\n')
        self.assertEqual(len(parsed), 1)
        self.assertIn('precedent: D-0004', parsed[0]['text'])


if __name__ == '__main__':
    unittest.main()
