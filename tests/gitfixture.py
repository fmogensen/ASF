"""A git fixture built once, copied per test (B-0071).

A module whose every test set up a bare origin, a seeded clone and a second checkout paid five
to eight git commands a test — most of the suite's time, with the assertions nearly free. A
:class:`Template` runs the module's builder once, the first time a test asks, into a directory of
its own; each test then gets a ``shutil.copytree`` of it under a fresh path, with every absolute
path of the template rewritten in the copies' git configs (the clones' ``origin`` remotes), so
the copy is a self-contained set of repos pointing at each other and never at the template.
Nothing is shared between tests: a test mutates its copy and the template is never touched.
"""
import atexit
import os
import shutil
import subprocess
import tempfile
import threading

#: The files git keeps absolute paths in — a clone's remote URL, a worktree's ``gitdir``.
_REWRITE = ('config', 'gitdir', 'commondir', 'FETCH_HEAD')


class Template:
    def __init__(self, build, prefix='fixture_'):
        self.build = build
        self.prefix = prefix
        self.root = None
        self._lock = threading.Lock()  # built once per process, even from two threads

    def _ensure(self):
        with self._lock:
            return self._ensure_locked()

    def _ensure_locked(self):
        if self.root is None:
            root = tempfile.mkdtemp(prefix=self.prefix + 'template-')
            atexit.register(shutil.rmtree, root, True)
            # no background gc/maintenance from the builder's first commit on: it would repack
            # and prune objects while the first copy walks the tree
            quiet = {'GIT_CONFIG_COUNT': '3',
                     'GIT_CONFIG_KEY_0': 'gc.auto', 'GIT_CONFIG_VALUE_0': '0',
                     'GIT_CONFIG_KEY_1': 'gc.autoDetach', 'GIT_CONFIG_VALUE_1': 'false',
                     'GIT_CONFIG_KEY_2': 'maintenance.auto', 'GIT_CONFIG_VALUE_2': 'false'}
            saved = {k: os.environ.get(k) for k in quiet}
            os.environ.update(quiet)
            try:
                self.build(root)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            _disable_hooks(root)
            for git_dir in _all_repo_roots(root):
                _no_housekeeping(git_dir)
            self.root = root
        return self.root

    def fresh(self, dest=None):
        """A copy of the template at ``dest`` (an existing, empty directory — or a new temp
        directory when None), its git configs pointing at the copy. Returns ``dest``."""
        root = self._ensure()
        if dest is None:  # ours: removed at exit, like the template (a caller's dir is its own)
            dest = tempfile.mkdtemp(prefix=self.prefix)
            atexit.register(shutil.rmtree, dest, True)
        for attempt in range(3):  # a copy that raced a vanishing file starts over, never half-used
            try:
                self._copy(root, dest)
                break
            except (OSError, shutil.Error):
                if attempt == 2:
                    raise
                for name in os.listdir(dest):
                    full = os.path.join(dest, name)
                    if os.path.isdir(full) and not os.path.islink(full):
                        shutil.rmtree(full, True)
                    else:
                        os.unlink(full)
        _rewrite_paths(dest, {root: dest, os.path.realpath(root): os.path.realpath(dest)})
        return dest


    @staticmethod
    def _copy(root, dest):
        for name in os.listdir(root):
            src = os.path.join(root, name)
            dst = os.path.join(dest, name)
            if os.path.isdir(src) and not os.path.islink(src):
                # a background ``git maintenance``/auto-gc lock can appear and vanish while the
                # copy walks the tree (a CI run failed on objects/maintenance.lock): never copied
                shutil.copytree(src, dst, symlinks=True,
                                ignore=shutil.ignore_patterns('*.lock', 'gc.pid'))
            else:
                shutil.copy2(src, dst, follow_symlinks=False)


def _disable_hooks(root):
    """``core.hooksPath=/dev/null`` on every working tree under ``root``: a fixture's own
    ``git commit`` must never run a real ``pre-commit`` — a product's, or (recursively) the
    suite's own (B-0073). Bare repos are left alone — a fixture that installs a server-side
    hook (``pre-receive``, to test a refused push) relies on git's default hooks dir there."""
    for git_dir in _working_tree_roots(root):
        subprocess.run(['git', 'config', 'core.hooksPath', '/dev/null'], cwd=git_dir, check=True)


def _working_tree_roots(root):
    """Every non-bare git repo directory under ``root`` (has ``.git``). Never descends into a
    ``.git`` directory."""
    roots = []
    for dirpath, dirnames, filenames in os.walk(root):
        if '.git' in dirnames or '.git' in filenames:
            roots.append(dirpath)
            dirnames[:] = [d for d in dirnames if d != '.git']
    return roots


def _all_repo_roots(root):
    """Every git repo directory under ``root``: the working trees :func:`_working_tree_roots`
    finds, plus the bare ones the builders make — a directory named ``*.git`` (equivalently, one
    ``git rev-parse --is-bare-repository`` calls true). The non-e2e fixtures fork templates too,
    and a bare origin's own ``receive-pack`` runs the same ``--auto`` hook on every push it gets."""
    roots = list(_working_tree_roots(root))
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '.git']
        if dirpath.endswith('.git') and dirpath not in roots:
            roots.append(dirpath)
    return roots


def _no_housekeeping(repo):
    """Turn off git's own background writer on ``repo``: a detached ``gc --auto`` or ``git
    maintenance run --auto``, started by a fixture's own commit or push, repacks and prunes loose
    objects while a copy of that fixture walks its ``objects/`` — the same race ``fresh``'s own
    lock-file ignore below already met once (a CI run failed on ``objects/maintenance.lock``)."""
    for key, value in (('gc.auto', '0'), ('gc.autoDetach', 'false'), ('maintenance.auto', 'false')):
        subprocess.run(['git', 'config', key, value], cwd=repo, check=True)


def _rewrite_paths(dest, mapping):
    for dirpath, _dirnames, filenames in os.walk(dest):
        for name in filenames:
            if name not in _REWRITE:
                continue
            full = os.path.join(dirpath, name)
            try:
                with open(full, encoding='utf-8') as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            new = text
            for old, replacement in mapping.items():
                new = new.replace(old, replacement)
            if new != text:
                with open(full, 'w', encoding='utf-8') as f:
                    f.write(new)


def publish(tree, origin, name='fixture', email='fixture@example.com', trunk='main',
            message='fixture'):
    """``tree`` (a directory of files) becomes a git repo whose ``trunk`` is pushed to a new bare
    ``origin`` — the checkout keeps ``origin`` as its remote, with ``origin/HEAD`` set, the way a
    clone has it. Unlike :class:`Template` it installs no ``core.hooksPath``: the end-to-end
    harness (``tests/e2e``) runs the factory's own hook install against the checkout."""
    def git(*args, cwd):
        subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)
    os.makedirs(os.path.dirname(origin), exist_ok=True)
    git('init', '-q', '--bare', '-b', trunk, origin, cwd=os.path.dirname(origin))
    _no_housekeeping(origin)  # `receive-pack` runs the same `--auto` hook on every push it gets
    git('init', '-q', '-b', trunk, cwd=tree)
    _no_housekeeping(tree)  # before the first commit: none of this repo's own can ever start one
    identity(tree, name, email)
    git('add', '-A', cwd=tree)
    git('commit', '-q', '-m', message, cwd=tree)
    git('remote', 'add', 'origin', origin, cwd=tree)
    git('push', '-q', '-u', 'origin', trunk, cwd=tree)
    git('remote', 'set-head', 'origin', trunk, cwd=tree)
    return tree


class OutsideSuite(AssertionError):
    """A test asked to set a git identity in a repo that is not the suite's own (F-0281)."""


def suite_roots():
    """Where a repo the suite made lives: the temp dir (both spellings) and the suite's
    ``ASF_HOME``."""
    roots = {tempfile.gettempdir(), os.path.realpath(tempfile.gettempdir())}
    if os.environ.get('ASF_HOME'):
        roots.add(os.path.realpath(os.environ['ASF_HOME']))
    return sorted(r for r in roots if r and r != os.sep)


def identity(repo, name='Test', email='test@example.com'):
    """Set ``repo``'s own git identity — the one sanctioned ``git config user.*`` site in the
    suite (F-0281). The suite's global config already carries an identity
    (:func:`asf.hermetic.suite_git_identity`), so only a test that needs a *different* author
    calls this. ``repo`` must resolve under :func:`suite_roots`: a cwd that resolved to a real
    repo once wrote ``Test <test@example.com>`` into an operator's record, and every record
    commit after it was authored by the suite. Raises :class:`OutsideSuite` otherwise."""
    real = os.path.realpath(repo)
    if not any(real.startswith(root + os.sep) for root in suite_roots()):
        raise OutsideSuite(f'{repo} resolves to {real}, outside the suite\'s temp roots '
                           f'{suite_roots()} — a test sets a git identity only in its own repo')
    for key, value in (('user.name', name), ('user.email', email)):
        subprocess.run(['git', '-C', real, 'config', key, value], check=True,
                       capture_output=True, text=True)


def executable_asf(bin_dir):
    """A do-nothing ``asf`` executable under ``bin_dir``, for a test that hands
    ``asf.hooks.ensure_git_hooks`` / ``install`` a ``which`` result: a hook is never written
    naming an ``asf`` that does not exist or is not executable (review-b-0111's ``/x/asf``)."""
    os.makedirs(bin_dir, exist_ok=True)
    path = os.path.join(bin_dir, 'asf')
    with open(path, 'w') as f:
        f.write('#!/bin/sh\nexit 0\n')
    os.chmod(path, 0o755)
    return path
