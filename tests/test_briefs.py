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
import contextlib
import importlib
import io
import os
import dataclasses
import json
import re
import shutil
import tempfile
import unittest
from unittest import mock

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


def _groom_row():
    r = row('GROOM → ADJUDICATE', 'F-0002', 'groom', 'groom/2026-09-22',
            '2 groom questions no rule answers, oldest F-0002 (undecided 21d)')
    r.groom_file = 'groom/2026-09-22.md'
    r.answers_file = '~/.ASF/state/sample/groom/2026-09-22.answers'
    r.open_questions = (
        '- [ ] F-0002 Per-customer rate limits on the public API — no Stories → answer: ____',
        '- [ ] B-0001 Checkout returns 500 when the payment provider times out — duplicate of '
        'B-0002? → answer: ____',
    )
    return r


ROWS['delivery-plan'] = row('DELIVERY → PLAN', 'F-0003', 'delivery-plan', 'plan/F-0003',
                            '3 items in one delivery, no plan', feature_id='F-0003')
ROWS['delivery-code'] = row('DELIVERY → CODE', 'F-0003', 'delivery-code', 'worker/F-0003',
                            'delivery plan approved, footprint free', feature_id='F-0003')
ROWS['groom'] = _groom_row()
ROWS['reshape'] = row('RESHAPE → PLAN', 'T-0050', 'reshape', 'plan/T-0050',
                      'groom: split asf/feeder | asf/harvest', feature_id='F-0001')


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

    def test_a_groom_brief_names_the_groom_file_the_answers_file_and_unblock(self):
        text = briefs.build(product(), ROWS['groom'], index(), [], REPO_FACTS).text
        self.assertIn('groom/2026-09-22.md', text)
        self.assertIn('~/.ASF/state/sample/groom/2026-09-22.answers', text)
        self.assertIn('unblock <id>', text)
        self.assertIn('THE REPOSITORY IS NOT YOUR WORK', text)
        brief = briefs.build(product(), ROWS['groom'], index(), [], REPO_FACTS)
        self.assertEqual(brief.model, 'heavy')
        self.assertTrue(brief.id_ranges_needed)

    def test_a_review_brief_asks_for_the_check_table(self):
        text = briefs.build(product(), ROWS['review'], index(), [], REPO_FACTS).text
        self.assertIn('| check | result | evidence |', text)
        self.assertIn('verdict: approved', text)

    def test_a_fixer_always_pushes(self):
        text = briefs.build(product(), ROWS['fixer'], index(), [], REPO_FACTS).text
        self.assertIn('ALWAYS PUSH SOMETHING', text)

    def test_b0064_an_adjudicate_brief_puts_the_ruling_in_the_report_not_a_commit(self):
        # B-0054/B-0064: sessions wrote docs/reviews/…-ruling.md and minted D-7851, D-8200…
        text = briefs.build(product(), ROWS['adjudicate'], index(), [], REPO_FACTS).text
        self.assertNotIn('DECISION CARD BODY', text)
        self.assertIn('THE RULING GOES TO THE RECORD, AND THE FACTORY WRITES IT THERE', text)
        self.assertIn('you never write a decision id', text)
        self.assertIn('ruling: <adjudicate only', text)

    def test_the_tail_carries_the_typed_report_and_the_operator_marker(self):
        text = briefs.build(product(), ROWS['spec'], index(), [], REPO_FACTS).text
        self.assertIn('\nREPORT\nitem: F-0001\nkind: spec\n', text)
        self.assertIn('NEEDS OPERATOR: <what> — <the command or the answer needed>', text)

    def test_every_kind_ends_with_the_forbid_background_paragraph(self):
        # B-0052: a session that backgrounds the suite and returns before it ends leaves no
        # commit, no push — the closing paragraph is code-generated once, so no template can
        # omit it or drift from its wording.
        for kind, r in sorted(ROWS.items()):
            with self.subTest(kind=kind):
                text = briefs.build(product(), r, index(), [], REPO_FACTS).text
                self.assertIn(
                    'Run the gate in the foreground and wait for it. Your last act is '
                    '`git push`. Never start a', text)
                self.assertIn(
                    'background task you do not wait for. A result with uncommitted or '
                    'unpushed work is a failed', text)
                self.assertIn('session (B-0051) and comes back to you as a correction.', text)

    def test_every_kind_ends_with_the_closing_paragraph_and_a_pushed_line_in_the_report(self):
        # F-0087 (B-0024, B-0052): the closing paragraph and the REPORT's `pushed:` line are
        # generated once for every kind; the report parser reads `pushed: no` as a failure
        for kind, r in sorted(ROWS.items()):
            with self.subTest(kind=kind):
                text = briefs.build(product(), r, index(), [], REPO_FACTS).text
                tail = text[text.rindex('## The heartbeat'):]
                self.assertIn('Your last act is `git push`', tail)
                self.assertIn('`pushed:` line: `pushed: no` is read as that failure at once', tail)
                self.assertIn(f'pushed: yes <the sha origin/{r.branch} now points at> | rebased <sha> — '
                              f'the factory publishes | no — <why>', tail)
                self.assertTrue(text.rstrip().endswith('```'), text[-200:])

    def test_t8_every_kind_carries_the_three_ruling_fields_in_the_tail(self):
        # F-0090 §2.6: the tail is one string, so every kind's brief ends with it byte for byte
        for kind, r in sorted(ROWS.items()):
            with self.subTest(kind=kind):
                brief = briefs.build(product(), r, index(), [], REPO_FACTS)
                ctx = build_mod.context(
                    product(), r, kind, dict(preamble_mod.collect(product(), r, index(), [], REPO_FACTS),
                                             kind=kind))
                self.assertTrue(brief.text.endswith(build_mod.render(build_mod.TAIL, ctx)))
                tail = brief.text[brief.text.rindex('## The heartbeat'):]
                ruling = tail.index('\nruling: <adjudicate only')
                fields = [tail.index(f'\n{name}: <adjudicate only') for name in
                          ('blocked_on', 'writes', 'superseded_by')]
                self.assertEqual(fields, sorted(fields))
                self.assertLess(ruling, fields[0])
                self.assertLess(fields[-1], tail.index('\nNEEDS OPERATOR: <only if'))

    def test_t8_the_adjudicate_template_names_the_ruling_fields(self):
        text = build_mod.load_template('adjudicate')
        for name in ('blocked_on', 'writes', 'superseded_by'):
            self.assertIn(name, text)
        self.assertIn('A paragraph with no field behind it changes nothing', text)
        self.assertLess(text.index('THE RULING GOES TO THE RECORD'), text.index('`blocked_on: T-0025`'))
        ctx = PlaceholderTest().context('adjudicate')
        self.assertEqual(build_mod.placeholders(build_mod.render(text, ctx)), [])

    def test_every_kind_carries_the_branch_rule_rebase_never_merge_never_force(self):
        # B-0056: eight spec branches were merges of their own stale remote — the session could
        # not publish the rebase it was handed and was told never to force; the rule is generated
        # once, names the branch, and gives the session the report line that ends its turn
        for kind, r in sorted(ROWS.items()):
            with self.subTest(kind=kind):
                text = briefs.build(product(), r, index(), [], REPO_FACTS).text
                tail = text[text.rindex('## The heartbeat'):]
                self.assertIn(f'never merge `origin/{r.branch}` or `origin/main` into it, never force-push', tail)
                self.assertIn('`pushed: rebased <sha> — the factory publishes`', tail)
                self.assertIn('Never invent an id', tail)

    def test_the_push_wording_is_not_duplicated_per_template(self):
        # fixer.md and rebase.md each carried their own copy of "a session that ends without a
        # push is counted dead and relaunched on top of you" — now that the closing paragraph
        # says this once for every kind, the template copies are dead weight.
        for kind in ('fixer', 'rebase'):
            with self.subTest(kind=kind):
                text = build_mod.load_template(kind)
                self.assertNotIn('counted dead and relaunched on top of you', text)


class ReshapeBriefTest(unittest.TestCase):
    def test_reshape_kind_renders_with_id_range(self):
        text = briefs.build(product(), ROWS['reshape'], index(), [], REPO_FACTS).text
        self.assertIn('## Your job: reshape T-0050', text)
        self.assertIn('groom: split asf/feeder | asf/harvest', text)
        self.assertIn('BACKLOG_ID_RANGE', text)
        self.assertNotIn('{', text)

    def test_merged_task_coder_brief_names_absorbed(self):
        idx = index()
        idx['items']['T-0001'] = dict(idx['items']['T-0001'], merged=['T-0002'])
        text = briefs.build(product(), ROWS['coder'], idx, [], REPO_FACTS).text
        self.assertIn('Also delivers: T-0002', text)

    def test_reshape_is_a_known_kind(self):
        self.assertEqual(build_mod.normalize_kind('reshape'), 'reshape')


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

    def test_sized_tests_line(self):
        facts_ = dict(REPO_FACTS, files={'tests/test_a.py': 476})
        sections = {'acceptance': 'run `tests/test_a.py` and `tests/test_new.py`'}
        collected = preamble_mod.collect(product(), ROWS['coder'], index(), [], facts_)
        collected.update(sections=sections, tests=preamble_mod.named_tests(sections, {}, None))
        text = '\n'.join(preamble_mod.state_lines(product(), collected))
        self.assertIn('Tests named by the card: tests/test_a.py (476 lines); '
                      'tests/test_new.py (new)', text)

    def test_unknown_head_still_says_unknown(self):
        text = preamble_mod.build(product(), ROWS['coder'], index(), [], None)
        self.assertIn('Head: (not known here)', text)

    def test_wanted_paths(self):
        collected = preamble_mod.collect(product(), ROWS['review'], index(), [], REPO_FACTS)
        collected.update(tests=['tests/test_checkout.py::test_one', 'tests/test_checkout.py'],
                         writes=['app/checkout/attempts.py', 'app/**', 'tests/test_checkout.py'])
        self.assertEqual(preamble_mod.wanted_paths(collected),
                         [collected['spec_path'], collected['plan_path'], collected['review_path'],
                          'tests/test_checkout.py', 'app/checkout/attempts.py'])
        self.assertEqual(len(preamble_mod.wanted_paths(collected, limit=2)), 2)

    def test_unknown_count(self):
        bare = preamble_mod.build(product(), ROWS['coder'], index(), [], None)
        self.assertGreaterEqual(preamble_mod.unknown_count(bare), 2)
        full = preamble_mod.build(product(), ROWS['coder'], index(), [], REPO_FACTS)
        self.assertEqual(preamble_mod.unknown_count(full), 0)

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

    def test_the_document_dirs_come_from_the_object(self):
        p = product(conventions={'specs_dir': 'specs', 'plans_dir': 'plans',
                                  'reviews_dir': 'reviews'})
        text = preamble_mod.build(p, ROWS['coder'], index(), [], REPO_FACTS)
        self.assertIn('Specs live in `specs`, plans in `plans`, reviews in `reviews`.', text)

    def test_no_product_means_the_defaults(self):
        self.assertFalse(hasattr(preamble_mod, 'DEFAULT_SPECS_DIR'))
        conv = preamble_mod.conventions(None)
        self.assertEqual(conv.specs_dir, 'docs/specs')
        self.assertEqual(conv.plans_dir, 'docs/plans')
        self.assertEqual(conv.reviews_dir, 'docs/reviews')


class DeliveryBriefTest(unittest.TestCase):
    """F-0102: the two brief kinds of a delivery, and the digest that folds its members."""

    def brief(self, kind):
        return briefs.build(product(), ROWS[kind], index(), [], REPO_FACTS)

    def test_delivery_plan_brief_renders(self):
        b = self.brief('delivery-plan')
        self.assertIn('## Your job: write the delivery plan for F-0003 — 3 items in one branch',
                      b.text)
        self.assertIn('in this order — F-0003, B-0001, S-0001', b.text)
        self.assertEqual(b.text.count('#### '), 3)
        self.assertIn('### The items of this delivery', b.text)
        self.assertIn('test_pending_page_says_so', b.text)
        self.assertIn('test_timeout_is_pending_not_500', b.text)
        self.assertIn('test_timeout_retries_fallback', b.text)
        self.assertIn('Delivery: 3 items, in this order — F-0003, B-0001, S-0001', b.text)
        self.assertEqual(build_mod.placeholders(b.text), [])
        self.assertEqual(b.model, 'heavy')

    def test_delivery_code_brief_renders(self):
        b = self.brief('delivery-code')
        self.assertIn('ONE COMMIT PER ITEM', b.text)
        self.assertIn('app/checkout/pay.py, app/checkout/retry.py, tests/test_checkout.py', b.text)
        self.assertIn('docs/plans/f-0003.md', b.text)
        self.assertEqual(b.model, 'light')

    def test_neither_kind_needs_an_id_range(self):
        self.assertFalse(build_mod.id_ranges_needed('delivery-plan'))
        self.assertFalse(build_mod.id_ranges_needed('delivery-code'))

    def test_a_card_without_delivers_gets_no_members_section(self):
        self.assertNotIn('The items of this delivery', self.brief('coder').text)

    def test_a_members_acceptance_stales_the_delivery_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = os.path.join(tmp, 'record')
            shutil.copytree(RECORD, record)
            prod = product(backlog_dir=record)
            before = build_mod.card_digest(prod, 'F-0003', index())
            path = os.path.join(record, 'stories', 'S-0001.md')
            with open(path, encoding='utf-8') as f:
                text = f.read()
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text.replace('test_timeout_retries_fallback', 'test_something_else'))
            self.assertNotEqual(build_mod.card_digest(prod, 'F-0003', index()), before)


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


class CardDigestTests(unittest.TestCase):
    """F-0090 D4: the digest changes when what a brief states changes, and only then."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.record = os.path.join(self.tmp, 'record')
        shutil.copytree(RECORD, self.record)
        self.product = product(backlog_dir=self.record)
        self.index = index()

    def digest(self, item_id='T-0001'):
        return build_mod.card_digest(self.product, item_id, self.index)

    def card(self, name='tasks/T-0001.md'):
        return os.path.join(self.record, name)

    def edit_card(self, old, new, name='tasks/T-0001.md'):
        with open(self.card(name), encoding='utf-8') as f:
            text = f.read()
        self.assertIn(old, text)
        with open(self.card(name), 'w', encoding='utf-8') as f:
            f.write(text.replace(old, new, 1))

    def test_it_is_sixteen_hex_and_stable_across_two_calls(self):
        d = self.digest()
        self.assertRegex(d, r'^[0-9a-f]{16}$')
        self.assertEqual(d, self.digest())

    def test_every_brief_carries_the_digest_of_its_card(self):
        for kind, r in sorted(ROWS.items()):
            with self.subTest(kind=kind):
                brief = briefs.build(self.product, r, self.index, [], REPO_FACTS)
                self.assertEqual(brief.card_digest,
                                 build_mod.card_digest(self.product, r.item_id, self.index))
                self.assertRegex(brief.card_digest, r'^[0-9a-f]{16}$')

    def test_a_typed_field_that_changes_changes_the_digest(self):
        before = self.digest()
        for key, value in (('writes', ['app/other.py']), ('after', ['T-0002']),
                           ('title', 'Something else'), ('tests', ['tests.test_x']),
                           ('blockedBy', ['B-0001']), ('severity', 'S1')):
            with self.subTest(key=key):
                item = self.index['items']['T-0001']
                original = item.get(key)
                item[key] = value
                self.assertNotEqual(self.digest(), before)
                if original is None:
                    del item[key]
                else:
                    item[key] = original
                self.assertEqual(self.digest(), before)

    def test_the_description_text_changes_the_digest(self):
        before = self.digest()
        self.edit_card('One row per attempt', 'Two rows per attempt')
        self.assertNotEqual(self.digest(), before)

    def test_a_linked_document_changes_the_digest(self):
        before = self.digest('F-0001')
        self.index['items']['F-0001']['links']['plan'] = 'docs/plans/other.md'
        self.assertNotEqual(self.digest('F-0001'), before)

    def test_a_feature_gaining_a_story_changes_the_digest(self):
        before = self.digest('F-0001')
        self.index['items']['S-9999'] = {'id': 'S-9999', 'type': 'story', 'title': 'new',
                                         'parent': 'F-0001', 'folder': 'stories'}
        self.index['items']['F-0001']['children'].append('S-9999')
        self.assertNotEqual(self.digest('F-0001'), before)

    def test_a_history_line_does_not_change_the_digest(self):
        before = self.digest()
        self.edit_card('- 2026-01-01: created',
                       '- 2026-01-01: created\n- 2026-01-02: ruling — it waits for T-0002')
        self.assertEqual(self.digest(), before)

    def test_the_machine_block_does_not_change_the_digest(self):
        before = self.digest()
        self.edit_card('state: New', 'state: Active')
        self.edit_card('updated: 2026-01-01T08:00:00Z', 'updated: 2026-02-01T08:00:00Z')
        item = self.index['items']['T-0001']
        item.update(state='Active', evidence=['abc1234'], stage_since='2026-02-01T08:00:00Z',
                    updated='2026-02-01T08:00:00Z')
        self.assertEqual(self.digest(), before)

    def test_an_item_the_index_does_not_hold_still_digests(self):
        self.assertRegex(self.digest('T-9999'), r'^[0-9a-f]{16}$')


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

    def test_a_groom_brief_grants_the_groom_and_answers_directories(self):
        brief = briefs.build(product(), ROWS['groom'], index(), [], REPO_FACTS)
        self.assertIn('groom', brief.add_dirs)
        self.assertIn(os.path.expanduser('~/.ASF/state/sample/groom'), brief.add_dirs)

    def test_a_groom_brief_grants_the_intake_directory_its_inbox_lines_name(self):
        brief = briefs.build(product(), ROWS['groom'], index(), [], REPO_FACTS)
        self.assertIn('inbox', brief.add_dirs)


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

    def test_asf_brief_fills_the_facts(self):
        calls = []

        def stub(prod, r, idx, inflight=None):
            calls.append(r)
            return REPO_FACTS

        args = argparse.Namespace(product='sample', item='T-0001', kind=None, inflight=None,
                                  json=False)
        out = io.StringIO()
        with mock.patch.object(build_mod.env, 'load_product', return_value=product()), \
                mock.patch.object(build_mod.facts_mod, 'repo_facts', stub), \
                contextlib.redirect_stdout(out):
            self.assertEqual(build_mod.cmd_brief(args), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].item_id, 'T-0001')
        self.assertIn('Head: abc1234 record the provider outcome', out.getvalue())


if __name__ == '__main__':
    unittest.main()
