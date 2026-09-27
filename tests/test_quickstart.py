"""tests/test_quickstart.py — ``tools/quickstart.sh`` run the way a stranger runs it: a bare
clone, no account, no network, no ``gh``. One ``bash tools/quickstart.sh <dir>`` subprocess under
:func:`asf.hermetic.build` (so the developer's own home is out of reach), with the same fake-CLI
stub directory and offline ``gh`` stub ``tests/test_sample_product.py`` builds — plus one more
stub, ``asf`` itself, so every ``asf`` this checkout's own git hooks and worker sessions shell out
to (never only the ones this script calls directly) resolves to *this* checkout, not whatever
happens to be pipx-installed on the machine running the suite.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from asf import hermetic

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_quickstart` does not
    from test_scheduler import fake_clis
except ImportError:  # pragma: no cover - import shape only
    from tests.test_scheduler import fake_clis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'tools', 'quickstart.sh')


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class QuickstartTest(unittest.TestCase):
    """One ``setUpClass`` running the whole script once; each test reads what it left."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf_quickstart_'))
        # an owned, empty TMPDIR (the machine running the suite has other sessions of its own
        # dropping and clearing scratch dirs in the shared system one at the same time — this is
        # `tests.test_sample_product.test_the_evidence_cache_is_under_the_products_state`'s own
        # pattern): anything the script writes under a stray default `mktemp`, not the `<dir>`
        # argument it was given, lands here instead, and the case below reads it back empty.
        cls.owned_tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf_quickstart_owned_tmp_'))

        stub_dir = os.path.join(cls.tmp, 'bin')
        fake_clis(stub_dir)
        with open(os.path.join(stub_dir, 'gh'), 'w') as f:
            f.write('#!/bin/sh\necho "gh: offline in tests" >&2\nexit 1\n')
        os.chmod(os.path.join(stub_dir, 'gh'), 0o755)
        # an `asf` first on PATH too — a stranger without pipx falls back to `python3 -m
        # asf.cli` for the commands this script runs directly (already exercised by hand: the
        # script's own detection is a two-line `command -v`), but a git hook or a worker session
        # shells out to the literal name (`asf.hooks.runnable_asf`), and that must be *this*
        # checkout too, not a stale install elsewhere on the machine running the suite.
        with open(os.path.join(stub_dir, 'asf'), 'w') as f:
            f.write(f'#!/bin/sh\nexec {sys.executable} -m asf.cli "$@"\n')
        os.chmod(os.path.join(stub_dir, 'asf'), 0o755)

        path = stub_dir + os.pathsep + os.environ.get('PATH', '')
        cls.env = hermetic.build(dict(os.environ, GH_TOKEN='', PATH=path, TMPDIR=cls.owned_tmp),
                                 home=cls.tmp)

        started = time.monotonic()
        cls.result = subprocess.run(['bash', SCRIPT, cls.tmp], cwd=cls.tmp, env=cls.env,
                                    capture_output=True, text=True, timeout=1200)
        cls.wall_seconds = time.monotonic() - started

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(cls.owned_tmp, ignore_errors=True)

    def record(self):
        return os.path.join(self.tmp, '.ASF', 'state', 'sample', 'record')

    def worktrees(self):
        return os.path.join(self.tmp, '.ASF', 'state', 'sample', 'worktrees')

    # ---- the run itself --------------------------------------------------------------------

    def test_exits_0(self):
        self.assertEqual(self.result.returncode, 0, self.result.stdout + self.result.stderr)

    def test_first_line_is_the_directory_it_prints_before_anything_else(self):
        self.assertEqual(self.result.stdout.splitlines()[0], self.tmp)

    def test_nine_steps_run_in_order(self):
        seen = [int(m.group(1)) for m in
                re.finditer(r'^quickstart: (\d)/9 ', self.result.stdout, re.M)]
        self.assertEqual(seen, list(range(1, 10)), self.result.stdout)

    def test_last_line_reports_the_elapsed_seconds_within_the_twenty_minute_bound(self):
        last = self.result.stdout.rstrip('\n').splitlines()[-1]
        m = re.match(r'^quickstart: done in (\d+)s$', last)
        self.assertIsNotNone(m, last)
        self.assertLess(int(m.group(1)), 1200, last)
        self.assertLess(self.wall_seconds, 1200, self.wall_seconds)

    def test_wrote_nothing_outside_the_directory_it_was_given(self):
        self.assertEqual(sorted(os.listdir(self.owned_tmp)), [], 'quickstart wrote under TMPDIR')

    # ---- the artefacts the script names ------------------------------------------------------

    def test_the_backlog_index_exists(self):
        index = os.path.join(self.tmp, 'sample', 'backlog', 'index.json')
        self.assertTrue(os.path.isfile(index), index)

    def test_the_tick_commit_is_in_the_record_clone(self):
        subjects = _git(['log', '--format=%s', 'origin/main'], cwd=self.record()).splitlines()
        self.assertTrue([s for s in subjects if s.startswith('tick: state')], subjects)

    def test_the_bug_launched_on_its_bugfix_branch(self):
        worktrees = self.worktrees()
        jobs = [d for d in os.listdir(worktrees) if os.path.isdir(os.path.join(worktrees, d))]
        self.assertTrue(jobs, f'no worktree under {worktrees}')
        branches = {_git(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=os.path.join(worktrees, j))
                   for j in jobs}
        self.assertIn('bugfix/B-0001', branches, branches)

    def test_doctor_ran_clean(self):
        self.assertIn('quickstart: 7/9 checking the install (asf doctor)', self.result.stdout)
        self.assertNotIn('quickstart: doctor is red', self.result.stderr)
