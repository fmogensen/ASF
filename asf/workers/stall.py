"""asf.workers.stall — which live sessions need a look, and the cold-retry correction hook.

``stall(product)`` over the live sessions in the ledger:

* no result line, pid alive, log silent longer than ``stage_limits.silent_min`` (default 30
  minutes; an int is minutes, a duration string like ``45m``/``1h`` works too) → ``STALL``;
* no result line and the pid is dead → ``DEAD``.

``~/.ASF/state/<product>/stall-ack.txt`` suppresses a known one: a line ``<job>`` acks it in
any state, ``<job> STALL`` / ``<job> DEAD`` only in that state.

``correct_once(product, session, error_text, runtime)`` — a step that fails gets one more try,
cold, before anyone files a Bug: the runtime is relaunched in the same worktree with the original
brief + ``CORRECTION: the step failed with:`` + the error, but under its own job name and log —
never the dead session's — so the retry's own ledger line carries its outcome and the dead run's
line is never rewritten to look like the one that passed (D-0048, part b). ``corrected: 1`` is
recorded on the original session so a second failure (or a session already corrected once)
returns False and the caller files the Bug instead.
"""
import os
import re
import time

from asf import env
from asf.workers import githooks
from asf.workers import health as health_mod
from asf.workers import lifecycle
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


def stall(product, now=None, alive=None, session_source=None, out=print):
    """Returns ``[(job, STALL|DEAD, minutes silent)]``, acked ones left out."""
    now = time.time() if now is None else now
    limit = silent_minutes(product)
    acks = load_acks(product)
    live = pool_mod.live_sessions(product)
    if alive is None:
        alive = health_mod.alive_for(product, live, session_source)
    found = []
    for s in live:
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
    """One retry, cold: its own job and log, its own ledger line — never the dead session's.
    True = a correction is now running (B-0085); its verdict is the next tick's, not this one's."""
    if session.get('corrected'):
        return False
    with open(session['brief'], encoding='utf-8') as f:
        original = f.read()
    path = session['brief'][:-3] + '.correction.md' if session['brief'].endswith('.md') \
        else session['brief'] + '.correction'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(original + CORRECTION_HEAD + error_text.rstrip() + '\n')
    retry_job = f"{session['job']}-correction"
    started = pool_mod.now_iso()
    sid = lifecycle.session_id(product.name, retry_job, started)
    hooks_dir = githooks.ensure(product)
    retry_env = {'ASF_SESSION': sid}
    if session.get('id_range'):
        retry_env['BACKLOG_ID_RANGE'] = session['id_range']
    job = runtime_mod.Job(product.name, retry_job, session.get('worktree'), path,
                          session.get('model'), account=_account(session),
                          env=retry_env, hooks_dir=hooks_dir)
    # launched, not waited on (B-0085): this runs inside the tick's health step, and waiting
    # here stopped health, harvest and the operator's tables for as long as a model session takes
    # — one tick sat inside four serial adjudications for an hour. The run is in the registry
    # with its pid; the next tick judges it exactly as it judges every other run.
    result = runtime.run(job)
    pool_mod.update_session(product, session['job'], corrected=1)
    session['corrected'] = 1
    retry = {
        'job': retry_job, 'item': session.get('item'), 'feature': session.get('feature'),
        'kind': session.get('kind'), 'account': session.get('account'),
        'model': session.get('model'), 'pid': result.pid, 'worktree': session.get('worktree'),
        'branch': session.get('branch'), 'started': started,
        'log': result.log_path, 'brief': path, 'id_range': session.get('id_range'),
        'runtime': runtime.name, 'session': sid, 'product': product.name,
    }
    pool_mod.append_session(product, retry)
    # True means "a correction is now running", not "it passed": the run has a pid and a registry
    # line, and health judges it on the next tick the way it judges any other run — its result AND
    # its push (B-0051). Nothing here waits for it.
    return True


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
