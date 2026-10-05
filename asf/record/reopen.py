"""asf.record.reopen — undo a falsely derived Closed/Resolved (``asf reopen``).

``asf set`` refuses ``state``/``stage`` (they are machine-derived by ``asf ingest``), and once
ingest writes ``Closed`` it never derives backwards — ``closing.sticky`` "holds" it there forever
(§1.4), evidence after the fact or not. That is right for an item that really landed; it is a
trap for one a bug in the evidence pass landed by mistake (a PR body's stray branch-path mention,
say). ``asf reopen <ID> --reason "…"`` is the supported way out: it re-derives the item's state
(and a Feature's stage) from *current* evidence exactly as ``asf ingest`` would
(:func:`asf.record.ingest.derive`), with the terminal hold turned off for this one id, and
refuses when that fresh derivation still genuinely lands it — a card closed wrongly gets
corrected in code, never by hand (the operator's own rule: fix the software, or here, re-run its
rule; never edit the state field directly).

A ``reopened:`` machine line — one entry per reopen, timestamped and reasoned — is the marker
that survives every later ``asf ingest`` untouched (ingest never drops a key it does not itself
derive, I1), and a ``## History`` line says what changed and why. The card's ``landing:`` stamp
is cleared: the close it recorded was the false one. The write goes through the same
staged/published path ``asf set`` uses: :func:`asf.record.stage.guarded`, then the console's
``_published`` commits and pushes it, hooks on.

A Story's Feature that reads Resolved or Closed is re-derived in the same pass, its hold lifted
too, and reopened with the Story when it no longer derives done (S6): the Feature's done stood on
that Story. ``asf untick`` (the one reverse of a tick) and ``asf audit-proofs`` ("no test, no
done": every Resolved/Closed Story with an unproved acceptance line) live here, and reopen
through the same :func:`reopen`.
"""
import collections
import dataclasses
import sys

from asf import env, proves
from asf.evidence import closing, evidence
from asf.record import frontmatter, writer
from asf.record.core import canonicalize, load_items, now_iso, today
from asf.record.ingest import EVIDENCE_TYPES, LANDING_KEY, MACHINE_KEY_ORDER, RULE_PREFIX
from asf.record.ingest import append_history_lines
from asf.record.ingest import derive, is_retired, write_fields


def _product(args):
    name = getattr(args, 'product', None)
    if not name:
        try:
            name = env.default_product_name()
        except env.ConfigError:
            name = None
    return env.load_product(name) if name else None


def _canonical(root):
    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return None
    return canonicalize(by_id)[0]


def cmd_reopen(args, root):
    product = _product(args)
    # always fresh: the whole point is to read past whatever cached evidence produced the wrong
    # closing, and a 3-minute-old cache is exactly what a fixed detector must not reuse here
    ev = evidence.load(fresh=True, product=product)
    return reopen(root, product, ev, args.id, args.reason)


def _done_parent(canonical, rec):
    """A Story's parent Feature when it reads Resolved or Closed — the item a Story's reopen
    re-derives in the same pass (S6): the Feature's done stood on that Story."""
    if rec['meta'].get('type') != 'story':
        return None
    fid = rec['meta'].get('parent')
    frec = canonical.get(fid) if fid else None
    if frec is None or frec['meta'].get('type') != 'feature' or is_retired(frec['meta']):
        return None
    state = frontmatter.split_machine(frec['meta'])[1].get('state', 'New')
    return fid if state in (closing.RESOLVED, closing.CLOSED) else None


def _reopened(c):
    if c.rule == closing.NO_RULE and c.state in (closing.RESOLVED, closing.CLOSED):
        # no rule decides it any more (e.g. parent-closed no longer fires on a matrix-todo item):
        # the Closed it carries is a leftover, not a derivation — it reopens to New
        return dataclasses.replace(c, state=closing.NEW)
    return c


def _plan(rec, c, new_state, new_stage, now, reason):
    """``(machine, ordered, history_line, summary)`` — what reopening ``rec`` to ``c`` writes."""
    typed, machine = frontmatter.split_machine(rec['meta'])
    old_state = machine.get('state', 'New')
    old_stage = machine.get('stage')
    lines = list(c.lines) + [RULE_PREFIX + c.rule]
    blocked, open_blockers = evidence.blocked_of(rec['meta'].get('blockedBy'), new_state)
    fields = dict(machine)
    fields['state'] = c.state
    if new_stage is not None:
        fields['stage'] = new_stage
    else:
        fields.pop('stage', None)
    fields['evidence'] = lines
    if blocked:
        fields['blocked'] = True
        fields['blocked_by_open'] = open_blockers
    else:
        fields.pop('blocked', None)
        fields.pop('blocked_by_open', None)
    fields['stage_since'] = now
    # the landing the false close stood on is no landing: the next close stamps afresh, and I14
    # reads `landing.as_of` against this reopen
    fields.pop(LANDING_KEY, None)
    marker = f"{now}: {reason} — {old_state} → {c.state}"
    fields['reopened'] = list(machine.get('reopened') or []) + [marker]
    fields['updated'] = now
    ordered = {k: fields[k] for k in MACHINE_KEY_ORDER if k in fields}
    ordered.update((k, v) for k, v in fields.items() if k not in ordered)
    stamp = now[:16].replace('T', ' ')
    stage_part = f", stage {old_stage or 'card'} → {new_stage or 'card'}" \
        if old_stage is not None or new_stage is not None else ''
    history_line = f"- {stamp} reopen: {reason} — state {old_state} → {c.state}{stage_part}"
    return machine, ordered, history_line, f"state {old_state} → {c.state}{stage_part}"


def reopen(root, product, ev, iid, reason, out=print):
    """Re-derive ``iid`` from ``ev`` with the terminal hold lifted, and write what it derives
    when that is no longer Resolved/Closed. A Story's Feature, Resolved or Closed, is re-derived
    in the same pass (its hold lifted too) and reopened with it when it no longer derives done.
    Returns 0, or 2 with the reason on stderr."""
    canonical = _canonical(root)
    if canonical is None:
        return 1
    rec = canonical.get(iid)
    if rec is None:
        print(f"error: no item {iid!r}", file=sys.stderr)
        return 2
    type_ = rec['meta'].get('type')
    if type_ not in EVIDENCE_TYPES:
        print(f"error: {iid} is a {type_} — reopen corrects an evidence-derived item only "
              f"({', '.join(sorted(EVIDENCE_TYPES))})", file=sys.stderr)
        return 2
    if type_ == 'feature' and is_retired(rec['meta']):
        print(f"error: {iid} is removed/moved — ingest derives nothing for it", file=sys.stderr)
        return 2
    old_state = frontmatter.split_machine(rec['meta'])[1].get('state', 'New')
    if old_state not in (closing.RESOLVED, closing.CLOSED):
        print(f"error: {iid} is {old_state} — reopen corrects a falsely derived Resolved or "
              f"Closed, nothing here to undo", file=sys.stderr)
        return 2

    now = now_iso()
    date = today()
    parent = _done_parent(canonical, rec)
    lifted = {iid} | ({parent} if parent else set())
    new_state, closings, _derived, stage_val, _task_ev, _evs = derive(
        canonical, ev, product, now, date, bypass_sticky=lifted)
    c = _reopened(closings[iid])
    if c.state in (closing.RESOLVED, closing.CLOSED):
        print(f"error: {iid} — current evidence still says {c.state} (rule: {c.rule}): "
              + '; '.join(c.lines), file=sys.stderr)
        return 2
    writes = [(iid, rec, _plan(rec, c, new_state, stage_val.get(iid), now, reason))]
    if parent:
        fc = _reopened(closings[parent])
        if fc.state not in (closing.RESOLVED, closing.CLOSED):
            writes.append((parent, canonical[parent],
                           _plan(canonical[parent], fc, new_state, stage_val.get(parent), now,
                                 f"{reason} (its Story {iid} reopened)")))

    def _write(_root):
        for _id, r, (machine, ordered, history_line, _s) in writes:
            write_fields(r['path'], machine, ordered)
            with open(r['path'], encoding='utf-8') as f:
                text = f.read()
            meta2, body2 = frontmatter.parse(text, path=r['relpath'])
            new_body = append_history_lines(body2, [history_line])
            if new_body != body2:
                writer.write_card(r['path'], frontmatter.render(meta2, new_body))

    from asf.record import stage as stage_mod
    _r, _staged, findings = stage_mod.guarded(
        root, 'reopen', _write, (), product=product, only=[r['relpath'] for _i, r, _p in writes])
    if findings:
        print(f"error: reopen refused — "
              + '; '.join(f'{f.invariant}: {f.message}' for f in findings), file=sys.stderr)
        return 2
    for _id, _r, plan in writes:
        out(f"{_id}: reopened — {plan[3]}")
    return 0


# ---- asf untick: the operator's one reverse of a tick ----------------------------------------

def cmd_untick(args, root):
    """``asf untick <story> <line>``: the line's tick and its proof no longer count. The bullet
    flips back to ``- [ ]`` and History records ``untick: line N`` (:func:`asf.proves.proved_lines`
    reads it as cancelling every earlier ``proved line N``), so a hand tick — which no landing
    can credit — goes back to the claim that must prove it. A Story that read Resolved or Closed
    is then reopened (:func:`reopen`), its Feature with it."""
    product = _product(args)
    canonical = _canonical(root)
    if canonical is None:
        return 1
    sid = args.id.upper()
    rec = canonical.get(sid)
    if rec is None or rec['meta'].get('type') != 'story':
        print(f"error: {args.id} is not a Story in the record", file=sys.stderr)
        return 2
    n = int(args.line)
    m = len(proves.bullets(rec['body']))
    if not 1 <= n <= m:
        print(f"error: {sid} has {m} acceptance line(s); there is no line {n}", file=sys.stderr)
        return 2
    new_body, changed = proves.untick(rec['body'], n)
    if not changed and n not in proves.proved_lines(rec['body']):
        print(f"error: {sid} line {n} is neither ticked nor proved — nothing to untick",
              file=sys.stderr)
        return 2
    reason = getattr(args, 'reason', None) or 'operator'
    stamp = now_iso()[:16].replace('T', ' ')
    new_body = append_history_lines(new_body, [f"- {stamp} untick: line {n} — {reason}"])

    def _write(_root):
        with open(rec['path'], encoding='utf-8') as f:
            meta2, _body = frontmatter.parse(f.read(), path=rec['relpath'])
        writer.write_card(rec['path'], frontmatter.render(meta2, new_body))

    from asf.record import stage as stage_mod
    _r, _staged, findings = stage_mod.guarded(root, 'untick', _write, (), product=product,
                                              only=[rec['relpath']])
    if findings:
        print("error: untick refused — "
              + '; '.join(f'{f.invariant}: {f.message}' for f in findings), file=sys.stderr)
        return 2
    print(f"{sid}: line {n} unticked — {reason}")
    state = frontmatter.split_machine(rec['meta'])[1].get('state', 'New')
    if state in (closing.RESOLVED, closing.CLOSED):
        ev = evidence.load(fresh=True, product=product)
        return reopen(root, product, ev, sid, f"untick line {n}: {reason}")
    return 0


# ---- asf audit-proofs: every done Story with an unproved line ---------------------------------

def audit(canonical, register, repo_dir=None):
    """``[(story, state, feature, [(line, text, why), …]), …]`` — every Story that reads
    Resolved or Closed while an acceptance line has no proved-line entry, no registered
    deferral and no inline ``proven by`` a file in the checkout ``repo_dir``
    (:func:`asf.proves.unproved`). Reads the record's cards and, for inline proofs, the
    checkout; writes nothing."""
    out = []
    cache = {}
    for sid, rec in sorted(canonical.items()):
        if rec['meta'].get('type') != 'story' or rec['meta'].get('removed'):
            continue
        state = frontmatter.split_machine(rec['meta'])[1].get('state', 'New')
        if state not in (closing.RESOLVED, closing.CLOSED):
            continue
        lines = proves.unproved(rec['body'], register, repo_dir=repo_dir, _cache=cache)
        if lines:
            out.append((sid, state, rec['meta'].get('parent') or '', lines))
    return out


def cmd_audit_proofs(args, root):
    """``asf audit-proofs``: list them; ``--apply`` reopens each (and its Feature)."""
    from asf.record import decisions
    product = _product(args)
    canonical = _canonical(root)
    if canonical is None:
        return 1
    found = audit(canonical, decisions.register(canonical, product), decisions.repo_dir(product))
    print('| Story | State | Feature | Unproved |')
    print('|---|---|---|---|')
    for sid, state, fid, lines in found:
        what = '; '.join(f"{n}: {t[:60]} ({w})" for n, t, w in lines)
        print(f"| {sid} | {state} | {fid or '—'} | {what.replace('|', '/')} |")
    why = collections.Counter('unregistered deferral' if w.startswith('deferred') else w.split(':', 1)[0]
                              for *_x, lines in found for _n, _t, w in lines)
    if why:
        print('\nunproved lines by reason: '
              + ', '.join(f'{k} {v}' for k, v in sorted(why.items(), key=lambda kv: -kv[1])))
    print(f"\n{len(found)} Resolved/Closed Story(ies) with an unproved acceptance line"
          + ('' if getattr(args, 'apply', False) or not found else ' — --apply reopens them'))
    if not getattr(args, 'apply', False) or not found:
        return 0
    ev = evidence.load(fresh=True, product=product)
    rc = 0
    reopened = collections.defaultdict(list)
    for sid, _state, fid, lines in found:
        r = reopen(root, product, ev, sid,
                   f"audit-proofs: line(s) {', '.join(str(n) for n, _t, _w in lines)} unproved")
        rc = max(rc, r)
        if r == 0 and fid:
            reopened[fid].append((sid, lines))
    for fid, stories in sorted(reopened.items()):
        rc = max(rc, record_replan(root, product, fid, stories))
    return rc


def audit_reshape(stories, date):
    """The Feature-level reshape intent for the Stories an audit reopened: each Story with its
    unproved lines and why, and what the replan is to plan."""
    named = '; '.join(f"{sid} line {n} ({why})" for sid, lines in stories for n, _t, why in lines)
    return (f"plan proof/build Tasks for the reopened Stories {', '.join(s for s, _l in stories)} "
            f"(audit-proofs {date}: unproved lines — {named}); reuse an existing test where it "
            f"already proves a line (a Proves trailer only); one Task per Story; build the lines "
            f"that are not built.")


def record_replan(root, product, fid, stories, out=print):
    """Record a pending replan on Feature ``fid`` for the ``stories`` (``[(sid, lines)]``) an
    audit reopened: its ``reshape:`` (:func:`asf.record.replan.pending`) names them and their
    unproved lines, so the next tick queues the Feature's RESHAPE → REPLAN row. Reopening a Story
    alone creates no work — a replan starts only from the Feature's ``reshape:``, and groom asks
    only for a Story no Task lists (2026-10-05). A reshape still pending keeps its text, the
    audit's added after it: one replan answers both. Returns 0, or 2 when the write is refused."""
    from asf.record import replan
    from asf.record import stage as stage_mod
    canonical = _canonical(root)
    rec = (canonical or {}).get(fid)
    if rec is None or rec['meta'].get('type') != 'feature' or is_retired(rec['meta']):
        return 0
    meta = rec['meta']
    how = audit_reshape(stories, today())
    current = str(meta.get('reshape') or '').strip()
    if current and replan.pending(dict(meta, id=fid)):
        if how in current:
            return 0
        how = f"{current} — and {how}"
    stamp = now_iso()[:16].replace('T', ' ')
    hist = (f"- {stamp} audit-proofs: replan pending — reopened "
            f"{', '.join(s for s, _l in stories)}")

    def _write(_root):
        frontmatter.write_typed(rec['path'], {'reshape': how})
        with open(rec['path'], encoding='utf-8') as f:
            meta2, body2 = frontmatter.parse(f.read(), path=rec['relpath'])
        new_body = append_history_lines(body2, [hist])
        if new_body != body2:
            writer.write_card(rec['path'], frontmatter.render(meta2, new_body))

    _r, _staged, findings = stage_mod.guarded(root, 'audit-proofs', _write, (), product=product,
                                              only=[rec['relpath']])
    if findings:
        print(f"error: {fid} replan refused — "
              + '; '.join(f'{f.invariant}: {f.message}' for f in findings), file=sys.stderr)
        return 2
    out(f"{fid}: replan pending — {', '.join(s for s, _l in stories)}")
    return 0
