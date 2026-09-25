"""asf.scorecard.diagnose — where cost and time go, and the causes that cross a threshold.

Pure: every function takes :class:`asf.scorecard.facts.Facts` and a window.

:func:`rank` orders the window's spend and time four ways — by session kind, by failure class (the
registry's end reasons of runs that died without landing), by CI job (``metrics/ci`` jobs, and the
local gate's red signatures), and by Feature. :func:`causes` turns the rankings into causes, each
with a stable **cause key** (``kind:correct``, ``failure:failed: not pushed``, ``ci-job:e2e``,
``gate:<signature>``, ``lead-time``, ``cost-per-feature``, ``clutter:stale-prs``,
``clutter:branches``), the number that crossed, and its threshold. :func:`metric` re-reads one
cause's number over any window — the same arithmetic the filing used, so a before/after
comparison compares like with like.

Every number is "higher is worse", and rates are per week so windows of any length compare.
"""
import dataclasses
import datetime
import re

from asf.scorecard import score
from asf.scorecard.facts import to_dt

#: The defaults; a product overrides any of them under ``improve: {scorecard: {thresholds: …}}``.
THRESHOLDS = {
    'kind_share': 0.15,          # a repair kind's share of the window's session spend
    'kind_min_usd': 20.0,        # … and at least this many dollars of it
    'failure_per_week': 5.0,     # runs a week one failure class ended without landing
    'ci_red_per_week': 3.0,      # red runs a week of one CI job / one gate signature
    'lead_days': 7.0,            # median days card → landed (needs 3 landings in the window)
    'usd_per_feature': 150.0,    # all-in spend per landed Feature (needs 1 landing)
    'stale_prs': 10,             # open PRs not updated for 3 days
    'branches': 40,              # branches no open PR carries
}
#: A cause's scope decides whose record its card goes to: ``factory`` causes are the factory's
#: own process, ``product`` causes are the product's code or CI.
FACTORY, PRODUCT = 'factory', 'product'


@dataclasses.dataclass(frozen=True)
class Cause:
    key: str
    scope: str
    value: float
    threshold: float
    unit: str
    title: str
    detail: str


def thresholds(overrides=None):
    out = dict(THRESHOLDS)
    for k, v in (overrides or {}).items():
        if k in out and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    return out


def _weeks(start, end):
    return max((end - start).total_seconds() / (7 * 86400), 1 / 7)


def _sig_class(sig):
    """A gate's first failing line with its digits and hex dropped — one test, one class."""
    s = re.sub(r'\b[0-9a-f]{7,40}\b', '', str(sig or ''))
    return re.sub(r'\s+', ' ', re.sub(r'\d+', 'N', s)).strip()[:80] or 'unknown'


def rank(facts, start, end):
    """``{'usd', 'hours', 'by_kind', 'by_failure', 'by_ci_job', 'by_feature'}`` over ``[start, end)``;
    each ``by_*`` a list of dicts, largest first."""
    sessions = [s for s in facts.sessions if score.in_window(s.get('ts'), start, end)]
    usd = sum(score._num(s.get('usd')) for s in sessions)
    minutes = sum(score._num(s.get('minutes')) for s in sessions)
    kinds = {}
    for s in sessions:
        k = score.session_kind(s)
        c = kinds.setdefault(k, {'name': k, 'usd': 0.0, 'sessions': 0, 'minutes': 0.0,
                                 'repair': score.is_repair(k)})
        c['usd'] += score._num(s.get('usd'))
        c['sessions'] += 1
        c['minutes'] += score._num(s.get('minutes'))
    for c in kinds.values():
        c['share'] = round(c['usd'] / usd, 3) if usd else 0.0
        c['usd'] = round(c['usd'], 2)
        c['minutes'] = round(c['minutes'], 1)

    fails = {}
    for r in facts.runs:
        if not score.is_dead(r) or not score.in_window(r.ended, start, end):
            continue
        k = score.failure_class(r.end_reason)
        c = fails.setdefault(k, {'name': k, 'runs': 0, 'usd': 0.0, 'minutes': 0.0})
        c['runs'] += 1
        c['usd'] = round(c['usd'] + (r.usd or 0.0), 2)
        c['minutes'] = round(c['minutes'] + r.minutes, 1)

    jobs = {}
    for run in facts.ci:
        if not score.in_window(run.get('ts'), start, end):
            continue
        for j in run.get('jobs') or ():
            if not isinstance(j, dict):
                continue
            name = str(j.get('name') or '?')
            c = jobs.setdefault(f'ci-job:{name}', {'name': f'ci-job:{name}', 'minutes': 0.0,
                                                   'runs': 0, 'red': 0, 'scope': PRODUCT})
            c['minutes'] = round(c['minutes'] + score._num(j.get('minutes')), 1)
            c['runs'] += 1
            c['red'] += 1 if j.get('conclusion') not in ('success', 'skipped', 'neutral', 'cancelled') else 0
    for g in facts.gates:
        if not score.in_window(g.get('ts'), start, end):
            continue
        red = g.get('conclusion') != 'success'
        name = f"gate:{_sig_class(g.get('signature'))}" if red else 'gate:green'
        c = jobs.setdefault(name, {'name': name, 'minutes': 0.0, 'runs': 0, 'red': 0, 'scope': PRODUCT})
        c['minutes'] = round(c['minutes'] + score._num(g.get('seconds')) / 60, 1)
        c['runs'] += 1
        c['red'] += 1 if red else 0

    feats = {}
    for s in sessions:
        fid = score.feature_of(facts.items, s.get('item')) or '(none)'
        c = feats.setdefault(fid, {'name': fid, 'usd': 0.0, 'sessions': 0, 'repair': 0,
                                   'title': (facts.items.get(fid) or {}).get('title', '')})
        c['usd'] = round(c['usd'] + score._num(s.get('usd')), 2)
        c['sessions'] += 1
        c['repair'] += 1 if score.is_repair(score.session_kind(s)) else 0

    def desc(d, key):
        return sorted(d.values(), key=lambda c: (-c[key], c['name']))
    return {'usd': round(usd, 2), 'hours': round(minutes / 60, 1),
            'by_kind': desc(kinds, 'usd'), 'by_failure': desc(fails, 'runs'),
            'by_ci_job': desc(jobs, 'minutes'), 'by_feature': desc(feats, 'usd')}


def _landed_window(facts, start, end):
    rows = score.feature_rows(facts)
    return [r for r in rows if score.in_window(r['landed'], start, end)]


def metric(facts, key, start, end):
    """One cause's number over ``[start, end)`` (per week where it is a count), or ``None`` when
    the window cannot say (no landing to take a median of, a clutter count the forge did not give)."""
    weeks = _weeks(start, end)
    if key.startswith('kind:'):
        name = key[len('kind:'):]
        r = rank(facts, start, end)
        for c in r['by_kind']:
            if c['name'] == name:
                return c['share']
        return 0.0
    if key.startswith('failure:'):
        name = key[len('failure:'):]
        runs = sum(1 for r in facts.runs if score.is_dead(r) and score.in_window(r.ended, start, end)
                   and score.failure_class(r.end_reason) == name)
        return round(runs / weeks, 2)
    if key.startswith(('ci-job:', 'gate:')):
        for c in rank(facts, start, end)['by_ci_job']:
            if c['name'] == key:
                return round(c['red'] / weeks, 2)
        return 0.0
    if key == 'lead-time':
        return score.median([r['lead_days'] for r in _landed_window(facts, start, end)])
    if key == 'cost-per-feature':
        return score.window_row(facts, start, end)['usd_per_feature']
    if key == 'clutter:stale-prs':
        return (facts.clutter or {}).get('stale_prs')
    if key == 'clutter:branches':
        return (facts.clutter or {}).get('branches')
    raise KeyError(key)


def causes(facts, start, end, limits=None):
    """Every cause over its threshold in ``[start, end)``, the furthest over (reading ÷ threshold)
    first."""
    t = thresholds(limits)
    weeks = _weeks(start, end)
    days = round((end - start).total_seconds() / 86400)
    r = rank(facts, start, end)
    out = []
    for c in r['by_kind']:
        if c['repair'] and c['share'] > t['kind_share'] and c['usd'] >= t['kind_min_usd']:
            out.append(Cause(
                f"kind:{c['name']}", FACTORY, c['share'], t['kind_share'], 'share of spend',
                f"Repair sessions '{c['name']}' take {c['share'] * 100:.0f} % of session spend",
                f"{c['sessions']} '{c['name']}' sessions cost ${c['usd']:,.2f} of ${r['usd']:,.2f} "
                f"over {days} days ({c['minutes'] / 60:.1f} h); threshold {t['kind_share'] * 100:.0f} %."))
    for c in r['by_failure']:
        per_week = round(c['runs'] / weeks, 2)
        if per_week > t['failure_per_week']:
            out.append(Cause(
                f"failure:{c['name']}", FACTORY, per_week, t['failure_per_week'], 'runs/week',
                f"Sessions die with '{c['name']}' {per_week:g} times a week",
                f"{c['runs']} runs ended '{c['name']}' without landing over {days} days, "
                f"${c['usd']:,.2f} and {c['minutes'] / 60:.1f} h spent on them; threshold "
                f"{t['failure_per_week']:g}/week."))
    for c in r['by_ci_job']:
        per_week = round(c['red'] / weeks, 2)
        if c['red'] and per_week > t['ci_red_per_week']:
            out.append(Cause(
                c['name'], PRODUCT, per_week, t['ci_red_per_week'], 'red runs/week',
                f"CI {c['name']} is red {per_week:g} times a week",
                f"{c['red']} of {c['runs']} runs red over {days} days, {c['minutes']:,.0f} runner "
                f"minutes; threshold {t['ci_red_per_week']:g}/week."))
    landed = _landed_window(facts, start, end)
    lead = score.median([x['lead_days'] for x in landed])
    if len(landed) >= 3 and lead is not None and lead > t['lead_days']:
        out.append(Cause(
            'lead-time', FACTORY, lead, t['lead_days'], 'days',
            f"A Feature takes {lead:g} days from card to landed",
            f"median over the {len(landed)} Features landed in {days} days; threshold "
            f"{t['lead_days']:g} days."))
    per_feature = score.window_row(facts, start, end)['usd_per_feature']
    if per_feature is not None and per_feature > t['usd_per_feature']:
        out.append(Cause(
            'cost-per-feature', FACTORY, per_feature, t['usd_per_feature'], 'USD/feature',
            f"A landed Feature costs ${per_feature:,.0f} all-in",
            f"${r['usd']:,.2f} of sessions over {days} days for {len(landed)} landed Features; "
            f"threshold ${t['usd_per_feature']:,.0f}. Where it went: "
            + ', '.join(f"{k['name']} ${k['usd']:,.0f}" for k in r['by_kind'][:4]) + '.'))
    clutter = facts.clutter or {}
    if (clutter.get('stale_prs') or 0) > t['stale_prs']:
        out.append(Cause(
            'clutter:stale-prs', FACTORY, clutter['stale_prs'], t['stale_prs'], 'PRs',
            f"{clutter['stale_prs']} PRs sit open with no update for 3 days",
            f"{clutter['stale_prs']} of {clutter.get('open_prs')} open PRs are stale; threshold "
            f"{t['stale_prs']}. The factory reaps what it opened."))
    if (clutter.get('branches') or 0) > t['branches']:
        out.append(Cause(
            'clutter:branches', FACTORY, clutter['branches'], t['branches'], 'branches',
            f"{clutter['branches']} branches carry no open PR",
            f"{clutter['branches']} branches besides the trunk have no open PR; threshold "
            f"{t['branches']}."))
    out.sort(key=lambda c: (-(c.value / c.threshold if c.threshold else c.value), c.key))
    return out


def window(as_of, days):
    end = to_dt(as_of) + datetime.timedelta(seconds=1)
    return end - datetime.timedelta(days=days), end
