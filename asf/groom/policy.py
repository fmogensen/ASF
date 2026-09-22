"""asf.groom.policy — rules as code (F-0085/D-0049): the ``approvals.groom`` gate, the open-
question grammar, and the four policies' thresholds.

Module-level imports are stdlib and :mod:`asf.env` only, so :mod:`asf.feeder.rows` may import
this module without an import cycle (the applier's side, :mod:`asf.groom.groom`, imports this
module instead — see ``POLICIES``, added once the policy pass itself lands).
"""
import re

#: D11 — deliberately timid defaults. A policy that answers wrongly is a card closed or decided
#: without anyone asking, so the first version answers only what is not really a question.
DUPLICATE_OVERLAP = 0.95
RECURRING_BUG_COUNT = 2
ADJUDICATE_ATTEMPTS = 2

#: A ``- [ ] <id> <title> — <why> → answer: ____`` line — a question no rule and no session has
#: yet answered (PD4). A barred line's slot reads ``____ (barred: …)`` instead, so it never
#: matches and is never counted open.
OPEN_QUESTION_RE = re.compile(r'^- \[ \]\s+(?P<id>[A-Z]-\d{4})\b.*→\s*answer:\s*____$')


def groom_auto(product):
    """The single gate (§2.1): ``approvals.groom: auto`` turns on the policy pass, the
    suppression pass, the adjudicate row and the digest. Unset, or anything else (``groom``,
    ``human-now``), and every behaviour this module and its callers add is off — ``asf groom``
    behaves exactly as it does today."""
    approvals = (product.approvals if product is not None else None) or {}
    return str(approvals.get('groom', '')).lower() == 'auto'


def open_questions(text):
    """``[(item_id, line)]`` for every still-open ``- [ ] <id> … → answer: ____`` line in
    ``text``, in file order (PD4) — the questions no rule has answered and no session has
    spoken for."""
    out = []
    for line in text.splitlines():
        m = OPEN_QUESTION_RE.match(line)
        if m:
            out.append((m.group('id'), line))
    return out


def _groom_config(product):
    return (product.groom if product is not None else None) or {}


def duplicate_overlap(product):
    """``groom.duplicate_overlap`` (default 0.95): ``close_exact_duplicate``'s threshold."""
    v = _groom_config(product).get('duplicate_overlap')
    return v if isinstance(v, (int, float)) else DUPLICATE_OVERLAP


def recurring_bug_count(product):
    """``groom.recurring_bug_count`` (default 2): ``decide_recurring_bug``'s threshold."""
    v = _groom_config(product).get('recurring_bug_count')
    return v if isinstance(v, int) else RECURRING_BUG_COUNT


def adjudicate_attempts(product):
    """``groom.adjudicate_attempts`` (default 2, D8): adjudicate sessions per groom date before
    the remaining questions go to the operator instead."""
    v = _groom_config(product).get('adjudicate_attempts')
    return v if isinstance(v, int) and v > 0 else ADJUDICATE_ATTEMPTS


def policy_on(product, name):
    """``groom.policies.<name>: off`` skips that policy; absent, or anything but ``off``, runs
    it."""
    policies = _groom_config(product).get('policies') or {}
    return str(policies.get(name, 'on')).lower() != 'off'
