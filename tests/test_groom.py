import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from asf.record import frontmatter
from asf.record.core import canonicalize, compute_derived, load_items, today, tokenize
from asf.groom import groom
from asf.groom import inbox as inbox_mod
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


class UndecidedFromFirstGroomTests(unittest.TestCase):
    """§2.5 / T6: every open item whose `decided` is not true is a question from its first groom."""

    def setUp(self):
        self.root = make_repo()
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        self.now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def fresh(self):
        return ['state: New', f'stage_since: {self.now}', f'updated: {self.now}']

    def sections(self):
        canonical, _dupes = canonicalize(load_items(self.root)[0])
        return groom.build_groom_sections(canonical, compute_derived(canonical), today())

    def ids(self, key):
        return [m.group(1) for m in map(groom._LINE_ID_RE.match, self.sections()[key]) if m]

    def test_a_feature_made_a_minute_ago_is_asked_on_the_next_groom(self):
        write_item(self.root, 'F-0001', 'feature', 'Just filed', parent='E-0009',
                  machine_lines=self.fresh())
        run(['index'], self.root)
        run(['groom'], self.root)
        with open(os.path.join(self.root, 'groom', today() + '.md')) as f:
            text = f.read()
        self.assertIn('## Undecided\n', text)
        self.assertNotIn('## Undecided > 3 days', text)
        block = text.split('## Undecided\n', 1)[1].split('\n## ', 1)[0]
        self.assertIn('F-0001 Just filed — undecided', block)
        self.assertIn('→ answer: ____', block)

    def test_a_decided_item_is_not_asked(self):
        write_item(self.root, 'F-0001', 'feature', 'Settled', parent='E-0009',
                  typed_lines=['decided: true'], machine_lines=self.fresh())
        self.assertEqual(self.ids('undecided'), [])

    def test_a_decision_and_a_rule_are_in_neither_section(self):
        write_item(self.root, 'D-0001', 'decision', 'A ruling', machine_lines=self.fresh())
        write_item(self.root, 'R-0001', 'rule', 'A standing rule', machine_lines=self.fresh())
        old = ['state: New', 'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z']
        write_item(self.root, 'D-0002', 'decision', 'An old ruling', machine_lines=old)
        write_item(self.root, 'R-0002', 'rule', 'An old rule', machine_lines=old)
        self.assertEqual(self.ids('undecided'), [])
        self.assertEqual(self.ids('undecided14'), [])

    def test_an_inbox_origin_card_is_asked_once_in_the_inbox_section(self):
        body = DEFAULT_BODY.replace('created\n', 'created\n- 2026-09-23: created (inbox) from a.md\n')
        write_item(self.root, 'F-0001', 'feature', 'From the inbox', parent='E-0009',
                  machine_lines=self.fresh(), body=body)
        self.assertEqual(self.ids('inbox'), ['F-0001'])
        self.assertEqual(self.ids('undecided'), [])

    def test_an_auto_filed_bug_is_asked_once_in_its_own_section(self):
        write_item(self.root, 'B-0001', 'bug', 'Blank page', parent='E-0009',
                  typed_lines=['signature: blank-page'], machine_lines=self.fresh())
        write_item(self.root, 'B-0002', 'bug', 'A hand-filed bug', parent='E-0009',
                  machine_lines=self.fresh())
        self.assertEqual(self.ids('auto_bugs'), ['B-0001'])
        self.assertEqual(self.ids('undecided'), ['B-0002'])

    def test_undecided14_holds_only_the_fourteen_day_set(self):
        old = ['state: New', 'stage_since: 2026-01-01T00:00:00Z', 'updated: 2026-01-01T00:00:00Z']
        write_item(self.root, 'F-0001', 'feature', 'Starved', parent='E-0009', machine_lines=old)
        write_item(self.root, 'F-0002', 'feature', 'New arrival', parent='E-0009',
                  machine_lines=self.fresh())
        self.assertEqual(self.ids('undecided14'), ['F-0001'])
        self.assertEqual(self.ids('undecided'), ['F-0001', 'F-0002'])

    def test_a_day_file_written_under_the_retired_title_is_still_understood(self):
        text = ('# Groom 2026-09-20\n\n## Undecided > 3 days\n\n'
                '- [ ] F-0001 Some idea — undecided 4d → answer: controller: yes\n')
        self.assertEqual(groom._line_sections(text), {'F-0001': 'undecided'})


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


if __name__ == '__main__':
    unittest.main()
