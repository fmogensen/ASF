"""asf.tick.tick — ``asf tick``: metrics backfill → ingest → file-bugs → rollup → index.

``--shadow`` runs the same sequence against a shadow clone of the product's backlog
(:mod:`asf.tick.shadow`) instead of the real one — never pushes, commits locally — then renders
the six tables (:mod:`asf.views`) into ``<shadow>/tables/*.md`` so ``asf shadow-diff`` has
something to compare against the pre-``asf`` tools' output.
"""
import argparse
import datetime
import os

from asf import env
from asf.record.index import do_index


def _ns(**kw):
    return argparse.Namespace(**kw)


def run_step0(root, product, fresh=False):
    """metrics backfill → ingest → file-bugs → rollup → index, against ``root``. Returns nothing;
    prints what each step printed, same as running the commands one at a time would.

    ``asf.record.ingest.cmd_ingest`` calls ``evidence.load()`` with no product (a gap the fuller
    0.1 command surface is meant to close — see ``asf/cli.py``'s module docstring): it falls back
    to ``$ASF_PRODUCT``/``config.yaml``'s ``default_product``, so this sets ``$ASF_PRODUCT`` for
    the tick's own process rather than widen ``cmd_ingest``'s signature for one caller.
    """
    os.environ['ASF_PRODUCT'] = product.name

    from asf.metrics.metrics import cmd_backfill, cmd_rollup
    from asf.record.ingest import cmd_ingest
    from asf.tick.file_bugs import cmd_file_bugs

    cmd_backfill(_ns(days=1, sessions=None, log=None, workflow='ci',
                     launch_dir=os.path.expanduser('~/.claude-workers/launch'), product=product.name), root)
    cmd_ingest(_ns(fresh=fresh, product=product.name), root)
    default_bug_epic = product.conventions.get('default_bug_epic')
    cmd_file_bugs(_ns(default_bug_epic=default_bug_epic), root)
    cmd_rollup(_ns(day=None, no_releases=False, product=product.name), root)
    do_index(root)


def render_tables(root, product):
    """{name: markdown text} for the six ``asf`` tables, against ``root``."""
    from asf.views import roadmap, board, parity, prod, sessions, status
    return {
        'roadmap': roadmap.render(root, product),
        'backlog': board.render(root, product),
        'parity': parity.render(root),
        'prod': prod.render(root, product),
        'sessions': sessions.render(root),
        'status': status.render(root, product),
    }


def write_tables(root, tables):
    tables_dir = os.path.join(root, 'tables')
    os.makedirs(tables_dir, exist_ok=True)
    for name, text in tables.items():
        with open(os.path.join(tables_dir, f'{name}.md'), 'w', encoding='utf-8') as f:
            f.write(text)
    return tables_dir


def cmd_tick(args, root=None):
    product = env.load_product(getattr(args, 'product', None))
    shadow = getattr(args, 'shadow', False)
    fresh = getattr(args, 'fresh', False)

    if shadow:
        from asf.tick.shadow import ensure_shadow_clone, commit_local
        backlog_root = ensure_shadow_clone(product)
        run_step0(backlog_root, product, fresh=fresh)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        committed = commit_local(backlog_root, f"tick: state {stamp}")
        tables = render_tables(backlog_root, product)
        tables_dir = write_tables(backlog_root, tables)
        print(f"tick --shadow: {'state committed' if committed else 'no change'} "
              f"in {backlog_root}; tables in {tables_dir}")
        return 0

    backlog_root = product.backlog_dir
    run_step0(backlog_root, product, fresh=fresh)
    print("tick: state updated")
    return 0
