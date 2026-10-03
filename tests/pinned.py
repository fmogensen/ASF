"""tests.pinned — run code under an older asf's own package: the *pinned-reader* tests.

A product's venv can be pinned to an older sha while this checkout moves on. Anything the newer
code writes that the pinned one reads — a product file, a card — must still load there. The
oldest such sha still live is named in ``tools/pinned-readers.txt``; :func:`pinned_shas` reads
it, :func:`pinned_tree` extracts that sha's ``asf/`` package (``git archive``) into a temp dir
once per process, and :func:`run_pinned` runs a snippet against it in a subprocess whose
``ASF_HOME`` is a fresh temp dir, so nothing of the operator's is read.

When the object is not in the clone (CI checks out shallow) it is fetched once from ``origin``;
when that fails too (offline) the test is *skipped with a loud line on stderr*, never passed
silently.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READERS_FILE = os.path.join(REPO_ROOT, 'tools', 'pinned-readers.txt')

_TREES = {}  # sha -> extracted dir (this process)


def pinned_shas():
    """The shas in ``tools/pinned-readers.txt`` (``#`` comments and blank lines skipped)."""
    with open(READERS_FILE, encoding='utf-8') as f:
        return [line.split('#', 1)[0].strip() for line in f
                if line.split('#', 1)[0].strip()]


def _git(*args, timeout=60):
    return subprocess.run(['git', *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          timeout=timeout)


def _has_commit(sha):
    try:
        return _git('cat-file', '-e', f'{sha}^{{commit}}').returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _skip(sha, why):
    msg = f'PINNED-READER TEST SKIPPED: {sha} unavailable ({why}) — the pinned loader was NOT run'
    print(msg, file=sys.stderr)
    raise unittest.SkipTest(msg)


def pinned_tree(sha):
    """A directory holding ``sha``'s ``asf/`` package, extracted once per process and removed at
    exit. Raises :class:`unittest.SkipTest` (loudly) when the object cannot be had."""
    if sha in _TREES:
        return _TREES[sha]
    if not _has_commit(sha):
        try:
            _git('fetch', '--quiet', '--depth=1', 'origin', sha, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            _skip(sha, f'fetch failed: {e}')
        if not _has_commit(sha):
            _skip(sha, 'not in this clone and not fetchable from origin')
    out = tempfile.mkdtemp(prefix='asf-pinned-')
    import atexit
    atexit.register(shutil.rmtree, out, True)
    archive = subprocess.run(['git', 'archive', '--format=tar', sha, 'asf/'], cwd=REPO_ROOT,
                             capture_output=True, timeout=120)
    if archive.returncode != 0:
        _skip(sha, archive.stderr.decode(errors='replace').strip())
    subprocess.run(['tar', '-x', '-C', out], input=archive.stdout, check=True)
    _TREES[sha] = out
    return out


def run_pinned(sha, code, stdin=''):
    """Run ``code`` (python source) with ``sha``'s package first on ``sys.path`` and a fresh
    ``ASF_HOME``; returns the completed process (text)."""
    tree = pinned_tree(sha)
    with tempfile.TemporaryDirectory(prefix='asf-pinned-home-') as home:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ASF_', 'PYTHON'))}
        env.update({'PYTHONPATH': tree, 'ASF_HOME': home, 'HOME': home,
                    'PYTHONDONTWRITEBYTECODE': '1'})
        return subprocess.run([sys.executable, '-c', code], input=stdin, capture_output=True,
                              text=True, env=env, cwd=home, timeout=120)
