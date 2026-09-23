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
import subprocess

from asf import detach, hermetic
from asf.workers import report

DEFAULT_BINARY = 'claude'
DEFAULT_PERMISSION_MODE = 'bypassPermissions'


class Job:
    """Everything one run needs. ``account`` is a :class:`asf.workers.pool.Account` (or None)."""

    def __init__(self, product, name, cwd, brief_path, model, account=None, add_dirs=(),
                 permission_mode=DEFAULT_PERMISSION_MODE, env=None, log_path=None,
                 settings_file=None, hooks_dir=None):
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
        # the worker's permission rules (allow list + the deny rules, e.g. never push to the main
        # branch); ``worker_pool.settings_file`` in config — a session without it runs unfenced
        self.settings_file = settings_file
        # the per-product git hook dir (asf.workers.githooks.ensure) — set as core.hooksPath so
        # every commit the session makes carries its ASF-Session trailer (F-0076)
        self.hooks_dir = hooks_dir

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
    for d in job.add_dirs:
        cmd += ['--add-dir', d]
    if job.model:
        cmd += ['--model', job.model]
    if job.settings_file:
        cmd += ['--settings', job.settings_file]
    cmd += ['--output-format', 'stream-json', '--verbose']
    return cmd


def build_env(job, base=None):
    """The job's environment: :func:`asf.hermetic.build` over ``base`` (default ``os.environ``)
    — no caller identity inherited from the tick, no git-hook variable — plus the account's
    isolated home/config dir and the job's own identity (``ASF_PRODUCT``, ``ASF_JOB``,
    ``ASF_SESSION``, ``BACKLOG_ID_RANGE``, …). When the job has a ``hooks_dir``,
    ``core.hooksPath`` is set to it, so every ``git`` the session runs picks up its
    ``ASF-Session`` trailer hook (F-0076). PYTHONPATH is left as the base has it: a session runs
    the product's code, not this package."""
    acct = job.account
    home = getattr(acct, 'home', None) if acct is not None else None
    identity = {'ASF_PRODUCT': job.product, 'ASF_JOB': job.name}
    identity.update(job.env)
    git_config = [('core.hooksPath', job.hooks_dir)] if job.hooks_dir else []
    out = hermetic.build(base, home=home, identity=identity, pythonpath=False,
                         git_config=git_config)
    if acct is not None and getattr(acct, 'config_dir', None):
        out['CLAUDE_CONFIG_DIR'] = os.path.expanduser(acct.config_dir)
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


# the CLI's own error texts, which it reports with ``subtype: success``
FAILURE_SIGNATURES = (
    ('unknown model', re.compile(r'issue with the selected model|model .* (?:not found|does not exist)', re.I)),
    ('auth', re.compile(r'invalid api key|please run /login|authentication[_ ]error|oauth token', re.I)),
    ('quota', re.compile(r'usage limit reached|rate limit|quota (?:exceeded|exhausted)', re.I)),
    ('permission', re.compile(r'permission denied|not permitted to use', re.I)),
)


def failure_reason(rec):
    """The signature name when a result's text is one of the CLI's error messages, else what
    the session's own typed REPORT declares (``pushed: no`` → ``unpushed work``,
    :mod:`asf.workers.report`), else None."""
    text = str((rec or {}).get('result') or '')
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


class ClaudeCodeRuntime(Runtime):
    name = 'claude_code'

    def __init__(self, binary=DEFAULT_BINARY):
        self.binary = binary

    def run(self, job, wait=False):
        log_path = job.log_path or job_log_path(job.product, job.name)
        with open(job.brief_path, 'rb') as brief, open(log_path, 'ab') as log:
            line = _session_line(job)
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

    def run(self, job, wait=False):
        with open(job.brief_path, encoding='utf-8') as f:
            self.calls.append((job, f.read()))
        step = self.script.pop(0) if self.script else {'ok': True, 'result': 'done'}
        self._next_pid += 1
        pid = step.get('pid', self._next_pid)
        log_path = job.log_path or job_log_path(job.product, job.name)
        with open(log_path, 'a', encoding='utf-8') as log:
            line = _session_line(job)
            if line is not None:
                log.write(line + '\n')
            log.write(json.dumps({'type': 'system', 'subtype': 'init', 'model': job.model}) + '\n')
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
