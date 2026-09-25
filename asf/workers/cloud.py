"""asf.workers.cloud — the cloud lane: a worker session that runs off the factory host.

A local session runs, builds and tests on this host; under host pressure the waves held for hours.
The cloud lane moves the whole session off the host. Its runtime, ``actions``
(:mod:`asf.workers.actions`), dispatches the product's ``asf-worker`` workflow on the product's
own CI runners (``cloud.runs_on``): the job checks the branch out, installs the runtime CLI, runs
``claude -p`` with the brief, and commits and pushes that branch exactly as a local session does,
so harvest is unchanged.

The runtime ``claude-cloud`` is refused at config check (:data:`REFUSED`,
:func:`config_problems`): the runtime CLI cannot create a cloud session non-interactively.

The brief (:func:`cloud_brief`) is the local brief plus a CLOUD block: the branch, the
``ASF-Session`` trailer each commit carries — no host hook stamps it there — and the end marker:
the session's last commit carries its REPORT as the body and an ``ASF-Report: <job>`` trailer,
and is pushed.

**Tracking.** The run records ``pid: actions:<run id>`` (:mod:`asf.workers.cloudpid`), the run's
name, its URL and the brief ref. :func:`sync` (run first by every health pass) reads the workflow
run's status, the branch on origin and the clock, and maps them onto the session states
(:func:`classify`): **working**, **finished** (the report commit on the branch — whose body
becomes the log's result line) or **dead** (the run ended without the report commit, it never
appeared, or it passed ``cloud.timeout_min`` — then it is cancelled, :func:`stop`). A finished or
dead run's worktree is brought up to ``origin/<branch>``, its brief ref is deleted, and the state
lands in the status file every liveness check reads — from then on health judges it like any run:
pushed or not, empty or not.

**Placement** (:func:`asf.workers.wave.wave`): a row goes to the cloud lane when the local lane
cannot take it — no local seat, or host pressure — ``cloud.enabled`` is true, the row is eligible
(``cloud.rows: any``, or a ``cloud-ok`` row) and the lane is under ``cloud.max_inflight``
(else ``worker_pool.caps.cloud_max_inflight``). Its accounts are ``cloud.accounts``, else the
pool accounts with ``role: cloud``: the account whose token the repo secret holds; its quota
bands and 5h headroom apply as for any launch.

Config (``~/.ASF/config.yaml``; a product file's ``cloud:`` overrides key by key)::

    cloud:
      enabled: true
      runtime: actions
      runs_on: [self-hosted, linux]   # the job's labels; default ubuntu-latest
      token_secret: CLAUDE_CODE_OAUTH_TOKEN
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
import subprocess
import time

from asf.workers import cloudpid

RUNTIME_ACTIONS = 'actions'
RUNTIME = RUNTIME_ACTIONS
#: the runtimes the lane launches with
RUNTIMES = (RUNTIME_ACTIONS,)
#: runtime -> why ASF refuses it at config check: a runtime that cannot launch is never enabled
REFUSED = {
    'claude-cloud': (
        'the runtime CLI cannot create a cloud session non-interactively: `claude -p --cloud` '
        'exits "Error: --cloud cannot be combined with --print. Starting a new cloud session '
        'with --cloud is interactive only", `claude --bg --cloud` exits "--bg and --cloud are '
        'different backends", and -p only messages an existing cloud session by id '
        '(verified on 2.1.282) — use runtime: actions'),
}
ROWS_ANY = 'any'
ROWS_CLOUD_OK = 'cloud-ok'
DEFAULT_TIMEOUT_MIN = 240
DEFAULT_LAUNCH_WAIT_S = 30
DEFAULT_RUNS_ON = ('ubuntu-latest',)
DEFAULT_TOKEN_SECRET = 'CLAUDE_CODE_OAUTH_TOKEN'
DEFAULT_WORKFLOW = 'asf-worker.yml'
#: a dispatched run that has not shown up in the run list after this long never will
LOST_AFTER_MIN = 15
REPORT_TRAILER = 'ASF-Report'
SESSION_TRAILER = 'ASF-Session'
SECRET_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

WORKING, FINISHED, DEAD = cloudpid.WORKING, cloudpid.FINISHED, cloudpid.DEAD


# ---- config -----------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Settings:
    enabled: bool = False
    runtime: str = RUNTIME
    max_inflight: int = 0
    rows: str = ROWS_CLOUD_OK
    accounts: tuple = ()
    timeout_min: float = DEFAULT_TIMEOUT_MIN
    launch_wait_s: float = DEFAULT_LAUNCH_WAIT_S
    runs_on: tuple = DEFAULT_RUNS_ON
    token_secret: str = DEFAULT_TOKEN_SECRET
    workflow: str = DEFAULT_WORKFLOW

    @property
    def on(self):
        """The lane can take a launch: enabled, a runtime it runs, and a seat to give."""
        return self.enabled and self.runtime in RUNTIMES and self.max_inflight > 0


def config_problems(block):
    """``[(dotted key, problem)]`` for a ``cloud:`` block (the operator's, or a product file's):
    a refused runtime (:data:`REFUSED`) is a config error, so nobody enables a lane that cannot
    launch. Called by :func:`asf.env.load_config` and the product-file check."""
    if not isinstance(block, dict):
        return [] if block is None else [('cloud', f'must be a map, not {block!r}')]
    out = []
    written = block.get('runtime')
    rt = str(written or RUNTIME).replace('_', '-')
    if rt in REFUSED and (written or block.get('enabled')):
        out.append(('cloud.runtime', f'{rt} is refused: {REFUSED[rt]}'))
    secret = block.get('token_secret')
    if secret is not None and not SECRET_RE.match(str(secret)):
        out.append(('cloud.token_secret', f'must be a repo secret name, not {secret!r}'))
    return out


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


def _labels(v):
    if not v:
        return DEFAULT_RUNS_ON
    if isinstance(v, str):
        v = [p.strip() for p in v.split(',')]
    return tuple(str(p) for p in v if str(p).strip())


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
                    max_inflight=max(0, _int(max_inflight, 0)),
                    rows=str(c.get('rows') or ROWS_CLOUD_OK),
                    accounts=tuple(str(a) for a in accounts),
                    timeout_min=_float(c.get('timeout_min'), DEFAULT_TIMEOUT_MIN),
                    launch_wait_s=_float(c.get('launch_wait_s'), DEFAULT_LAUNCH_WAIT_S),
                    runs_on=_labels(c.get('runs_on')),
                    token_secret=str(c.get('token_secret') or DEFAULT_TOKEN_SECRET),
                    workflow=str(c.get('workflow') or DEFAULT_WORKFLOW))


def lane_accounts(accounts, s):
    """The accounts whose quota cloud sessions spend: ``cloud.accounts`` when named, else the
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


def lane_runtime(s, product):
    """The runtime object that launches this lane's sessions for ``product``."""
    from asf.workers import actions  # local: actions imports this module
    return actions.ActionsRuntime(s, product)


# ---- the brief --------------------------------------------------------------------------------

def cloud_brief(text, job):
    """The brief a cloud session gets: the local brief, then the CLOUD block."""
    sid = job.session or ''
    lines = ['', '', 'CLOUD SESSION',
             'You run in a CI job, not on the factory host: build and test happen here, in this '
             'job. The worker environment (ASF_SESSION, BACKLOG_ID_RANGE, the caps) is already '
             'exported, and the product setup has already run.',
             f'- The repository is checked out on branch `{job.branch}` (base `{job.base}`). '
             f'Commit and push on `{job.branch}` only; never push `{job.base}`, never force-push.',
             f'- Every commit message carries the trailer `{SESSION_TRAILER}: {sid}` '
             f'(`git commit --trailer "{SESSION_TRAILER}: {sid}"`).',
             '- Run the tests the brief names here, before you push.',
             f'- Your last act: one commit — `--allow-empty` only when there is nothing else to '
             f'commit — whose message is the subject `asf: report {job.name}`, then your REPORT '
             f'block as the body, then the trailers `{SESSION_TRAILER}: {sid}` and '
             f'`{REPORT_TRAILER}: {job.name}`; push `{job.branch}`. The factory reads that commit '
             'as the end of this session; without it the session counts as failed.', '']
    return str(text or '').rstrip('\n') + '\n'.join(lines)


# ---- status -----------------------------------------------------------------------------------

def classify(view, report, elapsed_min, timeout_min, run_id=None):
    """``(status, why)`` — the cloud run's state from its evidence: ``view`` is the workflow
    run's ``{status, conclusion}`` (None when unread), ``report`` the report commit or None.
    Pure: no gh, no git, no clock."""
    if report:
        return FINISHED, f'report commit {report["sha"][:9]} on the branch'
    state = (view or {}).get('status')
    if state == 'completed':
        conclusion = (view or {}).get('conclusion') or 'unknown'
        return DEAD, f'run {run_id} ended {conclusion} without the report commit'
    if timeout_min and elapsed_min is not None and elapsed_min >= timeout_min:
        return DEAD, f'timed out after {timeout_min:g}m'
    if not run_id:
        if elapsed_min is not None and elapsed_min >= LOST_AFTER_MIN:
            return DEAD, 'the dispatched run never appeared'
        return WORKING, 'dispatched'
    return WORKING, f'run {run_id} {state or "unread"}'


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


def _product_of(run):
    from asf import env
    try:
        return env.load_product(run.get('product'))
    except env.ConfigError:
        return None


def stop(run, product=None, gh=None):
    """Stop a cloud run: its workflow run is cancelled (``gh run cancel``). ``(ok, detail)``; the
    token is recorded dead."""
    from asf.workers import actions
    parts, ok = [], True
    run_id = run.get('actions_run_id')
    product = product or _product_of(run)
    if run_id and product is not None:
        ok, detail = (gh or actions.Gh(product)).cancel(run_id)
        parts.append(detail)
    cloudpid.record(run.get('pid') or cloudpid.token(run.get('session') or ''), DEAD, 'stopped')
    return ok, '; '.join(parts) or 'no workflow run to cancel'


def _load_cfg():
    from asf import env
    try:
        return env.load_config()
    except env.ConfigError:
        return {}


def sync(product, cfg=None, now=None, gh=None, stop_fn=None, out=print):
    """Every live cloud run of ``product`` brought up to date — see the module doc. Returns
    ``[(job, status, why)]`` for each run looked at."""
    from asf.workers import actions
    from asf.workers import pool as pool_mod
    cfg = cfg if cfg is not None else _load_cfg()
    s = settings(cfg, product)
    now = time.time() if now is None else now
    gh = gh or actions.Gh(product)
    stop_fn = stop_fn or (lambda run: stop(run, product, gh))
    known = cloudpid.load()
    found = []
    for job, run in pool_mod.load_sessions(product).items():
        if run.get('ended') or not is_cloud(run):
            continue
        tok = run['pid']
        started = _parse_ts(run.get('started'))
        elapsed = None if started is None else (now - started) / 60.0
        run_id = run.get('actions_run_id')
        if not run_id and run.get('actions_run_name'):
            hit = gh.find_run(s.workflow, run['actions_run_name'])
            if hit:
                run_id = hit['id']
                pool_mod.update_session(product, job, actions_run_id=run_id,
                                        cloud_url=hit.get('url') or None)
                run.update(actions_run_id=run_id, cloud_url=hit.get('url') or None)
        report = None
        if not run.get('actions_run_name'):  # no workflow run: a launch of a refused runtime
            status, why = DEAD, 'no workflow run (a refused runtime launched it)'
        else:
            view = gh.view(run_id) if run_id else None  # before the report: no race with its end
            report = report_commit(run.get('worktree'), run.get('branch'), run.get('session'))
            status, why = classify(view, report, elapsed, s.timeout_min, run_id)
        if status == FINISHED and report and run.get('log'):
            _append(run['log'], {'type': 'result', 'subtype': 'success', 'is_error': False,
                                 'result': report['body'],
                                 'asf': {'cloud': {'report_commit': report['sha'],
                                                   'run': run_id}}})
        if status == DEAD:
            if why.startswith('timed out'):
                stop_fn(run)
            if run.get('log'):
                _append(run['log'], {'type': 'asf', 'subtype': 'cloud', 'status': DEAD, 'why': why})
        if status != WORKING:
            catch_up(run.get('worktree'), run.get('branch'))
            if run.get('brief_ref'):
                actions.delete_brief(run.get('worktree'), run['brief_ref'])
        before = (known.get(tok) or {}).get('status')
        cloudpid.record(tok, status, why, run=run_id, url=run.get('cloud_url'),
                        product=product.name, job=job)
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


def doctor_rows(cfg, product, run_cmd=None):
    """``[(required, ok, detail)]`` for the cloud lane — nothing when the lane is not enabled."""
    s = settings(cfg, product)
    if not s.enabled:
        return []
    if s.runtime in REFUSED:
        return [(True, False, f'cloud.runtime {s.runtime} is refused: {REFUSED[s.runtime]}')]
    rows = []
    if s.runtime not in RUNTIMES:
        rows.append((True, False, f'cloud.runtime {s.runtime!r} is not one ASF runs '
                                  f'({", ".join(RUNTIMES)})'))
    if s.max_inflight <= 0:
        rows.append((True, False, 'cloud.max_inflight is 0: the lane has no seat — set it '
                                  '(or worker_pool.caps.cloud_max_inflight)'))
    from asf.workers import pool as pool_mod
    accts = lane_accounts(pool_mod.accounts_from_config(cfg), s)
    if not accts:
        want = f'cloud.accounts {list(s.accounts)}' if s.accounts else 'a role: cloud account'
        rows.append((True, False, f'no cloud-lane account: {want} names none of '
                                  'worker_pool.accounts'))
    if s.runtime == RUNTIME_ACTIONS:
        from asf.workers import actions
        rows += actions.doctor_rows(s, product, run_cmd=run_cmd)
    if not any(not ok for _r, ok, _d in rows):
        rows.insert(0, (False, True, f'cloud lane on: {s.max_inflight} seat(s), runtime '
                                     f'{s.runtime} on [{", ".join(s.runs_on)}], accounts '
                                     f'{", ".join(a.name for a in accts)}, rows {s.rows}'))
    return rows


def capacity_clause(cfg, product):
    """``cloud <inflight>/<max>`` for the status and capacity views, or '' when the lane is off."""
    s = settings(cfg, product)
    if not s.enabled:
        return ''
    return f'cloud {inflight(product.name)}/{s.max_inflight}'
