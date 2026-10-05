"""asf.tune — the self-tuning loop (v1): per session kind, the model and the seat share, tuned
from measured outcomes within the operator's bounds, one trial at a time, reverted on regress.

Deterministic code, no LLM. Off unless ``config.yaml tune.enabled`` (or
``tune.products.<product>.enabled``) is true: off, the pass does nothing and the placement hook
applies nothing, so a product runs its configured models and seats exactly as before.

**The knobs.** Only a kind named under ``tune.bounds.<kind>`` is tuned, and only within its
bounds: ``models: [<label>, …]`` — the model labels of ``worker_pool.models`` this kind may run,
most capable first, cheapest last — and ``seats: [min, max]``, the concurrent sessions of this
kind. A knob with no bound is never moved. No model name is in this module: a label comes from
the bounds, and :func:`asf.workers.spawn.model_arg` maps it to the runtime's id.

**The signal**, per kind, off the session registry (:func:`asf.improve.measure.ended_runs` —
its runs, their spend and whether they landed) and the scorecard's run classes
(:func:`asf.scorecard.score.is_correction`, :func:`~asf.scorecard.score.is_dead`):

* ``repair`` — repair rounds per landed item: this kind's relaunches (a run after the job's
  first) plus the correction runs on the items it worked, over the items that landed;
* ``fail`` — the share of its runs that died (:func:`asf.scorecard.score.is_dead`);
* ``landed_share`` — the items it worked that landed; ``usd`` and ``minutes`` — its spend and
  wall time; ``objective`` — landed items per unit of spend (USD when every compared run has a
  spend, else per minute).

**The policy** (:func:`step`, run once per wave): a kind with no live trial and at least
``tune.min_samples`` runs since its last change and inside ``tune.window_days`` starts one trial,
the first of: the next cheaper model of its bounds, one seat fewer, one seat more — skipping a
value the window already reverted or moved away from. After ``tune.trial_samples`` runs of the
trial the numbers are compared with the baseline frozen at its start: **keep** when the
objective gained at least ``tune.min_gain`` and repair is at or below the baseline; **revert**
when repair or the failure rate worsened beyond ``tune.max_regress`` (a regression), or when
the gain did not come (``no gain``). A kept change is watched for one more ``trial_samples``
runs and reverted the same way if it regresses then. At most one live trial per kind.

**The guard rails**: never outside the bounds (a value the operator's bounds no longer hold is
dropped by the hook and reverted by the pass); an S1 row is never touched; ``asf tune freeze``
reverts every live trial and starts none until ``asf tune unfreeze``.

**The record**: ``state/<product>/tune.json`` (the knobs, the live trials, freeze) and
``state/<product>/tune.jsonl`` — the tune ledger, one line per change (trial, keep, revert,
freeze, unfreeze) with its reason and numbers; the wave also writes each as a ``tune`` event.
``asf tune history`` renders the ledger, ``asf status`` carries :func:`status_cell`, and
``asf release-readiness`` criterion 11 reads :func:`criterion`.
"""
import datetime
import json

from asf import env

STATE = 'tune.json'
LEDGER = 'tune.jsonl'
DEFAULTS = {'enabled': False, 'window_days': 7, 'min_samples': 10, 'trial_samples': 10,
            'min_gain': 0.10, 'max_regress': 0.10}
_NUMERIC = ('window_days', 'min_samples', 'trial_samples', 'min_gain', 'max_regress')
_STAMP = '%Y-%m-%dT%H:%M:%SZ'
EPS = 1e-9


# ------------------------------------------------------------ settings --

def _truthy(v):
    return v is True or str(v).strip().lower() in ('true', 'yes', 'on', '1')


def _bounds(raw):
    """``{kind: {'models': [label…], 'seats': (min, max) | None}}`` from ``tune.bounds``; a
    misshapen entry bounds nothing."""
    out = {}
    for kind, b in (raw or {}).items() if isinstance(raw, dict) else ():
        if not isinstance(b, dict):
            continue
        models = [str(m).strip() for m in (b.get('models') or []) if str(m).strip()] \
            if isinstance(b.get('models'), list) else []
        seats = None
        s = b.get('seats')
        if isinstance(s, list) and len(s) == 2:
            try:
                lo, hi = int(s[0]), int(s[1])
                if 0 <= lo <= hi:
                    seats = (lo, hi)
            except (TypeError, ValueError):
                pass
        out[str(kind)] = {'models': models, 'seats': seats}
    return out


def settings(cfg, product=None):
    """The tune settings for ``product``: ``config.yaml tune:`` with the product's own
    ``tune.products.<name>:`` over it (its ``bounds`` replace the shared ones kind by kind)."""
    cfg = cfg or {}
    block = cfg.get('tune') if isinstance(cfg.get('tune'), dict) else {}
    name = getattr(product, 'name', product)
    per = ((block.get('products') or {}) if isinstance(block.get('products'), dict) else {}).get(name)
    per = per if isinstance(per, dict) else {}
    out = dict(DEFAULTS)
    for src in (block, per):
        if 'enabled' in src:
            out['enabled'] = _truthy(src.get('enabled'))
        for k in _NUMERIC:
            v = src.get(k)
            try:
                if v is not None and not isinstance(v, bool):
                    out[k] = float(v) if k in ('min_gain', 'max_regress') else int(v)
            except (TypeError, ValueError):
                pass
    bounds = _bounds(block.get('bounds'))
    bounds.update(_bounds(per.get('bounds')))
    out['bounds'] = bounds
    return out


# ------------------------------------------------------------ state and ledger --

def _empty():
    return {'frozen': False, 'kinds': {}}


def load_state(product):
    from asf.state import store
    data = store.read(product, STATE, default=_empty()).data
    return data if isinstance(data, dict) else _empty()


def save_state(product, state):
    from asf.state import store
    store.write(product, STATE, state)


def ledger(product):
    from asf.state import store
    rows = store.read(product, LEDGER, default=[]).data
    return [r for r in rows or [] if isinstance(r, dict)]


def _append(product, rec, event=None):
    from asf.state import store
    store.append(product, LEDGER, rec)
    if event is not None:
        try:
            event('tune', **{('session_kind' if k == 'kind' else k): v for k, v in rec.items()})
        except Exception:  # noqa: BLE001 — the ledger line is the record; the event is a copy
            pass


def _now(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime(_STAMP)


def _since(now, days):
    d = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(days=days)
    return d.strftime(_STAMP)


# ------------------------------------------------------------ the signal --

def _resolve(label, cfg):
    """The ids a label's runs carry in the registry: the label and its runtime id."""
    out = {label}
    try:
        from asf.workers import spawn
        mid = spawn.model_arg(label, cfg)
        if mid:
            out.add(mid)
    except Exception:  # noqa: BLE001 — an unmapped label: only the label itself
        pass
    return out


def stats(kind_runs, all_runs):
    """The numbers over ``kind_runs`` (one kind's runs), corrections read off ``all_runs``."""
    from asf.scorecard import score
    rs = list(kind_runs)
    n = len(rs)
    items = {r.item for r in rs if r.item}
    landed = {r.item for r in rs if r.item and r.landed}
    kinds = {r.kind for r in rs}
    starts = min((r.started for r in rs), default='')
    corr = [r for r in all_runs if r.item in items and r.kind not in kinds
            and score.is_correction(r.kind or '') and r.started >= starts]
    rounds = sum(1 for r in rs if r.attempt >= 2) + len(corr)
    usd = [r.usd for r in rs]
    minutes = sum(r.minutes for r in rs)
    return {
        'n': n,
        'landed': len(landed),
        'repair': round(rounds / max(1, len(landed)), 3),
        'fail': round(sum(1 for r in rs if score.is_dead(r)) / n, 3) if n else 0.0,
        'landed_share': round(len(landed) / len(items), 3) if items else 0.0,
        'usd': None if any(u is None for u in usd) else round(sum(usd), 4),
        'minutes': round(minutes, 1),
        'wall': round(minutes / n, 1) if n else 0.0,
    }


def objective(s, unit):
    """Landed items per unit of spend; a run set that spent nothing is infinite when it landed."""
    cost = s.get('usd') if unit == 'usd' else s.get('minutes')
    if not cost:
        return float('inf') if s.get('landed') else 0.0
    return s['landed'] / cost


def _unit(a, b):
    return 'usd' if a.get('usd') and b.get('usd') else 'minutes'


def _worse(t, b, max_regress):
    """``t`` worse than ``b`` beyond ``max_regress``: relative over a non-zero baseline, the same
    figure as an absolute step over a zero one."""
    if b > 0:
        return t > b * (1 + max_regress) + EPS
    return t > max_regress + EPS


def judge(base, trial, s):
    """``(verdict, reason, gain)``: ``keep``, or ``revert`` with ``regress: …`` / ``no gain``."""
    mr = s['max_regress']
    if _worse(trial['repair'], base['repair'], mr):
        return 'revert', f"regress: repair {trial['repair']:g} vs {base['repair']:g}", None
    if _worse(trial['fail'], base['fail'], mr):
        return 'revert', f"regress: failure rate {trial['fail']:g} vs {base['fail']:g}", None
    unit = _unit(base, trial)
    ob, ot = objective(base, unit), objective(trial, unit)
    if 0 < ob < float('inf'):
        gain = (ot - ob) / ob if ot < float('inf') else 1.0
    else:
        gain = 1.0 if ot > ob else 0.0
    if gain >= s['min_gain'] - EPS and trial['repair'] <= base['repair'] + EPS:
        return 'keep', f"objective {gain:+.0%} per {unit}, repair {trial['repair']:g} vs {base['repair']:g}", gain
    return 'revert', f"no gain: objective {gain:+.0%} per {unit} (needs +{s['min_gain']:.0%})", gain


# ------------------------------------------------------------ the knobs --

def base_model(product, kind):
    """The label a kind runs with no tuning: its brief kind's default (:func:`model_for`)."""
    try:
        from asf.briefs.build import model_for
        return model_for(product, kind)
    except Exception:  # noqa: BLE001
        return None


def current(ks, knob, product, kind, bounds):
    if knob == 'model':
        return ks.get('model') or ks.get('base') or base_model(product, kind)
    seats = ks.get('seats')
    if seats is None and bounds.get('seats'):
        return bounds['seats'][1]
    return seats


def _in_bounds(knob, value, bounds):
    if knob == 'model':
        return value in bounds.get('models', ())
    s = bounds.get('seats')
    return s is not None and value is not None and s[0] <= value <= s[1]


def _blocked(rows, kind, knob, value, since):
    """A value this window already left behind: a trial of it reverted, or a keep away from it."""
    for r in rows:
        if r.get('kind') != kind or r.get('knob') != knob or (r.get('at') or '') < since:
            continue
        if r.get('event') == 'revert' and r.get('from') == value:
            return True
        if r.get('event') == 'keep' and r.get('from') == value:
            return True
    return False


def candidates(ks, product, kind, bounds, rows, since):
    """The knob steps to try, in order: the next cheaper model, one seat fewer, one seat more."""
    out = []
    models = bounds.get('models') or []
    cur = current(ks, 'model', product, kind, bounds)
    if cur in models and models.index(cur) + 1 < len(models):
        out.append(('model', cur, models[models.index(cur) + 1]))
    if bounds.get('seats'):
        lo, hi = bounds['seats']
        s = current(ks, 'seats', product, kind, bounds)
        if s is not None and s - 1 >= lo:
            out.append(('seats', s, s - 1))
        if s is not None and s + 1 <= hi:
            out.append(('seats', s, s + 1))
    return [c for c in out if not _blocked(rows, kind, c[0], c[2], since)]


def _runs_for(runs, kind, knob, value, since, cfg):
    rs = [r for r in runs if r.kind == kind and r.started >= since]
    if knob == 'model' and value:
        ids = _resolve(value, cfg)
        rs = [r for r in rs if r.model in ids]
    return rs


# ------------------------------------------------------------ the pass --

def step(product, cfg=None, *, runs=None, now=None, out=print, event=None):
    """One pass of the loop: every live trial or watch judged, a trial started where none runs.
    Returns the ledger records it wrote. A no-op while ``tune.enabled`` is off or frozen."""
    cfg = env.load_config() if cfg is None else cfg
    s = settings(cfg, product)
    if not s['enabled'] or not s['bounds']:
        return []
    state = load_state(product)
    if state.get('frozen'):
        return []
    stamp = _now(now)
    window = _since(now, s['window_days'])
    if runs is None:
        from asf.improve import measure
        runs = measure.ended_runs(product, since=window[:10])
    rows = ledger(product)
    wrote = []

    def log(kind, ev, knob, frm, to, reason, **nums):
        rec = dict({'at': stamp, 'kind': kind, 'event': ev, 'knob': knob, 'from': frm, 'to': to,
                    'reason': reason}, **nums)
        _append(product, rec, event)
        rows.append(rec)
        wrote.append(rec)
        out(f"tune: {kind} {knob} {frm}→{to} {ev} — {reason}")

    kinds = state.setdefault('kinds', {})
    for kind in sorted(s['bounds']):
        b = s['bounds'][kind]
        ks = kinds.setdefault(kind, {})
        if 'base' not in ks:
            ks['base'] = base_model(product, kind)
        # a tuned value the bounds no longer hold goes back to the kind's own
        for knob in ('model', 'seats'):
            if ks.get(knob) is not None and not _in_bounds(knob, ks[knob], b):
                log(kind, 'revert', knob, ks[knob], None if knob == 'seats' else ks.get('base'),
                    'outside the operator bounds')
                ks.pop(knob, None)
                ks.pop('watch', None)
        trial = ks.get('trial')
        if trial and not _in_bounds(trial['knob'], trial['to'], b):
            log(kind, 'revert', trial['knob'], trial['to'], trial['from'], 'outside the operator bounds')
            ks.pop('trial')
            ks['since'] = stamp
            trial = None
        if trial:
            trs = _runs_for(runs, kind, trial['knob'], trial['to'], trial['started'], cfg)
            if len(trs) < s['trial_samples']:
                continue
            t = stats(trs, runs)
            verdict, reason, _gain = judge(trial['baseline'], t, s)
            ks.pop('trial')
            ks['since'] = stamp
            if verdict == 'keep':
                ks[trial['knob']] = trial['to']
                ks['watch'] = {'knob': trial['knob'], 'from': trial['from'], 'to': trial['to'],
                               'started': stamp, 'baseline': trial['baseline']}
                log(kind, 'keep', trial['knob'], trial['from'], trial['to'], reason,
                    baseline=trial['baseline'], trial=t)
            else:
                log(kind, 'revert', trial['knob'], trial['to'], trial['from'], reason,
                    baseline=trial['baseline'], trial=t, regress=reason.startswith('regress'))
            continue
        watch = ks.get('watch')
        if watch:
            wrs = _runs_for(runs, kind, watch['knob'], watch['to'], watch['started'], cfg)
            if len(wrs) < s['trial_samples']:
                continue
            t = stats(wrs, runs)
            verdict, reason, _gain = judge(watch['baseline'], t, s)
            ks.pop('watch')
            if reason.startswith('regress'):
                ks[watch['knob']] = watch['from']
                ks['since'] = stamp
                log(kind, 'revert', watch['knob'], watch['to'], watch['from'],
                    f'after keep — {reason}', baseline=watch['baseline'], trial=t, regress=True)
                continue
        since = max(ks.get('since') or '', window)
        cur_model = current(ks, 'model', product, kind, b)
        cands = candidates(ks, product, kind, b, rows, window)
        for knob, frm, to in cands:
            brs = _runs_for(runs, kind, knob, cur_model if knob == 'model' else None, since, cfg)
            if len(brs) < s['min_samples']:
                continue
            base = stats(brs, runs)
            ks['trial'] = {'knob': knob, 'from': frm, 'to': to, 'started': stamp, 'baseline': base}
            log(kind, 'trial', knob, frm, to,
                f"baseline {base['n']} runs: repair {base['repair']:g}, fail {base['fail']:g}, "
                f"landed {base['landed_share']:.0%}", baseline=base)
            break
    save_state(product, state)
    return wrote


# ------------------------------------------------------------ the placement hook --

def effective(state, kind):
    """``(model label | None, seats | None)`` the placement applies for ``kind`` now."""
    ks = (state.get('kinds') or {}).get(kind) or {}
    model, seats = ks.get('model'), ks.get('seats')
    trial = ks.get('trial') or {}
    if trial.get('knob') == 'model':
        model = trial.get('to')
    elif trial.get('knob') == 'seats':
        seats = trial.get('to')
    return model, seats


def place(product, rows, running, cfg=None, out=print, state=None):
    """The one placement hook (the wave's): each non-S1 row of a tuned kind gets the kind's tuned
    model (a row on the kind's own label only — a class-specific label is left alone), and the
    rows past the kind's tuned seat share wait, printed ``waits … — tune: <kind> seat share``.
    Returns the rows that go on. Off — tune disabled — it returns ``rows`` untouched."""
    cfg = env.load_config() if cfg is None else cfg
    s = settings(cfg, product)
    if not s['enabled'] or not s['bounds']:
        return rows
    state = load_state(product) if state is None else state
    live = {}
    for r in running or ():
        k = (r or {}).get('kind')
        live[k] = live.get(k, 0) + 1
    kept = []
    for row in rows:
        kind = getattr(row, 'kind', None)
        b = s['bounds'].get(kind)
        if b is None or getattr(row, 'severity', None) == 'S1':
            kept.append(row)
            continue
        model, seats = effective(state, kind)
        ks = (state.get('kinds') or {}).get(kind) or {}
        base = ks.get('base') or base_model(product, kind)
        if model and _in_bounds('model', model, b) and row.model == base:
            row.model = model
        if seats is not None and _in_bounds('seats', seats, b):
            if live.get(kind, 0) >= seats:
                out(f"waits    {row.job:<24} {row.item:<10} — tune: {kind} seat share {seats} "
                    f"(live {live.get(kind, 0)})")
                continue
            live[kind] = live.get(kind, 0) + 1
        kept.append(row)
    return kept


def wave_hook(product, rows, running, out=print, event=None):
    """The wave step's call: the pass, then :func:`place`. Never raises — a fault is one line and
    the rows go on untuned."""
    try:
        cfg = env.load_config()
        step(product, cfg, out=out, event=event)
        return place(product, rows, running, cfg=cfg, out=out)
    except Exception as e:  # noqa: BLE001
        out(f'tune: skipped — {type(e).__name__}: {e}')
        return rows


# ------------------------------------------------------------ freeze --

def freeze(product, on=True, now=None, out=print):
    """``asf tune freeze``: every live trial reverted and logged, no new one until unfreeze."""
    state = load_state(product)
    stamp = _now(now)
    kinds = state.setdefault('kinds', {})
    if on:
        for kind in sorted(kinds):
            trial = kinds[kind].pop('trial', None)
            if trial:
                rec = {'at': stamp, 'kind': kind, 'event': 'revert', 'knob': trial['knob'],
                       'from': trial['to'], 'to': trial['from'], 'reason': 'frozen'}
                _append(product, rec)
                kinds[kind]['since'] = stamp
                out(f"tune: {kind} {trial['knob']} {trial['to']}→{trial['from']} revert — frozen")
    state['frozen'] = bool(on)
    _append(product, {'at': stamp, 'kind': '*', 'event': 'freeze' if on else 'unfreeze',
                      'knob': '', 'from': None, 'to': None, 'reason': 'operator'})
    save_state(product, state)
    out('tune: frozen — no trial runs until `asf tune unfreeze`' if on else 'tune: unfrozen')
    return 0


# ------------------------------------------------------------ views --

def _trial_progress(product, kind, trial, cfg, s, runs):
    trs = _runs_for(runs, kind, trial['knob'], trial['to'], trial['started'], cfg)
    t = stats(trs, runs) if trs else None
    base = trial.get('baseline') or {}
    rep = f", repair {t['repair']:g} vs {base.get('repair', 0):g}" if t else ''
    return f"{kind} {trial['from']}→{trial['to']} trial {len(trs)}/{s['trial_samples']}{rep}"


def status_cell(product, cfg=None, runs=None):
    """The ``Tune`` row of ``asf status``: ``tune: review heavy→light trial 4/10, repair 1.1 vs
    1.2``; ``None`` (no row) while the loop is off and has never written a line."""
    cfg = env.load_config() if cfg is None else cfg
    s = settings(cfg, product)
    rows = ledger(product)
    if not s['enabled']:
        return 'off (tune.enabled)' if rows else None
    state = load_state(product)
    if state.get('frozen'):
        return 'tune: frozen — `asf tune unfreeze` resumes'
    live = [(k, v['trial']) for k, v in sorted((state.get('kinds') or {}).items()) if v.get('trial')]
    if live:
        if runs is None:
            from asf.improve import measure
            runs = measure.ended_runs(product, since=min(t['started'] for _k, t in live)[:10])
        return 'tune: ' + '; '.join(_trial_progress(product, k, t, cfg, s, runs) for k, t in live)
    last = rows[-1] if rows else None
    tail = (f"; last: {last['kind']} {last.get('knob')} {last.get('from')}→{last.get('to')} {last['event']}"
            if last else '')
    return f"tune: no trial ({len(s['bounds'])} kind(s) bounded){tail}"


def render_history(rows):
    out = ['**TUNE HISTORY**', '', '| At | Kind | Event | Knob | Change | Reason |',
           '|---|---|---|---|---|---|']
    for r in rows:
        change = '' if r.get('event') in ('freeze', 'unfreeze') else f"{r.get('from')}→{r.get('to')}"
        out.append(f"| {(r.get('at') or '')[:16]} | {r.get('kind')} | {r.get('event')} | "
                   f"{r.get('knob') or ''} | {change} | {str(r.get('reason') or '').replace('|', '/')} |")
    if not rows:
        out.append('| — | — | — | — | — | no change yet |')
    return '\n'.join(out) + '\n'


# ------------------------------------------------------------ release criterion 11 --

def criterion(product, window_days, now=None, cfg=None, runs=None):
    """``(met, evidence)`` for release-readiness criterion 11, *Self-tuning live*: the loop is on,
    at least one change kept in the window (and not reverted since), and no unreverted
    regression — a live trial or watched keep whose runs already show a regress the pass has not
    acted on (it is frozen, or has not run)."""
    cfg = env.load_config() if cfg is None else cfg
    s = settings(cfg, product)
    if not s['enabled']:
        return False, 'tune.enabled is off'
    since = _since(now, window_days)
    rows = ledger(product)
    kept = []
    for i, r in enumerate(rows):
        if r.get('event') != 'keep' or (r.get('at') or '') < since:
            continue
        undone = any(x.get('event') == 'revert' and x.get('kind') == r.get('kind')
                     and x.get('knob') == r.get('knob') and x.get('from') == r.get('to')
                     for x in rows[i + 1:])
        if not undone:
            kept.append(r)
    state = load_state(product)
    open_ = []
    pending = [(k, v.get(w)) for k, v in sorted((state.get('kinds') or {}).items())
               for w in ('trial', 'watch') if v.get(w)]
    if pending:
        if runs is None:
            from asf.improve import measure
            runs = measure.ended_runs(product, since=since[:10])
        for kind, t in pending:
            trs = _runs_for(runs, kind, t['knob'], t['to'], t['started'], cfg)
            if len(trs) >= s['trial_samples']:
                verdict, reason, _g = judge(t['baseline'], stats(trs, runs), s)
                if reason.startswith('regress'):
                    open_.append(f"{kind} {t['knob']} {t['from']}→{t['to']} ({reason})")
    ev = (f"{len(kept)} kept change(s), {len(open_)} unreverted regression(s) in {window_days:g} d"
          + (f"; last kept: {kept[-1]['kind']} {kept[-1]['knob']} {kept[-1]['from']}→{kept[-1]['to']}"
             if kept else '')
          + ('; open: ' + ', '.join(open_) if open_ else '')
          + ('; frozen' if state.get('frozen') else ''))
    return bool(kept) and not open_, ev


# ------------------------------------------------------------ the CLI --

def add_parser(sub):
    p = sub.add_parser('tune', help='the self-tuning loop: history, freeze, unfreeze, status')
    p.add_argument('tune_command', choices=['history', 'freeze', 'unfreeze', 'status'])
    p.add_argument('--product')
    p.add_argument('--json', action='store_true')
    p.set_defaults(run=cmd_tune)
    return p


def cmd_tune(args):
    product = env.load_product(getattr(args, 'product', None))
    cmd = args.tune_command
    if cmd in ('freeze', 'unfreeze'):
        return freeze(product, on=(cmd == 'freeze'))
    if cmd == 'status':
        print(status_cell(product) or 'off (tune.enabled)')
        return 0
    rows = ledger(product)
    if getattr(args, 'json', False):
        print(json.dumps(rows, indent=1, default=str))
    else:
        print(render_history(rows), end='')
    return 0
