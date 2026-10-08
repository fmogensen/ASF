"""asf.schema — the record's schema version: the check every writing command makes first, and
``asf schema-migrate`` (the SCHEMA migration; not ``asf migrate``, which adopts items from a
product repo — see ``asf.tick.migrate``).

Two numbers must each agree with their own file before anything writes (D1 — one key name,
``schema_version``, two numbers, each compared against its own file: a config-field rename must
not cost the 500-card commit a record bump costs):

- the package's :data:`SCHEMA_VERSION` against the record's ``index.json['schema_version']``
  (read in the product's backlog checkout and, when it exists, the tick's record clone
  ``~/.ASF/state/<p>/record/``; an unstamped record counts as 0, a missing one has no opinion);
- the package's :data:`CONFIG_VERSION` against ``config.yaml``'s and every
  ``products/<p>.yaml``'s own ``schema_version`` (absent counts as 0 for :func:`pending` — no
  install in the field has ever been stamped — but stays "no opinion" for :func:`require`, so
  landing this never refuses an install that has not been touched yet).

:func:`require` is what a writing command calls first: on a **non-additive** gap (or a file
stamped newer than the package) it prints ``NEEDS OPERATOR: run asf schema-migrate — <detail>``
and exits 3 without writing. An additive gap — a package at ``n`` that still reads and writes a
file at ``n-1`` — never refuses; it is what :func:`auto_migrate` applies by itself, from the tick
or the upgrade, drained to a bound. Reads never check.

Migrations are forward-only, numbered by the version they produce (``MIGRATIONS[n]``/
``CONFIG_MIGRATIONS[n]`` takes a record (or the config files) from ``n-1`` to ``n``), idempotent,
and each is its own commit ``migrate: schema a → b`` in the record clone, pushed. A **non-additive**
migration bumps the package's minor version and is snapshotted first (:func:`snapshot`); its
inverse is :func:`restore`.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import NamedTuple

from asf import env

SCHEMA_VERSION = 1
#: ``config.yaml``'s and every ``products/<p>.yaml``'s own number — compared against its own
#: file, never against :data:`SCHEMA_VERSION` (D1).
CONFIG_VERSION = 1
EXIT_MISMATCH = 3
DRAIN_POLL_S = 30
DRAIN_TIMEOUT_S = 3600
#: The tick's own drain bound for a migration it runs itself (D4) — overridable as
#: ``upgrade.migrate_drain_s`` (:func:`tick_drain_s`), the operator-facing key; this is only the
#: default.
TICK_DRAIN_S = 120


def tick_drain_s(cfg=None):
    """``upgrade.migrate_drain_s`` from the operator config: a non-negative number, else
    :data:`TICK_DRAIN_S` — the shape ``asf.upgrade._config_number`` already uses (schema.py
    cannot import upgrade: upgrade imports schema)."""
    if cfg is None:
        try:
            cfg = env.load_config()
        except Exception:  # noqa: BLE001 — a config problem leaves the default in force
            cfg = {}
    block = (cfg or {}).get('upgrade')
    value = block.get('migrate_drain_s') if isinstance(block, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        value = TICK_DRAIN_S
    return value


class Migration(NamedTuple):
    """One step of either table. ``apply`` is the callable (a record dir, or the list of
    :func:`config_files`, depending on the table); ``additive`` — a package at ``n`` still reads
    and writes a file at ``n-1`` — decides whether :func:`auto_migrate` may run it unattended;
    ``note`` is one changelog/tick-log line. ``renames``/``removes`` are the config table's own:
    a ``CONFIG_MIGRATIONS`` entry declares the keys it renames (still read under the old name —
    :mod:`asf.env`'s deprecated reader) and the keys a later, non-additive entry removes."""
    apply: object
    additive: bool
    note: str
    renames: dict = {}
    removes: tuple = ()


def _migrate_to_1(record_dir):
    """0 → 1: the first stamped schema. Nothing else in the record changes; the stamp (on
    ``index.json`` and on every card, see :func:`stamp_cards`) is the change."""


def _config_to_1(paths):
    """0 → 1: the first stamped config. Nothing in the files changes — the stamp (written by the
    caller, :func:`migrate_config`) is the whole migration. The renames this version declares
    (``feeder.capacity``, ``worker_pool.reserve_for_s1``) make the old keys keep reading through
    :mod:`asf.env`'s deprecated reader; they are not rewritten, so an operator's comments and
    ``sample/config.yaml``'s own load-bearing shape stay byte for byte (PD8) — a renamed field
    keeps being read for at least one minor version, which is the promise ``removes`` keeps."""


# target version -> Migration. A new schema adds one entry and bumps SCHEMA_VERSION.
MIGRATIONS = {1: Migration(_migrate_to_1, additive=True, note='the first stamped schema')}

# target version -> Migration, for config.yaml and every products/<p>.yaml.
CONFIG_MIGRATIONS = {
    1: Migration(_config_to_1, additive=True, note='the first stamped config',
                renames={'feeder.capacity': 'capacity.per_product.sessions',
                         'worker_pool.reserve_for_s1': 'capacity.reserve_for_s1'},
                removes=()),
}


# ---- reading the numbers -----------------------------------------------------

def _product(product):
    return product if isinstance(product, env.Product) else env.load_product(product)


def record_version(backlog_dir):
    """``index.json['schema_version']`` in ``backlog_dir``; 0 when unstamped, None when the dir
    has no ``index.json`` at all."""
    path = os.path.join(backlog_dir, 'index.json')
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return 0
    return int(data.get('schema_version') or 0)


def _file_version(path):
    """``path``'s own ``schema_version`` (``config.yaml`` or a ``products/<p>.yaml``), the same
    tolerant reader :mod:`asf.env` already loads the file with; None when absent (no opinion —
    PD7) whether the file is missing or simply carries no such key."""
    try:
        v = (env.load_file(path) or {}).get('schema_version')
    except env.ConfigError:
        return None
    return int(v) if v is not None else None


def config_version():
    v = _file_version(env.config_path())
    return v


def product_version(name):
    """``products/<name>.yaml``'s own ``schema_version``, read the same way as
    :func:`config_version`."""
    return _file_version(env.product_path(name))


def config_files():
    """Every file a config migration touches: ``config.yaml`` plus every ``products/*.yaml`` —
    the set :func:`migrate_config` stamps and :func:`snapshot` copies."""
    pdir = os.path.join(env.ASF_HOME, 'products')
    products = sorted(glob.glob(os.path.join(pdir, '*.yaml'))) if os.path.isdir(pdir) else []
    return [env.config_path()] + products


def record_dirs(product):
    """``(label, dir)`` for every copy of the record a write could land in."""
    out = []
    if product.backlog_dir:
        out.append(('backlog', product.backlog_dir))
    clone = os.path.join(env.ASF_HOME, 'state', product.name, 'record')
    if os.path.isdir(os.path.join(clone, '.git')):
        out.append(('record clone', clone))
    return out


def _record_stamps(product):
    """Every :func:`record_dirs` entry's own stamp — a dir whose ``index.json`` does not exist
    at all (never yet indexed) answers no opinion and is left out, never coerced to 0: there is
    nothing yet to migrate. An existing-but-unstamped file is 0 (``record_version`` already
    answers that, PD7)."""
    return [v for v in (record_version(d) for _, d in record_dirs(product)) if v is not None]


def _config_stamps():
    """Every :func:`config_files` entry's own stamp, absent counting as 0 (PD7: no install in
    the field has ever been stamped, and that must not read as nothing pending)."""
    return [_file_version(p) or 0 for p in config_files()]


def _pending_half(table, target, stamps):
    """``[(a, b, Migration or None)]`` from the lowest of ``stamps`` up to ``target``; ``[]``
    when there is nothing to compare (no stamp at all) or the lowest already meets it. A ``None``
    migration is a version nobody defined — never additive, always :func:`blocking`."""
    if not stamps:
        return []
    current = min(stamps)
    if current >= target:
        return []
    return [(v - 1, v, table.get(v)) for v in range(current + 1, target + 1)]


def pending(product):
    """``{'record': [(a, b, Migration)], 'config': [(a, b, Migration)]}``: the steps standing
    between each file's stamp and the package's own number (:data:`SCHEMA_VERSION`,
    :data:`CONFIG_VERSION`) — the record's taken as the lowest stamp over :func:`record_dirs`,
    the config's as the lowest over :func:`config_files`. A stamp *greater* than the package's
    is never in here — that is forward-only, and :func:`migrate_dir`'s own refusal (unchanged);
    see :func:`blocking` for that case instead."""
    product = _product(product)
    return {'record': _pending_half(MIGRATIONS, SCHEMA_VERSION, _record_stamps(product)),
            'config': _pending_half(CONFIG_MIGRATIONS, CONFIG_VERSION, _config_stamps())}


def blocking(product):
    """The entries of :func:`pending` that refuse a write: ``(kind, 'newer', target, v)`` — a
    file stamped newer than the package, forward-only — or ``(kind, 'gap', a, b, Migration)`` —
    a step whose migration is undefined or declares ``additive=False``."""
    product = _product(product)
    out = []
    for kind, stamps, table, target in (
            ('record', _record_stamps(product), MIGRATIONS, SCHEMA_VERSION),
            ('config', _config_stamps(), CONFIG_MIGRATIONS, CONFIG_VERSION)):
        for v in stamps:
            if v > target:
                out.append((kind, 'newer', target, v))
        for a, b, m in _pending_half(table, target, stamps):
            if m is None or not m.additive:
                out.append((kind, 'gap', a, b, m))
    return out


def check(product):
    """``(ok, detail)``: package vs record vs config. ``detail`` names what differs."""
    product = _product(product)
    diffs = []
    for label, d in record_dirs(product):
        v = record_version(d)
        if v is not None and v != SCHEMA_VERSION:
            diffs.append(f'{product.name} {label} at schema {v}')
    cfg = config_version()
    if cfg is not None and cfg != SCHEMA_VERSION:
        diffs.append(f'config.yaml at schema {cfg}')
    if not diffs:
        return True, f'schema {SCHEMA_VERSION}'
    return False, f"package at schema {SCHEMA_VERSION}, {', '.join(diffs)}"


def require(product):
    """Call first in every writing command: exit 3 with the operator line on a non-additive
    mismatch (:func:`blocking`). An additive gap, or a file simply not yet stamped, never
    refuses (PD7). A file stamped *newer* than the package carries :func:`migrate_dir`'s own
    forward-only wording; anything else carries :func:`check`'s detail."""
    product = _product(product)
    block = blocking(product)
    if block:
        newer = next((b for b in block if b[1] == 'newer'), None)
        if newer:
            kind, _tag, target, v = newer
            detail = (f'{kind} at schema {v} is newer than this package ({target}) — '
                      'upgrade asf; migrations are forward-only')
        else:
            _ok, detail = check(product)
        print(f'NEEDS OPERATOR: run asf schema-migrate — {detail}', file=sys.stderr)
        raise SystemExit(EXIT_MISMATCH)
    return True


# ---- sessions in flight -----------------------------------------------------

def sessions_in_flight(product_name, alive=None):
    """The job names of the runs in ``state/<p>/sessions.jsonl`` still in flight — read through
    the registry's own fold (:mod:`asf.workers.lifecycle`, by job, per run), never a parser of its
    own: the tick closes a run with a line keyed by ``job``, and a correction line carries no pid.
    Only a job's latest run counts — every later line folds into it, so an earlier run of the
    same job never sees its ``ended``.

    A run is in flight only while it holds a seat (:func:`lifecycle.occupies`: no ``ended`` line
    and a pid that still answers) and its job log has no ok result yet — a dead pid, or a
    session whose result says success, is finished whatever health has not yet written."""
    from asf.workers import lifecycle, runtime
    path = os.path.join(env.ASF_HOME, 'state', product_name, 'sessions.jsonl')
    busy = []
    for job, run in lifecycle.latest(path).items():
        if not lifecycle.occupies(run, alive):
            continue
        if runtime.result_ok(runtime.read_result(run.get('log'))):
            continue
        busy.append(str(job))
    return sorted(busy)


# ---- the migration ------------------------------------------------------------

def _set_config_version(version, path=None):
    """Write ``schema_version: <version>`` into ``path`` (default ``config.yaml``), inserting it
    as the file's **first** key when no such line is there, and leaving every other byte alone —
    no reserialise: these files carry the operator's comments. Returns True when the file
    changed."""
    path = path or env.config_path()
    if not os.path.isfile(path):
        return False
    with open(path, encoding='utf-8') as f:
        text = f.read()
    if re.search(r'(?m)^schema_version:\s*\S+', text):
        new = re.sub(r'(?m)^schema_version:\s*\S+', f'schema_version: {version}', text)
    else:
        new = f'schema_version: {version}\n{text}'
    if new == text:
        return False
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new)
    return True


def _apply(migration, arg):
    """Run a table entry's own callable, whichever shape it carries: a :class:`Migration`'s
    ``apply``, or (a handful of existing tests patch ``MIGRATIONS``/``CONFIG_MIGRATIONS`` with a
    bare callable, the table's pre-F-0114 shape) the callable itself."""
    (migration.apply if isinstance(migration, Migration) else migration)(arg)


def migrate_dir(record_dir, commit=None, target=None):
    """Apply every migration from the record's stamp up to ``target`` (default the package's),
    one commit each via ``commit(message)``. Returns the list of ``(a, b)`` applied."""
    target = SCHEMA_VERSION if target is None else target
    current = record_version(record_dir) or 0
    if current > target:
        raise env.ConfigError(f'record at schema {current} is newer than this package ({target}) — '
                              'upgrade asf; migrations are forward-only')
    applied = []
    for v in range(current + 1, target + 1):
        _apply(MIGRATIONS[v], record_dir)
        stamp_cards(record_dir, v)
        stamp(record_dir, v)
        if commit:
            commit(f'migrate: schema {v - 1} → {v}')
        applied.append((v - 1, v))
    return applied


def stamp(backlog_dir, version):
    """Write ``schema_version`` into ``index.json`` (creating a bare one if absent), keeping every
    other key. Returns True when the file changed."""
    path = os.path.join(backlog_dir, 'index.json')
    data = {'items': {}}
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    if data.get('schema_version') == version:
        return False
    data['schema_version'] = version
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data, indent=2, sort_keys=True) + '\n')
    return True


def stamp_cards(record_dir, version):
    """Bring every card below ``version`` (unstamped counts as 0) up to it, rewriting only its
    machine block. Returns how many cards changed."""
    from asf.record import frontmatter
    from asf.record.core import load_items
    by_id, _errors = load_items(record_dir)
    changed = 0
    for recs in by_id.values():
        for rec in recs:
            _typed, machine = frontmatter.split_machine(rec['meta'])
            if int(machine.get('schema_version') or 0) >= version:
                continue
            # merged, never rebuilt (I1): every other machine line stays byte for byte
            frontmatter.merge_machine(rec['path'], {'schema_version': version},
                                      order=('schema_version',) + tuple(machine))
            changed += 1
    return changed


def migrate_config(target=None):
    """Apply every pending config step across :func:`config_files`, oldest first: snapshot first
    when the step is not additive (D10), run the step's ``apply`` over the whole set (which may
    change nothing in the files — PD8), then stamp every one of them to the step's number.
    Returns the ``(a, b, Migration)`` steps actually applied, oldest first."""
    target = CONFIG_VERSION if target is None else target
    stamps = _config_stamps()
    current = min(stamps) if stamps else target
    applied = []
    for v in range(current + 1, target + 1):
        m = CONFIG_MIGRATIONS.get(v)
        if m is None:
            break
        if not (m.additive if isinstance(m, Migration) else True):
            snapshot(None, v)
        files = config_files()
        _apply(m, files)
        for p in files:
            _set_config_version(v, p)
        applied.append((v - 1, v, m))
        current = v
    return applied


def auto_migrate(product, drain_s=None, out=print, sleep=time.sleep, clock=time.monotonic):
    """Apply every pending step this tick/upgrade can run unattended: drains
    :func:`sessions_in_flight` up to ``drain_s`` (default :func:`tick_drain_s`) before touching
    the record, applies the record's pending steps against the tick's own clone
    (:mod:`asf.tick.shadow`, one commit per step, pushed — a refused push leaves the commit local
    for the next tick, same as :func:`migrate_product`), then the config's pending steps.
    Returns ``(ok, lines)``: ``ok`` is False only when the drain bound passed with sessions still
    in flight, in which case nothing is applied and the caller decides what that means."""
    from asf.tick import shadow
    product = _product(product)
    drain_s = tick_drain_s() if drain_s is None else drain_s
    lines = []
    record_steps = pending(product)['record']
    if record_steps:
        busy = sessions_in_flight(product.name)
        deadline = clock() + drain_s
        while busy and clock() < deadline:
            sleep(DRAIN_POLL_S)
            busy = sessions_in_flight(product.name)
        if busy:
            a, b, _m = record_steps[0]
            return False, [f'schema {a} → {b} still pending — {len(busy)} session(s) in flight']
        clone = shadow.ensure_clone(product, shadow.record_dir(product))
        applied = migrate_dir(clone, commit=lambda msg: shadow.commit_local(clone, msg))
        for a, b in applied:
            lines.append(f'migrate: schema {a} → {b}')
        if applied and not shadow.push(clone):
            lines.append(f"push refused for schema {applied[-1][0]} → {applied[-1][1]} — "
                         "the next tick retries")
    for a, b, m in migrate_config():
        lines.append(f'upgrade: config.yaml schema {a} → {b} — {m.note}')
    return True, lines


def migrate_product(product, drain=False, drain_timeout=DRAIN_TIMEOUT_S, poll=DRAIN_POLL_S,
                    sleep=time.sleep, clock=time.monotonic):
    """Migrate one product's record clone and push. Returns ``(rc, message)``."""
    from asf.tick import shadow
    product = _product(product)
    busy = sessions_in_flight(product.name)
    if busy and not drain:
        return 1, (f'{product.name}: refused — {len(busy)} session(s) in flight ({", ".join(busy)}); '
                   'rerun with --drain to wait for them')
    deadline = clock() + drain_timeout
    while busy:
        if clock() >= deadline:
            return 1, f'{product.name}: gave up after {drain_timeout}s — still in flight: {", ".join(busy)}'
        sleep(poll)
        busy = sessions_in_flight(product.name)
    clone = shadow.ensure_clone(product, shadow.record_dir(product))
    applied = migrate_dir(clone, commit=lambda msg: shadow.commit_local(clone, msg))
    if not applied:
        return 0, f'{product.name}: at schema {SCHEMA_VERSION}, nothing to do'
    pushed = shadow.push(clone)
    steps = ', '.join(f'{a} → {b}' for a, b in applied)
    return 0, f"{product.name}: migrated {steps}{'' if pushed else ' (push refused — the next tick retries)'}"


def _backup_root():
    return os.path.join(env.ASF_HOME, 'state', 'backups')


def snapshot(product, version):
    """Back up before the non-additive step landing at ``version`` (D10): every
    :func:`config_files` entry copied under ``~/.ASF/state/backups/config-<version-1>-<stamp>/``,
    and, when ``product`` is given, its record clone tagged ``asf-schema-<version-1>`` at the
    commit before the step and the tag pushed. Returns the backup directory."""
    stamp_ = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    backup = os.path.join(_backup_root(), f'config-{version - 1}-{stamp_}')
    os.makedirs(backup, exist_ok=True)
    for p in config_files():
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(backup, os.path.basename(p)))
    if product is not None:
        from asf.tick import shadow
        product = _product(product)
        clone = shadow.record_dir(product)
        if os.path.isdir(os.path.join(clone, '.git')):
            tag = f'asf-schema-{version - 1}'
            subprocess.run(['git', 'tag', '-f', tag], cwd=clone, capture_output=True)
            subprocess.run(['git', 'push', '-q', 'origin', tag], cwd=clone, capture_output=True)
    return backup


def snapshots():
    """``(version, stamp, dir)`` for every backup on disk, newest first."""
    root = _backup_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        m = re.match(r'^config-(\d+)-(.+)$', name)
        if m:
            out.append((int(m.group(1)), m.group(2), os.path.join(root, name)))
    return sorted(out, key=lambda t: t[1], reverse=True)


def restore(product, version):
    """The inverse of :func:`snapshot`: the newest backup at or below ``version``, its files
    copied back over the current :func:`config_files`, and, when ``product`` is given and it was
    tagged, its record clone brought to ``asf-schema-<version>`` as a **fresh commit** (never a
    history rewrite or a force-push, so a record with an origin other people pull stays safe to
    restore from any checkout) and pushed. Returns the backup dir used, or None."""
    backup = next((d for v, _stamp, d in snapshots() if v <= version), None)
    if backup:
        names = set(os.listdir(backup))
        for p in config_files():
            if os.path.basename(p) in names:
                shutil.copy2(os.path.join(backup, os.path.basename(p)), p)
    if product is not None:
        from asf.tick import shadow
        product = _product(product)
        clone = shadow.record_dir(product)
        tag = f'asf-schema-{version}'
        if os.path.isdir(os.path.join(clone, '.git')):
            found = subprocess.run(['git', 'rev-parse', '-q', '--verify', f'refs/tags/{tag}'],
                                   cwd=clone, capture_output=True, text=True)
            if found.returncode == 0:
                subprocess.run(['git', 'checkout', '-q', tag, '--', '.'], cwd=clone,
                               capture_output=True)
                shadow.commit_local(clone, f'restore: schema snapshot {tag}')
                shadow.push(clone)
    return backup


def changelog_notes(since_record, since_config):
    """The ``note`` of every ``MIGRATIONS`` entry above ``since_record`` and every
    ``CONFIG_MIGRATIONS`` entry above ``since_config``, in version order; ``[]`` when nothing
    lies between."""
    notes = [MIGRATIONS[v].note for v in sorted(MIGRATIONS) if v > since_record]
    notes += [CONFIG_MIGRATIONS[v].note for v in sorted(CONFIG_MIGRATIONS) if v > since_config]
    return notes


def _all_products():
    d = os.path.join(env.ASF_HOME, 'products')
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith('.yaml'))


def cmd_schema_migrate(args):
    names = _all_products() if args.all else [args.product or env.default_product_name()]
    rc = 0
    for name in names:
        r, msg = migrate_product(name, drain=args.drain, drain_timeout=args.drain_timeout)
        print(f'schema-migrate: {msg}')
        rc = rc or r
    if rc == 0:
        for a, b, _m in migrate_config():
            print(f'schema-migrate: config.yaml schema {a} → {b}')
        if config_version() == CONFIG_VERSION:
            print(f'schema-migrate: config.yaml schema_version → {CONFIG_VERSION}')
    return rc


def register(subparsers):
    p = subparsers.add_parser(
        'schema-migrate',
        help="apply the record's forward-only schema migrations (not `migrate`, the item adopter)")
    g = p.add_mutually_exclusive_group()
    g.add_argument('--product')
    g.add_argument('--all', action='store_true', help='every product under ~/.ASF/products/')
    p.add_argument('--drain', action='store_true', help='wait for in-flight sessions instead of refusing')
    p.add_argument('--drain-timeout', type=int, default=DRAIN_TIMEOUT_S, help='seconds (default 3600)')
    p.set_defaults(run=cmd_schema_migrate)
    return p
