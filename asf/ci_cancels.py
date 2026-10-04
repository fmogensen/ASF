"""asf.ci_cancels — every cancelled CI run gets exactly one cause (F-0230).

The four cancels the factory makes with its own ``gh run cancel`` — relief, step-silence stall,
duplicate-push dedupe, and the post-merge cancel — write a durable claim the moment they are made
(:func:`asf.ci_queue.claim_cancel`, landed under #482): the factory's own cancels are claimed by
the code that made them and never inferred. Every other cancel is the host's, and is told apart by
the pair of heads either side of it — a content-free re-push (a rebase, a reword, a sign-off) named
``rewrite``, a real push named ``newhead``, and a head the clone cannot resolve named
``unresolved`` rather than guessed.

The ledger this module reads is the one the factory already writes: ``asf.ci_queue`` owns
``CANCELS_FILE``, ``claim_cancel`` and ``load_claims``; this module claims nothing of its own and
mints no second ledger. ``rewrite`` is the number F-0203 — already approved and planned — drives
to zero.
"""
import json
import os
import re
import subprocess
import datetime

from asf import env

#: every cause a cancelled run can have, in the order they are tried: the factory's own claims
#: first (it knows), then the run's own limit, then the two inferences, then the head pair, then
#: the honest nothing. First match wins; a run has exactly one.
CAUSES = ('relief', 'stall', 'dedupe', 'merged', 'timeout', 'rewrite', 'newhead',
          'unresolved', 'unclaimed')

#: what the operator does about each. `wasted` is the one this card exists to drive to zero.
GROUPS = {
    'traded':  ('relief', 'stall'),        # thrown away on purpose, to buy the trunk its runners
    'saved':   ('dedupe', 'merged'),       # the run was moot; the cancel kept the minutes
    'wasted':  ('rewrite',),               # a content-free re-push cut it short — nobody chose it
    'sound':   ('newhead', 'timeout'),     # the head is gone, or the job hit its own limit
    'unknown': ('unresolved', 'unclaimed'),
}

#: the landed cause strings that are the factory's own word about a cancel it made (F-0203 PD2),
#: plus the host's own annotation for a job that hit its declared limit — stronger evidence than
#: any supersede rule, so it is honoured as a claim rather than re-read from the head pair.
HONOURED = {'relief': 'relief', 'stall': 'stall', 'duplicate-push': 'dedupe',
            'merged-pr': 'merged', 'job-timeout': 'timeout',
            # a timeout read as runner contention (asf.ci_queue._contention): still the job's limit
            'contention-rerun': 'timeout', 'contention-alarm': 'timeout',
            'contention-refused': 'timeout'}

#: the landed cause strings that are `explain_cancels`' own readings of the evidence, not a claim
#: of authorship, and are re-classified here from the head pair instead. `superseded` in
#: particular is never honoured: it asks only whether a later run exists, never what that run's
#: head carried, which is the weak reading this card exists to replace.
REREAD = frozenset({'superseded', 'cancelled-left', 'orphan-rerun', 'orphan-refused'})

#: the footer goes RED at or above this share of cancelled runner-minutes spent on `rewrite`.
WASTED_ALERT_PCT = 20


def claims(state_dir):
    """The factory's own claims for cancels it made, ``{run id: claim}`` — a thin read of
    :func:`asf.ci_queue.load_claims`, dropping any value that is not a dict or carries no
    ``cause``. Never raises: ``load_claims`` already returns ``{}`` for a missing, unreadable or
    non-dict file, and this adds no second failure mode. Written by
    :func:`asf.ci_queue.claim_cancel`; a claim older than ``ci_queue.CLAIM_TTL_S`` (two days) is
    pruned on the next write, so a window wider than that reads empty here."""
    from asf import ci_queue
    got = ci_queue.load_claims(state_dir)
    return {k: v for k, v in got.items() if isinstance(v, dict) and v.get('cause')}


def _parse_ts(stamp):
    """An ISO-8601 stamp (``Z``, ``+00:00`` or naive) as an aware ``datetime``, or None for
    anything unparsable — the classifier's timestamps are always compared as aware values or not
    at all."""
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    s = stamp.strip()
    if s[-1:] in ('Z', 'z'):
        s = s[:-1] + '+00:00'
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def head_kind(repo, old, new, trunk='origin/main', fetch=True, run=None):
    """``'rewrite'`` when ``new`` is ``old`` re-pushed with no new work — a rebase onto a moved
    trunk, a reword, a sign-off trailer, a rebuild that dropped trunk copies; ``'newhead'`` when
    it carries a commit ``old`` did not; ``'unresolved'`` when the clone cannot resolve one of
    them, or any git read fails. Never raises.

    The rule, measured on all four shapes (F-0230 P8): nothing of ``old``'s was dropped
    (``git cherry <new> <old>`` has no ``+``) **and** everything ``new`` gained is already on the
    trunk (every ``+`` of ``git cherry <old> <new>`` is an ancestor of ``trunk``). A rebase gains
    exactly the trunk's own commits; a reword and a sign-off gain nothing; a real push gains a
    commit that is not on the trunk.

    A sha the clone lacks is fetched once by sha (``git fetch -q origin <sha>``) and re-checked,
    the shape :func:`asf.workers.lifecycle.lost_commits` keeps for the same question; ``fetch``
    False skips even that.
    """
    if not old or not new:
        return 'unresolved'
    if old == new:
        return 'rewrite'
    if not repo or not os.path.isdir(repo):
        return 'unresolved'
    run = run or subprocess.run

    def git(args):
        try:
            return run(['git', *args], cwd=repo, capture_output=True, text=True)
        except OSError:
            return None

    def resolved(sha):
        p = git(['cat-file', '-e', f'{sha}^{{commit}}'])
        if p is not None and p.returncode == 0:
            return True
        if fetch:
            git(['fetch', '-q', 'origin', sha])
            p = git(['cat-file', '-e', f'{sha}^{{commit}}'])
            if p is not None and p.returncode == 0:
                return True
        return False

    if not resolved(old) or not resolved(new):
        return 'unresolved'

    lost = git(['cherry', new, old])
    if lost is None or lost.returncode != 0:
        return 'unresolved'
    if any(ln.startswith('+') for ln in lost.stdout.splitlines()):
        return 'newhead'

    gained = git(['cherry', old, new])
    if gained is None or gained.returncode != 0:
        return 'unresolved'
    for ln in gained.stdout.splitlines():
        if not ln.startswith('+'):
            continue
        sha = ln.split()[1]
        anc = git(['merge-base', '--is-ancestor', sha, trunk])
        if anc is None or anc.returncode != 0:
            return 'newhead'
    return 'rewrite'


def cause(run, claims, runs, timeouts, prs, repo, trunk, fetch):
    """One member of :data:`CAUSES` for one cancelled run ``run``, first match wins, in
    :data:`CAUSES`' order (C4): the ledger's own claim (:data:`HONOURED` beats :data:`REREAD`
    beats a stray — F-0230 PD2, PD3), the run's own job limit (``timeout``, tried before every
    supersede rule), an inferred merge, an inferred dedupe, then the head pair of the run that
    superseded it (:func:`head_kind`). ``claims`` is :func:`claims`'s return; ``runs`` is every
    run of the window (not only the cancelled ones), so a superseding run that itself concluded
    green is still found; ``timeouts`` is a ``{job name: minutes}`` dict
    (:func:`asf.ci_pool.parse_timeouts`), a job with none on the host's own
    :data:`asf.ci_pool.DEFAULT_JOB_TIMEOUT_MIN`; ``prs`` is ``{pr number: merged_at or None}``.
    ``trunk`` is the bare trunk branch name (``product.main``, e.g. ``'main'``); ``head_kind`` is
    called with ``f'origin/{trunk}'``.

    Returns ``(cause, stray)``: ``stray`` is the claimed cause string neither :data:`HONOURED`
    nor :data:`REREAD` knows, else None — PD3's count of a landed cause this module has not been
    told the partition of.
    """
    rid = run.get('id')
    claim = claims.get(str(rid)) if rid is not None else None
    if claim:
        claimed = claim.get('cause')
        if claimed in HONOURED:
            return HONOURED[claimed], None
        if claimed not in REREAD:
            return 'unclaimed', claimed

    from asf import ci_pool
    for job in run.get('jobs') or ():
        started, completed = _parse_ts(job.get('started_at')), _parse_ts(job.get('completed_at'))
        if started is None or completed is None:
            continue
        limit = timeouts.get(job.get('name'), ci_pool.DEFAULT_JOB_TIMEOUT_MIN)
        if (completed - started).total_seconds() / 60 >= limit - 1:
            return 'timeout', None

    updated = _parse_ts(run.get('updated_at'))
    pr = run.get('pr')
    if pr is not None and updated is not None:
        merged_at = _parse_ts(prs.get(pr))
        if merged_at is not None and merged_at <= updated:
            return 'merged', None

    if run.get('event') == 'push' and run.get('head_branch') != trunk:
        sha = run.get('head_sha')
        if sha and any(r2.get('event') == 'pull_request' and r2.get('head_sha') == sha
                        and r2.get('id') != rid for r2 in runs):
            return 'dedupe', None

    created = _parse_ts(run.get('created_at'))
    if created is not None and updated is not None:
        later = []
        for r2 in runs:
            if r2.get('id') == rid or r2.get('head_branch') != run.get('head_branch'):
                continue
            c2 = _parse_ts(r2.get('created_at'))
            if c2 is not None and created < c2 <= updated:
                later.append((c2, r2))
        if later:
            later.sort(key=lambda pair: pair[0])
            newer = later[0][1]
            return head_kind(repo, run.get('head_sha'), newer.get('head_sha'),
                              trunk=f'origin/{trunk}', fetch=fetch), None

    return 'unclaimed', None


def _group(cause_name):
    for group, causes in GROUPS.items():
        if cause_name in causes:
            return group
    return None


def classify(runs, claims, timeouts, prs, repo, trunk, fetch=True):
    """:func:`cause` over every cancelled run of ``runs`` — ``{'rows': [...], 'strays': n}``,
    one record per cancelled run carrying its id, branch, head sha, cause, group and cancelled
    minutes, plus PD3's stray count. Pure over its inputs apart from :func:`head_kind`'s git
    reads: ``runs``, ``claims``, ``timeouts`` and ``prs`` are all passed in, so this needs no
    ``gh`` and no clock. It sorts by nothing and dedupes nothing — the table owns presentation."""
    rows, strays = [], 0
    for r in runs:
        if r.get('conclusion') != 'cancelled':
            continue
        c, stray = cause(r, claims, runs, timeouts, prs, repo, trunk, fetch)
        if stray is not None:
            strays += 1
        rows.append({'run': r.get('id'), 'branch': r.get('head_branch'), 'sha': r.get('head_sha'),
                      'cause': c, 'group': _group(c), 'minutes': r.get('cancelled_minutes', 0) or 0})
    return {'rows': rows, 'strays': strays}


def _apportion(minutes):
    """Integer percentages of ``minutes``' values (one per :data:`CAUSES` name) that sum to
    exactly 100 when their total is non-zero — the largest-remainder method, so the table's
    ``share`` column always adds up rather than drifting a point off from independent rounding.
    Every share is 0 when no minutes were spent."""
    total = sum(minutes.values())
    if not total:
        return {c: 0 for c in minutes}
    raw = {c: (m / total) * 100 for c, m in minutes.items()}
    floors = {c: int(v) for c, v in raw.items()}
    remainder = 100 - sum(floors.values())
    ranked = sorted(minutes, key=lambda c: raw[c] - floors[c], reverse=True)
    shares = dict(floors)
    for c in ranked[:remainder]:
        shares[c] += 1
    return shares


def rows(classification):
    """One row per cause in :data:`CAUSES` order, always — zeros included, so a cause that never
    appears is a cause the operator can see is zero and ``unclaimed`` at zero is this card's own
    success condition. Each row is ``{cause, group, runs, minutes, share}``; ``share`` is of
    runner-minutes, not of runs, because runners are what the card is spending
    (:func:`_apportion`)."""
    counts = {c: 0 for c in CAUSES}
    minutes = {c: 0 for c in CAUSES}
    for r in classification['rows']:
        c = r['cause']
        counts[c] += 1
        minutes[c] += r.get('minutes') or 0
    shares = _apportion(minutes)
    return [{'cause': c, 'group': _group(c), 'runs': counts[c], 'minutes': minutes[c],
             'share': shares[c]} for c in CAUSES]


def render(product_name, clause, table, strays=0):
    """The spec's §4 table (F-0230): the registry's own first line
    (:func:`asf.views.header.head`), one row per :data:`CAUSES` cause in order with the numeric
    columns right-aligned, then the footer — ``RED`` at or above :data:`WASTED_ALERT_PCT` naming
    ``rewrite`` as the waste and F-0203 (already approved and planned) as its cut, else ``OK``,
    the ``unknown`` share printed beside it either way, and PD3's stray count named when it is
    non-zero — a landed cause string this module has not been told the partition of."""
    from asf.views import header
    lines = [header.head('ci cancels', product_name, clause), '',
             '| cause      | group   | runs | minutes | share |',
             '| ---------- | ------- | ---: | ------: | ----: |']
    for row in table:
        lines.append(f"| {row['cause']:<10} | {row['group']:<7} | {row['runs']:>4} | "
                      f"{row['minutes']:>7} | {row['share']:>3} % |")
    wasted = sum(row['share'] for row in table if row['group'] == 'wasted')
    unknown = sum(row['share'] for row in table if row['group'] == 'unknown')
    status = 'RED' if wasted >= WASTED_ALERT_PCT else 'OK'
    footer = (f"{status} — {wasted} % of the cancelled runner-minutes were thrown away by a "
              f"content-free re-push (cause `rewrite`); F-0203 Tasks 2-4 cut exactly this. "
              f"unknown {unknown} %.")
    if strays:
        footer += (f" strays {strays} — a landed cause string this module does not know the "
                   f"partition of.")
    lines += ['', footer]
    return '\n'.join(lines)


def _run_listing(product, since):
    """Every finished run of ``product``'s CI workflow (``product.ci['workflow']``, P14) since
    ``since`` (a date), each carrying ``event`` — the REST listing P7 names, with the one field
    neither the queue's own ``gh run list`` nor ``metrics.ci_from_api``'s jq carries (F-0230
    PD7). ``pr`` is the first PR number the run belongs to, or None."""
    from asf.metrics import metrics
    workflow = product.ci.get('workflow') if isinstance(product.ci, dict) else None
    runs = metrics.gh_lines(
        ['api', f'repos/{product.repo_slug}/actions/runs?created=%3E%3D{since}'
                '&status=completed&per_page=100', '--paginate', '--jq',
         '.workflow_runs[]|{id,name,path,head_branch,head_sha,conclusion,created_at,updated_at,'
         'run_attempt,event,pr:[.pull_requests[].number]}|@json'])
    out = []
    for r in runs:
        if workflow and os.path.basename(r.get('path') or '') != workflow:
            continue
        pr = r.get('pr') or []
        out.append({**r, 'pr': pr[0] if pr else None})
    return out


def _jobs_of(product, run_id):
    """A cancelled run's own jobs — name, conclusion, ``started_at``, ``completed_at`` — read for
    cancelled runs only (C8): nothing here needs the jobs of a run that concluded ``success``."""
    from asf.metrics import metrics
    return metrics.gh_lines(
        ['api', f'repos/{product.repo_slug}/actions/runs/{run_id}/jobs?per_page=100',
         '--paginate', '--jq', '.jobs[]|{name,conclusion,started_at,completed_at}|@json'])


def _merged_at_cache(product, numbers):
    """``{pr number: merged_at or None}``, one ``gh api`` read per number — never
    ``metrics.pr_info``, which returns ``merged`` as a bool and cannot answer the inferred
    ``merged`` rule's *"at or before"* test (F-0230 PD7)."""
    from asf.metrics import metrics
    out = {}
    for n in numbers:
        d = metrics.gh_json(['api', f'repos/{product.repo_slug}/pulls/{n}'])
        out[n] = (d or {}).get('merged_at')
    return out


def cmd_cancels(args, out=print):
    """``asf ci cancels``: every cancelled run of the window, told apart into one of the nine
    causes, as a table (§4). Writes nothing (C11): no claim, no metrics event, no state file —
    a reading of the claims plus the host plus the clone, and re-reading it tomorrow with a
    longer window gives a different and better answer."""
    from asf import ci_pool
    from asf.metrics import metrics
    product = env.load_product(args.product)
    days = getattr(args, 'days', None) or 2
    since = metrics.days_back(metrics.today(), days)[0]
    runs = _run_listing(product, since)
    cancelled = [r for r in runs if r.get('conclusion') == 'cancelled']
    for r in cancelled:
        r['jobs'] = _jobs_of(product, r['id'])
        r['cancelled_minutes'] = sum(
            metrics.mins(j.get('started_at'), j.get('completed_at'))
            for j in r['jobs'] if j.get('conclusion') == 'cancelled')
    prs = _merged_at_cache(product, {r['pr'] for r in cancelled if r.get('pr') is not None})
    backend = ci_pool.backend_for(product)
    try:
        timeouts = backend.timeouts() if backend is not None else {}
    except ci_pool.BackendError:
        timeouts = {}
    classification = classify(runs, claims(env.state_dir(product)), timeouts, prs,
                               product.repo_dir, product.main,
                               fetch=not getattr(args, 'no_fetch', False))
    table = rows(classification)
    if getattr(args, 'json', False):
        out(json.dumps(table))
        return 0
    total_minutes = sum(row['minutes'] for row in table)
    clause = (f"{days} days, {len(cancelled)} of {len(runs)} runs cancelled, "
              f"{total_minutes} runner-minutes thrown away")
    out(render(product.name, clause, table, classification['strays']))
    return 0
