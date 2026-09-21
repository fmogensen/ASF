"""asf.tick.tick — ``asf tick``: the scheduled steps (:mod:`asf.tick.steps`), in order
``record → health → wave → prs → harvest → batch → daily``.

``record`` is step 0: metrics backfill → ingest → file-bugs → rollup → index, run in the tick's
own clone of the product's backlog (:mod:`asf.tick.shadow`).
``health``, ``wave``, ``prs``, ``harvest`` and ``daily`` are :mod:`asf.tick.step_health`,
``step_wave``, ``step_prs``, ``step_harvest`` and ``step_daily``; ``batch`` is a command the product declares (or ``off``), as is
any step a product chooses to run with its own command.

A step that fails prints one ``[step:<name>] FAILED <why>`` and the tick goes on to the next one;
the exit code is 1 when any step failed. Every step shares one :class:`Context`, so the record
clone is made (reset to origin) once per tick however many steps read it. After the steps, one
line goes to ``metrics/ticks/<day>.jsonl`` in the record clone — the ``ticks`` stream's schema
(``tick``, ``launches``, ``stalls``, ...) plus ``product`` and ``steps: [{step, ok, seconds}]`` —
and only then is the clone committed (``tick: state <ts>``) and pushed: one commit per tick, so
the derived state, the events the steps appended and the step log land together. One line per
tick, not per step: ``asf metrics rollup`` counts the stream's lines as ticks and reads every
stream field off each one.

``--shadow`` runs step 0 against a shadow clone instead — never pushes, commits locally, never
runs a command step — then renders the six tables (:mod:`asf.views`) into ``<shadow>/tables/*.md``
so ``asf shadow-diff`` has something to compare against the pre-``asf`` tools' output.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

from asf import env
from asf.record.index import do_index
from asf.tick import steps


def _ns(**kw):
    return argparse.Namespace(**kw)


DEFAULT_CI_WORKFLOW = 'ci'


def ci_provider(product):
    """``ci.provider`` of the product yaml, lowercased; ``none`` (no CI to read) is a value."""
    ci = product.ci if isinstance(product.ci, dict) else {'provider': product.ci}
    v = ci.get('provider')
    return str(v).strip().lower() if v is not None else None


def ci_workflow(product):
    """``ci.workflow``: the name of the CI workflow whose runs the backfill reads (default ``ci``)."""
    ci = product.ci if isinstance(product.ci, dict) else {}
    return ci.get('workflow') or DEFAULT_CI_WORKFLOW


def run_step0(root, product, fresh=False):
    """metrics backfill → ingest → file-bugs → rollup → index, against ``root``. Returns nothing;
    prints what each step printed, same as running the commands one at a time would.

    The backfill reads CI runs only — sessions come from the workers' own ledger, not from a
    launcher directory — and a product with ``ci: {provider: none}`` has none to read.

    ``asf.record.ingest.cmd_ingest`` calls ``evidence.load()`` with no product (a gap the fuller
    0.1 command surface is meant to close — see ``asf/cli.py``'s module docstring): it falls back
    to ``$ASF_PRODUCT``/``config.yaml``'s ``default_product``, so this sets ``$ASF_PRODUCT`` for
    the tick's own process rather than widen ``cmd_ingest``'s signature for one caller.
    """
    os.environ['ASF_PRODUCT'] = product.name

    from asf.metrics.metrics import cmd_backfill, cmd_rollup
    from asf.record.ingest import cmd_ingest
    from asf.tick.file_bugs import cmd_file_bugs

    if ci_provider(product) != 'none':
        cmd_backfill(_ns(days=1, sessions=None, log=None, workflow=ci_workflow(product),
                         launch_dir=None, product=product.name), root)
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
        'sessions': sessions.render(root, product),
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


class Context:
    """What one tick's steps share: the product, the flags, the record clone (made — cloned or
    reset to origin — at most once per tick), and the counters the tick line carries."""

    def __init__(self, product, fresh=False):
        self.product = product
        self.fresh = fresh
        self._record = None
        self.counts = {'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0}

    @property
    def has_record(self):
        return self._record is not None

    def record_root(self):
        """The record clone's path; the first call clones it or resets it to origin."""
        if self._record is None:
            from asf.tick import shadow
            self._record = shadow.ensure_clone(self.product, shadow.record_dir(self.product))
        return self._record

    def event(self, kind, **fields):
        """Append one line to ``metrics/events/<day>.jsonl`` in the record clone."""
        root = self.record_root()
        stamp = _stamp()
        rec = dict(fields, kind=kind, product=self.product.name, ts=stamp)
        path = os.path.join(root, 'metrics', 'events', f'{stamp[:10]}.jsonl')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + '\n')
        return rec


class StepFailed(Exception):
    """An ``asf`` step's own failure: the tick prints ``[step:<name>] FAILED <message>``."""


def run_record_step(product, fresh=False, ctx=None):
    """The ``record`` step, live: step 0 in the tick's own clone (``…/state/<product>/record``).
    Never touches the operator's backlog checkout (B-0013).

    Inside a tick (``ctx`` given) it only derives: the tick commits once, after its line
    (:func:`finish`). Called alone it commits (as the factory identity) and pushes itself.
    Returns the exit code: 0, or 1 when the clone could not be made or step 0 failed (the clone is
    derived state, the next run resets it and re-derives) — and, alone, when the push was
    refused."""
    alone = ctx is None
    ctx = ctx or Context(product, fresh=fresh)
    try:
        run_step0(ctx.record_root(), product, fresh=fresh)
    except (subprocess.CalledProcessError, env.ConfigError) as e:
        detail = (getattr(e, 'stderr', None) or str(e)).strip()
        print(f"tick: record failed ({detail})")
        return 1
    return commit_and_push(ctx) if alone else 0


def commit_and_push(ctx):
    """Commit the record clone as ``tick: state <ts>`` and push it; one line saying which. Returns
    0 (nothing to commit, or pushed) or 1 (push refused — re-derived next run)."""
    from asf.tick import shadow
    path = ctx.record_root()
    if not shadow.commit_local(path, f"tick: state {_stamp()}"):
        print(f"tick: no change ({path})")
        return 0
    pushed = shadow.push(path)
    if pushed:
        how = ' after a rebase onto origin' if pushed == 'rebased' else ''
        print(f"tick: state committed and pushed{how} ({path})")
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
        return run_shadow(product, fresh=fresh)  # record only, never a command step

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

    ctx = Context(product, fresh=fresh)
    rc = 0
    ran = []
    for step, owner, command in rows:
        if owner == 'off':
            print(f"tick: step {step} off (another job runs it)")
            continue
        if step == 'daily' and not steps.daily_due(product, getattr(args, 'daily', False)):
            print("tick: step daily already ran today")
            continue
        t0 = time.monotonic()
        if owner == 'asf':
            step_rc = run_asf_step(step, ctx)
        else:
            step_rc = steps.run_command(step, command, steps.command_timeout())
            if step_rc:
                print(f"tick: step {step} exited {step_rc}")
        ran.append({'step': step, 'ok': not step_rc, 'seconds': round(time.monotonic() - t0, 1)})
        if step == 'daily' and step_rc == 0:
            steps.write_daily_stamp(product)
        rc = rc or (1 if step_rc else 0)
    if ran:
        rc = finish(ctx, ran) or rc
    return rc


def finish(ctx, ran):
    """The tick's one commit: the ``metrics/ticks`` line first, then ``tick: state`` over the
    derived state, the steps' events and that line together. Only when a step made the record
    clone this tick: a ``--steps`` run of command steps alone does not clone the record to log
    itself. Returns 1 when the commit or its push failed, else 0 — printed, never raised (the
    steps already ran)."""
    if not ctx.has_record:
        return 0
    write_tick_line(ctx, ran)
    try:
        return commit_and_push(ctx)
    except (subprocess.CalledProcessError, OSError, env.ConfigError) as e:
        print(f"tick: state not committed ({(getattr(e, 'stderr', None) or str(e)).strip()})")
        return 1


def _asf_step(step):
    """The callable ``(ctx) -> rc`` for an ``asf`` step (looked up at call time, so a test can
    replace one module attribute)."""
    if step == 'record':
        return lambda ctx: run_record_step(ctx.product, fresh=ctx.fresh, ctx=ctx)
    import importlib
    return importlib.import_module(f'asf.tick.step_{step}').run


def run_asf_step(step, ctx):
    """Run one ``asf`` step; any failure is one ``[step:<name>] FAILED`` line and rc 1 — never
    an exception out of the tick."""
    try:
        return _asf_step(step)(ctx) or 0
    except Exception as e:  # noqa: BLE001 — one step's failure never stops the rest
        detail = (getattr(e, 'stderr', None) or str(e) or type(e).__name__).strip()
        print(f"[step:{step}] FAILED {detail.splitlines()[0] if detail else type(e).__name__}")
        return 1


def tick_line(ctx, ran, now=None):
    """The ``metrics/ticks`` line for this tick (the stream's schema + ``product`` + ``steps``)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return dict(ctx.counts, ts=now.strftime('%Y-%m-%dT%H:%M:%SZ'), tick=int(now.strftime('%H%M')),
                duration_s=round(sum(r['seconds'] for r in ran), 1), quota={},
                refused_files={}, product=ctx.product.name, steps=ran)


def write_tick_line(ctx, ran):
    """Append the tick line to the record clone (:func:`finish` commits it). A failure here is
    printed, never raised (the steps already ran)."""
    try:
        root = ctx.record_root()
        line = tick_line(ctx, ran)
        path = os.path.join(root, 'metrics', 'ticks', f"{line['ts'][:10]}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + '\n')
    except (subprocess.CalledProcessError, OSError, env.ConfigError) as e:
        print(f"tick: step log not written ({(getattr(e, 'stderr', None) or str(e)).strip()})")


def register(subparsers):
    """Add the ``tick`` subcommand (replaces the bare one in ``asf.cli``)."""
    p = subparsers.add_parser(
        'tick', help='the scheduled steps, in order: record, health, wave, prs, harvest, batch, daily '
                     '(a failing step never stops the rest; exit 1 if any failed)')
    p.add_argument('--product')
    p.add_argument('--shadow', action='store_true',
                   help='run record against a shadow clone, never pushed, never a command step')
    p.add_argument('--fresh', action='store_true', help="bypass evidence's cache")
    p.add_argument('--steps', help=f"comma list, a subset of {','.join(steps.STEPS)} (default: all)")
    p.add_argument('--manifest', action='store_true', help='print step / owner / command and exit')
    p.add_argument('--daily', action='store_true', help='run the daily step even if it already ran today')
    return p
