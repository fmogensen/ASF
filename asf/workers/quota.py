"""asf.workers.quota — each account's usage windows, and the band a launch is judged against.

The READ is a provider: ``QuotaSource.read(account) -> {"five_h_pct": .., "seven_d_pct": ..}``
(optionally ``seven_d_model_pct`` — a per-model 7-day window). Two sources: ``fake`` (a dict,
for tests) and ``command`` — ``config.yaml worker_pool.quota_command``, a command line with an
``{account}`` placeholder that prints one JSON line with those keys. Nothing here knows how a
vendor's usage is actually fetched.

The BAND is two thresholds per window, read from ``config.yaml quota_guards`` (percent): at or
above ``cooldown`` an account takes one job at a time; at or above ``stop`` it takes none.
Defaults: ``stop`` 95, ``cooldown`` 90, for every window. An unreadable account reads as
``stop`` (unknown ≠ free).

**A stale reading.** The source may print ``polled_at`` (ISO) — when the vendor was last asked. A
reading older than ``quota_guards.stale_after_min`` (default :data:`STALE_AFTER_MIN`) is stale: its
percentages are history, not the account's state, so they neither band nor budget a launch. The
account is then judged by the session-limit stop alone (a run that ended on a usage limit or a
429 stops its account until the reset, :mod:`asf.workers.headroom`), and ``asf status`` says
``stale since HH:MM``.
"""
import datetime
import json
import shlex
import subprocess

DEFAULT_STOP = {'five_h': 95, 'seven_d': 95, 'seven_d_model': 95}
BAND = 5                       # how far below the stop the cooldown opens, when unnamed
WINDOW_KEYS = {'five_h': 'five_h_pct', 'seven_d': 'seven_d_pct', 'seven_d_model': 'seven_d_model_pct'}
FREE, COOLDOWN, STOP = 'free', 'cooldown', 'stop'
STALE_AFTER_MIN = 30          # minutes after ``polled_at`` a reading stops being current


def guards_from_config(cfg):
    """``{'stop': {window: pct}, 'cooldown': {window: pct}}``. The older
    ``worker_pool.quota_guard: {max_5h, max_7d}`` (fractions) and the flat ``quota_guards``
    (percent) both set the stop; the new nested ``quota_guards: {stop, cooldown}`` wins over
    both. A cooldown not named is derived as ``stop − BAND``, and every cooldown is clamped to
    at most its own stop."""
    cfg = cfg or {}
    stop = dict(DEFAULT_STOP)
    cooldown = {}
    stale_after = STALE_AFTER_MIN
    old = ((cfg.get('worker_pool') or {}).get('quota_guard')) or {}
    if old.get('max_5h') is not None:
        stop['five_h'] = float(old['max_5h']) * 100
    if old.get('max_7d') is not None:
        stop['seven_d'] = float(old['max_7d']) * 100
    g = cfg.get('quota_guards')
    if isinstance(g, dict):
        for w in WINDOW_KEYS:
            if g.get(w) is not None:
                stop[w] = float(g[w])
        if g.get('stale_after_min') is not None:
            stale_after = float(g['stale_after_min'])
        named_stop = g.get('stop') or {}
        named_cooldown = g.get('cooldown') or {}
        for w in WINDOW_KEYS:
            if named_stop.get(w) is not None:
                stop[w] = float(named_stop[w])
            if named_cooldown.get(w) is not None:
                cooldown[w] = float(named_cooldown[w])
    for w in WINDOW_KEYS:
        if w not in cooldown:
            cooldown[w] = max(0, stop[w] - BAND)
        cooldown[w] = min(cooldown[w], stop[w])
    return {'stop': stop, 'cooldown': cooldown, 'stale_after_min': stale_after}


def polled_at(usage):
    """The reading's ``polled_at`` as an aware datetime, or None when it names none."""
    raw = (usage or {}).get('polled_at')
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def stale_since(usage, guards=None, now=None):
    """When a stale reading was polled (aware datetime), else None. A reading that names no
    ``polled_at`` is taken as current."""
    polled = polled_at(usage)
    if polled is None:
        return None
    limit = (guards or {}).get('stale_after_min', STALE_AFTER_MIN)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return polled if now - polled > datetime.timedelta(minutes=float(limit)) else None


def stale_label(since):
    return f"stale since {since.astimezone().strftime('%H:%M')}"


def band(usage, guards):
    """(state, why). ``usage`` None → ``(STOP, 'quota unreadable')``; a stale reading →
    ``FREE`` (its percentages are not judged; the pool's session-limit stop still applies). Stop is judged over every
    window before cooldown is — a stop in a later window must beat a cooldown in an earlier
    one."""
    if usage is None:
        return STOP, 'quota unreadable'
    since = stale_since(usage, guards)
    if since is not None:
        return FREE, f'quota {stale_label(since)} — session limits govern'
    for gk, uk in WINDOW_KEYS.items():
        v = usage.get(uk)
        if v is not None and float(v) >= guards['stop'][gk]:
            return STOP, f'{uk} {float(v):g} ≥ {guards["stop"][gk]:g}'
    for gk, uk in WINDOW_KEYS.items():
        v = usage.get(uk)
        if v is not None and float(v) >= guards['cooldown'][gk]:
            return COOLDOWN, f'{uk} {float(v):g} ≥ {guards["cooldown"][gk]:g}'
    return FREE, ''


class QuotaSource:
    def read(self, account):
        raise NotImplementedError


class FakeQuotaSource(QuotaSource):
    """``{account_name: usage_dict | None}``; an account not listed reads as 0/0."""

    def __init__(self, table=None):
        self.table = dict(table or {})

    def read(self, account):
        name = getattr(account, 'name', account)
        if name in self.table:
            return self.table[name]
        return {'five_h_pct': 0, 'seven_d_pct': 0}


class CommandQuotaSource(QuotaSource):
    """Runs ``command`` (``{account}`` substituted) and parses the last non-empty stdout line
    as JSON. Any failure → None (the account is then not under the guard)."""

    def __init__(self, command, timeout=60):
        self.command = command
        self.timeout = timeout

    def read(self, account):
        name = getattr(account, 'name', account)
        argv = [a.replace('{account}', name) for a in shlex.split(self.command)]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if p.returncode != 0:
            return None
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        if not lines:
            return None
        try:
            rec = json.loads(lines[-1])
        except json.JSONDecodeError:
            return None
        return rec if isinstance(rec, dict) else None


class NoQuotaSource(QuotaSource):
    """No ``quota_command`` configured: nothing to read, every account reads 0/0."""

    def read(self, account):
        return {'five_h_pct': 0, 'seven_d_pct': 0}


def source_from_config(cfg):
    cmd = ((cfg or {}).get('worker_pool') or {}).get('quota_command')
    return CommandQuotaSource(cmd) if cmd else NoQuotaSource()
