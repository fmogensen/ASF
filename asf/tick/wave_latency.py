"""asf.tick.wave_latency — how long the last tick took from its start to its wave's start.

Every session the factory launches waits on that span: 2026-10-06 it was 22 minutes of
bookkeeping. The tick writes it as its wave starts (``state/<product>/wave-latency.json``,
``{at, seconds}``), it goes on the tick's ``metrics/ticks`` line as ``wave_latency_s``,
``asf status`` shows it (:func:`cell`) and the dwell watchdog alarms past its limit
(:func:`asf.dwell.wave_latency`, ``conventions.watchdog.wave_latency``, default 2 min).
"""
import datetime
import json
import os

from asf import env

STORE = 'wave-latency.json'


def path(product):
    return os.path.join(env.state_dir(product), STORE)


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def write(product, seconds, at=None):
    """Record this tick's latency; never raised (the wave matters more than its meter)."""
    data = {'at': at or _stamp(), 'seconds': round(float(seconds), 1)}
    try:
        p = path(product)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f'{p}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except OSError:
        pass
    return data


def read(product):
    """``{at, seconds}`` of the last tick that reached its wave, or None."""
    try:
        with open(path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('seconds'), (int, float)):
        return None
    return data


def limit_min(product):
    from asf import dwell
    return dwell.limits(product).get('wave_latency')


def cell(product):
    """``asf status``'s ``Wave latency`` row: ``47s (tick at HH:MM)``, with ``— over the N min
    limit`` past it; None (no row) before any tick reached a wave."""
    data = read(product)
    if not data:
        return None
    from asf.views import index_reader as ix
    secs = data['seconds']
    text = f"{secs:.0f}s (tick at {ix.local_stamp(data.get('at'), '%H:%M')})"
    lim = limit_min(product)
    if lim is not None and secs >= lim * 60:
        text += f' — over the {lim:g} min limit'
    return text
