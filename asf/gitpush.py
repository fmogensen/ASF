"""asf.gitpush — every ``git push`` the factory makes, bounded in time.

A push runs the product's own ``pre-push`` hook, and a product's hook may lint, typecheck and
build for minutes. The factory makes two kinds of push:

* **ref-only** (``refs_only=True``): a ref that carries no new code — an ``archive/…`` head kept
  for reference, a branch delete, a tag or a brief ref. Its commits are already on origin, so
  the product's hook has nothing to judge: it is skipped with ``--no-verify``. (2026-09-26: one
  product's archive push sat 8+ minutes in its hook inside the tick's health step, and finished
  sessions piled up unharvested behind it.)
* **content**: the trunk, or a branch's commits published on a session's behalf. The product's
  hook runs as it always did.

Either way the push is killed — its whole process group, the hook's children too — after
``git.push_timeout_s`` seconds (:func:`push_timeout`), so no hook and no network hang holds a
tick. A timed-out push is a failed push: the ref is logged and the caller goes on; the branch
stays as it was (git updates a remote ref only once the hook passed and the pack went through).
"""
import os
import signal
import subprocess
import sys

from asf.conventions import DEFAULT_PUSH_TIMEOUT_S

#: The returncode a push killed on its timeout reports (the ``timeout(1)`` convention).
TIMED_OUT = 124

#: Variables a git hook exports: a push made under one would act on the hook's repo, not ``cwd``.
_HOOK_VARS = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_PREFIX', 'GIT_OBJECT_DIRECTORY',
              'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_QUARANTINE_PATH')


def push_timeout(conv=None):
    """The seconds one push may take: ``conventions.git.push_timeout_s``, at least 1."""
    try:
        return max(1, int(getattr(conv, 'push_timeout_s', None) or DEFAULT_PUSH_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_PUSH_TIMEOUT_S


def push_args(args, refs_only=False):
    """``git push`` + ``args``, with ``--no-verify`` when the push carries no new code."""
    return ['git', 'push'] + (['--no-verify'] if refs_only else []) + list(args)


def push(args, cwd, refs_only=False, timeout=None, env=None, log=None):
    """``git push <args>`` in ``cwd``: a :class:`subprocess.CompletedProcess` (text output).

    ``refs_only``: the push carries no new code — ``--no-verify``, the product's hook skipped.
    ``timeout``: seconds (default :func:`push_timeout`); past it the push's process group is
    killed, ``returncode`` is :data:`TIMED_OUT`, ``stderr`` names the refs, and a line goes to
    ``log`` (default stderr)."""
    cmd = push_args(args, refs_only)
    limit = timeout if timeout is not None else push_timeout()
    run_env = dict(os.environ if env is None else env)
    for var in _HOOK_VARS:
        run_env.pop(var, None)
    p = subprocess.Popen(cmd, cwd=cwd, env=run_env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)
    try:
        out, err = p.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
        out, err = p.communicate()
        refs = ' '.join(a for a in args if not str(a).startswith('-') and a != 'origin')
        line = f'push timed out after {limit}s: {refs or "?"} — left as it was'
        (log or (lambda s: print(s, file=sys.stderr)))(line)
        return subprocess.CompletedProcess(cmd, TIMED_OUT, out or '',
                                           ((err or '') + '\n' + line).strip())
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)
