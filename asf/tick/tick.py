"""asf.tick.tick — ``asf tick``: the scheduled steps (:mod:`asf.tick.steps`).

``record`` is step 0: metrics backfill → ingest → file-bugs → rollup → index, run in the tick's
own clone of the product's backlog (:mod:`asf.tick.shadow`), committed and pushed to origin.
Every other step is a legacy command the product declares, or ``off``.

``--shadow`` runs step 0 against a shadow clone instead — never pushes, commits locally, never
runs a legacy step — then renders the six tables (:mod:`asf.views`) into ``<shadow>/tables/*.md``
so ``asf shadow-diff`` has something to compare against the pre-``asf`` tools' output.
"""
import argparse
import datetime
import os
import subprocess
import sys

from asf import env
from asf.record.index import do_index
from asf.tick import steps


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


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run_record_step(product, fresh=False):
    """The ``record`` step, live: step 0 in the tick's own clone (``…/state/<product>/record``),
    committed as the factory identity, pushed to the backlog's origin. Never touches the operator's
    backlog checkout (B-0013). Returns the exit code: 0 (nothing to push, or pushed), 1 (push
    refused or the clone could not be made — the clone is derived state, the next run resets it
    and re-derives)."""
    from asf.tick import shadow
    path = shadow.record_dir(product)
    try:
        shadow.ensure_clone(product, path)
        run_step0(path, product, fresh=fresh)
        committed = shadow.commit_local(path, f"tick: state {_stamp()}")
    except (subprocess.CalledProcessError, env.ConfigError) as e:
        detail = (getattr(e, 'stderr', None) or str(e)).strip()
        print(f"tick: record failed ({detail})")
        return 1
    if not committed:
        print(f"tick: no change ({path})")
        return 0
    if shadow.push(path):
        print(f"tick: state committed and pushed ({path})")
        return 0
    print(f"tick: state committed, push refused — re-derived next run ({path})")
    return 1


def run_shadow(product, fresh=False):
    from asf.tick.shadow import ensure_shadow_clone, commit_local
    backlog_root = ensure_shadow_clone(product)
    run_step0(backlog_root, product, fresh=fresh)
    committed = commit_local(backlog_root, f"tick: state {_stamp()}")
    tables = render_tables(backlog_root, product)
    tables_dir = write_tables(backlog_root, tables)
    print(f"tick --shadow: {'state committed' if committed else 'no change'} "
          f"in {backlog_root}; tables in {tables_dir}")
    return 0


def cmd_tick(args, root=None):
    product = env.load_product(getattr(args, 'product', None))
    fresh = getattr(args, 'fresh', False)

    if getattr(args, 'shadow', False):
        return run_shadow(product, fresh=fresh)  # record only, never a legacy step

    try:
        chosen = steps.parse_steps(args.steps) if getattr(args, 'steps', None) else None
        rows = steps.resolve(product, chosen)
        if getattr(args, 'manifest', False):
            sys.stdout.write(steps.manifest_table(rows))
            return 0
        steps.check_owned(rows, product)
    except steps.StepError as e:
        print(e)
        return 2

    rc = 0
    for step, owner, command in rows:
        if owner == 'off':
            print(f"tick: step {step} off (another job runs it)")
            continue
        if step == 'daily' and not steps.daily_due(product, getattr(args, 'daily', False)):
            print("tick: step daily already ran today")
            continue
        if owner == 'asf':
            step_rc = run_record_step(product, fresh=fresh)
        else:
            step_rc = steps.run_legacy(step, command, steps.legacy_timeout())
            if step_rc:
                print(f"tick: step {step} exited {step_rc}")
        if step == 'daily' and step_rc == 0:
            steps.write_daily_stamp(product)
        rc = rc or (1 if step_rc else 0)
    return rc


def register(subparsers):
    """Add the ``tick`` subcommand (replaces the bare one in ``asf.cli``)."""
    p = subparsers.add_parser(
        'tick', help='the scheduled steps: record (metrics backfill -> ingest -> file-bugs -> rollup '
                     '-> index, committed and pushed) plus every step declared under legacy_steps')
    p.add_argument('--product')
    p.add_argument('--shadow', action='store_true',
                   help='run record against a shadow clone, never pushed, never a legacy step')
    p.add_argument('--fresh', action='store_true', help="bypass evidence's cache")
    p.add_argument('--steps', help=f"comma list, a subset of {','.join(steps.STEPS)} (default: all)")
    p.add_argument('--manifest', action='store_true', help='print step / owner / command and exit')
    p.add_argument('--daily', action='store_true', help='run the daily step even if it already ran today')
    return p
