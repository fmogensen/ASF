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
import ast
import json
import os
import re
import subprocess

from asf import env, reservations
from asf.briefs import preamble
from asf.evidence import review as review_mod
from asf.evidence import review_store
from asf.feeder import footprint
from asf.workers import lifecycle, pool, report, runtime

#: The most existing tests one brief names on its own account.
TEST_LIMIT = 6
#: The longest a field of the last report may run, in characters, its label included.
FIELD_CAP = 200
REPORT_FIELDS = ('status', 'pushed', 'tests', 'left out', 'ruling')
TEST_NAME_RE = re.compile(r'^test_|_test\.|\.test\.|\.spec\.')
#: The most top-level functions/classes the "Where to look" section names per file
#: (asf.briefs.preamble.outline_lines) — a file with more just reads longer under the same line.
OUTLINE_LIMIT = 15
#: A line-anchored, best-effort match for a function/class declaration in a language ``ast``
#: does not cover. One group each; the first that matches wins. Deliberately shallow — a name and
#: its declaration line is a pointer to grep from, not a parse.
_OUTLINE_RE = (
    re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+([A-Za-z_$][\w$]*)'),
    re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)'),
    re.compile(r'^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)'),           # Go
    re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?fn\s+([A-Za-z_]\w*)'),     # Rust
    re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_]\w*)'),  # Rust
    re.compile(r'^\s*(?:[\w.]+\s+)?function\s+([A-Za-z_]\w*)\s*\('),     # shell / bash
)


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

    A pushed branch reads its own tip — the sha ``ls-remote`` reported, not the local
    ``refs/remotes/origin/<branch>`` the wave's process may not have fetched this tick. An
    unpushed one reads the trunk's and *says so*: without the parenthetical the sha would read as
    the branch's own head, which it is not."""
    if not repo or not os.path.isdir(repo):
        return '', False, ''
    from asf import gitops  # the exact ref: ``archive/<branch>`` is not ``<branch>``
    ls = _git_text(repo, ['ls-remote', '--heads', 'origin', gitops.head_ref(branch)]) \
        if branch else ''
    sha = gitops.head_sha(ls, branch) if ls else ''
    exists = bool(sha)
    rev = sha if sha and _git(repo, ['cat-file', '-e', f'{sha}^{{commit}}']) is not None else ''
    if sha and not rev:
        # origin has moved and this clone has not fetched it: never pass the older ref off as
        # the head — the commit list would then read as the whole of what was done
        return f'{sha[:9]} (origin/{branch} — not in this clone yet)', True, ''
    own = bool(sha) and bool(rev)
    if not rev:
        trunk = f'origin/{main}'
        rev = trunk if _git(repo, ['rev-parse', '--verify', '-q', f'{trunk}^{{commit}}']) \
            is not None else ''
    if not rev:
        return '', exists, ''
    line = _git_text(repo, ['log', '-1', '--format=%h %s', rev])
    if not line:
        return '', exists, rev
    where = f'(origin/{branch})' if own \
        else f'(origin/{main} — {branch} is not on origin yet)' if branch else f'(origin/{main})'
    return f'{line} {where}', exists, rev


#: The most commits the brief lists between the trunk and the branch head; over it the block
#: prints the count and names the `git log` that has the rest.
COMMIT_LIMIT = 10


def commits_on(repo, rev, main, limit=COMMIT_LIMIT):
    """``{'total': n, 'lines': ['<short sha> <subject>', ...]}`` for ``origin/<main>..<rev>``,
    newest first, ``lines`` cut to ``limit``. ``{'total': 0, 'lines': []}`` for a rev the clone
    does not hold, a branch level with the trunk, and every failure — one ``git log``, no fetch."""
    if not repo or not rev:
        return {'total': 0, 'lines': []}
    lines = _git_text(repo, ['log', '--format=%h %s', f'origin/{main}..{rev}']).splitlines()
    return {'total': len(lines), 'lines': lines[:limit]}


def _lines_of(blob):
    return blob.count(b'\n') + (1 if blob and not blob.endswith(b'\n') else 0)


def _python_outline(text):
    """Top-level ``(name, 'class'|'def', start, end)`` of one Python file, via ``ast`` — a file
    that does not parse (a syntax error, a partial checkout) yields ``[]``, never a raised error
    that would refuse the whole brief over one unreadable file."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, 'def', node.lineno, getattr(node, 'end_lineno', node.lineno)))
        elif isinstance(node, ast.ClassDef):
            out.append((node.name, 'class', node.lineno, getattr(node, 'end_lineno', node.lineno)))
    return out[:OUTLINE_LIMIT]


def _regex_outline(text):
    """The same shape as :func:`_python_outline`, for anything ``ast`` does not read: one pass
    over the text, :data:`_OUTLINE_RE` tried top to bottom, first match wins. The range is the
    declaration line alone — a regex has no reach to a body's end, only a parser does."""
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        for pat in _OUTLINE_RE:
            m = pat.match(line)
            if m:
                out.append((m.group(1), 'def', i, i))
                break
    return out[:OUTLINE_LIMIT]


def file_outline(text, path):
    """Top-level functions/classes of ``text`` (the file at ``path``, its trunk content): Python
    read with ``ast``, anything else with the best-effort regex, ``[]`` when neither finds
    anything — the caller then prints the file's line count alone."""
    return _python_outline(text) if path.endswith('.py') else _regex_outline(text)


def outlines_of(repo, rev, tree, writes):
    """``{path: {'lines': N, 'defs': [(name, kind, start, end), ...]}}`` for every literal path
    in ``writes`` that ``rev``'s tree (``tree``, the checkout's file list) carries and whose blob
    decodes as UTF-8 — a glob, a path missing from the tree, or a binary file is left out, never
    guessed at. ``defs`` is capped at :data:`OUTLINE_LIMIT`; a file no reader finds anything in
    still gets an entry, with ``defs: []``, so its line count is still printed."""
    tree_set = set(tree or ())
    out = {}
    for path in dict.fromkeys(writes or ()):  # de-dup, first-seen order (unused: dict is by path)
        if not path or any(c in path for c in '*?[') or path not in tree_set:
            continue
        blob = _git(repo, ['cat-file', '-p', f'{rev}:{path}'])
        if blob is None:
            continue
        try:
            text = blob.decode('utf-8')
        except UnicodeDecodeError:
            continue
        out[path] = {'lines': _lines_of(blob), 'defs': file_outline(text, path)}
    return out


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


def _squeeze(text, cap=FIELD_CAP):
    """``text``, whitespace-collapsed, cut to ``cap`` with the ``…`` ellipsis when it runs over."""
    line = ' '.join(str(text).split())
    return line if len(line) <= cap else line[:cap - 1].rstrip() + '…'


def _cap(label, value):
    return _squeeze(f'{label}: {value}')


#: How much of a run's log the progress line is read from. A spent session's log is the largest
#: file this factory writes; the last assistant turn is at the end of it either way.
PROGRESS_TAIL_BYTES = 256 * 1024


def last_progress(log, tail=PROGRESS_TAIL_BYTES):
    """The last line an assistant turn printed in ``log``, squeezed and capped — what the run was
    doing when it stopped. Reads the last ``tail`` bytes only, drops the first partial line, and
    scans the parsed records backwards for a ``text`` block. ``''`` for no log, no readable
    record, and every failure."""
    if not log:
        return ''
    try:
        with open(log, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            offset = max(0, size - tail)
            f.seek(offset)
            data = f.read()
    except OSError:
        return ''
    if offset:
        data = data.split(b'\n', 1)[1] if b'\n' in data else b''
    for line in reversed(data.splitlines()):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get('type') != 'assistant':
            continue
        message = rec.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        texts = [b.get('text') for b in content if isinstance(b, dict)
                 and b.get('type') == 'text' and b.get('text')]
        if not texts:
            continue
        for text_line in texts[-1].splitlines():
            if text_line.strip():
                return _squeeze(text_line.strip())
        return ''
    return ''


def branch_review(repo, rev, tree_paths, product, slug, round_):
    """The branch's newest review file the tree carries, at or below ``round_``: ``(n, path,
    text)`` for the highest round whose :func:`asf.briefs.preamble.review_path_for` names a path
    ``tree_paths`` holds, read with one ``git cat-file -p``; ``None`` for no match, an empty read
    and every failure. Makes no listing of its own — the caller already holds one."""
    tree_set = set(tree_paths or ())
    for n in range(max(round_ or 0, 1), 0, -1):
        path = preamble.review_path_for(product, slug, n)
        if path not in tree_set:
            continue
        text = _git_text(repo, ['cat-file', '-p', f'{rev}:{path}'])
        if text:
            return (n, path, text)
    return None


def stored_review(product, item_id, branch, review=None):
    """The review of ``item_id`` filed off ``branch`` (:mod:`asf.evidence.review_store`) as the
    ``(n, path, text)`` triple :func:`branch_review` answers — when it is newer than ``review``,
    the branch's own (the branch wins only with a strictly higher round) — else ``review``."""
    if not item_id or not branch:
        return review
    slug = str(item_id).lower()
    stored = review_store.newest(review_store.root(product), slug, branch)
    if not review_store.prefer(stored, review[0] if review else None):
        return review
    return (stored['round'], preamble.review_path_for(product, slug, stored['round']),
            stored['text'])


def last_report(product, item_id, branch='', review=None):
    """The newest ended session's typed report on the item, else ``''``.

    ``<job> ended <ts> — <end_reason>`` and the five fields worth carrying, each capped — never
    the transcript above the REPORT block. A log that is gone leaves the one ledger line. A
    ``branch`` given keeps only that branch's runs; falsy, every run of the item, as before. When
    the newest run left no REPORT and ``review`` (an ``(n, path, text)`` triple) is given, one
    line — ``review round <n> (<path>): verdict <v>`` — stands in, omitted when the review carries
    no verdict."""
    if not item_id:
        return ''
    path = os.path.join(env.state_dir(product), 'sessions.jsonl')
    ended = [r for r in lifecycle.item_runs(path, item_id)
             if r.get('ended') and (not branch or r.get('branch') == branch)]
    if not ended:
        return ''
    run = max(ended, key=lambda r: str(r.get('ended')))
    lines = [f"{run.get('job', '?')} ended {run.get('ended')} — {run.get('end_reason') or '?'}"]
    result = runtime.read_result(run.get('log'))
    fields = report.parse((result or {}).get('result'))
    field_lines = [_cap(key, fields[key]) for key in REPORT_FIELDS if fields.get(key)]
    if not field_lines and review:
        n, review_path, text = review
        verdict = review_mod.verdict_of(text)
        if verdict is not None:
            field_lines = [f'review round {n} ({review_path}): verdict {verdict}']
    return '\n'.join(lines + field_lines)


def predecessor(product, item_id, branch, kind):
    """The run this launch is a relaunch of: the newest **ended**, unlanded run of ``item_id`` on
    ``branch`` whose ``kind`` is this row's, or ``{}``. ``attempt`` is that run's 1-based index
    among its job's own runs, in ledger order."""
    if not item_id or not branch or not kind:
        return {}
    try:
        runs = lifecycle.item_runs(pool.sessions_path(product), item_id)
    except (OSError, ValueError):
        return {}
    matches = [r for r in runs if r.get('ended') and r.get('branch') == branch
               and r.get('kind') == kind and not lifecycle.landed(r)]
    if not matches:
        return {}
    run = max(matches, key=lambda r: str(r.get('ended') or ''))
    job_runs = [r for r in runs if r.get('job') == run.get('job')]
    return {'job': run.get('job'), 'ended': run.get('ended'), 'end_reason': run.get('end_reason'),
            'attempt': job_runs.index(run) + 1}


def repo_facts(product, row, index, inflight=None):
    """The ten keys of :data:`asf.briefs.preamble.REPO_FACT_KEYS`, always all ten."""
    facts = {'head': '', 'branch_exists': False, 'files': {}, 'tests': [], 'last_report': '',
             'outlines': {}, 'commits': {'total': 0, 'lines': []}, 'progress': '', 'relaunch': {},
             'reservations': reservations.load(env.state_dir(product))}
    plain = preamble.collect(product, row, index, inflight)
    item_id = getattr(row, 'item_id', '') or ''
    main = getattr(product, 'main', 'main')
    repo = getattr(product, 'repo_dir', None)
    rev, tree = '', []
    if repo and os.path.isdir(repo):
        head, exists, rev = head_of(repo, plain['branch'], main)
        facts['head'], facts['branch_exists'] = head, exists
        if rev:
            tree = _git_text(repo, ['ls-tree', '-r', '--name-only', rev]).splitlines()
            facts['tests'] = tests_under(tree, plain['writes'])
            wanted = preamble.wanted_paths(dict(plain, tests=plain['tests'] + facts['tests']),
                                           product=product)
            facts['files'] = line_counts(repo, rev, wanted)
            facts['outlines'] = outlines_of(repo, rev, tree, plain['writes'])
            facts['commits'] = commits_on(repo, rev, main)
    relaunch = predecessor(product, item_id, plain['branch'], getattr(row, 'kind', ''))
    facts['relaunch'] = relaunch
    review = None
    if relaunch:
        try:
            runs = lifecycle.item_runs(pool.sessions_path(product), item_id)
        except (OSError, ValueError):
            runs = []
        run = next((r for r in runs if r.get('job') == relaunch.get('job')
                   and r.get('ended') == relaunch.get('ended')), None)
        if run:
            facts['progress'] = last_progress(run.get('log'))
        if repo and rev:
            review = branch_review(repo, rev, tree, product, (item_id or 'item').lower(),
                                   plain['round'])
        review = stored_review(product, item_id, plain['branch'], review)
    try:
        facts['last_report'] = last_report(product, item_id, branch=plain['branch'], review=review)
    except (OSError, ValueError):
        pass
    return facts
