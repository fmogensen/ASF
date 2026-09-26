"""asf.ci_queue — one start queue per product for every CI run ASF itself starts.

Four places start a run: the lane opening a PR (the host's ``pull_request`` run), the lane merging
onto the trunk (a PR merge or a fast-forward push: the trunk's ``push`` run), the tick's ``batch``
step, and a deploy dispatch. Each asks :func:`admit` first, with a key naming the start
(``pr:<branch>``, ``trunk:<branch>``, ``batch``, ``deploy:<env>``). A start that is not admitted
is not made: the lane leaves the branch where it is (no PR opened, no merge pushed — a push the host
would turn into a run is itself the thing held), the batch step and the deploy wait, and the next
tick asks again. Its entry keeps its place in line (``since``) while it keeps asking.

**Admission.** A run starts only while

- for a batch or an ordinary PR start, the product's CI ceiling (``capacity.ci``,
  :func:`asf.capacity.product_ci` / ``total.ci``) has room: runs in flight
  (:func:`asf.capacity.ci_runs_in_flight`, the one count the status row shows too) below it. An
  S1 or hotfix start, a trunk run and a deploy are exempt (:func:`ceiling_applies`): PR runs
  in flight never hold the trunk every deploy waits on; and
- for a batch or an ordinary PR start only (the same starts the ceiling holds), for every runner
  class the run needs, the free runners are at least its expected jobs there, after what every
  entry ahead of it in line needs of that class is set aside. An S1 or hotfix start, a trunk run
  and a deploy reserve nothing: they start at once, even with no runner free — the host queues
  their jobs anyway, and priority matters more than packing.

*Expected jobs per class* are measured (:func:`needs_from_history`): over the last
``ci.queue.history`` (default 10) runs of the workflow that start triggers that completed
``success`` or ``failure`` (a cancelled run is dropped), each read at its *latest attempt* only
(a superseded attempt's jobs never count, and attempts never overlap), per run and class the peak
number of jobs running *at once* — each job that got a runner (``runner_name`` set, conclusion
not ``skipped``) counted over its ``started_at``..``completed_at``, so a skipped or conditional
job never counts and sequential stages never add up — grouped by the class of the runner that
actually ran it (its ``ci.pool`` entry's ``class``, else its ``role``), never by the job's
``runs-on``; a runner outside the pool, by its labels mapped through :func:`label_classes` (a
label every carrier of which sits in one class names that class, so a sub-label such as
``fast-heavy`` carried only by ``heavy`` runners counts in ``heavy``, once — labels never make a
demand of their own that sums with the parent). A job listed twice (same job id) counts once. The
*median* of that over the runs (rounded up), capped at what the pool declares for that class. The figure is cached in the queue file keyed by the set of run
ids it was measured from (re-measured only when that set changes; the ids are listed again after
:data:`EXPECT_TTL_S`) and memoised per pass, so the hold line, the status Capacity row
(:func:`status_clause`) and ``asf ci queue`` all name the one number. *Free runners* come from the runners API: an
online runner that is not busy, counted at its ``slots``; runs this queue admitted in the last
:data:`PICKUP_S` are subtracted too, because their jobs are queued on the host before any runner
shows busy.

**Starvation guard.** An ordinary PR start (not a batch) that has waited in line longer than
``ci.queue.pr_wait_min`` (default 45) minutes is admitted once at least *half* its expected jobs
per class (rounded up) are free after the entries ahead are set aside — the ceiling still holds.
Measured peaks near the pool's size would otherwise hold a PR for as long as any other run is
in flight. The admission prints one line naming the wait and the free count.

**Order.** S1 and hotfix items first (0), then trunk runs (1: every deploy waits on a green
trunk, so a trunk run never queues behind PR runs), then PRs of customer-facing Features (2: the
item sits under a Feature, and the Feature says ``customer_facing: true`` or the branch touches
``customer_paths``), then everything else (3); within a priority, oldest first.

**Superseded trunk runs.** A trunk run judges every commit below it, so an older trunk run still
*queued* when a newer one exists is moot: :func:`cancel_superseded` cancels it, keeping the newest
queued-or-running ``push`` run on the trunk per workflow, one line per cancel. A run already in
progress finishes. The lane's in-process pass calls it every tick for a queued product. An entry nobody
asked about for :data:`STALE_S` leaves the line.

**Trunk starvation relief.** The host's own queue is first in, first out: a trunk run pushed
after PR runs already sit there waits behind all of them, and the start queue above cannot
reorder runs the host already holds. :func:`relieve_trunk` (every tick, from the lane's pass):
when the newest trunk ``push`` run of the trunk workflow has been queued longer than
``ci.queue.trunk_wait_min`` (default 20) minutes, it cancels runs queued *ahead* of it (created
earlier, not yet started — an in-progress run always finishes), lowest priority first — PR runs
of ordinary items, then of customer-facing Features, then batch runs; newest first within each —
until the free runners plus what the cancelled runs would have taken cover the trunk run's
expected jobs in every class. An S1 or hotfix run is never cancelled. Each cancel is remembered
in the queue file (``relief``) and, once the trunk run has started, re-run (``gh run rerun``)
through this queue at its original priority. One line per cancel and per re-run, naming the trunk
sha and its wait. ``mode: dry-run`` (or a dry-run pass) prints what it would do, writes nothing.
Every wait is UTC-aware now minus the host's UTC ``createdAt``, never local time.

**Every hold is one line**: ``ci queue: T-0341 waits — heavy 0 free, needs 3 (S2, 4th in line)``.

**Configuration** (``ci.queue`` in the product file)::

    ci:
      queue:
        mode: on          # on (default with a ci.pool) | dry-run | off
        history: 10       # runs of each workflow measured
        trunk_wait_min: 20  # a queued trunk run waiting longer gets runs ahead of it cancelled
        pr_wait_min: 45   # an ordinary PR start waiting longer starts on half its expected jobs
        workflows: {pr: checks.yml, trunk: checks.yml, batch: batch.yml}  # default ci.workflow

A product without ``ci.pool`` (or with ``mode: off``) is not queued: every start goes as it did
before, and no ``gh`` call is made here. ``mode: dry-run`` decides and prints each hold as
``ci queue (dry-run): … would wait`` but starts everything and writes nothing. ``asf ci queue``
prints the line with each entry's decision, writing nothing.
"""
import dataclasses
import datetime
import json
import math
import os
import re
import statistics
import subprocess

from asf import ci_pool, env

QUEUE_FILE = 'ci-queue.json'
KINDS = ('pr', 'trunk', 'batch', 'deploy')
MODES = ('on', 'dry-run', 'off')
FIELDS = ('mode', 'history', 'workflows', 'trunk_wait_min', 'pr_wait_min')
DEFAULT_HISTORY = 10
DEFAULT_TRUNK_WAIT_MIN = 20
DEFAULT_PR_WAIT_MIN = 45
#: a run the relief cancelled and could not re-run in this long is dropped from the file
RELIEF_TTL_S = 24 * 60 * 60
#: an entry not asked about again in this long has left the line (its caller moved on)
STALE_S = 30 * 60
#: a run admitted this recently still holds its runners: its jobs queue before a runner is busy
PICKUP_S = 3 * 60
#: how long a workflow's measured jobs per class are reused before its run ids are listed again
EXPECT_TTL_S = 10 * 60
#: the measure's version: a cached figure from another version is read again
EXPECT_VERSION = 4
#: the run conclusions measured: a cancelled (or otherwise cut short) run says nothing of its size
MEASURED_CONCLUSIONS = frozenset({'success', 'failure'})
GH_TIMEOUT_S = 30
S1, TRUNK, FEATURE, OTHER = 0, 1, 2, 3


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(stamp):
    """A host or queue-file stamp as a UTC-aware time: ``2026-09-25T19:04:00Z`` (the host's
    ``createdAt``), fractions and ``+hh:mm`` offsets too; a stamp with no zone is UTC. Never read
    as local time — a wait is always aware-now minus aware-stamp."""
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


def _age(stamp, now):
    t = _parse(stamp)
    return (now - t).total_seconds() if t else float('inf')


# ---- configuration ----------------------------------------------------------------------------

def _qcfg(product):
    ci = product.ci if isinstance(getattr(product, 'ci', None), dict) else {}
    q = ci.get('queue')
    return q if isinstance(q, dict) else {}


def mode(product):
    """``on``, ``dry-run`` or ``off``: ``off`` for a product with no ``ci.pool`` (today's
    behaviour), else ``ci.queue.mode`` (default ``on``)."""
    if not ci_pool.load_pool(product):
        return 'off'
    m = _qcfg(product).get('mode')
    if m is True or m is None:      # YAML reads a bare ``on`` as true
        return 'on'
    if m is False:                  # … and ``off`` as false
        return 'off'
    m = str(m).strip().lower()
    return m if m in MODES else 'on'


def history(product):
    v = _qcfg(product).get('history')
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else DEFAULT_HISTORY


def trunk_wait_min(product):
    """``ci.queue.trunk_wait_min``: minutes a queued trunk run waits before relief (default 20)."""
    v = _qcfg(product).get('trunk_wait_min')
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    return v if ok else DEFAULT_TRUNK_WAIT_MIN


def pr_wait_min(product):
    """``ci.queue.pr_wait_min``: minutes an ordinary PR start waits before the starvation guard
    admits it on half its expected jobs (default 45)."""
    v = _qcfg(product).get('pr_wait_min')
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    return v if ok else DEFAULT_PR_WAIT_MIN


def workflow_for(product, kind, default=None):
    """The workflow a ``kind`` of start triggers: ``ci.queue.workflows.<kind>``, else ``default``
    (a deploy passes its own), else ``ci.workflow``."""
    wf = _qcfg(product).get('workflows')
    if isinstance(wf, dict) and wf.get(kind):
        return str(wf[kind])
    ci = product.ci if isinstance(getattr(product, 'ci', None), dict) else {}
    return default or ci.get('workflow') or None


def config_problems(ci):
    """``[(dotted key, problem)]`` for a ``ci.queue`` the queue cannot read."""
    if not isinstance(ci, dict) or ci.get('queue') is None:
        return []
    q = ci['queue']
    if not isinstance(q, dict):
        return [('ci.queue', f"must be a map {{{', '.join(FIELDS)}}}, not {q!r}")]
    out = []
    for k in q:
        if k not in FIELDS:
            out.append((f'ci.queue.{k}', f"is not a field of ci.queue ({', '.join(FIELDS)})"))
    if q.get('mode') is not None and str(q['mode']).strip().lower() not in MODES \
            and q['mode'] is not True and q['mode'] is not False:
        out.append(('ci.queue.mode', f"must be one of {', '.join(MODES)}, not {q['mode']!r}"))
    for k in ('trunk_wait_min', 'pr_wait_min'):
        w = q.get(k)
        if w is not None and (isinstance(w, bool) or not isinstance(w, (int, float)) or w <= 0):
            out.append((f'ci.queue.{k}', f'must be a number of minutes > 0, not {w!r}'))
    h = q.get('history')
    if h is not None and (isinstance(h, bool) or not isinstance(h, int) or h < 1):
        out.append(('ci.queue.history', f'must be a whole number >= 1, not {h!r}'))
    wf = q.get('workflows')
    if wf is not None:
        if not isinstance(wf, dict):
            out.append(('ci.queue.workflows', f'must be a map of {", ".join(KINDS)}, not {wf!r}'))
        else:
            for k in wf:
                if k not in KINDS:
                    out.append((f'ci.queue.workflows.{k}', f"is not a start kind ({', '.join(KINDS)})"))
    return out


# ---- priority ---------------------------------------------------------------------------------

def _feature_of(items, item):
    seen = set()
    while isinstance(item, dict) and item.get('id') not in seen:
        seen.add(item.get('id'))
        if item.get('type') == 'feature':
            return item
        item = items.get(item.get('parent')) or items.get(item.get('feature') or '')
    return None


def _touches(files, paths):
    import fnmatch
    for f in files or ():
        for p in paths or ():
            p = str(p)
            if fnmatch.fnmatch(f, p) or f.startswith(p.rstrip('*').rstrip('/') + '/') or f == p:
                return True
    return False


def priority(item_id, items=None, branch='', files=(), product=None, kind=None):
    """``(rank, label)``: 0 for an S1 or hotfix item, 1 for a trunk run (``kind`` ``trunk``: every
    deploy waits on a green trunk), 2 for a customer-facing Feature's PR, 3 for the rest;
    ``label`` is what the hold line names (``S1``, ``hotfix``, ``trunk``, ``Feature``, the
    severity, or the item type)."""
    items = items or {}
    card = items.get(item_id or '') or {}
    sev = card.get('severity')
    if 'hotfix' in (branch or '').lower() or 'hotfix' in str(card.get('type') or '').lower():
        return S1, 'hotfix'
    if sev == 'S1':
        return S1, 'S1'
    if kind == 'trunk':
        return TRUNK, 'trunk'
    feature = _feature_of(items, card) if card else None
    if feature is not None:
        cust = feature.get('customer_facing') is True or card.get('customer_facing') is True
        if not cust and product is not None:
            cust = _touches(files, getattr(product, 'customer_paths', None) or [])
        if cust:
            return FEATURE, 'Feature'
    return OTHER, sev or (str(card.get('type')).capitalize() if card.get('type') else 'other')


# ---- the CI host ------------------------------------------------------------------------------

def class_key(entry):
    """The class a pool runner counts under: its ``class``, else its ``role``."""
    return entry.cls or entry.role


def _label_key(labels, roles):
    labels = [ci_pool._norm(l) for l in labels or ()]
    for l in labels:
        if l.startswith(ci_pool.CLASS_PREFIX):
            return l[len(ci_pool.CLASS_PREFIX):]
    for l in labels:
        if l in roles:
            return l
    return None


class Source:
    """What the queue reads off the CI host. :class:`GitHubSource` is the one in use; a test
    passes it a fake ``gh`` (``run=``)."""

    def runners(self):
        raise NotImplementedError

    def run_ids(self, workflow, n):
        """The last ``n`` runs of ``workflow`` that completed ``success`` or ``failure``, newest
        first: ``[(run id, latest attempt)]``, or None when unreadable."""
        raise NotImplementedError

    def attempt_jobs(self, run_id, attempt):
        """One attempt's jobs: ``[{runner_name, labels, conclusion, started_at, completed_at,
        run_attempt}]``, or None when unreadable."""
        raise NotImplementedError

    def run_jobs(self, workflow, n):
        """The latest attempt's jobs of each of :meth:`run_ids`."""
        ids = self.run_ids(workflow, n)
        if ids is None:
            return None
        return [j for j in (self.attempt_jobs(i, a) for i, a in ids) if j is not None]

    def inflight(self):
        """Runs of ``ci.workflow`` not completed, or None when unknown."""
        raise NotImplementedError


class GitHubSource(Source):
    def __init__(self, product, run=None):
        self.product = product
        self._run = run or subprocess.run
        self.backend = ci_pool.GitHubBackend(product, run=self._run)
        self.slug = product.repo_slug

    def _gh(self, args):
        try:
            p = self._run(['gh', *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S,
                          env=ci_pool._gh_env(self.product))
        except (OSError, subprocess.TimeoutExpired):
            return None
        return p.stdout if p.returncode == 0 else None

    def runners(self):
        return self.backend.runners()

    def run_ids(self, workflow, n):
        if not self.slug or not workflow:
            return None
        # cancelled runs are listed too and dropped here: list enough to still find ``n``
        text = self._gh(['run', 'list', '-R', self.slug, '--workflow', workflow, '--status',
                         'completed', '--limit', str(max(n * 3, 20)), '--json',
                         'databaseId,conclusion,attempt'])
        try:
            listed = [r for r in json.loads(text or 'null') if isinstance(r, dict)]
        except (TypeError, ValueError):
            return None
        out = []
        for r in listed:
            if r.get('databaseId') and r.get('conclusion') in MEASURED_CONCLUSIONS:
                a = r.get('attempt')
                ok = isinstance(a, int) and not isinstance(a, bool) and a >= 1
                out.append((r['databaseId'], a if ok else 1))
        return out[:n]

    def attempt_jobs(self, run_id, attempt):
        text = self._gh(['api', f'repos/{self.slug}/actions/runs/{run_id}/attempts/{attempt}/'
                                'jobs?per_page=100',
                         '--jq', '.jobs[] | {id, runner_name, labels, conclusion, started_at, '
                         'completed_at, run_attempt}'])
        if text is None:
            return None
        try:
            return [json.loads(l) for l in text.splitlines() if l.strip()]
        except ValueError:
            return None

    def inflight(self):
        from asf import capacity
        return capacity.ci_runs_in_flight(self.product, run=self._run, timeout=GH_TIMEOUT_S)


def label_classes(pool, runners=()):
    """``{label: class}``: the class a runner label stands for. A label names a class when every
    runner carrying it — the pool's declared labels (role, ``class-<name>``) and the live runners'
    labels, each runner in the class it is declared in — sits in that one class. So a sub-label
    (``fast-heavy``, carried only by ``heavy`` runners) maps to ``heavy`` and never becomes a class
    of its own; a label spanning classes (``self-hosted``, a provider) maps to none."""
    by_name = {e.runner: e for e in pool}
    seen = {}
    for e in pool:
        for l in e.labels():
            seen.setdefault(ci_pool._norm(l), set()).add(class_key(e))
    for r in runners or ():
        e = by_name.get(getattr(r, 'name', None))
        if e is None:
            continue
        for l in getattr(r, 'labels', None) or ():
            seen.setdefault(ci_pool._norm(l), set()).add(class_key(e))
    out = {l: next(iter(c)) for l, c in seen.items()
           if len(c) == 1 and l not in ci_pool.DEFAULT_LABELS}
    for e in pool:                       # an explicit class label always names its class
        if e.cls:
            out[ci_pool.CLASS_PREFIX + e.cls] = class_key(e)
    return out


def _as_label_map(labels):
    """A ``{label: class}`` map, from a map or from a plain set of role labels (role → itself)."""
    if isinstance(labels, dict):
        return labels
    return {ci_pool._norm(l): ci_pool._norm(l) for l in labels or ()}


def _job_class(job, by_name, labels):
    """The class of the runner that ran ``job``: the pool entry by ``runner_name``; a runner
    outside the pool, by the job's labels through ``labels`` (:func:`label_classes`) — one class
    however many of its labels map there, None when they map to none or disagree."""
    e = by_name.get(job.get('runner_name') or '')
    if e:
        return class_key(e)
    names = [ci_pool._norm(l if isinstance(l, str) else (l or {}).get('name', ''))
             for l in job.get('labels') or ()]
    for l in names:
        if l.startswith(ci_pool.CLASS_PREFIX) and l in labels:
            return labels[l]
    found = {labels[l] for l in names if l in labels}
    return found.pop() if len(found) == 1 else None


def peak_concurrent(jobs, by_name, labels):
    """``{class: peak jobs running at once}`` for one run's ``jobs``. A job counts only when it
    got a runner (``runner_name`` set) and did not end ``skipped``, once (a job id listed twice is
    one job), in one class (:func:`_job_class`); it runs from ``started_at`` to ``completed_at``
    (no ``completed_at``: to the end of the run; no ``started_at``: the whole run). A job ending
    as another starts does not overlap it, so sequential stages count once. ``labels`` is a
    :func:`label_classes` map (or the pool's role labels)."""
    labels = _as_label_map(labels)
    events = {}
    ids = set()
    for j in jobs or ():
        if not isinstance(j, dict) or not j.get('runner_name') or j.get('conclusion') == 'skipped':
            continue
        jid = j.get('id')
        if jid is not None:
            if jid in ids:
                continue
            ids.add(jid)
        key = _job_class(j, by_name, labels)
        if key is None:
            continue
        start = _parse(j.get('started_at'))
        end = _parse(j.get('completed_at')) if start is not None else None
        lo = start.timestamp() if start is not None else float('-inf')
        hi = end.timestamp() if end is not None else float('inf')
        hi = max(hi, lo)
        ev = events.setdefault(key, [])
        ev.append((lo, 1))
        ev.append((hi, 0))       # at an equal time an end (0) sorts before a start (1)
    out = {}
    for key, ev in events.items():
        n = peak = 0
        for _t, is_start in sorted(ev):
            n += 1 if is_start else -1
            peak = max(peak, n)
        out[key] = peak
    return out


def _median(values):
    """The median of ``values`` (not empty), rounded up to whole jobs."""
    return math.ceil(statistics.median(values))


def latest_attempt(jobs):
    """``jobs`` of one run, only those of its latest attempt (``run_attempt``): a superseded
    attempt's jobs never count, so attempts never overlap. Jobs naming no attempt all stay."""
    jobs = [j for j in jobs or () if isinstance(j, dict)]
    tried = [j['run_attempt'] for j in jobs if isinstance(j.get('run_attempt'), int)]
    if not tried:
        return jobs
    last = max(tried)
    return [j for j in jobs if j.get('run_attempt') in (None, last)]


def needs_from_history(runs, pool, runners=()):
    """``{class: expected jobs}`` from ``runs`` (each a list of jobs ``{id, runner_name, labels,
    conclusion, started_at, completed_at, run_attempt}``): per class the median over the runs of
    each run's peak concurrent jobs (:func:`peak_concurrent`) in its latest attempt
    (:func:`latest_attempt`), capped at the class's declared slots. Each job counts in the class
    of the runner that ran it; ``runners`` (the live runner read) widens the label map for a
    runner outside the pool. A job on a runner outside every class (a hosted runner) needs
    nothing of the pool."""
    if not runs:
        return {}
    by_name = {e.runner: e for e in pool}
    roles = label_classes(pool, runners)
    cap = {}
    for e in pool:
        cap[class_key(e)] = cap.get(class_key(e), 0) + e.slots
    peaks = [{k: n for k, n in peak_concurrent(latest_attempt(jobs), by_name, roles).items()
              if k in cap} for jobs in runs]
    out = {}
    for key in sorted({k for c in peaks for k in c}):
        n = _median([c.get(key, 0) for c in peaks])
        if n > 0:
            out[key] = min(n, cap[key])
    return out


def load_by_class(runners, pool):
    """``{class: {'online': slots, 'busy': slots}}`` for every class the pool declares, from one
    runner read. A busy runner holds all its slots. The one count :func:`free_by_class` (the
    queue) and the status view's Runners row both read, so the two never disagree."""
    by_name = {e.runner: e for e in pool}
    roles = set(ci_pool.roles(pool))
    out = {class_key(e): {'online': 0, 'busy': 0} for e in pool}
    for r in runners:
        if not r.online:
            continue
        e = by_name.get(r.name)
        key = class_key(e) if e else _label_key(r.labels, roles)
        if key in out:
            n = e.slots if e else 1
            out[key]['online'] += n
            if r.busy:
                out[key]['busy'] += n
    return out


def free_by_class(runners, pool):
    """``{class: free slots}`` for every class the pool declares: online, not busy runners."""
    return {c: v['online'] - v['busy'] for c, v in load_by_class(runners, pool).items()}


def runners_text(runners, pool):
    """The status view's Runners row from one runner read: ``19 online · heavy 12/12 busy ·
    light 4/7 busy`` per class of the pool, a plain ``n online, n busy, n idle`` total when the
    product declares no classes; ``, n offline`` when any are."""
    on = [r for r in runners if r.online]
    off = len(runners) - len(on)
    tail = f", {off} offline" if off else ""
    load = load_by_class(runners, pool)
    if not load:
        busy = sum(1 for r in on if r.busy)
        return f"{len(on)} online, {busy} busy, {len(on) - busy} idle" + tail
    total = sum(v['online'] for v in load.values())
    total += sum(1 for r in on if r.name not in {e.runner for e in pool}
                 and _label_key(r.labels, set(ci_pool.roles(pool))) not in load)
    parts = [f"{c} {v['busy']}/{v['online']} busy" for c, v in sorted(load.items())]
    return ' · '.join([f"{total} online", *parts]) + tail


# ---- the queue file ---------------------------------------------------------------------------

def _path(name):
    return os.path.join(env.state_dir(name), QUEUE_FILE)


def load(name):
    try:
        with open(_path(name), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for k in ('entries', 'started', 'expect', 'relief'):
        kind = list if k in ('started', 'relief') else dict
        if not isinstance(data.get(k), kind):
            data[k] = kind()
    return data


def save(name, data):
    path = _path(name)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def prune(data, now):
    data['entries'] = {k: v for k, v in data['entries'].items()
                       if _age(v.get('seen'), now) <= STALE_S}
    data['started'] = [s for s in data['started'] if _age(s.get('at'), now) <= PICKUP_S]
    return data


def line_order(entries):
    """The entries' keys in line order: priority, then oldest first."""
    return sorted(entries, key=lambda k: (entries[k].get('prio', OTHER),
                                          entries[k].get('since') or '', k))


def _ordinal(n):
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


@dataclasses.dataclass
class Decision:
    admitted: bool
    line: str = ''
    #: True when the product is not queued (no pool, ``mode: off``): the caller goes as before
    bypass: bool = False


def ceiling_applies(entry):
    """Do ``capacity.ci`` and the runner fit hold this start? Only a batch or an ordinary PR start
    (below S1 and trunk rank): an S1 or hotfix start, a trunk run and a deploy are exempt from
    both — PR runs in flight must never hold the trunk run every deploy waits on, and the host
    queues an exempt run's jobs itself, so it reserves no runners up front."""
    entry = entry or {}
    return entry.get('kind') in ('pr', 'batch') and entry.get('prio', OTHER) > TRUNK


def ceiling_reason(inflight, ceiling, admitted=0):
    """The hold's reason at the ceiling, from the one in-flight count
    (:func:`asf.capacity.ci_runs_in_flight`) — the status row renders it with the same count."""
    from asf import capacity
    extra = f' + {admitted} started this pass' if admitted else ''
    return (f'at the ci ceiling ({capacity.ci_inflight_text(inflight)}{extra}; batch and PR '
            f'starts below {ceiling})')


def fit_reason(short):
    """A runner-fit hold's reason from ``[(class, free, needs)]``."""
    return ', '.join(f'{c} {a} free, needs {n}' for c, a, n in short)


def shortfall(key, order, needs_of, free, half=False):
    """``[(class, free, needs)]`` for every class ``key`` needs more of than is free once the
    entries ahead of it in ``order`` are served; ``[]`` when it fits. ``half``: it fits on half
    its needs per class, rounded up (the starvation guard)."""
    avail = dict(free)
    for other in order:
        if other == key:
            break
        for c, n in (needs_of(other) or {}).items():
            if c in avail:
                avail[c] = max(0, avail[c] - n)
    want = (lambda n: math.ceil(n / 2)) if half else (lambda n: n)
    return [(c, avail.get(c, 0), n) for c, n in sorted((needs_of(key) or {}).items())
            if c in avail and want(n) > avail[c]]


def starved(entry, now, wait_min):
    """Has an ordinary PR start waited in line past ``wait_min`` minutes? (the starvation guard)"""
    entry = entry or {}
    if now is None or wait_min is None or entry.get('kind') != 'pr':
        return False
    if _parse(entry.get('since')) is None:
        return False
    return _age(entry.get('since'), now) > wait_min * 60


def decide(key, order, entries, needs_of, free, ceiling=None, inflight=None, admitted=0,
           now=None, pr_wait_min=None):
    """Pure: may ``key`` start now? ``(ok, why)``. ``order`` is the line; ``needs_of(key)`` the
    entry's ``{class: jobs}``; ``free`` the free slots per class (None: unknown — no runner
    check); ``ceiling``/``inflight`` the CI ceiling and runs in flight (None: no ceiling);
    ``admitted`` runs this pass already started. The ceiling and the runner fit both hold only a
    start :func:`ceiling_applies` to: an S1, hotfix, trunk or deploy start always goes. An
    ordinary PR start waiting past ``pr_wait_min`` (at ``now``) fits on half its expected jobs
    per class; that admission's ``why`` names the guard (the only admission with a ``why``)."""
    e = entries.get(key)
    if not ceiling_applies(e):
        return True, ''
    if ceiling is not None and inflight is not None and inflight + admitted >= ceiling:
        return False, ceiling_reason(inflight, ceiling, admitted)
    if free is None:
        return True, ''
    short = shortfall(key, order, needs_of, free)
    if not short:
        return True, ''
    if starved(e, now, pr_wait_min) and not shortfall(key, order, needs_of, free, half=True):
        waited = _dur(_age((e or {}).get('since'), now))
        return True, (f'starvation guard — waited {waited} (> {pr_wait_min}m), '
                      f'{fit_reason(short)}; half is free')
    return False, fit_reason(short)


class Queue:
    """One product's queue for one caller's pass: reads the host once, answers :meth:`admit`
    for each start, and writes the file after each answer."""

    def __init__(self, product, source=None, now=None, out=print, inflight=None, write=True):
        self.product = product
        self.mode = mode(product)
        self.pool = ci_pool.load_pool(product) if self.mode != 'off' else []
        self._source = source
        self.now = now or _now()
        self.out = out
        self._inflight = inflight
        self._free = None
        self._runners = ()
        self._read = False
        self.write = write and self.mode == 'on'
        self.data = prune(load(product.name), self.now) if self.mode != 'off' else None
        self.admitted_here = 0
        #: each workflow's expected jobs, measured once per pass: every line of it agrees
        self._needs = {}

    @property
    def source(self):
        if self._source is None:
            self._source = GitHubSource(self.product)
        return self._source

    def _read_host(self):
        if self._read:
            return
        self._read = True
        try:
            self._runners = self.source.runners()
            self._free = free_by_class(self._runners, self.pool)
        except ci_pool.BackendError:
            self._runners, self._free = (), None
        if self._inflight is None and self.ceiling() is not None:
            try:
                self._inflight = self.source.inflight()
            except Exception:  # noqa: BLE001 — an unreadable count never blocks
                self._inflight = None

    def ceiling(self):
        if hasattr(self, '_ceiling'):
            return self._ceiling
        from asf import capacity
        try:
            cfg = env.load_config()
        except Exception:  # noqa: BLE001 — no config: the product's own figure alone
            cfg = {}
        p, _src = capacity.product_ci(self.product, cfg)
        t = capacity.total_ci(cfg)
        vals = [v for v in (p, t) if v is not None]
        self._ceiling = min(vals) if vals else None
        return self._ceiling

    def needs(self, workflow):
        """The expected jobs per class of one run of ``workflow``: memoised for this pass, cached
        in the file keyed by the run ids measured (see the module doc)."""
        if not workflow:
            return {}
        if workflow not in self._needs:
            self._needs[workflow] = self._measure(workflow)
        return dict(self._needs[workflow])

    def _measure(self, workflow):
        cached = self.data['expect'].get(workflow)
        if not (isinstance(cached, dict) and cached.get('v') == EXPECT_VERSION):
            cached = None
        if cached is not None and _age(cached.get('at'), self.now) <= EXPECT_TTL_S:
            return dict(cached.get('needs') or {})
        ids = self.source.run_ids(workflow, history(self.product))
        if ids is None:
            return dict(cached.get('needs') or {}) if cached is not None else {}
        key = [int(i) for i, _a in ids]
        if cached is not None and cached.get('ids') == key:
            cached['at'] = _iso(self.now)       # the same runs: the same number
            return dict(cached.get('needs') or {})
        runs = [j for j in (self.source.attempt_jobs(i, a) for i, a in ids) if j is not None]
        self._read_host()           # the live labels map a runner outside the pool to its class
        n = needs_from_history(runs, self.pool, self._runners or ())
        self.data['expect'][workflow] = {'at': _iso(self.now), 'needs': n, 'runs': len(runs),
                                         'ids': key, 'v': EXPECT_VERSION}
        return n

    def free(self):
        self._read_host()
        if self._free is None:
            return None
        free = dict(self._free)
        for s in self.data['started']:
            for c, n in (s.get('needs') or {}).items():
                if c in free:
                    free[c] = max(0, free[c] - n)
        return free

    def admit(self, key, kind, item=None, prio=OTHER, label='other', workflow=None):
        """May the start ``key`` go now? Enqueues it (keeping its place), decides, and on
        admission moves it to ``started``. A hold prints its one line."""
        if self.mode == 'off':
            return Decision(True, bypass=True)
        workflow = workflow or workflow_for(self.product, kind)
        entries = self.data['entries']
        e = entries.get(key) or {'since': _iso(self.now)}
        e.update(kind=kind, item=item or key, prio=prio, label=label, workflow=workflow,
                 seen=_iso(self.now))
        entries[key] = e
        order = line_order(entries)
        needs = {k: self.needs(entries[k].get('workflow')) for k in order}
        self._read_host()
        free = self.free()
        ok, why = decide(key, order, entries, needs.get, free, self.ceiling(),
                         self._inflight, self.admitted_here, now=self.now,
                         pr_wait_min=pr_wait_min(self.product))
        pos = order.index(key) + 1
        if ok:
            if why:                 # the starvation guard: one line naming the wait
                self.out(f"ci queue: {e['item']} starts — {why} ({label}, {_ordinal(pos)} in line)")
            entries.pop(key, None)
            self.data['started'].append({'key': key, 'at': _iso(self.now), 'needs': needs[key]})
            self.admitted_here += 1
            decision = Decision(True, '')
        else:
            line = f"ci queue: {e['item']} waits — {why} ({label}, {_ordinal(pos)} in line)"
            e['why'] = line
            e['at_ceiling'] = why.startswith('at the ci ceiling')
            # a fit hold keeps its free count per class; the needs are re-read from the one
            # cached estimate wherever the hold is shown again (:func:`status_clause`)
            e['free'] = ({c: a for c, a, _n in shortfall(key, order, needs.get, free)}
                         if not e['at_ceiling'] and free is not None else None)
            if self.mode == 'dry-run':
                self.out(line.replace('ci queue:', 'ci queue (dry-run):', 1)
                         .replace(' waits — ', ' would wait — ', 1))
                decision = Decision(True, line)
            else:
                self.out(line)
                decision = Decision(False, line)
        if self.write:
            save(self.product.name, self.data)
        return decision


def admit(product, key, kind, item=None, items=None, branch='', files=(), workflow=None,
          source=None, out=print, inflight=None, queue=None):
    """One start's question, for a caller with no :class:`Queue` of its own."""
    q = queue or Queue(product, source=source, out=out, inflight=inflight)
    if q.mode == 'off':
        return Decision(True, bypass=True)
    prio, label = priority(item, items, branch, files, product, kind=kind)
    return q.admit(key, kind, item=item, prio=prio, label=label, workflow=workflow)


#: the statuses of a run that has not reached a runner yet
QUEUED_STATUSES = frozenset({'queued', 'waiting', 'pending', 'requested'})


def trunk_workflows(product):
    ci = product.ci if isinstance(getattr(product, 'ci', None), dict) else {}
    return sorted({w for w in (workflow_for(product, 'trunk'), ci.get('workflow')) if w})


def cancel_superseded(product, source=None, out=print, dry_run=False):
    """Cancel the trunk's superseded queued runs: per trunk workflow, every ``push`` run on the
    trunk still queued while a newer queued-or-running one exists. One line per cancel; the
    number cancelled. Not a queued product (no ``ci.pool``, ``mode: off``): nothing, no ``gh``
    call. ``dry_run`` (or ``mode: dry-run``) names what it would cancel. Never raises."""
    m = mode(product)
    if m == 'off' or not product.repo_slug:
        return 0
    dry_run = dry_run or m == 'dry-run'
    src = source or GitHubSource(product)
    trunk = getattr(product, 'main', None) or 'main'
    n = 0
    for wf in trunk_workflows(product):
        text = src._gh(['run', 'list', '-R', product.repo_slug, '--workflow', wf, '--branch',
                        trunk, '--event', 'push', '--limit', '50', '--json',
                        'databaseId,status,createdAt,headSha'])
        try:
            runs = [r for r in json.loads(text or 'null') or ()
                    if isinstance(r, dict) and r.get('status') != 'completed']
        except (TypeError, ValueError):
            continue
        if len(runs) < 2:
            continue
        runs.sort(key=lambda r: (str(r.get('createdAt') or ''), int(r.get('databaseId') or 0)))
        newest = runs[-1]
        for r in runs[:-1]:
            if r.get('status') not in QUEUED_STATUSES or not r.get('databaseId'):
                continue
            what = (f"ci queue: {'would cancel' if dry_run else 'cancelled'} superseded {trunk} "
                    f"run {r['databaseId']} ({wf} at {str(r.get('headSha') or '?')[:9]}) — "
                    f"run {newest.get('databaseId')} at {str(newest.get('headSha') or '?')[:9]} "
                    f"judges it")
            if dry_run:
                out(what)
                continue
            if src._gh(['run', 'cancel', str(r['databaseId']), '-R', product.repo_slug]) is None:
                out(f"ci queue: cancel of superseded {trunk} run {r['databaseId']} refused")
                continue
            out(what)
            n += 1
    return n


#: an item id in a branch name (``task/T-0341-…``)
_ITEM_IN_BRANCH_RE = re.compile(r'\b[A-Z]-\d{4,}\b')
_RUN_FIELDS = 'databaseId,status,event,headBranch,headSha,createdAt,startedAt'


def _dur(seconds):
    m = max(0, int(seconds // 60))
    return f'{m // 60}h{m % 60:02d}m' if m >= 60 else f'{m}m'


def _list_runs(src, product, workflow):
    text = src._gh(['run', 'list', '-R', product.repo_slug, '--workflow', workflow, '--limit',
                    '100', '--json', _RUN_FIELDS])
    try:
        return [r for r in json.loads(text or 'null') or () if isinstance(r, dict)]
    except (TypeError, ValueError):
        return None


def _covered(need, free, freed):
    return all(free.get(c, 0) + freed.get(c, 0) >= n for c, n in need.items())


def _rerun_cancelled(q, src, product, trunk_run, dry_run, out, now):
    """Re-run every run the relief cancelled, each through the queue at its original priority,
    now that the trunk run has started. The number re-run."""
    relief, keep, n = q.data['relief'], [], 0
    trunk = getattr(product, 'main', None) or 'main'
    for rec in sorted(relief, key=lambda r: (r.get('prio', OTHER), r.get('at') or '')):
        rid = rec.get('id')
        if not rid or _age(rec.get('at'), now) > RELIEF_TTL_S:
            out(f"ci queue: dropped cancelled {rec.get('kind')} run {rid} ({rec.get('item')}) — "
                f"not re-run within {RELIEF_TTL_S // 3600}h")
            continue
        d = q.admit(f'rerun:{rid}', rec.get('kind') or 'pr', item=rec.get('item'),
                    prio=rec.get('prio', OTHER), label=rec.get('label') or 'other',
                    workflow=rec.get('workflow'))
        if not d.admitted or d.line:        # held (or a dry-run mode hold): next tick asks again
            keep.append(rec)
            continue
        began = _parse((trunk_run or {}).get('startedAt')) or now
        waited = _dur((began - (_parse(rec.get('trunk_created')) or began)).total_seconds())
        what = (f"re-ran {rec.get('kind')} run {rid} ({rec.get('item')}, {rec.get('label')}) — "
                f"{trunk} run {rec.get('trunk_id')} at {str(rec.get('trunk_sha') or '?')[:9]} "
                f"started after waiting {waited}")
        if dry_run:
            out(f'ci queue: would have {what}')
            keep.append(rec)
            continue
        if src._gh(['run', 'rerun', str(rid), '-R', product.repo_slug]) is None:
            out(f"ci queue: re-run of cancelled {rec.get('kind')} run {rid} refused — "
                f"tried again next tick")
            keep.append(rec)
            continue
        out(f'ci queue: {what}')
        n += 1
    q.data['relief'] = keep
    return n


def relieve_trunk(product, items=None, source=None, out=print, dry_run=False, now=None):
    """Trunk starvation relief (see the module doc): cancel queued runs ahead of a trunk run
    queued past ``ci.queue.trunk_wait_min``, lowest priority first, until the free runners plus
    the freed ones cover its expected jobs per class; re-run them once it has started.
    ``(cancelled, re-run)``. Not a queued product: nothing, no ``gh`` call. Never raises."""
    m = mode(product)
    if m == 'off' or not product.repo_slug:
        return 0, 0
    dry_run = dry_run or m == 'dry-run'
    now = (now or _now()).astimezone(datetime.timezone.utc)
    src = source or GitHubSource(product)
    q = Queue(product, source=src, now=now, out=out, write=not dry_run)
    trunk = getattr(product, 'main', None) or 'main'
    twf = workflow_for(product, 'trunk')
    runs = _list_runs(src, product, twf) if twf else None
    if runs is None:
        return 0, 0
    pushes = [r for r in runs if r.get('event') == 'push' and r.get('headBranch') == trunk]
    pushes.sort(key=lambda r: (_parse(r.get('createdAt')) or now, int(r.get('databaseId') or 0)))
    newest = pushes[-1] if pushes else None
    queued = newest is not None and newest.get('status') in QUEUED_STATUSES
    rerun = 0
    if not queued:
        if q.data['relief']:
            rerun = _rerun_cancelled(q, src, product, newest, dry_run, out, now)
            if q.write:
                save(product.name, q.data)
        return 0, rerun
    created = _parse(newest.get('createdAt'))
    if created is None:
        return 0, 0
    waited = (now - created).total_seconds()
    if waited <= trunk_wait_min(product) * 60:
        return 0, 0
    need = q.needs(twf)
    free = q.free()
    if not need or free is None or _covered(need, free, {}):
        return 0, 0     # nothing measured, runners unreadable, or the runners are there already
    done = {rec.get('id') for rec in q.data['relief']}
    pr_wf, batch_wf = workflow_for(product, 'pr'), workflow_for(product, 'batch')
    cands = []
    for wf in sorted({w for w in (pr_wf, batch_wf) if w}):
        listed = runs if wf == twf else _list_runs(src, product, wf)
        for r in listed or ():
            rid, ev, branch = r.get('databaseId'), r.get('event'), r.get('headBranch') or ''
            if (not rid or rid == newest.get('databaseId') or rid in done
                    or r.get('status') not in QUEUED_STATUSES):
                continue
            rc = _parse(r.get('createdAt'))
            if rc is None or rc >= created:     # queued behind the trunk run: not in its way
                continue
            if ev == 'pull_request' and wf == pr_wf:
                kind = 'pr'
            elif wf == batch_wf and ev != 'pull_request' and not (ev == 'push' and branch == trunk):
                kind = 'batch'
            else:
                continue
            hit = _ITEM_IN_BRANCH_RE.search(branch)
            item = hit.group(0) if hit else None
            prio, label = priority(item, items, branch, product=product)
            if prio == S1:
                continue                        # an S1 or hotfix run is never cancelled
            if kind == 'batch':
                group, label, item = 2, 'batch', 'batch'
            else:
                group = 0 if prio >= OTHER else 1
            cands.append((group, -rc.timestamp(), -int(rid), r, kind, item or branch or kind,
                          prio, label, wf))
    cands.sort(key=lambda c: c[:3])
    freed, n = {}, 0
    sha = str(newest.get('headSha') or '?')[:9]
    for _g, _t, _i, r, kind, item, prio, label, wf in cands:
        if _covered(need, free, freed):
            break
        rid = r['databaseId']
        what = (f"{kind} run {rid} ({item}, {label}) — {trunk} run {newest.get('databaseId')} at "
                f"{sha} has waited {_dur(waited)} for runners")
        if dry_run:
            out(f'ci queue: would cancel queued {what}')
        elif src._gh(['run', 'cancel', str(rid), '-R', product.repo_slug]) is None:
            out(f'ci queue: cancel of queued {kind} run {rid} refused')
            continue
        else:
            out(f'ci queue: cancelled queued {what}')
            q.data['relief'].append({
                'id': rid, 'kind': kind, 'item': item, 'prio': prio, 'label': label,
                'workflow': wf, 'at': _iso(now), 'trunk_id': newest.get('databaseId'),
                'trunk_sha': newest.get('headSha'), 'trunk_created': _iso(created)})
            n += 1
        for c, k in q.needs(wf).items():
            freed[c] = freed.get(c, 0) + k
    if q.write:
        save(product.name, q.data)
    return n, 0


# ---- reading it -------------------------------------------------------------------------------

@dataclasses.dataclass
class LiveLine:
    """The line as it stands now: what ``asf ci queue`` prints and the status row reads."""
    mode: str
    queue: 'Queue'
    order: list
    entries: dict
    needs: dict
    free: object = None
    ceiling: object = None
    inflight: object = None
    #: ``[(key, ok, why)]`` in line order
    decisions: list = dataclasses.field(default_factory=list)


def live_line(product, source=None, inflight=None, now=None):
    """Every entry's decision now, in line order, from one host read (the runners, and the runs
    in flight when ``inflight`` is not handed in) and the one cached estimate per workflow.
    Writes nothing. None when the product is not queued."""
    q = Queue(product, source=source, now=now, out=lambda _line: None, inflight=inflight,
              write=False)
    if q.mode == 'off':
        return None
    entries = q.data['entries']
    order = line_order(entries)
    line = LiveLine(q.mode, q, order, entries, {})
    if not order:
        return line
    line.needs = {k: q.needs(entries[k].get('workflow')) for k in order}
    line.free, line.ceiling = q.free(), q.ceiling()
    line.inflight = q._inflight
    line.decisions = [(k, *decide(k, order, entries, line.needs.get, line.free, line.ceiling,
                                  line.inflight, now=q.now, pr_wait_min=pr_wait_min(product)))
                      for k in order]
    return line


def _tick_stamp(name):
    """``HH:MM`` (local) of the queue file's last write — the tick that wrote the snapshot."""
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(_path(name))).strftime('%H:%M')
    except OSError:
        return '?'


def status_clause(product, now=None, inflight=None, ceiling=None, source=None):
    """The Capacity row's queue clause — ``ci queue 3, head T-0341 waits — …`` — computed live by
    :func:`live_line`, the function ``asf ci queue`` prints, in the current mode;
    ``ci queue empty`` when nothing waits; None when the product is not queued. ``inflight`` is
    the row's own count (:func:`asf.capacity.ci_runs_in_flight`), handed to the queue so the row
    never shows two counts and reads it once. When the live read fails (the runners unreadable)
    the tick's stored snapshot is shown instead, labelled ``as of tick HH:MM``."""
    m = mode(product)
    if m == 'off':
        return None
    tag = ' (dry-run)' if m == 'dry-run' else ''
    try:
        line = live_line(product, source=source, inflight=inflight, now=now)
    except Exception:  # noqa: BLE001 — an unreadable host falls back to the snapshot
        line = None
    if line is not None and (not line.order or line.free is not None):
        if not line.order:
            return f'ci queue empty{tag}'
        key, ok, why = line.decisions[0]
        head = line.entries[key]
        pos = f" ({head.get('label') or 'other'}, 1st in line)"
        said = 'would start' if ok else f'waits — {why}'
        return f"ci queue {len(line.order)}{tag}, head {head.get('item')} {said}{pos}"
    return _snapshot_clause(product, now, inflight, ceiling, tag)


def _snapshot_clause(product, now, inflight, ceiling, tag):
    """The clause from the file alone (no ``gh``): the last tick's hold lines."""
    data = prune(load(product.name), now or _now())
    entries = data['entries']
    tag = f'{tag} (as of tick {_tick_stamp(product.name)})'
    if not entries:
        return f'ci queue empty{tag}'
    head = entries[line_order(entries)[0]]
    why = str(head.get('why') or f"ci queue: {head.get('item')} waits").split('ci queue: ', 1)[-1]
    pos = f" ({head.get('label') or 'other'}, 1st in line)"
    held_free = head.get('free')
    cached = data['expect'].get(head.get('workflow') or '')
    if (not head.get('at_ceiling') and isinstance(held_free, dict) and held_free
            and isinstance(cached, dict) and cached.get('v') == EXPECT_VERSION):
        # a fit hold re-stated from the one cached estimate the queue line reads
        need = cached.get('needs') or {}
        short = [(c, a, need[c]) for c, a in sorted(held_free.items()) if need.get(c, 0) > a]
        why = (f"{head.get('item')} waits — {fit_reason(short)}{pos}" if short else
               f"{head.get('item')} waits — the runners fit now, asked again next tick{pos}")
    elif head.get('at_ceiling') and inflight is not None and ceiling is not None:
        if inflight < ceiling or not ceiling_applies(head):
            why = f"{head.get('item')} waits — the ci ceiling has room now, asked again next tick{pos}"
        else:
            why = f"{head.get('item')} waits — {ceiling_reason(inflight, ceiling)}{pos}"
    return f'ci queue {len(entries)}{tag}, head {why}'


def header(name, m, waiting):
    """The view's first line. The command never writes — that is ``view only``; ``DRY RUN`` is
    kept for the queue's own ``dry-run`` mode (it says what would wait and holds nothing), so a
    queue that holds starts (``on``) never reads as a dry run."""
    what = 'mode DRY RUN — the queue holds nothing' if m == 'dry-run' else f'mode {m}'
    return f'== CI QUEUE {name} ({what}, {waiting} waiting; view only — nothing written)'


def cmd_queue(args, source=None, out=print):
    """``asf ci queue``: the line, in order, with each entry's decision now. Writes nothing."""
    product = env.load_product(args.product)
    m = mode(product)
    if m == 'off':
        out(f'ci queue: {product.name} is not queued (no ci.pool, or ci.queue.mode off) — '
            f'every CI start goes at once')
        return 0
    line = live_line(product, source=source)
    out(header(product.name, m, len(line.order)))
    if not line.order:
        return 0
    free, ceiling, entries = line.free, line.ceiling, line.entries
    from asf import capacity
    out(f"free: {', '.join(f'{c} {n}' for c, n in sorted((free or {}).items())) or 'unknown'}"
        + (f'; {capacity.ci_inflight_text(line.inflight)} ({capacity.CI_INFLIGHT_WHAT}); '
           f'batch and PR starts below {ceiling}' if ceiling is not None else ''))
    for i, (k, ok, why) in enumerate(line.decisions, 1):
        e = entries[k]
        need = ', '.join(f'{c} {n}' for c, n in sorted(line.needs[k].items())) or 'nothing measured'
        said = (f'would start ({why})' if why else 'would start') if ok else 'waits: ' + why
        out(f"{i}. {e.get('item')} [{e.get('kind')}, {e.get('label')}, since {e.get('since')}] "
            f"needs {need} — {said}")
    return 0


def register(ci_subparsers):
    q = ci_subparsers.add_parser('queue', help='the CI start queue: the line and what would start')
    env.add_product_arg(q)
    q.add_argument('--dry-run', action='store_true',
                   help='accepted for symmetry; the view never writes')
    q.set_defaults(run=cmd_queue)
    return q
