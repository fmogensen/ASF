"""asf.workers.stall — which live sessions need a look, and the cold-retry correction hook.

``stall(product)`` over the live sessions in the ledger:

* no result line, pid alive, log silent longer than ``stage_limits.silent_min`` (default 30
  minutes; an int is minutes, a duration string like ``45m``/``1h`` works too) → ``STALL``;
* no result line and the pid is dead → ``DEAD``;
* no result line, pid alive, writing but standing still by :mod:`asf.progress`'s judge over
  ``stage_limits.progress_min`` (default 20 minutes; ``0`` turns the row off) → ``STUCK``;
* a run's own result text declares a question that has not been raised yet
  (:func:`asf.workers.report.needs_input`), newest run per job, live or ended → ``INPUT``.

``~/.ASF/state/<product>/stall-ack.txt`` suppresses a known one: a line ``<job>`` acks it in
any state, ``<job> STALL`` / ``<job> DEAD`` / ``<job> STUCK`` / ``<job> INPUT`` only in that
state.

``correct_once(product, session, error_text, runtime)`` — a step that fails gets one more try,
cold, before anyone files a Bug: the runtime is relaunched in the same worktree with the original
brief + ``CORRECTION: the step failed with:`` + the error, but under its own job name and log —
never the dead session's — so the retry's own ledger line carries its outcome and the dead run's
line is never rewritten to look like the one that passed (D-0048, part b). ``corrected: 1`` is
recorded on the original session so a second failure (or a session already corrected once)
returns False and the caller files the Bug instead.

``capped(product)`` is the other half of "a live session the factory must end": a run over any
one token dimension's cap (:mod:`asf.tokens`) or either of a run's own caps — wall clock, turns
(:mod:`asf.budget`) — is signalled dead (``stop_session``: SIGTERM to its process group, SIGKILL
after a grace), gets one ``result`` line of the factory's own appended to its log — ``is_error``,
with a structured ``asf.cap`` or ``asf.run_cap`` object every reader already understands as
``failed: token cap`` or ``failed: run cap`` — and is marked ``capped`` on its registry line. A
run that already has a result is never touched, and a pid that no longer carries the run's
session id is never signalled (F-0076 D11): it is left to health's ``dead pid`` path.
"""
import calendar
import json
import os
import signal
import time

from asf import budget
from asf import env
from asf import progress
from asf import tokens
from asf.metrics import metrics as metrics_mod
from asf.workers import cloudpid
from asf.workers import githooks
from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import report as report_mod
from asf.workers import runtime as runtime_mod

DEFAULT_SILENT_MIN = 30
CORRECTION_HEAD = '\n\nCORRECTION: the step failed with:\n'
STOP_POLL_S = 0.5


def silent_minutes(product):
    return progress.minutes_of((product.stage_limits or {}).get('silent_min', DEFAULT_SILENT_MIN),
                               DEFAULT_SILENT_MIN)


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


def classify(session, now, silent_min, alive, verdict=None):
    """``STALL`` | ``DEAD`` | ``STUCK`` | None for one live session."""
    log = session.get('log')
    if runtime_mod.read_result(log) is not None:
        return None
    if not alive(session.get('pid')):
        return 'DEAD'
    if cloudpid.is_token(session.get('pid')):
        return None  # a cloud session's log is quiet by design: its time limit is the lane's
    try:
        mtime = os.path.getmtime(log)
    except (OSError, TypeError):
        return None
    if (now - mtime) / 60 > silent_min:
        return 'STALL'
    return 'STUCK' if verdict is not None and verdict.cls == progress.STUCK else None


def _input_rows(product, acks):
    """``[(job, question)]`` — every job's newest run, live or ended, whose result text declares
    a question not yet raised (D10: by the time a session is waiting on a human it has written
    its result and exited, so this is a different walk over the ledger, not a live-session rung)."""
    rows = []
    for job, s in pool_mod.load_sessions(product).items():
        if s.get('input_flagged') or (job, None) in acks or (job, 'INPUT') in acks:
            continue
        rec = runtime_mod.read_result(s.get('log'))
        if rec is None:
            continue
        question = report_mod.needs_input(str(rec.get('result') or ''))
        if question is not None:
            rows.append((job, question))
    return rows


def stall(product, now=None, alive=None, session_source=None, out=print, sample=True):
    """Returns ``[(job, STALL|DEAD|STUCK|INPUT, minutes)]``, acked ones left out.

    Before it judges, one sample per live local run is taken in-process (``sample=False`` for a
    caller that only wants to read): a sampler that died must cost resolution, never sight
    (F-0066 D7). The judged class is written onto the run's registry line when it changed, so the
    run's own outcome carries it (D13). ``INPUT`` is the one row over runs that are not live: a
    run whose result text declares a question (:func:`asf.workers.report.needs_input`) and that
    has not been raised yet, newest run per job.
    """
    now = time.time() if now is None else now
    limit = silent_minutes(product)
    plimit = progress.progress_min(product)
    acks = load_acks(product)
    live = pool_mod.live_sessions(product)
    if alive is None:
        alive = health_mod.alive_for(product, live, session_source)
    found = []
    for s in live:
        verdict = None
        if plimit > 0 and not cloudpid.is_token(s.get('pid')):
            if sample:
                progress.sample(product, s, now=now)
            verdict = progress.judge(progress.read(product, s['job']), now, plimit)
            if verdict.cls != s.get('progress'):
                pool_mod.update_session(product, s['job'], progress=verdict.cls)
        state = classify(s, now, limit, alive, verdict=verdict)
        if state is None or (s['job'], None) in acks or (s['job'], state) in acks:
            continue
        if state == 'STUCK':
            minutes = int(verdict.minutes)
            out(f'{state:<5} {s["job"]:<24} no progress {minutes}m — {verdict.evidence}')
        else:
            try:
                minutes = int((now - os.path.getmtime(s.get('log'))) / 60)
            except (OSError, TypeError):
                minutes = None
            out(f'{state:<5} {s["job"]:<24} silent {minutes if minutes is not None else "?"}m')
        found.append((s['job'], state, minutes))
    for job, question in _input_rows(product, acks):
        out(f'{"INPUT":<5} {job:<24} needs input — {question}')
        found.append((job, 'INPUT', None))
    if not found:
        out('stall: none')
    return found


def _signal(pid, sig):
    os.killpg(os.getpgid(int(pid)), sig)


def stop_session(session, alive, grace_s=None):
    """End a live run: SIGTERM to its process group, then SIGKILL if ``alive`` still says the pid
    is there after ``grace_s``. A run that is gone or not ours is not an error — the caller still
    writes its cap line."""
    grace_s = tokens.tunable('STOP_GRACE_S') if grace_s is None else grace_s
    pid = session.get('pid')
    if cloudpid.is_token(pid):
        from asf.workers import cloud
        cloud.stop(session)
        return
    try:
        _signal(pid, signal.SIGTERM)
        for _ in range(int(grace_s / STOP_POLL_S)):
            if not alive(pid):
                return
            time.sleep(STOP_POLL_S)
        if alive(pid):
            _signal(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _iso(now):
    if now is None:
        return pool_mod.now_iso()
    if isinstance(now, (int, float)):
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))
    return now


def _append_line(path, rec):
    """One line onto the log; a killed session may have left a partial line, which the new line
    must not run into."""
    with open(path, 'a+b') as f:
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b'\n':
                f.write(b'\n')
        f.write((json.dumps(rec) + '\n').encode('utf-8'))


def elapsed_min(session, now=None):
    """The run's ``started`` to ``now``, in minutes; ``None`` when the ledger has no usable
    ``started``."""
    started = session.get('started')
    if not started:
        return None
    try:
        began = calendar.timegm(time.strptime(str(started), '%Y-%m-%dT%H:%M:%SZ'))
    except ValueError:
        return None
    if now is None:
        at = time.time()
    elif isinstance(now, (int, float)):
        at = now
    else:
        try:
            at = calendar.timegm(time.strptime(str(now), '%Y-%m-%dT%H:%M:%SZ'))
        except ValueError:
            return None
    return int((at - began) / 60)


def capped(product, now=None, alive=None, caps=None, stop=True, out=print):
    """Returns ``[(job, measure, value, limit)]`` for every live run over one token dimension's
    cap or one of its own run caps (wall clock, turns), in ``live_sessions`` order; each is
    stopped, given a cap result line and marked ``capped`` on its registry line. ``stop=False``
    judges only: nothing is signalled, appended or marked.

    A malformed ``token_caps:`` block stops nothing (D13); a run that has a result is never
    touched; a pid that is not this run's is never signalled and is left to health."""
    at = _iso(now)
    if caps is None:
        try:
            caps = tokens.caps(product)
        except tokens.TokenCapError as e:
            out(f'token cap: {e}')
            return []
    conv = getattr(product, 'conventions', None)
    live = pool_mod.live_sessions(product)
    if alive is None:
        alive = health_mod.alive_for(product, live)
    found = []
    for s in live:
        if not s.get('log'):
            continue
        m = tokens.meter(s['log'])
        if m.result is not None:
            continue
        kind = metrics_mod.session_kind(s)
        verdict = tokens.over(m.by_dim, caps.get(kind) or caps.get('default') or {})
        run_verdict = None if verdict else budget.run_over(elapsed_min(s, now), m.turns, budget.run_caps(conv))
        if (verdict is None and run_verdict is None) or not alive(s.get('pid')):
            continue
        measure, value, limit = verdict or run_verdict
        detail = ''
        if stop:
            stop_session(s, alive)
            if runtime_mod.read_result(s['log']) is None:
                rec = (tokens.cap_result(kind, measure, value, limit, m.by_dim, at) if verdict
                       else budget.run_cap_result(kind, measure, value, limit, at))
                _append_line(s['log'], rec)
            else:
                detail = ' finished first'
            mark = ({'dimension': measure, 'tokens': value, 'limit': limit, 'at': at} if verdict
                    else {'measure': measure, 'value': value, 'limit': limit, 'at': at})
            pool_mod.update_session(product, s['job'], capped=mark)
        found.append((s['job'], measure, value, limit))
        out(f"CAP   {s['job']}  {measure} {value} over {limit} ({kind}){detail}")
    if not found:
        out('cap: none')
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
    retry_env = {**githooks.item_env(getattr(product, 'conventions', None), session.get('item'),
                                     session.get('branch')), 'ASF_SESSION': sid}
    if session.get('id_range'):
        retry_env['BACKLOG_ID_RANGE'] = session['id_range']
    job = runtime_mod.Job(product.name, retry_job, session.get('worktree'), path,
                          session.get('model'), account=_account(session),
                          env=retry_env, hooks_dir=hooks_dir,
                          passthrough=env.env_passthrough(_cfg()),
                          # the product's own credentials, as the first launch had them: without
                          # them the correction's git has no helper and cannot push (2026-10-06)
                          product_auth_env=env.product_auth_env(product),
                          branch=session.get('branch'), base=product.main)
    # launched, not waited on (B-0085): this runs inside the tick's health step, and waiting
    # here stopped health, harvest and the operator's tables for as long as a model session takes
    # — one tick sat inside four serial adjudications for an hour. The run is in the registry
    # with its pid; the next tick judges it exactly as it judges every other run.
    try:
        result = runtime.run(job)
    except runtime_mod.AuthEnvError as e:  # the account's credential file is gone: no retry
        print(f"correction of {session['job']} not launched: {e}")
        return False
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
