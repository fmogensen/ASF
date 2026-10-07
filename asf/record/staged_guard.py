"""asf.record.staged_guard — a record commit never deletes a card it did not mean to (F-0282).

A record write from a stale checkout or a stale index stages the whole tree as that checkout
sees it: a card another writer filed and pushed minutes earlier is in ``HEAD`` but not in the
stale tree, so the commit reads it as a deletion and the card is gone from the trunk with
nothing said (a story mint once deleted a filed S1 inbox card this way). The record's
pre-commit (``asf redact --pre-commit``, :mod:`asf.redact`) runs :func:`check` over the staged
index before anything else is judged.

A staged deletion is refused when all of these hold:

* the path is a card (``<item folder>/<id>….md``) or an intake note (``<intake dir>/<name>.md``,
  never ``<intake dir>/done/``) of a record — a repo whose ``HEAD`` carries ``index.json``;
* origin's trunk still carries the path — a card the trunk already removed is no loss;
* the branch did not create it: the path is in the merge base of ``HEAD`` and the trunk;
* its text survives nowhere in the commit: intake's own move (``<intake dir>/done/<slug>.md``
  carries the note's text under a ``→ <id>`` line) and a plain move both keep it, and pass.

No origin trunk to compare against is no refusal: the guard judges against what origin has, and
says nothing it cannot back. A person who means a deletion sets :data:`ALLOW_VAR` for that one
commit — the refusal names it.
"""
import os

from asf.redact import _run_git

#: The environment variable that lets one commit delete a card the guard would refuse.
ALLOW_VAR = 'ASF_ALLOW_CARD_DELETE'

#: The intake directory when no product names its own (:data:`asf.conventions.DEFAULT_INTAKE_DIR`).
DEFAULT_INTAKE = 'inbox'


def _git(repo, args):
    return _run_git(repo, args)


def _ok(p):
    return p.returncode == 0


def is_record(repo):
    """``repo``'s ``HEAD`` carries the derived ``index.json``: a record, not a code repo."""
    return _ok(_git(repo, ['cat-file', '-e', 'HEAD:index.json']))


def trunk_ref(repo, trunk=None):
    """``origin/<trunk>`` when that ref exists: the named trunk, else origin's ``HEAD``, else
    ``main``. None when origin has no such ref (nothing to judge against)."""
    names = [trunk] if trunk else []
    sym = _git(repo, ['symbolic-ref', '--short', '-q', 'refs/remotes/origin/HEAD'])
    if _ok(sym) and sym.stdout.strip().startswith('origin/'):
        names.append(sym.stdout.strip()[len('origin/'):])
    names.append('main')
    for name in names:
        ref = f'origin/{name}'
        if _ok(_git(repo, ['rev-parse', '--verify', '-q', ref + '^{commit}'])):
            return ref
    return None


def guarded(path, intake_dir=DEFAULT_INTAKE):
    """``path`` (repo-relative) is a card or an intake note — what a stale tree loses."""
    from asf.record.core import ITEM_FOLDERS
    parts = path.split('/')
    if len(parts) != 2 or not parts[1].endswith('.md'):
        return False
    folder, name = parts
    if folder == (intake_dir or DEFAULT_INTAKE).strip('/'):
        return True
    return folder in ITEM_FOLDERS and name[:1].isupper() and '-' in name


def _staged(repo):
    """``(deleted, written)``: the staged deletions, and every path the commit adds or changes."""
    p = _git(repo, ['diff', '--cached', '--name-status', '--no-renames', '-z'])
    if not _ok(p):
        return [], []
    fields = p.stdout.split('\0')
    deleted, written = [], []
    for status, path in zip(fields[0::2], fields[1::2]):
        if status.startswith('D'):
            deleted.append(path)
        elif status[:1] in ('A', 'M', 'T'):
            written.append(path)
    return deleted, written


def _blob(repo, spec):
    p = _git(repo, ['cat-file', '-p', spec])
    return p.stdout if _ok(p) else None


def _exists(repo, rev, path):
    return bool(rev) and _ok(_git(repo, ['cat-file', '-e', f'{rev}:{path}']))


def check(repo, trunk=None, intake_dir=None, environ=None):
    """The refusal lines for ``repo``'s staged index — ``[]`` when the commit may go. Each line
    names the card and why it is kept."""
    environ = os.environ if environ is None else environ
    if environ.get(ALLOW_VAR) == '1' or not is_record(repo):
        return []
    deleted, written = _staged(repo)
    deleted = [p for p in deleted if guarded(p, intake_dir)]
    if not deleted:
        return []
    ref = trunk_ref(repo, trunk)
    if ref is None:
        return []
    mb = _git(repo, ['merge-base', 'HEAD', ref])
    base = mb.stdout.strip() if _ok(mb) else ''
    kept = [t for t in (_blob(repo, f':{w}') for w in written) if t]
    out = []
    for path in deleted:
        if not _exists(repo, ref, path) or not _exists(repo, base, path):
            continue  # the trunk already dropped it, or this branch made it
        text = (_blob(repo, f'HEAD:{path}') or '').strip()
        if text and any(text in t for t in kept):
            continue  # moved: intake's done/ note, or the same text under another name
        out.append(f'staged-guard: {path} is on {ref} and this commit deletes it — a stale tree '
                   f'or index drops a card another writer filed (F-0282). Restore it '
                   f'(git restore --staged --worktree -- {path}), or set {ALLOW_VAR}=1 for a '
                   'deletion you mean')
    return out
