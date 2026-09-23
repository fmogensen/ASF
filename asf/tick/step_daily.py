"""asf.tick.step_daily — the tick's once-a-day step, in the record clone.

``groom --apply`` (yesterday's answers applied, the inbox filed, today's ``groom/<day>.md``),
``stale``, ``file-bugs``, ``rollup`` for yesterday with its releases, written into the record
clone — the tick's one commit (:func:`asf.tick.tick.finish`) carries them with the rest of the
tick. One line per part: ``daily: <part> ok|FAILED — <its last line>``. A part that fails does
not stop the others; the step fails (and the day is not stamped) when any did.
Whether it is due today is the tick's stamp (:func:`asf.tick.steps.daily_due`).
"""
import argparse
import contextlib
import datetime
import io
import os
import re


def _ns(**kw):
    return argparse.Namespace(**kw)


def yesterday(today=None):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=1)).isoformat()


_ANSWERS_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.answers$')


def _newest_answers_file(product):
    """The state dir's newest ``groom/<date>.answers`` (F-0085 §2.6, D4) — the adjudicate
    session's own answers, picked up by this tick's ``groom --apply``. ``None`` when there is
    none, or none still unapplied (an applied one is renamed ``.answers.done``, so it no longer
    matches)."""
    from asf import env
    d = os.path.join(env.state_dir(product), 'groom')
    if not os.path.isdir(d):
        return None
    dates = [m.group(1) for name in os.listdir(d)
             for m in [_ANSWERS_FILE_RE.match(name)] if m]
    if not dates:
        return None
    return os.path.join(d, f'{sorted(dates)[-1]}.answers')


def pending_answers_files(product):
    """Every unapplied ``groom/<date>.answers`` in the state dir, oldest first."""
    from asf import env
    d = os.path.join(env.state_dir(product), 'groom')
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, name) for name in sorted(os.listdir(d))
            if _ANSWERS_FILE_RE.match(name)]


def _newest_applied(product):
    """The newest date whose answers were applied (``<date>.answers.done``), or None."""
    from asf import env
    d = os.path.join(env.state_dir(product), 'groom')
    done = sorted(name[:-len('.answers.done')] for name in os.listdir(d)
                  if name.endswith('.answers.done')) if os.path.isdir(d) else []
    return done[-1] if done else None


def _same_file(a, b):
    try:
        with open(a, 'rb') as fa, open(b, 'rb') as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def carry_staged_answers(product, out=print):
    """An ended groom session's ``<date>.answers`` left in its worktree (its sandbox refused the
    state dir, groom-2026-09-22) is moved to the state dir, where the tick reads it. A live
    session's worktree is never touched; a file the state dir already holds, applied or not, is
    left where it is."""
    from asf import env
    from asf.workers import lifecycle, pool as pool_mod
    d = os.path.join(env.state_dir(product), 'groom')
    registry = pool_mod.sessions_path(product)
    owners = lifecycle.by_worktree(registry)
    moved = []
    for job, run in sorted(lifecycle.latest(registry).items()):
        if run.get('kind') not in lifecycle.NO_LANDING_KINDS or lifecycle.is_live(run):
            continue
        date = job.rsplit('groom-', 1)[-1]
        wt = run.get('worktree') or ''
        if lifecycle.is_live(owners.get(os.path.realpath(wt)) or {}):
            continue  # a session sent back into the same worktree is still at work there
        staged = os.path.join(wt, f'{date}.answers')
        if not _ANSWERS_FILE_RE.match(os.path.basename(staged)) or not os.path.isfile(staged):
            continue
        target = os.path.join(d, f'{date}.answers')
        if os.path.exists(target) or _same_file(staged, target + '.done'):
            continue  # a later session of the same day stages answers the applied file lacks
        os.makedirs(d, exist_ok=True)
        os.replace(staged, target)
        out(f'groom: carried {staged} to the state dir')
        moved.append(target)
    return moved


def apply_pending_answers(product, root, event=None, out=print):
    """The adjudicate session's answers, applied in the record clone by the tick that first
    sees them (F-0085 §2.6: "the next tick reads the answers file") rather than by the next
    day's ``groom --apply``. Only under ``approvals.groom: auto``. Returns how many files were
    applied; one line each."""
    from asf.groom import policy
    from asf.groom.groom import cmd_groom
    if not policy.groom_auto(product):
        return 0
    carry_staged_answers(product, out=out)
    epic = (product.conventions or {}).get('default_bug_epic')
    n = 0
    for path in pending_answers_files(product):
        date = os.path.basename(path)[:-len('.answers')]
        newer = _newest_applied(product)
        if newer and newer > date:
            # a later day's adjudicator ruled the same questions afresh: these must not land on
            # top of its answers
            os.replace(path, path + '.superseded')
            out(f'groom: answers {date} superseded by {newer} — not applied')
            continue
        rc, last = run_part(lambda: cmd_groom(_ns(date=None, apply=False, product=product.name,
                                                  default_bug_epic=epic, answers_file=path,
                                                  event=event), root))
        out(f"groom: answers {date} {'FAILED' if rc else 'applied'}" + (f' — {last}' if last else ''))
        if rc:
            break
        n += 1
    return n


def groom_every_tick(product, root, event=None, out=print):
    """The groom's intake and policy pass on every tick, not once a day: new and edited inbox
    cards are typed or asked, and the lines new since the last pass go into today's
    ``groom/<date>.md`` beside what it already holds (``cmd_groom`` ``incremental``). Only under
    ``approvals.groom: auto``; stale, file-bugs and the rollup stay the daily's. Says one line
    when it changed something, none when it did not. Returns the exit code."""
    from asf.groom import policy
    from asf.groom.groom import cmd_groom
    if not policy.groom_auto(product):
        return 0
    epic = (product.conventions or {}).get('default_bug_epic')
    rc, last = run_part(lambda: cmd_groom(_ns(date=None, apply=False, product=product.name,
                                              default_bug_epic=epic, answers_file=None,
                                              event=event, incremental=True), root))
    if rc or last:
        out(f"groom: tick {'FAILED' if rc else 'ok'}" + (f' — {last}' if last else ''))
    return rc


def parts(product, root, event=None):
    """``[(name, thunk)]`` in order; each thunk returns an exit code. ``event`` is ``ctx.event``
    (§4) — handed to ``groom`` alone, the only part that writes events today."""
    from asf import approvals
    from asf.groom.groom import cmd_groom
    from asf.metrics.metrics import cmd_rollup
    from asf.tick.file_bugs import cmd_file_bugs
    from asf.tick.stale import cmd_stale
    epic = (product.conventions or {}).get('default_bug_epic')
    answers_file = _newest_answers_file(product)
    file_bug_level = approvals.level_of(product, 'file_bug')
    return [
        ('groom', lambda: cmd_groom(_ns(date=None, apply=True, product=product.name,
                                        default_bug_epic=epic, answers_file=answers_file,
                                        event=event), root)),
        ('stale', lambda: cmd_stale(_ns(json=False), root)),
        ('file-bugs', lambda: cmd_file_bugs(_ns(default_bug_epic=epic,
                                                file_bug_level=file_bug_level), root)),
        ('rollup', lambda: cmd_rollup(_ns(day=yesterday(), no_releases=False,
                                          product=product.name), root)),
    ]


def run_part(thunk):
    """``(rc, last line of what it printed)``; an exception is rc 1 and its message."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = thunk() or 0
    except Exception as e:  # noqa: BLE001 — a part's failure never stops the other parts
        return 1, (str(e) or type(e).__name__).strip().splitlines()[0]
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return rc, (lines[-1] if lines else '')


def run(ctx, out=print):
    product = ctx.product
    root = ctx.record_root()
    failed = []
    for name, thunk in parts(product, root, event=ctx.event):
        rc, last = run_part(thunk)
        if rc:
            failed.append(name)
        out(f"daily: {name} {'FAILED' if rc else 'ok'}" + (f' — {last}' if last else ''))
    if failed:
        raise RuntimeError(f"daily parts failed: {', '.join(failed)}")
    return 0
