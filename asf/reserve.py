"""asf.reserve — B-0151: a factory-level number reservation for a product's declared sequences
(a migration file, a decision band), so two lane branches built in parallel never claim the same
next number.

``asf reserve <sequence>`` returns one past the highest number any of these carries: ``origin``'s
trunk, every open lane branch (unmerged code/fix branches on origin), and every number already
claimed for the sequence — and records the new claim, on ``origin`` itself, before returning, so
a second call made before the first claim's branch is even pushed does not repeat it (two
sessions each seeing only main, and both picking the same migration number, is the incident this
closes). The claim is a ref on origin (:func:`claim`), not a local file: a local ``ASF_HOME``
path is only ever seen by the one runner that wrote it, and a cloud session's runner is fresh
every time (:mod:`asf.workers.cloud`) — two cloud sessions racing before either has pushed their
own branch must still see each other, so the claim has to live somewhere every runner reaches.
Renumbering a landed collision against main at publish time is a separate step, not this one.
"""
import re

from asf import env, gitpush, refguard
from asf.evidence import evidence
from asf.harvest import harvest as H

#: ``refs/heads/reservations/<sequence>/<n>`` is one product's claim ref for one number: created
#: (never updated) on ``origin`` at a fresh, parentless commit (:data:`EMPTY_TREE`) — unrelated
#: to any other commit, so a second create attempt at the same ref is never a fast-forward and
#: git itself refuses it, whichever of two racing pushes origin saw first wins. The name carries
#: no ``code``/``fix``/… prefix, so :func:`open_branch_trees`'s own lane scan already skips it;
#: it is otherwise an ordinary head and outlives the branch it was claimed for on purpose (an
#: abandoned claim is never renumbered — the same accepted cost the old local file had).
RESERVATION_PREFIX = 'reservations'
#: The empty tree's well-known sha (``git hash-object -t tree /dev/null``) — every git object
#: store already has it, so a claim commit needs no blob or tree written first.
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'


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


def _reservation_ref(sequence, n):
    return f'refs/heads/{RESERVATION_PREFIX}/{sequence}/{n}'


def remote_reserved_numbers(product, sequence):
    """Every number already claimed for ``sequence`` on ``origin`` — one ``ls-remote``, the same
    view every runner gets, a fresh cloud checkout included (B-0151 review C1: a local file is
    only ever seen by the one runner that wrote it)."""
    out = H.sh(['git', 'ls-remote', '--heads', 'origin', _reservation_ref(sequence, '*')],
              cwd=product.repo_dir)
    numbers = []
    for line in out.stdout.splitlines():
        _sha, _tab, ref = line.partition('\t')
        tail = ref.rpartition('/')[2]
        if tail.isdigit():
            numbers.append(int(tail))
    return numbers


def claim(product, sequence, n):
    """Create :func:`_reservation_ref` (``sequence``, ``n``) on ``origin``: True once it is this
    call's commit that landed there, False when another session's claim already holds it — a
    fresh, parentless commit (:data:`EMPTY_TREE`) is never a fast-forward of another one, so git
    itself refuses whichever of two racing pushes origin sees second."""
    commit = H.sh(['git', 'commit-tree', EMPTY_TREE, '-m', f'reserve {sequence} {n}'],
                 cwd=product.repo_dir).stdout.strip()
    if not commit:
        raise SystemExit(f'reserve: could not build a claim commit in {product.repo_dir!r}')
    result = gitpush.push(['origin', f'{commit}:{_reservation_ref(sequence, n)}'],
                          product.repo_dir, refs_only=True, guard=refguard.guard_for(product))
    return result.returncode == 0


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


#: How many claims :func:`reserve` retries past a collision before it gives up — generous: a
#: collision only happens while two sessions race the very same number, and each loses at most
#: once before the next candidate is free.
MAX_ATTEMPTS = 50


def reserve(product, sequence):
    """Claim the next number in ``product``'s declared ``sequence``
    (``conventions.sequences``): max(trunk, every open lane branch, every number already
    claimed) + 1, claimed on ``origin`` (:func:`claim`) before it is returned — so a second call
    made before either session's branch is even pushed does not repeat it, cloud sessions
    included (B-0151 review C1). The zero-padded number, as a string. ``SystemExit`` when
    ``sequence`` names no declared pattern, or every candidate up to :data:`MAX_ATTEMPTS`
    collided (heavy, sustained contention — not the incident this closes)."""
    conv = product.conventions
    pattern = conv.map_of('sequences').get(sequence)
    if not pattern:
        raise SystemExit(f"reserve: {sequence!r} is not a declared sequence "
                         f"(conventions.sequences: {{{sequence}: ...}})")
    dirname, _filename = split_pattern(pattern)
    trees = open_branch_trees(product, dirname)
    for _attempt in range(MAX_ATTEMPTS):
        reserved = remote_reserved_numbers(product, sequence)
        n, width = compute_next(pattern, trees, reserved)
        if claim(product, sequence, n):
            return format_number(n, width)
    raise SystemExit(f"reserve: could not claim a number for {sequence!r} after "
                     f"{MAX_ATTEMPTS} collisions")


def cmd_reserve(args, root=None):
    product = env.load_product(args.product)
    print(reserve(product, args.sequence))
    return 0
