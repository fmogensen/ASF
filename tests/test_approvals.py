"""tests.test_approvals — F-0031. Task 1: the catalogue, the matrix, the classifier and the hold
ledger — ``CatalogueTest`` and ``MatrixTest`` are the plan's A1, ``LedgerTest`` the unit coverage
behind D9/D14. Task 2: ``HookTest``, the unit half of A2 (the end-to-end block is §3's, run by
path with the session's identity removed — PD7). Task 4: ``CliTest`` and ``DoctorTest`` are A5,
the operator's side — ``asf approvals``, ``list``, ``resolve``, and the doctor's ``approvals`` row.
Task 5: ``TickRaiseTest``, A3, on the tick's own harness — the raise, the parking and the
once-only events. Task 6: ``HarvestTest`` and ``FileBugsTest``, A4 — the harvest reads the merge
classes before landing a branch, and the bug filer reads ``file_bug``'s level; both on the
fixtures of ``tests.test_harvest.ProductHarvestTests`` and
``tests.test_file_bugs.FileBugsIntegrationTests``, imported rather than copied.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import approvals, cli, doctor, env, hooks
from asf.env import Product
from asf.feeder import rows as feeder_rows
from asf.tick import file_bugs, step_wave, tick
from tests.test_file_bugs import FileBugsIntegrationTests
from tests.test_harvest import ProductHarvestTests
from tests.test_tick import TickTestCase, _git

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
            # the tenth class's yaml line is the docs task's (F-0024 §5); until it lands the
            # example and sample may omit it, and after it they may carry it
            self.assertEqual(set(product.approvals.keys()) - names, set(), path)
            self.assertLessEqual(names - set(product.approvals.keys()),
                                 {'touch_amendable_set'}, path)
            for level in product.approvals.values():
                self.assertIn(level, approvals.LEVELS, path)

    def test_every_class_has_a_read_point_and_a_known_default(self):
        known_readers = {'hook', 'harvest', 'file_bugs', 'groom'}
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
    # `touch_amendable_set` is unwidenable (§2.2): `auto` is not one of its own `levels`, so it
    # is left unmapped here and takes its catalogue default, `human-now`.
    ALL_AUTO = 'approvals:\n' + ''.join(
        f'  {c.name}: auto\n' for c in approvals.CLASSES if 'auto' in c.levels)

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
        # `amendable_paths: []` opts every one of these paths out of the amendable set (F-0024),
        # which would otherwise intercept `.env`/the runtime settings file/etc before `classify`
        # ever sees them (§2.3, D7) — this loop is about each *other* class's own recogniser.
        self.write_product('deploy_sha:\n  workflow: deploy.yml\n'
                            'conventions:\n  amendable_paths: []\n' + self.ALL_HUMAN_NOW)
        record = self.record_repo()
        cases = [
            ('touch_production', 'Bash', {'command': 'git push origin HEAD:main'}, None),
            ('touch_production', 'Bash', {'command': 'gh workflow run deploy.yml'}, None),
            ('touch_security', 'Write', {'file_path': os.path.join(self.repo, '.env')}, None),
            ('touch_security', 'Write',
             {'file_path': os.path.join(self.repo, hooks.RUNTIME_SETTINGS_GLOBS[0])}, None),
            ('touch_security', 'Bash', {'command': 'git commit --no-verify -m wip'}, None),
            ('touch_security', 'Bash', {'command': 'git config core.hooksPath /dev/null'}, None),
            ('touch_security', 'Bash', {'command': 'git config --unset core.hooksPath'}, None),
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
                # the session is told it may not, and what instead — never to ask a person
                self.assertIn('  You may not do this: ', out)
                self.assertIn('Instead: ', out)
                self.assertNotIn('NEEDS OPERATOR:', out)
                self.assertNotIn('asf approvals resolve', out)

    def test_the_trunk_is_pushed_only_by_a_push_refspec(self):
        from asf.approvals import _pushes_trunk
        cases = [('git commit -m "not on main" && git push origin HEAD:lane/x', False),
                 ('git fetch origin main && git push -u origin HEAD:worker/T-1', False),
                 ('git log origin/main.. ; git push origin lane/x', False),
                 ('git push origin HEAD:main', True), ('git push origin main', True),
                 ('git -C /repo push origin HEAD:main', True),
                 ('git push origin +feat:refs/heads/main', True),
                 # a heredoc body is text, not a command — a review that quotes a push, with
                 # an apostrophe that leaves the body's quotes unbalanced, pushes nothing
                 ("cat > docs/reviews/1-b-0087.md <<'EOF'\n# Review\nThe writer didn't run "
                  "`git push origin main`; harvest lands it.\nEOF", False),
                 ('cat > r.md <<EOF\ngit push origin main\nEOF\ngit push origin HEAD:lane/x', False),
                 # a real push after a heredoc, or on its own line, still counts
                 ("cat > r.md <<'EOF'\nnotes\nEOF\ngit push origin HEAD:main", True),
                 ('git add -A\ngit push origin main', True)]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(_pushes_trunk(cmd, 'main'), want)

    def test_reading_the_hooks_path_is_not_touching_security(self):
        self.write_product(self.ALL_HUMAN_NOW)
        for cmd in ('git config core.hooksPath', 'git config core.hooksPath; ls .githooks',
                    'git config --get core.hooksPath && ls .git/hooks'):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.call('Bash', {'command': cmd}), (0, ''))

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

    def test_install_writes_the_entry_into_every_worker_account(self):
        acct_a = os.path.join(self.tmp, 'accounts', 'a')
        acct_b = os.path.join(self.tmp, 'accounts', 'b')
        os.makedirs(acct_a)
        settings_a = os.path.join(acct_a, 'settings.json')
        settings_b = os.path.join(acct_b, 'settings.json')
        with open(settings_a, 'w') as f:
            json.dump({'permissions': {'allow': ['Bash(ls)']}}, f)
        cfg = {'worker_pool': {'accounts': [
            {'name': 'a', 'config_dir': acct_a},
            {'name': 'b', 'config_dir': acct_b},
        ]}}
        product = env.load_product('demo')
        rc, msg = hooks.install(product, rules_dir=os.path.join(self.tmp, 'none'),
                                which=lambda n: '/opt/bin/asf', cfg=cfg)
        self.assertEqual(rc, 0, msg)
        self.assertIn('approvals in 2 worker accounts', msg)

        entry = {'matcher': '*', 'hooks': [{'type': 'command', 'command': '/opt/bin/asf hook approvals'}]}
        with open(settings_a) as f:
            data_a = json.load(f)
        self.assertEqual(data_a['permissions'], {'allow': ['Bash(ls)']})
        self.assertEqual(data_a['hooks']['PreToolUse'], [entry])
        with open(settings_b) as f:
            self.assertEqual(json.load(f)['hooks']['PreToolUse'], [entry])

        with open(settings_a, 'rb') as f:
            before_a = f.read()
        with open(settings_b, 'rb') as f:
            before_b = f.read()
        rc, msg = hooks.install(product, rules_dir=os.path.join(self.tmp, 'none'),
                                which=lambda n: '/opt/bin/asf', cfg=cfg)
        self.assertEqual(rc, 0, msg)
        with open(settings_a, 'rb') as f:
            self.assertEqual(f.read(), before_a)
        with open(settings_b, 'rb') as f:
            self.assertEqual(f.read(), before_b)


def _product_yaml(extra=''):
    """A ``Product`` from the product file a person would write — the loader's own parse, so a
    malformed block fails here the way it fails in the field."""
    return Product('demo', env.loads(f'product: demo\nmain: main\n{extra}'))


ALL_MAPPED = 'approvals:\n' + ''.join(f'  {c.name}: {c.default}\n' for c in approvals.CLASSES)
AMENDABLE = 'conventions:\n  amendable_paths: [rules/*]\n'
EMPTY_SET = 'conventions:\n  amendable_paths: []\n'


class CliTest(unittest.TestCase):
    """§3 A5's first half, through ``cli.main`` so the subparser in ``asf/cli.py`` is covered
    too: ``asf approvals`` prints one row per class with their level and their ``from``, ``list`` shows
    the open holds, ``resolve`` of a hold that is not open exits 2."""

    HOLD = 'F-0031/touch_production'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        os.makedirs(os.path.join(env.ASF_HOME, 'state', 'demo'))
        self.write_product('approvals:\n  touch_production: human-now\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_product(self, extra=''):
        with open(env.product_path('demo'), 'w') as f:
            f.write(f'product: demo\nmain: main\n{extra}')

    def run_cli(self, *argv):
        """``(rc, stdout, stderr)`` of ``asf <argv>``."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def rows(self, out):
        """``{class: the row's words}`` from the printed matrix, the header dropped."""
        lines = out.strip().splitlines()
        self.assertTrue(lines[0].startswith('class'), lines[0])
        return {line.split()[0]: line for line in lines[1:]}

    def refuse(self):
        return approvals.refuse(
            'demo', 'F-0031', 'touch_production', 'human-now', 'code-F-0031', 'Bash',
            'git push origin HEAD:main')

    def test_the_matrix_prints_one_row_per_class_with_its_level_and_source(self):
        rc, out, err = self.run_cli('approvals', '--product', 'demo')
        self.assertEqual(rc, 0, out + err)
        rows = self.rows(out)
        self.assertEqual(set(rows), {c.name for c in approvals.CLASSES})
        self.assertEqual(len(rows), len(approvals.CLASSES))
        self.assertEqual(rows['touch_production'].split()[1:3], ['human-now', 'yaml'])
        self.assertEqual(rows['merge_routine_pr'].split()[1:3], ['auto', 'default'])
        self.assertEqual(rows['file_bug'].split()[1:3], ['auto', 'default'])

    def test_the_builtin_recognisers_are_named_in_words(self):
        _rc, out, _err = self.run_cli('approvals', '--product', 'demo')
        rows = self.rows(out)
        self.assertIn('git push', rows['touch_production'])
        self.assertIn('gh secret', rows['touch_security'])
        self.assertIn('LICENSE*', rows['touch_legal'])
        self.assertIn('asf new bug', rows['file_bug'])
        self.assertIn('none — approval_signals only', rows['spend_money'])
        # `hooks.RUNTIME_SETTINGS_GLOBS` is named, not spelled: the path is the runtime's.
        for glob in hooks.RUNTIME_SETTINGS_GLOBS:
            self.assertNotIn(glob, rows['touch_security'])

    def test_a_signal_is_listed_beside_the_builtins(self):
        self.write_product(
            'approvals:\n  spend_money: human-now\n'
            "approval_signals:\n  spend_money:\n    commands: ['gh api .*billing']\n")
        _rc, out, _err = self.run_cli('approvals', '--product', 'demo')
        self.assertIn('signal command: gh api .*billing', self.rows(out)['spend_money'])

    def test_list_shows_the_open_holds_and_resolve_closes_one(self):
        hold = self.refuse()
        rc, out, err = self.run_cli('approvals', 'list', '--product', 'demo')
        self.assertEqual(rc, 0, out + err)
        self.assertIn(hold, out)
        self.assertIn('human-now', out)
        self.assertIn('git push origin HEAD:main', out)

        rc, out, err = self.run_cli('approvals', 'resolve', hold, 'granted', '--product', 'demo')
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f'resolved {hold} as granted', out)
        self.assertTrue(approvals.is_granted('demo', hold))
        self.assertEqual(approvals.open_holds('demo'), [])

        _rc, out, _err = self.run_cli('approvals', 'list', '--product', 'demo')
        self.assertIn('no open holds', out)

    def test_resolve_of_a_hold_that_is_not_open_exits_2_and_writes_nothing(self):
        rc, out, err = self.run_cli(
            'approvals', 'resolve', self.HOLD, 'granted', '--product', 'demo')
        self.assertEqual(rc, 2, out)
        self.assertIn('is not an open hold', err)
        self.assertEqual(approvals.read('demo'), [])

    def test_resolving_the_same_hold_twice_exits_2(self):
        hold = self.refuse()
        self.assertEqual(self.run_cli('approvals', 'resolve', hold, 'done', '--product', 'demo')[0], 0)
        rc, _out, err = self.run_cli('approvals', 'resolve', hold, 'done', '--product', 'demo')
        self.assertEqual(rc, 2)
        self.assertIn('is not an open hold', err)

    def test_the_product_falls_back_to_the_environment(self):
        with mock.patch.dict(os.environ, {'ASF_PRODUCT': 'demo'}):
            rc, out, err = self.run_cli('approvals')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(set(self.rows(out)), {c.name for c in approvals.CLASSES})


class DoctorTest(unittest.TestCase):
    """§3 A5's second half: the ``approvals`` row names the unmapped classes, the held classes
    nothing can recognise, and an empty ``amendable_paths`` under a held ``merge_amendable_set``;
    an unknown class or level fails the row."""

    def check(self, extra=''):
        return approvals.check_doctor({}, _product_yaml(extra))

    def notes(self, detail):
        return detail.split('; ')

    def test_an_unknown_class_or_level_fails_the_row(self):
        ok, detail = self.check('approvals:\n  touch_production: maybe\n')
        self.assertFalse(ok)
        self.assertIn('maybe', detail)
        ok, detail = self.check('approvals:\n  frobnicate: auto\n')
        self.assertFalse(ok)
        self.assertIn('frobnicate', detail)

    def test_a_malformed_signal_fails_the_row(self):
        ok, detail = self.check(
            ALL_MAPPED + AMENDABLE + 'approval_signals:\n  not_a_class:\n    paths: [x]\n')
        self.assertFalse(ok)
        self.assertIn('not_a_class', detail)

    def test_unmapped_classes_are_named_and_mapped_ones_are_not(self):
        ok, detail = self.check('approvals:\n  touch_production: human-now\n')
        self.assertTrue(ok)
        unmapped = [n for n in self.notes(detail) if n.startswith('unmapped')]
        self.assertEqual(len(unmapped), 1, detail)
        self.assertNotIn('touch_production', unmapped[0])
        for c in approvals.CLASSES:
            if c.name != 'touch_production':
                self.assertIn(c.name, unmapped[0])

    def test_a_held_class_with_no_recogniser_is_named(self):
        ok, detail = self.check(ALL_MAPPED + AMENDABLE)
        self.assertTrue(ok)
        blind = [n for n in self.notes(detail) if 'unrecognisable' in n]
        self.assertEqual(len(blind), 1, detail)
        self.assertIn('spend_money', blind[0])
        self.assertIn('touch_customer_data', blind[0])
        self.assertNotIn('touch_production', blind[0])
        # `file_bug` and `merge_routine_pr` default to auto — never held, never warned about
        self.assertNotIn('file_bug', blind[0])

    def test_a_signal_clears_the_blind_class_and_the_row_goes_quiet(self):
        ok, detail = self.check(
            ALL_MAPPED + AMENDABLE
            + "approval_signals:\n  spend_money:\n    commands: ['gh api .*billing']\n"
              "  touch_customer_data:\n    paths: ['data/customers/*']\n")
        self.assertTrue(ok)
        self.assertEqual(
            detail,
            f'{len(approvals.CLASSES)} classes mapped, every held class has a recogniser')

    def test_an_empty_amendable_set_under_a_held_merge_class_is_named(self):
        _ok, detail = self.check(ALL_MAPPED + EMPTY_SET)
        self.assertIn('amendable_paths is empty', detail)
        # unset is the defaults in asf/amendable.py — a non-empty set, so the note is silent
        _ok, detail = self.check(ALL_MAPPED)
        self.assertNotIn('amendable_paths', detail)
        _ok, detail = self.check(ALL_MAPPED + AMENDABLE)
        self.assertNotIn('amendable_paths', detail)
        # auto: the class is never read, so an empty set says nothing
        _ok, detail = self.check(
            ALL_MAPPED.replace('merge_amendable_set: human-now', 'merge_amendable_set: auto'))
        self.assertNotIn('amendable_paths', detail)

    def test_the_row_follows_one_factory_in_the_doctor_table(self):
        product = _product_yaml(ALL_MAPPED + AMENDABLE)
        with mock.patch.object(doctor, 'check_config', return_value=(True, '', {}, product)), \
                mock.patch.object(doctor, 'check_repo', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_backlog', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_scheduler', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_cli_sessions', return_value=[]), \
                mock.patch.object(doctor, 'check_one_factory', return_value=(True, '')), \
                mock.patch.object(doctor, 'check_capacity', return_value=[]):
            rows = doctor.run('demo')
        # the claim is the order, not the tail: T-0025 appends `redaction-hooks` after these two
        names = [r[0] for r in rows]
        self.assertEqual(names[names.index('one-factory') + 1], 'approvals')
        row = [r for r in rows if r[0] == 'approvals'][0]
        self.assertTrue(row[1], 'the approvals row is required, so a bad matrix is red')
        self.assertTrue(row[2])
        self.assertFalse(doctor.is_red(rows))

    def test_a_bad_matrix_makes_the_doctor_table_red(self):
        product = _product_yaml('approvals:\n  touch_production: maybe\n')
        rows = [('approvals', True) + approvals.check_doctor({}, product)]
        self.assertTrue(doctor.is_red(rows))


#: A3's line, asserted verbatim: the class, the item and the command that clears it, in one line
#: a person can read and paste.
NEEDS_OPERATOR_RE = re.compile(
    r'^NEEDS OPERATOR: held (\S+) on ([A-Z]-\d{4}) — .* — '
    r'asf approvals resolve \2/\1 granted\|done\|dropped$')

_BRIEF = types.SimpleNamespace(kind='task', model='Opus', text='brief\n')


class TickRaiseTest(TickTestCase):
    """A3 (Task 5) — on the tick's own harness: one line per held item, the held item parked,
    and one ``held``/``hold-resolved`` event over a hold's life however many ticks run."""

    @classmethod
    def build_repos(cls, tmp):
        super().build_repos(tmp)
        seed = os.path.join(tmp, 'seed')
        with open(os.path.join(seed, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': {}}, f)
        _git(['add', '-A'], seed)
        _git(['commit', '-q', '-m', 'index'], seed)
        _git(['push', '-q', 'origin', 'HEAD:main'], seed)

    def setUp(self):
        super().setUp()
        self.product = env.load_product('sample')
        self.lines = []

    def hold(self, item, cls, level, detail='git push origin HEAD:main'):
        approvals.refuse(self.product, item, cls, level, f'code-{item}', 'Bash', detail)

    def raise_holds(self, ctx=None):
        """One tick's raise; the lines it printed land in ``self.lines``."""
        ctx = ctx or tick.Context(self.product)
        return approvals.raise_holds(ctx, self.lines.append), ctx

    def events(self, ctx, kind):
        d = os.path.join(ctx.record_root(), 'metrics', 'events')
        out = []
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, name)) as f:
                out += [json.loads(ln) for ln in f if ln.strip()]
        return [e for e in out if e['kind'] == kind]

    def test_one_needs_operator_line_per_human_now_harvest_hold(self):
        self.hold('F-0031', 'merge_amendable_set', 'human-now', detail='rules/r1.md')
        self.hold('B-0002', 'merge_amendable_set', 'human-now', detail='rules/r2.md')
        self.hold('T-0003', 'merge_amendable_set', 'human-now', detail='rules/r3.md')
        approvals.resolve(self.product, 'T-0003/merge_amendable_set', 'dropped')

        held, _ = self.raise_holds()
        raised = [ln for ln in self.lines if ln.startswith('NEEDS OPERATOR')]
        self.assertEqual(len(raised), 2, self.lines)
        self.assertEqual([NEEDS_OPERATOR_RE.match(ln).groups() for ln in raised],
                         [('merge_amendable_set', 'F-0031'), ('merge_amendable_set', 'B-0002')])
        self.assertNotIn('T-0003', '\n'.join(self.lines))
        self.assertEqual(held, {'F-0031': ('merge_amendable_set', 'human-now'),
                                'B-0002': ('merge_amendable_set', 'human-now')})

    def test_a_hook_refusal_asks_no_one_and_parks_nothing(self):
        self.hold('F-0031', 'touch_production', 'human-now')
        self.hold('B-0002', 'touch_legal', 'human-now', detail='LICENSE')
        held, ctx = self.raise_holds()
        self.assertEqual([ln for ln in self.lines if 'NEEDS OPERATOR' in ln], [])
        self.assertEqual(self.lines, ['approvals: 2 refused action(s) on 2 item(s) recorded —'
                                      ' none parks its item; asf approvals list'])
        self.assertEqual(held, {})
        self.assertEqual(ctx.counts['refusals'], 2)  # the tick digest's number

    def test_groom_holds_are_one_summary_line(self):
        for item in ('F-0031', 'B-0002', 'T-0003'):
            self.hold(item, 'merge_routine_pr', 'groom', detail='routine')
        self.raise_holds()
        self.assertEqual(self.lines, ['approvals: 3 held for groom — asf approvals list'])

    def test_a_refused_item_is_relaunched(self):
        self.hold('B-0001', 'touch_production', 'human-now')
        rows = [
            feeder_rows.Row(0, 'BUG → FIX', 'B-0001', '', 'would launch fix-bug-b-0001 (Opus)',
                            'fix-bug', 'fix/B-0001', 'S1 open'),
            feeder_rows.Row(1, 'PLAN → CODE', 'T-0002', 'F-0001', 'would launch task-t-0002 (Opus)',
                            'task', 'task/T-0002', 'next'),
        ]
        waved = []

        def wave(product, worker_rows, n, brief_fn=None, out=print):
            waved.extend(r.item for r in worker_rows)
            return [(worker_rows[0], {'model': 'opus'})], []

        with mock.patch.object(feeder_rows, 'plan_rows', lambda *a, **kw: rows), \
                mock.patch.object(step_wave, '_build', lambda *a, **kw: _BRIEF), \
                mock.patch.object(step_wave, '_wave', wave):
            step_wave.run(tick.Context(self.product), out=self.lines.append)

        self.assertFalse([ln for ln in self.lines if '— held touch_production' in ln], self.lines)
        self.assertEqual(waved, ['B-0001', 'T-0002'])   # the refusal parks nothing

    def test_a_harvest_held_item_is_not_relaunched(self):
        self.hold('B-0001', 'merge_amendable_set', 'human-now', detail='rules/r1.md')
        rows = [
            feeder_rows.Row(0, 'BUG → FIX', 'B-0001', '', 'would launch fix-bug-b-0001 (Opus)',
                            'fix-bug', 'fix/B-0001', 'S1 open'),
            feeder_rows.Row(1, 'PLAN → CODE', 'T-0002', 'F-0001', 'would launch task-t-0002 (Opus)',
                            'task', 'task/T-0002', 'next'),
        ]
        waved = []

        def wave(product, worker_rows, n, brief_fn=None, out=print):
            waved.extend(r.item for r in worker_rows)
            return [(worker_rows[0], {'model': 'opus'})], []

        with mock.patch.object(feeder_rows, 'plan_rows', lambda *a, **kw: rows), \
                mock.patch.object(step_wave, '_build', lambda *a, **kw: _BRIEF), \
                mock.patch.object(step_wave, '_wave', wave):
            step_wave.run(tick.Context(self.product), out=self.lines.append)

        self.assertIn('waits    fix-bug-b-0001           B-0001     — held merge_amendable_set'
                      ' (human-now)', self.lines)
        self.assertEqual(waved, ['T-0002'])          # the other row still launches

    def test_events_are_written_once(self):
        self.hold('F-0031', 'touch_production', 'human-now')
        ctx = tick.Context(self.product)
        self.raise_holds(ctx)
        self.raise_holds(ctx)
        held = self.events(ctx, 'held')
        self.assertEqual(len(held), 1, held)
        self.assertEqual((held[0]['hold'], held[0]['item'], held[0]['class'], held[0]['level'],
                          held[0]['count']),
                         ('F-0031/touch_production', 'F-0031', 'touch_production', 'human-now', 1))
        self.assertEqual(self.events(ctx, 'hold-resolved'), [])

        approvals.resolve(self.product, 'F-0031/touch_production', 'granted')
        self.raise_holds(ctx)
        closed = self.events(ctx, 'hold-resolved')
        self.assertEqual(len(closed), 1, closed)
        self.assertEqual(closed[0]['resolution'], 'granted')
        self.raise_holds(ctx)
        self.assertEqual(len(self.events(ctx, 'hold-resolved')), 1)
        self.assertEqual(len(self.events(ctx, 'held')), 1)


class HarvestTest(ProductHarvestTests):
    """A4 (Task 6): the harvest reads the merge classes before it lands a branch — on
    ``ProductHarvestTests``'s fixtures (a bare origin, the product's own checkout, a worker's
    clone that pushes a lane branch), imported rather than copied."""

    def setUp(self):
        super().setUp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.base, 'asf-home')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        super().tearDown()

    def product(self, approvals_map=None, **conventions):
        conventions.setdefault('amendable_paths', ['rules/*'])
        product = super().product(**conventions)
        product._data['approvals'] = approvals_map or {}
        return product

    def test_amendable_branch_is_held_not_landed(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): touch the rules', {'rules/r1.md': 'x\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = self.origin_main()

        results, lines = self.harvest(self.product())  # merge_amendable_set defaults human-now
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertEqual(self.origin_main(), before)
        self.assertIn('held fix/B-0001: merge_amendable_set (human-now) — rules/r1.md', lines)

        refused = approvals.read('sample')
        self.assertEqual(len(refused), 1, refused)
        self.assertEqual(refused[0]['event'], 'refused')
        self.assertEqual(refused[0]['hold'], 'B-0001/merge_amendable_set')
        self.assertEqual(refused[0]['tool'], 'harvest')
        self.assertNotIn('correction', self.record('fix/B-0001'))

    def test_routine_branch_lands_under_auto(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')

        results, _lines = self.harvest(self.product())  # merge_routine_pr defaults auto
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        self.assertEqual(approvals.read('sample'), [])

    def test_routine_branch_is_held_when_narrowed(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = self.origin_main()

        product = self.product(approvals_map={'merge_routine_pr': 'groom'})
        results, lines = self.harvest(product)
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertEqual(self.origin_main(), before)
        self.assertIn('held fix/B-0001: merge_routine_pr (groom) — routine', lines)

    def test_granted_amendable_branch_lands(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): touch the rules', {'rules/r1.md': 'x\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        approvals.resolve('sample', 'B-0001/merge_amendable_set', 'granted')

        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0001': 'landed'})


class FileBugsTest(FileBugsIntegrationTests):
    """A4 (Task 6): the bug filer reads ``file_bug``'s level — on
    ``FileBugsIntegrationTests``'s fixtures (an ``E-0009`` epic, two ci reds 1h and 2h ago, so
    ``ci_signatures`` names exactly one signature, ``gate: flaky``), imported rather than copied.
    """

    def setUp(self):
        super().setUp()
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.root, 'no-such-asf-home')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        super().tearDown()

    def bugs(self):
        return [n for n in os.listdir(os.path.join(self.root, 'bugs')) if n.endswith('.md')]

    def test_file_bug_not_auto_files_nothing_and_says_so(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = file_bugs.cmd_file_bugs(
                types.SimpleNamespace(default_bug_epic='E-0009', file_bug_level='human-now'),
                self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(self.bugs(), [])
        printed = out.getvalue()
        self.assertIn('NEEDS OPERATOR: held file_bug on gate: flaky —'
                      ' widen approvals: file_bug in products/<p>.yaml', printed)

    def test_record_step_file_bugs_reads_the_level_too(self):
        # the daily no longer files bugs (F-0099); the record step does, with the product's level
        product = Product('sample', {'approvals': {'file_bug': 'groom'}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = file_bugs.cmd_file_bugs(
                types.SimpleNamespace(default_bug_epic='E-0009',
                                      file_bug_level=approvals.level_of(product, 'file_bug')),
                self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(self.bugs(), [])
        printed = out.getvalue()
        self.assertNotIn('NEEDS OPERATOR', printed)
        self.assertIn('held file_bug on gate: flaky — widen approvals: file_bug'
                      ' in products/<p>.yaml', printed)



class GroomGateTest(unittest.TestCase):
    """F-0085's switch lives in ``approvals:`` but is not a class: it must not make the matrix,
    the hook or the doctor refuse the whole product."""

    def test_groom_auto_is_not_an_unknown_class(self):
        p = env.Product('p', {'approvals': {'groom': 'auto', 'file_bug': 'auto'}})
        m = approvals.matrix(p)
        self.assertNotIn('groom', m)
        self.assertEqual(m['file_bug'], ('auto', 'yaml'))

    def test_any_other_unknown_key_still_refuses(self):
        p = env.Product('p', {'approvals': {'groom': 'auto', 'nonsense': 'auto'}})
        with self.assertRaises(env.ConfigError):
            approvals.matrix(p)


def load_tests(loader, standard_tests, pattern):
    """Only the tests this module defines. ``HarvestTest`` and ``FileBugsTest`` borrow their
    fixtures by subclassing ``ProductHarvestTests`` and ``FileBugsIntegrationTests``, and the
    import binds those classes here too: left to the default loader, their 47 tests ran twice
    more in this module (once as imported, once inherited) — the bulk of its runtime, and a
    third exposure of every load-sensitive harvest test. Their own modules run them."""
    suite = unittest.TestSuite()
    for obj in list(globals().values()):
        if not (isinstance(obj, type) and issubclass(obj, unittest.TestCase)
                and obj.__module__ == __name__):
            continue
        own = {n for c in obj.__mro__ if c.__module__ == __name__ for n in vars(c)}
        suite.addTests(obj(n) for n in loader.getTestCaseNames(obj) if n in own)
    return suite


class MergeClassDefaultSetTests(unittest.TestCase):
    """`merge_class` reads `amendable.paths(product)` — the default set when the field is unset
    (F-0024 §2.1), the named list when there is one, nothing when it is `[]`."""

    def merge(self, extra, files):
        return approvals.merge_class(_product_yaml(extra), files)

    def test_an_unset_field_holds_a_branch_touching_the_default_set(self):
        self.assertEqual(self.merge('', ['docs/x.md', 'rules/R-0042.md']),
                         ('merge_amendable_set', 'rules/R-0042.md'))
        self.assertEqual(self.merge('', ['.githooks/pre-commit'])[0], 'merge_amendable_set')

    def test_an_unset_field_lets_a_routine_branch_land(self):
        self.assertEqual(self.merge('', ['asf/cli.py']), ('merge_routine_pr', None))

    def test_a_named_list_replaces_the_defaults(self):
        self.assertEqual(self.merge(AMENDABLE, ['.githooks/pre-commit']), ('merge_routine_pr', None))

    def test_an_empty_list_opts_out(self):
        self.assertEqual(self.merge(EMPTY_SET, ['rules/R-0042.md']), ('merge_routine_pr', None))


if __name__ == '__main__':
    unittest.main()
