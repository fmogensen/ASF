"""asf.idea.cli — ``asf idea``: interrogate an idea into intake cards.

    asf idea "<text>" [--tree FILE] [--no-interrogate] [--product P] [--json]
    asf idea --enrich <ID> [--tree FILE] [--no-interrogate] [--product P] [--json]
    asf idea apply --tree FILE [--enrich ID] [--force] [--product P] [--json]

``apply`` is the deterministic half and calls no model: a tree in, intake cards out. The front
door runs the one session that writes the tree, then applies it. Exit 0 when the tree is applied —
including when the record answered every node, which is a result and is printed as one — and 2 for
a missing tree, a tree that does not parse, one already applied, or a card that does not round-trip
(F-0023 D11, D12)."""
import importlib
import json
import os
import sys

from asf import env
from asf.conventions import Conventions
from asf.idea.apply import (apply_tree, enrich_card, new_stamp, unused_stamp, write_applied)
from asf.idea.tree import TreeError, parse_tree
from asf.record.core import canonicalize, load_items, today


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


def _lines(tree, applied, rel_dir):
    """The printed form: a header, one line per node, the next step."""
    by_key = {a.node.key: a for a in applied.answered}
    names = list(applied.filed)
    rows = []
    for node in tree.nodes:
        answer = by_key.get(node.key)
        if answer:
            rows.append(('answered', answer.text, node))
        else:
            rows.append(('filed', f'{rel_dir}/{names.pop(0)}', node))
    what = max((len(r[1]) for r in rows), default=0)
    out = [f"idea: {len(rows)} node(s) — {len(applied.filed)} filed, "
           f"{len(applied.answered)} answered by the record"]
    for verb, subject, node in rows:
        out.append(f'  {verb:<8} {subject:<{what}}   {node.type:<7} {node.title}')
    out.append('next: asf groom  (the cards are typed and minted there)')
    return out


def _enrich(args, root, text, item_id):
    try:
        tree = parse_tree(text, enrich=True)
    except TreeError as e:
        return _refuse(f'{args.tree}: {e}')
    if len(tree.nodes) != 1:
        return _refuse(f'--enrich takes one node; {args.tree} holds {len(tree.nodes)}')
    canonical, _ = canonicalize(load_items(root)[0])
    node = tree.nodes[0]
    err = enrich_card(root, canonical, item_id, node, today())
    if err:
        return _refuse(f'error: {err}')
    from asf.record.publish import publish
    publish(root, canonical[item_id]['path'], f'record: enrich {item_id} (idea)')
    summary = (f'+{len(node.acceptance)} acceptance, '
               f'+{len(node.assumptions)} assumption(s)')
    if args.json:
        print(json.dumps({'enriched': item_id, 'acceptance': len(node.acceptance),
                          'assumptions': len(node.assumptions)}, indent=2))
    else:
        print(f'enriched {item_id}: {summary}')
    return 0


def apply_file(args, root, enrich=None):
    """Read ``args.tree`` and file it (or, with ``enrich``, write it into that card)."""
    tree_path = args.tree
    if not os.path.isfile(tree_path):
        return _refuse(f'no tree at {tree_path}')
    if enrich:
        with open(tree_path, encoding='utf-8') as f:
            return _enrich(args, root, f.read(), enrich)
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
        for line in _lines(tree, applied, conv.intake_dir):
            print(line)
    return 0


def _check_enrichable(root, item_id):
    """None, or the refusal: the card must exist and be a Feature — checked before a session is
    spent on it."""
    canonical, _ = canonicalize(load_items(root)[0])
    rec = canonical.get(item_id)
    if rec is None:
        return f'error: no item {item_id!r}'
    if rec['meta'].get('type') != 'feature':
        return f"error: {item_id} is a {rec['meta'].get('type')}; only a Feature is enriched"
    return None


def _index_of(root):
    try:
        with open(os.path.join(root, 'index.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _interrogate(product, root, ask, tree_path, stamp, item_id):
    """Run the one session whose only deliverable is ``tree_path``, in the foreground. Returns
    the session's Result. No worktree, no branch: its cwd is a scratch directory."""
    build_mod = importlib.import_module('asf.briefs.build')  # the package's `build` function wins the attribute
    from asf.feeder.rows import LAUNCH, Row
    from asf.workers import runtime as runtime_mod
    from asf.workers.spawn import load_cfg

    row = Row(tier=2, kind='IDEA', item_id=item_id or '', feature_id=item_id or '',
              action=LAUNCH, brief_kind='idea', branch='', reason=ask, tree_file=tree_path)
    brief = build_mod.build(product, row, _index_of(root))
    work = os.path.join(os.path.dirname(tree_path), stamp)
    os.makedirs(work, exist_ok=True)
    brief_path = tree_path + '.brief.md'
    with open(brief_path, 'w', encoding='utf-8') as f:
        f.write(brief.text)
    cfg = load_cfg()
    job = runtime_mod.Job(
        product.name, f'idea-{stamp}', work, brief_path,
        build_mod.model_for(product, 'idea'),
        add_dirs=[root, os.path.dirname(tree_path)],
        settings_file=(cfg.get('worker_pool') or {}).get('settings_file'))
    return runtime_mod.from_config(cfg).run(job, wait=True)


def cmd_idea(args, root):
    if args.text == 'apply':
        if not args.tree:
            return _refuse('asf idea apply wants --tree FILE')
        return apply_file(args, root, enrich=args.enrich)

    ask = (args.text or '').strip()
    if not ask and not args.enrich:
        return _refuse('asf idea wants the ask — asf idea "<text>" — or --enrich <ID>')
    if args.enrich:
        refusal = _check_enrichable(root, args.enrich)
        if refusal:
            return _refuse(refusal)
        ask = ask or f'enrich {args.enrich}'
    stamp = new_stamp()
    tree_path = args.tree
    if not tree_path:
        product = env.load_product(args.product)
        tree_path = os.path.join(env.state_dir(product), 'idea', f'{stamp}.tree.md')
    tree_path = os.path.abspath(os.path.expanduser(tree_path))
    args.tree = tree_path

    if not args.no_interrogate:
        product = env.load_product(args.product)
        os.makedirs(os.path.dirname(tree_path), exist_ok=True)
        result = _interrogate(product, root, ask, tree_path, stamp, args.enrich)
        if not os.path.isfile(tree_path):
            why = getattr(result, 'reason', None) or 'the session wrote no file'
            return _refuse(f'no tree at {tree_path} — {why}')
    return apply_file(args, root, enrich=args.enrich)
