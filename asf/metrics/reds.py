"""asf.metrics.reds — the red PR runs the operator sees, counted and told apart, and the PR/CI
targets measured over runs rather than days.

**Visible reds.** Every PR run (a ``ci`` stream event with a ``pr`` and no ``batch``) the host
shows as not green — :data:`RED`: ``failure``, ``cancelled``, ``timed_out``, ``startup_failure``
— is one visible red, and each gets exactly one class (:func:`classify_run`), first match wins:

``ours``     a cancel the factory made itself: the cancel ledger
             (:func:`asf.ci_queue.claim_cancel`, read through :func:`asf.ci_cancels.claims`) holds
             a claim for it whose cause is one of ``own_cancel_causes`` (``*``, the default: any
             claim — the ledger is only the factory's word), or the run was ``superseded`` by a
             newer run on its branch. Counted apart, so a fix that turns them into silent skips
             reads as fewer visible reds.
``infra``    the run ended on the runner rather than the code: ``timed_out`` or
             ``startup_failure``, a job cancelled at its own limit or by runner loss
             (:mod:`asf.ci_jobs`), a red whose only non-green jobs never got a runner, or a failed
             step matching ``infra_steps`` (out of memory, the runner's own set-up).
``tooling``  tooling not on the branch: CI ran the branch's own old copy of a tool. The same
             failed step (workflow, job, step) is red on ``tooling_min_prs`` or more distinct PRs
             within ``tooling_window_h`` hours, and green on newer PRs only — every red PR was
             cut before every PR that went green on it (:meth:`_Index.tooling`).
``check``    every failed job is a deterministic repo check (``check_patterns``: rules, sign-off,
             release notes, lint …).
``replay``   the same red replayed on an unchanged head: an earlier run or attempt of the same
             workflow on the same head was already red (a reopen, a blind re-run).
``flaky``    a later run of the same workflow on the same head went green with the failed jobs
             themselves green (a twin that skipped the failing job is no evidence).
``real``     the rest of the failures: the head needs a new commit to go green.
``unclassified`` a cancel nobody claimed and no rule explains (a hand cancel, a stray).

Reds on merge-queue batch refs are queue candidates, not PRs, and are not counted; every re-run
attempt is its own red (each one notified, though the host's run list shows only the latest).
The status row folds ``infra`` into ``flaky`` (neither is the code's); the rest are named when
non-zero.

**Targets** (:func:`targets`), each ``n/a`` for a product with no forge or no such run, each
one breach line past its threshold:

* ``first_pass`` — PRs green on their first head's first attempt over the last
  ``first_pass_window`` PRs, at least ``first_pass_min``;
* ``runner_reds`` — ``infra`` reds in the last ``runner_reds_window`` runs (every run, trunk and
  batch included), at most ``runner_reds_max``;
* ``trunk_green`` — the last ``trunk_green_window`` trunk runs of the required check, all green.

Every key is ``improve.scorecard.throughput.<key>`` (:data:`asf.metrics.throughput.DEFAULTS`);
``off`` disables a target. Pure over its inputs; :func:`load_claims` is the only reader.
"""
import collections
import datetime

from asf.metrics import throughput as tp

#: the run conclusions the host draws as not green
RED = ('failure', 'cancelled', 'timed_out', 'startup_failure')
REAL, FLAKY, INFRA, OURS, CHECK, REPLAY, TOOLING, UNCLASSIFIED = (
    'real', 'flaky', 'infra', 'ours', 'check', 'replay', 'tooling', 'unclassified')
CLASSES = (REAL, REPLAY, TOOLING, CHECK, FLAKY, INFRA, OURS, UNCLASSIFIED)
LABELS = {REAL: 'real (a new commit is needed)', FLAKY: 'flaky (same head green on re-run)',
          INFRA: 'infra (runner loss, no runner, OOM, timeout)', OURS: 'ours (ASF cancelled it)',
          CHECK: 'deterministic check', REPLAY: 'replay (same red, unchanged head)',
          TOOLING: 'tooling not on branch', UNCLASSIFIED: 'unclassified'}
FAILED = ('failure', 'timed_out', 'startup_failure')
#: the job causes (:data:`asf.ci_jobs.CAUSES`) and claim causes that are the runner's, not the code's
RUNNER_JOB_CAUSES = ('timeout', 'runner-loss')
RUNNER_CLAIMS = ('job-timeout', 'contention-rerun', 'contention-alarm', 'contention-refused')
TARGETS = ('first_pass', 'runner_reds', 'trunk_green')
TARGET_NAMES = {'first_pass': 'First PR run green', 'runner_reds': 'Runner-class reds',
                'trunk_green': 'Trunk required check green'}


def _match(text, patterns):
    t = str(text or '').lower()
    return bool(t) and any(str(p).lower() in t for p in patterns or ())


def _key(r):
    return (str(r.get('ts') or ''), r.get('attempt') or 1)


def is_pr_run(r):
    return r.get('pr') is not None and not r.get('batch')


def is_red(r):
    return r.get('conclusion') in RED


def _wf(r):
    return r.get('workflow') or ''


def _started(j):
    return bool(j.get('runner')) or (j.get('minutes') or 0) > 0


def _sigs(r):
    """The failed-step signatures of a run: ``(workflow, job, step)`` per failed job."""
    return {(_wf(r), j.get('name') or '', j.get('failed_step') or '')
            for j in r.get('jobs') or () if j.get('conclusion') in FAILED}


class _Index:
    """What the window says beyond one run: the runs per head and per PR, and per failed-step
    signature the PRs it was red on; per (workflow, job) the PRs it went green on."""

    def __init__(self, runs, base_time=None):
        self.base_time = base_time
        self.red_sha = collections.defaultdict(list)    # sig -> [(time, pr, sha)]
        self.green_sha = collections.defaultdict(list)  # (workflow, job) -> [(time, pr, sha)]
        self.by = collections.defaultdict(list)
        self.red_on = collections.defaultdict(list)     # sig -> [(time, pr)]
        self.green_on = collections.defaultdict(list)   # (workflow, job) -> [(time, pr)]
        self.first_seen = {}                            # pr -> its first run's time
        for r in runs:
            self.by[('sha', r.get('sha'))].append(r)
            self.by[self.pr_key(r)].append(r)
            if not is_pr_run(r):
                continue
            d = tp.to_dt(r.get('ts'))
            if d is None:
                continue
            pr = r['pr']
            self.first_seen[pr] = min(d, self.first_seen.get(pr, d))
            for sig in _sigs(r):
                self.red_on[sig].append((d, pr))
                self.red_sha[sig].append((d, pr, r.get('sha')))
            for j in r.get('jobs') or ():
                if j.get('conclusion') == 'success':
                    self.green_on[(_wf(r), j.get('name') or '')].append((d, pr))
                    self.green_sha[(_wf(r), j.get('name') or '')].append((d, pr, r.get('sha')))

    @staticmethod
    def pr_key(r):
        return ('pr', r.get('pr')) if r.get('pr') is not None else ('branch', r.get('branch'))

    def near(self, r):
        return self.by[('sha', r.get('sha'))] + self.by[self.pr_key(r)]

    def tooling(self, r, cfg):
        """The failed-step signature that reads as tooling not on the branch, else None: red on
        ``tooling_min_prs`` or more PRs within the span, green on PRs it was never red on, and
        every red run's head older than the green ones (all but ``tooling_max_older_share`` of the
        green PRs) — the step works on branches based after it changed and fails on the ones
        based before. A head's age is its trunk base's
        commit time (``base_time(sha)``, read from the clone) when the reader has one, else its
        PR's first run. A defect of the PRs' own spreads across old and new bases alike and does
        not separate."""
        n, hours = cfg.get('tooling_min_prs'), cfg.get('tooling_window_h') or 24
        d = tp.to_dt(r.get('ts'))
        if not n or d is None:
            return None
        span = datetime.timedelta(hours=hours)
        for sig in sorted(_sigs(r)):
            if not sig[2]:
                continue
            red = [(p, sha) for t, p, sha in self.red_sha[sig] if abs(t - d) <= span]
            if len({p for p, _s in red} if red else ()) < n:
                continue
            prs = {p for p, _s in red}
            green = [(p, sha) for t, p, sha in self.green_sha[sig[:2]] if abs(t - d) <= span and p not in prs]
            if green and self._older(red, green, cfg.get('tooling_max_older_share') or 0):
                return sig
        return None

    def _older(self, red, green, share):
        """Every red head older than the green ones — all but at most ``share`` of the green PRs
        (the branch that brought the tool in is based before it, yet carries it)."""
        if self.base_time is not None:
            rb = [self.base_time(sha) for _p, sha in red]
            gb = {}
            for p, sha in green:
                b = self.base_time(sha)
                if b is not None:
                    gb[p] = min(b, gb.get(p, b))
            if all(b is not None for b in rb) and gb:
                newest = max(rb)
                return sum(1 for b in gb.values() if b <= newest) <= share * len(gb)
        newest = max(self.first_seen[p] for p, _s in red)
        gp = {p for p, _s in green}
        return sum(1 for p in gp if self.first_seen[p] <= newest) <= share * len(gp)


def _own(cause, cfg):
    own = cfg.get('own_cancel_causes') or ()
    return bool(cause) and ('*' in own or cause in own)


def classify_run(r, runs, claims, cfg, index=None):
    """``(class, why)`` for one red run ``r`` — ``runs`` the ``ci`` events around it (its head's
    and its PR's), ``claims`` the cancel ledger ``{run id: {cause}}``."""
    index = index or _Index(runs)
    c = r.get('conclusion')
    jobs = r.get('jobs') or ()
    claim = (claims or {}).get(str(r.get('run'))) or {}
    cause = claim.get('cause') if isinstance(claim, dict) else None
    own = cfg.get('own_cancel_causes') or ()
    if c == 'cancelled':
        if _own(cause, cfg):
            return OURS, f'claimed: {cause}'
        if r.get('superseded') and ('superseded' in own or '*' in own):
            return OURS, 'superseded by a newer run on its branch'
        if cause in RUNNER_CLAIMS:
            return INFRA, f'claimed: {cause}'
        hit = next((j for j in jobs if j.get('cause') in RUNNER_JOB_CAUSES), None)
        if hit:
            return INFRA, f"job {hit.get('name')}: {hit.get('cause')}"
        return UNCLASSIFIED, f'cancel with no claim{f" ({cause})" if cause else ""}'
    if c in ('timed_out', 'startup_failure'):
        return INFRA, c
    hit = next((j for j in jobs if j.get('cause') in RUNNER_JOB_CAUSES), None)
    if hit:
        return INFRA, f"job {hit.get('name')}: {hit.get('cause')}"
    failed = [j for j in jobs if j.get('conclusion') in FAILED]
    unstarted = [j for j in jobs if j.get('conclusion') == 'cancelled' and not _started(j)]
    if not failed and unstarted:
        return INFRA, f"job {unstarted[0].get('name')} never got a runner"
    hit = next((j for j in failed if _match(j.get('failed_step'), cfg.get('infra_steps'))), None)
    if hit:
        return INFRA, f"step {hit.get('failed_step')!r}"
    sig = index.tooling(r, cfg)
    if sig:
        return TOOLING, f'{sig[1]}: {sig[2]!r} red across PRs, green on others'
    checks = cfg.get('check_patterns') or ()
    # a failed job with no failed step of its own (a matrix leg stopped by its sibling) says nothing
    named = [j for j in failed if j.get('failed_step')] or failed
    if named and all(_match(j.get('name'), checks) or _match(j.get('failed_step'), checks)
                     for j in named):
        return CHECK, ', '.join(sorted({str(j.get('failed_step') or j.get('name')) for j in named}))[:80]
    same = [x for x in runs if x is not r and x.get('sha') == r.get('sha') and _wf(x) == _wf(r)]
    if any(_key(x) < _key(r) and x.get('conclusion') in FAILED for x in same):
        return REPLAY, 'the same head was already red'
    names = {j.get('name') for j in failed}
    for x in same:
        if _key(x) > _key(r) and x.get('conclusion') == 'success':
            ok = {j.get('name') for j in x.get('jobs') or () if j.get('conclusion') == 'success'}
            if not x.get('jobs') or names <= ok:
                return FLAKY, 'same head green on re-run'
    return REAL, 'the head needs a new commit'


def classify(ci, claims, cfg, pick=is_pr_run, base_time=None):
    """Every red run ``pick`` selects, oldest first, each ``{run, attempt, ts, pr, sha,
    conclusion, cls, why}``. ``base_time(sha)``: when the head's trunk base was committed
    (:func:`git_base_time`), or None — the tooling class's age test."""
    runs = list(ci or ())
    index = _Index(runs, base_time)
    out = []
    for r in sorted((r for r in runs if pick(r) and is_red(r)), key=_key):
        cls, why = classify_run(r, index.near(r), claims, cfg, index)
        out.append({'run': r.get('run'), 'attempt': r.get('attempt') or 1, 'ts': r.get('ts'),
                    'pr': r.get('pr'), 'sha': r.get('sha'), 'conclusion': r.get('conclusion'),
                    'cls': cls, 'why': why})
    return out


def counts(reds):
    c = collections.Counter(x['cls'] for x in reds)
    return dict({k: c.get(k, 0) for k in CLASSES}, total=len(reds))


def last_runs(ci, n, pick=is_pr_run):
    """The last ``n`` runs ``pick`` selects, oldest first."""
    runs = sorted((r for r in ci or () if pick(r)), key=_key)
    return runs[-int(n):] if n else runs


def window_reds(ci, claims, cfg, start, end):
    return [x for x in classify(ci, claims, cfg) if tp._in(x['ts'], start, end)]


# ------------------------------------------------------------------ targets --

def first_pass_window(ci, n):
    """``(green, prs)`` over the last ``n`` PRs (by their first run): green when every run of the
    first head at attempt 1 concluded success."""
    by_pr = collections.defaultdict(list)
    for r in ci or ():
        if is_pr_run(r):
            by_pr[r['pr']].append(r)
    firsts = []
    for runs in by_pr.values():
        runs.sort(key=_key)
        head = [r for r in runs if r.get('sha') == runs[0].get('sha') and (r.get('attempt') or 1) == 1]
        firsts.append((_key(runs[0]), all(r.get('conclusion') == 'success' for r in head)))
    firsts.sort()
    firsts = firsts[-int(n):] if n else firsts
    return sum(1 for _k, g in firsts if g), len(firsts)


def _target(key, status, detail, value=None, limit=None):
    return {'key': key, 'name': TARGET_NAMES[key], 'status': status, 'detail': detail,
            'value': value, 'limit': limit}


def targets(ci, claims, cfg, forge=True, main=None, base_time=None):
    """The three run-window targets' rows (:data:`TARGETS`)."""
    out = []
    lim, n = cfg.get('first_pass_min'), cfg.get('first_pass_window') or 0
    green, prs = first_pass_window(ci, n) if forge else (0, 0)
    if not forge or not prs:
        out.append(_target('first_pass', tp.NA, 'no forge' if not forge else 'no PR CI run'))
    else:
        rate = tp._rate(green, prs)
        out.append(_target('first_pass', tp.ALARM if lim is not None and rate < lim else tp.OK,
                           f'{green}/{prs} of the last {prs} PRs green on the first run ({rate * 100:.0f} %)',
                           rate, None if lim is None else f'≥ {lim:g} over the last {n:g} PRs'))
    mx, n = cfg.get('runner_reds_max'), cfg.get('runner_reds_window') or 0
    runs = last_runs(ci, n, pick=lambda r: True) if forge else []
    if not runs:
        out.append(_target('runner_reds', tp.NA, 'no forge' if not forge else 'no CI run'))
    else:
        ids = {(r.get('run'), r.get('attempt') or 1) for r in runs}
        infra = [x for x in classify(ci, claims, cfg, pick=lambda r: True, base_time=base_time)
                 if (x['run'], x['attempt']) in ids and x['cls'] == INFRA]
        why = '; '.join(f"{x['run']} {x['why']}" for x in infra[-3:])
        out.append(_target('runner_reds', tp.ALARM if mx is not None and len(infra) > mx else tp.OK,
                           f'{len(infra)} runner-class red(s) in the last {len(runs)} runs'
                           + (f': {why}' if why else ''),
                           len(infra), None if mx is None else f'≤ {mx:g} in the last {n:g} runs'))
    n = cfg.get('trunk_green_window')
    trunk = (last_runs(ci, n, pick=lambda r: r.get('branch') == main and r.get('pr') is None
                       and not r.get('batch')) if forge and main and n else [])
    if not trunk:
        out.append(_target('trunk_green', tp.NA, 'off' if not n else 'no trunk run'))
    else:
        red = [r for r in trunk if r.get('conclusion') != 'success']
        out.append(_target('trunk_green', tp.ALARM if red else tp.OK,
                           f'{len(trunk) - len(red)}/{len(trunk)} of the last trunk runs green'
                           + (': red ' + ', '.join(f"{str(r.get('sha') or '')[:7]} {r.get('conclusion')}"
                                                  for r in red[-3:]) if red else ''),
                           len(red), f'the last {n:g} all green'))
    return out


def breach_line(row):
    return (f"metrics: BREACH {row['key']} — {row['detail']}"
            + (f" (limit {row['limit']})" if row.get('limit') else ''))


# ------------------------------------------------------------------ the reading --

def compute(f, now, cfg, days=tp.TREND_DAYS):
    """The reds section of the scorecard: the last 24 h, the last ``reds_window`` PR runs, a
    per-day count per class over ``days`` days, the targets and their breach lines. ``f`` is
    :func:`asf.metrics.throughput.load`'s facts (``ci``, ``claims``, ``forge``, ``main``)."""
    forge = bool(f.get('forge'))
    ci, claims = (f.get('ci') or []) if forge else [], f.get('claims') or {}
    end = tp.to_dt(now) + datetime.timedelta(seconds=1)
    base_time = f.get('base_time')
    reds = classify(ci, claims, cfg, base_time=base_time)
    n = cfg.get('reds_window') or 0
    window = last_runs(ci, n)
    ids = {(r.get('run'), r.get('attempt') or 1) for r in window}
    in_window = [x for x in reds if (x['run'], x['attempt']) in ids]
    day_end = end.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    trend = []
    for k in range(days - 1, -1, -1):
        e = day_end - datetime.timedelta(days=k)
        s = e - datetime.timedelta(days=1)
        trend.append(counts([x for x in reds if tp._in(x['ts'], s, min(e, end))]))
    rows = targets(ci, claims, cfg, forge=forge, main=f.get('main'), base_time=base_time)
    return {'forge': forge, 'runs': len(window),
            'day': counts([x for x in reds if tp._in(x['ts'], end - datetime.timedelta(hours=24), end)]),
            'window': counts(in_window), 'trend': trend, 'targets': rows,
            'unclassified': [x for x in in_window if x['cls'] == UNCLASSIFIED],
            'alarms': [breach_line(r) for r in rows if r['status'] == tp.ALARM]}


def summary(c):
    """``9 (real 3 · flaky 4 · ours 2)`` — flaky folds in infra; replay, tooling, check and
    unclassified are named only when non-zero."""
    parts = [f'real {c[REAL]}', f'flaky {c[FLAKY] + c[INFRA]}', f'ours {c[OURS]}']
    parts += [f'{k} {c[k]}' for k in (REPLAY, TOOLING, CHECK, UNCLASSIFIED) if c[k]]
    return f"{c['total']} ({' · '.join(parts)})"


def render(d):
    if not d or not d.get('forge'):
        return '**CI reds** — n/a (no forge: PR CI is not measured)\n'
    out = [f"**CI reds** — PR runs not green: the last 24 h, the last {d['runs']} PR runs, "
           'and per day oldest → today', '',
           '| Class | 24 h | Last PR runs | Trend (per day) |', '|---|---|---|---|']
    for k in ('total',) + CLASSES:
        out.append(f"| {'all' if k == 'total' else LABELS[k]} | {d['day'][k]} | {d['window'][k]} | "
                   + ' '.join(str(t[k]) for t in d['trend']) + ' |')
    out += ['', '**CI targets** — over runs, not days', '',
            '| Target | Status | Reading | Limit |', '|---|---|---|---|']
    for r in d['targets']:
        out.append(f"| {r['name']} | {r['status']} | {r['detail'].replace('|', '/')} | {r['limit'] or '—'} |")
    return '\n'.join(out) + '\n'


def criterion(d, cfg):
    """Release criterion 12, *PR CI healthy*: ``(met, evidence)`` — first-pass at or above its
    target over its window and no unclassified red in the last ``reds_window`` PR runs; ``n/a``
    (met) with no forge or no CI, *pending* (not met) while no PR run is recorded."""
    if not d or not d.get('forge'):
        return True, f'{tp.NA} — no forge or no CI'
    fp = next(r for r in d['targets'] if r['key'] == 'first_pass')
    if fp['status'] == tp.NA:     # no data is not yet, never met
        return False, f"pending — {fp['detail']} recorded yet"
    bad = d['unclassified']
    ok = fp['status'] != tp.ALARM and not bad
    return ok, (f"{fp['detail']}" + (f" (target {fp['limit']})" if fp['limit'] else '')
                + f"; {len(bad)} unclassified red(s) in the last {d['runs']} PR runs"
                + (': ' + ', '.join(f"{x['run']} {x['why']}" for x in bad[:3]) if bad else ''))


# ------------------------------------------------------------------ readers --

def git_base_time(repo, trunk, git=None):
    """``base_time(sha)`` over the clone at ``repo``: the commit time of ``sha``'s merge-base
    with ``origin/<trunk>`` (else ``<trunk>``), cached; None for a sha the clone cannot resolve.
    Reads only — nothing is fetched. None (no reader) with no clone. ``git(args, cwd)`` is
    :func:`asf.gitops.git` unless a test hands in its own."""
    import os
    if not repo or not trunk or not os.path.isdir(repo):
        return None
    if git is None:
        from asf import gitops
        git = gitops.git
    cache = {}

    def read(*args):
        r = git(list(args), repo)
        return r.data if getattr(r, 'ok', False) else None

    ref = f'origin/{trunk}' if read('rev-parse', '--verify', '-q', f'origin/{trunk}') else trunk

    def base_time(sha):
        if not sha:
            return None
        if sha not in cache:
            base = read('merge-base', sha, ref)
            cache[sha] = tp.to_dt(read('show', '-s', '--format=%cI', base)) if base else None
        return cache[sha]
    return base_time


def load_claims(product):
    """The cancel ledger (:func:`asf.ci_cancels.claims`), ``{}`` when unreadable."""
    try:
        from asf import ci_cancels, env
        return ci_cancels.claims(env.state_dir(product))
    except Exception:  # noqa: BLE001 — no ledger is no claim
        return {}


def status_cell(root, product, now=None):
    """The ``CI reds`` row of ``asf status``: ``reds 24h: 9 (real 3 · flaky 4 · ours 2)`` plus
    the last PR runs' count and any target in breach; ``None`` (no row) with no forge."""
    if not getattr(product, 'repo_slug', None) or getattr(product, 'ci', None) == 'none':
        return None
    from asf.metrics.metrics import read_stream
    try:
        ci = read_stream(root, 'ci')
    except (OSError, ValueError):
        ci = []
    _seats, cfg = tp.settings(product)
    now = now or datetime.datetime.now(tp.UTC)
    d = compute({'ci': ci, 'claims': load_claims(product), 'forge': True,
                 'main': getattr(product, 'main', None),
                 'base_time': git_base_time(getattr(product, 'repo_dir', None),
                                            getattr(product, 'main', None))}, now, cfg)
    text = f"reds 24h: {summary(d['day'])} · last {d['runs']} PR runs: {d['window']['total']} red"
    breached = [r['key'] for r in d['targets'] if r['status'] == tp.ALARM]
    return text + (f" · BREACH {', '.join(breached)}" if breached else '')
