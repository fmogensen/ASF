"""asf.record.ids — id minting, shared by ``new``, groom's inbox intake and ``file-bugs``.

A per-job id range (``BACKLOG_ID_RANGE=S:0300-0349,T:0900-0949``) lets a parallel writer reserve
a block so two worktrees never mint the same id.
"""
import os
import re

from asf.record import frontmatter
from asf.record.core import TYPES, now_iso


def _id_range(prefix):
    """BACKLOG_ID_RANGE=S:0300-0349,T:0900-0949 — a per-job reservation the controller hands a
    parallel writer. Returns (lo, hi) or None."""
    spec = os.environ.get('BACKLOG_ID_RANGE', '')
    for part in spec.split(','):
        m = re.match(rf'^\s*{prefix}:(\d+)-(\d+)\s*$', part)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _in_worktree(root):
    """A linked worktree has a .git FILE (pointing at the main repo); the canonical clone has a
    .git directory and a plain fixture directory has none — only the worktree case is the
    parallel-writer case."""
    return os.path.isfile(os.path.join(root, '.git'))


def mint_id(root, canonical, type_):
    folder, prefix = TYPES[type_]
    rng = _id_range(prefix)
    if rng is None and _in_worktree(root) and not os.environ.get('BACKLOG_ALLOW_MINT'):
        raise SystemExit(f"new: minting {prefix}-ids in a worktree needs BACKLOG_ID_RANGE (e.g. {prefix}:0300-0349) — "
                         f"parallel writers collide otherwise")
    max_n = 0
    for iid, rec in canonical.items():
        if rec['meta'].get('type') == type_:
            m = re.match(rf'^{prefix}-(\d+)$', iid)
            if m:
                max_n = max(max_n, int(m.group(1)))
    d = os.path.join(root, folder)
    if os.path.isdir(d):
        for name in os.listdir(d):
            m = re.match(rf'^{prefix}-(\d+)\.md$', name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    if rng:
        lo, hi = rng
        n = max(lo, max_n + 1) if max_n >= lo else lo
        if n > hi:
            raise SystemExit(f"new: id range {prefix}:{lo:04d}-{hi:04d} exhausted")
        return f"{prefix}-{n:04d}"
    return f"{prefix}-{max_n + 1:04d}"


def write_new_item(root, canonical, type_, new_id, typed_fields, body, date, why):
    folder, _prefix = TYPES[type_]
    meta = frontmatter.FrontmatterDict()
    meta['id'] = new_id
    meta['type'] = type_
    for k, v in typed_fields.items():
        if v not in (None, [], {}):
            meta[k] = v
    ts = now_iso()
    meta['state'] = 'New'
    meta['stage_since'] = ts
    meta['updated'] = ts
    meta.machine_keys = {'state', 'stage_since', 'updated'}
    full_body = (
        f"## Description\n{body}\n\n" if body else "## Description\n\n"
    ) + (
        "## Acceptance\n- [ ] \n\n"
        "## Non-goals\n\n"
        "## History\n"
        f"- {date}: created ({why})\n\n"
        "## Children\n\n"
        "## Backlinks\n"
    )
    text = frontmatter.render(meta, full_body)
    path = os.path.join(root, folder, f"{new_id}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    canonical[new_id] = {
        'meta': meta, 'body': full_body, 'path': path,
        'relpath': os.path.relpath(path, root), 'folder': folder,
        'name': f"{new_id}.md", 'text': text,
    }
    return new_id


# Back-compat alias: the original name inside backlog.py (private there, public here since
# asf.tick and asf.groom both call it).
_write_new_item = write_new_item
