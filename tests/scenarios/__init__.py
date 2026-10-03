"""The scenario harness (W6-PR0a): every close path, one fixture product, one fake PR host.

A close decision is spread over seven readers — the wave's trunk check before a launch, the
parked-run sweep, the relaunch cap's park-or-close, the record's ingest (whose merge facts the
lane's run lines feed), the approvals ledger's sweep of landed holds, the groom's ``close_*``
policies and the release note's "landed" headings. Each has its own tests against its own
mocks; none of them is asked what it does when the PR host misbehaves the way a real one does.
This package asks all of them the same question on the same world.

**The world** (:class:`World`, built once per process and forked per row): the end-to-end
harness's sample product (:class:`e2e.factory.Factory`, stage ``planned``) — a bare origin, the
product checkout, the record, the operator home and the fake ``gh`` first on ``PATH`` — plus:

* a trunk commit (:attr:`World.decoy`) that touches exactly the Task's ``writes:`` without naming
  it: what a session that found "the work already on origin/main" points at, and what
  :func:`asf.workers.landing.covers` attributes to the Task;
* the Task's own work on ``hand/t-0001``, open as PR #1 (titled with the Task's id), never fetched
  into the product checkout — only the host knows it is there;
* the Task's run branch ``feature/t-0001`` on origin at the old trunk head (nothing past it);
* one ended coder run of the Task whose REPORT says ``status: done`` and names the decoy.

With the host answering, PR #1 is open work: no path may close the Task. A **host behaviour**
(:data:`BEHAVIOURS`) then changes what the host says — ``rate-limit`` (every call refused for
rate, the fake's ``e2e rate-limit on``), ``close-unmerged`` (PR #1 closed without a merge, the
fake's ``e2e close-unmerged 1``), and ``merged`` (PR #1 squash-merged: the positive control,
which proves a row can see a close at all).

**A path** (:data:`PATHS`) is one close decider, run on a fresh fork the way the tick runs it,
and read back as an :class:`Outcome`: whether it closed the Task (a landing stamp on the run
line — ``harvested:`` — a derived ``Resolved``/``Closed``, a closed hold, a groom close answer
or a "landed" release-note line) and whether it waited (the host refused for rate). The table
of ``(path × behaviour) → expected`` lives in ``test_close_paths.py``: a new path or behaviour
is one entry here and one row there.
"""
import atexit
import contextlib
import dataclasses
import datetime
import io
import json
import os
import shutil
import tempfile
import types

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.scenarios…` does not
    from e2e.factory import Factory, NAME, SLUG, _git
except ImportError:  # pragma: no cover - import shape only
    from tests.e2e.factory import Factory, NAME, SLUG, _git

ITEM = 'T-0001'
JOB = 'coder-t-0001'
KIND = 'coder'
#: The branch the Task's run worked on: on origin, holding nothing past the trunk.
RUN_BRANCH = 'feature/t-0001'
#: The head of the Task's open PR: on origin only, never fetched into the product checkout.
PR_HEAD = 'hand/t-0001'
PR_TITLE = f'{ITEM} — count_lines'
WRITES = ('src/lines.py', 'tests/test_lines.py')
CARD = 'c0ffee00c0ffee00'
STARTED, ENDED = '2026-01-02T09:00:00Z', '2026-01-02T09:30:00Z'
DONE_STATES = ('Resolved', 'Closed')


def report(sha):
    """The run's REPORT: done, the work "already on origin/main" at ``sha``."""
    return ('```\nREPORT\n'
            f'item: {ITEM}\nkind: {KIND}\nstatus: done\nbranch: {RUN_BRANCH}\n'
            'pushed: none\ncommits: none\ntests: none\n'
            f'left out: the whole scope is already on origin/main under {sha[:9]}\n'
            'needs writes: none\nproves: none\n```')


@dataclasses.dataclass(frozen=True)
class Scenario:
    """What one row is about: the item, the branch its run worked on, the run's REPORT and the
    host's behaviour while the path decides."""
    item: str
    branch: str
    report_text: str
    host_behaviour: str


@dataclasses.dataclass
class Outcome:
    """What a path did to the Task. ``closed``: any close it wrote (``how`` says which);
    ``stamp``: the landing sha on the Task's run line, '' when none; ``state``: the card's state
    after an ingest over the same host (its on-disk state when the ingest waited); ``waited``:
    why the path stopped short of a decision ('' when it decided)."""
    closed: bool = False
    how: str = ''
    stamp: str = ''
    state: str = ''
    waited: str = ''
    lines: list = dataclasses.field(default_factory=list)

    def __str__(self):
        bits = [f'closed by {self.how}' if self.closed else 'closed nothing']
        if self.stamp:
            bits.append(f'stamp {self.stamp[:9]}')
        if self.state:
            bits.append(f'state {self.state}')
        if self.waited:
            bits.append(f'waited: {self.waited}')
        return ', '.join(bits)


# ---- the world ---------------------------------------------------------------------------------

class World:
    """The product as every row starts it (see the module doc), built once into a directory of
    its own; :meth:`fork` hands each row a copy."""

    def __init__(self):
        self.factory = None
        self.base = self.decoy = self.hand = ''

    def _build(self):
        root = tempfile.mkdtemp(prefix='scenarios-world-')
        atexit.register(shutil.rmtree, root, True)
        f = Factory(root, stage='planned').setup()
        self.base = _git(['rev-parse', 'main'], cwd=f.repo_origin)
        self.decoy = f.push('main', {WRITES[0]: 'def count_lines(text):\n'
                                                '    return len(text.splitlines())\n',
                                     WRITES[1]: '# the shared line helpers\n'},
                            'chore: shared line helpers')
        self.hand = f.push(PR_HEAD, {WRITES[0]: 'def count_lines(text):\n'
                                                '    return text.count("\\n") + 1\n',
                                     WRITES[1]: '# count_lines, by hand\n'},
                           f'{ITEM}: count_lines, by hand')
        _git(['push', '-q', f.repo_origin, f'{self.base}:refs/heads/{RUN_BRANCH}'], cwd=f.repo)
        _git(['fetch', '-q', 'origin', 'main', RUN_BRANCH], cwd=f.repo)
        number = f.open_pr(PR_HEAD, PR_TITLE)
        assert number == 1, number
        self._ledger(f)
        self.factory = f

    def _ledger(self, f):
        """The Task's one ended coder run, its REPORT naming the decoy."""
        os.makedirs(f.state_dir, exist_ok=True)
        log = os.path.join(f.state_dir, f'{JOB}.log.jsonl')
        with open(log, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'type': 'result', 'subtype': 'success',
                                 'result': report(self.decoy)}) + '\n')
        append_runs(f, {'job': JOB, 'item': ITEM, 'kind': KIND, 'branch': RUN_BRANCH,
                        'pid': 4242, 'log': log, 'started': STARTED, 'card_digest': CARD,
                        'launch_head': self.base},
                    {'job': JOB, 'ended': ENDED, 'end_reason': 'finished'})

    def scenario(self, behaviour):
        self.ensure()
        return Scenario(ITEM, RUN_BRANCH, report(self.decoy), behaviour)

    def ensure(self):
        if self.factory is None:
            self._build()
        return self

    def fork(self):
        """A copy of the world under a new temp directory, removed at exit."""
        self.ensure()
        dest = tempfile.mkdtemp(prefix='scenarios-row-')
        atexit.register(shutil.rmtree, dest, True)
        return self.factory.fork(dest)


WORLD = World()


def append_runs(f, *lines):
    with open(os.path.join(f.state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as fh:
        for line in lines:
            fh.write(json.dumps(line) + '\n')


def gh(f, *argv):
    p = f.gh(*argv)
    if p.returncode != 0:
        raise RuntimeError(f'gh {" ".join(argv)}: {p.stderr.strip()}')
    return p.stdout


# ---- the host behaviours -----------------------------------------------------------------------

def _open(f):
    """The host answers, PR #1 is open: the Task's work is unmerged."""


def _rate_limit(f):
    gh(f, 'e2e', 'rate-limit', 'on')


def _close_unmerged(f):
    gh(f, 'e2e', 'close-unmerged', '1')


def _merged(f):
    gh(f, 'pr', 'merge', '1', '-R', SLUG, '--squash')


BEHAVIOURS = {
    'open': _open,
    'rate-limit': _rate_limit,
    'close-unmerged': _close_unmerged,
    'merged': _merged,
}


# ---- reading a fork ----------------------------------------------------------------------------

def stamp(f):
    """The landing sha the ledger holds for the Task ('' when none): a run line's
    ``harvested:`` — what :func:`asf.evidence.evidence.merge_facts` reads as its merge fact."""
    from asf.workers import lifecycle
    path = os.path.join(f.state_dir, 'sessions.jsonl')
    for run in lifecycle.item_runs(path, ITEM):
        sha = str(run.get('harvested') or '')
        if sha and sha not in lifecycle.NOT_A_LANDING:
            return sha
    return ''


def on_disk_state(product, item=ITEM):
    canonical = canonical_of(product)
    from asf.record import frontmatter
    return frontmatter.split_machine(canonical[item]['meta'])[1].get('state', 'New')


def canonical_of(product):
    from asf.record.core import canonicalize, load_items
    by_id, _errors = load_items(product.backlog_dir)
    canonical, _dupes = canonicalize(by_id)
    return canonical


def derive(product):
    """``(states, waited)``: every item's state from one ingest's evidence over the host as it
    is now (:func:`asf.evidence.evidence.load` → :func:`asf.record.ingest.derive`, nothing
    written), or the on-disk states and the reason when the host refused for rate — the tick's
    ingest step waits then, and every later step reads the record as it stood."""
    from asf import gh_limit
    from asf.evidence import evidence
    from asf.record import frontmatter, ingest
    canonical = canonical_of(product)
    try:
        ev = evidence.load(fresh=True, product=product)
    except gh_limit.RateLimited as e:
        gh_limit.reset()
        return ({iid: frontmatter.split_machine(rec['meta'])[1].get('state', 'New')
                 for iid, rec in canonical.items()}, f'rate-limited: {e}')
    new_state = ingest.derive(canonical, ev, product)[0]
    return new_state, ''


@contextlib.contextmanager
def deciding(f):
    """The fork's environment (its home, its ``PATH`` with the fake ``gh`` first) and its
    product, with the process-wide rate-limit latch clear going in and coming out."""
    from asf import env, gh_limit
    gh_limit.reset()
    try:
        with f.seams():
            yield env.load_product(NAME)
    finally:
        gh_limit.reset()


def _decide(fn, product, out):
    """``fn(product, out)``'s own verdict, or the reason it stopped: a rate limit is not an
    answer (:class:`asf.gh_limit.RateLimited` leaves every fallback)."""
    from asf import gh_limit
    try:
        return fn(product, out), ''
    except gh_limit.RateLimited as e:
        gh_limit.reset()
        return None, f'rate-limited: {e}'


# ---- the close paths ---------------------------------------------------------------------------

def _closes_before_launch(world, f, product, out):
    from asf.workers import trunkclose
    return _decide(lambda p, o: trunkclose.closes_before_launch(p, KIND, ITEM, o), product, out)


def _close_parked(world, f, product, out):
    """The run is parked on a reason that carries the verified evidence, the way the relaunch cap
    words it (:func:`asf.workers.relaunch.landed_in` reads it back)."""
    from asf.workers import relaunch, trunkclose
    reason = (f'{JOB} launched 1 time(s) on {world.base[:9]} with the card and cause unchanged'
              f' — the work it names is on origin/main at {world.decoy[:9]} (verified): close '
              f'{ITEM} on that evidence. Not relaunched')
    append_runs(f, {'job': JOB, **relaunch.park_fields(reason, CARD, '2026-01-02T10:00:00Z')})
    return _decide(lambda p, o: trunkclose.close_parked(p, o), product, out)


def _relaunch_cap(world, f, product, out):
    """The wave's last word on a coder row of the Task (``step_wave.relaunch_capped``): the
    terminal report on an unmoved head parks the row — or closes it when the trunk evidence
    verifies (:func:`asf.workers.relaunch.assess`, then :func:`asf.workers.trunkclose.evidence`)."""
    from asf.tick import step_wave
    row = types.SimpleNamespace(kind=KIND, correction='')
    wrow = types.SimpleNamespace(job=JOB, item=ITEM, branch=RUN_BRANCH, card_digest=CARD,
                                 cause=None)
    held, waited = _decide(lambda p, o: step_wave.relaunch_capped(p, row, wrow, o), product, out)
    # True is "not launched" — parked or closed alike: only the run line's stamp says closed
    return (stamp(f) if held else None), waited


def _ingest(world, f, product, out):
    """The record's ingest over the evidence (its merge facts off the run lines included)."""
    states, waited = derive(product)
    return states.get(ITEM), waited


def _close_landed(world, f, product, out):
    """An open hold on the Task, then the approvals sweep over the states the ingest derived
    (the record's own when the ingest waited)."""
    from asf import approvals
    approvals.refuse(product, ITEM, 'merge_amendable_set', 'human-now', JOB, 'Bash',
                     'a held merge')
    states, waited = derive(product)
    items = {iid: {'state': s} for iid, s in states.items()}
    return approvals.close_landed(product, items, out), waited


#: The groom policies that close a card (``removed``), whatever their section.
GROOM_CLOSERS = ('close_duplicate_task', 'close_exact_duplicate', 'close_superseded',
                 'decide_or_close_ci_red', 'close_on_starvation')


def _groom(world, f, product, out):
    """Every groom ``close_*`` policy, asked about the Task over the record as the ingest left it."""
    from asf.groom import policy
    from asf.workers import lifecycle
    states, waited = derive(product)
    canonical = canonical_of(product)
    path = os.path.join(f.state_dir, 'sessions.jsonl')
    ledger_items = frozenset(r.get('item') for rs in lifecycle.runs(path).values() for r in rs
                             if r.get('item'))
    now = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    ctx = policy.Ctx(date='2026-06-01', now=now, ledger_items=ledger_items)
    answers = []
    for name, _sections, fn in policy.POLICIES:
        if name not in GROOM_CLOSERS:
            continue
        ans = fn(ITEM, canonical[ITEM], canonical, {}, ctx)
        if ans is not None and ans.field == 'removed':
            answers.append(f'{name}: {ans.why}')
            out(f'groom: {ITEM} {name} — {ans.why}')
    return answers, waited


def _release(world, f, product, out):
    """A release note over the trunk as it stands, the Task among the items the release moved,
    its state the ingest's. Listed under a "landed" heading = closed; and I12 holds either way."""
    from asf import invariants
    from asf.metrics import metrics
    states, waited = derive(product)
    canonical = canonical_of(product)
    from asf.record import frontmatter
    items = {}
    for iid, rec in canonical.items():
        typed, _machine = frontmatter.split_machine(rec['meta'])
        items[iid] = {'type': typed.get('type'), 'title': typed.get('title', ''),
                      'folder': rec['folder'], 'state': states.get(iid)}
    head = _git(['rev-parse', 'main'], cwd=f.repo_origin)
    text = metrics.render_release('2026-06-01', head, world.base, None, items,
                                  {ITEM: [(1, PR_TITLE)]}, tag='v0.0.1')
    found = invariants.check_i12(text, items)
    if found:
        raise AssertionError(f'I12 broken by the release note: {found}\n{text}')
    landed = [ln for ln in _landed_lines(text) if ITEM in ln]
    for ln in landed:
        out(f'release: {ln}')
    return landed, waited


def _landed_lines(text):
    from asf import invariants
    current, out = None, []
    for line in text.splitlines():
        if line.startswith('#'):
            current = line.strip() if line.strip() in invariants.LANDED_HEADINGS else None
            continue
        if current and line.startswith('- '):
            out.append(line)
    return out


def _merge_facts(world, f, product, out):
    """The lane's merge facts (:func:`asf.evidence.evidence.merge_facts`), read as the ingest
    reads them."""
    from asf.evidence import evidence
    return evidence.merge_facts(product).get('code', {}).get(ITEM), ''


#: ``name → (runner, how a truthy verdict closes)``. A runner is ``(world, fork, product, out)
#: → (verdict, waited)``; the ingest's verdict is a state, closed when Resolved or Closed.
PATHS = {
    'trunkclose.closes_before_launch': _closes_before_launch,
    'trunkclose.close_parked': _close_parked,
    'relaunch.assess → step_wave park/close': _relaunch_cap,
    'ingest.derive via merge_facts': _ingest,
    'approvals.close_landed': _close_landed,
    'groom/policy.close_*': _groom,
    'release notes (I12)': _release,
    'evidence.merge_facts': _merge_facts,
}


# ---- the extra world edits a row can ask for ---------------------------------------------------

def voided_landing(world, f):
    """The Task's run landed PR #1 at its merge (the lane's ``MERGED`` record, ``harvested:``),
    and an operator then reset that landing — a reset line naming the same ``(pr, head)``
    (:func:`asf.workers.lifecycle.note_reset`). W4-PR5: a voided ``(pr, head)`` is never a merge
    fact, even while the host says the PR merged."""
    from asf.workers import lifecycle
    merged = _git(['rev-parse', 'main'], cwd=f.repo_origin)
    append_runs(f, {'job': JOB, 'harvested': merged,
                    'lane': {'pr': 1, 'head': world.hand, 'state': 'MERGED', 'sha': merged}})
    lifecycle.note_reset(os.path.join(f.state_dir, 'sessions.jsonl'), ITEM,
                         {'pr': 1, 'branch': PR_HEAD, 'head': world.hand, 'archive': ''},
                         now='2026-01-03T09:00:00Z', alive=lambda *_a, **_k: False)


EDITS = {'voided-landing': voided_landing}
#: The edits that write a landing stamp themselves: the row's premise, not the path's close —
#: such a row is read by its path's verdict alone.
STAMPING_EDITS = ('voided-landing',)


def run(path, behaviour, edit=None, world=WORLD):
    """One row: fork the world, let the host behave, apply ``edit`` (a name in :data:`EDITS`),
    run ``path`` and read back what it did. Returns ``(Scenario, Outcome)``."""
    scenario = world.scenario(behaviour)
    f = world.fork()
    BEHAVIOURS[behaviour](f)
    lines = []
    with deciding(f) as product, contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        if edit:
            EDITS[edit](world, f)
        verdict, waited = PATHS[path](world, f, product, lines.append)
        states, _w = derive(product)
    o = Outcome(waited=waited, lines=lines, stamp=stamp(f), state=states.get(ITEM) or '')
    if path == 'ingest.derive via merge_facts':
        o.closed, o.how = verdict in DONE_STATES, f'the ingest ({verdict})'
    elif verdict:
        o.closed, o.how = True, f'{path} ({verdict})'
    if not o.closed and o.stamp and edit not in STAMPING_EDITS:
        o.closed, o.how = True, f'a landing stamp on the run line ({o.stamp[:9]})'
    return scenario, o
