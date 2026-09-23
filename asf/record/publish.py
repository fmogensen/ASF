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
