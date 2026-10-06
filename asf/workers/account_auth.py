"""asf.workers.account_auth — an account an auth error refused is out of the pool from its FIRST
failure, until an operator re-enables it.

A session whose launch the provider refuses for the account (an org not allowed, a 401/403, a
disabled subscription) fails in seconds, and the next launch on that account fails the same way:
one such account once burned seven jobs over ~2.5 h before anyone noticed. So:

* **The match.** A run's result text — or, a run that died with no result line, the raw text of
  its log's last run (the CLI's stderr lands there) — matched against
  ``worker_pool.auth_error_patterns`` (a list of regexes, matched ignoring case; unset, the
  runtime connector's :data:`asf.connectors.claude_code.DEFAULT_AUTH_ERROR_PATTERNS`). A match is
  the run's ``failed: auth`` (:data:`AUTH`).
* **The block.** Its account is written to ``~/.ASF/state/account-auth.json`` — one file for the
  machine, since the accounts are — and the pool's band reads it as ``stop`` whatever the quota
  reading says (:meth:`asf.workers.pool.Pool.band`), as do the capacity share and ``asf workers
  quota``. It stays until ``asf workers enable <account>`` (:func:`enable`), or until a run
  launched on it after the block finishes — a probe (:func:`proved`).
* **One alarm.** The failure that blocks the account prints the one ``ALARM`` line (its entry
  is then marked ``alarmed``), and doctor shows one red ``account auth`` row while any account
  is blocked; a later failure on an already-blocked account (a run launched before the block)
  prints a plain line.
* **Not burned.** A ``failed: auth`` run is the account's fault, not the work's: no attempt, no
  round, no hold (:func:`asf.workers.lifecycle.quota_exhausted`) — the item relaunches on another
  account, or, with no other account, waits; the alarm says which.

No pattern matches, no account is configured, or nothing ever fails: nothing changes.
"""
import datetime
import json
import os
import re

from asf import env
from asf.connectors.claude_code import DEFAULT_AUTH_ERROR_PATTERNS

#: The failure signature (``failed: auth``).
AUTH = 'auth'
ALARM = 'ALARM'

_cache = {}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def raw_patterns(cfg=None):
    """``worker_pool.auth_error_patterns`` from ``cfg`` (default: ``~/.ASF/config.yaml``), else
    the runtime connector's defaults."""
    if cfg is None:
        try:
            cfg = env.load_file(env.config_path())
        except Exception:  # noqa: BLE001 — an unreadable config falls back to the defaults
            cfg = {}
    v = ((cfg or {}).get('worker_pool') or {}).get('auth_error_patterns')
    if isinstance(v, (list, tuple)) and v:
        return tuple(str(p) for p in v)
    return DEFAULT_AUTH_ERROR_PATTERNS


def compiled(cfg=None):
    """The patterns as one compiled regex (a malformed entry is matched as plain text), cached
    on the config file's mtime when read from disk."""
    key = None
    if cfg is None:
        try:
            key = (env.config_path(), os.path.getmtime(env.config_path()))
        except OSError:
            key = (env.config_path(), None)
        if key in _cache:
            return _cache[key]
    parts = []
    for p in raw_patterns(cfg):
        try:
            re.compile(p)
            parts.append(f'(?:{p})')
        except re.error:
            parts.append(re.escape(p))
    rx = re.compile('|'.join(parts), re.I) if parts else re.compile(r'(?!x)x')
    if key is not None:
        _cache.clear()
        _cache[key] = rx
    return rx


class _Matcher:
    """The ``auth`` entry of :data:`asf.workers.runtime.FAILURE_SIGNATURES`: ``.search`` against
    the configured patterns, read when it is called."""

    def search(self, text):
        return compiled().search(text or '')


MATCHER = _Matcher()


def log_text(log_path):
    """The non-JSON lines of the log's last run (after its last launch boundary), joined — what
    the CLI printed outside its stream, an auth refusal before any ``init`` among it."""
    if not log_path or not os.path.exists(log_path):
        return ''
    from asf.workers import runtime as runtime_mod
    lines = []
    with open(log_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                lines.append(line.strip())
                continue
            if runtime_mod.launch_boundary(rec):
                lines = []
    return '\n'.join(lines)


def in_log(log_path):
    """True when the raw text of the log's last run matches an auth-error pattern."""
    return bool(MATCHER.search(log_text(log_path)))


# ---- the blocked accounts -----------------------------------------------------------

def path():
    return os.path.join(env.ASF_HOME, 'state', 'account-auth.json')


def blocked():
    """``{account: {at, job, product, error}}`` — every account an auth error took out."""
    try:
        with open(path(), encoding='utf-8') as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return {a: v for a, v in got.items() if isinstance(v, dict)} if isinstance(got, dict) else {}


def _write(table):
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(table, f, sort_keys=True, indent=1)
    os.replace(tmp, p)


def block(account, job='', product='', error=''):
    """Take ``account`` out of the pool; True only when it was not blocked already."""
    table = blocked()
    if account in table:
        return False
    table[account] = {'at': _now_iso(), 'job': job, 'product': product, 'error': error}
    _write(table)
    return True


def _alarm_once(account, error):
    """True the first time this block is alarmed: the entry is marked ``alarmed`` (a block the
    pool made before health's pass, :func:`asf.workers.lifecycle.note_spent_windows`, is
    alarmed by the first :func:`note`)."""
    table = blocked()
    rec = table.get(account)
    if rec is None or rec.get('alarmed'):
        return False
    rec['alarmed'] = _now_iso()
    rec['error'] = rec.get('error') or error
    _write(table)
    return True


def enable(account):
    """Put ``account`` back in the pool; True when it was blocked."""
    table = blocked()
    if table.pop(account, None) is None:
        return False
    _write(table)
    return True


def proved(run):
    """A run that FINISHED on a blocked account and started after its block — an operator's
    ``asf workers spawn --account`` probe — re-enables it; the line to print, or None."""
    acct = (run or {}).get('account') or ''
    rec = blocked().get(acct)
    from asf.workers import headroom
    started, at = headroom.parse_ts(run.get('started')), headroom.parse_ts((rec or {}).get('at'))
    if rec is None or started is None or at is None or started <= at:
        return None
    enable(acct)
    return f'{acct} re-enabled: a session on it finished after its auth error'


def enable_hint(account):
    return f'asf workers enable {account}'


def stop_reason(account):
    """The pool's band text for a blocked account."""
    return f'auth error — unusable until `{enable_hint(account)}`'


def _configured_accounts():
    try:
        cfg = env.load_file(env.config_path())
    except Exception:  # noqa: BLE001 — no config: no other account known
        return ()
    accts = ((cfg or {}).get('worker_pool') or {}).get('accounts') or ()
    return tuple(a.get('name') for a in accts if isinstance(a, dict) and a.get('name'))


def note(product, run, text, accounts=None):
    """Block the account of a run an auth error ended; the line to print — the ``ALARM`` for the
    failure that blocks it, a plain line for one on an account already blocked. ``accounts``:
    the pool's account names (default: ``worker_pool.accounts``), to say whether the item
    relaunches elsewhere or waits."""
    acct = (run or {}).get('account') or ''
    m = MATCHER.search(text or '')
    error = (m.group(0) if m else '').strip()[:120]
    if not acct:
        return f'auth error ({error or "?"}) on a run with no account — no attempt spent'
    block(acct, job=(run or {}).get('job', ''),
          product=getattr(product, 'name', product) or '', error=error)
    first = _alarm_once(acct, error)
    names = _configured_accounts() if accounts is None else tuple(accounts)
    others = [a for a in names if a != acct and a not in blocked()]
    where = ('the item relaunches on another account, no attempt spent' if others else
             'no other account: the item waits, no attempt spent')
    if not first:
        return f'{acct} already unusable (auth error) — {where}'
    return (f'{ALARM} account {acct} unusable: auth error on launch ({error or "?"}) — {where}; '
            f're-enable: {enable_hint(acct)}')


def doctor_row():
    """``(ok, detail)`` — one red row naming every blocked account, or None when none is."""
    table = blocked()
    if not table:
        return None
    shown = ', '.join(f"{a} ({v.get('error') or 'auth error'}, since {v.get('at') or '?'})"
                      for a, v in sorted(table.items()))
    hints = '; '.join(enable_hint(a) for a in sorted(table))
    return False, (f'{ALARM} {len(table)} account(s) unusable after an auth error: {shown} — '
                   f'its jobs go to other accounts or wait; re-enable: {hints}')
