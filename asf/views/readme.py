"""asf.views.readme — the README's generated spans (`asf readme`).

A page a human writes, with the numbers written by the build. Two markers, both HTML comments,
so GitHub renders the page as prose and the build reads it as a template:

    <!--asf:n sessions-->1,204<!--/asf:n-->        an inline value
    <!--asf:block scoreboard-->                    a table, markers on their own lines
    | Metric | Value | From |
    <!--/asf:block-->

Read-only over the record: every number comes from `asf.views.index_reader` and
`asf.metrics.metrics`, which the daily scorecard already computes with. Stdlib only.
"""
import argparse
import collections
import os
import re

from asf.metrics import metrics
from asf.views import index_reader

#: Digit runs a human may type in the page's prose. Every entry is a name, not a measurement:
#: a measurement belongs in a span.
LITERALS = ('0.1', '2.0')        # the release; Apache-2.0

#: One page: a reader scrolls it twice.
MAX_LINES = 150
MAX_BYTES = 10_000

Span = collections.namedtuple('Span', 'kind key start end body')

_MARKER = re.compile(r'<!--(/?)asf:(n|block)(?:[ \t]+([^\s>]+?))?[ \t]*-->')


def _line_of(text, pos):
    return text.count('\n', 0, pos) + 1


def _alone(text, m):
    """True when the marker is the only thing on its line."""
    a = text.rfind('\n', 0, m.start()) + 1
    b = text.find('\n', m.end())
    b = len(text) if b < 0 else b
    return text[a:m.start()].strip() == '' and text[m.end():b].strip() == ''


def spans(text):
    """Every span in document order. `start`/`end` are the offsets of the body: for an inline
    span the text between the markers, for a block the whole lines between the marker lines."""
    found = []
    seen = set()
    open_ = None                       # (kind, key, marker match, body start)
    for m in _MARKER.finditer(text):
        closing, kind, key = m.group(1) == '/', m.group(2), m.group(3)
        line = _line_of(text, m.start())
        if kind == 'block' and not _alone(text, m):
            raise ValueError('line %d: an asf:block marker must be alone on its line' % line)
        if not closing:
            if not key:
                raise ValueError('line %d: an asf:%s marker without a key' % (line, kind))
            if open_ is not None:
                raise ValueError('line %d: span %r opened inside span %r (line %d)'
                                 % (line, key, open_[1], _line_of(text, open_[2].start())))
            if key in seen:
                raise ValueError('line %d: key %r used twice' % (line, key))
            seen.add(key)
            if kind == 'block':
                nl = text.find('\n', m.end())
                start = len(text) if nl < 0 else nl + 1
            else:
                start = m.end()
            open_ = (kind, key, m, start)
            continue
        if open_ is None:
            raise ValueError('line %d: close of asf:%s with no open' % (line, kind))
        if open_[0] != kind:
            raise ValueError('line %d: close of asf:%s inside span %r, which is an asf:%s'
                             % (line, kind, open_[1], open_[0]))
        end = text.rfind('\n', 0, m.start()) + 1 if kind == 'block' else m.start()
        end = max(end, open_[3])
        found.append(Span(kind, open_[1], open_[3], end, text[open_[3]:end]))
        open_ = None
    if open_ is not None:
        raise ValueError('line %d: span %r is never closed'
                         % (_line_of(text, open_[2].start()), open_[1]))
    return found


def render(text, facts):
    """`text` with each span's body replaced by `facts['numbers'][key]['text']`. Bytes outside
    a span are spliced through untouched."""
    numbers = facts.get('numbers', {})
    found = spans(text)
    keys = {s.key for s in found}
    for s in found:
        if s.key not in numbers:
            raise ValueError('no fact for span %r' % s.key)
    for key in numbers:
        if key not in keys:
            raise ValueError('fact %r has no span in the page' % key)
    out = []
    at = 0
    for s in found:
        new = numbers[s.key]['text']
        if s.kind == 'block' and new and not new.endswith('\n'):
            new += '\n'
        out.append(text[at:s.start])
        out.append(new)
        at = s.end
    out.append(text[at:])
    return ''.join(out)


# ----------------------------------------------------------------- facts --

NONE = '—'                       # a number with nothing behind it
SHIPPED = ('landed', 'on-prod')
_ROW_ID = re.compile(r'^\| ([A-Z]-\d+) \|')


def _entry(value, text, source):
    return {'value': value, 'text': text, 'source': source}


def _none(source):
    """The no-events rule: `value` stays None, so 'none measured' is not 'measured zero'."""
    return _entry(None, NONE, source)


def _int(n, source):
    return _entry(n, '{:,}'.format(n), source) if n else _none(source)


def _usd(v):
    return '${:,.2f}'.format(v) if v < 10 else '${:,.0f}'.format(v)


def _window(streams):
    days = sorted({e['ts'][:10] for evs in streams for e in evs if e.get('ts')})
    if not days:
        return _none('metrics')
    first, last = days[0], days[-1]
    n = (metrics.dt.date.fromisoformat(last) - metrics.dt.date.fromisoformat(first)).days + 1
    return _entry([first, last, n], '%s … %s (%d days)' % (first, last, n), 'metrics')


def _commands_rows(parser):
    """[(name, help)], one per subcommand `parser` registers, in registration order."""
    rows = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            rows.extend((c.dest, c.help or '') for c in action._choices_actions)
    return rows


def _commands(parser):
    rows = _commands_rows(parser)
    if not rows:
        return _entry(None, NONE, 'cli')
    lines = ['| Command | What it does |', '|---|---|']
    lines += ['| `asf %s` | %s |' % (n, metrics.esc(h)) for n, h in rows]
    return _entry([n for n, _h in rows], '\n'.join(lines) + '\n', 'cli')


def _cost_per_feature(items, ci, sessions, shipped):
    lines = metrics.cost_table(items, ci, sessions)
    rows = [l for l in lines[2:]
            if _ROW_ID.match(l) and _ROW_ID.match(l).group(1) in shipped][:5]
    source = 'index.json + metrics/sessions + metrics/ci'
    if not rows:
        return _none(source)
    ids = [_ROW_ID.match(l).group(1) for l in rows]
    return _entry(ids, '\n'.join(lines[:2] + rows) + '\n', source)


def _scoreboard(numbers):
    lines = ['| Metric | Value | From |', '|---|---|---|']
    for key, label in SCORE_ROWS:
        n = numbers[key]
        lines.append('| %s | %s | `%s` |' % (label, n['text'], n['source']))
    return _entry(None, '\n'.join(lines) + '\n', 'index.json + metrics')


#: (key, label) — the scalar numbers the scoreboard block lists, in order.
SCORE_ROWS = (('window', 'Window'), ('items', 'Cards in the record'),
              ('features_shipped', 'Features shipped'), ('sessions', 'Agent sessions'),
              ('session_hours', 'Session hours'), ('spend_usd', 'Spend'),
              ('usd_per_feature', 'Spend per shipped Feature'), ('ci_runs', 'CI runs'),
              ('ci_green_pct', 'CI green'), ('ticks', 'Ticks'))


def facts(record_root, repo_root, product=None, now=None):
    """Every value the page can show, each with the reader that produced it. Reads only through
    `index_reader.load` and `metrics.read_stream`; `commands` reads the parser, so `repo_root`
    is not opened (it and `product` are the caller's context, kept for the command's sake)."""
    now = now or metrics.now_utc()
    items, record_generated = index_reader.load(record_root)
    ci = metrics.read_stream(record_root, 'ci')
    sessions = metrics.read_stream(record_root, 'sessions')
    ticks = metrics.read_stream(record_root, 'ticks')

    numbers = {'window': _window((ci, sessions, ticks))}
    numbers['items'] = _int(len(items), 'index.json')
    features = [i for i, it in items.items() if it.get('type') == 'feature']
    shipped = sorted(i for i in features if items[i].get('stage') in SHIPPED)
    numbers['features_shipped'] = (_entry(len(shipped), '{:,}'.format(len(shipped)), 'index.json')
                                   if features else _none('index.json'))
    numbers['sessions'] = _int(len(sessions), 'metrics/sessions')

    minutes = [s['minutes'] for s in sessions if s.get('minutes') is not None]
    hours = sum(minutes) / 60
    numbers['session_hours'] = (_entry(round(hours, 2), '{:,.1f}'.format(hours),
                                       'metrics/sessions.minutes') if minutes
                                else _none('metrics/sessions.minutes'))
    usd = [s['usd'] for s in sessions if s.get('usd') is not None]
    spend = sum(usd)
    numbers['spend_usd'] = (_entry(round(spend, 2), _usd(spend), 'metrics/sessions.usd') if usd
                            else _none('metrics/sessions.usd'))

    costs = metrics.compute_costs(ci, sessions)
    subs = [metrics.subtree_cost(items, costs, i)['usd'] for i in shipped]
    subs = [u for u in subs if u is not None]
    source = 'index.json + metrics/sessions.usd'
    if shipped and subs:
        per = sum(subs) / len(shipped)
        numbers['usd_per_feature'] = _entry(round(per, 2), _usd(per), source)
    else:
        numbers['usd_per_feature'] = _none(source)

    numbers['ci_runs'] = _int(len(ci), 'metrics/ci')
    if ci:
        green = 100.0 * sum(1 for r in ci if r.get('conclusion') == 'success') / len(ci)
        numbers['ci_green_pct'] = _entry(round(green, 1), '{:.0f}%'.format(green),
                                         'metrics/ci.conclusion')
    else:
        numbers['ci_green_pct'] = _none('metrics/ci.conclusion')
    numbers['ticks'] = _int(len(ticks), 'metrics/ticks')

    numbers['scoreboard'] = _scoreboard(numbers)
    numbers['cost_per_feature'] = _cost_per_feature(items, ci, sessions, set(shipped))
    from asf import cli
    numbers['commands'] = _commands(cli.build_parser())
    return {'generated': metrics.iso(now), 'record': os.path.basename(os.path.abspath(record_root)),
            'record_generated': record_generated, 'day': now.astimezone(metrics.dt.timezone.utc)
            .strftime('%Y-%m-%d'), 'numbers': numbers}
