"""asf.cli.main — a refused product file is one NEEDS OPERATOR line and exit 2, never a traceback
(B-0045); a record command uses the product's record, not the cwd, even when the cwd is a
product repo (B-0050)."""
import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import cli, env


class RefusedProductFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('repo_slug: x/y\nnot_a_declared_key: 1\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_refused_product_file_is_one_needs_operator_line_and_exit_2(self):
        for argv in (['status', '--product', 'sample'], ['tick', '--product', 'sample']):
            rc, out = self._run(argv)
            self.assertEqual(rc, 2, argv)
            lines = out.strip().splitlines()
            self.assertEqual(len(lines), 1, out)
            self.assertTrue(lines[0].startswith('NEEDS OPERATOR: '), out)
            self.assertIn('asf init --product sample', lines[0])


class CapacityCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('product: sample\nrepo_slug: x/y\ncapacity:\n  sessions: 3\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_capacity_json_parses_and_dispatches(self):
        rc, out = self._run(['capacity', '--product', 'sample', '--json'])
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(data, [{
            'product': 'sample',
            'sessions': {'ceiling': 3, 'inflight': 0, 'free': 3, 'bound_by': 'product'},
            'ci': {'ceiling': None, 'inflight': None, 'free': None, 'bound_by': None},
            'batch': {},
            'deprecated': [],
        }])


class RecordCommandUsesTheProductRecordTests(unittest.TestCase):
    """B-0050: ``asf groom`` (and the other record commands) resolve the record from the
    configured product, not from the cwd — a product repo's cwd is never mistaken for one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self._orig_cwd = os.getcwd()
        self._orig_asf_product = os.environ.get('ASF_PRODUCT')
        os.environ.pop('ASF_PRODUCT', None)

        self.product_repo = os.path.join(self.tmp, 'product-repo')
        self.backlog_dir = os.path.join(self.tmp, 'sample-backlog')
        os.makedirs(self.product_repo)
        os.makedirs(os.path.join(self.backlog_dir, 'epics'))
        with open(os.path.join(self.backlog_dir, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': {}}, f)

        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write(
                f"product: sample\nrepo_slug: x/y\nrepo_dir: {self.product_repo}\n"
                f"main: main\nbacklog_dir: {self.backlog_dir}\n"
            )
        os.chdir(self.product_repo)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        env.ASF_HOME = self._orig_home
        if self._orig_asf_product is None:
            os.environ.pop('ASF_PRODUCT', None)
        else:
            os.environ['ASF_PRODUCT'] = self._orig_asf_product
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_groom_from_a_product_repo_grooms_the_product_record_not_the_cwd(self):
        rc, out = self._run(['groom', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"record: {self.backlog_dir}", out)
        self.assertFalse(os.path.isdir(os.path.join(self.product_repo, 'groom')),
                         "groom/ must not land in the product repo cwd")
        today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
        self.assertTrue(os.path.isfile(os.path.join(self.backlog_dir, 'groom', f'{today}.md')),
                        "groom/<date>.md must land in the product's backlog_dir")

    def test_groom_honors_asf_product_env_var_from_a_product_repo(self):
        os.environ['ASF_PRODUCT'] = 'sample'
        rc, out = self._run(['groom'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[0], f"record: {self.backlog_dir}", out)
        self.assertFalse(os.path.isdir(os.path.join(self.product_repo, 'groom')))

    def test_groom_from_an_unconfigured_product_repo_refuses_rather_than_grooming_nothing(self):
        rc, out = self._run(['groom'])
        self.assertEqual(rc, 2, out)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1, out)
        self.assertTrue(lines[0].startswith('NEEDS OPERATOR: '), out)
        self.assertFalse(os.path.isdir(os.path.join(self.product_repo, 'groom')),
                         "a product repo with no configured product must never be treated as the record")

    def test_groom_from_a_bare_record_checkout_with_no_product_still_grooms_itself(self):
        os.chdir(self.backlog_dir)
        rc, out = self._run(['groom'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[0], f"record: {os.path.realpath(self.backlog_dir)}", out)
