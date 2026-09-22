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
            self.root = root
        return self.root

    def fresh(self, dest=None):
        """A copy of the template at ``dest`` (an existing, empty directory — or a new temp
        directory when None), its git configs pointing at the copy. Returns ``dest``."""
        root = self._ensure()
        dest = dest or tempfile.mkdtemp(prefix=self.prefix)
        for name in os.listdir(root):
            src = os.path.join(root, name)
            dst = os.path.join(dest, name)
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
        _rewrite_paths(dest, {root: dest, os.path.realpath(root): os.path.realpath(dest)})
        return dest


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
