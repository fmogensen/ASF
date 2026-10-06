"""tests.test_intake_severity — acceptance tests for F-0200. Task 1: a title's `S1:`/`S2:`/`S3:`
prefix is read as a severity (`inbox.graded`, `inbox.severity_of`), and a body `Error:` line or a
named failing spec is read as the signature a `type: bug` card was being asked for
(`inbox.body_signature`, `inbox.signed`). Task 2: a card still stuck on a question at S1 is said
aloud, every tick, naming its file (`inbox.stuck_s1_lines`, `cmd_groom`, `status.intake_cell`).
Fixtures are the suite's own: `tests.test_inbox_shape.card` / `seeded_canonical` for the pure
shape probes, `tests.test_groom.make_repo` / `write_item` / `run` for the record root and the
subprocess CLI, `tests.test_inbox_shape.make_record` / `seed` for Task 2's end-to-end reproduction."""
import argparse
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.groom import groom as groom_mod
from asf.groom import inbox, shape
from asf.record import frontmatter
from asf.record.core import today
from asf.views import status
from tests.test_groom import make_repo, run, write_item
from tests.test_inbox_shape import card, make_record, seed, seeded_canonical


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


EPIC_QUESTION = 'Which Epic is this under? No open Epic shares a title word with it.'


class StuckS1IsLoud(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='stuck_s1_test_')
        os.makedirs(os.path.join(self.root, 'inbox'), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _card(self, name, title, question, severity=None):
        lines = [f'# {title}']
        if severity:
            lines.append(f'severity: {severity}')
        lines += ['', 'Some context the card arrived with.', '', '## Question', question, '']
        with open(os.path.join(self.root, 'inbox', name), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

    def test_stuck_at_s1_by_header_is_one_loud_line_naming_the_file(self):
        self._card('a.md', 'Checkout is broken', EPIC_QUESTION, severity='S1')
        lines = inbox.stuck_s1_lines(self.root, 'inbox')
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('NEEDS OPERATOR:'))
        self.assertIn('S1 intake card inbox/a.md', lines[0])
        self.assertIn(f'"{EPIC_QUESTION}"', lines[0])

    def test_stuck_at_s1_by_title_prefix_reads_the_same(self):
        self._card('b.md', 'S1: Payment gateway flaky', EPIC_QUESTION)
        lines = inbox.stuck_s1_lines(self.root, 'inbox')
        self.assertEqual(len(lines), 1)
        self.assertIn('S1 intake card inbox/b.md', lines[0])

    def test_s2_and_s3_cards_say_nothing(self):
        self._card('a.md', 'Checkout is broken', EPIC_QUESTION, severity='S2')
        self._card('b.md', 'Payment is also broken', EPIC_QUESTION)  # no header, no prefix: S3
        self.assertEqual(inbox.stuck_s1_lines(self.root, 'inbox'), [])

    def test_two_stuck_cards_come_back_in_file_name_order(self):
        self._card('b.md', 'Second card', EPIC_QUESTION, severity='S1')
        self._card('a.md', 'First card', EPIC_QUESTION, severity='S1')
        lines = inbox.stuck_s1_lines(self.root, 'inbox')
        self.assertEqual(len(lines), 2)
        self.assertIn('inbox/a.md', lines[0])
        self.assertIn('inbox/b.md', lines[1])

    def test_no_groom_file_yet_gives_the_product_remedy_and_carries_no_machine_path(self):
        self._card('a.md', 'Checkout is broken', EPIC_QUESTION, severity='S1')
        product = argparse.Namespace(name='widgets')
        lines = inbox.stuck_s1_lines(self.root, 'inbox', groom_file='groom/2026-09-29.md',
                                     product=product)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith('asf groom --product widgets'))
        self.assertNotIn(self.root, lines[0])

    def test_no_groom_file_and_no_product_names_the_placeholder(self):
        self._card('a.md', 'Checkout is broken', EPIC_QUESTION, severity='S1')
        lines = inbox.stuck_s1_lines(self.root, 'inbox', groom_file='groom/2026-09-29.md')
        self.assertTrue(lines[0].endswith('asf groom --product <name>'))

    def test_an_existing_groom_file_is_named_in_the_remedy(self):
        self._card('a.md', 'Checkout is broken', EPIC_QUESTION, severity='S1')
        os.makedirs(os.path.join(self.root, 'groom'), exist_ok=True)
        with open(os.path.join(self.root, 'groom', '2026-09-29.md'), 'w', encoding='utf-8') as f:
            f.write('# Groom 2026-09-29\n')
        lines = inbox.stuck_s1_lines(self.root, 'inbox', groom_file='groom/2026-09-29.md')
        self.assertEqual(len(lines), 1)
        self.assertIn('answer its `inbox:a.md` line in groom/2026-09-29.md', lines[0])
        self.assertTrue(lines[0].endswith('(the next tick applies it)'))

    def test_question_lines_carries_the_severity_token_at_s1_and_not_at_s3(self):
        self._card('a.md', 'Checkout is broken', EPIC_QUESTION, severity='S1')
        self._card('b.md', 'Payment is broken', EPIC_QUESTION)  # S3 (no header, no prefix)
        lines = inbox.question_lines(self.root, 'inbox')
        a_line = next(l for l in lines if 'inbox:a.md' in l)
        b_line = next(l for l in lines if 'inbox:b.md' in l)
        self.assertTrue(a_line.startswith('- [ ] inbox:a.md S1 Checkout is broken — '))
        self.assertTrue(b_line.startswith('- [ ] inbox:b.md Payment is broken — '))

    def test_status_intake_cell_names_the_first_line_with_a_count_of_the_rest(self):
        self._card('a.md', 'Checkout is broken', EPIC_QUESTION, severity='S1')
        self._card('b.md', 'Payment gateway flaky', EPIC_QUESTION, severity='S1')
        product = argparse.Namespace(name='widgets',
                                     conventions=argparse.Namespace(intake_dir='inbox'))
        cell = status.intake_cell(self.root, product)
        self.assertIsNotNone(cell)
        self.assertTrue(cell.startswith('NEEDS OPERATOR:'))
        self.assertTrue(cell.endswith('+1 more'))

    def test_status_intake_cell_is_none_with_nothing_stuck(self):
        product = argparse.Namespace(name='widgets',
                                     conventions=argparse.Namespace(intake_dir='inbox'))
        self.assertIsNone(status.intake_cell(self.root, product))

    def test_render_over_a_root_with_no_intake_dir_and_no_product_prints_no_intake_row(self):
        # tests.test_install's status.render('/nonexistent', None, cfg={}) case (PD5): intake_cell
        # must return None itself, not raise into render's per-cell `? (...)` text.
        with mock.patch.object(status, 'version_cell', return_value='v'), \
                mock.patch.object(status, 'runners_cell', return_value=None):
            table = status.render('/nonexistent', None, cfg={})
        self.assertNotIn('Intake', table)


class TwoTicksOfASilentNight(unittest.TestCase):
    """The reproduction, end to end through `cmd_groom`: an `S1:`-titled card with no signature
    and no error line sits on a question for two ticks running, the second changing no file —
    and the groom keeps saying so until a third tick's edit lets it mint."""

    def setUp(self):
        self.root = make_record()
        seed(self.root)
        self.path = os.path.join(self.root, 'inbox', 'flaky.md')
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('# S1: p1-e2e is flaky on main\n\nBlocks the prod deploy.\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _groom(self, default_bug_epic=None):
        args = argparse.Namespace(date=None, apply=False, product=None,
                                  default_bug_epic=default_bug_epic, answers_file=None,
                                  event=None, incremental=True)
        out = io.StringIO()
        with mock.patch.object(env, 'load_product', side_effect=env.ConfigError('none')), \
                contextlib.redirect_stdout(out):
            rc = groom_mod.cmd_groom(args, self.root)
        return rc, out.getvalue()

    def test_a_silent_night_stays_loud_until_the_card_is_minted(self):
        rc, out1 = self._groom()
        self.assertEqual(rc, 0)
        with open(self.path, encoding='utf-8') as f:
            text1 = f.read()
        self.assertIn('## Question', text1)
        self.assertIn(EPIC_QUESTION, text1)
        self.assertIn('NEEDS OPERATOR: S1 intake card inbox/flaky.md', out1)

        rc, out2 = self._groom()
        self.assertEqual(rc, 0)
        with open(self.path, encoding='utf-8') as f:
            text2 = f.read()
        self.assertEqual(text1, text2)  # the second run changes no file …
        self.assertIn('NEEDS OPERATOR: S1 intake card inbox/flaky.md', out2)  # … and still says so
        self.assertIn(f'answer its `inbox:flaky.md` line in groom/{today()}.md', out2)

        with open(self.path, encoding='utf-8') as f:
            text = f.read()
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(text.replace('Blocks the prod deploy.',
                                 'Blocks the prod deploy.\nError: expected 200, got 500'))
        rc, out3 = self._groom(default_bug_epic='E-0001')
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.path))  # minted: the file leaves the intake dir
        bugs = os.listdir(os.path.join(self.root, 'bugs'))
        self.assertEqual(len(bugs), 1)
        with open(os.path.join(self.root, 'bugs', bugs[0]), encoding='utf-8') as f:
            bug_text = f.read()
        meta, _body = frontmatter.parse(bug_text, path=f'bugs/{bugs[0]}')
        self.assertEqual(meta['severity'], 'S1')

        rc, out4 = self._groom(default_bug_epic='E-0001')
        self.assertEqual(rc, 0)
        self.assertNotIn('NEEDS OPERATOR', out4)


if __name__ == '__main__':
    unittest.main()
