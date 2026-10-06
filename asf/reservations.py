"""asf.reservations — what every open ref has booked in a product's declared sequences, merged
into one map, so a plan is never approved that books a number another open branch already holds
(F-0208: a plan landed a migration band 0289-0290 on the trunk while an open code branch, in a
pull request of its own, already held 0289 — the planner had read the trunk and nothing else).

Three readers feed one map, because no one of them can see a whole booking:

* **added** — a file the ref's own diff adds under a sequence's directory. A coder's booking.
* **writes** — a number a `writes:` glob of an open card names. A *plan's* booking: a spec/plan
  lane branch is documents-only (`asf.harvest.lane.landing_class`), so a plan cannot book by
  adding the file — it books by writing a Task whose footprint names the number.
* **command** — the lines `conventions.reservations` prints, for a number no filename carries.

The map decides nothing on its own: :func:`holder` names the one ref that keeps a number and
:func:`refusal` is the words for every other ref that booked it. The rank is the pull request's
number, so the older claim holds and the newcomer re-books.
"""
import collections
import json
import os
import shlex

from asf.evidence import evidence
from asf.feeder import rows as feeder_rows
from asf.harvest import harvest as H
from asf.workers.pool import now_iso
from asf import reserve

#: A booking: one number, in one sequence, by one ref, read from one source.
Booking = collections.namedtuple('Booking', 'sequence number ref source')
ADDED, WRITES, COMMAND = 'added', 'writes', 'command'
#: The correction kind of a clash — :data:`asf.workers.lifecycle.RESERVED`, spelled once there.
FILE = 'reservations.json'
#: The most numbers one sequence contributes to the brief's line before it says how many are left.
BRIEF_LIMIT = 12


def sequences(conv):
    """``{name: (dirname, regex, width)}`` for every declared sequence whose pattern reads
    (:func:`asf.reserve.pattern_regex`); one that names no number field is skipped, not raised —
    a misdeclared sequence must not stop a lane pass."""
    out = {}
    for name, pattern in conv.map_of('sequences').items():
        dirname, filename = reserve.split_pattern(pattern)
        try:
            regex, width = reserve.pattern_regex(filename)
        except ValueError:
            continue
        out[name] = (dirname, regex, width)
    return out


def open_refs(product, heads, prs):
    """``[ref]`` — the trunk first, then every open lane branch: a head under any prefix the
    product declares (``legacy`` included) that is not the trunk and has no MERGED pull request.
    One definition, shared with :func:`asf.reserve.open_branch_trees` (B-0151 read ``code`` and
    ``fix`` alone, so a plan's booking was invisible to it — half of F-0208)."""
    conv = product.conventions
    trunk = conv.main
    lane_branches = sorted(
        b for b in heads or ()
        if b != trunk and conv.branch_kind(b) is not None
        and str((prs.get(b) or {}).get('state') or '').upper() != 'MERGED')
    return [trunk] + lane_branches


def rank(ref, trunk, prs):
    """How old a ref's claim is, lowest first: ``(-1, '')`` for the trunk (a landed number is not
    negotiable), ``(0, <pr number>)`` for a ref with a pull request, ``(1, ref)`` for one
    without — a branch that has opened no pull request yet is the newcomer and re-books.
    Consequence, named not fixed (C5): two ref-less branches are ordered by name, deterministic
    and arbitrary; the loser re-books."""
    if ref == trunk:
        return (-1, '')
    number = (prs.get(ref) or {}).get('number')
    if number is not None:
        return (0, number)
    return (1, ref)


def added_files(repo, trunk, ref):
    """The paths ``origin/<ref>``'s own commits *add* since it left the trunk —
    ``git diff --name-only --diff-filter=A origin/<trunk>...origin/<ref>``. The sibling of
    :func:`asf.harvest.lane.touched_files` and different from it in one flag on purpose: that one
    lists edits too, and a branch that fixes a typo in a landed ``0201_*.sql`` books nothing."""
    r = H.sh(['git', 'diff', '--name-only', '--diff-filter=A',
              f'origin/{trunk}...origin/{ref}'], cwd=repo)
    return [l for l in r.stdout.splitlines() if l.strip()]


def added_bookings(product, seqs, refs):
    """A :data:`ADDED` booking per number a ref adds under a sequence's directory, plus the
    trunk's own: for the trunk the whole directory listing counts (``origin/<trunk>:<dirname>``
    through :func:`asf.evidence.evidence.read_trees`, the read B-0151 already makes), because
    everything on the trunk is held."""
    trunk = product.conventions.main
    dirnames = sorted({dirname for dirname, _regex, _width in seqs.values()})
    trunk_rev = f'origin/{trunk}'
    trunk_trees = evidence.read_trees([f'{trunk_rev}:{d}' for d in dirnames], product=product)
    added_by_ref = {ref: added_files(product.repo_dir, trunk, ref)
                    for ref in refs if ref != trunk}
    out = []
    for name, (dirname, regex, _width) in seqs.items():
        trunk_names = list(trunk_trees.get(f'{trunk_rev}:{dirname}', {}))
        for n in reserve.numbers_in(trunk_names, regex):
            out.append(Booking(name, n, trunk, ADDED))
        for ref, added in added_by_ref.items():
            names = [os.path.basename(f) for f in added if os.path.dirname(f) == dirname]
            for n in reserve.numbers_in(names, regex):
                out.append(Booking(name, n, ref, ADDED))
    return out


def writes_bookings(product, items, seqs):
    """A :data:`WRITES` booking per number an open card's ``writes:`` glob names, attributed to
    the branch carrying that card's plan while the plan is off the trunk
    (:func:`asf.feeder.rows.plan_carrier`) and to the card's own lane branch once it has landed.
    A glob books only when its filename half matches the sequence's regex, so
    ``db/migrations/*`` and ``029[01]_*.sql`` book nothing."""
    items = items or {}
    out = []
    for item_id, item in items.items():
        if not feeder_rows.is_open(item):
            continue
        globs = item.get('writes') or []
        if not globs:
            continue
        feature = feeder_rows.feature_of(items, item)
        carrier = feeder_rows.plan_carrier(feature) if feature else ''
        kind = 'fix' if item.get('type') == 'bug' else 'code'
        branch = carrier or feeder_rows.branch_for(product, kind, item_id)
        for glob in globs:
            dirname, filename = reserve.split_pattern(glob)
            for name, (seq_dir, regex, _width) in seqs.items():
                if dirname != seq_dir:
                    continue
                for n in reserve.numbers_in([filename], regex):
                    out.append(Booking(name, n, branch, WRITES))
    return out


def command_bookings(product, refs):
    """``([Booking], error)`` from ``conventions.reservations``: one
    :func:`asf.harvest.harvest.sh_timed` run in ``product.repo_dir`` under
    :func:`asf.harvest.harvest.gate_env`, with every ref appended to the command's argv, capped by
    ``conventions.harvest.gate_timeout_s``. Each stdout line is ``<ref> <sequence> <lo>[-<hi>]``;
    a line that does not read is skipped and counted. A non-zero exit, a timeout or no command at
    all is ``([], <why>)`` — a product's broken script refuses no plan (C9); the other two
    readers carry the pass."""
    conv = product.conventions
    command = conv.get('reservations')
    if not command:
        return [], 'no reservations command configured'
    argv = shlex.split(str(command)) + list(refs)
    timeout = H.gate_timeout(conv)
    rc, out, err = H.sh_timed(argv, product.repo_dir, H.gate_env(), timeout)
    if rc is None:
        return [], H.timed_out_line(argv, timeout)
    if rc != 0:
        return [], (err or out or f'reservations command exited {rc}').strip()
    bookings, skipped = [], 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            if line.strip():
                skipped += 1
            continue
        ref, seq_name, numbers = parts
        lo_s, _dash, hi_s = numbers.partition('-')
        try:
            lo, hi = int(lo_s), int(hi_s) if hi_s else int(lo_s)
        except ValueError:
            skipped += 1
            continue
        for n in range(lo, hi + 1):
            bookings.append(Booking(seq_name, n, ref, COMMAND))
    why = f'{skipped} unreadable line(s) from the reservations command' if skipped else ''
    return bookings, why


def holdings(bookings, ranked):
    """``{sequence: {number: [ref, ...]}}`` — every ref that booked a number, its holder first
    (lowest :func:`rank`). Deterministic: the same bookings give the same order."""
    by_seq = collections.defaultdict(lambda: collections.defaultdict(set))
    for b in bookings:
        by_seq[b.sequence][b.number].add(b.ref)
    out = {}
    for name, numbers in by_seq.items():
        out[name] = {n: sorted(refs, key=lambda r: ranked.get(r, (1, r)))
                     for n, refs in numbers.items()}
    return out


def holder(holdings_map, sequence, number):
    """The one ref that keeps ``number``, or None."""
    refs = holdings_map.get(sequence, {}).get(number)
    return refs[0] if refs else None


def clashes(holdings_map, ref):
    """``[(sequence, number, holder)]`` for every number ``ref`` booked that another ref holds —
    in sequence then number order, so the refusal's words are stable."""
    out = []
    for sequence in sorted(holdings_map):
        for number in sorted(holdings_map[sequence]):
            refs = holdings_map[sequence][number]
            if ref in refs and refs[0] != ref:
                out.append((sequence, number, refs[0]))
    return out


def next_free(holdings_map, seqs, sequence):
    """One past the highest number anything holds in ``sequence``, zero-padded to the pattern's
    width: the answer the hold hands the session, from the same map that refused it."""
    _dirname, _regex, width = seqs.get(sequence, ('', None, 0))
    numbers = holdings_map.get(sequence, {})
    n = max(numbers, default=0) + 1
    return reserve.format_number(n, width)


#: The correction kind :func:`refusal` returns — :data:`asf.workers.lifecycle.RESERVED` is
#: defined as this constant (Task 2), so the two names can never drift.
RESERVED = 'reserved'


def _holdings_from_snapshot(snap):
    """The persisted ``sequences`` map (str-keyed ``held``, PD-12: json-safe) back into a
    :func:`holdings` shape (int-keyed), holder order already fixed by :func:`snapshot`."""
    out = {}
    for name, info in (snap or {}).get('sequences', {}).items():
        out[name] = {int(k): list(refs) for k, refs in (info.get('held') or {}).items()}
    return out


def refusal(snapshot, ref):
    """``(kind, text)`` for a ref that booked a number another ref holds, else None — the pair
    :func:`asf.harvest.lane.lane_refusal` returns and ``Lane.enter_back`` unpacks."""
    holds = _holdings_from_snapshot(snapshot)
    clash_list = clashes(holds, ref)
    if not clash_list:
        return None
    seq_info = (snapshot or {}).get('sequences') or {}
    prs = (snapshot or {}).get('prs') or {}
    by_seq = collections.OrderedDict()
    for sequence, number, holder_ref in clash_list:
        by_seq.setdefault((sequence, holder_ref), []).append(number)
    sentences = []
    for (sequence, holder_ref), numbers in by_seq.items():
        width = seq_info.get(sequence, {}).get('width') or 0
        numbers_str = ', '.join(reserve.format_number(n, width) for n in numbers)
        pr = (prs.get(holder_ref) or {}).get('number')
        pr_part = f' (PR {pr})' if pr else ''
        singular = len(numbers) == 1
        sentences.append(
            f'{sequence} {numbers_str} {"is" if singular else "are"} held by {holder_ref}'
            f'{pr_part} — book past {"it" if singular else "them"}: the next free number in '
            f'{sequence} is {next_free(holds, {sequence: ("", None, width)}, sequence)}.')
    text = 'reserved: ' + ' '.join(sentences) + (
        ' Renumber on this branch (rename the files and every reference to them) and push it '
        'again.')
    return RESERVED, text


def snapshot(product, items, heads, prs):
    """The whole map, once: ``{'at', 'trunk', 'refs', 'prs', 'sequences', 'errors'}``, where
    ``sequences`` is ``{name: {'width': int, 'held': {number: [ref, ...]}}}``."""
    conv = product.conventions
    trunk = conv.main
    seqs = sequences(conv)
    refs = open_refs(product, heads, prs)
    bookings = []
    bookings += added_bookings(product, seqs, refs)
    bookings += writes_bookings(product, items, seqs)
    cmd_bookings, cmd_error = command_bookings(product, refs)
    bookings += cmd_bookings
    ranked = {ref: rank(ref, trunk, prs) for ref in refs}
    holds = holdings(bookings, ranked)
    sequences_out = {}
    for name, (_dirname, _regex, width) in seqs.items():
        held = holds.get(name, {})
        sequences_out[name] = {
            'width': width,
            'held': {str(n): list(refs_) for n, refs_ in held.items()},
        }
    return {
        'at': now_iso(),
        'trunk': trunk,
        'refs': refs,
        'prs': prs,
        'sequences': sequences_out,
        'errors': [cmd_error] if cmd_error else [],
    }


def save(state_dir, snap):
    """Write :data:`FILE` in the product's state dir, as ``asf.harvest.lane`` writes its own
    small json (a temp file, one rename)."""
    path = os.path.join(state_dir, FILE)
    with open(path + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(snap, fh, sort_keys=True)
    os.replace(path + '.tmp', path)


def load(state_dir):
    """The last snapshot, or ``{}`` — a missing, truncated or unreadable file is no snapshot,
    never an error: the brief prints its ``(not known here)`` marker for it."""
    try:
        with open(os.path.join(state_dir, FILE), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def brief_lines(snap, limit=None):
    """The holdings in words for a planner's brief, at most ``limit`` numbers per sequence."""
    limit = tunable('BRIEF_LIMIT') if limit is None else limit
    seqs = (snap or {}).get('sequences') or {}
    prs = (snap or {}).get('prs') or {}
    out = []
    for name in sorted(seqs):
        info = seqs[name]
        held = info.get('held') or {}
        width = info.get('width') or 0
        by_holder = collections.defaultdict(list)
        for k, refs in held.items():
            if refs:
                by_holder[refs[0]].append(int(k))
        total = sum(len(v) for v in by_holder.values())
        shown, chunks = 0, []
        for holder_ref in sorted(by_holder):
            numbers = sorted(by_holder[holder_ref])
            keep = numbers[:max(limit - shown, 0)]
            if not keep:
                continue
            shown += len(keep)
            numbers_str = ', '.join(reserve.format_number(n, width) for n in keep)
            pr = (prs.get(holder_ref) or {}).get('number')
            pr_part = f', PR {pr}' if pr else ''
            chunks.append(f'{numbers_str} ({holder_ref}{pr_part})')
        if not chunks:
            continue
        line = f'{name} ' + ', '.join(chunks)
        if total > shown:
            line += f' +{total - shown} more'
        out.append(line)
    return out


# ---- tunables ---------------------------------------------------------------

#: The config key (``~/.ASF/config.yaml``) over each constant above; the constant is its default.
TUNABLES = {
    'BRIEF_LIMIT': 'brief.reservations_max',
}


def tunable(name):
    """The constant ``name`` of :data:`TUNABLES` with its config key over it."""
    from asf import config_keys
    return config_keys.value(TUNABLES[name], globals()[name])
