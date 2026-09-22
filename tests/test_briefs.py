"""asf.briefs: a golden brief per kind off the fixture record, the preamble's cap, the
placeholder contract, and the rule that no template may carry a name from
``tools/forbidden-names.txt``.

The fixture record under ``tests/fixtures/briefs/record/`` is a whole small backlog — an Epic,
a Feature with a spec and a plan link, its Story, a Task with a ``writes:`` footprint, a Feature
stuck at review round 4, and an S1 Bug whose ``## Fix`` is its plan — with the ``index.json``
those cards produce. Regenerate the goldens with ``ASF_UPDATE_GOLDEN=1 python3 -m unittest
tests.test_briefs``; read the diff before you commit one.
"""
import argparse
import importlib
import os
import dataclasses
import json
import re
import unittest

from asf import briefs
from asf.briefs import preamble as preamble_mod
from asf.env import Product
from asf.feeder.rows import Row

# ``asf.briefs.build`` is both the package's entry function and a submodule; the function wins
# the attribute lookup, so the module is asked for by name.
build_mod = importlib.import_module('asf.briefs.build')

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, 'fixtures', 'briefs')
RECORD = os.path.join(FIXTURES, 'record')
GOLDEN = os.path.join(FIXTURES, 'golden')
PATTERNS_FILE = os.path.join(REPO_ROOT, 'tools', 'forbidden-names.txt')
UPDATE = bool(os.environ.get('ASF_UPDATE_GOLDEN'))

CONVENTIONS = {
    'specs_dir': 'docs/specs',
    'plans_dir': 'docs/plans',
    'reviews_dir': 'docs/reviews',
    'review_pattern': 'docs/reviews/{n}-{slug}.md',
    'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task', 'fix': 'fix'},
}
REPO_FACTS = {
    'head': 'abc1234 record the provider outcome',
    'branch_exists': False,
    'files': {'docs/specs/checkout-resilience.md': 312,
              'docs/plans/checkout-resilience.md': 188},
    'tests': [],
    'last_report': 'REPORT\nstatus: partial\nleft out: the retry itself, F-0001 owns it',
}


def product(**extra):
    conv = dict(CONVENTIONS)
    conv.update(extra.pop('conventions', {}))
    data = {'backlog_dir': RECORD, 'main': 'main', 'repo_slug': 'acme/sample',
            'job_grants': ['~/.ASF/products'], 'conventions': conv}
    data.update(extra)
    return Product('sample', data)


def index():
    with open(os.path.join(RECORD, 'index.json'), encoding='utf-8') as f:
        return json.load(f)


def row(kind, item_id, brief_kind, branch, reason, feature_id='F-0001'):
    return Row(tier=2, kind=kind, item_id=item_id, feature_id=feature_id,
               action='would launch', brief_kind=brief_kind, branch=branch, reason=reason)


#: One row per kind — the four the feeder emits for this fixture, plus the five the rest of the
#: loop raises (a review after a coder, a fixer after a review, a rebase, a close, a spec/plan).
ROWS = {
    'spec': row('CARD → SPEC', 'F-0001', 'spec', 'spec/F-0001', 'decided card, no spec'),
    'plan': row('STARVED → PLAN', 'F-0001', 'plan', 'plan/F-0001', 'spec approved, no plan'),
    'coder': row('PLAN → CODE', 'T-0001', 'task', 'task/T-0001', 'plan approved, footprint free'),
    'review': row('CODE → REVIEW', 'T-0001', 'review', 'task/T-0001', 'the coder pushed'),
    'fixer': row('REVIEW → FIX', 'T-0001', 'fixer', 'task/T-0001', 'changes requested, r1'),
    'rebase': row('CONFLICT → REBASE', 'T-0001', 'rebase', 'task/T-0001',
                  'PR does not merge cleanly, no session on it'),
    'close': row('STALE → CLOSE', 'T-0001', 'close', 'task/T-0001',
                 'PR closed unmerged, branch left behind'),
    'adjudicate': row('STALEMATE → ADJUDICATE', 'F-0002', 'adjudicate', 'spec/F-0002',
                      'spec-review r4 >= r4: adjudicate, no further round', feature_id='F-0002'),
    'correct': dataclasses.replace(
        row('FIX → CORRECT', 'B-0001', 'correct', 'fix/B-0001', 'the harvest gate went red, round 1'),
        correction='FAIL: test_red_gate'),
    'fix-bug': row('BUG → FIX', 'B-0001', 'fix-bug', 'fix/B-0001',
                   'S1 open, decided, no session — its ## Fix is the plan'),
}


def forbidden_regex():
    pats = []
    with open(PATTERNS_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if line:
                pats.append(line)
    return re.compile('|'.join(pats), re.IGNORECASE)


class GoldenBriefTest(unittest.TestCase):
    """One golden brief per kind — the whole text, so a change to any of it is visible."""

    def test_golden_per_kind(self):
        for kind, r in sorted(ROWS.items()):
            with self.subTest(kind=kind):
                brief = briefs.build(product(), r, index(), [], REPO_FACTS)
                self.assertEqual(brief.kind, kind)
                path = os.path.join(GOLDEN, f'{kind}.md')
                if UPDATE:
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(brief.text)
                with open(path, encoding='utf-8') as f:
                    self.assertEqual(brief.text, f.read())

    def test_every_kind_has_a_template_and_a_golden(self):
        for kind in build_mod.KINDS:
            self.assertTrue(os.path.exists(build_mod.template_path(kind)), kind)
            self.assertIn(kind, ROWS)

    def test_first_line_is_the_backlog_item(self):
        brief = briefs.build(product(), ROWS['fix-bug'], index(), [], REPO_FACTS)
        self.assertEqual(brief.text.splitlines()[0],
                         'Backlog item: B-0001 — Checkout returns 500 when the payment '
                         'provider times out')

    def test_first_line_says_none_with_a_reason_when_the_card_is_not_in_the_index(self):
        r = row('PLAN → CODE', 'T-9999', 'task', 'task/T-9999', 'plan approved, footprint free')
        brief = briefs.build(product(), r, index(), [], REPO_FACTS)
        self.assertEqual(brief.text.splitlines()[0],
                         'Backlog item: none (T-9999 is not in the index)')
        self.assertEqual(brief.item_id, 'T-9999')

    def test_a_fix_bug_brief_carries_the_fix_and_the_named_test(self):
        text = briefs.build(product(), ROWS['fix-bug'], index(), [], REPO_FACTS).text
        self.assertIn('Catch the provider\'s timeout in `app/checkout/pay.py`', text)
        self.assertIn('tests/test_checkout.py::test_timeout_is_pending_not_500', text)
        self.assertNotIn('docs/reviews/', text)   # the S1 lane has no review round
        # a commit that names the card is the evidence; an already-landed fix still gets one
        self.assertIn('fix(B-0001):', text)
        self.assertIn('--allow-empty', text)

    def test_a_coder_brief_names_the_writes_boundary(self):
        text = briefs.build(product(), ROWS['coder'], index(), [], REPO_FACTS).text
        self.assertIn('app/checkout/attempts.py, tests/test_checkout.py', text)
        self.assertIn('outside that list is a refusal', text)

    def test_a_review_brief_asks_for_the_check_table(self):
        text = briefs.build(product(), ROWS['review'], index(), [], REPO_FACTS).text
        self.assertIn('| check | result | evidence |', text)
        self.assertIn('verdict: approved', text)

    def test_a_fixer_always_pushes(self):
        text = briefs.build(product(), ROWS['fixer'], index(), [], REPO_FACTS).text
        self.assertIn('ALWAYS PUSH SOMETHING', text)

    def test_an_adjudicate_brief_asks_for_a_decision_card_body(self):
        text = briefs.build(product(), ROWS['adjudicate'], index(), [], REPO_FACTS).text
        self.assertIn('DECISION CARD BODY', text)
        self.assertIn('## Consequences', text)

    def test_the_tail_carries_the_typed_report_and_the_operator_marker(self):
        text = briefs.build(product(), ROWS['spec'], index(), [], REPO_FACTS).text
        self.assertIn('\nREPORT\nitem: F-0001\nkind: spec\n', text)
        self.assertIn('NEEDS OPERATOR: <what> — <the command or the answer needed>', text)


class PreambleTest(unittest.TestCase):
    def test_identifiers_are_there_without_the_session_looking(self):
        text = preamble_mod.build(product(), ROWS['coder'], index(), [], REPO_FACTS)
        for fact in ('T-0001', 'F-0001', 'E-0001', 'task/T-0001',
                     'abc1234 record the provider outcome',
                     '`docs/specs/checkout-resilience.md` (312 lines)',
                     '`docs/plans/checkout-resilience.md` (188 lines)',
                     'app/checkout/attempts.py'):
            self.assertIn(fact, text)

    def test_a_spec_path_in_the_prose_is_not_read_as_a_test(self):
        sections = {'fix': 'see `docs/specs/checkout-resilience.md`, then run '
                           '`tests/test_checkout.py::test_one`'}
        self.assertEqual(preamble_mod.named_tests(sections, {}, None),
                         ['tests/test_checkout.py::test_one'])

    def test_a_document_the_record_does_not_carry_is_not_passed_off_as_one(self):
        text = preamble_mod.build(product(), ROWS['adjudicate'], index(), [], REPO_FACTS)
        self.assertIn('Spec: not in the record — its place is `docs/specs/f-0002.md`', text)

    def test_the_last_report_is_carried_over(self):
        text = preamble_mod.build(product(), ROWS['fixer'], index(), [], REPO_FACTS)
        self.assertIn('left out: the retry itself, F-0001 owns it', text)

    def test_sessions_in_flight_are_named(self):
        inflight = [{'item': 'T-0002', 'kind': 'coder', 'account': 'w1', 'age': '12m'}]
        text = preamble_mod.build(product(), ROWS['coder'], index(), inflight, REPO_FACTS)
        self.assertIn('Sessions in flight: T-0002 (coder, 12m)', text)

    def test_a_fact_nobody_passed_is_unknown_not_guessed(self):
        text = preamble_mod.build(product(), ROWS['coder'], index(), [], None)
        self.assertIn(f'Head: {preamble_mod.UNKNOWN}', text)
        self.assertIn('(exists: (not known here))', text)
        self.assertNotIn('(312 lines)', text)

    def test_the_cap_holds_and_the_description_goes_first(self):
        p = product(conventions={'preamble_max_lines': 30})
        text = preamble_mod.build(p, ROWS['coder'], index(), [], REPO_FACTS)
        self.assertLessEqual(len(text.splitlines()), 30, text)
        self.assertIn('…truncated', text)
        self.assertNotIn('never inside the provider client itself', text)
        for identifier in ('T-0001', 'F-0001', 'task/T-0001', 'app/checkout/attempts.py'):
            self.assertIn(identifier, text)

    def test_an_impossible_cap_still_keeps_every_identifier(self):
        p = product(conventions={'preamble_max_lines': 1})
        text = preamble_mod.build(p, ROWS['coder'], index(), [], REPO_FACTS)
        for identifier in ('T-0001', 'F-0001', 'E-0001', 'task/T-0001',
                           'docs/plans/checkout-resilience.md', 'app/checkout/attempts.py'):
            self.assertIn(identifier, text)
        self.assertIn('Never push to `main`', text)   # the rules are not negotiable either

    def test_acceptance_goes_only_after_the_description_is_gone(self):
        full = preamble_mod.build(product(), ROWS['coder'], index(), [], REPO_FACTS)
        p = product(conventions={'preamble_max_lines': len(full.splitlines()) - 1})
        text = preamble_mod.build(p, ROWS['coder'], index(), [], REPO_FACTS)
        self.assertIn('### Acceptance', text)
        self.assertIn('test_attempt_row_per_try', text)

    def test_the_default_rules_are_six(self):
        rules = preamble_mod.rules_block(product(), 'trunk')
        self.assertEqual(len([l for l in rules.splitlines() if l.startswith('- ')]), 6)
        self.assertIn('never force-push', rules)
        self.assertIn('`trunk`', rules)

    def test_the_operator_can_replace_the_rules(self):
        p = product(conventions={'rules_tail': 'ONE RULE: push to {main} and nothing else.'})
        text = preamble_mod.build(p, ROWS['coder'], index(), [], REPO_FACTS)
        self.assertIn('ONE RULE: push to main and nothing else.', text)
        self.assertNotIn('never force-push', text)

    def test_an_unreadable_card_is_not_an_error(self):
        p = product(backlog_dir=os.path.join(RECORD, 'does-not-exist'))
        text = preamble_mod.build(p, ROWS['coder'], index(), [], REPO_FACTS)
        self.assertIn('T-0001 — Record every payment attempt', text)

    def test_the_review_path_follows_the_product_pattern(self):
        p = product(conventions={'review_pattern': 'reviews/{slug}/r{n}.md'})
        text = preamble_mod.build(p, ROWS['fixer'], index(), [], REPO_FACTS)
        self.assertIn('`reviews/t-0001/r1.md`', text)

    def test_a_reviewer_writes_the_next_round_and_a_fixer_answers_this_one(self):
        # F-0002 sits at spec-review r4: the adjudicator answers r4, a reviewer would write r5.
        answering = preamble_mod.build(product(), ROWS['adjudicate'], index(), [], REPO_FACTS)
        self.assertIn('Review file to answer: `docs/reviews/4-f-0002.md` (round 4)', answering)
        r = row('CODE → REVIEW', 'F-0002', 'review', 'spec/F-0002', 'another round',
                feature_id='F-0002')
        writing = preamble_mod.build(product(), r, index(), [], REPO_FACTS)
        self.assertIn('Review file to write: `docs/reviews/5-f-0002.md` (round 5)', writing)


class PlaceholderTest(unittest.TestCase):
    def context(self, kind='coder'):
        r = ROWS[kind]
        facts = preamble_mod.collect(product(), r, index(), [], REPO_FACTS)
        facts['kind'] = kind
        return build_mod.context(product(), r, kind, facts)

    def test_every_template_placeholder_is_a_context_key(self):
        ctx = self.context()
        for kind in build_mod.KINDS:
            with self.subTest(kind=kind):
                text = build_mod.load_template(kind)
                unknown = sorted({f for f in build_mod.placeholders(text) if f not in ctx})
                self.assertEqual(unknown, [], f'{kind}.md asks for {unknown}')

    def test_an_unknown_placeholder_is_an_error_not_an_empty_string(self):
        with self.assertRaises(build_mod.BriefError) as e:
            build_mod.render('the plan is {plan_pathh}', self.context())
        self.assertIn('plan_pathh', str(e.exception))

    def test_no_placeholder_survives_into_a_built_brief(self):
        for kind, r in sorted(ROWS.items()):
            with self.subTest(kind=kind):
                text = briefs.build(product(), r, index(), [], REPO_FACTS).text
                leftover = [f for f in build_mod.placeholders(text)]
                self.assertEqual(leftover, [], f'{kind}: {leftover}')


class KindModelGrantTest(unittest.TestCase):
    def test_the_feeder_kind_task_is_the_coder_template(self):
        self.assertEqual(build_mod.normalize_kind('task'), 'coder')

    def test_an_unknown_kind_refuses(self):
        with self.assertRaises(build_mod.BriefError):
            build_mod.normalize_kind('preflight')

    def test_the_default_labels(self):
        p = product()
        for kind in ('spec', 'plan', 'adjudicate', 'review'):
            self.assertEqual(build_mod.model_for(p, kind), 'heavy', kind)
        for kind in ('coder', 'fixer', 'rebase', 'close', 'fix-bug'):
            self.assertEqual(build_mod.model_for(p, kind), 'light', kind)

    def test_the_product_can_override_a_label(self):
        p = product(conventions={'models': {'review': 'light'}})
        self.assertEqual(build_mod.model_for(p, 'review'), 'light')
        self.assertEqual(build_mod.model_for(p, 'spec'), 'heavy')

    def test_id_ranges_are_needed_only_where_cards_are_minted(self):
        for kind in ('spec', 'plan', 'adjudicate', 'fix-bug'):
            self.assertTrue(build_mod.id_ranges_needed(kind), kind)
        for kind in ('coder', 'review', 'fixer', 'rebase', 'close'):
            self.assertFalse(build_mod.id_ranges_needed(kind), kind)

    def test_add_dirs_come_from_job_grants(self):
        brief = briefs.build(product(), ROWS['spec'], index(), [], REPO_FACTS)
        self.assertEqual(brief.add_dirs, [os.path.expanduser('~/.ASF/products')])
        self.assertTrue(brief.id_ranges_needed)

    def test_no_grants_is_an_empty_list_not_a_failure(self):
        p = Product('bare', {'backlog_dir': RECORD})
        self.assertEqual(build_mod.add_dirs_for(p), [])


class GenericTest(unittest.TestCase):
    def test_no_template_carries_a_forbidden_name(self):
        rx = forbidden_regex()
        for kind in build_mod.KINDS:
            with self.subTest(kind=kind):
                with open(build_mod.template_path(kind), encoding='utf-8') as f:
                    m = rx.search(f.read())
                self.assertIsNone(m, f'{kind}.md: {m.group(0) if m else ""}')

    def test_no_golden_brief_carries_a_forbidden_name(self):
        rx = forbidden_regex()
        for kind in build_mod.KINDS:
            with self.subTest(kind=kind):
                with open(os.path.join(GOLDEN, f'{kind}.md'), encoding='utf-8') as f:
                    m = rx.search(f.read())
                self.assertIsNone(m, f'{kind}: {m.group(0) if m else ""}')


class CliTest(unittest.TestCase):
    def test_register_adds_the_verb(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command')
        briefs.register(sub)
        args = parser.parse_args(['brief', '--product', 'sample', '--item', 'B-0001'])
        self.assertEqual(args.command, 'brief')
        self.assertEqual(args.item, 'B-0001')
        self.assertIs(args.func, build_mod.cmd_brief)


if __name__ == '__main__':
    unittest.main()
