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
import subprocess

DEFAULT_BINARY = 'claude'
DEFAULT_PERMISSION_MODE = 'bypassPermissions'


class Job:
    """Everything one run needs. ``account`` is a :class:`asf.workers.pool.Account` (or None)."""

    def __init__(self, product, name, cwd, brief_path, model, account=None, add_dirs=(),
                 permission_mode=DEFAULT_PERMISSION_MODE, env=None, log_path=None):
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


class Result:
    """``ok`` is True/False once the session finished, None while it still runs (detached)."""

    def __init__(self, ok=None, pid=None, returncode=None, text='', log_path=None):
        self.ok = ok
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
    cmd += ['--output-format', 'stream-json', '--verbose']
    return cmd


def build_env(job, base=None):
    """The job's environment: ``base`` (default ``os.environ``) + the account's isolated
    home/config dir + the job's own variables (``BACKLOG_ID_RANGE``, ``ASF_JOB``, ...)."""
    out = dict(os.environ if base is None else base)
    acct = job.account
    if acct is not None:
        if getattr(acct, 'home', None):
            out['HOME'] = os.path.expanduser(acct.home)
        if getattr(acct, 'config_dir', None):
            out['CLAUDE_CONFIG_DIR'] = os.path.expanduser(acct.config_dir)
    out['ASF_PRODUCT'] = job.product
    out['ASF_JOB'] = job.name
    out.update(job.env)
    return out


def read_result(log_path):
    """The parsed last line of a job log when it is a result line, else None."""
    if not log_path or not os.path.exists(log_path):
        return None
    last = None
    with open(log_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if line.strip():
                last = line
    if last is None:
        return None
    try:
        rec = json.loads(last)
    except json.JSONDecodeError:
        return None
    return rec if isinstance(rec, dict) and rec.get('type') == 'result' else None


def result_ok(rec):
    return bool(rec) and not rec.get('is_error') and rec.get('subtype', 'success') == 'success'


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
            proc = subprocess.Popen(build_command(job, self.binary), cwd=job.cwd,
                                    env=build_env(job), stdin=brief, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        if not wait:
            return Result(pid=proc.pid, log_path=log_path)
        rc = proc.wait()
        rec = read_result(log_path)
        return Result(ok=rc == 0 and result_ok(rec), pid=proc.pid, returncode=rc,
                      text=(rec or {}).get('result', ''), log_path=log_path)


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
