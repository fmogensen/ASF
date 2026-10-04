"""asf.tick.migrate_landing — stamp the cards that closed before the ``landing:`` stamp existed
(``asf migrate-landing --product p [--apply | --revert]``).

From this release on, :mod:`asf.record.ingest` writes ``landing: {sha, as_of, by}`` on every
Task, Bug and Story the moment a closing rule closes it. A card closed earlier carries none; this
command stamps each such card once, from what its ``evidence:`` lines (and a typed ``landed:``)
already name — ``merge <sha> of <branch> lands <id>``, ``commit <sha> names <id>``, ``PR #<n>
merged (<sha>)``, ``fix merged (<sha>)`` — spelled in full against the product repo, with ``by:
migration``. A card whose evidence names no sha git knows gets ``sha: ''`` and ``note: pre-I14``.
``as_of`` is the card's ``stage_since`` (when it entered its state), else now.

* no flag: the dry run — one line per card it would stamp, and the count; nothing is written;
* ``--apply``: write the stamps (a card already stamped is left alone: ``--apply`` twice is no
  diff), through the record's stage, as one commit;
* ``--revert``: remove every ``by: migration`` stamp — and only those — the rollback (it
  writes: a rollback is never a dry run).

Only the machine key is written; no ``## History`` line (like ``schema_version``'s restamp).
"""
import re
import sys

from asf import env
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items, now_iso
from asf.record.ingest import LANDING_KEY, LANDING_TYPES, MACHINE_KEY_ORDER

BY = 'migration'
NOTE = 'pre-I14'
DONE_STATES = ('Resolved', 'Closed')
_EVIDENCE_SHA_RES = (
    re.compile(r'^(?:merge|commit) ([0-9a-f]{7,40}) (?:of .+ lands|names) '),
    re.compile(r'^(?:PR #\d+ |fix )merged \(([0-9a-f]{7,40})\)'),
)
_SHA_RE = re.compile(r'^[0-9a-f]{7,40}$')
_FULL_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
_ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}')


def evidence_shas(meta):
    """The shas a card's record names as its landing, newest evidence first: a typed
    ``landed:``, then each ``evidence:`` line's (:data:`_EVIDENCE_SHA_RES`)."""
    typed, machine = frontmatter.split_machine(meta)
    out = []
    landed = str(typed.get('landed') or '').strip().lower()
    if _SHA_RE.match(landed):
        out.append(landed)
    lines = machine.get('evidence')
    for line in lines if isinstance(lines, list) else []:
        for rx in _EVIDENCE_SHA_RES:
            m = rx.match(str(line))
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out


def candidates(canonical):
    """``[(iid, rec, [sha, ...])]`` — every Task/Bug/Story whose state is Resolved or Closed and
    that carries no ``landing:`` yet, with the shas its record names."""
    out = []
    for iid, rec in sorted(canonical.items()):
        typed, machine = frontmatter.split_machine(rec['meta'])
        if typed.get('type') not in LANDING_TYPES or LANDING_KEY in machine:
            continue
        if machine.get('state') not in DONE_STATES:
            continue
        out.append((iid, rec, evidence_shas(rec['meta'])))
    return out


def stamp_for(machine, shas, full, now):
    """The ``landing:`` the migration writes: the first of ``shas`` git spells in full (``full``:
    ``{sha: 40-hex}``), else ``sha: ''`` with ``note: pre-I14``."""
    sha = next((full.get(s) or (s if _FULL_SHA_RE.match(s) else '') for s in shas
                if full.get(s) or _FULL_SHA_RE.match(s)), '')
    since = str(machine.get('stage_since') or '')
    as_of = since if _ISO_RE.match(since) else now
    out = {'sha': sha, 'as_of': as_of, 'by': BY}
    if not sha:
        out['note'] = NOTE
    return out


def plan(canonical, product, now=None):
    """``[(iid, rec, stamp)]`` — the stamps ``--apply`` writes. Every short sha is spelled in
    full against the product repo in one git call (:func:`asf.evidence.evidence.full_shas`)."""
    from asf.evidence import evidence
    now = now or now_iso()
    rows = candidates(canonical)
    want = {s for _i, _r, shas in rows for s in shas if not _FULL_SHA_RE.match(s)}
    full = evidence.full_shas(product, want) if want and product is not None else {}
    return [(iid, rec, stamp_for(frontmatter.split_machine(rec['meta'])[1], shas, full, now))
            for iid, rec, shas in rows]


def reverts(canonical):
    """``[(iid, rec)]`` — every card whose ``landing:`` this migration wrote (``by: migration``)."""
    out = []
    for iid, rec in sorted(canonical.items()):
        landing = frontmatter.split_machine(rec['meta'])[1].get(LANDING_KEY)
        if isinstance(landing, dict) and landing.get('by') == BY:
            out.append((iid, rec))
    return out


def _write_stamps(_root, rows):
    for _iid, rec, stamp in rows:
        frontmatter.merge_machine(rec['path'], {LANDING_KEY: stamp}, order=MACHINE_KEY_ORDER)


def _drop_stamps(_root, rows):
    for _iid, rec in rows:
        frontmatter.merge_machine(rec['path'], {}, drop=(LANDING_KEY,))


def _guarded(root, product, fn, rows, relpaths):
    """``fn(root, rows)`` through the record's stage (one writer, the invariants over its
    change), then the index. True when no invariant refused any of it."""
    from asf.record import stage
    from asf.record.index import do_index
    _r, _staged, findings = stage.guarded(root, 'migrate-landing', fn, (rows,), product=product,
                                          only=relpaths)
    do_index(root)
    return not findings


def migrate(root, product=None, apply=False, revert=False, out=print, now=None):
    """The command's body over the record at ``root``: the dry run, ``apply`` (stamp) or
    ``revert`` (unstamp, which writes). Returns ``(exit code, cards changed)``."""
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1, 0
    canonical, _dupes = canonicalize(by_id)
    if revert:
        rows = reverts(canonical)
        for iid, _rec in rows:
            out(f"unstamp {iid}")
        if rows and not _guarded(root, product, _drop_stamps, rows,
                                 [r['relpath'] for _i, r in rows]):
            return 2, 0
        out(f"migrate-landing --revert: {len(rows)} card(s) unstamped")
        return 0, len(rows)
    rows = plan(canonical, product, now)
    for iid, _rec, stamp in rows:
        sha = stamp['sha'][:9] or f"'' ({NOTE})"
        out(f"{'stamp' if apply else 'would stamp'} {iid} landing {sha}")
    if apply and rows and not _guarded(root, product, _write_stamps, rows,
                                       [r['relpath'] for _i, r, _s in rows]):
        return 2, 0
    unknown = sum(1 for _i, _r, s in rows if not s['sha'])
    out(f"migrate-landing: {len(rows)} card(s) "
        f"{'stamped' if apply else 'would be stamped (dry run: add --apply)'}"
        f" — {unknown} with no sha ({NOTE})")
    return 0, len(rows) if apply else 0


def cmd_migrate_landing(args, root):
    product = env.load_product(args.product) if getattr(args, 'product', None) else None
    rc, _n = migrate(root, product, apply=bool(args.apply), revert=bool(args.revert))
    return rc


def register(sub):
    """``asf migrate-landing --product P [--apply | --revert]``."""
    p = sub.add_parser('migrate-landing',
                       help='stamp landing: on the Tasks/Bugs/Stories closed before the stamp '
                            'existed (a dry run unless --apply; --revert removes those stamps)')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='write the stamps (else a dry run)')
    mode.add_argument('--revert', action='store_true',
                      help='remove every by: migration stamp, and only those (the rollback)')
    p.add_argument('--product')
    return p
