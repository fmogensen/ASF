"""asf.workers.seats — the claim ledger: one file every product on the host can read, one lock
that makes a claim atomic, and the one definition of when a claim is alive (F-0189).

An account's cap is the machine's, not one product's (F-0076 S-8154), but until this module a
launch's seat lived only in the pool that picked it — one process's own list — so two products'
waves each read the account at its opening load and each filled it to the cap. A claim closes
that: it is held at ``<ASF_HOME>/state/seats.json``, where every product looks, and tested
against the cap under the ``flock`` this module holds, in the same atomic step as ``Pool.take``
(``asf.workers.pool``).

Nothing imports this module yet, and nothing observable changes while that stays true.
"""
import calendar
import contextlib
import fcntl
import json
import os
import time

from asf import env
from asf.workers import lifecycle

LEDGER = 'seats.json'          # <ASF_HOME>/state/seats.json — the machine's, not one product's
LOCK = 'seats.lock'
CLAIM_TTL_S = 30 * 60          # a claim no launch caught up with is the crash floor, not a seat
LOCK_WAIT_S = 5.0              # a holder writes one small file; a wait past this is not a holder
POLL_S = 0.02
FIELDS = ('account', 'product', 'job', 'model', 'kind', 'lane', 'pid', 'at')

_TIME_FMT = '%Y-%m-%dT%H:%M:%SZ'


class Busy(Exception):
    """The lock was not free inside ``wait_s``."""


class Unusable(Exception):
    """The state dir cannot hold the ledger at all; its reason is its ``str``."""


def root():
    """``<ASF_HOME>/state`` — resolved per call (P7): a test that swaps ``env.ASF_HOME`` between
    two calls gets two answers."""
    return os.path.join(env.ASF_HOME, 'state')


def ledger_path():
    return os.path.join(root(), LEDGER)


def lock_path():
    return os.path.join(root(), LOCK)


def _stamp(now=None):
    return time.strftime(_TIME_FMT, time.gmtime(now))


def _epoch(at):
    """``at`` (an ISO stamp from :func:`_stamp`) as epoch seconds, or ``None`` when it will not
    parse — an unparseable claim is treated as expired, not as an exception."""
    try:
        return calendar.timegm(time.strptime(at, _TIME_FMT))
    except (TypeError, ValueError):
        return None


def _registered_keys(registered):
    if registered is None:
        registered = lifecycle.live_all(root())
    return {(r.get('product'), r.get('job')) for r in registered}


def _alive(claim, registered_keys, now):
    """A claim dies three ways (C5): its claimer's pid is gone, it is older than
    :data:`CLAIM_TTL_S`, or a live registered run already holds its ``(product, job)`` — the
    launch line landed, and the registry holds that seat now."""
    if not lifecycle.pid_alive(claim.get('pid')):
        return False
    age = _epoch(claim.get('at'))
    if age is None or (now - age) > tunable('CLAIM_TTL_S'):
        return False
    if (claim.get('product'), claim.get('job')) in registered_keys:
        return False
    return True


def _sweep(claims, registered, now):
    keys = _registered_keys(registered)
    live = [c for c in claims if _alive(c, keys, now)]
    return live, len(live) != len(claims)


def _load(path):
    """``(claims, why)`` off disk. A missing file is no claims and no reason — the normal state
    of a quiet host. A file that will not open or will not parse is no claims, with the reason;
    it is never rewritten here — a lock-free read is not a writer's turn (C1)."""
    if not os.path.isfile(path):
        return [], ''
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [], str(e)
    if not isinstance(data, list):
        return [], f'{path}: not a list'
    return [c for c in data if isinstance(c, dict)], ''


def read(registered=None, now=None):
    """The live claims, lock-free — what :meth:`asf.workers.pool.Pool.from_config` counts.
    Never raises: a missing or garbage ``seats.json`` is ``([], why)``, and ``why`` is what the
    wave prints (C6) rather than losing.

    ``registered`` is the caller's already-computed live registered runs (so a caller that has
    just walked every registry, like ``from_config``, does not walk it twice); ``None`` walks
    :func:`asf.workers.lifecycle.live_all` itself."""
    now = time.time() if now is None else now
    claims, why = _load(ledger_path())
    live, _dropped = _sweep(claims, registered, now)
    return live, why


class Ledger:
    """The claims already swept, held under the caller's lock. ``add``/``remove`` mutate
    ``self.claims`` and rewrite the whole file — a claim is removed as often as it is added, and
    a partial write of a list is not a list."""

    def __init__(self, claims, path):
        self.claims = list(claims)
        self._path = path

    def _write(self):
        tmp = self._path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.claims, f)
        os.replace(tmp, self._path)

    def add(self, claim):
        """Store exactly :data:`FIELDS` of ``claim`` and nothing else."""
        self.claims.append({k: claim.get(k) for k in FIELDS})
        self._write()

    def remove(self, product, job):
        self.claims = [c for c in self.claims
                       if not (c.get('product') == product and c.get('job') == job)]
        self._write()


@contextlib.contextmanager
def held(wait_s=None):
    """The exclusive claim lock; yields a :class:`Ledger` already swept. Raises :class:`Busy`
    when the lock is not free inside ``wait_s``, or :class:`Unusable` when the state root cannot
    be created or its lock file cannot be opened — carrying the reason as its ``str`` (C6)."""
    wait_s = tunable('LOCK_WAIT_S') if wait_s is None else wait_s
    try:
        os.makedirs(root(), exist_ok=True)
    except OSError as e:
        raise Unusable(str(e)) from e
    try:
        f = open(lock_path(), 'a')
    except OSError as e:
        raise Unusable(str(e)) from e
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy(f'seats lock busy: {lock_path()}')
                time.sleep(POLL_S)
        claims, why = _load(ledger_path())
        live, dropped = _sweep(claims, None, time.time())
        ledger = Ledger(live, ledger_path())
        if dropped or why:  # a sweep that dropped rows, or a garbage file, writes on the way in
            ledger._write()
        yield ledger
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


# ---- tunables ---------------------------------------------------------------

#: The config key (``~/.ASF/config.yaml``) over each constant above; the constant is its default.
TUNABLES = {
    'CLAIM_TTL_S': 'worker_pool.seats.claim_ttl_s',
    'LOCK_WAIT_S': 'worker_pool.seats.lock_wait_s',
}


def tunable(name):
    """The constant ``name`` of :data:`TUNABLES` with its config key over it."""
    from asf import config_keys
    return config_keys.value(TUNABLES[name], globals()[name])
