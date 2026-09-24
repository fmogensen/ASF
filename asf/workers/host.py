"""asf.workers.host — whether this host has room for one more session: a policy, not a guess.

The READ is a probe: ``probe().read() -> {'load15', 'cores', 'swap_pct'}`` — the 15-minute load
average (``os.getloadavg``), the core count, and swap in use as a percent (macOS ``sysctl -n
vm.swapusage``; Linux ``/proc/meminfo``). A value the probe cannot read is ``None``, and ``None``
never holds anything: an unreadable host is not a loaded one. ``$ASF_HOST_READING`` (``"<load15>
<cores> <swap_pct>"``) stands in for the host — the suite's quiet host, or an operator asking
what the tick would do at a given load.

The GUARD is two thresholds from ``config.yaml host_guards``: ``load_per_core`` (the 15-minute
load over the core count) and ``swap_pct``. At or above either, the tick starts no new session;
running sessions are never touched. Defaults (the key absent): 2.0 per core, 85 % swap. A value
of 0 turns that one guard off.
"""
import os
import re
import subprocess

DEFAULT_LOAD_PER_CORE = 2.0
DEFAULT_SWAP_PCT = 85
READING_ENV = 'ASF_HOST_READING'

_SWAPUSAGE_RE = re.compile(r'total\s*=\s*([\d.]+)M\s+used\s*=\s*([\d.]+)M')


def guards_from_config(cfg):
    """``{'load_per_core': float|None, 'swap_pct': float|None}`` — ``None`` is a guard turned
    off (0 or less in the config); an absent key takes its default."""
    g = (cfg or {}).get('host_guards')
    g = g if isinstance(g, dict) else {}
    out = {}
    for key, default in (('load_per_core', DEFAULT_LOAD_PER_CORE), ('swap_pct', DEFAULT_SWAP_PCT)):
        v = g.get(key)
        try:
            v = default if v is None else float(v)
        except (TypeError, ValueError):
            v = default
        out[key] = v if v > 0 else None
    return out


def judge(reading, guards):
    """``(held, why)``; ``why`` reads ``host pressure load 90/cores 12, swap 87%``."""
    r = reading or {}
    load, cores, swap = r.get('load15'), r.get('cores'), r.get('swap_pct')
    over_load = (guards.get('load_per_core') is not None and load is not None and cores
                 and float(load) >= guards['load_per_core'] * cores)
    over_swap = (guards.get('swap_pct') is not None and swap is not None
                 and float(swap) >= guards['swap_pct'])
    if not (over_load or over_swap):
        return False, ''
    parts = []
    if load is not None and cores:
        parts.append(f'load {float(load):.0f}/cores {cores}')
    if swap is not None:
        parts.append(f'swap {float(swap):.0f}%')
    return True, 'host pressure ' + ', '.join(parts)


def parse_swapusage(text):
    """macOS ``sysctl -n vm.swapusage`` → percent used; ``None`` when unparsable."""
    m = _SWAPUSAGE_RE.search(text or '')
    if not m:
        return None
    total, used = float(m.group(1)), float(m.group(2))
    return 0.0 if total <= 0 else used * 100.0 / total


def parse_meminfo(text):
    """Linux ``/proc/meminfo`` → swap percent used; ``None`` when the fields are missing."""
    vals = {}
    for line in (text or '').splitlines():
        key, _, rest = line.partition(':')
        if key in ('SwapTotal', 'SwapFree'):
            try:
                vals[key] = float(rest.split()[0])
            except (IndexError, ValueError):
                return None
    if 'SwapTotal' not in vals or 'SwapFree' not in vals:
        return None
    total = vals['SwapTotal']
    return 0.0 if total <= 0 else (total - vals['SwapFree']) * 100.0 / total


class SystemProbe:
    """The host itself. Never raises: what it cannot read is ``None``."""

    def read(self):
        try:
            load = os.getloadavg()[2]
        except (OSError, AttributeError):
            load = None
        return {'load15': load, 'cores': os.cpu_count(), 'swap_pct': self._swap()}

    @staticmethod
    def _swap():
        try:
            with open('/proc/meminfo', encoding='utf-8') as f:
                return parse_meminfo(f.read())
        except OSError:
            pass
        try:
            p = subprocess.run(['sysctl', '-n', 'vm.swapusage'], capture_output=True, text=True,
                               timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return parse_swapusage(p.stdout) if p.returncode == 0 else None


class FixedProbe:
    """One reading, whatever the host says (``$ASF_HOST_READING``, or a test)."""

    def __init__(self, load15=None, cores=None, swap_pct=None):
        self.reading = {'load15': load15, 'cores': cores, 'swap_pct': swap_pct}

    def read(self):
        return dict(self.reading)


def probe():
    """``$ASF_HOST_READING`` (``"<load15> <cores> <swap_pct>"``) when set, else the host."""
    fixed = os.environ.get(READING_ENV, '').split()
    if len(fixed) == 3:
        try:
            return FixedProbe(float(fixed[0]), int(fixed[1]), float(fixed[2]))
        except ValueError:
            pass
    return SystemProbe()


def pressure(cfg, source=None):
    """``(held, why, reading)`` for this host now, under ``cfg``'s ``host_guards``."""
    reading = (source or probe()).read()
    held, why = judge(reading, guards_from_config(cfg))
    return held, why, reading
