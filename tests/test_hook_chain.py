"""tests.test_hook_chain — F-0121 Task 1: the two chained bodies, and the branch that renames a
foreign hook behind them.

Follows ``tests.test_install``'s ``GitHookTests``: real ``git init`` repos, a stand-in ``asf`` in
place of the redaction scanner, and the installed hooks run for real through ``git commit`` and
``git push`` — because "stdin reaches both" is only provable by running them. The stand-in ``asf``
here is its own copy (:func:`_stub_asf`), not imported from ``tests.test_install`` (PD9): no test
module in this suite imports another's helpers.
"""
import glob
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env, hooks

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(args, cwd=None):
    clean = {k: v for k, v in os.environ.items() if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')}
    return subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True,
                          env=clean).stdout.strip()


#: A literal that stands in for a secret — no real vendor shape, so
#: ``check_generic.sh``/``check_conventions.sh`` stay clean on this file itself.
_MARKER = 'THE-SECRET-MARKER'


def _stub_asf(bin_dir):
    """A stand-in ``asf`` executable for this module's fixtures (PD9, copied from
    ``tests.test_install._stub_asf`` rather than imported): answers
    ``redact --pre-commit|--pre-push --product <p>``, refusing when the change it is asked about
    carries :data:`_MARKER` (never printed itself). The ``--pre-push`` half also writes the raw
    stdin bytes it read to ``.git-gate-stdin.log`` at the hook's own current directory (the
    working tree root git runs it from), so a test can compare what the gate saw on stdin to what
    the chained hook's foreign half saw of the same buffered file (``StdinReachesBothTests``)."""
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, 'asf')
    with open(path, 'w') as f:
        f.write(f'''#!/usr/bin/env python3
import subprocess, sys

def refuse():
    print('redact: refused — 1 finding(s)')
    print('x:1: secret (rule:stand-in)')
    sys.exit(1)

if sys.argv[1:3] == ['redact', '--pre-commit']:
    diff = subprocess.run(['git', 'diff', '--cached', '-U0'], capture_output=True, text=True).stdout
    refuse() if {_MARKER!r} in diff else sys.exit(0)
elif sys.argv[1:3] == ['redact', '--pre-push']:
    data = sys.stdin.buffer.read()
    with open('.git-gate-stdin.log', 'wb') as log:
        log.write(data)
    found = False
    for line in data.decode().splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] == '0' * 40:
            continue
        shown = subprocess.run(['git', 'show', parts[1]], capture_output=True, text=True).stdout
        found = found or {_MARKER!r} in shown
    refuse() if found else sys.exit(0)
else:
    sys.exit(0)
''')
    os.chmod(path, 0o755)
    return path


#: The line a foreign hook's log (:func:`_write_foreign_hook`) puts between two invocations'
#: records — never a byte git or the gate stand-in would themselves write into that log.
_REC_END = '===FOREIGN-HOOK-RECORD-END==='


def _write_foreign_hook(path, log, rc=0):
    """An executable at ``path`` standing in for the operator's own hook (PD9): each invocation
    appends one record to ``log`` — its arguments, then the raw bytes it read on stdin, then a
    line reading :data:`_REC_END` — and exits ``rc``. What proves the chain really ran, and that
    the failing half's status is the hook's, is bytes on this file, not an exit code alone."""
    quoted = log.replace("'", "'\\''")
    with open(path, 'w') as f:
        f.write(f'''#!/bin/sh
{{
  printf 'ARGS'
  for a in "$@"; do printf '\\t%s' "$a"; done
  printf '\\n'
  cat
  printf '\\n{_REC_END}\\n'
}} >> '{quoted}'
exit {rc}
''')
    os.chmod(path, 0o755)


def _read_records(log):
    """``[(args, stdin)]`` logged by :func:`_write_foreign_hook`, one tuple per invocation, in
    order — ``args`` a tuple of strings, ``stdin`` the exact bytes (as ``str``) it read."""
    if not os.path.isfile(log):
        return []
    with open(log, encoding='utf-8') as f:
        data = f.read()
    out = []
    for chunk in data.split(_REC_END + '\n'):
        if not chunk:
            continue
        first, _, rest = chunk.partition('\n')
        assert rest.endswith('\n'), rest   # the printf '\n' this module's helper always adds
        stdin = rest[:-1]
        args = tuple(first[len('ARGS'):].split('\t')[1:])
        out.append((args, stdin))
    return out


class _HookChainCase(unittest.TestCase):
    """Shared fixture: a tmpdir, the stand-in ``asf``, and helpers to build a repo with a
    *foreign* hook already in the way, then chain it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hook_chain_test_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.asf_path = _stub_asf(os.path.join(self.tmp, 'bin'))
        self.which = lambda name: self.asf_path if name == 'asf' else None

    def _repo(self, name, bare=False):
        path = os.path.join(self.tmp, name)
        args = ['init', '-q', '-b', 'main']
        if bare:
            args.append('--bare')
        _git(args + [path])
        if not bare:
            _git(['config', 'user.email', 'test@example.com'], path)
            _git(['config', 'user.name', 'test'], path)
        return path

    def _product(self, repo_dir=None, backlog_dir=None, name='sample'):
        data = {}
        if repo_dir:
            data['repo_dir'] = repo_dir
        if backlog_dir:
            data['backlog_dir'] = backlog_dir
        return env.Product(name, data)

    def _foreign(self, repo, name, log=None, rc=0, hooks_dir=None):
        """Writes a foreign (non-asf) hook ``name`` into ``repo``'s real hooks directory,
        appending its invocations to ``log`` (a fresh path under ``self.tmp`` when None).
        Returns ``(hook_path, log_path)``."""
        d = hooks_dir or hooks.git_hooks_dir(repo)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        log = log or os.path.join(self.tmp, f'{name}-{os.path.basename(repo)}.log')
        _write_foreign_hook(path, log, rc=rc)
        return path, log

    def _chain(self, repo, name, log=None, rc=0):
        """A foreign hook ``name`` is written into ``repo``, then chained via
        :func:`hooks.ensure_git_hooks`. Returns ``(product, hook_path, local_path, log_path, ok, detail)``."""
        hooks_dir = hooks.git_hooks_dir(repo)
        path, log = self._foreign(repo, name, log=log, rc=rc, hooks_dir=hooks_dir)
        product = self._product(repo_dir=repo)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which, chain=True)
        return product, path, path + hooks.LOCAL_SUFFIX, log, ok, detail


class ForeignPreCommitIsChainedTests(_HookChainCase):
    """S-32700: a foreign ``pre-commit`` is renamed to ``pre-commit.local``, ASF's gate runs
    first, and a real ``git commit`` really runs both halves."""

    def test_the_foreign_hook_is_renamed_and_asf_writes_its_own(self):
        repo = self._repo('repo')
        path, log = self._foreign(repo, 'pre-commit')
        os.chmod(path, 0o700)   # a mode distinct from _write_hook's 0o755, to prove it survives
        with open(path, 'rb') as f:
            before = f.read()
        before_mode = os.stat(path).st_mode & 0o777
        product = self._product(repo_dir=repo)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which, chain=True)
        self.assertTrue(ok, detail)
        local = path + hooks.LOCAL_SUFFIX
        with open(local, 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.stat(local).st_mode & 0o777, before_mode)
        self.assertIn(f'chained {path} → {local}', detail)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(hooks.is_git_hook_ours(text, 'pre-commit'))
        self.assertEqual(hooks.chained_local(text, 'pre-commit'), 'pre-commit.local')

    def test_both_halves_run_on_a_clean_commit(self):
        repo = self._repo('repo')
        product, path, local, log, ok, detail = self._chain(repo, 'pre-commit')
        self.assertTrue(ok, detail)
        with open(os.path.join(repo, 'clean.txt'), 'w') as f:
            f.write('clean\n')
        _git(['add', 'clean.txt'], repo)
        r = subprocess.run(['git', 'commit', '-q', '-m', 'clean'], cwd=repo,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        records = _read_records(log)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][0], ())   # pre-commit gets no positional arguments

    def test_the_gates_refusal_stops_the_foreign_half(self):
        repo = self._repo('repo')
        product, path, local, log, ok, detail = self._chain(repo, 'pre-commit')
        self.assertTrue(ok, detail)
        with open(os.path.join(repo, 'secret.txt'), 'w') as f:
            f.write(_MARKER + '\n')
        _git(['add', 'secret.txt'], repo)
        before = _git(['rev-parse', '--is-inside-work-tree'], repo)
        r = subprocess.run(['git', 'commit', '-q', '-m', 'wip'], cwd=repo,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(_MARKER, r.stdout + r.stderr)
        self.assertEqual(_read_records(log), [])   # the foreign half never ran

    def test_a_failing_foreign_half_fails_the_commit(self):
        repo = self._repo('repo')
        product, path, local, log, ok, detail = self._chain(repo, 'pre-commit', rc=1)
        self.assertTrue(ok, detail)
        with open(os.path.join(repo, 'clean.txt'), 'w') as f:
            f.write('clean\n')
        _git(['add', 'clean.txt'], repo)
        r = subprocess.run(['git', 'commit', '-q', '-m', 'clean'], cwd=repo,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(_read_records(log)), 1)   # it did run — and its failure is the hook's

    def test_the_chain_works_from_a_tracked_core_hooks_path_directory(self):
        repo = self._repo('repo')
        custom = os.path.join(repo, '.githooks')
        os.makedirs(custom)
        _git(['config', 'core.hooksPath', '.githooks'], repo)
        product, path, local, log, ok, detail = self._chain(repo, 'pre-commit')
        self.assertTrue(ok, detail)
        self.assertEqual(path, os.path.join(custom, 'pre-commit'))
        with open(os.path.join(repo, 'clean.txt'), 'w') as f:
            f.write('clean\n')
        _git(['add', 'clean.txt'], repo)
        r = subprocess.run(['git', 'commit', '-q', '-m', 'clean'], cwd=repo,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(_read_records(log)), 1)

    def test_generated_bodies_pass_sh_dash_n(self):
        for name in hooks.GIT_HOOK_NAMES:
            body = hooks._git_hook_body(name, '/abs/asf', 'sample', chained=True)
            r = subprocess.run(['sh', '-n', '-c', body], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, (name, r.stderr))


class ForeignPrePushIsChainedTests(_HookChainCase):
    """S-32701: a foreign ``pre-push`` is renamed to ``pre-push.local``, ASF's gate runs first,
    and a real ``git push`` against a bare origin really runs both halves."""

    def _clone_with_foreign_pre_push(self, rc=0):
        origin = self._repo('origin', bare=True)
        repo = os.path.join(self.tmp, 'clone')
        _git(['clone', '-q', origin, repo])
        _git(['config', 'user.email', 'test@example.com'], repo)
        _git(['config', 'user.name', 'test'], repo)
        with open(os.path.join(repo, 'seed'), 'w') as f:
            f.write('seed\n')
        _git(['add', 'seed'], repo)
        _git(['commit', '-q', '--no-verify', '-m', 'seed'], repo)
        _git(['push', '-q', '-u', 'origin', 'HEAD:main'], repo)
        product, path, local, log, ok, detail = self._chain(repo, 'pre-push', rc=rc)
        self.assertTrue(ok, detail)
        return origin, repo, path, local, log

    def test_the_foreign_hook_is_renamed_and_asf_writes_its_own(self):
        origin, repo, path, local, log = self._clone_with_foreign_pre_push()
        with open(local, encoding='utf-8') as f:
            self.assertNotIn(hooks.HOOK_MARKER, f.read())   # the operator's own, untouched
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(hooks.is_git_hook_ours(text, 'pre-push'))
        self.assertEqual(hooks.chained_local(text, 'pre-push'), 'pre-push.local')

    def test_both_halves_run_on_a_clean_push(self):
        origin, repo, path, local, log = self._clone_with_foreign_pre_push()
        with open(os.path.join(repo, 'clean.txt'), 'w') as f:
            f.write('clean\n')
        _git(['add', 'clean.txt'], repo)
        _git(['commit', '-q', '--no-verify', '-m', 'clean'], repo)
        r = subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=repo,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        records = _read_records(log)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][0], ('origin', origin))

    def test_a_marker_in_the_pushed_commit_is_refused_with_the_foreign_half_never_reached(self):
        origin, repo, path, local, log = self._clone_with_foreign_pre_push()
        before = _git(['rev-parse', 'main'], origin)
        with open(os.path.join(repo, 'secret.txt'), 'w') as f:
            f.write(_MARKER + '\n')
        _git(['add', 'secret.txt'], repo)
        _git(['commit', '-q', '--no-verify', '-m', 'wip'], repo)
        r = subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=repo,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn(_MARKER, r.stdout + r.stderr)
        self.assertEqual(_read_records(log), [])
        self.assertEqual(_git(['rev-parse', 'main'], origin), before)   # nothing pushed

    def test_a_failing_foreign_half_fails_the_push(self):
        origin, repo, path, local, log = self._clone_with_foreign_pre_push(rc=1)
        with open(os.path.join(repo, 'clean.txt'), 'w') as f:
            f.write('clean\n')
        _git(['add', 'clean.txt'], repo)
        _git(['commit', '-q', '--no-verify', '-m', 'clean'], repo)
        r = subprocess.run(['git', 'push', 'origin', 'HEAD:main'], cwd=repo,
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(_read_records(log)), 1)   # it did run — its failure is the push's


class StdinReachesBothTests(_HookChainCase):
    """S-32701: the chained ``pre-push`` buffers stdin once and both halves read the same
    bytes, whatever git's own protocol shape — one ref, several, a deletion, or none at all —
    and the hook's own ``"$@"`` reaches the foreign half unchanged. Invokes the installed hook
    script directly, so the stdin it is given is exact and controlled."""

    def setUp(self):
        super().setUp()
        self.repo = self._repo('repo')
        self.product, self.path, self.local, self.log, ok, detail = self._chain(self.repo, 'pre-push')
        self.assertTrue(ok, detail)
        self.gate_log = os.path.join(self.repo, '.git-gate-stdin.log')
        with open(os.path.join(self.repo, 'a'), 'w') as f:
            f.write('a\n')
        _git(['add', 'a'], self.repo)
        _git(['commit', '-q', '--no-verify', '-m', 'a'], self.repo)
        self.c1 = _git(['rev-parse', 'HEAD'], self.repo)
        with open(os.path.join(self.repo, 'b'), 'w') as f:
            f.write('b\n')
        _git(['add', 'b'], self.repo)
        _git(['commit', '-q', '--no-verify', '-m', 'b'], self.repo)
        self.c2 = _git(['rev-parse', 'HEAD'], self.repo)

    def _invoke(self, stdin, args=('origin', 'https://example.invalid/repo.git'), env=None):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        return subprocess.run(['sh', self.path, *args], cwd=self.repo, input=stdin,
                              capture_output=True, env=full_env)

    def test_one_ref(self):
        line = f'refs/heads/main {self.c2} refs/heads/main {self.c1}\n'.encode()
        r = self._invoke(line)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.gate_log, 'rb') as f:
            self.assertEqual(f.read(), line)
        records = _read_records(self.log)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1].encode(), line)

    def test_two_refs_in_one_push(self):
        lines = (f'refs/heads/main {self.c2} refs/heads/main {self.c1}\n'
                 f'refs/heads/other {self.c1} refs/heads/other {"0" * 40}\n').encode()
        r = self._invoke(lines)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.gate_log, 'rb') as f:
            self.assertEqual(f.read(), lines)
        records = _read_records(self.log)
        self.assertEqual(records[0][1].encode(), lines)

    def test_a_deletion_is_dropped_by_the_gates_scan_but_still_reaches_the_foreign_half(self):
        line = f'(delete) {"0" * 40} refs/heads/gone {self.c1}\n'.encode()
        r = self._invoke(line)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)   # nothing scanned, nothing refused
        with open(self.gate_log, 'rb') as f:
            self.assertEqual(f.read(), line)   # buffered whole, not filtered before the gate runs
        records = _read_records(self.log)
        self.assertEqual(records[0][1].encode(), line)

    def test_empty_stdin_reaches_the_foreign_half_as_zero_bytes(self):
        r = self._invoke(b'')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.gate_log, 'rb') as f:
            self.assertEqual(f.read(), b'')
        records = _read_records(self.log)
        self.assertEqual(records[0][1], '')   # not one blank line

    def test_the_hooks_own_arguments_reach_the_foreign_half_unchanged(self):
        r = self._invoke(b'', args=('origin', 'git@example.invalid:x/y.git'))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        records = _read_records(self.log)
        self.assertEqual(records[0][0], ('origin', 'git@example.invalid:x/y.git'))

    def test_no_temp_file_is_left_under_tmpdir_after_a_push(self):
        tmpdir = os.path.join(self.tmp, 'tmpdir')
        os.makedirs(tmpdir)
        r = self._invoke(b'', env={'TMPDIR': tmpdir})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(glob.glob(os.path.join(tmpdir, 'asf-pre-push.*')), [])

    def test_a_tmpdir_that_cannot_be_written_to_refuses_the_push_without_running_the_gate(self):
        nowhere = os.path.join(self.tmp, 'no-such-tmpdir')
        r = self._invoke(b'refs/heads/main deadbeef refs/heads/main deadbeef\n',
                         env={'TMPDIR': nowhere})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.gate_log))
        self.assertEqual(_read_records(self.log), [])


class ChainingIsIdempotentTests(_HookChainCase):
    """S-32702: a second ``ensure_git_hooks(…, chain=True)`` over the result of the first changes
    nothing — the hook's bytes, the ``.local``'s bytes and both mtimes are equal before and
    after — and a chained hook is not an upgrade candidate."""

    def _snapshot(self, *paths):
        out = {}
        for p in paths:
            with open(p, 'rb') as f:
                data = f.read()
            out[p] = (data, os.stat(p).st_mtime_ns)
        return out

    def test_a_second_and_third_run_change_nothing(self):
        repo = self._repo('repo')
        product, path, local, log, ok, detail = self._chain(repo, 'pre-commit')
        self.assertTrue(ok, detail)
        self.assertIn('chained', detail)
        before = self._snapshot(path, local)
        for _ in range(2):
            ok, detail = hooks.ensure_git_hooks(product, which=self.which, chain=True)
            self.assertTrue(ok, detail)
            self.assertEqual(detail, 'pre-commit, pre-push in 1 repos')   # no rename clause
            self.assertNotIn('chained', detail)
            after = self._snapshot(path, local)
            self.assertEqual(after, before)

    def test_a_chained_pre_commit_is_not_an_upgrade_candidate(self):
        repo = self._repo('repo')
        product, path, local, log, ok, detail = self._chain(repo, 'pre-commit')
        self.assertTrue(ok, detail)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIsNone(hooks.init_hook_upgrade(text, 'pre-commit'))
        self.assertIsNone(hooks.staged_check_upgrade(text, 'pre-commit'))


class ChainingIsNeverUnattendedTests(_HookChainCase):
    """S-32702: an unattended caller must never rename a file in the operator's repo. Task 1's
    two function-level cases (PD3) — the CLI's own ``--no-chain`` is Task 2's, appended here."""

    def test_chain_false_over_a_foreign_hook_is_todays_refusal(self):
        repo = self._repo('repo')
        path, log = self._foreign(repo, 'pre-push')
        with open(path, 'rb') as f:
            before = f.read()
        product = self._product(repo_dir=repo)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which, chain=False)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith('NEEDS OPERATOR: '), detail)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(path + hooks.LOCAL_SUFFIX))

    def test_the_default_keyword_the_spawn_gate_relies_on_still_refuses(self):
        # asf/workers/spawn.py:441 calls ensure_git_hooks(product) with no chain keyword at all —
        # this asserts the default it relies on is still False, not that spawn.py is edited here.
        repo = self._repo('repo')
        path, log = self._foreign(repo, 'pre-push')
        with open(path, 'rb') as f:
            before = f.read()
        product = self._product(repo_dir=repo)
        ok, detail = hooks.ensure_git_hooks(product, which=self.which)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith('NEEDS OPERATOR: '), detail)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(path + hooks.LOCAL_SUFFIX))
