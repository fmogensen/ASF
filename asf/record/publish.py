"""asf.record.publish — commit and push the card a console command filed (B-0091).

``asf new`` and ``asf inbox`` write a file into the record checkout; the tick reads the record
from origin, so a card left uncommitted never reaches it. Only the filed path is committed (never
the rest of the working tree), signed off, and pushed through the one record push.

Every commit carries what the published paths imply for the derived record — ``index.json`` and
the Children/Backlinks of every card they touch (what ``asf index`` writes) — derived from ``HEAD``
plus those paths, never from the working tree: an operator's uncommitted edit to another card
stays out of the commit, and the record pre-commit (``asf check --staged``) sees a record whose
derivation matches its cards."""
import os
import shutil
import sys
import tempfile

from asf.record import tree
from asf.redact import _run_git
from asf.tick import shadow


def _is_checkout(root):
    top = shadow._sh(['git', 'rev-parse', '--show-toplevel'], cwd=root, check=False)
    return (top.returncode == 0
            and os.path.realpath(top.stdout.strip()) == os.path.realpath(root)
            and shadow._remote_url(root) is not None)


def publish(root, path, message):
    """Commit ``path`` in the record checkout ``root`` and push it. A ``root`` that is not a git
    checkout with an ``origin`` is left alone. One stderr line when the push is refused."""
    if not _is_checkout(root):
        return True
    rel = os.path.relpath(path, root)
    if not commit_paths(root, [rel], message):
        return True
    if shadow.push(root):
        return True
    print(f"warning: {rel} committed but the push was refused — push the record by hand",
          file=sys.stderr)
    return False


def commit_paths(root, rels, message):
    """Commit ``rels`` as the working tree of the record checkout ``root`` has them, with the
    derived record they imply (:func:`asf.record.index.refresh` over ``HEAD`` plus ``rels``),
    signed off. The commit is built in a scratch index, so nothing else staged or edited in the
    checkout rides along; afterwards the checkout's index and working tree hold what was
    committed for those paths — a card the operator had edited by hand keeps the edit, with its
    Children/Backlinks brought up to date. False when there was nothing to commit; a refusal
    by the pre-commit raises (``subprocess.CalledProcessError``), its output on stderr."""
    from asf.record.core import title_scrub
    from asf.record.index import refresh
    dirty = set(_dirty(root))
    scrub = title_scrub(root)
    with tempfile.TemporaryDirectory(prefix='asf-publish-') as scratch:
        tree_dir = os.path.join(scratch, 'tree')
        git_env = tree.head_index(root, os.path.join(scratch, 'index'))
        tree.lay_out(root, tree_dir, tree.record_paths(), git_env)
        for rel in rels:
            _copy(os.path.join(root, rel), os.path.join(tree_dir, rel))
        derived = [p for p in refresh(tree_dir, scrub=scrub) if p not in rels]
        paths = sorted(set(rels) | set(derived))
        tracked = set(_run_git(root, ['ls-files', '-z', '--', *paths],
                               git_env=git_env).stdout.split('\0'))
        # a path neither on disk nor in HEAD (written, then removed again) is nothing to commit
        paths = [p for p in paths if os.path.lexists(os.path.join(tree_dir, p)) or p in tracked]
        if not paths:
            return False
        _run_git(root, ['--work-tree', tree_dir, 'add', '-A', '--', *paths],
                 git_env=git_env).check_returncode()
        if _run_git(root, ['diff', '--cached', '--quiet'], git_env=git_env).returncode == 0:
            return False
        done = _run_git(root, ['commit', '-q', '-s', '-m', message], git_env=git_env)
        if done.returncode != 0:
            sys.stderr.write(done.stdout + done.stderr)
            done.check_returncode()
        _run_git(root, ['reset', '-q', '--', *paths])
        for p in paths:
            if p in derived and p in dirty and p != 'index.json':
                continue  # a hand edit stays; its derived sections are refreshed below
            _copy(os.path.join(tree_dir, p), os.path.join(root, p))
    hand = {p for p in derived if p in dirty and p != 'index.json'}
    if hand:
        refresh(root, scrub=scrub, only=hand, index=False)
    return True


def _copy(src, dst):
    """``dst`` made what ``src`` is: its bytes and mode, or gone when ``src`` is."""
    if os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        shutil.copymode(src, dst)
    elif os.path.lexists(dst) and not os.path.isdir(dst):
        os.remove(dst)


def _dirty(root):
    """``{relpath: content}`` for every path ``git status`` lists as changed or untracked in
    ``root`` — the content read now (``None`` for a deleted path), so a path already dirty before
    a command is told apart from one the command wrote."""
    st = shadow._sh(['git', 'status', '--porcelain', '-z', '--untracked-files=all'], cwd=root,
                    check=False)
    out = {}
    entries = st.stdout.split('\0') if st.returncode == 0 else []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, rel = entry[:2], entry[3:]
        if 'R' in code or 'C' in code:
            out[entries[i]] = None  # the rename's source: gone from where it was
            i += 1
        try:
            with open(os.path.join(root, rel), 'rb') as f:
                out[rel] = f.read()
        except OSError:
            out[rel] = None
    return out


def snapshot(root):
    """What the record checkout ``root`` already had changed before a command ran — the
    ``before`` :func:`publish_changes` takes. ``None`` when ``root`` is not a checkout with an
    ``origin`` (nothing will be published)."""
    return _dirty(root) if _is_checkout(root) else None


def publish_changes(root, before, message):
    """Commit and push every path the command changed in the record checkout ``root`` since
    :func:`snapshot` — and nothing the operator had already changed by hand — the way
    :func:`publish` does one card: signed off, through the one record push, which rebases when
    origin moved. ``asf groom`` rewrites cards, the groom file, the digest and ``index.json``; left
    in the checkout, the tick's clone (reset to origin) never sees them. No change: no commit.
    Returns True when origin has the change (or there was none to give it)."""
    if before is None:
        return True
    rels = sorted(rel for rel, content in _dirty(root).items()
                  if rel not in before or before[rel] != content)
    if not rels:
        return True
    if not commit_paths(root, rels, message):
        return True
    if shadow.push(root):
        return True
    print(f"warning: {message!r} committed but the push was refused — push the record by hand",
          file=sys.stderr)
    return False
