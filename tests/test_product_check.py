"""``asf product check <file> --product <p>`` (asf.product_check): the product file run through
the loader of the venv the product runs — here fake venvs whose ``bin/python`` is this test's own
interpreter over a chosen ``asf`` package. Temp asf home and pipx dir; nothing of the operator's."""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from asf import clockinstall, env, installs, product_check, scheduler
from tests import pinned
from tests.test_doctor_pin import ROOT, SHA, old_reader, shim_venv


class ProductCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-product-check-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'asf-home')
        os.makedirs(os.path.join(self.home, 'products'))
        self.venvs = os.path.join(self.tmp, 'pipx', 'venvs')
        os.makedirs(self.venvs)
        agents = os.path.join(self.tmp, 'LaunchAgents')
        os.makedirs(agents)
        for patch in (mock.patch.object(env, 'ASF_HOME', self.home),
                      mock.patch.dict(os.environ, {'PIPX_HOME': os.path.dirname(self.venvs)}),
                      mock.patch.object(scheduler, 'launch_agents_dir', return_value=agents),
                      mock.patch.object(clockinstall, 'venvs_dir', return_value=self.venvs)):
            patch.start()
            self.addCleanup(patch.stop)
        self.file = os.path.join(self.tmp, 'sample.yaml')
        with open(self.file, 'w', encoding='utf-8') as f:
            f.write(f'product: sample\nrepo_dir: {self.tmp}\nbacklog_dir: {self.tmp}\n'
                    'surprise: 1\n')

    def check(self, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = product_check.check(self.file, 'sample', cfg={}, **kw)
        return rc, buf.getvalue()

    def pin(self, pythonpath):
        venv = shim_venv(self.venvs, 'asf-factory-sample-abcdef1', pythonpath)
        installs.write('sample', SHA, venv, by='test')
        return venv

    def test_an_unknown_key_under_an_older_pinned_reader_is_refused(self):
        self.pin(old_reader(self.tmp))
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn('under pin asf-factory-sample-abcdef1 @ abcdef1: REFUSED', out)
        self.assertIn('error   line 4: surprise is not a field of the product file', out)

    def test_an_unknown_key_under_this_reader_is_a_warning_and_exit_0(self):
        self.pin(ROOT)
        rc, out = self.check()
        self.assertEqual(rc, 0, out)
        self.assertIn('loads (1 warning(s))', out)
        self.assertIn('warning line 4: surprise', out)

    def test_venv_overrides_the_pin(self):
        self.pin(ROOT)
        candidate = shim_venv(self.tmp, 'candidate', old_reader(self.tmp))
        rc, out = self.check(venv=candidate)
        self.assertEqual(rc, 1)
        self.assertIn('under venv candidate', out)

    def test_the_real_pinned_reader_refuses_an_unknown_key(self):
        sha = pinned.pinned_shas()[0]
        tree = pinned.pinned_tree(sha)   # skips loudly when the object cannot be had
        self.pin(tree)
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn('surprise', out)

    def test_a_missing_file_is_exit_2(self):
        self.file = os.path.join(self.tmp, 'absent.yaml')
        rc, out = self.check()
        self.assertEqual(rc, 2)
        self.assertIn('no such file', out)

    def test_the_cli_parses_product_check(self):
        from asf import cli
        args = cli.build_parser().parse_args(['product', 'check', self.file, '--product',
                                              'sample', '--venv', '/x'])
        self.assertEqual((args.product_command, args.file, args.product, args.venv),
                         ('check', self.file, 'sample', '/x'))
        self.assertIs(args.run, product_check.cmd_product)


if __name__ == '__main__':
    unittest.main()
