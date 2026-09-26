"""asf.workers.host — whether this host has room for one more session: a policy, not a guess.

The READ is a probe: ``probe().read() -> {'load15', 'load1', 'cores', 'swap_pct'}`` — the
15-minute and 1-minute load averages (``os.getloadavg``), the core count, and swap in use as a
percent (macOS ``sysctl -n vm.swapusage``; Linux ``/proc/meminfo``). A value the probe cannot
read is ``None``, and ``None`` never holds anything: an unreadable host is not a loaded one.
``$ASF_HOST_READING`` (``"<load15> <cores> <swap_pct>"``, or with a 4th field ``"<load15>
<cores> <swap_pct> <load1>"``) stands in for the host — the suite's quiet host, or an operator
asking what the tick would do at a given load.

The GUARD is two thresholds from ``config.yaml host_guards``: ``load_per_core`` (the load over
the core count) and ``swap_pct``. At or above either, the tick starts no new session; running
sessions are never touched. Defaults (the key absent): 2.0 per core, 85 % swap. A value of 0
turns that one guard off.

The load guard judges the 15-minute average, but a spike that already ended leaves load15
elevated for up to 15 minutes after load1 has dropped back down — every product would sit idle
on work the host can now easily run. Where a 1-minute reading is available, the load guard holds
only when both load1 and load15 are at/above the limit: a sustained load holds, a spike that has
already ended does not. Without a 1-minute reading (``load1`` absent, e.g. the old 3-field
``$ASF_HOST_READING``), the guard falls back to load15 alone, exactly as before.

An S1 Bug's fix session is the one row the LOAD half of this guard does not hold (the wave
step's own rule, :mod:`asf.tick.step_wave`): :func:`load_only_hold` tells that case apart from a
host over its memory/swap guard, which holds every row regardless of severity.
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


def over_parts(reading, guards):
    """``(over_load, over_mem)`` — whether this reading trips the load guard, and whether it
    trips the memory/swap guard, each judged on its own (:func:`judge` ORs them together; the S1
    load-hold bypass, :mod:`asf.tick.step_wave`, needs to tell the two apart: it may pass load
    pressure, never memory pressure)."""
    r = reading or {}
    load, load1, cores = r.get('load15'), r.get('load1'), r.get('cores')
    swap = r.get('swap_pct')
    # macOS never gives swap back once used: 90 % swap with 69 % of memory free held every wave
    # for hours (2026-09-25). Where the host reports memory pressure itself, that is the reading
    # the memory guard judges; swap is only the fallback.
    mem = r.get('mem_pct')
    if mem is not None:
        swap = mem
    over_load15 = (guards.get('load_per_core') is not None and load is not None and cores
                   and float(load) >= guards['load_per_core'] * cores)
    # a spike that already ended leaves load15 high for up to 15 minutes after load1 has
    # dropped: only a sustained load — both averages over the limit — holds.
    over_load = over_load15 and (load1 is None or float(load1) >= guards['load_per_core'] * cores)
    over_mem = (guards.get('swap_pct') is not None and swap is not None
                and float(swap) >= guards['swap_pct'])
    return over_load, over_mem


def load_only_hold(reading, guards):
    """Whether this reading holds on the LOAD guard alone — memory/swap is not over. The one
    case the S1 load-hold bypass may pass: it never passes a host over its memory/swap guard."""
    over_load, over_mem = over_parts(reading, guards)
    return over_load and not over_mem


def judge(reading, guards):
    """``(held, why)``; ``why`` reads ``host pressure load 90/cores 12, swap 87%`` — or, with a
    1-minute reading, ``host pressure load 34 (1m 32)/cores 10, swap 87%``."""
    r = reading or {}
    load, load1, cores = r.get('load15'), r.get('load1'), r.get('cores')
    swap = r.get('swap_pct')
    mem = r.get('mem_pct')
    label = 'memory' if mem is not None else 'swap'
    if mem is not None:
        swap = mem
    over_load, over_mem = over_parts(reading, guards)
    if not (over_load or over_mem):
        return False, ''
    parts = []
    if load is not None and cores:
        if load1 is not None:
            parts.append(f'load {float(load):.0f} (1m {float(load1):.0f})/cores {cores}')
        else:
            parts.append(f'load {float(load):.0f}/cores {cores}')
    if swap is not None:
        parts.append(f'{label} {float(swap):.0f}%')
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
            load1, _load5, load15 = os.getloadavg()
        except (OSError, AttributeError):
            load1 = load15 = None
        return {'load15': load15, 'load1': load1, 'cores': os.cpu_count(),
                'swap_pct': self._swap(), 'mem_pct': self._memory()}

    @staticmethod
    def _memory():
        """Memory in use as the kernel judges it, percent — macOS ``kern.memorystatus_level`` is
        the free share the pressure system acts on; ``None`` elsewhere or when unreadable."""
        try:
            p = subprocess.run(['sysctl', '-n', 'kern.memorystatus_level'], capture_output=True,
                               text=True, timeout=5)
            level = int(p.stdout.strip()) if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        return None if level is None or not 0 <= level <= 100 else 100 - level

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

    def __init__(self, load15=None, cores=None, swap_pct=None, load1=None):
        self.reading = {'load15': load15, 'cores': cores, 'swap_pct': swap_pct, 'load1': load1}

    def read(self):
        return dict(self.reading)


def probe():
    """``$ASF_HOST_READING`` (``"<load15> <cores> <swap_pct>"``, or with a 4th field, ``"<load15>
    <cores> <swap_pct> <load1>"``) when set, else the host."""
    fixed = os.environ.get(READING_ENV, '').split()
    if len(fixed) in (3, 4):
        try:
            load1 = float(fixed[3]) if len(fixed) == 4 else None
            return FixedProbe(float(fixed[0]), int(fixed[1]), float(fixed[2]), load1)
        except ValueError:
            pass
    return SystemProbe()


def pressure(cfg, source=None):
    """``(held, why, reading)`` for this host now, under ``cfg``'s ``host_guards``."""
    reading = (source or probe()).read()
    held, why = judge(reading, guards_from_config(cfg))
    return held, why, reading
