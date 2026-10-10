"""asf.dwell — the dwell-time watchdog: every wait the factory can sit in has an owner and a
deadline (``asf watchdog``, and the tick's ``watchdog`` step).

2026-10-04: a required job cancelled by its own timeout held a PR for hours (nothing re-ran it);
a green batch on the trunk tip waited 50+ minutes behind a priority batch; status said rows were
ready while the wave launched none; an ungrantable approval hold waited for a person for ever.
Each was a state with no deadline, found by a person reading logs. This module names each such
state (:data:`STATES`), with the longest it may last, who owns it, and — only where it is safe —
what is done when it lasts longer:

=====================  =====  ===========  ==============================================
state                  max    owner        on a breach
=====================  =====  ===========  ==============================================
tick_running           15     tick         alarm (the tick's own budget ends a hung one)
pr_green_not_landing   30     merge queue  alarm, with the lane's reason
check_cancelled        10     ci           re-run the failed jobs, once per head
green_batch_blocked    10     merge queue  alarm
chain_no_cut           20     merge queue  alarm
launchable_idle        10     wave         alarm, with the wave's reason
ungrantable_hold       0      approvals    drop the hold
record_behind          15     record step  alarm
runner_offline         10     ci host      alarm
branch_no_pr           10     lane         alarm
wave_latency           2      tick         alarm (the last tick's start → wave start)
=====================  =====  ===========  ==============================================

Minutes; ``conventions.watchdog: {<state>: <minutes> | off}`` in the product file sets each one
for that product. A fact that carries its own time (a check's ``completedAt``, a lane record's
``at``, a hold's first refusal, the tick's start) is aged from it; any other is aged from the
first pass that saw it (``state/<product>/dwell.json``, cleared when the state ends).

Each breach is one ``watchdog: BREACH …`` line. Run by the tick (:func:`run_step`) it is also
a ``watchdog`` ledger event and one Bug per ``(state, key)`` (signature ``watchdog <state>:
<key>``, bumped at most once a day — :mod:`asf.tick.file_bugs`), and the two actions act. ``asf
watchdog`` (:func:`cmd_watchdog`) reports only: it acts on nothing and files nothing.
"""
import datetime
import fcntl
import json
import os
import re
import time

from asf import env

#: ``(name, minutes, owner, action, what)`` — the table above
STATES = (
    ('tick_running', 15, 'tick', None, 'a tick holds the product lock'),
    ('pr_green_not_landing', 30, 'merge queue', None, 'a PR green on its head, not landing'),
    ('check_cancelled', 10, 'ci', 'rerun', 'a required check cancelled or timed out'),
    ('green_batch_blocked', 10, 'merge queue', None,
     'a batch green on the trunk tip with a batch below it'),
    ('chain_no_cut', 20, 'merge queue', None, 'green PRs wait and the queue cuts no batch'),
    ('launchable_idle', 10, 'wave', None, 'a launchable row while a seat is free'),
    ('ungrantable_hold', 0, 'approvals', 'drop', 'an approval hold no grant can release'),
    ('record_behind', 15, 'record step', None, 'a record checkout off its origin'),
    ('runner_offline', 10, 'ci host', None, 'a CI runner offline'),
    ('branch_no_pr', 10, 'lane', None, 'a pushed branch with no PR'),
    ('wave_latency', 2, 'tick', None, "the last tick's start to its wave's start"),
)
NAMES = tuple(s[0] for s in STATES)
BY_NAME = {s[0]: s for s in STATES}
#: the product file's map of per-state limits (under ``conventions:``)
CONFIG_KEY = 'watchdog'
#: ``state/<product>/<STORE>``: first-seen times and the actions taken
STORE = 'dwell.json'
#: ``state/<product>/<TICK_STAMP>``: when the running tick took the product lock
TICK_STAMP = 'tick.started'
#: a check that ended so: contention or a runner lost, never a verdict on the code
CANCELLED = ('CANCELLED', 'TIMED_OUT')
#: the tick's own record clone (``state/<product>/record``), as the record_behind key names it
RECORD_CLONE = 'record clone'
#: how long an action's mark is kept (seconds)
ACTED_KEEP_S = 7 * 24 * 3600
_RUNNING = ('IN_PROGRESS', 'QUEUED', 'PENDING', 'WAITING', 'REQUESTED')
_RUN_ID_RE = re.compile(r'/actions/runs/(\d+)')


class Finding:
    """One fact in a watched state: ``key`` names it within the state (a PR, a branch, a
    runner), ``since`` is its own epoch time when the fact carries one (else the first pass
    that saw it ages it), ``act`` what the action needs (a PR's head and run ids)."""

    def __init__(self, state, key, detail, since=None, act=None):
        self.state, self.key, self.detail, self.since, self.act = state, key, detail, since, act
        self.age_s = 0.0
        self.limit_min = None
        self.action = ''

    @property
    def owner(self):
        return BY_NAME[self.state][2]

    @property
    def breach(self):
        return self.limit_min is not None and self.age_s >= self.limit_min * 60

    def line(self):
        act = f' → {self.action}' if self.action else ''
        return (f'watchdog: BREACH {self.state} {self.key} — {int(self.age_s // 60)} min, '
                f'limit {self.limit_min} min (owner {self.owner}) — {self.detail}{act}')

    def as_json(self):
        return {'state': self.state, 'key': self.key, 'detail': self.detail,
                'age_min': round(self.age_s / 60, 1), 'limit_min': self.limit_min,
                'owner': self.owner, 'breach': self.breach, 'action': self.action}


# ---- configuration ----------------------------------------------------------------------------

def limits(product):
    """``{state: minutes or None}`` — :data:`STATES`' defaults under the product's
    ``conventions.watchdog`` (``off`` → None: the state is not watched). A value that is not a
    number of minutes keeps the default."""
    conv = getattr(product, 'conventions', None)
    raw = conv.get(CONFIG_KEY) if conv is not None else None
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for name, minutes, *_rest in STATES:
        v = raw.get(name, minutes)
        if isinstance(v, str) and v.strip().lower() in ('off', 'false', 'no'):
            out[name] = None
        elif v is False:
            out[name] = None
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[name] = v
        else:
            out[name] = minutes
    return out


# ---- times and the store ----------------------------------------------------------------------

def parse_ts(text):
    """Epoch seconds of an ISO stamp (``Z`` or an offset), or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def store_path(product):
    return os.path.join(env.state_dir(product), STORE)


def load_store(product):
    try:
        with open(store_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    for k in ('seen', 'acted'):
        if not isinstance(data.get(k), dict):
            data[k] = {}
    return data


def save_store(product, data):
    path = store_path(product)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True, indent=1)
    os.replace(tmp, path)


def mark_tick(product, now=None):
    """Stamp the tick that just took the product lock — what ``tick_running`` ages."""
    try:
        with open(os.path.join(env.state_dir(product), TICK_STAMP), 'w', encoding='utf-8') as f:
            json.dump({'at': _iso(now or time.time()), 'pid': os.getpid()}, f)
    except OSError:
        pass


# ---- the facts the probes read (a test replaces the class) -------------------------------------

class Facts:
    """Every read the probes make, each once per pass. The host reads (``gh``, a runner list,
    ``git fetch``) are made only for a product that has what they need."""

    def __init__(self, product, root=None, now=None):
        self.product = product
        self.root = root
        self.now = now if now is not None else time.time()
        self._cache = {}

    def _once(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    @property
    def state_dir(self):
        return env.state_dir(self.product)

    def tick(self):
        """``(started epoch or None, pid)`` of a tick holding the product lock, else None."""
        from asf.tick import tick as tick_mod
        path = tick_mod.lock_path(self.product)
        if not os.path.exists(path):
            return None
        with open(path, 'a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                pass
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
                return None
        try:
            with open(os.path.join(self.state_dir, TICK_STAMP), encoding='utf-8') as f:
                stamp = json.load(f)
        except (OSError, ValueError):
            stamp = {}
        return parse_ts(stamp.get('at')), stamp.get('pid')

    def lane(self):
        """``{branch: lane record}`` off the session ledger (the lane's own states)."""
        def read():
            from asf.workers import lifecycle, pool as pool_mod
            return {b: lifecycle.lane_of(r) for b, r in
                    lifecycle.by_branch(pool_mod.sessions_path(self.product)).items()
                    if lifecycle.lane_of(r)}
        return self._once('lane', read)

    def queue_on(self):
        return bool(self.product.conventions.merge_queue())

    def batches(self):
        from asf import merge_queue
        return self._once('batches', lambda: merge_queue.load(self.state_dir)['batches'])

    def requests(self):
        from asf import merge_queue
        return self._once('requests', lambda: merge_queue.load_requests(self.state_dir))

    def required(self):
        """The landing's required check names: ``conventions.landing_checks``, else the
        trunk's protection (cached by the lane for an hour). ``()`` when none can be read."""
        def read():
            named = self.product.conventions.get('landing_checks')
            if named:
                return (str(named),) if isinstance(named, str) else tuple(str(n) for n in named)
            if not self.product.repo_slug:
                return ()
            from asf.harvest import lane as lane_mod
            return tuple(lane_mod.protected_checks(self.product.repo_slug, self.product.main,
                                                   self.state_dir))
        return self._once('required', read)

    def open_prs(self):
        """The open PRs with their check rollup, or None when unreadable (or no slug)."""
        def read():
            if not self.product.repo_slug:
                return None
            from asf import connectors
            r = connectors.forge().open_prs(self.product.repo_slug, limit=100,
                                fields=('number', 'headRefName', 'headRefOid', 'isDraft',
                                        'statusCheckRollup'))
            return r.data if r.ok and isinstance(r.data, list) else None
        return self._once('prs', read)

    def live_refs(self):
        """The branch names on origin (one ``git ls-remote --heads``), or None when unreadable
        — a batch the merge queue still lists but origin no longer has is no chain."""
        def read():
            from asf import gitops
            if not self.product.repo_dir:
                return None
            r = gitops.git(['ls-remote', '--heads', 'origin'], self.product.repo_dir,
                           timeout=60)
            if not r.ok:
                return None
            return {ln.split('\trefs/heads/', 1)[1] for ln in (r.data or '').splitlines()
                    if '\trefs/heads/' in ln}
        return self._once('live_refs', read)

    def checks_at(self, sha):
        """The check runs on ``sha``, or None when unreadable."""
        if not self.product.repo_slug:
            return None
        from asf import connectors
        r = connectors.forge().checks(self.product.repo_slug, sha)
        return r.data if r.ok else None

    def trunk_sha(self):
        """The trunk's tip on origin, or None."""
        def read():
            from asf import gitops
            if not self.product.repo_dir:
                return None
            r = gitops.git(['ls-remote', 'origin', f'refs/heads/{self.product.main}'],
                           self.product.repo_dir, timeout=60)
            return r.data.split()[0] if r.ok and r.data else None
        return self._once('trunk', read)

    def runners(self):
        """``[(name, online)]`` of the product's self-hosted runners; None when it declares
        none (``ci.runner_org`` / ``ci.pool``) or they are unreadable."""
        def read():
            ci = self.product.ci if isinstance(self.product.ci, dict) else {}
            if not (ci.get('runner_org') or ci.get('pool')):
                return None
            from asf import ci_pool
            backend = ci_pool.backend_for(self.product)
            if backend is None:
                return None
            try:
                return [(r.name, r.online) for r in backend.runners()]
            except Exception:  # noqa: BLE001 — an unreadable runner list is no finding
                return None
        return self._once('runners', read)

    def checkouts(self):
        """``[(label, path)]`` of the record checkouts to compare with their origin."""
        from asf.tick import shadow
        out = [(RECORD_CLONE, shadow.record_dir(self.product))]
        if self.product.backlog_dir:
            out.append(('backlog_dir', self.product.backlog_dir))
        return [(label, p) for label, p in out if p and os.path.isdir(os.path.join(p, '.git'))]

    def divergence(self, path, fetch=True):
        """``(ahead, behind, upstream)`` of ``path`` against its upstream (after a fetch when
        ``fetch``), or None when git cannot tell (no upstream, offline)."""
        from asf import gitops
        if fetch:
            gitops.fetch(path)
        up = gitops.git(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'], path)
        if not up.ok or not up.data:
            return None
        r = gitops.git(['rev-list', '--left-right', '--count', 'HEAD...@{u}'], path)
        parts = r.data.split() if r.ok else []
        if len(parts) != 2:
            return None
        return int(parts[0]), int(parts[1]), up.data

    def would_start(self):
        """:func:`asf.tick.step_wave.would_start` on the record, or None without one."""
        def read():
            root = self.root
            if not root or not os.path.isfile(os.path.join(root, 'index.json')):
                return None
            from asf.tick import step_wave
            return step_wave.would_start(self.product, root)
        return self._once('wave', read)

    def holds(self):
        from asf import approvals
        return approvals.open_holds(self.product)

    def rerun_ids(self):
        """Ids of the runs the CI queue's trunk/S1 relief cancelled and already holds to
        re-run on its own (:func:`asf.ci_queue.rerun_ids`) — :func:`check_cancelled` defers to
        it the way :func:`asf.harvest.lane.pr_checks` already does: that cancel is no verdict
        on the code, and watchdog re-running (or alarming on) it races the relief's own plan."""
        from asf import ci_queue
        return self._once('rerun_ids', lambda: ci_queue.rerun_ids(self.state_dir))


# ---- the probes: one per state, each a list of Findings ----------------------------------------

def tick_running(facts):
    t = facts.tick()
    if not t:
        return []
    since, pid = t
    at = _iso(since) if since else 'an unknown time'
    return [Finding('tick_running', 'tick', f'tick pid {pid or "?"} holds the lock since {at}',
                    since)]


def _green_now(rec):
    green = rec.get('green')
    return isinstance(green, dict) and green.get('head') and green.get('head') == rec.get('head')


def pr_green_not_landing(facts):
    from asf.harvest import lane as lane_mod
    out = []
    for branch, rec in sorted(facts.lane().items()):
        # QUEUED/MERGING are in a batch: the batch states watch those
        if rec.get('state') not in (lane_mod.WAITING, lane_mod.WAITING_CI, lane_mod.GATE,
                                    lane_mod.REVIEW, lane_mod.PR_OPEN) or not _green_now(rec):
            continue
        pr = rec.get('pr')
        out.append(Finding('pr_green_not_landing', f'#{pr}' if pr else branch,
                           f"{branch} green at {str(rec.get('head'))[:9]} — "
                           f"{rec.get('state')}: {rec.get('reason') or 'no reason recorded'}"))
    return out


def _latest_by_name(checks):
    """``checks``, one entry per ``name`` — the newest: a check the rollup still carries from
    before a rerun (same name, an older ``completedAt``) is never judged beside the rerun's own,
    later result (B-82658: the stale ``CANCELLED`` twin breached for ever after the rerun it
    named had already gone green). A check still running has no ``completedAt`` and is the
    newest. The list's order is kept."""
    def when(c):
        t = parse_ts(c.get('completedAt'))
        return float('inf') if t is None else t
    best = {}
    for i, c in enumerate(checks):
        name = c.get('name') or ''
        if name not in best or when(c) >= when(checks[best[name]]):
            best[name] = i
    keep = set(best.values())
    return [c for i, c in enumerate(checks) if i in keep]


def check_cancelled(facts):
    prs = facts.open_prs()
    if prs is None:
        return []
    required = facts.required()
    held = facts.rerun_ids()
    from asf.harvest import lane as lane_mod
    out = []
    for pr in prs:
        checks = [c for c in pr.get('statusCheckRollup') or () if isinstance(c, dict)]
        if pr.get('isDraft') or not checks:
            continue
        checks = _latest_by_name(checks)
        if any(str(c.get('status') or '').upper() in _RUNNING for c in checks):
            continue                           # something still runs on this head
        # a matrix leg's own run failing for real (not cancelled) fails the whole run under the
        # default fail-fast strategy: a sibling leg's CANCELLED is that collateral, not an infra
        # fluke — rerunning it only cancels it again, forever, since the real failure persists.
        failed_runs = {m.group(1) for c in checks
                       if str(c.get('conclusion') or '').upper() == 'FAILURE'
                       for m in [_RUN_ID_RE.search(str(c.get('detailsUrl') or ''))] if m}
        gone = [c for c in checks if str(c.get('conclusion') or '').upper() in CANCELLED
                and (not required or lane_mod.required_name(c.get('name'), required))
                and not any(m.group(1) in failed_runs
                            for m in [_RUN_ID_RE.search(str(c.get('detailsUrl') or ''))] if m)
                # the CI queue's relief already holds this run to re-run on its own
                and not any(m.group(1) in held
                            for m in [_RUN_ID_RE.search(str(c.get('detailsUrl') or ''))] if m)]
        if not gone:
            continue
        ended = [parse_ts(c.get('completedAt')) for c in gone]
        ended = [t for t in ended if t]
        sha = pr.get('headRefOid') or ''
        runs = sorted({m.group(1) for c in gone
                       for m in [_RUN_ID_RE.search(str(c.get('detailsUrl') or ''))] if m})
        names = ', '.join(dict.fromkeys(str(c.get('name')) for c in gone))
        out.append(Finding('check_cancelled', f"#{pr.get('number')}@{sha[:9]}",
                           f"{pr.get('headRefName')}: {names} "
                           f"{'/'.join(sorted({str(c.get('conclusion')).lower() for c in gone}))}"
                           f", nothing running", max(ended) if ended else None,
                           act={'runs': runs, 'pr': pr.get('number'), 'sha': sha}))
    return out


def green_batch_blocked(facts):
    if not facts.queue_on():
        return []
    batches = facts.batches()
    if len(batches) < 2:
        return []
    trunk = facts.trunk_sha()
    if not trunk:
        return []
    from asf import merge_queue
    out = []
    for b in batches[1:]:
        if b.get('base') != trunk:
            continue
        runs = facts.checks_at(b['sha'])
        state, _why = merge_queue.verdict(runs, facts.required()) if runs is not None \
            else ('pending', '')
        if state != 'green':
            continue
        out.append(Finding('green_batch_blocked', b['ref'],
                           f"batch {b['ref']} @ {b['sha'][:9]} is green on the trunk tip "
                           f"{trunk[:9]} and waits behind {batches[0]['ref']}"))
    return out


def chain_no_cut(facts):
    """Green PRs wait and the queue cuts no batch. Only a PR whose required checks read green on
    its exact head *now* counts (a lane record's once-green, or an ``asf land`` request that is
    merely not red, may since have a cancelled gate or a red one — 2026-10-05: four such were
    counted "green waiting"); a PR already in a batch of the chain does not wait. Only a batch
    still on origin is the chain: a dropped or landed one is never named."""
    if not facts.queue_on():
        return []
    from asf import merge_queue
    from asf.harvest import lane as lane_mod
    live = facts.live_refs()
    batches = [b for b in facts.batches() if live is None or b.get('ref') in live]
    in_chain = {str(m.get('branch')) for b in batches for m in b.get('members') or ()}
    required = facts.required()

    def green(head):
        runs = facts.checks_at(head) if head else None
        return runs is not None and merge_queue.verdict(runs, required)[0] == 'green'
    heads = {}
    for b, rec in facts.lane().items():
        if rec.get('state') in (lane_mod.WAITING, lane_mod.WAITING_CI) and _green_now(rec):
            heads[b] = rec.get('head')
    reqs = [r for r in facts.requests().values() if not r.get('red')]
    if reqs:
        by_pr = {str(p.get('number')): p.get('headRefOid') for p in facts.open_prs() or ()}
        for r in reqs:
            heads.setdefault(str(r.get('branch')), by_pr.get(str(r.get('pr'))))
    waiting = [b for b, head in heads.items() if b not in in_chain and green(head)]
    if not waiting:
        return []
    newest = max(batches, key=lambda b: str(b.get('cut_at') or ''), default=None)
    after = f"after {newest['ref']}" if newest else 'empty chain'
    return [Finding('chain_no_cut', after,
                    f"{len(waiting)} green PR(s) wait ({', '.join(sorted(waiting)[:4])}"
                    f"{' …' if len(waiting) > 4 else ''}) and no batch was cut "
                    + (f"since {newest['ref']} at {newest.get('cut_at')}" if newest
                       else '(the chain is empty)'))]


def launchable_idle(facts):
    from asf.feeder import rows as feeder_rows
    from asf.tick import step_wave
    got = facts.would_start()
    if not got:
        return []
    screened, seats, running = got
    free = seats - len(running)
    if free <= 0:
        return []
    out = []
    for s in screened:
        # the operator's own pause is not an idle seat: it holds every row on purpose, so a
        # paused product must never breach 10 minutes into the hold it asked for — B-84833
        if not s.row.launches or s.kind in (step_wave.NO_SEAT, step_wave.PAUSED):
            continue
        # the groom row speaks for the day's open questions, not for the item it happens to be
        # named after (the same exception asf.feeder.rows.hold_unlanded makes) — B-84837
        if s.row.kind in (feeder_rows.GROOM_ADJUDICATE, feeder_rows.GROOM_CLERK):
            continue
        why = s.why or "passes the wave's filter, not started yet"
        out.append(Finding('launchable_idle', s.row.item_id,
                           f'{s.row.item_id} ({s.row.kind}) — {why}; {free} seat(s) free'))
    return out


def ungrantable_hold(facts):
    from asf import approvals
    out = []
    for e in facts.holds():
        c = approvals.CLASSES_BY_NAME.get(e.get('class'))
        if c is None or c.grantable:
            continue
        hold = f"{e['item']}/{e['class']}"
        out.append(Finding('ungrantable_hold', hold,
                           f"{hold} ({e.get('level')}) — no grant can release it: "
                           f"{str(e.get('detail') or '')[:120]}", parse_ts(e.get('first')),
                           act={'hold': hold}))
    return out


def record_behind(facts):
    out = []
    for label, path in facts.checkouts():
        # the record clone is fetched by every tick's record step, under the tick's lock: it is
        # read as that fetch left it; any other checkout is fetched first
        d = facts.divergence(path, fetch=label != RECORD_CLONE)
        if not d or (d[0] == 0 and d[1] == 0):
            continue
        ahead, behind, up = d
        out.append(Finding('record_behind', label,
                           f'{label} {path}: {ahead} ahead, {behind} behind {up}'))
    return out


def runner_offline(facts):
    rs = facts.runners()
    return [Finding('runner_offline', name, f'runner {name} is offline')
            for name, online in rs or () if not online]


def branch_no_pr(facts):
    from asf.harvest import lane as lane_mod
    return [Finding('branch_no_pr', branch,
                    f"{branch} pushed at {str(rec.get('head'))[:9]}, no PR — "
                    f"{rec.get('reason') or 'no reason recorded'}", parse_ts(rec.get('at')))
            for branch, rec in sorted(facts.lane().items())
            if rec.get('state') == lane_mod.PUSHED and not rec.get('pr')]


def wave_latency(facts):
    """The last tick's start → wave start (:mod:`asf.tick.wave_latency`), aged as that span:
    every session the wave launches waited it out first."""
    from asf.tick import wave_latency as wl
    data = wl.read(facts.product)
    if not data:
        return []
    secs = float(data['seconds'])
    return [Finding('wave_latency', 'tick',
                    f"the tick at {data.get('at') or '?'} reached its wave {secs:.0f}s after its start",
                    facts.now - secs)]


PROBES = {
    'tick_running': tick_running, 'pr_green_not_landing': pr_green_not_landing,
    'check_cancelled': check_cancelled, 'green_batch_blocked': green_batch_blocked,
    'chain_no_cut': chain_no_cut, 'launchable_idle': launchable_idle,
    'ungrantable_hold': ungrantable_hold, 'record_behind': record_behind,
    'runner_offline': runner_offline, 'branch_no_pr': branch_no_pr,
    'wave_latency': wave_latency,
}


# ---- the actions: the only two that change anything ------------------------------------------

def rerun(facts, f, store):
    """Re-run the failed jobs of each run behind ``f``'s cancelled checks — once per PR head
    (``store['acted']``): a second cancel on the same head is a person's to look at."""
    mark = f'check_cancelled|{f.key}'
    if mark in store['acted']:
        f.detail += ' — re-run once on this head already; needs a look'
        return 'none (re-run once already)'
    runs = (f.act or {}).get('runs') or []
    if not runs:
        return 'none (no run id on the checks)'
    from asf import connectors
    done = [rid for rid in runs if connectors.ci().rerun(facts.product.repo_slug, rid).ok]
    if not done:
        return f"re-run refused for run(s) {', '.join(runs)}"
    store['acted'][mark] = _iso(facts.now)
    return f"re-ran failed jobs of run(s) {', '.join(done)} (once on this head)"


def drop(facts, f, store):
    """Resolve an ungrantable hold ``dropped``: no person can grant it, so it waits on no one."""
    from asf import approvals
    hold = (f.act or {}).get('hold') or f.key
    approvals.resolve(facts.product, hold, 'dropped')
    return f'dropped {hold}'


ACTIONS = {'rerun': rerun, 'drop': drop}


# ---- the pass ---------------------------------------------------------------------------------

def check(product, facts=None, act=False, out=print, root=None):
    """``[Finding]`` — every fact in a watched state, aged, limited, breaches first acted on
    when ``act``. Each probe that could not read is one ``watchdog: <state> unreadable`` line
    and keeps its first-seen times; the times of the states that ended are cleared."""
    facts = facts or Facts(product, root=root)
    lim = limits(product)
    store = load_store(product)
    seen = store['seen']
    found = []
    for name in NAMES:
        if lim[name] is None:
            continue
        try:
            got = PROBES[name](facts)
        except Exception as e:  # noqa: BLE001 — one unreadable state never stops the rest
            out(f'watchdog: {name} unreadable ({type(e).__name__}: {e})')
            continue
        keys = set()
        for f in got:
            k = f'{name}|{f.key}'
            keys.add(k)
            first = seen.setdefault(k, facts.now)
            start = f.since if f.since is not None else first
            f.age_s = max(0.0, facts.now - start)
            f.limit_min = lim[name]
            found.append(f)
        for k in [k for k in seen if k.startswith(f'{name}|') and k not in keys]:
            del seen[k]
    # a head's one re-run is remembered for a week — long past any PR head's life
    for k, at in list(store['acted'].items()):
        if (parse_ts(at) or 0) < facts.now - ACTED_KEEP_S:
            del store['acted'][k]
    for f in found:
        action = BY_NAME[f.state][3]
        if f.breach and act and action:
            try:
                f.action = ACTIONS[action](facts, f, store)
            except Exception as e:  # noqa: BLE001 — a failed action is said, the alarm stands
                f.action = f'{action} failed ({type(e).__name__}: {e})'
    try:
        save_store(product, store)
    except OSError as e:
        out(f'watchdog: {STORE} not written ({e})')
    return found


def bug_info(f):
    """The Bug a breach files (:func:`asf.tick.file_bugs._file_or_bump_bug`'s ``info``)."""
    what = BY_NAME[f.state][4]
    return {'title': f'Watchdog: {what} past {f.limit_min} min — {f.key}',
            'severity': 'S3', 'runs': [],
            'evidence': [f'{_iso(time.time())} {f.line()}'],
            'acceptance': [f'no `{f.state}` breach on {f.key} for a day']}


def file_cards(product, root, breaches, out=print):
    """One Bug per ``(state, key)`` breached, filed or bumped at most once a day — under the
    product's ``file_bug`` approval level; another level only says what it would file."""
    from asf import approvals
    from asf.record.core import canonicalize, load_items, today
    from asf.record.index import do_index
    from asf.tick import file_bugs
    if not breaches or not root:
        return {}
    level = approvals.level_of(product, 'file_bug')
    sigs = {f'watchdog {f.state}: {f.key}': f for f in breaches}
    if level != 'auto':
        for sig in sorted(sigs):
            out(f'held file_bug on {sig} — widen approvals: file_bug in products/<p>.yaml')
        return {sig: 'held' for sig in sigs}
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    epic, _why = file_bugs.usable_bug_epic(canonical, product.conventions.get('default_bug_epic'))
    outcomes = {}
    for sig, f in sorted(sigs.items()):
        outcomes[sig] = file_bugs._file_or_bump_bug(root, canonical, sig, bug_info(f), today(),
                                                    default_bug_epic=epic)
        if outcomes[sig] == 'filed':
            by_id, _errors = load_items(root)
            canonical, _dupes = canonicalize(by_id)
    if any(o in ('filed', 'bumped') for o in outcomes.values()):
        do_index(root)
    return outcomes


def run_step(ctx, out=print, facts=None):
    """The tick's ``watchdog`` step: every breach printed, acted on where :data:`STATES` says,
    written as a ``watchdog`` event and filed as a Bug. Never a failure of the tick: a breach
    is the alarm, not the step's outcome."""
    product = ctx.product
    try:
        root = ctx.record_root()
    except Exception as e:  # noqa: BLE001 — no record: the probes that need none still run
        out(f'watchdog: no record clone ({type(e).__name__}: {e}) — no wave preview, no Bug')
        root = None
    found = check(product, facts=facts or Facts(product, root=root), act=True, out=out)
    breaches = [f for f in found if f.breach]
    for f in breaches:
        out(f.line())
        if root is not None:
            ctx.event('watchdog', **{k: v for k, v in f.as_json().items() if k != 'breach'})
    try:
        file_cards(product, root, breaches, out=out)
    except Exception as e:  # noqa: BLE001 — a card not filed is filed by the next breach
        out(f'watchdog: Bugs not filed ({type(e).__name__}: {e})')
    if not breaches:
        out(f'watchdog: {len(found)} watched, no breach')
    return 0


# ---- the CLI ----------------------------------------------------------------------------------

def _record_root(product):
    from asf.tick import shadow
    for root in (shadow.record_dir(product), product.backlog_dir):
        if root and os.path.isfile(os.path.join(root, 'index.json')):
            return root
    return None


def cmd_watchdog(args, out=print):
    """``asf watchdog --product P [--json]`` — every watched fact and its age; exit 1 on a
    breach. Reports only: no action, no event, no Bug (the tick's step does those)."""
    product = env.load_product(getattr(args, 'product', None))
    found = check(product, act=False, out=out, root=_record_root(product))
    if getattr(args, 'json', False):
        out(json.dumps([f.as_json() for f in found], indent=1, sort_keys=True))
    else:
        for f in found:
            if f.breach:
                out(f.line())
        for f in found:
            if not f.breach:
                out(f'watchdog: ok     {f.state} {f.key} — {int(f.age_s // 60)} min of '
                    f'{f.limit_min} — {f.detail}')
        if not found:
            out('watchdog: nothing in a watched state')
    return 1 if any(f.breach for f in found) else 0


def register(subparsers):
    p = subparsers.add_parser(
        'watchdog', help='the dwell-time watchdog: every state the factory can wait in, its age '
                         'against its limit (report only; the tick step acts and files)')
    p.add_argument('--product')
    p.add_argument('--json', action='store_true', help='every watched fact as JSON')
    p.set_defaults(func=cmd_watchdog)
    return p
