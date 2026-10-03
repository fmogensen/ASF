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

Every entry is a function ``(lane, f, cause) -> Outcome`` over what the lane already does, never
a new git path. :func:`apply` runs one, only under ``conventions.flags.mechanical: on`` (default
off — today's rows), writes the event on the run (``mechanical: {event, kind, head, resolved}``;
a resolved one also on the lane record, reason ``mechanical:<kind>``) and says one line. The
gate still runs on the rebuilt head: a resolved cause goes back to PUSHED, never past it.
"""
import dataclasses

from asf.feeder import footprint, widen
from asf.harvest import harvest as H
from asf.workers import lifecycle
from asf.workers.pool import now_iso

#: ``conventions.flags.<FLAG>`` turns the table on; anything but an "on" word is off.
FLAG = 'mechanical'
ON_WORDS = ('on', 'true', 'yes', '1')
#: The event name written on the run (and on the lane record of a resolved cause).
EVENT = 'mechanical'
#: The lane state a resolved cause leaves the branch in: the gate runs again on its new head.
PUSHED = 'PUSHED'
#: A resolved cause clears a pending correction of these kinds — the work they asked for is done.
CLEARS = ('conflict', lifecycle.REBASE_CONFLICT, lifecycle.NAMING, lifecycle.COPIES, 'merge')


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


#: The table: cause kind → entry. 3b/3c add ``unpushed``, ``copies``, ``hook refused``, ``naming``.
MECHANICAL = {
    'conflict': rebase,
    lifecycle.REBASE_CONFLICT: rebase,
    lifecycle.FOOTPRINT: widen_footprint,
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
