"""asf.kernel.needed — the still-needed gate before a fresh launch, and the stale-CI sweep (ASF 0.2).

The operator's question: "is there validation of what already exists before we take a story and
start implementing it?" Stage one of that check, code only (never an LLM guess), on the facts
:mod:`asf.kernel.needed_probe` reads each tick:

- **Already satisfied** (``config.needed_satisfied``). Before every launch of a Ready Task or
  Bug (:func:`fresh`: a relaunch after an answer too) the tests its card names as its proof
  (:func:`test_ids`: ``proves:`` lines, test ids in its Acceptance, a Bug's ``Fixes:`` lines and
  ``signature`` — never its plan's lines, which drift) are probed: the ids that did *not* exist
  when the item was planned (its plan document's first commit on the trunk, else the trunk at its
  creation date) are run on a fresh detached worktree of origin's trunk
  (:class:`asf.kernel.trunk.TrunkProbe`: the question resolvers' per-tick cap and cache). All
  green: the item is Done with the note ``satisfied on main at <sha>: <tests>`` and nothing
  launches (:class:`~asf.kernel.actions.Satisfied`, logged ``SATISFIED <item>``); not run yet:
  the launch waits a tick (:data:`PENDING`). A test that existed before the plan proves nothing
  and is never counted; a missing, red or unloadable one launches the item as before.
- **Found done by its session** (:func:`take_done`). A build REPORT ``status: done`` with no push
  that names a sha the trunk holds and passing tests is Done the same way — no empty commit, PR
  or review.
- **Empty PR**. An open kernel PR that changes nothing (:func:`empty_pr`, its CI reported) is
  closed: Done when the item's probe is satisfied, else Ready with :data:`EMPTY_FINDING`.
- **Old CI** (``config.cancel_stale_ci``). A queued or running ``pull_request`` run
  (``Facts.ci_runs``, read before the PRs) whose pull requests are all closed — or closed by the
  kernel this tick — or whose head is no longer its open PR's head, is cancelled
  (:class:`~asf.kernel.actions.CancelRun`); a run tied to no PR, or a push run, is never touched.

Pure: nothing here reads a disk, a network or a clock.
"""
import dataclasses
import re

from asf.kernel import actions as A
from asf.kernel import resolvers
from asf.kernel.model import State

#: the item types the satisfied check takes
GATED = ('task', 'bug')

#: the most cancels one tick asks for
MAX_CANCELS = 20

#: a ``proves:`` line (a card body's, or a plan Task's)
PROVES_RE = re.compile(r'^\s*(?:[-*]\s*)?proves:\s*(.+)$', re.I | re.M)
#: a Bug's ``Fixes:`` line
FIXES_RE = re.compile(r'^\s*(?:[-*]\s*)?Fixes:\s*(.+)$', re.M)
#: a test module path, with an optional ``::Class`` (``tests/test_x.py::XTests``)
PATH_CLASS_RE = re.compile(r'(?<![\w./-])((?:[\w-]+/)*test_\w+\.py)(?:::(\w+))?')
#: a ``## Acceptance`` section up to the next heading
ACCEPTANCE_RE = re.compile(r'^##\s+Acceptance\s*$(.*?)(?=^##\s|\Z)', re.M | re.S)


def module_of(path):
    """The dotted module of a test file path (``tests/kernel/test_x.py`` ->
    ``tests.kernel.test_x``)."""
    return path[:-3].replace('/', '.') if path.endswith('.py') else path


def _acceptance(text):
    return '\n'.join(m.group(1) for m in ACCEPTANCE_RE.finditer(text or ''))


def test_ids(item, items=None):
    """The test ids ``item``'s card names as the proof of its work — never its plan document's
    lines, which drift: every dotted test id (or test path with ``::Class`` parts) on a
    ``proves:`` line, in its ``## Acceptance`` (and its declared Stories'), and for a Bug on a
    ``Fixes:`` line or in its ``signature``. An id another one extends is dropped; ``[]`` when
    there is none or more than :data:`asf.kernel.resolvers.MAX_IDS`."""
    items = items or {}
    body = item.body or ''
    texts = [m.group(1) for m in PROVES_RE.finditer(body)] + [_acceptance(body)]
    texts += [_acceptance(items[s].body) for s in item.stories if s in items]
    if item.type == 'bug':
        texts += [m.group(1) for m in FIXES_RE.finditer(body)] + [item.signature or '']
    ids = []
    for text in texts:
        ids += resolvers.TEST_ID_RE.findall(text)
        for path, cls in PATH_CLASS_RE.findall(text):
            ids.append('%s.%s' % (module_of(path), cls) if cls else module_of(path))
    ids = list(dict.fromkeys(ids))
    ids = [i for i in ids if not any(o != i and o.startswith(i + '.') for o in ids)]
    return ids if 0 < len(ids) <= resolvers.MAX_IDS else []


def _lineage_later(iid, items, seen=()):
    it = items.get(iid)
    if it is None or iid in seen:
        return False
    return it.priority == 'later' or (bool(it.parent)
                                      and _lineage_later(it.parent, items, seen + (iid,)))


def fresh(it, items, prs=(), sessions=()):
    """Whether ``it`` is a launch the gate checks first — every launch of a Ready item, a
    relaunch after an answer included: a Task or Bug, New or Ready on its card, not parked or
    retired, not reopened, with no open PR and no live session."""
    return (it.type in GATED and it.state in (State.NEW, State.READY)
            and not _lineage_later(it.id, items) and not it.reopened
            and not any(p.item_id == it.id and not p.merged for p in prs)
            and not any(s.item_id == it.id and s.alive for s in sessions))


def candidates(items, prs=(), sessions=(), inherit=True):
    """The :func:`fresh` items and the items of an open PR that changes nothing
    (:func:`empty_pr`), in launch order (:func:`asf.kernel.decide.launch_order`): the ones the
    next free seat takes are probed first."""
    from asf.kernel.decide import launch_order
    empty = {p.item_id for p in prs if empty_pr(p)}
    out = [iid for iid, it in items.items()
           if fresh(it, items, prs, sessions) or (iid in empty and it.type in GATED)]
    return sorted(out, key=lambda iid: launch_order(iid, items, inherit))


def empty_pr(pr):
    """Whether ``pr`` is open and changes nothing (GitHub lists no file: a 0/0 diff)."""
    return not pr.merged and not pr.files and bool(pr.head_sha)


def satisfied(probe):
    """``(sha, tests)`` when an item's probe (``Facts.needed[iid]``: ``{ids, tests}``) says every
    new test it names ran green on the trunk, else None."""
    if not probe:
        return None
    result, ids = probe.get('tests') or {}, list(probe.get('ids') or [])
    if not ids or result.get('error') or not result.get('ok') or result.get('failed'):
        return None
    if not int(result.get('ran') or 0) or not result.get('sha'):
        return None
    return str(result['sha']), ids


def note(sha, tests):
    """The card note of a satisfied item."""
    return 'satisfied on main at %s: %s' % (str(sha)[:9], ', '.join(tests))


def gate_line(rec):
    """``<VERDICT> <item>: <why>`` of one :attr:`asf.kernel.actions.Plan.gate` record."""
    return '%s %s%s' % (rec['verdict'], rec['item'], ': %s' % rec['why'] if rec.get('why') else '')


#: a sha a done REPORT names (its ``pushed:``, ``commits:``, ``tests:`` … lines)
SHA_RE = re.compile(r'(?<![0-9a-fA-F])([0-9a-f]{7,40})(?![0-9a-fA-F])')

#: the plan note of a Ready item held for its proving-tests check this tick
PENDING = 'waits for its proving-tests check on main'

#: the finding an empty PR's item is relaunched with when its tests are not green on main
EMPTY_FINDING = ('PR #%d changed nothing (an empty diff) and the card\'s proving tests are not '
                 'green on main: make the change the card asks for, or name the landed sha and the '
                 'passing tests in a done REPORT with pushed: no')


def report_shas(s):
    """The shas ended session ``s``'s REPORT names, in order."""
    text = ' '.join(str(v) for v in (s.fields or {}).values())
    return list(dict.fromkeys(SHA_RE.findall(text)))


def _done_unchanged(s, facts):
    """``(sha, tests)`` when ended build session ``s`` reported ``done``, claimed no push, named a
    sha the trunk holds (``Facts.landed_shas``) and passing tests: its item was already done on
    main. None otherwise."""
    from asf.kernel import reports as R
    if not s.ended or s.alive or s.kind == 'review' or s.status != R.DONE:
        return None
    fields = s.fields or {}
    if R.pushed_claim(fields.get('pushed'))[0] or s.result == 'pushed':
        return None
    tests = ' '.join(str(fields.get('tests') or '').split())
    if not tests or re.match(r'^(?:none|n/a|-)\b', tests, re.I) or R._failing(tests):
        return None
    landed = facts.landed_shas or {}
    sha = next((landed[x] for x in report_shas(s) if landed.get(x)), '')
    if not sha:
        return None
    named = list(dict.fromkeys(resolvers.TEST_ID_RE.findall(tests)))
    return sha, named or [R.cap(tests, 160)]


def take_done(facts, config):
    """``(facts, actions, {item: (sha, tests)})``: every ended build session that found its item
    already done on main (:func:`_done_unchanged`, no open PR) taken out of the facts ``decide``
    judges, with a :class:`~asf.kernel.actions.Satisfied` — no empty commit, PR or review."""
    if not config.needed_satisfied:
        return facts, [], {}
    found = {}
    for s in facts.sessions:
        it = facts.items.get(s.item_id)
        if it is None or it.type not in GATED or s.item_id in found:
            continue
        if any(p.item_id == s.item_id and not p.merged for p in facts.prs):
            continue
        got = _done_unchanged(s, facts)
        if got:
            found[s.item_id] = got
    if not found:
        return facts, [], {}
    keep = [s for s in facts.sessions if not (s.item_id in found and not s.alive)]
    actions = [A.Satisfied(iid, sha, tests) for iid, (sha, tests) in sorted(found.items())]
    return dataclasses.replace(facts, sessions=keep), actions, found


def judge(facts, config, judged, make, done=None):
    """``(actions, gate lines, notes)`` of the satisfied check on ``judged`` (``make(state,
    hold=…)`` builds a judged value):

    - an item :func:`take_done` found done is Done;
    - a :func:`fresh` item judged Ready whose probe is :func:`satisfied` is Done
      (:class:`~asf.kernel.actions.Satisfied`); one whose named new tests are not run yet is held
      a tick (:data:`PENDING`);
    - the kernel PR of an item that changes nothing (:func:`empty_pr`, its CI reported, no live
      session) is closed: Done when the item's probe is satisfied, else Ready with
      :data:`EMPTY_FINDING` (a relaunch marker)."""
    from asf.kernel.decide import RELAUNCH, kernel_owned
    actions, lines, notes = [], [], {}
    if not config.needed_satisfied:
        return actions, lines, notes
    for iid, (sha, tests) in sorted((done or {}).items()):
        judged[iid] = make(State.DONE)
        lines.append({'item': iid, 'verdict': 'SATISFIED',
                      'why': 'the session found it done: ' + note(sha, tests)})
    live = {s.item_id for s in facts.sessions if s.alive}
    for p in facts.prs:
        it = facts.items.get(p.item_id)
        if (it is None or it.type not in GATED or not empty_pr(p) or p.item_id in live
                or not kernel_owned(p.branch, config, p.item_id)
                or not any(c.status == 'completed' for c in p.checks)):
            continue
        got = satisfied((facts.needed or {}).get(p.item_id))
        actions.append(A.ClosePR(p.number, p.branch, p.item_id,
                                 'it changes nothing (an empty diff)'))
        if got:
            judged[p.item_id] = make(State.DONE)
            actions.append(A.Satisfied(p.item_id, *got))
            lines.append({'item': p.item_id, 'verdict': 'SATISFIED', 'why': note(*got)})
        else:
            judged[p.item_id] = make(State.READY, hold=True)
            actions.append(A.ClearStuck(p.item_id, RELAUNCH + EMPTY_FINDING % p.number))
            lines.append({'item': p.item_id, 'verdict': 'EMPTY',
                          'why': 'PR #%d closed, relaunched' % p.number})
    for iid in sorted(facts.needed or {}):
        it, j = facts.items.get(iid), judged.get(iid)
        if it is None or j is None or j.state is not State.READY or j.hold:
            continue
        if not fresh(it, facts.items, facts.prs, facts.sessions):
            continue
        probe = facts.needed[iid]
        got = satisfied(probe)
        if got is not None:
            judged[iid] = make(State.DONE)
            actions.append(A.Satisfied(iid, *got))
            lines.append({'item': iid, 'verdict': 'SATISFIED', 'why': note(*got)})
        elif probe.get('ids') and not probe.get('tests'):
            j.hold = True
            notes.setdefault(iid, []).append(PENDING)
    return actions, lines, notes


def stale_runs(facts, config, closing=()):
    """The :class:`~asf.kernel.actions.CancelRun` of every queued or running ``pull_request`` run
    that can no longer matter: each PR it names is closed (not open on GitHub, or ``closing`` this
    tick), or none of its open PRs has its head any more. Nothing when the open heads are unread;
    at most :data:`MAX_CANCELS`."""
    heads = facts.open_heads
    if not config.cancel_stale_ci or heads is None:
        return []
    closing = set(closing)
    out = []
    for r in facts.ci_runs or ():
        if not str(r.event or '').startswith('pull_request') or not r.prs or not r.head_sha:
            continue
        live = [n for n in r.prs if n in heads and n not in closing]
        if not live:
            why = 'PR %s closed' % ', '.join('#%d' % n for n in r.prs)
        elif not any(heads[n] == r.head_sha for n in live):
            why = "head %s is no longer PR #%d's head" % (r.head_sha[:9], live[0])
        else:
            continue
        out.append(A.CancelRun(r.run_id, why))
        if len(out) >= MAX_CANCELS:
            break
    return out
