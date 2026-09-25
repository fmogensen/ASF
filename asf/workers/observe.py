"""asf.workers.observe — every agent session running on this machine, whose it is, and which
account it is on (F-0076).

The READ is a :class:`SessionSource`: ``.read() -> [{"pid", "ppid", "env": {…}}]``, raising
:class:`ObserveError` when the table cannot be read. Three sources: ``process`` (default — one
``ps axeww -o pid=,ppid=,command=`` call, filtered to the runtime binary and de-duplicated to one
entry per session — F-0076 D6), ``command`` (``worker_pool.sessions_command``, one JSON object
per line, for a machine whose ``ps`` prints a different form) and ``fake`` (an empty table, for
tests that want the real process table kept out of the suite; a populated one is reached only
through :func:`read`'s own ``source=`` override).

:func:`read` turns each raw entry into an :class:`Observed`: the account (F-0076 D7 —
``CLAUDE_CONFIG_DIR``, then ``HOME`` against the account's own ``home``, then ``HOME`` against
the tick's own, all realpath'd), whose it is (``asf`` iff ``ASF_SESSION`` is set — D8), and the
product/job the id names. It never raises: an unreadable source comes back as ``([], why)``, and
the wave prints that ``why`` and falls back to the registry alone (D10).

:func:`identity_alive` is the D11 rule nothing here calls yet: a run is alive only while the
process at its pid still carries its session's id.
"""
import json
import os
import re
import shlex
import subprocess
from collections import namedtuple

from asf.workers import cloudpid
from asf.workers import lifecycle
from asf.workers import runtime as runtime_mod

Observed = namedtuple('Observed', 'pid ppid account session product job owner cwd')

#: The env keys a source may carry (D7's account rule, D8's owner rule, and the id itself).
ENV_KEYS = ('CLAUDE_CONFIG_DIR', 'HOME', 'ASF_SESSION', 'ASF_PRODUCT', 'ASF_JOB')


class ObserveError(Exception):
    pass


class SessionSource:
    def read(self):
        raise NotImplementedError


class ProcessSource(SessionSource):
    """``ps axeww -o pid=,ppid=,command=``, one call. A line is a session when its command
    matches ``match`` (default: ``binary`` as ``argv[0]``, D6). A candidate whose ppid chain,
    walked through the *full* table, reaches another candidate is a session's own child (a shell,
    a nested agent) and is dropped, not counted twice."""

    def __init__(self, binary=None, match=None):
        self.binary = binary or runtime_mod.DEFAULT_BINARY
        self.match = match or rf'^(\S*/)?{re.escape(self.binary)}(\s|$)'

    def _table(self):
        try:
            p = subprocess.run(['ps', 'axeww', '-o', 'pid=,ppid=,command='],
                               capture_output=True, text=True)
        except FileNotFoundError as e:
            raise ObserveError(str(e)) from e
        if p.returncode != 0:
            why = (p.stderr or p.stdout or '').strip()
            raise ObserveError(why or 'ps exited non-zero')
        out = []
        for line in p.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
                continue
            out.append((int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else ''))
        return out

    def read(self):
        table = self._table()
        by_ppid = {pid: ppid for pid, ppid, _ in table}
        candidates = {pid: rest for pid, ppid, rest in table if re.search(self.match, rest)}

        def _has_candidate_ancestor(pid):
            seen = {pid}
            cur = by_ppid.get(pid)
            while cur is not None and cur not in seen:
                if cur in candidates:
                    return True
                seen.add(cur)
                cur = by_ppid.get(cur)
            return False

        return [{'pid': pid, 'ppid': by_ppid[pid], 'env': _parse_env(rest)}
                for pid, rest in candidates.items() if not _has_candidate_ancestor(pid)]


class CommandSource(SessionSource):
    """``worker_pool.sessions_command``, ``shlex.split`` and run: one JSON object per non-empty
    line, ``{"pid", "ppid", "env": {…}}``. No command configured, a non-zero exit or output that
    does not parse are all :class:`ObserveError` — raised at read time (D10), not at config
    load, so a launch that never asks for sessions is never refused for it."""

    def __init__(self, command):
        self.command = command

    def read(self):
        if not self.command:
            raise ObserveError('worker_pool.sessions_command is not set')
        try:
            argv = shlex.split(self.command)
        except ValueError as e:
            raise ObserveError(str(e)) from e
        try:
            p = subprocess.run(argv, capture_output=True, text=True)
        except OSError as e:
            raise ObserveError(str(e)) from e
        if p.returncode != 0:
            why = (p.stderr or p.stdout or '').strip()
            raise ObserveError(why or f'{self.command}: exited non-zero')
        out = []
        for line in p.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ObserveError(f'unparsable session line: {line!r}') from e
            if not isinstance(rec, dict) or 'pid' not in rec:
                raise ObserveError(f'unparsable session line: {line!r}')
            out.append({'pid': rec['pid'], 'ppid': rec.get('ppid'), 'env': rec.get('env') or {}})
        return out


class FakeSource(SessionSource):
    """A fixed table, for tests: ``entries`` in the command source's shape
    (``{"pid", "ppid", "env": {…}}``), returned as they stand (D3) — no match filter, no
    ancestor drop, because a test states exactly the sessions it means."""

    def __init__(self, entries=None):
        self.entries = list(entries or [])

    def read(self):
        return [{'pid': e['pid'], 'ppid': e.get('ppid'), 'env': dict(e.get('env') or {})}
                for e in self.entries]


def _parse_env(text, keys=ENV_KEYS):
    """The ``KEY=VALUE`` tokens of ``text`` for ``keys``; the last occurrence of a key wins,
    because the environment follows the argv in ``ps``'s output."""
    pattern = re.compile(r'(?:^|\s)(' + '|'.join(re.escape(k) for k in keys) + r')=(\S*)')
    found = {}
    for m in pattern.finditer(text):
        found[m.group(1)] = m.group(2)
    return found


def source_from_config(cfg):
    pool = (cfg or {}).get('worker_pool') or {}
    kind = pool.get('sessions') or 'process'
    if kind == 'command':
        return CommandSource(pool.get('sessions_command'))
    if kind == 'fake':
        return FakeSource([])
    binary = pool.get('binary') or runtime_mod.DEFAULT_BINARY
    return ProcessSource(binary, pool.get('session_match'))


def _realpath(path):
    return os.path.realpath(os.path.expanduser(path)) if path else None


def _account_for(env_vars, accounts, tick_home):
    """D7, in account order: an account with a ``config_dir`` matches on
    ``CLAUDE_CONFIG_DIR``; one with no ``config_dir`` matches on ``HOME`` against the home its
    sessions run under (:func:`asf.workers.runtime.session_home`: its ``home:``, or its isolated
    one); one on the operator's HOME (``isolate_home: false``, no ``home:``) matches on ``HOME``
    against the tick's own, and only when the session
    sets no ``CLAUDE_CONFIG_DIR`` at all. Every comparison is realpath'd on both sides (D11).
    ``None`` when nothing matches."""
    config_dir_rp = _realpath(env_vars.get('CLAUDE_CONFIG_DIR'))
    home_rp = _realpath(env_vars.get('HOME'))
    for acct in accounts:
        acct_config_dir = getattr(acct, 'config_dir', None)
        # the HOME its sessions run under: its ``home:``, its isolated one, or the operator's
        acct_home = runtime_mod.session_home(acct) if hasattr(acct, 'name') else None
        if acct_config_dir:
            if config_dir_rp is not None and config_dir_rp == _realpath(acct_config_dir):
                return acct.name
        elif acct_home:
            if home_rp is not None and home_rp == _realpath(acct_home):
                return acct.name
        else:
            if (not env_vars.get('CLAUDE_CONFIG_DIR') and tick_home is not None
                    and home_rp == tick_home):
                return acct.name
    return None


def read(cfg, accounts, source=None):
    """``([Observed], why)`` — every session ``source`` (default: :func:`source_from_config`)
    sees, with its account and owner assigned (D7, D8) and its product/job parsed from
    ``ASF_SESSION`` when it has one. ``why`` is ``''`` on success, or the reason the source could
    not be read; on failure this returns ``([], why)`` and never raises (D10)."""
    src = source if source is not None else source_from_config(cfg)
    try:
        entries = src.read()
    except ObserveError as e:
        return [], str(e)
    tick_home = _realpath(os.environ.get('HOME'))
    out = []
    for e in entries:
        env_vars = e.get('env') or {}
        sid = env_vars.get('ASF_SESSION') or None
        parsed = lifecycle.parse_session(sid) if sid else None
        product, job = (parsed[0], parsed[1]) if parsed else (None, None)
        out.append(Observed(
            pid=e['pid'], ppid=e.get('ppid'), account=_account_for(env_vars, accounts, tick_home),
            session=sid, product=product, job=job, owner='asf' if sid else 'foreign', cwd=None))
    return out, ''


def identity_alive(observed, runs):
    """``callable(pid) -> bool`` (D11): a pid one of ``runs`` recorded is alive only while an
    observed session still sits there carrying that run's ``session`` — or, for a run recorded
    before this change and so with no ``session`` of its own, while any observed session sits at
    its pid. A pid no run recorded is alive iff it is observed at all."""
    by_pid = {o.pid: o for o in observed}
    run_by_pid = {}
    for run in runs:
        pid = run.get('pid')
        if pid is not None and pid not in run_by_pid:
            run_by_pid[pid] = run

    def alive(pid):
        if cloudpid.is_token(pid):  # a cloud run is no process here: its status file answers
            return cloudpid.alive(pid)
        run = run_by_pid.get(pid)
        o = by_pid.get(pid)
        if run is not None:
            return o is not None and (not run.get('session') or run.get('session') == o.session)
        return pid in by_pid

    return alive
