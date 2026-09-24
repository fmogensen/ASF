"""asf.schema — the record's schema version: the check every writing command makes first, and
``asf schema-migrate`` (the SCHEMA migration; not ``asf migrate``, which adopts items from a
product repo — see ``asf.tick.migrate``).

Three numbers must agree before anything writes:

- the package's :data:`SCHEMA_VERSION` (what this install of ``asf`` reads and writes),
- the record's ``index.json['schema_version']`` (what the record was last migrated to) — read in
  the product's backlog checkout and, when it exists, the tick's record clone
  (``~/.ASF/state/<p>/record/``); an unstamped record counts as 0,
- ``config.yaml``'s ``schema_version`` (what the operator config is written for; absent = no
  opinion).

:func:`require` is what a writing command calls first: on a mismatch it prints
``NEEDS OPERATOR: run asf schema-migrate — <detail>`` and exits 3 without writing. Reads never
check.

Migrations are forward-only, numbered by the version they produce (``MIGRATIONS[n]`` takes a
record from ``n-1`` to ``n``), idempotent, and each one is its own commit
``migrate: schema a → b`` in the record clone, pushed. Rollback is reverting that commit and
reinstalling the older package.
"""
import json
import os
import re
import sys
import time

from asf import env

SCHEMA_VERSION = 1
EXIT_MISMATCH = 3
DRAIN_POLL_S = 30
DRAIN_TIMEOUT_S = 3600


def _migrate_to_1(record_dir):
    """0 → 1: the first stamped schema. Nothing else in the record changes; the stamp (on
    ``index.json`` and on every card, see :func:`stamp_cards`) is the change."""


# target version -> fn(record_dir). A new schema adds one entry and bumps SCHEMA_VERSION.
MIGRATIONS = {1: _migrate_to_1}


# ---- reading the three numbers ----------------------------------------------

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
            machine.pop('schema_version', None)
            frontmatter.write_machine(rec['path'], {'schema_version': version, **machine})
            changed += 1
    return changed


def config_version():
    v = env.load_config().get('schema_version')
    return int(v) if v is not None else None


def record_dirs(product):
    """``(label, dir)`` for every copy of the record a write could land in."""
    out = []
    if product.backlog_dir:
        out.append(('backlog', product.backlog_dir))
    clone = os.path.join(env.ASF_HOME, 'state', product.name, 'record')
    if os.path.isdir(os.path.join(clone, '.git')):
        out.append(('record clone', clone))
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
    """Call first in every writing command: exit 3 with the operator line on a mismatch."""
    ok, detail = check(product)
    if not ok:
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


# ---- the migration ----------------------------------------------------------

def _set_config_version(version):
    path = env.config_path()
    if not os.path.isfile(path):
        return False
    with open(path, encoding='utf-8') as f:
        text = f.read()
    new = re.sub(r'(?m)^schema_version:\s*\S+', f'schema_version: {version}', text)
    if new == text:
        return False
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new)
    return True


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
        MIGRATIONS[v](record_dir)
        stamp_cards(record_dir, v)
        stamp(record_dir, v)
        if commit:
            commit(f'migrate: schema {v - 1} → {v}')
        applied.append((v - 1, v))
    return applied


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
    if rc == 0 and _set_config_version(SCHEMA_VERSION):
        print(f'schema-migrate: config.yaml schema_version → {SCHEMA_VERSION}')
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
