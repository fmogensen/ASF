"""The scenario harness (W6-PR0a, W6-PR0b): every close path, one fixture product, one fake PR host.

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
* one ended coder run of the Task whose REPORT says ``status: done`` and names the decoy;
* the trunk's branch protection requiring one check (:data:`CHECK`), green on PR #1.

With the host answering, PR #1 is open work: no path may close the Task. A **host behaviour**
(:data:`BEHAVIOURS`) then changes what the host says — ``rate-limit`` (every call refused for
rate, the fake's ``e2e rate-limit on`` with the recorded refusal of ``tests/fixtures/gh/
rate-limit``), ``close-unmerged`` (PR #1 closed without a merge, the
fake's ``e2e close-unmerged 1``), and ``merged`` (PR #1 squash-merged: the positive control,
which proves a row can see a close at all). The check behaviours (:data:`CHECK_BEHAVIOURS`,
W6-PR0b) change only what the host says about PR #1's checks, each the fake's stand-in for a
recorded ``gh`` answer under ``tests/fixtures/gh``: ``stale-merge-ref`` (red from a run on a
merge ref the trunk moved past), ``skip-job`` and ``skip-job-attested`` (the required check
skipped, on a head without and with the merge queue's attestation), ``hide-job-failure`` (a run
concluding success over a failed job) and ``path-filter`` (the required check never created).

**A path** (:data:`PATHS`) is one close decider, run on a fresh fork the way the tick runs it,
and read back as an :class:`Outcome`: whether it closed the Task (a landing stamp on the run
line — ``harvested:`` — a derived ``Resolved``/``Closed``, a closed hold, a groom close answer
or a "landed" release-note line) and whether it waited (the host refused for rate). The table
of ``(path × behaviour) → expected`` lives in ``test_close_paths.py``: a new path or behaviour
is one entry here and one row there.

**An edit** (:data:`EDITS`) changes the world before the path runs: the named edges of a landing
(:data:`EDGES`, S-M14 — the merge methods, a reworded squash, a revert, the document lane, an
archive, a cloud session, a batch ref, an attested sha; ``test_edges.py``), the voided landing,
and a card newer than the commit said to cover it. :func:`run_written` is a row of
``test_i14_refuse.py``: the path under ``flags.i14``, then the record's guarded ingest write, the
Task's card read back from disk.
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
    import contracts
    from e2e.factory import Factory, NAME, SLUG, _git
except ImportError:  # pragma: no cover - import shape only
    from tests import contracts
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
#: The trunk's one required check (branch protection), green on PR #1 as the world starts.
CHECK = 'tests'
#: The job a hidden failure is in: not required, its run concluding success all the same.
HIDDEN_JOB = 'e2e-nightly'


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
        gh(f, 'e2e', 'required', CHECK)
        gh(f, 'e2e', 'default-checks', f'{CHECK}=pass')
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
    """Every call answered with GitHub's own recorded refusal (W3-PR3's
    ``tests/fixtures/gh/rate-limit``)."""
    rc, _out, err = contracts.load('rate-limit', 'run-list')
    gh(f, 'e2e', 'rate-limit', 'on', str(rc), err)


def _close_unmerged(f):
    gh(f, 'e2e', 'close-unmerged', '1')


def _merged(f):
    gh(f, 'pr', 'merge', '1', '-R', SLUG, '--squash')


def _stale_merge_ref(f):
    """PR #1 red on its required check from a run on a merge ref the trunk has since moved
    past (the recorded ``tests/fixtures/gh/stale-merge-ref``): the run predates the tip."""
    gh(f, 'e2e', 'stale-merge-ref', '1', CHECK)
    f.push('main', {'NOTES': 'an unrelated trunk change\n'}, 'chore: notes')


def _skip_job(f):
    """PR #1's required check skipped on a head nobody attested."""
    gh(f, 'e2e', 'skip-job', '1', CHECK)


def _skip_job_attested(f):
    """PR #1's required check skipped on a head the merge queue attested."""
    gh(f, 'e2e', 'skip-job', '1', CHECK, 'attested')


def _hide_job_failure(f):
    """PR #1's run concludes success while a job of it failed."""
    gh(f, 'e2e', 'hide-job-failure', '1', HIDDEN_JOB)


def _path_filter(f):
    """The workflow's path filter never creates PR #1's required check."""
    gh(f, 'e2e', 'path-filter', CHECK)


BEHAVIOURS = {
    'open': _open,
    'rate-limit': _rate_limit,
    'close-unmerged': _close_unmerged,
    'merged': _merged,
    'stale-merge-ref': _stale_merge_ref,
    'skip-job': _skip_job,
    'skip-job-attested': _skip_job_attested,
    'hide-job-failure': _hide_job_failure,
    'path-filter': _path_filter,
}
#: The behaviours that change only what the host says about PR #1's checks: the PR stays open,
#: its work unmerged — no close path reads checks, so each answers as under ``open``.
CHECK_BEHAVIOURS = ('stale-merge-ref', 'skip-job', 'skip-job-attested', 'hide-job-failure',
                    'path-filter')
#: Their rows of the close-path table, ``(path, behaviour, expected, gap, edit)`` as
#: ``test_close_paths.ROWS`` (run by ``test_host_behaviours.py``): open, unmerged work on every
#: path, whatever the host says about its checks.
CHECK_ROWS = tuple((p, b, 'none', None, None) for b in CHECK_BEHAVIOURS for p in (
    'trunkclose.closes_before_launch', 'trunkclose.close_parked',
    'relaunch.assess → step_wave park/close', 'ingest.derive via merge_facts',
    'approvals.close_landed', 'groom/policy.close_*', 'release notes (I12)',
    'evidence.merge_facts'))


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
    product, with the process-wide rate-limit latch and read caches clear going in and out."""
    from asf import attestation, env, gh_limit, stale_ref
    gh_limit.reset()
    # the process-wide read caches: every fork's PR #1 has the same head and run ids
    attestation._SEEN.clear()
    stale_ref._RUNS.clear()
    try:
        with f.seams():
            yield env.load_product(NAME)
    finally:
        gh_limit.reset()
        attestation._SEEN.clear()
        stale_ref._RUNS.clear()


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


def doc_lane_landing(world, f):
    """A reshape of the Task ran on the plan lane and its plan merged: a run line on a plan-lane
    branch stamped ``harvested:`` at the trunk head. W8-PR3: a document's merge is never the
    Task's landing — not a code merge fact, not a close, and
    :func:`asf.workers.landing.verify_landings` calls it neither verified nor unverified (no
    NEEDS DECISION row: the Task stays at its build stage)."""
    from asf import env
    from asf.evidence import evidence
    from asf.workers import landing, lifecycle
    product = env.load_product(NAME)
    branch = evidence.branch_prefixes(product)['plan'] + ITEM.lower()
    merged = _git(['rev-parse', 'main'], cwd=f.repo_origin)
    append_runs(f, {'job': f'reshape-{ITEM.lower()}', 'item': ITEM, 'kind': 'reshape',
                    'branch': branch, 'pid': 4343, 'started': STARTED},
                {'job': f'reshape-{ITEM.lower()}', 'ended': ENDED, 'end_reason': 'finished',
                 'harvested': merged})
    path = os.path.join(f.state_dir, 'sessions.jsonl')
    occ = lifecycle.occupancy(path, alive=lambda *_a, **_k: False)
    assert occ['landed_on'].get(ITEM) == branch, occ['landed_on']
    got = landing.verify_landings(product, occ, {ITEM: {'type': 'task', 'state': 'New'}},
                                  path=path)
    assert got == ({}, {}), f'a plan-lane merge is neither verified nor unverified: {got}'


def _trunk(f):
    return _git(['rev-parse', 'main'], cwd=f.repo_origin)


def _commit(f, ref, parents, message, tree_of=None):
    """A commit on the bare origin made by hand (``commit-tree``): ``tree_of``'s tree (the first
    parent's when None) under ``parents``, written to ``refs/heads/<ref>``. Returns its sha."""
    tree = _git(['rev-parse', f'{tree_of or parents[0]}^{{tree}}'], cwd=f.repo_origin)
    args = ['commit-tree', tree, '-m', message]
    for p in parents:
        args += ['-p', p]
    sha = _git(args, cwd=f.repo_origin)
    _git(['update-ref', f'refs/heads/{ref}', sha], cwd=f.repo_origin)
    return sha


def _lane_merged(f, world, sha, job=JOB, branch=PR_HEAD, pr=1, **extra):
    """The lane's record of PR ``pr`` merged at ``sha`` on ``job``'s run line."""
    append_runs(f, {'job': job, 'harvested': sha, **extra,
                    'lane': {'pr': pr, 'head': world.hand, 'state': 'MERGED', 'sha': sha,
                             'branch': branch}})


def edge_squash(world, f):
    """PR #1 squash-merged: one new trunk commit, its subject the PR's title and number."""
    gh(f, 'pr', 'merge', '1', '-R', SLUG, '--squash')


def edge_merge_commit(world, f):
    """PR #1 merged with a merge commit: the PR's own commit reaches the trunk as a parent."""
    gh(f, 'pr', 'merge', '1', '-R', SLUG, '--merge')


def edge_rebase_merge(world, f):
    """PR #1 rebase-merged: its commit replayed on the trunk (a new sha, the same subject)."""
    gh(f, 'pr', 'merge', '1', '-R', SLUG, '--rebase')


def edge_reworded_patch(world, f):
    """PR #1 squash-merged under a subject a person reworded: it names neither the Task nor the
    PR's title."""
    gh(f, 'e2e', 'squash-subject', 'shared line counting')
    gh(f, 'pr', 'merge', '1', '-R', SLUG, '--squash')


def edge_revert(world, f):
    """PR #1 squash-merged, then reverted on the trunk (``This reverts commit <sha>.``): the
    trunk holds the Task's work no more (S-M15)."""
    gh(f, 'pr', 'merge', '1', '-R', SLUG, '--squash')
    merged = _trunk(f)
    _commit(f, 'main', [merged], f'Revert "{PR_TITLE} (#1)"\n\nThis reverts commit {merged}.',
            tree_of=f'{merged}^')


#: The Task's spec, merged through the document lane.
SPEC_BRANCH = 'spec/t-0001'


def edge_doc_lane(world, f):
    """PR #1 closed unmerged; the Task's *spec* merged through the document lane (a ``spec/``
    branch, its PR titled with the Task's id, the lane's ``MERGED`` record on a spec run): a
    document landed, not the Task's code."""
    gh(f, 'e2e', 'close-unmerged', '1')
    f.push(SPEC_BRANCH, {'specs/t-0001.md': f'# {ITEM} — count_lines\n'}, f'{ITEM}: spec')
    number = f.open_pr(SPEC_BRANCH, f'{ITEM} spec — count_lines')
    gh(f, 'pr', 'merge', str(number), '-R', SLUG, '--squash')
    append_runs(f, {'job': 'spec-t-0001', 'item': ITEM, 'kind': 'spec', 'branch': SPEC_BRANCH,
                    'pid': 4243, 'started': STARTED, 'ended': ENDED, 'end_reason': 'finished',
                    'harvested': _trunk(f),
                    'lane': {'pr': number, 'state': 'MERGED', 'sha': _trunk(f),
                             'branch': SPEC_BRANCH}})


#: The archive a closed PR's head is kept under (:data:`asf.evidence.evidence.ARCHIVE_PR_TAG`).
ARCHIVE_BRANCH = 'archive/t-0001'


def edge_archive_branch(world, f):
    """PR #1 closed unmerged, its head kept as the ``archive/pr-1`` tag and an ``archive/``
    branch naming the Task: provenance, never a landing."""
    gh(f, 'e2e', 'close-unmerged', '1')
    _git(['update-ref', 'refs/tags/archive/pr-1', world.hand], cwd=f.repo_origin)
    _git(['update-ref', f'refs/heads/{ARCHIVE_BRANCH}', world.hand], cwd=f.repo_origin)


def edge_cloud_session(world, f):
    """A cloud session's run of the Task: its branch ends in the empty ``asf: report`` commit
    (the REPORT in its message), its PR squash-merged, the lane's ``MERGED`` record on the cloud
    run's line (a ``cloud:`` pid token)."""
    tip = _commit(f, PR_HEAD, [world.hand],
                  f'asf: report cloud-t-0001\n\n{report(world.decoy)}')
    gh(f, 'pr', 'merge', '1', '-R', SLUG, '--squash')
    append_runs(f, {'job': 'cloud-t-0001', 'item': ITEM, 'kind': KIND, 'branch': PR_HEAD,
                    'pid': 'cloud:t-0001', 'started': STARTED, 'ended': ENDED,
                    'end_reason': 'finished', 'launch_head': world.base})
    _lane_merged(f, world, _trunk(f), job='cloud-t-0001', head_tip=tip)


def edge_cloud_report_only(world, f):
    """PR #1 closed unmerged; only a cloud session's empty ``asf: report`` commit — its subject
    naming the job, its body the REPORT naming the Task — reached the trunk: nothing landed."""
    gh(f, 'e2e', 'close-unmerged', '1')
    trunk = _trunk(f)
    _commit(f, 'main', [trunk], f'asf: report {JOB}\n\n{report(world.decoy)}\n\n'
                                f'item: {ITEM}')


#: The merge queue's batch ref prefix (``merge_queue.DEFAULTS['ref_prefix']``).
BATCH_REF = 'batch/20260102T0930'


def _batch(world, f):
    from asf import merge_queue
    return _commit(f, BATCH_REF, [_trunk(f), world.hand],
                   f'Merge PR #1 ({PR_TITLE}) into main\n\n{merge_queue.TRAILER}')


def edge_batch_ref(world, f):
    """The merge queue cut a batch of PR #1 (a ``--no-ff`` merge on the trunk's tip, pushed as
    ``batch/<stamp>``) whose checks have not finished: the batch is on origin, not on the
    trunk."""
    sha = _batch(world, f)
    append_runs(f, {'job': JOB, 'lane': {'pr': 1, 'head': world.hand, 'state': 'QUEUED',
                                         'batch': BATCH_REF, 'sha': sha}})


def edge_attested_sha(world, f):
    """The merge queue landed PR #1's batch: the trunk fast-forwarded to the batch sha, which
    carries ``asf/attested`` = success; the host had not marked the PR merged, so the queue
    closed it; the lane's ``MERGED`` record names the batch sha."""
    from asf import merge_queue
    sha = _batch(world, f)
    _git(['update-ref', 'refs/heads/main', sha], cwd=f.repo_origin)
    _git(['update-ref', '-d', f'refs/heads/{BATCH_REF}'], cwd=f.repo_origin)
    gh(f, 'e2e', 'status', sha, merge_queue.ATTEST_CONTEXT, 'success')
    gh(f, 'pr', 'close', '1', '-R', SLUG)
    _lane_merged(f, world, sha, batch=BATCH_REF)


def card_after_cover(world, f):
    """The Task's card was created the day after the trunk commit covering its ``writes:`` (the
    decoy): that commit cannot be the Task's work (S-M16's ``covers`` rule)."""
    product_dir = os.path.join(f.record_dir, 'tasks')
    at = datetime.datetime.fromtimestamp(
        int(_git(['log', '-1', '--format=%ct', world.decoy], cwd=f.repo_origin)),
        datetime.timezone.utc) + datetime.timedelta(days=1)
    day, stamp = at.strftime('%Y-%m-%d'), at.strftime('%Y-%m-%dT%H:%M:%SZ')
    path = os.path.join(product_dir, f'{ITEM}.md')
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    text = (text.replace('2026-01-01T09:00:00Z', stamp)
            .replace('- 2026-01-01: created', f'- {day}: created'))
    assert stamp in text, path
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)


EDITS = {'voided-landing': voided_landing, 'doc-lane-landing': doc_lane_landing,
         'card-after-cover': card_after_cover,
         'squash': edge_squash, 'merge-commit': edge_merge_commit,
         'rebase-merge': edge_rebase_merge, 'reworded-patch': edge_reworded_patch,
         'revert': edge_revert, 'doc-lane': edge_doc_lane, 'archive-branch': edge_archive_branch,
         'cloud-session': edge_cloud_session, 'cloud-report-only': edge_cloud_report_only,
         'batch-ref': edge_batch_ref, 'attested-sha': edge_attested_sha}
#: The named edges of S-M14, each an edit on the ``open`` world.
EDGES = ('squash', 'merge-commit', 'rebase-merge', 'reworded-patch', 'revert', 'doc-lane',
         'archive-branch', 'cloud-session', 'cloud-report-only', 'batch-ref', 'attested-sha')
#: The edits that write a landing stamp themselves: the row's premise, not the path's close —
#: such a row is read by its path's verdict alone.
STAMPING_EDITS = ('voided-landing', 'doc-lane-landing', 'doc-lane', 'cloud-session',
                  'attested-sha')


#: The plan items a gap row names (``gap``): the item whose change turns that row green.
GAPS = {
    'W4-PR3b': 'I14 under flags.i14: refuse puts back a close without a sound landing',
    'W6-PR6c': 'the record\'s readers cut over to facts/landing (the revert rule)',
}


def holds(expected, o, world=WORLD):
    """Whether outcome ``o`` is ``expected``: ``none`` (closed nothing), ``closes``, or
    ``decoy`` (closed on the trunk commit covering the Task's ``writes:``)."""
    if expected == 'none':
        return not o.closed
    return o.closed and (expected != 'decoy' or o.stamp == world.decoy)


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


# ---- the I14 refuse rows -----------------------------------------------------------------------

def set_flag(f, name, value):
    """``conventions.flags.<name>: <value>`` in the fork's product file — what every load of the
    product reads (:meth:`asf.conventions.Conventions.flag`)."""
    path = os.path.join(f.home, 'products', f'{NAME}.yaml')
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    assert '\nconventions:\n' in text and '\n  flags:' not in text, path
    text = text.replace('\nconventions:\n', f'\nconventions:\n  flags:\n    {name}: {value}\n', 1)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)


def write_record(product, out):
    """The tick's ingest *write*: :func:`asf.record.ingest.ingest_into` over fresh evidence
    through :func:`asf.record.stage.guarded` — every close the paths' stamps and the host's word
    lead to reaches the record this one way, and the record invariants (I14 among them under
    ``flags.i14``) put back what they refuse. ``(findings, waited)``."""
    from asf import gh_limit
    from asf.evidence import evidence
    from asf.record import ingest, stage
    try:
        ev = evidence.load(fresh=True, product=product)
    except gh_limit.RateLimited as e:
        gh_limit.reset()
        return [], f'rate-limited: {e}'
    _rc, _staged, findings = stage.guarded(product.backlog_dir, 'ingest', ingest.ingest_into,
                                           (ev, product), product=product, out=out)
    return findings, ''


def run_written(path, behaviour, edit=None, i14='refuse', world=WORLD):
    """One refuse row: as :func:`run`, under ``flags.i14: <i14>``, then the record's guarded
    ingest write. The outcome is the Task's card *on disk* — the one close I14 guards."""
    scenario = world.scenario(behaviour)
    f = world.fork()
    set_flag(f, 'i14', i14)
    BEHAVIOURS[behaviour](f)
    lines = []
    with deciding(f) as product, contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        assert product.flag('i14') == i14, product.flag('i14')
        if edit:
            EDITS[edit](world, f)
        _verdict, waited = PATHS[path](world, f, product, lines.append)
        findings, wrote = write_record(product, lines.append)
        state = on_disk_state(product)
    o = Outcome(waited=waited or wrote, lines=lines, stamp=stamp(f), state=state)
    o.closed, o.how = state in DONE_STATES, f'the record ({state})'
    o.lines += [f'finding: {x}' for x in findings]
    return scenario, o

