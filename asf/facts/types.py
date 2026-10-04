"""asf.facts.types — what a fact is: an answer that says when it was read, or that it could not be.

A decider used to read the host and the trunk through helpers that turned a failure into ``[]``,
``''``, ``False`` or ``None`` — a value that reads exactly like "nothing landed", "no PR open",
"no session alive". A fact cannot be mistaken that way: it is one of the dataclasses below, every
one of them carries :class:`AsOf` (the head it was read against and the clock it was read at),
and when the answer could not be read it is :class:`Unknown` with the reason — never a default.

The review verdict is not here: its authority is the verdict block reader
(:mod:`asf.evidence.review` ``Verdict``/``Stale``, head-bound, carried across a rebase by patch
id), and a second type for it would be a second authority.
"""
import datetime
from dataclasses import dataclass, field

STAMP = '%Y-%m-%dT%H:%M:%SZ'


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime(STAMP)


@dataclass(frozen=True)
class AsOf:
    """When a fact was read: ``head`` — the sha (or ref tip) it was read against, ``''`` when it
    is not head-bound — and ``at``, the UTC clock (``…Z``)."""
    head: str = ''
    at: str = ''

    @classmethod
    def now(cls, head=''):
        return cls(head or '', now_iso())


@dataclass(frozen=True)
class Unknown:
    """The answer could not be read: ``reason`` says why (``'rc 1: …'``, ``'timeout'``, ``'rate
    limited'``, ``'not read this tick'`` …). A decider holds on it — it never closes, launches or
    lands on an Unknown."""
    reason: str
    as_of: AsOf


@dataclass(frozen=True)
class Landed:
    """The item's work is on the trunk at ``sha``; ``by`` says which rule attributed it
    (``pr-merge``, ``names``, ``trunkclose/<arm>``)."""
    sha: str
    by: str
    as_of: AsOf


@dataclass(frozen=True)
class NotLanded:
    """The item's work is not on the trunk: ``why``. ``hint_sha`` — a trunk commit that covers
    the item's ``writes:`` — is a hint for a reader, never a landing."""
    why: str
    as_of: AsOf
    hint_sha: str = ''


@dataclass(frozen=True)
class Alive:
    """A session's process is running as ``pid``."""
    pid: int
    as_of: AsOf


@dataclass(frozen=True)
class Dead:
    """A session's process is gone (or its pid was reused) — seen ``since``."""
    since: str
    as_of: AsOf


@dataclass(frozen=True)
class OpenPrs:
    """The product's open PRs as the host listed them: ``prs``, a tuple of ``{number,
    headRefName, headRefOid, title}``. An empty tuple is a real "none open"."""
    prs: tuple = field(default_factory=tuple)
    as_of: AsOf = field(default_factory=AsOf)

    def by_head(self, branch):
        """The open PR whose head is ``branch``, or None (none open for it)."""
        return next((p for p in self.prs if p.get('headRefName') == branch), None)


def is_unknown(fact):
    """True when ``fact`` is :class:`Unknown`."""
    return isinstance(fact, Unknown)
