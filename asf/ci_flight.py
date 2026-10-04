"""asf.ci_flight — may the factory re-push this branch right now?

A factory push that carries no new content — a rebase onto a moved trunk, a reword, a sign-off
trailer, a rebuild that drops trunk copies — supersedes whatever CI run the branch has going.
The run's queued and started jobs are thrown away and the PR goes back to the end of the runner
queue. A product's S1 PR #858 was superseded four times in forty minutes that way and never once
reached a verdict (F-0203).

So: while a branch has a run in flight, such a push waits — unless the push is the only thing
that can make the PR mergeable at all (:data:`CONFLICT`, :data:`RED`, :data:`STRICT`), and for an
S1 or hotfix branch unless it is :data:`CONFLICT`. Nothing here decides *whether* a rewrite is
wanted; each call site has already decided that. This module answers only *now, or next pass*.

A session's own push is never asked about: it carries the work, and :func:`asf.workers.lifecycle.
publish` is its route. The guard sits at the rewrite sites, never in the funnel they share.
"""
from asf import gh_limit, github

#: the PR does not merge cleanly into the trunk — its run is against a head that cannot land
CONFLICT = 'conflicting'
#: a required check is red on this very head, and this push is its repair
RED = 'a required check is red on this head'
#: the host requires an up-to-date branch and every required check is green (merge time only)
STRICT = 'the host requires an up-to-date branch'
#: the exceptions an S1 or hotfix branch admits while a run is in flight (F-0203 C4)
URGENT_EXCEPTIONS = (CONFLICT,)

CI_FLIGHT_TIMEOUT_S = 30
#: the one line a deferred rewrite writes, whatever the site (F-0203 C11)
DEFER_FMT = '{what} deferred: {branch} CI in flight (run {run})'
#: the line when the host could not say whether a run is in flight: Unknown is never "nothing
#: in flight", so the rewrite waits — whatever exception it claims
UNKNOWN_FMT = '{what} deferred: {branch} CI in flight unknown ({why})'


def run_in_flight(product, branch, run=None, timeout=CI_FLIGHT_TIMEOUT_S):
    """The newest run of the product's ``ci.workflow`` on ``branch`` the host has not completed:
    ``{'id': <int>, 'status': <str>}``; None when nothing is in flight or no ``ci.workflow`` /
    ``repo_slug`` is configured (no CI to wait for); an Unknown :class:`asf.github.Result` when
    ``gh`` failed, timed out, was rate limited or printed something unparsable — never read as
    "nothing in flight". Read with the product's own login (:func:`asf.ci_pool._gh_env`) through
    :func:`asf.github.gh`. Never raises."""
    ci = product.ci if isinstance(getattr(product, 'ci', None), dict) else {}
    workflow = ci.get('workflow')
    repo_slug = getattr(product, 'repo_slug', None)
    if not workflow or not repo_slug or not branch:
        return None
    from asf import ci_pool
    try:
        r = github.gh(['run', 'list', '-R', repo_slug, '--workflow', workflow, '--branch', branch,
                       '--limit', '20', '--json', 'databaseId,status,createdAt'],
                      json=True, timeout=timeout, run=run, env=ci_pool._gh_env(product))
    except gh_limit.RateLimited:
        return github.unknown('rate limited')
    if not r.ok:
        return r
    rows = r.data
    if not isinstance(rows, list):
        return github.unknown('bad json')
    live = [r for r in rows if isinstance(r, dict) and r.get('status') != 'completed'
            and r.get('databaseId')]
    if not live:
        return None
    newest = max(live, key=lambda r: (str(r.get('createdAt') or ''), int(r.get('databaseId') or 0)))
    return {'id': newest['databaseId'], 'status': newest.get('status') or ''}


def is_unknown(run):
    """True when :func:`run_in_flight`'s answer is Unknown (the host could not say)."""
    return isinstance(run, github.Result) and run.unknown


def urgent(branch, severity=None, item=None, items=None):
    """True when this is an S1 or hotfix branch — :func:`asf.ci_queue.priority`'s tier 0, which
    is the tree's one definition of it (C5). With ``items`` the answer is that function's;
    without, it is ``hotfix`` in ``branch`` or ``severity == 'S1'``, the two a ``pool.Row``
    carries."""
    if items:
        from asf import ci_queue
        return ci_queue.priority(item, items, branch=branch)[0] == ci_queue.S1
    return 'hotfix' in (branch or '').lower() or severity == 'S1'


def verdict(product, branch, what, needed=None, severity=None, item=None, items=None,
            flight=None):
    """``''`` when the rewrite may be pushed now, else the one line it is deferred with.

    ``what`` is the verb the line names (``rebase``, ``reword``, ``sign-off``, ``update``);
    ``needed`` is the exception the site is claiming, or None. With no run in flight the answer is
    always ``''`` — this is the only question asked, and the ``gh`` read behind it is the only
    cost added to a pass with nothing in flight.

    |  run in flight | urgent | ``needed`` | verdict |
    | --- | --- | --- | --- |
    | no  | —   | —                    | push |
    | yes | no  | None                 | **defer** |
    | yes | no  | CONFLICT/RED/STRICT  | push |
    | yes | yes | CONFLICT             | push |
    | yes | yes | RED/STRICT           | **defer** |
    | unknown | — | —                  | **defer** |

    An Unknown read is never "nothing in flight": the rewrite waits for a pass that can see.
    """
    flight = flight or Flight()
    run = flight.read(product, branch)
    if is_unknown(run):
        return UNKNOWN_FMT.format(what=what, branch=branch, why=run.reason or 'unknown')
    if not run:
        return ''
    allowed = URGENT_EXCEPTIONS if urgent(branch, severity, item, items) else (CONFLICT, RED,
                                                                                STRICT)
    # needed=None never matches; kept as belt and braces against a future None entering allowed.
    if needed in allowed and needed is not None:
        return ''
    return DEFER_FMT.format(what=what, branch=branch, run=run['id'])


class Flight:
    """The per-pass reader a caller holds: one :func:`run_in_flight` per branch asked about,
    remembered for the pass. A test substitutes any object with the same ``read(product,
    branch)``."""

    _MISSING = object()

    def __init__(self):
        self._seen = {}

    def read(self, product, branch):
        key = (getattr(product, 'name', None) or id(product), branch)
        got = self._seen.get(key, self._MISSING)
        if got is self._MISSING:
            got = run_in_flight(product, branch)
            self._seen[key] = got
        return got
