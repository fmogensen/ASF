"""asf.tick.tick — ``asf tick``: the scheduled steps (:mod:`asf.tick.steps`), in order
``record → wave → health → groom → prs → harvest → batch → daily``, then the record's tail.

``record`` is step 0: ingest → plan-tasks → replan → slice → index, the parts the wave decides
from, run in the tick's own clone of the product's backlog (:mod:`asf.tick.shadow`). The wave runs
right after it (``tick.wave_first``, default on: 2026-10-06 one tick spent 22 minutes on
bookkeeping before it launched anything); the tick logs the span as ``tick: wave latency`` and
keeps it (:mod:`asf.tick.wave_latency`, the dwell watchdog's ``wave_latency``). When health then
ends, holds or corrects a run, the wave runs once more after the groom, so a freed seat is still
this tick's; health spares the runs this tick's own wave launched. The record's tail — metrics
backfill → plan-order → file-bugs → rollup → index — runs as ``record-tail`` before the harvest
starts (the background harvest reads the record), each part on its cadence
(:mod:`asf.tick.cadence`: ``every_n``, at least hourly); so may any step but ``record``, ``wave``
and ``daily``. ``tick.wave_first: false`` keeps the order this replaced.
``health``, ``groom``, ``wave``, ``prs``, ``harvest`` and ``daily`` are :mod:`asf.tick.step_health`,
``step_groom``, ``step_wave``, ``step_prs``, ``step_harvest`` and ``step_daily``; ``batch`` is a command the product declares (or ``off``), as is
any step a product chooses to run with its own command.

Each step the tick actually runs prints a start line naming the step, its owner, the tick's pid and
the UTC time, and an end line with the seconds and ``ok=``; a step the tick skipped prints neither
(F-0142). A step that fails prints one ``[step:<name>] FAILED <why>`` and the tick goes on to the
next one; the exit code is 1 when any step failed. Every step shares one :class:`Context`, so the record
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

from asf import capacity, ci_queue, env
from asf.record.index import do_index
from asf.tick import network, steps, summary, watchdog


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
    """Step 0, the record step's own work: the parts the wave decides from
    (:func:`run_record_fast`). The record's bookkeeping — backfill, plan-order, file-bugs, rollup
    (:func:`run_record_tail`) — runs after the wave in a tick, on its cadence
    (:mod:`asf.tick.cadence`); ``--shadow``, ``--dry-run`` and a ``record`` run alone run it right
    after this (:func:`run_record`). Returns nothing; prints what each part printed."""
    run_record_fast(root, product, fresh=fresh)


def run_record(root, product, fresh=False):
    """The whole record at once: step 0, then every part of its tail."""
    run_step0(root, product, fresh=fresh)
    run_record_tail(root, product)


def _record_env(product):
    """``asf.record.ingest.cmd_ingest`` calls ``evidence.load()`` with no product (a gap the fuller
    0.1 command surface is meant to close — see ``asf/cli.py``'s module docstring): it falls back
    to ``$ASF_PRODUCT``/``config.yaml``'s ``default_product``, so this sets ``$ASF_PRODUCT`` for
    the tick's own process rather than widen ``cmd_ingest``'s signature for one caller."""
    os.environ['ASF_PRODUCT'] = product.name


def run_record_fast(root, product, fresh=False):
    """What the wave decides from: ingest → plan-tasks → replan → slice → index. Every one of
    them changes which rows may launch (an item's state, a freshly minted or re-cut Task, a
    Feature's delivery), so they run on every tick, before the wave."""
    _record_env(product)
    from asf.record import stage
    from asf.record.ingest import cmd_ingest

    with timed('ingest'):
        cmd_ingest(_ns(fresh=fresh, product=product.name), root)
    if product.repo_dir:  # B-0060: a landed plan's Tasks become cards, once
        from asf.evidence import evidence
        from asf.record.plan_tasks import mint_plan_tasks
        with timed('plan-tasks'):
            mint_plan_tasks(root, product, evidence.load(product=product))
        from asf.record import plan_order
        # a Feature's reshape: its landed replan rewrites, adds and drops its open Tasks
        from asf.record import replan
        with timed('replan'):
            replan.apply_replans(root, product, plan_order.trunk_reader(product))
        if product.conventions.delivery_feature():
            # the Feature is the delivery unit: a planned Feature's free Tasks become one
            # delivery (or ordered slices), the freshly minted and the half-built alike
            from asf.record import slice as slice_mod
            with timed('slice'):
                stage.guarded(root, 'slice', slice_mod.deliveries,
                              (product, slice_mod.busy_items(product)), product=product)
    with timed('index'):
        do_index(root)


def run_record_tail(root, product, due=None):
    """The record's bookkeeping, which decides no launch: metrics backfill → plan-order →
    file-bugs → rollup, then the index again when any of them ran. ``due(name)`` says whether a
    part runs (default: all — :class:`asf.tick.cadence.Cadence` in a tick). The wave never waits
    on these: a Task with no ``after:`` yet still waits on its plan's order through the wave's
    own overlay (:func:`asf.record.plan_order.overlay`).

    The backfill reads CI runs only — sessions come from the workers' own ledger, not from a
    launcher directory — and a product with ``ci: {provider: none}`` has none to read. Returns
    the parts that ran."""
    _record_env(product)
    due = due or (lambda name: True)
    from asf import approvals
    from asf.metrics.metrics import cmd_backfill, cmd_rollup
    from asf.record import stage
    from asf.tick.file_bugs import cmd_file_bugs

    ran = []
    if ci_provider(product) != 'none' and due('backfill'):
        ran.append('backfill')
        with timed('backfill'):
            cmd_backfill(_ns(days=1, sessions=None, log=None, workflow=ci_workflow(product),
                             launch_dir=None, product=product.name), root)
    if product.repo_dir and due('plan-order'):
        # open Tasks minted before the minter wrote order: the plan's order lands as `after:`
        from asf.record import plan_order
        ran.append('plan-order')
        with timed('plan-order'):
            stage.guarded(root, 'plan-order', plan_order.backfill,
                          (plan_order.trunk_reader(product),), product=product)
    if due('file-bugs'):
        ran.append('file-bugs')
        default_bug_epic = product.conventions.get('default_bug_epic')
        with timed('file-bugs'):
            bug_args = _ns(default_bug_epic=default_bug_epic, product=product.name,
                           file_bug_level=approvals.level_of(product, 'file_bug'))
            stage.guarded(root, 'file-bugs', lambda r: cmd_file_bugs(bug_args, r),
                          product=product)
    if due('rollup'):
        ran.append('rollup')
        with timed('rollup'):
            cmd_rollup(_ns(day=None, no_releases=False, product=product.name), root)
    if ran:
        with timed('index'):
            do_index(root)
    return ran


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
        self.seats = None         # the wave's seat reading, carried on the tick line
        self.record_tail_pending = False  # the record's fast parts ran; its tail is still due
        self.started = time.monotonic()   # the tick's start: what wave_latency_s is aged from
        self.wave_latency_s = None
        self.wave_started_at = None       # UTC stamp of this tick's wave start, once it ran
        self.health_freed = False         # health ended, held or corrected a run this tick
        self.cadence = None               # this tick's asf.tick.cadence.Cadence, once made
        from asf.facts import cache as facts_cache, landing as facts_landing
        facts_cache.clear()  # a tick reads its facts afresh (asf.facts.cache)
        # under flags.facts shadow|new: the pass's one open-PR read, the shadow's only gh fact
        facts_landing.prime(product)

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
        if alone:
            run_record_tail(root, product)
        elif not wave_first_on():  # the order before wave-first: the whole record, here
            run_record_tail(root, product, due=ctx.cadence.due if ctx.cadence else None)
        else:  # the tail runs after the wave (:func:`run_record_tail_step`)
            ctx.record_tail_pending = True
    except (subprocess.CalledProcessError, env.ConfigError) as e:
        detail = (getattr(e, 'stderr', None) or str(e)).strip()
        print(f"tick: record failed ({detail})")
        ctx.stale_reason = _reason(detail)
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
    run_record(backlog_root, product, fresh=fresh)
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
    started = time.monotonic()  # the tick's start, for wave_latency_s
    product = env.load_product(getattr(args, 'product', None))
    fresh = getattr(args, 'fresh', False)

    if getattr(args, 'shadow', False):
        return run_shadow(product, fresh=fresh)  # record only, never a command step

    if getattr(args, 'with_venv', None) or getattr(args, 'state_copy', None):
        if not getattr(args, 'dry_run', False):
            print('tick: --with-venv and --state-copy go with --dry-run')
            return 2
    if getattr(args, 'dry_run', False):
        from asf.tick import dry_run
        if getattr(args, 'with_venv', None):  # that venv's own rehearsal, on the same snapshot
            return dry_run.run_with_venv(product, args.with_venv, fresh=fresh,
                                         state_copy=getattr(args, 'state_copy', None))
        return dry_run.run(product, fresh=fresh,  # a throwaway copy; never pushes or launches
                           state_copy=getattr(args, 'state_copy', None))

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

    from asf import upgrade
    off = upgrade.checkout_off_main()
    if off:
        print(f'tick: warning — {off}', file=sys.stderr)
    if upgrade.waiting(product.name):
        return 0  # a pending upgrade needs a gap between ticks: this one does not start
    if upgrade.draining(product.name):
        # a move drains: what is in flight lands (the harvest's background run judges the
        # batches), nothing new launches
        kept = [r for r in rows if r[0] in upgrade.DRAIN_STEPS]
        dropped = [r[0] for r in rows if r[0] not in upgrade.DRAIN_STEPS]
        if dropped:
            print(f"tick: a move of {product.name} drains (asf upgrade --product) — "
                  f"{', '.join(dropped)} wait; {', '.join(r[0] for r in kept) or 'nothing'} runs")
        rows = kept
        if not rows:
            return 0

    if not any(owner == 'asf' for _, owner, _ in rows):
        return _run_locked(args, product, fresh, rows, chosen, Locks(product, held=False),
                           started=started)
    wait_s = DAILY_LOCK_WAIT_S if any(r[0] == 'daily' for r in rows) else 0
    lock = acquire_lock(product, wait_s)
    if lock is None:
        print(f"tick: another tick of {product.name} is running — skipped")
        return 0
    # a tick that never ends held this lock forever and no later tick started (2026-10-04):
    # past its wall-clock budget it names its step and exits, which releases the lock
    timer = watchdog.arm(tick_budget_s(product, [r[0] for r in rows]))
    from asf import dwell
    dwell.mark_tick(product)   # what the dwell watchdog's tick_running ages
    try:
        return _run_locked(args, product, fresh, rows, chosen, Locks(product, held=True),
                           started=started)
    finally:
        if timer is not None:
            timer.cancel()
        lock.close()


def tick_budget_s(product, step_names):
    """This tick's wall-clock budget (:mod:`asf.tick.watchdog`): config ``tick.budget_s``, else
    a multiple of the interval of the clock running ``step_names``."""
    configured = watchdog.configured()
    interval = None if configured is not None else watchdog.clock_interval(product, step_names)
    return watchdog.seconds_for(product, step_names, budget_s=configured, interval_s=interval)


def _run_locked(args, product, fresh, rows, chosen, locks=None, started=None):
    locks = locks or Locks(product, held=True)
    ctx = Context(product, fresh=fresh)
    if started is not None:
        ctx.started = started
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
        upgraded.append(upgrade.cmd_upgrade(_ns(skip_pipx=False, ref=head, owner=product.name,
                                                wait=upgrade.drain_wait_s())))
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
    if upgraded and upgraded[-1] == drift.DEFERRED and upgrade.read_pending(product.name):
        # the owner already drained for upgrade.drain_wait_s and the floor is still busy. It
        # keeps working (skipping its steps starved its own product for as long as the slowest
        # other tick ran, 2026-09-25: 40 min), but it drains too: while the marker is pending
        # no tick spawns a background harvest (step_harvest.run), the other products' ticks do
        # not start, and what runs ends — the owner's next start finds the gap.
        marker = upgrade.read_pending(product.name)
        print(f'tick: upgrade to {marker["sha"][:7]} pending — this tick runs'
              ' without a new background harvest; the install goes at the next start')
    if any(s == 'wave' and o == 'asf' for s, o, _ in rows) and not any(s == 'watchdog' for s, _, _ in rows):
        # the dwell watchdog (asf.dwell) runs with every asf wave, whichever clock carries it — a
        # product whose clocks predate the step is watched too; `steps: {watchdog: off}` stops it
        w_step, w_owner, w_command = steps.resolve(product, ['watchdog'])[0]
        if w_owner != 'off':
            at = max(i for i, r in enumerate(rows) if r[0] in ('wave', 'prs', 'harvest', 'batch'))
            rows = list(rows[:at + 1]) + [(w_step, w_owner, w_command)] + list(rows[at + 1:])
    if not any(s == 'daily' for s, _, _ in rows):
        # this tick's own clock doesn't carry daily (it isn't the daily clock) — catch it up
        # when its own clock's time has passed and it still hasn't succeeded today (B-0123)
        d_step, d_owner, d_command = steps.resolve(product, ['daily'])[0]
        if d_owner != 'off' and steps.daily_catch_up_due(product):
            print(steps.daily_catch_up_message(product))
            rows = list(rows) + [(d_step, d_owner, d_command)]
    rows = wave_first(rows)
    from asf.tick import cadence as cadence_mod
    ctx.cadence = cadence_mod.Cadence(product, chosen or [])
    # every asf step's module, imported now rather than lazily as each step runs (B-0135): an
    # install that swaps the package mid-tick can no longer land between two of this tick's own
    # step imports and mix old and new modules in one run
    for step, owner, _ in rows:
        if owner == 'asf':
            _asf_step(step)
    rc = 0
    ran = []
    resolved = None
    started = time.monotonic()
    queue = list(rows)
    while queue:
        step, owner, command = queue.pop(0)
        if step == 'wave' and groom_first(ctx, queue):
            # a new inbox card: the groom mints it before this wave, as it always did
            groom = next(r for r in queue if r[0] == 'groom')
            queue.remove(groom)
            queue[:0] = [groom, (step, owner, command)]
            continue
        if step in AFTER_TAIL and ctx.record_tail_pending and not ctx.stale_reason:
            # the record's tail lands before the background harvest starts reading the record
            ctx.record_tail_pending = False
            entry = run_record_tail_step(ctx)
            if entry:
                ran.append(entry)
        if owner == 'off':
            print(f"tick: step {step} off (another job runs it)")
            continue
        if step == 'daily' and not steps.daily_due(product, getattr(args, 'daily', False)):
            print("tick: step daily already ran today")
            continue
        if step in ctx.cadence.n and not ctx.cadence.due(step):
            continue  # its every_n: the one `deferred` line is the cadence's
        t0 = time.monotonic()
        watchdog.enter(step)
        if step == 'wave' and ctx.wave_latency_s is None:
            note_wave_latency(ctx)
        if owner == 'asf':
            print(step_start_line(step, owner), flush=True)
            step_rc = run_asf_step(step, ctx)
            if step == 'harvest':
                lane_check(ctx)  # report only: never the step's rc, never an abort
        else:
            if resolved is None:
                resolved = capacity.resolve(product)
            # the fixed in-flight ceiling gates the batch step only for a product the CI queue
            # does not admit: a queued one is admitted by free runner capacity against the
            # run's need (asf.ci_queue.ceiling_gate), the ceiling its fallback when the runners
            # are unreadable
            if (step == 'batch' and ci_queue.mode(product) == 'off' and resolved.ci is not None
                    and resolved.ci_inflight is not None
                    and resolved.ci_inflight >= resolved.ci):
                print(f"waits    batch — at ci capacity ({resolved.ci_inflight}/{resolved.ci})")
                step_rc = 0
            elif step == 'batch' and not ci_queue.admit(
                    product, 'batch', 'batch', item='batch',
                    inflight=resolved.ci_inflight).admitted:
                step_rc = 0  # the hold's one line is printed by the queue
            else:
                with locks.command(step) as free:
                    if not free:
                        print(f"tick: step {step} is already running — skipped")
                        continue
                    # after the guard, never before: a start line the doctor reads as a running
                    # step must mean a step that started (F-0142, D11)
                    print(step_start_line(step, owner), flush=True)
                    step_rc = steps.run_command(step, command, steps.command_timeout(),
                                                cwd=product.repo_dir or None,
                                                extra_env=capacity.env_overlay(resolved, product))
                if step_rc:
                    print(f"tick: step {step} exited {step_rc}")
                    if step == 'daily':
                        steps.write_daily_failure(product, f"exited {step_rc}")
        ran.append({'step': step, 'ok': not step_rc, 'seconds': round(time.monotonic() - t0, 1)})
        print(step_end_line(step, ran[-1]['seconds'], ok=not step_rc, owner=owner), flush=True)
        if step == 'health' and ctx.wave_started_at and ctx.health_freed and not step_rc:
            # health ended, held or corrected a run after this tick's wave: the seat it freed
            # and the correction it wrote are this tick's to launch, as when health ran first —
            # one more wave, after the groom when it is still to run
            again = next(r for r in rows if r[0] == 'wave')
            ctx.lane_passed = False  # the branches health just settled advance in its lane pass
            at = next((i + 1 for i, r in enumerate(queue) if r[0] == 'groom'), 0)
            queue.insert(at, again)
        if step == 'daily' and step_rc == 0:
            steps.write_daily_stamp(product)
        rc = rc or (1 if step_rc else 0)
        if step == 'record':
            from asf.tick import record_health
            record_health.record(ctx.product, ok=not step_rc, reason=ctx.stale_reason)
        if step == 'record' and step_rc:
            # B-0083: a failed record leaves the last good index in place; every later step would
            # act on a stale board with full confidence, so none of them runs
            print(f"tick: record failed — {ctx.stale_reason or 'see above'}; nothing else ran")
            break
    if ctx.record_tail_pending and not ctx.stale_reason:  # a clock with no harvest
        ctx.record_tail_pending = False
        entry = run_record_tail_step(ctx)
        if entry:
            ran.append(entry)
    watchdog.enter('commit')
    if ran and ctx.has_record:
        with locks.record('state commit') as ok:
            if ok:
                rc = finish(ctx, ran) or rc
    print(total_line(time.monotonic() - started))
    if ctx.stale_reason:
        print(f"RECORD STALE — {ctx.stale_reason}\n")
    summary.run(ctx, chosen, ran=ran)
    return rc


DEFAULT_WAVE_FIRST = True
#: the steps the record's tail runs before: the background harvest reads the record it writes
AFTER_TAIL = ('harvest', 'batch', 'watchdog', 'daily')


def wave_first_on():
    """``tick.wave_first`` (default true): the wave runs right after the record's fast parts."""
    v = (env.load_config().get('tick') or {}).get('wave_first')
    return DEFAULT_WAVE_FIRST if v is None else bool(v)


def wave_first(rows):
    """``rows`` with ``wave`` moved up to right after ``record`` (to the front when the clock
    carries no record): health, groom and the record's tail no longer stand between a tick's
    start and its launches (2026-10-06: 22 minutes). When health then ends, holds or corrects a
    run, the wave runs once more after it (and after the groom), so a freed seat or a correction
    is still launched by the same tick; a new inbox card has the groom run before the first wave
    (:func:`groom_first`). Unchanged when ``tick.wave_first`` is false or the clock carries no
    wave."""
    names = [r[0] for r in rows]
    if 'wave' not in names or not wave_first_on():
        return rows
    wave = rows[names.index('wave')]
    rest = [r for r in rows if r[0] != 'wave']
    at = next((i + 1 for i, r in enumerate(rest) if r[0] == 'record'), 0)
    return rest[:at] + [wave] + rest[at:]


def fresh_inbox(ctx):
    """True when the record's intake holds a card the groom has not asked about yet (no
    ``## Question`` block): one it would mint into a card on this tick."""
    try:
        d = os.path.join(ctx.record_root(), ctx.product.conventions.intake_dir)
        names = [n for n in os.listdir(d) if n.endswith('.md')]
    except (OSError, subprocess.CalledProcessError, env.ConfigError, AttributeError):
        return False
    for name in names:
        try:
            with open(os.path.join(d, name), encoding='utf-8') as f:
                if not any(line.strip() == '## Question' for line in f):
                    return True
        except OSError:
            continue
    return False


def groom_first(ctx, queue):
    """Whether the groom, still queued behind the wave, runs before it on this tick: only when
    a new inbox card waits (:func:`fresh_inbox`), so a card filed is launched by the same tick —
    a tick with nothing new to mint never waits on the groom."""
    return any(r[0] == 'groom' and r[1] == 'asf' for r in queue) and fresh_inbox(ctx)


def note_wave_latency(ctx):
    """The tick's start → this wave's start: one line, the tick's ``wave_latency_s`` and the
    product's ``wave-latency.json`` (:mod:`asf.tick.wave_latency`)."""
    from asf.tick import wave_latency
    ctx.wave_started_at = _stamp()  # health spares what this wave launches (step_health)
    ctx.wave_latency_s = round(time.monotonic() - ctx.started, 1)
    print(f'tick: wave latency {ctx.wave_latency_s:.1f}s (tick start → wave start)', flush=True)
    wave_latency.write(ctx.product, ctx.wave_latency_s)


def run_record_tail_step(ctx):
    """The record's tail (:func:`run_record_tail`) after the tick's other steps, each part on its
    cadence; start and end lines as a step's (``record-tail``). A failure is one ``FAILED`` line
    and ``ok: false`` on the tick line — the next due tick re-derives it; never raised."""
    t0 = time.monotonic()
    watchdog.enter('record-tail')
    print(step_start_line('record-tail', 'asf'), flush=True)
    ok = True
    try:
        run_record_tail(ctx.record_root(), ctx.product, due=ctx.cadence.due)
    except Exception as e:  # noqa: BLE001 — the tail never costs the tick its commit
        ok = False
        detail = (getattr(e, 'stderr', None) or str(e) or type(e).__name__).strip()
        print(f"[step:record-tail] FAILED {_first_line(detail) or type(e).__name__}")
    entry = {'step': 'record-tail', 'ok': ok, 'seconds': round(time.monotonic() - t0, 1)}
    print(step_end_line('record-tail', entry['seconds'], ok=ok), flush=True)
    return entry


def step_start_line(step, owner, pid=None, at=None):
    """``[step:<name>] start owner=<asf|command> pid=<pid> at=<ts>`` — printed as the step begins,
    so the tick log names the step it is inside while the step is still running, and the doctor's
    SCHEDULER row can read it back (F-0142). ``pid`` is the tick's own process: the step's owner is
    what the line is about, and a command step's child is named on its own
    ``[command:<step>]`` lines (D4)."""
    return (f'[step:{step}] start owner={owner} pid={pid or os.getpid()} '
            f'at={at or _stamp()}')


def step_end_line(step, seconds, ok=True, owner='asf', pid=None, at=None):
    """``[step:<name>] <seconds>s ok=<yes|no> owner=<owner> pid=<pid> at=<ts>`` — printed as each
    step ends, so a slow step is named in the log while the tick is still running, and the start
    line it closes is no longer read as a running step.

    The ``[step:<name>] <seconds>s`` prefix is the shape this line has always had and is what
    `asf doctor` matches an *ended* step on (D1); the fields after it are F-0142's."""
    return (f'[step:{step}] {seconds:.1f}s ok={"yes" if ok else "no"} owner={owner} '
            f'pid={pid or os.getpid()} at={at or _stamp()}')


def total_line(seconds):
    return f'tick: total {seconds:.1f}s'


def finish(ctx, ran):
    """The tick's one commit: the ``metrics/ticks`` line first, then ``tick: state`` over the
    derived state, the steps' events and that line together. Only when a step made the record
    clone this tick: a ``--steps`` run of command steps alone does not clone the record to log
    itself. Returns 1 when the commit or its push failed, else 0 — printed, never raised (the
    steps already ran). A failure here named no step in ``ran`` (B-0146: every named step read
    ``ok`` while the tick exited 1) — on failure it appends its own ``commit`` entry, so the TICK
    line names it too."""
    if not ctx.has_record:
        return 0
    file_invariant_bugs(ctx)
    write_tick_line(ctx, ran)
    try:
        rc = commit_and_push(ctx)
        reason = None if not rc else 'push refused'
    except (subprocess.CalledProcessError, OSError, env.ConfigError) as e:
        detail = (getattr(e, 'stderr', None) or str(e)).strip()
        print(f"tick: state not committed ({detail})")
        rc, reason = 1, _first_line(detail) or type(e).__name__
    if rc:
        ran.append({'step': 'commit', 'ok': False, 'seconds': 0.0, 'reason': reason})
    return rc


def file_invariant_bugs(ctx):
    """The record check point's last half (R9): every write a staged writer was refused on this
    tick (:func:`asf.record.stage.drain` — the offending paths were already put back, the rest
    stands) becomes one Bug per ``(invariant, cause)`` in the record clone, committed with the
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


#: B-0124: a tick that fails offline says "offline", not the raw git error, so the reason is
#: legible on the status table and in the digest. The markers live in :mod:`asf.tick.network`.
_OFFLINE_MARKERS = network.OFFLINE_MARKERS


def _reason(detail):
    text = _first_line(detail)
    return 'offline' if network.is_offline_text(text) else text


def run_asf_step(step, ctx):
    """Run one ``asf`` step; any failure is one ``[step:<name>] FAILED`` line and rc 1 — never
    an exception out of the tick."""
    from asf import gh_limit
    try:
        return _asf_step(step)(ctx) or 0
    except gh_limit.RateLimited:
        # GitHub's rate limit: whatever the step read is unknown, so it decided nothing — one
        # line (already printed once by the wrapper), and the next tick decides. A record or a
        # daily cut short is a failed one (nothing after a stale record runs; the daily reruns).
        print(f"[step:{step}] skipped — GitHub rate limit; no GitHub decision this tick")
        if step == 'record':
            ctx.stale_reason = 'GitHub rate limit'
            return 1
        if step == 'daily':
            steps.write_daily_failure(ctx.product, 'GitHub rate limit')
            return 1
        return 0
    except Exception as e:  # noqa: BLE001 — one step's failure never stops the rest
        import traceback
        detail = (getattr(e, 'stderr', None) or str(e) or type(e).__name__).strip()
        print(f"[step:{step}] FAILED {_first_line(detail) or type(e).__name__}")
        # the tick log carries the whole traceback: the one line names the step, this the line
        print(traceback.format_exc().rstrip(), flush=True)
        if step == 'record':
            ctx.stale_reason = _reason(detail) or type(e).__name__
        if step == 'daily':
            steps.write_daily_failure(ctx.product, _first_line(detail) or type(e).__name__)
        return 1


def tick_line(ctx, ran, now=None):
    """The ``metrics/ticks`` line for this tick (the stream's schema + ``product`` + ``steps``)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    line = dict(ctx.counts, ts=now.strftime('%Y-%m-%dT%H:%M:%SZ'), tick=int(now.strftime('%H%M')),
                duration_s=round(sum(r['seconds'] for r in ran), 1), quota={},
                refused_files={}, product=ctx.product.name, steps=ran)
    seats = getattr(ctx, 'seats', None)
    if isinstance(seats, dict):   # the wave's seat reading (asf.metrics.throughput)
        line['seats'] = seats
    if getattr(ctx, 'wave_latency_s', None) is not None:
        line['wave_latency_s'] = ctx.wave_latency_s
    return line


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
        return
    if line.get('seats'):
        seat_alarm(ctx, os.path.dirname(path), line['ts'])


def seat_alarm(ctx, ticks_dir, ts, out=print):
    """One ``metrics: BREACH seats`` line while an idle-while-launchable stretch runs
    (:func:`asf.metrics.throughput.live_breach`, over today's and yesterday's tick lines)."""
    from asf.metrics import metrics, throughput
    try:
        day = datetime.date.fromisoformat(ts[:10])
        ticks = []
        for d in (day - datetime.timedelta(days=1), day):
            ticks += metrics.read_file(os.path.join(ticks_dir, f'{d.isoformat()}.jsonl'))
        line = throughput.live_breach(ticks, throughput.settings(ctx.product)[0])
    except Exception as e:  # noqa: BLE001 — an alarm not raised never fails the tick
        out(f'metrics: seat alarm not read ({type(e).__name__}: {e})')
        return
    if line:
        out(line)


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
    p.add_argument('--state-copy', metavar='DIR',
                   help='with --dry-run: the state snapshot to rehearse — made at DIR when absent '
                        '(the caller removes it), read as it is when present; two runs that name '
                        'one DIR rehearse the same state')
    p.add_argument('--with-venv', metavar='VENV',
                   help="with --dry-run: run the rehearsal under that venv's own interpreter "
                        '(any release), on --state-copy (the ab-dry-run.sh tool)')
    p.add_argument('--fresh', action='store_true', help="bypass evidence's cache")
    p.add_argument('--steps', help=f"comma list, a subset of {','.join(steps.STEPS)} (default: all)")
    p.add_argument('--manifest', action='store_true', help='print step / owner / command and exit')
    p.add_argument('--daily', action='store_true', help='run the daily step even if it already ran today')
    return p
