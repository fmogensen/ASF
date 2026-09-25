"""asf.record.index — rewrite Children/Backlinks and index.json (``asf index``)."""
import json
import os
import sys

from asf.record import frontmatter
from asf.record.core import (build_index_data, canonicalize, compute_derived, expected_body, load_items,
                             render_index_json, title_scrub)
from asf.schema import SCHEMA_VERSION


def do_index(root):
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    refresh(root, create_index=True)
    return 0


def refresh(root, scrub=None, only=None, index=True, create_index=False):
    """Rewrite every card's Children/Backlinks and ``index.json`` to what the record derives —
    :func:`do_index` for a record that may carry a card with a parse error: that card alone is
    skipped (its index entry kept as it stands), never the refresh of every other. ``only`` (a
    set of relpaths) limits the cards rewritten; ``index`` False leaves ``index.json`` alone, and
    a record without one is given one only with ``create_index``. Returns the relpaths written."""
    by_id, parse_errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    derived = compute_derived(canonical)
    if scrub is None:
        scrub = title_scrub(root)  # a protected name in one title is never copied into another card
    written = []
    for iid, rec in canonical.items():
        if only is not None and rec['relpath'] not in only:
            continue
        new_body = expected_body(rec, canonical, derived, scrub)
        if new_body != rec['body']:
            new_text = frontmatter.render(rec['meta'], new_body)
            with open(rec['path'], 'w', encoding='utf-8') as f:
                f.write(new_text)
            written.append(rec['relpath'])
    if index and (create_index or os.path.isfile(os.path.join(root, 'index.json'))):
        broken = {f for f, _line, _why in parse_errors}
        if write_index_json(root, canonical, derived, keep=broken):
            written.append('index.json')
    return written


def write_index_json(root, canonical, derived, keep=()):
    """Write ``index.json`` when its items differ from the record's; True when it was written.
    An entry whose card is in ``keep`` (relpaths — the cards that fail to parse) is carried over
    as it stands: the one card that cannot be read never takes the rest of the index with it."""
    data = build_index_data(canonical, derived)
    index_path = os.path.join(root, 'index.json')
    old_items = None
    # a record's first index is stamped with this package's schema; an existing one keeps its own
    data['schema_version'] = SCHEMA_VERSION
    if os.path.isfile(index_path):
        with open(index_path, encoding='utf-8') as f:
            try:
                old = json.load(f)
            except json.JSONDecodeError:
                old = {}
        old_items = old.get('items')
        # the schema stamp is the record's, not the items': a rewrite carries it over (asf schema)
        if 'schema_version' in old:
            data['schema_version'] = old['schema_version']
        else:
            del data['schema_version']  # unstamped stays unstamped until `schema-migrate`
        if keep and isinstance(old_items, dict):
            for iid, entry in old_items.items():
                if iid not in data['items'] and entry_relpath(iid, entry) in keep:
                    data['items'][iid] = entry
    if old_items != data['items']:
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(render_index_json(data))
        return True
    return False


def entry_relpath(iid, entry):
    """The card an ``index.json`` entry was derived from, relative to the record."""
    folder = (entry or {}).get('folder') if isinstance(entry, dict) else None
    return f"{folder}/{iid}.md" if folder else None


def cmd_index(args, root):
    return do_index(root)


# Back-compat alias for callers ported from backlog.py's private `_do_index`.
_do_index = do_index
