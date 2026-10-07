"""The opt-in factory-only merge rule (``conventions.merge.factory_only``, default false)."""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

from asf import env, factory_only, hermetic
from asf.conventions import Conventions
from asf.env import Product

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK_SCRIPT = os.path.join(REPO, 'tools', 'check_factory_only.sh')

PREFIXES = {'code': 'worker/', 'fix': 'fix/', 'spec': 'spec/', 'plan': 'plan/',
            'legacy': ['old/']}


def conv(merge=None):
    data = {'branch_prefixes': PREFIXES}
    if merge is not None:
        data['merge'] = merge
    return Conventions.from_mapping(data)


ON = conv({'mode': 'auto', 'factory_only': True})
ON_ID = conv({'mode': 'auto', 'factory_only': True, 'require_item_id': True})


class VerdictTests(unittest.TestCase):

    def test_off_by_default_and_a_word_merge_keeps_it_off(self):
        for c in (conv(), conv('auto'), conv({'mode': 'queue'})):
            self.assertFalse(c.merge_factory_only)
            self.assertTrue(factory_only.verdict(c, 'main', 'hand/fix', 'main', ['a.py'])[0])

    def test_the_map_keeps_its_mode(self):
        self.assertEqual(ON.merge, 'auto')
        self.assertTrue(ON.merge_auto())
        self.assertEqual(conv({'factory_only': True}).merge, 'manual')
        self.assertEqual(ON.shape_findings(), [])

    def test_on_a_hand_branch_into_the_trunk_is_refused(self):
        ok, why = factory_only.verdict(ON, 'main', 'hand/fix', 'main', ['src/a.py'])
        self.assertFalse(ok)
        self.assertIn('hand/fix is not a factory branch', why)
        self.assertIn('worker/', why)

    def test_on_every_factory_prefix_passes_legacy_included(self):
        for head in ('worker/T-0001', 'fix/B-0001', 'spec/F-0001', 'plan/F-0001',
                     'cloud/direct-F-0001', 'old/T-0001'):
            with self.subTest(head=head):
                self.assertTrue(factory_only.verdict(ON, 'main', head, 'main', ['a.py'])[0])

    def test_on_the_escapes_release_tags_and_bot_paths(self):
        self.assertTrue(factory_only.verdict(ON, 'main', '', '', [], 'refs/tags/v0.1.133')[0])
        self.assertTrue(factory_only.verdict(ON, 'main', 'release-bot', 'main',
                                             ['CHANGELOG.md'])[0])
        self.assertFalse(factory_only.verdict(ON, 'main', 'release-bot', 'main',
                                              ['CHANGELOG.md', 'src/a.py'])[0])
        custom = conv({'factory_only': True, 'bot_paths': ['CHANGELOG.md', 'docs/release/*']})
        self.assertTrue(factory_only.verdict(custom, 'main', 'bot', 'main',
                                             ['docs/release/v1.md'])[0])

    def test_on_a_pr_into_another_branch_passes(self):
        self.assertTrue(factory_only.verdict(ON, 'main', 'hand/fix', 'next', ['a.py'])[0])

    def test_require_item_id_refuses_a_factory_branch_with_no_item_id(self):
        cases = (('fix/typo', 'fix'), ('spec/wip', 'spec'), ('worker/', 'code'),
                 ('cloud/direct-', 'direct'))
        for head, kind in cases:
            with self.subTest(head=head):
                ok, why = factory_only.verdict(ON_ID, 'main', head, 'main', ['src/a.py'])
                self.assertFalse(ok)
                self.assertIn('merge.require_item_id', why)
                self.assertIn(f'the {kind} lane', why)

    def test_require_item_id_passes_a_branch_named_after_an_item(self):
        for head in ('worker/T-0001', 'fix/B-0428', 'plan/F-0097-replan', 'worker/T-58250',
                     'cloud/direct-F-0040', 'old/T-0001'):
            with self.subTest(head=head):
                self.assertTrue(factory_only.verdict(ON_ID, 'main', head, 'main', ['a.py'])[0])

    def test_require_item_id_off_lets_every_unnamed_factory_branch_through(self):
        for head in ('fix/typo', 'spec/wip', 'worker/', 'cloud/direct-'):
            with self.subTest(head=head):
                self.assertTrue(factory_only.verdict(ON, 'main', head, 'main', ['src/a.py'])[0])

    def test_require_item_id_still_reaches_the_bot_paths_escape(self):
        self.assertTrue(factory_only.verdict(ON_ID, 'main', 'fix/typo', 'main',
                                             ['CHANGELOG.md'])[0])

    def test_on_id_keeps_its_map_shape(self):
        self.assertEqual(ON_ID.shape_findings(), [])


class CliTests(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def product_file(self, merge):
        path = os.path.join(self.d, 'sample.yaml')
        with open(path, 'w') as f:
            f.write('main: main\nconventions:\n  branch_prefixes:\n    code: worker/\n'
                    f'  merge: {merge}\n')
        return path

    def run_cli(self, merge, head):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = factory_only.main(['--product-file', self.product_file(merge), '--head', head,
                                    '--base', 'main', '--ref', 'refs/pull/1/merge',
                                    '--files', 'src/a.py'])
        return rc, out.getvalue()

    def test_on_refuses_with_exit_1(self):
        rc, out = self.run_cli('{mode: auto, factory_only: true}', 'hand/fix')
        self.assertEqual(rc, 1)
        self.assertIn('refused', out)

    def test_on_passes_a_factory_branch(self):
        self.assertEqual(self.run_cli('{mode: auto, factory_only: true}', 'worker/T-1')[0], 0)

    def test_off_passes_everything(self):
        rc, out = self.run_cli('auto', 'hand/fix')
        self.assertEqual(rc, 0)
        self.assertIn('off', out)


class CiEntryPoint(unittest.TestCase):
    """``tools/check_factory_only.sh``, run as the CI step itself runs it — against the
    committed ``.asf/product.yaml``, ``--files`` always supplied so no git and no network is
    touched."""

    def run_script(self, args=(), **env_extra):
        base = hermetic.build()
        for var in ('GITHUB_BASE_REF', 'GITHUB_HEAD_REF', 'GITHUB_SHA'):
            base.pop(var, None)
        base.update(env_extra)
        out = subprocess.run(['bash', CHECK_SCRIPT, *args], cwd=REPO, env=base,
                             capture_output=True, text=True)
        return out.returncode, out.stdout + out.stderr

    def test_a_hand_branch_is_refused(self):
        rc, out = self.run_script(['--head', 'feat/round-j', '--base', 'main',
                                   '--files', 'asf/cli.py'])
        self.assertEqual(rc, 1, out)
        self.assertIn('is not a factory branch', out)

    def test_require_item_id_refuses_an_unnamed_factory_branch(self):
        rc, out = self.run_script(['--head', 'fix/exact-path-overlap', '--base', 'main',
                                   '--files', 'asf/cli.py'])
        self.assertEqual(rc, 1, out)
        self.assertIn('merge.require_item_id', out)

    def test_every_committed_prefix_with_an_item_id_passes(self):
        for prefix in ('worker/', 'fix/', 'spec/', 'plan/', 'cloud/direct-'):
            head = f'{prefix}T-0001'
            with self.subTest(head=head):
                rc, out = self.run_script(['--head', head, '--base', 'main', '--files', 'a.py'])
                self.assertEqual(rc, 0, out)
                self.assertIn('is a factory branch', out)

    def test_bot_paths_escape(self):
        rc, out = self.run_script(['--head', 'release-bot', '--base', 'main',
                                   '--files', 'CHANGELOG.md'])
        self.assertEqual(rc, 0, out)
        self.assertIn('bot paths', out)

    def test_bot_paths_escape_needs_every_file_to_match(self):
        rc, out = self.run_script(['--head', 'release-bot', '--base', 'main',
                                   '--files', 'CHANGELOG.md', 'asf/cli.py'])
        self.assertEqual(rc, 1, out)

    def test_a_release_tag_passes(self):
        rc, out = self.run_script(['--ref', 'refs/tags/v0.1.214'])
        self.assertEqual(rc, 0, out)
        self.assertIn('release tag', out)

    def test_a_pr_into_another_branch_passes(self):
        rc, out = self.run_script(['--head', 'feat/x', '--base', 'next', '--files', 'asf/cli.py'])
        self.assertEqual(rc, 0, out)
        self.assertIn('not the trunk', out)

    def test_no_arguments_and_no_pull_request_passes(self):
        rc, out = self.run_script([])
        self.assertEqual(rc, 0, out)
        self.assertIn('not a pull request', out)

    def test_a_missing_product_file_exits_2_never_0(self):
        rc, out = self.run_script([], FACTORY_ONLY_PRODUCT_FILE='/no/such/file.yaml')
        self.assertEqual(rc, 2, out)
        self.assertIn('missing', out)
        self.assertNotEqual(rc, 0)


class CommittedProductFile(unittest.TestCase):
    """``.asf/product.yaml``: generic and committed, read by this repository's own CI."""

    PATH = os.path.join(REPO, '.asf', 'product.yaml')

    def setUp(self):
        with open(self.PATH, encoding='utf-8') as f:
            self.text = f.read()
        self.data = env.load_file(self.PATH)
        self.conventions = Product('asf', self.data).conventions

    def test_well_formed_and_generic(self):
        self.assertEqual(env.product_problems(self.text), ([], []))
        self.assertEqual(env.validate_product_text(self.text), [])

    def test_top_level_keys(self):
        self.assertEqual(set(self.data), {'main', 'conventions'})

    def test_main_is_the_trunk(self):
        self.assertEqual(Product('asf', self.data).main, 'main')

    def test_merge_block(self):
        self.assertTrue(self.conventions.merge_factory_only)
        self.assertTrue(self.conventions.merge_require_item_id)
        self.assertEqual(self.conventions.merge, 'auto')
        self.assertEqual(self.conventions.merge_bot_paths, ['CHANGELOG.md'])
        self.assertEqual(self.conventions.shape_findings(), [])

    def test_branch_prefixes_do_not_drift_from_the_defaults(self):
        self.assertEqual(self.conventions.all_prefixes(),
                         Conventions.from_mapping({}).all_prefixes())


if __name__ == '__main__':
    unittest.main()
