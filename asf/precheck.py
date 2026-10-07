"""asf.precheck — the mechanical pass that reads a code branch before the expensive review.

The level is derived, not configured per branch: the Task's own size class
(:func:`asf.size.classify`) and the diff's changed lines decide it, and the level decides the
dimensions the pass must cover and the model that covers them. The findings land as one table;
this module is the only thing that parses it, and the only thing that says which of its rows
sends a branch back. Nothing here opens a file, runs git or knows a path.
"""
import collections
import re

from asf import size

LOW, HIGH, MAX = 'low', 'high', 'max'
LEVELS = (LOW, HIGH, MAX)
PASS, FAIL, NA = 'pass', 'fail', 'n/a'
RESULTS = {PASS, FAIL, NA}
#: The confidence a finding is stated at; only HIGH sends a branch back (D6).
CONF_HIGH, CONF_MED, CONF_LOW = 'high', 'medium', 'low'
CONFIDENCES = {CONF_HIGH, CONF_MED, CONF_LOW, NA}
HEADER = ('check', 'result', 'confidence', 'evidence')
#: The correction kind and the brief kind have one owner: this name, spelled nowhere else.
KIND = 'precheck'

Row = collections.namedtuple('Row', 'check result confidence evidence line')

#: The dimensions each level must cover, cumulative: HIGH is LOW's plus its own, MAX is HIGH's.
DIMENSIONS = {
    LOW: (
        'the diff stays inside the declared footprint',
        'every changed function is reachable, and every caller it changed still compiles',
        'the tests the change names were run, and their last line is green',
        'no secret value, no host name and no account name is printed or committed',
    ),
    HIGH: (
        'every error path added is reached by something, and raises or returns what its caller reads',
        'every value read from outside the process is checked before it is used',
        'a changed behaviour has a test that fails when the change is reverted',
        'nothing added duplicates something the repo already has',
    ),
    MAX: (
        'each touched file was read whole, not only its hunks',
        'each finding was re-derived from the code before it was written down',
        'the change holds under concurrency, retry and partial failure',
    ),
}

#: The rows any diff touching a sensitive class owes, whatever the class.
SECURITY_DIMENSIONS = (
    'the diff adds no new trust in a value that comes from outside the process',
    'nothing the diff adds weakens or removes an existing check, guard or hook',
    'no secret value appears in the diff, a fixture, a test or a log line it adds',
)
#: The row one touched class owes. The class name is the product's; this sentence is not.
CLASS_DIMENSION = ('{name}: every path the diff adds or changes here enforces what the code '
                    'around it enforces, and the attack an outsider would try is named')

#: A row's own ``head:`` line, the same one-line rule as ``asf.evidence.review.head_of``.
_HEAD_LINE_RE = re.compile(r'^[\s*#>|_-]*head[\s*_]*:[\s*_`]*(?P<sha>[0-9a-f]{7,40})\b', re.I | re.M)
#: A row's own ``level:`` line (PD7) — the peer of ``head:``.
_LEVEL_LINE_RE = re.compile(r'^[\s*#>|_-]*level[\s*_]*:[\s*_`]*(?P<level>low|high|max)\b', re.I | re.M)
_UNESCAPED_PIPE_RE = re.compile(r'(?<!\\)\|')
_SEP_CELL_RE = re.compile(r'^[-:]+$')
_LEADING_TOKEN_RE = re.compile(r'^\S+')


def class_dimension(name):
    """:data:`CLASS_DIMENSION` for one class name."""
    return CLASS_DIMENSION.format(name=name)


def security_dimensions(hit):
    """The dimensions a diff touching ``hit`` (``{class: [file]}``) owes: one per class, in the
    order the configuration named them, then :data:`SECURITY_DIMENSIONS`. ``()`` for ``{}``."""
    if not hit:
        return ()
    return tuple(class_dimension(name) for name in hit) + SECURITY_DIMENSIONS


def dimensions(level, security=()):
    """The level's cumulative list, then ``security`` — LOW's first, the classes last. An unknown
    level reads as HIGH's (D4's rule for an unknown class, read the same way); never empty."""
    if level not in LEVELS:
        level = HIGH
    out = []
    for lvl in (LOW, HIGH, MAX):
        out.extend(DIMENSIONS[lvl])
        if lvl == level:
            break
    out.extend(security)
    return tuple(out)


def level_for(size_class, changed_lines, max_lines, security=False):
    """``(level, why)`` (D3/D4). ``max`` when ``changed_lines`` is known and over ``max_lines``;
    else ``low`` for :data:`asf.size.SMALL` and ``high`` for anything else, an unknown class
    included — floored at :data:`HIGH` when ``security``, a sensitive diff never read at
    :data:`LOW`, whatever its footprint says. ``why`` is the sentence the row, the brief and the
    lane line all print; the floor names itself: ``size class small, floored by sensitive
    paths``."""
    if changed_lines is not None and max_lines and changed_lines > max_lines:
        return MAX, f'{changed_lines} changed lines > {max_lines}'
    if size_class == size.SMALL:
        if security:
            return HIGH, f'size class {size.SMALL}, floored by sensitive paths'
        return LOW, f'size class {size.SMALL}'
    return HIGH, f'size class {size_class or "unknown"}'


def _cells(line):
    """A ``|``-delimited row split on unescaped ``|``, its leading and trailing empty cells
    dropped, each cell's ``\\|`` unescaped and stripped."""
    parts = _UNESCAPED_PIPE_RE.split(line)
    if parts and parts[0] == '':
        parts = parts[1:]
    if parts and parts[-1] == '':
        parts = parts[:-1]
    return [p.replace('\\|', '|').strip() for p in parts]


def _norm(text):
    """Lowercase, whitespace collapsed to one space, ``*`` and backticks stripped, trailing
    punctuation stripped — the one normalizer the header match and :func:`covered` both use."""
    t = (text or '').lower().replace('*', '').replace('`', '')
    t = re.sub(r'\s+', ' ', t).strip()
    return re.sub(r'[.,:;!?)]+$', '', t)


def _unfilled(cell):
    return '<' in cell or '|' in cell


def parse(text):
    """``([Row, …], [fault, …])``. The table begins at the first line whose four cells,
    normalized, are :data:`HEADER`; the next line must be a separator (every cell only ``-``
    and ``:``); with neither, ``([], [])`` — no table found is not a fault. Rows run from there
    to the first line not starting with ``|``, each ``Row.line`` its 1-based line in ``text``. A
    row whose cell count is not 4 is a fault, and no Row. A ``result`` or ``confidence`` cell
    still carrying the skeleton (holding ``<`` or an inner ``|``) is a fault, and the row is kept
    with that cell's value as it stands, so it can never satisfy :func:`covered` or read as a
    pass. A result outside :data:`RESULTS`, or a confidence outside :data:`CONFIDENCES`, is a
    fault — unfilled is reported as unfilled and not as either. A FAIL row with an empty evidence
    cell is a fault: a finding with no ``file:line`` is not a finding."""
    lines = (text or '').splitlines()
    header_i = None
    for i, line in enumerate(lines):
        cells = _cells(line)
        if len(cells) == 4 and tuple(_norm(c) for c in cells) == HEADER:
            header_i = i
            break
    if header_i is None:
        return [], []
    if header_i + 1 >= len(lines):
        return [], []
    sep_cells = _cells(lines[header_i + 1])
    if len(sep_cells) != 4 or not all(_SEP_CELL_RE.match(c) for c in sep_cells):
        return [], []

    rows = []
    faults = []
    for i in range(header_i + 2, len(lines)):
        line = lines[i]
        if not line.startswith('|'):
            break
        lineno = i + 1
        cells = _cells(line)
        if len(cells) != 4:
            faults.append(f'row {lineno}: {len(cells)} cells, expected 4')
            continue
        check, result, confidence, evidence = cells
        result_unfilled, confidence_unfilled = _unfilled(result), _unfilled(confidence)
        if result_unfilled:
            faults.append(f'row {lineno}: result is unfilled ({result})')
        if confidence_unfilled:
            faults.append(f'row {lineno}: confidence is unfilled ({confidence})')
        if not result_unfilled and result not in RESULTS:
            faults.append(f'row {lineno}: result "{result}" is not pass/fail/n/a')
        if not confidence_unfilled and confidence not in CONFIDENCES:
            faults.append(f'row {lineno}: confidence "{confidence}" is not high/medium/low/n/a')
        if not result_unfilled and result == FAIL and not evidence:
            faults.append(f'row {lineno}: fail row has empty evidence')
        rows.append(Row(check=check, result=result, confidence=confidence, evidence=evidence,
                         line=lineno))
    return rows, faults


def covered(rows, level, security=()):
    """The dimensions of :func:`dimensions` ``(level, security)`` no row's normalized ``check``
    matches, in level order — the pass's own completeness."""
    checks = {_norm(r.check) for r in rows}
    return tuple(d for d in dimensions(level, security) if _norm(d) not in checks)


def findings(rows):
    """The FAIL rows, in file order."""
    return [r for r in rows if r.result == FAIL]


def blocking(rows):
    """The FAIL rows at :data:`CONF_HIGH` — non-empty is the bounce (D6)."""
    return [r for r in rows if r.result == FAIL and r.confidence == CONF_HIGH]


def keys(rows):
    """The sorted, unique leading ``file:line`` token of each blocking row's evidence — the
    hold's finding, ``c_items``' shape (``asf/evidence/review.py:180``), read the same way, so
    ``same_finding`` compares like with like."""
    out = set()
    for row in blocking(rows):
        m = _LEADING_TOKEN_RE.match((row.evidence or '').strip())
        if m:
            out.add(m.group(0).rstrip(':,;'))
    return sorted(out)


def faults(text, level, security=()):
    """The lines ``asf precheck`` prints: :func:`parse`'s own faults, then one
    ``dimension not covered: "<name>"`` per uncovered dimension (a class row included), then
    ``no head: line`` when :func:`head_of` finds none, then ``no level: line`` when
    :func:`level_of` finds none (PD7)."""
    rows, out = parse(text)
    out = list(out)
    for name in covered(rows, level, security):
        out.append(f'dimension not covered: "{name}"')
    if head_of(text) is None:
        out.append('no head: line')
    if level_of(text) is None:
        out.append('no level: line')
    return out


def render_table(level, security=()):
    """The skeleton the brief carries: the header, the separator, and one row per dimension of
    ``level`` then ``security``, so :func:`parse` finds one row per dimension and exactly two
    *unfilled* faults per row."""
    header = '| ' + ' | '.join(HEADER) + ' |'
    sep = '| ' + ' | '.join('---' for _ in HEADER) + ' |'
    rows = [f'| {dim} | <pass\\|fail> | <n/a\\|high\\|medium\\|low> | '
            for dim in dimensions(level, security)]
    return '\n'.join([header, sep, *rows])


def head_of(text):
    """The sha the file's own ``head:`` line names, or None — the same one-line rule as
    ``asf.evidence.review.head_of``."""
    m = _HEAD_LINE_RE.search(text or '')
    return m.group('sha').lower() if m else None


def level_of(text):
    """The file's own ``level:`` line value (``low``/``high``/``max``), or None (PD7)."""
    m = _LEVEL_LINE_RE.search(text or '')
    return m.group('level').lower() if m else None


# ---- the command ----
# Everything above this line opens no file, runs no git and knows no path (§2.1's own promise).
# Everything below it is the one part of this module that does: the pass's own self-check
# (`asf precheck PATH --level …`) and the report over what landed (`asf precheck report`) — the
# shape `asf/redact.py`'s `add_arguments`/`cmd_redact`/`register`/`main` already has.
import argparse
import datetime
import json
import os
import sys

from asf.evidence import review as review_mod

#: ``precheck-<slug>.md`` (D2's default pattern) — the one name this command reads off disk.
_PRECHECK_FILE_RE = re.compile(r'^precheck-(?P<slug>.+)\.md$', re.I)
#: ``<n>-<slug>.md`` — the default review pattern (`conventions.DEFAULT_REVIEW_PATTERN`), read the
#: same way, beside the precheck file, in the same directory.
_REVIEW_FILE_RE = re.compile(r'^(?P<n>\d+)-(?P<slug>.+)\.md$', re.I)
#: A leading ``file:line`` evidence token with its line dropped — what a review's bare-path C
#: item (``asf.evidence.review.c_items``) is compared against.
_PATH_OF_RE = re.compile(r'^(?P<path>[^:]+)')


def add_arguments(p):
    p.add_argument('target', help="a precheck file's path, or the literal 'report'")
    p.add_argument('--level', choices=LEVELS)
    p.add_argument('--json', action='store_true')
    p.add_argument('--days', type=int, default=30, help="report's own window (default 30)")
    from asf import env
    env.add_product_arg(p)


def register(subparsers):
    p = subparsers.add_parser(
        'precheck', help="the mechanical pass's own self-check, and the report over what it found")
    add_arguments(p)
    p.set_defaults(run=cmd_precheck)
    return p


def main(argv=None):
    parser = argparse.ArgumentParser(prog='asf.precheck')
    add_arguments(parser)
    args = parser.parse_args(argv)
    return cmd_precheck(args)


def _fault_line(path, fault):
    """One :func:`faults` entry, prefixed ``<path>:<line>: `` when it names a file line (a parse
    fault's own ``row <n>: …``), else ``<path>: `` (a dimension/head/level fault, which names
    none) — the same shape ``asf.review_checks._fault_line`` gives a review's own faults."""
    if fault.startswith('row '):
        n, _, rest = fault[len('row '):].partition(': ')
        return f'{path}:{n}: {rest}'
    return f'{path}: {fault}'


def cmd_precheck(args):
    if args.target == 'report':
        return cmd_precheck_report(args)
    if not args.level:
        print('usage: asf precheck PATH --level {low,high,max} [--json]', file=sys.stderr)
        return 2
    try:
        with open(args.target, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        print(f'{args.target}: {e}', file=sys.stderr)
        return 2

    rows, _ = parse(text)
    flts = faults(text, args.level)
    bl = blocking(rows)
    fnd = findings(rows)

    if args.json:
        payload = {'path': args.target, 'level': args.level, 'head': head_of(text),
                   'rows': [dict(r._asdict()) for r in rows], 'findings': len(fnd),
                   'blocking': len(bl), 'faults': flts}
        print(json.dumps(payload, sort_keys=True))
    else:
        plural = '' if len(flts) == 1 else 's'
        print(f'== PRECHECK {args.target}  level {args.level}, {len(rows)} rows, '
              f'{len(fnd)} findings ({len(bl)} high), {len(flts)} fault{plural}')
        for fault in flts:
            print(_fault_line(args.target, fault))
        tail = ' — this branch will be sent back' if bl else ''
        print(f'blocking: {len(bl)}{tail}')
    return 1 if flts else 0


def _fail_paths(rows):
    """The file path (its line number dropped) of every FAIL row's evidence, over *every* FAIL
    row — not only the blocking ones: a `medium` finding the reviewer re-raises is exactly what
    the report's ``repeated`` count is for. Mirrors :func:`keys` without :func:`blocking`'s
    confidence filter."""
    out = set()
    for row in findings(rows):
        m = _LEADING_TOKEN_RE.match((row.evidence or '').strip())
        if m:
            tok = m.group(0).rstrip(':,;')
            out.add(_PATH_OF_RE.match(tok).group('path'))
    return out


def _confidence_counts(rows):
    """The FAIL rows of ``rows``, tallied by confidence."""
    counts = {CONF_HIGH: 0, CONF_MED: 0, CONF_LOW: 0}
    for row in findings(rows):
        if row.confidence in counts:
            counts[row.confidence] += 1
    return counts


def _scan_reviews_dir(reviews_dir):
    """``({slug: precheck path}, {slug: [(round, review path), …]})`` over one directory, read
    straight off disk — no git, no network."""
    try:
        names = sorted(os.listdir(reviews_dir))
    except OSError:
        return {}, {}
    precheck_files, review_files = {}, {}
    for name in names:
        path = os.path.join(reviews_dir, name)
        m = _PRECHECK_FILE_RE.match(name)
        if m:
            precheck_files[m.group('slug').lower()] = path
            continue
        m = _REVIEW_FILE_RE.match(name)
        if m:
            review_files.setdefault(m.group('slug').lower(), []).append(
                (int(m.group('n')), path))
    return precheck_files, review_files


def cmd_precheck_report(args):
    from asf import env
    try:
        product = env.load_product(args.product)
    except env.ConfigError as e:
        print(f'precheck report: {e}', file=sys.stderr)
        return 2

    root = product.repo_dir
    reviews_dir = os.path.join(root, product.conventions.reviews_dir) if root else ''
    precheck_files, review_files = _scan_reviews_dir(reviews_dir)
    cutoff = datetime.datetime.now().timestamp() - args.days * 86400

    out = []
    for slug in sorted(set(precheck_files) & set(review_files)):
        ppath = precheck_files[slug]
        try:
            mtime = os.path.getmtime(ppath)
            with open(ppath, encoding='utf-8') as f:
                ptext = f.read()
        except OSError:
            continue
        if mtime < cutoff:
            continue

        prows, _ = parse(ptext)
        level = level_of(ptext) or HIGH
        fail_paths = _fail_paths(prows)
        rounds, repeated_total, new_total = [], 0, 0
        for n, rpath in sorted(review_files[slug]):
            try:
                with open(rpath, encoding='utf-8') as f:
                    body = f.read()
            except OSError:
                continue
            items = review_mod.c_items(body)
            repeated = sum(1 for p in items if p in fail_paths)
            new = len(items) - repeated
            repeated_total += repeated
            new_total += new
            rounds.append({'round': n, 'c': len(items), 'repeated': repeated, 'new': new})

        out.append({'item': slug.upper(), 'level': level, 'findings': _confidence_counts(prows),
                    'rounds': rounds, 'repeated': repeated_total, 'new': new_total})

    if args.json:
        print(json.dumps({'days': args.days, 'items': out}, sort_keys=True))
        return 0

    print(f'== PRECHECK REPORT  last {args.days} days, {len(out)} item(s)')
    for row in out:
        f = row['findings']
        print(f"{row['item']}  level {row['level']}, findings high {f[CONF_HIGH]} "
              f"medium {f[CONF_MED]} low {f[CONF_LOW]}, {len(row['rounds'])} round(s), "
              f"repeated {row['repeated']}, new {row['new']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
