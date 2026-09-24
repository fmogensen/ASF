"""asf.workers.runtime — how one job's session actually runs.

``Runtime.run(job) -> Result`` with two backends:

* ``claude_code`` — the headless coding-agent CLI: ``<binary> -p --permission-mode <m>
  --model <model> --add-dir <d>... --output-format stream-json --verbose``, the brief on stdin,
  cwd = the job's worktree, the account's own home/config dir in the environment, stdout
  streamed to ``~/.ASF/logs/jobs/<product>/<job>.jsonl``. The last line of that log is the
  session's result (``{"type": "result", ...}``). :func:`build_command` / :func:`build_env` are
  pure functions so the command line has a golden test.
* ``fake`` — replays scripted results (a list, or a JSON fixture file) and writes the same log
  shape, so every test (and a dry run) exercises the real bookkeeping with no agent, network or
  account.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

from asf import detach, env, hermetic
from asf.workers import report

DEFAULT_BINARY = 'claude'
DEFAULT_PERMISSION_MODE = 'bypassPermissions'


class Job:
    """Everything one run needs. ``account`` is a :class:`asf.workers.pool.Account` (or None)."""

    def __init__(self, product, name, cwd, brief_path, model, account=None, add_dirs=(),
                 permission_mode=DEFAULT_PERMISSION_MODE, env=None, log_path=None,
                 settings_file=None, hooks_dir=None, resume=None, passthrough=(),
                 product_auth_env=None):
        self.product = product
        self.name = name
        self.cwd = cwd
        self.brief_path = brief_path
        self.model = model
        self.account = account
        self.add_dirs = list(add_dirs or [])
        self.permission_mode = permission_mode
        self.env = dict(env or {})
        self.log_path = log_path
        # the product's own auth_env (products/<name>.yaml conventions.auth_env, expanded
        # {VARIABLE: file}): merged over the account's at build_env time, the product's value
        # winning — GitHub access is per product, not per account (asf.env.product_auth_env)
        self.product_auth_env = dict(product_auth_env or {})
        # the worker's permission rules (allow list + the deny rules, e.g. never push to the main
        # branch); ``worker_pool.settings_file`` in config — a session without it runs unfenced
        self.settings_file = settings_file
        # the per-product git hook dir (asf.workers.githooks.ensure) — set as core.hooksPath so
        # every commit the session makes carries its ASF-Session trailer (F-0076)
        self.hooks_dir = hooks_dir
        # the runtime's own session id to continue (``runtime_session``), or None for a fresh run
        self.resume = resume
        # ``worker_pool.env_passthrough``: the names the session keeps from the tick's
        # environment beyond the allow-list (asf.hermetic.WORKER_ALLOW)
        self.passthrough = tuple(passthrough or ())

    @property
    def session(self):
        return self.env.get('ASF_SESSION')


class Result:
    """``ok`` is True/False once the session finished, None while it still runs (detached)."""

    def __init__(self, ok=None, pid=None, returncode=None, text='', log_path=None, reason=None):
        self.ok = ok
        self.reason = reason
        self.pid = pid
        self.returncode = returncode
        self.text = text
        self.log_path = log_path

    def __repr__(self):
        return f'Result(ok={self.ok!r}, pid={self.pid!r}, returncode={self.returncode!r})'


def job_log_path(product_name, job_name):
    from asf import env
    d = os.path.join(env.log_dir(), 'jobs', product_name)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'{job_name}.jsonl')


def build_command(job, binary=DEFAULT_BINARY):
    """The headless command line — a pure function of the job (golden-tested)."""
    cmd = [binary, '-p', '--permission-mode', job.permission_mode]
    if job.resume:
        cmd += ['--resume', job.resume]
    for d in job.add_dirs:
        cmd += ['--add-dir', d]
    if job.model:
        cmd += ['--model', job.model]
    if job.settings_file:
        cmd += ['--settings', job.settings_file]
    cmd += ['--output-format', 'stream-json', '--verbose']
    return cmd


# ---- the session's HOME --------------------------------------------------------------------

def homes_dir():
    """``<ASF_HOME>/state/homes``: one directory per worker account (never a product's name)."""
    from asf import env
    return os.path.join(env.ASF_HOME, 'state', 'homes')


def session_home(acct):
    """The HOME a session of ``acct`` runs under: its ``home:`` path when it names one; else,
    under ``isolate_home`` (the default), its own directory under :func:`homes_dir`; else None —
    ``isolate_home: false`` with no ``home:``, the operator's own HOME. An account-less job
    (a dry run, a test) is None too."""
    if acct is None:
        return None
    home = getattr(acct, 'home', None)
    if home:
        return os.path.expanduser(str(home))
    if getattr(acct, 'isolate_home', True):
        return os.path.join(homes_dir(), acct.name)
    return None


def _seed_target(home, src, operator_home):
    """Where ``src`` lands in ``home``: at its path relative to the operator's home when it sits
    under it (``~/.config/gh`` → ``<home>/.config/gh``), else under its own name."""
    src = os.path.abspath(src)
    for base in dict.fromkeys((operator_home, os.path.realpath(operator_home))):
        base = base.rstrip(os.sep)
        if src.startswith(base + os.sep):
            return os.path.join(home, os.path.relpath(src, base))
    return os.path.join(home, os.path.basename(src.rstrip(os.sep)))


def _copy_file(src, dst):
    """``src`` (symlinks followed) onto ``dst`` atomically: a session already running under the
    home never reads a half-written file."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.seed-', dir=os.path.dirname(dst))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


#: The first line of the minimal ``.gitconfig`` :func:`seed_home` writes into an isolated home
#: that got none from ``home_seed``: the file is ASF's, rewritten at every launch.
GITCONFIG_MARK = '# written by asf: the session identity only (user.name/user.email)'


def operator_git_identity(operator_home=None):
    """``(user.name, user.email)`` from the operator's global git config (either may be '')."""
    environ = dict(os.environ)
    if operator_home:
        environ['HOME'] = operator_home
    out = []
    for key in ('user.name', 'user.email'):
        try:
            p = subprocess.run(['git', 'config', '--global', '--get', key], capture_output=True,
                               text=True, env=environ, timeout=10)
            out.append(p.stdout.strip() if p.returncode == 0 else '')
        except (OSError, subprocess.SubprocessError):
            out.append('')
    return tuple(out)


def write_identity_gitconfig(home, operator_home=None):
    """Give an isolated ``home`` a ``.gitconfig`` holding only the operator's ``user.name`` and
    ``user.email`` — nothing else from the operator's config (no credential helper, no
    include). A ``.gitconfig`` that ``home_seed`` put there is left alone; ASF's own is
    rewritten. Returns the path written, or None."""
    path = os.path.join(home, '.gitconfig')
    if os.path.exists(path):
        with open(path, encoding='utf-8', errors='replace') as f:
            if f.readline().rstrip('\n') != GITCONFIG_MARK:
                return None
    name, email = operator_git_identity(operator_home)
    lines = [GITCONFIG_MARK, '[user]']
    lines += [f'\tname = {name}'] if name else []
    lines += [f'\temail = {email}'] if email else []
    fd, tmp = tempfile.mkstemp(prefix='.gitconfig-', dir=home)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp, path)
    return path


def seed_home(acct, operator_home=None):
    """Create the account's :func:`session_home` and copy each ``home_seed`` path into it —
    re-copied at every launch, so a refreshed login or an edited config file reaches the next
    session. Nothing but the listed paths ever enters the home. Returns ``(home, missing)``:
    the home (None when the session keeps the operator's) and the seed paths that do not exist."""
    home = session_home(acct)
    if home is None:
        return None, []
    os.makedirs(home, mode=0o700, exist_ok=True)
    operator_home = operator_home or os.path.expanduser('~')
    missing = []
    for src in getattr(acct, 'home_seed', None) or ():
        src = os.path.expanduser(src)
        if not os.path.exists(src):
            missing.append(src)
            continue
        dst = _seed_target(home, src, operator_home)
        if os.path.realpath(dst) == os.path.realpath(src):
            continue
        if os.path.isdir(src):
            for root, _dirs, files in os.walk(src, followlinks=True):
                for name in files:
                    path = os.path.join(root, name)
                    if os.path.exists(path):
                        _copy_file(path, os.path.join(dst, os.path.relpath(path, src)))
        else:
            _copy_file(src, dst)
    if os.path.realpath(home) != os.path.realpath(operator_home):
        write_identity_gitconfig(home, operator_home)
        link_factory_cli(home, operator_home)
    return home, missing


#: Where the installer puts the ``asf`` entry point, relative to a HOME — the path a product's own
#: git hooks and scripts name as ``$HOME/.local/bin/asf``.
CLI_REL = os.path.join('.local', 'bin', 'asf')


def link_factory_cli(home, operator_home):
    """``<home>/.local/bin/asf`` → the operator's installed ``asf``, so a session under an isolated
    HOME reaches the factory's CLI where hooks and scripts expect it (a product's pre-push that runs
    ``$HOME/.local/bin/asf`` refused every push without it). Nothing when there is no installed
    CLI to point at; a stale link is replaced."""
    target = os.path.join(operator_home, CLI_REL)
    if not os.path.exists(target):
        found = shutil.which('asf')
        if not found:
            return None
        target = found
    link = os.path.join(home, CLI_REL)
    if os.path.realpath(link) == os.path.realpath(target) and os.path.lexists(link):
        return link
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.path.realpath(target), link)
    return link


# ---- the account's credentials (auth_env) -----------------------------------------------------

#: The variables the runtime authenticates from, first the one ``auth_env`` should set: a session
#: in an isolated HOME finds no login (on macOS the runtime's OAuth credential sits in the login
#: keychain, found through the operator's HOME), so it must be handed one explicitly.
RUNTIME_AUTH_VARS = ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY')

#: The runtime's one-time command that prints a long-lived token for ``CLAUDE_CODE_OAUTH_TOKEN``.
RUNTIME_TOKEN_COMMAND = 'claude setup-token'

#: The code host's token variable: when an account's ``auth_env`` sets it, git in the session
#: pushes over HTTPS with it (:func:`git_credential_config`), and ``gh`` reads it as is.
GIT_TOKEN_VAR = 'GH_TOKEN'
GIT_TOKEN_HOST = 'https://github.com'

#: The credential helper git runs for :data:`GIT_TOKEN_HOST`: it echoes the token from the
#: session's own environment — the token is never written to a file, a config or a remote URL.
GIT_CREDENTIAL_HELPER = ('!f() { test "$1" = get || return 0; echo username=x-access-token; '
                         'echo "password=$' + GIT_TOKEN_VAR + '"; }; f')


class AuthEnvError(Exception):
    """An account's ``auth_env`` file cannot be read: the launch is refused (NEEDS OPERATOR)."""

    def __init__(self, msg, clear=''):
        super().__init__(msg)
        self.clear = clear


def auth_env_howto(var, path):
    """The one-time step that creates ``path`` for ``var`` — never a value."""
    if var == 'CLAUDE_CODE_OAUTH_TOKEN':
        return (f'run `{RUNTIME_TOKEN_COMMAND}`, authorize as the account, then save the token '
                f'it prints: pbpaste > {path} && chmod 600 {path}')
    if var == GIT_TOKEN_VAR:
        return ('create a fine-grained token (the product repo only; Contents and Pull requests '
                f'read/write) and save it: pbpaste > {path} && chmod 600 {path}')
    return f'write the value of {var} into {path} and chmod 600 {path}'


def auth_env_values(acct, product_auth_env=None):
    """``{VARIABLE: value}``: an account's ``auth_env`` merged with ``product_auth_env`` (a
    product's ``conventions.auth_env``, :func:`asf.env.product_auth_env`) — the product's file
    wins for a variable both name, since GitHub access is per product (different products can
    live under different GitHub owners) while the runtime login stays per account. Each file's
    content is read stripped. A missing, unreadable or empty file raises :class:`AuthEnvError`
    naming the file and how to create it — never a value. ``{}`` when neither names any."""
    out = {}
    name = getattr(acct, 'name', '?')
    product_auth_env = product_auth_env or {}
    merged = dict(getattr(acct, 'auth_env', None) or {})
    merged.update(product_auth_env)
    for var, path in merged.items():
        path = os.path.expanduser(path)
        why = None
        try:
            with open(path, encoding='utf-8') as f:
                value = f.read().strip()
            if not value:
                why = 'is empty'
        except FileNotFoundError:
            why = 'does not exist'
        except (OSError, UnicodeDecodeError) as e:
            why = f'cannot be read ({type(e).__name__})'
        if why:
            howto = auth_env_howto(var, path)
            source = "product's conventions.auth_env" if var in product_auth_env else f'worker account {name}'
            raise AuthEnvError(f'NEEDS OPERATOR: {source}: auth_env {var} file {path} '
                               f'{why} — {howto}', clear=howto)
        out[var] = value
    return out


def git_credential_config(auth):
    """The ``(key, value)`` git config pairs a session gets when ``auth`` carries
    :data:`GIT_TOKEN_VAR`: the helper list for :data:`GIT_TOKEN_HOST` reset (no keychain, no
    helper from any gitconfig) and then :data:`GIT_CREDENTIAL_HELPER`. ``[]`` otherwise."""
    if not (auth or {}).get(GIT_TOKEN_VAR):
        return []
    key = f'credential.{GIT_TOKEN_HOST}.helper'
    return [(key, ''), (key, GIT_CREDENTIAL_HELPER)]


def mask(text, values):
    """``text`` with every secret value in ``values`` (``{VARIABLE: value}``) replaced by
    ``[redacted:VARIABLE]`` — for anything ASF writes that a session's command produced."""
    for var, value in (values or {}).items():
        if value:
            text = text.replace(value, f'[redacted:{var}]')
    return text


def build_env(job, base=None):
    """The job's environment: :func:`asf.hermetic.build` in ``worker`` mode over ``base``
    (default ``os.environ``) — only the allow-list and the job's ``passthrough`` names survive;
    no caller identity, no hook variable, no token the tick happened to carry — plus the
    session's own HOME (:func:`session_home`), the account's config dir as
    ``CLAUDE_CONFIG_DIR``, and the job's own identity (``ASF_PRODUCT``, ``ASF_JOB``,
    ``ASF_SESSION``, ``BACKLOG_ID_RANGE``, …), and the account's ``auth_env`` values merged with
    the job's ``product_auth_env`` (the product's own wins for a shared variable —
    :func:`auth_env_values` — raises :class:`AuthEnvError` when a file is missing), with git's
    HTTPS credential for the code host taken from ``GH_TOKEN`` when it is one of them
    (:func:`git_credential_config`). When the job has a ``hooks_dir``,
    ``core.hooksPath`` is set to it, so every commit the session makes picks up its
    ``ASF-Session`` trailer hook (F-0076). No PYTHONPATH: a session runs the product's code,
    not this package."""
    acct = job.account
    # ASF_HOME rides along: the session's HOME is its own, so ``~/.ASF`` there is empty, and the
    # approvals hook (``asf hook approvals``) run inside the session must read the factory's
    # real config and product files, not look for them under the session's home.
    identity = {'ASF_PRODUCT': job.product, 'ASF_JOB': job.name, 'ASF_HOME': env.ASF_HOME}
    identity.update(job.env)
    auth = auth_env_values(acct, job.product_auth_env)
    git_config = [('core.hooksPath', job.hooks_dir)] if job.hooks_dir else []
    git_config += git_credential_config(auth)
    out = hermetic.build(base, home=session_home(acct), identity=identity, pythonpath=False,
                         git_config=git_config, mode='worker', passthrough=job.passthrough)
    if acct is not None and getattr(acct, 'config_dir', None):
        out['CLAUDE_CONFIG_DIR'] = os.path.expanduser(acct.config_dir)
    out.update(auth)
    return out


def _session_line(job):
    """The one ``{"type": "asf", "subtype": "session", ...}`` line a run's log segment opens
    with, or None for a job with no session (F-0076). ``started`` is parsed back from the id's
    own stamp, so the line and the registry agree without a new :class:`Job` field."""
    if not job.session:
        return None
    from asf.workers import lifecycle  # local: lifecycle imports this module
    parsed = lifecycle.parse_session(job.session)
    if not parsed:
        return None
    product, name, stamp = parsed
    started = f'{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z'
    return json.dumps({'type': 'asf', 'subtype': 'session', 'session': job.session,
                       'product': product, 'job': name, 'started': started})


def read_result(log_path):
    """The parsed result line of the log's **last run**, else None.

    A run opens with a ``system``/``init`` line and closes with a ``result`` line — but the
    runtime may write more ``system`` lines (background-task bookkeeping) after the result, so
    the result is not necessarily the last line (B-0028). A later ``init`` (a same-session
    correction appended to the same log) starts a new run: its result is the one that counts,
    and until it arrives the session is still running."""
    if not log_path or not os.path.exists(log_path):
        return None
    result = None
    with open(log_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get('type') == 'system' and rec.get('subtype') == 'init':
                result = None
            elif rec.get('type') == 'result':
                result = rec
    return result


def init_line(log_path):
    """The last run's ``system``/``init`` record, or None — the same scan :func:`read_result`
    makes, rewinding at every ``init``."""
    if not log_path or not os.path.exists(log_path):
        return None
    init = None
    with open(log_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get('type') == 'system' and rec.get('subtype') == 'init':
                init = rec
    return init


def runtime_session(log_path):
    """The runtime's own session id for the log's last run: :func:`init_line`'s ``session_id``,
    else ''. This is the id a continuation resumes; ``ASF_SESSION`` is ours and names a
    launch (F-0076, D1), not a conversation."""
    return str((init_line(log_path) or {}).get('session_id') or '')


# the CLI's own error texts, which it reports with ``subtype: success``
FAILURE_SIGNATURES = (
    ('unknown model', re.compile(r'issue with the selected model|model .* (?:not found|does not exist)', re.I)),
    ('auth', re.compile(r'invalid api key|please run /login|authentication[_ ]error|oauth token', re.I)),
    ('quota', re.compile(r'usage limit reached|rate limit|quota (?:exceeded|exhausted)', re.I)),
    ('permission', re.compile(r'permission denied|not permitted to use', re.I)),
)


def failure_reason(rec):
    """``'token cap'`` when the record carries the factory's structured ``asf.cap`` object, else
    the signature name when a result's text is one of the CLI's error messages, else what the
    session's own typed REPORT declares (``pushed: no`` → ``unpushed work``,
    :mod:`asf.workers.report`), else None.

    The structured field is read first: a session's own report text lands in ``result`` and is
    forgeable, the factory's field is not (F-0028 D9)."""
    asf = (rec or {}).get('asf')
    if isinstance(asf, dict) and isinstance(asf.get('cap'), dict):
        return 'token cap'
    text = str((rec or {}).get('result') or '')
    if report.parse(text):
        # a session that wrote its typed REPORT reached its end: the CLI's error texts are not in
        # it, and its prose may quote them ("check_x: permission denied in this session")
        return report.failure(text)
    for name, pattern in FAILURE_SIGNATURES:
        if pattern.search(text):
            return name
    return report.failure(text)


def result_ok(rec):
    return (bool(rec) and not rec.get('is_error') and rec.get('subtype', 'success') == 'success'
            and failure_reason(rec) is None)


class Runtime:
    name = 'base'

    def run(self, job, wait=False):
        raise NotImplementedError

    def continue_run(self, job):
        """Continue ``job.resume``'s conversation with the text at ``job.brief_path`` on stdin.
        ``None`` from a runtime that cannot — the caller falls back."""
        return None


class ClaudeCodeRuntime(Runtime):
    name = 'claude_code'

    def __init__(self, binary=DEFAULT_BINARY):
        self.binary = binary

    def run(self, job, wait=False):
        log_path = job.log_path or job_log_path(job.product, job.name)
        seed_home(job.account)
        with open(job.brief_path, 'rb') as brief, open(log_path, 'ab') as log:
            # a continued run is the writer's session: a second ``asf`` line would claim otherwise
            line = None if job.resume else _session_line(job)
            if line is not None:
                log.write((line + '\n').encode('utf-8'))
                log.flush()
            if not wait:  # never the caller's child: no <defunct> left in a running tick
                pid = detach.spawn(build_command(job, self.binary), cwd=job.cwd,
                                   env=build_env(job), stdin=brief, stdout=log,
                                   stderr=subprocess.STDOUT)
                return Result(pid=pid, log_path=log_path)
            proc = subprocess.Popen(build_command(job, self.binary), cwd=job.cwd,
                                    env=build_env(job), stdin=brief, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        rc = proc.wait()
        rec = read_result(log_path)
        return Result(ok=rc == 0 and result_ok(rec), pid=proc.pid, returncode=rc,
                      text=(rec or {}).get('result', ''), log_path=log_path,
                      reason=failure_reason(rec))

    def continue_run(self, job, wait=False):
        """:meth:`run` with ``job.resume`` set. None when the spawn raises, or (``wait=True``)
        when the process exits non-zero having written no ``init`` line."""
        log_path = job.log_path or job_log_path(job.product, job.name)
        had = init_line(log_path)
        try:
            result = self.run(job, wait=wait)
        except Exception:
            return None
        if wait and result.returncode and init_line(log_path) in (None, had):
            return None
        return result


class FakeRuntime(Runtime):
    """Replays scripted results in order: each is a dict ``{"ok": bool, "result": str,
    "pid": int, "running": bool}``. ``running`` leaves the log without a result line (a session
    still going). Every call is kept in ``calls`` as ``(job, brief_text)``."""
    name = 'fake'

    def __init__(self, script=None, path=None):
        if path:
            with open(path, encoding='utf-8') as f:
                script = json.load(f)
        self.script = list(script or [])
        self.calls = []
        self._next_pid = 90000
        self._runs = {}

    def run(self, job, wait=False):
        with open(job.brief_path, encoding='utf-8') as f:
            self.calls.append((job, f.read()))
        return self._replay(job)

    def continue_run(self, job):
        """Replays the next scripted step, appending a second ``init``+``result`` pair to the
        same log; a step carrying ``{"decline": true}`` is a runtime that cannot: None."""
        with open(job.brief_path, encoding='utf-8') as f:
            text = f.read()
        if self.script and self.script[0].get('decline'):
            self.script.pop(0)
            return None
        self.calls.append((job, text))
        return self._replay(job)

    def _replay(self, job):
        step = self.script.pop(0) if self.script else {'ok': True, 'result': 'done'}
        self._next_pid += 1
        pid = step.get('pid', self._next_pid)
        log_path = job.log_path or job_log_path(job.product, job.name)
        with open(log_path, 'a', encoding='utf-8') as log:
            line = None if job.resume else _session_line(job)
            if line is not None:
                log.write(line + '\n')
            n = self._runs[job.name] = self._runs.get(job.name, 0) + 1
            log.write(json.dumps({'type': 'system', 'subtype': 'init', 'model': job.model,
                                  'session_id': f'fake:{job.name}:{n}'}) + '\n')
            if step.get('running'):
                return Result(pid=pid, log_path=log_path)
            ok = bool(step.get('ok', True))
            log.write(json.dumps({'type': 'result', 'subtype': 'success' if ok else 'error',
                                  'is_error': not ok, 'result': step.get('result', '')}) + '\n')
        return Result(ok=ok, pid=pid, returncode=0 if ok else 1, text=step.get('result', ''),
                      log_path=log_path)


def from_config(cfg):
    """The runtime ``config.yaml worker_pool.backend`` names (``claude-code`` default)."""
    pool = (cfg or {}).get('worker_pool') or {}
    backend = str(pool.get('backend') or 'claude-code').replace('-', '_')
    if backend == 'fake':
        return FakeRuntime(path=os.path.expanduser(pool['fake_script'])
                           if pool.get('fake_script') else None)
    return ClaudeCodeRuntime(binary=pool.get('binary') or DEFAULT_BINARY)
