"""asf.connectors.command — any connector kind as a shell command with a JSON contract.

For a user who would rather not write Python: ``connectors.<kind>: {command: "<cmd>"}`` makes
every operation of that kind one run of ``<cmd> <operation>``:

* **stdin** — one JSON object ``{"kind": …, "op": …, "args": […], "kwargs": {…}}``: the
  operation's positional and keyword arguments (anything not JSON — a callable, an object — is
  passed as its public attributes, or left out);
* **stdout** — the answer as JSON on the last non-empty line;
* **exit status** — 0 is an answer; anything else, a timeout, or unparseable output is Unknown
  (``ok`` false, ``reason`` saying why), never an empty answer.

Optional keys beside ``command``: ``timeout_s`` (default :data:`DEFAULT_TIMEOUT_S`) and ``env``
(extra environment variables for the command). The ``quota`` and ``secrets`` kinds keep their
own one-line contracts (:mod:`asf.connectors.quota`, :mod:`asf.connectors.secrets`).
"""
import json
import os
import shlex
import subprocess

#: Seconds one operation may take before its answer is Unknown.
DEFAULT_TIMEOUT_S = 120
#: Keyword arguments that are the in-tree transport's own knobs, never sent to a command.
TRANSPORT_KWARGS = ('run', 'env')


def jsonable(value, depth=0):
    """``value`` as JSON-able data: plain data as is, an object as its public attributes, a
    callable (or anything deeper than a few levels) as None."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth > 4 or callable(value):
        return None
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in value]
    attrs = getattr(value, '__dict__', None)
    if isinstance(attrs, dict):
        return {k: jsonable(v, depth + 1) for k, v in attrs.items() if not k.startswith('_')}
    return str(value)


class CommandConnector:
    """A connector of any generic kind whose every operation runs ``command``."""

    name = 'command'

    def __init__(self, kind, command, timeout=DEFAULT_TIMEOUT_S, extra_env=None, run=None):
        self.kind = kind
        self.command = command
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})
        self._run = run

    @classmethod
    def from_spec(cls, kind, spec):
        spec = spec if isinstance(spec, dict) else {}
        return cls(kind, str(spec.get('command') or ''),
                   timeout=float(spec.get('timeout_s') or DEFAULT_TIMEOUT_S),
                   extra_env={str(k): str(v) for k, v in (spec.get('env') or {}).items()})

    def call(self, op, *args, **kwargs):
        """Run ``<command> <op>`` with the arguments on stdin: an :class:`asf.github.Result`."""
        from asf import github
        if not self.command:
            return github.unknown(f'connectors.{self.kind}: no command configured')
        kwargs = {k: v for k, v in kwargs.items() if k not in TRANSPORT_KWARGS}
        timeout = kwargs.pop('timeout', None) or self.timeout
        payload = json.dumps({'kind': self.kind, 'op': op, 'args': jsonable(list(args)),
                              'kwargs': jsonable(kwargs)})
        argv = shlex.split(self.command) + [op]
        env = dict(os.environ, **self.extra_env) if self.extra_env else None
        try:
            p = (self._run or subprocess.run)(argv, input=payload, capture_output=True,
                                              text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return github.unknown('timeout')
        except OSError as e:
            return github.unknown(f'command not runnable: {e}')
        if p.returncode != 0:
            first = next((ln.strip() for ln in (p.stderr or '').splitlines() if ln.strip()), '')
            return github.Result(False, None, p.returncode, p.stdout, p.stderr, github.now_iso(),
                                 f'rc {p.returncode}: {first}' if first else f'rc {p.returncode}')
        lines = [ln for ln in (p.stdout or '').splitlines() if ln.strip()]
        try:
            data = json.loads(lines[-1]) if lines else None
        except ValueError:
            return github.Result(False, None, p.returncode, p.stdout, p.stderr, github.now_iso(),
                                 'bad json')
        return github.Result(True, data, p.returncode, p.stdout, p.stderr, github.now_iso(), '')

    def __getattr__(self, op):
        if op.startswith('_'):
            raise AttributeError(op)

        def operation(*args, **kwargs):
            return self.call(op, *args, **kwargs)
        operation.__name__ = op
        return operation
