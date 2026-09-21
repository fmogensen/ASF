"""asf.tick.shadow — the clones ``asf tick`` works in.

A clone of a product's backlog, fetched and hard-reset to its origin's default branch before
every tick (a stray local commit — from a prior run whose push finally failed — is discarded,
not fast-forwarded past). Its push has one recovery (B-0030): when origin moved while the tick
ran, the clone is rebased onto it once and pushed again, so the lines the steps appended
(events, the tick line, filed cards) are not lost with a refused commit; a conflict on that
rebase is aborted and the refusal stands. The live tick's clone is
``~/.ASF/state/<product>/record/``: it commits there and pushes (:func:`push`). The shadow tick's
is ``…/shadow/``: it commits locally and never pushes, so it can be compared against the real
tools without touching anything — see ``asf shadow-diff``. Neither ever touches the operator's own
backlog checkout.
"""
import os
import re
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


def record_dir(product):
    return os.path.join(env.state_dir(product), 'record')


def factory_identity():
    """``(name, email)`` from config ``factory.git_identity`` (``Name <email>``), default
    ``ASF <asf@localhost>`` — what a state commit from a tick clone is authored as."""
    raw = ((env.load_config().get('factory') or {}).get('git_identity')) or 'ASF <asf@localhost>'
    m = re.match(r'^\s*(.*?)\s*<([^>]*)>\s*$', str(raw))
    return (m.group(1), m.group(2)) if m else (str(raw).strip(), 'asf@localhost')


def ensure_clone(product, path, exclude_tables=False):
    """Clone or reset the clone at ``path``, return the path. Never touches the operator's backlog
    checkout — it is read once, for the ``origin`` url, and never again.

    A derived-state clone owns no state of its own (bug class B-1352): ``git pull --ff-only``
    leaves a clone stuck the moment a prior run's push failed and its local commit didn't reach
    origin, so every run instead resets hard to ``origin/<default branch>`` — a stray local
    commit is discarded, not fought. The clone commits as the factory identity (repo-local
    ``user.name``/``user.email``, set every run).

    ``exclude_tables``: keep ``tables/`` (the shadow tick's rendered views, ``asf shadow-diff``'s
    inputs) out of the clone's history — they are not backlog content.
    """
    if not os.path.isdir(os.path.join(path, '.git')):
        url = _remote_url(product.backlog_dir) if product.backlog_dir else None
        url = url or product.backlog_dir
        if not url:
            raise env.ConfigError(f'product {product.name} has no backlog_dir to learn the origin url from')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _sh(['git', 'clone', '-q', url, path])
    else:
        _sh(['git', 'fetch', '-q', 'origin'], cwd=path)
        branch = _default_branch(path)
        _sh(['git', 'reset', '-q', '--hard', f'origin/{branch}'], cwd=path)
        _sh(['git', 'clean', '-fdq'], cwd=path)
    name, email = factory_identity()
    _sh(['git', 'config', 'user.name', name], cwd=path)
    _sh(['git', 'config', 'user.email', email], cwd=path)
    if exclude_tables:
        exclude = os.path.join(path, '.git', 'info', 'exclude')
        with open(exclude, 'a+', encoding='utf-8') as f:
            f.seek(0)
            if '/tables/' not in f.read().splitlines():
                f.write('\n/tables/\n')
    return path


def ensure_shadow_clone(product):
    """The shadow clone (``…/shadow``): :func:`ensure_clone` with ``tables/`` excluded."""
    return ensure_clone(product, shadow_dir(product), exclude_tables=True)


def commit_local(path, message):
    """``git add -A; git commit -s`` in the clone; a no-change tick makes no commit."""
    _sh(['git', 'add', '-A'], cwd=path)
    status = _sh(['git', 'status', '--porcelain'], cwd=path)
    if not status.stdout.strip():
        return False
    _sh(['git', 'commit', '-q', '-s', '-m', message], cwd=path)
    return True


def _push_once(path, branch):
    return _sh(['git', 'push', '-q', 'origin', f'HEAD:{branch}'], cwd=path, check=False).returncode == 0


def push(path, out=None):
    """``git push origin HEAD:<default branch>``; True if origin took it.

    On a refusal: fetch; if ``origin/<branch>`` is still an ancestor of HEAD origin has not
    moved and the refusal has another cause (a hook, an unreachable remote) — False, as before.
    If origin moved (a card pushed by hand while the tick ran — B-0030), rebase onto it once and
    push again: True, and one line through ``out`` when given. A rebase that conflicts is
    aborted and the push is False: the clone is derived state plus that tick's appended lines,
    and the next run resets it and re-derives (the appended lines of that one tick are lost, as
    the docstring above says)."""
    branch = _default_branch(path)
    if _push_once(path, branch):
        return True
    if _sh(['git', 'fetch', '-q', 'origin'], cwd=path, check=False).returncode != 0:
        return False
    if _sh(['git', 'merge-base', '--is-ancestor', f'origin/{branch}', 'HEAD'],
           cwd=path, check=False).returncode == 0:
        return False
    rebase = _sh(['git', '-c', 'core.editor=true', 'rebase', f'origin/{branch}'],
                 cwd=path, check=False)
    if rebase.returncode != 0:
        _sh(['git', 'rebase', '--abort'], cwd=path, check=False)
        return False
    if not _push_once(path, branch):
        return False
    if out:
        out(f'tick: origin moved during the tick — rebased onto origin/{branch} and pushed')
    return True
