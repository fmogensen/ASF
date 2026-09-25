"""The build's one addition to ``pyproject.toml``: stamp ``git describe`` into the package.

``tools/install.sh`` pins a sha (``pipx install git+<repo>@<sha>``), so a git install carries no
tag, and ``asf --version`` fell to the static ``__version__`` (never bumped: the tag is the
version). The build runs in the clone pip made, so ``git describe`` there is the release:
``stamp`` writes it into the *built* package as ``asf/_build.py`` — never into the source tree,
so a checkout stays clean — and :func:`asf.cli._release` reads it.

Stdlib only at import, so a test can load :func:`stamp` without setuptools.
"""
import os
import subprocess


def describe(src):
    """``git describe --tags --match 'v[0-9]*'`` in ``src``, or ``''``."""
    try:
        out = subprocess.run(['git', '-C', src, 'describe', '--tags', '--match', 'v[0-9]*'],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return out.stdout.strip() if out.returncode == 0 else ''


def stamp(src, build_lib):
    """Write ``build_lib/asf/_build.py`` with ``src``'s describe; returns its path."""
    path = os.path.join(build_lib, 'asf', '_build.py')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('"""Written by the build (setup.py): the release it was built from."""\n'
                f'DESCRIBE = {describe(src)!r}\n')
    return path


if __name__ == '__main__':
    from setuptools import setup
    from setuptools.command.build_py import build_py

    _SRC = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()

    class BuildPy(build_py):
        def run(self):
            super().run()
            stamp(_SRC, self.build_lib)

    setup(cmdclass={'build_py': BuildPy})
