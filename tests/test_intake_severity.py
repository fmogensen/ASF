"""tests.test_intake_severity — acceptance tests for F-0200's Task 1: a title's `S1:`/`S2:`/`S3:`
prefix is read as a severity (`inbox.graded`, `inbox.severity_of`), and a body `Error:` line or a
named failing spec is read as the signature a `type: bug` card was being asked for
(`inbox.body_signature`, `inbox.signed`). Fixtures are the suite's own:
`tests.test_inbox_shape.card` / `seeded_canonical` for the pure shape probes,
`tests.test_groom.make_repo` / `write_item` / `run` for the record root and the subprocess CLI."""
import os
import unittest

from asf.groom import inbox, shape
from asf.record import frontmatter
from tests.test_groom import make_repo, run, write_item
from tests.test_inbox_shape import card, seeded_canonical


class SeverityFromTitle(unittest.TestCase):
    def setUp(self):
        self.canonical = seeded_canonical()

    def test_title_prefix_is_a_severity_and_the_title_stops_carrying_it(self):
        c = inbox.graded(card('S1: p1-e2e is flaky on main'))
        self.assertEqual(c.title, 'p1-e2e is flaky on main')
        self.assertEqual(inbox.severity_of(c), 'S1')

    def test_lower_case_and_a_space_before_the_colon_read_the_same(self):
        for title in ('s2: x', 'S2 : x'):
            c = inbox.graded(card(title))
            self.assertEqual(c.title, 'x')
            self.assertEqual(inbox.severity_of(c), 'S2')

    def test_a_severity_header_wins_and_the_prefix_is_cut_anyway(self):
        c = inbox.graded(card('S1: x', headers={'severity': 'S3'}))
        self.assertEqual(c.title, 'x')
        self.assertEqual(inbox.severity_of(c), 'S3')

    def test_severity_header_is_upper_cased_before_the_membership_test(self):
        self.assertEqual(inbox.severity_of(card('x', headers={'severity': 's1'})), 'S1')

    def test_forms_outside_the_grammar_are_titles_left_untouched(self):
        for title in ('S4: x', '[S1] x', 'S1 - x', 'S1:', 'S1:   '):
            c = card(title)
            self.assertIs(inbox.graded(c), c)

    def test_a_bug_titled_with_a_prefix_gets_the_clean_title_as_its_signature(self):
        c = inbox.declared(inbox.graded(card('S1: checkout down', headers={'type': 'bug'})))
        self.assertEqual(c.title, 'checkout down')
        self.assertEqual(c.headers['signature'], 'checkout down')

    def test_end_to_end_the_minted_bug_carries_the_severity_and_the_clean_title(self):
        root = make_repo()
        write_item(root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], root)
        with open(os.path.join(root, 'inbox', 'flaky.md'), 'w', encoding='utf-8') as f:
            f.write('# S1: p1-e2e is flaky on main\nsignature: checkout-pay\n\n'
                    'Blocks the prod deploy.\n')
        r = run(['groom', '--default-bug-epic', 'E-0009'], root)
        self.assertEqual(r.returncode, 0, r.stderr)

        self.assertEqual(os.listdir(os.path.join(root, 'bugs')), ['B-0001.md'])
        with open(os.path.join(root, 'bugs', 'B-0001.md')) as f:
            text = f.read()
        meta, _body = frontmatter.parse(text, path='bugs/B-0001.md')
        self.assertEqual(meta['severity'], 'S1')
        self.assertEqual(meta['title'], 'p1-e2e is flaky on main')

        done_files = os.listdir(os.path.join(root, 'inbox', 'done'))
        self.assertEqual(len(done_files), 1)
        with open(os.path.join(root, 'inbox', 'done', done_files[0])) as f:
            done_text = f.read()
        self.assertIn('# S1: p1-e2e is flaky on main', done_text)


class BodySignature(unittest.TestCase):
    def test_error_lines_are_taken_whole(self):
        for line in ('Error: expected 200, got 500', 'TypeError: x is not a function',
                     'asf.env.ConfigError: no product'):
            self.assertEqual(inbox.body_signature(f'Some context.\n{line}\nMore context.'), line)

    def test_a_failing_spec_line_reads_as_flakys_own_key_with_no_prefix(self):
        from asf.tick import flaky
        for line in ('e2e/checkout.spec.ts:12:3 › pays',
                     '[chromium] › e2e/checkout.spec.ts:12:3 › pays'):
            sig = inbox.body_signature(line)
            self.assertEqual(sig, 'e2e/checkout.spec.ts:12 › pays')
            m = flaky._TEST_RE.match(line)
            self.assertEqual(sig, flaky.test_key({'file': m.group('file').strip(),
                                                  'line': int(m.group('line')),
                                                  'title': m.group('title')}))
            self.assertNotIn(flaky.SIG_PREFIX, sig)

    def test_the_first_matching_line_wins(self):
        self.assertEqual(
            inbox.body_signature('no match here\nError: first\nError: second'), 'Error: first')

    def test_an_over_long_line_is_capped_with_an_ellipsis(self):
        sig = inbox.body_signature('Error: ' + ('x' * 200))
        self.assertEqual(len(sig), inbox.SIGNATURE_MAX)
        self.assertTrue(sig.endswith('…'))

    def test_no_matching_line_is_none(self):
        self.assertIsNone(inbox.body_signature('Nothing here reads as a signature.'))

    def test_signed_is_a_no_op_when_the_card_already_settles_the_reading(self):
        desc = 'Error: expected 200, got 500'
        cards = (
            card('x', description=desc, acceptance=['one']),
            card('x', headers={'writes': 'a/**'}, description=desc),
            card('x', headers={'type': 'feature'}, description=desc),
            card('x', headers={'signature': 'already set'}, description=desc),
        )
        for c in cards:
            self.assertIs(inbox.signed(c), c)

    def test_a_card_with_defect_words_and_no_evidence_still_gets_the_defect_question(self):
        canonical = seeded_canonical()
        result = shape.derive(inbox.signed(card('Checkout is broken')), canonical)
        self.assertIsInstance(result, shape.Question)
        self.assertIn('paste that line into the body', result.text)

    def test_a_flaky_titled_card_with_an_error_line_is_typed_bug_not_asked_for_an_epic(self):
        canonical = seeded_canonical()
        c = inbox.signed(card('p1-e2e is flaky on main', description='Error: expected 200, got 500'))
        self.assertEqual(c.headers['signature'], 'Error: expected 200, got 500')
        result = shape.derive(c, canonical, default_bug_parent='E-0001')
        self.assertEqual(result, shape.Shape('bug', 'signature', 'E-0001'))


if __name__ == '__main__':
    unittest.main()
