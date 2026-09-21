"""asf.record.index — rewrite Children/Backlinks and index.json (``asf index``)."""
import json
import os
import sys

from asf.record import frontmatter
from asf.record.core import build_index_data, canonicalize, compute_derived, expected_body, load_items, render_index_json


def do_index(root):
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    canonical, _dupes = canonicalize(by_id)
    derived = compute_derived(canonical)

    for iid, rec in canonical.items():
        new_body = expected_body(rec, canonical, derived)
        if new_body != rec['body']:
            new_text = frontmatter.render(rec['meta'], new_body)
            with open(rec['path'], 'w', encoding='utf-8') as f:
                f.write(new_text)

    data = build_index_data(canonical, derived)
    index_path = os.path.join(root, 'index.json')
    old_items = None
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
    if old_items != data['items']:
        with open(index_path, 'w', encoding='utf-8') as f:
            f.write(render_index_json(data))
    return 0


def cmd_index(args, root):
    return do_index(root)


# Back-compat alias for callers ported from backlog.py's private `_do_index`.
_do_index = do_index
