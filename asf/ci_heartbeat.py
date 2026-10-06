"""asf.ci_heartbeat — one source of truth for the boxes a product's ``ci.pool`` declares, and
whether each has beaten recently (B-0178).

The CI box list used to exist in three hand-kept copies that drifted: the product's ``ci.pool``,
the heartbeat watchdog's own BOXES map, and the heartbeat fleet installer's own BOXES line. A box
rebuilt and re-declared in ``ci.pool`` could still be missing from the installer's copy, so it
never got the heartbeat agent and ran for weeks with no beat before the watchdog noticed by hand.

Fix: :func:`boxes` is the one list — a watchdog, an installer or ``asf ci doctor`` reads it
instead of keeping a copy; ``asf ci boxes`` is the CLI surface. A box's freshness comes from
either of two beats, read together by :func:`ages`: an external watchdog's own seen file
(``ci_heartbeat.seen_file``, :func:`read_seen` — the box list it already had before this fix,
left running as-is) or ``asf ci heartbeat <box>`` (:func:`record` — a timer + read-only script on
the box itself, run instead once a box is migrated off the watchdog's own polling; never touches
the runner, never waits for idle), landing in ``<state dir>/ci-heartbeat.json``. Whichever beat is
newer wins, so a box moving from one mechanism to the other never reads stale while it still has
a recent beat from either. :func:`doctor_rows` turns a box silent for more than
``ci_heartbeat.stale_min`` (default :data:`DEFAULT_STALE_MIN`) minutes into a red doctor row
naming it; :func:`missing` is the same list an installer would target. No host name or IP is ever
hand-kept or stored in this module, only what the operator's own ``ci.pool`` already declares.
"""
import json
import os
import time

from asf import env

#: ``ci_heartbeat.stale_min`` when the operator config leaves it out
DEFAULT_STALE_MIN = 10
#: the seen file under the ASF home when ``ci_heartbeat.seen_file`` is unset (an external
#: watchdog's own write — this module only reads it)
SEEN_FILE = os.path.join('state', 'ci-heartbeat', 'seen.json')
#: the self-report file ``asf ci heartbeat <box>`` writes, under the product's state dir
HEARTBEAT_FILE = 'ci-heartbeat.json'


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
    """``{box: epoch}`` off the watchdog's seen file, or None when there is none or it is
    unreadable."""
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


def _heartbeat_path(product):
    return os.path.join(env.state_dir(product), HEARTBEAT_FILE)


def _read_heartbeats(product):
    """``{box: epoch}`` off the self-report file ``asf ci heartbeat <box>`` writes, or ``{}``
    — a missing file or unreadable JSON are the same "no self-reported beat yet" (never
    raises)."""
    try:
        with open(_heartbeat_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for box, v in data.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[str(box)] = float(v)
    return out


def record(product, box, when=None):
    """One box's self-reported beat: a plain file write to ``<state dir>/ci-heartbeat.json``.
    This is what ``asf ci heartbeat <box>`` runs on the box itself — timer-driven, read-only to
    the runner (never touches it, never waits for idle)."""
    data = _read_heartbeats(product)
    data[box] = when if when is not None else _now()
    path = _heartbeat_path(product)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def ages(product, now=None, cfg=None):
    """``[(box, minutes since its newest heartbeat or None)]`` for every pool box — the newer of
    the watchdog's seen file and a self-reported beat, box by box. None when neither source has
    anything to say (no watchdog runs and no box has self-reported)."""
    cfg = _cfg(cfg)
    seen = read_seen(cfg)
    reported = _read_heartbeats(product)
    if seen is None and not reported:
        return None
    seen = seen or {}
    now = now if now is not None else _now()
    out = []
    for b in boxes(product):
        at = max((v for v in (seen.get(b), reported.get(b)) if v is not None), default=None)
        out.append((b, (now - at) / 60 if at is not None else None))
    return out


def missing(product, now=None, cfg=None):
    """The pool boxes with no heartbeat newer than :func:`stale_min` minutes — every box when
    neither heartbeat source has anything (nothing has been read from any of them)."""
    cfg = _cfg(cfg)
    got = ages(product, now, cfg)
    if got is None:
        return boxes(product)
    limit = stale_min(cfg)
    return [b for b, age in got if age is None or age > limit]


def doctor_rows(product, now=None, cfg=None):
    """``[(required, ok, detail)]``: one red row per pool box with no heartbeat for over
    :func:`stale_min` minutes, else one ok row; ``[]`` with no pool and no heartbeat source."""
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


def cmd_heartbeat(args):
    """``asf ci heartbeat <box>``: record this box's self-reported beat now."""
    product = env.load_product(args.product)
    record(product, args.box)
    print(f'ci heartbeat: {args.box} beat recorded')
    return 0


def register(sub):
    p = sub.add_parser('boxes', help="the CI boxes of ci.pool, one box=target line each — the "
                                     "one list a heartbeat watchdog and installer read")
    env.add_product_arg(p)
    p.add_argument('--missing', action='store_true',
                   help='only the boxes with no heartbeat in the last ci_heartbeat.stale_min min')
    p.add_argument('--json', action='store_true', help='a list of {box, target, age_min}')
    p.set_defaults(run=cmd_boxes)
    h = sub.add_parser('heartbeat', help="record a box's self-reported beat now (asf ci "
                                        "heartbeat <box>) — the one write a box's own timer "
                                        "makes instead of the watchdog polling it")
    env.add_product_arg(h)
    h.add_argument('box', help='the box name, as declared in ci.pool')
    h.set_defaults(run=cmd_heartbeat)
    return p
