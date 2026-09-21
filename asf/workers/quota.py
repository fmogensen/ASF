"""asf.workers.quota — each account's usage windows, and the guard a launch must be under.

The READ is a provider: ``QuotaSource.read(account) -> {"five_h_pct": .., "seven_d_pct": ..}``
(optionally ``seven_d_model_pct`` — a per-model 7-day window). Two sources: ``fake`` (a dict,
for tests) and ``command`` — ``config.yaml worker_pool.quota_command``, a command line with an
``{account}`` placeholder that prints one JSON line with those keys. Nothing here knows how a
vendor's usage is actually fetched.

The GUARD is ``config.yaml quota_guards`` (percent): ``five_h`` 92, ``seven_d`` 85,
``seven_d_model`` 90 by default. An account is under the guard when every window it reports is
below its threshold. An unreadable account is NOT under the guard (unknown ≠ free).
"""
import json
import shlex
import subprocess

DEFAULT_GUARDS = {'five_h': 92, 'seven_d': 85, 'seven_d_model': 90}
WINDOW_KEYS = {'five_h': 'five_h_pct', 'seven_d': 'seven_d_pct', 'seven_d_model': 'seven_d_model_pct'}


def guards_from_config(cfg):
    """``quota_guards:`` (percent) wins; the older ``worker_pool.quota_guard: {max_5h, max_7d}``
    (fractions) is honoured when that is all there is."""
    cfg = cfg or {}
    out = dict(DEFAULT_GUARDS)
    g = cfg.get('quota_guards')
    if isinstance(g, dict):
        for k in DEFAULT_GUARDS:
            if g.get(k) is not None:
                out[k] = float(g[k])
        return out
    old = ((cfg.get('worker_pool') or {}).get('quota_guard')) or {}
    if old.get('max_5h') is not None:
        out['five_h'] = float(old['max_5h']) * 100
    if old.get('max_7d') is not None:
        out['seven_d'] = float(old['max_7d']) * 100
    return out


def under_guard(usage, guards):
    """(ok, why). ``usage`` None → not ok."""
    if usage is None:
        return False, 'quota unreadable'
    for gk, uk in WINDOW_KEYS.items():
        v = usage.get(uk)
        if v is not None and float(v) >= guards[gk]:
            return False, f'{uk} {float(v):g} ≥ {guards[gk]:g}'
    return True, ''


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
