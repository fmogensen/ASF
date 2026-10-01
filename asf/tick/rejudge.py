"""asf.tick.rejudge — a park written for a question the report never asked is released, and a
reshape's "does not split" is taken by the factory.

Run by the ``health`` step, after health has ended the sessions and before ``widen_footprint``:

1. :func:`release_answered` — a park the factory wrote because a run's report declared a question
   (a ``blocked`` park, or a relaunch cap whose last report read ``needs input — …``) is read
   again through :func:`asf.workers.report.needs_input`. When the report asks nothing — its
   ``NEEDS OPERATOR:`` is ``none`` (or ``none — <a note>``, ``omit``), the question is a
   reshape's ``does not split`` answer, or the report's ``needs writes:`` names paths the
   widening rule decides — the park is released (``unparked``, as ``asf unpark`` writes it) and
   the run is judged on what its report says: ``status: done`` on an empty branch is
   ``nothing to land``; unpushed work is a correct round (:func:`asf.workers.lifecycle.hold`); a
   ``needs writes:`` claim is read again by the widening rule. A park with a real question stays.
2. :func:`take_no_split` — an ended reshape run whose report answers that its Task does not split
   (:func:`asf.workers.report.no_split`) drops the Task's ``reshape:`` and records the answer in
   ``reshape_declined:`` (the groom's own split key when the reshape was the groom's split, and
   :data:`asf.feeder.rows.WHOLE`), one History line, one commit; a footprint correction the rule
   had answered with a reshape is handed back to the rule as ``whole`` — a Task that does not
   split is widened, or waits on the live Task that writes the paths, never reshaped again
   (:func:`asf.feeder.widen.decide`). The Task goes back to its own code/delivery row.
"""
import re

from asf.feeder import rows as feeder_rows
from asf.feeder import widen
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import report as report_mod
from asf.workers import runtime as runtime_mod

#: The park kinds written for a question a run's report declared.
QUESTION_PARKS = (lifecycle.BLOCKED, lifecycle.RELAUNCH_CAP)
#: A relaunch cap's reason carries the report's claim; only a question's is re-judged here.
ASKED = 'needs input — '
#: The reshape value the groom's split answer writes: ``split <a> | <b> (groom <date>)``.
GROOM_SPLIT_RE = re.compile(r'^split (?P<areas>.+?) \(groom [^)]*\)\s*$')
RESHAPE_KIND = 'reshape'
#: The end reasons of a run whose work is not on origin (the health step's own prefixes).
UNPUSHED_PREFIXES = ('failed: not pushed', f'failed: {report_mod.UNPUSHED}')


def _text(run):
    rec = runtime_mod.read_result((run or {}).get('log'))
    return str(rec.get('result') or '') if isinstance(rec, dict) else ''


def asked(corr):
    """True when ``corr`` is a park written for a question its run's report declared."""
    corr = corr or {}
    if not corr.get('parked') or corr.get('kind') not in QUESTION_PARKS:
        return False
    return corr.get('kind') == lifecycle.BLOCKED or ASKED in str(corr.get('reason') or '')


def judged(run, text):
    """``(fields, what)``: the run judged on its own report once its park is released — the
    fields to append, and what it now is."""
    reason = str(run.get('end_reason') or '')
    status = (report_mod.parse(text).get('status') or '').strip().lower().split(' ')[0]
    if reason == f'failed: {lifecycle.EMPTY_BRANCH}' and status == 'done':
        return {'end_reason': lifecycle.NOTHING_TO_LAND}, lifecycle.NOTHING_TO_LAND
    return {}, reason or 'ended'


def release_answered(ctx, out=print, now=None):
    """Release every question park whose run's report, read again, asks nothing. Returns
    ``{job: what the run now is}``."""
    product = ctx.product
    path = pool_mod.sessions_path(product)
    now = now or pool_mod.now_iso()
    done = {}
    for job, run in sorted(lifecycle.latest(path).items()):
        corr = lifecycle.pending_correction(run, path)
        if not asked(corr):
            continue
        text = _text(run)
        if not text or report_mod.needs_input(text) is not None:
            continue  # a real question: the park stands
        fields, what = judged(run, text)
        why = 'its report asks no question (re-judged)'
        pool_mod.update_session(product, job, correction=None, unparked=now, unpark_why=why,
                                footprint_read=None, operator_flagged=None, **fields)
        run = dict(run, correction=None, unparked=now, **fields)
        reason = str(run.get('end_reason') or '')
        if reason.startswith(UNPUSHED_PREFIXES):
            if run.get('kind') != RESHAPE_KIND:  # a reshape's own answer is take_no_split's
                hfields, _line = lifecycle.hold(path, run, lifecycle.UNPUSHED,
                                                lifecycle.unpushed_text(reason), now,
                                                main=product.main)
                pool_mod.update_session(product, job, **hfields)
                what = 'a correct round: its work is not on origin'
        ctx.event('unparked', job=job, item=run.get('item'), text=why)
        out(f'unparked {run.get("item")} (job {job}): {why} — {what}')
        done[job] = what
    return done


def declined_keys(item_id, reshape):
    """The ``reshape_declined:`` entries a no-split answer adds: :data:`feeder_rows.WHOLE`, and
    the groom's own split key when ``reshape`` is the groom's split (so it is not proposed
    again)."""
    keys = [feeder_rows.WHOLE]
    m = GROOM_SPLIT_RE.match(str(reshape or '').strip())
    if m:
        from asf.groom import shape
        areas = [a.strip() for a in m.group('areas').split('|') if a.strip()]
        keys.append(shape.proposal_key('split', (item_id,), areas))
    return keys


def take_no_split(ctx, items, out=print, now=None):
    """Take every ended reshape run's "does not split" answer (see the module doc). ``items`` is
    updated in place. Returns ``{item: the answer}``."""
    from asf.tick import widen_footprint
    product = ctx.product
    path = pool_mod.sessions_path(product)
    now = now or pool_mod.now_iso()
    stamp = now[:16].replace('T', ' ')
    latest = lifecycle.latest(path)
    done, reindex = {}, False
    for job, run in sorted(latest.items()):
        if run.get('kind') != RESHAPE_KIND or not run.get('ended') or run.get('nosplit_read'):
            continue
        item_id = run.get('item')
        answer = report_mod.no_split(_text(run))
        task = (items or {}).get(item_id) or {}
        if not answer or task.get('type') != 'task' or lifecycle.closed_state(items, item_id):
            pool_mod.update_session(product, job, nosplit_read=1)
            continue
        declined = list(task.get('reshape_declined') or ())
        add = [k for k in declined_keys(item_id, task.get('reshape')) if k not in declined]
        if task.get('reshape') or add:
            updates = {'reshape_declined': declined + add}
            if task.get('reshape'):
                updates['reshape'] = None
            note = f'reshape declined: {answer[:160]} — whole, back to its own row ({job})'
            err = widen_footprint.write_card(ctx.record_root(), item_id, updates, stamp, note,
                                             product=product)
            if err:
                out(f'no split {item_id}: card unchanged — {err}')
                continue
            reindex = True
            if items is not None and item_id in items:
                items[item_id] = dict(task, reshape=None, reshape_declined=declined + add)
        for other_job, other in latest.items():
            if other.get('item') != item_id:
                continue
            corr = lifecycle.pending_correction(other, path)
            if not corr:
                continue
            if corr.get('kind') == lifecycle.FOOTPRINT and corr.get('verdict') == widen.RESHAPE:
                pool_mod.update_session(product, other_job, correction=dict(
                    {k: v for k, v in corr.items() if k not in ('verdict', 'detail')}, whole=True))
            elif corr.get('parked') and other_job == job:
                pool_mod.update_session(product, other_job, correction=None, unparked=now,
                                        unpark_why='no split: the Task stays whole',
                                        operator_flagged=None)
        pool_mod.update_session(product, job, nosplit_read=1)
        ctx.event('no split', job=job, item=item_id, text=answer)
        out(f'no split {item_id}: {answer[:120]} — whole, back to its own row')
        done[item_id] = answer
    if reindex:
        err = widen_footprint._reindex(ctx.record_root())
        if err:
            out(f'no split: index not re-derived — {err}')
    return done


def run(ctx, items, out=print):
    """Both passes, the no-split answer first (it releases its own reshape's park)."""
    taken = take_no_split(ctx, items, out=out)
    released = release_answered(ctx, out=out)
    return taken, released
