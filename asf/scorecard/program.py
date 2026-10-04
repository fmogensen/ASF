"""asf.scorecard.program — the improvement program's metrics: one row per product and window.

The weekly scorecard (:mod:`asf.scorecard.score`) says what landed and what it cost; this row
says where the spend went that no landing needed, in the program's own terms, so every target in
``docs/program/targets.yaml`` is one key here and ``asf scorecard --check`` can read it:

* ``idle_hours`` — the hours of launch gaps longer than :data:`IDLE_GAP_H` (the factory sat);
* ``offline_ticks`` — tick-log lines ``tick: record failed — offline``;
* ``waste_by_class`` — each run classed once, in order: ``failed`` (the process or the push
  gave out), ``loop`` (the 4th+ run of one job on one head that landed nothing), ``superseded``,
  ``nothing`` (an empty branch, nothing to land);
* ``mechanical_only_corrects`` — correct runs whose attributed causes are all mechanical
  (:data:`MECHANICAL`); a run's causes are the correction records of its job between the
  previous run's start and its own;
* ``max_runs_job_head`` — the most runs one job made on one launch head;
* ``heavy_share`` — the share of the window's spend on the heavy model; ``cardless_heavy_reviews``
  — reviews of a cardless ``PR-*`` item on the heavy model; ``reshape`` — reshape runs;
* ``infra_ended`` — runs the process or platform ended (dead pid, stopped, quota, bare failed);
* ``rows_waiting_on_item`` / ``top_roots`` / ``reviews_held_after`` — over the board's plan rows,
  the rows that wait on another item, transitively to the item at the root of each chain, and
  the review rows among them;
* :data:`PENDING` — keys whose producers land later in the program: ``None`` until they do.

Pure over its inputs (:func:`row`); :func:`load` is the only reader.
"""
import collections
import datetime
import glob
import json
import os
import re

from asf.scorecard.facts import iso, to_dt

#: A launch gap longer than this many hours is idle time.
IDLE_GAP_H = 2.0
#: A correction cause of one of these kinds is mechanical: code could resolve it with no session.
MECHANICAL = frozenset({'copies', 'hook refused', 'naming', 'unpushed', 'footprint',
                        'rebase conflict', 'conflict', 'merge', 'died'})
#: The waste classes, in the order a run is tried against them.
WASTE_CLASSES = ('failed', 'loop', 'superseded', 'nothing')
#: The run of a job on one head from which on it is a loop.
LOOP_FROM = 4
#: End-reason classes the process or platform caused (the run never reached its own report).
def _infra():
    from asf.workers import lifecycle
    return frozenset({lifecycle.DEAD_PID, 'stopped', 'quota-exhausted', 'failed (bare)'})


INFRA = _infra()
#: The tick-log line an offline record step writes (``asf.tick.tick``).
OFFLINE_LINE = 'tick: record failed — offline'
#: Keys whose producers land later in the program; ``None`` until registered (:func:`register`).
PENDING = ('gh_calls_per_tick', 'facts_disagree', 'i14_report_lines', 'mechanical_resolved',
           'mechanical_residue', 'refguard_warns')
_PRODUCERS = {}
_ID = re.compile(r'^[A-Z]{1,2}-\d{3,4}$')
_DELIVERY = re.compile(r'WAITS ON delivery ([A-Z]{1,2}-\d{3,4})')
_STAMP = re.compile(r'\b(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\b')


def register(key, producer):
    """A later PR's producer of one key: ``producer(ctx) -> value``, ``ctx`` the :func:`row`
    inputs. Replaces the key's ``None``."""
    _PRODUCERS[key] = producer


def _i14_report_lines(ctx):
    """``i14_report_lines`` — invariant I14's report records in the window
    (:func:`asf.invariants.i14_report_lines`); ``None`` without a product."""
    if ctx.get('product') is None:
        return None
    from asf import invariants
    return invariants.i14_report_lines(ctx['product'], ctx['start'], ctx['end'])


register('i14_report_lines', _i14_report_lines)


# ------------------------------------------------------------ classes --

def end_class(reason):
    """An ``end_reason`` → one short class (``dead pid``, ``not pushed``, ``finished`` …)."""
    from asf.workers import lifecycle
    er = str(reason or '').strip()
    if er in ('', 'None'):
        return 'none'
    if er == 'finished':
        return 'finished'
    if lifecycle.is_dead_reason(er) or er.startswith(lifecycle.DEAD):
        return lifecycle.DEAD_PID
    if er.startswith('stopped'):
        return 'stopped'
    if er.startswith('failed: quota'):
        return 'quota-exhausted'
    if er == 'failed':
        return 'failed (bare)'
    if er.startswith('failed: '):
        return er[8:].split(':')[0].strip()[:32]
    return er.split(':')[0].strip()[:32]


def waste_class(run, attempt):
    """The first of :data:`WASTE_CLASSES` ``run`` falls in (``attempt`` = its run number on its
    head), or ``None`` for a run that did useful work."""
    from asf.workers import lifecycle
    er = str(run.get('end_reason') or '')
    nothing = 'empty branch' in er or 'nothing to land' in er
    if not nothing and er.startswith(('failed', lifecycle.DEAD, 'stopped')):
        return 'failed'
    if attempt >= LOOP_FROM and not run.get('harvested'):
        return 'loop'
    if er.startswith('superseded') or run.get('harvested') == 'superseded':
        return 'superseded'
    if nothing:
        return 'nothing'
    return None


def is_heavy(model, heavy):
    return bool(model) and model in heavy


# ------------------------------------------------------------ the ledger --

def ledger_runs(ledger, usd=None):
    """Every run of the registry at ``ledger`` (:func:`asf.workers.lifecycle.runs`), as dicts
    carrying ``_job``, ``_causes`` (the sorted correction kinds recorded on its job since the
    previous run started), ``_usd`` (``usd[(job, started)]``, else the line's own ``usd``) and
    ``_head`` (the launch head, 7 characters)."""
    from asf.workers import lifecycle
    usd = usd or {}
    corr = collections.defaultdict(list)
    for rec in lifecycle.read_lines(ledger):
        c = rec.get('correction')
        if isinstance(c, dict) and c.get('at'):
            corr[rec['job']].append(c)
    out = []
    for job, rs in lifecycle.runs(ledger).items():
        prev = ''
        for r in sorted((r for r in rs if r.get('started')), key=lambda r: r['started']):
            causes = sorted({c.get('kind') or '?' for c in corr.get(job, ())
                             if prev < c['at'] <= r['started']})
            prev = r['started']
            u = usd.get((job, r['started']), r.get('usd'))
            out.append(dict(r, _job=job, _causes=causes,
                            _usd=u if isinstance(u, (int, float)) else None,
                            _head=str(r.get('launch_head') or '')[:7]))
    out.sort(key=lambda r: (r['started'], r['_job']))
    return out


def offline_lines(texts):
    """``[stamp or None]`` per offline line in the tick-log ``texts``: a line is dated by the
    newest ISO stamp the log printed before it (tick logs carry no clock of their own)."""
    out = []
    for text in texts:
        stamp = None
        for line in (text or '').splitlines():
            m = _STAMP.search(line)
            if m:
                stamp = m.group(1)
            if OFFLINE_LINE in line:
                out.append(stamp)
    return out


# ------------------------------------------------------------ the board --

def roots(rows):
    """``(waiting, top)`` over plan rows (dicts with ``item_id``, ``waits_on``, ``action``,
    ``brief_kind``): the rows that wait on another item, and ``[(root, n)]`` — each chain
    followed to the item it ends on, counted, most first."""
    dep = {}
    for r in rows:
        w = str(r.get('waits_on') or '')
        if _ID.match(w):
            dep[r['item_id']] = w
        elif w == 'delivery':
            m = _DELIVERY.search(str(r.get('action') or ''))
            if m:
                dep[r['item_id']] = m.group(1)

    def root(i, seen=()):
        if i in seen:
            return i
        return root(dep[i], seen + (i,)) if i in dep else i
    counts = collections.Counter(root(i) for i in dep)
    return dep, counts.most_common()


# ------------------------------------------------------------ the row --

def _cell(rs):
    return {'runs': len(rs), 'usd': round(sum(r['_usd'] or 0.0 for r in rs), 2)}


def idle_hours(starts, start, end, gap_h=IDLE_GAP_H):
    """The hours of the gaps longer than ``gap_h`` between consecutive launches in the window."""
    ds = sorted(d for d in (to_dt(s) for s in starts) if d is not None and start <= d < end)
    total = 0.0
    for a, b in zip(ds, ds[1:]):
        h = (b - a).total_seconds() / 3600
        if h > gap_h:
            total += h
    return round(total, 2)


def row(runs, start, end, *, heavy=(), offline=None, board=None, extra_starts=(), product=None):
    """The program row over ``[start, end)``.

    ``runs`` — :func:`ledger_runs`; ``heavy`` — the heavy model ids; ``offline`` —
    :func:`offline_lines` (``None``: not read); ``board`` — plan rows as dicts (``None``: not
    read); ``extra_starts`` — other products' launch stamps for ``idle_hours`` (``--all``);
    ``product`` — for the keys read from its state files (``None``: not read)."""
    heavy = set(heavy)
    win = [r for r in runs if (d := to_dt(r.get('started'))) is not None and start <= d < end]
    usd = sum(r['_usd'] or 0.0 for r in win)
    # the attempt number of each run on its (job, head), over the whole ledger
    seen = collections.Counter()
    attempt = {}
    for r in runs:
        k = (r['_job'], r['_head'])
        seen[k] += 1
        attempt[id(r)] = seen[k]
    waste = {c: [] for c in WASTE_CLASSES}
    for r in win:
        c = waste_class(r, attempt[id(r)] if r['_head'] else 0)
        if c:
            waste[c].append(r)
    mech = [r for r in win if r.get('kind') == 'correct' and r['_causes']
            and set(r['_causes']) <= MECHANICAL]
    jh = collections.Counter((r['_job'], r['_head']) for r in win if r['_head'])
    top = jh.most_common(1)
    cardless = [r for r in win if r.get('kind') == 'review'
                and str(r.get('item') or '').startswith('PR-')]
    reshape = [r for r in win if r.get('kind') == 'reshape']
    ended = [r for r in win if r.get('ended')]
    infra = [r for r in ended if end_class(r.get('end_reason')) in INFRA]
    out = {
        'start': iso(start), 'end': iso(end), 'runs': len(win), 'usd': round(usd, 2),
        'idle_hours': idle_hours([r['started'] for r in win] + list(extra_starts), start, end),
        'offline_ticks': None if offline is None else sum(
            1 for s in offline if s is None or start <= to_dt(s) < end),
        'waste_by_class': {c: _cell(rs) for c, rs in waste.items()},
        'mechanical_only_corrects': dict(_cell(mech), by_cause=collections.Counter(
            '+'.join(r['_causes']) for r in mech).most_common(8)),
        'max_runs_job_head': ({'job': top[0][0][0], 'head': top[0][0][1], 'runs': top[0][1]}
                              if top else {'job': None, 'head': None, 'runs': 0}),
        'job_heads_over_3': sum(1 for n in jh.values() if n > 3),
        'heavy_share': round(sum(r['_usd'] or 0.0 for r in win if is_heavy(r.get('model'), heavy))
                             / usd, 3) if usd else None,
        'cardless_reviews': _cell(cardless),
        'cardless_heavy_reviews': sum(1 for r in cardless if is_heavy(r.get('model'), heavy)),
        'reshape': dict(_cell(reshape), heavy=sum(1 for r in reshape
                                                  if is_heavy(r.get('model'), heavy))),
        'infra_ended': dict(_cell(infra), by_class=collections.Counter(
            end_class(r.get('end_reason')) for r in infra).most_common()),
        'rows_waiting_on_item': None, 'top_roots': None, 'reviews_held_after': None,
    }
    if board is not None:
        dep, top_roots = roots(board)
        out['rows_waiting_on_item'] = len(dep)
        out['top_roots'] = top_roots[:8]
        out['reviews_held_after'] = sum(1 for r in board if r.get('item_id') in dep
                                        and str(r.get('brief_kind') or '') == 'review')
    ctx = {'runs': runs, 'window': win, 'start': start, 'end': end, 'board': board,
           'product': product}
    for key in PENDING:
        out[key] = None
    for key, producer in _PRODUCERS.items():
        try:
            out[key] = producer(ctx)
        except Exception:  # noqa: BLE001 — one key's producer never loses the row
            out[key] = None
    return out


# ------------------------------------------------------------ windows --

def window(spec, now=None):
    """``7d`` / ``36h`` / ``since=<iso>`` → ``(start, end)``; ``end`` is ``now`` + 1 s."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    end = now + datetime.timedelta(seconds=1)
    spec = str(spec or '7d').strip()
    if spec.startswith('since='):
        start = to_dt(spec[len('since='):])
        if start is None:
            raise ValueError(f'--window {spec}: not an ISO time')
        return start, end
    m = re.fullmatch(r'(\d+(?:\.\d+)?)([dh])', spec)
    if not m:
        raise ValueError(f'--window {spec}: want <n>d, <n>h or since=<iso>')
    n = float(m.group(1))
    span = datetime.timedelta(days=n) if m.group(2) == 'd' else datetime.timedelta(hours=n)
    return end - span, end


# ------------------------------------------------------------ the reader --

def heavy_models(cfg=None):
    """The heavy model ids the operator's ``worker_pool.models.heavy`` maps the label onto."""
    if cfg is None:
        from asf import env
        try:
            cfg = env.load_file(env.config_path())
        except Exception:  # noqa: BLE001
            cfg = {}
    table = (((cfg or {}).get('worker_pool') or {}).get('models')) or {}
    v = table.get('heavy') if isinstance(table, dict) else None
    return {v} if isinstance(v, str) and v else set()


def _tick_logs(product):
    from asf import env
    texts = []
    for path in sorted(glob.glob(os.path.join(env.log_dir(), f'tick-{product.name}*.log'))):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                texts.append(f.read())
        except OSError:
            continue
    return texts


def _board(product):
    """The plan rows ``asf next --all`` computes, as dicts; ``None`` when they cannot be read."""
    try:
        from asf import capacity as capacity_mod
        from asf.feeder import rows as R
        from asf.tick import step_wave
        from asf.views import index_reader as ix
        root = product.backlog_dir
        items, _generated = ix.load(root)
        inputs = step_wave.plan_inputs(product, root)
        rows = R.plan_rows(items, product, step_wave.inflight(product),
                           capacity_mod.resolve(product).sessions, **inputs, decision_limit=0)
        return [dict(r.__dict__) for r in rows]
    except Exception:  # noqa: BLE001 — the board unreadable is a None key, never a lost row
        return None


def load(product, start, end, *, board=True, others=()):
    """The program row for ``product`` over ``[start, end)`` from its registry, job logs, tick
    logs and (``board``) its plan rows; ``others`` — more products whose launches count toward
    ``idle_hours``."""
    from asf.improve import measure
    from asf.workers import pool
    ledger = pool.sessions_path(product)
    try:
        usd = {(r.job, r.started): r.usd for r in measure.ended_runs(product, ledger=ledger)}
    except (OSError, ValueError):
        usd = {}
    runs = ledger_runs(ledger, usd)
    extra = []
    for p in others:
        extra += [r['started'] for r in ledger_runs(pool.sessions_path(p))]
    return row(runs, start, end, heavy=heavy_models(), offline=offline_lines(_tick_logs(product)),
               board=_board(product) if board else None, extra_starts=extra, product=product)


def dumps(d):
    return json.dumps(d, indent=1, default=str)
