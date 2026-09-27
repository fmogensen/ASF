"""asf.workers.cuxlock — a wedged account-manager usage lock: named always, reclaimed on opt-in.

The quota source (``worker_pool.quota_command``) may refresh its readings through the cux account
manager, which serialises every refresh on one global file lock (``~/.cux/.lock``). When a cux
poller hangs while it holds that lock, every refresh times out (``acquire lock: timeout``), every
reading goes stale and the wave throttles launches — four times on one day, for up to 6 h.

**Detect (always on).** :func:`probe_wedge` lists the lock's holders (``lsof``) against the
process table (``ps``). A hold older than :data:`WEDGED_AFTER_MIN` is wedged; ``asf status`` (Quota
row) and ``asf doctor`` then say ``cux lock wedged N min — held by pid X (child of Y)``. N is a
lower bound: the youngest holder's age.

**Reclaim (opt-in, ``quota_guards.reclaim_cux_lock: true``; default off).** A holder is a target
only when EVERY one of these holds (:func:`decide`):

* it is a cux process and NOT a claude process;
* its parent is a cux process — it is a child, never the wrapper;
* it has no children of its own — a live session always has one;
* it is at least :data:`RECLAIM_AFTER_MIN` minutes old;
* it is not pid 0/1, this process or one of its ancestors.

The lock is reclaimed only when at least one holder is a target and every other holder is the
parent of a target (the cux binary that forked the pollers — it is never signalled). Anything else
holding the lock stops the whole reclaim. Before acting the table is read a second time and the
same targets (pid AND start time — a reused pid never matches) must come out; then each target
gets SIGTERM (never SIGKILL), the lock file moves to ``.lock.stale-HHMM``, ``cux usage refresh``
runs once, and the tick logs the pids.
"""
import collections
import datetime
import fcntl
import os
import shlex
import signal
import subprocess

WEDGED_AFTER_MIN = 5       # a refresh holds the lock for seconds; minutes is a wedge
RECLAIM_AFTER_MIN = 15     # a target must be at least this old
REFRESH_TIMEOUT_S = 120

Proc = collections.namedtuple('Proc', 'pid ppid age_s command start')
Wedge = collections.namedtuple('Wedge', 'wedged label minutes')
Decision = collections.namedtuple('Decision', 'act targets refused why')


def lock_path():
    """``$ASF_CUX_LOCK`` when set (a non-default cux home; the test suite's own), else
    ``~/.cux/.lock``."""
    if os.environ.get('ASF_CUX_LOCK'):
        return os.environ['ASF_CUX_LOCK']
    return os.path.join(os.path.expanduser('~'), '.cux', '.lock')


def reclaim_enabled(cfg):
    """Only a real YAML ``true`` turns reclaim on — a string, a number or a typo stays off."""
    g = (cfg or {}).get('quota_guards')
    return isinstance(g, dict) and g.get('reclaim_cux_lock') is True


# ---- reading the process table -------------------------------------------------------------

def parse_etime(text):
    """``[[dd-]hh:]mm:ss`` → seconds, or None."""
    try:
        days = 0
        if '-' in text:
            d, text = text.split('-', 1)
            days = int(d)
        parts = [int(x) for x in text.split(':')]
    except ValueError:
        return None
    if not 2 <= len(parts) <= 3:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


def parse_ps(text):
    """``ps -axo pid=,ppid=,etime=,lstart=,command=`` → ``{pid: Proc}``. ``lstart`` is five
    words (``Mon Sep 28 00:36:22 2026``); a line that does not parse is skipped."""
    table = {}
    for line in text.splitlines():
        f = line.split(None, 8)
        if len(f) < 9:
            continue
        try:
            pid, ppid = int(f[0]), int(f[1])
        except ValueError:
            continue
        age = parse_etime(f[2])
        if age is None:
            continue
        table[pid] = Proc(pid, ppid, age, f[8].strip(), ' '.join(f[3:8]))
    return table


def parse_lsof(text):
    """``lsof -F p`` → the holder pids, in order."""
    out = []
    for line in text.splitlines():
        if line.startswith('p'):
            try:
                pid = int(line[1:])
            except ValueError:
                continue
            if pid not in out:
                out.append(pid)
    return out


def _argv(proc):
    try:
        return shlex.split(proc.command)
    except ValueError:
        return proc.command.split()


def _names(proc):
    """The program's base name, and — for an interpreter (``node x/cux``) — its script's."""
    argv = _argv(proc)
    if not argv:
        return []
    names = [os.path.basename(argv[0]).lstrip('-')]
    if names[0] in ('node', 'bun', 'deno', 'python', 'python3') and len(argv) > 1:
        names.append(os.path.basename(argv[1]))
    return names


def is_cux(proc):
    return proc is not None and 'cux' in _names(proc)


def is_claude(proc):
    """A claude process: its program (or interpreted script, or that script's directory) is
    named claude-something. Flags such as ``--dangerously-skip-permissions`` are not names."""
    if proc is None:
        return False
    argv = _argv(proc)
    names = _names(proc)
    if len(names) > 1 and len(argv) > 1:
        names.extend(p for p in argv[1].split('/') if p)
    return any('claude' in n.lower() for n in names)


def children(table, pid):
    return [p for p in table.values() if p.ppid == pid and p.pid != pid]


def ancestors(table, pid):
    out, seen = set(), set()
    while pid in table and pid not in seen:
        seen.add(pid)
        out.add(pid)
        pid = table[pid].ppid
    out.add(pid)
    return out


def probe(path):
    """``(holder pids, process table)`` for the lock at ``path``; ``([], {})`` when it cannot be
    read (no lsof, a timeout)."""
    try:
        lsof = subprocess.run(['lsof', '-F', 'p', path], capture_output=True, text=True,
                              timeout=15)
        holders = parse_lsof(lsof.stdout)
        if not holders:
            return [], {}
        ps = subprocess.run(['ps', '-axo', 'pid=,ppid=,etime=,lstart=,command='],
                            capture_output=True, text=True, timeout=15,
                            env=dict(os.environ, LC_ALL='C'))
    except (OSError, subprocess.SubprocessError):
        return [], {}
    return holders, parse_ps(ps.stdout)


# ---- the two pure judgements ---------------------------------------------------------------

def diagnose(holders, table):
    """The lock's state as a :class:`Wedge`."""
    procs = [table[p] for p in holders if p in table]
    if not procs:
        return Wedge(False, 'cux lock free', 0)
    minutes = min(p.age_s for p in procs) // 60
    leaves = [p for p in procs if not children(table, p.pid)]
    lead = max(leaves or procs, key=lambda p: p.age_s)
    more = f', +{len(holders) - 1} more' if len(holders) > 1 else ''
    who = f'pid {lead.pid} (child of {lead.ppid}){more}'
    if minutes >= WEDGED_AFTER_MIN:
        return Wedge(True, f'cux lock wedged {minutes} min — held by {who}', minutes)
    return Wedge(False, f'cux lock held {minutes} min by {who}', minutes)


def _refusal(proc, table, protect, min_age_s):
    """Why ``proc`` must not be signalled, or '' when it is a target."""
    if proc is None:
        return 'not in the process table'
    if proc.pid in (0, 1) or proc.pid in protect:
        return 'this process or its ancestor'
    if is_claude(proc):
        return 'a claude process'
    if not is_cux(proc):
        return 'not a cux process'
    if not is_cux(table.get(proc.ppid)):
        return 'not a child of a cux process (the wrapper)'
    if children(table, proc.pid):
        return 'has children'
    if proc.age_s < min_age_s:
        return f'younger than {min_age_s // 60} min'
    return ''


def decide(holders, table, enabled, protect=None, min_age_s=RECLAIM_AFTER_MIN * 60):
    """A :class:`Decision` over a process table: ``act`` with ``targets`` (Procs) only when every
    rule in the module doc holds; ``refused`` maps each other holder to why. Pure."""
    if not enabled:
        return Decision(False, (), {}, 'reclaim off (quota_guards.reclaim_cux_lock)')
    protect = set(protect or ()) | {os.getpid()}
    protect |= ancestors(table, os.getpid())
    targets, refused = [], {}
    for pid in holders:
        why = _refusal(table.get(pid), table, protect, min_age_s)
        if why:
            refused[pid] = why
        else:
            targets.append(table[pid])
    if not targets:
        return Decision(False, (), refused, 'no holder qualifies')
    parents = {t.ppid for t in targets}
    blockers = [p for p in refused if p not in parents]
    if blockers:
        return Decision(False, (), refused,
                        f'holder {blockers[0]} is not a target and not a target\'s parent')
    return Decision(True, tuple(targets), refused, '')


# ---- the probe for status/doctor, and the tick's reclaim -----------------------------------

def probe_wedge(lock=None):
    """The lock's :class:`Wedge`, or None when there is no lock file (no cux on this host)."""
    lock = lock or lock_path()
    if not os.path.exists(lock):
        return None
    holders, table = probe(lock)
    return diagnose(holders, table)


def _default_refresh():
    try:
        return subprocess.run(['cux', 'usage', 'refresh'], capture_output=True, text=True,
                              timeout=REFRESH_TIMEOUT_S).returncode
    except (OSError, subprocess.SubprocessError) as e:
        return f'{type(e).__name__}'


def _stale_name(lock, now):
    base = f"{lock}.stale-{now.strftime('%H%M')}"
    name, n = base, 1
    while os.path.exists(name):
        name, n = f'{base}-{n}', n + 1
    return name


def reclaim(cfg, lock=None, probe=probe, kill=os.kill, refresh=_default_refresh, protect=None,
            guard_dir=None, now=None):
    """Reclaim a wedged lock when ``cfg`` opts in and :func:`decide` agrees twice. Returns a
    record: ``acted``, ``why``, and — when it acted — ``terminated`` ({pid, ppid, age_min}),
    ``moved_to`` and ``refresh``."""
    lock = lock or lock_path()
    rec = {'acted': False, 'why': ''}
    enabled = reclaim_enabled(cfg)
    if not enabled:
        rec['why'] = 'reclaim off (quota_guards.reclaim_cux_lock)'
        return rec
    if not os.path.exists(lock):
        rec['why'] = 'no lock file'
        return rec
    holders, table = probe(lock)
    wedge = diagnose(holders, table)
    rec['label'] = wedge.label
    if not wedge.wedged:
        rec['why'] = 'not wedged'
        return rec
    first = decide(holders, table, enabled, protect)
    rec['refused'] = {str(k): v for k, v in first.refused.items()}
    if not first.act:
        rec['why'] = first.why
        return rec
    if guard_dir is None:
        from asf import env
        guard_dir = os.path.join(env.ASF_HOME, 'state')
    os.makedirs(guard_dir, exist_ok=True)
    with open(os.path.join(guard_dir, 'cux-lock-reclaim.lock'), 'a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            rec['why'] = 'another tick is reclaiming'
            return rec
        inode = os.stat(lock).st_ino
        holders2, table2 = probe(lock)
        second = decide(holders2, table2, enabled, protect)
        same = {(t.pid, t.start) for t in first.targets} == {(t.pid, t.start) for t in second.targets}
        if not second.act or not same:
            rec['why'] = 'the second look differs — nothing touched'
            return rec
        rec['terminated'] = []
        for t in second.targets:
            try:
                kill(t.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            rec['terminated'].append({'pid': t.pid, 'ppid': t.ppid, 'age_min': t.age_s // 60})
        now = now or datetime.datetime.now()
        try:
            if os.stat(lock).st_ino == inode:
                dest = _stale_name(lock, now)
                os.rename(lock, dest)
                rec['moved_to'] = dest
        except OSError:
            pass
        rec['refresh'] = refresh()
        rec['acted'] = True
        return rec


def tick_pass(cfg, out=print, event=None):
    """The wave's one call, before it reads quota: reclaim when opted in, and say what it did."""
    if not reclaim_enabled(cfg):
        return None
    rec = reclaim(cfg)
    if rec.get('acted'):
        pids = ', '.join(f"{t['pid']} (child of {t['ppid']}, {t['age_min']} min)"
                         for t in rec.get('terminated', []))
        out(f"quota    {rec.get('label', 'cux lock wedged')} — reclaimed: SIGTERM {pids or 'none'}; "
            f"lock moved to {os.path.basename(rec.get('moved_to') or '?')}; "
            f"refresh {rec.get('refresh')}")
        if event is not None:
            event('cux_lock_reclaim', terminated=rec.get('terminated', []),
                  moved_to=rec.get('moved_to'), refresh=str(rec.get('refresh')),
                  label=rec.get('label'))
    elif rec.get('label', '').startswith('cux lock wedged'):
        out(f"quota    {rec['label']} — not reclaimed: {rec.get('why')}")
    return rec
