"""asf.views.tokens — the ``TOKENS`` table (``asf tokens``): input tokens per session, per brief kind,
in the ``--days`` before and after a split day.

A reader over ``metrics/sessions/<day>.jsonl`` (the ``in_tokens`` and ``turns`` keys of the
``sessions`` stream). It imports nothing from ``asf.metrics`` and writes nothing. Each cell is a
median: one enormous session would otherwise be the whole reading.
"""
import datetime
import json
import os
import statistics

DASH = '—'
MINUS = '−'


def _day_rows(root, day):
    path = os.path.join(root, 'metrics', 'sessions', day + '.jsonl')
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def windows(split, days):
    """(before, after) as lists of ISO days: ``days`` ending the day before ``split``; ``split`` and the
    ``days - 1`` after it."""
    d0 = datetime.date.fromisoformat(split)
    before = [(d0 - datetime.timedelta(days=i)).isoformat() for i in range(days, 0, -1)]
    after = [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(days)]
    return before, after


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _cell(rows):
    return {'count': len(rows),
            'in_tokens': _median([_num(r.get('in_tokens')) for r in rows]),
            'turns': _median([_num(r.get('turns')) for r in rows])}


def _delta(before, after):
    if before is None or after is None or before == 0:
        return None
    return (after - before) / before * 100


def compute(root, split, days):
    """One row per kind present in either window (sorted), then ``all``."""
    before_days, after_days = windows(split, days)
    before = [r for d in before_days for r in _day_rows(root, d)]
    after = [r for d in after_days for r in _day_rows(root, d)]
    kinds = sorted({str(r.get('kind')) for r in before + after})
    out = []
    for kind in kinds + ['all']:
        def pick(rows, kind=kind):
            return rows if kind == 'all' else [r for r in rows if str(r.get('kind')) == kind]
        b, a = _cell(pick(before)), _cell(pick(after))
        out.append({'kind': kind, 'before': b['count'], 'before_in': b['in_tokens'],
                    'after': a['count'], 'after_in': a['in_tokens'],
                    'delta_pct': _delta(b['in_tokens'], a['in_tokens']),
                    'before_turns': b['turns'], 'after_turns': a['turns']})
    return out


def _n(v):
    return DASH if v is None else format(round(v), ',')


def _delta_text(d):
    if d is None:
        return DASH
    sign = '+' if d > 0 else MINUS if d < 0 else ''
    return sign + str(abs(round(d))) + ' %'


def render(rows, split, days):
    lines = ['TOKENS — input tokens per session, ' + str(days) + ' d either side of ' + split, '',
             '| kind | before | in/session | after | in/session | Δ | turns |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for r in rows:
        cells = [r['kind'], str(r['before']), _n(r['before_in']), str(r['after']), _n(r['after_in']),
                 _delta_text(r['delta_pct']), _n(r['before_turns']) + ' → ' + _n(r['after_turns'])]
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


def cmd_tokens(args, root):
    split = args.split or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    rows = compute(root, split, args.days)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render(rows, split, args.days), end='')
    return 0
