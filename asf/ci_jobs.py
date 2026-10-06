"""asf.ci_jobs — what a CI job's ending says: why a cancelled job was cancelled.

The host has no separate conclusion for a job that ran out of time: it concludes ``cancelled``,
exactly as a job cancelled with its run does, and exactly as a matrix leg cancelled by its
sibling's failure does (F-0131 P1, P16). Three very different things, one word, and three
different people who should hear about them — so every cancelled job carries its cause:

``timeout``      it ran to its own limit. Raise the limit or make the job faster.
``runner-loss``  it started, ran well short of its limit, and the run reached a verdict without
                 it. Nobody outside a job can cancel one job (P15), so the runner went away.
``failure``      the residue: the cancel followed a verdict or a decision somewhere else — a
                 sibling's red, a newer push, the queue, a person. It says nothing about this job.

A job's ``cause`` is why *that job* stopped; a :class:`asf.scorecard.diagnose.Cause` is a
factory-wide diagnosis a card is filed from. The two are one import apart and are not the same
thing.

Pure functions over the job rows of the ``ci`` stream: no host call, no filesystem, no
configuration. The caller passes what it already has.
"""
from asf import ci_pool

#: a job conclusion that is a verdict of its own — :data:`asf.ci_pool.FAIL`, aliased rather than
#: copied so the tree keeps one definition of a red job (F-0131 C15)
RED = ci_pool.FAIL

#: it ran to its own limit
TIMEOUT = 'timeout'
#: it started, died well short of its limit, and the run reached a verdict without it
RUNNER_LOSS = 'runner-loss'
#: the cancel followed a verdict or a decision elsewhere; no claim about this job
FAILURE = 'failure'
CAUSES = (TIMEOUT, RUNNER_LOSS, FAILURE)

#: minutes of grace on the limit test: ``mins`` floors a duration to whole minutes and the host
#: overshoots by seconds (the live case: a 360-minute limit, 6 h 0 m 15 s of duration → 360).
TIMEOUT_SLACK_MIN = 1
CANCELLED = 'cancelled'


def _started(job):
    """Whether ``job`` ever reached a runner (PD16's one predicate, used by arms 4 and 5)."""
    return bool(job.get('runner')) or (job.get('minutes') or 0) > 0


def cancel_cause(job, jobs, conclusion, superseded=False):
    """The cause of ``job``'s cancel — one of :data:`CAUSES` — or None when ``job`` did not end
    ``cancelled``. ``jobs`` is every job row of the same run (``job`` included), ``conclusion``
    the run's own, ``superseded`` whether a newer run on its branch threw this one away.

    Each arm is tried in order; the first that holds is the answer.
    """
    if job.get('conclusion') != CANCELLED:
        return None
    limit = job.get('limit')
    if limit is not None and (job.get('minutes') or 0) + TIMEOUT_SLACK_MIN >= limit:
        return TIMEOUT
    if any(j.get('conclusion') in RED for j in jobs) or conclusion in RED:
        return FAILURE
    if superseded:
        return FAILURE
    if not _started(job):
        return FAILURE
    if all(j.get('conclusion') == CANCELLED for j in jobs if _started(j)):
        return FAILURE
    if limit is not None:
        return RUNNER_LOSS
    return FAILURE
