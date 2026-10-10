"""asf.kernel.gate — the kernel's proof gate, each criterion from the record (ASF 0.2).

``asf kernel gate --product P`` reads the ``kernel.gate`` block (:mod:`asf.kernel.settings`) and
prints one row per criterion, PASS or FAIL, exit 0 only when every one passes:

- **silent stuck** (≤ ``silent_stuck_max``): Stuck items in the last tick's plan with no reason
  or no owner. **owned stuck** is shown beside it (information, never a FAIL).
- **first-push green** (≥ ``first_push_green_min``): of the PRs a kernel build session opened in
  the window (the last ``window_h`` hours, never before ``since``), the share whose first pushed
  head had every required check (``conventions.landing_checks``; every check when none is named)
  conclude ``success``. A kernel build session is a ``build-<item>-<epoch>`` row on the session
  ledger; its PR is the one on its branch created after it started. The first pushed head is the
  session's first logged push (:mod:`asf.workers.pushlog`), else the PR's last commit made before
  it was opened. A verdict is cached per PR in ``state/<p>/kernel-gate.json`` once every
  required check on that head has concluded, so GitHub is asked once per PR.
- **landed** (≥ ``landed_min``): distinct Tasks and Bugs whose kernel PR merged since ``since``
  (the window's start when ``since`` is unset).
"""
import datetime
import json
import os
import re

from asf.kernel import settings as S
from asf.kernel.model import OWNERS

CACHE_FILE = 'kernel-gate.json'

#: a kernel build session's job id (:meth:`asf.kernel.ports.RealSessions.launch`)
KERNEL_JOB = re.compile(r'^build-[a-z]+-\d+-\d+$')

#: the item id prefixes the landed count takes (Tasks and Bugs)
LANDED_PREFIXES = ('T-', 'B-')


def silent_stuck(plan):
    """``(silent, owned)``: Stuck rows of a saved plan without a reason or a known owner, and
    the rest."""
    silent = owned = 0
    for row in (plan or {}).get('states', {}).values():
        if row.get('state') != 'stuck':
            continue
        if str(row.get('reason') or '').strip() and row.get('owner') in OWNERS:
            owned += 1
        else:
            silent += 1
    return silent, owned


def kernel_builds(rows, start):
    """The session-ledger rows of kernel build sessions started at or after ``start``."""
    out = []
    for r in rows:
        t = S.parse_time(r.get('started'))
        if KERNEL_JOB.match(str(r.get('job') or '')) and r.get('branch') and t and t >= start:
            out.append(dict(r, _started=t))
    return out


def kernel_prs(builds, prs):
    """``[(build row, pr)]``: each build's PR — on its branch, created after it started, the
    first such."""
    out, taken = [], set()
    for b in sorted(builds, key=lambda r: r['_started']):
        cands = sorted((p for p in prs if p.get('headRefName') == b['branch']
                        and p['number'] not in taken
                        and (S.parse_time(p.get('createdAt')) or b['_started']) >= b['_started']),
                       key=lambda p: p['number'])
        if cands:
            taken.add(cands[0]['number'])
            out.append((b, cands[0]))
    return out


def head_verdict(checks, required):
    """True (green), False (red) or None (not concluded) for one head's check runs."""
    by_name = {}
    for c in checks:
        by_name.setdefault(c.get('name'), []).append(c)
    names = list(required) or [n for n, cs in by_name.items()
                               if any(c.get('conclusion') not in ('skipped', 'neutral') for c in cs)]
    if not names:
        return None
    for n in names:
        cs = by_name.get(n) or []
        if not cs or any(c.get('status') != 'completed' for c in cs):
            return None
        if not any(c.get('conclusion') == 'success' for c in cs):
            return False
    return True


class GitHub:
    """The three reads the gate makes (one ``gh`` per call; a test hands its own ``run``)."""

    def __init__(self, product, run=None):
        self.slug, self.product, self._run = product.repo_slug, product, run

    def _gh(self, args):
        from asf import ci_pool, github
        r = github.gh(args, json=True, env=ci_pool._gh_env(self.product), run=self._run)
        return r.data if r.ok else None

    def prs(self, since):
        return self._gh(['pr', 'list', '-R', self.slug, '--state', 'all', '--limit', '500',
                         '--search', 'created:>=%s' % since.strftime('%Y-%m-%d'),
                         '--json', 'number,headRefName,createdAt,mergedAt']) or []

    def first_head(self, pr):
        """The PR's last commit made before it was opened (the first push's head)."""
        commits = self._gh(['api', 'repos/%s/pulls/%d/commits' % (self.slug, pr['number'])]) or []
        opened = S.parse_time(pr.get('createdAt'))
        before = [c for c in commits
                  if (S.parse_time(((c.get('commit') or {}).get('committer') or {}).get('date'))
                      or opened) <= opened] if opened else commits
        return (before or commits or [{}])[-1].get('sha')

    def checks(self, sha):
        data = self._gh(['api', 'repos/%s/commits/%s/check-runs?per_page=100' % (self.slug, sha)])
        return (data or {}).get('check_runs') or []


def _cache(state_dir, data=None):
    path = os.path.join(state_dir, CACHE_FILE)
    if data is None:
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(path + '.tmp', path)
    return data


def evaluate(product, gh, rows, plan, state_dir, now=None, pushes=None):
    """The gate's rows ``[(criterion, value, target, ok)]`` (``ok`` None: information only)."""
    from asf.trunk_ruleset import required_contexts
    now = now or datetime.datetime.now(datetime.timezone.utc)
    g = product.kernel['gate']
    since = g['since']
    start = now - datetime.timedelta(hours=float(g['window_h']))
    if since and since > start:
        start = since
    land_from = since or start
    prs = gh.prs(min(start, land_from))
    cache = _cache(state_dir)
    required = required_contexts(product)
    green = judged = 0
    for b, pr in kernel_prs(kernel_builds(rows, min(start, land_from)), prs):
        if (S.parse_time(pr.get('createdAt')) or now) < start:
            continue
        key = str(pr['number'])
        if key not in cache:
            first = (pushes(b['job']) if pushes else []) or []
            sha = first[0] if first else gh.first_head(pr)
            verdict = head_verdict(gh.checks(sha), required) if sha else None
            if verdict is None:
                continue
            cache[key] = {'sha': sha, 'green': verdict, 'item': b.get('item')}
        judged += 1
        green += bool(cache[key]['green'])
    _cache(state_dir, cache)
    landed = {b.get('item') for b, pr in kernel_prs(kernel_builds(rows, land_from), prs)
              if (S.parse_time(pr.get('mergedAt')) or start) >= land_from and pr.get('mergedAt')
              and str(b.get('item') or '').startswith(LANDED_PREFIXES)}
    silent, owned = silent_stuck(plan)
    share = green / judged if judged else 0.0
    return [
        ('silent stuck', str(silent), '≤ %d' % g['silent_stuck_max'],
         silent <= g['silent_stuck_max']),
        ('owned stuck', str(owned), '-', None),
        ('first-push green', '%d/%d = %.0f%%' % (green, judged, 100 * share),
         '≥ %.0f%%' % (100 * g['first_push_green_min']),
         judged > 0 and share >= g['first_push_green_min']),
        ('landed Tasks/Bugs', str(len(landed)), '≥ %d' % g['landed_min'],
         len(landed) >= g['landed_min']),
    ], start, land_from


def render(rows, start, land_from):
    out = ['window from %s; landed since %s' % (start.strftime('%Y-%m-%dT%H:%MZ'),
                                                land_from.strftime('%Y-%m-%dT%H:%MZ')), '',
           '| criterion | value | target | verdict |', '| --- | --- | --- | --- |']
    out += ['| %s | %s | %s | %s |' % (c, v, t, '-' if ok is None else ('PASS' if ok else 'FAIL'))
            for c, v, t, ok in rows]
    return '\n'.join(out)


def gate(product, out=print, gh=None, now=None, state_dir=None):
    """``asf kernel gate``: print the table; 0 when every criterion passes, else 1."""
    from asf import env
    from asf.kernel.loop import PLAN_FILE
    from asf.workers import pool, pushlog
    state_dir = state_dir or os.path.join(env.ASF_HOME, 'state', product.name)
    try:
        with open(os.path.join(state_dir, PLAN_FILE), encoding='utf-8') as f:
            plan = json.load(f)
    except (OSError, ValueError):
        plan = {}
    rows = list(pool.load_sessions(product).values())
    table, start, land_from = evaluate(product, gh or GitHub(product), rows, plan, state_dir,
                                       now=now, pushes=lambda job: pushlog.shas(product, job))
    out(render(table, start, land_from))
    return 0 if all(ok is not False for _c, _v, _t, ok in table) else 1
