"""asf.views.scorecard — ``asf scorecard [--product p] [--weeks n] [--json]``, and the ``Value`` row
of ``asf status``.

The SCORECARD: the headline, one row per ISO week (``--weeks``, the current week first), one row
per Feature landed in those weeks, where the last ``window_days`` went (by session kind, failure
class, CI job, Feature), the causes over threshold and the loop's state for each (filed, card,
verdict), and the stored weekly snapshots. Computed from facts on every run; nothing is written.
"""
import json

from asf import env
from asf.scorecard import diagnose, loop, score
from asf.scorecard.facts import load, state_file


def _m(v):
    return '—' if v is None else f'${v:,.2f}'


def _n(v, fmt='g'):
    return '—' if v is None else format(v, fmt)


def _tok(n):
    return f'{n / 1e6:.1f} M' if n >= 1e6 else (f'{n / 1e3:.0f} k' if n >= 1e3 else str(n))


def value_cell(root, product):
    """The ``Value`` row: the rolling 7 days, from the record alone (no registry, no forge)."""
    facts = load(root, product, registry=False, forge=False)
    return score.headline_line(score.headline(facts))


def compute(root, product, weeks=4, facts=None):
    cfg = loop.settings(product)
    facts = facts or load(root, product)
    rows = score.feature_rows(facts)
    week_rows = score.weekly(facts, weeks, rows)
    oldest = week_rows[-1]['start']
    start, end = diagnose.window(facts.as_of, cfg['window_days'])
    causes = diagnose.causes(facts, start, end, cfg['thresholds'])
    state = loop._read_json(state_file(product, loop.CAUSES), {})
    return {
        'product': product.name, 'as_of': facts.as_of, 'window_days': cfg['window_days'],
        'headline': score.headline(facts, rows=rows), 'clutter': facts.clutter,
        'weeks': week_rows,
        'features': [r for r in rows if (r['landed'] or '') >= oldest],
        'rank': diagnose.rank(facts, start, end),
        'causes': [dict(c.__dict__, loop=state.get(c.key)) for c in causes],
        'loop': state,
        'snapshots': loop.snapshots(product)[-weeks:],
    }


def render(d):
    out = [f"**SCORECARD {d['product']}** — {d['as_of']}", '',
           f"Value: {score.headline_line(d['headline'], d['clutter'])}", '',
           '| Week of | Landed | On prod | Lead (card→landed) | Lead (→prod) | Task lead | $ all-in | $/feature all-in '
           '| $/feature own | Tokens | CI min | Repair sessions | Repair/feature | Bugs (S1) | Dead sessions |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for w in d['weeks']:
        out.append(f"| {w['week']} | {w['landed']} | {w['on_prod']} | {_n(w['median_lead_days'])} d "
                   f"| {_n(w['median_prod_days'])} d | {_n(w['median_task_days'])} d | {_m(w['usd'])} "
                   f"| {_m(w['usd_per_feature'])} "
                   f"| {_m(w['own_usd_per_feature'])} | {_tok(w['tokens'])} | {w['ci_min']:,.0f} "
                   f"| {w['repair_sessions']} | {_n(w['repair_per_feature'])} | {w['bugs']} ({w['s1']}) "
                   f"| {w['dead_sessions']} ({_m(w['dead_usd'])}) |")
    c = d['clutter'] or {}
    out += ['', f"Clutter now: {_n(c.get('stale_prs'))} stale PRs of {_n(c.get('open_prs'))} open · "
                f"{_n(c.get('branches'))} branches with no open PR", '']
    out += ['**Features landed**', '',
            '| Feature | Landed | Prod | Lead | $ own | $ on its bugs | Sessions | Tokens | CI min '
            '| Repair | Corrections | Send-backs | Reopens | Bugs (S1) |',
            '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for r in d['features']:
        title = r['title'][:48].replace('|', '/')
        out.append(f"| {r['id']} {title} | {(r['landed'] or '')[:10]} | {(r['prod'] or '—')[:10]} "
                   f"| {_n(r['lead_days'])} d | {_m(r['usd'])} | {_m(r['bug_usd'])} | {r['sessions']} "
                   f"| {_tok(r['tokens'])} | {r['ci_min']:,.0f} | {r['repair_sessions']} | {r['corrections']} "
                   f"| {r['send_backs']} | {r['reopens']} | {r['bugs']} ({r['s1']}) |")
    if not d['features']:
        out.append('| (none landed in these weeks) | | | | | | | | | | | | | |')
    rk = d['rank']
    out += ['', f"**Where the last {d['window_days']} days went** — {_m(rk['usd'])}, {rk['hours']:g} h", '',
            '| By | Top five |', '|---|---|',
            '| session kind | ' + '; '.join(f"{k['name']} {_m(k['usd'])} ({k['share'] * 100:.0f} %, "
                                         f"{k['sessions']})" for k in rk['by_kind'][:5]) + ' |',
            '| failure class | ' + ('; '.join(f"{k['name']} ×{k['runs']} ({_m(k['usd'])})"
                                             for k in rk['by_failure'][:5]) or '—') + ' |',
            '| CI job | ' + ('; '.join(f"{k['name']} {k['minutes']:,.0f} min, {k['red']} red"
                                      for k in rk['by_ci_job'][:5]) or '—') + ' |',
            '| Feature | ' + '; '.join(f"{k['name']} {_m(k['usd'])} ({k['repair']} repair)"
                                     for k in rk['by_feature'][:5]) + ' |']
    out += ['', '**Causes over threshold** (filed once each through the inbox; verified after landing)', '']
    if d['causes']:
        out += ['| Cause | Reading | Threshold | Loop |', '|---|---|---|---|']
        for c in d['causes']:
            st = c.get('loop') or {}
            where = (f"filed {st.get('filed')} → {st.get('target')}" + (f", card {st['card']}" if st.get('card') else '')
                     + (f", {st['verdict']}" if st.get('verdict') else '')) if st else 'files on the next daily run'
            out.append(f"| {c['key']} | {c['value']:g} {c['unit']} | {c['threshold']:g} | {where} |")
    else:
        out.append('none')
    done = {k: v for k, v in d['loop'].items() if v.get('verdict')}
    if done:
        out += ['', '**Verified**', '']
        for k, v in sorted(done.items()):
            out.append(f"- {k}: {v.get('card')} {v['verdict']} ({_n(v.get('before'))} → {_n(v.get('after'))})")
    return '\n'.join(out) + '\n'


def cmd_scorecard(args, root):
    product = env.load_product(getattr(args, 'product', None))
    d = compute(root, product, weeks=max(1, int(getattr(args, 'weeks', 4) or 4)))
    if getattr(args, 'json', False):
        print(json.dumps(d, indent=1, default=str))
    else:
        print(render(d), end='')
    return 0
