"""asf.idea.cli — ``asf idea apply --tree FILE``: a tree in, intake cards out.

The deterministic half of the interrogator; it calls no model. Exit 0 when the tree is applied —
including when the record answered every node, which is a result and is printed as one — and 2
for a missing tree, a tree that does not parse, or one already applied (F-0023 D11, D12)."""
import json
import os
import sys

from asf import env
from asf.conventions import Conventions
from asf.idea.apply import apply_tree, new_stamp, unused_stamp, write_applied
from asf.idea.tree import TreeError, parse_tree
from asf.record.core import canonicalize, load_items


def _conventions(args):
    """The product's conventions, else the documented defaults when there is no product config
    to read (a record checkout run on its own)."""
    try:
        return env.load_product(getattr(args, 'product', None)).conventions
    except (env.ConfigError, OSError):
        return Conventions()


def _refuse(text):
    print(text, file=sys.stderr)
    return 2


def _lines(tree, applied, filed_names, rel_dir):
    """The printed form: a header, one line per node, the next step."""
    by_key = {a.node.key: a for a in applied.answered}
    rows = []
    for node in tree.nodes:
        answer = by_key.get(node.key)
        if answer:
            rows.append(('answered', answer.text, node))
        else:
            rows.append(('filed', f'{rel_dir}/{filed_names.pop(0)}', node))
    what = max((len(r[1]) for r in rows), default=0)
    out = [f"idea: {len(rows)} node(s) — {len(applied.filed)} filed, "
           f"{len(applied.answered)} answered by the record"]
    for verb, subject, node in rows:
        out.append(f'  {verb:<8} {subject:<{what}}   {node.type:<7} {node.title}')
    out.append('next: asf groom  (the cards are typed and minted there)')
    return out


def cmd_apply(args, root):
    tree_path = args.tree
    if not os.path.isfile(tree_path):
        return _refuse(f'no tree at {tree_path}')
    if os.path.exists(tree_path + '.applied') and not args.force:
        return _refuse(f'already applied: {tree_path}.applied exists — --force applies it again')
    with open(tree_path, encoding='utf-8') as f:
        text = f.read()
    try:
        tree = parse_tree(text)
    except TreeError as e:
        return _refuse(f'{tree_path}: {e}')

    conv = _conventions(args)
    canonical, _ = canonicalize(load_items(root)[0])
    intake_dir = os.path.join(root, conv.intake_dir)
    stamp = unused_stamp(intake_dir, new_stamp())
    applied = apply_tree(root, tree, canonical, intake_dir, stamp,
                         overlap=conv.answer_overlap, tree_text=text)
    data = write_applied(tree_path, stamp, applied, os.path.relpath(applied.report_path, root))
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for line in _lines(tree, applied, list(applied.filed), conv.intake_dir):
            print(line)
    return 0


def cmd_idea(args, root):
    if args.idea_command == 'apply':
        return cmd_apply(args, root)
    return _refuse(f'unknown idea command: {args.idea_command}')
