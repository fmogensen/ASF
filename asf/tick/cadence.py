"""asf.tick.cadence — which of a tick's deferrable parts run on this tick (``every_n``).

2026-10-06: one tick spent 22 minutes on bookkeeping (the record's backfill, plan-order,
file-bugs and rollup; health; groom) before its wave launched a session. The wave now runs right
after the record's fast parts (:func:`asf.tick.tick.run_record_fast`), and the bookkeeping that
does not decide a launch runs after it — some of it not on every tick:

* ``tick.every_n: {<name>: <n>}`` in ``config.yaml`` — every product's default;
* ``clocks.<clock>.every_n: {<name>: <n>}`` in the product file — that clock's own, over it.

``<name>`` is a deferrable record part (:data:`RECORD_TAIL`) or a tick step other than
``record`` and ``wave`` (:data:`UNGATED`: what the wave decides from, and the wave). ``n`` is a
whole number of ticks; 1 (the default for anything unnamed) is every tick. However large ``n``,
a part whose last run is older than ``tick.deferred_max_age_s`` (default 3600: at least hourly)
runs anyway, so a slow or paused clock never starves it.

What ran when lives in ``state/<product>/cadence.json`` (``{name: {skipped, last}}``): local to
the host, never in the record — a lost file only means each part runs on its next tick.
"""
import json
import os
import time

from asf import env

#: the record parts that decide no launch: they run after the wave, each on its own cadence
RECORD_TAIL = ('backfill', 'plan-order', 'file-bugs', 'rollup')
#: never gated: the wave reads the record, the wave is what the tick is for, daily keeps its own stamp
UNGATED = ('record', 'wave', 'daily')
#: a part that has not run for this long runs on the next tick whatever its ``every_n``
DEFAULT_MAX_AGE_S = 3600
#: ``config.yaml``'s product-wide defaults (``tick.every_n``): the record's bookkeeping every
#: third tick; anything unnamed every tick
DEFAULT_EVERY_N = {'backfill': 3, 'plan-order': 3, 'file-bugs': 3, 'rollup': 3}
STORE = 'cadence.json'


def gateable():
    """Every name ``every_n`` may carry."""
    from asf.tick import steps
    return tuple(RECORD_TAIL) + tuple(s for s in steps.STEPS if s not in UNGATED)


def refusal(value):
    """Why ``every_n: value`` is not usable, or None."""
    if not isinstance(value, dict):
        return f'every_n must be a map of step or record part to a whole number, not {value!r}'
    names = gateable()
    for k, v in value.items():
        if k not in names:
            return f"every_n: {k} is not gateable ({', '.join(names)})"
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            return f'every_n: {k} must be a whole number of ticks ≥ 1, not {v!r}'
    return None


def _tick_cfg():
    t = env.load_config().get('tick')
    return t if isinstance(t, dict) else {}


def max_age_s():
    """``tick.deferred_max_age_s`` (default 3600)."""
    v = _tick_cfg().get('deferred_max_age_s')
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 \
        else DEFAULT_MAX_AGE_S


def _clean(m):
    return {k: v for k, v in (m or {}).items()
            if isinstance(v, int) and not isinstance(v, bool) and v >= 1} \
        if isinstance(m, dict) else {}


def clock_entry(product, step_names):
    """The product file's clock entry whose steps are ``step_names``, else ``{}``."""
    for entry in ((product._get('clocks') or {}) if hasattr(product, '_get') else {}).values():
        if not isinstance(entry, dict):
            continue
        s = entry.get('steps')
        s = s if isinstance(s, list) else ([s] if s else [])
        if step_names and set(s) == set(step_names):  # --steps comes in manifest order
            return entry
    return {}


def every_n(product, step_names):
    """``{name: n}`` for this tick: the defaults, ``tick.every_n`` over them, the running
    clock's ``every_n`` over that."""
    tick_cfg = _tick_cfg()
    out = dict(DEFAULT_EVERY_N)
    if 'every_n' in tick_cfg:   # config.yaml's tick.every_n
        out.update(_clean(tick_cfg.get('every_n')))
    out.update(_clean(clock_entry(product, step_names).get('every_n')))
    return {k: v for k, v in out.items() if k not in UNGATED}


def _path(product):
    return os.path.join(env.state_dir(product), STORE)


def load(product):
    try:
        with open(_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(product, data):
    p = _path(product)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f'{p}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, sort_keys=True, indent=1)
        os.replace(tmp, p)
    except OSError:
        pass  # a lost cadence only runs each part on its next tick


class Cadence:
    """One tick's view: :meth:`due` decides and records each name once."""

    def __init__(self, product, step_names, now=None, out=print):
        self.product = product
        self.n = every_n(product, step_names)
        self.max_age = max_age_s()
        self.now = time.time() if now is None else now
        self.data = load(product)
        self.out = out

    def due(self, name):
        """True when ``name`` runs this tick; the decision is recorded (and saved) either way."""
        n = self.n.get(name, 1)
        rec = self.data.get(name) if isinstance(self.data.get(name), dict) else {}
        last = rec.get('last')
        skipped = rec.get('skipped') if isinstance(rec.get('skipped'), int) else 0
        old = not isinstance(last, (int, float)) or self.now - last >= self.max_age
        run = n <= 1 or old or skipped >= n - 1
        if run:
            self.data[name] = {'skipped': 0, 'last': self.now}
        else:
            self.data[name] = {'skipped': skipped + 1, 'last': last}
            self.out(f'tick: {name} deferred — every {n} ticks ({skipped + 1}/{n - 1} skipped; '
                     f'runs at least every {int(self.max_age // 60)} min)')
        save(self.product, self.data)
        return run
