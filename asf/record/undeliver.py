"""asf.record.undeliver — take one member out of its lead's delivery (``asf undeliver``, F-0263).

A delivery is a lead's ``delivers:`` list, each member pointing back with ``delivered_by:``
(:mod:`asf.record.slice`). When an adjudication rules that the lead's PR does not build a member,
the member must go back to its own lane — but neither field is settable by ``asf set``, retiring
the member is refused (``delivers references removed item``) and a copy is a duplicate, so the
member reads ``WAITS ON delivery <lead>`` for ever.

``asf undeliver <task> --why "<reason>"`` is the supported path: the member leaves the lead's
``delivers:`` (the whole list goes when only the lead is left in it), its own ``delivered_by:`` is
deleted, and each card gets a ``## History`` line — written through the ``asf set`` writer
(:func:`asf.record.setfield.set_typed`: the parser round-trip and the record stage). The console
wraps it in the record's publish path (``asf.cli._published``), so both cards and the derived
``index.json`` land in one signed-off commit.
"""
import sys

from asf.record.check import product_of
from asf.record.core import canonicalize, is_open, load_items, today

#: The History line each card gets; ``{who}`` is empty on the member, ``<task> `` on the lead.
HISTORY = '- {date} undeliver: {who}undelivered from {lead}: {why}'


def cmd_undeliver(args, root):
    from asf.record.setfield import set_typed
    why = ' '.join(str(args.why or '').split())
    if not why:
        print("error: asf undeliver wants --why \"<the reason>\"", file=sys.stderr)
        return 2
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    tid = str(args.task or '').strip()
    rec = canonical.get(tid) or canonical.get(tid.upper())
    if rec is None:
        print(f"error: no item {tid!r}", file=sys.stderr)
        return 2
    tid = rec['meta'].get('id')
    lid = str(rec['meta'].get('delivered_by') or '').strip()
    if not lid:
        print(f"error: {tid} is not a delivery member (no delivered_by:)", file=sys.stderr)
        return 2
    lead = canonical.get(lid)
    if lead is None:
        print(f"error: {tid}'s lead {lid} has no card", file=sys.stderr)
        return 2
    if not is_open(lead):
        # a done lead no longer speaks for the member, but the stale edge still reads as one
        print(f"note: {lid} is not open ({lead['meta'].get('removed') and 'retired' or 'Closed'})"
              f" — cleaning the edge anyway")
    product = product_of(args)
    delivers = [str(m) for m in lead['meta'].get('delivers') or ()]
    kept = [m for m in delivers if m != tid]
    # a delivery of the lead alone is no delivery: the list goes with its last member
    lead_updates = {'delivers': kept if [m for m in kept if m != lid] else None}
    lead_line = HISTORY.format(date=today(), who=f'{tid} ', lead=lid, why=why)
    member_line = HISTORY.format(date=today(), who='', lead=lid, why=why)
    # the lead first: a member still pointing at a lead that no longer lists it breaks no
    # invariant, while a lead listing a member that no longer points back does
    before = lead['text']
    err = set_typed(lead, lead_updates, writer='undeliver', product=product,
                    history=[lead_line])
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    err = set_typed(rec, {'delivered_by': None}, writer='undeliver', product=product,
                    history=[member_line])
    if err:
        from asf.record.setfield import _write
        _write(None, lead['path'], before)  # never leave half an edge behind
        print(f"error: {err}; {lead['relpath']} put back", file=sys.stderr)
        return 2
    rest = ', '.join(kept) if lead_updates['delivers'] else 'none (delivers: dropped)'
    print(f"{tid}: undelivered from {lid} — {lid} delivers {rest}")
    return 0
