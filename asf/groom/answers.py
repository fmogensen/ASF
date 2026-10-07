"""asf.groom.answers — the adjudicate session's answers, and the operator's own, carried in and
applied.

The session's deliverable is a ``groom/<date>.answers`` file. It may be left in its worktree
(:func:`carry_staged_answers` moves it to the state dir); the operator's answer, written by hand
into ``groom/<date>.md`` in the record checkout, is copied out as ``<date>.operator.answers``
(:func:`carry_checkout_answers`, F-0260); the tick that first sees either applies it
(:func:`apply_pending_answers`), renamed ``.answers.done`` once applied.

The answers file's name has one pattern, :data:`asf.groom.groom.ANSWERS_FILE_RE`
(``<date>.answers`` and ``<date>.<who>.answers``): the scanner here and the applier there read
the same names, or a file is appliable and invisible (F-0260 P16).
"""
import argparse
import os
import re

from asf.groom.groom import ANSWERS_FILE_RE


def _ns(**kw):
    return argparse.Namespace(**kw)


#: The applier's own shape — one regex for the scanner and the applier.
_ANSWERS_FILE_RE = ANSWERS_FILE_RE

#: Who the operator's own answers file is named for: ``<date>.operator.answers``.
OPERATOR = 'operator'

#: A groom line's filled-or-not answer slot.
_ANSWER_SLOT_RE = re.compile(r'→\s*answer:\s*(?P<answer>.*)$')
#: A groom day's own file in the record: ``groom/<date>.md``.
_GROOM_FILE_RE = re.compile(r'^(?P<date>\d{4}-\d{2}-\d{2})\.md$')


def _groom_state_dir(product):
    from asf import env
    return os.path.join(env.state_dir(product), 'groom')


def pending_answers_files(product):
    """Every unapplied answers file in the state dir, oldest first, as ``(path, date)`` pairs —
    the shape the applier reads (F-0260 P16): ``<date>.answers`` and ``<date>.<who>.answers``
    (``2026-10-06.operator.answers`` among them), ``date`` the bare date of its name."""
    d = _groom_state_dir(product)
    if not os.path.isdir(d):
        return []
    return [(os.path.join(d, name), m.group('date')) for name in sorted(os.listdir(d))
            for m in [_ANSWERS_FILE_RE.match(name)] if m]


def _newest_applied(product):
    """The newest date whose answers were applied (``<date>[.<who>].answers.done``), or None —
    the bare date, never the date and a word (F-0260 P17)."""
    d = _groom_state_dir(product)
    if not os.path.isdir(d):
        return None
    done = sorted(m.group('date') for name in os.listdir(d) if name.endswith('.done')
                  for m in [_ANSWERS_FILE_RE.match(name[:-len('.done')])] if m)
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


def _checkout_head_text(repo, rel):
    """``HEAD:<rel>`` in the operator's checkout, '' when ``HEAD`` lacks it — read, never written."""
    from asf import gitops
    r = gitops.git(['show', f'HEAD:{rel}'], repo)
    return r.stdout if r.ok else ''


def _filled_lines(text):
    """The groom lines of ``text`` whose answer slot is filled — each whole, right-stripped."""
    from asf.groom.groom import _LINE_TOKEN_RE, unanswered
    out = []
    for raw in (text or '').splitlines():
        line = raw.rstrip()
        m = _ANSWER_SLOT_RE.search(line)
        if m and _LINE_TOKEN_RE.match(line) and not unanswered(m.group('answer')):
            out.append(line)
    return out


def _already_carried(state, stem):
    """Every line in a ``<stem>*`` file of the state dir — applied, pending or superseded (D3)."""
    out = set()
    if not os.path.isdir(state):
        return out
    for seen in os.listdir(state):
        if seen.startswith(stem):
            try:
                with open(os.path.join(state, seen), encoding='utf-8') as f:
                    out.update(line.rstrip() for line in f)
            except OSError:
                pass
    return out


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
    not (D3): the edit stays in the operator's tree after it lands, and is carried once. Returns
    the paths written."""
    from asf.tick import shadow
    repo = product.backlog_dir
    if not repo or not os.path.isdir(os.path.join(repo, '.git')):
        return []
    if os.path.realpath(repo) == os.path.realpath(shadow.record_dir(product)):
        return []
    groom_dir = os.path.join(repo, 'groom')
    if not os.path.isdir(groom_dir):
        return []
    state = _groom_state_dir(product)
    written = []
    for name in sorted(os.listdir(groom_dir)):
        m = _GROOM_FILE_RE.match(name)
        if not m:
            continue
        date, rel = m.group('date'), f'groom/{name}'
        try:
            with open(os.path.join(groom_dir, name), encoding='utf-8') as f:
                lines = _filled_lines(f.read())
        except OSError:
            continue
        if not lines:
            continue
        head = set(_filled_lines(_checkout_head_text(repo, rel)))
        stem = f'{date}.{OPERATOR}.answers'
        carried = _already_carried(state, stem)
        lines = [line for line in lines if line not in head and line not in carried]
        if not lines:
            continue
        os.makedirs(state, exist_ok=True)
        target = os.path.join(state, stem)
        with open(target, 'a', encoding='utf-8') as f:  # a pending one gains only the new lines
            f.write(''.join(line + '\n' for line in lines))
        out(f"groom: carried {len(lines)} answered line{'s' if len(lines) != 1 else ''} from "
            f"the record checkout — {rel}")
        written.append(target)
    return written


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
