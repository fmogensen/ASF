"""asf.record.idclaim — an id is taken by a push: one allocator for local and cloud writers.

A claim is a ref created on the record repo's origin, ``refs/asf/ids/<P>-<lo>`` (``P`` the id
prefix, ``lo`` the first number of the block, four digits). The ref points at a parentless,
empty-tree commit whose message names the block and its claimant::

    asf id claim T:5000-5049

    range: T:5000-5049
    claimant: <job or session id>
    nonce: <uuid>

The push is create-only (``--force-with-lease=<ref>:`` — an empty expected value means *the ref
must not exist*), so of two writers that compute the same next block exactly one wins; the other
is refused, fetches ``refs/asf/ids/*`` again and takes the block above the new top. A single id
is a block of one. Several prefixes are claimed by one ``--atomic`` push: all or none.

The ref name carries only ``lo``: two writers that read the same top always collide on the same
name, whatever block size each asks for. Writers whose floors differ (a record max one of them
has not pulled) can still pick overlapping blocks under different names, so after a win the
claim list is read once more and a block that overlaps another claim is given up and the next
attempt goes above both. Attempts are bounded (:data:`ATTEMPTS`).

Where it runs: :func:`asf.workers.spawn.reserve_id_range` claims a job's ``BACKLOG_ID_RANGE``
here before any launch (local env var, and the cloud brief's text), and ``asf new`` claims its
single id here when it runs outside a job's range. Claims are on unless the product sets
``conventions.flags.id_claim: off``; a record repo with no ``origin`` keeps the local-only behaviour.
"""
import os
import re
import subprocess
import uuid
from collections import namedtuple
from types import SimpleNamespace

REF_NS = 'refs/asf/ids'
ATTEMPTS = 8
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
RANGE_LINE = re.compile(r'^range:\s*([A-Z]):(\d+)-(\d+)\s*$', re.MULTILINE)
CLAIMANT_LINE = re.compile(r'^claimant:\s*(.*)$', re.MULTILINE)
REF_NAME = re.compile(rf'^{REF_NS}/([A-Z])-(\d+)$')

Claim = namedtuple('Claim', 'prefix lo hi claimant ref')


class ClaimError(RuntimeError):
    """No block could be claimed within the bounded attempts, or the push failed outright."""


def _git(repo, *args, env=None):
    """One git call through :func:`asf.gitops.git`, read as ``returncode``/``stdout``/``stderr``."""
    from asf import gitops
    r = gitops.git(list(args), repo, env=env)
    return SimpleNamespace(returncode=0 if r.ok else (r.rc or -1), stdout=r.stdout or '',
                           stderr=r.stderr or r.reason or '')


def _empty_tree(repo):
    """Write the empty tree into ``repo`` (commit-tree needs the object) and return its id."""
    p = subprocess.run(['git', '-C', repo, 'mktree'], capture_output=True, text=True,  # client-exempt: mktree reads its (empty) stdin, which gitops.git does not feed
                       input='')
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else EMPTY_TREE


def enabled(product=None):
    """Claims are on by default; a product turns them off with ``conventions.flags.id_claim:
    off`` (a flag, not a new product key: a pinned older reader would refuse a new key)."""
    try:
        v = product.conventions.flag('id_claim', 'push') if product is not None else 'push'
    except Exception:  # noqa: BLE001 — a product without conventions keeps the default
        v = 'push'
    return str(v).strip().lower() not in ('off', 'false', 'no', 'none', 'local', '0')


def has_origin(repo, remote='origin'):
    if not repo or not os.path.isdir(repo):
        return False
    return _git(repo, 'remote', 'get-url', remote).returncode == 0


def fetch(repo, remote='origin'):
    """Mirror origin's claims into the local ``refs/asf/ids/*`` (pruning gone ones)."""
    p = _git(repo, 'fetch', '--quiet', '--no-tags', '--prune', remote,
             f'+{REF_NS}/*:{REF_NS}/*')
    if p.returncode != 0:
        raise ClaimError(f'fetch {REF_NS}: {p.stderr.strip()}')


def claims(repo):
    """Every claim the local mirror holds (run :func:`fetch` first for origin's view)."""
    p = _git(repo, 'for-each-ref', REF_NS, '--format=%(refname)%00%(contents)%01')
    out = []
    for rec in (p.stdout or '').split('\x01'):
        rec = rec.strip('\n')
        if '\x00' not in rec:
            continue
        ref, msg = rec.split('\x00', 1)
        m = RANGE_LINE.search(msg)
        if m:
            prefix, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
        else:  # a ref someone made by hand: its name is still a taken number
            n = REF_NAME.match(ref.strip())
            if not n:
                continue
            prefix, lo = n.group(1), int(n.group(2))
            hi = lo
        c = CLAIMANT_LINE.search(msg)
        out.append(Claim(prefix, lo, hi, (c.group(1).strip() if c else ''), ref.strip()))
    return out


def top(cl, prefix):
    return max((c.hi for c in cl if c.prefix == prefix), default=0)


def covers(cl, iid):
    """The claim whose block holds ``iid`` (``T-5003``), or None."""
    m = re.match(r'^([A-Z])-(\d+)$', iid or '')
    if not m:
        return None
    p, n = m.group(1), int(m.group(2))
    return next((c for c in cl if c.prefix == p and c.lo <= n <= c.hi), None)


def ref_for(prefix, lo):
    return f'{REF_NS}/{prefix}-{lo:04d}'


def _commit(repo, prefix, lo, hi, claimant):
    msg = (f'asf id claim {prefix}:{lo:04d}-{hi:04d}\n\nrange: {prefix}:{lo:04d}-{hi:04d}\n'
           f'claimant: {claimant}\nnonce: {uuid.uuid4().hex}\n')
    ident = {'GIT_AUTHOR_NAME': 'asf', 'GIT_AUTHOR_EMAIL': 'asf@localhost',
             'GIT_COMMITTER_NAME': 'asf', 'GIT_COMMITTER_EMAIL': 'asf@localhost'}
    tree = _empty_tree(repo)
    p = _git(repo, 'commit-tree', tree, '-m', msg, env=ident)
    if p.returncode != 0:
        raise ClaimError(f'commit-tree: {p.stderr.strip()}')
    return p.stdout.strip()


def _push_create(repo, remote, shas):
    """One atomic, create-only push of ``{ref: sha}``. True when origin took every ref."""
    args = ['push', '--quiet', '--atomic', '--no-verify', '--porcelain', remote]
    args += [f'--force-with-lease={ref}:' for ref in shas]
    args += [f'{sha}:{ref}' for ref, sha in shas.items()]
    return _git(repo, *args).returncode == 0


def claim(repo, sizes, claimant, floors=None, start=1, remote='origin', attempts=ATTEMPTS):
    """Claim one block per prefix on ``remote``: ``sizes`` ``{'T': 50, 'S': 50}``. Each block
    starts above ``start - 1``, above ``floors[prefix]`` (the caller's own top — the record's
    highest id, a local reservation) and above every block origin already holds. Returns
    ``{prefix: (lo, hi)}``; raises :class:`ClaimError` when every attempt was refused."""
    floors = floors or {}
    last = ''
    for _attempt in range(max(1, attempts)):
        fetch(repo, remote)
        cl = claims(repo)
        want = {}
        for p, size in sizes.items():
            lo = max(int(start), int(floors.get(p, 0)) + 1, top(cl, p) + 1)
            want[p] = (lo, lo + max(1, int(size)) - 1)
        shas = {ref_for(p, lo): _commit(repo, p, lo, hi, claimant) for p, (lo, hi) in want.items()}
        if not _push_create(repo, remote, shas):
            last = 'refused (a ref already exists)'
            continue
        fetch(repo, remote)
        # the ref on origin must be our commit: a push of a sha origin already holds reports
        # "up to date" — the nonce makes that impossible, and this makes it checked
        if any(_git(repo, 'rev-parse', '--verify', '-q', ref).stdout.strip() != sha
               for ref, sha in shas.items()):
            last = 'origin holds another commit at the ref'
            continue
        mine = set(shas)
        clash = [c for c in claims(repo) if c.ref not in mine
                 and any(c.prefix == p and c.lo <= hi and lo <= c.hi for p, (lo, hi) in want.items())]
        if clash:  # a writer with another floor took an overlapping block: give ours up, go above
            last = f'overlaps {clash[0].ref}'
            continue
        return want
    raise ClaimError(f'no id block claimed after {attempts} attempt(s): {last}')


def claim_one(repo, prefix, claimant, floor=0, remote='origin', attempts=ATTEMPTS):
    """A single id (a block of one): ``T-0123``."""
    lo, _hi = claim(repo, {prefix: 1}, claimant, floors={prefix: floor}, remote=remote,
                    attempts=attempts)[prefix]
    return f'{prefix}-{lo:04d}'


def claim_exact(repo, iid, claimant, remote='origin'):
    """Claim the one id ``iid`` (``T-0999``) itself, a block of one at its own number — for an id
    a landed document already cites that no claim covers and the record does not hold. True when
    origin now holds this claim; False when another claim covers it, or origin refused the
    create-only push (someone took the ref first). A failed fetch raises :class:`ClaimError`."""
    m = re.match(r'^([A-Z])-(\d+)$', iid or '')
    if not m:
        return False
    prefix, n = m.group(1), int(m.group(2))
    fetch(repo, remote)
    if covers(claims(repo), iid) is not None:
        return False
    ref = ref_for(prefix, n)
    sha = _commit(repo, prefix, n, n, claimant)
    if not _push_create(repo, remote, {ref: sha}):
        return False
    fetch(repo, remote)
    return _git(repo, 'rev-parse', '--verify', '-q', ref).stdout.strip() == sha


def range_text(blocks, order=None):
    """``{'S': (lo, hi)}`` → ``S:5000-5049,T:...`` (the ``BACKLOG_ID_RANGE`` form)."""
    keys = order or sorted(blocks)
    return ','.join(f'{p}:{blocks[p][0]:04d}-{blocks[p][1]:04d}' for p in keys if p in blocks)


def parse_range(text):
    """``S:5000-5049,T:...`` → ``{'S': (5000, 5049), ...}``."""
    return {m.group(1): (int(m.group(2)), int(m.group(3)))
            for m in re.finditer(r'([A-Z]):(\d+)-(\d+)', text or '')}
