"""asf.reviews — the table a review ends in, and the verdict derived from it.

A gate returns one check per thing it looked at. A review file carries one or two tables of
`| check | result | evidence |`; this module is the only thing in the factory that parses them,
the only thing that knows which checks a kind of review must cover, and the only thing that
turns rows into a verdict. Nothing here opens a file, runs git or knows a path.
"""
import collections
import re

MECHANICAL, STANDARD = 1, 2
PASS, FAIL, NA = 'pass', 'fail', 'n/a'
APPROVED, CHANGES, BOUNCE = 'APPROVED', 'CHANGES REQUESTED', 'BOUNCE'
RESULTS = {PASS, FAIL, NA}
#: The result column may be headed `result` or the card's `ok`.
HEADERS = (('check', 'result', 'evidence'), ('check', 'ok', 'evidence'))
KINDS = ('spec', 'plan', 'code')

Check = collections.namedtuple('Check', 'name result evidence block line')

CHECKLIST = {
    'spec': ((  # the mechanical pass
        'the five sections are present, in order',
        '`## Stories` is last, and every Story line names a test',
        'every acceptance test is a fenced block that can be run',
        'every id the spec mints is inside the session range',
        'no section is empty and no decision row is missing its why',
    ), (        # the standard
        'the spec is the card, no wider and no narrower',
        'a plan could be cut from this without asking a question',
    )),
    'plan': ((
        'one Task per Story, and no Story unclaimed',
        "every Task declares `writes:`, and no two Tasks' footprints overlap",
        "every Task's acceptance fences are byte-identical to the spec's",
        'the stated order is acyclic and every `after:` names a real Task',
    ), (
        'each Task is one session of work',
        'the order is the order the work actually needs',
    )),
    'code': ((
        'the diff stays inside `writes:`',
        'every Step of the Task is implemented',
        "the acceptance tests are byte-identical to the plan's",
        'those tests were run and are green',
        'the Gate commands are green',
        'no secret value printed, no background process, no skipped check',
    ), (
        'the change reads like the code around it',
        'the test would fail if the change were reverted',
    )),
}

#: A review's own typed verdict line: `verdict: approved`, `**Verdict:** changes requested`.
_TYPED_LINE_RE = re.compile(r'^[\s*#>|_-]*verdict[\s*_]*:[\s*_`]*(?P<v>[^\n|]+)', re.I | re.M)
#: A legacy verdict word anywhere in the text, when no `verdict:` line is found.
_TYPED_WORD_RE = re.compile(r'APPROVED|CHANGES REQUESTED|BOUNCE|REVISE', re.I)
#: A table separator row: `| --- | --- | --- |` (optional alignment colons).
_SEPARATOR_RE = re.compile(r':?-{3,}:?')
#: A result cell that still carries the unfilled skeleton, or is otherwise malformed.
_UNFILLED = 'unfilled'


def cells(line):
    """Split a markdown table row on unescaped `|`; a literal pipe is escaped `\\|`."""
    c = re.split(r'(?<!\\)\|', line.strip())
    if c and not c[0].strip():
        c = c[1:]
    if c and not c[-1].strip():
        c = c[:-1]
    return [x.replace('\\|', '|').strip() for x in c]


def _header_cells(line):
    return tuple(c.replace('`', '').replace('*', '').strip().lower() for c in cells(line))


def _is_separator(line):
    if not line.strip().startswith('|'):
        return False
    c = cells(line)
    return bool(c) and all(_SEPARATOR_RE.fullmatch(x.strip()) for x in c)


def parse(text):
    """(`[Check, …]`, `[fault, …]`). A table begins at a line whose cells, lower-cased and
    stripped of backticks and `*`, are one of `HEADERS`, and whose next line is a separator —
    otherwise no table is found there, no rows, no fault. Rows run until the first line that
    does not start with `|`; blocks are numbered from 1 in order of appearance. A row whose cell
    count is not 3 is a fault naming its line, not a row. A result cell not in `RESULTS` is a
    fault naming its line — except a cell still carrying the skeleton (`<pass|fail>`, or holding
    any `|` or `<`), which is reported as `'unfilled'` rather than a fault: it is a row that has
    not yet been answered. An empty evidence cell makes an otherwise-answered row `fail`.
    """
    lines = (text or '').splitlines()
    checks, faults = [], []
    block, i, n = 0, 0, len(lines)
    while i < n:
        line = lines[i]
        if line.strip().startswith('|'):
            if _header_cells(line) in HEADERS and i + 1 < n and _is_separator(lines[i + 1]):
                block += 1
                i += 2
                while i < n and lines[i].strip().startswith('|'):
                    row = cells(lines[i])
                    if len(row) != 3:
                        faults.append(f'line {i + 1}: expected 3 cells, found {len(row)}')
                    else:
                        name, result, evidence = row
                        r = result.strip().lower()
                        if r in RESULTS:
                            res = FAIL if not evidence.strip() else r
                            checks.append(Check(name=name, result=res, evidence=evidence,
                                                block=block, line=i + 1))
                        elif '<' in result or '|' in result:
                            checks.append(Check(name=name, result=_UNFILLED, evidence=evidence,
                                                block=block, line=i + 1))
                        else:
                            faults.append(f'line {i + 1}: invalid result "{result}"')
                    i += 1
                continue
        i += 1
    return checks, faults


def normalize(name):
    """Lower-case, collapse whitespace, strip backticks, `*`, a leading `-` and trailing
    `.,;:` — the one comparison used for coverage and for acceptance matching."""
    s = (name or '').replace('`', '').replace('*', '').strip()
    if s.startswith('-'):
        s = s[1:].strip()
    s = re.sub(r'\s+', ' ', s.lower())
    return s.rstrip('.,;:').strip()


def required(kind):
    """The normalized mechanical names of `CHECKLIST[kind]`, in checklist order."""
    return tuple(normalize(name) for name in CHECKLIST[kind][0])


def missing(checks, required_names):
    """The names of `required_names` no row of `checks` covers, in checklist order."""
    have = {normalize(c.name) for c in checks}
    return [name for name in required_names if name not in have]


def verdict(text, required=()):
    """`''` when no table is present at all; `BOUNCE` when `parse` returned a fault, a row is
    unfilled, or a required check is missing; `CHANGES` when any row is `fail`; else `APPROVED`.
    An `n/a` row is neither a fail nor a missing check."""
    checks, faults = parse(text)
    if not checks and not faults:
        return ''
    if faults or any(c.result == _UNFILLED for c in checks) or missing(checks, required):
        return BOUNCE
    if any(c.result == FAIL for c in checks):
        return CHANGES
    return APPROVED


def typed_verdict(text):
    """The legacy word: a `verdict:`-prefixed line first, else the first verdict word anywhere
    in the text; `''` when neither is found."""
    if not text:
        return ''
    m = _TYPED_LINE_RE.search(text)
    word = m.group('v') if m else None
    if word is None:
        m = _TYPED_WORD_RE.search(text)
        word = m.group(0) if m else None
    if word is None:
        return ''
    w = word.strip().strip('`*_ ').upper()
    if w.startswith('APPROVED'):
        return APPROVED
    if w.startswith('BOUNCE'):
        return BOUNCE
    return CHANGES


def faults(text, required_names):
    """Every line the pre-review prints: the parse faults, one `result is unfilled` line per
    unanswered row, then one `required check missing: "<name>"` per missing name."""
    checks, structural = parse(text)
    out = list(structural)
    for c in checks:
        if c.result == _UNFILLED:
            out.append(f'line {c.line}: result is unfilled')
    for name in missing(checks, required_names):
        out.append(f'required check missing: "{name}"')
    return out


def render_table(rows):
    """The skeleton: `| check | result | evidence |`, the separator, and one
    `| <name> | <pass\\|fail> | |` per row — the escaped pipe, so the skeleton re-parses as one
    cell."""
    lines = ['| check | result | evidence |', '| --- | --- | --- |']
    lines += [f'| {name} | <pass\\|fail> | |' for name in rows]
    return '\n'.join(lines)


def passed(checks):
    """The normalized names of every `PASS` row."""
    return [normalize(c.name) for c in checks if c.result == PASS]
