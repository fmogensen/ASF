"""asf.briefs.facts — the one module of ``asf.briefs`` that runs git and reads the session ledger.

The preamble (:mod:`asf.briefs.preamble`) is text over a ``repo_facts`` dict and runs nothing, so
that it is deterministic and testable without a repository. Somebody has to fill the dict, and
this is that somebody: :func:`repo_facts` returns exactly the keys the preamble reads
(:data:`asf.briefs.preamble.REPO_FACT_KEYS`), for the tick's launch path and for ``asf brief``
alike, so what an operator previews is what a session is handed.

Two rules:

* **Nothing is fetched.** One ``git ls-remote --heads origin <branch>`` per call — the same one
  the launch path always made — and everything else comes from refs the clone already has. A
  launch never waits on a ``git fetch``.
* **Every step may fail into nothing.** A missing repository, an unpushed branch, a rotated log
  are all normal. A fact that cannot be read is left empty and the preamble prints its
  ``(not known here)`` marker; it is never guessed.
"""
import os
import re
import subprocess

from asf import env
from asf.briefs import preamble
from asf.feeder import footprint
from asf.workers import lifecycle, report, runtime

#: The most existing tests one brief names on its own account.
TEST_LIMIT = 6
#: The longest a field of the last report may run, in characters, its label included.
FIELD_CAP = 200
REPORT_FIELDS = ('status', 'pushed', 'tests', 'left_out', 'ruling')
TEST_NAME_RE = re.compile(r'^test_|_test\.|\.test\.|\.spec\.')


def _git(repo, args, stdin=None):
    """A git command's stdout as bytes, or None when it fails or cannot run."""
    try:
        p = subprocess.run(['git', '-C', repo, *args], input=stdin, capture_output=True)
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


def _git_text(repo, args):
    out = _git(repo, args)
    return out.decode('utf-8', errors='replace').strip() if out is not None else ''


def head_of(repo, branch, main):
    """``(head, branch_exists, rev)`` — the line naming the commit the work starts from, whether
    the branch is on origin, and the rev the other facts are read at.

    A pushed branch reads its own tip. An unpushed one reads the trunk's and *says so*: without
    the parenthetical the sha would read as the branch's own head, which it is not."""
    if not repo or not os.path.isdir(repo):
        return '', False, ''
    exists = bool(branch) and bool(_git_text(repo, ['ls-remote', '--heads', 'origin', branch]))
    rev = ''
    if exists:
        rev = f'origin/{branch}' if _git(repo, ['rev-parse', '--verify', '-q',
                                                 f'origin/{branch}^{{commit}}']) is not None else ''
    if not rev:
        trunk = f'origin/{main}'
        rev = trunk if _git(repo, ['rev-parse', '--verify', '-q', f'{trunk}^{{commit}}']) \
            is not None else ''
    if not rev:
        return '', exists, ''
    line = _git_text(repo, ['log', '-1', '--format=%h %s', rev])
    if not line:
        return '', exists, rev
    where = f'({rev})' if rev == f'origin/{branch}' \
        else f'({rev} — {branch} is not on origin yet)' if branch else f'({rev})'
    return f'{line} {where}', exists, rev


def _lines_of(blob):
    return blob.count(b'\n') + (1 if blob and not blob.endswith(b'\n') else 0)


def line_counts(repo, rev, paths):
    """``{path: lines}`` for every path the rev carries — one ``git cat-file --batch``. A path
    the rev does not carry is absent from the map, never ``0``, which would read as empty."""
    paths = [p for p in paths if p]
    if not repo or not rev or not paths:
        return {}
    out = _git(repo, ['cat-file', '--batch'],
               stdin=''.join(f'{rev}:{p}\n' for p in paths).encode('utf-8'))
    if out is None:
        return {}
    counts, pos = {}, 0
    for path in paths:
        end = out.find(b'\n', pos)
        if end < 0:
            break
        header = out[pos:end].split()
        pos = end + 1
        if len(header) == 3 and header[1] == b'blob' and header[2].isdigit():
            size = int(header[2])
            counts[path] = _lines_of(out[pos:pos + size])
            pos += size + 1
        elif len(header) == 3 and header[2].isdigit():   # a tree, a commit: not a file
            pos += int(header[2]) + 1
    return counts


def tests_under(tree_paths, writes, limit=TEST_LIMIT):
    """The test files of a tree that sit inside the item's ``writes:`` footprint — sorted, cut
    to ``limit``. Pure: a concrete path is a glob with no wildcard, so it is compared as one."""
    found = sorted({p for p in tree_paths or []
                    if TEST_NAME_RE.search(os.path.basename(p))
                    and any(footprint.globs_overlap(p, w) for w in writes or [])})
    return found[:limit]


def _cap(label, value):
    line = f'{label}: {" ".join(str(value).split())}'
    return line if len(line) <= FIELD_CAP else line[:FIELD_CAP - 1].rstrip() + '…'


def _item(value):
    """One list item as a line: a string as is, an object as its own values."""
    if isinstance(value, dict):
        return ' '.join(str(v) for v in value.values())
    return str(value)


def last_report(product, item_id):
    """The newest ended session's typed report on the item, else ``''``.

    ``<job> ended <ts> — <end_reason>`` and the five fields worth carrying, each capped — never
    the transcript above the report. A log that is gone leaves the one ledger line."""
    if not item_id:
        return ''
    path = os.path.join(env.state_dir(product), 'sessions.jsonl')
    ended = [r for r in lifecycle.item_runs(path, item_id) if r.get('ended')]
    if not ended:
        return ''
    run = max(ended, key=lambda r: str(r.get('ended')))
    lines = [f"{run.get('job', '?')} ended {run.get('ended')} — {run.get('end_reason') or '?'}"]
    result = runtime.read_result(run.get('log'))
    try:
        fields = report.typed((result or {}).get('result'), None)
    except report.ReportError:
        return '\n'.join(lines)
    for key in REPORT_FIELDS:
        value = fields.get(key)
        if isinstance(value, list):
            value = ' '.join(_item(v) for v in value)
        if value:
            lines.append(_cap(key, value))
    return '\n'.join(lines)


def repo_facts(product, row, index, inflight=None):
    """The five keys of :data:`asf.briefs.preamble.REPO_FACT_KEYS`, always all five."""
    facts = {'head': '', 'branch_exists': False, 'files': {}, 'tests': [], 'last_report': ''}
    plain = preamble.collect(product, row, index, inflight)
    repo = getattr(product, 'repo_dir', None)
    if repo and os.path.isdir(repo):
        head, exists, rev = head_of(repo, plain['branch'], getattr(product, 'main', 'main'))
        facts['head'], facts['branch_exists'] = head, exists
        if rev:
            tree = _git_text(repo, ['ls-tree', '-r', '--name-only', rev]).splitlines()
            facts['tests'] = tests_under(tree, plain['writes'])
            wanted = preamble.wanted_paths(dict(plain, tests=plain['tests'] + facts['tests']),
                                           product=product)
            facts['files'] = line_counts(repo, rev, wanted)
    try:
        facts['last_report'] = last_report(product, getattr(row, 'item_id', ''))
    except (OSError, ValueError):
        pass
    return facts
