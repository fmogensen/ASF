"""asf.cli.main — a refused product file is one NEEDS OPERATOR line and exit 2, never a traceback (B-0045)."""
import contextlib
import io
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
