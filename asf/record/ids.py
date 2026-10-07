"""asf.record.ids — id minting, shared by ``new``, groom's inbox intake and ``file-bugs``.

A per-job id range (``BACKLOG_ID_RANGE=S:0300-0349,T:0900-0949``) lets a parallel writer reserve
a block so two worktrees never mint the same id.

Ids are claimed by push (:mod:`asf.record.idclaim`): a job's block is a create-only ref
``refs/asf/ids/<P>-<lo>`` on the record repo's origin, claimed before the launch (so a cloud
session, which has neither the env var's host nor the asf CLI, is safe too — its brief carries
the range); ``asf new`` outside a block claims its single id the same way. A record repo
without ``origin``, or a product with ``conventions.flags.id_claim: off``, keeps the local behaviour.
At land, :mod:`asf.record.idcheck` refuses a plan whose new ids no claim covers.
"""
import os
import re

from asf.record import frontmatter, writer
from asf.record.core import TYPES, now_iso
from asf.schema import SCHEMA_VERSION


def _id_range(prefix):
    """BACKLOG_ID_RANGE=S:0300-0349,T:0900-0949 — a per-job reservation the controller hands a
    parallel writer. Returns (lo, hi) or None."""
    spec = os.environ.get('BACKLOG_ID_RANGE', '')
    for part in spec.split(','):
        m = re.match(rf'^\s*{prefix}:(\d+)-(\d+)\s*$', part)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def next_in_block(taken, lo, hi):
    """The block's own next free number: one above the highest of ``taken`` inside ``lo``-``hi``,
    or ``lo`` if the block holds none. A full block returns ``hi + 1`` instead of raising, so the
    caller's own ``if n > hi:`` decides the refusal."""
    inside = [n for n in taken if lo <= n <= hi]
    return max(inside) + 1 if inside else lo


def record_top(root, prefix):
    """The highest number the record at ``root`` holds for ``prefix`` (its card file names)."""
    top = 0
    for folder, p in set(TYPES.values()):
        if p != prefix:
            continue
        d = os.path.join(root, folder)
        if os.path.isdir(d):
            for name in os.listdir(d):
                m = re.match(rf'^{prefix}-(\d+)\.md$', name)
                if m:
                    top = max(top, int(m.group(1)))
    return top


def _in_worktree(root):
    """A linked worktree has a .git FILE (pointing at the main repo); the canonical clone has a
    .git directory and a plain fixture directory has none — only the worktree case is the
    parallel-writer case."""
    return os.path.isfile(os.path.join(root, '.git'))


def _claims_enabled(root):
    """``ids.claim`` of the product whose checkout holds ``root`` (default on)."""
    from asf import env
    from asf.record import idclaim
    try:
        name = env.product_of_dir(root)
        product = env.load_product(name) if name else None
    except Exception:  # noqa: BLE001 — an unresolvable product keeps the default
        product = None
    return idclaim.enabled(product)


def origin_top(root, prefix, remote='origin'):
    """The highest number ``origin/<trunk>``'s tree holds for ``prefix`` — the floor a mint must
    clear whatever the working tree says.

    ``record_top`` reads the checkout, and a checkout behind origin reads a lower top: on
    2026-10-06 a record 28 commits behind minted B-0268, B-0269 and B-0270, each already a card
    on origin (F-0260 P9). One ``fetch`` and one ``ls-tree``; 0 when there is no origin, no
    trunk, or the fetch fails — a floor that cannot be read is no floor, never a refusal.

    Returns ``(top, reachable)`` — ``reachable`` is the fetch's own ``ok`` (F-0260 PD6)."""
    from asf import env, gitops
    try:
        name = env.product_of_dir(root)
        product = env.load_product(name) if name else None
    except Exception:  # noqa: BLE001 — an unresolvable product keeps the default trunk name
        product = None
    trunk = product.main if product is not None else 'main'
    if not gitops.git(['fetch', '--quiet', '--no-tags', remote, trunk], root).ok:
        return 0, False
    top = 0
    for folder, p in set(TYPES.values()):
        if p != prefix:
            continue
        lt = gitops.git(['ls-tree', '-r', '--name-only', f'{remote}/{trunk}', '--', folder], root)
        if not lt.ok:
            continue
        for name in (lt.data or '').splitlines():
            m = re.match(rf'^{prefix}-(\d+)\.md$', os.path.basename(name))
            if m:
                top = max(top, int(m.group(1)))
    return top, True


def mint_id(root, canonical, type_, claim=False, claimant=None):
    """The next id for ``type_``. Inside ``BACKLOG_ID_RANGE``: the next free number of the
    job's claimed block — one above the highest number of the block the record holds, and the
    block's ``lo`` when it holds none. The record's tip outside the block is read nowhere: a
    block claimed later lands its ids above this one's ``hi``, and that must take no number
    away from a block nobody has used. Otherwise, with the record repo holding an ``origin`` and
    claims on (``conventions.flags.id_claim``, default on): a single id claimed by push
    (:mod:`asf.record.idclaim`), its floor cleared against both the working tree's own top and
    :func:`origin_top` — a checkout behind origin must never remint an id origin already holds.
    An unreachable origin falls back to the local path for every caller but ``asf new``
    (``claim=True``), which keeps today's refusal (F-0260 PD6). Otherwise, or with no origin: the
    record's top + 1, cleared against origin's own top where there is one to read, and stepping
    over every block the local claim mirror shows taken."""
    from asf.record import idclaim
    folder, prefix = TYPES[type_]
    rng = _id_range(prefix)
    taken = set()
    for iid, rec in canonical.items():
        if rec['meta'].get('type') == type_:
            m = re.match(rf'^{prefix}-(\d+)$', iid)
            if m:
                taken.add(int(m.group(1)))
    d = os.path.join(root, folder)
    if os.path.isdir(d):
        for name in os.listdir(d):
            m = re.match(rf'^{prefix}-(\d+)\.md$', name)
            if m:
                taken.add(int(m.group(1)))
    max_n = max(taken, default=0)
    if rng:
        lo, hi = rng
        n = next_in_block(taken, lo, hi)
        if n > hi:
            raise SystemExit(f"new: id range {prefix}:{lo:04d}-{hi:04d} exhausted — every number "
                              f"of the block is in the record")
        return f"{prefix}-{n:04d}"
    worktree = _in_worktree(root)
    has_git = worktree or os.path.isdir(os.path.join(root, '.git'))
    top = None
    if has_git and idclaim.has_origin(root) and _claims_enabled(root):
        top, reachable = origin_top(root, prefix)
        if reachable or claim:
            who = claimant or os.environ.get('ASF_SESSION') or os.environ.get('ASF_JOB') or 'asf new'
            try:
                return idclaim.claim_one(root, prefix, who, floor=max(max_n, top))
            except idclaim.ClaimError as e:
                raise SystemExit(f"new: {prefix}-id claim on origin failed: {e}") from None
        print(f'ids: origin unreachable — {prefix}-id minted locally, may collide')
    if worktree and not os.environ.get('BACKLOG_ALLOW_MINT'):
        raise SystemExit(f"new: minting {prefix}-ids in a worktree needs BACKLOG_ID_RANGE (e.g. {prefix}:0300-0349) "
                         f"or an origin to claim on — parallel writers collide otherwise")
    if top is None:
        top, _reachable = origin_top(root, prefix) if has_git else (0, False)
    n = max(max_n, top) + 1
    if has_git and idclaim.has_origin(root):
        try:
            idclaim.fetch(root)
        except idclaim.ClaimError:
            pass
    blocks = idclaim.claims(root) if has_git else []
    c = idclaim.covers(blocks, f"{prefix}-{n}")
    while c is not None:  # a number inside a block a job holds is that job's, never ours
        n = c.hi + 1
        c = idclaim.covers(blocks, f"{prefix}-{n}")
    return f"{prefix}-{n:04d}"


def write_new_item(root, canonical, type_, new_id, typed_fields, body, date, why,
                    acceptance=(), sections=None, shape=None, state='New'):
    folder, _prefix = TYPES[type_]
    meta = frontmatter.FrontmatterDict()
    meta['id'] = new_id
    meta['type'] = type_
    for k, v in typed_fields.items():
        if v not in (None, [], {}):
            meta[k] = v
    ts = now_iso()
    meta['schema_version'] = SCHEMA_VERSION
    meta['state'] = state  # the state a card is born in, 'New' unless the writer records something already true
    meta['stage_since'] = ts
    meta['updated'] = ts
    meta.machine_keys = {'schema_version', 'state', 'stage_since', 'updated'}

    sections_text = ''
    for heading, items in (sections or {}).items():
        if not items:
            continue
        sections_text += f"## {heading}\n" + ''.join(f"- {item}\n" for item in items) + "\n"

    acceptance_text = ''.join(f"- [ ] {item}\n" for item in acceptance) if acceptance else "- [ ] \n"

    history_line = f"- {date}: created ({why})"
    if shape is not None:
        rule, shape_type = shape
        history_line += f" — shape: {rule} → {shape_type}"

    full_body = (
        (f"## Description\n{body}\n\n" if body else "## Description\n\n")
        + sections_text
        + f"## Acceptance\n{acceptance_text}\n"
        + "## Non-goals\n\n"
        + "## History\n"
        + history_line + "\n\n"
        + "## Children\n\n"
        + "## Backlinks\n"
    )
    text = frontmatter.render(meta, full_body)
    path = os.path.join(root, folder, f"{new_id}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    writer.write_card(path, text)
    canonical[new_id] = {
        'meta': meta, 'body': full_body, 'path': path,
        'relpath': os.path.relpath(path, root), 'folder': folder,
        'name': f"{new_id}.md", 'text': text,
    }
    return new_id


# Back-compat alias: the original name inside backlog.py (private there, public here since
# asf.tick and asf.groom both call it).
_write_new_item = write_new_item
