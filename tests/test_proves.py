"""tests.test_proves — F-0040, Task 1 (S-18750): the claim grammar, the bullet reader, the
validator, the idempotent tick and the branch reader. Pure functions over text and temp dirs; no
network, no ``~/.ASF``, no fixture repo — the branch-reading half takes a stub ``git`` callable
(D11)."""
import os
import tempfile
import unittest

from asf import proves
from asf.proves import Claim


class ParseTests(unittest.TestCase):
    def test_trailer_after_a_commit_subject_with_signoff_after_it(self):
        text = (
            "fix(T-0123): the parser reads a Proves trailer\n\n"
            "Proves: S-18750 line 1 — tests/test_proves.py::ParseTests::test_trailer\n\n"
            "Signed-off-by: A <a@example.com>\n"
        )
        claims = proves.parse(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].story, 'S-18750')
        self.assertEqual(claims[0].line, 1)
        self.assertEqual(claims[0].test, 'tests/test_proves.py::ParseTests::test_trailer')
        self.assertIn('Proves:', claims[0].raw)
        self.assertNotIn('Signed-off-by', claims[0].raw)

    def test_claim_under_a_proves_bullet_in_a_pull_request_body(self):
        text = "## Proves\n- Proves: S-18750 line 1 - tests/test_proves.py::ParseTests\n"
        claims = proves.parse(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].story, 'S-18750')
        self.assertEqual(claims[0].line, 1)
        self.assertEqual(claims[0].test, 'tests/test_proves.py::ParseTests')

    def test_all_three_dashes_and_a_bare_hyphen(self):
        for dash in ('-', '–', '—'):
            text = f"Proves: S-18750 line 1 {dash} tests/test_proves.py::ParseTests\n"
            claims = proves.parse(text)
            self.assertEqual(len(claims), 1, dash)
            self.assertEqual(claims[0].test, 'tests/test_proves.py::ParseTests')

    def test_a_line_with_no_dash_parses_as_nothing(self):
        text = "Proves: S-18750 line 1 tests/test_proves.py::ParseTests\n"
        self.assertEqual(proves.parse(text), [])

    def test_says_proves_but_names_no_id_no_line_or_no_test_is_nothing(self):
        self.assertEqual(proves.parse("Proves: nothing useful here\n"), [])
        self.assertEqual(proves.parse("Proves: S-18750 — no line number\n"), [])
        self.assertEqual(proves.parse("Proves: S-18750 line 1 — \n"), [])

    def test_duplicates_collapsed_in_first_seen_order(self):
        text = (
            "Proves: S-18750 line 1 — tests/test_proves.py::A\n"
            "Proves: S-18751 line 2 — tests/test_proves.py::B\n"
            "Proves: S-18750 line 1 — tests/test_proves.py::A\n"
        )
        claims = proves.parse(text)
        self.assertEqual([(c.story, c.line, c.test) for c in claims], [
            ('S-18750', 1, 'tests/test_proves.py::A'),
            ('S-18751', 2, 'tests/test_proves.py::B'),
        ])

    def test_a_five_digit_and_a_four_digit_story_id_both_parse(self):
        text = (
            "Proves: S-18750 line 1 — tests/test_proves.py::A\n"
            "Proves: S-1234 line 2 — tests/test_proves.py::B\n"
        )
        claims = proves.parse(text)
        self.assertEqual({c.story for c in claims}, {'S-18750', 'S-1234'})


class BulletsTests(unittest.TestCase):
    def test_checkbox_bullets_with_a_non_acceptance_section_between_them(self):
        body = (
            "## Acceptance\n"
            "- [ ] first\n"
            "## Notes\n"
            "not a bullet\n"
            "## Acceptance\n"
            "- [x] second\n"
            "## History\n"
            "- irrelevant\n"
        )
        self.assertEqual(proves.bullets(body), ['first', 'second'])

    def test_no_acceptance_heading_yields_empty(self):
        self.assertEqual(proves.bullets("## History\n- [ ] not counted here\n"), [])

    def test_leading_whitespace_and_uppercase_x_are_tolerated(self):
        body = "## Acceptance\n  - [ ] one\n  - [X] two\n"
        self.assertEqual(proves.bullets(body), ['one', 'two'])


class CardBulletsTests(unittest.TestCase):
    def test_unreadable_path_yields_empty(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(proves.card_bullets(root, {'folder': 'stories', 'id': 'S-1'}), [])

    def test_reads_the_cards_acceptance_bullets(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'stories'))
            with open(os.path.join(root, 'stories', 'S-18750.md'), 'w', encoding='utf-8') as f:
                f.write("## Acceptance\n- [ ] one\n- [ ] two\n")
            got = proves.card_bullets(root, {'folder': 'stories', 'id': 'S-18750'})
            self.assertEqual(got, ['one', 'two'])


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        os.makedirs(os.path.join(self.root.name, 'stories'))
        with open(os.path.join(self.root.name, 'stories', 'S-18750.md'), 'w',
                  encoding='utf-8') as f:
            f.write("## Acceptance\n- [ ] one\n- [ ] two\n")
        self.story = {'type': 'story', 'folder': 'stories', 'id': 'S-18750'}
        self.task = {'type': 'task', 'stories': ['S-18750']}
        self.items = {'S-18750': self.story, 'T-0123': self.task}

    def _claim(self, line=1, story='S-18750', test='tests/test_proves.py::ParseTests'):
        return Claim(story, line, test, f'Proves: {story} line {line} — {test}')

    def test_no_claim(self):
        good, problems = proves.validate([], self.task, self.items, self.root.name)
        self.assertEqual(good, [])
        self.assertEqual(problems, ['no claim'])

    def test_unknown_story(self):
        claim = self._claim(story='S-9999')
        good, problems = proves.validate([claim], self.task, self.items, self.root.name)
        self.assertEqual(good, [])
        self.assertEqual(problems, [f'unknown story: {claim.raw}'])

    def test_a_bug_id_is_also_unknown_story(self):
        self.items['S-9999'] = {'type': 'bug', 'folder': 'bugs', 'id': 'S-9999'}
        claim = self._claim(story='S-9999')
        good, problems = proves.validate([claim], self.task, self.items, self.root.name)
        self.assertEqual(problems, [f'unknown story: {claim.raw}'])

    def test_not_this_tasks(self):
        other_task = {'type': 'task', 'stories': ['S-0001']}
        claim = self._claim()
        good, problems = proves.validate([claim], other_task, self.items, self.root.name)
        self.assertEqual(good, [])
        self.assertEqual(problems, [f"not this Task's: {claim.raw}"])

    def test_no_such_line(self):
        claim = self._claim(line=7)
        good, problems = proves.validate([claim], self.task, self.items, self.root.name)
        self.assertEqual(good, [])
        self.assertEqual(problems, [f'no such line: {claim.raw}'])

    def test_no_such_test_only_checked_when_tree_given(self):
        claim = self._claim(test='tests/nope.py::Missing')
        good, problems = proves.validate([claim], self.task, self.items, self.root.name)
        self.assertEqual(problems, [])
        self.assertEqual(good, [claim])

        good, problems = proves.validate([claim], self.task, self.items, self.root.name,
                                          tree=['tests/test_proves.py'])
        self.assertEqual(good, [])
        self.assertEqual(problems, [f'no such test: {claim.raw}'])

    def test_a_test_path_before_double_colon_is_what_is_looked_up(self):
        claim = self._claim(test='tests/test_proves.py::ParseTests::test_x')
        good, problems = proves.validate([claim], self.task, self.items, self.root.name,
                                          tree=['tests/test_proves.py'])
        self.assertEqual(problems, [])
        self.assertEqual(good, [claim])

    def test_one_good_and_one_malformed_claim_is_a_rejection(self):
        good_claim = self._claim(line=1)
        bad_claim = self._claim(line=99)
        good, problems = proves.validate([good_claim, bad_claim], self.task, self.items,
                                          self.root.name)
        self.assertEqual(good, [good_claim])
        self.assertEqual(problems, [f'no such line: {bad_claim.raw}'])


class TickTests(unittest.TestCase):
    def test_flips_exactly_the_nth_bullet_leaving_the_rest_byte_identical(self):
        body = "## Acceptance\n- [ ] one\n- [ ] two\n- [ ] three\n## History\n- x\n"
        new_body, changed = proves.tick(body, 2, 'ignored')
        self.assertTrue(changed)
        self.assertEqual(new_body,
                          "## Acceptance\n- [ ] one\n- [x] two\n- [ ] three\n## History\n- x\n")

    def test_a_second_tick_of_the_same_line_is_a_no_op(self):
        body = "## Acceptance\n- [ ] one\n- [ ] two\n"
        once, changed1 = proves.tick(body, 1, 'note')
        twice, changed2 = proves.tick(once, 1, 'note')
        self.assertTrue(changed1)
        self.assertFalse(changed2)
        self.assertEqual(once, twice)

    def test_a_line_that_does_not_exist_is_unchanged(self):
        body = "## Acceptance\n- [ ] one\n"
        new_body, changed = proves.tick(body, 5, 'note')
        self.assertFalse(changed)
        self.assertEqual(new_body, body)

    def test_note_is_never_written_by_this_function(self):
        body = "## Acceptance\n- [ ] one\n"
        new_body, _ = proves.tick(body, 1, 'a distinctive note nobody should see')
        self.assertNotIn('distinctive note', new_body)


class RenderTests(unittest.TestCase):
    def test_one_bullet_per_claim_the_trailers_own_text(self):
        claims = [
            Claim('S-18750', 1, 'tests/test_proves.py::A', 'raw a'),
            Claim('S-18751', 2, 'tests/test_proves.py::B', 'raw b'),
        ]
        self.assertEqual(proves.render(claims),
                          "- S-18750 line 1 — tests/test_proves.py::A\n"
                          "- S-18751 line 2 — tests/test_proves.py::B")


class BranchTests(unittest.TestCase):
    def test_claims_on_branch_asks_for_the_one_log_range(self):
        calls = []

        def git(*args):
            calls.append(args)
            return "Proves: S-18750 line 1 — tests/test_proves.py::ParseTests\n"

        claims = proves.claims_on_branch(git, 'main', 'worker/T-0123')
        self.assertEqual(calls, [('log', '--format=%B', 'origin/main..origin/worker/T-0123')])
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].story, 'S-18750')

    def test_an_empty_range_yields_no_claims(self):
        claims = proves.claims_on_branch(lambda *a: '', 'main', 'worker/T-0123')
        self.assertEqual(claims, [])


if __name__ == '__main__':
    unittest.main()
