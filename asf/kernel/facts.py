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
The still-needed gate's probe (:mod:`asf.kernel.needed_probe`, after the question probes, so the
two share the trunk probe's per-tick cap) fills ``Facts.needed`` and ``Facts.landed_shas``; the
repo's queued and running CI runs are read before the PRs (``Facts.ci_runs``) and every open PR's
head after (``Facts.open_heads``, off the port's open listing).

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


def read_resolved(ports, items, sessions, prs=()):
    """``Facts.resolved``: for every probe :func:`asf.kernel.resolvers.match` finds in the
    :func:`open_questions` (a ``gate`` one on :func:`asf.kernel.resolvers.branch_of`), the host's
    trunk probe (``ports.trunk``, :mod:`asf.kernel.trunk`) or, for ``inbox-bug``, the record's
    intake card (``record.inbox_filed``: ``{filed: path or ''}``); ``{}`` when none matches, the
    port is missing, or it fails."""
    probes = []
    for iid, text in open_questions(items, sessions):
        p = resolvers.match(text, resolvers.branch_of(iid, sessions, prs))
        if p is not None and p not in probes:
            probes.append(p)
    out = {}
    probe = getattr(getattr(ports, 'trunk', None), 'probe', None)
    run = [p for p in probes if p.cls in resolvers.PROBED]
    if probe and run:
        try:
            out.update(probe(run) or {})
        except Exception:  # noqa: BLE001 — an unprobed question stays with the operator
            pass
    filed = getattr(ports.record, 'inbox_filed', None)
    bugs = [p for p in probes if p.cls == resolvers.INBOX_BUG]
    if filed and bugs:
        try:
            got = filed([p.target[0] for p in bugs]) or {}
            out.update({p.key: {'filed': got[p.target[0]]} for p in bugs if p.target[0] in got})
        except Exception:  # noqa: BLE001
            pass
    return out


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


def read_seats(ports):
    """The seats the session port can launch on now (its ``capacity``), or None when it cannot
    say (no such method, or the read failed): ``decide`` then keeps ``Config.max_sessions``."""
    capacity = getattr(ports.sessions, 'capacity', None)
    if capacity is None:
        return None
    try:
        return int(capacity())
    except Exception:  # noqa: BLE001 — an unread capacity never stops the tick
        return None


def read_runs(ports):
    """``Facts.ci_runs``: the repo's queued and running workflow runs
    (:meth:`asf.kernel.ports.RealGitHub.active_runs`), read *before* the PRs — a run's PR then
    open and missing from the listing after is closed for certain; ``[]`` when the port has no
    such read, is slowed by the rate guard, or fails."""
    from asf import gh_limit
    from asf.kernel.ports import PortError
    read = getattr(ports.github, 'active_runs', None)
    if read is None:
        return []
    try:
        return list(read() or [])
    except (PortError, OSError, gh_limit.RateLimited):
        return []


def read_needed(ports, items, prs, sessions):
    """``(Facts.needed, Facts.landed_shas)`` off the still-needed gate's probe
    (:class:`asf.kernel.needed_probe.NeededProbe` over ``ports.trunk``); empty when the ports have
    no trunk probe or a read fails (every item then launches as before)."""
    trunk = getattr(ports, 'trunk', None)
    if trunk is None or not hasattr(trunk, 'repo'):
        return {}, {}
    from asf.kernel.needed_probe import NeededProbe
    probe = NeededProbe(trunk)
    try:
        needed = probe.read(items, prs, sessions)
    except Exception:  # noqa: BLE001 — an unprobed item launches as before
        needed = {}
    try:
        landed = probe.landed(sessions)
    except Exception:  # noqa: BLE001
        landed = {}
    return needed, landed


def read_facts(ports):
    """The :class:`~asf.kernel.model.Facts` the three ports describe now."""
    record = ports.record
    items = record.items()
    orphans = []
    runs = read_runs(ports)
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
    seats = read_seats(ports)
    look_bad = getattr(record, 'unreadable', None)
    strict, main = read_trunk(ports) if not github_error else (True, [])
    resolved = read_resolved(ports, items, sessions, prs)
    needed, landed = read_needed(ports, items, prs, sessions) if not github_error else ({}, {})
    heads = getattr(ports.github, 'open_heads', None) if not github_error else None
    return Facts(unreadable=dict(look_bad() or {}) if look_bad else {},
                 items=items, prs=prs, sessions=sessions, reviews=reviews,
                 answers=answers, specs_landed=specs, paused=record.paused(), branches=pushed,
                 stranded=stranded, id_claims=read_id_claims(record, items, sessions),
                 resolved=resolved, needed=needed, landed_shas=landed, ci_runs=runs,
                 open_heads=dict(heads) if isinstance(heads, dict) else None,
                 github_error=github_error, orphan_prs=orphans, strict=strict, main=main,
                 seats=seats,
                 now=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
