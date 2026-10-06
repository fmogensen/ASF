"""asf/evals/adapters.py — the dispatch between a lever's adapter name and the function that
scores it. Each adapter is one function, ``fires(given, want) -> (bool, detail)``; the scorer
(``asf.evals.run.score``) compares the result against a task's ``expect``, never this module.

``ADAPTERS`` starts empty here: ``asf.evals.set`` already refuses a manifest entry whose
``adapter`` name is not one of the three known words at load time, so an unknown *name* is
caught before this module ever sees it. What is missing before the matcher, kind-detector and
rule-check adapters land (later Tasks) is the *function* itself, and :func:`fires` reports that
by raising, never by guessing a verdict for a lever it cannot run.
"""


class Broken(Exception):
    """An adapter could say neither fire nor no-fire — a check that timed out, could not run, or
    answered with neither a pass nor a violation. ``str(exc)`` is the line a check printed when
    it could say neither, or as close to one as the adapter could produce."""


#: ``{adapter name: fires(given, want) -> (bool, detail)}``. Populated by later Tasks.
ADAPTERS = {}


def fires(lever, task):
    """Dispatch ``task`` to ``lever``'s adapter function, called as ``fn(task.given, task.want)``.

    Raises :class:`asf.evals.set.EvalError` when ``lever.adapter`` has no registered function
    yet — a gap in this package, never a silent no-fire. Anything the adapter itself raises,
    :class:`Broken` or otherwise, is not caught here: that is the scorer's problem."""
    fn = ADAPTERS.get(lever.adapter)
    if fn is None:
        from asf.evals.set import EvalError
        raise EvalError(
            f'no adapter function registered for {lever.adapter!r} (lever {lever.id!r})')
    return fn(task.given, task.want)
