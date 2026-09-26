"""asf.workers.remote — the cloud lane's ``claude-remote`` runtime: a worker session as a
claude.ai routine run.

The runtime CLI cannot open a cloud session non-interactively (``claude-cloud`` stays refused,
:data:`asf.workers.cloud.REFUSED`), but its built-in ``RemoteTrigger`` tool calls the claude.ai
routine API (``/v1/code/triggers``) with the logged-in account's own OAuth login, in-process —
the token is never on a command line, in a file ASF writes, or in a log. That tool is present in
``-p`` mode (verified on 2.1.283). The ``auth_env`` runtime token a local session runs on
(``CLAUDE_CODE_OAUTH_TOKEN``, a setup-token) is inference-only — the API answers 401
``oauth_scope_insufficient`` — so the helper runs on the account's config-dir login instead
(:func:`helper_env`: the account's ``CLAUDE_CONFIG_DIR`` under the operator's HOME, whose
keychain holds that login; the runtime token variables dropped).

**The helper** (:class:`TriggerClient`): one small ``claude -p`` per API call (``cloud.helper_model``,
default ``haiku``; ``--allowedTools RemoteTrigger``) told to relay one JSON argument object to
``RemoteTrigger`` unchanged. ASF never trusts its prose: it reads the tool call the model made and
the tool's raw result off the ``stream-json`` log (a result too large to inline is read from the
file the CLI saved it to), and a create whose body the model altered is disabled and retried
once with a fresh helper, then refused.

**A launch** (:class:`RemoteRuntime`): the brief is :func:`asf.workers.cloud.cloud_brief` with the
routine's setting (check the branch out, export the worker environment, run the setup). It goes
to origin's brief ref first (:func:`asf.workers.actions.push_brief`, ``refs/asf/briefs/<job>``,
deleted when the run ends) — never into the routine body: the helper is a model, and a model
does not copy a multi-KB brief byte-exact. ``create`` makes a routine on the dispatching
account — ``run_once_at`` far out, so nothing fires by schedule — whose one event is a short,
fixed, ASCII pointer to that ref (:func:`pointer`), whose source is the product repo, in the account's
environment (``cloud.environment_id``: one id, or a map account → id — an environment belongs to
one account, and another account's is refused ``environment_not_found``) with ``cloud.model`` (else the row's model) and ``cloud.allowed_tools``;
``run`` fires it. Each helper call is killed at :data:`HELPER_TIMEOUT_S` — the launch fails
``timeout``, and a routine the killed create had made is disabled — and a wave tries at most
``cloud.max_creates_per_tick`` launches, so the lane never holds a tick for long. The run
records ``pid: remote:<trigger id>``, the run's session id and link, and its ``brief_ref``.
The routine runs on the dispatching account, so it counts in that account's in-flight like any
session.

**Tracking** (:func:`evidence`, called by :func:`asf.workers.cloud.sync`): the report commit on
the branch finishes the run, as for ``actions``; the routine's ``last_run`` (a ``get``, at most
every ``cloud.poll_min`` minutes, cached in the cloud status file) ends it dead when the run
finished without the report commit; ``cloud.timeout_min`` ends it dead too. A run that is no
longer working has its routine disabled (:func:`retire`, once).

Config (``cloud:``)::

    cloud:
      enabled: true
      runtime: claude-remote
      environment_id: env_…        # the routine's claude.ai environment, or {account: env_…}
      model: claude-opus-5         # optional: default the row's model
      allowed_tools: [Bash, Read, Write, Edit, Glob, Grep]
      helper_model: haiku          # the local relay's model
      poll_min: 15                 # how often a working run's routine is read
      max_creates_per_tick: 2      # cloud launches one wave tries (0: no limit)
      accounts: [acct-a]
      max_inflight: 4
      rows: any
"""
import json
import os
import re
import subprocess
import time

from asf import env
from asf import hermetic
from asf.workers import actions
from asf.workers import cloud
from asf.workers import cloudpid
from asf.workers import runtime as runtime_mod

RUNTIME = 'claude-remote'
PREFIX = cloudpid.REMOTE_PREFIX
DEFAULT_ALLOWED_TOOLS = ('Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep')
DEFAULT_HELPER_MODEL = 'haiku'
DEFAULT_POLL_MIN = 15
#: a routine ASF fires by ``run``: its schedule never comes
FAR_FUTURE = '2036-01-01T00:00:00Z'
#: one helper call's hard limit: a create relays a short body in seconds; a helper past this
#: is killed and the launch fails clean (``timeout``) instead of holding the tick
HELPER_TIMEOUT_S = 120
#: the variables that carry a runtime token: a helper that saw one would call the API with it
#: (inference-only: 401) instead of the account's own login
TOKEN_VARS = ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')
ID_RE = re.compile(r'^[\w-]+$')
SAVED_RE = re.compile(r'Full output saved to:\s*(\S+)')
URL_RE = re.compile(r'https://claude\.ai/\S+')
#: ``last_run.status`` values of a run that is still going
RUNNING_STATES = ('', 'ROUTINE_RUN_STATUS_UNSPECIFIED', 'ROUTINE_RUN_STATUS_RUNNING',
                  'ROUTINE_RUN_STATUS_PENDING', 'ROUTINE_RUN_STATUS_QUEUED',
                  'ROUTINE_RUN_STATUS_STARTING')


class HelperError(Exception):
    pass


class HelperTimeout(HelperError):
    """A helper killed at :data:`HELPER_TIMEOUT_S`; ``partial`` is the log it wrote so far."""

    def __init__(self, msg, partial=''):
        super().__init__(msg)
        self.partial = partial


def token(trigger_id):
    return f'{PREFIX}{trigger_id}'


def is_remote(run):
    pid = (run or {}).get('pid')
    return isinstance(pid, str) and pid.startswith(PREFIX)


def repo_url(product):
    slug = getattr(product, 'repo_slug', None)
    return f'https://github.com/{slug}' if slug else None


# ---- the routine body -------------------------------------------------------------------------

_SAFE_RE = re.compile(r'[^\w@%+=:,./-]')


def _safe(v):
    """A name for the pointer: ASCII, no quoting — anything else is dropped."""
    return _SAFE_RE.sub('', str(v or '').encode('ascii', 'ignore').decode())


def pointer(job, ref, url):
    """The routine's one message: a short fixed ASCII pointer to the brief on ``ref`` — the
    helper relays it byte-exact where a multi-KB brief comes back altered."""
    ref, name = _safe(ref), _safe(job.name)
    return (f'You are ASF worker session {_safe(job.session) or name} for job {name}. '
            f'Repository {_safe(url)}, branch {_safe(job.branch)}. Your task is the brief on the '
            f'git ref {ref}: run `git fetch origin {ref} && git show FETCH_HEAD:brief.md` and '
            'follow that brief exactly; it is your whole task. Do nothing before you have read it.')


def trigger_body(name, prompt, environment_id, model, allowed_tools, url):
    """The ``create`` body: a run-once routine ASF fires itself (``run_once_at`` far out), one
    user event carrying ``prompt``, the repo ``url`` as its source. Pure (golden-tested)."""
    ctx = {'allowed_tools': list(allowed_tools or DEFAULT_ALLOWED_TOOLS),
           'sources': [{'git_repository': {'url': url}}]}
    if model:
        ctx['model'] = model
    return {'name': name, 'enabled': True, 'persist_session': False,
            'run_once_at': FAR_FUTURE,
            'job_config': {'ccr': {
                'environment_id': environment_id,
                'events': [{'type': 'user',
                            'data': {'message': {'role': 'user', 'content': prompt}}}],
                'session_context': ctx}}}


def setting_lines(job):
    """The CLOUD block's opening for a routine run: where it runs, and what it does first."""
    exports = ' '.join(f'{k}={_sh(v)}' for k, v in sorted((job.env or {}).items())
                       if re.match(r'^[A-Z_][A-Z0-9_]*$', str(k)) and '\n' not in str(v))
    out = ['You run in a claude.ai cloud session, not on the factory host: build and test '
           'happen here, in this session.',
           f'- First check the branch out: `git fetch origin {job.branch} && git checkout -B '
           f'{job.branch} origin/{job.branch}` (base `{job.base}`). Commit and push on '
           f'`{job.branch}` only; never push `{job.base}`, never force-push.']
    if exports:
        out.append(f'- Every shell command runs with the worker environment: `export {exports}`.')
    setup = getattr(job, 'setup', None)
    if setup:
        out.append(f'- Then run the product setup once: `{setup}`.')
    return out


def _sh(v):
    v = str(v)
    return v if re.match(r'^[\w@%+=:,./-]*$', v) else "'" + v.replace("'", "'\\''") + "'"


# ---- the helper -------------------------------------------------------------------------------

def helper_env(account, base=None):
    """The helper's environment: the worker allow-list, the account's ``CLAUDE_CONFIG_DIR`` as a
    local session gets it — and no runtime token variable, so the CLI calls the API with the
    config dir's own login. HOME stays the operator's: that login lives in the operator's
    keychain, which a session's isolated HOME does not reach ("Not logged in")."""
    out = hermetic.build(base, pythonpath=False, mode='worker')
    if account is not None and getattr(account, 'config_dir', None):
        out['CLAUDE_CONFIG_DIR'] = os.path.expanduser(account.config_dir)
    for var in TOKEN_VARS:
        out.pop(var, None)
    return out


def helper_prompt(args):
    return ('You are a relay. Make exactly one tool call, then stop. Call the RemoteTrigger tool '
            '(load it with ToolSearch "select:RemoteTrigger" first if it is deferred) with '
            'exactly the arguments below — the JSON object between the markers, every field '
            'and every character unchanged, nothing added. Do not call it twice, do not call '
            'any other tool. Reply with only the first line of its result.\n'
            '<<<ARGS\n' + json.dumps(args, ensure_ascii=False) + '\nARGS>>>\n')


def tool_calls(stdout, tool='RemoteTrigger'):
    """``[(input, result text)]`` of each ``tool`` call in a ``stream-json`` log, in order."""
    uses, results = [], {}
    for line in (stdout or '').splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        msg = rec.get('message') if isinstance(rec, dict) else None
        content = msg.get('content') if isinstance(msg, dict) else None
        for c in content if isinstance(content, list) else ():
            if not isinstance(c, dict):
                continue
            if c.get('type') == 'tool_use' and c.get('name') == tool:
                uses.append((c.get('id'), c.get('input') or {}))
            elif c.get('type') == 'tool_result':
                body = c.get('content')
                if isinstance(body, list):
                    body = ''.join(p.get('text', '') for p in body if isinstance(p, dict))
                results[c.get('tool_use_id')] = str(body or '')
    return [(inp, results.get(uid)) for uid, inp in uses]


def read_saved(text):
    """A result the CLI saved to a file (``<persisted-output>``): the file's content."""
    m = SAVED_RE.search(text or '') if '<persisted-output>' in (text or '') else None
    if not m:
        return text
    try:
        with open(m.group(1), encoding='utf-8') as f:
            return f.read()
    except OSError:
        return text


def http_json(text):
    """``(status, object, tail)`` of a ``RemoteTrigger`` result: ``HTTP <n>``, the JSON, and the
    summary the tool appends (the routine's link)."""
    text = read_saved(text or '')
    first, _, rest = text.lstrip().partition('\n')
    m = re.match(r'HTTP (\d+)', first.strip())
    code = int(m.group(1)) if m else None
    rest = rest.lstrip()
    try:
        obj, end = json.JSONDecoder().raw_decode(rest)
    except ValueError:
        return code, None, rest
    return code, obj, rest[end:].strip()


class TriggerClient:
    """The routine API through one ``claude -p`` per call, on ``account``'s login. ``run`` is
    injectable (tests: a fake that answers with a ``stream-json`` log)."""

    def __init__(self, account, binary=None, model=DEFAULT_HELPER_MODEL, run=None, cwd=None,
                 timeout=HELPER_TIMEOUT_S):
        self.account = account
        self.timeout = timeout
        self.binary = binary or _binary()
        self.model = model or DEFAULT_HELPER_MODEL
        self._run = run or subprocess.run
        self.cwd = cwd

    def command(self):
        return [self.binary, '-p', '--model', self.model, '--allowedTools', 'RemoteTrigger',
                '--output-format', 'stream-json', '--verbose']

    def call(self, action, trigger_id=None, body=None, session_id=None):
        """``(status, object, tail, sent)`` of one API call; ``sent`` is the argument object the
        helper passed. Raises :class:`HelperError` when no call was made."""
        args = {'action': action}
        if trigger_id:
            if not ID_RE.match(str(trigger_id)):
                raise HelperError(f'not a trigger id: {trigger_id!r}')
            args['trigger_id'] = str(trigger_id)
        if session_id:
            if not ID_RE.match(str(session_id)):
                raise HelperError(f'not a session id: {session_id!r}')
            args['session_id'] = str(session_id)
        if body is not None:
            args['body'] = body
        cwd = self.cwd or _helper_dir()
        try:
            p = self._run(self.command(), input=helper_prompt(args), capture_output=True,
                          text=True, env=helper_env(self.account), cwd=cwd,
                          timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            partial = e.stdout or e.output or ''
            if isinstance(partial, bytes):
                partial = partial.decode('utf-8', 'replace')
            raise HelperTimeout(f'timeout: helper {action} killed after {self.timeout:g}s',
                                partial) from None
        except (OSError, subprocess.SubprocessError) as e:
            raise HelperError(f'helper {action}: {type(e).__name__}') from None
        calls = [(inp, res) for inp, res in tool_calls(p.stdout)
                 if inp.get('action') == action]
        if not calls or calls[0][1] is None:
            tail = (p.stderr or '').strip().splitlines()
            raise HelperError(f'helper {action}: no RemoteTrigger call made (exit {p.returncode}'
                              f'{"; " + tail[-1][:200] if tail else ""})')
        sent, result = calls[0]
        code, obj, tail = http_json(result)
        return code, obj, tail, sent

    def create(self, body, attempts=2):
        """``(trigger id, routine link)``. A body the helper altered is disabled and the create
        retried with a fresh helper, ``attempts`` in all; then refused."""
        for _ in range(max(1, attempts)):
            try:
                code, obj, tail, sent = self.call('create', body=body)
            except HelperTimeout as e:
                self._reap(e)
                raise
            tid = trigger_id_of(obj)
            if code not in (200, 201) or not tid:
                raise HelperError(f'create refused: HTTP {code} {_err(obj, tail)}')
            if sent.get('body') == body:
                return tid, _link(tail)
            self.disable(tid)
        raise HelperError(f'create: the helper altered the body; routine {tid} disabled')

    def _reap(self, e):
        """A create killed at the timeout may have made its routine: disable it."""
        for inp, res in tool_calls(e.partial):
            tid = trigger_id_of(http_json(res)[1]) if inp.get('action') == 'create' else None
            if tid:
                try:
                    self.disable(tid)
                    e.args = (f'{e.args[0]}; routine {tid} disabled',)
                except HelperError:
                    e.args = (f'{e.args[0]}; routine {tid} left enabled (disable failed)',)

    def fire(self, trigger_id):
        """The run's session id (None when the answer names none)."""
        code, obj, tail, _sent = self.call('run', trigger_id=trigger_id)
        if code not in (200, 201, 202):
            raise HelperError(f'run refused: HTTP {code} {_err(obj, tail)}')
        return session_of(obj)

    def get(self, trigger_id):
        """The routine (the answer is ``{trigger: {…, last_run}}``), or None."""
        code, obj, _tail, _sent = self.call('get', trigger_id=trigger_id)
        if code != 200 or not isinstance(obj, dict):
            return None
        return obj['trigger'] if isinstance(obj.get('trigger'), dict) else obj

    def disable(self, trigger_id):
        code, _obj, _tail, _sent = self.call('update', trigger_id=trigger_id,
                                             body={'enabled': False})
        return code == 200


def trigger_id_of(obj):
    """The routine id in a ``create`` answer (``{outcome, trigger: {id, …}}``)."""
    if not isinstance(obj, dict):
        return None
    t = obj.get('trigger') if isinstance(obj.get('trigger'), dict) else obj
    tid = t.get('id')
    return tid if isinstance(tid, str) and tid.startswith('trig_') else None


def environment_for(s, account_name):
    """The environment a routine of ``account_name`` runs in: ``cloud.environment_id``'s entry
    for it, else the one for every account. Environments belong to an account: another
    account's is refused (``environment_not_found``)."""
    return dict(s.environments).get(account_name) or s.environment_id or None


def session_of(obj):
    """The run session id in an API answer: a ``session_id`` field anywhere, or a ``cse_…``."""
    if isinstance(obj, dict):
        for k in ('session_id', 'id'):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith(('cse_', 'session_')):
                return v
        for v in obj.values():
            hit = session_of(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = session_of(v)
            if hit:
                return hit
    return None


def session_url(session_id):
    """A run's claude.ai link: ``cse_X`` is served at ``/code/session_X``."""
    if not session_id:
        return None
    return f"https://claude.ai/code/session_{str(session_id).split('_', 1)[-1]}"


def _link(tail):
    m = URL_RE.search(tail or '')
    return m.group(0).rstrip('.,)') if m else None


def _err(obj, tail):
    if isinstance(obj, dict) and isinstance(obj.get('error'), dict):
        return str(obj['error'].get('message') or obj['error'])[:200]
    return (tail or '')[:200]


def _safe_name(text):
    return ' '.join(_safe(w) for w in str(text).split() if _safe(w))


def _binary():
    wp = (cloud._load_cfg() or {}).get('worker_pool') or {}
    return wp.get('binary') or runtime_mod.DEFAULT_BINARY


def _helper_dir():
    d = os.path.join(env.ASF_HOME, 'state', 'remote')
    os.makedirs(d, exist_ok=True)
    return d


# ---- the runtime ------------------------------------------------------------------------------

class RemoteRuntime(runtime_mod.Runtime):
    """``claude-remote``: create a routine on the job's account and fire it. ``run`` returns a
    :class:`~asf.workers.runtime.Result` whose ``pid`` is ``remote:<trigger id>``."""
    name = RUNTIME
    lane = 'cloud'

    def __init__(self, s, product, client=None):
        self.settings = s
        self.product = product
        self._client = client

    def client(self, account):
        if self._client is not None:
            return self._client(account) if callable(self._client) else self._client
        return TriggerClient(account, model=self.settings.helper_model)

    def run(self, job, wait=False):
        from asf.workers.spawn import SpawnError  # local: spawn imports the runtimes
        s, product = self.settings, self.product
        url = repo_url(product)
        if not url:
            raise SpawnError('cloud lane: the product has no repo_slug for the routine source')
        acct_name = getattr(job.account, 'name', None)
        env_id = environment_for(s, acct_name)
        if not env_id:
            raise SpawnError(f'cloud lane: cloud.environment_id names no environment for account '
                             f'{acct_name} (claude-remote)')
        log_path = job.log_path or runtime_mod.job_log_path(job.product, job.name)
        with open(job.brief_path, encoding='utf-8') as f:
            text = cloud.cloud_brief(f.read(), job, setting=setting_lines(job))
        ok, ref = actions.push_brief(job.cwd, job.name, text, job.env, getattr(job, 'setup', None))
        if not ok:
            raise SpawnError(f'cloud lane: {ref}')
        name = _safe_name(f'asf {product.name} {job.name} {job.session or ""}')
        body = trigger_body(name, pointer(job, ref, url), env_id, s.model or job.model,
                            s.allowed_tools, url)
        client = self.client(job.account)
        try:
            tid, link = client.create(body)
        except HelperError as e:
            actions.delete_brief(job.cwd, ref)
            raise SpawnError(f'cloud lane: {e}') from None
        try:
            sid = client.fire(tid)
        except HelperError as e:
            try:
                client.disable(tid)
            except HelperError:
                pass
            actions.delete_brief(job.cwd, ref)
            raise SpawnError(f'cloud lane: routine {tid}: {e}') from None
        tok = token(tid)
        where = session_url(sid) or link
        line = runtime_mod._session_line(job)
        with open(log_path, 'a', encoding='utf-8') as log:
            if line is not None:
                log.write(line + '\n')
            log.write(json.dumps({'type': 'asf', 'subtype': 'cloud', 'runtime': self.name,
                                  'trigger': tid, 'run': sid, 'url': where,
                                  'routine': link, 'brief_ref': ref}) + '\n')
        cloudpid.record(tok, cloud.WORKING, 'dispatched', run=sid, url=where, routine=link)
        result = runtime_mod.Result(pid=tok, log_path=log_path)
        # 'runtime_lane', never 'lane': see asf.workers.actions.ActionsRuntime.run for why.
        result.extra = {'runtime_lane': 'cloud', 'cloud_runtime': self.name, 'remote_trigger_id': tid,
                        'remote_session_id': sid, 'cloud_url': where, 'remote_routine_url': link,
                        'brief_ref': ref}
        return result

    def continue_run(self, job, wait=False):
        return None  # a routine run is not resumed from here: the caller falls back


# ---- tracking ---------------------------------------------------------------------------------

def trigger_of(run):
    return run.get('remote_trigger_id') or str(run.get('pid') or '')[len(PREFIX):] or None


def last_run(run, s, now, client=None, known=None):
    """The routine's ``last_run`` — read at most every ``cloud.poll_min`` minutes (the answer is
    cached on the run's token in the cloud status file), None when unread."""
    tok = run.get('pid')
    rec = (known if known is not None else cloudpid.load()).get(tok) or {}
    polled = rec.get('polled')
    if polled is not None and now - float(polled) < max(0.0, s.poll_min) * 60:
        return rec.get('last_run')
    tid = trigger_of(run)
    if not tid:
        return rec.get('last_run')
    try:
        obj = (client or TriggerClient(_account(run), model=s.helper_model)).get(tid)
    except HelperError:
        obj = None
    lr = (obj or {}).get('last_run') if isinstance(obj, dict) else None
    lr = lr if isinstance(lr, dict) else rec.get('last_run')
    cloudpid.record(tok, rec.get('status') or cloud.WORKING, rec.get('why') or '',
                    polled=now, last_run=lr)
    return lr


def view_of(lr):
    """A routine's ``last_run`` as the ``{status, conclusion}`` :func:`asf.workers.cloud.classify`
    reads."""
    if not isinstance(lr, dict):
        return None
    state = str(lr.get('status') or '')
    if lr.get('finished_at') or state not in RUNNING_STATES:
        return {'status': 'completed',
                'conclusion': state.replace('ROUTINE_RUN_STATUS_', '').lower() or 'ended'}
    return {'status': 'in_progress', 'conclusion': ''}


def evidence(run, s, elapsed, now, client=None):
    """``(status, why, report)`` of a routine run — see the module doc."""
    tid = trigger_of(run)
    view = view_of(last_run(run, s, now, client))  # before the report: no race with its end
    report = cloud.report_commit(run.get('worktree'), run.get('branch'), run.get('session'))
    status, why = cloud.classify(view, report, elapsed, s.timeout_min, tid)
    if status != cloud.WORKING:
        retire(run, client)
    return status, why, report


def retire(run, client=None):
    """Disable the run's routine, once (``retired`` on its token). True when disabled now."""
    tok = run.get('pid')
    rec = cloudpid.load().get(tok) or {}
    tid = trigger_of(run)
    if rec.get('retired') or not tid:
        return False
    try:
        ok = (client or TriggerClient(_account(run))).disable(tid)
    except HelperError:
        ok = False
    if ok:
        cloudpid.record(tok, rec.get('status') or cloud.WORKING, rec.get('why') or '',
                        retired=True)
    return ok


def _account(run):
    from asf.workers import pool as pool_mod
    name = run.get('account')
    for a in pool_mod.accounts_from_config(cloud._load_cfg()):
        if a.name == name:
            return a
    return None


def doctor_rows(s, product):
    """``[(required, ok, detail)]``: the product repo and the environment."""
    rows = []
    if not repo_url(product):
        rows.append((True, False, 'the product has no repo_slug: the routine clones the '
                                  'product repo'))
    if not s.environment_id and not s.environments:
        rows.append((True, False, 'cloud.environment_id is not set: the claude.ai cloud '
                                  'environment the routine runs in'))
    return rows
