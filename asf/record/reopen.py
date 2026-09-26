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
derive, I1), and a ``## History`` line says what changed and why. The write goes through the same
staged/published path ``asf set`` uses: :func:`asf.record.stage.guarded`, then the console's
``_published`` commits and pushes it, hooks on.
"""
import sys

from asf import env
from asf.evidence import closing, evidence
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items, now_iso, today
from asf.record.ingest import EVIDENCE_TYPES, MACHINE_KEY_ORDER, RULE_PREFIX, append_history_lines
from asf.record.ingest import derive, is_retired, write_fields


def cmd_reopen(args, root):
    name = getattr(args, 'product', None)
    if not name:
        try:
            name = env.default_product_name()
        except env.ConfigError:
            name = None
    product = env.load_product(name) if name else None
    # always fresh: the whole point is to read past whatever cached evidence produced the wrong
    # closing, and a 3-minute-old cache is exactly what a fixed detector must not reuse here
    ev = evidence.load(fresh=True, product=product)

    by_id, parse_errors = load_items(root)
    if parse_errors:
        for f, line, why in parse_errors:
            print(f"{f}:{line}: {why}", file=sys.stderr)
        return 1
    canonical, _dupes = canonicalize(by_id)
    rec = canonical.get(args.id)
    if rec is None:
        print(f"error: no item {args.id!r}", file=sys.stderr)
        return 2
    type_ = rec['meta'].get('type')
    if type_ not in EVIDENCE_TYPES:
        print(f"error: {args.id} is a {type_} — reopen corrects an evidence-derived item only "
              f"({', '.join(sorted(EVIDENCE_TYPES))})", file=sys.stderr)
        return 2
    if type_ == 'feature' and is_retired(rec['meta']):
        print(f"error: {args.id} is removed/moved — ingest derives nothing for it", file=sys.stderr)
        return 2
    typed, machine = frontmatter.split_machine(rec['meta'])
    old_state = machine.get('state', 'New')
    old_stage = machine.get('stage')
    if old_state not in (closing.RESOLVED, closing.CLOSED):
        print(f"error: {args.id} is {old_state} — reopen corrects a falsely derived Resolved or "
              f"Closed, nothing here to undo", file=sys.stderr)
        return 2

    now = now_iso()
    date = today()
    new_state, closings, _derived, stage_val, _task_ev, _evs = derive(
        canonical, ev, product, now, date, bypass_sticky={args.id})
    c = closings[args.id]
    if c.state in (closing.RESOLVED, closing.CLOSED):
        print(f"error: {args.id} — current evidence still says {c.state} (rule: {c.rule}): "
              + '; '.join(c.lines), file=sys.stderr)
        return 2

    new_stage = stage_val.get(args.id)
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
    marker = f"{now}: {args.reason} — {old_state} → {c.state}"
    fields['reopened'] = list(machine.get('reopened') or []) + [marker]
    fields['updated'] = now
    ordered = {k: fields[k] for k in MACHINE_KEY_ORDER if k in fields}
    ordered.update((k, v) for k, v in fields.items() if k not in ordered)

    stamp = now[:16].replace('T', ' ')
    stage_part = f", stage {old_stage or 'card'} → {new_stage or 'card'}" \
        if old_stage is not None or new_stage is not None else ''
    history_line = f"- {stamp} reopen: {args.reason} — state {old_state} → {c.state}{stage_part}"

    def _write(_root, path, relpath):
        write_fields(path, machine, ordered)
        with open(path, encoding='utf-8') as f:
            text = f.read()
        meta2, body2 = frontmatter.parse(text, path=relpath)
        new_body = append_history_lines(body2, [history_line])
        if new_body != body2:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(frontmatter.render(meta2, new_body))

    from asf.record import stage as stage_mod
    _r, _staged, findings = stage_mod.guarded(
        root, 'reopen', _write, (rec['path'], rec['relpath']), product=product,
        only=[rec['relpath']])
    if findings:
        print(f"error: reopen refused — "
              + '; '.join(f'{f.invariant}: {f.message}' for f in findings), file=sys.stderr)
        return 2

    print(f"{args.id}: reopened — state {old_state} → {c.state}{stage_part}")
    return 0
