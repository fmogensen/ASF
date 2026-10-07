"""One id grammar (``asf.record.core.ID_DIGITS``): four or more digits, never exactly four.

A claimed id block runs past 9999 (``T:49890-49939``, ``S:29500-29549``), and every parser that
spelled ``\\d{4}`` itself read those ids as nothing — a groom answer on ``T-49891`` was not an
answer, a plan's ``stories: S-29501`` named no Story, a ``merged into T-49890`` merged nothing.
Two halves: each parser reads a five-digit id (the behaviour), and no new four-digit-only id
regex appears anywhere in the package or the suite (the ratchet).
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: an id-shaped ``-\d{4}`` (or ``-[0-9]{4}``) in a regex: a letter, ``]``, ``)`` or ``+`` then a
#: hyphen, then exactly four digits not followed by a comma, a digit or a hyphen — a date's
#: ``\d{4}-\d\d`` and a ``{4,}`` are not it
FOUR_ONLY = re.compile(r"[A-Za-z\])+]-\(?(?:\\d|\[0-9\])\{4\}(?![,\d-])")

#: the sites that still spell four digits, each owned outside this change: ``path → the line's
#: text``. A site is removed here when its owner swaps it to core's pattern; never added to.
ALLOWED = {
    # the lane's own item regex — swapped by the cloud/runtime workstream
    'asf/harvest/lane.py': "ITEM_ID_RE = re.compile(r'\\b([A-Za-z]+-\\d{4})\\b')",
    # the ingest's merged-into reader — swapped by the record-sync workstream
    'asf/record/ingest.py': "_MERGED_INTO = re.compile(r'\\bmerged into ([Tt]-\\d{4})\\b')",
    # a job name's item suffix, and the scorecard's job-name split: both read job names, where the
    # minter has only ever issued four-digit ids so far — next to swap
    'asf/workers/runtime.py': "_ITEM_RE = re.compile(r'([a-z])-(\\d{4})$', re.I)",
    'asf/scorecard/score.py': "_JOB_RE = re.compile(r'^(.*?)-[a-z]-\\d{4}')",
    # suite assertions on a fresh fixture's first minted id (always T-0001-shaped)
    'tests/test_readme_first_user.py': "m = re.search(r'^quickstart: first Task landed: (T-\\d{4})$'",
    'tests/test_quickstart.py': "m = re.search(r'^quickstart: first Task landed: (T-\\d{4})$'",
    'tests/test_approvals.py': "r'^NEEDS OPERATOR: held (\\S+) on ([A-Z]-\\d{4}) — .* — '",
    'tests/test_fixture_shapes.py': "BRANCH = re.compile(r'^(main|worker/T-\\d{4})$')",
    'tests/e2e/fakes/session.py': "JOB_RE = re.compile(r'^(?P<kind>.+)-(?P<item>[a-z]+-\\d{4})(?:-correction)?$')",
    # prose: the docstring that names the old pattern
    'tests/test_metrics.py': "With `^[EFSTBDR]-\\d{4}$` the resolved id was refused",
}


def four_digit_sites():
    out = []
    for top in ('asf', 'tests', 'tools'):
        for d, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                if not name.endswith('.py'):
                    continue
                path = os.path.join(d, name)
                rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
                if rel == 'tests/test_id_pattern.py':
                    continue
                with open(path, encoding='utf-8') as f:
                    for n, line in enumerate(f, 1):
                        if FOUR_ONLY.search(line):
                            out.append((rel, n, line.strip()))
    return out


class NoFourDigitIdRegex(unittest.TestCase):

    def test_no_new_four_digit_only_id_regex(self):
        new = [f'{rel}:{n}: {text}' for rel, n, text in four_digit_sites()
               if not (rel in ALLOWED and ALLOWED[rel] in text)]
        self.assertEqual(new, [], 'an id regex spells \\d{4}: import ID_DIGITS / ID_TOKEN_RE / ID_RE '
                                  'from asf.record.core instead')

    def test_the_allow_list_holds_only_live_sites(self):
        live = {rel for rel, _n, text in four_digit_sites()
                if rel in ALLOWED and ALLOWED[rel] in text}
        self.assertEqual(sorted(set(ALLOWED) - live), [],
                         'an allowed site is gone: drop it from ALLOWED (the ratchet only tightens)')

    def test_the_scan_sees_a_four_digit_id_and_not_a_date(self):
        self.assertTrue(FOUR_ONLY.search(r"re.compile(r'\b[A-Z]-\d{4}\b')"))
        self.assertTrue(FOUR_ONLY.search(r"re.compile(r'([EFSTBDR])-(\d{4})(?![0-9])')"))
        self.assertIsNone(FOUR_ONLY.search(r"re.compile(r'^\d{4}-\d\d-\d\d$')"))
        self.assertIsNone(FOUR_ONLY.search(r"re.compile(r'^release-\d{4}-\d\d-\d\d$')"))
        self.assertIsNone(FOUR_ONLY.search(r"re.compile(r'\b[A-Z]-\d{4,}\b')"))


class EveryParserReadsAFiveDigitId(unittest.TestCase):

    def test_core(self):
        from asf.record import core
        self.assertTrue(core.ID_RE.match('T-49891'))
        self.assertTrue(core.ID_RE.match('T-0001'))
        self.assertIsNone(core.ID_RE.match('T-001'))
        self.assertEqual(core.ID_TOKEN_RE.findall('after T-49890 and S-0003'), ['T-49890', 'S-0003'])
        self.assertEqual(core.MENTION_TOKEN_RE.findall('see S-29501.'), ['S-29501'])
        self.assertTrue(core.MENTION_ID_RE.fullmatch('S-29501'))

    def test_plan_tasks_reads_a_five_digit_story(self):
        from asf.record import plan_tasks
        canonical = {'S-29501': {}, 'S-0003': {}}
        self.assertEqual(plan_tasks.stories_of('stories: S-29501, S-0003, S-29999', canonical),
                         ['S-29501', 'S-0003'])

    def test_replan_reads_five_digit_tasks_and_stories(self):
        from asf.record import replan
        doc = replan.parse('replan: F-1144 abcdef123456\n\n'
                           '### Task T-49891: the schema\nstories: S-29501\nafter: none\n\n'
                           '### Task new: more\nafter: T-49891\n\n'
                           '### Drop T-49892: gone\n')
        self.assertEqual(doc['fid'], 'F-1144')
        self.assertEqual(doc['tasks'][0]['id'], 'T-49891')
        self.assertEqual(doc['tasks'][0]['stories'], ['S-29501'])
        self.assertEqual(doc['tasks'][1]['after'], ['T-49891'])
        self.assertEqual(doc['drops'], [('T-49892', 'gone')])

    def test_feeder_merged_into(self):
        from asf.feeder import rows
        self.assertEqual(rows.MERGED_INTO_RE.search('merged into T-49890').group(1), 'T-49890')

    def test_groom_answer_lines(self):
        from asf.groom import digest, groom, inbox, policy, sticky
        line = '- [ ] T-49891 a title — why → answer: ____'
        self.assertEqual(groom.ANSWER_LINE_RE.match(line).group('id'), 'T-49891')
        self.assertEqual(groom.ANSWER_UNBLOCK.match('unblock T-49891').group(1), 'T-49891')
        self.assertTrue(groom._ID_RE.match('S-29501'))
        self.assertEqual(groom._LINE_ID_RE.match(line).group(1), 'T-49891')
        self.assertEqual(digest.ANSWER_LINE_RE.match(line).group('id'), 'T-49891')
        self.assertEqual(digest.NEEDS_OPERATOR_ID_RE.match('NEEDS OPERATOR: T-49891 x').group('id'),
                         'T-49891')
        self.assertEqual(policy.OPEN_QUESTION_RE.match(line).group('id'), 'T-49891')
        self.assertEqual(policy._SUPPRESSABLE_RE.match(line).group('id'), 'T-49891')
        self.assertTrue(policy._CARD_PATH_RE.search('backlog/tasks/T-49891.md'))
        self.assertEqual(sticky._CARD_LINE_RE.match(line).group('id'), 'T-49891')
        self.assertEqual(sticky._LINE_ID_RE.match(line).group(1), 'T-49891')
        parent = [c for c in inbox._CLAUSES if c[2] == 'parent <id>'][0][0]
        self.assertEqual(parent.match('parent F-12345').group(1), 'F-12345')

    def test_the_rest(self):
        from asf import approvals, version
        from asf.metrics import metrics
        from asf.roles import roles
        from asf.scorecard import facts
        from asf.tick import file_bugs
        self.assertEqual(facts.ID_RE.search('fixes t-49891 now').group(2), '49891')
        self.assertEqual(metrics.COMMIT_ID_RE.findall('T-49891: x'), ['T-49891'])
        self.assertEqual(file_bugs._ITEM_ID_RE.findall('on T-49891'), ['T-49891'])
        self.assertTrue(re.search(roles.INCIDENT, 'seen on B-12345'))
        self.assertEqual(version.PREFIX_RE.sub('', 'T-49891 — the change'), 'the change')
        pat = [p for p in approvals._COMMAND_PATTERNS['touch_amendable_set'] if 'R-' in p][0]
        self.assertTrue(re.search(pat, 'asf set R-10001 x=y'))

    def test_invariants_reads_a_five_digit_line(self):
        import inspect
        from asf import invariants
        self.assertIn("rf'^- \\[?([A-Z]-{ID_DIGITS})\\b'", inspect.getsource(invariants))


if __name__ == '__main__':
    unittest.main()
