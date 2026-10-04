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
working tree, never committed here; ``step_wave.lane_pass(ctx, out, dry_run=True)`` threads this
call's own ``dry_run`` onto the :class:`asf.harvest.lane.Lane` it builds, which turns every PR
open, review wait, merge, archive and delete into one ``lane: DRY …``/``DRY: would …`` line
instead of the host or push call, and the ``ci_queue`` pass the lane's own ``lane_pass`` runs at
its end the same way; :func:`asf.harvest.harvest.run_product_harvest` is exactly what the live
harvest runs, on the same throwaway ``git worktree`` copy of the trunk its gate always builds —
the gate's own "dry runs in-process on copies") and nothing is launched (the wave's rows are
planned — :func:`asf.feeder.rows.plan_rows`, the same call :mod:`asf.tick.step_wave` makes — and
printed, never handed to :mod:`asf.workers.wave`).

Every one of those ``dry_run`` threads is also backstopped structurally
(:mod:`asf.mutation_guard`, on for this whole function): a git push
(:func:`asf.gitpush.push`) and a mutating ``gh`` call (:func:`asf.harvest.harvest._gh`,
:meth:`asf.ci_queue.GitHubSource.gh_try`, :func:`asf.metrics.metrics.gh`) refuse on their own
while it is on, whatever ``dry_run`` value the step that called them carried — so a step that
forgets to thread its own flag this far (2026-09-29: ``step_wave.lane_pass`` once built its
``Lane`` with ``dry_run`` hardcoded ``False``, and ``asf tick --dry-run`` deleted two
already-landed branches and let the ci-queue pass cancel four queued runs) still cannot write
anything real.

Prints, in order:

* ``== record`` — ``git diff --stat`` of the copy's record clone after step 0 (ingest et al.,
  never committed);
* ``== lane`` — :func:`asf.tick.step_wave.lane_pass`'s own lines: a spec/plan branch adopted
  (:func:`asf.tick.land_spec.adopt`) and one ``lane: …``/``waiting …``/``held …``/``DRY: would
  …`` per open code branch or PR (:class:`asf.harvest.lane.Lane`);
* ``== wave`` — one line per row the feeder would launch (``would launch``) or hold (``waits``),
  a ``would close`` line for a row (or a park) whose Task's work is verified on the trunk
  (:mod:`asf.workers.trunkclose`), and any ``INVARIANT …`` line the feeder check point logs
  (:func:`asf.invariants.feeder_gate`) — dropped rows never launch here either; under host
  pressure (:func:`asf.tick.step_wave.host_hold`) every launching row is a ``waits … — held:
  host pressure …`` line and the section ends on ``wave: held: …``, as a live tick's would;
* ``== groom rules`` — only under ``flags.groom_rules``: a ``would close``/``keeps`` line per
  duplicate the dedupe rules find and a ``would file a verify Task`` line per covered Feature
  (:func:`asf.groom.policy.apply_groom_rules`);
* ``== harvest`` — the gate's own lines: any ``held``/``waiting``/``landed`` outcome the real
  gate would reach for a branch the lane pass brought to GATE.

Two runs back to back, with the real state and the product's origins unchanged in between, print
the same thing: no step here writes a wall-clock timestamp into what it prints.

**Two venvs, one state** (``--state-copy <dir>``, ``--with-venv <venv>``). ``--state-copy``
names a frozen snapshot of the state directory: made there on first use (the caller removes it),
read — never written — by every run that names it; each run still works on its own private copy
of it. ``--with-venv`` runs the rehearsal as a subprocess of that venv's own interpreter (no
``PYTHONPATH``, cwd ``/``) against the snapshot: the child gets a throwaway ``ASF_HOME`` whose
entries link to the real ones, except ``state/<product>``, which links to the snapshot — so any
release's ``asf tick --dry-run``, one that predates both flags included, rehearses the same
state as its pair (the ``ab-dry-run.sh`` tool: the pinned venv and a candidate, side by side).
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile

from asf import env


#: Left out of the copy: the worker worktrees are full product checkouts (tens of GB, with
#: symlinked dependency trees) that no dry-run step reads; the copy gets an empty dir in their
#: place, so nothing it runs can reach a live session's checkout either. ``trunk-merge`` (a
#: 30 GB merge-queue checkout) and ``turbo-cache`` are the same: heavy caches, never read.
_NOT_COPIED = ('worktrees', 'trunk-merge', 'turbo-cache')

#: A copy never leaves less than this free on the target filesystem.
MIN_FREE_BYTES = 10 * 1024 ** 3


class CopyRefused(RuntimeError):
    """The copy would not fit with :data:`MIN_FREE_BYTES` to spare."""


def _tree_size(path, skip=_NOT_COPIED):
    """Bytes the copy of ``path`` would occupy: allocated blocks, symlinks counted not followed,
    the top-level ``skip`` names left out."""
    total = 0
    stack = [(path, True)]
    while stack:
        d, top = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if top and e.name in skip:
                continue
            try:
                st = e.stat(follow_symlinks=False)
                total += getattr(st, 'st_blocks', 0) * 512 or st.st_size
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, False))
            except OSError:
                continue
    return total


def _check_space(src, dest_parent, min_free=None):
    need = _tree_size(src)
    free = shutil.disk_usage(dest_parent).free
    floor = MIN_FREE_BYTES if min_free is None else min_free
    if free - need < floor:
        gb = 1024 ** 3
        raise CopyRefused(
            f'state copy refused: {src} needs {need / gb:.1f} GB, {dest_parent} has '
            f'{free / gb:.1f} GB free; the copy would leave under {floor / gb:.0f} GB free')
    return need


class _TermAsExit:
    """SIGTERM raises SystemExit for the life of this object (main thread only), so the
    ``finally`` that removes a temp dir runs on a kill as it does on Ctrl-C."""

    def __enter__(self):
        self._old = None
        try:
            self._old = signal.signal(signal.SIGTERM, lambda _n, _f: sys.exit(143))
        except ValueError:  # not the main thread
            pass
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            signal.signal(signal.SIGTERM, self._old)


def _copy_tree(src, dest):
    _check_space(src, os.path.dirname(os.path.abspath(dest)))
    shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(*_NOT_COPIED))
    for name in _NOT_COPIED:
        os.makedirs(os.path.join(dest, name), exist_ok=True)


def _copy_state(product, source=None):
    """A throwaway copy of ``product``'s state directory (or of the snapshot ``source``), under
    a fresh temp dir — all of it but :data:`_NOT_COPIED`, symlinks copied as links, never
    followed. Returns ``(tmp, copy_path)``; the caller removes ``tmp`` when done, and a copy that
    fails removes it here. Never mutates ``real``."""
    real = source or env.state_dir(product)  # makes it if missing; read-only below
    tmp = tempfile.mkdtemp(prefix='asf-dry-run-')
    copy_path = os.path.join(tmp, 'state')
    try:
        with _TermAsExit():
            _copy_tree(real, copy_path)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, copy_path


def snapshot(product, dest):
    """``dest`` as the frozen state snapshot two runs share: copied from the real state
    directory when ``dest`` is absent or empty, else taken as it is. Returns ``dest``."""
    dest = os.path.abspath(dest)
    if os.path.isdir(dest) and os.listdir(dest):
        return dest
    if os.path.isdir(dest):
        os.rmdir(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _copy_tree(env.state_dir(product), dest)
    return dest


def shadow_home(product_name, snap, base):
    """A throwaway ``ASF_HOME`` under ``base``: every entry of the real one linked, ``state/``
    a dir of links to the real per-product state dirs except ``state/<product>``, which links
    to ``snap``. Returns its path."""
    real = os.path.abspath(env.ASF_HOME)
    home = os.path.join(base, 'home')
    os.makedirs(os.path.join(home, 'state'))
    for entry in sorted(os.listdir(real)) if os.path.isdir(real) else []:
        if entry != 'state':
            os.symlink(os.path.join(real, entry), os.path.join(home, entry))
    real_state = os.path.join(real, 'state')
    for entry in sorted(os.listdir(real_state)) if os.path.isdir(real_state) else []:
        if entry != product_name:
            os.symlink(os.path.join(real_state, entry), os.path.join(home, 'state', entry))
    os.symlink(snap, os.path.join(home, 'state', product_name))
    return home


def run_with_venv(product, venv, fresh=False, state_copy=None, run=subprocess.run, err=None):
    """``asf tick --dry-run --with-venv <venv>``: the rehearsal run by ``venv``'s interpreter
    against the snapshot ``state_copy`` (made there when absent; a throwaway one when not
    given). Returns the child's exit status; its output is the child's own."""
    err = err or (lambda line: print(line, file=sys.stderr))
    venv = os.path.abspath(os.path.expanduser(venv))
    python = os.path.join(venv, 'bin', 'python')
    if not os.path.exists(python):
        err(f'tick --dry-run --with-venv: {python} is not on disk')
        return 2
    base = tempfile.mkdtemp(prefix='asf-dry-run-venv-')
    try:
        with _TermAsExit():
            snap = snapshot(product, state_copy or os.path.join(base, 'snapshot'))
        home = shadow_home(product.name, snap, base)
        child = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
        child['ASF_HOME'] = home
        argv = [python, '-m', 'asf.cli', 'tick', '--dry-run', '--product', product.name]
        if fresh:
            argv.append('--fresh')
        err(f'tick --dry-run --with-venv {venv}: state {snap}')
        return run(argv, env=child, cwd='/').returncode
    finally:
        shutil.rmtree(base, ignore_errors=True)


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
    from asf import capacity as capacity_mod
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
    planned, _dropped = step_wave.gated_plan(wave_items, product, running, resolved.sessions,
                                             inputs, out=out)
    if not planned:
        out('(nothing planned)')
        return planned
    from asf.workers import trunkclose
    trunkclose.close_parked(product, out=out, dry_run=True)
    host_held, host_why, _reading = step_wave.host_hold(planned)
    for row in planned:
        key = getattr(row, step_wave.KIND_JOB_KEY.get(row.brief_kind, ''), None)
        job = step_wave.job_name(row.brief_kind, row.item_id, key=key)
        if row.launches and trunkclose.closes_before_launch(product, row.brief_kind,
                                                            row.item_id, out, dry_run=True):
            continue
        if row.launches and host_held:
            out(f'waits        {job:<24} {row.item_id:<10} — held: {host_why}')
        elif row.launches:
            out(f'would launch {job:<24} {row.item_id:<10} {row.action}')
        else:
            out(f'waits        {job:<24} {row.item_id:<10} — {row.action}')
    if host_held:
        out(f'wave: held: {host_why} — no new session this tick; running sessions go on')
    return planned


def run(product, fresh=False, out=print, state_copy=None):
    """``asf tick --dry-run``. Returns 0; a step's own failure is one line, same as a live tick —
    never worth losing the rest of the rehearsal over.

    :func:`asf.mutation_guard.active` is on for the whole rehearsal: every ``git push`` and every
    mutating ``gh`` call — however deep the call, and whether or not the step that made it
    remembered its own ``dry_run`` flag — refuses instead of running, the backstop behind each
    step's own ``dry_run`` threading (2026-09-29 plan §6 regression: a missed thread here deleted
    two already-landed branches and let the ci-queue pass cancel four queued runs)."""
    from asf import mutation_guard
    from asf.harvest import harvest as harvest_mod
    from asf.tick import step_wave
    from asf.tick.tick import Context, run_step0

    tmp, copy_path = _copy_state(product, snapshot(product, state_copy) if state_copy else None)
    try:
        with mutation_guard.active(), _StateDirOverride(product.name, copy_path):
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
            step_wave.lane_pass(ctx, out, dry_run=True)  # land_spec.adopt + the code lane (R2)

            _wave_rows(product, root, out)

            from asf.groom import policy as groom_policy
            if groom_policy.groom_rules(product):   # flags.groom_rules unset: no section at all
                out('== groom rules')
                groom_policy.apply_groom_rules(product, root, out=out, dry_run=True)

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
