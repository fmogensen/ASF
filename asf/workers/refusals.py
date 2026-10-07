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
from typing import NamedTuple

from asf import env
from asf.workers import cloudpid, lifecycle, pushlog

#: A refusal's kind. Each is the constant that already owns its string, so a rename there follows.
NAMING = lifecycle.NAMING                    # 'naming'
PRE_PUSH = lifecycle.HOOK_REFUSED            # 'hook refused'
REFGUARD = 'refguard'
PUSH_ALLOW = 'push-allow'
REDACTION = 'redaction'
KINDS = (NAMING, PRE_PUSH, REFGUARD, PUSH_ALLOW, REDACTION)

#: The correction kinds that are a refusal ASF made at the door, not a finding about the work
#: (F-0266 C6). ``'gate'`` is the lane's hold when the product's own pre-push check fails on the
#: transplant (asf/harvest/lane.py, "the pre-push check fails on the transplant"); ``'merge'`` is
#: its refusal of a merge commit on a lane branch ("merge commit on a lane branch", lane.py's
#: ``lane_refusal``) — neither has a constant of its own to alias (F-0266 PD6).
CORRECTION_KINDS = (lifecycle.NAMING, lifecycle.COPIES, 'merge', 'conflict', 'gate',
                    lifecycle.HOOK_REFUSED, lifecycle.INCOMPLETE)

#: One line of a dead reason, at most.
CLAUSE_MAX = 200

#: A pattern a ``REF GUARD: refused …`` line is recognised by, and only that head: a
#: ``REF GUARD (warn): …`` line is a push that proceeded, never a refusal (F-0266 PD5), and a
#: coder "fixing" this to match it too would file a successful push as a run's dead reason.
_REFGUARD_RE = re.compile(r'REF GUARD: refused ')
#: The push-allow refusal's own text (asf/workers/githooks.py), the head every variant shares —
#: the branches it names follow, word-split, and differ push to push.
_PUSH_ALLOW_TEXT = 'asf: push refused — an ASF session pushes only to factory branches'
_ISO = '%Y-%m-%dT%H:%M:%SZ'


class Refusal(NamedTuple):
    kind: str        #: one of :data:`KINDS`, or a correction's own kind (:data:`CORRECTION_KINDS`)
    line: str         #: the refusal's own first line, never this module's wording
    at: str = ''      #: ISO-8601, '' when the source carries none
    where: str = ''   #: 'ledger' | 'publish' | 'correction' | 'run log'


def path(product, job):
    """``<state>/<p>/gates/<job>.refusals`` (:data:`asf.workers.pushlog.PUSHES_DIR`, pushlog's own
    shape, F-0266 P14) — ``job`` through :func:`os.path.basename`, so no value can walk the path
    outside that directory."""
    return os.path.join(env.state_dir(product), pushlog.PUSHES_DIR,
                        f'{os.path.basename(job or "")}.refusals')


def env_for(product, job):
    """``{'ASF_REFUSAL_LOG': path(...)}``; makes the directory."""
    p = path(product, job)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return {'ASF_REFUSAL_LOG': p}


def clear(product, job):
    """Remove the file at launch, so it holds one run's refusals. Never raises."""
    try:
        os.remove(path(product, job))
    except OSError:
        pass


def ledger(product, job):
    """Every :class:`Refusal` the file holds, oldest first; ``[]`` for no file. A line that is not
    a JSON object with a ``kind`` and a ``line`` is dropped rather than raised on: a hook's output
    is not a contract."""
    out = []
    try:
        with open(path(product, job), encoding='utf-8') as f:
            lines = list(f)
    except OSError:
        return out
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if not (isinstance(rec, dict) and rec.get('kind') and rec.get('line')):
            continue
        out.append(Refusal(kind=rec['kind'], line=rec['line'], at=rec.get('at') or '',
                           where='ledger'))
    return out


def from_record(run):
    """The Refusals already on ``run``, a registry line: ``run['publish_refused']`` and a
    :data:`CORRECTION_KINDS` correction. In no particular order; both are per-run fields and need
    no window (F-0266 PD3 — unlike the ledger, which is per job)."""
    run = run or {}
    out = []
    line = run.get('publish_refused')
    if line:
        text = line.split('refused: ', 1)[-1] if 'refused: ' in line else line
        if _REFGUARD_RE.search(text):
            kind = REFGUARD
        elif lifecycle.REDACT_REFUSAL_RE.search(text) or lifecycle.HOOK_REDACTION_RE.search(text):
            kind = REDACTION
        elif lifecycle.push_failure(text) == lifecycle.HOOK_REFUSED:
            kind = PRE_PUSH
        else:
            kind = None
        if kind:
            out.append(Refusal(kind=kind, line=text.split('\n', 1)[0], where='publish'))
    corr = run.get('correction')
    if isinstance(corr, dict) and corr.get('kind') in CORRECTION_KINDS:
        text = str(corr.get('text') or '')
        out.append(Refusal(kind=corr['kind'], line=text.split('\n', 1)[0],
                           at=corr.get('at') or '', where='correction'))
    return out


def recognise(text):
    """The LAST :class:`Refusal` in a blob of session or git output, or ``None``: the lines are
    scanned from the end and the first hit is returned (F-0266 C12 — the card asks for the last
    refusal, and a list in a one-line reason is unreadable).

    The patterns, one per kind, tried in this order per line: ``push-allow``; ``refguard`` on
    ``REF GUARD: refused `` and nothing else — a ``REF GUARD (warn): …`` line is a push that
    proceeded, never a refusal (F-0266 PD5); ``naming``; ``redaction``; and ``pre-push`` last,
    because it is the widest pattern and a ``REF GUARD`` line that also says "push" must file as
    ``refguard``, not ``pre-push``.

    Two limits worth knowing (F-0266 PD7): a refusal buried deep in a long remote-run summary is
    not found through :func:`asf.workers.remote.run_log_summary`'s 160-character-per-event cut,
    and :data:`asf.workers.lifecycle.REDACT_REFUSAL_RE`'s ``(?:^|; )`` anchor matches only at the
    start of such a summary once its newlines are collapsed — ``redaction`` is reliably recognised
    from the ledger and from ``publish_refused``, not from a remote summary."""
    for line in reversed((text or '').splitlines()):
        line = line.strip()
        if not line:
            continue
        if _PUSH_ALLOW_TEXT in line:
            return Refusal(kind=PUSH_ALLOW, line=line, where='run log')
        if _REFGUARD_RE.search(line):
            return Refusal(kind=REFGUARD, line=line, where='run log')
        if 'commits do not name ' in line:
            return Refusal(kind=NAMING, line=line, where='run log')
        if lifecycle.REDACT_REFUSAL_RE.search(line) or 'REDACTION REFUSED' in line:
            return Refusal(kind=REDACTION, line=line, where='run log')
        if lifecycle.push_failure(line) == lifecycle.HOOK_REFUSED:
            return Refusal(kind=PRE_PUSH, line=line, where='run log')
    return None


def last(product, job, run=None, text=None):
    """The newest :class:`Refusal` of **this run**, by ``at`` (a Refusal with no ``at`` sorts
    oldest), out of the ledger, the record and — only when both are empty and ``text`` is given —
    ``text``.

    The ledger is filtered to the run's own window (F-0266 PD3: the file is per job, not per run,
    so an unfiltered read would attribute one job's *current* ledger to every historical dead run
    of it — the cross-run attribution C7 forbids): a Refusal whose ``at`` is ``>= run['started']``
    and, when the run has ended, ``<= run['ended']`` is this run's; with no ``run``, or a run with
    no ``started``, the ledger is read as it is (the live-run case, the only run the file can
    belong to)."""
    entries = ledger(product, job)
    started = (run or {}).get('started')
    if started:
        ended = (run or {}).get('ended')
        entries = [r for r in entries if r.at >= started and (not ended or r.at <= ended)]
    if run is not None:
        entries = entries + from_record(run)
    if not entries and text:
        found = recognise(text)
        entries = [found] if found else []
    if not entries:
        return None
    return max(entries, key=lambda r: r.at or '')


def _parse(stamp):
    return datetime.datetime.strptime(stamp, _ISO)


def clause(refusal, now=None):
    """`` — last ASF refusal (<kind>, <n>m before): <line>``, cut to :data:`CLAUSE_MAX`; ``''``
    for ``None``. ``<n>`` is the whole minutes between the Refusal's ``at`` and ``now`` (default:
    now), and the ``, <n>m before`` part is omitted when the Refusal carries no ``at`` — never a
    fabricated ``0m``."""
    if refusal is None:
        return ''
    line = (refusal.line or '').split('\n', 1)[0]
    age = ''
    if refusal.at:
        when = now or lifecycle.now_iso_utc()
        try:
            minutes = max(0, int((_parse(when) - _parse(refusal.at)).total_seconds() // 60))
            age = f', {minutes}m before'
        except ValueError:
            age = ''
    out = f' — last ASF refusal ({refusal.kind}{age}): {line}'
    return out[:CLAUSE_MAX]


def dead_reason(run, product=None, text=None):
    """``run['dead_why']`` (or, when absent, :func:`asf.workers.cloudpid.why` of its ``pid`` —
    F-0266 PD2: the Dead group of ``asf sessions`` can never carry ``dead_why``, since health
    writes it in the same call that takes a run out of that group, so the live field it is a copy
    of is read instead) plus :func:`clause` of :func:`last`, composed now (F-0266 C14 — the lane
    may record a correction in a pass later than the one that ended the run, so a stored sentence
    would be permanently refusal-free). ``''`` when the run carries neither."""
    run = run or {}
    why = run.get('dead_why') or cloudpid.why(run.get('pid'))
    if not why:
        return ''
    if product is None:
        return why
    return why + clause(last(product, run.get('job'), run, text))
