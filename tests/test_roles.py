"""asf.roles: the role files, the nine refusals over them, and the one binding table.

Every shipped file is read the way a session would meet it — frontmatter of exactly ``name`` and
``purpose``, five sections in order, doctrine a bullet per incident — and each refusal is produced
by exactly one mutation of a valid file in a temporary roles dir (``$ASF_ROLES_DIR``), so a
validator that stops refusing is a red test, not a quiet one.
"""
import hashlib
import importlib
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from asf.roles import roles

# ``asf.briefs.build`` is both the package's entry function and a submodule; the function wins
# the attribute lookup, so the module is asked for by name.
build_mod = importlib.import_module('asf.briefs.build')

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
PATTERNS_FILE = os.path.join(REPO_ROOT, 'tools', 'forbidden-names.txt')

PURPOSES = {
    'asf-writer': 'the document is the requirement, executable without asking anyone anything',
    'asf-reviewer': 'one finding per acceptance criterion, each with the evidence for it',
    'asf-coder': 'implement the plan inside the declared boundary, and nothing beside it',
    'asf-fixer': 'close the binding list exactly as it is written, and widen nothing',
    'asf-diagnostician': 'build the red loop first; a hypothesis carries its prediction',
    'asf-harvester': 'what landed, what did not, and the one line that says why',
    'asf-interrogator': 'rule on the open question and record what closed it',
    'asf-prober': 'the state of what is running, reported as it is, absence reported as absence',
    'asf-security': 'attack the guarantee before a user does',
    'asf-documenter': 'the surface a reader meets, kept true to the code under it',
    'asf-locator': 'exact file:line locations with a short excerpt, never a whole file',
    'asf-builder': 'take one card from requirement to pushed code in a single session, no wider '
                   'than the card',
}

VALID = """---
name: demo
purpose: keep a small thing true
---

## Identity

You are the demo role, and you exist so a test has something to mutate.

## Doctrine

- The first rule cites the first incident (B-0051).
- The second rule cites another, and runs onto a second line so a wrapped bullet is
  still one bullet (B-0062).
- The third rule cites a prior-art finding (b109).
- The fourth rule cites a decision (D-0025).
- The fifth rule cites a bug (B-0076).

## Output

The named side file, and only it.

## Economy

Read what the brief names. Do not survey.

## Boundaries

You write the one file and nothing else.
"""


def doctrine_bullets(role):
    """The doctrine's bullets, each with its wrapped lines — counted here, not by the loader."""
    bullets = []
    for line in role.sections['Doctrine'].split('\n'):
        if line.startswith('- '):
            bullets.append(line)
        elif bullets and line.strip():
            bullets[-1] += ' ' + line.strip()
    return bullets


def forbidden_regex():
    pats = []
    with open(PATTERNS_FILE, encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if line:
                pats.append(line)
    return re.compile('|'.join(pats), re.IGNORECASE)


class RolesDirCase(unittest.TestCase):
    """A temporary roles dir the loader is pointed at through ``$ASF_ROLES_DIR``."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='asf-roles-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.dict(os.environ, {'ASF_ROLES_DIR': self.dir})
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, text, name='demo'):
        with open(os.path.join(self.dir, name + '.md'), 'w', encoding='utf-8') as f:
            f.write(text)
        return roles.load(name)

    def problems(self, text, name='demo'):
        return roles.validate(self.put(text, name))


class ShapeTests(unittest.TestCase):
    """Every shipped file has the shape §2.1 fixes."""

    def test_every_shipped_file_is_valid(self):
        loaded = roles.load_all()
        self.assertTrue(loaded)
        for name, role in loaded.items():
            with self.subTest(role=name):
                self.assertEqual(roles.validate(role), [])

    def test_frontmatter_is_exactly_name_and_purpose(self):
        for name, role in roles.load_all().items():
            with self.subTest(role=name):
                self.assertEqual(sorted(role.keys), ['name', 'purpose'])
                self.assertEqual(role.name, name)
                self.assertTrue(role.purpose)

    def test_five_sections_in_order(self):
        for name, role in roles.load_all().items():
            with self.subTest(role=name):
                self.assertEqual(roles.SECTIONS,
                                 ('Identity', 'Doctrine', 'Output', 'Economy', 'Boundaries'))
                self.assertEqual(list(role.headings), list(roles.SECTIONS))
                self.assertEqual(list(role.sections), list(roles.SECTIONS))

    def test_the_file_is_at_or_under_sixty_lines(self):
        self.assertEqual(roles.MAX_LINES, 60)
        for name, role in roles.load_all().items():
            with self.subTest(role=name):
                with open(role.path, encoding='utf-8') as f:
                    self.assertLessEqual(len(f.read().splitlines()), 60)
                self.assertLessEqual(role.lines, 60)

    def test_doctrine_is_four_to_eight_bullets_each_citing_an_incident(self):
        for name, role in roles.load_all().items():
            bullets = doctrine_bullets(role)
            with self.subTest(role=name):
                self.assertGreaterEqual(len(bullets), roles.DOCTRINE_MIN)
                self.assertLessEqual(len(bullets), roles.DOCTRINE_MAX)
                for bullet in bullets:
                    self.assertRegex(bullet, roles.INCIDENT)

    def test_economy_is_at_or_under_six_lines(self):
        self.assertEqual(roles.ECONOMY_MAX_LINES, 6)
        for name, role in roles.load_all().items():
            with self.subTest(role=name):
                lines = [l for l in role.sections['Economy'].split('\n') if l.strip()]
                self.assertLessEqual(len(lines), 6)

    def test_output_never_restates_the_report_envelope(self):
        for name, role in roles.load_all().items():
            with self.subTest(role=name):
                self.assertNotIn('status:', role.sections['Output'])
                self.assertNotIn('REPORT', role.sections['Output'].split('\n'))

    def test_the_sha_is_over_the_body_only(self):
        role = roles.load('asf-coder')
        self.assertEqual(role.sha, hashlib.sha256(role.text.encode('utf-8')).hexdigest()[:12])
        self.assertEqual(len(role.sha), 12)
        with open(role.path, encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('purpose', role.text)
        self.assertTrue(raw.endswith(role.text))

    def test_the_directory_is_the_packages_own_unless_overridden(self):
        env = {k: v for k, v in os.environ.items() if k != 'ASF_ROLES_DIR'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(roles.roles_dir(), roles.ROLES_DIR)
        with mock.patch.dict(os.environ, {'ASF_ROLES_DIR': '/nowhere'}):
            self.assertEqual(roles.roles_dir(), '/nowhere')


class ValidateTests(RolesDirCase):
    """Each refusal is produced by exactly one mutation of a valid file, and names itself."""

    def one(self, text, label):
        problems = self.problems(text)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(label), problems[0])
        return problems[0]

    def test_a_valid_file_has_no_problem(self):
        self.assertEqual(self.problems(VALID), [])

    def test_1_frontmatter_missing_a_key(self):
        self.one(VALID.replace('purpose: keep a small thing true\n', ''), 'frontmatter: missing')
        self.one(VALID.replace('name: demo\n', ''), 'frontmatter: missing')

    def test_1_frontmatter_name_is_not_the_stem(self):
        self.one(VALID.replace('name: demo', 'name: other'), 'frontmatter: name')

    def test_2_frontmatter_carries_a_key_beyond_the_two(self):
        self.one(VALID.replace('---\n\n## Identity', 'owner: someone\n---\n\n## Identity'),
                 'frontmatter: unknown key')

    def test_3_a_section_is_missing(self):
        text = VALID.split('## Boundaries')[0]
        self.one(text, 'sections: missing Boundaries')

    def test_3_a_section_is_out_of_order(self):
        head, rest = VALID.split('## Output')
        output, rest = rest.split('## Economy')
        economy, boundaries = rest.split('## Boundaries')
        text = f'{head}## Economy{economy}## Output{output}## Boundaries{boundaries}'
        self.one(text, 'sections: out of order')

    def test_3_a_heading_is_not_one_of_the_five(self):
        self.one(VALID.replace('## Boundaries', '## Limits'), 'sections:')

    def test_4_the_file_is_over_its_cap(self):
        padded = VALID + '\n'.join(['Keep to the one file.'] * 60) + '\n'
        self.one(padded, 'lines:')

    def test_5_doctrine_has_too_few_bullets(self):
        text = VALID.replace('- The fourth rule cites a decision (D-0025).\n', '') \
                    .replace('- The fifth rule cites a bug (B-0076).\n', '')
        self.one(text, 'doctrine:')

    def test_5_doctrine_has_too_many_bullets(self):
        extra = ''.join(f'- Another rule, cited (B-00{n}).\n' for n in range(10, 14))
        self.one(VALID.replace('\n## Output', extra + '\n## Output'), 'doctrine:')

    def test_6_a_doctrine_bullet_cites_no_incident(self):
        problem = self.one(VALID.replace('(b109)', ''), 'incident:')
        self.assertIn('third rule', problem)

    def test_7_economy_is_over_its_cap(self):
        extra = ''.join(f'Read one more thing, number {n}.\n' for n in range(6))
        self.one(VALID.replace('Do not survey.\n', 'Do not survey.\n' + extra), 'economy:')

    def test_8_a_banned_word_is_anywhere_in_the_file(self):
        for word in roles.forbidden():
            for text in (VALID.replace('mutate.', f'mutate, {word.upper()}.'),
                         VALID.replace('keep a small thing true', f'keep a {word} true')):
                with self.subTest(word=word):
                    self.one(text, 'forbidden:')

    def test_9_output_carries_a_report_contract(self):
        self.one(VALID.replace('and only it.', 'and only it.\n\nREPORT'), 'output:')
        fenced = 'and only it.\n\n```\nstatus: done\n```'
        self.one(VALID.replace('and only it.', fenced), 'output:')

    def test_the_nine_problems_are_reported_in_the_order_of_the_table(self):
        text = (VALID.replace('name: demo', 'name: other')
                .replace('---\n\n## Identity', 'owner: someone\n---\n\n## Identity')
                .replace('## Boundaries', '## Limits')
                .replace('(b109)', '')
                .replace('Do not survey.\n', 'Do not survey.\n' + 'More reading.\n' * 6)
                .replace('and only it.', 'and only it.\nREPORT'))
        text += '\n'.join(['Keep to the one file.'] * 60) + '\nA model is banned.\n'
        text = text.replace('- The fourth rule cites a decision (D-0025).\n', '') \
                   .replace('- The fifth rule cites a bug (B-0076).\n', '')
        labels = [p.split(':')[0] for p in self.problems(text)]
        self.assertEqual(labels, ['frontmatter', 'frontmatter', 'sections', 'lines', 'doctrine',
                                  'incident', 'economy', 'forbidden', 'output'])

    def test_the_incident_pattern_reads_as_the_spec_meant_it(self):
        self.assertTrue(re.search(roles.INCIDENT, 'B-0051'))
        self.assertTrue(re.search(roles.INCIDENT, 'b60'))
        self.assertTrue(re.search(roles.INCIDENT, '(B-0051)'))
        self.assertFalse(re.search(roles.INCIDENT, 'B-51'))
        self.assertFalse(re.search(roles.INCIDENT, 'b1234'))
        self.assertFalse(re.search(roles.INCIDENT, 'a bug'))

    def test_a_file_with_no_frontmatter_cannot_be_read(self):
        with open(os.path.join(self.dir, 'demo.md'), 'w', encoding='utf-8') as f:
            f.write('## Identity\n')
        with self.assertRaises(roles.RoleError):
            roles.load('demo')


class BlockTests(RolesDirCase):
    """The brief's block: the body under one heading, cut by whole sections, last first."""

    def test_a_block_that_fits_is_the_body_under_the_heading(self):
        out = roles.block(self.put(VALID), 60)
        self.assertTrue(out.startswith('## Who you are — demo\n\n## Identity'))
        self.assertIn('## Boundaries', out)
        self.assertNotIn('truncated', out)
        self.assertNotIn('purpose:', out)

    def test_a_block_over_its_cap_is_cut_on_a_section_boundary(self):
        out = roles.block(self.put(VALID), 19)
        lines = out.split('\n')
        self.assertLessEqual(len(lines), 19)
        self.assertEqual(lines[-1], '… (role demo truncated at 19 lines)')
        self.assertIn('## Doctrine', out)
        self.assertNotIn('## Output', out)
        self.assertEqual(lines[-2], '')

    def test_the_last_section_goes_first(self):
        kept = [h for h in roles.SECTIONS
                if f'## {h}' in roles.block(self.put(VALID), 24)]
        self.assertEqual(kept, list(roles.SECTIONS[:len(kept)]))
        self.assertLess(len(kept), len(roles.SECTIONS))

    def test_every_shipped_role_fits_the_default_cap_whole(self):
        for name, role in roles.load_all().items():
            with self.subTest(role=name):
                self.assertNotIn('truncated', roles.block(role, 60))


class NoModelTests(RolesDirCase):
    """No role names a model, a backend, an effort or a label."""

    def test_the_banned_words_are_the_spec_s_six(self):
        self.assertEqual(roles.forbidden(),
                         ('model', 'backend', 'effort', 'tools', build_mod.HEAVY, build_mod.LIGHT))

    def test_no_shipped_file_says_any_of_them(self):
        for name, role in roles.load_all().items():
            lowered = role.raw.lower()
            for word in roles.forbidden():
                with self.subTest(role=name, word=word):
                    self.assertNotIn(word.lower(), lowered)

    def test_a_model_in_the_frontmatter_is_refused_twice(self):
        problems = self.problems(VALID.replace('---\n\n## Identity',
                                               'model: heavy\n---\n\n## Identity'))
        self.assertTrue(any(p.startswith('frontmatter: unknown key model') for p in problems),
                        problems)
        self.assertTrue(any(p.startswith('forbidden:') for p in problems), problems)


class GenericTests(unittest.TestCase):
    """The repository stays generic: no role file carries a name the public list forbids."""

    def test_no_role_file_carries_a_forbidden_name(self):
        pattern = forbidden_regex()
        for name, role in roles.load_all().items():
            with self.subTest(role=name):
                self.assertIsNone(pattern.search(role.raw), pattern.search(role.raw))


class BindingTests(unittest.TestCase):
    """Ten files, every kind bound, every role accounted for."""

    def test_the_roles_of_the_spec_ship(self):
        self.assertEqual(set(roles.load_all()), set(PURPOSES))

    def test_every_purpose_is_the_specs_verbatim(self):
        for name, role in roles.load_all().items():
            with self.subTest(role=name):
                self.assertEqual(role.purpose, PURPOSES[name])

    def test_every_kind_is_bound_and_no_more(self):
        self.assertEqual(set(roles.BINDINGS), set(build_mod.KINDS))

    def test_every_bound_role_has_a_file(self):
        loaded = roles.load_all()
        for kind, name in roles.BINDINGS.items():
            with self.subTest(kind=kind):
                self.assertIn(name, loaded)

    def test_every_file_is_bound_or_unbound_with_a_reason(self):
        bound = set(roles.BINDINGS.values())
        for name in roles.load_all():
            with self.subTest(role=name):
                if name in bound:
                    self.assertNotIn(name, roles.UNBOUND)
                else:
                    self.assertTrue(roles.UNBOUND.get(name, '').strip())
        self.assertEqual(set(roles.UNBOUND),
                         {'asf-prober', 'asf-security', 'asf-documenter', 'asf-locator'})
        self.assertEqual(set(roles.load_all()), bound | set(roles.UNBOUND))

    def test_the_bindings_keep_the_labels_of_today(self):
        # D6: identity moves nothing about cost — the role a kind runs under is the same set the
        # per-kind label table already groups.
        # The one exception, by operator policy 2026-09-27: adjudicate runs light (S1 heavy) while
        # groom, its role-mate, stays heavy — a ruling over a held branch is not a groom pass.
        moved = {'adjudicate'}
        heavy = {k for k, v in build_mod.DEFAULT_MODELS.items() if v == build_mod.HEAVY}
        by_role = {}
        for kind, name in roles.BINDINGS.items():
            if kind not in moved:
                by_role.setdefault(name, set()).add(kind in heavy)
        for name, labels in by_role.items():
            with self.subTest(role=name):
                self.assertEqual(len(labels), 1)

    def test_for_kind_reads_the_table_and_refuses_the_unknown(self):
        self.assertEqual(roles.for_kind('review'), 'asf-reviewer')
        self.assertEqual(roles.for_kind('adjudicate'), 'asf-interrogator')
        with self.assertRaises(roles.RoleError):
            roles.for_kind('preflight')

    def test_load_refuses_a_role_with_no_file(self):
        with self.assertRaises(roles.RoleError):
            roles.load('standards')


class AliasTests(unittest.TestCase):
    """A bare pre-rename name (``coder``) is an alias for its ``asf-`` file, for every shipped
    role — the rename must not break a caller still holding the old name."""

    def test_every_shipped_role_has_a_bare_alias(self):
        loaded = roles.load_all()
        self.assertEqual(set(roles.ALIASES.values()), set(loaded))
        for old, new in roles.ALIASES.items():
            with self.subTest(old=old):
                self.assertEqual(new, roles.ROLE_PREFIX + old)

    def test_load_of_the_bare_name_is_load_of_the_prefixed_one(self):
        for old, new in roles.ALIASES.items():
            with self.subTest(old=old):
                self.assertEqual(roles.load(old), roles.load(new))

    def test_alias_note_names_the_old_and_the_new(self):
        self.assertEqual(roles.alias_note('coder'), "role 'coder' is now 'asf-coder'")
        self.assertIsNone(roles.alias_note('asf-coder'))
        self.assertIsNone(roles.alias_note('no-such-role'))

    def test_every_file_stem_starts_with_the_prefix(self):
        for name in roles.load_all():
            with self.subTest(role=name):
                self.assertTrue(name.startswith(roles.ROLE_PREFIX), name)

    def test_every_binding_points_at_an_existing_file(self):
        loaded = roles.load_all()
        for kind, name in roles.BINDINGS.items():
            with self.subTest(kind=kind):
                self.assertIn(name, loaded)
                self.assertTrue(name.startswith(roles.ROLE_PREFIX))


if __name__ == '__main__':
    unittest.main()
