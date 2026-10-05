"""asf.connectors.secrets — the ``secrets`` connectors: resolve an ``auth_env`` reference.

``worker_pool.accounts[].auth_env`` and a product's ``conventions.auth_env`` map a variable to a
reference; the connector turns the reference into the value a session gets.

* ``file`` (default) — the reference is a file path; the value is its content, stripped.
* ``command`` — ``connectors.secrets: {command: "<cmd> {ref}"}``: the command runs with
  ``{ref}`` substituted (``~`` already expanded) and its stdout, stripped, is the value. A
  non-zero exit, a timeout or empty output reads as a reference that cannot be read
  (:class:`OSError`), so the launch refuses with NEEDS OPERATOR exactly as for a missing file.
"""
import shlex
import subprocess

#: Seconds a secrets command may take.
DEFAULT_TIMEOUT_S = 30


class FileSecrets:
    name = 'file'

    def __init__(self, cfg=None):
        self.cfg = cfg

    def read(self, ref):
        with open(ref, encoding='utf-8') as f:
            return f.read().strip()


class CommandSecrets:
    name = 'command'

    def __init__(self, command, timeout=DEFAULT_TIMEOUT_S, run=None):
        self.command = command
        self.timeout = timeout
        self._run = run

    def read(self, ref):
        argv = [a.replace('{ref}', ref) for a in shlex.split(self.command)]
        try:
            p = (self._run or subprocess.run)(argv, capture_output=True, text=True,
                                              timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise OSError(f'secrets command timed out after {self.timeout}s') from e
        if p.returncode != 0:
            raise OSError(f'secrets command exited {p.returncode}')
        return (p.stdout or '').strip()


def file(cfg=None):
    return FileSecrets(cfg)


def command(cfg=None):
    spec = ((cfg or {}).get('connectors') or {}).get('secrets')
    spec = spec if isinstance(spec, dict) else {}
    return CommandSecrets(str(spec.get('command') or ''),
                          timeout=float(spec.get('timeout_s') or DEFAULT_TIMEOUT_S))
