"""Path resolution (F-0087, class "path resolution": B-0036, B-0042, B-0050): every operator
command, run from a cwd that is nothing — not a record, not a product repo — with ``--product``,
acts on the product's own record and repo, creates nothing under the cwd, and a record command
says ``record: <path>`` on its first line. The B-0050 audit's live matrix as a test: one row per
command, in-process through :func:`asf.cli.main`.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import cli, env

CARD = ('---\nid: E-0001\ntype: epic\ntitle: the epic\n# ---- machine ----\nstate: New\n---\n'
        '## Description\nx\n\n## History\n- made\n')
BUG = ('---\nid: B-0001\ntype: bug\ntitle: the bug\nparent: E-0001\nseverity: S1\ndecided: true\n'
       '# ---- machine ----\nstate: New\n---\n## Description\nx\n\n## Fix\ny\n\n## History\n- made\n')

#: The audit table: (argv, kind). ``record`` commands print ``record: <path>`` first (``new`` and
#: ``stale`` on stderr — their stdout is a value: the minted id, the json table); ``repo``
#: commands work in the product repo; ``view`` commands read the record. Every one of them,
#: from an empty cwd, leaves that cwd empty.
COMMANDS = [
    (['new', 'bug', '--title', 'a bug from nowhere', '--parent', 'E-0001', '--severity', 'S2'], 'record'),
    (['check'], 'record'),
    (['index'], 'record'),
    (['ingest'], 'record'),
    (['migrate', '--dry-run'], 'record'),
    (['groom'], 'record'),
    (['stale'], 'record'),
    (['file-bugs'], 'record'),
    (['rules', 'check'], 'record'),
    (['roadmap'], 'record'),
    (['backlog'], 'record'),
    (['parity'], 'record'),
    (['prod'], 'record'),
    (['sessions'], 'record'),
    (['status'], 'record'),
    (['next'], 'view'),
    (['brief'], 'view'),
    (['doctor'], 'view'),
    (['harvest', '--dry-run'], 'repo'),
    (['workers', 'health'], 'repo'),
    (['workers', 'stall'], 'repo'),
    (['workers', 'quota'], 'repo'),
    (['scheduler', 'status'], 'repo'),
    (['tick', '--steps', 'health'], 'repo'),
]


def _git(args, cwd):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


class EveryCommandFromNowhere(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf_paths_'))
        cls._home, cls._cwd = env.ASF_HOME, os.getcwd()
        cls._env = dict(os.environ)
        os.environ.pop('ASF_PRODUCT', None)
        env.ASF_HOME = os.environ['ASF_HOME'] = os.path.join(cls.tmp, 'home')
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        # the record: a bare origin and a checkout with two cards and an index
        cls.record = os.path.join(cls.tmp, 'record')
        os.makedirs(os.path.join(cls.record, 'epics'))
        os.makedirs(os.path.join(cls.record, 'bugs'))
        with open(os.path.join(cls.record, 'epics', 'E-0001.md'), 'w') as f:
            f.write(CARD)
        with open(os.path.join(cls.record, 'bugs', 'B-0001.md'), 'w') as f:
            f.write(BUG)
        with open(os.path.join(cls.record, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': {}}, f)
        for path in (cls.record,):
            _git(['init', '-q', '-b', 'main'], path)
            _git(['config', 'user.email', 't@example.com'], path)
            _git(['config', 'user.name', 't'], path)
        origin = os.path.join(cls.tmp, 'record.git')
        _git(['init', '-q', '--bare', '-b', 'main', origin], cls.tmp)
        _git(['add', '-A'], cls.record)
        _git(['commit', '-q', '-m', 'seed'], cls.record)
        _git(['remote', 'add', 'origin', origin], cls.record)
        _git(['push', '-q', '-u', 'origin', 'main'], cls.record)
        # the product repo: a clone of its own bare origin
        repo_origin = os.path.join(cls.tmp, 'repo.git')
        _git(['init', '-q', '--bare', '-b', 'main', repo_origin], cls.tmp)
        cls.repo = os.path.join(cls.tmp, 'repo')
        _git(['clone', '-q', repo_origin, cls.repo], cls.tmp)
        _git(['config', 'user.email', 't@example.com'], cls.repo)
        _git(['config', 'user.name', 't'], cls.repo)
        with open(os.path.join(cls.repo, 'README'), 'w') as f:
            f.write('r\n')
        _git(['add', '-A'], cls.repo)
        _git(['commit', '-q', '-m', 'init'], cls.repo)
        _git(['push', '-q', 'origin', 'HEAD:main'], cls.repo)
        with open(env.product_path('sample'), 'w') as f:
            f.write(f'product: sample\nrepo_dir: {cls.repo}\nbacklog_dir: {cls.record}\nmain: main\n'
                    'ci:\n  provider: none\nsteps:\n  batch: off\n  daily: off\n')
        with open(env.config_path(), 'w') as f:
            f.write('scheduler:\n  kind: none\nworker_pool:\n  backend: fake\n')
        cls.nowhere = os.path.join(cls.tmp, 'nowhere')
        os.makedirs(cls.nowhere)
        os.chdir(cls.nowhere)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._cwd)
        env.ASF_HOME = cls._home
        os.environ.clear()
        os.environ.update(cls._env)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = cli.main(argv)
            except SystemExit as e:  # argparse refusing --product
                rc = f'SystemExit({e.code})'
        return rc, out.getvalue()

    def test_every_command_leaves_the_cwd_empty_and_names_its_record(self):
        for argv, kind in COMMANDS:
            with self.subTest(command=' '.join(argv)):
                rc, out = self.run_cli(argv + ['--product', 'sample'])
                self.assertEqual(os.listdir(self.nowhere), [], f'{argv} wrote into the cwd:\n{out}')
                self.assertNotIsInstance(rc, str, f'{argv} does not accept --product:\n{out}')
                self.assertNotIn('Traceback', out, out)
                if kind == 'record':
                    first = out.splitlines()[0] if out.strip() else ''
                    self.assertEqual(first, f'record: {self.record}', f'{argv}:\n{out}')

    def test_a_record_command_from_a_product_repo_cwd_still_uses_the_products_record(self):
        os.chdir(self.repo)
        try:
            before = sorted(os.listdir(self.repo))
            rc, out = self.run_cli(['new', 'bug', '--title', 'from the repo', '--parent', 'E-0001',
                                    '--severity', 'S2', '--product', 'sample'])
            self.assertEqual(sorted(os.listdir(self.repo)), before, out)
            self.assertEqual(out.splitlines()[0], f'record: {self.record}', out)
        finally:
            os.chdir(self.nowhere)

    def test_a_command_step_runs_in_the_products_repo_not_the_cwd(self):
        # B-0050 item 4: the tick's command steps ran in the tick's cwd
        with open(env.product_path('cmd'), 'w') as f:
            f.write(f'product: cmd\nrepo_dir: {self.repo}\nbacklog_dir: {self.record}\nmain: main\n'
                    'ci:\n  provider: none\nsteps:\n  batch: pwd\n  daily: off\n')
        rc, out = self.run_cli(['tick', '--steps', 'batch', '--product', 'cmd'])
        self.assertIn(f'[command:batch] {self.repo}', out, out)
        self.assertEqual(os.listdir(self.nowhere), [])

    def test_the_stamp_names_the_products_repo_not_the_shell(self):
        rc, out = self.run_cli(['status', '--product', 'sample'])
        self.assertNotIn('@unknown', out, out)


if __name__ == '__main__':
    unittest.main()
