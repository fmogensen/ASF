import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.record import frontmatter
from asf.record.core import canonicalize, compute_derived, load_items, today, tokenize
from asf.groom import groom
from asf.groom import digest
from asf.groom import inbox as inbox_mod
from asf.groom import policy
from asf.views import index_reader
from asf import hermetic

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
LIMITS_FIXTURE = os.path.join(HERE, 'fixtures', 'limits.json')
FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']

DEFAULT_BODY = (
    "## Description\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n"
)
FOLDER_OF = {'epic': 'epics', 'feature': 'features', 'story': 'stories', 'task': 'tasks',
             'bug': 'bugs', 'decision': 'decisions', 'rule': 'rules'}


def make_repo():
    root = tempfile.mkdtemp(prefix='groom_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    os.makedirs(os.path.join(root, 'inbox', 'done'))
    os.makedirs(os.path.join(root, 'groom'))
    os.makedirs(os.path.join(root, 'tools'))
    shutil.copy(LIMITS_FIXTURE, os.path.join(root, 'tools', 'limits.json'))
    return root


def write_item(root, id_, type_, title, parent=None, typed_lines=(), machine_lines=None, body=None):
    if machine_lines is None:
        machine_lines = ['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                         'updated: 2026-09-01T00:00:00Z']
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}"]
    if parent:
        lines.append(f"parent: {parent}")
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.extend(machine_lines)
    header = '\n'.join(lines)
    path = os.path.join(root, FOLDER_OF[type_], f"{id_}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f"---\n{header}\n---\n{body if body is not None else DEFAULT_BODY}")
    return path


def run(args, cwd):
    env = hermetic.build()
    env.pop('BACKLOG_ID_RANGE', None)  # a job's range must not leak into the fixture's own mints (B-0012)
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.run([sys.executable, '-m', 'asf.cli'] + args, cwd=cwd, env=env,
                           capture_output=True, text=True)


class InboxParsingTests(unittest.TestCase):
    def test_title_strips_leading_hash(self):
        card = inbox_mod.parse_inbox_file("# Something is off\nmore text\n")
        self.assertEqual(card.title, 'Something is off')
        self.assertIsNone(card.headers.get('type'))
        self.assertIsNone(card.headers.get('parent'))
        self.assertEqual(card.description, 'more text')

    def test_explicit_type_and_parent_lines(self):
        text = "New idea\ntype: bug\nparent: F-0042\nIt is broken.\n"
        card = inbox_mod.parse_inbox_file(text)
        self.assertEqual(card.title, 'New idea')
        self.assertEqual(card.headers.get('type'), 'bug')
        self.assertEqual(card.headers.get('parent'), 'F-0042')
        self.assertEqual(card.description, 'It is broken.')

    def test_header_lines_after_a_blank_line(self):
        text = "New idea\n\nsignature: checkout-pay\n\nIt is broken.\n"
        card = inbox_mod.parse_inbox_file(text)
        self.assertEqual(card.title, 'New idea')
        self.assertEqual(card.headers.get('signature'), 'checkout-pay')
        self.assertEqual(card.description, 'It is broken.')

    def test_infer_parent_epic_by_shared_words(self):
        root = make_repo()
        try:
            write_item(root, 'E-0001', 'epic', 'Billing and plans', typed_lines=['decided: true'])
            write_item(root, 'E-0002', 'epic', 'Onboarding flow', typed_lines=['decided: true'])
            by_id, _ = load_items(root)
            canonical, _ = canonicalize(by_id)
            hit = inbox_mod.infer_parent_epic(canonical, tokenize('New billing plan tiers'))
            self.assertEqual(hit, 'E-0001')
            miss = inbox_mod.infer_parent_epic(canonical, tokenize('Completely unrelated words'))
            self.assertIsNone(miss)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class InboxAnswerGrammarTests(unittest.TestCase):
    """T-0298: a refused `inbox:` answer is a line, not a silence."""

    def test_feature_is_unchanged(self):
        self.assertEqual(inbox_mod.parse_answer('feature'), ({'type': 'feature'}, None))

    def test_close_is_unchanged(self):
        self.assertEqual(inbox_mod.parse_answer('close'), ('close', None))

    def test_a_trailing_why_names_the_clause_and_the_missing_why(self):
        parsed, reason = inbox_mod.parse_answer(
            'parent E-0001 — because the card changes the feeder')
        self.assertIsNone(parsed)
        self.assertIn('parent E-0001 — because the card changes the feeder', reason)
        self.assertIn('an answer carries no why', reason)

    def test_the_ascii_dash_is_the_same_mistake(self):
        parsed, reason = inbox_mod.parse_answer(
            'parent E-0001 - because the card changes the feeder')
        self.assertIsNone(parsed)
        self.assertIn('an answer carries no why', reason)

    def test_a_clause_outside_the_grammar_names_the_five_forms(self):
        parsed, reason = inbox_mod.parse_answer('nonsense')
        self.assertIsNone(parsed)
        self.assertNotIn('an answer carries no why', reason)
        self.assertIn('"nonsense" is not a clause', reason)
        self.assertIn('close | feature | bug <signature> | parent <id> | S1|S2|S3', reason)

    def test_a_bad_inbox_answer_and_a_good_one_on_one_page(self):
        root = make_repo()
        try:
            for name, title in (('billing-tiers.md', 'Billing tiers'),
                                 ('onboarding.md', 'Onboarding flow')):
                with open(os.path.join(root, 'inbox', name), 'w', encoding='utf-8') as f:
                    f.write(f"# {title}\n\n## Question\nFeature or bug?\n")
            groom_path = os.path.join(root, 'groom', '2026-09-20.md')
            with open(groom_path, 'w', encoding='utf-8') as f:
                f.write(
                    "# Groom 2026-09-20\n\n## Inbox cards with a question\n"
                    "- [ ] inbox:billing-tiers.md Billing tiers — Feature or bug? "
                    "→ answer: parent E-0001 — because the card changes the feeder\n"
                    "- [ ] inbox:onboarding.md Onboarding flow — Feature or bug? "
                    "→ answer: feature\n"
                )
            events = []
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                applied = groom.apply_groom_answers(
                    root, {}, groom_path, '2026-09-21',
                    event=lambda kind, **kw: events.append((kind, kw)))
            self.assertEqual(applied, 1)
            self.assertEqual([e[1]['item'] for e in events], ['inbox:onboarding.md'])

            refusal_lines = [l for l in out.getvalue().split('\n')
                             if l.startswith('groom: answer not applied')]
            self.assertEqual(len(refusal_lines), 1)
            self.assertIn('inbox:billing-tiers.md', refusal_lines[0])
            self.assertIn('an answer carries no why', refusal_lines[0])

            with open(os.path.join(root, 'inbox', 'billing-tiers.md'), encoding='utf-8') as f:
                self.assertIn('## Question', f.read())  # refused: changes nothing
            with open(os.path.join(root, 'inbox', 'onboarding.md'), encoding='utf-8') as f:
                onboarding_text = f.read()
            self.assertNotIn('## Question', onboarding_text)
            self.assertIn('type: feature', onboarding_text)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_an_unfilled_inbox_slot_is_not_an_answer(self):
        root = make_repo()
        try:
            for name, title in (('bare.md', 'Bare'), ('settled.md', 'Settled'),
                                 ('barred.md', 'Barred')):
                with open(os.path.join(root, 'inbox', name), 'w', encoding='utf-8') as f:
                    f.write(f"# {title}\n\n## Question\nFeature or bug?\n")
            groom_path = os.path.join(root, 'groom', '2026-09-20.md')
            with open(groom_path, 'w', encoding='utf-8') as f:
                f.write(
                    "# Groom 2026-09-20\n\n## Inbox cards with a question\n"
                    "- [ ] inbox:bare.md Bare — Feature or bug? → answer: ____\n"
                    "- [ ] inbox:settled.md Settled — Feature or bug? "
                    "→ answer: ____ (settled: the card left the inbox)\n"
                    "- [ ] inbox:barred.md Barred — Feature or bug? "
                    "→ answer: ____ (barred: approvals.groom)\n"
                )
            events = []
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                applied = groom.apply_groom_answers(
                    root, {}, groom_path, '2026-09-21',
                    event=lambda kind, **kw: events.append((kind, kw)))
            self.assertEqual(applied, 0)
            self.assertEqual(events, [])
            self.assertEqual(out.getvalue(), '')  # unfilled, not refused: no line at all

            for name in ('bare.md', 'settled.md', 'barred.md'):
                with open(os.path.join(root, 'inbox', name), encoding='utf-8') as f:
                    self.assertIn('## Question', f.read())  # untouched
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_an_unfilled_inbox_slot_beside_a_real_answer_still_applies_the_real_one(self):
        root = make_repo()
        try:
            for name, title in (('barred.md', 'Barred'), ('onboarding.md', 'Onboarding flow')):
                with open(os.path.join(root, 'inbox', name), 'w', encoding='utf-8') as f:
                    f.write(f"# {title}\n\n## Question\nFeature or bug?\n")
            groom_path = os.path.join(root, 'groom', '2026-09-20.md')
            with open(groom_path, 'w', encoding='utf-8') as f:
                f.write(
                    "# Groom 2026-09-20\n\n## Inbox cards with a question\n"
                    "- [ ] inbox:barred.md Barred — Feature or bug? "
                    "→ answer: ____ (barred: approvals.groom)\n"
                    "- [ ] inbox:onboarding.md Onboarding flow — Feature or bug? "
                    "→ answer: feature\n"
                )
            events = []
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                applied = groom.apply_groom_answers(
                    root, {}, groom_path, '2026-09-21',
                    event=lambda kind, **kw: events.append((kind, kw)))
            self.assertEqual(applied, 1)
            self.assertEqual([e[1]['item'] for e in events], ['inbox:onboarding.md'])
            self.assertEqual(out.getvalue(), '')

            with open(os.path.join(root, 'inbox', 'barred.md'), encoding='utf-8') as f:
                self.assertIn('## Question', f.read())  # unanswered: untouched
            with open(os.path.join(root, 'inbox', 'onboarding.md'), encoding='utf-8') as f:
                self.assertNotIn('## Question', f.read())  # answered: applied
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_second_apply_of_an_already_closed_card_stays_silent(self):
        root = make_repo()
        try:
            with open(os.path.join(root, 'inbox', 'thing.md'), 'w', encoding='utf-8') as f:
                f.write("# Thing\n\n## Question\nFeature or bug?\n")
            groom_path = os.path.join(root, 'groom', '2026-09-20.md')
            with open(groom_path, 'w', encoding='utf-8') as f:
                f.write(
                    "# Groom 2026-09-20\n\n## Inbox cards with a question\n"
                    "- [ ] inbox:thing.md Thing — Feature or bug? → answer: close\n"
                )
            with contextlib.redirect_stdout(io.StringIO()):
                applied = groom.apply_groom_answers(root, {}, groom_path, '2026-09-21')
            self.assertEqual(applied, 1)

            # a second `--apply` over the same prev_path: `close` already moved the card to
            # `done/`, so `apply_answer` returns `(False, None)` — not a grammar refusal.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                applied_again = groom.apply_groom_answers(root, {}, groom_path, '2026-09-21')
            self.assertEqual(applied_again, 0)
            self.assertEqual(out.getvalue(), '')
        finally:
            shutil.rmtree(root, ignore_errors=True)


class AnswerParsingTests(unittest.TestCase):
    def test_yes(self):
        self.assertEqual(groom._parse_answer('yes'), ('decided', True))

    def test_no_and_close(self):
        self.assertEqual(groom._parse_answer('no'), ('removed', None))
        self.assertEqual(groom._parse_answer('close'), ('removed', None))

    def test_rank(self):
        self.assertEqual(groom._parse_answer('rank 2'), ('rank', 2))

    def test_parent(self):
        self.assertEqual(groom._parse_answer('parent F-0010'), ('parent', 'F-0010'))

    def test_severity(self):
        self.assertEqual(groom._parse_answer('S1'), ('severity', 'S1'))

    def test_unblock(self):
        self.assertEqual(groom._parse_answer('unblock S-0140'), ('unblock', 'S-0140'))

    def test_landed_and_open(self):
        self.assertEqual(groom._parse_answer('landed 9f2ac41'), ('landed', '9f2ac41'))
        self.assertEqual(groom._parse_answer('landed 9F2AC41'), ('landed', '9f2ac41'))
        self.assertEqual(groom._parse_answer('open'), ('reconciled', None))
        self.assertEqual(groom._parse_answer('landed'), (None, None))
        self.assertEqual(groom._parse_answer('landed 9f2a'), (None, None))
        self.assertEqual(groom._parse_answer('landed nothex1'), (None, None))

    def test_blank_and_placeholder(self):
        self.assertEqual(groom._parse_answer(''), (None, None))
        self.assertEqual(groom._parse_answer('____'), (None, None))

    def test_unrecognized(self):
        self.assertEqual(groom._parse_answer('maybe later'), (None, None))


class NearDuplicateTests(unittest.TestCase):
    def test_pair_over_threshold_is_reported_once(self):
        root = make_repo()
        try:
            write_item(root, 'F-0001', 'feature', 'Free plan signup flow', typed_lines=['decided: true'])
            write_item(root, 'F-0002', 'feature', 'Free plan signup flow redesign',
                      typed_lines=['decided: true'])
            write_item(root, 'F-0003', 'feature', 'Totally different thing', typed_lines=['decided: true'])
            by_id, _ = load_items(root)
            canonical, _ = canonicalize(by_id)
            lines = groom.groom_near_duplicates(canonical)
            self.assertEqual(len(lines), 1, lines)
            self.assertIn('F-0002', lines[0])
            self.assertIn('near-duplicate of F-0001', lines[0])
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ConflictSectionTests(unittest.TestCase):
    """F-0046 Task 5: the groom carries a rule/decision conflict pair through to the rendered
    file, once, between the section it sits beside (`## Near-duplicate titles`) and the one it
    precedes (`## Undecided > 14 days`)."""

    def setUp(self):
        self.root = make_repo()
        write_item(
            self.root, 'R-0007', 'rule', 'Harvest goes through rebase_and_resolve',
            typed_lines=['scope: harvest', 'enforced: true', 'reason: keep the trunk clean',
                        'check: check.sh'],
            machine_lines=['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                          'updated: 2026-09-01T00:00:00Z'],
            body=("## Statement\nHarvest never touches the trunk directly; it works through "
                  "`harvest.rebase_and_resolve`.\n\n## Children\n\n## Backlinks\n"))
        write_item(
            self.root, 'D-0042', 'decision', 'A worker branch fast-forwards the trunk itself',
            typed_lines=['decided: true', 'decided_by: ops', 'date: 2026-09-15',
                        'scope: harvest'],
            machine_lines=['state: New', 'stage_since: 2026-09-15T00:00:00Z',
                          'updated: 2026-09-15T00:00:00Z'],
            body=("## Statement\nA worker branch may fast-forward the trunk itself, bypassing "
                  "`harvest.rebase_and_resolve` for a clean history.\n\n## Children\n\n"
                  "## Backlinks\n"))
        run(['index'], self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _groom(self):
        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.root, 'groom', today() + '.md'), encoding='utf-8') as f:
            return f.read(), r

    def test_section_sits_between_dupes_and_undecided14_with_the_full_grammar(self):
        text, r = self._groom()
        dupes_at = text.index('## Near-duplicate titles')
        conflicts_at = text.index('## Conflicting rules and decisions')
        undecided14_at = text.index('## Undecided > 14 days')
        self.assertTrue(dupes_at < conflicts_at < undecided14_at, text)
        block = text[conflicts_at:undecided14_at]

        head = ("- [ ] D-0042 conflicts R-0007 — scope `harvest`, shares "
                "`harvest.rebase_and_resolve`; keep A · keep B · both · merge "
                "(merge keeps D-0042) → answer: ____")
        a_line = ('      A D-0042 "A worker branch may fast-forward the trunk itself, bypassing '
                  '`harvest.rebase_and_resolve` for a clean history." — 2026-09-15')
        b_line = ('      B R-0007 "Harvest never touches the trunk directly; it works through '
                  '`harvest.rebase_and_resolve`." — rules/R-0007.md')
        prec_line = '      precedence: D-0042 (newer, 2026-09-15)'
        self.assertIn(head, block)
        self.assertIn(a_line, block)
        self.assertIn(b_line, block)
        self.assertIn(prec_line, block)

        self.assertIn('Conflicting rules and decisions: 1', r.stdout)

    def test_policy_open_questions_counts_the_pair_once_under_its_lead_id(self):
        text, _r = self._groom()
        ids = [iid for iid, _line in policy.open_questions(text)]
        self.assertEqual(ids.count('D-0042'), 1)
        self.assertNotIn('R-0007', ids)

    def test_digest_answer_line_matches_and_why_is_the_text_after_the_last_dash(self):
        text, _r = self._groom()
        matched = [m for m in (digest.ANSWER_LINE_RE.match(l) for l in text.splitlines()) if m]
        conflict_matches = [m for m in matched if m.group('id') == 'D-0042']
        self.assertEqual(len(conflict_matches), 1)
        m = conflict_matches[0]
        self.assertEqual(m.group('answer'), '____')
        self.assertEqual(digest._why(m.group('rest')),
                         'scope `harvest`, shares `harvest.rebase_and_resolve`; '
                         'keep A · keep B · both · merge (merge keeps D-0042)')

    def test_no_conflicting_pair_renders_none(self):
        root = make_repo()
        try:
            write_item(root, 'F-0001', 'feature', 'Something', typed_lines=['decided: true'])
            run(['index'], root)
            r = run(['groom'], root)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(root, 'groom', today() + '.md'), encoding='utf-8') as f:
                text = f.read()
            conflicts_at = text.index('## Conflicting rules and decisions')
            undecided14_at = text.index('## Undecided > 14 days')
            self.assertEqual(text[conflicts_at:undecided14_at].strip(),
                             '## Conflicting rules and decisions\n\n(none)')
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ConflictDedupeTests(unittest.TestCase):
    """F-0046 Task 5: ``merge_groom_text`` learns the conflicts block's own dedupe token — the
    pair, not the lead id — so a same-tick or every-tick re-run never doubles a block and never
    collapses two distinct pairs that happen to share a lead id."""

    @staticmethod
    def _block(a, b, survivor=None):
        survivor = survivor or a
        head = (f"- [ ] {a} conflicts {b} — reason; keep A · keep B · both · merge "
                f"(merge keeps {survivor}) → answer: ____")
        return '\n'.join([head, f'      A {a} "stmt" — src', f'      B {b} "stmt" — src',
                          f'      precedence: {survivor} (why)'])

    def test_incremental_merge_adds_once_then_nothing(self):
        existing = "# Groom d\n\n## Conflicting rules and decisions\n\n(none)\n"
        block = self._block('D-0001', 'R-0001')
        text, added = groom.merge_groom_text(existing, {'conflicts': [block]})
        self.assertEqual(added, 1)
        self.assertIn(block, text)
        text2, added2 = groom.merge_groom_text(text, {'conflicts': [block]})
        self.assertEqual(added2, 0)
        self.assertEqual(text2, text)

    def test_one_lead_id_two_partners_yields_two_blocks_not_one(self):
        # the naive lead-id token (`_LINE_TOKEN_RE`'s first `\S+`) is the same, `R-0001`, on both
        # lines — the collapse `_CONFLICT_TOKEN_RE`/`line_token` must not let happen.
        existing = "# Groom d\n\n## Conflicting rules and decisions\n\n(none)\n"
        b1 = self._block('R-0001', 'D-0001')
        b2 = self._block('R-0001', 'D-0002')
        text, added = groom.merge_groom_text(existing, {'conflicts': [b1, b2]})
        self.assertEqual(added, 2)
        self.assertIn(b1, text)
        self.assertIn(b2, text)

    def test_answered_conflicts_line_is_untouched_through_incremental_merge(self):
        answered_head = ("- [x] D-0001 conflicts R-0001 — reason; keep A · keep B · both · merge "
                         "(merge keeps D-0001) → answer: keep A")
        existing = ("# Groom d\n\n## Conflicting rules and decisions\n\n" + answered_head + "\n"
                    '      A D-0001 "stmt" — src\n      B R-0001 "stmt" — src\n'
                    '      precedence: D-0001 (why)\n')
        text, added = groom.merge_groom_text(existing, {'conflicts': [self._block('D-0001', 'R-0001')]})
        self.assertEqual(added, 0)
        self.assertIn(answered_head, text)


class ConflictApplyTests(unittest.TestCase):
    """F-0046 Task 6: the four answers to a conflicts line, applied through `asf groom --apply`.
    `R-0007 conflicts D-0042` by scope, with `D-0042` the precedence winner (newer)."""

    def setUp(self):
        self.root = make_repo()
        write_item(
            self.root, 'R-0007', 'rule', 'Harvest goes through rebase_and_resolve',
            typed_lines=['scope: harvest', 'enforced: true', 'reason: keep the trunk clean',
                        'check: check.sh'],
            machine_lines=['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                          'updated: 2026-09-01T00:00:00Z'],
            body=("## Statement\nHarvest never touches the trunk directly; it works through "
                  "`harvest.rebase_and_resolve`.\n\n## Source\nrules/legacy-harvest-note.md\n\n"
                  "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n"))
        write_item(
            self.root, 'D-0042', 'decision', 'A worker branch fast-forwards the trunk itself',
            typed_lines=['decided: true', 'decided_by: ops', 'date: 2026-09-15',
                        'scope: harvest'],
            machine_lines=['state: New', 'stage_since: 2026-09-15T00:00:00Z',
                          'updated: 2026-09-15T00:00:00Z'],
            body=("## Statement\nA worker branch may fast-forward the trunk itself, bypassing "
                  "`harvest.rebase_and_resolve` for a clean history.\n\n## Source\n\n"
                  "## History\n- 2026-09-15: created\n\n## Children\n\n## Backlinks\n"))
        run(['index'], self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _question(answer):
        return ("- [ ] D-0042 conflicts R-0007 — scope `harvest`, shares "
                "`harvest.rebase_and_resolve`; keep A · keep B · both · merge "
                f"(merge keeps D-0042) → answer: {answer}\n"
                '      A D-0042 "stmt" — src\n      B R-0007 "stmt" — src\n'
                '      precedence: D-0042 (newer, 2026-09-15)\n')

    def _write_yesterday(self, answer):
        with open(os.path.join(self.root, 'groom', '2026-09-20.md'), 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Conflicting rules and decisions\n\n" +
                    self._question(answer))

    def _apply(self, date='2026-09-21'):
        return run(['groom', '--date', date, '--apply'], self.root)

    def _text(self, folder, name):
        with open(os.path.join(self.root, folder, f'{name}.md'), encoding='utf-8') as f:
            return f.read()

    def test_keep_a_writes_the_pair_check_demands(self):
        self._write_yesterday('keep A')
        r = self._apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('applied 2', r.stdout)

        loser = self._text('rules', 'R-0007')
        meta, body = frontmatter.parse(loser, path='rules/R-0007.md')
        self.assertEqual(meta['superseded_by'], 'D-0042')
        self.assertIn('- 2026-09-21 groom: superseded_by → D-0042 (operator)', body)

        winner = self._text('decisions', 'D-0042')
        meta, body = frontmatter.parse(winner, path='decisions/D-0042.md')
        self.assertEqual(meta['supersedes'], ['R-0007'])
        self.assertIn('- 2026-09-21 groom: supersedes → R-0007 (operator)', body)
        # the pair asf check demands (Task 2): each field names the other card, in both
        # directions, with no dangling half
        self.assertEqual(run(['index'], self.root).returncode, 0)

    def test_keep_b_is_the_mirror(self):
        self._write_yesterday('keep B')
        r = self._apply()
        self.assertEqual(r.returncode, 0, r.stderr)

        winner = self._text('rules', 'R-0007')
        meta, _body = frontmatter.parse(winner, path='rules/R-0007.md')
        self.assertEqual(meta['supersedes'], ['D-0042'])

        loser = self._text('decisions', 'D-0042')
        meta, body = frontmatter.parse(loser, path='decisions/D-0042.md')
        self.assertEqual(meta['superseded_by'], 'R-0007')
        self.assertIn('- 2026-09-21 groom: superseded_by → R-0007 (operator)', body)

    def test_both_declines_and_carries_no_supersession_field(self):
        self._write_yesterday('both')
        r = self._apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('applied 2', r.stdout)

        for folder, name in (('rules', 'R-0007'), ('decisions', 'D-0042')):
            meta, body = frontmatter.parse(self._text(folder, name), path=f'{folder}/{name}.md')
            self.assertEqual(meta['conflict_declined'], ['D-0042+R-0007'])
            self.assertNotIn('superseded_by', meta)
            self.assertNotIn('supersedes', meta)
            self.assertIn('- 2026-09-21 groom: conflict declined D-0042+R-0007 (operator)', body)

        # the next groom no longer proposes an already-declined pair
        r2 = run(['groom', '--date', '2026-09-22'], self.root)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        with open(os.path.join(self.root, 'groom', '2026-09-22.md'), encoding='utf-8') as f:
            text = f.read()
        conflicts_at = text.index('## Conflicting rules and decisions')
        undecided14_at = text.index('## Undecided > 14 days')
        self.assertEqual(text[conflicts_at:undecided14_at].strip(),
                         '## Conflicting rules and decisions\n\n(none)')

    def test_merge_copies_source_and_history_onto_the_survivor(self):
        self._write_yesterday('merge')
        r = self._apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('applied 2', r.stdout)

        winner = self._text('decisions', 'D-0042')
        meta, body = frontmatter.parse(winner, path='decisions/D-0042.md')
        self.assertEqual(meta.get('supersedes'), ['R-0007'])
        self.assertIn('rules/legacy-harvest-note.md', body)
        self.assertIn('- 2026-09-01: created (from R-0007)', body)

        loser = self._text('rules', 'R-0007')
        meta, body = frontmatter.parse(loser, path='rules/R-0007.md')
        self.assertEqual(meta['superseded_by'], 'D-0042')
        self.assertIn("- 2026-09-21 groom: merged R-0007's source and history, "
                     "superseded_by → D-0042 (operator)", body)
        # the loser's own body is otherwise unchanged
        self.assertIn('Harvest never touches the trunk directly', body)

    def test_merge_onto_a_survivor_with_no_source_section_lands_the_supersession_anyway(self):
        write_item(
            self.root, 'D-0043', 'decision', 'A migrated decision with no Source section',
            typed_lines=['decided: true', 'decided_by: ops', 'date: 2026-09-16'],
            machine_lines=['state: New', 'stage_since: 2026-09-16T00:00:00Z',
                          'updated: 2026-09-16T00:00:00Z'],
            body=("## Statement\nHarvest never touches the trunk directly; it works through "
                  "`harvest.rebase_and_resolve`, one more time.\n\n## Context\n\n"
                  "## History\n- 2026-09-16: created\n\n## Children\n\n## Backlinks\n"))
        run(['index'], self.root)
        with open(os.path.join(self.root, 'groom', '2026-09-20.md'), 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Conflicting rules and decisions\n\n" +
                    ("- [ ] D-0043 conflicts R-0007 — overlap 0.62; keep A · keep B · both · "
                     "merge (merge keeps D-0043) → answer: merge\n"))
        r = self._apply()
        self.assertEqual(r.returncode, 0, r.stderr)

        winner = self._text('decisions', 'D-0043')
        meta, body = frontmatter.parse(winner, path='decisions/D-0043.md')
        self.assertEqual(meta['supersedes'], ['R-0007'])
        self.assertNotIn('## Source', body)
        self.assertIn('one more time.', body)  # every other section byte-identical otherwise

        loser = self._text('rules', 'R-0007')
        meta, _body = frontmatter.parse(loser, path='rules/R-0007.md')
        self.assertEqual(meta['superseded_by'], 'D-0043')

    def test_blank_slot_bar_yes_and_rank_write_nothing(self):
        for answer in ('', '____', 'yes', 'rank 3'):
            with self.subTest(answer=answer):
                self._write_yesterday(answer)
                r = self._apply()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn('applied 0', r.stdout)
                meta, _body = frontmatter.parse(self._text('rules', 'R-0007'),
                                                path='rules/R-0007.md')
                self.assertNotIn('superseded_by', meta)
                self.assertNotIn('decided', meta)

    def test_the_answer_may_carry_a_why(self):
        self._write_yesterday('keep A — R-0007 is the older wording')
        r = self._apply()
        self.assertEqual(r.returncode, 0, r.stderr)
        meta, body = frontmatter.parse(self._text('rules', 'R-0007'), path='rules/R-0007.md')
        self.assertEqual(meta['superseded_by'], 'D-0042')
        self.assertIn('- 2026-09-21 groom: superseded_by → D-0042 (operator)', body)
        self.assertNotIn('older wording', body)  # the why is not written onto either card

    def test_groom_answer_events_carry_section_conflicts_and_the_field_per_card(self):
        self._write_yesterday('keep A')
        with open(os.path.join(self.root, 'groom', '2026-09-20.md'), encoding='utf-8') as f:
            prev_sections = groom._line_sections(f.read())
        by_id, _errors = load_items(self.root)
        canonical, _dupes = canonicalize(by_id)
        events = []
        groom.apply_groom_answers(
            self.root, canonical, os.path.join(self.root, 'groom', '2026-09-20.md'), '2026-09-21',
            event=lambda kind, **kw: events.append((kind, kw)), sections=prev_sections)
        by_item = {e[1]['item']: e[1] for e in events}
        self.assertEqual(by_item['R-0007']['section'], 'conflicts')
        self.assertEqual(by_item['R-0007']['field'], 'superseded_by')
        self.assertEqual(by_item['D-0042']['section'], 'conflicts')
        self.assertEqual(by_item['D-0042']['field'], 'supersedes')


class ConflictIdempotencyTests(unittest.TestCase):
    """F-0046 Task 6: `--apply` requires every answer to be idempotent, and the two refusals to
    write nothing at all."""

    def setUp(self):
        self.root = make_repo()
        write_item(
            self.root, 'R-0007', 'rule', 'Harvest goes through rebase_and_resolve',
            typed_lines=['scope: harvest', 'enforced: true', 'reason: keep the trunk clean',
                        'check: check.sh'],
            machine_lines=['state: New', 'stage_since: 2026-09-01T00:00:00Z',
                          'updated: 2026-09-01T00:00:00Z'],
            body=("## Statement\nHarvest never touches the trunk directly; it works through "
                  "`harvest.rebase_and_resolve`.\n\n## Source\nrules/legacy-harvest-note.md\n\n"
                  "## History\n- 2026-09-01: created\n\n## Children\n\n## Backlinks\n"))
        write_item(
            self.root, 'D-0042', 'decision', 'A worker branch fast-forwards the trunk itself',
            typed_lines=['decided: true', 'decided_by: ops', 'date: 2026-09-15',
                        'scope: harvest'],
            machine_lines=['state: New', 'stage_since: 2026-09-15T00:00:00Z',
                          'updated: 2026-09-15T00:00:00Z'],
            body=("## Statement\nA worker branch may fast-forward the trunk itself, bypassing "
                  "`harvest.rebase_and_resolve` for a clean history.\n\n## Source\n\n"
                  "## History\n- 2026-09-15: created\n\n## Children\n\n## Backlinks\n"))
        run(['index'], self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_yesterday(self, answer):
        with open(os.path.join(self.root, 'groom', '2026-09-20.md'), 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Conflicting rules and decisions\n\n"
                    "- [ ] D-0042 conflicts R-0007 — scope `harvest`, shares "
                    "`harvest.rebase_and_resolve`; keep A · keep B · both · merge "
                    f"(merge keeps D-0042) → answer: {answer}\n")

    def _text(self, folder, name):
        with open(os.path.join(self.root, folder, f'{name}.md'), encoding='utf-8') as f:
            return f.read()

    def test_reapplying_the_same_file_writes_nothing_the_second_time(self):
        self._write_yesterday('keep A')
        run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        snapshot = (self._text('rules', 'R-0007'), self._text('decisions', 'D-0042'))
        r2 = run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn('applied 0', r2.stdout)
        self.assertEqual((self._text('rules', 'R-0007'), self._text('decisions', 'D-0042')),
                         snapshot)

    def test_merge_run_twice_does_not_duplicate_source_or_history(self):
        self._write_yesterday('merge')
        run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        snapshot = self._text('decisions', 'D-0042')
        r2 = run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn('applied 0', r2.stdout)
        self.assertEqual(self._text('decisions', 'D-0042'), snapshot)
        self.assertEqual(snapshot.count('rules/legacy-harvest-note.md'), 1)
        self.assertEqual(snapshot.count('- 2026-09-01: created (from R-0007)'), 1)

    def test_keep_a_refuses_when_the_loser_is_already_superseded_by_a_third_card(self):
        write_item(
            self.root, 'D-0051', 'decision', 'A third card, unrelated to the pair',
            typed_lines=['decided: true', 'decided_by: ops', 'date: 2026-09-18'])
        frontmatter.write_typed(os.path.join(self.root, 'rules', 'R-0007.md'),
                                {'superseded_by': 'D-0051'})
        run(['index'], self.root)
        self._write_yesterday('keep A')
        r = run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('applied 0', r.stdout)
        self.assertIn('groom: keep A D-0042+R-0007 skipped — R-0007 is already superseded by '
                      'D-0051', r.stdout)
        meta, _body = frontmatter.parse(self._text('decisions', 'D-0042'),
                                        path='decisions/D-0042.md')
        self.assertNotIn('supersedes', meta)

    def test_keep_a_refuses_a_supersession_cycle(self):
        # R-0007 already superseded by D-0042 the other way around; `keep A` on this pair would
        # try to make R-0007 supersede D-0042 back — a 2-cycle.
        frontmatter.write_typed(os.path.join(self.root, 'decisions', 'D-0042.md'),
                                {'superseded_by': 'R-0007'})
        frontmatter.write_typed(os.path.join(self.root, 'rules', 'R-0007.md'),
                                {'supersedes': ['D-0042']})
        run(['index'], self.root)
        with open(os.path.join(self.root, 'groom', '2026-09-20.md'), 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Conflicting rules and decisions\n\n"
                    "- [ ] D-0042 conflicts R-0007 — reason; keep A · keep B · both · merge "
                    "(merge keeps D-0042) → answer: keep A\n")
        r = run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('applied 0', r.stdout)
        self.assertIn('groom: keep A D-0042+R-0007 skipped — R-0007 would close a supersession '
                      'cycle through D-0042', r.stdout)
        meta, _body = frontmatter.parse(self._text('decisions', 'D-0042'),
                                        path='decisions/D-0042.md')
        self.assertEqual(meta['superseded_by'], 'R-0007')  # unchanged


class GroomInboxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_inbox_file_becomes_a_bug_card_under_configured_default_epic(self):
        with open(os.path.join(self.root, 'inbox', 'thing.md'), 'w', encoding='utf-8') as f:
            f.write("# Checkout is broken\nsignature: checkout-pay\nCustomers cannot pay.\n")

        r = run(['groom', '--default-bug-epic', 'E-0009'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)

        inbox_left = os.listdir(os.path.join(self.root, 'inbox'))
        self.assertEqual(inbox_left, ['done'])
        done_files = os.listdir(os.path.join(self.root, 'inbox', 'done'))
        self.assertEqual(len(done_files), 1)
        with open(os.path.join(self.root, 'inbox', 'done', done_files[0])) as f:
            done_text = f.read()
        self.assertTrue(done_text.startswith('→ B-0001\n') or done_text.startswith('-> B-0001\n')
                        or done_text.startswith('→ B-0001'))

        bugs = os.listdir(os.path.join(self.root, 'bugs'))
        self.assertEqual(bugs, ['B-0001.md'])
        with open(os.path.join(self.root, 'bugs', 'B-0001.md')) as f:
            text = f.read()
        meta, _body = frontmatter.parse(text, path='bugs/B-0001.md')
        self.assertEqual(meta['title'], 'Checkout is broken')
        self.assertEqual(meta['parent'], 'E-0009')
        self.assertEqual(meta['decided'], False)

        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            groom_text = f.read()
        self.assertIn('B-0001 Checkout is broken', groom_text)

    def test_fixture_mints_the_first_id_with_a_job_range_exported(self):
        # B-0012: a ranged worker exports BACKLOG_ID_RANGE; the fixture's own runs must not inherit it
        with mock.patch.dict(os.environ, {'BACKLOG_ID_RANGE': 'B:0900-0949,S:0900-0949,T:0900-0949'}):
            with open(os.path.join(self.root, 'inbox', 'thing.md'), 'w', encoding='utf-8') as f:
                f.write("# Checkout is broken\nsignature: checkout-pay\nCustomers cannot pay.\n")
            r = run(['groom', '--default-bug-epic', 'E-0009'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(os.path.join(self.root, 'bugs')), ['B-0001.md'])

    def test_inbox_bug_with_no_default_configured_asks_a_question(self):
        with open(os.path.join(self.root, 'inbox', 'thing.md'), 'w', encoding='utf-8') as f:
            f.write("# Checkout is broken\nsignature: checkout-pay\nCustomers cannot pay.\n")

        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        path = os.path.join(self.root, 'inbox', 'thing.md')
        self.assertTrue(os.path.isfile(path), "with no default epic configured, groom asks instead of guessing")
        with open(path) as f:
            self.assertIn('## Question', f.read())

    def test_intake_dir_is_a_convention(self):
        # T-0040: `process_inbox`'s `intake_dir` is the product's convention, not a hardcoded
        # `inbox/` — a product that declares `intake_dir: cards` is read from `cards/`, and
        # `inbox/` (pre-created by `make_repo`, empty) is left untouched.
        os.makedirs(os.path.join(self.root, 'cards'))
        with open(os.path.join(self.root, 'cards', 'thing.md'), 'w', encoding='utf-8') as f:
            f.write("# Checkout is broken\nsignature: checkout-pay\nCustomers cannot pay.\n")
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BACKLOG_ID_RANGE', None)
            created = inbox_mod.process_inbox(self.root, canonical, today(),
                                              default_bug_parent='E-0009', intake_dir='cards')
        self.assertEqual(len(created), 1)
        self.assertEqual(os.listdir(os.path.join(self.root, 'inbox', 'done')), [])
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'cards', 'done'))), 1)

    def test_ambiguous_feature_gets_one_question_and_is_left_in_inbox(self):
        path = os.path.join(self.root, 'inbox', 'vague.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("A nicer settings page\nNo strong feelings about scope yet.\n")

        r = run(['groom'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(path), "the file should stay in inbox/, not move")
        with open(path) as f:
            text = f.read()
        self.assertIn('## Question', text)
        self.assertEqual(os.listdir(os.path.join(self.root, 'features')), [])

        # a second run must not append a second Question
        run(['groom'], self.root)
        with open(path) as f:
            text2 = f.read()
        self.assertEqual(text2.count('## Question'), 1)


class GroomApplyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Some idea', parent='E-0009',
                  typed_lines=['decided: false'])
        run(['index'], self.root)
        with open(os.path.join(self.root, 'groom', '2026-09-20.md'), 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Undecided > 3 days\n\n"
                    "- [ ] F-0001 Some idea — undecided 4d → answer: yes\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_apply_writes_typed_field_and_history_byte_identical_elsewhere(self):
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            f.read()
        r = run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('applied 1', r.stdout)

        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            after = f.read()
        meta, body = frontmatter.parse(after, path='features/F-0001.md')
        self.assertEqual(meta['decided'], True)
        self.assertIn('2026-09-21 groom: decided → true (operator)', body)
        # every typed line other than `decided` is untouched
        self.assertIn('id: F-0001', after)
        self.assertIn('title: Some idea', after)
        self.assertIn('parent: E-0009', after)

    def test_apply_is_idempotent_on_a_second_run_same_date(self):
        run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            snapshot = f.read()
        r2 = run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        self.assertIn('applied 0', r2.stdout)
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            self.assertEqual(f.read(), snapshot)

    def test_controller_prefix_changes_history_attribution(self):
        with open(os.path.join(self.root, 'groom', '2026-09-20.md'), 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Undecided > 3 days\n\n"
                    "- [ ] F-0001 Some idea — undecided 4d → answer: controller: yes\n")
        run(['groom', '--date', '2026-09-21', '--apply'], self.root)
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            text = f.read()
        self.assertIn('(controller, starvation policy)', text)


class TwoAnswersFilesTests(unittest.TestCase):
    """§2.4.1: ``ANSWERS_FILE_RE`` matches a day's judgement file and its clerk half by name —
    not a malformed neighbor — and each applies through ``cmd_groom``'s ``answers_file`` on its
    own call, every answer landing on its card (groom.py:704, groom.py:792)."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'F-0001', 'feature', 'Some idea', parent='E-0009',
                  typed_lines=['decided: false'])
        write_item(self.root, 'F-0002', 'feature', 'Another idea', parent='E-0009',
                  typed_lines=['decided: false'])
        run(['index'], self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def apply(self, path):
        args = argparse.Namespace(date=None, apply=False, product=None, default_bug_epic=None,
                                  answers_file=path, event=None)
        with mock.patch.object(env, 'load_product', side_effect=env.ConfigError('none')):
            return groom.cmd_groom(args, self.root)

    def test_the_regex_matches_both_names_and_not_a_malformed_one(self):
        judgement = groom.ANSWERS_FILE_RE.match('2026-09-20.answers')
        clerk = groom.ANSWERS_FILE_RE.match('2026-09-20.clerk.answers')
        self.assertEqual(judgement.group('date'), '2026-09-20')
        self.assertIsNone(judgement.group('half'))
        self.assertEqual(clerk.group('date'), '2026-09-20')
        self.assertEqual(clerk.group('half'), 'clerk')
        self.assertIsNone(groom.ANSWERS_FILE_RE.match('2026-09-20.answers.bak'))
        self.assertIsNone(groom.ANSWERS_FILE_RE.match('2026-09-20.answers.done'))

    def test_both_files_apply_and_every_answer_lands_on_its_card(self):
        judgement = os.path.join(self.root, 'groom', '2026-09-20.answers')
        clerk = os.path.join(self.root, 'groom', '2026-09-20.clerk.answers')
        with open(judgement, 'w', encoding='utf-8') as f:
            f.write('- [ ] F-0001 Some idea — undecided 4d → answer: yes\n')
        with open(clerk, 'w', encoding='utf-8') as f:
            f.write('- [ ] F-0002 Another idea — undecided 4d → answer: yes\n')

        self.assertEqual(self.apply(judgement), 0)
        self.assertEqual(self.apply(clerk), 0)

        self.assertFalse(os.path.exists(judgement))
        self.assertFalse(os.path.exists(clerk))
        self.assertTrue(os.path.exists(judgement + '.done'))
        self.assertTrue(os.path.exists(clerk + '.done'))
        with open(os.path.join(self.root, 'features', 'F-0001.md')) as f:
            self.assertIn('decided: true', f.read())
        with open(os.path.join(self.root, 'features', 'F-0002.md')) as f:
            self.assertIn('decided: true', f.read())


class GroomSectionCoverageTests(unittest.TestCase):
    """Features without Stories, Stories without Tasks after plan-approved, blocked-on-Closed —
    the sections that don't already have integration coverage above."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_feature_without_stories(self):
        write_item(self.root, 'F-0001', 'feature', 'Lonely feature', parent='E-0009',
                  typed_lines=['decided: true'])
        run(['index'], self.root)
        run(['groom'], self.root)
        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertIn('F-0001 Lonely feature — no Stories', text)

    def test_story_without_task_after_plan_approved(self):
        write_item(self.root, 'F-0001', 'feature', 'Feature with a gap', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', 'stage: building 0/1',
                                 'stage_since: 2026-09-01T00:00:00Z', 'updated: 2026-09-01T00:00:00Z'])
        write_item(self.root, 'S-0001', 'story', 'Uncovered story', parent='F-0001',
                  typed_lines=['decided: true'])
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertIn('S-0001 has no Task listing it in stories:', r.stdout)

        run(['groom'], self.root)
        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertIn('S-0001 Uncovered story', text)
        self.assertIn('no Task lists it', text)

    def test_removed_task_does_not_cover_a_story(self):
        write_item(self.root, 'F-0001', 'feature', 'Feature with a gap', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Active', 'stage: building 0/1',
                                 'stage_since: 2026-09-01T00:00:00Z', 'updated: 2026-09-01T00:00:00Z'])
        write_item(self.root, 'S-0001', 'story', 'Uncovered story', parent='F-0001',
                  typed_lines=['decided: true'])
        write_item(self.root, 'T-0001', 'task', 'Removed task', parent='F-0001',
                  typed_lines=['stories: [S-0001]', 'removed: merged into T-0002 (groom 2026-09-21)'])
        run(['index'], self.root)
        r = run(['check'], self.root)
        self.assertIn('S-0001 has no Task listing it in stories:', r.stdout)

        run(['groom'], self.root)
        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertIn('S-0001 Uncovered story', text)
        self.assertIn('no Task lists it', text)

    def test_blocked_on_a_closed_item(self):
        write_item(self.root, 'F-0001', 'feature', 'Blocker', parent='E-0009',
                  typed_lines=['decided: true'],
                  machine_lines=['state: Closed', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z'])
        write_item(self.root, 'F-0002', 'feature', 'Blocked one', parent='E-0009',
                  typed_lines=['decided: true', 'blockedBy: [F-0001]'])
        run(['index'], self.root)
        run(['groom'], self.root)
        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertIn('F-0002 Blocked one — blockedBy F-0001, which is Closed', text)


def _machine(cost=None, **extra):
    lines = ['state: Active', 'stage_since: 2026-09-01T00:00:00Z',
             'updated: 2026-09-01T00:00:00Z']
    if cost is not None:
        lines.append(f"cost: {{sessions: {cost[0]}, usd: {cost[1]}}}")
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    return lines


class OverBudgetQuestionTests(unittest.TestCase):
    """T6 (F-0092 §2.7): the groom asks one question per open card a budget has stopped, and
    `budget <n> [$<usd>]` raises it."""

    def setUp(self):
        self.root = make_repo()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def canonical(self):
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        return canonical

    def derived(self):
        return compute_derived(self.canonical())

    def product(self, **approvals):
        data = {'approvals': approvals} if approvals else {}
        return env.Product('demo', data)

    def test_one_line_per_open_over_budget_card_naming_both_measures_and_the_answers(self):
        write_item(self.root, 'T-0001', 'task', 'Chatty task', machine_lines=_machine((9, 13.53)))
        write_item(self.root, 'T-0002', 'task', 'Frugal task', machine_lines=_machine((1, 2)))
        lines = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertTrue(line.startswith('- [ ] T-0001 Chatty task — '), line)
        self.assertIn('9/3 sessions', line)
        self.assertIn('$13.53/$10', line)
        self.assertIn('product default', line)
        self.assertIn('`budget 12`', line)
        self.assertIn('`budget 12 $25`', line)
        self.assertIn('close it (`no: <why>`)', line)
        self.assertIn('reshape it (`reshape: <how>`)', line)
        self.assertTrue(line.endswith('→ answer: ____'), line)

    def test_over_on_usd_alone_names_the_usd_measure(self):
        write_item(self.root, 'T-0001', 'task', 'Pricey task', machine_lines=_machine((1, 15)))
        line = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())[0]
        self.assertIn('1/3 sessions', line)
        self.assertIn('$15/$10', line)

    def test_a_card_under_its_raised_budget_is_not_asked(self):
        write_item(self.root, 'T-0001', 'task', 'Already raised', typed_lines=['budget_sessions: 20'],
                  machine_lines=_machine((9, 2)))
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), self.derived(), self.product()), [])

    def test_an_item_budget_is_named_item_not_product_default(self):
        write_item(self.root, 'T-0001', 'task', 'Raised low', typed_lines=['budget_sessions: 5'],
                  machine_lines=_machine((5, 1)))
        line = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())[0]
        self.assertIn('item', line)
        self.assertNotIn('product default', line)

    def test_a_closed_card_is_not_asked(self):
        write_item(self.root, 'T-0001', 'task', 'Done and over',
                  machine_lines=['state: Closed', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z', 'cost: {sessions: 9, usd: 13.53}'])
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), self.derived(), self.product()), [])

    def test_a_reshaped_card_is_not_asked(self):
        write_item(self.root, 'T-0001', 'task', 'Reshaping', typed_lines=['reshape: split it'],
                  machine_lines=_machine((9, 13.53)))
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), self.derived(), self.product()), [])

    def test_no_product_asks_nothing(self):
        write_item(self.root, 'T-0001', 'task', 'Chatty task', machine_lines=_machine((9, 13.53)))
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), self.derived(), None), [])

    def _run_groom(self, date='2026-09-21'):
        args = argparse.Namespace(date=date, apply=False, product=None, default_bug_epic=None,
                                  answers_file=None, event=None)
        with mock.patch.object(env, 'load_product', return_value=self.product()):
            rc = groom.cmd_groom(args, self.root)
        with open(os.path.join(self.root, 'groom', f'{date}.md')) as f:
            return rc, f.read()

    def test_rendered_and_attributed_to_over_budget_when_it_has_lines(self):
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        write_item(self.root, 'T-0001', 'task', 'Chatty task', parent='E-0009',
                  machine_lines=_machine((9, 13.53)))
        run(['index'], self.root)
        rc, text = self._run_groom()
        self.assertEqual(rc, 0)
        self.assertIn('## Over budget', text)
        self.assertIn('T-0001 Chatty task — 9/3 sessions, $13.53/$10 over budget', text)
        self.assertEqual(groom._line_sections(text).get('T-0001'), 'over_budget')

    def test_no_lines_no_section(self):
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        run(['index'], self.root)
        rc, text = self._run_groom()
        self.assertEqual(rc, 0)
        self.assertNotIn('## Over budget', text)

    def test_the_full_line_matches_the_spec_word_for_word(self):
        write_item(self.root, 'T-0001', 'task', 'Chatty task', machine_lines=_machine((9, 13.53)))
        line = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())[0]
        self.assertEqual(
            line,
            '- [ ] T-0001 Chatty task — 9/3 sessions, $13.53/$10 over budget (product default): '
            'raise it (`budget 12` or `budget 12 $25`), close it (`no: <why>`) or reshape it '
            '(`reshape: <how>`) → answer: ____')

    # The applier's side: `budget <n> [$<usd>]` raises the card, idempotently, attributed.

    def write_prev(self, answer, prefix=''):
        path = os.path.join(self.root, 'groom', '2026-09-20.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Over budget\n\n"
                    f"- [ ] T-0001 Chatty task — 9/3 sessions, $13.53/$10 over budget (product "
                    f"default): raise it (`budget 12` or `budget 12 $25`), close it "
                    f"(`no: <why>`) or reshape it (`reshape: <how>`) → answer: {prefix}{answer}\n")
        return path

    def _ensure_chatty_task(self):
        if not getattr(self, '_chatty_task_ready', False):
            write_item(self.root, 'T-0001', 'task', 'Chatty task', machine_lines=_machine((9, 13.53)))
            run(['index'], self.root)
            self._chatty_task_ready = True

    def apply(self, answer, prefix='', adjudicator_job=None):
        self._ensure_chatty_task()
        prev = self.write_prev(answer, prefix)
        canonical = self.canonical()
        with open(prev, encoding='utf-8') as f:
            sections = groom._line_sections(f.read())
        events = []
        applied = groom.apply_groom_answers(self.root, canonical, prev, '2026-09-21',
                                            adjudicator_job=adjudicator_job,
                                            event=lambda kind, **f: events.append((kind, f)),
                                            sections=sections)
        with open(os.path.join(self.root, 'tasks', 'T-0001.md')) as f:
            text = f.read()
        return applied, text, events

    def test_budget_n_writes_sessions_only(self):
        applied, text, events = self.apply('budget 12')
        self.assertEqual(applied, 1)
        self.assertIn('budget_sessions: 12', text)
        self.assertNotIn('budget_usd:', text)
        self.assertIn('2026-09-21 groom: budget → 12 sessions (operator)', text)
        self.assertEqual(events, [('groom_answer', {'item': 'T-0001', 'section': 'over_budget',
                                                     'field': 'budget', 'value': '12 sessions',
                                                     'by': 'operator'})])

    def test_budget_n_usd_writes_both(self):
        applied, text, _events = self.apply('budget 12 $25')
        self.assertEqual(applied, 1)
        self.assertIn('budget_sessions: 12', text)
        self.assertIn('budget_usd: 25', text)
        self.assertIn('2026-09-21 groom: budget → 12 sessions / $25 (operator)', text)

    def test_a_second_apply_is_idempotent(self):
        self.apply('budget 12')
        applied2, text2, _events = self.apply('budget 12')
        self.assertEqual(applied2, 0)
        self.assertEqual(text2.count('budget_sessions: 12'), 1)
        self.assertEqual(text2.count('groom: budget →'), 1)

    def test_no_closes_the_card_as_any_no_does(self):
        applied, text, _events = self.apply('no: not worth more')
        self.assertEqual(applied, 1)
        self.assertIn('removed: not worth more (groom 2026-09-21)', text)

    def test_budget_soon_is_skipped_naming_the_grammar(self):
        self._ensure_chatty_task()
        out = io.StringIO()
        prev = self.write_prev('budget soon')
        canonical = self.canonical()
        with open(prev, encoding='utf-8') as f:
            sections = groom._line_sections(f.read())
        with contextlib.redirect_stdout(out):
            applied = groom.apply_groom_answers(self.root, canonical, prev, '2026-09-21',
                                                sections=sections)
        self.assertEqual(applied, 0)
        self.assertIn('budget <n> [$<usd>]', out.getvalue())

    def test_adjudicator_prefix_applies_and_attributes_under_auto(self):
        applied, text, events = self.apply('budget 12', prefix='adjudicator: ',
                                           adjudicator_job='groom-2026-09-20')
        self.assertEqual(applied, 1)
        self.assertIn('budget_sessions: 12', text)
        self.assertIn('(adjudicator, groom-2026-09-20)', text)
        self.assertEqual(events[0][1]['by'], 'adjudicator:groom-2026-09-20')


class EpicOverBudgetQuestionTests(unittest.TestCase):
    """T-0280 (F-0052 §2.6): the groom asks one question per open Epic whose subtree spend is
    over its own `budget_usd`, and `budget $<usd>` is the ruling that raises it."""

    def setUp(self):
        self.root = make_repo()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def canonical(self):
        by_id, _ = load_items(self.root)
        canonical, _ = canonicalize(by_id)
        return canonical

    def derived(self):
        return compute_derived(self.canonical())

    def product(self):
        return env.Product('demo', {})

    def _epic_line(self, lines, iid='E-0001'):
        found = [l for l in lines if l.startswith(f'- [ ] {iid} ')]
        return found[0] if found else None

    def test_one_line_per_open_over_budget_epic_naming_both_figures_and_the_answers(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['budget_usd: 500'])
        write_item(self.root, 'T-0001', 'task', 'Chatty task', parent='E-0001',
                  machine_lines=_machine((0, 300)))
        write_item(self.root, 'T-0002', 'task', 'Frugal task', parent='E-0001',
                  machine_lines=_machine((0, 212.40)))
        lines = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())
        line = self._epic_line(lines)
        self.assertIsNotNone(line, lines)
        self.assertEqual(
            line,
            '- [ ] E-0001 Factory — $512.40/$500 spent, new work held: raise it '
            '(`budget $750`), reshape it (`reshape: <how>`) or close its work '
            '(`no: <why>`) → answer: ____')

    def test_an_epic_under_its_budget_is_not_asked(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['budget_usd: 500'])
        write_item(self.root, 'T-0001', 'task', 'Frugal task', parent='E-0001',
                  machine_lines=_machine((0, 400)))
        lines = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())
        self.assertIsNone(self._epic_line(lines))

    def test_an_epic_with_no_budget_is_not_asked(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        write_item(self.root, 'T-0001', 'task', 'Chatty task', parent='E-0001',
                  machine_lines=_machine((0, 600)))
        lines = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())
        self.assertIsNone(self._epic_line(lines))

    def test_a_closed_epic_is_not_asked(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['budget_usd: 500'],
                  machine_lines=['state: Closed', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z'])
        write_item(self.root, 'T-0001', 'task', 'Chatty task', parent='E-0001',
                  machine_lines=_machine((0, 600)))
        lines = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())
        self.assertIsNone(self._epic_line(lines))

    def test_a_reshaped_epic_is_not_asked(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory',
                  typed_lines=['budget_usd: 500', 'reshape: split it'])
        write_item(self.root, 'T-0001', 'task', 'Chatty task', parent='E-0001',
                  machine_lines=_machine((0, 600)))
        lines = groom.groom_over_budget_section(self.canonical(), self.derived(), self.product())
        self.assertIsNone(self._epic_line(lines))

    def test_no_product_asks_nothing(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['budget_usd: 500'])
        write_item(self.root, 'T-0001', 'task', 'Chatty task', parent='E-0001',
                  machine_lines=_machine((0, 600)))
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), self.derived(), None), [])

    def test_a_removed_task_under_the_epic_is_not_summed(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['budget_usd: 500'])
        write_item(self.root, 'T-0001', 'task', 'Dropped', parent='E-0001',
                  typed_lines=['removed: duplicate (groom 2026-09-01)'],
                  machine_lines=_machine((0, 600)))
        write_item(self.root, 'T-0002', 'task', 'Kept', parent='E-0001',
                  machine_lines=_machine((0, 100)))
        canonical, derived = self.canonical(), self.derived()
        lines = groom.groom_over_budget_section(canonical, derived, self.product())
        self.assertIsNone(self._epic_line(lines))
        run(['index'], self.root)
        ix_items, _generated = index_reader.load(self.root)
        view = groom._index_view(canonical, derived)
        self.assertEqual(index_reader.subtree_usd(view, view['E-0001']),
                         index_reader.subtree_usd(ix_items, ix_items['E-0001']))
        self.assertEqual(index_reader.subtree_usd(view, view['E-0001']), 100)

    def test_answer_for_budget_dollars_only(self):
        self.assertEqual(groom._parse_answer('budget $750'), ('budget', {'budget_usd': 750}))

    def test_answer_for_budget_dollars_only_keeps_a_fraction(self):
        self.assertEqual(groom._parse_answer('budget $12.50'), ('budget', {'budget_usd': 12.5}))

    def test_answer_for_budget_with_no_dollar_sign_is_still_sessions(self):
        self.assertEqual(groom._parse_answer('budget 750'), ('budget', {'budget_sessions': 750}))

    def test_answer_for_the_two_present_forms_are_unchanged(self):
        self.assertEqual(groom._parse_answer('budget 12'), ('budget', {'budget_sessions': 12}))
        self.assertEqual(groom._parse_answer('budget 12 $25'),
                         ('budget', {'budget_sessions': 12, 'budget_usd': 25}))

    def _write_epic_prev(self, answer, prefix=''):
        path = os.path.join(self.root, 'groom', '2026-09-20.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# Groom 2026-09-20\n\n## Over budget\n\n"
                    "- [ ] E-0001 Factory — $600/$500 spent, new work held: raise it "
                    "(`budget $900`), reshape it (`reshape: <how>`) or close its work "
                    f"(`no: <why>`) → answer: {prefix}{answer}\n")
        return path

    def _ensure_over_budget_epic(self):
        if not getattr(self, '_epic_ready', False):
            write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['budget_usd: 500'])
            write_item(self.root, 'T-0001', 'task', 'Chatty task', parent='E-0001',
                      machine_lines=_machine((0, 600)))
            run(['index'], self.root)
            self._epic_ready = True

    def apply_epic(self, answer, prefix='', adjudicator_job=None):
        self._ensure_over_budget_epic()
        prev = self._write_epic_prev(answer, prefix)
        canonical = self.canonical()
        with open(prev, encoding='utf-8') as f:
            sections = groom._line_sections(f.read())
        events = []
        applied = groom.apply_groom_answers(self.root, canonical, prev, '2026-09-21',
                                            adjudicator_job=adjudicator_job,
                                            event=lambda kind, **f: events.append((kind, f)),
                                            sections=sections)
        with open(os.path.join(self.root, 'epics', 'E-0001.md')) as f:
            text = f.read()
        return applied, text, events

    def test_budget_dollars_only_writes_typed_budget_usd_and_nothing_else(self):
        applied, text, events = self.apply_epic('budget $750')
        self.assertEqual(applied, 1)
        self.assertIn('budget_usd: 750', text)
        self.assertNotIn('budget_sessions:', text)
        self.assertIn('2026-09-21 groom: budget → $750 (operator)', text)
        self.assertEqual(events, [('groom_answer', {'item': 'E-0001', 'section': 'over_budget',
                                                     'field': 'budget', 'value': '$750',
                                                     'by': 'operator'})])

    def test_a_second_apply_is_idempotent(self):
        self.apply_epic('budget $750')
        applied2, text2, _events = self.apply_epic('budget $750')
        self.assertEqual(applied2, 0)
        self.assertEqual(text2.count('budget_usd: 750'), 1)
        self.assertEqual(text2.count('groom: budget →'), 1)

    def test_a_sessions_answer_against_an_epic_is_refused(self):
        out = io.StringIO()
        self._ensure_over_budget_epic()
        prev = self._write_epic_prev('budget 12')
        canonical = self.canonical()
        with open(prev, encoding='utf-8') as f:
            sections = groom._line_sections(f.read())
        with contextlib.redirect_stdout(out):
            applied = groom.apply_groom_answers(self.root, canonical, prev, '2026-09-21',
                                                sections=sections)
        self.assertEqual(applied, 0)
        self.assertIn('an Epic has no session budget', out.getvalue())
        with open(os.path.join(self.root, 'epics', 'E-0001.md')) as f:
            text = f.read()
        self.assertNotIn('budget_sessions:', text)

    def test_a_two_value_answer_against_an_epic_is_also_refused(self):
        out = io.StringIO()
        self._ensure_over_budget_epic()
        prev = self._write_epic_prev('budget 12 $25')
        canonical = self.canonical()
        with open(prev, encoding='utf-8') as f:
            sections = groom._line_sections(f.read())
        with contextlib.redirect_stdout(out):
            applied = groom.apply_groom_answers(self.root, canonical, prev, '2026-09-21',
                                                sections=sections)
        self.assertEqual(applied, 0)
        self.assertIn('an Epic has no session budget', out.getvalue())
        with open(os.path.join(self.root, 'epics', 'E-0001.md')) as f:
            text = f.read()
        self.assertNotIn('budget_sessions:', text)

    def _run_groom(self, date='2026-09-21'):
        args = argparse.Namespace(date=date, apply=False, product=None, default_bug_epic=None,
                                  answers_file=None, event=None)
        with mock.patch.object(env, 'load_product', return_value=self.product()):
            rc = groom.cmd_groom(args, self.root)
        with open(os.path.join(self.root, 'groom', f'{date}.md')) as f:
            return rc, f.read()

    def test_rendered_and_attributed_to_over_budget(self):
        write_item(self.root, 'E-0001', 'epic', 'Factory', typed_lines=['budget_usd: 500'])
        write_item(self.root, 'T-0001', 'task', 'Chatty task', parent='E-0001',
                  machine_lines=_machine((0, 600)))
        run(['index'], self.root)
        rc, text = self._run_groom()
        self.assertEqual(rc, 0)
        self.assertIn('## Over budget', text)
        self.assertIn('E-0001 Factory — $600/$500 spent, new work held', text)
        self.assertEqual(groom._line_sections(text).get('E-0001'), 'over_budget')


if __name__ == '__main__':
    unittest.main()
