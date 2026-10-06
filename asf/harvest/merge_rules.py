"""asf.harvest.merge_rules — the files whose conflicts code settles, never a session.

A rebuild on the trunk (:func:`asf.harvest.lane.drop_trunk_copies`) stops at the first pick
git cannot merge. Some of those stops are not judgement at all: two branches each added a row
to a register, a section to the changelog, a line to a jsonl log, or each regenerated the same
generated file. The product names those files and how each merges, in
``conventions.merge_rules`` — a list of::

    {paths: [glob, …] | path: glob, strategy: union | append | renumber | regenerate,
     id: <regex with one digit group>, run: <command>}

* ``union`` — every line of both sides kept, the trunk's first (``git merge-file --union``).
* ``append`` — the trunk's file, then the lines the branch added (only additions: a branch
  that changed or removed a line of the base is a residue, a session's to merge).
* ``renumber`` — a register: like ``append``, and a row the branch added whose id (``id``'s
  first group, zero-padded to its width) the trunk's side already holds is given the next free
  number; its rows go after the trunk's last id row. Applied on a clean merge too: git merges
  two added rows clean and the ids collide all the same.
* ``regenerate`` — the trunk's version stands in the pick; ``run`` (or the matching
  ``conventions.generated`` entry's) regenerates it on the rebuilt head (:func:`regenerate`).

Implicit rules, after the product's own: the ``changelog_file`` merges by ``union``, and every
``conventions.generated`` entry by ``regenerate``. A path no rule names conflicts as today.
Nothing here names a product: the files and ids are the product's config.
"""
import dataclasses
import fnmatch
import os
import re
import shutil
import subprocess
import tempfile

from asf.harvest import harvest as H

UNION, APPEND, RENUMBER, REGENERATE = 'union', 'append', 'renumber', 'regenerate'
STRATEGIES = (UNION, APPEND, RENUMBER, REGENERATE)
#: The conventions key the product's rules live under.
KEY = 'merge_rules'
DEFAULT_CHANGELOG = 'CHANGELOG.md'
REGEN_TIMEOUT_S = 900


@dataclasses.dataclass(frozen=True)
class Rule:
    globs: tuple
    strategy: str
    id_re: object = None
    run: str = ''

    def names(self, path):
        return any(fnmatch.fnmatchcase(path, g) or path == g for g in self.globs)


def _get(conv, key):
    if conv is None:
        return None
    if hasattr(conv, 'get'):
        return conv.get(key)
    return getattr(conv, key, None)


def _globs(entry):
    g = entry.get('paths') or entry.get('path') or ()
    return tuple([g] if isinstance(g, str) else [x for x in g if isinstance(x, str)])


def rules(conv):
    """The product's :class:`Rule` list, its own first, then the implicit ones. An entry with
    no paths, an unknown strategy, or a ``renumber`` whose ``id`` is not a regex with one group
    is no rule."""
    out = []
    for e in _get(conv, KEY) or ():
        if not isinstance(e, dict):
            continue
        globs, strategy = _globs(e), str(e.get('strategy') or '').strip().lower()
        if not globs or strategy not in STRATEGIES:
            continue
        id_re = None
        if strategy == RENUMBER:
            try:
                id_re = re.compile(str(e.get('id') or ''))
            except re.error:
                continue
            if id_re.groups < 1 or not e.get('id'):
                continue
        out.append(Rule(globs, strategy, id_re, str(e.get('run') or '').strip()))
    changelog = _get(conv, 'changelog_file') or DEFAULT_CHANGELOG
    out.append(Rule((changelog,), UNION))
    for e in _get(conv, 'generated') or ():
        if isinstance(e, dict) and _globs(e):
            out.append(Rule(_globs(e), REGENERATE, None, str(e.get('run') or '').strip()))
    return out


def rule_for(the_rules, path):
    """The first rule naming ``path``, or None."""
    for r in the_rules or ():
        if r.names(path):
            return r
    return None


# ---- the merges --------------------------------------------------------------------------------

def _lines(text):
    return text.splitlines(keepends=True) if text else []


def _added(base, side):
    """The lines ``side`` added to ``base`` (in order), or None when ``side`` dropped or changed
    a line of ``base`` — not an append."""
    left = {}
    for ln in base:
        left[ln] = left.get(ln, 0) + 1
    added = []
    for ln in side:
        if left.get(ln):
            left[ln] -= 1
        else:
            added.append(ln)
    if any(left.values()):
        return None
    return added


def _ensure_nl(lines):
    if lines and not lines[-1].endswith('\n'):
        lines[-1] = lines[-1] + '\n'
    return lines


def union(base, ours, theirs):
    """``git merge-file --union``: both sides' lines, the trunk's (``ours``) first."""
    tmp = tempfile.mkdtemp(prefix='asf-union-')
    try:
        paths = []
        for name, text in (('ours', ours), ('base', base), ('theirs', theirs)):
            p = os.path.join(tmp, name)
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write(text or '')
            paths.append(p)
        r = subprocess.run(['git', 'merge-file', '-p', '--union', *paths], capture_output=True,
                           text=True)
        return r.stdout if r.returncode >= 0 else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def append(base, ours, theirs):
    """The trunk's file, then the lines the branch added that the trunk does not hold — or
    None when either side is not an append to ``base``."""
    b, o, t = _lines(base), _lines(ours), _lines(theirs)
    added = _added(b, t)
    if added is None or _added(b, o) is None:
        return None
    have = set(o)
    return ''.join(_ensure_nl(list(o)) + [ln for ln in added if ln not in have])


def _id(id_re, line):
    m = id_re.search(line)
    return (m, int(m.group(1))) if m and m.group(1) and m.group(1).isdigit() else (None, None)


def renumber(base, ours, theirs, id_re):
    """``(text, {old: new})``: the register merged — the trunk's rows, then the branch's added
    rows after the trunk's last id row, each colliding id given the next free number. ``(None,
    {})`` when either side is not an append to ``base``."""
    b, o, t = _lines(base), _lines(ours), _lines(theirs)
    added = _added(b, t)
    if added is None or _added(b, o) is None:
        return None, {}
    have = set(o)
    added = [ln for ln in added if ln not in have]
    taken = set()
    last = -1
    for i, ln in enumerate(o):
        _m, n = _id(id_re, ln)
        if n is not None:
            taken.add(n)
            last = i
    for ln in t:
        _m, n = _id(id_re, ln)
        if n is not None:
            taken.add(n)
    ours_ids = {n for n in (_id(id_re, ln)[1] for ln in o) if n is not None}
    nxt = max(taken) + 1 if taken else 1
    renames, rows = {}, []
    for ln in added:
        m, n = _id(id_re, ln)
        if m is not None and n in ours_ids:
            s, e = m.span(1)
            new = str(nxt).zfill(e - s)
            renames[m.group(0)] = m.group(0)[:s - m.start()] + new + m.group(0)[e - m.start():]
            ln = ln[:s] + new + ln[e:]
            nxt += 1
        rows.append(ln)
    o = _ensure_nl(list(o))
    at = last + 1 if last >= 0 else len(o)
    return ''.join(o[:at] + _ensure_nl(rows) + o[at:]), renames


def merge(rule, base, ours, theirs):
    """``(text, info)``: ``rule``'s merge of one file's three versions (``ours``: the trunk's
    side), or ``(None, why)`` when the rule cannot settle it. ``info``: ``{'renames': …}`` for a
    register, ``{'regenerate': True}`` for a generated file."""
    if rule.strategy == UNION:
        got = union(base, ours, theirs)
        return (got, {}) if got is not None else (None, 'union merge failed')
    if rule.strategy == APPEND:
        got = append(base, ours, theirs)
        return (got, {}) if got is not None else (None, 'not an append on both sides')
    if rule.strategy == RENUMBER:
        got, renames = renumber(base, ours, theirs, rule.id_re)
        return (got, {'renames': renames}) if got is not None else \
            (None, 'not an append of rows on both sides')
    if rule.strategy == REGENERATE:
        return ours, {'regenerate': True}
    return None, f'unknown strategy {rule.strategy}'


# ---- on git objects ----------------------------------------------------------------------------

def _git(repo, *args, **kw):
    return H.sh(['git', *args], cwd=repo, **kw)


def _blob_text(repo, rev, path):
    """``(text, mode)`` of ``path`` at ``rev``; ``(None, None)`` when absent or not text."""
    ls = _git(repo, 'ls-tree', rev, '--', path).stdout.split()
    if len(ls) < 3 or ls[1] != 'blob':
        return None, None
    r = subprocess.run(['git', 'cat-file', 'blob', ls[2]], cwd=repo, capture_output=True)
    if r.returncode != 0:
        return None, None
    try:
        return r.stdout.decode('utf-8'), ls[0]
    except UnicodeDecodeError:
        return None, None


def resolve_pick(repo, the_rules, tree, merge_base, ours_rev, theirs_rev, files):
    """Settle one pick's conflicted ``files`` by rule, on objects only: ``(new_tree, done,
    why)``. ``tree`` is git's merge result (conflict markers in the conflicted files);
    ``done`` is ``[(path, strategy, info)]``. ``new_tree`` None when any file has no rule or
    its rule cannot settle it — ``why`` names it; nothing is written but objects."""
    done, writes = [], []
    for path in files:
        rule = rule_for(the_rules, path)
        if rule is None:
            return None, [], f'{path}: no merge rule'
        base, _ = _blob_text(repo, merge_base, path)
        ours, mode = _blob_text(repo, ours_rev, path)
        theirs, tmode = _blob_text(repo, theirs_rev, path)
        if ours is None or theirs is None:
            return None, [], f'{path}: deleted on one side or not text'
        text, info = merge(rule, base or '', ours, theirs)
        if text is None:
            return None, [], f'{path}: {rule.strategy} — {info}'
        writes.append((path, mode or tmode, text))
        done.append((path, rule.strategy, info))
    new = write_tree(repo, tree, writes)
    return (new, done, '') if new else (None, [], 'git could not write the merged tree')


def write_tree(repo, tree, writes):
    """``tree`` with each ``(path, mode, text)`` of ``writes`` replaced — through a throwaway
    index, never the repo's own. The new tree id, or ''."""
    fd, idx = tempfile.mkstemp(prefix='asf-merge-index-')
    os.close(fd)
    os.unlink(idx)
    env = H.clean_env(dict(os.environ, GIT_INDEX_FILE=idx))
    try:
        r = subprocess.run(['git', 'read-tree', tree], cwd=repo, env=env, capture_output=True)
        if r.returncode != 0:
            return ''
        for path, mode, text in writes:
            h = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=repo, env=env,
                               input=text.encode('utf-8'), capture_output=True)
            blob = h.stdout.decode().strip()
            if h.returncode != 0 or not blob:
                return ''
            u = subprocess.run(['git', 'update-index', '--add', '--cacheinfo',
                                f'{mode or "100644"},{blob},{path}'], cwd=repo, env=env,
                               capture_output=True)
            if u.returncode != 0:
                return ''
        w = subprocess.run(['git', 'write-tree'], cwd=repo, env=env, capture_output=True,
                           text=True)
        return w.stdout.strip() if w.returncode == 0 else ''
    finally:
        if os.path.exists(idx):
            os.unlink(idx)


def renumber_clean(repo, the_rules, tree, merge_base, ours_rev, theirs_rev):
    """A clean pick's register rows checked too: for each ``renumber`` file both sides changed,
    the rule's merge replaces git's when they differ (two added rows, one id). ``(tree, done)``
    — ``tree`` unchanged and ``[]`` when nothing collided or a rule could not settle it (the
    product's check then says)."""
    regs = [r for r in the_rules or () if r.strategy == RENUMBER]
    if not regs:
        return tree, []
    def changed(a, b):
        d = _git(repo, 'diff', '--name-only', '--no-renames', a, b)
        return set(d.stdout.split()) if d.returncode == 0 else set()
    both = changed(merge_base, ours_rev) & changed(merge_base, theirs_rev)
    writes, done = [], []
    for path in sorted(both):
        rule = rule_for(the_rules, path)
        if rule is None or rule.strategy != RENUMBER:
            continue
        base, _ = _blob_text(repo, merge_base, path)
        ours, mode = _blob_text(repo, ours_rev, path)
        theirs, _ = _blob_text(repo, theirs_rev, path)
        if ours is None or theirs is None:
            continue
        text, renames = renumber(base or '', ours, theirs, rule.id_re)
        if text is None or not renames:
            continue
        writes.append((path, mode, text))
        done.append((path, RENUMBER, {'renames': renames}))
    if not writes:
        return tree, []
    new = write_tree(repo, tree, writes)
    return (new, done) if new else (tree, [])


def regenerate(repo, state_dir, head, the_rules, paths, ident, setup=None,
               timeout=REGEN_TIMEOUT_S):
    """``(sha, why)``: one commit on ``head`` with the generated ``paths`` regenerated by their
    rules' commands (after ``setup``), built in a throwaway detached checkout under
    ``<state>/merge-rules/``; ``sha`` is ``head`` itself when nothing changed or no rule has a
    command (the trunk's version stands; the gate says), '' with ``why`` on a failure."""
    cmds = []
    for p in paths:
        r = rule_for(the_rules, p)
        if r is not None and r.run and r.run not in cmds:
            cmds.append(r.run)
    if not cmds:
        return head, ''
    holder = os.path.join(state_dir, 'merge-rules')
    os.makedirs(holder, exist_ok=True)
    wt = tempfile.mkdtemp(prefix='wt-', dir=holder)
    os.rmdir(wt)
    add = _git(repo, 'worktree', 'add', '-q', '--detach', wt, head)
    if add.returncode != 0:
        shutil.rmtree(wt, ignore_errors=True)
        return '', f'no checkout of {head[:9]}: {H.tail(add.stderr)}'
    try:
        for cmd in ([setup] if setup else []) + cmds:
            try:
                p = subprocess.run(cmd, shell=True, cwd=wt, env=H.clean_env(dict(os.environ)),
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=timeout)
                rc, out = p.returncode, (p.stdout or '') + (p.stderr or '')
            except subprocess.TimeoutExpired:
                rc, out = None, f'timed out after {timeout}s'
            if rc != 0:
                tail = ' | '.join([l for l in out.splitlines() if l.strip()][-5:])
                return '', f'`{cmd}` failed: {tail}'
        globs = [g for p in paths for g in (rule_for(the_rules, p).globs
                                            if rule_for(the_rules, p) else ())]
        st = _git(wt, 'status', '--porcelain', '-z', '--no-renames', '--untracked-files=all')
        made = [e[3:] for e in st.stdout.split('\0') if len(e) > 3]
        fresh = [p for p in made if any(fnmatch.fnmatchcase(p, g) or p == g for g in globs)]
        if not fresh:
            return head, ''
        if _git(wt, 'add', '-A', '--', *fresh).returncode != 0:
            return '', 'git add of the regenerated files failed'
        tree = _git(wt, 'write-tree').stdout.strip()
        made_c = subprocess.run(
            ['git', 'commit-tree', tree, '-p', head],
            input=f'chore: regenerate {", ".join(sorted(fresh))}\n', cwd=repo,
            capture_output=True, text=True, env=H.clean_env(dict(os.environ, **(ident or {}))))
        sha = made_c.stdout.strip()
        if made_c.returncode != 0 or not sha:
            return '', f'git commit-tree failed: {H.tail(made_c.stderr)}'
        return sha, ''
    finally:
        _git(repo, 'worktree', 'remove', '--force', wt)
        shutil.rmtree(wt, ignore_errors=True)
