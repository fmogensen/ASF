"""asf.connectors.claude_code — the default ``runtime`` connector: the Claude Code CLI.

The one module that names the coding-agent CLI. It runs a worker session locally
(:class:`ClaudeCodeRuntime`: ``<binary> -p …``, the brief on stdin, the stream-json log), hands
the cloud lane its runtime (``cloud.runtime``: ``claude-remote`` — :mod:`asf.workers.remote`,
a routine created through the CLI — or ``actions``), and answers the CLI's own probes (its
``--help`` for the capability check, its ``plugin --help`` for the plugin install). The binary is
``worker_pool.binary`` (default :data:`DEFAULT_BINARY`).

Top-level imports are stdlib only: :mod:`asf.workers.runtime` and :mod:`asf.workers.capability`
import this module's constants, so it must stay a leaf; everything else is imported at call time.
"""
import shutil
import subprocess

#: The CLI's binary when ``worker_pool.binary`` names none.
DEFAULT_BINARY = 'claude'
#: What an operator runs once to mint an account's long-lived session token.
TOKEN_COMMAND = f'{DEFAULT_BINARY} setup-token'


def configured_binary(cfg):
    return ((cfg or {}).get('worker_pool') or {}).get('binary') or DEFAULT_BINARY


class ClaudeCodeRuntime:
    """A local worker session: the CLI headless, detached unless ``wait``."""
    name = 'claude_code'

    def __init__(self, binary=DEFAULT_BINARY):
        self.binary = binary

    def run(self, job, wait=False):
        from asf import detach
        from asf.workers import runtime as rt
        log_path = job.log_path or rt.job_log_path(job.product, job.name)
        rt.seed_home(job.account)
        job_env, binary = rt.guard_env(rt.build_env(job), self.binary)
        cmd = rt.build_command(job, binary)
        with open(rt.session_brief(job), 'rb') as brief, open(log_path, 'ab') as log:
            # a continued run is the writer's session: a second ``asf`` line would claim otherwise
            line = None if job.resume else rt._session_line(job)
            if line is not None:
                log.write((line + '\n').encode('utf-8'))
                log.flush()
            if not wait:  # never the caller's child: no <defunct> left in a running tick
                pid = detach.spawn(cmd, cwd=job.cwd,
                                   env=job_env, stdin=brief, stdout=log,
                                   stderr=subprocess.STDOUT)
                return rt.Result(pid=pid, log_path=log_path)
            proc = subprocess.Popen(cmd, cwd=job.cwd,
                                    env=job_env, stdin=brief, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        rc = proc.wait()
        rec = rt.read_result(log_path)
        return rt.Result(ok=rc == 0 and rt.result_ok(rec), pid=proc.pid, returncode=rc,
                         text=(rec or {}).get('result', ''), log_path=log_path,
                         reason=rt.failure_reason(rec))

    def continue_run(self, job, wait=False):
        """:meth:`run` with ``job.resume`` set. None when the spawn raises, or (``wait=True``)
        when the process exits non-zero having written no ``init`` line."""
        from asf.workers import runtime as rt
        log_path = job.log_path or rt.job_log_path(job.product, job.name)
        had = rt.init_line(log_path)
        try:
            result = self.run(job, wait=wait)
        except Exception:
            return None
        if wait and result.returncode and rt.init_line(log_path) in (None, had):
            return None
        return result


class ClaudeCodeConnector:
    """``connectors.runtime: claude-code`` (the default)."""
    name = 'claude-code'
    token_command = TOKEN_COMMAND

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    @property
    def binary(self):
        return configured_binary(self.cfg)

    def local(self):
        """The runtime a local worker session runs on."""
        return ClaudeCodeRuntime(binary=self.binary)

    def cloud(self, settings, product):
        """The runtime the cloud lane launches on, per ``cloud.runtime``."""
        from asf.workers import cloud
        if settings.runtime == cloud.RUNTIME_REMOTE:
            from asf.workers import remote
            return remote.RemoteRuntime(settings, product)
        from asf.workers import actions
        return actions.ActionsRuntime(settings, product)

    def help_text(self, resolved, environ=None, timeout=None):
        """``<resolved> --help``'s stdout, or None on any failure (the capability probe)."""
        return help_text(resolved, environ, timeout)

    def plugin_command(self):
        """The CLI's path when its ``plugin`` subcommand offers a non-interactive install."""
        return plugin_command()

    def install_plugin(self, cli, dest, env=None):
        """Add the marketplace at ``dest`` and install the plugin through ``cli``."""
        subprocess.run([cli, 'plugin', 'marketplace', 'add', dest], check=False, env=env)
        subprocess.run([cli, 'plugin', 'install', 'asf@asf'], check=False, env=env)


class FakeRuntimeConnector(ClaudeCodeConnector):
    """``connectors.runtime: fake`` (or ``worker_pool.backend: fake``): the replay runtime."""
    name = 'fake'

    def local(self):
        from asf.workers import runtime as rt
        return rt.fake_from_config(self.cfg)


def help_text(resolved, environ=None, timeout=None):
    from asf.workers import capability
    try:
        proc = subprocess.run([resolved, '--help'], capture_output=True, text=True,
                              timeout=timeout or capability.PROBE_TIMEOUT_S, env=environ)
        return proc.stdout if proc.returncode == 0 and proc.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None


def plugin_command():
    """The runtime's own CLI, probed for a non-interactive plugin-install subcommand: a binary on
    ``PATH`` whose ``plugin --help`` names both ``install`` and ``marketplace``; else None."""
    cli = shutil.which(DEFAULT_BINARY)
    if not cli:
        return None
    try:
        result = subprocess.run([cli, 'plugin', '--help'], capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    help_out = result.stdout + result.stderr
    if 'install' in help_out and 'marketplace' in help_out:
        return cli
    return None


def connector(cfg=None):
    return ClaudeCodeConnector(cfg)


def fake(cfg=None):
    return FakeRuntimeConnector(cfg)
