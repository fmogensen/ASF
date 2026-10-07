"""asf.groom.answers — the adjudicate session's answers, and the operator's own, carried in and
applied.

The session's deliverable is a ``groom/<date>.answers`` file. It may be left in its worktree
(:func:`carry_staged_answers` moves it to the state dir); the tick that first sees it applies it
(:func:`apply_pending_answers`), renamed ``.answers.done`` once applied.

``_ANSWERS_FILE_RE`` is an alias of :data:`asf.groom.groom.ANSWERS_FILE_RE` — the applier's own
shape (``<date>.answers`` and ``<date>.<half>.answers``) — so the scanner here and the applier
there agree on what a pending file looks like; two regexes for one file name is how a widened
shape went unseen by this module (F-0260 P16).
"""
import argparse
import os
import re

from asf.groom.groom import ANSWERS_FILE_RE as _ANSWERS_FILE_RE

#: A groom day's own file, in the checkout: ``<date>.md``, never the applier's ``.answers`` shape.
_GROOM_DATE_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.md$')
#: Any recognised line's answer slot, wherever the line opens — an id, ``inbox:<file>`` or a
#: conflicts pair all end the same way (``groom.ANSWER_LINE_RE``, ``INBOX_ANSWER_RE`` and
#: ``conflicts.conflict_lines`` each render it), so one suffix match reads every shape.
_ANSWER_SUFFIX_RE = re.compile(r'→\s*answer:\s*(?P<answer>.*)$')


def _ns(**kw):
    return argparse.Namespace(**kw)


def _strip_done_suffix(name):
    for suf in ('.done', '.superseded'):
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def pending_answers_files(product):
    """Every unapplied ``groom/<date>.answers`` (or ``<date>.<half>.answers``) in the state dir,
    as ``(path, date)`` pairs, oldest first."""
    from asf import env
    d = os.path.join(env.state_dir(product), 'groom')
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        m = _ANSWERS_FILE_RE.match(name)
        if m:
            out.append((os.path.join(d, name), m.group('date')))
    return out


def _newest_applied(product):
    """The newest date whose answers were applied (``<date>.answers.done``, ``<date>.<half>.
    answers.done``), or None. The date is the regex's own group, not a suffix slice: that slice
    gave ``2026-10-06.operator`` for a half-named file, comparing a date against a date-and-a-word
    (F-0260 P17)."""
    from asf import env
    d = os.path.join(env.state_dir(product), 'groom')
    if not os.path.isdir(d):
        return None
    dates = [m.group('date') for name in os.listdir(d) if name.endswith('.done')
             for m in [_ANSWERS_FILE_RE.match(_strip_done_suffix(name))] if m]
    return sorted(dates)[-1] if dates else None


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


def carry_checkout_answers(product, out=print):
    """The operator's own answer, written by hand into ``groom/<date>.md`` in the record
    checkout, copied into the state dir as ``<date>.operator.answers`` — where the tick's
    applier reads it (:func:`apply_pending_answers`), in the clone, and publishes the card from
    there.

    The `NEEDS OPERATOR` row tells the operator to answer in that file and says the next tick
    applies it (``asf.groom.inbox.stuck_s1_lines``). The tick's clone is reset to origin every
    run and reads this checkout for nothing but its ``origin`` url, so an uncommitted line in it
    reached no tick at all (F-0260). This is the one thing the tick reads out of that tree, and
    it reads it: nothing here writes, commits or pushes in the operator's checkout.

    A line is carried when its answer slot is filled and the checkout's own ``HEAD`` copy of the
    same file does not carry it verbatim (D2) — exactly the operator's uncommitted edit, with no
    fetch. Nothing is written when every such line is already in a file beside it, applied or
    not (D3). Returns the paths written."""
    from asf import env, gitops
    from asf.groom.groom import _CONFLICT_TOKEN_RE, _LINE_TOKEN_RE, unanswered
    from asf.tick.shadow import record_dir

    checkout = product.backlog_dir
    if not checkout or not os.path.isdir(os.path.join(checkout, '.git')):
        return []
    if os.path.realpath(checkout) == os.path.realpath(record_dir(product)):
        return []
    groom_dir = os.path.join(checkout, 'groom')
    if not os.path.isdir(groom_dir):
        return []

    state_groom_dir = os.path.join(env.state_dir(product), 'groom')
    carried = set()
    if os.path.isdir(state_groom_dir):
        for name in os.listdir(state_groom_dir):
            m = _ANSWERS_FILE_RE.match(_strip_done_suffix(name))
            if not m or m.group('half') != 'operator':
                continue
            with open(os.path.join(state_groom_dir, name), encoding='utf-8') as f:
                carried.update(f.read().splitlines())

    written = []
    for name in sorted(os.listdir(groom_dir)):
        m = _GROOM_DATE_FILE_RE.match(name)
        if not m:
            continue
        date = m.group(1)
        with open(os.path.join(groom_dir, name), encoding='utf-8') as f:
            wt_text = f.read()
        shown = gitops.git(['show', f'HEAD:groom/{name}'], checkout)
        head_text = shown.data if shown.ok else ''

        new_lines = []
        for line in wt_text.splitlines():
            if not (_CONFLICT_TOKEN_RE.match(line) or _LINE_TOKEN_RE.match(line)):
                continue
            am = _ANSWER_SUFFIX_RE.search(line)
            if not am or unanswered(am.group('answer')) or line in head_text or line in carried:
                continue
            new_lines.append(line)
        if not new_lines:
            continue

        os.makedirs(state_groom_dir, exist_ok=True)
        out_path = os.path.join(state_groom_dir, f'{date}.operator.answers')
        with open(out_path, 'a', encoding='utf-8') as f:
            f.write('\n'.join(new_lines) + '\n')
        n = len(new_lines)
        out(f"groom: carried {n} answered line{'' if n == 1 else 's'} from the record checkout"
            f' — groom/{name}')
        written.append(out_path)
    return written


def apply_pending_answers(product, root, event=None, out=print):
    """The adjudicate session's answers, and the operator's own hand-written ones, applied in the
    record clone by the tick that first sees them (F-0085 §2.6: "the next tick reads the answers
    file") rather than by the next day's ``groom --apply``. Only under ``approvals.groom: auto``.
    Returns how many files were applied; one line each."""
    from asf.groom import policy
    from asf.groom.groom import cmd_groom
    from asf.tick.step_daily import run_part
    if not policy.groom_auto(product):
        return 0
    carry_staged_answers(product, out=out)
    carry_checkout_answers(product, out=out)   # the operator's own, from the record checkout
    epic = (product.conventions or {}).get('default_bug_epic')
    n = 0
    for path, date in pending_answers_files(product):
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
