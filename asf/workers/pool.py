"""asf.workers.pool — the accounts, the session ledger, the feeder row, and the pick rule.

Accounts come from ``config.yaml worker_pool.accounts``::

    worker_pool:
      accounts:
        - name: acct-a
          role: local          # the lane: local | cloud
          cap: 3               # concurrent sessions
          caps: {opus: 2}      # optional per-model ceiling
          config_dir: ~/.ASF/accounts/acct-a   # the runtime's isolated config/home
      reserve_for_s1: {local: 1, cloud: 1}

The pick rule (the legacy one): among accounts under their caps and under the quota guard,
the lowest load wins (ties by name). No account under the guard →
``NEEDS OPERATOR: no account under quota — …`` as the row's reason, never an exception.

Reserved S1 capacity: while any S1 Bug is open, a lane's non-``BUG → FIX`` rows may use at most
``lane cap − reserve`` slots; the reserved slot only goes to a ``BUG → FIX`` row.

The session ledger is ``~/.ASF/state/<product>/sessions.jsonl``, append-only: the first line
for a job is its launch record, later lines for the same job are field updates (``ended``,
``end_reason``, ``corrected``); :func:`load_sessions` folds them, last write wins.
"""
import datetime
import json
import os
import re

from asf import env
from asf.workers import quota as quota_mod

DEFAULT_RESERVE = {'local': 1, 'cloud': 1}
SESSION_FIELDS = ('job', 'item', 'feature', 'kind', 'account', 'model', 'pid', 'worktree',
                  'branch', 'started')

REASON_RESERVED = 'reserved for S1'
REASON_FULL = 'pool full'
REASON_NO_QUOTA = ('NEEDS OPERATOR: no account under quota — wait for a window to reset, or '
                   'add an account under worker_pool.accounts (see: asf workers quota)')


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---- accounts ---------------------------------------------------------------

class Account:
    def __init__(self, name, role='local', cap=1, caps=None, home=None, config_dir=None):
        self.name = name
        self.role = role or 'local'
        self.cap = int(cap if cap is not None else 1)
        self.caps = {str(k).lower(): int(v) for k, v in (caps or {}).items()}
        self.home = home
        self.config_dir = config_dir

    @classmethod
    def from_dict(cls, d):
        return cls(d['name'], role=d.get('role'), cap=d.get('cap', 1), caps=d.get('caps'),
                   home=d.get('home'), config_dir=d.get('config_dir'))

    def __repr__(self):
        return f'Account({self.name!r}, role={self.role!r}, cap={self.cap})'


def accounts_from_config(cfg):
    return [Account.from_dict(a) for a in (((cfg or {}).get('worker_pool') or {}).get('accounts') or [])
            if isinstance(a, dict) and a.get('name')]


def reserve_from_config(cfg):
    out = dict(DEFAULT_RESERVE)
    r = ((cfg or {}).get('worker_pool') or {}).get('reserve_for_s1')
    if isinstance(r, dict):
        out.update({k: int(v) for k, v in r.items()})
    return out


# ---- the feeder row ---------------------------------------------------------

# `STATE → ACTION ITEM "title" [(S1)] [(anything)]   → launch <job> (<Model>)`
ROW_RE = re.compile(r'^(?P<state>\S+) → (?P<action>\S+) +(?P<item>\S+) +"(?P<title>.*)"'
                    r'(?P<mid>.*?)→ launch (?P<job>\S+) \((?P<model>[^)]+)\)\s*$')
SEV_RE = re.compile(r'\((S[0-4])\)')
ACTION_KIND = {'FIX': 'fix-bug', 'SPEC': 'spec', 'PLAN': 'plan', 'BUILD': 'task', 'TASK': 'task'}


class Row:
    """One feeder row: something to launch. ``is_fix`` = a ``BUG → FIX`` row."""

    def __init__(self, job, item, state='', action='', title='', model='', kind=None,
                 severity=None, feature=None, lane=None, branch=None, test=None):
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


# The fields that belong to ONE run of a job. A new launch line writes them as null so the
# fold below clears them (B-0041); `load_sessions` then drops the nulls.
RUN_FIELDS = ('ended', 'end_reason', 'rc', 'corrected', 'operator_flagged', 'harvested',
              'correction', 'rounds')


def load_sessions(product):
    """``{job: folded record}`` in launch order. A run field whose folded value is null is
    dropped — that is how a relaunch line clears the previous run's terminal fields."""
    out = {}
    path = sessions_path(product)
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get('job'):
                out.setdefault(rec['job'], {}).update(rec)
    return {job: {k: v for k, v in s.items() if not (k in RUN_FIELDS and v is None)}
            for job, s in out.items()}


def update_session(product, job, **fields):
    append_session(product, dict(fields, job=job))


def live_sessions(product):
    return [s for s in load_sessions(product).values() if not s.get('ended')]


# ---- the pool ---------------------------------------------------------------

class Pool:
    """Accounts + their current load + the guard. ``live`` is the list of live session records
    (each with ``account``, ``model``); :meth:`take` adds one as a wave launches."""

    def __init__(self, accounts, quota_source=None, guards=None, reserve=None, live=()):
        self.accounts = list(accounts)
        self.quota = quota_source or quota_mod.NoQuotaSource()
        self.guards = guards or dict(quota_mod.DEFAULT_GUARDS)
        self.reserve = dict(DEFAULT_RESERVE if reserve is None else reserve)
        self.live = [dict(s) for s in live]
        self._usage = {}

    @classmethod
    def from_config(cls, cfg, product, quota_source=None):
        return cls(accounts_from_config(cfg),
                   quota_source=quota_source or quota_mod.source_from_config(cfg),
                   guards=quota_mod.guards_from_config(cfg), reserve=reserve_from_config(cfg),
                   live=live_sessions(product))

    def usage(self, account):
        if account.name not in self._usage:
            self._usage[account.name] = self.quota.read(account)
        return self._usage[account.name]

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
        guarded = [a for a in room if quota_mod.under_guard(self.usage(a), self.guards)[0]]
        if not guarded:
            return None, REASON_NO_QUOTA
        guarded.sort(key=lambda a: (self.load(a), a.name))
        return guarded[0], ''

    def take(self, account, model, job=''):
        self.live.append({'job': job, 'account': account.name, 'model': model})
