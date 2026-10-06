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

* ``copies`` / ``merge`` (W2-PR3c) → the same rebuild on the trunk as ``conflict``: trunk
  history under the branch (copies of trunk commits, a merge of the trunk) dropped, its own
  commits picked onto a fresh trunk. The lane's own :meth:`asf.harvest.lane.Lane.drop_copies`
  tries it first on every pass; the table is what retries a copies hold once the trunk moved.
* ``naming`` (W2-PR3c) → :meth:`asf.harvest.lane.Lane.repair_naming`; a branch it cannot reword
  because it is not a straight line on the trunk is rebuilt on the trunk first, then reworded.
* ``hook refused`` (W2-PR3c) — the f-0086 loop: a session's worktree that merged the trunk in
  carries trunk history, so the publish's lane-N rewrite (a plain chain only) cannot run and the
  repo's hook refuses every run. :func:`publish_worktree` drops that history
  (:func:`drop_trunk_history`: the own commits rebased onto ``origin/<main>``) and publishes
  again; a finding of the session's own still refuses, the worktree as it was.

A pending correction of a :data:`RETRY` kind is tried again by :func:`retry_pending` on a later
pass — a new head, or the trunk given :data:`RETRY_AFTER` to move — before any session or an
adjudication (``at_cap``) takes it: the review/adjudicate rows a mechanical cause alone raised.
A resolved move records the head it left (``mechanical_from`` on the lane record); an approval
current on that head is carried to the new one (:func:`carry_review`), so a clean rebuild buys no
fresh review round — the gate still runs on the new head.

Every entry is a function ``(lane, f, cause) -> Outcome`` over what the lane already does, never
a new git path. :func:`apply` runs one, only under ``conventions.flags.mechanical: on`` (default
off — today's rows), writes the event on the run (``mechanical: {event, kind, head, resolved}``;
a resolved one also on the lane record, reason ``mechanical:<kind>``) and says one line. The
gate still runs on the rebuilt head: a resolved cause goes back to PUSHED, never past it.
"""
import dataclasses
import datetime
import os
import re

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
          lifecycle.UNPUSHED, lifecycle.HOOK_REFUSED)
#: The pending corrections :func:`retry_pending` tries again by code on a later pass: the ones a
#: rebuild on a moved trunk may settle with no session (W2-PR3c).
RETRY = ('conflict', lifecycle.REBASE_CONFLICT, lifecycle.COPIES, 'merge')
#: How long an unresolved try on one head waits before the table tries it again: the trunk has
#: moved by then, or it has not and the try costs a rebase and nothing else.
RETRY_AFTER = datetime.timedelta(hours=1)


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
    #: nothing judged yet: the pre-push check on the rebuilt head runs in the background
    #: (``lane.rebuild_check``) — the branch waits for a later pass, never a session round
    deferred: bool = False


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
    if got.get('deferred'):
        return Outcome(kind, False, head, got['deferred'], deferred=True)
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


def naming(lane, f, cause):
    """``naming``: the lane rewords the subjects itself (:meth:`asf.harvest.lane.Lane.
    repair_naming`, trees identical). A branch it cannot reword because it is not its own
    straight line on the trunk (trunk history under it) is rebuilt on the trunk first
    (:meth:`asf.harvest.lane.Lane.rebase_onto_trunk`) and reworded on the rebuilt head."""
    head = f.get('head') or ''
    rec = lane.repair_naming(f)
    if isinstance(rec, dict):
        return Outcome(lifecycle.NAMING, True, f.get('head') or head, 'reworded the subjects')
    if rec is not None:  # DEFERRED: a later pass, never a session
        return Outcome(lifecycle.NAMING, False, head, '')
    got = lane.rebase_onto_trunk(f) or {}
    if not got.get('pushed'):
        return Outcome(lifecycle.NAMING, False, head, '')
    f.pop('naming_repair', None)
    still = (f.get('refusal') or (None,))[0] == lifecycle.NAMING
    rec = lane.repair_naming(f) if still else None
    if still and not isinstance(rec, dict):  # rebuilt, the subjects still not reworded
        return Outcome(lifecycle.NAMING, False, f.get('head') or got['pushed'],
                       f'The lane rebuilt the branch on origin/{lane.trunk} and still cannot '
                       f'reword its subjects — name the item in each subject')
    return Outcome(lifecycle.NAMING, True, f.get('head') or got['pushed'],
                   f'rebuilt on origin/{lane.trunk}'
                   + (', the subjects reworded' if isinstance(rec, dict) else ''))


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


def trunk_history(wt, main):
    """True when ``wt``'s HEAD carries trunk history past ``origin/<main>`` (freshly fetched):
    a merge commit, or a commit whose patch the trunk already holds (``git cherry`` ``-``)."""
    tracking = f'refs/remotes/origin/{main}'
    if _git(wt, 'fetch', '-q', 'origin', f'+refs/heads/{main}:{tracking}').returncode != 0:
        return False
    merges = _git(wt, 'rev-list', '--merges', f'{tracking}..HEAD')
    if merges.returncode == 0 and merges.stdout.split():
        return True
    cherry = _git(wt, 'cherry', tracking, 'HEAD')
    return cherry.returncode == 0 and any(ln.startswith('- ') for ln in cherry.stdout.splitlines())


def hook_refusal(line):
    """True for a publish refused by the repo's pre-push hook or the redaction scan it runs —
    :func:`asf.workers.lifecycle.push_failure`'s one definition."""
    return lifecycle.push_failure(line) == lifecycle.HOOK_REFUSED


def drop_trunk_history(wt, main):
    """``(ok, line)``: ``wt``'s own commits rebased onto ``origin/<main>`` — its merges of the
    trunk and its copies of trunk commits dropped (``git rebase``), the worktree moved to the
    result. Only a clean worktree that carries trunk history (:func:`trunk_history`); a conflict
    is aborted and the worktree reset to where it was: ``(False, line)``. Never a push."""
    st = _git(wt, 'status', '--porcelain', '--untracked-files=no')
    if st.returncode != 0 or st.stdout.strip() or not trunk_history(wt, main):
        return False, ''
    head = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
    tracking = f'refs/remotes/origin/{main}'
    r = _git(wt, 'rebase', '-q', tracking)
    if r.returncode != 0:
        files = _git(wt, 'diff', '--name-only', '--diff-filter=U').stdout.split()
        _git(wt, 'rebase', '--abort')
        _git(wt, 'reset', '-q', '--hard', head)
        return False, (f'dropping the trunk history conflicts in: {", ".join(files) or "?"}')
    own = lifecycle._count(_git(wt, 'rev-list', '--count', f'{tracking}..HEAD'))
    return True, f'dropped the trunk history ({own} own commit(s) on origin/{main})'


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
    kind = UNPUSHED
    if not ok and hook_refusal(line):
        # W2-PR3c: the hook refused what the worktree's trunk history carries — dropped, and the
        # publish (its lane-N rewrite now on a plain chain) runs again; else as it was
        head = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
        dropped, how = drop_trunk_history(wt, main)
        if dropped:
            kind = lifecycle.HOOK_REFUSED
            ok2, line2 = lifecycle.publish(wt, branch, remote_sha, main=main,
                                           protected=protected, push_timeout_s=push_timeout_s,
                                           droppable=droppable)
            if ok2:
                ok, line = True, f'{how}: {line2}'
            else:
                _git(wt, 'reset', '-q', '--hard', head)
                line = f'{line} (the factory {how}, and that refused too: {line2})'
        elif how:
            line = f'{line} ({how})'
    new = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
    return ok, line, Outcome(kind, bool(ok), new, line)


def unpushed(lane, f, cause):
    """``unpushed`` / ``hook refused``: the run's worktree published (:func:`publish_worktree`,
    which drops trunk history a hook refused) — resolved when it went out. No worktree, or none
    on disk: a residue (the hold as today)."""
    run = f.get('run') or {}
    wt, b = run.get('worktree'), f.get('branch') or run.get('branch')
    head = f.get('head') or ''
    if not wt or not b or not os.path.isdir(wt):
        return Outcome((cause or {}).get('kind') or UNPUSHED, False, head, '')
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

#: The table: cause kind → entry.
MECHANICAL = {
    'conflict': rebase,
    lifecycle.REBASE_CONFLICT: rebase,
    lifecycle.COPIES: rebase,
    'merge': rebase,
    lifecycle.NAMING: naming,
    lifecycle.FOOTPRINT: widen_footprint,
    lifecycle.UNPUSHED: unpushed,
    lifecycle.HOOK_REFUSED: unpushed,
}


# ---- applying one ------------------------------------------------------------------------------

def handles(product, kind):
    """True when the lane should try ``kind`` here before writing its hold."""
    return kind in MECHANICAL and enabled(product)


def event(out, at=None, trunk=None):
    """The ``mechanical`` event of ``out`` as written on the run; ``trunk``, when the lane
    knows it, the trunk sha it was tried on."""
    ev = {'event': EVENT, 'kind': out.kind, 'head': out.head, 'resolved': out.resolved,
          'at': at or now_iso()}
    if trunk:
        ev['trunk'] = trunk
    return ev


def trunk_of(lane):
    """The lane's ``origin/<trunk>`` sha, or None when it has no repo to read it from."""
    read = getattr(lane, 'trunk_sha_now', None)
    if read is None or not getattr(lane, 'repo', None):
        return None
    try:
        return read() or None
    except Exception:  # noqa: BLE001 — a fact for the retry's bound, never a reason to fail
        return None


def to_dt(stamp):
    """An ISO ``…Z`` stamp as an aware datetime, or None."""
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def tried(run, kind, head, now=None, trunk=None):
    """True when ``run`` already carries an unresolved ``kind`` try on ``head`` — on this very
    ``trunk``, or younger than :data:`RETRY_AFTER` — so the table does not try it again yet."""
    ev = (run or {}).get(EVENT) or {}
    if ev.get('kind') != kind or ev.get('head') != head or ev.get('resolved'):
        return False
    if trunk and ev.get('trunk') == trunk:
        return True
    at = to_dt(ev.get('at'))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return at is not None and now - at < RETRY_AFTER


def note(lane, f, out):
    """Write ``out``'s event on ``f``'s run (and on the run in hand) — a try the lane made on its
    own path (:meth:`asf.harvest.lane.Lane.drop_copies`), counted like the table's. No-op with
    the flag off or on a dry run."""
    if not enabled(getattr(lane, 'product', None)) or getattr(lane, 'dry_run', False):
        return
    run = f.get('run') or {}
    job = run.get('job') or f.get('branch')
    if not job:
        return
    ev = event(out, trunk=trunk_of(lane))
    H.mark_session(lane.state_dir, job, mechanical=ev, branch=f.get('branch'))
    if f.get('run') is not None:
        f['run'][EVENT] = ev


def carry_review(lane, f, was):
    """The approval current on the head the table's move left (``was``: ``(head, review)``
    before it) carried to the head it made: ``f['review']`` current again, ``carried`` naming the
    old head. Only for an approved review that was current there."""
    old, rv = was
    now = f.get('review') or {}
    if not rv or not rv.get('current') or now.get('current') or not old \
            or old == f.get('head') or rv.get('verdict') != 'approved':
        return
    if not now or now.get('path') == rv.get('path'):
        f['review'] = dict(now or rv, current=True, carried=old)
        lane.out(f'{EVENT} {f.get("branch")}: {rv.get("path")} approved {old[:9]} — carried to '
                 f'{(f.get("head") or "")[:9]} (the lane\'s own move; the gate runs on it)')


def retry_pending(lane, f):
    """A pending correction of a :data:`RETRY` kind tried again by the table (:func:`apply`)
    before a session — or an adjudication, ``at_cap`` — takes it: with the flag on, no session
    live on the branch, not parked, and not :func:`tried` on this head within
    :data:`RETRY_AFTER`. The :class:`Outcome`, or None when nothing was tried."""
    corr = f.get('correction') or {}
    kind = corr.get('kind')
    if kind not in RETRY or corr.get('parked') or corr.get('ruled') or f.get('live') \
            or not f.get('head') \
            or not f.get('kind') or f.get('foreign') or not enabled(getattr(lane, 'product', None)) \
            or getattr(lane, 'dry_run', False):
        return None
    if tried(f.get('run'), kind, f['head'], trunk=trunk_of(lane)):
        return None
    return apply(lane, f, {'kind': kind, 'text': corr.get('text') or ''})


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
    was = (f.get('head'), dict(f['review']) if f.get('review') else None)  # a snapshot: the
    # entry's push reads the review's currency again, on the same dict
    out = entry(lane, f, cause)
    b = f.get('branch')
    run = f.get('run') or {}
    if out.resolved:
        if f.get('run') is None:
            lane.write(f, lane.record(f, PUSHED, 'adopted'))
            run = f.get('run') or {}
        moved = was[0] if was[0] and was[0] != (out.head or f.get('head')) else None
        lane.set(f, PUSHED, f'{EVENT}:{kind}', event=EVENT, mechanical=kind,
                 mechanical_from=moved)
        carry_review(lane, f, was)
        corr = f.get('correction') or {}
        fields = {'mechanical': event(out, trunk=trunk_of(lane))}
        if corr.get('kind') in CLEARS:
            fields['correction'] = None
            f['correction'] = None
        H.mark_session(lane.state_dir, run.get('job') or b, **fields, branch=b)
        if f.get('run') is not None:
            f['run'][EVENT] = fields[EVENT]
        lane.results[b] = 'mechanical'
        lane.out(f'{EVENT}:{kind} {b}: resolved — {out.why} '
                 f'({out.head[:9]}, no session; the gate runs on the new head)')
    elif out.deferred:  # the check on the rebuilt head runs in the background: no try counted
        lane.out(f'{EVENT}:{kind} {b}: deferred — {out.why}')
    else:
        if run.get('job') or b:
            ev = event(out, trunk=trunk_of(lane))
            H.mark_session(lane.state_dir, run.get('job') or b, mechanical=ev, branch=b)
            if f.get('run') is not None:
                f['run'][EVENT] = ev
        lane.out(f'{EVENT}:{kind} {b}: residue — '
                 + (out.why.split('\n', 1)[0] if out.why else 'not the lane\'s to settle')
                 + ' — a session gets it')
    return out
