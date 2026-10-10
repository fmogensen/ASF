"""asf.kernel.facts — one tick's :class:`~asf.kernel.model.Facts`, read through the ports (ASF 0.2).

Every read happens here, once per tick, and nothing is written. Three filters keep the facts to
what ``decide`` should act on:

- an operator answer counts for an item with an open ``question`` on its card, and for a Stuck
  item of any owner when it was given after the Stuck was recorded (``Answer.at`` later than
  ``Item.stuck_since``); with either time unknown it counts only for a Stuck with ``owner:
  operator``. The answers ledger also holds the old floor's answers, already acted on, and an
  answer older than the Stuck never clears it (:func:`answer_counts`);
- a PR whose item is not on the record is dropped (a branch naming a retired or foreign id);
- a landed spec counts only while its Feature's card is not Done: a finished Feature's Stories
  are history, never minted afresh (the old record never minted the Stories of its early specs).

The verdicts are the kernel's review ledger (``state/<product>/kernel-reviews.jsonl``, one row per
reviewer report, keyed by the tree it read), GitHub's own reviews on the head, and the old floor's
approvals that still name the head (:meth:`asf.kernel.ports.RealGitHub.floor_approvals`). Parked
items (``priority: later`` on the item or an ancestor) are not filtered here: ``decide`` holds
that rule, so it is the same everywhere. The pushed branches
(:meth:`asf.kernel.ports.RealGitHub.branches`) count only for items on the record.
"""
from asf.kernel.model import Facts, State


def answer_counts(answer, it):
    """Whether operator ``answer`` is one ``it`` waits on: ``it`` has an open question, or is Stuck
    and the answer is newer than its Stuck (with a time unknown: only a Stuck on the operator)."""
    if it.question:
        return True
    if it.state is not State.STUCK or it.stuck is None:
        return False
    if answer.at and it.stuck_since:
        return str(answer.at) > str(it.stuck_since)
    return it.stuck.owner == 'operator'


def read_facts(ports):
    """The :class:`~asf.kernel.model.Facts` the three ports describe now."""
    record = ports.record
    items = record.items()
    prs = [p for p in ports.github.prs() if p.item_id in items]
    reviews = list(record.reviews()) + list(ports.github.reviews([p for p in prs if not p.merged]))
    answers = [a for a in record.answers()
               if a.item_id in items and answer_counts(a, items[a.item_id])]
    specs = {fid: text for fid, text in record.specs_landed().items()
             if fid in items and items[fid].state is not State.DONE}
    branches = getattr(ports.github, 'branches', None)
    pushed = [b for b in (branches() if branches else []) if b.item_id in items]
    return Facts(items=items, prs=prs, sessions=list(ports.sessions.sessions()), reviews=reviews,
                 answers=answers, specs_landed=specs, paused=record.paused(), branches=pushed)
