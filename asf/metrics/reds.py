"""asf.metrics.reds — the red PR runs the operator sees, counted and told apart, and the PR/CI
targets measured over runs rather than days.

**Visible reds.** Every PR run (a ``ci`` stream event with a ``pr`` and no ``batch``) the host
shows as not green — :data:`RED`: ``failure``, ``cancelled``, ``timed_out``, ``startup_failure``
— is one visible red, and each gets exactly one class (:func:`classify_run`), first match wins:

``ours``     a cancel the factory made itself: its claim in the cancel ledger
             (:func:`asf.ci_queue.claim_cancel`, read through :func:`asf.ci_cancels.claims`) names
             one of ``own_cancel_causes`` (queue relief, step-silence stall, dedupe, the post-merge
             cancel, the merge queue's drop and reap), or the run was ``superseded`` by a newer run
             on its branch. Counted apart, so a fix that turns them into silent skips reads as
             fewer visible reds.
``infra``    the run or a job of it ended on the runner rather than the code: ``timed_out`` or
             ``startup_failure``, a job cancelled at its own limit or by runner loss
             (:mod:`asf.ci_jobs`), a timeout claim, or a failed step matching ``infra_steps``
             (out of memory, the runner's own set-up).
``check``    every failed job is a deterministic repo check (``check_patterns``: rules, sign-off,
             release notes, lint …) — red the same way on every attempt.
``flaky``    a later run of the same head went green (a re-run).
``real``     a later run on the same PR carried a new head: a new commit was needed.
``pending``  a failure nothing has followed yet (the PR is still on it, or was abandoned).
``unclassified`` a cancel nobody claimed and no rule explains (a hand cancel, a stray).

The status row folds ``infra`` into ``flaky`` (both re-run green without a code change).

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
REAL, FLAKY, INFRA, OURS, CHECK, PENDING, UNCLASSIFIED = (
    'real', 'flaky', 'infra', 'ours', 'check', 'pending', 'unclassified')
CLASSES = (REAL, FLAKY, INFRA, OURS, CHECK, PENDING, UNCLASSIFIED)
LABELS = {REAL: 'real (a new commit was needed)', FLAKY: 'flaky (same head green on re-run)',
          INFRA: 'infra (runner loss, OOM, timeout)', OURS: 'ours (ASF cancelled it)',
          CHECK: 'deterministic check', PENDING: 'pending (nothing ran after it yet)',
          UNCLASSIFIED: 'unclassified'}
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


def classify_run(r, runs, claims, cfg):
    """``(class, why)`` for one red run ``r`` — ``runs`` every ``ci`` event (the later runs of
    its head and PR are read from it), ``claims`` the cancel ledger ``{run id: {cause}}``."""
    c = r.get('conclusion')
    jobs = r.get('jobs') or ()
    claim = (claims or {}).get(str(r.get('run'))) or {}
    cause = claim.get('cause') if isinstance(claim, dict) else None
    own = cfg.get('own_cancel_causes') or ()
    if c == 'cancelled':
        if cause in own:
            return OURS, f'claimed: {cause}'
        if cause in RUNNER_CLAIMS:
            return INFRA, f'claimed: {cause}'
        hit = next((j for j in jobs if j.get('cause') in RUNNER_JOB_CAUSES), None)
        if hit:
            return INFRA, f"job {hit.get('name')}: {hit.get('cause')}"
        if r.get('superseded') and 'superseded' in own:
            return OURS, 'superseded by a newer run on its branch'
        return UNCLASSIFIED, f'cancel with no claim{f" ({cause})" if cause else ""}'
    if c in ('timed_out', 'startup_failure'):
        return INFRA, c
    hit = next((j for j in jobs if j.get('cause') in RUNNER_JOB_CAUSES), None)
    if hit:
        return INFRA, f"job {hit.get('name')}: {hit.get('cause')}"
    failed = [j for j in jobs if j.get('conclusion') in ('failure', 'timed_out', 'startup_failure')]
    hit = next((j for j in failed if _match(j.get('failed_step'), cfg.get('infra_steps'))), None)
    if hit:
        return INFRA, f"step {hit.get('failed_step')!r}"
    checks = cfg.get('check_patterns') or ()
    if failed and all(_match(j.get('name'), checks) or _match(j.get('failed_step'), checks)
                      for j in failed):
        return CHECK, ', '.join(sorted({str(j.get('failed_step') or j.get('name')) for j in failed}))[:80]
    later = [x for x in runs if _key(x) > _key(r) and x is not r]
    if any(x.get('sha') == r.get('sha') and x.get('conclusion') == 'success' for x in later):
        return FLAKY, 'same head green on re-run'
    pr, branch = r.get('pr'), r.get('branch')
    if any(x.get('sha') != r.get('sha') and ((pr is not None and x.get('pr') == pr)
                                            or (pr is None and branch and x.get('branch') == branch))
           for x in later):
        return REAL, 'a new head followed'
    return PENDING, 'no run followed yet'


def classify(ci, claims, cfg, pick=is_pr_run):
    """Every red run ``pick`` selects, oldest first, each ``{run, attempt, ts, pr, sha,
    conclusion, cls, why}``."""
    runs = list(ci or ())
    by = collections.defaultdict(list)
    for r in runs:
        by[('sha', r.get('sha'))].append(r)
        by[('pr', r.get('pr')) if r.get('pr') is not None else ('branch', r.get('branch'))].append(r)
    out = []
    for r in sorted((r for r in runs if pick(r) and is_red(r)), key=_key):
        near = by[('sha', r.get('sha'))] + by[('pr', r.get('pr')) if r.get('pr') is not None
                                              else ('branch', r.get('branch'))]
        cls, why = classify_run(r, near, claims, cfg)
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


def targets(ci, claims, cfg, forge=True, main=None):
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
        infra = [x for x in classify(ci, claims, cfg, pick=lambda r: True)
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
    reds = classify(ci, claims, cfg)
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
    rows = targets(ci, claims, cfg, forge=forge, main=f.get('main'))
    return {'forge': forge, 'runs': len(window),
            'day': counts([x for x in reds if tp._in(x['ts'], end - datetime.timedelta(hours=24), end)]),
            'window': counts(in_window), 'trend': trend, 'targets': rows,
            'unclassified': [x for x in in_window if x['cls'] == UNCLASSIFIED],
            'alarms': [breach_line(r) for r in rows if r['status'] == tp.ALARM]}


def summary(c):
    """``9 (real 3 · flaky 4 · ours 2)`` — flaky folds in infra; check, pending and unclassified
    are named only when non-zero."""
    parts = [f'real {c[REAL]}', f'flaky {c[FLAKY] + c[INFRA]}', f'ours {c[OURS]}']
    parts += [f'{k} {c[k]}' for k in (CHECK, PENDING, UNCLASSIFIED) if c[k]]
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
    out += [''] + d['alarms'] if d['alarms'] else []
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
                 'main': getattr(product, 'main', None)}, now, cfg)
    text = f"reds 24h: {summary(d['day'])} · last {d['runs']} PR runs: {d['window']['total']} red"
    breached = [r['key'] for r in d['targets'] if r['status'] == tp.ALARM]
    return text + (f" · BREACH {', '.join(breached)}" if breached else '')
