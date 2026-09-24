"""asf.groom.answers — the adjudicate session's answers, carried in and applied.

The session's deliverable is a ``groom/<date>.answers`` file. It may be left in its worktree
(:func:`carry_staged_answers` moves it to the state dir); the tick that first sees it applies it
(:func:`apply_pending_answers`), renamed ``.answers.done`` once applied.
"""
import argparse
import os
import re


def _ns(**kw):
    return argparse.Namespace(**kw)


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
        if lifecycle.is_live(owners.get(lifecycle.path_key(wt)) or {}):
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
    from asf.tick.step_daily import run_part
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
