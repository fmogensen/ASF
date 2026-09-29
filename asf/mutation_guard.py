"""asf.mutation_guard — the dry-run backstop every git-push and ``gh`` wrapper checks before it
writes anything.

A dry run is only as safe as every call site that remembers to ask it "am I dry?" — and one that
forgets fails open, not safe: 2026-09-29, :func:`asf.tick.step_wave.lane_pass` built its
:class:`~asf.harvest.lane.Lane` with ``dry_run`` hardcoded ``False`` regardless of the tick's own
flag, so ``asf tick --dry-run`` deleted two already-landed branches pending their delete and the
``ci_queue`` pass it also runs cancelled four queued CI runs — every one of the lane's own
``if self.dry_run`` checks was individually correct and still missed it, because the object they
all read was wrong from the start.

This module is the structural fix for *that* class of bug: one flag, set once for the life of a
dry-run pass (:func:`active`), that the few functions which actually shell out to a mutating
``git push`` or ``gh`` call (:mod:`asf.gitpush`, :func:`asf.harvest.harvest._gh`,
:meth:`asf.ci_queue.GitHubSource.gh_try`) check for themselves — so a step that never threads its
own ``dry_run`` argument at all still cannot write anything while a dry run is in progress; it
just refuses, prints one ``would …`` line, and lets its caller's own refusal handling (every one
of these already handles a refused push or a refused ``gh`` call) take it from there. A read
(``gh pr list``, ``gh run view``, a plain ``gh api`` GET) is never touched — only what would
change something on the host or the remote (:func:`is_mutating_gh`).
"""
import contextlib
import threading

_state = threading.local()


def is_active():
    """True for the life of the innermost :func:`active` block on this thread."""
    return getattr(_state, 'active', False)


@contextlib.contextmanager
def active():
    """On for the life of the ``with`` block, however deep the calls inside it go. Nests safely:
    only the outermost exit turns it back off, so a dry-run helper that itself opens another
    ``active()`` block (a test, a nested rehearsal) never turns the guard off early for its
    caller."""
    was = is_active()
    _state.active = True
    try:
        yield
    finally:
        _state.active = was


#: ``gh`` subcommands (their first two tokens) that write to the host — everything else (a list,
#: a view, a plain ``api`` GET) is a read this guard never touches.
_MUTATING_GH_PREFIXES = frozenset({
    ('run', 'cancel'), ('run', 'rerun'),
    ('workflow', 'run'),
    ('pr', 'merge'), ('pr', 'close'), ('pr', 'create'), ('pr', 'comment'), ('pr', 'edit'),
    ('pr', 'reopen'),
    ('release', 'create'), ('release', 'delete'), ('release', 'edit'), ('release', 'upload'),
})
#: ``gh api`` methods that write; a GET (explicit or, since it is the default, no ``-X`` at all)
#: is a read.
_MUTATING_API_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})


def is_mutating_gh(args):
    """True when a ``gh`` argv would write something: a listed subcommand, or ``api`` called with
    ``-X``/``--method`` naming a write verb."""
    args = [str(a) for a in args or ()]
    if tuple(args[:2]) in _MUTATING_GH_PREFIXES:
        return True
    if args[:1] == ['api']:
        for i, a in enumerate(args):
            if a in ('-X', '--method') and i + 1 < len(args):
                return args[i + 1].strip().upper() in _MUTATING_API_METHODS
    return False


def would_line(kind, args):
    return f'DRY-RUN guard: refused — would run {kind} {" ".join(str(a) for a in args)}'
