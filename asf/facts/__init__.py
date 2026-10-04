"""asf.facts — one authority per fact, each answer with ``as_of`` and an explicit Unknown.

Several deciders answer the same question (did this item land, is that session alive) each in
its own way, and each reads an unreadable host as a "no". This package is where those answers
move to: :mod:`asf.facts.types` (the facts — every one carries :class:`AsOf`, and an answer that
could not be read is :class:`Unknown`), :mod:`asf.facts.cache` (what one pass has read, keyed by
head; the one open-PR read per pass) and :mod:`asf.facts.disagree` (the shadow's log).

A decider moves in three steps, one product flag for all of them —
``conventions.flags.facts: old | shadow | new`` (default ``old``):

* ``old``    — the decider's own answer, nothing else runs;
* ``shadow`` — both run; a disagreement is logged (:func:`asf.facts.disagree.log`) and the old
  answer is returned. Whatever the new side raises is logged as ``error:<Type>`` and never
  reaches the decider; in shadow a gh fact comes only from :mod:`asf.facts.cache`;
* ``new``    — the fact's answer.

No decider is wired here: :func:`shadow` is the seam each one calls with its current answer as
``old_fn``.
"""
from asf import gh_limit
from asf.facts import cache, disagree
from asf.facts.types import (AsOf, Alive, Dead, Landed, NotLanded, OpenPrs, Unknown,
                             is_unknown)

OLD, SHADOW, NEW = 'old', 'shadow', 'new'
MODES = (OLD, SHADOW, NEW)
DEFAULT_MODE = OLD
FLAG = 'facts'

__all__ = ['AsOf', 'Alive', 'Dead', 'Landed', 'NotLanded', 'OpenPrs', 'Unknown', 'is_unknown',
           'MODES', 'mode', 'shadow', 'cache', 'disagree']


def mode(product):
    """``conventions.flags.facts`` of ``product``: ``old`` | ``shadow`` | ``new``; unset or any
    other value is ``old``."""
    conv = getattr(product, 'conventions', product)
    reader = getattr(conv, 'flag', None)
    value = reader(FLAG, DEFAULT_MODE) if callable(reader) else DEFAULT_MODE
    value = value.strip().lower() if isinstance(value, str) else ''
    return value if value in MODES else DEFAULT_MODE


def _same(old, new):
    return old == new


def shadow(product, fact, key, old_fn, new_fn, *, decider='', same=None):
    """The decider's answer under the product's mode (see the module doc). ``same(old, new)``
    says whether the two answers agree (default ``==``) — the seam where a fact is mapped onto
    the old decider's shape. ``old_fn`` raising raises (it is the decider); ``new_fn`` raising in
    shadow is logged and swallowed — a rate limit included (the process stays latched)."""
    m = mode(product)
    if m == NEW:
        return new_fn()
    old = old_fn()
    if m == OLD:
        return old
    try:
        new = new_fn()
    except (Exception, gh_limit.RateLimited) as e:  # noqa: BLE001 — shadow never raises into old
        disagree.log(product, fact, key, old, f'error:{type(e).__name__}', decider=decider)
        return old
    try:
        agree = (same or _same)(old, new)
    except Exception as e:  # noqa: BLE001
        agree = False
        new = f'error:{type(e).__name__} (same)'
    if not agree:
        disagree.log(product, fact, key, old, new, decider=decider)
    return old
