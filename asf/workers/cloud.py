"""asf.workers.cloud — the cloud lane: a worker session that runs as a Claude Code cloud session.

A local session runs, builds and tests on this host; under host pressure the waves held for hours.
The cloud lane moves the whole session off the host. Its runtime, ``claude-cloud``, launches the
runtime CLI's own cloud session::

    claude -p (--cloud | --environment <ccpool_…>) --on-branch <branch> --model <id> -n <job>
           --forward-home-settings false --output-format json          (the brief on stdin)

``--environment`` (``cloud.environment``) puts the session on a self-hosted environment — a pool on
the product's own runners; without it the session runs on the default cloud environment. The
session checks the repository out on the job's branch, works, tests *there*, commits and pushes
that branch exactly as a local session does, so harvest is unchanged. The launch runs under the
worker account's own home and login (:func:`asf.workers.runtime.build_env`): the session and its
quota belong to that account.

The brief (:func:`cloud_brief`) is the local brief plus a CLOUD block: the branch, the product's
setup command, the worker environment (``BACKLOG_ID_RANGE``, ``ASF_SESSION``, the ``worker_env``
caps) to export, the ``ASF-Session`` trailer each commit carries — no host hook stamps it there —
and the end marker: the session's last commit carries its REPORT as the body and an
``ASF-Report: <job>`` trailer, and is pushed.

**Tracking.** The run records ``pid: cloud:<ASF session>`` (:mod:`asf.workers.cloudpid`), the
launcher's own pid as ``launcher_pid``, and — as soon as the CLI prints it — the cloud session's
id and URL (``cloud_session``, ``cloud_url``). :func:`sync` (run first by every health pass)
reads the launch output, the branch on origin and the clock, and maps them onto the session
states (:func:`classify`): **working**, **finished** (a result line from an attached launcher, or
the report commit on the branch — whose body becomes the log's result line) or **dead** (the
launch failed, the launcher left no session, or the run passed ``cloud.timeout_min``; a timed-out
session is told to stop, :func:`stop`). A finished or dead run's worktree is brought up to
``origin/<branch>``, and the state lands in the status file every liveness check reads — from
then on health judges it like any run: pushed or not, empty or not.

**Placement** (:func:`asf.workers.wave.wave`): a row goes to the cloud lane when the local lane
cannot take it — no local seat, or host pressure — ``cloud.enabled`` is true, the row is eligible
(``cloud.rows: any``, or a ``cloud-ok`` row) and the lane is under ``cloud.max_inflight``
(else ``worker_pool.caps.cloud_max_inflight``). Its accounts are ``cloud.accounts``, else the
pool accounts with ``role: cloud``; their quota bands and 5h headroom apply as for any launch.

Config (``~/.ASF/config.yaml``; a product file's ``cloud:`` overrides key by key)::

    cloud:
      enabled: true
      runtime: claude-cloud
      environment: ccpool_…      # optional: a self-hosted environment
      max_inflight: 4
      rows: any                  # or cloud-ok (default)
      accounts: [acct-a]         # optional: default = the role: cloud accounts
      timeout_min: 240
"""
import dataclasses
import datetime
import json
import os
import re
import signal
import subprocess
import time

from asf import detach
from asf.workers import cloudpid
from asf.workers import runtime as runtime_mod

RUNTIME = 'claude-cloud'
RUNTIMES = (RUNTIME,)
ROWS_ANY = 'any'
ROWS_CLOUD_OK = 'cloud-ok'
DEFAULT_TIMEOUT_MIN = 240
DEFAULT_LAUNCH_WAIT_S = 30
REPORT_TRAILER = 'ASF-Report'
SESSION_TRAILER = 'ASF-Session'
ENVIRONMENT_RE = re.compile(r'^ccpool_\S+$')
STOP_TEXT = ('ASF: this session has passed its time limit and is stopped by the factory. Do not '
             'commit or push anything further; end now.')

WORKING, FINISHED, DEAD = cloudpid.WORKING, cloudpid.FINISHED, cloudpid.DEAD


# ---- config -----------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Settings:
    enabled: bool = False
    runtime: str = RUNTIME
    environment: str = ''
    max_inflight: int = 0
    rows: str = ROWS_CLOUD_OK
    accounts: tuple = ()
    timeout_min: float = DEFAULT_TIMEOUT_MIN
    launch_wait_s: float = DEFAULT_LAUNCH_WAIT_S
    binary: str = runtime_mod.DEFAULT_BINARY

    @property
    def on(self):
        """The lane can take a launch: enabled, a runtime it knows, and a seat to give."""
        return self.enabled and self.runtime in RUNTIMES and self.max_inflight > 0


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def raw(cfg, product=None):
    """The ``cloud:`` block: the operator's, with the product file's keys over it."""
    out = dict((cfg or {}).get('cloud') or {}) if isinstance((cfg or {}).get('cloud'), dict) else {}
    own = product._get('cloud') if product is not None and hasattr(product, '_get') else None
    if isinstance(own, dict):
        out.update(own)
    return out


def settings(cfg, product=None):
    """:class:`Settings` for ``product`` under ``cfg``."""
    c = raw(cfg, product)
    wp = (cfg or {}).get('worker_pool') or {}
    caps = wp.get('caps') if isinstance(wp.get('caps'), dict) else {}
    max_inflight = c.get('max_inflight')
    if max_inflight is None:
        max_inflight = caps.get('cloud_max_inflight')
    accounts = c.get('accounts') or ()
    if isinstance(accounts, str):
        accounts = (accounts,)
    return Settings(enabled=bool(c.get('enabled')),
                    runtime=str(c.get('runtime') or RUNTIME).replace('_', '-'),
                    environment=str(c.get('environment') or ''),
                    max_inflight=max(0, _int(max_inflight, 0)),
                    rows=str(c.get('rows') or ROWS_CLOUD_OK),
                    accounts=tuple(str(a) for a in accounts),
                    timeout_min=_float(c.get('timeout_min'), DEFAULT_TIMEOUT_MIN),
                    launch_wait_s=_float(c.get('launch_wait_s'), DEFAULT_LAUNCH_WAIT_S),
                    binary=str(c.get('binary') or wp.get('binary') or runtime_mod.DEFAULT_BINARY))


def lane_accounts(accounts, s):
    """The accounts whose login launches cloud sessions: ``cloud.accounts`` when named, else the
    ``role: cloud`` ones."""
    if s.accounts:
        return [a for a in accounts if a.name in s.accounts]
    return [a for a in accounts if a.role == 'cloud']


def eligible(row, s):
    """``cloud.rows: any`` takes every row; the default takes only a row marked ``cloud-ok``."""
    return s.rows == ROWS_ANY or bool(getattr(row, 'cloud_ok', False)) \
        or getattr(row, 'lane', None) == 'cloud'


def is_cloud(run):
    return cloudpid.is_token((run or {}).get('pid'))


# ---- launch -----------------------------------------------------------------------------------

def build_command(job, s):
    """The launch command line — a pure function of the job and the settings (golden-tested)."""
    cmd = [s.binary, '-p']
    cmd += ['--environment', s.environment] if s.environment else ['--cloud']
    if getattr(job, 'branch', None):
        cmd += ['--on-branch', job.branch]
    if job.model:
        cmd += ['--model', job.model]
    cmd += ['-n', job.name, '--forward-home-settings', 'false', '--output-format', 'json']
    return cmd


def _sh_quote(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def cloud_brief(text, job):
    """The brief a cloud session gets: the local brief, then the CLOUD block."""
    sid = job.session or ''
    exports = ' '.join(f'{k}={_sh_quote(v)}' for k, v in sorted(job.env.items())
                       if re.match(r'^[A-Z_][A-Z0-9_]*$', str(k)))
    lines = ['', '', 'CLOUD SESSION',
             'You run in a Claude Code cloud session, not on the factory host: clone, build and '
             'test happen here, in this session.',
             f'- The repository is checked out on branch `{job.branch}` (base `{job.base}`). '
             f'Commit and push on `{job.branch}` only; never push `{job.base}`, never force-push.']
    if getattr(job, 'setup', None):
        lines.append(f'- First set the checkout up: `{job.setup}`.')
    if exports:
        lines.append(f'- Every shell you run starts with: `export {exports}`.')
    lines += [f'- Every commit message carries the trailer `{SESSION_TRAILER}: {sid}` '
              f'(`git commit --trailer "{SESSION_TRAILER}: {sid}"`).',
              '- Run the tests the brief names here, before you push.',
              f'- Your last act: one commit — `--allow-empty` only when there is nothing else to '
              f'commit — whose message is the subject `asf: report {job.name}`, then your REPORT '
              f'block as the body, then the trailers `{SESSION_TRAILER}: {sid}` and '
              f'`{REPORT_TRAILER}: {job.name}`; push `{job.branch}`. The factory reads that commit '
              'as the end of this session; without it the session counts as unfinished until it '
              'times out.', '']
    return str(text or '').rstrip('\n') + '\n'.join(lines)


class Launch:
    """What the launcher printed: the cloud session's id and URL, an error, or (an attached
    launcher) the session's result line."""

    def __init__(self, session_id='', url='', error='', result=None):
        self.session_id = session_id
        self.url = url
        self.error = error
        self.result = result

    def __repr__(self):
        return f'Launch({self.session_id!r}, error={self.error!r}, result={self.result is not None})'


SESSION_LINE_RE = re.compile(r'^\s*Session ID:\s*(\S+)')
VIEW_LINE_RE = re.compile(r'^\s*View:\s*(https?://\S+)')


def parse_launch(log_path):
    """:class:`Launch` from the run's log: the CLI's JSON (``{"ok": true, "session_id", "url"}``,
    or ``{"ok": false, "error"}``), its text form (``Session ID: …``, ``View: …``, ``Error: …``)
    and an attached launcher's ``{"type": "result", "session_id", "session_url", …}``."""
    out = Launch()
    if not log_path or not os.path.exists(log_path):
        return out
    with open(log_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                rec = json.loads(text)
            except json.JSONDecodeError:
                rec = None
            if isinstance(rec, dict):
                if rec.get('type') == 'result':
                    out.result = rec
                    out.session_id = str(rec.get('session_id') or out.session_id)
                    out.url = str(rec.get('session_url') or out.url)
                elif rec.get('ok') is False:
                    out.error = str(rec.get('error') or 'cloud session not created')
                elif rec.get('ok') is True:
                    out.session_id = str(rec.get('session_id') or out.session_id)
                    out.url = str(rec.get('url') or rec.get('session_url') or out.url)
                continue
            m = SESSION_LINE_RE.match(text)
            if m:
                out.session_id = m.group(1)
                continue
            m = VIEW_LINE_RE.match(text)
            if m:
                out.url = m.group(1)
                continue
            if text.startswith('Error:') and not out.session_id:
                out.error = text[len('Error:'):].strip()
    return out


class CloudRuntime(runtime_mod.Runtime):
    """``claude-cloud``: the session runs as a Claude Code cloud session. ``run`` starts the
    launcher detached (the brief on stdin, its output into the job's log), waits up to
    ``launch_wait_s`` for the session's id, and returns a :class:`~asf.workers.runtime.Result`
    whose ``pid`` is the run's token (:mod:`asf.workers.cloudpid`)."""
    name = RUNTIME
    lane = 'cloud'

    def __init__(self, s, spawn=None, sleep=time.sleep, alive=None):
        self.settings = s
        self._spawn = spawn or detach.spawn
        self._sleep = sleep
        self._alive = alive or _pid_alive

    def run(self, job, wait=False):
        log_path = job.log_path or runtime_mod.job_log_path(job.product, job.name)
        runtime_mod.seed_home(job.account)
        stdin_path = f'{job.brief_path}.cloud'
        with open(job.brief_path, encoding='utf-8') as f:
            text = f.read()
        with open(stdin_path, 'w', encoding='utf-8') as f:
            f.write(cloud_brief(text, job))
        line = runtime_mod._session_line(job)
        tok = cloudpid.token(job.session or job.name)
        with open(stdin_path, 'rb') as brief, open(log_path, 'ab') as log:
            if line is not None:
                log.write((line + '\n').encode('utf-8'))
                log.flush()
            pid = self._spawn(build_command(job, self.settings), cwd=job.cwd,
                              env=runtime_mod.build_env(job), stdin=brief, stdout=log,
                              stderr=subprocess.STDOUT)
        launch = parse_launch(log_path)
        deadline = time.monotonic() + max(0.0, self.settings.launch_wait_s)
        while (not launch.session_id and not launch.error and self._alive(pid)
               and time.monotonic() < deadline):
            self._sleep(1.0)
            launch = parse_launch(log_path)
        cloudpid.record(tok, WORKING, 'launched', session=launch.session_id or None,
                        url=launch.url or None)
        result = runtime_mod.Result(pid=tok, log_path=log_path)
        result.extra = {'lane': 'cloud', 'launcher_pid': pid,
                        'cloud_session': launch.session_id or None,
                        'cloud_url': launch.url or None,
                        'cloud_environment': self.settings.environment or None}
        return result

    def continue_run(self, job, wait=False):
        return None  # a cloud session is not resumed from here: the caller falls back


def _pid_alive(pid):
    from asf.workers import lifecycle  # local: lifecycle reads this module's tokens
    return lifecycle.pid_alive(pid)


# ---- status -----------------------------------------------------------------------------------

def classify(launch, report, launcher_alive, elapsed_min, timeout_min):
    """``(status, why)`` — the cloud run's state from its evidence. Pure: no git, no clock."""
    if launch.result is not None:
        return FINISHED, 'the attached launcher returned the session result'
    if report:
        return FINISHED, f'report commit {report["sha"][:9]} on the branch'
    if launch.error and not launcher_alive:
        return DEAD, f'launch failed: {launch.error}'
    if timeout_min and elapsed_min is not None and elapsed_min >= timeout_min:
        return DEAD, f'timed out after {timeout_min:g}m'
    if launcher_alive:
        return WORKING, 'launcher attached' if launch.session_id else 'launching'
    if launch.session_id:
        return WORKING, f'cloud session {launch.session_id} running'
    return DEAD, 'the launcher exited without a cloud session'


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)


TRAILER_RE = re.compile(r'^(?P<key>[A-Za-z0-9-]+):\s*(?P<value>.*)$')


def report_commit(worktree, branch, session, fetch=True):
    """``{sha, body}`` of the newest commit on ``origin/<branch>`` whose trailers carry this run's
    ``ASF-Session`` and an ``ASF-Report`` — the cloud session's end marker — else None."""
    if not worktree or not os.path.isdir(worktree) or not branch or not session:
        return None
    if fetch:
        _git(['fetch', '-q', 'origin', branch], worktree)
    p = _git(['log', '-n', '50', '--format=%H%x1f%B%x1e', f'origin/{branch}'], worktree)
    if p.returncode != 0:
        return None
    for entry in p.stdout.split('\x1e'):
        sha, _, body = entry.strip('\n').partition('\x1f')
        if not sha:
            continue
        trailers = {}
        for ln in body.strip().splitlines()[::-1]:
            m = TRAILER_RE.match(ln.strip())
            if not m:
                break
            trailers.setdefault(m.group('key').lower(), m.group('value').strip())
        if trailers.get(SESSION_TRAILER.lower()) == session and REPORT_TRAILER.lower() in trailers:
            return {'sha': sha.strip(), 'body': body.strip()}
    return None


def _parse_ts(text):
    try:
        return datetime.datetime.strptime(str(text), '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _append(path, rec):
    with open(path, 'a+b') as f:
        f.seek(0, os.SEEK_END)
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b'\n':
                f.write(b'\n')
        f.write((json.dumps(rec) + '\n').encode('utf-8'))


def catch_up(worktree, branch):
    """Fast-forward the run's worktree to ``origin/<branch>``: health's evidence (pushed, empty,
    unpushed) is then read off the commits the cloud session made."""
    if not worktree or not os.path.isdir(worktree) or not branch:
        return False
    _git(['fetch', '-q', 'origin', branch], worktree)
    if _git(['merge-base', '--is-ancestor', 'HEAD', f'origin/{branch}'], worktree).returncode != 0:
        return False
    return _git(['merge', '-q', '--ff-only', f'origin/{branch}'], worktree).returncode == 0


def _account(cfg, name):
    from asf.workers import pool as pool_mod
    return next((a for a in pool_mod.accounts_from_config(cfg) if a.name == name), None)


def stop(run, cfg=None, run_cmd=None, kill=None):
    """Stop a cloud run: its attached launcher (a process here) is terminated, and the cloud
    session is told to stop (``claude -p --cloud <id> <stop text>``, under the account that
    launched it). ``(ok, detail)``; the token is recorded dead."""
    cfg = cfg if cfg is not None else _load_cfg()
    s = settings(cfg)
    kill = kill or _kill
    run_cmd = run_cmd or subprocess.run
    parts = []
    lp = run.get('launcher_pid')
    if lp and _pid_alive(lp):
        kill(lp)
        parts.append(f'launcher {lp} terminated')
    sid = run.get('cloud_session')
    if sid:
        job = runtime_mod.Job(run.get('product') or '', run.get('job') or '', run.get('worktree'),
                              None, None, account=_account(cfg, run.get('account')))
        try:
            p = run_cmd([s.binary, '-p', '--cloud', sid, STOP_TEXT], cwd=run.get('worktree') or None,
                        env=runtime_mod.build_env(job), capture_output=True, text=True, timeout=120)
            parts.append(f'session {sid} told to stop' if p.returncode == 0
                         else f'session {sid}: stop message refused (exit {p.returncode})')
        except (OSError, subprocess.SubprocessError, runtime_mod.AuthEnvError) as e:
            parts.append(f'session {sid}: stop message failed ({type(e).__name__})')
    cloudpid.record(run.get('pid') or cloudpid.token(run.get('session') or ''), DEAD, 'stopped')
    return True, '; '.join(parts) or 'nothing running here'


def _kill(pid):
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except (OSError, ValueError, TypeError):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (OSError, ValueError, TypeError):
            pass


def _load_cfg():
    from asf import env
    try:
        return env.load_config()
    except env.ConfigError:
        return {}


def sync(product, cfg=None, now=None, launcher_alive=None, stop_fn=None, out=print):
    """Every live cloud run of ``product`` brought up to date — see the module doc. Returns
    ``[(job, status, why)]`` for each run looked at."""
    from asf.workers import pool as pool_mod
    cfg = cfg if cfg is not None else _load_cfg()
    s = settings(cfg, product)
    now = time.time() if now is None else now
    launcher_alive = launcher_alive or _pid_alive
    stop_fn = stop_fn or (lambda run: stop(run, cfg))
    known = cloudpid.load()
    found = []
    for job, run in pool_mod.load_sessions(product).items():
        if run.get('ended') or not is_cloud(run):
            continue
        tok = run['pid']
        launch = parse_launch(run.get('log'))
        if launch.session_id and launch.session_id != run.get('cloud_session'):
            pool_mod.update_session(product, job, cloud_session=launch.session_id,
                                    cloud_url=launch.url or None)
            run.update(cloud_session=launch.session_id, cloud_url=launch.url or None)
        launch.session_id = launch.session_id or run.get('cloud_session') or ''
        report = None if launch.result is not None else report_commit(
            run.get('worktree'), run.get('branch'), run.get('session'))
        started = _parse_ts(run.get('started'))
        elapsed = None if started is None else (now - started) / 60.0
        up = launcher_alive(run.get('launcher_pid'))
        status, why = classify(launch, report, up, elapsed, s.timeout_min)
        if status == FINISHED and launch.result is None and report and run.get('log'):
            _append(run['log'], {'type': 'result', 'subtype': 'success', 'is_error': False,
                                 'result': report['body'],
                                 'session_id': launch.session_id or run.get('cloud_session'),
                                 'asf': {'cloud': {'report_commit': report['sha']}}})
        if status == DEAD:
            if why.startswith('timed out'):
                stop_fn(run)
            if run.get('log'):
                _append(run['log'], {'type': 'asf', 'subtype': 'cloud', 'status': DEAD, 'why': why})
        if status != WORKING:
            catch_up(run.get('worktree'), run.get('branch'))
        before = (known.get(tok) or {}).get('status')
        cloudpid.record(tok, status, why, session=launch.session_id or run.get('cloud_session'),
                        url=launch.url or run.get('cloud_url'), product=product.name, job=job)
        if status != before and status != WORKING:
            out(f'cloud    {job:<24} {status}: {why}')
        found.append((job, status, why))
    return found


# ---- placement's counts, status and doctor --------------------------------------------------

def inflight(product_name):
    """The product's live cloud runs (a working token)."""
    from asf.workers import lifecycle
    from asf.workers import pool as pool_mod
    return sum(1 for r in lifecycle.latest(pool_mod.sessions_path(product_name)).values()
               if is_cloud(r) and lifecycle.occupies(r))


def _origin_is_github(repo_dir):
    p = _git(['remote', 'get-url', 'origin'], repo_dir) if repo_dir and os.path.isdir(repo_dir) else None
    return bool(p and p.returncode == 0 and 'github.com' in p.stdout)


def doctor_rows(cfg, product, run_cmd=None):
    """``[(required, ok, detail)]`` for the cloud lane — nothing when the lane is not enabled."""
    s = settings(cfg, product)
    if not s.enabled:
        return []
    run_cmd = run_cmd or subprocess.run
    rows = []
    if s.runtime not in RUNTIMES:
        rows.append((True, False, f'cloud.runtime {s.runtime!r} is not one ASF runs '
                                  f'({", ".join(RUNTIMES)})'))
    if s.max_inflight <= 0:
        rows.append((True, False, 'cloud.max_inflight is 0: the lane has no seat — set it '
                                  '(or worker_pool.caps.cloud_max_inflight)'))
    if s.environment and not ENVIRONMENT_RE.match(s.environment):
        rows.append((True, False, f'cloud.environment {s.environment!r} is not a self-hosted '
                                  'environment id (ccpool_…)'))
    from asf.workers import pool as pool_mod
    accts = lane_accounts(pool_mod.accounts_from_config(cfg), s)
    if not accts:
        want = f'cloud.accounts {list(s.accounts)}' if s.accounts else 'a role: cloud account'
        rows.append((True, False, f'no cloud-lane account: {want} names none of '
                                  'worker_pool.accounts'))
    try:
        p = run_cmd([s.binary, '--help'], capture_output=True, text=True, timeout=30)
        text = (p.stdout or '') + (p.stderr or '')
    except (OSError, subprocess.SubprocessError) as e:
        text, p = '', None
        rows.append((True, False, f'{s.binary} --help failed ({type(e).__name__}): the runtime '
                                  'CLI is not installed where the tick runs'))
    if p is not None:
        need = ['--cloud'] + (['--environment'] if s.environment else [])
        missing = [flag for flag in need if flag not in text]
        if missing:
            rows.append((True, False, f'{s.binary} has no {", ".join(missing)}: upgrade the '
                                      'runtime CLI for cloud sessions'))
    if not _origin_is_github(getattr(product, 'repo_dir', None)):
        rows.append((True, False, 'the product repo has no GitHub origin: a cloud session '
                                  'checks the repository out from GitHub'))
    if not rows:
        where = f'environment {s.environment}' if s.environment else 'the default cloud environment'
        rows.append((False, True, f'cloud lane on: {s.max_inflight} seat(s) on {where}, '
                                  f'accounts {", ".join(a.name for a in accts)}, rows {s.rows}'))
    setup = getattr(getattr(product, 'conventions', None), 'worktree_setup', None)
    if setup:
        rows.append((False, True, f'the cloud environment must be able to run `{setup}` '
                                  '(its setup script, network access)'))
    return rows


def capacity_clause(cfg, product):
    """``cloud <inflight>/<max>`` for the status and capacity views, or '' when the lane is off."""
    s = settings(cfg, product)
    if not s.enabled:
        return ''
    return f'cloud {inflight(product.name)}/{s.max_inflight}'
