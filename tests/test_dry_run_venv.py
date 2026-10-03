"""``asf tick --dry-run --with-venv <venv> --state-copy <dir>`` (asf.tick.dry_run) and the A/B
scripts ``tools/ab-dry-run.sh`` / ``tools/ab-ci-queue.sh`` — the pinned venv and a candidate on one
state. The subprocess is a captured fake; the venvs are temp dirs; the asf home is a temp dir."""
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from asf import env
from asf.tick import dry_run, tick

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AB_DRY = os.path.join(ROOT, 'tools', 'ab-dry-run.sh')
AB_CIQ = os.path.join(ROOT, 'tools', 'ab-ci-queue.sh')


def _script(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('#!/bin/sh\n' + body)
    os.chmod(path, 0o755)
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf-dry-venv-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'asf-home')
        state = os.path.join(self.home, 'state')
        os.makedirs(os.path.join(state, 'sample', 'record'))
        os.makedirs(os.path.join(state, 'sample', 'worktrees', 'big'))
        os.makedirs(os.path.join(state, 'other'))
        os.makedirs(os.path.join(self.home, 'products'))
        with open(os.path.join(state, 'sample', 'record', 'index.json'), 'w') as f:
            f.write('{}')
        with open(os.path.join(self.home, 'config.yaml'), 'w') as f:
            f.write('default_product: sample\n')
        patch = mock.patch.object(env, 'ASF_HOME', self.home)
        patch.start()
        self.addCleanup(patch.stop)
        self.product = env.Product('sample', {})
        self.venv = os.path.join(self.tmp, 'venvs', 'candidate')
        _script(os.path.join(self.venv, 'bin', 'python'), 'exit 0\n')


class WithVenvTests(Fixture):
    def test_the_child_is_that_venvs_interpreter_on_the_snapshot_with_no_pythonpath(self):
        seen = {}

        def run(argv, env=None, cwd=None):
            home = env['ASF_HOME']
            seen.update(argv=argv, cwd=cwd, pythonpath=env.get('PYTHONPATH'),
                        snap=os.path.realpath(os.path.join(home, 'state', 'sample')),
                        other=os.path.realpath(os.path.join(home, 'state', 'other')),
                        config=os.path.realpath(os.path.join(home, 'config.yaml')),
                        files=sorted(os.listdir(os.path.join(home, 'state', 'sample'))))
            return types.SimpleNamespace(returncode=0)

        snap = os.path.join(self.tmp, 'snap')
        with mock.patch.dict(os.environ, {'PYTHONPATH': '/somewhere'}):
            rc = dry_run.run_with_venv(self.product, self.venv, fresh=True, state_copy=snap,
                                       run=run, err=lambda _l: None)
        self.assertEqual(rc, 0)
        self.assertEqual(seen['argv'], [os.path.join(self.venv, 'bin', 'python'), '-m', 'asf.cli',
                                        'tick', '--dry-run', '--product', 'sample', '--fresh'])
        self.assertEqual(seen['cwd'], '/')
        self.assertIsNone(seen['pythonpath'])
        self.assertEqual(seen['snap'], snap)
        self.assertEqual(seen['other'], os.path.join(self.home, 'state', 'other'))
        self.assertEqual(seen['config'], os.path.join(self.home, 'config.yaml'))
        self.assertEqual(seen['files'], ['record', 'worktrees'])
        self.assertEqual(os.listdir(os.path.join(snap, 'worktrees')), [])  # never copied
        self.assertTrue(os.path.isdir(snap), 'the caller owns --state-copy')

    def test_two_runs_on_one_state_copy_share_it_and_the_real_state_is_untouched(self):
        snap = os.path.join(self.tmp, 'snap')
        homes = []

        def run(argv, env=None, cwd=None):
            homes.append(os.path.realpath(os.path.join(env['ASF_HOME'], 'state', 'sample')))
            return types.SimpleNamespace(returncode=0)

        dry_run.run_with_venv(self.product, self.venv, state_copy=snap, run=run, err=lambda _l: None)
        with open(os.path.join(self.home, 'state', 'sample', 'record', 'index.json'), 'w') as f:
            f.write('{"moved": 1}')            # the live state moves between the two runs
        dry_run.run_with_venv(self.product, self.venv, state_copy=snap, run=run, err=lambda _l: None)
        self.assertEqual(homes, [snap, snap])
        with open(os.path.join(snap, 'record', 'index.json')) as f:
            self.assertEqual(f.read(), '{}', 'the second run reads the first snapshot')

    def test_a_venv_with_no_interpreter_is_exit_2(self):
        lines = []
        rc = dry_run.run_with_venv(self.product, os.path.join(self.tmp, 'nope'), err=lines.append,
                                   run=lambda *a, **k: self.fail('no run'))
        self.assertEqual(rc, 2)
        self.assertIn('not on disk', lines[0])

    def test_an_in_process_run_copies_from_the_state_copy(self):
        snap = dry_run.snapshot(self.product, os.path.join(self.tmp, 'snap'))
        with open(os.path.join(snap, 'marker'), 'w') as f:
            f.write('x')
        tmp, copy = dry_run._copy_state(self.product, snap)
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertTrue(os.path.exists(os.path.join(copy, 'marker')))
        self.assertFalse(os.path.exists(os.path.join(self.home, 'state', 'sample', 'marker')))

    def test_cmd_tick_routes_with_venv_and_refuses_it_without_dry_run(self):
        args = tick.register(__import__('argparse').ArgumentParser().add_subparsers()) \
            .parse_args(['--product', 'sample', '--dry-run', '--with-venv', self.venv,
                         '--state-copy', '/s'])
        with mock.patch.object(env, 'load_product', return_value=self.product), \
                mock.patch.object(dry_run, 'run_with_venv', return_value=0) as rw:
            self.assertEqual(tick.cmd_tick(args), 0)
        rw.assert_called_once_with(self.product, self.venv, fresh=False, state_copy='/s')
        args.dry_run = False
        with mock.patch.object(env, 'load_product', return_value=self.product), \
                mock.patch('builtins.print') as said:
            self.assertEqual(tick.cmd_tick(args), 2)
        self.assertIn('go with --dry-run', said.call_args[0][0])


OUT_A = '''tick --dry-run: header
== record
(no change)
== lane
lane: waiting PR #1
== wave
would launch build T-1
== harvest
(nothing)
'''


class AbScriptTests(Fixture):
    def setUp(self):
        super().setUp()
        self.a = os.path.join(self.tmp, 'venvs', 'a')
        self.b = os.path.join(self.tmp, 'venvs', 'b')
        for v in (self.a, self.b):
            _script(os.path.join(v, 'bin', 'python'), 'exit 0\n')
        self.out_b = os.path.join(self.tmp, 'b.out')
        with open(self.out_b, 'w') as f:
            f.write(OUT_A)
        with open(os.path.join(self.tmp, 'a.out'), 'w') as f:
            f.write(OUT_A)
        # the orchestrator: prints a.out or b.out by the --with-venv it was given, and fails
        # unless every run names one --state-copy
        self.cli = _script(os.path.join(self.tmp, 'fake-cli'), f'''
venv=""; copy=""; prev=""
for x in "$@"; do
  [ "$prev" = --with-venv ] && venv=$x
  [ "$prev" = --state-copy ] && copy=$x
  prev=$x
done
[ -n "$copy" ] || exit 9
echo "$copy" >> {self.tmp}/copies
cat {self.tmp}/$(basename "$venv").out
echo "gate copy /var/x/asf-dry-run-$(basename "$venv")9z/state/record held"
''')

    def ab(self, script, *args, extra=None):
        child = dict(os.environ, ASF_HOME=self.home, ASF_AB_CLI=self.cli, **(extra or {}))
        child.pop('PYTHONPATH', None)
        return subprocess.run([script, 'sample', *args], capture_output=True, text=True,
                              env=child, timeout=60)

    def test_identical_runs_exit_0_on_one_state_copy(self):
        p = self.ab(AB_DRY, self.a, self.b)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('identical', p.stdout)
        with open(os.path.join(self.tmp, 'copies')) as f:
            copies = f.read().split()
        self.assertEqual(len(copies), 2)
        self.assertEqual(copies[0], copies[1])

    def test_a_new_would_line_under_b_exits_1(self):
        with open(self.out_b, 'a') as f:
            f.write('would launch build T-2\n')
        p = self.ab(AB_DRY, self.a, self.b)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn('NEW would-lines under B', p.stdout)
        self.assertIn('T-2', p.stdout)

    def test_a_diff_with_no_new_would_line_exits_0(self):
        with open(self.out_b, 'w') as f:
            f.write(OUT_A.replace('lane: waiting PR #1', 'lane: waiting PR #1 (checks)'))
        p = self.ab(AB_DRY, self.a, self.b)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn('B adds no would-line', p.stdout)

    def test_pin_names_the_products_pinned_venv(self):
        os.makedirs(os.path.join(self.home, 'state', 'sample'), exist_ok=True)
        with open(os.path.join(self.home, 'state', 'sample', 'install.json'), 'w') as f:
            f.write('{"sha": "abc", "venv": "%s"}' % self.a)
        p = self.ab(AB_DRY, 'pin', 'pin')
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(f'A {self.a}', p.stdout)

    def test_ci_queue_identical_0_and_differing_1(self):
        for v, text in ((self.a, 'line 1 — would start'), (self.b, 'line 1 — would start')):
            _script(os.path.join(v, 'bin', 'python'),
                    f'[ "$*" = "-m asf.cli ci queue --product sample" ] || exit 7\n'
                    f'[ -z "$PYTHONPATH" ] || exit 8\necho "{text}"\n')
        p = self.ab(AB_CIQ, self.a, self.b, extra={'PYTHONPATH': '/x'})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('identical', p.stdout)
        _script(os.path.join(self.b, 'bin', 'python'), 'echo "line 1 — waits: busy"\n')
        p = self.ab(AB_CIQ, self.a, self.b)
        self.assertEqual(p.returncode, 1)
        self.assertIn('+line 1 — waits: busy', p.stdout)

    def test_an_unknown_venv_is_exit_2(self):
        p = self.ab(AB_DRY, os.path.join(self.tmp, 'nope'), self.b,
                    extra={'PIPX_HOME': os.path.join(self.tmp, 'pipx')})
        self.assertEqual(p.returncode, 2)


if __name__ == '__main__':
    unittest.main()
