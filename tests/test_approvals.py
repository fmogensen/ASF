"""tests.test_approvals — F-0031. Task 1: the catalogue, the matrix, the classifier and the hold
ledger — ``CatalogueTest`` and ``MatrixTest`` are the plan's A1, ``LedgerTest`` the unit coverage
behind D9/D14. Task 2: ``HookTest``, the unit half of A2 (the end-to-end block is §3's, run by
path with the session's identity removed — PD7).
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import approvals, env, hooks
from asf.env import Product

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _product(name='demo', **data):
    return Product(name, data)


class CatalogueTest(unittest.TestCase):
    def test_every_class_is_named_and_mapped_in_the_example_and_sample(self):
        names = {c.name for c in approvals.CLASSES}
        for path in (
            os.path.join(REPO_ROOT, 'docs', 'products.example.yaml'),
            os.path.join(REPO_ROOT, 'sample', 'product.yaml'),
        ):
            with open(path, encoding='utf-8') as f:
                data = env.loads(f.read())
            product = Product('x', data)
            self.assertEqual(set(product.approvals.keys()), names, path)
            for level in product.approvals.values():
                self.assertIn(level, approvals.LEVELS, path)

    def test_every_class_has_a_read_point_and_a_known_default(self):
        known_readers = {'hook', 'harvest', 'file_bugs'}
        for c in approvals.CLASSES:
            self.assertIn(c.default, approvals.LEVELS, c.name)
            self.assertTrue(c.read_by, c.name)
            self.assertTrue(set(c.read_by) <= known_readers, c.name)


class MatrixTest(unittest.TestCase):
    def test_unmapped_class_takes_its_catalogue_default(self):
        product = _product(approvals={'merge_routine_pr': 'groom'})
        m = approvals.matrix(product)
        self.assertEqual(m['merge_routine_pr'], ('groom', 'yaml'))
        for c in approvals.CLASSES:
            if c.name == 'merge_routine_pr':
                continue
            self.assertEqual(m[c.name], (c.default, 'default'), c.name)

    def test_unknown_class_or_level_is_a_config_error(self):
        with self.assertRaises(env.ConfigError):
            approvals.matrix(_product(approvals={'frobnicate': 'auto'}))
        with self.assertRaises(env.ConfigError):
            approvals.matrix(_product(approvals={'touch_production': 'maybe'}))

    def test_signal_under_an_unknown_class_is_a_config_error(self):
        with self.assertRaises(env.ConfigError):
            approvals.signals(_product(approval_signals={'not_a_class': {'paths': ['x']}}))
        with self.assertRaises(env.ConfigError):
            approvals.signals(_product(approval_signals={'spend_money': {'globs': ['x']}}))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = 'demo'

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _refuse(self):
        return approvals.refuse(
            self.product, 'F-0031', 'touch_production', 'human-now', 'code-F-0031',
            'hook', 'git push origin HEAD:main')

    def test_refuse_opens_a_hold(self):
        hold = self._refuse()
        self.assertEqual(hold, 'F-0031/touch_production')
        self.assertEqual([h['hold'] for h in approvals.open_holds(self.product)], [hold])
        self.assertFalse(approvals.is_granted(self.product, hold))
        entry = approvals.holds(self.product)[hold]
        self.assertEqual(entry['item'], 'F-0031')
        self.assertEqual(entry['class'], 'touch_production')
        self.assertEqual(entry['count'], 1)
        self.assertIsNone(entry['resolution'])

    def test_resolve_closes_the_hold(self):
        hold = self._refuse()
        approvals.resolve(self.product, hold, 'granted')
        self.assertEqual(approvals.open_holds(self.product), [])
        self.assertTrue(approvals.is_granted(self.product, hold))

    def test_repeat_refusal_bumps_count(self):
        hold = self._refuse()
        self._refuse()
        self.assertEqual(approvals.holds(self.product)[hold]['count'], 2)

    def test_resolve_of_an_invalid_resolution_is_a_value_error(self):
        hold = self._refuse()
        with self.assertRaises(ValueError):
            approvals.resolve(self.product, hold, 'maybe')

    def test_a_malformed_line_is_skipped(self):
        hold = self._refuse()
        with open(approvals.ledger_path(self.product), 'a', encoding='utf-8') as f:
            f.write('not json\n')
        self.assertEqual([h['hold'] for h in approvals.open_holds(self.product)], [hold])


class HookTest(unittest.TestCase):
    """§3 A2's unit cases: ``approvals.run_hook`` on a temp ``ASF_HOME``, each call given its own
    explicit ``environ`` — the session running this suite carries an ``ASF_JOB`` and an
    ``ASF_PRODUCT`` of its own, and none of them may leak in (PD7)."""

    ITEM = 'F-0031'
    JOB = 'code-F-0031'
    ALL_HUMAN_NOW = 'approvals:\n' + ''.join(f'  {c.name}: human-now\n' for c in approvals.CLASSES)
    ALL_AUTO = 'approvals:\n' + ''.join(f'  {c.name}: auto\n' for c in approvals.CLASSES)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        os.makedirs(os.path.join(env.ASF_HOME, 'state', 'demo'))
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        self.write_product('approvals:\n  touch_production: human-now\n')
        with open(os.path.join(env.ASF_HOME, 'state', 'demo', 'sessions.jsonl'), 'w') as f:
            f.write(json.dumps({
                'job': self.JOB, 'item': self.ITEM, 'branch': 'worker/f-0031',
                'started': '2026-09-22T00:00:00Z', 'pid': 1,
            }) + '\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_product(self, extra=''):
        with open(env.product_path('demo'), 'w') as f:
            f.write(f'product: demo\nmain: main\n{extra}')

    def call(self, tool_name, tool_input, cwd=None, job=JOB, product='demo'):
        """``(rc, what the runtime would feed back to the model)``."""
        environ = {}
        if job:
            environ['ASF_JOB'] = job
        if product:
            environ['ASF_PRODUCT'] = product
        call = json.dumps({
            'tool_name': tool_name, 'tool_input': tool_input, 'cwd': cwd or self.repo})
        out = io.StringIO()
        rc = approvals.run_hook(call, environ, out=out)
        return rc, out.getvalue()

    def push(self, **kw):
        return self.call('Bash', {'command': 'git push origin HEAD:main'}, **kw)

    def ledger(self):
        return approvals.read('demo')

    def assertNoLedger(self):
        self.assertFalse(os.path.isfile(approvals.ledger_path('demo')), 'the ledger was written')

    def record_repo(self):
        """A record repo (``index.json`` at its root) — what ``new_epic`` recognises a new
        ``epics/*.md`` in."""
        root = os.path.join(self.tmp, 'record')
        os.makedirs(os.path.join(root, 'epics'))
        with open(os.path.join(root, 'index.json'), 'w') as f:
            f.write('{}\n')
        subprocess.run(['git', 'init', '-q', '-b', 'main', root], check=True)
        return root

    def test_each_builtin_recogniser_refuses_under_human_now(self):
        self.write_product('deploy_sha:\n  workflow: deploy.yml\n' + self.ALL_HUMAN_NOW)
        record = self.record_repo()
        cases = [
            ('touch_production', 'Bash', {'command': 'git push origin HEAD:main'}, None),
            ('touch_production', 'Bash', {'command': 'gh workflow run deploy.yml'}, None),
            ('touch_security', 'Write', {'file_path': os.path.join(self.repo, '.env')}, None),
            ('touch_security', 'Write',
             {'file_path': os.path.join(self.repo, hooks.RUNTIME_SETTINGS_GLOBS[0])}, None),
            ('touch_security', 'Bash', {'command': 'git commit --no-verify -m wip'}, None),
            ('touch_legal', 'Edit', {'file_path': os.path.join(self.repo, 'LICENSE')}, None),
            ('new_epic', 'Write',
             {'file_path': os.path.join(record, 'epics', 'E-0002.md')}, record),
            ('new_epic', 'Bash', {'command': 'asf new epic "a new epic"'}, None),
            ('file_bug', 'Bash', {'command': 'asf new bug "a bug"'}, None),
        ]
        for cls, tool_name, tool_input, cwd in cases:
            with self.subTest(cls=cls, tool=tool_name, input=tool_input):
                rc, out = self.call(tool_name, tool_input, cwd=cwd)
                self.assertEqual(rc, 2, out)
                self.assertIn(f'REFUSED {cls} (human-now) on {self.ITEM} — ', out)
                self.assertIn(
                    f'asf approvals resolve {self.ITEM}/{cls} granted|done|dropped', out)

    def test_signals_extend_a_class(self):
        target = {'file_path': os.path.join(self.repo, 'data', 'customers', 'list.csv')}
        self.write_product('approvals:\n  touch_customer_data: human-now\n')
        rc, out = self.call('Write', target)
        self.assertEqual((rc, out), (0, ''))

        self.write_product(
            'approvals:\n  touch_customer_data: human-now\n'
            "approval_signals:\n  touch_customer_data:\n    paths: ['data/customers/*']\n")
        rc, out = self.call('Write', target)
        self.assertEqual(rc, 2, out)
        self.assertIn(f'REFUSED touch_customer_data (human-now) on {self.ITEM} —', out)

    def test_auto_passes_and_writes_nothing(self):
        self.write_product(self.ALL_AUTO)
        self.assertEqual(self.push(), (0, ''))
        self.assertNoLedger()

    def test_groom_refuses_and_holds(self):
        self.write_product('approvals:\n  touch_production: groom\n')
        rc, out = self.push()
        self.assertEqual(rc, 2, out)
        self.assertIn(f'REFUSED touch_production (groom) on {self.ITEM} —', out)
        self.assertEqual([
            (r['event'], r['hold'], r['level'], r['job'], r['tool']) for r in self.ledger()
        ], [('refused', f'{self.ITEM}/touch_production', 'groom', self.JOB, 'Bash')])

    def test_granted_hold_passes(self):
        hold = f'{self.ITEM}/touch_production'
        self.assertEqual(self.push()[0], 2)
        approvals.resolve('demo', hold, 'granted')
        self.assertEqual(self.push(), (0, ''))
        self.assertEqual(approvals.holds('demo')[hold]['count'], 1)

    def test_no_job_means_no_enforcement(self):
        self.assertEqual(self.push(job=None), (0, ''))
        self.assertNoLedger()

    def test_operator_config_write_is_always_refused(self):
        self.write_product(self.ALL_AUTO)
        refusal = 'REFUSED operator config — the factory never edits its own authority'
        for tool_name, tool_input in (
            ('Write', {'file_path': env.product_path('demo')}),
            ('Edit', {'file_path': env.config_path()}),
            ('Bash', {'command': f"sed -i '' s/auto/human-now/ {env.product_path('demo')}"}),
            ('Bash', {'command': f'rm {env.config_path()}'}),
        ):
            with self.subTest(tool=tool_name, input=tool_input):
                rc, out = self.call(tool_name, tool_input)
                self.assertEqual(rc, 2, out)
                self.assertIn(refusal, out)
        # reading it is not writing it, and D8 is not a class: nothing is held either way
        self.assertEqual(self.call('Bash', {'command': f'cat {env.product_path("demo")}'}), (0, ''))
        self.assertNoLedger()

    def test_invalid_matrix_refuses_classified_actions_only(self):
        self.write_product('approvals:\n  touch_production: maybe\n')
        rc, out = self.push()
        self.assertEqual(rc, 2, out)
        self.assertIn('the matrix is invalid', out)
        self.assertIn('maybe', out)
        self.assertIn(f'REFUSED touch_production (human-now) on {self.ITEM} —', out)
        self.assertEqual([r['level'] for r in self.ledger()], ['human-now'])

        self.assertEqual(self.call('Bash', {'command': 'ls'}), (0, ''))

    def test_classifier_exception_fails_closed(self):
        with mock.patch.object(approvals, 'classify', side_effect=RuntimeError('boom')):
            rc, out = self.push()
        self.assertEqual(rc, 2, out)
        self.assertEqual(out, 'approvals hook failed: boom — refused\n')


if __name__ == '__main__':
    unittest.main()
