"""asf.improve.measure — the arithmetic: every run of the session registry, its spend, the table.

Pure. :func:`ended_runs` reads a ledger and a directory of job logs; :func:`table` takes runs and
returns numbers and touches no file and no clock, so every row of it is a unit test.

The unit is a **run** — a launch line and what folds onto it (``lifecycle.runs``) — never a job,
and never ``lifecycle.latest``: a job sent back three times spent three sessions. A run *landed*
when the trunk got it (``harvested``), not when the session said it finished.

Spend is per job. A job log holds one ``result`` line per assistant turn and ``total_cost_usd``
is cumulative within a ``session_id``, so a job's spend is the sum over its session ids of each
id's max (:func:`job_spend`) — not the last line (``metrics.session_event`` reads that one, and
undercounts a resumed job) and not the sum of every line. The registry cannot say which session id
belongs to which run, so a job's spend is split over its ended runs pro rata by minutes.

**Cloud runs carry no usage data**: the cloud lane's job logs never hold a ``total_cost_usd``
line, so :func:`job_spend` reads ``None`` for one and every run of that job gets ``usd=None``.
:func:`ended_runs` fills that gap where it can: a cloud run with no spend is given the median
``usd`` of this call's own priced runs sharing its ``(kind, model)``, once there are at least
``measure.cost_estimate_min_samples`` of them (``config.yaml``, :func:`settings`) — fewer leaves
``usd`` ``None`` rather than guess from too little. A filled-in run carries ``usd_estimated=True``
so a reader can tell a measurement from a guess.
"""
import collections
import dataclasses
import datetime
import json
import os
import statistics

from asf.workers import cloudpid, lifecycle, pool

_STAMP = '%Y-%m-%dT%H:%M:%SZ'
#: ``config.yaml measure:`` defaults.
DEFAULTS = {'cost_estimate_min_samples': 3}


@dataclasses.dataclass(frozen=True)
class Run:
    """One ended run, exactly as §2.1 fences it."""
    job: str
    kind: str
    model: str
    item: str | None
    started: str
    ended: str
    minutes: float
    landed: bool
    end_reason: str
    usd: float | None
    publish_refused: str = ''  #: the registry's `publish_refused` key; read by diagnose's sub-causes and nothing else
    worktree: str = ''  #: the registry's `worktree` key; read by diagnose's sub-causes and nothing else
    attempt: int = 1        # 1-based, among this job's ended runs in ledger order
    cloud: bool = False     #: ran on the cloud lane (its pid is a cloud token); read by asf.metrics.throughput
    usd_estimated: bool = False  #: `usd` is a same-(kind, model) median, not a measurement


class Cell(collections.namedtuple('Cell', 'sessions hours usd')):
    """One row of a breakdown: its sessions, hours and USD. ``usd_per_hour`` is derived."""
    __slots__ = ()

    @property
    def usd_per_hour(self):
        return round(self.usd / self.hours, 2) if self.hours else 0.0


def _epoch(stamp):
    try:
        return datetime.datetime.strptime(str(stamp), _STAMP).replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def _minutes(started, ended):
    a, b = _epoch(started), _epoch(ended)
    if a is None or b is None:
        return 0.0
    return round(max(0.0, (b - a) / 60), 1)


def job_spend(job, logs_dir):
    """What ``<logs_dir>/<job>.jsonl`` says the job cost: the max ``total_cost_usd`` of each
    ``session_id``'s ``result`` lines, summed (a line with no id forms the group ``'none'``).
    ``None`` when there is no log or no result line carrying the field."""
    if not logs_dir:
        return None
    path = os.path.join(logs_dir, f'{job}.jsonl')
    if not os.path.isfile(path):
        return None
    best = {}
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get('type') != 'result':
                continue
            cost = rec.get('total_cost_usd')
            if not isinstance(cost, (int, float)) or isinstance(cost, bool):
                continue
            sid = rec.get('session_id') or 'none'
            best[sid] = max(best.get(sid, cost), cost)
    return sum(best.values()) if best else None


def settings(cfg=None):
    """``config.yaml measure:`` over :data:`DEFAULTS`: ``cost_estimate_min_samples``, the priced
    same-``(kind, model)`` runs a cloud run with no usage data needs before its spend is
    estimated rather than left ``None``."""
    if cfg is None:
        from asf import env
        try:
            cfg = env.load_config()
        except Exception:  # noqa: BLE001 — no config is the defaults
            cfg = {}
    block = (cfg or {}).get('measure')
    block = block if isinstance(block, dict) else {}
    out = dict(DEFAULTS)
    try:
        out['cost_estimate_min_samples'] = max(
            1, int(block.get('cost_estimate_min_samples', DEFAULTS['cost_estimate_min_samples'])))
    except (TypeError, ValueError):
        pass
    return out


def _estimate_cloud_spend(runs, cfg):
    """``runs`` with every cloud, unpriced run's ``usd`` filled from the median of this set's own
    priced runs sharing its ``(kind, model)`` — only once there are ``cost_estimate_min_samples``
    of them (:func:`settings`); otherwise the run is returned unchanged."""
    if not any(r.usd is None and r.cloud for r in runs):
        return runs
    min_n = settings(cfg)['cost_estimate_min_samples']
    priced = collections.defaultdict(list)
    for r in runs:
        if r.usd is not None:
            priced[(r.kind, r.model)].append(r.usd)
    out = []
    for r in runs:
        same = priced.get((r.kind, r.model), ())
        if r.usd is None and r.cloud and len(same) >= min_n:
            out.append(dataclasses.replace(r, usd=statistics.median(same), usd_estimated=True))
        else:
            out.append(r)
    return out


def _logs_dir_of(product):
    if product is None:
        return None
    from asf.metrics import metrics
    name = getattr(product, 'name', product)
    return os.path.dirname(metrics.job_log_path(name, 'x'))


def ended_runs(product, *, ledger=None, logs_dir=None, since=None, as_of=None, cfg=None):
    """Every ended run of every job of ``product``'s registry (``ledger`` overrides the path),
    oldest first, each carrying its share of the job's spend from ``logs_dir`` (default: the
    product's job-log directory). ``since`` (``YYYY-MM-DD``) keeps runs with ``ended[:10] >=
    since``; ``as_of`` (a full ISO stamp) keeps ``ended <= as_of``. The split of a job's spend is
    over *all* its ended runs, before either filter, so a window carries its own share — and so
    is :func:`_estimate_cloud_spend`'s fill-in of a cloud run with no usage data (``cfg``: the
    ``config.yaml`` the estimate's ``measure.cost_estimate_min_samples`` reads; default
    ``env.load_config()``)."""
    if ledger is None:
        ledger = pool.sessions_path(product)
    if logs_dir is None:
        logs_dir = _logs_dir_of(product)
    out = []
    for job, runs in lifecycle.runs(ledger).items():
        ended = [r for r in runs if r.get('ended')]
        if not ended:
            continue
        minutes = [_minutes(r.get('started'), r['ended']) for r in ended]
        spend = job_spend(job, logs_dir)
        total = sum(minutes)
        for attempt, (r, m) in enumerate(zip(ended, minutes), 1):
            share = (m / total) if total else 1 / len(ended)
            out.append(Run(
                job=job, kind=r.get('kind') or '', model=r.get('model') or '',
                item=r.get('item') or None, started=r.get('started') or '', ended=r['ended'],
                minutes=m, landed=bool(r.get('harvested')), end_reason=r.get('end_reason') or '',
                usd=None if spend is None else spend * share,
                publish_refused=r.get('publish_refused') or '', worktree=r.get('worktree') or '',
                attempt=attempt, cloud=cloudpid.is_token(r.get('pid'))))
    out = _estimate_cloud_spend(out, cfg)
    if since:
        out = [r for r in out if r.ended[:10] >= since]
    if as_of:
        out = [r for r in out if r.ended <= as_of]
    out.sort(key=lambda r: (r.started, r.job))
    return out


def _cells(runs, key):
    acc = {}
    for r in runs:
        s, m, u = acc.get(key(r), (0, 0.0, 0.0))
        acc[key(r)] = (s + 1, m + r.minutes, u + (r.usd or 0.0))
    return {k: Cell(s, round(m / 60, 1), round(u, 2)) for k, (s, m, u) in sorted(acc.items())}


def _per_session(runs):
    """``(usd, hours)`` averaged over ``runs``, each ``None`` when ``runs`` is empty."""
    if not runs:
        return None, None
    return (round(sum(r.usd or 0.0 for r in runs) / len(runs), 2),
            round(sum(r.minutes for r in runs) / len(runs) / 60, 1))


def table(runs, record_usd=None):
    """The numbers over ``runs``, one flat dict (§2.1). ``record_usd`` is the same window's spend
    as the record's ``cost:`` blocks hold it, carried beside ``usd`` and ``None`` when not given."""
    runs = list(runs)
    minutes = sum(r.minutes for r in runs)
    usd = sum(r.usd or 0.0 for r in runs)
    landed = [r for r in runs if r.landed]
    landed_items = {r.item for r in landed if r.item}
    per_item = collections.Counter(r.item for r in runs if r.item)
    repeat = sorted(i for i, n in per_item.items() if n >= 3)
    on_repeat = [r for r in runs if r.item in repeat]
    n_items = len(landed_items)
    relaunches = [r for r in runs if r.attempt >= 2]
    first_launches = [r for r in runs if r.attempt == 1]
    usd_per_session, hours_per_session = _per_session(relaunches)
    first_usd_per_session, first_hours_per_session = _per_session(first_launches)

    def share(part, whole):
        return round(part / whole, 3) if whole else 0.0

    return {
        'sessions': len(runs),
        'hours': round(minutes / 60, 1),
        'usd': round(usd, 2),
        'record_usd': record_usd,
        'landed_sessions': len(landed),
        'landed_items': n_items,
        'non_landing_share': share(minutes - sum(r.minutes for r in landed), minutes),
        'by_kind': _cells(runs, lambda r: r.kind),
        'by_model': _cells(runs, lambda r: r.model),
        'by_kind_model': _cells(runs, lambda r: (r.kind, r.model)),
        'repeat_items': {
            'count': len(repeat), 'items': repeat,
            'usd_share': share(sum(r.usd or 0.0 for r in on_repeat), usd),
            'hours_share': share(sum(r.minutes for r in on_repeat), minutes)},
        'minutes_per_landed_item': round(minutes / n_items, 1) if n_items else None,
        'usd_per_landed_item': round(usd / n_items, 2) if n_items else None,
        'relaunches': {
            'sessions': len(relaunches), 'share': share(len(relaunches), len(runs)),
            'usd_per_session': usd_per_session, 'hours_per_session': hours_per_session,
            'first_usd_per_session': first_usd_per_session, 'first_hours_per_session': first_hours_per_session,
        },
    }
