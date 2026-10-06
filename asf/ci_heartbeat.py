"""asf.ci_heartbeat — one source of truth for the boxes a product's ``ci.pool`` declares, and
whether each has beaten recently (B-0178).

The CI box list used to exist in three hand-kept copies that drifted: the product's ``ci.pool``,
the heartbeat watchdog's own BOXES map, and the heartbeat fleet installer's own BOXES line. A box
rebuilt and re-declared in ``ci.pool`` could still be missing from the installer's copy, so it
never got the heartbeat agent installed and ran for weeks with no beat before the watchdog
noticed. Fix: ``ci.pool`` is the one list (:func:`boxes`) — the watchdog and the installer read
it instead of keeping their own, and ``asf ci heartbeat <box>`` is the one write a box's agent
makes, landing in ``<state dir>/ci-heartbeat.json`` (:func:`record`). :func:`stale` is the one
read — no host name or IP is ever hand-kept or stored in this module, only what the operator's
own ``ci.pool`` already declares. :func:`doctor_rows` turns a box silent for more than
:data:`STALE_AFTER_MIN` into a red doctor row naming it; the same list (:func:`stale`) is what an
installer would target to re-install on exactly the boxes missing a beat.
"""
import datetime
import json
import os

from asf import env

HEARTBEAT_FILE = 'ci-heartbeat.json'
#: a box with no beat for longer than this is stale (the card's acceptance: "> 10 min")
STALE_AFTER_MIN = 10
_STAMP_FMT = '%Y-%m-%dT%H:%M:%SZ'


def boxes(pool):
    """Every box :mod:`asf.ci_pool` declares, deduped, in declaration order. A runner declared
    with no ``box`` of its own stands for its own box — the same fallback
    :func:`asf.ci_pool.reserve_plan` uses. This is the one list a watchdog or an installer should
    read instead of hand-keeping their own copy."""
    out = []
    for e in pool:
        b = e.box or e.runner
        if b not in out:
            out.append(b)
    return out


def _path(product):
    return os.path.join(env.state_dir(product), HEARTBEAT_FILE)


def _read(product):
    """``{box: iso stamp}``, or ``{}`` — a missing file or unreadable JSON are the same "no
    beats yet" (never raises)."""
    try:
        with open(_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _now_stamp(when=None):
    when = when or datetime.datetime.now(datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).strftime(_STAMP_FMT)


def record(product, box, when=None):
    """One box's beat: a plain file write to ``<state dir>/ci-heartbeat.json``. This is what
    ``asf ci heartbeat <box>`` runs on the box itself — timer-driven, read-only to the runner
    (never touches it, never waits for idle)."""
    data = _read(product)
    data[box] = _now_stamp(when)
    path = _path(product)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _parse(stamp):
    try:
        t = datetime.datetime.strptime(stamp, _STAMP_FMT)
    except (ValueError, TypeError):
        return None
    return t.replace(tzinfo=datetime.timezone.utc)


def stale(pool, product, now=None):
    """``[(box, last-seen iso stamp or None)]``, sorted by box — every box :func:`boxes` declares
    whose beat is missing or older than :data:`STALE_AFTER_MIN` minutes. A plain file read
    against the one pool list; never calls out."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    beats = _read(product)
    out = []
    for box in boxes(pool):
        stamp = beats.get(box)
        seen = _parse(stamp)
        if seen is None or (now - seen) > datetime.timedelta(minutes=STALE_AFTER_MIN):
            out.append((box, stamp if seen else None))
    return sorted(out)


def doctor_rows(pool, product, now=None):
    """``[(required, ok, detail)]`` for the doctor: one red row per box :func:`stale` finds,
    naming it; one ok row when the pool declares boxes and none is stale. ``[]`` without a pool
    (:mod:`asf.ci_pool.doctor_rows` already returns ``[]`` then, before this is ever called)."""
    if not pool:
        return []
    missing = stale(pool, product, now=now)
    if not missing:
        return [(True, True, f"heartbeat: {len(boxes(pool))} box(es), all beating within "
                             f"{STALE_AFTER_MIN} min")]
    return [(True, False,
             f"heartbeat: {box} has not beaten in over {STALE_AFTER_MIN} min"
             f"{'' if seen is None else f' (last seen {seen})'} — asf ci heartbeat install target")
            for box, seen in missing]


# ---- the commands -----------------------------------------------------------------------------

def cmd_heartbeat(args, out=print):
    """``asf ci heartbeat <box>``: record this box's beat now. The one write a box's agent
    makes; the watchdog and the installer never need their own copy of what a beat looks like."""
    from asf import env as _env
    product = _env.load_product(args.product)
    record(product, args.box)
    out(f"ci heartbeat: {args.box} beat recorded")
    return 0


def cmd_boxes(args, out=print):
    """``asf ci boxes``: every box ci.pool declares, one per line — what the installer targets,
    read off the one source instead of a hand-kept BOXES line."""
    from asf import ci_pool, env as _env
    product = _env.load_product(args.product)
    for box in boxes(ci_pool.load_pool(product)):
        out(box)
    return 0
