"""asf.tick.shadow — the clones ``asf tick`` works in.

A clone of a product's backlog, fetched and hard-reset to its origin's default branch before
every tick (a stray local commit — from a prior run whose push finally failed — is discarded,
not fast-forwarded past). Its push is one function, :func:`push` (B-0030, B-0044): when origin
moved while the tick ran — or again between the push's own fetch and push — the clone is
rebased onto it and pushed again, up to :data:`PUSH_RETRIES` times, so the lines the steps
appended (events, the tick line, filed cards) are not lost with a refused commit; a conflict on
machine-owned content is resolved by ownership, any other conflict is aborted and the refusal
stands. The live tick's clone is
``~/.ASF/state/<product>/record/``: it commits there and pushes (:func:`push`). The shadow tick's
is ``…/shadow/``: it commits locally and never pushes, so it can be compared against the real
tools without touching anything — see ``asf shadow-diff``. Neither writes the operator's own
backlog checkout; after the live tick's push, :func:`sync_operator_checkout` only fast-forwards
it to origin (clean and on the trunk, never a reset), because the read views read it.
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


#: How many times origin may move under one push before the tick gives up on this tick's
#: lines (the next run resets the clone and re-derives, as the module docstring says).
PUSH_RETRIES = 3


def push(path, out=None, retries=PUSH_RETRIES):
    """``git push origin HEAD:<default branch>``; True if origin took it. The one record push
    (B-0030, B-0044): every path that moves the clone onto origin lives here.

    On a refusal: fetch; if ``origin/<branch>`` is still an ancestor of HEAD, origin has not
    moved and the refusal has another cause (a hook, an unreachable remote) — False. If origin
    moved — a card pushed by hand while the tick ran, or between this function's fetch and its
    push — rebase onto it and push again, up to ``retries`` times, so a race between the fetch
    and the push is just the next round. A rebase that conflicts on cards or ``index.json`` is
    resolved by ownership (the hand side's typed fields and body, the tick's derivation re-run)
    and continued; any other conflict is aborted and the push is False. One line through
    ``out`` when the push needed a rebase."""
    branch = _default_branch(path)
    if _push_once(path, branch):
        return True
    rederived = False
    for _round in range(retries):
        if _sh(['git', 'fetch', '-q', 'origin'], cwd=path, check=False).returncode != 0:
            return False
        if _sh(['git', 'merge-base', '--is-ancestor', f'origin/{branch}', 'HEAD'],
               cwd=path, check=False).returncode == 0:
            return False  # origin has not moved: the refusal has another cause
        rebase = _sh(['git', '-c', 'core.editor=true', 'rebase', f'origin/{branch}'],
                     cwd=path, check=False)
        while rebase.returncode != 0:
            if not _resolve_by_ownership(path):
                _sh(['git', 'rebase', '--abort'], cwd=path, check=False)
                return False
            rederived = True
            rebase = _sh(['git', '-c', 'core.editor=true', 'rebase', '--continue'], cwd=path, check=False)
        if _push_once(path, branch):
            if out:
                if rederived:
                    out(f'tick: origin moved — re-derived onto origin/{branch} and pushed')
                else:
                    out(f'tick: origin moved during the tick — rebased onto origin/{branch} and pushed')
            return True
    return False


def _stage(path, n, rel):
    r = _sh(['git', 'show', f':{n}:{rel}'], cwd=path, check=False)
    return r.stdout if r.returncode == 0 else None


def _body(text):
    """The card text after its closing frontmatter ``---`` line."""
    parts = text.split('\n---\n', 1)
    return parts[1] if len(parts) == 2 else text


def _resolve_by_ownership(path):
    """Mid-rebase (B-0044): ``ours`` is origin (the hand side), ``theirs`` the tick's commit. Every
    conflicted card and ``index.json`` take the hand side, then the index derivation re-runs in the
    clone; the ingest machine block is left as the hand side has it and the next tick re-derives it.
    False when a conflict falls outside those regions (not a card, or a body both sides edited)."""
    from asf.record.index import do_index
    conflicted = _sh(['git', 'diff', '--name-only', '--diff-filter=U'], cwd=path, check=False).stdout.split()
    if not conflicted:
        return False
    for rel in conflicted:
        if rel != 'index.json':
            if not rel.endswith('.md'):
                return False
            base, ours, theirs = (_stage(path, n, rel) for n in (1, 2, 3))
            if ours is None or theirs is None:
                return False
            if base is not None and _body(base) != _body(ours) != _body(theirs) != _body(base):
                return False
        if _sh(['git', 'checkout', '--ours', '--', rel], cwd=path, check=False).returncode != 0:
            return False
    if do_index(path) != 0:
        return False
    return _sh(['git', 'add', '-A'], cwd=path, check=False).returncode == 0


def sync_operator_checkout(product, out=print):
    """Fast-forward the operator's record checkout (``backlog_dir``) to ``origin/<trunk>``.

    The tick commits and pushes from its own clone; ``asf status``'s Decisions row, ``asf
    backlog`` and ``asf next`` read ``backlog_dir``. Only a console command run there pulled it,
    so answers the tick applied and pushed never showed: the checkout sat where the last console
    command left it and the Decisions count froze. The same rule as the product checkout's
    (``harvest.sync_checkout``, B-0042): only on the trunk with a clean tree, only ``--ff-only``;
    anything else is left alone and named in one line. Silent when already current. True when
    moved."""
    repo = product.backlog_dir
    if not repo or not os.path.isdir(os.path.join(repo, '.git')):
        return False
    if os.path.realpath(repo) == os.path.realpath(record_dir(product)):
        return False
    if _sh(['git', 'fetch', '-q', 'origin'], cwd=repo, check=False).returncode != 0:
        return False
    trunk = _default_branch(repo)
    behind = _sh(['git', 'rev-list', '--count', f'HEAD..origin/{trunk}'], cwd=repo, check=False)
    if behind.returncode != 0 or behind.stdout.strip() in ('', '0'):
        return False
    head = _sh(['git', 'symbolic-ref', '-q', '--short', 'HEAD'], cwd=repo, check=False).stdout.strip()
    if head != trunk:
        out(f'record: {repo} not fast-forwarded — on {head or "a detached HEAD"}, not {trunk}')
        return False
    if _sh(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=repo,
           check=False).stdout.strip():
        out(f'record: {repo} not fast-forwarded — working tree has local changes')
        return False
    merge = _sh(['git', 'merge', '-q', '--ff-only', f'origin/{trunk}'], cwd=repo, check=False)
    if merge.returncode != 0:
        detail = (merge.stderr or merge.stdout).strip().splitlines()
        out(f'record: {repo} not fast-forwarded — {detail[-1] if detail else "merge refused"}')
        return False
    return True
