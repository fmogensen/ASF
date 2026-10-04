"""asf.state.store — the one way a state file is read and written: whole, versioned, locked.

Every state file under ``<ASF_HOME>/state/`` was its own module's business: a fixed ``.tmp``
name (two writers share it), no lock around a read-modify-write (a second process's land
request or rebuild request vanished between the read and the write), no version (a format
change reset the file silently), and a parse error read as "empty" — the next write then
destroyed whatever the file held. This module is the shared answer; the callers move one owner
at a time (the merge queue's requests and rebuilds first, behind ``flags.queue_store``).

* :func:`atomic_write_json` — ``mkstemp`` in the file's own directory, ``fsync``, ``os.replace``:
  a reader sees the old file or the new one, never half of either, and a crash leaves the old;
* :func:`read` — lock-free, ``Read(data, version, state)`` with ``state`` one of
  :data:`OK`/:data:`ABSENT`/:data:`CORRUPT`. Only an absent file takes ``default``'s value as
  normal; a corrupt one also reads as ``default`` but says so, loudly, and its version is its own;
* :func:`write` — under the file's ``flock``; ``expect`` (a version from :func:`read`) that is
  no longer current raises :class:`StoreConflict`;
* :func:`update` — under the ``flock``: read, ``fn(data)``, atomic write. A corrupt file is
  copied to ``<name>.corrupt-<ts>`` and :class:`StoreCorrupt` raised — it is never overwritten
  with ``fn(default)``. A lock not free inside ``timeout_s`` raises :class:`StoreBusy`;
* :func:`write_at` / :func:`update_at` — the same two for a caller that holds a state file's path
  (a module handed a state directory) rather than its product; the file name is still the
  registered name;
* :func:`append` — one jsonl record, ``O_APPEND`` under the same ``flock``;
* :func:`reap` — the files :mod:`asf.state.registry` does not name, or names with a TTL they
  outlived, moved to ``.trash/<date>/`` (purged after 14 days); a dry run unless ``apply``.

The version of a file is its ``st_mtime_ns``, kept strictly increasing by every store write (a
write that lands in the same tick as the last one is stamped one nanosecond on). The *schema* a
file was written with is the registry's ``schema`` at that moment, kept in the directory's
``.schemas.json`` sidecar (:func:`schema`) — the file's own shape is untouched, so every
existing reader still reads it.

The lock is ``<file>.lock`` beside the file, taken per call and never across one: ``update`` does
not nest (a second ``update`` of the same file inside ``fn`` waits on itself until
:class:`StoreBusy`). A ``shared`` name (one file per host, written by every product) is refused
for every mutation until every product sets ``flags.store_shared: on`` (:func:`shared_ready`):
while one product still writes it with its own code, a second locking scheme beside it would
only look safe.
"""
import contextlib
import copy
import datetime
import errno
import fcntl
import glob
import json
import os
import shutil
import sys
import tempfile
import time
from collections import namedtuple

from asf import env
from asf.state import registry

OK, ABSENT, CORRUPT = 'ok', 'absent', 'corrupt'
LOCK_SUFFIX = '.lock'
CORRUPT_INFIX = '.corrupt-'
TIMEOUT_S = 10.0
POLL_S = 0.01
SHARED_FLAG = 'store_shared'

Read = namedtuple('Read', 'data version state')


class StoreError(Exception):
    """Every refusal of this module."""


class StoreUnregistered(StoreError, KeyError):
    """The name is not in :mod:`asf.state.registry`."""

    def __str__(self):
        return str(self.args[0]) if self.args else ''


class StoreBusy(StoreError):
    """The file's lock was not free inside the timeout."""


class StoreConflict(StoreError):
    """``write(expect=v)`` found another version on disk."""


class StoreRefused(StoreError):
    """A mutation of a shared file before every product runs the store."""


class StoreCorrupt(StoreError):
    """The file exists and will not parse. ``path`` is the file, ``quarantine`` its copy."""

    def __init__(self, path, quarantine, why):
        super().__init__(f'{path}: corrupt ({why}); a copy is at {quarantine}')
        self.path, self.quarantine, self.why = path, quarantine, why


def _warn(line):
    print(line, file=sys.stderr, flush=True)


# ---- names and paths ----------------------------------------------------------------------------

def _spec(name):
    spec = registry.spec(name)
    if spec is None:
        raise StoreUnregistered(f'{name!r} is not a registered state file (asf.state.registry)')
    return spec


def _product_name(product):
    return getattr(product, 'name', None) or product or env.default_product_name()


def path(product, name):
    """The file ``name`` stands for: ``<ASF_HOME>/state/<product>/<name>``, or
    ``<ASF_HOME>/state/<name>`` for a shared name. The directory is created; the file is not."""
    spec = _spec(name)
    if spec.shared:
        root = os.path.join(env.ASF_HOME, 'state')
        os.makedirs(root, exist_ok=True)
        return os.path.join(root, name)
    return os.path.join(env.state_dir(_product_name(product)), name)


# ---- the lock -----------------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(target, timeout_s=TIMEOUT_S):
    """``flock`` on ``target + '.lock'``, polled until ``timeout_s`` — then :class:`StoreBusy`."""
    lock = target + LOCK_SUFFIX
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else TIMEOUT_S)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    line = f'STATE BUSY: {lock} held past {timeout_s}s'
                    _warn(line)
                    raise StoreBusy(line) from None
                time.sleep(POLL_S)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ---- writing ------------------------------------------------------------------------------------

def _atomic_write_bytes(target, payload, mode):
    directory = os.path.dirname(os.path.abspath(target))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f'.{os.path.basename(target)}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _dumps(data, indent):
    return (json.dumps(data, indent=indent) + '\n').encode('utf-8')


def atomic_write_json(target, data, *, mode=0o644, indent=2):
    """``data`` as json at ``target`` in one ``os.replace``: a reader sees the old file or the new
    one; a crash before the replace leaves the old one and no temp file. No lock — the callers
    that read-modify-write go through :func:`update`."""
    _atomic_write_bytes(target, _dumps(data, indent), mode)


def _version(target):
    try:
        return os.stat(target).st_mtime_ns
    except FileNotFoundError:
        return 0


def _stamp_after(target, previous):
    """Keep the version strictly increasing: a write in the same mtime tick as the last one is
    stamped one nanosecond on."""
    current = _version(target)
    if current <= previous:
        current = previous + 1
        os.utime(target, ns=(current, current))
    return current


# ---- reading ------------------------------------------------------------------------------------

def _parse(target, kind):
    """``(data, why)`` of an existing file; ``why`` is '' when it parsed."""
    with open(target, 'rb') as f:
        raw = f.read()
    if kind == registry.JSONL:
        records, bad = [], 0
        for line in raw.decode('utf-8', 'replace').splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                bad += 1
        if bad:  # a torn line is a skipped line, not a lost file
            _warn(f'STATE: {target}: {bad} unparseable line(s) skipped')
        return records, ''
    if kind == registry.TEXT:
        return raw.decode('utf-8', 'replace'), ''
    try:
        return json.loads(raw.decode('utf-8')), ''
    except (ValueError, UnicodeDecodeError) as e:
        return None, (str(e) or type(e).__name__).splitlines()[0]


def _read_at(target, kind, default):
    version = _version(target)
    if not version and not os.path.exists(target):
        return Read(copy.deepcopy(default), 0, ABSENT), ''
    try:
        data, why = _parse(target, kind)
    except FileNotFoundError:
        return Read(copy.deepcopy(default), 0, ABSENT), ''
    except OSError as e:
        data, why = None, str(e)
    if why:
        return Read(copy.deepcopy(default), version, CORRUPT), why
    return Read(data, version, OK), ''


def read(product, name, default=None):
    """``Read(data, version, state)``, lock-free. ``absent`` → ``(default, 0, 'absent')``;
    ``corrupt`` → ``(default, <its version>, 'corrupt')`` and one ``STATE CORRUPT`` line on
    stderr. A jsonl file reads as its list of records (a torn line is skipped, said once)."""
    spec = _spec(name)
    if spec.kind in (registry.STAMP, registry.LOCK):
        raise StoreError(f'{name}: a {spec.kind} file has no content to read')
    target = path(product, name)
    got, why = _read_at(target, spec.kind, default)
    if got.state == CORRUPT:
        _warn(f'STATE CORRUPT: {target}: {why} — read as the default; an update will refuse it')
    return got


# ---- quarantine ---------------------------------------------------------------------------------

def _quarantine(target):
    """Copy a corrupt ``target`` to ``<target>.corrupt-<utc ts>`` — once per distinct content:
    a file every tick finds corrupt is copied once, not once a tick."""
    with open(target, 'rb') as f:
        raw = f.read()
    for old in sorted(glob.glob(glob.escape(target) + CORRUPT_INFIX + '*')):
        try:
            with open(old, 'rb') as f:
                if f.read() == raw:
                    return old
        except OSError:
            continue
    ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    dest = f'{target}{CORRUPT_INFIX}{ts}'
    shutil.copy2(target, dest)
    return dest


# ---- shared files -------------------------------------------------------------------------------

def _flag_on(value):
    return value is True or str(value).strip().lower() in ('on', 'true', 'yes', '1')


def shared_ready():
    """``(ready, laggards)``: every product under ``<ASF_HOME>/products/`` sets
    ``flags.store_shared: on``. A product whose file will not load is a laggard (its ticks may
    still be writing with their own code); no product at all is not ready."""
    pdir = os.path.join(env.ASF_HOME, 'products')
    try:
        names = sorted(f[:-len('.yaml')] for f in os.listdir(pdir) if f.endswith('.yaml'))
    except OSError:
        names = []
    laggards = []
    for name in names:
        try:
            on = _flag_on(env.load_product(name).flag(SHARED_FLAG, 'off'))
        except Exception:  # noqa: BLE001 — an unreadable product is one we cannot vouch for
            on = False
        if not on:
            laggards.append(name)
    return bool(names) and not laggards, laggards


def _mutable(name, spec):
    if not spec.shared:
        return
    ready, laggards = shared_ready()
    if not ready:
        who = ', '.join(laggards) or 'no product registered'
        raise StoreRefused(f'{name} is shared by every product; the store writes it only once all '
                           f'set flags.{SHARED_FLAG}: on (not yet: {who})')


# ---- the schema sidecar -------------------------------------------------------------------------

def _note_schema(target, name, schema):
    side = os.path.join(os.path.dirname(target), registry.SCHEMAS)
    try:
        with open(side, encoding='utf-8') as f:
            current = json.load(f)
    except (OSError, ValueError):
        current = None
    if isinstance(current, dict) and current.get(name) == schema:
        return
    with _locked(side):
        try:
            with open(side, encoding='utf-8') as f:
                current = json.load(f)
        except (OSError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current[name] = schema
        atomic_write_json(side, current)


def schema(product, name):
    """The registry schema ``name`` was last written with by the store, or ``None``."""
    target = path(product, name)
    try:
        with open(os.path.join(os.path.dirname(target), registry.SCHEMAS), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data.get(name) if isinstance(data, dict) else None


# ---- mutations ----------------------------------------------------------------------------------

def _json_spec(name, verb):
    spec = _spec(name)
    if spec.kind != registry.JSON:
        raise StoreError(f'{name}: {verb} takes a json file, this one is {spec.kind}')
    return spec


def write(product, name, data, *, expect=None, timeout_s=TIMEOUT_S, mode=0o644):
    """Replace ``name`` with ``data`` under its lock; the new version. ``expect`` (a version from
    :func:`read`, 0 for "absent") that is not the one on disk raises :class:`StoreConflict`. A
    corrupt file being replaced is copied aside first."""
    return write_at(path(product, name), data, expect=expect, timeout_s=timeout_s, mode=mode)


def update(product, name, fn, *, default=None, timeout_s=TIMEOUT_S, mode=0o644):
    """Read-modify-write under the lock; returns what was written. ``fn(data)`` returns the new
    data — or ``None``, and then ``data`` (mutated in place) is written. An absent file starts
    from a copy of ``default``; a corrupt one raises :class:`StoreCorrupt` after a copy, and is
    left as it is."""
    return update_at(path(product, name), fn, default=default, timeout_s=timeout_s, mode=mode)


def _registered_target(target, verb):
    """``(target, name, spec)`` for an explicit path whose file name is a registered json name.
    A shared name is refused as :func:`_mutable` refuses it."""
    target = os.path.abspath(target)
    name = os.path.basename(target)
    spec = _json_spec(name, verb)
    _mutable(name, spec)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    return target, name, spec


def write_at(target, data, *, expect=None, timeout_s=TIMEOUT_S, mode=0o644):
    """:func:`write` for a caller that holds the file's path rather than its product (a module
    handed a state directory): the file name must be a registered name, and everything else —
    the lock, ``expect``, the corrupt copy, the version — is :func:`write`'s."""
    target, name, spec = _registered_target(target, 'write')
    with _locked(target, timeout_s):
        previous = _version(target)
        if expect is not None and expect != previous:
            raise StoreConflict(f'{target}: version {previous}, expected {expect}')
        if previous and _read_at(target, spec.kind, None)[0].state == CORRUPT:
            _warn(f'STATE CORRUPT: {target} replaced; a copy is at {_quarantine(target)}')
        atomic_write_json(target, data, mode=mode)
        version = _stamp_after(target, previous)
    _note_schema(target, name, spec.schema)
    return version


def update_at(target, fn, *, default=None, timeout_s=TIMEOUT_S, mode=0o644):
    """:func:`update` for a caller that holds the file's path rather than its product: the file
    name must be a registered name; the lock, the corrupt refusal and the version are
    :func:`update`'s."""
    target, name, spec = _registered_target(target, 'update')
    with _locked(target, timeout_s):
        got, why = _read_at(target, spec.kind, default)
        if got.state == CORRUPT:
            dest = _quarantine(target)
            _warn(f'STATE CORRUPT: {target}: {why} — update refused; a copy is at {dest}')
            raise StoreCorrupt(target, dest, why)
        data = got.data
        result = fn(data)
        new = data if result is None else result
        atomic_write_json(target, new, mode=mode)
        _stamp_after(target, got.version)
    _note_schema(target, name, spec.schema)
    return new


def append(product, name, record, *, timeout_s=TIMEOUT_S):
    """One json line onto the jsonl file ``name``: ``O_APPEND``, one ``write``, under the lock. A
    file whose last line was torn (no newline) gets one first, so the torn line stays one bad
    line and this record stays whole."""
    spec = _spec(name)
    if spec.kind != registry.JSONL:
        raise StoreError(f'{name}: append takes a jsonl file, this one is {spec.kind}')
    _mutable(name, spec)
    target = path(product, name)
    line = (json.dumps(record, sort_keys=True) + '\n').encode('utf-8')
    with _locked(target, timeout_s):
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            size = os.fstat(fd).st_size
            if size:
                with open(target, 'rb') as f:
                    f.seek(size - 1)
                    if f.read(1) != b'\n':
                        line = b'\n' + line
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    _note_schema(target, name, spec.schema)


# ---- the reaper ---------------------------------------------------------------------------------

def _why_reap(name, full, now):
    if name == registry.SCHEMAS or name.startswith('.') and not name.endswith('.tmp'):
        return None
    if name.startswith('.') and name.endswith('.tmp'):  # a writer's temp file a crash left
        return 'temp file' if now - os.path.getmtime(full) > 86400 else None
    spec = registry.spec(name)
    if spec is None:
        return 'unregistered'
    if spec.shared:
        return 'shared name in a product directory'
    if spec.ttl_days is not None and now - os.path.getmtime(full) > spec.ttl_days * 86400:
        return f'expired (ttl {spec.ttl_days} d)'
    return None


def reap(product, *, apply=False, now=None, out=None):
    """``[(name, why)]`` for every top-level file in the product's state directory the registry
    does not name, or names with a TTL it outlived, plus a writer's temp file older than a day.
    Directories are their owners' (worktrees, the record clone, briefs) and are never touched.
    With ``apply`` each is moved to ``.trash/<YYYY-MM-DD>/`` and trash older than
    :data:`registry.TRASH_DAYS` is purged (``('.trash/<day>', 'purged')``)."""
    now = time.time() if now is None else now
    root = env.state_dir(_product_name(product))
    found = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        why = _why_reap(name, full, now)
        if why:
            found.append((name, why))
    if not apply:
        return found
    day = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime('%Y-%m-%d')
    trash = os.path.join(root, registry.TRASH, day)
    for name, why in found:
        os.makedirs(trash, exist_ok=True)
        dest, n = os.path.join(trash, name), 1
        while os.path.exists(dest):
            dest, n = os.path.join(trash, f'{name}.{n}'), n + 1
        os.replace(os.path.join(root, name), dest)
        if out:
            out(f'reap: {name} → {registry.TRASH}/{day}/ ({why})')
    cutoff = now - registry.TRASH_DAYS * 86400
    tdir = os.path.join(root, registry.TRASH)
    for entry in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else ():
        try:
            when = datetime.datetime.strptime(entry, '%Y-%m-%d').replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
        if when < cutoff:
            shutil.rmtree(os.path.join(tdir, entry), ignore_errors=True)
            found.append((f'{registry.TRASH}/{entry}', 'purged'))
    return found
