"""asf.record.tree — a record as git holds it, laid out in a scratch directory.

The pre-commit check judges what is being committed, not the working tree: the cards as the
commit's index stages them, against the cards at ``HEAD``. A command that publishes derives the
index and the Children/Backlinks it commits the same way — from ``HEAD`` plus the paths it
publishes — so an operator's uncommitted edit to another card never rides along. Only what the
record's derivation reads is laid out (the item folders and ``index.json``)."""
import os

from asf.record.core import ITEM_FOLDERS
from asf.redact import _run_git


def record_paths(prefix=''):
    """The pathspecs of the files the derivation reads, for a record at ``prefix`` in its repo."""
    return [prefix + f for f in ITEM_FOLDERS] + [prefix + 'index.json']


def has_head(repo):
    return _run_git(repo, ['rev-parse', '--verify', '-q', 'HEAD']).returncode == 0


def head_index(repo, index_file):
    """Write ``HEAD``'s tree into the scratch index ``index_file`` (an empty one when HEAD is
    unborn). Returns the ``git_env`` that points git at it."""
    git_env = {'GIT_INDEX_FILE': index_file}
    if has_head(repo):
        _run_git(repo, ['read-tree', 'HEAD'], git_env=git_env).check_returncode()
    else:
        _run_git(repo, ['read-tree', '--empty'], git_env=git_env).check_returncode()
    return git_env


def lay_out(repo, dest, pathspecs, git_env=None):
    """Check the index's blobs for ``pathspecs`` out under ``dest`` — the index the environment
    names (``GIT_INDEX_FILE``, which a hook inherits from ``git commit``), or ``git_env``'s."""
    os.makedirs(dest, exist_ok=True)
    listed = _run_git(repo, ['ls-files', '-z', '--', *pathspecs], git_env=git_env)
    listed.check_returncode()
    if not listed.stdout:
        return
    _run_git(repo, ['checkout-index', '-f', '-z', '--stdin', '--prefix=' + dest.rstrip('/') + '/'],
             input_text=listed.stdout, git_env=git_env).check_returncode()


def lay_out_head(repo, dest, pathspecs, scratch):
    """``HEAD``'s ``pathspecs`` under ``dest``; ``scratch`` is a directory for the index it reads."""
    git_env = head_index(repo, os.path.join(scratch, 'head.index'))
    lay_out(repo, dest, pathspecs, git_env)
