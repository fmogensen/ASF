"""asf.ci_heartbeat — the CI box list's one source, and the doctor's heartbeat row (B-0178).

The boxes a product's self-hosted runners live on are its ``ci.pool`` (each runner's ``box:``;
``ci.pool: discover`` reads the census). Nothing else keeps a copy: a heartbeat watchdog and its
fleet installer — operator tooling, outside this package — read the list from
``asf ci boxes --product <p>`` (one ``box=target`` line per box), and ``--missing`` names only the
boxes with no fresh heartbeat, so an installer targets exactly those. On 2026-10-06 the
installer's hand-kept copy omitted a box rebuilt two weeks earlier: it ran with no heartbeat
until the watchdog's alarm was read by hand.

A box's ssh target is the operator config's ``ci_heartbeat.targets.<box>``, else the box name
itself (an ssh alias) — no host or address ever lives in this package or a product file.

Freshness is read off one file the watchdog writes (``ci_heartbeat.seen_file``, default
``<ASF home>/state/ci-heartbeat/seen.json``): ``{box: epoch}`` (or ``{box: {"at": epoch}}``),
the newest heartbeat it read from each box. The doctor's ``ci heartbeat`` section is red for a
pool box with none newer than ``ci_heartbeat.stale_min`` (default :data:`DEFAULT_STALE_MIN`)
minutes. No pool, or no seen file (no watchdog runs — a host-run CI, a minimal product): no row.
"""
import json
import os
import time

from asf import env

#: ``ci_heartbeat.stale_min`` when the operator config leaves it out
DEFAULT_STALE_MIN = 10
#: the seen file under the ASF home when ``ci_heartbeat.seen_file`` is unset
SEEN_FILE = os.path.join('state', 'ci-heartbeat', 'seen.json')


def _now():
    return time.time()


def _cfg(cfg):
    if cfg is not None:
        return cfg
    try:
        return env.load_config() or {}
    except Exception:  # noqa: BLE001 — a config problem leaves the defaults in force
        return {}


def _block(cfg):
    b = (cfg or {}).get('ci_heartbeat')
    return b if isinstance(b, dict) else {}


def boxes(product):
    """The product's CI boxes, sorted, each once: every ``box:`` of its ``ci.pool``."""
    from asf import ci_pool
    try:
        pool = ci_pool.load_pool(product)
    except Exception:  # noqa: BLE001 — an unreadable pool is no box list
        return []
    return sorted({e.box for e in pool if e.box})


def target(box, cfg=None):
    """The ssh target of ``box``: ``ci_heartbeat.targets.<box>``, else the box name."""
    targets = _block(_cfg(cfg)).get('targets')
    got = targets.get(box) if isinstance(targets, dict) else None
    return str(got) if got else box


def stale_min(cfg=None):
    v = _block(_cfg(cfg)).get('stale_min')
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 \
        else DEFAULT_STALE_MIN


def seen_path(cfg=None):
    p = _block(_cfg(cfg)).get('seen_file')
    return os.path.expanduser(str(p)) if p else os.path.join(env.ASF_HOME, SEEN_FILE)


def read_seen(cfg=None):
    """``{box: epoch}`` off the seen file, or None when there is none or it is unreadable."""
    try:
        with open(seen_path(cfg), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for box, v in data.items():
        at = v.get('at') if isinstance(v, dict) else v
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            out[str(box)] = float(at)
    return out


def ages(product, now=None, cfg=None):
    """``[(box, minutes since its newest heartbeat or None)]`` for every pool box; None when no
    seen file exists (no watchdog to read)."""
    cfg = _cfg(cfg)
    seen = read_seen(cfg)
    if seen is None:
        return None
    now = now if now is not None else _now()
    return [(b, (now - seen[b]) / 60 if b in seen else None) for b in boxes(product)]


def missing(product, now=None, cfg=None):
    """The pool boxes with no heartbeat newer than :func:`stale_min` minutes — every box when
    no seen file exists (nothing has been read from any of them)."""
    cfg = _cfg(cfg)
    got = ages(product, now, cfg)
    if got is None:
        return boxes(product)
    limit = stale_min(cfg)
    return [b for b, age in got if age is None or age > limit]


def doctor_rows(product, now=None, cfg=None):
    """``[(required, ok, detail)]``: one red row per pool box with no heartbeat for over
    :func:`stale_min` minutes, else one ok row; ``[]`` with no pool or no seen file."""
    cfg = _cfg(cfg)
    got = ages(product, now, cfg)
    if not got:
        return []
    limit = stale_min(cfg)
    red = []
    for box, age in got:
        if age is None:
            red.append((True, False, f'{box}: no heartbeat ever read — install the heartbeat '
                                     f'there (asf ci boxes --missing)'))
        elif age > limit:
            red.append((True, False, f'{box}: no heartbeat for {int(age)} min (limit {limit:g})'))
    return red or [(True, True, f'{len(got)} box(es) of ci.pool, each with a heartbeat in the '
                                f'last {limit:g} min')]


def cmd_boxes(args):
    """``asf ci boxes``: the pool's boxes, one ``box=target`` line each (``--json``: a list of
    ``{box, target, age_min}``); ``--missing``: only those with no fresh heartbeat."""
    product = env.load_product(args.product)
    cfg = _cfg(None)
    now = _now()
    names = missing(product, now, cfg) if getattr(args, 'missing', False) else boxes(product)
    if getattr(args, 'json', False):
        age = dict(ages(product, now, cfg) or ())
        print(json.dumps([{'box': b, 'target': target(b, cfg),
                           'age_min': None if age.get(b) is None else round(age[b], 1)}
                          for b in names]))
        return 0
    for b in names:
        print(f'{b}={target(b, cfg)}')
    return 0


def register(sub):
    p = sub.add_parser('boxes', help="the CI boxes of ci.pool, one box=target line each — the "
                                     "one list a heartbeat watchdog and installer read")
    env.add_product_arg(p)
    p.add_argument('--missing', action='store_true',
                   help='only the boxes with no heartbeat in the last ci_heartbeat.stale_min min')
    p.add_argument('--json', action='store_true', help='a list of {box, target, age_min}')
    p.set_defaults(run=cmd_boxes)
    return p
