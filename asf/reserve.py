"""asf.reserve — B-0151: a factory-level number reservation for a product's declared sequences
(a migration file, a decision band), so two lane branches built in parallel never claim the same
next number.

``asf reserve <sequence>`` returns one past the highest number any of these carries: ``origin``'s
trunk, every open lane branch (unmerged code/fix branches on origin), and every number this
product has already reserved for the sequence — and records the new claim before returning, so a
second call made before the first claim's branch is even pushed does not repeat it (two sessions
each seeing only main, and both picking the same migration number, is the incident this closes).
Renumbering a landed collision against main at publish time is a separate step, not this one.
"""
import json
import os
import re

from asf import env
from asf.evidence import evidence

#: The reservations file for one product's sequence, under its own state dir
#: (:func:`asf.env.state_dir`) — never the product repo, so a claim survives a branch that is
#: never pushed without leaving a trace an operator has to clean up.
RESERVATIONS_FILE = 'reservations-%s.json'


def pattern_regex(filename_pattern):
    """A sequence's filename pattern (``NNNN_*.sql``) -> ``(regex, width)``: the run of ``N``s is
    a capture group of that many digits (``width``), ``*`` is a glob wildcard, anything else is
    literal. Raises ``ValueError`` when the pattern names no ``N`` run — a sequence with nothing
    to reserve."""
    out, width, i = [], None, 0
    while i < len(filename_pattern):
        c = filename_pattern[i]
        if c == 'N':
            j = i
            while j < len(filename_pattern) and filename_pattern[j] == 'N':
                j += 1
            width = j - i
            out.append(r'(\d{%d})' % width)
            i = j
        elif c == '*':
            out.append('.*')
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    if width is None:
        raise ValueError(f"sequence pattern {filename_pattern!r} names no number field (NNNN)")
    return re.compile('^' + ''.join(out) + '$'), width


def split_pattern(pattern):
    """``db/migrations/NNNN_*.sql`` -> ``('db/migrations', 'NNNN_*.sql')``."""
    dirname, _slash, filename = pattern.rpartition('/')
    return dirname, filename


def numbers_in(names, regex):
    """Every number ``regex`` (from :func:`pattern_regex`) finds among ``names``."""
    matches = (regex.match(name) for name in names)
    return [int(m.group(1)) for m in matches if m]


def compute_next(pattern, trees, reserved=()):
    """The next number for ``pattern``: one past the highest number any of ``trees`` (a mapping
    of source label -> the filenames it carries — the trunk and every open lane branch) or
    ``reserved`` (numbers this product already claimed, not yet necessarily on any branch)
    carries. Returns ``(n, width)``; ``width`` is the pattern's zero-pad width."""
    _dirname, filename_pattern = split_pattern(pattern)
    regex, width = pattern_regex(filename_pattern)
    found = [n for names in trees.values() for n in numbers_in(names, regex)]
    found.extend(reserved)
    return max(found, default=0) + 1, width


def format_number(n, width):
    return str(n).zfill(width)


def _reservations_path(product, sequence):
    return os.path.join(env.state_dir(product), RESERVATIONS_FILE % sequence)


def _load_reservations(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return [int(n) for n in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_reservations(path, numbers):
    tmp = f'{path}.{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(numbers, f)
    os.replace(tmp, path)


def open_branch_trees(product, dirname):
    """``{source label -> [filenames]}`` for ``origin/<main>`` and every open lane branch (a
    code or fix branch with no merged PR) — the same "unmerged work on origin" evidence read
    :func:`asf.evidence.evidence.migrate_sources` already does for hotfix reports, reused here
    for a sequence's directory."""
    conv = product.conventions
    branches = evidence.remote_branches(product=product)
    prs = evidence.pr_list(product=product)
    pr_by_head = {}
    for p in prs:
        pr_by_head.setdefault(p.get('headRefName') or '', []).append(p)
    prefixes = evidence.branch_prefixes(product)
    work = tuple({prefixes[k] for k in ('code', 'fix') if prefixes.get(k)})
    open_branches = sorted(
        b for b in branches
        if b != conv.main and b.startswith(work)
        and not any(p.get('state') == 'MERGED' for p in pr_by_head.get(b, [])))
    revs = [f'origin/{conv.main}'] + [f'origin/{b}' for b in open_branches]
    trees = evidence.read_trees([f'{r}:{dirname}' for r in revs], product=product)
    return {r: list(trees.get(f'{r}:{dirname}', {})) for r in revs}


def reserve(product, sequence):
    """Claim the next number in ``product``'s declared ``sequence``
    (``conventions.sequences``): max(trunk, every open lane branch, every number this product
    already reserved) + 1, recorded before it is returned. The zero-padded number, as a string.
    ``SystemExit`` when ``sequence`` names no declared pattern."""
    conv = product.conventions
    pattern = conv.map_of('sequences').get(sequence)
    if not pattern:
        raise SystemExit(f"reserve: {sequence!r} is not a declared sequence "
                         f"(conventions.sequences: {{{sequence}: ...}})")
    dirname, _filename = split_pattern(pattern)
    trees = open_branch_trees(product, dirname)
    path = _reservations_path(product, sequence)
    reserved = _load_reservations(path)
    n, width = compute_next(pattern, trees, reserved)
    _save_reservations(path, reserved + [n])
    return format_number(n, width)


def cmd_reserve(args, root=None):
    product = env.load_product(args.product)
    print(reserve(product, args.sequence))
    return 0
