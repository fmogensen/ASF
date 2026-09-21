"""asf.tick.shadow — the shadow clone every ``asf tick --shadow`` run works against.

A clone of a product's backlog at ``~/.ASF/state/<product>/shadow/``: fetched and fast-forwarded
from the backlog's own origin before every shadow tick, committed to locally (never pushed) after
one. It exists so the shadow tick can be compared against the real tools without ever touching a
product's real backlog checkout — see ``asf shadow-diff``.
"""
import os
import subprocess

from asf import env


def shadow_dir(product):
    return os.path.join(env.state_dir(product), 'shadow')


def _sh(args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check)


def _remote_url(backlog_dir):
    out = _sh(['git', 'remote', 'get-url', 'origin'], cwd=backlog_dir, check=False)
    return out.stdout.strip() if out.returncode == 0 else None


def _default_branch(clone_dir):
    out = _sh(['git', 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD'], cwd=clone_dir, check=False)
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip().rsplit('/', 1)[-1]
    return 'main'


def ensure_shadow_clone(product):
    """Clone or fast-forward the shadow, return its path. Never touches the real backlog checkout."""
    path = shadow_dir(product)
    if not os.path.isdir(os.path.join(path, '.git')):
        url = _remote_url(product.backlog_dir) or product.backlog_dir
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _sh(['git', 'clone', '-q', url, path])
        # `tables/*.md` (asf shadow-diff's inputs) live inside this clone at the path the shadow
        # tick spec names, but are not backlog content — exclude locally so `git add -A` never
        # stages them into the shadow's history.
        exclude = os.path.join(path, '.git', 'info', 'exclude')
        with open(exclude, 'a', encoding='utf-8') as f:
            f.write('\n/tables/\n')
    else:
        _sh(['git', 'fetch', '-q', 'origin'], cwd=path)
        branch = _default_branch(path)
        _sh(['git', 'checkout', '-q', branch], cwd=path)
        _sh(['git', 'pull', '-q', '--ff-only', 'origin', branch], cwd=path)
    return path


def commit_local(path, message):
    """``git add -A; git commit -s`` in the shadow; a no-change tick makes no commit."""
    _sh(['git', 'add', '-A'], cwd=path)
    status = _sh(['git', 'status', '--porcelain'], cwd=path)
    if not status.stdout.strip():
        return False
    _sh(['git', 'commit', '-q', '-s', '-m', message], cwd=path)
    return True
