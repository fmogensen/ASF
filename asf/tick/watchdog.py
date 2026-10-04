"""asf.tick.watchdog — a tick's wall-clock budget: past it, the tick names its step and exits.

A tick holds ``state/<product>/tick.lock`` for as long as it runs, and every later tick of the
product skips while it does (:func:`asf.tick.tick.acquire_lock`). A tick that never ends — a
step spinning at 100% CPU (2026-10-04: a product's wave step ran 23+ minutes after its lane pass
and launched nothing meanwhile) — held the lock forever: nothing in the tick bounded it.

:func:`arm` starts a daemon timer when the tick takes the lock. When it fires, the tick prints one
``tick: over its wall-clock budget …`` line naming the step it was in (:func:`enter`, set by the
step loop), flushes, and ends the process (``os._exit``): the lock is an ``flock`` on an open
file, so the kernel releases it with the process, and the next tick starts on schedule. An
``os._exit`` rather than an exception: a step's broad ``except Exception`` would swallow one, and
the spinning loop would go on.

The budget is config ``tick.budget_s`` when set (``0`` turns the watchdog off), else
:data:`INTERVAL_FACTOR` × the interval of the clock that runs these steps, never under
:data:`FLOOR_S` (a loaded host's healthy ticks have run 40+ minutes; the budget is for the
tick that never ends, not the slow one), and :data:`DEFAULT_S` for a clock with no interval
(a timed clock, a hand-run tick).
"""
import os
import sys
import threading
import time

from asf import env

#: the budget is this many of the clock's own intervals
INTERVAL_FACTOR = 3
#: never under this (seconds): a tick on a loaded host has taken 43 min and finished fine
FLOOR_S = 60 * 60
#: a clock with no interval (a timed clock, a hand-run ``asf tick``)
DEFAULT_S = 2 * 60 * 60
#: the exit code of a tick ended by its budget (``timeout(1)``'s own)
EXIT_CODE = 124

_state = {'step': 'start', 'since': None}


def enter(step):
    """The step the tick is in now — what the budget line names."""
    _state['step'] = step
    _state['since'] = time.monotonic()


def configured():
    """Config ``tick.budget_s`` as seconds, ``None`` when unset or unreadable."""
    try:
        v = (env.load_config().get('tick') or {}).get('budget_s')
    except Exception:  # noqa: BLE001 — an unreadable config falls back to the clock's budget
        return None
    if v is None or isinstance(v, bool):
        return None
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return None


def clock_interval(product, steps):
    """The interval (seconds) of the product's clock whose steps are ``steps``, else ``None``."""
    if not steps:
        return None
    try:
        from asf import scheduler
        for c in scheduler.clocks(product):
            if c.interval_s and list(c.steps or []) == list(steps):
                return int(c.interval_s)
    except Exception:  # noqa: BLE001 — no clock readable: the default budget
        return None
    return None


def seconds_for(product, steps, budget_s=None, interval_s=None):
    """The budget for a tick of ``steps``: ``budget_s`` (config) when given, else
    :data:`INTERVAL_FACTOR` × ``interval_s`` floored at :data:`FLOOR_S`, else :data:`DEFAULT_S`.
    0 means no budget."""
    if budget_s is not None:
        return budget_s
    if interval_s:
        return max(INTERVAL_FACTOR * interval_s, FLOOR_S)
    return DEFAULT_S


def line(seconds):
    """The one line a tick ended by its budget prints."""
    step = _state['step']
    since = _state['since']
    in_step = f' ({time.monotonic() - since:.0f}s in it)' if since is not None else ''
    return (f'tick: over its wall-clock budget ({seconds}s) in step {step}{in_step} — '
            f'exiting; the tick lock is released and the next tick starts on schedule')


def _expire(seconds, exit_fn):
    try:
        print(line(seconds), flush=True)
        print(line(seconds), file=sys.stderr, flush=True)
    finally:
        exit_fn(EXIT_CODE)


def arm(seconds, exit_fn=os._exit):
    """Start the watchdog: after ``seconds`` the tick prints :func:`line` and ``exit_fn(124)``.
    Returns the timer (``cancel()`` it when the tick ends), or ``None`` for no budget."""
    enter('start')
    if not seconds or seconds <= 0:
        return None
    t = threading.Timer(seconds, _expire, args=(seconds, exit_fn))
    t.daemon = True
    t.start()
    return t
