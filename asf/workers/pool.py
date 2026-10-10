"""asf.workers.pool — the accounts, the session ledger, the feeder row, and the pick rule.

Accounts come from ``config.yaml worker_pool.accounts``::

    worker_pool:
      accounts:
        - name: acct-a
          role: local          # the lane: cloud, or any other name (local, worker) = local
          cap: 3               # concurrent sessions
          caps: {opus: 2}      # optional per-model ceiling
          config_dir: ~/.ASF/accounts/acct-a   # the runtime's isolated config/home
      reserve_for_s1: {local: 1, cloud: 1}   # deprecated: now ``capacity.reserve_for_s1`` in
                                              # config.yaml (asf.capacity.reserve); this key is
                                              # still read when the new one is not set.

The pick rule: among accounts under their caps, a free account wins over a cooling one, and a
stopped account is never picked; the lowest load wins among free accounts (ties by name), and
among cooling accounts (eligible only at load 0) ties go to the name. Nothing launchable, but
some account held only by the cooldown → ``quota cooldown — one job at a time``, an ordinary
wait. Every candidate at or above its stop, or unreadable →
``NEEDS OPERATOR: no account under quota — …`` as the row's reason, never an exception — but only
when no account could take the row even with a free seat: while some account sits at its cap under
its quota, the row waits for that seat (``pool full — accounts at cap: a 4/4, …; the rest stopped:
…``), an ordinary wait.

The 5h window is a budget (:mod:`asf.workers.headroom`): a candidate takes the launch only while
its ``five_h_pct`` + this wave's launches on it + an allowance for its running sessions + this
launch's estimate stay under the 5h stop (:meth:`Pool.headroom`). When no candidate fits, the
reason names the one closest to fitting: ``headroom: <acct> would exceed 65% (now 51%, +10%
committed, +10% this launch)``. An account a session limit stopped is ``stop`` until its reset;
when that is all that stops the pool, the reason is ``quota: <acct> stopped until 15:20 (session
limit)`` — a wait for a known reset, not a page.

Reserved S1 capacity: while any S1 Bug is open, a lane's non-``BUG → FIX`` rows may use at most
``lane cap − reserve`` slots; the reserved slot only goes to a ``BUG → FIX`` row.

The session ledger is ``~/.ASF/state/<product>/sessions.jsonl``, append-only: a launch line
(``started`` + ``pid``) opens a run of its job, later lines for the same job update that run
(``ended``, ``end_reason``, ``correction``, ``harvested``); :mod:`asf.workers.lifecycle` folds
them per run and :func:`load_sessions` hands out each job's latest.

An account's cap is the machine's, not one product's: :meth:`Pool.from_config` sums load over
every product's registry (:func:`asf.workers.lifecycle.live_all`) and over the sessions
:mod:`asf.workers.observe` sees on our accounts that no registry knows — foreign ones included —
counting each seat once (F-0076 S-8154).
"""
import datetime
import json
import os
import re
import threading

from asf import env
from asf import capacity as capacity_mod
from asf.workers import account_auth
from asf.workers import headroom as headroom_mod
from asf.workers import lifecycle
from asf.workers import observe
from asf.workers import quota as quota_mod
from asf.workers import seats as seats_mod

DEFAULT_RESERVE = capacity_mod.DEFAULT_RESERVE  # re-export: existing importers keep working
SESSION_FIELDS = ('job', 'item', 'feature', 'kind', 'account', 'model', 'pid', 'worktree',
                  'branch', 'started', 'card_digest')

REASON_RESERVED = 'reserved for S1'
REASON_FULL = 'pool full'
REASON_NO_QUOTA = ('NEEDS OPERATOR: no account under quota — wait for a window to reset, or '
                   'add an account under worker_pool.accounts (see: asf workers quota)')
REASON_COOLDOWN = 'quota cooldown — one job at a time'

#: an account naming no ``provider`` is this one kind (F-0138)
DEFAULT_PROVIDER = 'default'


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---- accounts ---------------------------------------------------------------

class Account:
    def __init__(self, name, role='local', cap=1, caps=None, home=None, config_dir=None,
                 home_seed=(), isolate_home=env.DEFAULT_ISOLATE_HOME, auth_env=None,
                 identity=None, provider=None):
        self.name = name
        self.role = role or 'local'
        self.cap = int(cap if cap is not None else 1)
        self.caps = {str(k).lower(): int(v) for k, v in (caps or {}).items()}
        self.home = home
        self.config_dir = config_dir
        #: ``home_seed``: the paths copied into the account's per-account home
        self.home_seed = list(home_seed or ())
        #: ``isolate_home``: a HOME of the session's own (default), or the operator's (false)
        self.isolate_home = bool(isolate_home)
        #: ``auth_env``: ``{VARIABLE: file}`` — each file's content is that variable in this
        #: account's sessions (:func:`asf.workers.runtime.auth_env_values`)
        self.auth_env = dict(auth_env or {})
        #: ``identity``: a ``{name, email}`` map overriding what this account's sessions commit
        #: as (:func:`asf.identity.agent_identity`), or None for the agent default
        self.identity = dict(identity) if identity else None
        #: ``provider``: the provider *kind* — an opaque operator label naming what sort of
        #: account this is, so accounts that share a window scale share one estimate (F-0138).
        #: The window's dollars are read per kind (``quota_guards.window_usd.<provider>``);
        #: accounts that name none are one kind, :data:`DEFAULT_PROVIDER`.
        self.provider = str(provider or DEFAULT_PROVIDER)

    @classmethod
    def from_dict(cls, d):
        return cls(d['name'], role=d.get('role'), cap=d.get('cap', 1), caps=d.get('caps'),
                   home=d.get('home'), config_dir=d.get('config_dir'),
                   home_seed=env.account_home_seed(d), isolate_home=env.isolate_home(d),
                   auth_env=env.account_auth_env(d), identity=env.account_identity(d),
                   provider=d.get('provider'))

    def __repr__(self):
        return f'Account({self.name!r}, role={self.role!r}, cap={self.cap})'


def accounts_from_config(cfg):
    return [Account.from_dict(a) for a in (((cfg or {}).get('worker_pool') or {}).get('accounts') or [])
            if isinstance(a, dict) and a.get('name')]


def reserve_from_config(cfg):
    return capacity_mod.reserve(cfg)


# ---- the feeder row ---------------------------------------------------------

# `STATE → ACTION ITEM "title" [(S1)] [(anything)]   → launch <job> (<Model>)`
ROW_RE = re.compile(r'^(?P<state>\S+) → (?P<action>\S+) +(?P<item>\S+) +"(?P<title>.*)"'
                    r'(?P<mid>.*?)→ launch (?P<job>\S+) \((?P<model>[^)]+)\)\s*$')
SEV_RE = re.compile(r'\((S[0-4])\)')
CLOUD_OK_RE = re.compile(r'\bcloud-ok\b')
ACTION_KIND = {'FIX': 'fix-bug', 'SPEC': 'spec', 'PLAN': 'plan', 'BUILD': 'task', 'TASK': 'task'}


class Row:
    """One feeder row: something to launch. ``is_fix`` = a ``BUG → FIX`` row."""

    def __init__(self, job, item, state='', action='', title='', model='', kind=None,
                 severity=None, feature=None, lane=None, branch=None, test=None, add_dirs=(),
                 card_digest='', cloud_ok=False, host_load_bypass=False, correction_kind='',
                 local_only=False):
        self.job = job
        self.item = item
        self.state = state
        self.action = action
        self.title = title
        self.model = model
        self.kind = kind or ACTION_KIND.get(action, action.lower() or 'task')
        self.severity = severity
        self.feature = feature
        self.lane = lane
        self.branch = branch
        self.test = test
        #: directories this row's session may use outside its worktree, beside the product's
        #: ``job_grants`` (the brief's ``add_dirs`` — a groom row's answers directory, PD7)
        self.add_dirs = list(add_dirs or ())
        #: :func:`asf.briefs.build.card_digest` of the card as this row's brief stated it — what
        #: the ledger keeps so a later card change can be told from a dispute (F-0090 D4)
        self.card_digest = card_digest or ''
        #: :func:`asf.workers.relaunch.cause_key` of the feeder row: what the relaunch cap
        #: compares beside the head and the card (set by the wave)
        self.cause = ''
        #: the row may run in the cloud lane (``cloud-ok``; :func:`asf.workers.cloud.eligible`)
        self.cloud_ok = bool(cloud_ok)
        #: the row never leaves the host (its item's ``local_only: true``;
        #: :func:`asf.workers.cloud.local_only`)
        self.local_only = bool(local_only)
        #: the lane's ``kind`` for the correction this row answers — a rewrite of the branch's
        #: history has no channel back from the cloud lane
        #: (:data:`asf.workers.cloud.REWRITE_KINDS`, F-0289)
        self.correction_kind = correction_kind or ''
        #: this launch is the one S1 row passing the host guard's LOAD hold (the wave step's own
        #: rule, :func:`asf.tick.step_wave.s1_bypass_live`) — carried onto the session ledger so
        #: a later wave can see the bypass is still live.
        self.host_load_bypass = bool(host_load_bypass)

    @property
    def is_fix(self):
        return self.state == 'BUG' and self.action == 'FIX'

    @property
    def is_s1_fix(self):
        return self.is_fix and self.severity == 'S1'

    @classmethod
    def from_dict(cls, d):
        return cls(d['job'], d.get('item', ''), state=d.get('state', ''),
                   action=d.get('action', ''), title=d.get('title', ''), model=d.get('model', ''),
                   kind=d.get('kind'), severity=d.get('severity'), feature=d.get('feature'),
                   lane=d.get('lane'), branch=d.get('branch'), test=d.get('test'),
                   cloud_ok=d.get('cloud_ok') or d.get('cloud-ok'),
                   host_load_bypass=d.get('host_load_bypass'),
                   correction_kind=d.get('correction_kind'), local_only=d.get('local_only'))

    def __repr__(self):
        return f'Row({self.state} → {self.action} {self.item} {self.job})'


def parse_row(line):
    """A feeder text row, or a JSON object line, → Row; anything else → None."""
    line = line.strip()
    if not line:
        return None
    if line.startswith('{'):
        try:
            return Row.from_dict(json.loads(line))
        except (json.JSONDecodeError, KeyError):
            return None
    m = ROW_RE.match(line)
    if not m:
        return None
    sev = SEV_RE.search(m.group('mid'))
    return Row(m.group('job'), m.group('item'), state=m.group('state'), action=m.group('action'),
               title=m.group('title'), model=m.group('model'), severity=sev.group(1) if sev else None,
               cloud_ok=bool(CLOUD_OK_RE.search(m.group('mid'))))


def s1_open(rows):
    return any(r.is_s1_fix for r in rows)


def model_key(model):
    return str(model or '').strip().lower()


def is_cloud_lane(s):
    """A session the cloud runtime launched: ``runtime_lane: cloud`` — the current key
    (:func:`asf.workers.actions.ActionsRuntime.run`, :func:`asf.workers.remote.RemoteRuntime.run`)
    — or, for a launch line written before that split, the bare ``lane: cloud`` it used to write
    into the harvest lane state machine's own key (F-lane-collision; the ledger is append-only,
    so an old line still reads this way)."""
    return s.get('runtime_lane') == 'cloud' or s.get('lane') == 'cloud'


# ---- the session ledger -----------------------------------------------------

def sessions_path(product):
    return os.path.join(env.state_dir(product), 'sessions.jsonl')


#: a wave's launches append from threads (:func:`asf.workers.wave.wave`): one line at a time
_APPEND_LOCK = threading.Lock()


def append_session(product, record):
    line = json.dumps(record, sort_keys=True) + '\n'
    with _APPEND_LOCK, open(sessions_path(product), 'a', encoding='utf-8') as f:
        f.write(line)


# The fields that belong to ONE run of a job (:data:`asf.workers.lifecycle.RUN_FIELDS`): a
# launch line opens a new run and the fold never carries them across (B-0041).
RUN_FIELDS = lifecycle.RUN_FIELDS


def load_sessions(product):
    """``{job: its latest run}`` in launch order — :func:`asf.workers.lifecycle.latest`."""
    return lifecycle.latest(sessions_path(product))


def update_session(product, job, **fields):
    """One update line for ``job``. A correction written without ``at`` gets one now: the
    readers order corrections, and tell an answered one from a pending one, by it."""
    corr = fields.get('correction')
    if isinstance(corr, dict) and corr.get('text') and not corr.get('at'):
        now = datetime.datetime.now(datetime.timezone.utc)
        fields['correction'] = dict(corr, at=now.strftime('%Y-%m-%dT%H:%M:%SZ'))
    append_session(product, dict(fields, job=job))


def live_sessions(product):
    return [s for s in load_sessions(product).values() if lifecycle.is_live(s)]


# ---- the pool ---------------------------------------------------------------

def in_lane(account, lane):
    """``account`` takes a row of ``lane``: any lane when None; ``local`` is every account that
    is not ``role: cloud`` (a host account's role may be ``local``, ``worker`` or another name);
    any other lane is its own role."""
    if lane is None:
        return True
    if lane == 'local':
        return account.role != 'cloud'
    return account.role == lane


def lane_of(account):
    """The lane ``account`` serves: ``cloud`` for ``role: cloud``, else ``local``."""
    return 'cloud' if account.role == 'cloud' else 'local'


def claim_rows(claims, base):
    """One live row per claim not already spoken for by ``base`` (:meth:`Pool.from_config`,
    :meth:`Pool._refresh`) — the two never disagree about what a claim row looks like.

    A claim whose ``(product, job)`` is already a row of ``base`` is dropped: the launch line
    landed and the registry holds that seat now, and counting both would be the mirror of the
    defect this card closes. A claim of this process's own pid becomes the **exact** 7-key seat
    dict :meth:`Pool.take` appends to ``live`` — those keys and no others, so :meth:`Pool.untake`'s
    identity scan still finds it (P11). Another process's claim becomes a row that carries
    ``lane``, so :func:`is_cloud_lane` reads a cloud claim as cloud and it holds no local seat."""
    held = {(s.get('product'), s.get('job')) for s in base}
    rows = []
    for c in claims:
        if (c.get('product'), c.get('job')) in held:
            continue
        if c.get('pid') == os.getpid():
            rows.append({'job': c.get('job'), 'account': c.get('account'),
                        'model': c.get('model'), 'product': c.get('product'),
                        'kind': c.get('kind'), 'wave': True, 'lane': c.get('lane')})
        else:
            rows.append({'account': c.get('account'), 'model': c.get('model'),
                        'product': c.get('product'), 'job': c.get('job'),
                        'kind': c.get('kind'), 'lane': c.get('lane'), 'claim': True})
    return rows


class Pool:
    """Accounts + their current load + the guard. ``live`` is the list of live session records
    (each with ``account``, ``model``); :meth:`take` adds one as a wave launches.

    Built by :meth:`from_config`, ``live`` spans every product's registry and the observed
    sessions no registry knows, each seat once, plus this host's outstanding claims
    (:mod:`asf.workers.seats`); ``unreadable`` is why the session table could not be read, and
    ``''`` when it could; ``seats_degraded`` is why claims are not being shared, and ``''`` when
    they are. A hand-built pool is readable, shares no claims across processes, and its ``live``
    is base + claims: ``_base`` is the list it was given, kept as given across a ``take``."""

    def __init__(self, accounts, quota_source=None, guards=None, reserve=None, live=(),
                 unreadable='', costs=None, limits=None, blocked=None):
        self.accounts = list(accounts)
        self.quota = quota_source or quota_mod.NoQuotaSource()
        self.guards = guards or quota_mod.guards_from_config({})
        self.reserve = dict(DEFAULT_RESERVE if reserve is None else reserve)
        #: the build's own rows — kept as given for a hand-built pool (PD2); a ``from_config``
        #: pool re-reads instead (``self._reread``) and never consults this again
        self._base = [dict(s) for s in live]
        self.live = [dict(s) for s in live]
        self.unreadable = unreadable
        #: why claims are not shared across products right now, or ``''`` (PD5)
        self.seats_degraded = ''
        #: ``from_config``'s closure over ``(seen_pids, seen_sessions)`` that re-reads the
        #: registries without re-running ``observe.read`` (C3); ``None`` for a hand-built pool
        self._reread = None
        #: the per-launch share of a 5h window (:class:`asf.workers.headroom.CostTable`)
        self.costs = costs or headroom_mod.CostTable()
        #: ``{account: {until, …}}`` — accounts a session limit stopped until their reset
        self.limits = dict(limits or {})
        #: ``{account: {at, …}}`` — accounts an auth error took out until re-enabled
        #: (:mod:`asf.workers.account_auth`)
        self.blocked = dict(blocked or {})
        self._usage = {}
        self._committed = {}

    @classmethod
    def from_config(cls, cfg, product, quota_source=None, session_source=None):
        """Load is summed over every product's registry, not this one's, plus the sessions on the
        machine that no registry knows: an account's cap is the machine's, so a session under
        product ``b`` spends the same seat as one under ``a``, and a foreign session on one of our
        accounts spends a seat too (F-0076 S-8154). ``product`` stays the wave's own, and is what
        the running check falls back to for a registry run carrying no ``product``.

        Each seat is counted once. An observed session is *extra* only when it is attributed to a
        pool account and neither its pid nor its ``ASF_SESSION`` belongs to a registered run — so
        a run and its own process are one seat, and a run whose process ``ps`` has not shown yet
        still holds its seat. An extra carries ``model: None``: it counts toward ``cap``, and
        falls out of the per-model ``caps`` ceiling, because its model is not known.

        When the session table cannot be read, ``unreadable`` carries the reason and ``live`` is
        the registered runs alone — the spec's D10 fallback, which the wave prints once.
        """
        accounts = accounts_from_config(cfg)
        observed, why = observe.read(cfg, accounts, source=session_source)
        seen_pids = {o.pid for o in observed} - {None}
        seen_sessions = {o.session for o in observed} - {None}

        def registered_now():
            # a run holds a seat while its pid answers, or while ps shows its process — a dead
            # run with no ``ended`` line yet is no load (:func:`asf.workers.lifecycle.occupies`)
            return [r for r in lifecycle.live_all(os.path.join(env.ASF_HOME, 'state'),
                                                  alive=lambda _pid: True)
                   if lifecycle.pid_alive(r.get('pid')) or r.get('pid') in seen_pids
                   or (r.get('session') is not None and r.get('session') in seen_sessions)]

        registered = registered_now()
        pids = {r.get('pid') for r in registered} - {None}
        sessions = {r.get('session') for r in registered} - {None}
        extras = [{'account': o.account, 'model': None, 'product': o.product,
                  'job': o.job, 'session': o.session, 'pid': o.pid, 'owner': o.owner}
                 for o in observed
                 if o.account is not None and o.pid not in pids and o.session not in sessions]
        live = registered + extras
        # every product's outstanding claim, counted once against every pool it is not a row of
        # yet (:mod:`asf.workers.seats`, PD5); ``registered`` is passed so the sweep's rule (c)
        # walks no registry twice (P4)
        claims, seats_why = seats_mod.read(registered=registered)
        # a run that died on a usage limit stops its account now, not at its product's next
        # health pass — another product's wave may be the next to place a launch
        try:
            lifecycle.note_spent_windows(os.path.join(env.ASF_HOME, 'state'))
        except Exception:  # noqa: BLE001 — health still records it; never a blocker
            pass
        pool = cls(accounts,
                   quota_source=quota_source or quota_mod.source_from_config(cfg),
                   guards=quota_mod.guards_from_config(cfg), reserve=reserve_from_config(cfg),
                   live=live, unreadable=why, costs=headroom_mod.table_from_config(cfg),
                   limits=headroom_mod.active_limits(), blocked=account_auth.blocked())
        pool.seats_degraded = seats_why
        # ``observe.read`` is not re-run: the re-read keeps the ps-observed ``extras`` frozen from
        # this build and only re-walks the registries (C3)
        pool._reread = lambda: registered_now() + extras
        pool.live = pool._base + claim_rows(claims, pool._base)
        return pool

    def usage(self, account):
        if account.name not in self._usage:
            self._usage[account.name] = self.quota.read(account)
        return self._usage[account.name]

    def providers(self):
        """``{account name: its provider kind}`` — what a sample line is attributed to."""
        return {a.name: a.provider for a in self.accounts}

    def limit(self, account):
        """The reset a session limit stopped ``account`` until (ISO), or None."""
        rec = self.limits.get(account.name) or {}
        until = headroom_mod.parse_ts(rec.get('until'))
        return rec.get('until') if until is not None and until > headroom_mod.now_utc() else None

    def band(self, account):
        if account.name in self.blocked:
            return quota_mod.STOP, account_auth.stop_reason(account.name)
        until = self.limit(account)
        if until:
            return quota_mod.STOP, f'session limit until {headroom_mod.reset_label(until)}'
        return quota_mod.band(self.usage(account), self.guards)

    def banded_seats(self):
        """``(banded, caps)``: the seats the quota guard has taken across this pool right now,
        and the seats the pool would have with every account free.

        Per account: ``cap`` at stop, ``cap - 1`` in cooldown, ``0`` when free — the complement of
        :func:`asf.capacity.usable_slots`' sum over the same accounts, so ``caps - banded`` is that
        figure exactly (§3.2 pins them). Read off :meth:`usage`, which is memoised per instance
        (:meth:`band`), so this costs no reading, no subprocess and no file.

        An unreadable account bands as ``STOP`` (:func:`asf.workers.quota.band`, "unknown ≠
        free"), so its whole cap counts as taken — which is what the pool does with it anyway."""
        caps = sum(a.cap for a in self.accounts)
        banded = 0
        for a in self.accounts:
            state, _why = self.band(a)
            banded += a.cap if state == quota_mod.STOP else a.cap - 1 if state == quota_mod.COOLDOWN else 0
        return banded, caps

    def headroom(self, account, kind, model):
        """``(fits, why, projected)``: whether one more ``(kind, model)`` launch keeps ``account`` under the
        5h guard — ``five_h_pct`` now, plus this wave's launches on it at their full estimate,
        plus the sessions already running on it at the table's allowance, plus this launch
        (:mod:`asf.workers.headroom`). An account with no 5h reading, or a stale one, is the band's
        to judge."""
        u = self.usage(account) or {}
        if u.get('five_h_pct') is None or quota_mod.stale_since(u, self.guards) is not None:
            return True, '', 0.0
        now = float(u['five_h_pct'])
        running = sum(self.costs.cost(s.get('kind'), s.get('model')) for s in self.live
                      if s.get('account') == account.name and not s.get('wave'))
        committed = round(self._committed.get(account.name, 0.0)
                          + running * self.costs.allowance, 1)
        cost = self.costs.cost(kind, model)
        guard = self.guards['stop']['five_h']
        total = now + committed + cost
        if total < guard:
            return True, '', total
        return False, (f'headroom: {account.name} would exceed {guard:g}% (now {now:g}%, '
                       f'+{committed:g}% committed, +{cost:g}% this launch)'), total

    def load(self, account, model=None):
        """The account's seats on this host: its cloud-lane runs (``lane: cloud``) hold none —
        they are counted against ``cloud.max_inflight`` (:meth:`cloud_load`)."""
        return sum(1 for s in self.live if s.get('account') == account.name
                   and not is_cloud_lane(s)
                   and (model is None or model_key(s.get('model')) == model_key(model)))

    def lane_load(self, lane):
        names = {a.name for a in self.accounts if in_lane(a, lane)}
        return sum(1 for s in self.live if s.get('account') in names and not is_cloud_lane(s))

    def cloud_load(self, account=None):
        """Live cloud-lane runs — every account's, or ``account``'s."""
        return sum(1 for s in self.live if is_cloud_lane(s)
                   and (account is None or s.get('account') == account.name))

    def pick_cloud(self, kind, model, cloud_settings):
        """``(Account, '')`` or ``(None, reason)`` for one cloud-lane launch
        (:mod:`asf.workers.cloud`): under ``max_inflight``, among the lane's accounts, a free
        account (fewest cloud runs, then name) before a cooling one at no cloud run; the quota
        band and the 5h headroom apply as for a local launch — the session spends that account's
        quota wherever it runs."""
        from asf.workers import cloud as cloud_mod
        s = cloud_settings
        n = self.cloud_load()
        if n >= s.max_inflight:
            return None, f'cloud full — {n}/{s.max_inflight} in flight'
        cands = cloud_mod.lane_accounts(self.accounts, s)
        if not cands:
            return None, 'cloud: no cloud-lane account'
        free, cooling, over, stopped = [], [], [], []
        for a in cands:
            state, why = self.band(a)
            if state not in (quota_mod.FREE, quota_mod.COOLDOWN):
                stopped.append(f'{a.name} ({why})')
                continue
            fits, why, total = self.headroom(a, kind, model)
            if not fits:
                over.append((total, why))
            elif state == quota_mod.FREE:
                free.append(a)
            elif self.cloud_load(a) == 0:
                cooling.append(a)
        if free:
            return min(free, key=lambda a: (self.cloud_load(a), a.name)), ''
        if cooling:
            return min(cooling, key=lambda a: a.name), ''
        if over:
            return None, min(over)[1]
        if stopped:
            return None, 'cloud: accounts stopped: ' + ', '.join(stopped)
        return None, REASON_COOLDOWN

    def lane_cap(self, lane):
        return sum(a.cap for a in self.accounts if in_lane(a, lane))

    def under_caps(self, account, model):
        if self.load(account) >= account.cap:
            return False
        mcap = account.caps.get(model_key(model))
        return mcap is None or self.load(account, model) < mcap

    def pick_account(self, kind, model, is_fix=False, s1_is_open=False, lane=None):
        """(Account, '') or (None, reason). ``kind`` is carried for the reason text only.

        ``lane='local'`` is every account that is not ``role: cloud`` — a host account may carry
        any other role (``local``, ``worker``, …); reading it as ``role == 'local'`` left a pool
        of ``role: worker`` accounts with no local candidate at all once the cloud lane was on,
        and every row waited on a bare ``pool full``. A full pool always names its seats."""
        cands = [a for a in self.accounts if in_lane(a, lane)]
        room = [a for a in cands if self.under_caps(a, model)]
        if not cands:
            roles = ', '.join(f'{a.name} {a.role}' for a in self.accounts) or 'none'
            return None, (f'{REASON_FULL} — no account serves the {lane} lane '
                          f'(worker_pool.accounts roles: {roles})')
        if not room:
            return None, self.full_reason(cands, [], model)
        if s1_is_open and not is_fix:
            room = [a for a in room
                    if self.lane_load(lane_of(a))
                    < self.lane_cap(lane_of(a)) - self.reserve.get(lane_of(a), 0)]
            if not room:
                return None, REASON_RESERVED
        free, cooling, held, over, limited, stopped = [], [], [], [], [], []
        for a in room:
            state, _why = self.band(a)
            if state == quota_mod.STOP and self.limit(a):
                limited.append(a)
            if state not in (quota_mod.FREE, quota_mod.COOLDOWN):
                stopped.append(a)
                continue
            fits, why, total = self.headroom(a, kind, model)
            if not fits:
                over.append((total, why))
            elif state == quota_mod.FREE:
                free.append(a)
            elif self.load(a) == 0:
                cooling.append(a)
            else:
                held.append(a)              # cooling and already carrying its one job
        if free:
            free.sort(key=lambda a: (self.load(a), a.name))
            return free[0], ''
        if cooling:
            cooling.sort(key=lambda a: a.name)  # every one of them is at load 0
            return cooling[0], ''
        # Nothing launches. A seat at cap under its quota is the limiter the row actually waits
        # on — it is named first, with every other account's own reason beside it, so a wait
        # never names an idle stopped account while the working ones sit full (2026-09-26: acct-a
        # and acct-d at 4/4 printed ``quota: acct-c stopped until 17:10``).
        full = self.at_cap(cands, room, model)
        if full:
            return None, self.full_reason(full, stopped, model, over=over, held=held)
        if over:
            return None, min(over)[1]       # the account closest to fitting
        if held:
            return None, REASON_COOLDOWN
        refused = [a for a in stopped if a.name in self.blocked]
        if refused and len(refused) == len(stopped):
            return None, 'quota: ' + '; '.join(f'{a.name} {account_auth.stop_reason(a.name)}'
                                               for a in refused)
        if limited:
            first = min(limited, key=lambda a: self.limit(a))
            return None, (f'quota: {first.name} stopped until '
                          f'{headroom_mod.reset_label(self.limit(first))} (session limit)')
        return None, REASON_NO_QUOTA

    def at_cap(self, cands, room, model):
        """The candidates that are out of ``room`` only for their seats, not their quota: a row
        that finds every account under its caps stopped still waits for one of these to free a
        slot — an ordinary wait, not a page."""
        names = {a.name for a in room}
        return [a for a in cands if a.name not in names
                and self.band(a)[0] in (quota_mod.FREE, quota_mod.COOLDOWN)]

    def full_reason(self, full, stopped, model, over=(), held=()):
        """``pool full — accounts at cap: a 4/4, b 4/4; the rest stopped: c (seven_d_pct 100 ≥
        95), d (session limit until 17:10); headroom: e would exceed 90% (…); cooling: f``: the
        seats the row waits on, and why each account with a free seat cannot take it —
        ``over`` is the headroom misses as ``(projected, why)``, ``held`` the cooling accounts
        already carrying their one job."""
        def seats(a):
            mcap = a.caps.get(model_key(model))
            if mcap is not None and self.load(a, model) >= mcap and self.load(a) < a.cap:
                return f'{a.name} {self.load(a, model)}/{mcap:g} {model_key(model)}'
            return f'{a.name} {self.load(a)}/{a.cap}'
        why = f'{REASON_FULL} — accounts at cap: ' + ', '.join(seats(a) for a in full)
        if stopped:
            why += '; the rest stopped: ' + ', '.join(f'{a.name} ({self.band(a)[1]})'
                                                     for a in stopped)
        if over:
            why += '; ' + min(over)[1]      # the account closest to fitting
        if held:
            why += '; cooling: ' + ', '.join(a.name for a in held)
        return why

    def _refresh(self, ledger):
        """Recompute ``self.live`` under the caller's lock (:mod:`asf.workers.seats`), called only
        by :meth:`take`, :meth:`untake` and :meth:`retake`. A ``from_config`` pool re-reads the
        registries fresh — a launch line that landed since the build stops being double-counted —
        a hand-built pool keeps the list its caller gave it (PD2); either way the ledger's claims
        are layered on top (:func:`claim_rows`), so the two can never disagree about a claim row."""
        base = self._reread() if self._reread is not None else self._base
        self.live = base + claim_rows(ledger.claims, base)

    def take(self, account, model, job='', product=None, kind=None, lane=None):
        """Claim one seat on ``account`` for this launch, across every product on this host.

        Returns ``True`` when the seat is ours. ``False`` when the account filled up between the
        pick and this call — another product's wave claimed it, or a launch of ours landed — and
        the row must be decided again (:func:`asf.workers.wave.wave`)."""
        try:
            with seats_mod.held() as ledger:
                self._refresh(ledger)
                if lane != 'cloud' and not self.under_caps(account, model):
                    return False  # a held lock is milliseconds; the caller retries (C6)
                ledger.add({'account': account.name, 'product': product, 'job': job,
                           'model': model, 'kind': kind, 'lane': lane, 'pid': os.getpid(),
                           'at': now_iso()})
        except seats_mod.Busy:
            return False
        except seats_mod.Unusable as e:
            # a host that cannot hold a ledger must still launch (C6); the operator sees the line
            self.seats_degraded = str(e)
        self.live.append({'job': job, 'account': account.name, 'model': model,
                          'product': product, 'kind': kind, 'wave': True, 'lane': lane})
        self._committed[account.name] = (self._committed.get(account.name, 0.0)
                                         + self.costs.cost(kind, model))
        return True

    def untake(self, account, model, job='', product=None, kind=None, lane=None):
        """Undo one :meth:`take` — a seat the wave reserved for a launch that then failed
        (:func:`asf.workers.wave.wave` reserves before the launch's setup runs). Also removes the
        claim, so a failed spawn frees the seat now rather than at ``CLAIM_TTL_S`` (C5); a ``Busy``
        or ``Unusable`` ledger here loses only this eager release — the sweep's rule (a) or (b)
        collects it — so neither raises out of ``untake``."""
        want = {'job': job, 'account': account.name, 'model': model, 'product': product,
                'kind': kind, 'wave': True, 'lane': lane}
        found = False
        for i in range(len(self.live) - 1, -1, -1):
            if self.live[i] == want:
                del self.live[i]
                self._committed[account.name] = (self._committed.get(account.name, 0.0)
                                                 - self.costs.cost(kind, model))
                found = True
                break
        try:
            with seats_mod.held() as ledger:
                ledger.remove(product, job)
        except (seats_mod.Busy, seats_mod.Unusable):
            pass
        return found

    def retake(self, account, seat, model):
        """Move a held claim's ``model`` (C8) — one lock, no cap test, never refused: the swap
        happens after the launch already succeeded, and refusing there would free a seat for a
        session that is running. Replaces the claim's ``model`` and the live row's together, and
        returns the new seat dict."""
        product, job = seat.get('product'), seat.get('job')
        try:
            with seats_mod.held() as ledger:
                ledger.remove(product, job)
                ledger.add({'account': account.name, 'product': product, 'job': job,
                           'model': model, 'kind': seat.get('kind'), 'lane': seat.get('lane'),
                           'pid': os.getpid(), 'at': now_iso()})
        except (seats_mod.Busy, seats_mod.Unusable):
            pass
        old = dict(seat, account=account.name, wave=True)
        new_seat = dict(seat, model=model)
        new_live = dict(new_seat, account=account.name, wave=True)
        for i, s in enumerate(self.live):
            if s == old:
                self.live[i] = new_live
                break
        return new_seat
