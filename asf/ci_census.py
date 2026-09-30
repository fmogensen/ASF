"""asf.ci_census — ``ci.pool: discover``: the census off the host, the tier off the measure, the
band that stops a flap, the floor that keeps the fast tier full.

The census is the discovered form of :mod:`asf.ci_pool`'s declared pool: one entry per runner the
CI host reports, its role the tier :func:`tier_of` places from :mod:`asf.ci_measure`'s scores. Two
writers only — the tick (:func:`refresh` then :func:`apply_tiers`) and ``asf ci census --apply``
— and every other reader is a plain file read of ``<state dir>/ci-census.json``
(:func:`cached`), never the CI host, never raising.
"""
import collections
import dataclasses
import datetime
import json
import os

from asf import ci_measure, ci_pool, env
from asf.metrics import metrics

#: ``ci.pool: discover`` — the pool is every runner the host reports, not a declared list
CENSUS_FILE = 'ci-census.json'
PLACES_FILE = 'ci-places.jsonl'
CENSUS_VERSION = 1
#: a census older than this turns its doctor row amber — it is never discarded (D2)
CENSUS_STALE_S = 60 * 60
FAST, BULK = ci_pool.ASF_PREFIX + 'fast', ci_pool.ASF_PREFIX + 'bulk'
TIERS = (FAST, BULK)
#: promote at or below, demote at or above; the band between holds the tier already carried (D6)
PROMOTE_AT, DEMOTE_AT = 1.25, 1.60


def _iso(d):
    return d.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_ts(stamp):
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    s = stamp.strip()
    if s[-1:] in ('Z', 'z'):
        s = s[:-1] + '+00:00'
    try:
        t = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc)


def _census_path(product):
    return os.path.join(env.state_dir(product), CENSUS_FILE)


def _places_path(product):
    return os.path.join(env.state_dir(product), PLACES_FILE)


def _read(product):
    """The census file's parsed dict, or ``None`` — a missing file, unreadable JSON or a wrong
    ``v`` are all the same "no census" (D2). Never raises."""
    try:
        with open(_census_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('v') != CENSUS_VERSION:
        return None
    return data


def cached(product):
    """``[PoolEntry]`` from ``<state dir>/ci-census.json``: one entry per runner the last census
    saw, its ``role`` its tier, ``provider``/``box``/``size``/``class`` empty (O2, O5), ``slots``
    1. ``[]`` when there is no census, it is unreadable, or its ``v`` is not
    :data:`CENSUS_VERSION` (D2). Never raises, never calls the host."""
    data = _read(product)
    if not data:
        return []
    out = []
    for r in data.get('runners') or []:
        if not isinstance(r, dict):
            continue
        name, tier = r.get('runner'), r.get('tier')
        if not name or not tier:
            continue
        out.append(ci_pool.PoolEntry(runner=str(name), provider='', role=str(tier)))
    return sorted(out, key=lambda e: e.runner)


def taken_at(product):
    """The census's ISO stamp, or None — the doctor row's age."""
    data = _read(product)
    return data.get('taken') if data else None


def run_wall_p50_min(product):
    """The p50 run wall minutes the last :func:`refresh` computed, or ``None`` — what
    :func:`asf.ci_queue`'s two wait limits pace off (PD7). Never calls the host."""
    data = _read(product)
    if not data:
        return None
    return (data.get('measure') or {}).get('run_wall_p50_min')


def tier_of(score, carried=None):
    """The tier a runner belongs in. No score, no ratio, or fewer than MIN_READINGS: BULK (D7).
    Flaky: BULK (D15). ratio <= PROMOTE_AT: FAST. ratio >= DEMOTE_AT: BULK. Between: ``carried``
    — the tier the last census gave it — else BULK (D6)."""
    if score is None or score.ratio is None or score.n < ci_measure.MIN_READINGS:
        return BULK
    if score.flaky:
        return BULK
    if score.ratio <= PROMOTE_AT:
        return FAST
    if score.ratio >= DEMOTE_AT:
        return BULK
    return carried if carried in TIERS else BULK


@dataclasses.dataclass(frozen=True)
class Move:
    runner: str
    frm: object    # the tier the runner carried, or None — a runner censused for the first time
    to: str
    score: object  # the runner's ci_measure.Score, or None
    floored: bool
    reason: str

    def line(self):
        tiers = f'{self.frm} → {self.to}' if self.frm else f'→ {self.to}'
        return f'ci place: {self.runner} {tiers} — {self.reason}'

    def record(self, now):
        s = self.score
        kind = _worst_kind(s) if self.to == BULK and s and s.ratios else None
        return {
            'ts': _iso(now), 'runner': self.runner, 'from': self.frm, 'to': self.to,
            'ratio': s.ratio if s else None, 'median_s': (s.medians.get(kind) if s and kind else None),
            'kind': kind, 'n': s.n if s else 0,
            'threshold': (PROMOTE_AT if self.to == FAST else DEMOTE_AT if self.frm else None),
            'why': self.reason,
        }


def _worst_kind(score):
    if not score or not score.ratios:
        return None
    return max(score.ratios, key=lambda k: score.ratios[k])


def _provisional_reason(score):
    n = score.n if score else 0
    return f"{n} green reading{'s' if n != 1 else ''}, fewer than {ci_measure.MIN_READINGS}: provisional"


def _flaky_reason(score):
    return f'green rate {score.green_rate} over {score.n} readings — flaky, never {FAST}'


def _promote_reason(score):
    n_kinds = len(score.ratios)
    return (f"{score.ratio:.2f}× over {n_kinds} job kind{'s' if n_kinds != 1 else ''}, "
            f'{score.n} green readings in {ci_measure.WINDOW_DAYS} d')


def _demote_reason(score):
    kind = _worst_kind(score)
    median_s = score.medians[kind]
    ratio = score.ratios[kind]
    return (f'runs {kind} at {median_s:g}s, {ratio:.2g}× the fleet best '
            f'(demote at {DEMOTE_AT:.2f}×), {score.n} green readings in '
            f'{ci_measure.WINDOW_DAYS} d')


def _floor_reason(score):
    if score is None or score.ratio is None:
        return 'the fast tier would be empty; no readings yet'
    return f'the fast tier would be empty; best ratio {score.ratio:.2g}×'


def _reason_for(tier, score, floored):
    if floored:
        return _floor_reason(score)
    if score is None or score.ratio is None or score.n < ci_measure.MIN_READINGS:
        return _provisional_reason(score)
    if score.flaky:
        return _flaky_reason(score)
    if tier == FAST:
        return _promote_reason(score)
    return _demote_reason(score)


def _prev_tiers(product):
    data = _read(product)
    prev = {r['runner']: r.get('tier') for r in (data or {}).get('runners') or []
            if isinstance(r, dict) and r.get('runner')}
    since = {r['runner']: r.get('since') for r in (data or {}).get('runners') or []
             if isinstance(r, dict) and r.get('runner')}
    return prev, since


def _read_events(root, now):
    return (metrics.read_stream(root, 'ci', metrics.days_back(metrics.today(), ci_measure.WINDOW_DAYS))
            if root else [])


def _place(host_runners, prev, sc):
    """``({runner: tier}, {floored runner names})`` — every online runner tiered against
    ``prev``, offline ones carrying their prior tier (or BULK, never tiered), and the fast tier
    floored to the best-ratio online runner when tiering left it empty (D8)."""
    tiers = {}
    for r in host_runners:
        carried = prev.get(r.name)
        tiers[r.name] = tier_of(sc.get(r.name), carried=carried) if r.online else \
            (carried if carried in TIERS else BULK)
    floored = set()
    online_names = {r.name for r in host_runners if r.online}
    if online_names and not any(tiers[n] == FAST for n in tiers):
        best = min(online_names, key=lambda n: sc[n].ratio if sc.get(n) and sc[n].ratio is not None
                   else float('inf'))
        tiers[best] = FAST
        floored.add(best)
    return tiers, floored


def _moves_for(tiers, floored, prev, sc):
    moves = []
    for name in sorted(tiers):
        to, frm = tiers[name], prev.get(name)
        if to == frm:
            continue
        score = sc.get(name)
        moves.append(Move(runner=name, frm=frm, to=to, score=score, floored=name in floored,
                          reason=_reason_for(to, score, name in floored)))
    return moves


def refresh(product, backend, root=None, now=None, out=print):
    """Take a census: read the runners (P2), score them (:mod:`asf.ci_measure` over the ``ci``
    stream in ``root``), tier each one against the tier it already carries, floor the fast tier
    (D8), write the file and the placement history, and return the moves. No workflow file is
    read (D9)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        host_runners = backend.runners()
    except ci_pool.BackendError as e:
        out(f'ci census: not taken — cannot read the CI host ({e})')
        return []

    prev, prev_since = _prev_tiers(product)
    events = _read_events(root, now)
    rdgs = ci_measure.readings(events, now=now)
    sc = ci_measure.scores(rdgs)
    tiers, floored = _place(host_runners, prev, sc)
    moves = _moves_for(tiers, floored, prev, sc)

    runners_doc = []
    for r in sorted(host_runners, key=lambda r: r.name):
        tier = tiers[r.name]
        since = _iso(now) if tier != prev.get(r.name) else prev_since.get(r.name) or _iso(now)
        score = sc.get(r.name)
        runners_doc.append({
            'runner': r.name, 'tier': tier,
            'ratio': score.ratio if score else None, 'n': score.n if score else 0,
            'green_rate': score.green_rate if score else None,
            'flaky': bool(score.flaky) if score else False,
            'since': since,
            'why': _reason_for(tier, score, r.name in floored) if r.online else
                   'not tiered this census — offline',
        })

    doc = {
        'v': CENSUS_VERSION, 'taken': _iso(now), 'window_days': ci_measure.WINDOW_DAYS,
        'runners': runners_doc,
        'measure': {
            'taken': _iso(now),
            'kinds': sorted({k for s in sc.values() for k in s.medians}),
            'run_wall_p50_min': ci_measure.run_wall_p50(events, now=now),
        },
    }
    path = _census_path(product)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    os.replace(tmp, path)

    ci_measure.write_baselines(product, rdgs, tiers, now=now)

    if moves:
        with open(_places_path(product), 'a', encoding='utf-8') as f:
            for mv in moves:
                f.write(json.dumps(mv.record(now), sort_keys=True) + '\n')
        for mv in moves:
            out(mv.line())
    else:
        by_tier = collections.Counter(tiers.values())
        counts = ', '.join(f'{t} {by_tier.get(t, 0)}' for t in TIERS if by_tier.get(t))
        out(f'ci census: {len(tiers)} runners — {counts}')

    return moves


def apply_tiers(product, backend, moves, out=print):
    """Write only the tier labels of the runners in ``moves``: add the new tier, remove the old,
    adds first. Every other label on the runner is untouched (D9). A promotion to :data:`FAST`
    is started as a trial (``ci_pool.start_trial``, D10)."""
    if not moves:
        return
    by_name = {r.name: r for r in backend.runners()}
    for mv in moves:
        r = by_name.get(mv.runner)
        if r is None:
            continue
        current = sorted(r.norm_labels())
        target = (set(current) - {mv.frm} if mv.frm else set(current)) | {mv.to}
        add = [mv.to] if mv.to not in r.norm_labels() else []
        remove = [mv.frm] if mv.frm and mv.frm in r.norm_labels() else []
        step = ci_pool.Step(runner=mv.runner, current=current, target=sorted(target), add=add,
                            remove=remove, blocked=[], trial=(mv.to == FAST),
                            entry=ci_pool.PoolEntry(runner=mv.runner, provider='', role=mv.to),
                            host=r)
        try:
            if step.add:
                backend.add_labels(r, step.add)
            if step.remove:
                backend.remove_label(r, step.remove[0])
        except ci_pool.BackendError as e:
            out(f'ci place: {mv.runner} label write failed — {e}')
            continue
        if step.trial:
            ci_pool.start_trial(product.name, step, now=None)


def census_rows(product, now=None):
    """``[(required, ok, detail)]`` — the doctor's ``ci census`` rows (PD3): required, so a
    discovered product with no census yet is visible on its own, never inside
    :func:`asf.ci_pool.doctor_rows` (which returns ``[]`` with no pool, D2). ``[]`` for a
    product that does not declare ``ci.pool: discover``. No host call."""
    if ci_pool.pool_mode(product) != ci_pool.DISCOVER:
        return []
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = []
    data = _read(product)
    if not data:
        out.append((True, False, 'ci census: no census yet — the next tick takes one'))
    else:
        runners = data.get('runners') or []
        by_tier = collections.Counter(r.get('tier') for r in runners if isinstance(r, dict))
        counts = ', '.join(f'{t} {by_tier.get(t, 0)}' for t in TIERS if by_tier.get(t))
        taken = _parse_ts(data.get('taken'))
        age_min = int((now - taken).total_seconds() // 60) if taken else None
        stale = taken is not None and (now - taken).total_seconds() > CENSUS_STALE_S
        detail = f'ci census: {len(runners)} runners — {counts}'
        detail += f' (taken {age_min} min ago)' if age_min is not None else ''
        out.append((True, not stale, detail))
    for r in ci_pool.load_reserve(product):
        if r.spread_by == 'box':
            out.append((False, True, 'ci census: ci.reserve spread_by: box has no effect on a '
                                     'discovered pool — the host reports no machine '
                                     '(spread_by: none)'))
    return out


def cmd_census(args, out=print):
    """``asf ci census``: the census, the scores and the tier moves it would make; ``--apply``
    takes one and writes it. The stream root is :func:`asf.tick.shadow.record_dir` — a path, no
    ``git`` call — so a dry run costs no more than a file read."""
    product = env.load_product(args.product)
    backend = ci_pool.backend_for(product)
    if backend is None:
        out(f"ci census: no runner backend for ci.provider "
            f"{(product.ci or {}).get('provider')!r} in this release")
        return 2
    from asf.tick import shadow
    root = shadow.record_dir(product.name)
    if getattr(args, 'apply', False):
        moves = refresh(product, backend, root=root, out=out)
        apply_tiers(product, backend, moves, out=out)
        return 0
    try:
        host_runners = backend.runners()
    except ci_pool.BackendError as e:
        out(f'ci census: cannot read the CI host — {e}')
        return 2
    prev, _since = _prev_tiers(product)
    events = _read_events(root, datetime.datetime.now(datetime.timezone.utc))
    sc = ci_measure.scores(ci_measure.readings(events))
    tiers, floored = _place(host_runners, prev, sc)
    moves = {mv.runner: mv for mv in _moves_for(tiers, floored, prev, sc)}
    rows = []
    for name in sorted(tiers):
        score = sc.get(name)
        move = moves[name].line()[len(f'ci place: {name} '):] if name in moves else '—'
        rows.append((name, tiers[name], f'{score.ratio:.2f}' if score and score.ratio is not None else '—',
                    str(score.n) if score else '0',
                    f'{score.green_rate:.2f}' if score else '—', move))
    out(ci_pool._table(('runner', 'tier', 'ratio', 'readings', 'green', 'move'), rows))
    by_tier = collections.Counter(tiers.values())
    counts = ', '.join(f'{t} {by_tier.get(t, 0)}' for t in TIERS if by_tier.get(t))
    out(f'census: {len(tiers)} runners — {counts} (dry run; --apply writes the labels)')
    return 0
