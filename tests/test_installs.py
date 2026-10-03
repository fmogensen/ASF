"""asf.installs — the per-product pin: install.json, venv names, on-disk checks, prune.

Hermetic: a temp ASF_HOME and a temp PIPX_HOME; pipx is a fake ``run``."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import cli, env, installs


def make_venv(path, sha):
    """A venv on disk as pipx leaves it: ``bin/asf``, ``bin/python`` and the dist's
    ``direct_url.json`` naming ``sha``."""
    os.makedirs(os.path.join(path, 'bin'), exist_ok=True)
    for name in ('asf', 'python'):
        p = os.path.join(path, 'bin', name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n')
        os.chmod(p, 0o755)
    dist = os.path.join(path, 'lib', 'python3.12', 'site-packages',
                        'asf_factory-0.1.dist-info')
    os.makedirs(dist, exist_ok=True)
    with open(os.path.join(dist, 'direct_url.json'), 'w', encoding='utf-8') as f:
        json.dump({'url': 'https://example.invalid/r.git',
                   'vcs_info': {'vcs': 'git', 'commit_id': sha}}, f)
    return path


class InstallsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='installs_test_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.object(env, 'ASF_HOME', os.path.join(self.tmp, 'home'))
        patch.start()
        self.addCleanup(patch.stop)
        self.pipx_home = os.path.join(self.tmp, 'pipx')
        envp = mock.patch.dict(os.environ, {'PIPX_HOME': self.pipx_home})
        envp.start()
        self.addCleanup(envp.stop)
        self.venvs = os.path.join(self.pipx_home, 'venvs')


class RecordTest(InstallsCase):
    SHA, OLD = 'a' * 40, 'b' * 40

    def test_the_dist_name_is_the_clis(self):
        self.assertEqual(installs.DIST, cli.DIST_NAME)

    def test_venv_name_carries_the_product_and_sha7(self):
        self.assertEqual(installs.venv_name('alpha', '8d5e25a62' + 'f' * 31),
                         'asf-factory-alpha-8d5e25a')
        self.assertEqual(installs.suffix('alpha', self.SHA), '-alpha-aaaaaaa')
        self.assertEqual(installs.venv_dir('alpha', self.SHA),
                         os.path.join(self.venvs, 'asf-factory-alpha-aaaaaaa'))

    def test_no_file_reads_as_no_pin(self):
        self.assertIsNone(installs.read('alpha'))
        self.assertFalse(installs.pinned('alpha'))

    def test_an_unreadable_or_older_file_reads_as_no_pin(self):
        path = installs.record_path('alpha')
        os.makedirs(os.path.dirname(path))
        for text in ('{not json', '[]', '{"sha": "abc"}', '{"venv": "/x"}'):
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            self.assertIsNone(installs.read('alpha'), text)

    def test_write_then_read_keeps_previous(self):
        installs.write('alpha', self.SHA, '/v/new', previous={'sha': self.OLD, 'venv': '/v/old'},
                       by='console')
        rec = installs.read('alpha')
        self.assertEqual((rec.sha, rec.venv, rec.previous_sha, rec.previous_venv, rec.by,
                          rec.policy),
                         (self.SHA, '/v/new', self.OLD, '/v/old', 'console', 'pinned'))
        self.assertEqual(rec.interpreter, '/v/new/bin/python')
        self.assertEqual(rec.cli_path, '/v/new/bin/asf')

    def test_a_shell_reader_takes_the_first_venv_line_and_gets_the_current_one(self):
        """The dispatcher reads the file with sed, not JSON: the top-level ``venv`` is the first
        ``"venv":`` line, before ``previous``'s."""
        installs.write('alpha', self.SHA, '/v/new', previous={'sha': self.OLD, 'venv': '/v/old'})
        with open(installs.record_path('alpha'), encoding='utf-8') as f:
            venv_lines = [ln for ln in f if '"venv":' in ln]
        self.assertIn('/v/new', venv_lines[0])
        self.assertEqual(len(venv_lines), 2)

    def test_site_packages_is_found_in_the_venv(self):
        venv = make_venv(os.path.join(self.tmp, 'v'), self.SHA)
        self.assertTrue(installs.site_packages(venv).endswith(
            os.path.join('lib', 'python3.12', 'site-packages')))
        self.assertIsNone(installs.site_packages(os.path.join(self.tmp, 'none')))


class OnDiskTest(InstallsCase):
    SHA = 'c' * 40

    def test_on_disk_needs_the_venv_and_its_commit(self):
        self.assertIsNone(installs.on_disk('alpha', self.SHA))
        venv = make_venv(installs.venv_dir('alpha', self.SHA), self.SHA)
        self.assertEqual(installs.on_disk('alpha', self.SHA), venv)
        make_venv(venv, 'd' * 40)  # the dir holds another commit: not this sha's venv
        self.assertIsNone(installs.on_disk('alpha', self.SHA))

    def test_venvs_root_asks_pipx_when_no_pipx_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PIPX_HOME', None)
            run = mock.Mock(return_value=mock.Mock(returncode=0, stdout='/elsewhere/venvs\n'))
            self.assertEqual(installs.venvs_root(run), '/elsewhere/venvs')
            run.assert_called_once()
            self.assertEqual(run.call_args[0][0][:2], ['pipx', 'environment'])


class PruneTest(InstallsCase):
    def venv(self, sha, age):
        path = make_venv(installs.venv_dir('alpha', sha), sha)
        os.utime(path, (age, age))
        return path

    def test_prune_keeps_three_and_never_the_current_or_previous(self):
        shas = [c * 40 for c in '12345']
        paths = [self.venv(s, 1000 + i) for i, s in enumerate(shas)]
        # the current pin is the oldest venv, the previous the second oldest
        installs.write('alpha', shas[0], paths[0], previous={'sha': shas[1], 'venv': paths[1]})
        self.venv('9' * 40, 999)  # another product's venv never counts
        os.rename(installs.venv_dir('alpha', '9' * 40), installs.venv_dir('beta', '9' * 40))
        calls = []

        def run(cmd, **_kw):
            calls.append(cmd)
            shutil.rmtree(os.path.join(self.venvs, cmd[2]))
            return mock.Mock(returncode=0)
        removed = installs.prune('alpha', run=run, out=lambda _l: None)
        self.assertEqual(removed, paths[2:4])
        self.assertEqual(calls, [['pipx', 'uninstall', os.path.basename(p)] for p in paths[2:4]])
        self.assertEqual(installs.list_venvs('alpha'), [paths[0], paths[1], paths[4]])

    def test_prune_with_few_venvs_removes_nothing(self):
        self.venv('1' * 40, 1000)
        run = mock.Mock(side_effect=AssertionError('nothing to prune'))
        self.assertEqual(installs.prune('alpha', run=run, out=lambda _l: None), [])


if __name__ == '__main__':
    unittest.main()
