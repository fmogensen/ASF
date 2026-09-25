"""asf.tick.tick — ``asf tick``: the scheduled steps (:mod:`asf.tick.steps`), in order
``record → health → groom → wave → prs → harvest → batch → daily``.

``record`` is step 0: metrics backfill → ingest → file-bugs → rollup → index, run in the tick's
own clone of the product's backlog (:mod:`asf.tick.shadow`).
``health``, ``groom``, ``wave``, ``prs``, ``harvest`` and ``daily`` are :mod:`asf.tick.step_health`,
``step_groom``, ``step_wave``, ``step_prs``, ``step_harvest`` and ``step_daily``; ``batch`` is a command the product declares (or ``off``), as is
any step a product chooses to run with its own command.

A step that fails prints one ``[step:<name>] FAILED <why>`` and the tick goes on to the next one;
the exit code is 1 when any step failed. Every step shares one :class:`Context`, so the record
clone is made (reset to origin) once per tick however many steps read it. After the steps, one
line goes to ``metrics/ticks/<day>.jsonl`` in the record clone — the ``ticks`` stream's schema
(``tick``, ``launches``, ``stalls``, ...) plus ``product`` and ``steps: [{step, ok, seconds}]`` —
and only then is the clone committed (``tick: state <ts>``) and pushed: one commit per tick, so
the derived state, the events the steps appended and the step log land together. One line per
tick, not per step: ``asf metrics rollup`` counts the stream's lines as ticks and reads every
stream field off each one. After the commit the tick prints its summary — the sessions in flight
and the ones that ended since the last tick on this clock (:mod:`asf.tick.summary`).

``--shadow`` runs step 0 against a shadow clone instead — never pushes, commits locally, never
runs a command step — then renders the six tables (:mod:`asf.views`) into ``<shadow>/tables/*.md``
so ``asf shadow-diff`` has something to compare against the pre-``asf`` tools' output.

``--dry-run`` runs record, the lane pass, wave planning and the harvest gate the way a real tick
would, against a throwaway copy of the whole state directory (:mod:`asf.tick.dry_run`) — never
pushes, opens or merges a PR, or launches a session; the real state directory is never opened for
writing. Prints every lane state, the rows it would launch and any ``INVARIANT`` line (plan §6
rollout).
"""
import argparse
import contextlib
import datetime
import json
import os
import subprocess
import sys
import time

from asf import capacity, env
from asf.record.index import do_index
from asf.tick import steps, summary


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


@contextlib.contextmanager
def timed(part, out=None):
    """``[record:<part>] N.Ns`` once the part is done (or has raised): each part of the record
    step says what it cost, so the next regression names itself in the tick log."""
    start = time.monotonic()
    try:
        yield
    finally:
        (out or print)(f"[record:{part}] {time.monotonic() - start:.1f}s", flush=True)


def run_step0(root, product, fresh=False):
    """metrics backfill → ingest → plan-tasks → file-bugs → rollup → index, against ``root``. Returns nothing;
    prints what each step printed, same as running the commands one at a time would.

    The backfill reads CI runs only — sessions come from the workers' own ledger, not from a
    launcher directory — and a product with ``ci: {provider: none}`` has none to read.

    ``asf.record.ingest.cmd_ingest`` calls ``evidence.load()`` with no product (a gap the fuller
    0.1 command surface is meant to close — see ``asf/cli.py``'s module docstring): it falls back
    to ``$ASF_PRODUCT``/``config.yaml``'s ``default_product``, so this sets ``$ASF_PRODUCT`` for
    the tick's own process rather than widen ``cmd_ingest``'s signature for one caller.
    """
    os.environ['ASF_PRODUCT'] = product.name

    from asf import approvals
    from asf.metrics.metrics import cmd_backfill, cmd_rollup
    from asf.record import stage
    from asf.record.ingest import cmd_ingest
    from asf.tick.file_bugs import cmd_file_bugs

    if ci_provider(product) != 'none':
        with timed('backfill'):
            cmd_backfill(_ns(days=1, sessions=None, log=None, workflow=ci_workflow(product),
                             launch_dir=None, product=product.name), root)
    with timed('ingest'):
        cmd_ingest(_ns(fresh=fresh, product=product.name), root)
    if product.repo_dir:  # B-0060: a landed plan's Tasks become cards, once
        from asf.evidence import evidence
        from asf.record.plan_tasks import mint_plan_tasks
        with timed('plan-tasks'):
            mint_plan_tasks(root, product, evidence.load(product=product))
        # open Tasks minted before the minter wrote order: the plan's order lands as `after:`
        from asf.record import plan_order
        with timed('plan-order'):
            stage.guarded(root, 'plan-order', plan_order.backfill,
                          (plan_order.trunk_reader(product),), product=product)
    default_bug_epic = product.conventions.get('default_bug_epic')
    with timed('file-bugs'):
        bug_args = _ns(default_bug_epic=default_bug_epic,
                       file_bug_level=approvals.level_of(product, 'file_bug'))
        stage.guarded(root, 'file-bugs', lambda r: cmd_file_bugs(bug_args, r), product=product)
    with timed('rollup'):
        cmd_rollup(_ns(day=None, no_releases=False, product=product.name), root)
    with timed('index'):
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
        self.stale_reason = None  # set when the record step failed: the index is not this tick's
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
        with timed('clone'):
            root = ctx.record_root()
        run_step0(root, product, fresh=fresh)
    except (subprocess.CalledProcessError, env.ConfigError) as e:
        detail = (getattr(e, 'stderr', None) or str(e)).strip()
        print(f"tick: record failed ({detail})")
        ctx.stale_reason = _first_line(detail)
        return 1
    return commit_and_push(ctx) if alone else 0


def commit_and_push(ctx):
    """Commit the record clone as ``tick: state <ts>`` and push it; one line saying which (two when
    origin moved during the tick and the clone was rebased onto it first — B-0030). Returns
    0 (nothing to commit, or pushed) or 1 (push refused — re-derived next run)."""
    from asf.tick import shadow
    path = ctx.record_root()
    if not shadow.commit_local(path, f"tick: state {_stamp()}"):
        print(f"tick: no change ({path})")
        rc = 0
    elif shadow.push(path, out=print):
        print(f"tick: state committed and pushed ({path})")
        rc = 0
    else:
        print(f"tick: state committed, push refused — re-derived next run ({path})")
        rc = 1
    # the read views read the operator's checkout: bring it up to what origin now holds
    shadow.sync_operator_checkout(ctx.product, out=print)
    return rc


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


#: The daily runs once a day, so it waits out a running tick; an interval tick skips (the next
#: one is minutes away, and a tick's harvest gate can run for half an hour).
DAILY_LOCK_WAIT_S = 45 * 60
LOCK_POLL_S = 10


#: A clock of command steps only holds no product lock while its commands run; it takes the lock
#: for its record parts, waiting up to this long for a running tick rather than skipping.
RECORD_LOCK_WAIT_S = 10 * 60


def lock_path(product):
    return os.path.join(env.state_dir(product), 'tick.lock')


def step_lock_path(product, step):
    """A command step's own lock: the same script never overlaps itself, whichever clock runs it."""
    return os.path.join(env.state_dir(product), f'tick-{step}.lock')


def _flock(path, wait_s=0):
    import fcntl
    f = open(path, 'a')
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if time.monotonic() >= deadline:
                f.close()
                return None
            time.sleep(LOCK_POLL_S)


def acquire_lock(product, wait_s=0):
    """The product's tick lock (an open file holding ``flock``), or ``None`` when another tick
    of the product still holds it after ``wait_s``. Every job of one product shares the record
    clone (``state/<product>/record``): two ticks at once raced its fetch and push
    (``cannot lock ref 'refs/remotes/origin/main'``). The lock dies with its process."""
    return _flock(lock_path(product), wait_s)


def acquire_step_lock(product, step):
    """A command step's lock, or ``None`` when that step is already running (never waits)."""
    return _flock(step_lock_path(product, step))


class Locks:
    """Which locks a tick holds. A clock with an ``asf`` step holds the product lock throughout
    (``held``). A clock of command steps only (``held`` false) runs its commands outside it — a
    legacy script can run for half an hour, and the product's main clock skipped on every run
    meanwhile — and takes the product lock briefly, waiting, around each record part."""

    def __init__(self, product, held):
        self.product = product
        self.held = held

    @contextlib.contextmanager
    def record(self, what, wait_s=None):
        """Yields True while the record may be touched; False (and one line) when a running
        tick still holds it after ``wait_s`` (default :data:`RECORD_LOCK_WAIT_S`)."""
        if self.held:
            yield True
            return
        lock = acquire_lock(self.product, RECORD_LOCK_WAIT_S if wait_s is None else wait_s)
        if lock is None:
            print(f"tick: another tick of {self.product.name} holds the record — {what} skipped")
            yield False
            return
        try:
            yield True
        finally:
            lock.close()

    @contextlib.contextmanager
    def command(self, step):
        """Yields True while ``step``'s own lock is held; False when that step is already running."""
        lock = acquire_step_lock(self.product, step)
        if lock is None:
            yield False
            return
        try:
            yield True
        finally:
            lock.close()


def cmd_tick(args, root=None):
    product = env.load_product(getattr(args, 'product', None))
    fresh = getattr(args, 'fresh', False)

    if getattr(args, 'shadow', False):
        return run_shadow(product, fresh=fresh)  # record only, never a command step

    if getattr(args, 'dry_run', False):
        from asf.tick import dry_run
        return dry_run.run(product, fresh=fresh)  # a throwaway copy; never pushes, never launches

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

    if not any(owner == 'asf' for _, owner, _ in rows):
        return _run_locked(args, product, fresh, rows, chosen, Locks(product, held=False))
    wait_s = DAILY_LOCK_WAIT_S if any(r[0] == 'daily' for r in rows) else 0
    lock = acquire_lock(product, wait_s)
    if lock is None:
        print(f"tick: another tick of {product.name} is running — skipped")
        return 0
    try:
        return _run_locked(args, product, fresh, rows, chosen, Locks(product, held=True))
    finally:
        lock.close()


def _run_locked(args, product, fresh, rows, chosen, locks=None):
    locks = locks or Locks(product, held=True)
    ctx = Context(product, fresh=fresh)
    try:
        return _run_steps(args, product, ctx, rows, chosen, locks)
    except env.ConfigError as e:
        from asf.cli import needs_operator_line
        line = needs_operator_line(e, product.name)
        print(line)
        with locks.record('needs-operator event') as ok:
            if ok:
                try:
                    ctx.event('needs-operator', message=line)
                except (subprocess.CalledProcessError, OSError, env.ConfigError):
                    pass
        return 2


def _run_steps(args, product, ctx, rows, chosen, locks=None):
    from asf import drift, upgrade
    locks = locks or Locks(product, held=True)
    # the version check can upgrade the install: never under a running tick, and never worth
    # delaying a command clock for (the running tick prints it)
    upgraded = []

    def run_upgrade(head):
        upgraded.append(upgrade.cmd_upgrade(_ns(skip_pipx=False, ref=head)))
        return upgraded[-1]

    with locks.record('version check', wait_s=0) as ok:
        if ok:
            drift.report(product, autonomy=str((product.approvals or {}).get('upgrade', '')).lower(),
                         upgrade=run_upgrade)
    if upgraded and upgraded[-1] == 0:
        # this process still runs the old package over a replaced install: a later lazy import
        # would load the new one half-way through the tick, so the steps wait for the next tick
        print('tick: the install was upgraded under this tick; its steps run on the next tick')
        return 0
    if not any(s == 'daily' for s, _, _ in rows):
        # this tick's own clock doesn't carry daily (it isn't the daily clock) — catch it up
        # when its own clock's time has passed and it still hasn't succeeded today (B-0123)
        d_step, d_owner, d_command = steps.resolve(product, ['daily'])[0]
        if d_owner != 'off' and steps.daily_catch_up_due(product):
            print(steps.daily_catch_up_message(product))
            rows = list(rows) + [(d_step, d_owner, d_command)]
    rc = 0
    ran = []
    resolved = None
    started = time.monotonic()
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
            if step == 'harvest':
                lane_check(ctx)  # report only: never the step's rc, never an abort
        else:
            if resolved is None:
                resolved = capacity.resolve(product)
            if (step == 'batch' and resolved.ci is not None and resolved.ci_inflight is not None
                    and resolved.ci_inflight >= resolved.ci):
                print(f"waits    batch — at ci capacity ({resolved.ci_inflight}/{resolved.ci})")
                step_rc = 0
            else:
                with locks.command(step) as free:
                    if not free:
                        print(f"tick: step {step} is already running — skipped")
                        continue
                    step_rc = steps.run_command(step, command, steps.command_timeout(),
                                                cwd=product.repo_dir or None,
                                                extra_env=capacity.env_overlay(resolved, product))
                if step_rc:
                    print(f"tick: step {step} exited {step_rc}")
        ran.append({'step': step, 'ok': not step_rc, 'seconds': round(time.monotonic() - t0, 1)})
        print(step_timing_line(step, ran[-1]['seconds']))
        if step == 'daily' and step_rc == 0:
            steps.write_daily_stamp(product)
        rc = rc or (1 if step_rc else 0)
        if step == 'record' and step_rc:
            # B-0083: a failed record leaves the last good index in place; every later step would
            # act on a stale board with full confidence, so none of them runs
            print(f"tick: record failed — {ctx.stale_reason or 'see above'}; nothing else ran")
            break
    if ran and ctx.has_record:
        with locks.record('state commit') as ok:
            if ok:
                rc = finish(ctx, ran) or rc
    print(total_line(time.monotonic() - started))
    if ctx.stale_reason:
        print(f"RECORD STALE — {ctx.stale_reason}\n")
    summary.run(ctx, chosen, ran=ran)
    return rc


def step_timing_line(step, seconds):
    """``[step:<name>] <seconds>s`` — printed as each step ends, so a slow step is named in the
    log while the tick is still running."""
    return f'[step:{step}] {seconds:.1f}s'


def total_line(seconds):
    return f'tick: total {seconds:.1f}s'


def finish(ctx, ran):
    """The tick's one commit: the ``metrics/ticks`` line first, then ``tick: state`` over the
    derived state, the steps' events and that line together. Only when a step made the record
    clone this tick: a ``--steps`` run of command steps alone does not clone the record to log
    itself. Returns 1 when the commit or its push failed, else 0 — printed, never raised (the
    steps already ran)."""
    if not ctx.has_record:
        return 0
    file_invariant_bugs(ctx)
    write_tick_line(ctx, ran)
    try:
        return commit_and_push(ctx)
    except (subprocess.CalledProcessError, OSError, env.ConfigError) as e:
        print(f"tick: state not committed ({(getattr(e, 'stderr', None) or str(e)).strip()})")
        return 1


def file_invariant_bugs(ctx):
    """The record check point's last half (R9): every write a staged writer was refused on this
    tick (:func:`asf.record.stage.drain` — the offending paths were already put back, the rest
    stands) becomes one Bug per ``(invariant, path)`` in the record clone, committed with the
    tick. Printed, never raised: an invariant never aborts a tick."""
    from asf.record import stage
    findings = stage.drain()
    if not findings:
        return {}
    try:
        from asf import approvals
        from asf.tick import file_bugs
        product = ctx.product
        return file_bugs.file_invariant_bugs(
            ctx.record_root(), findings, level=approvals.level_of(product, 'file_bug'),
            default_bug_epic=product.conventions.get('default_bug_epic'))
    except Exception as e:  # noqa: BLE001 — a Bug not filed is refiled next time it recurs
        print(f"tick: invariant Bugs not filed ({type(e).__name__}: {e})")
        return {}


def lane_check(ctx):
    """The lane check point, after harvest: the ``lane`` invariants reported, I9 an event."""
    from asf import invariants
    return invariants.lane_report(ctx.product, event=lambda kind, **f: ctx.event(kind, **f))


def _asf_step(step):
    """The callable ``(ctx) -> rc`` for an ``asf`` step (looked up at call time, so a test can
    replace one module attribute)."""
    if step == 'record':
        return lambda ctx: run_record_step(ctx.product, fresh=ctx.fresh, ctx=ctx)
    import importlib
    return importlib.import_module(f'asf.tick.step_{step}').run


def _first_line(text):
    lines = (text or '').strip().splitlines()
    return lines[0].strip() if lines else ''


def run_asf_step(step, ctx):
    """Run one ``asf`` step; any failure is one ``[step:<name>] FAILED`` line and rc 1 — never
    an exception out of the tick."""
    try:
        return _asf_step(step)(ctx) or 0
    except Exception as e:  # noqa: BLE001 — one step's failure never stops the rest
        import traceback
        detail = (getattr(e, 'stderr', None) or str(e) or type(e).__name__).strip()
        print(f"[step:{step}] FAILED {_first_line(detail) or type(e).__name__}")
        # the tick log carries the whole traceback: the one line names the step, this the line
        print(traceback.format_exc().rstrip(), flush=True)
        if step == 'record':
            ctx.stale_reason = _first_line(detail) or type(e).__name__
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
    p.add_argument('--dry-run', action='store_true',
                   help='record, the lane pass, wave planning and the harvest gate, against a '
                        'throwaway copy of the state directory — never pushes, opens or merges a '
                        'PR, or launches a session (plan §6 rollout)')
    p.add_argument('--fresh', action='store_true', help="bypass evidence's cache")
    p.add_argument('--steps', help=f"comma list, a subset of {','.join(steps.STEPS)} (default: all)")
    p.add_argument('--manifest', action='store_true', help='print step / owner / command and exit')
    p.add_argument('--daily', action='store_true', help='run the daily step even if it already ran today')
    return p
