"""asf.record.retire — take an item that landed by hand off the board (``asf retire``).

``asf retire <item> --why "<reason>" [--landed <ref>…]`` is the supported way to say "this is
done, outside the factory": no hand edit of a card's ``removed:`` line, no operator approval.

* A card (``F-0012``, ``B-0007`` …) gets ``removed: retired: <reason> (landed <refs>)`` — and a
  ``landed:`` sha when a ref is one — plus a ``## History`` line, written through the ``asf set``
  writer (:func:`asf.record.setfield.set_typed`: the parser round-trip and the record stage).
* A note in the record's intake directory that no groom typed yet moves to ``<intake>/done/``
  with the same words in its header (:func:`asf.groom.inbox.retire_note`).

The console wraps the command in the record's publish path (``asf.cli._published``): one
signed-off commit with the derived index, retried when another commit lands meanwhile, pushed
through the one record push. Either way the item leaves the feeder, and intake never mints it
again — not from the moved note, and not from the same note or signature filed a second time
(:func:`asf.groom.inbox.retired_keys`).
"""
import sys

from asf.record.check import LANDED_SHA_RE, product_of
from asf.record.core import canonicalize, load_items, today


def landing_ref(raw):
    """``(kind, text)`` for one ``--landed`` value: ``('pr', '#123')`` for ``123`` or ``#123``,
    ``('sha', 'abc1234')`` for a 7–40 character hex sha, else ``('ref', raw)`` (a URL, say)."""
    ref = str(raw or '').strip()
    if ref.lstrip('#').isdigit():
        return 'pr', '#' + ref.lstrip('#')
    if LANDED_SHA_RE.fullmatch(ref):
        return 'sha', ref
    return 'ref', ref


def removal(why, refs):
    """What ``removed:`` (or a retired note's header) says: :data:`asf.groom.inbox.RETIRED`, the
    reason, and the landing refs when there are any."""
    from asf.groom.inbox import RETIRED
    text = f"{RETIRED}: {why}"
    return text + (f" (landed {', '.join(t for _k, t in refs)})" if refs else '')


def _flat(values):
    out = []
    for v in values or ():
        out.extend(v if isinstance(v, (list, tuple)) else [v])
    return [str(v) for v in out if str(v).strip()]


def cmd_retire(args, root):
    why = ' '.join(str(args.why or '').split())
    if not why:
        print("error: asf retire wants --why \"<the reason>\"", file=sys.stderr)
        return 2
    refs = list(dict.fromkeys(landing_ref(r) for r in _flat(args.landed)))
    text = removal(why, refs)
    item = str(args.item or '').strip()

    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    rec = canonical.get(item) or canonical.get(item.upper())
    if rec is not None:
        return _retire_card(args, rec, why, refs, text)

    from asf.groom import inbox
    intake_dir = inbox._intake_dir(args)
    name = inbox.find_note(root, intake_dir, item)
    if name is None:
        print(f"error: no item and no {intake_dir}/ note {item!r}", file=sys.stderr)
        return 2
    inbox.retire_note(root, intake_dir, name, text, today())
    print(f"{intake_dir}/{name}: {text} — moved to {intake_dir}/done/")
    return 0


def _retire_card(args, rec, why, refs, text):
    from asf.record.setfield import set_typed
    item_id = rec['meta'].get('id')
    if rec['meta'].get('removed'):
        print(f"error: {item_id} is already removed: {rec['meta'].get('removed')}",
              file=sys.stderr)
        return 2
    updates = {'removed': text}
    sha = next((t for k, t in refs if k == 'sha'), None)
    if sha and not rec['meta'].get('landed'):
        updates['landed'] = sha
    landed = f" (landed {', '.join(t for _k, t in refs)})" if refs else ''
    hist = f"- {today()} retire: retired, removed — {why}{landed}"
    err = set_typed(rec, updates, writer='retire', product=product_of(args), history=[hist])
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print(f"{item_id}: {text}")
    return 0
