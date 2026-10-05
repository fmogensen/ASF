"""The build's one addition to ``pyproject.toml``: the version, from the git tag.

The package version is never a hand-edited constant: the build runs in the clone pip made, so
``git describe --tags`` there is the release. :func:`build_version` turns it into the package
version (``0.1.108`` on a tag, ``0.1.107.post15+g96fa0feca`` past one), and ``stamp`` writes the
describe and that version into the *built* package as ``asf/_build.py`` — never into the source
tree, so a checkout stays clean — which :mod:`asf` and :func:`asf.cli._release` read.

Stdlib only at import, so a test can load :func:`stamp` without setuptools.
"""
import os
import re
import subprocess


def describe(src):
    """``git describe --tags --match 'v[0-9]*'`` in ``src``, or ``''``."""
    try:
        out = subprocess.run(['git', '-C', src, 'describe', '--tags', '--match', 'v[0-9]*'],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return out.stdout.strip() if out.returncode == 0 else ''


def build_version(described):
    """The package version off a describe line (``asf.version.pep440``, inlined: the build may
    not import the package it builds)."""
    m = re.match(r'^v(\d+\.\d+\.\d+)(?:-(\d+)-g([0-9a-f]+))?$', (described or '').strip())
    if not m:
        return '0.0.0'
    if m.group(2) in (None, '0'):
        return m.group(1)
    return f'{m.group(1)}.post{m.group(2)}+g{m.group(3)}'


def stamp(src, build_lib):
    """Write ``build_lib/asf/_build.py`` with ``src``'s describe and version; returns its path."""
    path = os.path.join(build_lib, 'asf', '_build.py')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    described = describe(src)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('"""Written by the build (setup.py): the release it was built from."""\n'
                f'DESCRIBE = {described!r}\n'
                f'VERSION = {build_version(described)!r}\n')
    return path


if __name__ == '__main__':
    from setuptools import setup
    from setuptools.command.build_py import build_py

    _SRC = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()

    class BuildPy(build_py):
        def run(self):
            super().run()
            stamp(_SRC, self.build_lib)

    setup(version=build_version(describe(_SRC)), cmdclass={'build_py': BuildPy})
