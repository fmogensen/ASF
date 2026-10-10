"""asf.kernel.facts — one tick's :class:`~asf.kernel.model.Facts`, read through the ports (ASF 0.2).

Every read happens here, once per tick, and nothing is written. Three filters keep the facts to
what ``decide`` should act on:

- an operator answer counts for an item with an open ``question`` on its card, and for a Stuck
  item of any owner when it was given after the Stuck was recorded (``Answer.at`` later than
  ``Item.stuck_since``); with either time unknown it counts only for a Stuck with ``owner:
  operator``. The answers ledger also holds the old floor's answers, already acted on, and an
  answer older than the Stuck never clears it — save one naming the item's last session
  (``sessions.last_jobs``) not yet on its card (:func:`answer_counts`);
- a PR whose item is not on the record is dropped (a branch naming a foreign id, or a card gone
  from the record); the open ones are kept apart as ``Facts.orphan_prs``, for ``decide`` to close
  those on a kernel branch;
- a landed spec counts only while its Feature's card is not Done: a finished Feature's Stories
  are history, never minted afresh (the old record never minted the Stories of its early specs).

The verdicts are the kernel's review ledger (``state/<product>/kernel-reviews.jsonl``, one row per
reviewer report, keyed by the tree it read), GitHub's own reviews on the head, and the old floor's
approvals that still name the head (:meth:`asf.kernel.ports.RealGitHub.floor_approvals`). Parked
items (``priority: later`` on the item or an ancestor) are not filtered here: ``decide`` holds
that rule, so it is the same everywhere. The pushed branches
(:meth:`asf.kernel.ports.RealGitHub.branches`) count only for items on the record. A Stuck item a
refused force-push left (:func:`asf.kernel.decide.stranded`) has its last ended session's kept
worktree read (``sessions.stranded``, when the port has it) into ``Facts.stranded``. The ids an
id-claim question cites (:func:`claim_questions`) are looked up on the record's origin
(``record.id_claims``, when the port has it) into ``Facts.id_claims``; the questions a
:mod:`asf.kernel.resolvers` class matches are probed on origin's trunk (``ports.trunk``, when
the ports have it: :mod:`asf.kernel.trunk`) into ``Facts.resolved``.

The trunk's ruleset strictness (``Facts.strict``, :meth:`asf.kernel.ports.RealGitHub.strict`:
the rules read once a tick, shared with the required checks) and its newest commits
(``Facts.main``, :meth:`asf.kernel.ports.RealGitHub.main_commits`) are read when the port has
them; an unreadable strictness is True (the merge train stays), unreadable commits are none.

A GitHub read that fails (the port has already retried a transient one) leaves no partial PR
facts: ``prs``, GitHub's reviews and the pushed branches are empty and ``Facts.github_error``
says why — ``decide`` then plans blind (:func:`asf.kernel.decide.blind_plan`).
"""
import datetime
import string

from asf.kernel import idclaims
from asf.kernel import resolvers

from asf.kernel.decide import stranded as decide_stranded
from asf.kernel.model import Facts, State


def answer_counts(answer, it, last_job=None):
    """Whether operator ``answer`` is one ``it`` waits on: ``it`` has an open question, or is Stuck
    and the answer is newer than its Stuck (with a time unknown: only a Stuck on the operator) —
    or names ``last_job``, the item's last session, and is not on the card yet: the Stuck that
    session's end led to may be recorded ticks after the operator answered it."""
    if it.question:
        return True
    if it.state is not State.STUCK or it.stuck is None:
        return False
    if answer.job and answer.job == last_job and answer.text not in it.answers:
        return True
    if answer.at and it.stuck_since:
        return str(answer.at) > str(it.stuck_since)
    return it.stuck.owner == 'operator'


def open_questions(items, sessions):
    """``[(item id, text)]``: every ended session's question, and every operator-Stuck item's
    question (else its reason), for an item on the record."""
    out = []
    for s in sessions:
        if s.ended and not s.alive and s.kind != 'review' and s.question:
            out.append((s.item_id, s.question))
    for iid in sorted(items):
        it = items[iid]
        if it.state is State.STUCK and it.stuck is not None and it.stuck.owner == 'operator':
            out.append((iid, it.question or it.stuck.reason))
    return [(iid, t) for iid, t in out if iid in items]


def claim_questions(items, sessions):
    """The :func:`open_questions` that :func:`asf.kernel.idclaims.is_claim_question`
    recognises."""
    return [(iid, t) for iid, t in open_questions(items, sessions)
            if idclaims.is_claim_question(t)]


def read_resolved(ports, items, sessions):
    """``Facts.resolved``: the host's trunk probe (``ports.trunk``, :mod:`asf.kernel.trunk`) of
    every probe :func:`asf.kernel.resolvers.match` finds in the :func:`open_questions`; ``{}``
    when none matches, the ports have no probe, or it fails."""
    probe = getattr(getattr(ports, 'trunk', None), 'probe', None)
    probes = []
    for _iid, text in open_questions(items, sessions):
        p = resolvers.match(text)
        if p is not None and p not in probes:
            probes.append(p)
    if not probe or not probes:
        return {}
    try:
        return dict(probe(probes) or {})
    except Exception:  # noqa: BLE001 — an unprobed question stays with the operator
        return {}


def read_id_claims(record, items, sessions):
    """``Facts.id_claims`` for the ids the claim questions cite (every prefix: ``decide`` narrows
    to the configured ones); ``{}`` when there is none or the port cannot read claims."""
    look = getattr(record, 'id_claims', None)
    ids = []
    for iid, text in claim_questions(items, sessions):
        for i in idclaims.cited(text, string.ascii_uppercase, exclude=(iid,)) or ():
            if i not in ids:
                ids.append(i)
    if not look or not ids:
        return {}
    try:
        return dict(look(ids) or {})
    except Exception:  # noqa: BLE001 — an unreadable claim leaves the question with the operator
        return {}


def read_github(ports, items, orphans=None):
    """``(prs, GitHub reviews, pushed branches, error)``: all three empty and ``error`` the reason
    when GitHub could not be read (a rate limit too) — never a part of them. ``orphans`` (a list,
    when given) receives the open PRs whose item is not on the record."""
    from asf import gh_limit
    from asf.kernel.ports import PortError
    try:
        every = list(ports.github.prs())
        prs = [p for p in every if p.item_id in items]
        reviews = list(ports.github.reviews([p for p in prs if not p.merged]))
        branches = getattr(ports.github, 'branches', None)
        pushed = [b for b in (branches() if branches else []) if b.item_id in items]
    except (PortError, OSError, gh_limit.RateLimited) as e:  # a blind tick, never a crash
        return [], [], [], (str(e).splitlines() or [type(e).__name__])[0]
    if orphans is not None:
        orphans += [p for p in every if p.item_id not in items and not p.merged]
    return prs, reviews, pushed, ''


def read_trunk(ports):
    """``(strict, main commits)`` off the GitHub port: True and ``[]`` for what it cannot
    read (a port without the method, a failed read)."""
    from asf import gh_limit
    from asf.kernel.ports import PortError
    gh = ports.github
    strict, main = True, []
    try:
        if hasattr(gh, 'strict'):
            strict = bool(gh.strict())
    except (PortError, OSError, gh_limit.RateLimited):
        strict = True
    try:
        if hasattr(gh, 'main_commits'):
            main = list(gh.main_commits() or [])
    except (PortError, OSError, gh_limit.RateLimited):
        main = []
    return strict, main


def read_facts(ports):
    """The :class:`~asf.kernel.model.Facts` the three ports describe now."""
    record = ports.record
    items = record.items()
    orphans = []
    prs, gh_reviews, pushed, github_error = read_github(ports, items, orphans)
    reviews = list(record.reviews()) + gh_reviews
    last_jobs = getattr(ports.sessions, 'last_jobs', None)
    last = dict(last_jobs() or {}) if last_jobs else {}
    answers = [a for a in record.answers()
               if a.item_id in items and answer_counts(a, items[a.item_id], last.get(a.item_id))]
    specs = {fid: text for fid, text in record.specs_landed().items()
             if fid in items and items[fid].state is not State.DONE}
    look = getattr(ports.sessions, 'stranded', None)
    stranded = []
    for iid in sorted(items):
        it = items[iid]
        if look and it.state is State.STUCK and decide_stranded(it.stuck):
            s = look(iid)
            if s is not None:
                stranded.append(s)
    sessions = list(ports.sessions.sessions())
    look_bad = getattr(record, 'unreadable', None)
    strict, main = read_trunk(ports) if not github_error else (True, [])
    return Facts(unreadable=dict(look_bad() or {}) if look_bad else {},
                 items=items, prs=prs, sessions=sessions, reviews=reviews,
                 answers=answers, specs_landed=specs, paused=record.paused(), branches=pushed,
                 stranded=stranded, id_claims=read_id_claims(record, items, sessions),
                 resolved=read_resolved(ports, items, sessions),
                 github_error=github_error, orphan_prs=orphans, strict=strict, main=main,
                 now=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
