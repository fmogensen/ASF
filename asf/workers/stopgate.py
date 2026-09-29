"""asf.workers.stopgate — the built-in ``Stop`` hook: a factory session may not end with its
branch's work off origin.

``asf hook unpushed`` (wired by :mod:`asf.hooks`, Task 3) answers the runtime's own contract for
a hook — the one :mod:`asf.approvals` already uses: rc ``0`` lets the stop through, rc ``2``
blocks it and feeds stderr back to the model, which then keeps working instead of ending
(F-0158 §2.2). The gate measures nothing itself: it asks
:func:`asf.workers.lifecycle.unpublished`, the one owner of "is this on origin", so the gate
never drifts from what :mod:`asf.workers.health` will later judge the run on.

It is bounded (§2.3): a run's stop may be refused only ``conventions.stop_gate_rounds`` times
(default :data:`asf.conventions.DEFAULT_STOP_GATE_ROUNDS`), then the gate stands aside and the
factory's own publish/correction path is the net — an unbounded gate would turn one stuck session
into an infinite one. It never commits and never pushes for the session: it refuses and names the
commands, because a hook that wrote to git from inside a live session would race the session's
own git, and the session is the one that knows what its commit message should say.

Every exception lets the stop through and says the hook broke, not the session — the inversion of
:mod:`asf.approvals`' own fail-closed rule: a gate that cannot judge must not trap the session it
cannot judge (B-0125, inverted).
"""
import json
import os
import sys

from asf import env

#: factory scratch under ``env.state_dir(product)``: never committed, one integer per job,
#: cleared by :func:`clear` at launch.
GATES_DIR = 'gates'

#: what the operator's ledger and the session's own transcript are told when the hook breaks —
#: never a refusal, so the operator does not read it as one.
ERROR_LINE = 'unpushed gate: {why}; not a refusal — retry'


def counter_path(product, job):
    """``~/.ASF/state/<p>/gates/<job>.stop``. ``job`` goes through :func:`os.path.basename` so
    no environment value can walk the path outside :data:`GATES_DIR`."""
    return os.path.join(env.state_dir(product), GATES_DIR, f'{os.path.basename(job or "")}.stop')


def refusals(product, job):
    """The integer in :func:`counter_path`'s file — ``0`` for one missing, unreadable or not an
    integer. Never raises."""
    try:
        with open(counter_path(product, job), encoding='utf-8') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def bump(product, job):
    """One more refusal for ``job``, written before the refusal is printed (so a gate that dies
    between the two refuses one time fewer, never one more). ``True`` when the write reached
    disk, ``False`` on an ``OSError``."""
    path = counter_path(product, job)
    n = refusals(product, job) + 1
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(str(n))
        return True
    except OSError:
        return False


def clear(product, job):
    """Remove ``job``'s counter, ignoring a missing file and any ``OSError`` — called by
    :func:`asf.workers.spawn.spawn` at launch so a correction round arrives with a fresh bound."""
    try:
        os.remove(counter_path(product, job))
    except OSError:
        pass


def spent(product, job, conv, payload):
    """True when this run's stop may no longer be refused: its counter has reached
    ``conv.stop_gate_rounds``, or the runtime says it has already blocked this stop once and the
    bound is one. The counter is the authority; ``stop_hook_active`` is the runtime's own loop
    guard, read as corroboration and never as permission to refuse again.

    Bumps the counter (before returning ``False``) when the bound is not yet reached, so the
    count is written before :func:`run_hook` prints the refusal (§2.3). A counter that cannot be
    written (an unwritable or full state dir) never advances on its own; the one escape from the
    refusal loop that would otherwise trap the session is the runtime's own ``stop_hook_active``,
    corroborating that this exact stop was already blocked once (PD4)."""
    from asf.conventions import DEFAULT_STOP_GATE_ROUNDS
    try:
        bound = max(0, int(getattr(conv, 'stop_gate_rounds', DEFAULT_STOP_GATE_ROUNDS)))
    except (TypeError, ValueError):
        bound = DEFAULT_STOP_GATE_ROUNDS
    if refusals(product, job) >= bound:
        return True
    wrote = bump(product, job)
    return (not wrote) and bool((payload or {}).get('stop_hook_active'))


def refusal_lines(branch, detail, item, kind):
    """The block the session reads. Names the evidence and the exact commands, and nothing else
    — pure, no io, no clock. The commit line is dropped when ``detail`` counts no uncommitted
    file, so a session that committed and only forgot the push is not told to ``git add``; the
    push line is never dropped (§1.1: whatever is missing, the branch must reach origin)."""
    from asf.briefs import preamble
    prefix = preamble.SUBJECT_KIND.get(kind or '', 'task')
    uncommitted = _uncommitted_count(detail)
    lines = [f'REFUSED: your work is not on origin — {detail}',
             f'This is branch {branch}. Finish it before you end your turn:']
    if uncommitted:
        lines.append(f"  git add -A && git commit -s -m '{prefix}({item}): <what you did>'")
    lines.append(f'  git push origin {branch}')
    lines.append(f'A commit subject must name {item} as a token. Never force-push, never '
                 '--no-verify, never a')
    lines.append('branch but this one. If the push is refused as non-fast-forward, stop there — '
                 'do not merge and')
    lines.append('do not force — and say `pushed: rebased <sha> — the factory publishes` in your '
                 'REPORT (B-0056).')
    return lines


def _uncommitted_count(detail):
    try:
        return int(str(detail).split('uncommitted', 1)[0].strip().rsplit(' ', 1)[-1])
    except (ValueError, IndexError):
        return 1  # unrecognised text: show the safer, fuller command rather than drop it


def stood_aside_line(branch, detail, n):
    """The line printed when :func:`spent` is true: the session was refused, was warned, and was
    let go — the transcript's record that this run's stop was not silently allowed."""
    return (f'STOOD ASIDE: refused {n} time(s) — {branch} is still not on origin ({detail}). '
            'The gate stands aside; the factory\'s own publish is now the net.')


def run_hook(stdin_text, environ=None, out=sys.stderr, product=None):
    """``asf hook unpushed`` — the rc the runtime reads: 0 lets the stop through, 2 blocks it and
    feeds ``out`` back to the model, which then keeps working.

    0 at once when this is not a factory session (no ``ASF_JOB``, D4), when the run's work is not
    its branch (:func:`asf.workers.lifecycle.lands`), when there is no branch or it is the trunk,
    when the tree is published (:func:`asf.workers.lifecycle.unpublished`), or when the run's
    refusals are spent (§2.3). Every exception lets the stop through and says the hook broke, not
    the session (B-0125, inverted: a gate that breaks must not trap the session it cannot judge).
    """
    environ = environ or {}
    job = environ.get('ASF_JOB')
    if not job:
        return 0
    try:
        return _run(stdin_text, environ, out, product, job)
    except Exception as e:                          # never traps the session it cannot judge
        from asf import approvals
        approvals.record_hook_error(product or environ.get('ASF_PRODUCT'), job,
                                     RuntimeError(f'unpushed gate: {e}'))
        print(ERROR_LINE.format(why=str(e) or type(e).__name__), file=out)
        return 0


def _run(stdin_text, environ, out, product, job):
    import fnmatch

    from asf import refguard
    from asf.workers import lifecycle, pool

    payload = json.loads(stdin_text or '{}') or {}
    prod = env.load_product(product or environ.get('ASF_PRODUCT'))
    run = pool.load_sessions(prod).get(job)
    worktree = (run or {}).get('worktree')
    branch = (run or {}).get('branch')
    if not run or not worktree or not os.path.isdir(worktree):
        return 0
    if not lifecycle.lands(run, pool.sessions_path(prod)):
        return 0
    if not branch or branch == 'HEAD':
        return 0
    protected = refguard.patterns(prod.main, refguard.listed(prod.conventions))
    if any(branch == p or fnmatch.fnmatchcase(branch, p) for p in protected):
        return 0
    ok, detail = lifecycle.unpublished(worktree, branch, prod.main)
    if ok:
        return 0
    if spent(prod, job, prod.conventions, payload):
        print(stood_aside_line(branch, detail, refusals(prod, job)), file=out)
        return 0
    print('\n'.join(refusal_lines(branch, detail, run.get('item'), run.get('kind'))), file=out)
    return 2
