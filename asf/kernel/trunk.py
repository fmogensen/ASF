"""asf.kernel.trunk — the host's probe of origin's trunk for :mod:`asf.kernel.resolvers` (ASF 0.2).

:meth:`TrunkProbe.probe` takes the :class:`~asf.kernel.resolvers.Probe` values the tick's questions
matched and returns ``{probe key: result}``:

- ``trunk-tests``: ``git fetch origin <main>``, a fresh detached worktree of ``origin/<main>``
  under ``state/<product>/resolve-tmp/``, ``<python> -m unittest -v <ids>`` in it (the hermetic
  environment, no ``ASF_*`` variable of the factory's own, ``test_timeout_s`` and the whole
  process group killed past it), then the worktree removed and pruned — the clean floor; a
  leftover from a crashed tick is swept first. ``{sha, ran, failed, ok}``; ``error`` when it could
  not tell (a timeout, a test id that does not load, nothing ran).
- ``symbol``: the dotted name's module read at ``origin/<main>`` (``git show``, no worktree) and
  walked with :mod:`ast`: ``{sha, exists, where (path:line), path, defined (its top-level
  classes)}``; ``error`` when no module of the name is at trunk.

Each result is kept in ``state/<product>/kernel-resolve.json`` by probe key (a question asked again
is not re-run); at most ``test_runs_per_tick`` test runs happen per tick, the rest wait for the
next. A dry run (:mod:`asf.mutation_guard` active) fetches nothing and runs no test; it reads
symbols off the local ``origin/<main>`` and the cache. A disabled class is never probed.
"""
import ast
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from asf.kernel import resolvers as R

CACHE_FILE = 'kernel-resolve.json'
TMP_DIR = 'resolve-tmp'

#: a cached result older than this is dropped (seconds)
CACHE_TTL_S = 7 * 86400

#: ``FAIL: test_c (tests.test_x.Red.test_c)`` / ``ERROR: …`` in unittest's output
FAILED_RE = re.compile(r'^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)', re.M)
RAN_RE = re.compile(r'^Ran (\d+) tests? in ', re.M)
#: unittest's stand-in for a test id that does not load
LOAD_FAILED = 'unittest.loader._FailedTest'


class TrunkProbe:
    """The probe of ``repo``'s ``origin/<main>`` (see the module doc)."""

    def __init__(self, repo, main, state_dir, python='python3', timeout_s=600,
                 trunk_tests=True, symbols=True, test_runs_per_tick=1):
        self.repo, self.main, self.state_dir = repo, main or 'main', state_dir
        self.python, self.timeout_s = python, timeout_s
        self.enabled = {R.TRUNK_TESTS: trunk_tests, R.SYMBOL: symbols}
        self.test_runs_per_tick = test_runs_per_tick

    @classmethod
    def for_product(cls, product, state_dir):
        k = product.kernel['resolve']
        return cls(product.repo_dir, product.main, state_dir, python=k['python'],
                   timeout_s=int(k['test_timeout_s']), trunk_tests=bool(k['trunk_tests']),
                   symbols=bool(k['symbols']), test_runs_per_tick=int(k['test_runs_per_tick']))

    # ---- the cache --------------------------------------------------------------------------

    def _cache_path(self):
        return os.path.join(self.state_dir, CACHE_FILE)

    def _load(self):
        try:
            with open(self._cache_path(), encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        now = time.time()
        return {k: v for k, v in data.items() if isinstance(v, dict)
                and now - float(v.get('at') or 0) < CACHE_TTL_S} if isinstance(data, dict) else {}

    def _save(self, cache):
        os.makedirs(self.state_dir, exist_ok=True)
        path = self._cache_path()
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=1, sort_keys=True)
        os.replace(path + '.tmp', path)

    # ---- the probe ----------------------------------------------------------------------------

    def probe(self, probes):
        """``{probe key: result}`` for ``probes`` (see the module doc)."""
        from asf import mutation_guard
        dry = mutation_guard.is_active()
        probes = [p for p in dict.fromkeys(probes) if self.enabled.get(p.cls)]
        if not probes or not self.repo:
            return {}
        cache = self._load()
        out, fetched, runs, sha = {}, False, 0, None
        for p in probes:
            if p.key in cache:
                out[p.key] = cache[p.key]
                continue
            if p.cls == R.TRUNK_TESTS and (dry or runs >= self.test_runs_per_tick):
                continue
            if not fetched and not dry:
                from asf import gitops
                gitops.git(['fetch', '-q', 'origin', self.main], self.repo)
                fetched = True
            sha = sha or self._trunk_sha()
            if not sha:
                return out
            if p.cls == R.TRUNK_TESTS:
                runs += 1
                result = self._run_tests(sha, p.target)
            else:
                result = self._symbol(sha, p.target[0])
            result.update(sha=sha, at=time.time())
            out[p.key] = cache[p.key] = result
            if not dry:
                self._save(cache)
        return out

    def _trunk_sha(self):
        from asf import gitops
        return gitops.rev_parse(self.repo, 'origin/%s' % self.main) or ''

    # ---- trunk-tests --------------------------------------------------------------------------

    def _sweep(self, root):
        """Remove every leftover probe worktree under ``root`` (a crashed tick's) and prune."""
        from asf import gitops
        for name in os.listdir(root) if os.path.isdir(root) else ():
            path = os.path.join(root, name)
            gitops.git(['worktree', 'remove', '--force', path], self.repo)
            shutil.rmtree(path, ignore_errors=True)
        gitops.git(['worktree', 'prune'], self.repo)

    def _run_tests(self, sha, ids):
        from asf import gitops
        root = os.path.join(self.state_dir, TMP_DIR)
        os.makedirs(root, exist_ok=True)
        self._sweep(root)
        wt = tempfile.mkdtemp(prefix='trunk-', dir=root)
        try:
            r = gitops.git(['worktree', 'add', '-q', '--force', '--detach', wt, sha], self.repo)
            if not r.ok:
                return {'error': 'worktree add: %s' % r.reason}
            return self._unittest(wt, ids)
        finally:
            gitops.git(['worktree', 'remove', '--force', wt], self.repo)
            shutil.rmtree(wt, ignore_errors=True)
            gitops.git(['worktree', 'prune'], self.repo)

    def _env(self, wt):
        from asf import hermetic
        env = hermetic.build(worktree=wt, trunk=self.main, pythonpath=False)
        for var in [v for v in env if v.startswith('ASF_')]:
            env.pop(var)
        env['PYTHONPATH'] = wt
        return env

    def _unittest(self, wt, ids):
        try:
            p = subprocess.Popen([self.python, '-m', 'unittest', '-v', *ids], cwd=wt,
                                 env=self._env(wt), stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, start_new_session=True)
        except OSError as e:
            return {'error': 'cannot run %s: %s' % (self.python, e)}
        try:
            text, _ = p.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.communicate()
            return {'error': 'timeout after %ds' % self.timeout_s}
        return parse_unittest(text, p.returncode)

    # ---- symbol --------------------------------------------------------------------------------

    def _show(self, sha, path):
        from asf import gitops
        r = gitops.git(['show', '%s:%s' % (sha, path)], self.repo)
        return r.stdout if r.ok else None

    def _symbol(self, sha, name):
        parts = name.split('.')
        for n in range(len(parts), 0, -1):
            base = '/'.join(parts[:n])
            for path in (base + '.py', base + '/__init__.py'):
                src = self._show(sha, path)
                if src is not None:
                    return symbol_in(src, path, parts[n:])
        return {'error': 'no module of %s at trunk' % name}


def parse_unittest(text, rc):
    """``{ran, failed, ok}`` of ``python -m unittest -v`` output, or ``{error}`` when a test id
    did not load or nothing ran."""
    if LOAD_FAILED in text:
        return {'error': 'a test id does not load at trunk'}
    m = RAN_RE.findall(text)
    ran = int(m[-1]) if m else 0
    if not ran:
        return {'error': 'no test ran (rc %s)' % rc}
    failed = []
    for short, full in FAILED_RE.findall(text):
        tid = full if full.endswith('.' + short) else '%s.%s' % (full, short)
        if tid not in failed:
            failed.append(tid)
    return {'ran': ran, 'failed': failed, 'ok': rc == 0 and not failed}


def symbol_in(src, path, attrs):
    """``{exists, where, path, defined}`` of the ``attrs`` chain in module source ``src``."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {'error': 'cannot parse %s: %s' % (path, e)}
    defined = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    out = {'path': path, 'defined': defined}
    node, line = tree, 1
    for a in attrs:
        found = None
        for child in getattr(node, 'body', ()):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and child.name == a:
                found = child
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                if any(isinstance(t, ast.Name) and t.id == a for t in targets):
                    found = child
        if found is None:
            return dict(out, exists=False)
        node, line = found, found.lineno
    return dict(out, exists=True, where='%s:%d' % (path, line))
