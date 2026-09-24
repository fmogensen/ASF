"""asf.tick.summary — the two tables the tick ends with: the sessions in flight, and the ones
that ended since the last tick on this clock.

Folded out of the session ledger through :mod:`asf.workers.lifecycle` (the one model of a lane
job's life), each row titled from the record clone's ``index.json``. Best-effort console output:
it writes nothing to the record, and a failure to render never changes the tick's exit code.
"""
import datetime
import os

from asf import env, scheduler
from asf.tick import shadow
from asf.views import index_reader
from asf.views.sessions import pid_alive
from asf.workers import lifecycle, pool

LEDGER_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


# ---- the clock and its stamp -------------------------------------------------------

def clock(chosen):
    return scheduler.steps_slug(chosen) if chosen else 'all'


def stamp_path(product, clock_name):
    return os.path.join(env.state_dir(product), f'summary-{clock_name}.stamp')


def read_stamp(product, clock_name):
    try:
        with open(stamp_path(product, clock_name), encoding='utf-8') as f:
            text = f.read().strip()
    except OSError:
        return None
    return text or None


def write_stamp(product, clock_name, now_iso):
    with open(stamp_path(product, clock_name), 'w', encoding='utf-8') as f:
        f.write(now_iso + '\n')


def window_start(stamp, now):
    if stamp:
        return stamp
    now_dt = datetime.datetime.strptime(now, LEDGER_FORMAT)
    return (now_dt - datetime.timedelta(hours=24)).strftime(LEDGER_FORMAT)


def age(start_iso, end_iso):
    try:
        start = datetime.datetime.strptime(start_iso, LEDGER_FORMAT)
        end = datetime.datetime.strptime(end_iso, LEDGER_FORMAT)
    except (TypeError, ValueError):
        return '?'
    delta = (end - start).total_seconds()
    if delta < 0:
        return '?'
    if delta < 60:
        return '<1m'
    minutes, _ = divmod(int(delta), 60)
    if delta < 3600:
        return f'{minutes}m'
    hours, minutes = divmod(minutes, 60)
    if delta < 86400:
        return f'{hours}h{minutes:02d}m'
    days, hours = divmod(hours, 24)
    return f'{days}d{hours:02d}h'


# ---- titles, read once from the record clone's index -------------------------------

def titles(product):
    try:
        items, _generated = index_reader.load(shadow.record_dir(product))
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {item_id: item.get('title') or '' for item_id, item in items.items()}


# ---- the rows, both through lifecycle -----------------------------------------------

def inflight_rows(product, alive):
    rows = []
    for run in pool.live_sessions(product):
        row = dict(run)
        row['status'] = 'working' if alive(run.get('pid')) else 'dead pid'
        rows.append(row)
    rows.sort(key=lambda r: r.get('started') or '￿')
    return rows


def done_rows(product, since, now):
    all_runs = [r for rs in lifecycle.runs(pool.sessions_path(product)).values() for r in rs]
    rows = [r for r in all_runs if r.get('ended') and since < r['ended'] <= now]
    rows.sort(key=lambda r: r['ended'])
    return rows


# ---- the render, pure ---------------------------------------------------------------

def _cell(value):
    text = str(value) if value not in (None, '') else ''
    return text if text else '—'


def _block(title, columns, records, suffix=''):
    count = 'none' if not records else ('1 session' if len(records) == 1 else f'{len(records)} sessions')
    lines = [f'{title} — {count}{suffix}']
    if not records:
        return lines
    rows = [[_cell(rec.get(c)) for c in columns] for rec in records]
    widths = [max(len(columns[i]), max(len(row[i]) for row in rows)) for i in range(len(columns) - 1)]

    def fmt(cells):
        return '  '.join([cells[i].ljust(widths[i]) for i in range(len(widths))] + [cells[-1]])

    lines.append(fmt(list(columns)))
    lines.extend(fmt(row) for row in rows)
    return lines


def credits_landing(run):
    """True when the DONE table may say a run ``landed``: it was harvested at a real sha AND it
    ended ``finished`` — which health writes only for a pushed branch with commits of its own
    (:func:`asf.workers.lifecycle.judge`). A run that wrote nothing (an empty end marked landed
    because an earlier run's work was on the trunk, a synthetic adoption, an archive) is not
    credited with someone else's sha."""
    sha = (run or {}).get('harvested')
    return (bool(sha) and sha not in lifecycle.NOT_A_LANDING and lifecycle.finished(run)
            and not run.get('adopted'))


IN_FLIGHT_COLUMNS =('job', 'item', 'kind', 'feature', 'account', 'model', 'status', 'since', 'what')
DONE_COLUMNS = ('job', 'item', 'kind', 'result', 'took', 'what')


def render(inflight, done, titles_by_item, since, now, first):
    in_records = [{
        'job': r.get('job'), 'item': r.get('item'), 'kind': r.get('kind'),
        'feature': r.get('feature'), 'account': r.get('account'), 'model': r.get('model'),
        'status': r.get('status'), 'since': age(r.get('started'), now),
        'what': titles_by_item.get(r.get('item')),
    } for r in inflight]

    done_records = []
    for r in done:
        result = r.get('end_reason')
        if credits_landing(r):
            result = f"{result}, landed {r['harvested'][:7]}"
        done_records.append({
            'job': r.get('job'), 'item': r.get('item'), 'kind': r.get('kind'),
            'result': result, 'took': age(r.get('started'), r.get('ended')),
            'what': titles_by_item.get(r.get('item')),
        })

    done_title = f'DONE since {since}'
    done_suffix = ' (first tick on this clock)' if first else ''
    lines = []
    for title, columns, records, suffix in (
        ('IN FLIGHT', IN_FLIGHT_COLUMNS, in_records, ''),
        (done_title, DONE_COLUMNS, done_records, done_suffix),
    ):
        lines.append('')
        lines.extend(_block(title, columns, records, suffix))
    return '\n'.join(lines)


# ---- the entry point ------------------------------------------------------------------

def run(ctx, chosen, out=print, now=None, alive=pid_alive):
    now = now or pool.now_iso()
    try:
        product = ctx.product
        clock_name = clock(chosen)
        stamp = read_stamp(product, clock_name)
        since = window_start(stamp, now)
        inflight = inflight_rows(product, alive)
        done = done_rows(product, since, now)
        out(render(inflight, done, titles(product), since, now, stamp is None))
        write_stamp(product, clock_name, now)
    except Exception as e:  # noqa: BLE001
        first_line = str(e).strip().splitlines()[0] if str(e).strip() else ''
        detail = first_line or type(e).__name__
        out(f'tick: summary not rendered ({detail})')
