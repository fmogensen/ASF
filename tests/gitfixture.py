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

#: The files git keeps absolute paths in — a clone's remote URL, a worktree's ``gitdir``.
_REWRITE = ('config', 'gitdir', 'commondir', 'FETCH_HEAD')


class Template:
    def __init__(self, build, prefix='fixture_'):
        self.build = build
        self.prefix = prefix
        self.root = None

    def _ensure(self):
        if self.root is None:
            root = tempfile.mkdtemp(prefix=self.prefix + 'template-')
            atexit.register(shutil.rmtree, root, True)
            self.build(root)
            _disable_hooks(root)
            self.root = root
        return self.root

    def fresh(self, dest=None):
        """A copy of the template at ``dest`` (an existing, empty directory — or a new temp
        directory when None), its git configs pointing at the copy. Returns ``dest``."""
        root = self._ensure()
        if dest is None:  # ours: removed at exit, like the template (a caller's dir is its own)
            dest = tempfile.mkdtemp(prefix=self.prefix)
            atexit.register(shutil.rmtree, dest, True)
        for name in os.listdir(root):
            src = os.path.join(root, name)
            dst = os.path.join(dest, name)
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
        _rewrite_paths(dest, {root: dest, os.path.realpath(root): os.path.realpath(dest)})
        return dest


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
    git('init', '-q', '-b', trunk, cwd=tree)
    git('config', 'user.email', email, cwd=tree)
    git('config', 'user.name', name, cwd=tree)
    git('add', '-A', cwd=tree)
    git('commit', '-q', '-m', message, cwd=tree)
    git('remote', 'add', 'origin', origin, cwd=tree)
    git('push', '-q', '-u', 'origin', trunk, cwd=tree)
    git('remote', 'set-head', 'origin', trunk, cwd=tree)
    return tree


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
