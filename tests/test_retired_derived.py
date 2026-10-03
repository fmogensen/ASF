"""A retired card (``removed:`` or ``moved_to:``) derives empty ``## Children`` and
``## Backlinks`` sections — the four words inside the ``if`` at
``asf/record/core.py:301-302`` (``is_retired(rec['meta'])``), landed in ``b191575fd`` and
never covered by a test since. This file is the first one.
"""
import shutil
import unittest

from asf.record.check import record_findings
from asf.record.core import build_index_data, compute_derived, load_items, canonicalize
from asf.record.index import refresh
from tests.test_groom import make_repo, write_item

CHILDREN_STALE = '## Children section is stale (run `asf index`)'
BACKLINKS_STALE = '## Backlinks section is stale (run `asf index`)'

LIVE_BODY = (
    "## Description\n\nMentions the retired feature F-0002.\n\n"
    "## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n"
)

RETIRED_BODY_FILLED = (
    "## Description\n\nMentions the live feature F-0001.\n\n"
    "## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-01: created\n\n"
    "## Children\n- [S-0001](../stories/S-0001.md) A story — New\n\n"
    "## Backlinks\n- [F-0001](../features/F-0001.md) Live feature\n"
)

RETIRED_BODY_NO_SECTIONS = (
    "## Description\n\nMentions the live feature F-0001.\n\n"
    "## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-01: created\n"
)


class RetiredDerivedSectionsTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, True)

    def build(self, retire_line, body=None):
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Live feature', parent='E-0001',
                   body=LIVE_BODY)
        write_item(self.root, 'F-0002', 'feature', 'Retired feature', parent='E-0001',
                   typed_lines=[retire_line],
                   body=RETIRED_BODY_FILLED if body is None else body)
        write_item(self.root, 'S-0001', 'story', 'A story', parent='F-0002')

    def test_a_retired_card_is_stale_on_both_headings(self):
        self.build('removed: obsolete')
        findings, _warnings, _index_wrong = record_findings(self.root)
        pairs = {(relpath, msg) for relpath, _line, msg in findings}
        self.assertIn(('features/F-0002.md', CHILDREN_STALE), pairs)
        self.assertIn(('features/F-0002.md', BACKLINKS_STALE), pairs)

    def test_a_refresh_empties_the_retired_sections_and_keeps_the_live_one(self):
        self.build('removed: obsolete')
        refresh(self.root, create_index=True)
        with open(f"{self.root}/features/F-0002.md", encoding='utf-8') as f:
            retired_text = f.read()
        with open(f"{self.root}/features/F-0001.md", encoding='utf-8') as f:
            live_text = f.read()
        self.assertIn("## Children\n\n## Backlinks\n", retired_text)
        self.assertIn(
            "## Backlinks\n- [F-0002](../features/F-0002.md) Retired feature\n", live_text)

    def test_the_record_converges_after_one_refresh(self):
        self.build('removed: obsolete')
        refresh(self.root, create_index=True)
        findings, _warnings, _index_wrong = record_findings(self.root)
        for _relpath, _line, msg in findings:
            self.assertNotIn(msg, (CHILDREN_STALE, BACKLINKS_STALE))

    def test_moved_to_reads_exactly_as_removed(self):
        self.build('moved_to: other:F-0049')
        findings, _warnings, _index_wrong = record_findings(self.root)
        pairs = {(relpath, msg) for relpath, _line, msg in findings}
        self.assertIn(('features/F-0002.md', CHILDREN_STALE), pairs)
        self.assertIn(('features/F-0002.md', BACKLINKS_STALE), pairs)
        refresh(self.root, create_index=True)
        with open(f"{self.root}/features/F-0002.md", encoding='utf-8') as f:
            retired_text = f.read()
        self.assertIn("## Children\n\n## Backlinks\n", retired_text)

    def test_a_card_retired_before_its_sections_were_written_is_byte_identical(self):
        self.build('removed: obsolete', body=RETIRED_BODY_NO_SECTIONS)
        with open(f"{self.root}/features/F-0002.md", encoding='utf-8') as f:
            before = f.read()
        refresh(self.root, create_index=True)
        with open(f"{self.root}/features/F-0002.md", encoding='utf-8') as f:
            after = f.read()
        self.assertEqual(before, after)
        findings, _warnings, _index_wrong = record_findings(self.root)
        for relpath, _line, msg in findings:
            if msg in (CHILDREN_STALE, BACKLINKS_STALE):
                self.assertNotEqual(relpath, 'features/F-0002.md')

    def test_the_retired_cards_graph_is_kept_on_purpose(self):
        """``compute_derived`` and ``index.json`` still carry the retired card's real children
        and backlinks after a refresh — only the rendered body is emptied. ``close_exact_duplicate``,
        ``close_duplicate_task`` and ``close_on_starvation`` all guard on ``derived[iid]['children']``,
        and ``_descendants`` rolls a retired Feature's subtree cost up through the index's
        ``children`` (P13, P14): a later reader must not "fix" this to match the emptied body.
        """
        self.build('removed: obsolete')
        refresh(self.root, create_index=True)
        by_id, _parse_errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        derived = compute_derived(canonical)
        self.assertEqual(derived['F-0002'], {'children': ['S-0001'], 'backlinks': ['F-0001']})
        index_data = build_index_data(canonical, derived)
        self.assertEqual(index_data['items']['F-0002']['children'], ['S-0001'])
        self.assertEqual(index_data['items']['F-0002']['backlinks'], ['F-0001'])
        # the rule's other boundary: a live card still carries every live child and backlink
        self.assertEqual(derived['F-0001'], {'children': [], 'backlinks': ['F-0002']})


if __name__ == '__main__':
    unittest.main()
