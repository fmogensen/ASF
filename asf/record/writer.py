"""asf.record.writer — the one way a record file is written: whole, in one ``os.replace``.

Every record writer used to ``open(path, 'w')`` the card: the file is truncated first and
filled after, so a reader in between — a tick's ``load_items``, a session's ``asf show``,
``git add`` — saw an empty or half-written card. Each write here goes to a temp file beside the
target and replaces it (:func:`asf.state.store.atomic_write_bytes`): a reader sees the old card
or the new one, never part of either, and a failed write leaves the old card and no temp file.

The bytes are the text's utf-8, exactly what ``open(path, 'w', encoding='utf-8')`` wrote on
POSIX, so an unchanged card stays byte-identical. An existing file keeps its permission bits; a
new one gets what ``open`` would have given it (``0o666`` less the umask). A symlinked path is
written through the link. There is no lock: a card write is a whole-file replace, and a lock
file beside a card would land in the record's tree. The repo's record-writers lint refuses an
``open(..., 'w')`` anywhere else under ``asf/record/``.
"""
import os
import stat

from asf.state import store


def _read_umask():
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


#: read once at import: ``os.umask`` can only be read by setting it, and setting it while another
#: thread creates a file would give that file the wrong mode
UMASK = _read_umask()


def _mode(target):
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        return 0o666 & ~UMASK


def write_text(path, text):
    """``text`` (utf-8) at ``path``, atomically; the directory is created when missing."""
    target = os.path.realpath(path)
    payload = text.encode('utf-8')
    store.atomic_write_bytes(target, payload, mode=_mode(target))


def write_card(path, text):
    """A card's whole ``text`` at ``path`` — :func:`write_text` under the name the sites read."""
    write_text(path, text)


def write_bytes(path, payload):
    """Raw bytes at ``path`` (a staged copy, a card restored as it stood), atomically."""
    target = os.path.realpath(path)
    store.atomic_write_bytes(target, payload, mode=_mode(target))
