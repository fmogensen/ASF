"""asf.tick.wave_clock — the wave's own clock (T-defect #54): launches without waiting on the
product's long tick.

Even with wave-first (#782) the wave still runs inside the product's one tick, behind whichever
clock fires it: on a busy product that clock's own health, harvest and CI-log work can run 20+
minutes, so a row the feeder would launch, or a free seat, sits idle the whole time, and a
breaker cool-down edit is only read at the next tick. Every job of a product shares one tick lock
(:func:`asf.tick.tick.lock_path`/:func:`asf.tick.tick.acquire_lock`), so a second, short clock
carrying the ``wave`` step would just queue behind the same lock.

This module is that second clock's own job instead: ``asf wave --product <p>`` runs the launch
path alone — read the record, plan, screen and spawn (:func:`asf.tick.step_wave.launch_now`) —
under its own lock (:func:`lock_path`, ``tick-wave.lock``), never the product's tick lock, and on
its own, short-lived clone of the record (:func:`record_dir`): fetched and reset to origin fresh
every run (:func:`asf.tick.shadow.ensure_clone`), so a tick committing to its own ``…/record``
clone at the same moment never collides with this job's read, and this job never pushes a thing —
whatever it reads is a consistent snapshot of what origin held at the moment it fetched.

Installed by the scheduler like the merge queue's own 60s job (:data:`asf.scheduler.WAVE_CLOCK`,
#786's pattern) — every product whose clocks carry a ``wave`` step gets it, unless the product
already declares its own clock named ``wave`` or ``clocks.wave: off`` turns it off
(:func:`off`). The product's tick keeps its own ``wave`` step; it skips running it only when this
job ran within the clock's own period (:func:`ran_recently`), read by
:mod:`asf.tick.tick` — so a tick landing between two of this job's runs still launches, and the
two are never both mid-launch for long. Either way a row is launched once: every spawn goes
through :func:`asf.workers.wave.wave`'s own cross-process seat claim
(:class:`asf.workers.pool.Pool`, under :mod:`asf.workers.seats`'s lock) and a job already on the
session ledger plans as already running (:func:`asf.tick.step_wave.inflight`) — the same guards
two products' waves already share, now shared between this job and its own product's tick.

Breaker and quota config are never cached here: :func:`asf.workers.cloud.settings` and
:class:`asf.workers.pool.Pool` both read ``config.yaml`` fresh on every call, so a cool-down edit
takes effect on this job's very next run, not the product's next long tick.
"""
import calendar
import fcntl
import json
import os
import time

from asf import env

LOCK_NAME = 'tick-wave.lock'
MARKER_NAME = 'wave-job.json'
RECORD_DIRNAME = 'wave-record'

#: the clock's own default period (``clocks.wave.every``, :mod:`asf.scheduler`); also the window
#: :func:`ran_recently` uses by default — the product's own wave step skips only while this job's
#: next run is already due soon
DEFAULT_EVERY_S = 60
MIN_EVERY_S = 60

_TIME_FMT = '%Y-%m-%dT%H:%M:%SZ'


def lock_path(product):
    """The wave job's own lock — never :func:`asf.tick.tick.lock_path`, the product tick's."""
    return os.path.join(env.state_dir(product), LOCK_NAME)


def record_dir(product):
    """This job's own clone of the record — never :func:`asf.tick.shadow.record_dir`, the
    product tick's own (shared across every one of its steps, reset under the tick lock)."""
    return os.path.join(env.state_dir(product), RECORD_DIRNAME)


def marker_path(product):
    return os.path.join(env.state_dir(product), MARKER_NAME)


def _stamp(now=None):
    return time.strftime(_TIME_FMT, time.gmtime(now))


def _epoch(at):
    try:
        return calendar.timegm(time.strptime(at, _TIME_FMT))
    except (TypeError, ValueError):
        return None


def _flock(path, wait_s=0):
    f = open(path, 'a')
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(0.2)


def acquire_lock(product):
    """This job's own lock (an open file holding ``flock``), or ``None`` when another run of it
    for this product is still going. Never waits: a clock firing every minute skips rather than
    queuing behind a slow run."""
    return _flock(lock_path(product))


def off(cfg=None):
    """``clocks.wave: off`` — the dedicated clock is never installed and this job is a no-op; the
    product's own tick keeps launching from its wave step alone, exactly as before #54."""
    cfg = env.load_config() if cfg is None else cfg
    return str((cfg.get('clocks') or {}).get('wave', '')).strip().lower() == 'off'


def every_s(cfg=None):
    """``clocks.wave.every`` (a duration, :func:`asf.tick.stale.limit_seconds`'s syntax — ``1m``,
    ``90s``), else :data:`DEFAULT_EVERY_S`; never under :data:`MIN_EVERY_S`, the floor every other
    clock's ``every:`` keeps (:func:`asf.scheduler._clock_refusal`)."""
    from asf.tick import stale
    cfg = env.load_config() if cfg is None else cfg
    wave = (cfg.get('clocks') or {}).get('wave')
    raw = wave.get('every') if isinstance(wave, dict) else None
    if raw is None:
        return DEFAULT_EVERY_S
    try:
        return max(MIN_EVERY_S, stale.limit_seconds(raw))
    except ValueError:
        return DEFAULT_EVERY_S


def note_ran(product, now=None):
    """Record that this job just completed without raising — read back by :func:`ran_recently`
    (the product's own tick skips its wave step while this is recent) — whether or not it
    launched anything: a run that found nothing to launch still means the clock is alive and
    due again soon. Never called for a run that raised (B-84831): a clock that errors every
    time would otherwise mark itself "recent" forever, and the tick's own wave step would skip
    forever behind it, leaving a launchable row idle with a free seat with no way out."""
    rec = {'at': _stamp(now)}
    path = marker_path(product)
    tmp = f'{path}.tmp-{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(rec, f)
    os.replace(tmp, path)


def last_ran_at(product):
    """Epoch seconds of this job's last run, or ``None`` — never raises: no marker (the clock has
    never fired, or is off) reads the same as "not recently"."""
    try:
        with open(marker_path(product), encoding='utf-8') as f:
            at = json.load(f).get('at')
    except (OSError, ValueError, AttributeError):
        return None
    return _epoch(at)


def ran_recently(product, within_s=None, now=None):
    """Whether this job ran for ``product`` within the last ``within_s`` seconds (default
    :func:`every_s`'s own — its next run is already due about then)."""
    at = last_ran_at(product)
    if at is None:
        return False
    within_s = every_s() if within_s is None else within_s
    now = time.time() if now is None else now
    return (now - at) < within_s


class _Context:
    """What :func:`asf.tick.step_wave.launch_now` needs of a tick's own
    :class:`asf.tick.tick.Context` — nothing more: this job shares no state with the product's
    tick and commits nothing of its own. ``record_root`` is this job's own clone
    (:func:`record_dir`), fetched and reset to origin fresh every call; ``event`` goes nowhere —
    the product's own tick still records its own wave's events, launches included, the way it
    always has, and this job has no commit of its own to carry them in."""

    def __init__(self, product):
        self.product = product
        self.counts = {'launches': 0}
        self._root = None

    def record_root(self):
        if self._root is None:
            from asf.tick import shadow
            self._root = shadow.ensure_clone(self.product, record_dir(self.product))
        return self._root

    def event(self, kind, **fields):
        return None


def run(product, out=print):
    """One run of the wave job: plan and spawn (:func:`asf.tick.step_wave.launch_now`) under this
    job's own lock, never the product's tick lock. Returns 0 whether or not anything launched;
    only a lock already held, or ``clocks.wave: off``, makes it a no-op (also 0 — a clock that
    exits non-zero for finding nothing to do pages an operator for no reason). A run that raises
    leaves no marker (B-84831): :func:`note_ran` fires only once this call is known to have
    completed, so a clock stuck erroring every time goes stale and the product's own tick
    (:func:`asf.tick.tick.wave_job_recent`) takes over launching again instead of skipping
    behind it forever."""
    if off():
        out('wave: clocks.wave is off — nothing to do')
        return 0
    lock = acquire_lock(product)
    if lock is None:
        out('wave: another run for this product is still going — skipped')
        return 0
    try:
        from asf.tick import step_wave
        ctx = _Context(product)
        rc = step_wave.launch_now(ctx, out=out)
        note_ran(product)
        return rc
    except Exception as e:  # noqa: BLE001 — a scheduled clock never dies silently mid-log
        import traceback
        out(f'wave: FAILED {(str(e) or type(e).__name__).splitlines()[0]}')
        out(traceback.format_exc().rstrip())
        return 1
    finally:
        lock.close()


def cmd_wave(args):
    product = env.load_product(getattr(args, 'product', None))
    return run(product)


def register(subparsers):
    p = subparsers.add_parser(
        'wave', help="the wave's own clock: the launch path alone, under tick-wave.lock, never "
                     "the product's tick lock (asf.tick.wave_clock, #54)")
    p.add_argument('--product')
