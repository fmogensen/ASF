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
from asf.record.core import canonicalize, load_items, today, tokenize
from asf.groom import groom
from asf.groom import digest
from asf.groom import inbox as inbox_mod
from asf.groom import policy
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

    def product(self, **approvals):
        data = {'approvals': approvals} if approvals else {}
        return env.Product('demo', data)

    def test_one_line_per_open_over_budget_card_naming_both_measures_and_the_answers(self):
        write_item(self.root, 'T-0001', 'task', 'Chatty task', machine_lines=_machine((9, 13.53)))
        write_item(self.root, 'T-0002', 'task', 'Frugal task', machine_lines=_machine((1, 2)))
        lines = groom.groom_over_budget_section(self.canonical(), self.product())
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
        line = groom.groom_over_budget_section(self.canonical(), self.product())[0]
        self.assertIn('1/3 sessions', line)
        self.assertIn('$15/$10', line)

    def test_a_card_under_its_raised_budget_is_not_asked(self):
        write_item(self.root, 'T-0001', 'task', 'Already raised', typed_lines=['budget_sessions: 20'],
                  machine_lines=_machine((9, 2)))
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), self.product()), [])

    def test_an_item_budget_is_named_item_not_product_default(self):
        write_item(self.root, 'T-0001', 'task', 'Raised low', typed_lines=['budget_sessions: 5'],
                  machine_lines=_machine((5, 1)))
        line = groom.groom_over_budget_section(self.canonical(), self.product())[0]
        self.assertIn('item', line)
        self.assertNotIn('product default', line)

    def test_a_closed_card_is_not_asked(self):
        write_item(self.root, 'T-0001', 'task', 'Done and over',
                  machine_lines=['state: Closed', 'stage_since: 2026-09-01T00:00:00Z',
                                 'updated: 2026-09-01T00:00:00Z', 'cost: {sessions: 9, usd: 13.53}'])
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), self.product()), [])

    def test_a_reshaped_card_is_not_asked(self):
        write_item(self.root, 'T-0001', 'task', 'Reshaping', typed_lines=['reshape: split it'],
                  machine_lines=_machine((9, 13.53)))
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), self.product()), [])

    def test_no_product_asks_nothing(self):
        write_item(self.root, 'T-0001', 'task', 'Chatty task', machine_lines=_machine((9, 13.53)))
        self.assertEqual(groom.groom_over_budget_section(self.canonical(), None), [])

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
        line = groom.groom_over_budget_section(self.canonical(), self.product())[0]
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


if __name__ == '__main__':
    unittest.main()
