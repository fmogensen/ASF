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
    canonical, _dupes = canonicalize(by_id)
    derived = compute_derived(canonical)
    scrub = title_scrub(root)  # a protected name in one title is never copied into another card

    for iid, rec in canonical.items():
        new_body = expected_body(rec, canonical, derived, scrub)
        if new_body != rec['body']:
            new_text = frontmatter.render(rec['meta'], new_body)
            with open(rec['path'], 'w', encoding='utf-8') as f:
                f.write(new_text)

    write_index_json(root, canonical, derived)
    return 0


def refresh_index_json(root):
    """Rewrite ``index.json`` alone (no card body) when it no longer matches the record — what
    ``asf set`` runs after its write, so a field it changed never leaves the index stale for the
    pre-commit check to refuse. A record with a parse error is left alone."""
    by_id, parse_errors = load_items(root)
    if parse_errors:
        return
    canonical, _dupes = canonicalize(by_id)
    write_index_json(root, canonical, compute_derived(canonical))


def write_index_json(root, canonical, derived):
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
    if old_items != data['items']:
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(render_index_json(data))


def cmd_index(args, root):
    return do_index(root)


# Back-compat alias for callers ported from backlog.py's private `_do_index`.
_do_index = do_index
