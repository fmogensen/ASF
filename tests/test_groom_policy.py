"""asf.groom.policy — the gate (T1), the open-question grammar it reads (PD4), the four policies
and the policy pass (T2-T4), suppression (T6), the approval bound (T7), the answers file (T10)
and the digest (T11).
"""
import argparse
import datetime
import json
import os
import shutil
import tempfile
import unittest

from asf.env import Product
from asf.groom import digest, groom, policy
from asf.record import frontmatter
from asf.record.core import canonicalize, compute_derived, load_items, today
from tests.test_groom import make_repo, run, write_item

UTC = datetime.timezone.utc


def _fresh_machine_lines():
    """``machine_lines`` for an item that must not also show up in ``undecided3``/``undecided14``
    (whose age is measured against the real wall clock, not ``--date``) — a bug or feature whose
    own section (``auto_bugs``, ``dupes``, ``blocked_closed``) is the only one meant to carry it."""
    now = datetime.datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    return ['state: New', f'stage_since: {now}', f'updated: {now}']


def product(approvals=None, groom=None):
    data = {}
    if approvals is not None:
        data['approvals'] = approvals
    if groom is not None:
        data['groom'] = groom
    return Product('sample', data)


class GateTests(unittest.TestCase):
    """T1: a product without ``approvals.groom: auto`` behaves exactly as today."""

    def test_auto_turns_the_gate_on(self):
        self.assertTrue(policy.groom_auto(product({'groom': 'auto'})))

    def test_auto_is_case_insensitive(self):
        self.assertTrue(policy.groom_auto(product({'groom': 'AUTO'})))

    def test_unset_is_off(self):
        self.assertFalse(policy.groom_auto(product({})))

    def test_no_approvals_at_all_is_off(self):
        self.assertFalse(policy.groom_auto(product()))

    def test_the_word_groom_is_off(self):
        self.assertFalse(policy.groom_auto(product({'groom': 'groom'})))

    def test_human_now_is_off(self):
        self.assertFalse(policy.groom_auto(product({'groom': 'human-now'})))

    def test_no_product_is_off(self):
        self.assertFalse(policy.groom_auto(None))

    def test_a_gate_off_cmd_groom_writes_the_same_file_as_before(self):
        """Task 1 wires only the gate; the policy pass itself is a later Task. This pins
        today's plain ``asf groom`` output as the baseline the gate must not disturb while it
        is off."""
        root = make_repo()
        try:
            write_item(root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
            write_item(root, 'F-0001', 'feature', 'Lonely feature', parent='E-0009',
                      typed_lines=['decided: true'])
            run(['index'], root)
            r = run(['groom'], root)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(root, 'groom', today() + '.md')) as f:
                text = f.read()
            self.assertIn('F-0001 Lonely feature — no Stories', text)
            self.assertNotIn('controller:', text)
            self.assertNotIn('by rule', r.stdout)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class OpenQuestionsTests(unittest.TestCase):
    """PD4: a ``- [ ] … → answer: ____`` line is open; nothing else is."""

    def test_open_lines_in_file_order(self):
        text = ("# Groom 2026-09-22\n\n## Undecided > 3 days\n\n"
               "- [ ] F-0001 Some idea — undecided 4d → answer: ____\n"
               "- [ ] F-0002 Another — undecided 5d → answer: ____\n")
        self.assertEqual(policy.open_questions(text),
                         [('F-0001', "- [ ] F-0001 Some idea — undecided 4d → answer: ____"),
                          ('F-0002', "- [ ] F-0002 Another — undecided 5d → answer: ____")])

    def test_an_answered_line_is_not_open(self):
        text = "- [ ] F-0001 Some idea — undecided 4d → answer: yes\n"
        self.assertEqual(policy.open_questions(text), [])

    def test_a_spoken_for_line_is_not_open(self):
        text = "- [x] F-0001 Some idea — no Stories → answer: (spoken for: CARD → SPEC)\n"
        self.assertEqual(policy.open_questions(text), [])

    def test_a_barred_line_is_not_open(self):
        text = "- [ ] E-0004 New goal — reads as a new Epic → answer: ____ (barred: approvals.new_epic)\n"
        self.assertEqual(policy.open_questions(text), [])


class ThresholdTests(unittest.TestCase):
    """D11's defaults, and a product yaml's own values overriding them."""

    def test_defaults(self):
        p = product()
        self.assertEqual(policy.duplicate_overlap(p), 0.95)
        self.assertEqual(policy.recurring_bug_count(p), 2)
        self.assertEqual(policy.adjudicate_attempts(p), 2)

    def test_overrides(self):
        p = product(groom={'duplicate_overlap': 0.8, 'recurring_bug_count': 5,
                           'adjudicate_attempts': 4})
        self.assertEqual(policy.duplicate_overlap(p), 0.8)
        self.assertEqual(policy.recurring_bug_count(p), 5)
        self.assertEqual(policy.adjudicate_attempts(p), 4)

    def test_an_invalid_override_falls_back_to_the_default(self):
        p = product(groom={'adjudicate_attempts': 0})
        self.assertEqual(policy.adjudicate_attempts(p), 2)

    def test_policy_on_default_is_on(self):
        self.assertTrue(policy.policy_on(product(), 'close_exact_duplicate'))

    def test_policy_off_skips_it(self):
        p = product(groom={'policies': {'close_exact_duplicate': 'off'}})
        self.assertFalse(policy.policy_on(p, 'close_exact_duplicate'))
        self.assertTrue(policy.policy_on(p, 'decide_recurring_bug'))


class PolicyTests(unittest.TestCase):
    """T2: one answering case and one nearest-miss case per policy, calling each function
    directly against real parsed records (no ``cmd_groom`` involved)."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _load(self):
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        return canonical, compute_derived(canonical)

    def _ctx(self, **kw):
        kw.setdefault('date', '2026-09-22')
        kw.setdefault('now', datetime.datetime(2026, 9, 22, tzinfo=UTC))
        return policy.Ctx(**kw)

    def test_policy_names_match_groom_POLICY_NAMES(self):
        self.assertEqual([name for name, _section, _fn in policy.POLICIES], list(groom.POLICY_NAMES))

    # -- unblock_on_closed -----------------------------------------------------------------

    def test_unblock_on_closed_answers_when_the_blocker_is_closed(self):
        write_item(self.root, 'F-0001', 'feature', 'Blocker', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Closed', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z'])
        write_item(self.root, 'F-0002', 'feature', 'Blocked', parent='E-0009',
                  typed_lines=['decided: true', 'blockedBy: [F-0001]'])
        canonical, derived = self._load()
        ans = policy.unblock_on_closed('F-0002', canonical['F-0002'], canonical, derived, self._ctx())
        self.assertEqual(ans, policy.Answer('unblock F-0001', 'unblock', 'F-0001',
                                            'blocker F-0001 is Closed'))

    def test_unblock_on_closed_nearest_miss_blocker_still_active(self):
        write_item(self.root, 'F-0001', 'feature', 'Blocker', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z'])
        write_item(self.root, 'F-0002', 'feature', 'Blocked', parent='E-0009',
                  typed_lines=['decided: true', 'blockedBy: [F-0001]'])
        canonical, derived = self._load()
        ans = policy.unblock_on_closed('F-0002', canonical['F-0002'], canonical, derived, self._ctx())
        self.assertIsNone(ans)

    # -- close_exact_duplicate --------------------------------------------------------------

    def test_close_exact_duplicate_answers_at_or_above_the_threshold(self):
        write_item(self.root, 'F-0001', 'feature', 'Free plan signup flow', parent='E-0009',
                  typed_lines=['decided: true'])
        write_item(self.root, 'F-0002', 'feature', 'Free plan signup flow redesign', parent='E-0009',
                  typed_lines=['decided: false'])
        canonical, derived = self._load()
        ans = policy.close_exact_duplicate('F-0002', canonical['F-0002'], canonical, derived,
                                           self._ctx(duplicate_overlap=0.8))
        self.assertEqual(ans.word, 'no')
        self.assertEqual(ans.field, 'removed')
        self.assertIn('duplicate of F-0001 (overlap 0.80)', ans.why)

    def test_close_exact_duplicate_nearest_miss_below_the_threshold(self):
        write_item(self.root, 'F-0001', 'feature', 'Free plan signup flow', parent='E-0009',
                  typed_lines=['decided: true'])
        write_item(self.root, 'F-0002', 'feature', 'Free plan signup flow redesign', parent='E-0009',
                  typed_lines=['decided: false'])
        canonical, derived = self._load()
        ans = policy.close_exact_duplicate('F-0002', canonical['F-0002'], canonical, derived,
                                           self._ctx(duplicate_overlap=0.81))
        self.assertIsNone(ans)

    # -- decide_recurring_bug ----------------------------------------------------------------

    def test_decide_recurring_bug_answers_at_the_threshold_count(self):
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0009',
                  typed_lines=['decided: false', 'signature: checkout-fail', 'count: 2'])
        canonical, derived = self._load()
        ans = policy.decide_recurring_bug('B-0001', canonical['B-0001'], canonical, derived,
                                          self._ctx(recurring_bug_count=2))
        self.assertEqual(ans, policy.Answer('yes', 'decided', True, 'auto-filed, seen 2 times'))

    def test_decide_recurring_bug_nearest_miss_count_one(self):
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0009',
                  typed_lines=['decided: false', 'signature: checkout-fail', 'count: 1'])
        canonical, derived = self._load()
        ans = policy.decide_recurring_bug('B-0001', canonical['B-0001'], canonical, derived,
                                          self._ctx(recurring_bug_count=2))
        self.assertIsNone(ans)

    # -- close_on_starvation ------------------------------------------------------------------

    def test_close_on_starvation_answers_past_the_limit(self):
        write_item(self.root, 'F-0003', 'feature', 'Stale idea', parent='E-0009',
                  typed_lines=['decided: false'],
                  machine_lines=['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z'])
        canonical, derived = self._load()
        ans = policy.close_on_starvation('F-0003', canonical['F-0003'], canonical, derived,
                                         self._ctx(now=datetime.datetime(2026, 9, 16, 1, 0, 0, tzinfo=UTC),
                                                   undecided_close='14d'))
        self.assertEqual(ans.word, 'no')
        self.assertEqual(ans.field, 'removed')
        self.assertIn('starvation policy', ans.why)

    def test_close_on_starvation_nearest_miss_one_hour_short(self):
        write_item(self.root, 'F-0003', 'feature', 'Stale idea', parent='E-0009',
                  typed_lines=['decided: false'],
                  machine_lines=['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z'])
        canonical, derived = self._load()
        ans = policy.close_on_starvation('F-0003', canonical['F-0003'], canonical, derived,
                                         self._ctx(now=datetime.datetime(2026, 9, 14, 23, 0, 0, tzinfo=UTC),
                                                   undecided_close='14d'))
        self.assertIsNone(ans)

    def test_close_on_starvation_never_answers_an_item_a_session_ever_ran_on(self):
        write_item(self.root, 'F-0003', 'feature', 'Stale idea', parent='E-0009',
                  typed_lines=['decided: false'],
                  machine_lines=['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z'])
        canonical, derived = self._load()
        ans = policy.close_on_starvation('F-0003', canonical['F-0003'], canonical, derived,
                                         self._ctx(now=datetime.datetime(2026, 9, 16, 1, 0, 0, tzinfo=UTC),
                                                   undecided_close='14d', ledger_items=frozenset({'F-0003'})))
        self.assertIsNone(ans)


class GroomAutoTestCase(unittest.TestCase):
    """A :func:`make_repo` root plus a temp ``ASF_HOME`` with a ``sample`` product yaml pointed
    at it (``backlog_dir``), for ``cmd_groom`` runs that need ``approvals.groom: auto`` — the
    gate only turns on for a real, loadable product (T1), so these go through the ``asf`` CLI
    subprocess (:func:`tests.test_groom.run`) rather than calling ``cmd_groom`` in-process."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], self.root)
        self.asf_home = tempfile.mkdtemp(prefix='groom_asf_home_')
        os.makedirs(os.path.join(self.asf_home, 'products'))
        self._orig_asf_home = os.environ.get('ASF_HOME')
        os.environ['ASF_HOME'] = self.asf_home

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.asf_home, ignore_errors=True)
        if self._orig_asf_home is None:
            os.environ.pop('ASF_HOME', None)
        else:
            os.environ['ASF_HOME'] = self._orig_asf_home

    def write_product(self, approvals=None, groom_cfg=None, stage_limits=None):
        lines = ['repo_slug: x/y', f'backlog_dir: {self.root}']
        if approvals:
            lines.append('approvals:')
            lines.extend(f'  {k}: {v}' for k, v in approvals.items())
        if groom_cfg:
            lines.append('groom:')
            for k, v in groom_cfg.items():
                lines.append(f'  {k}: {v}')
        if stage_limits:
            lines.append('stage_limits:')
            lines.extend(f'  {k}: {v}' for k, v in stage_limits.items())
        with open(os.path.join(self.asf_home, 'products', 'sample.yaml'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

    def run_groom(self, extra_args=()):
        return run(['groom', '--product', 'sample', *extra_args], self.root)


class PolicyPassTests(GroomAutoTestCase):
    """T3, T4: an answer becomes one typed field and one History line and nothing else, and a
    second run on the same date changes no card."""

    def test_answer_writes_one_field_one_history_line_and_nothing_else(self):
        self.write_product(approvals={'groom': 'auto'})
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0009',
                  typed_lines=['decided: false', 'signature: checkout-fail', 'count: 2'],
                  machine_lines=_fresh_machine_lines())
        write_item(self.root, 'F-0001', 'feature', 'Untouched feature', parent='E-0009',
                  typed_lines=['decided: true'])
        run(['index'], self.root)
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            before = f.read()

        r = self.run_groom(['--date', '2026-09-22'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('by rule 1', r.stdout)

        with open(os.path.join(self.root, 'bugs', 'B-0001.md')) as f:
            bug_text = f.read()
        meta, body = frontmatter.parse(bug_text, path='bugs/B-0001.md')
        self.assertEqual(meta['decided'], True)
        self.assertIn('2026-09-22 groom: decided → true (controller, decide_recurring_bug)', body)

        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            after = f.read()
        self.assertEqual(after, before)

    def test_second_run_same_date_changes_nothing(self):
        self.write_product(approvals={'groom': 'auto'})
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0009',
                  typed_lines=['decided: false', 'signature: checkout-fail', 'count: 2'],
                  machine_lines=_fresh_machine_lines())
        run(['index'], self.root)

        r1 = self.run_groom(['--date', '2026-09-22'])
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn('by rule 1', r1.stdout)
        with open(os.path.join(self.root, 'bugs', 'B-0001.md')) as f:
            snapshot = f.read()
        digest_path = os.path.join(self.root, 'groom', '2026-09-22-digest.md')
        with open(digest_path) as f:
            digest1 = f.read()
        self.assertIn('1 answered by rule · 0 ruled by the adjudicator · 0 spoken for · 0 for you',
                      digest1)
        self.assertIn('- B-0001 decided → true — decide_recurring_bug', digest1)

        r2 = self.run_groom(['--date', '2026-09-22'])
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn('by rule 0', r2.stdout)
        with open(os.path.join(self.root, 'bugs', 'B-0001.md')) as f:
            self.assertEqual(f.read(), snapshot)
        with open(digest_path) as f:
            digest2 = f.read()
        # The card is decided now, so it drops out of the day's own groom file — the digest still
        # derives the same answer from History (D9), just without the reason that file no longer
        # carries; the summary count and the rule line's id/field/value stay the same either way.
        self.assertIn('1 answered by rule · 0 ruled by the adjudicator · 0 spoken for · 0 for you',
                      digest2)
        self.assertIn('- B-0001 decided → true — decide_recurring_bug', digest2)


class ApprovalBoundTests(GroomAutoTestCase):
    """T7's policy half: with ``new_epic: human-now``, an inbox card inferred as an Epic is
    answered by no policy and its line is not among ``open_questions``."""

    def test_epic_inbox_card_is_barred_and_not_open(self):
        self.write_product(approvals={'groom': 'auto', 'new_epic': 'human-now'})
        with open(os.path.join(self.root, 'inbox', 'goal.md'), 'w', encoding='utf-8') as f:
            f.write('# A new goal for the year\ntype: epic\nSomething ambitious.\n')

        r = self.run_groom()
        self.assertEqual(r.returncode, 0, r.stderr)

        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertIn('(barred: approvals.new_epic)', text)
        self.assertNotIn('controller:', text)
        self.assertEqual(policy.open_questions(text), [])

        with open(os.path.join(self.root, 'groom', today() + '-digest.md')) as f:
            digest_text = f.read()
        for_you = digest_text[digest_text.index('## For you'):]
        self.assertIn('NEEDS OPERATOR:', for_you)
        self.assertIn('approvals.new_epic', for_you)

    def test_non_epic_question_is_unaffected_by_the_bound(self):
        """An undecided card is the control case: it holds no policy (§2.2's ``undecided3`` row)
        and no feeder row (``feature_rows`` only fires past ``decided: true``), so it stays a
        plain open question — unlike a decided, spec-less Feature, which T6's suppression pass
        would otherwise speak for as ``CARD → SPEC`` before the bound is ever reached."""
        self.write_product(approvals={'groom': 'auto', 'new_epic': 'human-now'})
        write_item(self.root, 'F-0001', 'feature', 'Undecided idea', parent='E-0009',
                  typed_lines=['decided: false'],
                  machine_lines=['state: New', 'stage_since: 2026-09-17T00:00:00Z',
                                 'updated: 2026-09-17T00:00:00Z'])
        run(['index'], self.root)

        r = self.run_groom()
        self.assertEqual(r.returncode, 0, r.stderr)

        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertNotIn('barred', text)
        self.assertIn('F-0001 Undecided idea — undecided', text)
        self.assertIn('→ answer: ____', text)


class SuppressionTests(GroomAutoTestCase):
    """T6: a question the feeder already has a row for is asked of nobody."""

    def test_a_feature_with_a_card_spec_row_is_spoken_for(self):
        self.write_product(approvals={'groom': 'auto'})
        write_item(self.root, 'F-0001', 'feature', 'Lonely feature', parent='E-0009',
                  typed_lines=['decided: true'])
        run(['index'], self.root)

        r = self.run_groom()
        self.assertEqual(r.returncode, 0, r.stderr)

        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertIn('- [x] F-0001 Lonely feature — no Stories → answer: '
                      '(spoken for: CARD → SPEC)', text)
        self.assertNotIn('controller:', text)
        self.assertEqual(policy.open_questions(text), [])

    def test_an_item_held_by_a_live_session_is_spoken_for(self):
        self.write_product(approvals={'groom': 'auto'})
        write_item(self.root, 'F-0003', 'feature', 'Stale idea', parent='E-0009',
                  typed_lines=['decided: false'],
                  machine_lines=['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z'])
        run(['index'], self.root)
        # written straight to the session ledger's path, not through ``pool``/``env.load_product``
        # — those read the module-level ``env.ASF_HOME``, set once at import and not refreshed by
        # ``os.environ['ASF_HOME']`` alone, so an in-process write must build the path itself.
        sessions_path = os.path.join(self.asf_home, 'state', 'sample', 'sessions.jsonl')
        os.makedirs(os.path.dirname(sessions_path), exist_ok=True)
        with open(sessions_path, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'job': 'spec-F-0003', 'item': 'F-0003', 'kind': 'spec',
                               'account': 'a', 'started': '2026-09-22T00:00:00Z'}) + '\n')

        r = self.run_groom()
        self.assertEqual(r.returncode, 0, r.stderr)

        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertIn('(spoken for: spec)', text)
        self.assertEqual(policy.open_questions(text), [])


class AnswersFileTests(unittest.TestCase):
    """T10: the adjudicate session's answers file is applied once by the next ``cmd_groom``,
    attributed to the job, and renamed so it is not applied twice."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Some idea', parent='E-0009',
                  typed_lines=['decided: false'])
        self.answers_dir = tempfile.mkdtemp(prefix='groom_answers_')
        self.answers_path = os.path.join(self.answers_dir, '2026-09-21.answers')
        with open(self.answers_path, 'w', encoding='utf-8') as f:
            f.write("- [ ] F-0001 Some idea — undecided 4d → answer: adjudicator: yes\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.answers_dir, ignore_errors=True)

    def _cmd_groom(self, answers_file):
        return groom.cmd_groom(argparse.Namespace(
            date='2026-09-22', apply=False, product=None, default_bug_epic=None,
            answers_file=answers_file, event=None), self.root)

    def test_applied_once_attributed_and_renamed(self):
        rc = self._cmd_groom(self.answers_path)
        self.assertEqual(rc, 0)

        self.assertFalse(os.path.exists(self.answers_path))
        self.assertTrue(os.path.exists(self.answers_path + '.done'))

        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            text = f.read()
        meta, body = frontmatter.parse(text, path='features/F-0001.md')
        self.assertEqual(meta['decided'], True)
        self.assertIn('2026-09-22 groom: decided → true (adjudicator, groom-2026-09-21)', body)

    def test_second_run_does_not_reapply(self):
        self._cmd_groom(self.answers_path)
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            snapshot = f.read()

        rc = self._cmd_groom(self.answers_path)  # renamed away by the first run — no-op
        self.assertEqual(rc, 0)
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            self.assertEqual(f.read(), snapshot)


class UnblockTests(unittest.TestCase):
    """T5: applying ``unblock <id>`` removes it from ``blockedBy``, one of two ids, the last id,
    twice, and an absent id — all through ``apply_groom_answers`` directly."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _apply(self, answer_line, date):
        prev = os.path.join(self.root, 'groom', '2026-09-20.md')
        with open(prev, 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Blocked on a Closed item\n\n" + answer_line + "\n")
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        applied = groom.apply_groom_answers(self.root, canonical, prev, date)
        with open(os.path.join(self.root, 'features', 'F-0002.md')) as f:
            text = f.read()
        return applied, text

    def test_removes_one_of_two_ids(self):
        write_item(self.root, 'F-0002', 'feature', 'Blocked one', parent='E-0009',
                  typed_lines=['decided: true', 'blockedBy: [F-0000, F-0001]'])
        applied, text = self._apply(
            "- [ ] F-0002 Blocked one — blockedBy F-0001, which is Closed → answer: unblock F-0001",
            '2026-09-21')
        self.assertEqual(applied, 1)
        meta, body = frontmatter.parse(text, path='features/F-0002.md')
        self.assertEqual(meta['blockedBy'], ['F-0000'])
        self.assertIn('2026-09-21 groom: blockedBy → F-0000 (operator)', body)

    def test_removes_the_last_id_and_the_field_is_gone(self):
        write_item(self.root, 'F-0002', 'feature', 'Blocked one', parent='E-0009',
                  typed_lines=['decided: true', 'blockedBy: [F-0001]'])
        applied, text = self._apply(
            "- [ ] F-0002 Blocked one — blockedBy F-0001, which is Closed → answer: unblock F-0001",
            '2026-09-21')
        self.assertEqual(applied, 1)
        meta, body = frontmatter.parse(text, path='features/F-0002.md')
        self.assertNotIn('blockedBy', meta)
        self.assertIn('2026-09-21 groom: blockedBy → (none) (operator)', body)

    def test_applying_twice_is_a_noop(self):
        write_item(self.root, 'F-0002', 'feature', 'Blocked one', parent='E-0009',
                  typed_lines=['decided: true', 'blockedBy: [F-0001]'])
        line = "- [ ] F-0002 Blocked one — blockedBy F-0001, which is Closed → answer: unblock F-0001"
        applied1, _text1 = self._apply(line, '2026-09-21')
        self.assertEqual(applied1, 1)
        applied2, text2 = self._apply(line, '2026-09-22')
        self.assertEqual(applied2, 0)
        meta, _body = frontmatter.parse(text2, path='features/F-0002.md')
        self.assertNotIn('blockedBy', meta)

    def test_an_absent_id_is_a_noop(self):
        write_item(self.root, 'F-0002', 'feature', 'Blocked one', parent='E-0009',
                  typed_lines=['decided: true', 'blockedBy: [F-0000]'])
        applied, text = self._apply(
            "- [ ] F-0002 Blocked one — blockedBy F-0000, which is Closed → answer: unblock F-9999",
            '2026-09-21')
        self.assertEqual(applied, 0)
        meta, _body = frontmatter.parse(text, path='features/F-0002.md')
        self.assertEqual(meta['blockedBy'], ['F-0000'])


class AttributionTests(unittest.TestCase):
    """PD2, PD3: ``controller: <policy> <word>`` attributes that policy, and ``adjudicator:
    <word>`` attributes the job — beside the existing bare ``controller: <word>`` form."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Some idea', parent='E-0009',
                  typed_lines=['decided: false'])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _apply(self, answer_line, date='2026-09-21', **kwargs):
        prev = os.path.join(self.root, 'groom', '2026-09-20.md')
        with open(prev, 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Undecided > 3 days\n\n" + answer_line + "\n")
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        applied = groom.apply_groom_answers(self.root, canonical, prev, date, **kwargs)
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            text = f.read()
        return applied, text

    def test_controller_with_a_known_policy_name_attributes_that_policy(self):
        applied, text = self._apply(
            "- [ ] F-0001 Some idea — undecided 4d → answer: controller: decide_recurring_bug yes")
        self.assertEqual(applied, 1)
        self.assertIn('(controller, decide_recurring_bug)', text)
        self.assertNotIn('starvation policy', text)

    def test_adjudicator_prefix_attributes_the_job(self):
        applied, text = self._apply(
            "- [ ] F-0001 Some idea — undecided 4d → answer: adjudicator: yes",
            adjudicator_job='groom-2026-09-21')
        self.assertEqual(applied, 1)
        self.assertIn('(adjudicator, groom-2026-09-21)', text)


def _body_with_history(*history_lines):
    return ("## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n## History\n"
           "- 2026-09-01: created\n" + '\n'.join(history_lines) +
           "\n\n## Children\n\n## Backlinks\n")


class DigestTests(unittest.TestCase):
    """T11: the digest is regenerated from History plus the day's own files, never state of its
    own — each test calls :func:`digest.render_digest` directly against a hand-built record and
    a hand-built groom/answers text, the same way ``PolicyTests`` calls a policy directly."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _load(self):
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        return canonical

    def test_answered_by_rule_section(self):
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0009',
                  typed_lines=['decided: true'],
                  body=_body_with_history(
                      '- 2026-09-22 groom: decided → true (controller, decide_recurring_bug)'))
        groom_text = ("# Groom 2026-09-22\n\n## Auto-filed Bugs not yet decided\n\n"
                     "- [ ] B-0001 Checkout fails — auto-filed, count 3 → answer: "
                     "controller: decide_recurring_bug yes\n")
        text = digest.render_digest(self.root, '2026-09-22', self._load(), groom_text, [], 0, 2)
        self.assertIn('1 answered by rule · 0 ruled by the adjudicator · 0 spoken for · 0 for you', text)
        self.assertIn('## Answered by rule', text)
        self.assertIn('- B-0001 decided → true — decide_recurring_bug: auto-filed, count 3', text)
        self.assertIn('generated by asf groom@', text)

    def test_ruled_by_adjudicator_section(self):
        write_item(self.root, 'F-0081', 'feature', 'Some idea', parent='E-0009',
                  typed_lines=['decided: true'],
                  body=_body_with_history(
                      '- 2026-09-22 groom: decided → true (adjudicator, groom-2026-09-22)'))
        answers_done = "- [ ] F-0081 Some idea — undecided 5d → answer: adjudicator: yes\n"
        text = digest.render_digest(self.root, '2026-09-22', self._load(), "# Groom 2026-09-22\n",
                                    [answers_done], 0, 2)
        self.assertIn('0 answered by rule · 1 ruled by the adjudicator · 0 spoken for · 0 for you', text)
        self.assertIn('## Ruled by the adjudicator (groom-2026-09-22)', text)
        self.assertIn('- F-0081 decided → true — undecided 5d', text)

    def test_spoken_for_holds_suppressed_and_capped_open_questions(self):
        groom_text = ("# Groom 2026-09-22\n\n## Features without Stories\n\n"
                     "- [x] F-0012 Some feature — no Stories → answer: (spoken for: CARD → SPEC)\n\n"
                     "## Undecided > 3 days\n\n"
                     "- [ ] F-0020 Another idea — undecided 4d → answer: ____\n")
        text = digest.render_digest(self.root, '2026-09-22', self._load(), groom_text, [], 0, 2)
        self.assertIn('0 answered by rule · 0 ruled by the adjudicator · 2 spoken for · 0 for you', text)
        self.assertIn('- F-0012 no Stories — (spoken for: CARD → SPEC)', text)
        self.assertIn('- F-0020 undecided 4d — (spoken for: GROOM → ADJUDICATE)', text)

    def test_for_you_holds_barred_over_cap_open_and_answers_file_needs_operator(self):
        groom_text = ("# Groom 2026-09-22\n\n## Inbox cards to decide\n\n"
                     "- [ ] E-0004 New goal — reads as a new Epic → answer: "
                     "____ (barred: approvals.new_epic)\n\n"
                     "## Undecided > 3 days\n\n"
                     "- [ ] F-0020 Another idea — undecided 4d → answer: ____\n")
        answers_done = "NEEDS OPERATOR: F-0030 rewrite the billing page — touches money\n"
        text = digest.render_digest(self.root, '2026-09-22', self._load(), groom_text,
                                    [answers_done], attempts=2, cap=2)
        self.assertIn('0 answered by rule · 0 ruled by the adjudicator · 0 spoken for · 3 for you', text)
        self.assertIn('NEEDS OPERATOR: E-0004 — reads as a new Epic; approvals.new_epic is not auto.', text)
        self.assertIn('NEEDS OPERATOR: F-0020 — undecided 4d', text)
        self.assertIn('NEEDS OPERATOR: F-0030 rewrite the billing page — touches money', text)

    def test_open_question_under_the_cap_stays_spoken_for(self):
        groom_text = ("# Groom 2026-09-22\n\n## Undecided > 3 days\n\n"
                     "- [ ] F-0020 Another idea — undecided 4d → answer: ____\n")
        text = digest.render_digest(self.root, '2026-09-22', self._load(), groom_text, [], attempts=1, cap=2)
        self.assertIn('GROOM → ADJUDICATE', text)
        self.assertNotIn('NEEDS OPERATOR: F-0020', text)

    def test_empty_sections_print_none(self):
        text = digest.render_digest(self.root, '2026-09-22', self._load(), "# Groom 2026-09-22\n",
                                    [], 0, 2)
        self.assertEqual(text.count('(none)'), 4)
        self.assertIn('0 answered by rule · 0 ruled by the adjudicator · 0 spoken for · 0 for you', text)

    def test_write_digest_writes_even_when_everything_is_empty(self):
        path = digest.write_digest(self.root, '2026-09-22', self._load(), "# Groom 2026-09-22\n",
                                   [], 0, 2)
        self.assertEqual(path, os.path.join(self.root, 'groom', '2026-09-22-digest.md'))
        self.assertTrue(os.path.isfile(path))


class DigestWiringTests(GroomAutoTestCase):
    """T11's ``cmd_groom`` half: the digest is written on every gated run, and only then."""

    def test_digest_written_on_a_gated_run(self):
        self.write_product(approvals={'groom': 'auto'})
        write_item(self.root, 'B-0001', 'bug', 'Checkout fails', parent='E-0009',
                  typed_lines=['decided: false', 'signature: checkout-fail', 'count: 2'],
                  machine_lines=_fresh_machine_lines())
        run(['index'], self.root)

        r = self.run_groom(['--date', '2026-09-22'])
        self.assertEqual(r.returncode, 0, r.stderr)

        digest_path = os.path.join(self.root, 'groom', '2026-09-22-digest.md')
        self.assertTrue(os.path.isfile(digest_path))
        with open(digest_path) as f:
            text = f.read()
        self.assertIn('1 answered by rule · 0 ruled by the adjudicator · 0 spoken for · 0 for you', text)
        self.assertIn('decide_recurring_bug', text)

    def test_no_digest_when_the_gate_is_off(self):
        self.write_product()
        run(['index'], self.root)
        r = self.run_groom(['--date', '2026-09-22'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.isfile(os.path.join(self.root, 'groom', '2026-09-22-digest.md')))


if __name__ == '__main__':
    unittest.main()
