"""asf.record.publish — commit and push the card a console command filed (B-0091).

``asf new`` and ``asf inbox`` write a file into the record checkout; the tick reads the record
from origin, so a card left uncommitted never reaches it. Only the filed path is committed (never
the rest of the working tree), signed off, and pushed through the one record push."""
import os
import sys

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
    shadow._sh(['git', 'add', '--', rel], cwd=root)
    shadow._sh(['git', 'commit', '-q', '-s', '-m', message, '--only', '--', rel], cwd=root)
    if shadow.push(root):
        return True
    print(f"warning: {rel} committed but the push was refused — push the record by hand",
          file=sys.stderr)
    return False


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
    shadow._sh(['git', 'add', '-A', '--', *rels], cwd=root)
    shadow._sh(['git', 'commit', '-q', '-s', '-m', message, '--', *rels], cwd=root)
    if shadow.push(root):
        return True
    print(f"warning: {message!r} committed but the push was refused — push the record by hand",
          file=sys.stderr)
    return False
