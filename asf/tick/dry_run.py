"""asf.tick.dry_run — ``asf tick --dry-run``: the rollout's read-only rehearsal (plan §6).

Runs record, the lane pass, wave planning and the harvest's gate decisions the way a real tick
would — record → lane pass → wave planning → prs (folded into the lane pass, T2) → harvest, all
in this process, never spawning the harvest as the detached background process a live tick does
(:mod:`asf.tick.step_harvest`) — against a **throwaway copy** of the product's state
directory (:func:`asf.env.state_dir`, the tick's own record clone included, since it lives at
``state/<product>/record``). Every write a step makes lands on that copy; the real state
directory is never opened for writing, which is what makes it safe to run against a live
product's real state (package gate §11, rollout §6): nothing here reads the copy back into the
real one, and nothing pushes it anywhere.

Nothing is pushed (the record step's diff is read, never committed —
:func:`asf.tick.shadow.commit_local`/:func:`asf.tick.shadow.push` are never called), nothing is
written to GitHub or the trunk (a spec/plan's ``land_spec.adopt`` only stages the record clone's
working tree, never committed here; the lane's own ``dry_run=True`` turns every PR open, review
wait and merge into one ``lane: DRY …``/``DRY: would …`` line instead of the host call —
:class:`asf.harvest.lane.Lane`, reused unmodified: :func:`asf.harvest.harvest.run_product_harvest`
is exactly what the live harvest runs, on the same throwaway ``git worktree`` copy of the trunk
its gate always builds — the gate's own "dry runs in-process on copies") and nothing is launched
(the wave's rows are planned — :func:`asf.feeder.rows.plan_rows`, the same call
:mod:`asf.tick.step_wave` makes — and printed, never handed to :mod:`asf.workers.wave`).

Prints, in order:

* ``== record`` — ``git diff --stat`` of the copy's record clone after step 0 (ingest et al.,
  never committed);
* ``== lane`` — :func:`asf.tick.step_wave.lane_pass`'s own lines: a spec/plan branch adopted
  (:func:`asf.tick.land_spec.adopt`) and one ``lane: …``/``waiting …``/``held …``/``DRY: would
  …`` per open code branch or PR (:class:`asf.harvest.lane.Lane`);
* ``== wave`` — one line per row the feeder would launch (``would launch``) or hold (``waits``),
  and any ``INVARIANT …`` line the feeder check point logs
  (:func:`asf.invariants.feeder_gate`) — dropped rows never launch here either; under host
  pressure (:func:`asf.tick.step_wave.host_hold`) every launching row is a ``waits … — held:
  host pressure …`` line and the section ends on ``wave: held: …``, as a live tick's would;
* ``== harvest`` — the gate's own lines: any ``held``/``waiting``/``landed`` outcome the real
  gate would reach for a branch the lane pass brought to GATE.

Two runs back to back, with the real state and the product's origins unchanged in between, print
the same thing: no step here writes a wall-clock timestamp into what it prints.
"""
import os
import shutil
import subprocess
import tempfile

from asf import env


#: Left out of the copy: the worker worktrees are full product checkouts (tens of GB, with
#: symlinked dependency trees) that no dry-run step reads; the copy gets an empty dir in their
#: place, so nothing it runs can reach a live session's checkout either.
_NOT_COPIED = ('worktrees',)


def _copy_state(product):
    """A throwaway copy of ``product``'s state directory, under a fresh temp dir — all of it but
    :data:`_NOT_COPIED`, symlinks copied as links, never followed. Returns ``(tmp, copy_path)``;
    the caller removes ``tmp`` when done, and a copy that fails removes it here. Never mutates
    ``real``."""
    real = env.state_dir(product)  # makes it if missing; read-only below, never written to
    tmp = tempfile.mkdtemp(prefix='asf-dry-run-')
    copy_path = os.path.join(tmp, 'state')
    try:
        shutil.copytree(real, copy_path, symlinks=True,
                        ignore=shutil.ignore_patterns(*_NOT_COPIED))
        for name in _NOT_COPIED:
            os.makedirs(os.path.join(copy_path, name), exist_ok=True)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, copy_path


class _StateDirOverride:
    """``env.state_dir(product)`` resolves to a throwaway copy for one product only, for the
    life of this object; every other product (and every other call once it is undone) resolves
    for real — a dry run never swaps ``env.ASF_HOME`` itself, so ``config.yaml`` and every
    product file are still read for real, and another product's fair-share accounting still sees
    its real, live state."""

    def __init__(self, product_name, copy_path):
        self._name = product_name
        self._copy_path = copy_path
        self._real = env.state_dir

    def _patched(self, product=None):
        name = product.name if isinstance(product, env.Product) else \
            (product or env.default_product_name())
        return self._copy_path if name == self._name else self._real(product)

    def __enter__(self):
        env.state_dir = self._patched
        return self

    def __exit__(self, *exc):
        env.state_dir = self._real


def _record_diff(root, out):
    diff = subprocess.run(['git', 'diff', '--stat'], cwd=root, capture_output=True,
                          text=True).stdout.strip()
    out('== record')
    out(diff if diff else '(no change)')


def _wave_rows(product, root, out):
    """Plans the wave's rows — the same call :func:`asf.tick.step_wave.run` makes — and prints
    them, never building a brief or handing any of them to :mod:`asf.workers.wave`. Applies the
    feeder's invariant check point (its own ``INVARIANT …`` lines) first, same as a live tick."""
    from asf import invariants
    from asf import capacity as capacity_mod
    from asf.feeder import rows as feeder_rows
    from asf.record import plan_order
    from asf.tick import step_wave
    from asf.views import index_reader

    out('== wave')
    if not os.path.isfile(os.path.join(root, 'index.json')):
        out('(no record index — nothing planned)')
        return []
    wave_items, _generated = index_reader.load(root)
    if product.repo_dir:
        wave_items = plan_order.overlay(wave_items, plan_order.trunk_reader(product))
    running = step_wave.inflight(product)
    resolved = capacity_mod.resolve(product)
    inputs = step_wave.plan_inputs(product, root)
    planned = feeder_rows.plan_rows(wave_items, product, running, resolved.sessions, **inputs)
    planned = invariants.feeder_gate(product, planned, wave_items, out=out)
    if not planned:
        out('(nothing planned)')
        return planned
    host_held, host_why, _reading = step_wave.host_hold(planned)
    for row in planned:
        key = getattr(row, step_wave.KIND_JOB_KEY.get(row.brief_kind, ''), None)
        job = step_wave.job_name(row.brief_kind, row.item_id, key=key)
        if row.launches and host_held:
            out(f'waits        {job:<24} {row.item_id:<10} — held: {host_why}')
        elif row.launches:
            out(f'would launch {job:<24} {row.item_id:<10} {row.action}')
        else:
            out(f'waits        {job:<24} {row.item_id:<10} — {row.action}')
    if host_held:
        out(f'wave: held: {host_why} — no new session this tick; running sessions go on')
    return planned


def run(product, fresh=False, out=print):
    """``asf tick --dry-run``. Returns 0; a step's own failure is one line, same as a live tick —
    never worth losing the rest of the rehearsal over."""
    from asf.harvest import harvest as harvest_mod
    from asf.tick import step_wave
    from asf.tick.tick import Context, run_step0

    tmp, copy_path = _copy_state(product)
    try:
        with _StateDirOverride(product.name, copy_path):
            ctx = Context(product, fresh=fresh)
            try:
                root = ctx.record_root()
                run_step0(root, product, fresh=fresh)
            except Exception as e:  # noqa: BLE001 — one line, same as a live tick's record step
                detail = (str(e) or type(e).__name__).strip().splitlines()[0]
                out(f'tick --dry-run: record failed ({detail})')
                return 1
            _record_diff(root, out)

            out('== lane')
            step_wave.lane_pass(ctx, out)  # land_spec.adopt + the code lane, in-process (R2)

            _wave_rows(product, root, out)

            out('== harvest')
            if product.repo_dir:
                # the lane pass already ran (above) — decide only the gate's outcomes, the same
                # split the tick's detached harvest makes (asf.tick.step_harvest.background)
                items = harvest_mod.record_items(root) or {}
                harvest_mod.run_product_harvest(product, copy_path, dry_run=True, out=out,
                                                items=items, lane_pass=False)
            else:
                out('(no repo_dir — nothing to gate)')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0
