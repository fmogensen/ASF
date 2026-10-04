"""asf.harvest.mechanical — the causes code settles before any correction is written.

A correction buys an LLM session. Some causes a session is handed are not judgement at all: git
decides whether a branch rebases onto the trunk, and the ``widen_footprint`` rule
(:mod:`asf.feeder.widen`) decides whether a footprint grows. This module is the one table of
those causes, applied by the lane (:mod:`asf.harvest.lane`) *before* it writes a hold:

* ``conflict`` / ``rebase conflict`` → :meth:`asf.harvest.lane.Lane.rebase_onto_trunk` — the
  branch's own commits picked onto a fresh trunk, the product's pre-push check run on the result,
  ONE push over a lease. Resolved when it pushed; a real textual conflict (its files named) or a
  red check is the residue a session gets, with what the lane tried in its text.
* ``footprint`` → :func:`asf.feeder.widen.widen` — the rule's verdict on the paths a red gate
  needs outside ``writes:``. A widening never turns a red head green: the lane only names the
  verdict in the hold (the health step's ``widen_footprint`` writes the record), so the cause is
  always a residue here. Pushing the same red head on as PUSHED would re-gate it red each pass.
* ``unpushed`` (W2-PR3b) — a run that ended ``not pushed`` with commits in its worktree:
  :func:`publish_worktree`, called by the health step's publish (:func:`asf.workers.health.
  publish_gap`) and by the table for the lane. Its two refusals become git's: origin's commits
  the head lacks that are ONLY review or notes rounds (``reviews_dir``/``reports_dir``,
  :func:`round_file`) are archived and the head published over them under the lease; a rebase
  onto ``origin/<branch>`` that conflicts because origin was rewritten under the worktree (a
  lane rebuild, a transplant) is replayed from the fork point — only the session's own commits
  onto the rewritten tip (:func:`replay_own`) — and published. A run whose report declared
  ``pushed: rebased <its head>`` over the very tip its worktree held (a correct/transplant run
  that cut the branch fresh: :func:`declared_transplant`) is published over that tip, archived.
  Anything else origin holds that the push would lose still refuses, as today; no commits is
  the ``empty branch`` path.

Every entry is a function ``(lane, f, cause) -> Outcome`` over what the lane already does, never
a new git path. :func:`apply` runs one, only under ``conventions.flags.mechanical: on`` (default
off — today's rows), writes the event on the run (``mechanical: {event, kind, head, resolved}``;
a resolved one also on the lane record, reason ``mechanical:<kind>``) and says one line. The
gate still runs on the rebuilt head: a resolved cause goes back to PUSHED, never past it.
"""
import dataclasses
import os

from asf import gitpush, refguard
from asf.feeder import footprint, widen
from asf.harvest import harvest as H
from asf.workers import lifecycle, report
from asf.workers.pool import now_iso

#: ``conventions.flags.<FLAG>`` turns the table on; anything but an "on" word is off.
FLAG = 'mechanical'
ON_WORDS = ('on', 'true', 'yes', '1')
#: The event name written on the run (and on the lane record of a resolved cause).
EVENT = 'mechanical'
#: The lane state a resolved cause leaves the branch in: the gate runs again on its new head.
PUSHED = 'PUSHED'
#: A resolved cause clears a pending correction of these kinds — the work they asked for is done.
CLEARS = ('conflict', lifecycle.REBASE_CONFLICT, lifecycle.NAMING, lifecycle.COPIES, 'merge',
          lifecycle.UNPUSHED)


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What one table entry did: the cause ``kind``, whether it is ``resolved`` (no correction),
    the branch ``head`` after it, and ``why`` — for a residue, the sentence the correction
    carries (what the lane tried and where it stopped). ``files``: the files a residue names."""
    kind: str
    resolved: bool
    head: str = ''
    why: str = ''
    files: tuple = ()


def enabled(product):
    """``conventions.flags.mechanical`` is on (``on``/``true``/``yes``); default off."""
    if product is None:
        return False
    flag = getattr(product, 'flag', None)
    if flag is None:
        conv = getattr(product, 'conventions', None)
        flag = getattr(conv, 'flag', None)
    value = flag(FLAG, 'off') if flag is not None else 'off'
    return value is True or str(value).strip().lower() in ON_WORDS


# ---- the entries --------------------------------------------------------------------------------

def rebase(lane, f, cause):
    """``conflict`` / ``rebase conflict``: the lane rebases the branch onto the trunk itself
    (:meth:`asf.harvest.lane.Lane.rebase_onto_trunk`). Resolved when it pushed the rebuilt head."""
    kind = cause.get('kind') or 'conflict'
    got = lane.rebase_onto_trunk(f) or {}
    if got.get('pushed'):
        return Outcome(kind, True, got['pushed'], f'rebased onto origin/{lane.trunk}')
    head = f.get('head') or ''
    if got.get('conflict'):
        sha, files = got['conflict']
        return Outcome(kind, False, head,
                       f'The lane tried the rebase itself and git stops at {sha[:9]}: '
                       f'conflicts in {", ".join(files) or "?"} — resolve exactly those files',
                       tuple(files or ()))
    if got.get('check'):
        return Outcome(kind, False, head,
                       f'The lane rebased it onto origin/{lane.trunk} cleanly, but the '
                       f'product\'s pre-push check fails on the result (nothing was pushed): '
                       f'{got["check"]}\nRebase, fix what the check names, run it, then push')
    return Outcome(kind, False, head, '')  # the lane does not rebase it: today's round


def widen_footprint(lane, f, cause):
    """``footprint``: the ``widen_footprint`` verdict (:func:`asf.feeder.widen.widen`) on the
    paths a red gate needs outside ``writes:``, read off the lane's items. Never resolved: the
    widening is the record's (the health step writes it), and the red is a session's to make
    green — the hold goes on, its text naming the verdict."""
    item = f.get('item')
    needs = [p for p in cause.get('needs') or () if p]
    head = f.get('head') or ''
    if not item or not needs:
        return Outcome('footprint', False, head, '')
    product = getattr(lane, 'product', None)
    v = widen.widen(getattr(lane, 'items', None) or {}, item, needs, widen.max_files(product),
                    widened_before=lifecycle.widenings(getattr(lane, 'path', None), item),
                    shared=footprint.shared_globs(product))
    detail = f' ({v.detail})' if v.detail else ''
    return Outcome('footprint', False, head,
                   f'mechanical: the widening rule says {v.kind} +{" ".join(v.paths)}{detail}; '
                   f'a wider writes: alone leaves this head red — make the failing tests pass')


def round_file(conv):
    """``path -> bool``: True for a file under the product's ``reviews_dir`` or ``reports_dir`` —
    what a review or notes round commits. A commit touching only those carries no code: the
    factory files it in its own store (:mod:`asf.evidence.review_store`), so dropping it from a
    branch, its tip archived, loses nothing a push must keep."""
    dirs = []
    for key in ('reviews_dir', 'reports_dir'):
        d = getattr(conv, key, None)
        if d is None and hasattr(conv, 'get'):
            d = conv.get(key)
        d = str(d or '').strip().rstrip('/')
        if d:
            dirs.append(d)

    def droppable(path):
        return any(path == d or path.startswith(d + '/') for d in dirs)
    return droppable


def _git(wt, *args):
    return lifecycle._git(list(args), wt)


def replay_own(wt, branch):
    """``(ok, line)``: replay the worktree's OWN commits onto a rewritten ``origin/<branch>``.

    A rebase onto ``origin/<branch>`` conflicts when origin was rebuilt under the worktree (the
    lane's rebase onto the trunk, a transplant, a session's resolution): git replays the old
    tip's commits over their own rewritten copies. The fork point — the newest tip of
    ``origin/<branch>`` the repo's reflog saw that HEAD descends from — splits HEAD into the old
    tip and the session's own commits; only those are replayed (``git rebase --onto``). Not a
    rewrite (the fork point is the plain merge base), a dirty tree, or a conflict: ``(False,
    line)`` with the worktree exactly as it was. Never a merge, never a push."""
    st = _git(wt, 'status', '--porcelain')
    if st.returncode != 0 or st.stdout.strip():
        return False, ''
    tracking = f'refs/remotes/origin/{branch}'
    if _git(wt, 'fetch', '-q', 'origin', f'+refs/heads/{branch}:{tracking}').returncode != 0:
        return False, ''
    head = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
    tip = _git(wt, 'rev-parse', tracking).stdout.strip()
    fork = _git(wt, 'merge-base', '--fork-point', tracking, 'HEAD').stdout.strip()
    base = _git(wt, 'merge-base', tracking, 'HEAD').stdout.strip()
    if not head or not tip or not fork or fork == base:
        return False, ''
    own = lifecycle._count(_git(wt, 'rev-list', '--count', f'{fork}..HEAD'))
    r = _git(wt, 'rebase', '-q', '--onto', tracking, fork)
    if r.returncode != 0:
        files = _git(wt, 'diff', '--name-only', '--diff-filter=U').stdout.split()
        _git(wt, 'rebase', '--abort')
        _git(wt, 'reset', '-q', '--hard', head)
        return False, (f'the session\'s own {own} commit(s) past {fork[:9]} conflict with the '
                       f'rewritten origin/{branch} in: {", ".join(files) or "?"}')
    return True, (f'replayed {own} own commit(s) past {fork[:9]} onto the rewritten '
                  f'origin/{branch} ({tip[:9]})')


def declared_transplant(wt, branch, remote_sha, declared, seen):
    """True when the factory may publish ``wt``'s HEAD over ``remote_sha`` as a transplant: the
    run's report declared ``pushed: rebased <sha>`` (``declared``, :func:`asf.workers.report.
    rebased`) and that sha is this HEAD, and origin's tip is ``seen`` — the tip the worktree's
    own ``origin/<branch>`` held before the publish, so the session replaced what it saw and
    nothing pushed after it looked is dropped (the lease then guards origin moving since)."""
    declared = (declared or '').strip().lower()
    if len(declared) < 7 or not remote_sha or seen != remote_sha:
        return False
    head = _git(wt, 'rev-parse', 'HEAD').stdout.strip().lower()
    return bool(head) and head.startswith(declared)


def publish_worktree(product, wt, branch, remote_sha, main='main', protected=None,
                     push_timeout_s=None, declared=''):
    """``(ok, line, outcome)``: :func:`asf.workers.lifecycle.publish` of ``wt``'s HEAD with the
    table's three answers to its refusals — review/notes rounds dropped (``droppable``), a
    rewritten origin replayed onto (:func:`replay_own`, then published again; a refusal there
    leaves the worktree as it was), and a declared transplant published over the tip it replaces
    (:func:`declared_transplant`: ``declared`` is the sha the run's report said it rebased to —
    the brief's ``pushed: rebased <sha> — the factory publishes``; the old tip is archived).
    ``outcome``: the :class:`Outcome` (kind ``unpushed``)."""
    droppable = round_file(getattr(product, 'conventions', None))
    seen = _git(wt, 'rev-parse', '-q', '--verify',
                f'refs/remotes/origin/{branch}^{{commit}}').stdout.strip()
    ok, line = lifecycle.publish(wt, branch, remote_sha, main=main, protected=protected,
                                 push_timeout_s=push_timeout_s, droppable=droppable)
    if not ok and lifecycle.rebase_conflict(line):
        head = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
        replayed, how = replay_own(wt, branch)
        if replayed:
            tip = _git(wt, 'rev-parse', f'refs/remotes/origin/{branch}').stdout.strip()
            ok2, line2 = lifecycle.publish(wt, branch, tip, main=main, protected=protected,
                                           push_timeout_s=push_timeout_s, droppable=droppable)
            if ok2:
                ok, line = True, f'{how}: {line2}'
            else:
                _git(wt, 'reset', '-q', '--hard', head)
                line = f'{line} (the factory {how}, and that refused too: {line2})'
        elif how:
            line = f'{line} ({how})'
    if not ok and (lifecycle.stale_head(line) or lifecycle.rebase_conflict(line)) \
            and declared_transplant(wt, branch, remote_sha, declared, seen):
        ok2, line2 = lifecycle.publish(wt, branch, remote_sha, main=main, protected=protected,
                                       push_timeout_s=push_timeout_s, transplant=True)
        if ok2:
            ok, line = True, f'the run declared a transplant: {line2}'
        else:
            line = f'{line} (published as the declared transplant, that refused too: {line2})'
    new = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
    return ok, line, Outcome(UNPUSHED, bool(ok), new, line)


def unpushed(lane, f, cause):
    """``unpushed``: the run's worktree published (:func:`publish_worktree`) — resolved when it
    went out. No worktree, or none on disk: a residue (the hold as today)."""
    run = f.get('run') or {}
    wt, b = run.get('worktree'), f.get('branch') or run.get('branch')
    head = f.get('head') or ''
    if not wt or not b or not os.path.isdir(wt):
        return Outcome(UNPUSHED, False, head, '')
    product = getattr(lane, 'product', None)
    conv = getattr(product, 'conventions', None)
    _ok, _line, out = publish_worktree(
        product, wt, b, lifecycle.RemoteHeads().sha(wt, b) or '',
        main=getattr(lane, 'trunk', None) or 'main',
        protected=refguard.listed(conv) if conv is not None else None,
        push_timeout_s=gitpush.push_timeout(conv),
        declared=report.rebased(str((lifecycle.result_of(run) or {}).get('result') or '')))
    return out


#: The cause kind of a run that ended ``not pushed`` with its work in the worktree.
UNPUSHED = lifecycle.UNPUSHED

#: The table: cause kind → entry. 3c adds ``copies``, ``hook refused``, ``naming``.
MECHANICAL = {
    'conflict': rebase,
    lifecycle.REBASE_CONFLICT: rebase,
    lifecycle.FOOTPRINT: widen_footprint,
    lifecycle.UNPUSHED: unpushed,
}


# ---- applying one ------------------------------------------------------------------------------

def handles(product, kind):
    """True when the lane should try ``kind`` here before writing its hold."""
    return kind in MECHANICAL and enabled(product)


def event(out, at=None):
    """The ``mechanical`` event of ``out`` as written on the run."""
    return {'event': EVENT, 'kind': out.kind, 'head': out.head, 'resolved': out.resolved,
            'at': at or now_iso()}


def apply(lane, f, cause):
    """Run the table's entry for ``cause['kind']`` on ``f``'s branch, or None when the flag is
    off, the kind is not in the table, or the pass is a dry run. Resolved: the branch is set
    PUSHED with reason ``mechanical:<kind>`` (the event on the lane record), a correction it
    answers is cleared, and ``lane.results`` says ``'mechanical'``. Either way the event is on
    the run and one ``mechanical:<kind>`` line is said."""
    kind = (cause or {}).get('kind')
    entry = MECHANICAL.get(kind)
    if entry is None or not enabled(getattr(lane, 'product', None)) \
            or getattr(lane, 'dry_run', False):
        return None
    out = entry(lane, f, cause)
    b = f.get('branch')
    run = f.get('run') or {}
    if out.resolved:
        if f.get('run') is None:
            lane.write(f, lane.record(f, PUSHED, 'adopted'))
            run = f.get('run') or {}
        lane.set(f, PUSHED, f'{EVENT}:{kind}', event=EVENT, mechanical=kind)
        corr = f.get('correction') or {}
        fields = {'mechanical': event(out)}
        if corr.get('kind') in CLEARS:
            fields['correction'] = None
            f['correction'] = None
        H.mark_session(lane.state_dir, run.get('job') or b, **fields)
        lane.results[b] = 'mechanical'
        lane.out(f'{EVENT}:{kind} {b}: resolved — {out.why} '
                 f'({out.head[:9]}, no session; the gate runs on the new head)')
    else:
        if run.get('job') or b:
            H.mark_session(lane.state_dir, run.get('job') or b, mechanical=event(out))
        lane.out(f'{EVENT}:{kind} {b}: residue — '
                 + (out.why.split('\n', 1)[0] if out.why else 'not the lane\'s to settle')
                 + ' — a session gets it')
    return out
