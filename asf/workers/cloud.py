"""asf.workers.cloud — the cloud lane: a worker session that runs off the factory host.

A local session runs, builds and tests on this host; under host pressure the waves held for hours.
The cloud lane moves the whole session off the host. Its runtime, ``actions``
(:mod:`asf.workers.actions`), dispatches the product's ``asf-worker`` workflow on the product's
own CI runners (``cloud.runs_on``): the job checks the branch out, installs the runtime CLI, runs
``claude -p`` with the brief, and commits and pushes that branch exactly as a local session does,
so harvest is unchanged.

The runtime ``claude-remote`` (:mod:`asf.workers.remote`) runs the session as a claude.ai routine
instead: the runtime CLI's ``RemoteTrigger`` tool creates and fires it on the dispatching
account's own login — no repo secret. The runtime ``claude-cloud`` is refused at config check
(:data:`REFUSED`, :func:`config_problems`): the runtime CLI cannot create a cloud session
non-interactively.

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

**Placement** (:func:`asf.workers.wave.wave`). With ``cloud.default: false`` (the default) the
cloud is OVERFLOW: a row goes there when the local lane cannot take it — no local seat, or host
pressure — ``cloud.enabled`` is true, the row is eligible (``cloud.rows: any``, or a ``cloud-ok``
row) and the lane is under ``cloud.max_inflight`` (else ``worker_pool.caps.cloud_max_inflight``).
With ``cloud.default: true`` the cloud is the DEFAULT executor: a row of a kind in
:data:`DEFAULT_KINDS` (every kind with ``cloud.rows: any``; a ``cloud-ok`` row too) goes to the
cloud first, up to ``max_inflight``; the local lane takes it when the cloud lane is full or
unready. A row whose kind is in ``cloud.local_only`` or whose item carries ``local_only: true``
never leaves the host, in either mode. Its accounts are ``cloud.accounts``, else the pool
accounts with ``role: cloud``: the account whose token the repo secret holds; its quota bands and
5h headroom apply as for any launch.

**Host pressure** holds the local lane only: a cloud row runs nothing here, and is bounded by
``max_inflight`` and its account's quota instead. The fair share still bounds both lanes: a
cloud session is a live session (:func:`asf.capacity.live_sessions`).

**Readiness** (:func:`readiness`, read once per wave — never per row): the lane takes launches
only while the doctor's critical checks pass (:func:`checks`: the lane is on, the token secret's
name is in the repo's secrets, the workflow is on the trunk, an online runner carries
``runs_on``, a cloud account resolves). An unready lane is one line — ``cloud lane unready:
<why> — local lane only`` — and every row stays local. ``asf cloud doctor --product <p>``
prints each check as ``ok`` or ``gap`` (:func:`doctor`).

Config (``~/.ASF/config.yaml``; a product file's ``cloud:`` overrides key by key)::

    cloud:
      enabled: true
      runtime: actions
      runs_on: [self-hosted, linux]   # the job's labels; default ubuntu-latest
      token_secret: CLAUDE_CODE_OAUTH_TOKEN
      max_inflight: 4
      rows: any                  # or cloud-ok (default)
      default: true              # cloud first (default false: overflow only)
      local_only: [groom]        # kinds that never leave the host
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
RUNTIME_REMOTE = 'claude-remote'
RUNTIME = RUNTIME_ACTIONS
#: the runtimes the lane launches with
RUNTIMES = (RUNTIME_ACTIONS, RUNTIME_REMOTE)
#: runtime -> why ASF refuses it at config check: a runtime that cannot launch is never enabled
REFUSED = {
    'claude-cloud': (
        'the runtime CLI cannot create a cloud session non-interactively: `claude -p --cloud` '
        'exits "Error: --cloud cannot be combined with --print. Starting a new cloud session '
        'with --cloud is interactive only", `claude --bg --cloud` exits "--bg and --cloud are '
        'different backends", and -p only messages an existing cloud session by id '
        '(verified on 2.1.282) — use runtime: claude-remote (a routine created through the '
        'CLI\'s RemoteTrigger tool) or actions'),
}
ROWS_ANY = 'any'
ROWS_CLOUD_OK = 'cloud-ok'
#: the row kinds ``cloud.default: true`` sends to the cloud first (``task`` is the feeder's name
#: for a ``coder`` row)
DEFAULT_KINDS = ('coder', 'task', 'correct', 'review', 'adjudicate', 'fix-bug', 'spec', 'plan',
                 'direct', 'groom')
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


#: ``cloud.max_creates_per_tick``'s default for ``claude-remote``: each create is a helper call
DEFAULT_REMOTE_CREATES_PER_TICK = 2


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
    default: bool = False
    local_only: tuple = ()
    # claude-remote (asf.workers.remote)
    environment_id: str = ''     # one environment for every lane account …
    environments: tuple = ()     # … or ((account, environment), …): environments are per account
    model: str = ''
    allowed_tools: tuple = ()
    helper_model: str = 'haiku'
    poll_min: float = 15
    #: cloud launches one wave may try (0: no limit) — a launch holds the tick while it runs
    max_creates_per_tick: int = 0

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
    envs = block.get('environment_id')
    if rt == RUNTIME_REMOTE and block.get('enabled') and not envs:
        out.append(('cloud.environment_id', 'claude-remote needs the claude.ai cloud environment '
                                            'the routine runs in (env_…, or {account: env_…})'))
    if isinstance(envs, dict) and not all(isinstance(v, str) and v for v in envs.values()):
        out.append(('cloud.environment_id', f'must map an account to an env_… id, not {envs!r}'))
    tools = block.get('allowed_tools')
    if tools is not None and not (isinstance(tools, list) and all(isinstance(t, str) for t in tools)):
        out.append(('cloud.allowed_tools', f'must be a list of tool names, not {tools!r}'))
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


def truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ('true', 'yes', 'on', '1')
    return bool(v)


def _kinds(v):
    if not v:
        return ()
    if isinstance(v, str):
        v = [p.strip() for p in v.split(',')]
    return tuple(str(p).strip() for p in v if str(p).strip())


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
                    workflow=str(c.get('workflow') or DEFAULT_WORKFLOW),
                    default=truthy(c.get('default')),
                    local_only=_kinds(c.get('local_only')),
                    environment_id='' if isinstance(c.get('environment_id'), dict)
                    else str(c.get('environment_id') or ''),
                    environments=tuple(sorted((str(k), str(v)) for k, v in
                                              c['environment_id'].items()))
                    if isinstance(c.get('environment_id'), dict) else (),
                    model=str(c.get('model') or ''),
                    allowed_tools=tuple(str(t) for t in c.get('allowed_tools') or ()
                                        if isinstance(c.get('allowed_tools'), list)),
                    helper_model=str(c.get('helper_model') or 'haiku'),
                    poll_min=_float(c.get('poll_min'), 15),
                    max_creates_per_tick=max(0, _int(
                        c.get('max_creates_per_tick'),
                        DEFAULT_REMOTE_CREATES_PER_TICK
                        if str(c.get('runtime') or '').replace('_', '-') == 'claude-remote'
                        else 0)))


def lane_accounts(accounts, s):
    """The accounts whose quota cloud sessions spend: ``cloud.accounts`` when named, else the
    ``role: cloud`` ones."""
    if s.accounts:
        out = [a for a in accounts if a.name in s.accounts]
    else:
        out = [a for a in accounts if a.role == 'cloud']
    if s.runtime == RUNTIME_REMOTE and s.environments and not s.environment_id:
        out = [a for a in out if a.name in dict(s.environments)]  # a routine needs its environment
    return out


def local_only(row, s):
    """The row never leaves the host: its kind is in ``cloud.local_only``, or its item says
    ``local_only: true``."""
    return getattr(row, 'kind', None) in s.local_only or truthy(getattr(row, 'local_only', False))


def eligible(row, s):
    """``cloud.rows: any`` takes every row; the default takes only a row marked ``cloud-ok``.
    A local-only row is never eligible."""
    if local_only(row, s):
        return False
    return s.rows == ROWS_ANY or bool(getattr(row, 'cloud_ok', False)) \
        or getattr(row, 'lane', None) == 'cloud'


def first(row, s):
    """``cloud.default: true``: the row goes to the cloud before the local lane — a kind in
    :data:`DEFAULT_KINDS` (any kind with ``cloud.rows: any``), or a ``cloud-ok`` row, and not
    local-only."""
    if not s.default or local_only(row, s):
        return False
    return eligible(row, s) or getattr(row, 'kind', None) in DEFAULT_KINDS


def is_cloud(run):
    return cloudpid.is_token((run or {}).get('pid'))


def lane_runtime(s, product):
    """The runtime object that launches this lane's sessions for ``product``."""
    if s.runtime == RUNTIME_REMOTE:
        from asf.workers import remote  # local: remote imports this module
        return remote.RemoteRuntime(s, product)
    from asf.workers import actions  # local: actions imports this module
    return actions.ActionsRuntime(s, product)


# ---- the brief --------------------------------------------------------------------------------

def cloud_brief(text, job, setting=None):
    """The brief a cloud session gets: the local brief, then the CLOUD block. ``setting``: the
    block's opening lines for a runtime that is not a CI job (:mod:`asf.workers.remote`)."""
    sid = job.session or ''
    setting = list(setting) if setting else [
        'You run in a CI job, not on the factory host: build and test happen here, in this '
        'job. The worker environment (ASF_SESSION, BACKLOG_ID_RANGE, the caps) is already '
        'exported, and the product setup has already run.',
        f'- The repository is checked out on branch `{job.branch}` (base `{job.base}`). '
        f'Commit and push on `{job.branch}` only; never push `{job.base}`, never force-push.']
    lines = ['', '', 'CLOUD SESSION', *setting,
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
    """Stop a cloud run: its workflow run is cancelled (``gh run cancel``), or its routine disabled
    (``claude-remote``). ``(ok, detail)``; the token is recorded dead."""
    from asf.workers import actions
    from asf.workers import remote
    if remote.is_remote(run):
        ok = remote.retire(run)
        cloudpid.record(run['pid'], DEAD, 'stopped')
        return True, f'routine {remote.trigger_of(run)} disabled' if ok \
            else f'routine {remote.trigger_of(run)} left as it is (disable refused or done)'
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


def sync(product, cfg=None, now=None, gh=None, stop_fn=None, out=print, remote_client=None):
    """Every live cloud run of ``product`` brought up to date — see the module doc. Returns
    ``[(job, status, why)]`` for each run looked at."""
    from asf.workers import actions
    from asf.workers import pool as pool_mod
    from asf.workers import remote
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
        if remote.is_remote(run):  # a claude-remote routine run
            run_id = run.get('remote_session_id')
        elif not run_id and run.get('actions_run_name'):
            hit = gh.find_run(s.workflow, run['actions_run_name'])
            if hit:
                run_id = hit['id']
                pool_mod.update_session(product, job, actions_run_id=run_id,
                                        cloud_url=hit.get('url') or None)
                run.update(actions_run_id=run_id, cloud_url=hit.get('url') or None)
        report = None
        if remote.is_remote(run):
            status, why, report = remote.evidence(run, s, elapsed, now, remote_client)
        elif not run.get('actions_run_name'):  # no workflow run: a launch of a refused runtime
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


def checks(cfg, product, run_cmd=None):
    """``[(name, required, ok, detail)]`` — every readiness check of the lane, one per line of
    ``asf cloud doctor``: on/default, runtime, seats, accounts, then the runtime's own
    (:func:`asf.workers.actions.checks`: workflow, secret, runner). ``required`` marks a
    critical check: one failing makes the lane unready (:func:`readiness`)."""
    s = settings(cfg, product)
    rows = [('enabled', True, s.enabled,
             'cloud.enabled: true' if s.enabled else 'cloud.enabled: false — the lane takes no '
                                                     'launch')]
    rows.append(('default', False, s.default,
                 'cloud.default: true — the cloud lane is the default executor' if s.default
                 else 'cloud.default: false — the cloud lane is overflow only'))
    if s.runtime in REFUSED:
        rows.append(('runtime', True, False,
                     f'cloud.runtime {s.runtime} is refused: {REFUSED[s.runtime]}'))
        return rows
    if s.runtime not in RUNTIMES:
        rows.append(('runtime', True, False, f'cloud.runtime {s.runtime!r} is not one ASF runs '
                                             f'({", ".join(RUNTIMES)})'))
    else:
        rows.append(('runtime', True, True, f'cloud.runtime {s.runtime}'))
    if s.max_inflight <= 0:
        rows.append(('seats', True, False, 'cloud.max_inflight is 0: the lane has no seat — set '
                                           'it (or worker_pool.caps.cloud_max_inflight)'))
    else:
        rows.append(('seats', True, True, f'cloud.max_inflight {s.max_inflight}'))
    from asf.workers import pool as pool_mod
    accts = lane_accounts(pool_mod.accounts_from_config(cfg), s)
    if not accts:
        want = f'cloud.accounts {list(s.accounts)}' if s.accounts else 'a role: cloud account'
        rows.append(('accounts', True, False, f'no cloud-lane account: {want} names none of '
                                              'worker_pool.accounts'))
    else:
        rows.append(('accounts', True, True,
                     f'cloud accounts {", ".join(a.name for a in accts)}'))
    if s.runtime == RUNTIME_ACTIONS:
        from asf.workers import actions
        rows += actions.checks(s, product, run_cmd=run_cmd)
    if s.runtime == RUNTIME_REMOTE:
        from asf.workers import remote
        gaps = remote.doctor_rows(s, product)
        rows += [('remote', req, ok, d) for req, ok, d in gaps]
        if not gaps:
            envs = s.environment_id or ', '.join(f'{a}={e}' for a, e in s.environments)
            rows.append(('remote', True, True, f'claude-remote: routines in {envs}, source '
                                               f'{remote.repo_url(product)}'))
    return rows


def readiness(cfg, product, run_cmd=None):
    """``(ready, why)``: the lane is on and every critical :func:`checks` row passes. The wave
    reads it once per tick — each call costs a few ``gh`` calls."""
    s = settings(cfg, product)
    if not s.on:
        return False, 'cloud lane off (cloud.enabled, runtime, max_inflight)'
    return _verdict(checks(cfg, product, run_cmd=run_cmd))


def _verdict(rows):
    gaps = [d for _n, req, ok, d in rows if req and not ok]
    return (not gaps), '; '.join(gaps)


def doctor(cfg, product, run_cmd=None, out=print):
    """``asf cloud doctor``: one ``ok``/``gap`` line per :func:`checks` row, then ``ready: yes`` or
    ``ready: no — <why>``. 0 when the lane is ready, else 1."""
    s = settings(cfg, product)
    out(f'cloud doctor: {product.name} (repo {getattr(product, "repo_slug", None) or "?"}, '
        f'trunk {getattr(product, "main", None) or "?"}, rows {s.rows}'
        f'{", local_only " + ",".join(s.local_only) if s.local_only else ""})')
    rows = checks(cfg, product, run_cmd=run_cmd)
    for _n, _req, ok, detail in rows:
        out(f'{"ok " if ok else "gap"} {detail}')
    ready, why = _verdict(rows)
    out('ready: yes' if ready else f'ready: no — {why}')
    return 0 if ready else 1


def doctor_rows(cfg, product, run_cmd=None):
    """``[(required, ok, detail)]`` for the cloud lane — nothing when the lane is not enabled:
    the failing :func:`checks`, else one green summary row."""
    s = settings(cfg, product)
    if not s.enabled:
        return []
    rows = [(req, ok, d) for name, req, ok, d in checks(cfg, product, run_cmd=run_cmd)
            if not ok and name not in ('enabled', 'default')]
    if not rows:
        from asf.workers import pool as pool_mod
        accts = lane_accounts(pool_mod.accounts_from_config(cfg), s)
        rows.insert(0, (False, True, f'cloud lane on: {s.max_inflight} seat(s), runtime '
                                     f'{s.runtime} on [{", ".join(s.runs_on)}], accounts '
                                     f'{", ".join(a.name for a in accts)}, rows {s.rows}'
                                     f'{", default" if s.default else ""}'))
    return rows


def capacity_clause(cfg, product):
    """``cloud <inflight>/<max>`` for the status and capacity views, or '' when the lane is off."""
    s = settings(cfg, product)
    if not s.enabled:
        return ''
    return f'cloud {inflight(product.name)}/{s.max_inflight}'
