"""asf.kernel.facts — one tick's :class:`~asf.kernel.model.Facts`, read through the ports (ASF 0.2).

Every read happens here, once per tick, and nothing is written. Three filters keep the facts to
what ``decide`` should act on:

- an operator answer counts only for an item the kernel is waiting on for one (a ``question`` on
  the card, or Stuck with ``owner: operator``) — the answers ledger also holds the old floor's
  answers, already acted on;
- a PR whose item is not on the record is dropped (a branch naming a retired or foreign id);
- a landed spec counts only while its Feature's card is not Done: a finished Feature's Stories
  are history, never minted afresh (the old record never minted the Stories of its early specs).
"""
from asf.kernel.model import Facts, State


def read_facts(ports):
    """The :class:`~asf.kernel.model.Facts` the three ports describe now."""
    record = ports.record
    items = record.items()
    prs = [p for p in ports.github.prs() if p.item_id in items]
    reviews = list(record.reviews()) + list(ports.github.reviews([p for p in prs if not p.merged]))
    waiting = {iid for iid, it in items.items()
               if it.question or (it.state is State.STUCK and it.stuck
                                  and it.stuck.owner == 'operator')}
    answers = [a for a in record.answers() if a.item_id in waiting]
    specs = {fid: text for fid, text in record.specs_landed().items()
             if fid in items and items[fid].state is not State.DONE}
    return Facts(items=items, prs=prs, sessions=list(ports.sessions.sessions()), reviews=reviews,
                 answers=answers, specs_landed=specs, paused=record.paused())
