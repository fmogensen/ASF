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

Either way the push is killed — its whole process group, the hook's children too — after a budget
sized to its own kind (:func:`push_timeout`): ``git.push_timeout_s`` for a push that runs the
hook, ``git.ref_push_timeout_s`` for one that skips it. No hook and no network hang holds a
tick. A timed-out push is a failed push: the ref is logged and the caller goes on; the branch
stays as it was (git updates a remote ref only once the hook passed and the pack went through).

Every push passes a ``guard`` (:class:`asf.refguard.Guard`, keyed on the repository being
pushed): a target that is that repository's trunk or a protected ref is warned about or refused
here, before ``git`` runs, by ``conventions.flags.refguard``. Only a landing path built to advance
the trunk passes a door (``guard.door``); the record's publish passes
:data:`asf.refguard.RECORD`. the clients lint checks every call passes ``guard=``.
"""
import os
import signal
import subprocess
import sys
import time

from asf import hermetic, mutation_guard, refguard
from asf.conventions import DEFAULT_PUSH_TIMEOUT_S, DEFAULT_REF_PUSH_TIMEOUT_S

#: The push door takes a ref guard: the clients lint enforces ``guard=`` at every
#: ``gitpush.push(`` call while this is set.
__gitpush_door__ = True

#: The returncode a push killed on its timeout reports (the ``timeout(1)`` convention).
TIMED_OUT = 124


def push_timeout(conv=None, refs_only=False):
    """The seconds one push may take, at least 1 — which budget depends on whether the product's
    pre-push hook runs in it (``refs_only``, the same flag that decides ``--no-verify``):

    * a **hooked** push — a session's branch published, the trunk, a lane rewrite — is bounded by
      the product's own lint, typecheck and build: ``conventions.git.push_timeout_s``
      (:data:`asf.conventions.DEFAULT_PUSH_TIMEOUT_S`, 900 s). One number for both kinds meant
      120 s, and 120 s killed real publishes on a loaded host (F-0165).
    * a **hookless** push — an archive head, a delete, a tag, a brief or a reservation ref — has
      only the network in it: ``conventions.git.ref_push_timeout_s``
      (:data:`asf.conventions.DEFAULT_REF_PUSH_TIMEOUT_S`, 120 s), short on purpose.

    An unset key and a ``0`` resolve alike — both fall back to that key's own default."""
    key = 'ref_push_timeout_s' if refs_only else 'push_timeout_s'
    default = DEFAULT_REF_PUSH_TIMEOUT_S if refs_only else DEFAULT_PUSH_TIMEOUT_S
    try:
        return max(1, int(getattr(conv, key, None) or default))
    except (TypeError, ValueError):
        return default


def _timed(result, started):
    """``result`` with ``seconds`` on it: how long the push took, the product's hook included. The
    factory's own bound is a guess until somebody measures the hook it is bounding (F-0165), so
    every push reports its own; :func:`asf.workers.lifecycle.publish` puts it on its line."""
    result.seconds = round(time.monotonic() - started, 1)
    return result


def push_args(args, refs_only=False):
    """``git push`` + ``args``, with ``--no-verify`` when the push carries no new code."""
    return ['git', 'push'] + (['--no-verify'] if refs_only else []) + list(args)


def targets(args):
    """The refspecs of ``git push <args>``: every argument after the remote that is not an
    option (``-q``, ``--force-with-lease=…``, ``--delete``). After ``--delete`` a name is the
    ref deleted — the target all the same."""
    words = [str(a) for a in args if not str(a).startswith('-')]
    return words[1:]


def _say(log, line):
    (log or (lambda s: print(s, file=sys.stderr)))(line)


def push(args, cwd, *, guard, refs_only=False, conv=None, timeout=None, env=None, log=None):
    """``git push <args>`` in ``cwd``: a :class:`subprocess.CompletedProcess` (text output).

    ``refs_only``: the push carries no new code — ``--no-verify``, the product's hook skipped.
    ``conv``: the product's conventions — the budget is :func:`push_timeout` of ``conv`` and this
    push's own ``refs_only``, so the flag that skips the hook is the flag that sizes the clock and
    the two can never disagree (F-0165). ``timeout``: an explicit number, for a caller that means
    one (:func:`asf.workers.lifecycle.publish`'s ``push_timeout_s``).
    Past the budget the push's process group is killed, ``returncode`` is :data:`TIMED_OUT`,
    ``stderr`` names the refs, and a line naming the key to raise goes to ``log`` (default
    stderr). A dry run in progress (:mod:`asf.mutation_guard`) refuses instead of
    running ``git`` at all — the backstop for a caller that never threaded its own ``dry_run``
    flag this far (2026-09-29): a non-zero, non-:data:`TIMED_OUT` ``returncode`` and ``stderr``
    naming why, exactly the shape a refused push already is to every caller here.
    ``seconds`` on the returned process is how long it took, timed out or not.

    ``guard`` (required, :class:`asf.refguard.Guard`): each target (:func:`targets`) is asked
    :meth:`~asf.refguard.Guard.refusal` first. Under ``refuse`` the push returns
    ``CompletedProcess(cmd, 1, '', line)`` without running ``git``; under ``warn`` the line goes
    to ``log`` and the push proceeds."""
    started = time.monotonic()
    cmd = push_args(args, refs_only)
    for target in targets(args):
        line = guard.refusal(target)
        if not line:
            continue
        _say(log, line)
        if guard.mode == refguard.REFUSE:
            return _timed(subprocess.CompletedProcess(cmd, 1, '', line), started)
    if mutation_guard.is_active():
        line = mutation_guard.would_line('git', cmd)
        _say(log, line)
        return _timed(subprocess.CompletedProcess(cmd, 1, '', line), started)
    limit = timeout if timeout is not None else push_timeout(conv, refs_only)
    run_env = hermetic.git_env(env)
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
        key = 'ref_push_timeout_s' if refs_only else 'push_timeout_s'
        line = (f'push timed out after {limit}s: {refs or "?"} — left as it was '
                f'(raise git.{key})')
        _say(log, line)
        return _timed(subprocess.CompletedProcess(cmd, TIMED_OUT, out or '',
                                           ((err or '') + '\n' + line).strip()), started)
    return _timed(subprocess.CompletedProcess(cmd, p.returncode, out, err), started)
