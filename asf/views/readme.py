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
import json
import os
import re
import subprocess

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


# ---------------------------------------------------------------- checks --

#: The three ``##`` headings the page's parts carry, in order (§2.4).
HEADINGS = ('## The argument', '## The mental model', '## The manual')

_INLINE_CODE = re.compile(r'`([^`\n]+)`')
_LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
_DIGIT_RUN = re.compile(r'\d[\d,]*(?:\.\d+)?')
_SCHEME = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')
_FENCE = re.compile(r'^```', re.M)


def _in_ranges(pos, ranges):
    return any(a <= pos < b for a, b in ranges)


def _in_fence(text, pos):
    """True when `pos` sits inside a triple-backtick fenced block: a command's own argument is
    not read by the link check (PD6)."""
    return len(_FENCE.findall(text[:pos])) % 2 == 1


def _skip_target(target):
    """A link target or an inline-code path the link check does not resolve: an in-page anchor,
    an absolute or home-relative path, a shell variable, or a URL scheme (PD6)."""
    return not target or target.startswith(('#', '~', '/', '$')) or bool(_SCHEME.match(target))


def _drift_complaints(found, numbers):
    problems = []
    keys = set()
    for s in found:
        keys.add(s.key)
        entry = numbers.get(s.key)
        if entry is None:
            problems.append('span %r has no fact in the committed facts file' % s.key)
        elif entry.get('text') != s.body:
            problems.append('span %r has drifted from its fact' % s.key)
    for key in numbers:
        if key not in keys:
            problems.append('fact %r has no span in the page' % key)
    return problems


def _digit_complaints(text, found):
    skip = [(s.start, s.end) for s in found]
    skip += [(m.start(), m.end()) for m in _INLINE_CODE.finditer(text)]
    skip += [(m.start(1), m.end(1)) for m in _LINK.finditer(text)]
    problems = []
    for m in _DIGIT_RUN.finditer(text):
        if m.group() in LITERALS or _in_ranges(m.start(), skip):
            continue
        problems.append('line %d: %r is a typed number — not in a span, inline code, a link '
                        'target or LITERALS' % (_line_of(text, m.start()), m.group()))
    return problems


def _link_complaints(text, root):
    problems = []
    for m in _LINK.finditer(text):
        target = m.group(1).split('#', 1)[0].strip()
        if _skip_target(target):
            continue
        if not os.path.exists(os.path.join(root, target)):
            problems.append('line %d: link target %r does not exist under the repo root'
                            % (_line_of(text, m.start(1)), m.group(1)))
    for m in _INLINE_CODE.finditer(text):
        content = m.group(1)
        if '/' not in content or _skip_target(content) or _in_fence(text, m.start()):
            continue
        if not os.path.exists(os.path.join(root, content)):
            problems.append('line %d: %r does not exist under the repo root'
                            % (_line_of(text, m.start()), content))
    return problems


def _budget_complaints(text):
    problems = []
    lines = len(text.splitlines())
    if lines > MAX_LINES:
        problems.append('the page is %d lines, over the %d-line budget' % (lines, MAX_LINES))
    size = len(text.encode('utf-8'))
    if size > MAX_BYTES:
        problems.append('the page is %d bytes, over the %d-byte budget' % (size, MAX_BYTES))
    return problems


def _heading_complaints(text):
    positions = {}
    for i, line in enumerate(text.splitlines()):
        if line.strip() in HEADINGS and line.strip() not in positions:
            positions[line.strip()] = i
    missing = [h for h in HEADINGS if h not in positions]
    if missing:
        return ['heading %r is missing' % h for h in missing]
    if [positions[h] for h in HEADINGS] != sorted(positions.values()):
        return ['the three part headings are out of order']
    return []


def complaints(text, facts, root):
    """§2.1's five checks over `text`, one string per problem, empty when the page is sound:
    a span whose body differs from its fact (and a fact with no span); a digit run in prose
    outside a span, inline code, a link target and `LITERALS`; a relative link or path that does
    not resolve under `root`; the line and byte budget; the three part headings, absent or out
    of order."""
    found = spans(text)
    numbers = facts.get('numbers', {}) if isinstance(facts, dict) else {}
    return (_drift_complaints(found, numbers) + _digit_complaints(text, found)
            + _link_complaints(text, root) + _budget_complaints(text) + _heading_complaints(text))


def check(repo_root, conv):
    """The committed page and its facts, checked: `complaints`'s list, or `None` when the page
    carries no span — the case that makes `asf readme` safe in front of any product."""
    with open(os.path.join(repo_root, conv.readme), encoding='utf-8') as f:
        text = f.read()
    if not spans(text):
        return None
    facts_path = os.path.join(repo_root, conv.readme_facts)
    if not os.path.isfile(facts_path):
        return ['the facts file %r is missing — run `asf readme --refresh`' % conv.readme_facts]
    with open(facts_path, encoding='utf-8') as f:
        data = json.load(f)
    return complaints(text, data, repo_root)


def _git_root(cwd):
    try:
        out = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True,
                             text=True, timeout=5, cwd=cwd)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return cwd


def cmd_readme(args, root=None):
    """`asf readme [--product P] [--check] [--refresh] [--json]` (§2.3). `--refresh` resolves
    the repo through `env.load_product(...).repo_dir` and the record through
    `asf.cli.resolve_record`, then rewrites the facts file and the page. The default and
    `--check` read the *committed* facts file, need no record, and report drift — `--product`
    resolving is optional here, falling back to the git root (PD7). A README with no span is
    not an error on any form."""
    from asf import conventions as conventions_mod
    from asf import env
    root = root or os.getcwd()
    product_name = getattr(args, 'product', None)
    as_json = getattr(args, 'json', False)

    if getattr(args, 'refresh', False):
        from asf.cli import resolve_record
        product = env.load_product(product_name)
        repo_dir = product.repo_dir or root
        conv = product.conventions
        page_path = os.path.join(repo_dir, conv.readme)
        with open(page_path, encoding='utf-8') as f:
            text = f.read()
        if not spans(text):
            print('readme: no spans — nothing to render')
            return 0
        record_root = resolve_record(args)
        data = facts(record_root, repo_dir, product=product_name)
        facts_path = os.path.join(repo_dir, conv.readme_facts)
        facts_changed = metrics.write_if_changed(facts_path, json.dumps(data, indent=2,
                                                                        sort_keys=True) + '\n')
        print('readme: %s %s' % (conv.readme_facts, 'rewritten' if facts_changed else 'unchanged'))
        page_changed = metrics.write_if_changed(page_path, render(text, data))
        print('readme: %s %s' % (conv.readme, 'rewritten' if page_changed else 'unchanged'))
        return 0

    try:
        product = env.load_product(product_name)
        repo_dir = product.repo_dir
        conv = product.conventions
    except env.ConfigError:
        repo_dir = None
        conv = conventions_mod.Conventions()
    repo_dir = repo_dir or _git_root(root)

    problems = check(repo_dir, conv)
    if problems is None:
        if as_json:
            print(json.dumps({'ok': True, 'complaints': [], 'day': None, 'stale_days': None}))
        else:
            print('readme: no spans — nothing to render')
        return 0

    facts_path = os.path.join(repo_dir, conv.readme_facts)
    day = None
    if os.path.isfile(facts_path):
        with open(facts_path, encoding='utf-8') as f:
            day = json.load(f).get('day')
    if as_json:
        stale_days = None
        if day:
            today = metrics.now_utc().strftime('%Y-%m-%d')
            stale_days = (metrics.dt.date.fromisoformat(today)
                         - metrics.dt.date.fromisoformat(day)).days
        print(json.dumps({'ok': not problems, 'complaints': problems, 'day': day,
                          'stale_days': stale_days}))
        return 1 if problems else 0
    for p in problems:
        print(p)
    return 1 if problems else 0
