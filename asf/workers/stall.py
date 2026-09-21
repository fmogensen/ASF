"""asf.workers.stall — which live sessions need a look, and the same-session correction hook.

``stall(product)`` over the live sessions in the ledger:

* no result line, pid alive, log silent longer than ``stage_limits.silent_min`` (default 30
  minutes; an int is minutes, a duration string like ``45m``/``1h`` works too) → ``STALL``;
* no result line and the pid is dead → ``DEAD``.

``~/.ASF/state/<product>/stall-ack.txt`` suppresses a known one: a line ``<job>`` acks it in
any state, ``<job> STALL`` / ``<job> DEAD`` only in that state.

``correct_once(product, session, error_text, runtime)`` — a step that fails gets one more try in
the same session context before anyone files a Bug: the runtime is re-invoked with the original
brief + ``CORRECTION: the step failed with:`` + the error, and ``corrected: 1`` is recorded on
the session. It returns True when that retry succeeds; a second failure (or a session already
corrected once) returns False so the caller files the Bug.
"""
import os
import re
import time

from asf import env
from asf.workers import health as health_mod
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

DEFAULT_SILENT_MIN = 30
CORRECTION_HEAD = '\n\nCORRECTION: the step failed with:\n'


def silent_minutes(product):
    v = (product.stage_limits or {}).get('silent_min', DEFAULT_SILENT_MIN)
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r'^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$', str(v))
    if not m:
        return float(DEFAULT_SILENT_MIN)
    n = float(m.group(1))
    return n * {'s': 1 / 60, 'm': 1, '': 1, 'h': 60, 'd': 1440}[m.group(2)]


def ack_path(product):
    return os.path.join(env.state_dir(product), 'stall-ack.txt')


def load_acks(product):
    acks = set()
    path = ack_path(product)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                parts = line.split('#', 1)[0].split()
                if parts:
                    acks.add((parts[0], parts[1].upper() if len(parts) > 1 else None))
    return acks


def classify(session, now, silent_min, alive):
    """``STALL`` | ``DEAD`` | None for one live session."""
    log = session.get('log')
    if runtime_mod.read_result(log) is not None:
        return None
    if not alive(session.get('pid')):
        return 'DEAD'
    try:
        mtime = os.path.getmtime(log)
    except (OSError, TypeError):
        return None
    return 'STALL' if (now - mtime) / 60 > silent_min else None


def stall(product, now=None, alive=health_mod.pid_alive, out=print):
    """Returns ``[(job, STALL|DEAD, minutes silent)]``, acked ones left out."""
    now = time.time() if now is None else now
    limit = silent_minutes(product)
    acks = load_acks(product)
    found = []
    for s in pool_mod.live_sessions(product):
        state = classify(s, now, limit, alive)
        if state is None or (s['job'], None) in acks or (s['job'], state) in acks:
            continue
        try:
            quiet = int((now - os.path.getmtime(s.get('log'))) / 60)
        except (OSError, TypeError):
            quiet = None
        found.append((s['job'], state, quiet))
    for job, state, quiet in found:
        out(f'{state:<5} {job:<24} silent {quiet if quiet is not None else "?"}m')
    if not found:
        out('stall: none')
    return found


def correct_once(product, session, error_text, runtime):
    """One same-session retry with the error appended to the brief. True = it now passes."""
    if session.get('corrected'):
        return False
    with open(session['brief'], encoding='utf-8') as f:
        original = f.read()
    path = session['brief'][:-3] + '.correction.md' if session['brief'].endswith('.md') \
        else session['brief'] + '.correction'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(original + CORRECTION_HEAD + error_text.rstrip() + '\n')
    job = runtime_mod.Job(product.name, session['job'], session.get('worktree'), path,
                          session.get('model'), account=_account(session),
                          env={'BACKLOG_ID_RANGE': session['id_range']}
                          if session.get('id_range') else None,
                          log_path=session.get('log'))
    result = runtime.run(job, wait=True)
    pool_mod.update_session(product, session['job'], corrected=1)
    session['corrected'] = 1
    return bool(result.ok)


def _account(session):
    name = session.get('account')
    if not name:
        return None
    for a in pool_mod.accounts_from_config(_cfg()):
        if a.name == name:
            return a
    return pool_mod.Account(name)


def _cfg():
    try:
        return env.load_config()
    except env.ConfigError:
        return {}
