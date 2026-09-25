"""asf.ci_queue — one start queue per product for every CI run ASF itself starts.

Four places start a run: the lane opening a PR (the host's ``pull_request`` run), the lane merging
onto the trunk (a PR merge or a fast-forward push: the trunk's ``push`` run), the tick's ``batch``
step, and a deploy dispatch. Each asks :func:`admit` first, with a key naming the start
(``pr:<branch>``, ``trunk:<branch>``, ``batch``, ``deploy:<env>``). A start that is not admitted
is not made: the lane leaves the branch where it is (no PR opened, no merge pushed — a push the host
would turn into a run is itself the thing held), the batch step and the deploy wait, and the next
tick asks again. Its entry keeps its place in line (``since``) while it keeps asking.

**Admission.** A run starts only while

- the product's CI ceiling (``capacity.ci``, :func:`asf.capacity.product_ci` / ``total.ci``) has
  room: runs in flight below it — the hard ceiling above the queue; and
- for every runner class the run needs, the free runners are at least its expected jobs there,
  after what every entry ahead of it in line needs of that class is set aside.

*Expected jobs per class* are measured: the last ``ci.queue.history`` (default 10) completed runs
of the workflow that start triggers, their jobs grouped by the class of the runner each ran on
(its ``ci.pool`` entry's ``class``, else its ``role``; a runner outside the pool, by its
``class-<name>`` or role label), the median per run rounded up — capped at what the pool declares
for that class, so a run bigger than the class can still start once the class is idle. The figure
is cached in the queue file for :data:`EXPECT_TTL_S`. *Free runners* come from the runners API: an
online runner that is not busy, counted at its ``slots``; runs this queue admitted in the last
:data:`PICKUP_S` are subtracted too, because their jobs are queued on the host before any runner
shows busy.

**Order.** S1 and hotfix items first (0), then trunk runs (1: every deploy waits on a green
trunk, so a trunk run never queues behind PR runs), then PRs of customer-facing Features (2: the
item sits under a Feature, and the Feature says ``customer_facing: true`` or the branch touches
``customer_paths``), then everything else (3); within a priority, oldest first.

**Superseded trunk runs.** A trunk run judges every commit below it, so an older trunk run still
*queued* when a newer one exists is moot: :func:`cancel_superseded` cancels it, keeping the newest
queued-or-running ``push`` run on the trunk per workflow, one line per cancel. A run already in
progress finishes. The lane's in-process pass calls it every tick for a queued product. An entry nobody
asked about for :data:`STALE_S` leaves the line.

**Every hold is one line**: ``ci queue: T-0341 waits — heavy 0 free, needs 3 (S2, 4th in line)``.

**Configuration** (``ci.queue`` in the product file)::

    ci:
      queue:
        mode: on          # on (default with a ci.pool) | dry-run | off
        history: 10       # runs of each workflow measured
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
import statistics
import subprocess

from asf import ci_pool, env

QUEUE_FILE = 'ci-queue.json'
KINDS = ('pr', 'trunk', 'batch', 'deploy')
MODES = ('on', 'dry-run', 'off')
DEFAULT_HISTORY = 10
#: an entry not asked about again in this long has left the line (its caller moved on)
STALE_S = 30 * 60
#: a run admitted this recently still holds its runners: its jobs queue before a runner is busy
PICKUP_S = 3 * 60
#: how long a workflow's measured jobs per class are reused before they are read again
EXPECT_TTL_S = 60 * 60
GH_TIMEOUT_S = 30
S1, TRUNK, FEATURE, OTHER = 0, 1, 2, 3


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(stamp):
    try:
        return datetime.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


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
        return [('ci.queue', f'must be a map {{mode, history, workflows}}, not {q!r}')]
    out = []
    for k in q:
        if k not in ('mode', 'history', 'workflows'):
            out.append((f'ci.queue.{k}', 'is not a field of ci.queue (mode, history, workflows)'))
    if q.get('mode') is not None and str(q['mode']).strip().lower() not in MODES \
            and q['mode'] is not True and q['mode'] is not False:
        out.append(('ci.queue.mode', f"must be one of {', '.join(MODES)}, not {q['mode']!r}"))
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

    def run_jobs(self, workflow, n):
        """The last ``n`` completed runs of ``workflow``: ``[[{runner_name, labels}]]``."""
        raise NotImplementedError

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

    def run_jobs(self, workflow, n):
        if not self.slug or not workflow:
            return None
        text = self._gh(['run', 'list', '-R', self.slug, '--workflow', workflow, '--status',
                         'completed', '--limit', str(n), '--json', 'databaseId'])
        try:
            ids = [r['databaseId'] for r in json.loads(text or 'null')]
        except (TypeError, ValueError, KeyError):
            return None
        out = []
        for run_id in ids:
            text = self._gh(['api', f'repos/{self.slug}/actions/runs/{run_id}/jobs?per_page=100',
                             '--jq', '.jobs[] | {runner_name, labels}'])
            if text is None:
                continue
            try:
                out.append([json.loads(l) for l in text.splitlines() if l.strip()])
            except ValueError:
                continue
        return out

    def inflight(self):
        ci = self.product.ci if isinstance(self.product.ci, dict) else {}
        if not ci.get('workflow') or not self.slug:
            return None
        text = self._gh(['run', 'list', '-R', self.slug, '--workflow', ci['workflow'], '--limit',
                         '50', '--json', 'status', '--jq',
                         '[.[] | select(.status != "completed")] | length'])
        text = (text or '').strip()
        return int(text) if text.isdigit() else None


def needs_from_history(runs, pool):
    """``{class: expected jobs}`` from ``runs`` (each a list of ``{runner_name, labels}``): per
    class the median count per run, rounded up, capped at the class's declared slots. A job on a
    runner outside every class (a hosted runner) needs nothing of the pool."""
    if not runs:
        return {}
    by_name = {e.runner: e for e in pool}
    roles = set(ci_pool.roles(pool))
    cap = {}
    for e in pool:
        cap[class_key(e)] = cap.get(class_key(e), 0) + e.slots
    counts = []
    for jobs in runs:
        c = {}
        for j in jobs or ():
            e = by_name.get(j.get('runner_name') or '')
            key = class_key(e) if e else _label_key([l if isinstance(l, str) else l.get('name', '')
                                                     for l in j.get('labels') or ()], roles)
            if key in cap:
                c[key] = c.get(key, 0) + 1
        counts.append(c)
    out = {}
    for key in sorted({k for c in counts for k in c}):
        n = math.ceil(statistics.median([c.get(key, 0) for c in counts]))
        if n > 0:
            out[key] = min(n, cap[key])
    return out


def free_by_class(runners, pool):
    """``{class: free slots}`` for every class the pool declares: online, not busy runners."""
    by_name = {e.runner: e for e in pool}
    roles = set(ci_pool.roles(pool))
    out = {class_key(e): 0 for e in pool}
    for r in runners:
        if not r.online or r.busy:
            continue
        e = by_name.get(r.name)
        key = class_key(e) if e else _label_key(r.labels, roles)
        if key in out:
            out[key] += e.slots if e else 1
    return out


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
    for k in ('entries', 'started', 'expect'):
        if not isinstance(data.get(k), dict if k != 'started' else list):
            data[k] = {} if k != 'started' else []
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


def decide(key, order, entries, needs_of, free, ceiling=None, inflight=None):
    """Pure: may ``key`` start now? ``(ok, why)``. ``order`` is the line; ``needs_of(key)`` the
    entry's ``{class: jobs}``; ``free`` the free slots per class (None: unknown — no runner
    check); ``ceiling``/``inflight`` the CI ceiling and runs in flight (None: no ceiling)."""
    if ceiling is not None and inflight is not None and inflight >= ceiling:
        return False, f'at the ci ceiling ({inflight}/{ceiling} runs in flight)'
    if free is None:
        return True, ''
    avail = dict(free)
    for other in order:
        if other == key:
            break
        for c, n in (needs_of(other) or {}).items():
            if c in avail:
                avail[c] = max(0, avail[c] - n)
    short = [(c, avail.get(c, 0), n) for c, n in sorted((needs_of(key) or {}).items())
             if c in avail and n > avail[c]]
    if short:
        return False, ', '.join(f'{c} {a} free, needs {n}' for c, a, n in short)
    return True, ''


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
        self._read = False
        self.write = write and self.mode == 'on'
        self.data = prune(load(product.name), self.now) if self.mode != 'off' else None
        self.admitted_here = 0

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
            self._free = free_by_class(self.source.runners(), self.pool)
        except ci_pool.BackendError:
            self._free = None
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
        """The expected jobs per class of one run of ``workflow`` (cached)."""
        if not workflow:
            return {}
        cached = self.data['expect'].get(workflow)
        if isinstance(cached, dict) and _age(cached.get('at'), self.now) <= EXPECT_TTL_S:
            return dict(cached.get('needs') or {})
        runs = self.source.run_jobs(workflow, history(self.product))
        if runs is None:
            return {}
        n = needs_from_history(runs, self.pool)
        self.data['expect'][workflow] = {'at': _iso(self.now), 'needs': n, 'runs': len(runs)}
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
        inflight = self._inflight
        if inflight is not None:
            inflight += self.admitted_here
        ok, why = decide(key, order, entries, needs.get, self.free(), self.ceiling(), inflight)
        pos = order.index(key) + 1
        if ok:
            entries.pop(key, None)
            self.data['started'].append({'key': key, 'at': _iso(self.now), 'needs': needs[key]})
            self.admitted_here += 1
            decision = Decision(True, '')
        else:
            line = f"ci queue: {e['item']} waits — {why} ({label}, {_ordinal(pos)} in line)"
            e['why'] = line
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


# ---- reading it -------------------------------------------------------------------------------

def status_clause(product, now=None):
    """The Capacity row's queue clause — ``ci queue 3, head T-0341 waits — …`` — from the file
    alone (no ``gh``); ``ci queue empty`` when nothing waits; None when the product is not
    queued."""
    m = mode(product)
    if m == 'off':
        return None
    data = prune(load(product.name), now or _now())
    entries = data['entries']
    tag = ' (dry-run)' if m == 'dry-run' else ''
    if not entries:
        return f'ci queue empty{tag}'
    head = entries[line_order(entries)[0]]
    why = str(head.get('why') or f"ci queue: {head.get('item')} waits").split('ci queue: ', 1)[-1]
    return f'ci queue {len(entries)}{tag}, head {why}'


def cmd_queue(args, source=None, out=print):
    """``asf ci queue``: the line, in order, with each entry's decision now. Writes nothing."""
    product = env.load_product(args.product)
    m = mode(product)
    if m == 'off':
        out(f'ci queue: {product.name} is not queued (no ci.pool, or ci.queue.mode off) — '
            f'every CI start goes at once')
        return 0
    q = Queue(product, source=source, out=out, write=False)
    entries = q.data['entries']
    order = line_order(entries)
    out(f'== CI QUEUE {product.name} ({m}, {len(order)} waiting, DRY RUN — nothing written)')
    if not order:
        return 0
    needs = {k: q.needs(entries[k].get('workflow')) for k in order}
    free, ceiling = q.free(), q.ceiling()
    out(f"free: {', '.join(f'{c} {n}' for c, n in sorted((free or {}).items())) or 'unknown'}"
        + (f'; ceiling {q._inflight if q._inflight is not None else "?"}/{ceiling}'
           if ceiling is not None else ''))
    for i, k in enumerate(order, 1):
        e = entries[k]
        ok, why = decide(k, order, entries, needs.get, free, ceiling, q._inflight)
        need = ', '.join(f'{c} {n}' for c, n in sorted(needs[k].items())) or 'nothing measured'
        out(f"{i}. {e.get('item')} [{e.get('kind')}, {e.get('label')}, since {e.get('since')}] "
            f"needs {need} — {'would start' if ok else 'waits: ' + why}")
    return 0


def register(ci_subparsers):
    q = ci_subparsers.add_parser('queue', help='the CI start queue: the line and what would start')
    env.add_product_arg(q)
    q.add_argument('--dry-run', action='store_true',
                   help='accepted for symmetry; the view never writes')
    q.set_defaults(run=cmd_queue)
    return q
