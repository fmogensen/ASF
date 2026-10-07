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


class ProseTests(unittest.TestCase):
    def test_the_cards_own_claim(self):
        self.assertEqual(
            proves.claim_prose('tests/guard.test.ts (type guard, size guard, EXIF strip only)'),
            'type guard size guard EXIF strip only')

    def test_citation_only_shapes_are_empty(self):
        for test in (
            'tests/test_proves.py::ParseTests',
            'tests/only-guard.test.ts',
            'tests/e2e/half/upload.spec.ts',
            'src/guards/except.test.tsx',
            'tests/minus.spec.ts',
            'tests/partial.test.js::"refuses a qualified claim"',
            'tests/test_half_a_line.py',
        ):
            self.assertEqual(proves.claim_prose(test), '', test)

    def test_a_quoted_span_is_blanked_in_all_three_quote_shapes(self):
        for test in (
            'tests/guard.test.ts "refuses a half-open range"',
            'tests/guard.test.ts ("the partial case")',
            'tests/guard.test.ts “except the EXIF strip”',
        ):
            self.assertEqual(proves.claim_prose(test), '', test)

    def test_a_two_word_marker_matches_across_tokens(self):
        prose = proves.claim_prose('tests/a.py — the size guard is not yet written')
        self.assertIn('not yet', prose)


class PartialClaimTests(unittest.TestCase):
    CARD_CLAIM = ('Proves: S-0021 line 6 — tests/guard.test.ts '
                  '(type guard, size guard, EXIF strip only)\n')

    def test_the_cards_own_claim_is_refused_not_counted(self):
        counted, refused = proves.parse_all(self.CARD_CLAIM)
        self.assertEqual(counted, [])
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].refusal, 'partial claim: "only"')
        self.assertEqual(refused[0].raw, self.CARD_CLAIM.strip())
        self.assertEqual(proves.parse(self.CARD_CLAIM), [])

    def test_one_marker_fires_per_claim_and_the_reason_names_it(self):
        _, refused = proves.parse_all(self.CARD_CLAIM)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].refusal, 'partial claim: "only"')

    def test_all_seven_default_markers_fire_case_insensitively(self):
        for marker, word in (
            ('only', 'Only'), ('partial', 'PARTIAL'), ('partly', 'partly'),
            ('half', 'half'), ('except', 'except'), ('not yet', 'not yet'),
            ('minus', 'minus'),
        ):
            text = f'Proves: S-0021 line 1 — tests/a.py ({word} covered)\n'
            _, refused = proves.parse_all(text)
            self.assertEqual(len(refused), 1, marker)
            self.assertEqual(refused[0].refusal, f'partial claim: "{marker}"', marker)

    def test_half_done_fires_on_half(self):
        text = 'Proves: S-0021 line 1 — tests/a.py (half-done)\n'
        _, refused = proves.parse_all(text)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].refusal, 'partial claim: "half"')

    def test_the_commit_subject_fixture_parses_to_one_counted_claim(self):
        text = 'fix(B-0001): the first half\n\nProves: S-0077 line 1 — tests/test_a.py'
        counted, refused = proves.parse_all(text)
        self.assertEqual(refused, [])
        self.assertEqual(len(counted), 1)
        self.assertEqual(counted[0].test, 'tests/test_a.py')

    def test_a_marker_only_inside_a_quoted_span_is_counted_the_accepted_hole(self):
        text = 'Proves: S-0021 line 1 — tests/g.test.ts "EXIF strip only"\n'
        counted, refused = proves.parse_all(text)
        self.assertEqual(refused, [])
        self.assertEqual(len(counted), 1)

    def test_a_mixed_text_of_one_whole_and_one_qualified_claim(self):
        text = (
            'Proves: S-0021 line 1 — tests/a.py\n'
            'Proves: S-0021 line 2 — tests/b.py (only partial)\n'
            'Proves: S-0021 line 1 — tests/a.py\n'
            'Proves: S-0021 line 2 — tests/b.py (only partial)\n'
        )
        counted, refused = proves.parse_all(text)
        self.assertEqual(len(counted), 1)
        self.assertEqual(len(refused), 1)
        self.assertEqual(counted[0].line, 1)
        self.assertEqual(refused[0].line, 2)


class MarkerConfigTests(unittest.TestCase):
    def test_unset_is_the_default(self):
        self.assertEqual(self._markers(cfg={}), proves.PARTIAL_MARKERS)

    def _markers(self, cfg):
        from asf import config_keys
        return tuple(str(m) for m in config_keys.value('proves.partial_markers',
                                                        proves.PARTIAL_MARKERS, cfg=cfg)
                     if str(m).strip())

    def test_the_operators_list_replaces_wholesale(self):
        cfg = {'proves': {'partial_markers': ['bogus']}}
        self.assertEqual(proves.partial_marker('tests/a.py (bogus clause)', self._markers(cfg)),
                          'bogus')
        self.assertEqual(proves.partial_marker('tests/a.py (only part)', self._markers(cfg)), '')

    def test_an_empty_list_refuses_nothing_the_cards_claim_included(self):
        markers = self._markers({'proves': {'partial_markers': []}})
        self.assertEqual(markers, ())
        self.assertEqual(
            proves.partial_marker(
                'tests/guard.test.ts (type guard, size guard, EXIF strip only)', markers), '')

    def test_a_malformed_value_keeps_the_default(self):
        for cfg in ({'proves': {'partial_markers': 'only'}}, {'proves': {'partial_markers': {}}}):
            self.assertEqual(self._markers(cfg), proves.PARTIAL_MARKERS, cfg)

    def test_config_keys_value_does_not_raise(self):
        from asf import config_keys
        config_keys.value('proves.partial_markers', proves.PARTIAL_MARKERS, cfg={})


class NotProvedTrailerTests(unittest.TestCase):
    def test_trailer_reads_into_a_claim_with_empty_test(self):
        text = 'Not proved: S-0021 line 7 — the EXIF strip has no test yet\n'
        claims = proves.parse_not_proved(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].story, 'S-0021')
        self.assertEqual(claims[0].line, 7)
        self.assertEqual(claims[0].test, '')
        self.assertEqual(claims[0].refusal, 'the EXIF strip has no test yet')

    def test_all_three_dashes_and_a_bullet_prefix(self):
        for prefix, dash in (('', '-'), ('', '–'), ('', '—'), ('- ', '-')):
            text = f'{prefix}Not proved: S-0021 line 7 {dash} the EXIF strip has no test yet\n'
            claims = proves.parse_not_proved(text)
            self.assertEqual(len(claims), 1, text)

    def test_duplicates_collapse(self):
        text = ('Not proved: S-0021 line 7 — gap\n' * 2)
        self.assertEqual(len(proves.parse_not_proved(text)), 1)

    def test_a_line_naming_no_id_no_line_or_no_gap_yields_nothing(self):
        self.assertEqual(proves.parse_not_proved('Not proved: nothing useful here\n'), [])
        self.assertEqual(proves.parse_not_proved('Not proved: S-0021 — no line number\n'), [])
        self.assertEqual(proves.parse_not_proved('Not proved: S-0021 line 7 — \n'), [])

    def test_a_proves_line_is_not_read_as_a_not_proved_one(self):
        text = 'Proves: S-0021 line 7 — tests/a.py\n'
        self.assertEqual(proves.parse_not_proved(text), [])


class RenderNotProvedTests(unittest.TestCase):
    def test_a_refused_claim_carries_its_test_in_parens(self):
        claims = [Claim('S-0021', 6, 'tests/guard.test.ts …', 'raw', 'partial claim: "only"')]
        self.assertEqual(proves.render_not_proved(claims),
                          '- S-0021 line 6 — partial claim: "only" (tests/guard.test.ts …)')

    def test_a_declared_gap_has_no_parenthesis(self):
        claims = [Claim('S-0021', 7, '', 'raw', 'the EXIF strip has no test yet')]
        self.assertEqual(proves.render_not_proved(claims),
                          '- S-0021 line 7 — the EXIF strip has no test yet')

    def test_empty_list_is_empty_string(self):
        self.assertEqual(proves.render([]), '')
        self.assertEqual(proves.render_not_proved([]), '')

    def test_render_of_a_counted_claim_is_unchanged(self):
        claims = [Claim('S-18750', 1, 'tests/test_proves.py::A', 'raw a')]
        self.assertEqual(proves.render(claims), '- S-18750 line 1 — tests/test_proves.py::A')


class UnknownStoryTests(unittest.TestCase):
    """F-0285: a ``Proves:`` line on a Story id the record holds no card for (a spec that cited
    ids nobody minted) proves nothing, and says so: ``Not proved: … — unknown story``."""

    TEXT = ('Proves: S-29504 line 1 — tests/a.test.ts\n'
            'Proves: S-0001 line 2 — tests/b.test.ts\n')

    def test_given_the_records_stories_a_phantom_claim_is_refused_as_unknown(self):
        counted, refused = proves.parse_all(self.TEXT, known={'S-0001': {'type': 'story'}})
        self.assertEqual([c.story for c in counted], ['S-0001'])
        self.assertEqual([(c.story, c.refusal) for c in refused], [('S-29504', 'unknown story')])
        self.assertEqual(proves.render_not_proved(refused),
                         '- S-29504 line 1 — unknown story (tests/a.test.ts)')

    def test_an_id_set_works_too_and_a_non_story_card_is_unknown(self):
        _c, refused = proves.parse_all(self.TEXT, known={'S-0001'})
        self.assertEqual([c.story for c in refused], ['S-29504'])
        _c, refused = proves.parse_all(self.TEXT, known={'S-0001': {'type': 'task'},
                                                         'S-29504': {'type': 'story'}})
        self.assertEqual([c.story for c in refused], ['S-0001'])

    def test_without_the_record_nothing_changes(self):
        counted, refused = proves.parse_all(self.TEXT)
        self.assertEqual((len(counted), refused), (2, []))


class UnprovedReasonTests(unittest.TestCase):
    BODY = ("## Acceptance\n- [ ] one\n- [ ] two\n- [ ] three\n- [ ] four\n- [ ] five\n"
            "- [ ] six\n## History\n")

    def test_a_mapping_gives_its_text_for_the_named_line(self):
        out = proves.unproved(self.BODY, refused={6: 'partial claim: "only"'})
        by_line = {n: why for n, _text, why in out}
        self.assertEqual(by_line[6], 'partial claim: "only"')
        self.assertEqual(by_line[5], proves.NO_PROOF)

    def test_a_bare_iterable_gives_partial_claim(self):
        out = proves.unproved(self.BODY, refused=(6,))
        by_line = {n: why for n, _text, why in out}
        self.assertEqual(by_line[6], proves.PARTIAL_CLAIM)

    def test_a_line_already_proved_is_absent_even_when_refused_names_it(self):
        refused = {6: 'partial claim: "only"'}

        body = self.BODY.replace('## History\n', '## History\n- ingest: proved line 6 (x)\n')
        out = proves.unproved(body, refused=refused)
        self.assertNotIn(6, {n for n, _t, _w in out})

        out = proves.unproved(self.BODY, also_proved=(6,), refused=refused)
        self.assertNotIn(6, {n for n, _t, _w in out})

        deferred_body = self.BODY.replace('- [ ] six', '- [ ] six — deferred by D-0012')
        out = proves.unproved(deferred_body, register=('D-0012',), refused=refused)
        self.assertNotIn(6, {n for n, _t, _w in out})

        with tempfile.TemporaryDirectory() as repo_dir:
            with open(os.path.join(repo_dir, 'x.py'), 'w', encoding='utf-8') as f:
                f.write('pass\n')
            ticked_body = self.BODY.replace('- [ ] six', '- [x] six — proven by x.py')
            out = proves.unproved(ticked_body, repo_dir=repo_dir, refused=refused)
            self.assertNotIn(6, {n for n, _t, _w in out})

    def test_no_refused_is_identical_to_today(self):
        self.assertEqual(proves.unproved(self.BODY), proves.unproved(self.BODY, refused=()))


if __name__ == '__main__':
    unittest.main()
