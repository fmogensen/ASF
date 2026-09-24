"""Two groom defects seen 2026-09-24.

1. An undecided card younger than three days, filed neither through the inbox nor with a
   signature (B-0087: an S2 Bug, "rule: filed"), had a line in no groom section: no rule, no
   adjudicator and no operator was ever asked, and it sat undecided while ``asf status`` counted
   it. Every undecided card now has an open line each groom.
2. An answer carrying its one sentence of why (``yes — one session, one gate``, the grammar the
   adjudicate brief itself asks for) matched no answer word, so ``asf groom --apply`` printed
   ``applied 0`` and said nothing. The word before the ``—`` is the answer; a line whose answer
   is still not one the grammar knows is named, with why it was skipped.
"""
import argparse
import datetime
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from asf.groom import digest, groom, policy
from asf.record import frontmatter
from asf.record.core import canonicalize, compute_derived, load_items
from tests.test_groom import make_repo, run, write_item
from tests.test_groom_shape import ShapeRepo, meta_of

UTC = datetime.timezone.utc


def _now_lines():
    now = datetime.datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    return ['state: New', f'stage_since: {now}', f'updated: {now}']


def _load(root):
    canonical, _ = canonicalize(load_items(root)[0])
    return canonical, compute_derived(canonical)


def _meta(root, folder, iid):
    with open(os.path.join(root, folder, f'{iid}.md'), encoding='utf-8') as f:
        return frontmatter.parse(f.read(), path=f'{folder}/{iid}.md')


class EveryUndecidedCardIsAskedTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['decided: true'])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_fresh_undecided_bug_filed_by_rule_gets_an_open_question(self):
        write_item(self.root, 'B-0087', 'bug', 'The console is never told what a tick did',
                   parent='E-0001', typed_lines=['severity: S2', 'decided: false'],
                   machine_lines=_now_lines())
        canonical, derived = _load(self.root)
        sections = groom.build_groom_sections(canonical, derived, '2026-09-24')
        text = groom.render_groom_file('2026-09-24', sections)
        self.assertIn('B-0087', [iid for iid, _line in policy.open_questions(text)], text)

    def test_every_undecided_card_has_a_line_in_a_decision_section(self):
        write_item(self.root, 'B-0001', 'bug', 'Fresh bug', parent='E-0001',
                   typed_lines=['severity: S3'], machine_lines=_now_lines())
        write_item(self.root, 'F-0001', 'feature', 'Fresh feature', parent='E-0001',
                   typed_lines=['decided: false'], machine_lines=_now_lines())
        write_item(self.root, 'F-0002', 'feature', 'Old feature', parent='E-0001',
                   typed_lines=['decided: false'])
        write_item(self.root, 'F-0003', 'feature', 'Decided feature', parent='E-0001',
                   typed_lines=['decided: true'], machine_lines=_now_lines())
        canonical, derived = _load(self.root)
        sections = groom.build_groom_sections(canonical, derived, '2026-09-24')
        asked = {line.split()[3] for key in policy.DECISION_SECTIONS
                 for line in sections.get(key) or []}
        self.assertLessEqual({'B-0001', 'F-0001', 'F-0002'}, asked)
        # a card already asked in another section is not asked twice
        fresh = sections['undecided_new']
        self.assertEqual(sorted(l.split()[3] for l in fresh), ['B-0001', 'F-0001'])


class AnswerWithWhyTests(unittest.TestCase):
    def test_the_word_before_the_dash_is_the_answer(self):
        self.assertEqual(groom._parse_answer('yes — one session, one gate'), ('decided', True))
        self.assertEqual(groom._parse_answer('S2 — loses decisions'), ('severity', 'S2'))
        self.assertEqual(groom._parse_answer('rank 3 — not this week'), ('rank', 3))

    def test_a_no_with_why_carries_the_why_as_the_reason(self):
        self.assertEqual(groom._parse_answer('no — the same defect as B-0105'),
                         ('removed', 'the same defect as B-0105'))


class AdjudicatorAnswersWithWhyTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Some idea', parent='E-0009',
                   typed_lines=['decided: false'])
        write_item(self.root, 'B-0001', 'bug', 'Groom edits drift', parent='E-0009',
                   typed_lines=['decided: false'])
        with open(os.path.join(self.root, 'groom', '2026-09-21.md'), 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-21\n\n## Undecided > 3 days\n\n"
                    "- [ ] F-0001 Some idea — undecided 4d → answer: ____\n"
                    "- [ ] B-0001 Groom edits drift — undecided 4d → answer: ____\n")
        self.dir = tempfile.mkdtemp(prefix='groom_answers_')
        self.path = os.path.join(self.dir, '2026-09-21.answers')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write("- [ ] F-0001 Some idea — undecided 4d → answer: adjudicator: yes — "
                    "self-upgrade stops products drifting.\n"
                    "- [ ] B-0001 Groom edits drift — undecided 4d → answer: adjudicator: S2 — "
                    "edits are lost against the tick clone.\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_answers_with_a_why_are_applied_and_a_severity_decides_the_bug(self):
        rc = groom.cmd_groom(argparse.Namespace(
            date='2026-09-22', apply=False, product=None, default_bug_epic=None,
            answers_file=self.path, event=None), self.root)
        self.assertEqual(rc, 0)
        self.assertIs(_meta(self.root, 'features', 'F-0001')[0]['decided'], True)
        bug = _meta(self.root, 'bugs', 'B-0001')[0]
        self.assertEqual(bug['severity'], 'S2')
        self.assertIs(bug['decided'], True)


class OperatorAnswerOnAGroomLineTests(ShapeRepo):
    MERGE = '- [ ] T-0001 merge T-0001+T-0002 — F-0001: writes overlap → answer: '

    def merge_fixture(self):
        self.task('T-0001', ['asf/groom/**'], stories=['S-0001'])
        self.task('T-0002', ['asf/groom/groom.py', 'tests/x.py'], stories=['S-0001', 'S-0002'])
        run(['index'], self.root)

    def test_merge_yes_with_a_why_applies(self):
        self.merge_fixture()
        self.answer(self.MERGE + 'yes — one session, one gate')
        r = self.groom('--apply')
        self.assertIn('applied 2', r.stdout)
        self.assertEqual(meta_of(self.root, 'T-0002')['removed'], 'merged into T-0001 (groom 2026-09-21)')

    def test_an_answer_the_grammar_does_not_know_is_named_as_skipped(self):
        self.merge_fixture()
        self.answer(self.MERGE + 'sure thing')
        r = self.groom('--apply')
        self.assertIn('applied 0', r.stdout)
        self.assertIn("T-0001 answer 'sure thing' skipped", r.stdout)


class DigestParksNothingOnTheOperatorTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_an_open_question_past_the_cap_goes_to_the_next_adjudicator_not_the_operator(self):
        groom_text = ("# Groom 2026-09-22\n\n## Undecided > 3 days\n\n"
                      "- [ ] F-0020 Another idea — undecided 4d → answer: ____\n")
        canonical, _ = canonicalize(load_items(self.root)[0])
        text = digest.render_digest(self.root, '2026-09-22', canonical, groom_text, [],
                                    attempts=2, cap=2)
        self.assertNotIn('NEEDS OPERATOR: F-0020', text)
        self.assertIn('0 for you', text)
        self.assertIn('F-0020', text)


if __name__ == '__main__':
    unittest.main()
