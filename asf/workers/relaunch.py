"""asf.workers.relaunch — the one relaunch cap every launching row passes (the wave's last word).

A row the feeder emits every tick launches a session every tick unless something in between
changes. Two product loops on 2026-09-29/30 showed what "nothing changed" costs: a reshape row
launched 76 times and a delivery row 69 times, every run handed the same branch head and the
same card, every run ending ``finished`` with a report that said the work was already on the
trunk or needed a person. Each rule the feeder has (the idle-branch row, the delivery lead, the
reshape of a card with no ``writes:``) was right on its own; none of them read what the previous
run of the same row said.

So the wave asks this module before it launches: the runs of the same job (one job = one kind of
session on one item) since the item's last ``asf unpark``, newest first, while each was handed
the same **state** — the branch head it launched on, the card digest its brief was cut from and
the row's cause (its correction text) — are a streak of launches that changed nothing. At
:data:`CAP` such runs the row parks instead of launching again. A run whose own report ended
terminal (:func:`terminal`: ``status: done`` or ``blocked``, or a ``NEEDS OPERATOR:`` line) with
nothing changed after it parks at one: the session already said there is nothing more it can do.

A park is a pending correction carrying ``parked`` on the job's newest run (the same mark every
other park writes): the feeder's FIX → CORRECT row shows it ``PARKED <reason>`` in NEXT and
status, no session is launched on it, a card change or a re-plan lifts it (health), and
``asf unpark`` releases it by hand. A new commit, a new review (a commit on the branch), a card
edit or a new correction all break the streak by construction.

**One head, at most :data:`CAP` correction rounds.** A correction's cause is its text, and the
text names the review file it answers (``7-t-0042.md``, then ``8-t-0042.md``) or the red checks
of the moment — so on one unmoved head every round carried a new cause, and the streak above
broke each time (a product's ``correct-t-0042``, 2026-09-30: four launches on ``3fe323b`` in
2h40m, the causes ``7b35…``, ``7b35…``, ``a520…``, ``b0d6…``; a review session between them
filed a fresh round on the same head and wrote the next text). For the kinds in
:data:`HEAD_CAPPED` the cause is not compared: :func:`head_streak` counts the job's runs launched
on the head about to be launched on, with the same card, since the item's last unpark, and at
:data:`CAP` the row parks — only a new commit (or a card edit, or ``asf unpark``) buys a round.
"""
import hashlib
import re

from asf.workers import landing
from asf.workers import lifecycle
from asf.workers import report as report_mod

#: Launches of one job handed the same state before the row parks instead of launching again
#: (config ``worker_pool.caps.relaunch``).
CAP = 2
#: The correction kind of a relaunch-cap park.
RELAUNCH_CAP = 'relaunch cap'
#: A report status that says the session has nothing more to do on this state.
TERMINAL_STATUSES = ('done', report_mod.BLOCKED)
SHA_RE = re.compile(r'\b[0-9a-f]{7,40}\b')
#: Kinds whose launches on one head are capped whatever their cause text (:func:`head_streak`):
#: a correction round on a head that did not move is the same round again.
HEAD_CAPPED = ('correct',)


def cause_key(row_kind, correction=''):
    """A short digest of what a row launches for: its feeder kind and its correction text."""
    raw = f'{row_kind or ""}\n{correction or ""}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _same(run, head, card, cause):
    """True when ``run`` was handed the state given. A field the run's record never carried (a
    run launched before this module) or the caller cannot know (an origin branch that is gone)
    is no evidence of a change."""
    for key, want in (('launch_head', head), ('card_digest', card), ('cause', cause)):
        have = run.get(key)
        if have and want and have != want:
            return False
    return True


def streak(path, job, item, head=None, card='', cause=''):
    """The ended runs of ``job``, newest first, handed the same state as the launch about to be
    made, back to the first one that was not — and never past the item's latest unpark. The
    runs in it must also agree among themselves on the head (so a branch that moved between them
    breaks it even when the current head is unknown). A spent window's run is not counted."""
    all_runs = lifecycle.item_runs(path, item) if item else []
    since = _since_unpark(all_runs)
    rs = sorted((r for r in all_runs if r.get('job') == job and r.get('ended')
                 and not lifecycle.quota_exhausted(r) and (r.get('started') or '') > since),
                key=lambda r: r.get('started') or '', reverse=True)
    out, first_head = [], None
    for r in rs:
        if not _same(r, head, card, cause):
            break
        h = r.get('launch_head') or ''
        if first_head is None:
            first_head = h
        elif h and first_head and h != first_head:
            break
        out.append(r)
    return out


def _since_unpark(all_runs):
    return max((r.get('unparked') or '' for r in all_runs), default='')


def head_streak(path, job, item, head, card=''):
    """The ended runs of ``job`` (a :data:`HEAD_CAPPED` kind), newest first, launched on ``head``
    with the same card since the item's latest unpark — the cause not compared, since a
    correction's text changes with every review round filed on the same head. ``[]`` when the
    head is unknown: only a known head can be shown unmoved."""
    if not head or not item:
        return []
    all_runs = lifecycle.item_runs(path, item)
    since = _since_unpark(all_runs)
    out = []
    for r in all_runs:
        if r.get('job') != job or not r.get('ended') or lifecycle.quota_exhausted(r) \
                or (r.get('started') or '') <= since or r.get('kind') not in HEAD_CAPPED:
            continue
        have = r.get('launch_head') or ''
        if not have or not (have.startswith(head) or head.startswith(have)):
            continue
        if card and r.get('card_digest') and r['card_digest'] != card:
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.get('started') or '', reverse=True)


def terminal(text):
    """The report's own terminal claim, or '': its ``NEEDS OPERATOR:`` question or ``status:
    blocked`` words (:func:`asf.workers.report.needs_input`), else ``status: done`` with its
    ``left out:`` line. A report with no REPORT block, or ``status: partial``, claims nothing."""
    question = report_mod.needs_input(text)
    if question:
        return f'needs input — {question}'
    rep = report_mod.parse(text)
    status = (rep.get('status') or '').strip().lower().split(' ', 1)[0]
    if status in TERMINAL_STATUSES:
        left = (rep.get('left out') or '').strip().split('\n', 1)[0]
        return f'status {status}' + (f' — {left}' if left and left.lower() != 'none' else '')
    return ''


def _result_text(run):
    try:
        return str((lifecycle.result_of(run) or {}).get('result') or '')
    except Exception:  # noqa: BLE001 — an unreadable log claims nothing
        return ''


def on_trunk(repo, main, text, exclude=(), item='', writes=(), prs=()):
    """The first commit ``text`` names that is on ``origin/<main>`` in ``repo`` AND attributable to
    ``item`` (:func:`asf.workers.landing.attributable`: named by it, its PR's merge, or covering
    its ``writes:``) — a claim "the work is already on the trunk" checked against git, never
    taken on its word — or ''. A commit that is merely an ancestor of the trunk (the trunk head
    a branch merged in) proves nothing. ``exclude``: shas that prove nothing (the run's own
    launch head)."""
    if not repo or not text or not item:
        return ''
    skip = {s[:7] for s in exclude if s}
    for sha in dict.fromkeys(SHA_RE.findall(text)):
        if sha[:7] in skip:
            continue
        if landing.attributable(repo, main, sha, item, writes, prs):
            return sha
    return ''


#: A park reason's verified trunk evidence, read back (:func:`landed_in`).
LANDED_RE = re.compile(r'is on origin/(\S+) at ([0-9a-f]{7,40}) \(verified\)')


def landed_in(reason):
    """The sha a park ``reason`` says git verified on the trunk, or ''."""
    m = LANDED_RE.search(reason or '')
    return m.group(2) if m else ''


def verdict(path, job, item, head=None, card='', cause='', repo=None, main='main', cap=None,
            writes=(), product=None):
    """``None`` when the launch may go ahead, else the park's reason: :data:`CAP` launches of
    ``job`` on one state, or one whose report ended terminal on it (:func:`terminal`) while the
    branch still sits on the head it was handed."""
    return assess(path, job, item, head, card, cause, repo, main, cap, writes, product)[0]


def assess(path, job, item, head=None, card='', cause='', repo=None, main='main', cap=None,
           writes=(), product=None):
    """``(reason, landed)``: :func:`verdict`'s reason (or None), and the sha of the commit the
    last report names that git verified on ``origin/<main>`` ('' when none) — the evidence a
    park closes its card on instead of waiting for a person (:mod:`asf.workers.trunkclose`).
    The host is asked too (:func:`_held`): an open PR of the item's means not landed, and a
    host that could not answer makes ``landed`` :class:`asf.workers.landing.Unknown` — no
    decision this pass, neither a park nor a close.
    Under ``product``'s ``flags.facts: shadow`` the ``landed`` half is compared with the landing
    fact (:func:`asf.facts.landing.landed`); the answer is always this one."""
    if cap is None:
        from asf import config_keys
        cap = config_keys.value('worker_pool.caps.relaunch', CAP)
    runs = streak(path, job, item, head, card, cause)
    on_head = head_streak(path, job, item, head, card)
    if len(on_head) >= cap and len(on_head) > len(runs):
        # the cause moved round to round, the head did not: the same correction again
        at = head[:9]
        causes = ', '.join(dict.fromkeys((r.get('cause') or '?')[:8] for r in on_head))
        return (f'{job} launched {len(on_head)} time(s) on {at} without a new commit (the '
                f'card unchanged, the cause moved: {causes}). Not relaunched: the row is parked '
                f'until a new commit or card edit changes its state, or `asf unpark {item}`'), ''
    if not runs:
        return None, ''
    text = _result_text(runs[0])
    claim = terminal(text)
    # the terminal shortcut needs the head known: a branch gone from origin (landed and deleted)
    # is relaunched fresh, and one run on it proves nothing about the next
    if len(runs) < cap and not (claim and head):
        return None, ''
    at = (runs[0].get('launch_head') or head or '')[:9] or 'an unchanged head'
    n = len(runs)
    why = (f'{job} launched {n} time(s) on {at} with the card and cause unchanged'
           + (f', its last report: {claim[:300]}' if claim else ''))
    landed = on_trunk(repo, main, text, exclude=[r.get('launch_head') for r in runs], item=item,
                      writes=writes, prs=landing.run_prs(path, item)) if claim else ''
    if landed and lifecycle.voided_sha(path, item, landed):
        why += f' — claims voided landing {landed[:7]}'
        landed = ''  # the operator voided it (`asf reset`): parked, never closed on it
    if landed:
        landed = _held(product, repo, main, path, item, landed, writes,
                       [r.get('branch') for r in runs])
    if claim and product is not None and repo and item:
        from asf.facts import landing as facts_landing
        landed = facts_landing.shadow(
            product, facts_landing.RELAUNCH, item, landed,
            lambda: facts_landing.landed(product, item, repo=repo, main=main, path=path,
                                         writes=list(writes or ()),
                                         branches=[r.get('branch') for r in runs]),
            agree=facts_landing.agree_closes)
    if landed:
        why += (f' — the work it names is on origin/{main} at {landed[:9]} (verified): close '
                f'{item} on that evidence')
    return (f'{why}. Not relaunched: the row is parked until a new commit, review, card edit or '
            f'decision changes its state, or `asf unpark {item}`'), landed


def _held(product, repo, main, path, item, sha, writes, branches):
    """``sha`` when it stands as the item's landing; '' when the item's own work is unmerged —
    a branch of its runs, or an open PR naming it (the pass's open-PR read,
    :func:`asf.workers.landing.pass_open_work`; with no product, :func:`asf.workers.landing.
    open_prs`) — or when ``sha`` only covers the item's ``writes:`` and its own PR is open or
    was closed unmerged (:func:`asf.workers.landing.covers_refused`); :class:`asf.workers.
    landing.Unknown` when the host could not tell."""
    if landing.open_work(repo, main, path, item, branches, ask_gh=False):
        return ''  # the item's own commits are unmerged: nothing on the trunk is its landing
    if product is None:
        work = landing.open_work(repo, main, path, item, branches, ask_gh=True)
    else:
        work = landing.pass_open_work(product, repo, main, item)
    if work is None:
        return landing.Unknown('the open PRs could not be read')
    if work:
        return ''  # an open PR holds the item's work: the trunk commit is not its landing
    if product is not None \
            and landing.attribution(repo, main, sha, item, writes,
                                    landing.run_prs(path, item)) == 'covers':
        held = landing.covers_refused(product, item)
        if held is None:
            return landing.Unknown('the item\'s PRs could not be read')
        if held:
            return ''  # its own PR is open or closed unmerged: the cover is not its work
    return sha


def park_fields(reason, card, now):
    """The update that parks the job's newest run (read by the feeder as ``PARKED``)."""
    return {'correction': {'kind': RELAUNCH_CAP, 'text': reason, 'at': now, 'parked': True,
                           'reason': reason, 'card': card},
            'operator_flagged': 1}
