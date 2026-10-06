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
appeared, it stopped beating — :mod:`asf.workers.heartbeat`, then it is continued in the same
pass — or it passed ``cloud.timeout_min``, the backstop — then it is cancelled, :func:`stop`). A finished or
dead run's worktree is brought up to ``origin/<branch>``, its brief ref is deleted, and the state
lands in the status file every liveness check reads — from then on health judges it like any run:
pushed or not, empty or not.

**Placement** (:func:`asf.workers.wave.wave`) follows ``cloud.mode`` (:data:`MODES`), re-read
every tick — the tick is a fresh process that loads both files, so a one-line change applies on
the next tick with no restart. ``overflow`` (the default; ``local`` is its alias: local first)
and ``primary`` are below; ``off`` launches no new cloud session whatever else is configured —
the live ones drain as usual (:func:`sync` reads them on every health pass) — and keeps the
``role: cloud`` accounts off the local lane. ``cloud.default: true`` is the older spelling of
``mode: primary``; a written ``mode`` wins.

With ``cloud.mode: primary`` the cloud is the
DEFAULT executor: every row goes there first except one whose kind is in :data:`LOCAL_KINDS` —
the closed exception list of three kinds the lane structurally cannot collect — or in
``cloud.local_only``, or whose item carries ``local_only: true``; the local lane takes such a row,
and takes any other one too once the cloud lane cannot: it is full (``max_inflight``) or unready
(the doctor's critical checks), no lane account has quota headroom, the tick's
``max_creates_per_tick`` is spent, or its launches are erroring — ``cloud.fallback_failures``
(default 3) failed creates within ``cloud.fallback_window_min`` (default 30) trip a
``cloud.fallback_cooldown_min`` (default 30) cool-down in which every row goes local
(:class:`Breaker`). Each such launch says why on its tick-log line (``… — cloud fallback:
<why>``) and the status row's cloud clause carries the last one (:func:`capacity_clause`).
With ``cloud.mode: overflow`` the cloud is OVERFLOW: a row goes there only when the local lane cannot take it — no local seat,
or host pressure — ``cloud.enabled`` is true, the row is eligible (``cloud.rows: any``, or a
``cloud-ok`` row) and the lane is under ``cloud.max_inflight`` (else
``worker_pool.caps.cloud_max_inflight``); the exception list holds in this mode too — the
overflow door never opens for a :data:`LOCAL_KINDS` row either. Its accounts are
``cloud.accounts``, else the pool accounts with ``role: cloud``: the account whose token the repo
secret holds; its quota bands and 5h headroom apply as for any launch.

**Host pressure** holds the local lane only: a cloud row runs nothing here, and is bounded by
``max_inflight`` and its account's quota instead. The fair share bounds the local lane: the wave
step adds ``max_inflight`` seats beside the share and hands the wave the share's free local seats
(``local_seats``: the share less the local sessions live), so the local accounts never take the
cloud's seats; the status row reads ``sessions <local>/<share>, cloud <n>/<max_inflight>``.

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
      mode: primary              # overflow (default; alias local) | primary | off
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
#: The closed list of kinds that never leave the factory host — the EXCEPTION, in both modes and
#: under every setting. A kind is here only when the cloud lane has no channel back for its work:
#: the lane collects a session by one pushed commit on the product branch and nothing else
#: (asf.workers.actions's workflow, :func:`report_commit`), and these three produce none.
#:
#: ``groom``, ``groom-clerk`` — the brief forbids the commit and the push in as many words ("THE
#:   REPOSITORY IS NOT YOUR WORK"), and the answers file it writes instead is a path on this host
#:   that the next tick reads. In the cloud the run ends ``dead`` and the day's answers are lost.
#: ``close`` — one report line, no commit, no push, and a brief that says to leave the branch
#:   alone; there is nothing for the lane to carry back.
#:
#: ``cloud.local_only`` adds to this list. Nothing takes from it (F-0216 C4).
LOCAL_KINDS = ('groom', 'groom-clerk', 'close')
DEFAULT_TIMEOUT_MIN = 240
DEFAULT_LAUNCH_WAIT_S = 30
DEFAULT_RUNS_ON = ('ubuntu-latest',)
DEFAULT_TOKEN_SECRET = 'CLAUDE_CODE_OAUTH_TOKEN'
DEFAULT_WORKFLOW = 'asf-worker.yml'
#: a dispatched run that has not shown up in the run list after this long never will (config
#: ``cloud.lost_after_min``)
LOST_AFTER_MIN = 15
REPORT_TRAILER = 'ASF-Report'
SESSION_TRAILER = 'ASF-Session'
SECRET_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

WORKING, FINISHED, DEAD = cloudpid.WORKING, cloudpid.FINISHED, cloudpid.DEAD


#: ``cloud.max_creates_per_tick``'s default for ``claude-remote``: each create is a helper call
DEFAULT_REMOTE_CREATES_PER_TICK = 2

MODE_OVERFLOW, MODE_PRIMARY, MODE_OFF = 'overflow', 'primary', 'off'
#: ``cloud.mode``'s values: overflow (local first, the cloud takes what local cannot), primary
#: (cloud first, local is the exception and the fallback), off (no new cloud launch)
MODES = (MODE_OVERFLOW, MODE_PRIMARY, MODE_OFF)
#: the other spellings ``cloud.mode`` accepts, and the mode each one is
MODE_ALIASES = {'local': MODE_OVERFLOW}
#: primary mode's launch-error fallback (:class:`Breaker`): this many failed cloud creates …
DEFAULT_FALLBACK_FAILURES = 3
#: … within this many minutes send every row local for …
DEFAULT_FALLBACK_WINDOW_MIN = 30
#: … this many minutes
DEFAULT_FALLBACK_COOLDOWN_MIN = 30
#: how long ``asf status`` keeps naming the last fallback
FALLBACK_SHOWN_S = 3600


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
    default: bool = False        # mode == primary (the field ``cloud.default`` used to set)
    local_only: tuple = ()
    mode: str = MODE_OVERFLOW
    fallback_failures: int = DEFAULT_FALLBACK_FAILURES
    fallback_window_min: float = DEFAULT_FALLBACK_WINDOW_MIN
    fallback_cooldown_min: float = DEFAULT_FALLBACK_COOLDOWN_MIN
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
        """The lane can take a launch: enabled, not ``mode: off``, a runtime it runs, and a seat
        to give."""
        return (self.enabled and self.mode != MODE_OFF and self.runtime in RUNTIMES
                and self.max_inflight > 0)


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
    mode = block.get('mode')
    if mode is not None and parse_mode(mode) is None:
        out.append(('cloud.mode', f'must be one of {", ".join(MODES + tuple(MODE_ALIASES))}, '
                                  f'not {mode!r}'))
    for key in ('fallback_failures', 'fallback_window_min', 'fallback_cooldown_min'):
        v = block.get(key)
        if v is not None and (isinstance(v, bool) or _float(v, -1) <= 0):
            out.append((f'cloud.{key}', f'must be a number above 0, not {v!r}'))
    from asf.workers import heartbeat  # the lane's override of workers.heartbeat_*
    return out + heartbeat.config_problems(block, 'cloud')


def parse_mode(v):
    """``cloud.mode`` as one of :data:`MODES` (an alias resolved), or None for a value it is
    not. A YAML ``off`` read as false is ``off``."""
    if v is False:
        return MODE_OFF
    m = str(v).strip().lower() if isinstance(v, str) else None
    m = MODE_ALIASES.get(m, m)
    return m if m in MODES else None


def _mode(c):
    """The block's mode: ``mode`` when written (an unreadable one is the default — the config
    check refuses it), else ``default: true`` → primary, else overflow."""
    if c.get('mode') is not None:
        return parse_mode(c.get('mode')) or MODE_OVERFLOW
    return MODE_PRIMARY if truthy(c.get('default')) else MODE_OVERFLOW


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
                    default=_mode(c) == MODE_PRIMARY,
                    local_only=_kinds(c.get('local_only')),
                    mode=_mode(c),
                    fallback_failures=max(1, _int(c.get('fallback_failures'),
                                                  DEFAULT_FALLBACK_FAILURES)),
                    fallback_window_min=_float(c.get('fallback_window_min'),
                                               DEFAULT_FALLBACK_WINDOW_MIN),
                    fallback_cooldown_min=_float(c.get('fallback_cooldown_min'),
                                                 DEFAULT_FALLBACK_COOLDOWN_MIN),
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
    """The row never leaves the host: its kind is a :data:`LOCAL_KINDS` one (the floor, both
    modes — F-0216 C3), a kind the operator added in ``cloud.local_only``, or its item says
    ``local_only: true``. The kind is the canonical one: ``asf.tick.step_wave.worker_row``
    carries ``brief.kind``, which is :func:`asf.briefs.build.normalize_kind`'s output."""
    kind = getattr(row, 'kind', None)
    return kind in LOCAL_KINDS or kind in s.local_only \
        or truthy(getattr(row, 'local_only', False))


def eligible(row, s):
    """``cloud.rows: any`` takes every row; the default takes only a row marked ``cloud-ok``.
    A local-only row is never eligible."""
    if local_only(row, s):
        return False
    return s.rows == ROWS_ANY or bool(getattr(row, 'cloud_ok', False)) \
        or getattr(row, 'lane', None) == 'cloud'


def first(row, s):
    """``cloud.mode: primary``: the row goes to the cloud lane before the local one. Every kind
    but a local-only one — there is no allow-list (``cloud.rows`` bounds the overflow path)."""
    return bool(s.default) and not local_only(row, s)


def is_cloud(run):
    return cloudpid.is_token((run or {}).get('pid'))


def lane_runtime(s, product):
    """The runtime object that launches this lane's sessions for ``product``: the active runtime
    connector's cloud runtime (``cloud.runtime``: ``claude-remote`` or ``actions``)."""
    from asf import connectors  # local: the connector imports this module
    return connectors.get('runtime').cloud(s, product)


# ---- the brief --------------------------------------------------------------------------------

#: The CLOUD block's push rule for a product that declares ``conventions.pre_push_check``: a cloud
#: job has no pre-push hook, so the host's repo checks never ran there (2026-10-05: six PR reds
#: in a day were a forbidden name in a review file a cloud session committed and pushed).
CLOUD_PRE_PUSH = ('- No pre-push hook runs here. Before every push, the review/report push '
                  'included, run the product\'s pre-push check `{command}` and see it pass: a '
                  'red one is fixed and committed first, never pushed for CI to find.')


def cloud_brief(text, job, setting=None, product=None):
    """The brief a cloud session gets: the local brief, then the CLOUD block. ``setting``: the
    block's opening lines for a runtime that is not a CI job (:mod:`asf.workers.remote`).
    ``product``: names its ``conventions.pre_push_check`` (:data:`CLOUD_PRE_PUSH`) when set."""
    sid = job.session or ''
    setting = list(setting) if setting else [
        'You run in a CI job, not on the factory host: build and test happen here, in this '
        'job. The worker environment (ASF_SESSION, BACKLOG_ID_RANGE, the caps) is already '
        'exported, and the product setup has already run.',
        f'- The repository is checked out on branch `{job.branch}` (base `{job.base}`). '
        f'Commit and push on `{job.branch}` only; never push `{job.base}`, never force-push.']
    id_range = ((getattr(job, 'env', None) or {}).get('BACKLOG_ID_RANGE') or '').strip()
    if id_range:  # claimed on origin before launch (asf.record.idclaim): the text is the handover
        setting.append(
            f'- Your id block, claimed for this session: `BACKLOG_ID_RANGE={id_range}`. Every '
            'new S-/T-/B- id you write (a card, a spec, a plan) is the next free number of '
            'this block for its prefix; never invent one outside it — the record refuses an '
            'id no claim covers when the work lands.')
    lines = ['', '', 'CLOUD SESSION', *setting,
             f'- Every commit message carries the trailer `{SESSION_TRAILER}: {sid}` '
             f'(`git commit --trailer "{SESSION_TRAILER}: {sid}"`).',
             '- Run the tests the brief names here, before you push.',
             '- A review file the brief says to leave uncommitted is committed here, with the '
             'report commit: off the factory host the branch is the only way back.',
             *_pre_push_lines(product),
             f'- Your last act: one commit — `--allow-empty` only when there is nothing else to '
             f'commit — whose message is the subject `asf: report {job.name}`, then your REPORT '
             f'block as the body, then the trailers `{SESSION_TRAILER}: {sid}` and '
             f'`{REPORT_TRAILER}: {job.name}`; push `{job.branch}`. The factory reads that commit '
             'as the end of this session; without it the session counts as failed.', '']
    beat = getattr(job, 'heartbeat', None)
    if beat is not None:  # asf.workers.heartbeat: the run's proof of movement, every runtime
        from asf.workers import heartbeat
        lines[-1:] = heartbeat.brief_lines(job.name, sid, beat) + ['']
    return str(text or '').rstrip('\n') + '\n'.join(lines)


def _pre_push_lines(product):
    if product is None:
        return []
    from asf import approvals
    try:
        command = approvals.pre_push_check(product)
    except Exception:  # noqa: BLE001 — no check readable: no line
        command = None
    return [CLOUD_PRE_PUSH.format(command=command)] if command else []


# ---- status -----------------------------------------------------------------------------------

def classify(view, report, elapsed_min, timeout_min, run_id=None, lost_after_min=None):
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
        if lost_after_min is None:
            from asf import config_keys
            lost_after_min = config_keys.value('cloud.lost_after_min', LOST_AFTER_MIN)
        if elapsed_min is not None and elapsed_min >= lost_after_min:
            return DEAD, 'the dispatched run never appeared'
        return WORKING, 'dispatched'
    return WORKING, f'run {run_id} {state or "unread"}'


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)


TRAILER_RE = re.compile(r'^(?P<key>[A-Za-z0-9-]+):\s*(?P<value>.*)$')


class ReportUnreadable(Exception):
    """``report_commit``'s own fetch of ``origin/<branch>`` failed (a network blip, a rate
    limit): the local view of the branch cannot be trusted, so the caller must never read a
    not-found report off it as a definite answer (B-0293: a transient fetch failure read that
    way wrongly ended a finished cloud or remote-routine run ``dead pid`` — permanently, since
    neither lane revisits an ``ended`` run)."""


def report_commit(worktree, branch, session, fetch=True):
    """``{sha, body}`` of the newest commit on ``origin/<branch>`` whose trailers carry this run's
    ``ASF-Session`` and an ``ASF-Report`` — the cloud session's end marker — else None.
    Raises :class:`ReportUnreadable` when ``fetch`` itself fails: a stale or absent local view of
    ``origin/<branch>`` is never read as "no report yet"."""
    if not worktree or not os.path.isdir(worktree) or not branch or not session:
        return None
    if fetch:
        fetched = _git(['fetch', '-q', 'origin', branch], worktree)
        if fetched.returncode != 0:
            raise ReportUnreadable((fetched.stderr or fetched.stdout or 'git fetch failed').strip())
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


def sync(product, cfg=None, now=None, gh=None, stop_fn=None, out=print, remote_client=None,
         beats=None, relaunch_runtime=None):
    """Every live cloud run of ``product`` brought up to date — see the module doc. Returns
    ``[(job, status, why)]`` for each run looked at.

    A working run launched with the heartbeat (:mod:`asf.workers.heartbeat`) is also judged by
    its beats — ``beats``: the pass's one ``ls-remote`` — and a STALLED one ends ``dead``
    (``stalled: no beat <n>m``, with the routine's ``worker_status`` from one ``list_runs``), is
    stopped, and is continued in this same pass on ``relaunch_runtime`` (default: the run's own
    runtime)."""
    from asf.workers import actions
    from asf.workers import heartbeat
    from asf.workers import pool as pool_mod
    from asf.workers import remote
    cfg = cfg if cfg is not None else _load_cfg()
    s = settings(cfg, product)
    now = time.time() if now is None else now
    gh = gh or actions.Gh(product)
    stop_fn = stop_fn or (lambda run: stop(run, product, gh))
    known = cloudpid.load()
    beats = beats or heartbeat.Beats(product)
    hb_state = heartbeat.load_state(product)
    hb_before = json.dumps(hb_state, sort_keys=True)
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
            look = gh.find_run(s.workflow, run['actions_run_name'])
            if look.unknown:  # Unknown is no answer: never "not found", never LOST/DEAD from it
                why = f'run lookup unreadable ({look.reason}): left as it is'
                out(f'cloud    {job:<24} {why}')
                found.append((job, (known.get(tok) or {}).get('status') or WORKING, why))
                continue
            hit = look.data
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
            try:
                report = report_commit(run.get('worktree'), run.get('branch'), run.get('session'))
            except ReportUnreadable as e:
                why = f'report unreadable ({e}): left as it is'
                out(f'cloud    {job:<24} {why}')
                found.append((job, (known.get(tok) or {}).get('status') or WORKING, why))
                continue
            status, why = classify(view, report, elapsed, s.timeout_min, run_id)
        beat = heartbeat.for_run(run, cfg, product) if status == WORKING else None
        stalled = False
        if beat is not None:
            stalled, quiet, _line = heartbeat.observe(product, run, beats, now, beat, out=out,
                                                      state=hb_state)
            if stalled:
                status, why = DEAD, f'{heartbeat.STALLED}: no beat {int(quiet)}m'
                if remote.is_remote(run):  # diagnostic only: the heartbeat decided
                    diag = remote.diagnose(run, s, remote_client)
                    if diag:
                        why += '; ' + ', '.join(f'{k}={v}' for k, v in diag.items() if v)
                        cloudpid.record(tok, DEAD, why, **diag)
        if status == FINISHED and report and run.get('log'):
            _append(run['log'], {'type': 'result', 'subtype': 'success', 'is_error': False,
                                 'result': report['body'],
                                 'asf': {'cloud': {'report_commit': report['sha'],
                                                   'run': run_id}}})
        if status == DEAD:
            if stalled and remote.is_remote(run):
                remote.retire(run, remote_client)  # once: stop's own retire is then a no-op
            if why.startswith(('timed out', heartbeat.STALLED)):
                stop_fn(run)
            if run.get('log'):
                _append(run['log'], {'type': 'asf', 'subtype': 'cloud', 'status': DEAD, 'why': why})
        head = notes = None
        if stalled:  # the last beat's snapshot onto the branch, its notes kept, its ref gone
            wt = run.get('worktree')
            last = heartbeat.last_beat(wt, job)
            notes = (last or {}).get('notes') or ''
            head, _what = heartbeat.land_snapshot(wt, run.get('branch'), last)
            heartbeat.delete_ref(wt, job)
            hb_state.pop(job, None)
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
        if stalled and heartbeat.resumable(run, beat):
            if remote.is_remote(run):
                summary = remote.run_log_summary(run, s, remote_client)
                rt = relaunch_runtime or remote.RemoteRuntime(s, product, client=remote_client)
            else:
                summary = heartbeat.summarize_events(heartbeat.log_records(run.get('log')))
                rt = relaunch_runtime or actions.ActionsRuntime(s, product, gh=gh)
            new = heartbeat.launch(product, run, why, rt, cfg, notes=notes, summary=summary,
                                   head=head, out=out)
            heartbeat.seed(hb_state, new, head)
    if json.dumps(hb_state, sort_keys=True) != hb_before:
        heartbeat._save_state(product, hb_state)
    return found


def settle_ended(run):
    """An ended run's token that still says ``working`` in the status file, settled: ``finished``
    for a run ended finished, ``dead`` for any other end. :func:`sync` reads only runs with no
    ``ended`` line, so a run health ends in the same pass that first synced it (a quota-exhausted
    result, a dead pid) kept its token ``working`` for ever, and every liveness check read it
    alive (T-0196). Returns the status written, or None when there was nothing to settle."""
    tok = (run or {}).get('pid')
    if not run.get('ended') or not cloudpid.is_token(tok) or cloudpid.status(tok) != WORKING:
        return None
    from asf.workers import lifecycle
    reason = run.get('end_reason') or 'ended'
    status = FINISHED if reason == lifecycle.FINISHED else DEAD
    cloudpid.record(tok, status, f'run ended: {reason}', job=run.get('job'))
    return status


# ---- primary mode's fallback -------------------------------------------------------------------

def _state_file(product, name):
    from asf import env  # local: env imports this module
    try:
        return os.path.join(env.state_dir(product), name)
    except (OSError, AttributeError, TypeError):
        return None


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            got = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return got if isinstance(got, dict) else {}


def _write_json(path, data):
    if not path:
        return
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, sort_keys=True, indent=1)
    except OSError:
        pass


def _clock(ts):
    return time.strftime('%H:%M', time.localtime(ts))


class Breaker:
    """Primary mode's launch-error fallback, in ``<state>/cloud-breaker.json``: a cloud create
    that fails is noted; ``cloud.fallback_failures`` of them within ``cloud.fallback_window_min``
    trip a ``cloud.fallback_cooldown_min`` cool-down in which the wave sends every row to the
    local lane. A create that succeeds clears the count (they must be consecutive). The same
    signal ``asf cloud doctor`` cannot give: the lane passes every check and still fails to
    create — a helper refusing, a runtime erroring, a quota the reader does not see. A state
    dir that cannot be read or written only loses the memory."""

    FILE = 'cloud-breaker.json'

    def __init__(self, product, s, clock=None):
        self.s = s
        self.clock = clock or time.time
        self.path = _state_file(product, self.FILE)
        self.data = _read_json(self.path) if self.path else {}

    def tripped(self):
        """``''``, or why every row goes local now."""
        until = float(self.data.get('until') or 0)
        if until <= self.clock():
            return ''
        return (f'cloud launches erroring — {self.data.get("count")} failed creates in '
                f'{self.s.fallback_window_min:g} min (last: {self.data.get("why")}); local lane '
                f'until {_clock(until)}')

    def fail(self, why):
        """Note one failed create. The trip line when this one trips the breaker, else None."""
        now = self.clock()
        horizon = now - self.s.fallback_window_min * 60
        fails = [t for t in self.data.get('fails') or () if isinstance(t, (int, float))
                 and t >= horizon] + [now]
        why = ' '.join(str(why).split())[:200]
        if len(fails) >= self.s.fallback_failures:
            until = now + self.s.fallback_cooldown_min * 60
            self.data = {'fails': [], 'until': until, 'count': len(fails), 'why': why}
            _write_json(self.path, self.data)
            return self.tripped()
        self.data = dict(self.data, fails=fails, why=why)
        _write_json(self.path, self.data)
        return None

    def ok(self):
        """A create succeeded: the failures were not consecutive."""
        if self.data.get('fails'):
            self.data = dict(self.data, fails=[])
            _write_json(self.path, self.data)


FALLBACK_FILE = 'cloud-fallback.json'


def note_fallback(product, job, why, now=None):
    """Record that the local lane took ``job``, a row primary mode sends to the cloud, and why —
    the status row names the last one (:func:`last_fallback`)."""
    _write_json(_state_file(product, FALLBACK_FILE),
                {'at': time.time() if now is None else now, 'job': job, 'why': str(why)})


def last_fallback(product, now=None):
    """``{at, job, why}`` of the last fallback within :data:`FALLBACK_SHOWN_S`, or None."""
    path = _state_file(product, FALLBACK_FILE)
    got = _read_json(path) if path else {}
    now = time.time() if now is None else now
    try:
        at = float(got.get('at'))
    except (TypeError, ValueError):
        return None
    return got if now - at <= FALLBACK_SHOWN_S else None


def fallback_state(product, s, now=None):
    """One phrase: why the local lane takes primary mode's rows now, or the last time it did —
    ``''`` when neither (or the mode is not primary)."""
    if s.mode != MODE_PRIMARY or not s.on:
        return ''
    tripped = Breaker(product, s, clock=(lambda: now) if now is not None else None).tripped()
    if tripped:
        return f'fallback: {tripped}'
    last = last_fallback(product, now)
    if not last:
        return ''
    ago = max(0, int(((time.time() if now is None else now) - float(last['at'])) // 60))
    return f'last fallback {ago}m ago: {last.get("job")} local — {last.get("why")}'


# ---- placement's counts, status and doctor --------------------------------------------------

def inflight(product_name):
    """The product's live cloud runs (a working token)."""
    from asf.workers import lifecycle
    from asf.workers import pool as pool_mod
    return sum(1 for r in lifecycle.latest(pool_mod.sessions_path(product_name)).values()
               if is_cloud(r) and lifecycle.occupies(r))


#: ``asf cloud doctor``'s mode row, per mode
MODE_DETAIL = {
    MODE_PRIMARY: f'cloud.mode: primary — the cloud lane is the default executor '
                  f'(local: {", ".join(LOCAL_KINDS)}, cloud.local_only, cards marked local_only, '
                  f'and the fallback)',
    MODE_OVERFLOW: 'cloud.mode: overflow — the cloud lane is overflow only',
    MODE_OFF: 'cloud.mode: off — no new cloud launch; live cloud runs drain',
}


def checks(cfg, product, run_cmd=None):
    """``[(name, required, ok, detail)]`` — every readiness check of the lane, one per line of
    ``asf cloud doctor``: on/default, runtime, seats, accounts, then the runtime's own
    (:func:`asf.workers.actions.checks`: workflow, secret, runner). ``required`` marks a
    critical check: one failing makes the lane unready (:func:`readiness`)."""
    s = settings(cfg, product)
    rows = [('enabled', True, s.enabled and s.mode != MODE_OFF,
             'cloud.enabled: true' if s.enabled and s.mode != MODE_OFF
             else 'cloud.mode: off — the lane takes no launch' if s.enabled
             else 'cloud.enabled: false — the lane takes no launch')]
    rows.append(('default', False, s.default, MODE_DETAIL[s.mode]))
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
        return False, 'cloud lane off (cloud.enabled, cloud.mode, runtime, max_inflight)'
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
    if s.mode == MODE_OFF:
        return [(False, True, MODE_DETAIL[MODE_OFF])]
    rows = [(req, ok, d) for name, req, ok, d in checks(cfg, product, run_cmd=run_cmd)
            if not ok and name not in ('enabled', 'default')]
    if not rows:
        from asf.workers import pool as pool_mod
        accts = lane_accounts(pool_mod.accounts_from_config(cfg), s)
        rows.insert(0, (False, True, f'cloud lane on: {s.max_inflight} seat(s), runtime '
                                     f'{s.runtime} on [{", ".join(s.runs_on)}], accounts '
                                     f'{", ".join(a.name for a in accts)}, rows {s.rows}'
                                     f', mode {s.mode}'))
    return rows


def capacity_clause(cfg, product):
    """``cloud <inflight>/<max>`` for the status and capacity views, or '' when the lane is off."""
    s = settings(cfg, product)
    if not s.enabled:
        return ''
    base = f'cloud {inflight(product.name)}/{s.max_inflight}'
    if s.mode == MODE_OFF:
        return f'{base} off (no new launch; live runs drain)'
    if s.mode != MODE_PRIMARY:
        return base
    why = fallback_state(product, s)
    return f'{base} primary' + (f' ({why})' if why else '')


def lane_split(cfg, product):
    """``asf doctor``'s ``lane split`` row: the effective split of this product's work between
    the hosts — the mode, the local session ceiling and where it comes from (written, or
    ``capacity.sessions: auto`` sized off the host), the cloud lane's seats, what never leaves
    the host, and the fallback now."""
    from asf import capacity as capacity_mod  # local: capacity reads the pool, which reads this
    s = settings(cfg, product)
    try:
        local, source = capacity_mod.product_sessions(product, cfg)
        local_txt = f'local sessions {local} ({source})'
    except Exception as e:  # noqa: BLE001 — an unreadable ceiling is one phrase, not a crash
        local_txt = f'local sessions unreadable ({type(e).__name__}: {e})'
    lane = ('cloud lane off' if not s.enabled else 'no new cloud launch' if s.mode == MODE_OFF
            else f'cloud max_inflight {s.max_inflight}' if s.on
            else 'cloud lane not ready to launch (asf cloud doctor)')
    keep = ', '.join(LOCAL_KINDS + tuple(k for k in s.local_only if k not in LOCAL_KINDS))
    why = {MODE_PRIMARY: 'cloud first; local takes local-only rows and the fallback',
           MODE_OVERFLOW: 'local first; the cloud takes what local cannot',
           MODE_OFF: 'local only'}[s.mode]
    state = fallback_state(product, s) or ('no fallback in the last hour'
                                           if s.mode == MODE_PRIMARY and s.on else '')
    return (f'mode {s.mode} ({why}) — {local_txt}, {lane}, local only: {keep} and cards marked '
            f'local_only' + (f'; {state}' if state else ''))
