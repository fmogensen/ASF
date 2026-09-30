"""asf.gh_limit — a GitHub rate limit is UNKNOWN, never state.

Every ``gh`` wrapper reads a failed call as *something*: "no runs", "PR not found", "checks
empty", "merge refused". When the shared token's hourly core limit runs out (2026-09-30 07:41Z:
the ticks, the lane, the ci-queue and a product session's watchers all on one 5,000/hr token)
every call fails at once, and each of those readings is wrong — a lane that reads a refused
merge as "WAITING", a queue that reads "no runs" and re-runs, a check reader that reads "empty"
as green.

So a rate-limited response is not a failure a caller interprets. The wrapper that ran it calls
:func:`inspect`, which raises :class:`RateLimited` — a :class:`BaseException`, like
``KeyboardInterrupt``, so no ``except Exception`` fallback anywhere between the wrapper and the
top of the pass can swallow it into a "failed → act anyway" branch. From then on this process is
*latched*: every later wrapper call raises before it spawns (:func:`guard`), so no second call
burns quota or returns a misleading empty answer. The tick's step runner and the CLI entry catch
it, print one line, and the decision waits for the next tick.

The budget (:func:`low`): ``gh api rate_limit`` does not count against the core limit, so a pass
reads it before its GitHub-heavy work and skips the polling it can do without when the remaining
calls are under the product's reserve (``conventions.gh_rate_reserve``, default
:data:`DEFAULT_RESERVE`).
"""
import json
import os
import re
import subprocess
import sys
import time

#: remaining core calls under which non-essential polling is skipped this pass
DEFAULT_RESERVE = 500

#: what GitHub (and ``gh``) print for a primary or secondary rate limit
_LIMIT_RE = re.compile(
    r'API rate limit exceeded|secondary rate limit|rate limit exceeded|'
    r'x-ratelimit-remaining:\s*0\b|HTTP 429|abuse detection',
    re.IGNORECASE)


class RateLimited(BaseException):
    """GitHub refused a call for rate: nothing read from it (or after it, in this process) is
    state. A BaseException on purpose: never caught by an ``except Exception`` fallback."""


#: how long a latch holds a long-lived process (a watch loop) off GitHub; a tick or a queue pass
#: ends well inside it, so for them the latch is the process's life
LATCH_S = 600

#: how long a read is reused inside one process: a pass asks the same listing from several
#: places (the capacity read five times a tick, the runners four) — seconds apart, one answer
MEMO_S = 30

_state = {'latched': None, 'latched_at': 0.0, 'said': False, 'budget': {}, 'low_said': False,
          'memo': {}}


def is_rate_limited(*texts):
    """True when any of ``texts`` (a call's stderr/stdout) is GitHub's rate-limit answer."""
    return any(_LIMIT_RE.search(t) for t in texts if isinstance(t, str) and t)


def latched():
    """The reason this process stopped calling GitHub, or None."""
    if _state['latched'] and time.monotonic() - _state['latched_at'] > LATCH_S:
        _state.update(latched=None, said=False, budget={}, low_said=False)
    return _state['latched']


def reset():
    """Forget the latch, the cached budget and the read memo (tests)."""
    _state.update(latched=None, latched_at=0.0, said=False, budget={}, low_said=False, memo={})


def say(line, out=None):
    """Print ``line`` once per process — the one line a rate limit costs the log."""
    if _state['said']:
        return
    _state['said'] = True
    print(line, file=out or sys.stderr, flush=True)


def guard(args=()):
    """Before a wrapper spawns ``gh``: raise :class:`RateLimited` while this process is latched."""
    if latched():
        raise RateLimited(_state['latched'])


def trip(reason, args=()):
    """Latch this process and raise :class:`RateLimited` for ``reason``."""
    what = ' '.join(str(a) for a in list(args)[:2])
    line = f'gh: GitHub rate limit — {reason}' + (f' (on gh {what})' if what else '') + \
        '; no GitHub decision this pass, next tick retries'
    _state['latched'], _state['latched_at'] = line, time.monotonic()
    say(line)
    raise RateLimited(line)


def inspect(args, returncode, stdout='', stderr=''):
    """After a wrapper ran ``gh args``: a failed call whose output is a rate-limit answer latches
    the process and raises :class:`RateLimited`; anything else returns quietly."""
    if returncode == 0 or not isinstance(returncode, int):
        return
    if is_rate_limited(stderr, stdout):
        lines = (stderr or stdout or '').strip().splitlines()
        trip(lines[-1].strip()[:160] if lines else 'rate limited', args)


def inspect_proc(args, proc):
    """:func:`inspect` over a finished process (or any object carrying its fields)."""
    inspect(args, getattr(proc, 'returncode', 0), getattr(proc, 'stdout', '') or '',
            getattr(proc, 'stderr', '') or '')


def cmd_is_gh(cmd):
    """True when ``cmd`` (an argv list or a shell string) runs ``gh``."""
    if isinstance(cmd, str):
        return cmd.lstrip().startswith('gh ')
    return bool(cmd) and os.path.basename(str(cmd[0])) == 'gh'


# -------------------------------------------------------------------------------- memo --

def memo_get(key):
    """A read's output this process got under ``key`` within :data:`MEMO_S`, else None."""
    hit = _state['memo'].get(key)
    if hit is None or time.monotonic() - hit[0] > MEMO_S:
        return None
    return hit[1]


def memo_put(key, value):
    """Keep a successful read's output under ``key``."""
    if value is not None:
        _state['memo'][key] = (time.monotonic(), value)


def forget():
    """Drop every memoised read: a write this process made can change any of them."""
    _state['memo'].clear()


# ------------------------------------------------------------------------------ budget --

def reserve_of(product):
    """The product's ``conventions.gh_rate_reserve`` (an int ≥ 0), else :data:`DEFAULT_RESERVE`."""
    try:
        v = product.conventions.get('gh_rate_reserve')
    except Exception:  # noqa: BLE001 — no conventions readable: the default
        v = None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return DEFAULT_RESERVE
    return v if v >= 0 else DEFAULT_RESERVE


def remaining(product=None, run=None, env=None):
    """The core calls left on the token ``product``'s ``gh`` uses (``gh api rate_limit``, which
    costs none of them), or None when unreadable. Read once per process per token."""
    if env is None:
        try:
            from asf import ci_pool
            env = ci_pool._gh_env(product) if product is not None else dict(os.environ)
        except Exception:  # noqa: BLE001 — no product env: the ambient login
            env = dict(os.environ)
    key = env.get('GH_TOKEN') or env.get('GITHUB_TOKEN') or ''
    if key in _state['budget']:
        return _state['budget'][key]
    try:
        p = (run or subprocess.run)(['gh', 'api', 'rate_limit', '--jq', '.resources.core'],
                                    capture_output=True, text=True, timeout=30, env=env)
        core = json.loads(p.stdout) if p.returncode == 0 else None
        left = int(core['remaining']) if isinstance(core, dict) else None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        left = None
    _state['budget'][key] = left
    return left


def low(product=None, run=None, env=None, out=None):
    """True when the token's remaining core calls are under the product's reserve: the pass
    skips its non-essential polling (one line says so, once). Unreadable is not low."""
    left = remaining(product, run=run, env=env)
    floor = reserve_of(product) if product is not None else DEFAULT_RESERVE
    if left is None or left >= floor:
        return False
    if not _state.get('low_said'):
        _state['low_said'] = True
        print(f'gh: {left} GitHub calls left this hour (reserve {floor}) — non-essential polling'
              ' skipped this pass', file=out or sys.stderr, flush=True)
    return True
