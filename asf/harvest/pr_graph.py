"""asf.harvest.pr_graph — every open PR's head checks in ONE GraphQL query per tick.

``pr_checks`` used to spend two REST calls per open PR (``gh pr checks`` and
``commits/<sha>/check-runs``) every time the lane asked, so a repo with a dozen open PRs burnt
two dozen core calls a tick on the shared token. One ``repository.pullRequests`` query returns,
for all open PRs, the head sha, labels, mergeable state and the head commit's check runs from
EVERY event (``checkSuites`` → ``checkRuns``: a ``workflow_dispatch`` run the CI queue started
counts) and its commit status contexts, as the same ``gh pr checks`` rows
(``name, bucket, link, workflow, startedAt``) the REST path builds. The judgement over them
(exact head, newest run per workflow and name, ``heavy_after_review`` crediting) stays in
:func:`asf.harvest.lane.pr_checks` and its callers, unchanged.

Read once per process (:mod:`asf.gh_limit`'s memo). A PR the snapshot cannot vouch for — not in
it, another head than asked, or a page cut off (more suites/runs than one page) — is *absent*:
the caller reads it the old REST way. A failed query is absent for every PR; a rate limit is
never absent — :class:`asf.gh_limit.RateLimited` propagates.
"""
import json
import re

from asf import gh_limit
from asf.harvest import harvest as H

#: PRs, check suites per head and check runs per suite one query reads; anything cut off marks
#: that PR incomplete (read by REST) rather than judged on a partial list
PRS, SUITES, RUNS = 30, 20, 50

QUERY = '''query($owner:String!,$name:String!){repository(owner:$owner,name:$name){
pullRequests(states:OPEN,first:%d,orderBy:{field:UPDATED_AT,direction:DESC}){
pageInfo{hasNextPage}
nodes{number headRefOid mergeable labels(first:20){nodes{name}}
commits(last:1){nodes{commit{oid
status{contexts{context state targetUrl createdAt}}
checkSuites(first:%d){pageInfo{hasNextPage}
nodes{workflowRun{workflow{name}}
checkRuns(first:%d){pageInfo{hasNextPage}
nodes{name status conclusion startedAt detailsUrl}}}}}}}}}}}''' % (PRS, SUITES, RUNS)

#: a check run's conclusion -> the ``gh pr checks`` bucket (anything else completed is ``fail``)
_RUN_BUCKETS = {'success': 'pass', 'skipped': 'skipping', 'neutral': 'skipping',
                'cancelled': 'cancel', 'stale': 'pending'}
_CTX_BUCKETS = {'success': 'pass', 'failure': 'fail', 'error': 'fail', 'pending': 'pending',
                'expected': 'pending'}


def _nodes(conn):
    got = conn.get('nodes') if isinstance(conn, dict) else None
    return [n for n in got if isinstance(n, dict)] if isinstance(got, list) else []


def _more(conn):
    return bool(((conn or {}).get('pageInfo') or {}).get('hasNextPage'))


def rows_of(pr):
    """``(head, rows, complete)`` for one ``pullRequests`` node: the head commit's ``gh pr
    checks`` rows — every event's check runs, then the status contexts — and False when a page
    was cut off."""
    commits = _nodes(pr.get('commits'))
    commit = commits[-1] if commits else {}
    commit = commit.get('commit') if isinstance(commit.get('commit'), dict) else {}
    complete = True
    rows = []
    suites = commit.get('checkSuites')
    complete &= not _more(suites)
    for suite in _nodes(suites):
        wf = ((suite.get('workflowRun') or {}).get('workflow') or {}).get('name') or ''
        runs = suite.get('checkRuns')
        complete &= not _more(runs)
        for r in _nodes(runs):
            if not r.get('name'):
                continue
            done = str(r.get('status') or '').lower() == 'completed'
            bucket = _RUN_BUCKETS.get(str(r.get('conclusion') or '').lower(), 'fail') \
                if done else 'pending'
            rows.append({'name': r['name'], 'bucket': bucket, 'link': r.get('detailsUrl') or '',
                         'workflow': wf, 'startedAt': r.get('startedAt') or ''})
    status = commit.get('status') if isinstance(commit.get('status'), dict) else {}
    for c in status.get('contexts') or ():
        if isinstance(c, dict) and c.get('context'):
            rows.append({'name': c['context'],
                         'bucket': _CTX_BUCKETS.get(str(c.get('state') or '').lower(), 'pending'),
                         'link': c.get('targetUrl') or '', 'workflow': '',
                         'startedAt': c.get('createdAt') or ''})
    seen, out = set(), []
    for r in rows:  # one row per link (a run listed under two suites is one check)
        if r['link'] and r['link'] in seen:
            continue
        seen.add(r['link'])
        out.append(r)
    return commit.get('oid') or pr.get('headRefOid'), out, complete


def snapshot(slug):
    """``{number: {'head', 'labels', 'mergeable', 'checks', 'complete'}}`` for the repo's open
    PRs — one GraphQL call, reused for :data:`asf.gh_limit.MEMO_S` — or None when the query
    failed (the caller reads per PR over REST). Raises :class:`asf.gh_limit.RateLimited`."""
    key = ('pr-graph', slug)
    hit = gh_limit.memo_get(key)
    if hit is not None:
        return hit
    owner, _, name = str(slug).partition('/')
    if not owner or not name:
        return None
    rc, stdout, _err = H._gh(['api', 'graphql', '-f', f'query={QUERY}', '-F', f'owner={owner}',
                              '-F', f'name={name}'])
    try:
        data = json.loads(stdout) if rc == 0 and stdout.strip() else None
        prs = data['data']['repository']['pullRequests']
        if data.get('errors') or not isinstance(prs.get('nodes'), list):
            return None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    out = {}
    for pr in _nodes(prs):
        head, rows, complete = rows_of(pr)
        if pr.get('number') is None:
            continue
        out[int(pr['number'])] = {
            'head': head, 'checks': rows, 'complete': complete,
            'mergeable': pr.get('mergeable'),
            'labels': [n.get('name') for n in _nodes(pr.get('labels')) if n.get('name')]}
    gh_limit.memo_put(key, out)
    return out


def checks_for(slug, number, head):
    """PR ``number``'s rows at exactly ``head`` from the snapshot, or None when the snapshot
    cannot vouch for them (no snapshot, PR not in it, another head, or a cut-off page)."""
    try:
        num = int(number)
    except (TypeError, ValueError):
        return None
    if not head:
        return None
    got = (snapshot(slug) or {}).get(num)
    if not got or got['head'] != head or not got['complete']:
        return None
    return [dict(r) for r in got['checks']]
