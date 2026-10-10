"""tools/check_clients.py — the "no new raw gh/git call site" ratchet (run by check_clients.sh).

Every ``gh`` call belongs in :mod:`asf.github` and every ``git`` call in ``asf.gitops`` /
:mod:`asf.gitpush`: one place that knows the rate limit, the dry-run guard, the timeout and what
a failure means (Unknown, never an empty answer). The raw sites that exist today are migrated a
file group at a time; until then this check holds the line — per file, it counts

* ``gh``  — lines holding a ``['gh'`` / ``["gh"`` argv (outside asf/github.py, asf/gitops.py,
  asf/gitpush.py),
* ``git`` — lines holding a ``['git'`` / ``["git"`` argv (same exemptions),

and fails when any file's count exceeds its line in tools/clients-baseline.txt (a file not
listed has a baseline of 0). A line carrying ``# client-exempt: <reason>`` is not counted for
``gh``/``git``. A count below its baseline passes and says so: lower the baseline in the PR that
migrated the site, so the ratchet keeps the gain.

The same count, with a ceiling of 0, holds each connector's executable to its connector module
(:mod:`asf.connectors`): ``claude`` (an argv, a ``which``, or a default-binary assignment of the
coding-agent CLI) only in asf/connectors/claude_code.py, ``launchctl`` only in
asf/connectors/launchd.py, ``systemctl`` only in asf/connectors/systemd.py, ``openssl`` only in
asf/ghapp.py (the one module that signs a JWT — F-0333). ``pipx`` is the installer kind and stays
allowed everywhere for now (not counted).

One more rule, live once :mod:`asf.gitpush` declares ``__gitpush_door__ = True`` (the push
door takes a ref guard): every ``gitpush.push(`` call in asf/ passes ``guard=``. The rule keys on
that module attribute, not on a list of paths that moves.

  python3 tools/check_clients.py                   check (exit 1 on a rise)
  python3 tools/check_clients.py --against HEAD^1  CI: only what this change adds can fail
  python3 tools/check_clients.py --write-baseline  rewrite the baseline from the tree
"""
import argparse
import ast
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join('tools', 'clients-baseline.txt')
PACKAGE = 'asf'
#: The client modules — the only files where a raw ``gh``/``git`` argv belongs.
CLIENTS = frozenset({'asf/github.py', 'asf/gitops.py', 'asf/gitpush.py'})
EXEMPT = '# client-exempt:'
PATTERNS = {
    'gh': re.compile(r"""\[\s*['"]gh['"]\s*[,\]]"""),
    'git': re.compile(r"""\[\s*['"]git['"]\s*[,\]]"""),
    'claude': re.compile(r"""\[\s*['"]claude['"]\s*[,\]]|which\(\s*['"]claude['"]|"""
                         r"""=\s*['"]claude['"]\s*(#.*)?$"""),
    'launchctl': re.compile(r"""['"]launchctl['"]"""),
    'systemctl': re.compile(r"""['"]systemctl['"]"""),
    'openssl': re.compile(r"""['"]openssl['"]"""),
}
KINDS = tuple(PATTERNS)
#: Where each kind's executable may be invoked: its client or connector module.
OWNERS = {
    'gh': CLIENTS,
    'git': CLIENTS,
    'claude': frozenset({'asf/connectors/claude_code.py'}),
    'launchctl': frozenset({'asf/connectors/launchd.py'}),
    'systemctl': frozenset({'asf/connectors/systemd.py'}),
    'openssl': frozenset({'asf/ghapp.py'}),
}


def py_files(root):
    for base, dirs, files in os.walk(os.path.join(root, PACKAGE)):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for name in sorted(files):
            if name.endswith('.py'):
                yield os.path.relpath(os.path.join(base, name), root).replace(os.sep, '/')


def count_text(rel, text):
    """``{kind: n}`` for one file's ``text`` (only kinds with n > 0)."""
    counts = {}
    for line in text.splitlines():
        for kind, rx in PATTERNS.items():
            if rel in OWNERS[kind] or EXEMPT in line:
                continue
            if rx.search(line):
                counts[kind] = counts.get(kind, 0) + 1
    return counts


def count_tree(root):
    """``{(kind, path): n}`` over every ``asf/**/*.py``."""
    out = {}
    for rel in py_files(root):
        with open(os.path.join(root, rel), encoding='utf-8') as f:
            for kind, n in count_text(rel, f.read()).items():
                out[(kind, rel)] = n
    return out


def count_rev(root, rev):
    """``{(kind, path): n}`` over ``asf/**/*.py`` as committed at ``rev``; ``None`` when the
    revision cannot be read (a shallow checkout without it)."""
    def git(*args):
        argv = ['git', '-C', root, *args]  # client-exempt: a lint, not the factory
        return subprocess.run(argv, capture_output=True, text=True)
    ls = git('ls-tree', '-r', '--name-only', rev, '--', PACKAGE)
    if ls.returncode != 0:
        return None
    out = {}
    for rel in ls.stdout.split():
        if not rel.endswith('.py'):
            continue
        shown = git('show', f'{rev}:{rel}')
        if shown.returncode != 0:
            return None
        for kind, n in count_text(rel, shown.stdout).items():
            out[(kind, rel)] = n
    return out


def read_baseline(path):
    base = {}
    if not os.path.exists(path):
        return base
    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            kind, rel, n = line.split()
            base[(kind, rel)] = int(n)
    return base


def write_baseline(path, counts):
    with open(path, 'w', encoding='utf-8') as f:
        f.write('# tools/clients-baseline.txt — raw gh/git call sites per file,\n'
                '# the ceiling tools/check_clients.sh enforces. Lower a line when a PR migrates a\n'
                '# site; never raise one (`python3 tools/check_clients.py --write-baseline`).\n')
        for kind in KINDS:
            for (k, rel), n in sorted(counts.items()):
                if k == kind and n:
                    f.write(f'{kind} {rel} {n}\n')


def door_open(root):
    """True when asf/gitpush.py declares ``__gitpush_door__ = True``."""
    path = os.path.join(root, PACKAGE, 'gitpush.py')
    try:
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == '__gitpush_door__' for t in node.targets):
            return isinstance(node.value, ast.Constant) and node.value.value is True
    return False


def unguarded_pushes(rel, text):
    """``[line]`` of each ``gitpush.push(…)`` call in ``text`` that passes no ``guard=``."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    names = {'gitpush'}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == 'asf':
            names.update(a.asname or a.name for a in node.names if a.name == 'gitpush')
        if isinstance(node, ast.Import):
            names.update(a.asname for a in node.names if a.name == 'asf.gitpush' and a.asname)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'push' and isinstance(node.func.value, ast.Name)
                and node.func.value.id in names
                and not any(k.arg == 'guard' or k.arg is None for k in node.keywords)):
            hits.append(node.lineno)
    return hits


def check(root, out=print, against=None):
    """0 when nothing rose above its baseline (and, with the push door open, every push is
    guarded); 1 otherwise. Prints one line per finding. ``against``: a revision (CI passes the
    trunk this change lands on); a file's ceiling is the higher of its baseline line and its count
    there, so a site another change landed meanwhile never turns this one red — only the sites
    this change adds do."""
    counts = count_tree(root)
    base = read_baseline(os.path.join(root, BASELINE))
    if against:
        there = count_rev(root, against)
        if there is None:
            out(f'check_clients: {against} unreadable — checking against the baseline alone')
        else:
            for key, n in there.items():
                base[key] = max(base.get(key, 0), n)
    rc = 0
    for key in sorted(set(counts) | set(base)):
        kind, rel = key
        n, ceiling = counts.get(key, 0), base.get(key, 0)
        if n > ceiling:
            client = ' / '.join(sorted(OWNERS[kind]))
            out(f'check_clients: {rel}: {n} raw {kind} call site(s), baseline {ceiling} — call '
                f'{client}, or mark the line `{EXEMPT} <why>`')
            rc = 1
        elif n < ceiling:
            out(f'check_clients: {rel}: {kind} {n} < baseline {ceiling} — lower {BASELINE}')
    if door_open(root):
        for rel in py_files(root):
            if rel == 'asf/gitpush.py':
                continue
            with open(os.path.join(root, rel), encoding='utf-8') as f:
                for line in unguarded_pushes(rel, f.read()):
                    out(f'check_clients: {rel}:{line}: gitpush.push( without guard=')
                    rc = 1
    total = {k: sum(n for (kk, _), n in counts.items() if kk == k) for k in KINDS}
    out('check_clients: ' + ('ok' if rc == 0 else 'FAILED') + ' — '
        + ', '.join(f'{k} {total[k]}' for k in KINDS))
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--root', default=ROOT)
    ap.add_argument('--write-baseline', action='store_true')
    ap.add_argument('--against', metavar='REV',
                    help='also allow each file the count it has at REV (the trunk it lands on)')
    a = ap.parse_args(argv)
    if a.write_baseline:
        write_baseline(os.path.join(a.root, BASELINE), count_tree(a.root))
        return 0
    return check(a.root, against=a.against)


if __name__ == '__main__':
    sys.exit(main())
