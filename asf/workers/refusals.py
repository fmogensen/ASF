"""asf.workers.refusals — what ASF itself refused, and the one sentence that says so.

A run that ends without its report is recorded by what the factory could not find ("without the
report commit"), never by what stopped the session. When what stopped it was a refusal ASF made
— the naming check, a pre-push check, a protected ref — that refusal was written down at the
moment it was made, in four places, and read back into nothing.

This module is the one owner of a refusal's kind, its one-line text and the clause a dead reason
carries. Its four sources:

* the session's own refusals, appended by the hook shim to ``<state>/<p>/gates/<job>.refusals``
  (``ASF_REFUSAL_LOG``, cleared per launch beside ``ASF_PUSH_LOG``) — the push-allow refusal, the
  product's own ``pre-push`` hook, ``asf trunk-check --pre-push``;
* ``run['publish_refused']`` — the factory's own publish refusal (refguard, a redaction finding,
  the repo's hook), already on the registry line;
* ``run['correction']`` whose ``kind`` is one of :data:`CORRECTION_KINDS` — the lane's refusal of
  this run's branch, with its own ``at``;
* a cloud run's log, through :func:`recognise` — the one source that costs an API call, read only
  when the three above answer nothing (a cloud session runs no shim: F-0266 P9).

Only the dead run's own refusals are ever read: a refusal earned by the run before it is that
run's dead reason, not this one's.
"""
import datetime
import json
import os
import re
import time
from typing import NamedTuple

from asf import env
from asf.workers import lifecycle

#: A refusal's kind. Each is the constant that already owns its string, so a rename follows.
NAMING = lifecycle.NAMING                    # 'naming'
PRE_PUSH = lifecycle.HOOK_REFUSED            # 'hook refused'
REFGUARD = 'refguard'
PUSH_ALLOW = 'push-allow'
REDACTION = 'redaction'
KINDS = (NAMING, PRE_PUSH, REFGUARD, PUSH_ALLOW, REDACTION)

#: The correction kinds that are a refusal ASF made at the door, not a finding about the work (C6).
CORRECTION_KINDS = (lifecycle.NAMING, lifecycle.COPIES, 'merge', 'conflict',
                    lifecycle.HOOK_REFUSED, lifecycle.INCOMPLETE)

#: One line of a dead reason, at most.
CLAUSE_MAX = 200
#: The words that open the clause — one owner, so a reader can tell a reason already carries one.
CLAUSE_HEAD = 'last ASF refusal'
#: The ledger directory under ``env.state_dir(product)`` — :mod:`asf.workers.pushlog`'s own.
GATES_DIR = 'gates'

#: ``(kind, pattern)`` in the order :func:`recognise` tries them on one line: ``pre-push`` is the
#: widest and goes last, so a ``REF GUARD`` line that also says "push" files as ``refguard``.
_PATTERNS = (
    (PUSH_ALLOW, re.compile(r'asf: push refused — an ASF session pushes only to factory branches')),
    (REFGUARD, re.compile(r'REF GUARD(?: \(warn\))?: refused')),
    (NAMING, re.compile(r'commits do not name ')),
    (REDACTION, re.compile(r'redact: \S+:\d+ |REDACTION REFUSED')),
)


class Refusal(NamedTuple):
    kind: str        #: one of KINDS (or a CORRECTION_KINDS kind off the record)
    line: str        #: the refusal's own first line, never this module's wording
    at: str = ''     #: ISO-8601, '' when the source carries none
    where: str = ''  #: 'ledger' | 'publish' | 'correction' | 'run log'


def path(product, job):
    """``<state>/<p>/gates/<job>.refusals``; ``job`` through :func:`os.path.basename`, as
    :func:`asf.workers.pushlog.path`, so no value can walk out of :data:`GATES_DIR`."""
    return os.path.join(env.state_dir(product), GATES_DIR,
                        f'{os.path.basename(job or "")}.refusals')


def env_for(product, job):
    """``{'ASF_REFUSAL_LOG': path(...)}`` — the session environment the hook shim appends to."""
    p = path(product, job)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return {'ASF_REFUSAL_LOG': p}


def clear(product, job):
    """Remove ``job``'s ledger at launch, so the file holds one run's refusals. Never raises."""
    try:
        os.remove(path(product, job))
    except OSError:
        pass


def _first_line(text):
    for line in str(text or '').splitlines():
        if line.strip():
            return line.strip()
    return ''


def ledger(product, job):
    """Every :class:`Refusal` the ledger holds, oldest first; ``[]`` for no file. A line it cannot
    parse is dropped — a hook's output is not a contract."""
    try:
        with open(path(product, job), encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not rec.get('kind'):
            continue
        line = _first_line(rec.get('line'))
        kind = str(rec['kind'])
        if kind == PRE_PUSH:  # the shim files every hook exit as one kind: name the door it was
            seen = recognise(rec.get('line'))
            if seen is not None:
                kind = seen.kind
        out.append(Refusal(kind, line, str(rec.get('at') or ''), 'ledger'))
    return out


def _kind_of_text(text):
    """The kind a factory refusal's text files under, or None when it is none of :data:`KINDS`."""
    seen = recognise(text)
    if seen is not None:
        return seen.kind
    if lifecycle.push_failure(text) == lifecycle.HOOK_REFUSED:
        return PRE_PUSH
    return None


def from_record(run):
    """The Refusals on a registry line: its ``publish_refused`` and a :data:`CORRECTION_KINDS`
    ``correction`` — read, never re-written into the ledger (C5)."""
    run = run or {}
    out = []
    pub = run.get('publish_refused')
    if pub:
        kind = _kind_of_text(pub)
        if kind:
            out.append(Refusal(kind, _first_line(pub), str(run.get('ended') or ''), 'publish'))
    corr = run.get('correction')
    if isinstance(corr, dict) and corr.get('kind') in CORRECTION_KINDS:
        kind = corr['kind']
        if kind == PRE_PUSH:
            kind = _kind_of_text(corr.get('text')) or PRE_PUSH
        out.append(Refusal(kind, _first_line(corr.get('text')), str(corr.get('at') or ''),
                           'correction'))
    return out


def recognise(text):
    """The LAST :class:`Refusal` in a blob of session or git output, or None (C12)."""
    for line in reversed(str(text or '').splitlines()):
        for kind, pat in _PATTERNS:
            if pat.search(line):
                return Refusal(kind, line.strip(), '', 'run log')
        if lifecycle.push_failure(line) == lifecycle.HOOK_REFUSED:
            return Refusal(PRE_PUSH, line.strip(), '', 'run log')
    return None


def _ts(at):
    try:
        return datetime.datetime.fromisoformat(str(at).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def last(product, job, run=None, text=None):
    """The newest Refusal of this run, by ``at`` (a Refusal with no ``at`` sorts oldest), out of
    the ledger, the record and — only when both are empty and ``text`` is given — ``text``."""
    found = (ledger(product, job) if product and job else []) + from_record(run)
    if not found:
        return recognise(text) if text else None
    return max(enumerate(found), key=lambda ir: (_ts(ir[1].at) or 0.0, ir[0]))[1]


def clause(refusal, now=None):
    """`` — last ASF refusal (<kind>, <n>m before): <line>``, cut to :data:`CLAUSE_MAX` on its
    first line; ``''`` for None. ``now`` (epoch seconds or ISO) is the moment the minutes count
    back from: the wall clock when not given."""
    if refusal is None:
        return ''
    when = refusal.kind
    at = _ts(refusal.at)
    if at is not None:
        ref = _ts(now) if isinstance(now, str) else now
        ref = time.time() if ref is None else ref
        when += f', {max(0, int((ref - at) // 60))}m before'
    text = f' — {CLAUSE_HEAD} ({when}): {_first_line(refusal.line)}'
    return text if len(text) <= CLAUSE_MAX else text[:CLAUSE_MAX - 1] + '…'


def dead_reason(run, product=None, text=None):
    """``run['dead_why']`` plus :func:`clause` of :func:`last`, composed now (C14): a refusal the
    lane records in a later pass than the one that ended the run still appears. ``''`` when the
    run carries no ``dead_why``."""
    run = run or {}
    why = run.get('dead_why') or ''
    if not why:
        return ''
    if CLAUSE_HEAD in why:  # the sync already named it
        return why
    return why + clause(last(product, run.get('job'), run, text), now=run.get('ended'))
