"""asf.workers.pool — the accounts, the session ledger, the feeder row, and the pick rule.

Accounts come from ``config.yaml worker_pool.accounts``::

    worker_pool:
      accounts:
        - name: acct-a
          role: local          # the lane: local | cloud
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
``NEEDS OPERATOR: no account under quota — …`` as the row's reason, never an exception.

The 5h window is a budget (:mod:`asf.workers.headroom`): a candidate takes the launch only while
its ``five_h_pct`` + this wave's launches on it + an allowance for its running sessions + this
launch's estimate stay under the 5h stop (:meth:`Pool.headroom`). When no candidate fits, the
reason names the one closest to fitting: ``quota: <acct> would exceed 65% (now 51%, +10%
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

from asf import env
from asf import capacity as capacity_mod
from asf.workers import headroom as headroom_mod
from asf.workers import lifecycle
from asf.workers import observe
from asf.workers import quota as quota_mod

DEFAULT_RESERVE = capacity_mod.DEFAULT_RESERVE  # re-export: existing importers keep working
SESSION_FIELDS = ('job', 'item', 'feature', 'kind', 'account', 'model', 'pid', 'worktree',
                  'branch', 'started', 'card_digest')

REASON_RESERVED = 'reserved for S1'
REASON_FULL = 'pool full'
REASON_NO_QUOTA = ('NEEDS OPERATOR: no account under quota — wait for a window to reset, or '
                   'add an account under worker_pool.accounts (see: asf workers quota)')
REASON_COOLDOWN = 'quota cooldown — one job at a time'


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---- accounts ---------------------------------------------------------------

class Account:
    def __init__(self, name, role='local', cap=1, caps=None, home=None, config_dir=None,
                 home_seed=(), isolate_home=env.DEFAULT_ISOLATE_HOME, auth_env=None):
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

    @classmethod
    def from_dict(cls, d):
        return cls(d['name'], role=d.get('role'), cap=d.get('cap', 1), caps=d.get('caps'),
                   home=d.get('home'), config_dir=d.get('config_dir'),
                   home_seed=env.account_home_seed(d), isolate_home=env.isolate_home(d),
                   auth_env=env.account_auth_env(d))

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
ACTION_KIND = {'FIX': 'fix-bug', 'SPEC': 'spec', 'PLAN': 'plan', 'BUILD': 'task', 'TASK': 'task'}


class Row:
    """One feeder row: something to launch. ``is_fix`` = a ``BUG → FIX`` row."""

    def __init__(self, job, item, state='', action='', title='', model='', kind=None,
                 severity=None, feature=None, lane=None, branch=None, test=None, add_dirs=(),
                 card_digest=''):
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
                   lane=d.get('lane'), branch=d.get('branch'), test=d.get('test'))

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
               title=m.group('title'), model=m.group('model'), severity=sev.group(1) if sev else None)


def s1_open(rows):
    return any(r.is_s1_fix for r in rows)


def model_key(model):
    return str(model or '').strip().lower()


# ---- the session ledger -----------------------------------------------------

def sessions_path(product):
    return os.path.join(env.state_dir(product), 'sessions.jsonl')


def append_session(product, record):
    with open(sessions_path(product), 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, sort_keys=True) + '\n')


# The fields that belong to ONE run of a job (:data:`asf.workers.lifecycle.RUN_FIELDS`): a
# launch line opens a new run and the fold never carries them across (B-0041).
RUN_FIELDS = lifecycle.RUN_FIELDS


def load_sessions(product):
    """``{job: its latest run}`` in launch order — :func:`asf.workers.lifecycle.latest`."""
    return lifecycle.latest(sessions_path(product))


def update_session(product, job, **fields):
    append_session(product, dict(fields, job=job))


def live_sessions(product):
    return [s for s in load_sessions(product).values() if lifecycle.is_live(s)]


# ---- the pool ---------------------------------------------------------------

class Pool:
    """Accounts + their current load + the guard. ``live`` is the list of live session records
    (each with ``account``, ``model``); :meth:`take` adds one as a wave launches.

    Built by :meth:`from_config`, ``live`` spans every product's registry and the observed
    sessions no registry knows, each seat once; ``unreadable`` is why the session table could not
    be read, and ``''`` when it could. A hand-built pool is readable and its ``live`` is one
    product's list."""

    def __init__(self, accounts, quota_source=None, guards=None, reserve=None, live=(),
                 unreadable='', costs=None, limits=None):
        self.accounts = list(accounts)
        self.quota = quota_source or quota_mod.NoQuotaSource()
        self.guards = guards or quota_mod.guards_from_config({})
        self.reserve = dict(DEFAULT_RESERVE if reserve is None else reserve)
        self.live = [dict(s) for s in live]
        self.unreadable = unreadable
        #: the per-launch share of a 5h window (:class:`asf.workers.headroom.CostTable`)
        self.costs = costs or headroom_mod.CostTable()
        #: ``{account: {until, …}}`` — accounts a session limit stopped until their reset
        self.limits = dict(limits or {})
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
        # a run holds a seat while its pid answers, or while ps shows its process — a dead run
        # with no ``ended`` line yet is no load (:func:`asf.workers.lifecycle.occupies`)
        registered = [r for r in lifecycle.live_all(os.path.join(env.ASF_HOME, 'state'),
                                                    alive=lambda _pid: True)
                      if lifecycle.pid_alive(r.get('pid')) or r.get('pid') in seen_pids
                      or (r.get('session') is not None and r.get('session') in seen_sessions)]
        pids = {r.get('pid') for r in registered} - {None}
        sessions = {r.get('session') for r in registered} - {None}
        extra = [o for o in observed
                 if o.account is not None and o.pid not in pids and o.session not in sessions]
        live = registered + [{'account': o.account, 'model': None, 'product': o.product,
                              'job': o.job, 'session': o.session, 'pid': o.pid, 'owner': o.owner}
                             for o in extra]
        return cls(accounts,
                   quota_source=quota_source or quota_mod.source_from_config(cfg),
                   guards=quota_mod.guards_from_config(cfg), reserve=reserve_from_config(cfg),
                   live=live, unreadable=why, costs=headroom_mod.table_from_config(cfg),
                   limits=headroom_mod.active_limits())

    def usage(self, account):
        if account.name not in self._usage:
            self._usage[account.name] = self.quota.read(account)
        return self._usage[account.name]

    def limit(self, account):
        """The reset a session limit stopped ``account`` until (ISO), or None."""
        rec = self.limits.get(account.name) or {}
        until = headroom_mod.parse_ts(rec.get('until'))
        return rec.get('until') if until is not None and until > headroom_mod.now_utc() else None

    def band(self, account):
        until = self.limit(account)
        if until:
            return quota_mod.STOP, f'session limit until {headroom_mod.reset_label(until)}'
        return quota_mod.band(self.usage(account), self.guards)

    def headroom(self, account, kind, model):
        """``(fits, why, projected)``: whether one more ``(kind, model)`` launch keeps ``account`` under the
        5h guard — ``five_h_pct`` now, plus this wave's launches on it at their full estimate,
        plus the sessions already running on it at the table's allowance, plus this launch
        (:mod:`asf.workers.headroom`). An account with no 5h reading is the band's to judge."""
        u = self.usage(account) or {}
        if u.get('five_h_pct') is None:
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
        return False, (f'quota: {account.name} would exceed {guard:g}% (now {now:g}%, '
                       f'+{committed:g}% committed, +{cost:g}% this launch)'), total

    def load(self, account, model=None):
        return sum(1 for s in self.live if s.get('account') == account.name
                   and (model is None or model_key(s.get('model')) == model_key(model)))

    def lane_load(self, lane):
        names = {a.name for a in self.accounts if a.role == lane}
        return sum(1 for s in self.live if s.get('account') in names)

    def lane_cap(self, lane):
        return sum(a.cap for a in self.accounts if a.role == lane)

    def under_caps(self, account, model):
        if self.load(account) >= account.cap:
            return False
        mcap = account.caps.get(model_key(model))
        return mcap is None or self.load(account, model) < mcap

    def pick_account(self, kind, model, is_fix=False, s1_is_open=False, lane=None):
        """(Account, '') or (None, reason). ``kind`` is carried for the reason text only."""
        cands = [a for a in self.accounts if lane is None or a.role == lane]
        room = [a for a in cands if self.under_caps(a, model)]
        if not room:
            return None, REASON_FULL
        if s1_is_open and not is_fix:
            room = [a for a in room
                    if self.lane_load(a.role) < self.lane_cap(a.role) - self.reserve.get(a.role, 0)]
            if not room:
                return None, REASON_RESERVED
        free, cooling, held, over, limited = [], [], False, [], []
        for a in room:
            state, _why = self.band(a)
            if state == quota_mod.STOP and self.limit(a):
                limited.append(a)
                continue
            if state not in (quota_mod.FREE, quota_mod.COOLDOWN):
                continue
            fits, why, total = self.headroom(a, kind, model)
            if not fits:
                over.append((total, why))
            elif state == quota_mod.FREE:
                free.append(a)
            elif self.load(a) == 0:
                cooling.append(a)
            else:
                held = True                 # cooling and already carrying its one job
        if free:
            free.sort(key=lambda a: (self.load(a), a.name))
            return free[0], ''
        if cooling:
            cooling.sort(key=lambda a: a.name)  # every one of them is at load 0
            return cooling[0], ''
        if over:
            return None, min(over)[1]       # the account closest to fitting
        if held:
            return None, REASON_COOLDOWN
        if limited:
            first = min(limited, key=lambda a: self.limit(a))
            return None, (f'quota: {first.name} stopped until '
                          f'{headroom_mod.reset_label(self.limit(first))} (session limit)')
        return None, REASON_NO_QUOTA

    def take(self, account, model, job='', product=None, kind=None):
        """One launch of this wave on ``account``: a seat, and its full estimate committed
        against the account's 5h headroom."""
        self.live.append({'job': job, 'account': account.name, 'model': model,
                          'product': product, 'kind': kind, 'wave': True})
        self._committed[account.name] = (self._committed.get(account.name, 0.0)
                                         + self.costs.cost(kind, model))
