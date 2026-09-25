"""asf.harvest.lane — the PR lane as one state machine.

A lane branch's life — pushed, a PR (or, fast-forward, the branch as its own PR), a review, the
gate, merged or back — is one state per branch, moved only here. Both landing modes run the same
machine; only the :class:`Host` differs (:class:`FastForwardHost` pushes the trunk,
:class:`GitHubHost` merges a PR). Every other module reads the lane through :func:`state`,
:func:`busy_items` and :func:`snapshot` (or :func:`asf.workers.lifecycle.occupancy`, which folds
the same run lines); none writes it.

**Where the state lives.** On the run line in ``state/<product>/sessions.jsonl``, never a ledger
of its own: a transition is ``mark_session(job, lane={state, head, pr, at, reason, …})`` on the
run that owns the branch (:func:`asf.workers.lifecycle.by_branch`: the last run naming a branch
owns it). Adopting a branch or PR no run made writes a synthetic run. A v0.1.2 binary reading
the same file ignores the ``lane`` field. Transitions that finish a run still write
``harvested:`` / ``correction:`` on it (:func:`asf.workers.lifecycle.hold`).

**When it runs.** :func:`lane_pass` — the transitions the feeder reads (PUSHED, PR_OPEN, REVIEW,
BACK, STALE, a moved head, a merge seen) — runs in-process in the tick before the wave. Only the
gate's outcomes (MERGED, QUEUED, BACK, WAITING, WAITING_CI) are decided in the detached harvest
(:func:`gate_pass`). Before any external merge the lane writes ``MERGING`` (with the PR number, or
the sha a fast-forward pushes); a later merge seen with that MERGING before it is our own, and a
tick that died between the merge and its ledger line is closed at the merge's own sha (R3, R8).

**Facts.** :func:`facts` gathers everything once per pass — one ``gh pr list --state all`` (PR
mode), one ``git ls-remote``, the run per branch, the newest review per head
(:mod:`asf.evidence.review`) — and every transition is a pure function of them
(:func:`next_state`), unit-tested with no git and no ``gh``.

Transitions (plan §2 plus the §9 overrides):

- T1  (run) → PUSHED          the run ended and ``origin/<branch>`` is ahead of the trunk; a
                               branch or PR no run holds is adopted the same way (T2 adoption)
- T2  PUSHED → PR_OPEN         a PR was opened or one already exists; FF: at once, pr=None
- T3  PR_OPEN → REVIEW         ``conventions.lane.review`` requires a review for the landing
                               class (a state, no round is spent)
- T4  REVIEW → GATE            the review of the current head reads approved (or policy none)
- T5  REVIEW → BACK            the review of the current head reads changes (rounds+1)
- T6  GATE → WAITING_CI        required checks pending/absent under ``landing_checks_missing``
- T7  WAITING_CI → GATE        re-decided every harvest
- T8  GATE → WAITING           trunk red alone (or a required check red on the trunk's latest
                               completed run that the PR does not turn green on top of that
                               red), no merge budget, deferred, a shared path, a gate
                               timeout, an approval hold, host pressure (:func:`held_by_host`:
                               the gate is a full suite and this host has no room for it, B-0109)
                               — never a correction, never a round
- T9  GATE → BACK              red alone on a green trunk, conflict, a lane refusal
- T10 GATE → MERGING → MERGED  merged by the host (FF ``push_ff``; PR ``gh pr merge``)
- T10q GATE → QUEUED → MERGED  a merge queue took it; queue-rejected → WAITING (R5)
- T11 any open → MERGED        found merged (the PR, the pushed sha, or the diff on the trunk)
- T12 any open → STALE         PR closed unmerged, branch gone, item superseded, PUSHED unmoved
                               past ``lane.stale_after``
- T12r STALE → PR_OPEN         the PR was reopened (R6)
- T12o (orphan) → STALE        a lane branch no run holds (:meth:`Lane.orphan_facts`): its card
                               done or removed, another branch answering for the card, no card
                               at all, or too far behind to rebase — archived, its PR closed with
                               a comment, the branch deleted; an open card it alone answers for
                               is adopted instead (T2), and a landed one is MERGED (T11).
                               :data:`ORPHANS_PER_PASS` bound one pass
- T13 BACK → PUSHED            the correcting session finished with a new head
- T13n PUSHED/BACK → PUSHED    a naming refusal: the lane rewords the subjects itself
                               (:meth:`Lane.repair_naming`) — no session, no round; only a
                               reword it cannot push goes back to the session (no round)
- Th  any open, head moved     → PUSHED(new head) (R4)
"""
import datetime
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from asf import approvals, customer_content, env
from asf.evidence import review as review_mod
from asf.feeder import footprint, widen
from asf.harvest import harvest as H
from asf.workers import githooks
from asf.workers import host as host_mod
from asf.workers import lifecycle
from asf.workers.pool import now_iso

PUSHED = 'PUSHED'
PR_OPEN = 'PR_OPEN'
REVIEW = 'REVIEW'
GATE = 'GATE'
WAITING_CI = 'WAITING_CI'
WAITING = 'WAITING'
QUEUED = 'QUEUED'
MERGING = 'MERGING'
BACK = 'BACK'
MERGED = 'MERGED'
STALE = 'STALE'
REAPED = 'REAPED'

#: Every lane state, in the order a branch normally passes them.
LANE_STATES = (PUSHED, PR_OPEN, REVIEW, GATE, WAITING_CI, WAITING, QUEUED, MERGING, BACK,
               MERGED, STALE, REAPED)
#: The states a branch ends in: no transition out except to REAPED (and STALE → PR_OPEN on a
#: reopened PR).
TERMINAL_STATES = (MERGED, STALE, REAPED)
#: The states that still move — each has an outgoing transition with a time bound.
OPEN_STATES = tuple(s for s in LANE_STATES if s not in TERMINAL_STATES)
#: The states in which the branch's item is busy for the feeder (no launch row): every open
#: state except BACK, which is the feeder's to correct.
BUSY_STATES = tuple(s for s in OPEN_STATES if s != BACK)
#: The states the gate (the detached harvest) decides; the in-process pass leaves them be.
GATE_STATES = (GATE, WAITING, WAITING_CI)

#: The two landing classes (:func:`landing_class`).
DOCS = 'docs'
CODE = 'code'

#: the WAITING reason of a green branch whose trunk run the CI start queue holds (asf.ci_queue)
CI_QUEUE = 'ci queue'

LANDING_FF = 'fast-forward'
LANDING_PR = 'pull-request'
ITEM_ID_RE = re.compile(r'\b([A-Za-z]+-\d{4})\b')
#: :func:`pr_item`'s ids — an open PR on the trunk no card claims, reviewed and merged under
#: ``conventions.merge: auto``.
PR_ITEM_RE = re.compile(r'^PR-(\d+)$')
ADJUDICATE_SUBJECT_RE = re.compile(r'^adjudicate\(')
#: the correction kind a docs branch the product gate refuses goes back with: the feeder turns it
#: into a STARVED → SPEC/PLAN session on that branch (R7)
LANDING_GATE = 'landing-gate'
#: the correction kind of a push the repo's pre-push hook refused (T9 ``kind=hook``)
HOOK = 'hook'
SUPERSEDED = 'superseded'
#: The merge methods tried in order; a repo that refuses one is offered the next.
MERGE_METHODS = ('--squash', '--merge', '--rebase')
#: The methods that write a commit of their own, and so take a subject (``--rebase`` writes none).
SUBJECT_METHODS = ('--squash', '--merge')
#: ``gh pr checks`` buckets that make a PR red, and those that count as run and passed — a
#: required check a path filter skipped (``skipping``) decided it is not needed: passed (§12).
RED_BUCKETS = ('fail', 'cancel')
PASS_BUCKETS = ('pass', 'skipping')
#: A trunk check run that ended in one of these is red (:meth:`GitHubHost.trunk_red`).
TRUNK_RED_CONCLUSIONS = ('failure', 'cancelled', 'timed_out', 'startup_failure')
#: How many trunk commits, newest first, are read for a check's latest completed run.
TRUNK_RED_DEPTH = 5
#: ``conventions.landing_checks_missing`` values.
MISSING_LOCAL_GATE = 'local-gate'
MISSING_WAIT = 'wait'
DEFAULT_LANDING_WAIT_MIN = 30
REQUIRED_TTL_S = 3600
REQUIRED_CACHE = 'landing-required-checks.json'
#: The WAITING reason of a set the host-pressure guard would not start a gate for (B-0109).
HOST_PRESSURE = 'host-pressure'
#: Consecutive gate timeouts, and the file the status and doctor rows read (§12).
GATE_SLOW = 'gate-slow.json'
GATE_SLOW_AFTER = 2
#: Full gates one landing may spend: the first, and the confirmations after each bisection.
CONFIRM_ROUNDS = 3
#: how many transitions one branch may take in one pass
MAX_STEPS = 8
#: how many orphans (run-less lane branches) one pass takes up — closes or adopts; the rest wait
#: for the next pass (each close is an archive push, a PR close and a branch delete)
ORPHANS_PER_PASS = 25
BRIEF_KIND = {'spec': 'spec', 'plan': 'plan', 'fix': 'fix-bug', 'direct': 'direct'}
#: The lane's ref-only pushes (an archive, a branch delete) leave from a clean checkout detached at
#: ``origin/<trunk>`` under the state dir, never the product's own checkout: a repo's pre-push hook
#: lints whatever the pushing checkout holds, and a checkout behind or edited by a person refused
#: every delete (2026-09-25: a checkout 6 ahead and 624 behind). The hook runs in full — only the
#: tree it reads is the trunk's.
REF_PUSH_DIR = 'ref-push'
#: the last pass's failed ref pushes, for the status and doctor rows (:func:`ref_push_line`)
REF_PUSH_FAILED = 'ref-push-failed.json'
#: a lane record whose branch delete failed; the next pass tries it again (:meth:`Lane.advance`)
DELETE_OWED = 'owed'
#: a ref-push checkout older than this is a crashed pass's leftover, removed on the next
REF_PUSH_LEFTOVER_S = 3600


# ---- small helpers ----------------------------------------------------------------------------

def _conv(product):
    return getattr(product, 'conventions', product)


def landing(product):
    """``conventions.landing``, else ``fast-forward`` when ``steps.batch`` is off or unset, else
    ``pull-request``."""
    value = product.conventions.get('landing')
    if value:
        return str(value).strip().lower()
    batch = (product._get('steps') or {}).get('batch')
    if batch is None or batch is False or str(batch).strip().lower() == 'off':
        return LANDING_FF
    return LANDING_PR


def repo_slug(product):
    """``repo_slug`` from the product yaml, else ``owner/name`` off the repo's origin url; None
    when the origin is no hosted repo — there is no PR host."""
    if product.repo_slug:
        return product.repo_slug
    if not product.repo_dir:
        return None
    from asf.init import slug_from_url
    url = H.sh(['git', 'remote', 'get-url', 'origin'], cwd=product.repo_dir).stdout.strip()
    return slug_from_url(url)


def item_of(branch, record):
    """The branch's item id: the session's ``item``, else the id token in the branch name."""
    item = (record or {}).get('item')
    if item:
        return str(item)
    m = ITEM_ID_RE.search(branch.rsplit('/', 1)[-1]) or ITEM_ID_RE.search(branch)
    return m.group(1).upper() if m else None


def pr_item(number):
    """The lane's id for an open PR no factory item made (``merge: auto``): ``PR-<n>`` — what
    its review file is named after and what its synthetic run carries."""
    return f'PR-{int(number):04d}'


def is_pr_item(item):
    """True for an id :func:`pr_item` minted."""
    return bool(item) and bool(PR_ITEM_RE.match(str(item)))


def _parse_at(stamp):
    try:
        return datetime.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _glob_hit(pattern, path):
    pattern = str(pattern).strip()
    if not pattern:
        return False
    if pattern.endswith('/'):
        return path.startswith(pattern)
    return fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip('/') + '/')


def landing_class(product, files):
    """:data:`DOCS` when every path in ``files`` lies under a docs root — ``specs_dir``,
    ``plans_dir``, ``reviews_dir`` or a ``conventions.doc_paths`` glob — else :data:`CODE`. The
    one rule for docs vs code."""
    conv = _conv(product)
    roots = [str(conv.get(k)).strip('/') + '/' for k in ('specs_dir', 'plans_dir', 'reviews_dir')
             if conv.get(k)]
    globs = list(conv.get('doc_paths') or ())
    files = [f for f in files or () if f]
    if files and all(any(f.startswith(r) for r in roots) or any(_glob_hit(g, f) for g in globs)
                     for f in files):
        return DOCS
    return CODE


def shared_hits(conv, files):
    """The files among ``files`` under a ``conventions.shared_paths`` glob."""
    globs = list(conv.get('shared_paths') or ())
    return [f for f in files or () if any(_glob_hit(g, f) for g in globs)]


# ---- git facts about one branch (moved from harvest) --------------------------------------------

def touched_files(repo, trunk, branch):
    """The files ``origin/<branch>`` changed since it left the trunk."""
    r = H.sh(['git', 'diff', '--name-only', f'origin/{trunk}...origin/{branch}'], cwd=repo)
    return [l for l in r.stdout.splitlines() if l.strip()]


def changed_lines(repo, trunk, branch):
    """Lines ``origin/<branch>`` adds plus removes since it left the trunk (a binary file counts
    as none); None when git cannot say."""
    r = H.sh(['git', 'diff', '--numstat', f'origin/{trunk}...origin/{branch}'], cwd=repo)
    if r.returncode != 0:
        return None
    total = 0
    for line in r.stdout.splitlines():
        parts = line.split('\t')
        total += sum(int(p) for p in parts[:2] if p.isdigit())
    return total


#: The branch kind of a ``lane: direct`` Feature (:data:`asf.conventions.DEFAULT_BRANCH_PREFIXES`)
DIRECT = 'direct'


def small_task(items, item):
    """True when ``item`` is a Task of a ``size: s`` Feature on the full lane."""
    card = (items or {}).get(item or '') or {}
    if card.get('type') != 'task':
        return False
    from asf.feeder import rows as feeder_rows  # local: the feeder reads the lane's states
    feature = feeder_rows._task_feature(items, card) or {}
    return str(feature.get('size') or '').lower() == 's' and feature.get('lane') != DIRECT


def review_waived(conv, kind, items, item, lines):
    """Why a code branch needs no ASF review of its head, else '': a ``lane: direct`` Feature's
    branch (one session, no review round — CI and the gate judge it), or a small Feature's Task
    whose diff is under ``review.skip_under_lines``."""
    if kind == DIRECT:
        return 'direct lane: CI and the gate judge it, no review round'
    limit = conv.review_skip_under_lines()
    if limit and lines is not None and lines < limit and small_task(items, item):
        return f'size s: {lines} changed lines < review.skip_under_lines {limit}'
    return ''


def first_commit_body(repo, trunk, branch):
    """The body of the oldest commit ``origin/<branch>`` carries past the trunk — a direct
    Feature's "what and how" note, which its PR description carries — or ''."""
    r = H.sh(['git', 'log', '--reverse', '--no-merges', '--format=%H',
              f'origin/{trunk}..origin/{branch}'], cwd=repo)
    shas = r.stdout.split() if r.returncode == 0 else []
    if not shas:
        return ''
    body = H.sh(['git', 'log', '-1', '--format=%b', shas[0]], cwd=repo)
    return body.stdout.strip() if body.returncode == 0 else ''


def _subjects(repo, trunk, branch):
    return H.sh(['git', 'log', '--no-merges', '--format=%s', f'origin/{trunk}..origin/{branch}'],
                cwd=repo).stdout.splitlines()


def commits_name_item(repo, trunk, branch, item):
    """True when every commit subject on ``origin/<branch>`` not on ``origin/<trunk>`` names
    ``item`` as a token."""
    subjects = _subjects(repo, trunk, branch)
    return bool(subjects) and all(githooks.names_item(s, item) for s in subjects)


#: the author and committer fields :func:`reword_branch` carries onto each rewritten commit
_IDENT = (('GIT_AUTHOR_NAME', '%an'), ('GIT_AUTHOR_EMAIL', '%ae'), ('GIT_AUTHOR_DATE', '%ad'),
          ('GIT_COMMITTER_NAME', '%cn'), ('GIT_COMMITTER_EMAIL', '%ce'),
          ('GIT_COMMITTER_DATE', '%cd'))


def reword_branch(repo, trunk, branch, item, kind=None):
    """``(new_tip, n)``: ``origin/<branch>`` from its merge-base with ``origin/<trunk>`` onward,
    each subject not naming ``item`` rewritten by :func:`asf.workers.githooks.name_subject` —
    trees, authors, committers and dates identical, body untouched, no checkout and no hook run
    (``git commit-tree``). ``(None, 0)`` when nothing needs a reword or a commit cannot be
    rebuilt (a merge commit, a git error)."""
    tip = H.sh(['git', 'rev-parse', '--verify', '-q', f'origin/{branch}'], cwd=repo).stdout.strip()
    base = H.sh(['git', 'merge-base', f'origin/{trunk}', f'origin/{branch}'],
                cwd=repo).stdout.strip()
    if not tip or not base:
        return None, 0
    revs = H.sh(['git', 'rev-list', '--reverse', '--topo-order', f'{base}..{tip}'],
                cwd=repo).stdout.split()
    parent, n = base, 0
    for sha in revs:
        r = H.sh(['git', 'log', '-1', '--date=raw',
                  '--format=' + '%x00'.join(f for _, f in _IDENT) + '%x00%T%x00%P', sha], cwd=repo)
        fields = r.stdout.rstrip('\n').split('\x00')
        if r.returncode != 0 or len(fields) != len(_IDENT) + 2 or len(fields[-1].split()) != 1:
            return None, 0  # a merge commit, or no commit: the lane refuses it otherwise
        raw = H.sh(['git', 'cat-file', 'commit', sha], cwd=repo).stdout
        message = raw.split('\n\n', 1)[1] if '\n\n' in raw else ''
        subject, nl, body = message.partition('\n')
        named = githooks.name_subject(subject, item, kind)
        if named == subject and fields[-1] == parent:
            parent = sha
            continue
        n += named != subject
        env = dict(os.environ, **{k: v for (k, _), v in zip(_IDENT, fields)})
        made = subprocess.run(['git', 'commit-tree', fields[-2], '-p', parent],
                              input=named + nl + body, cwd=repo, capture_output=True, text=True,
                              env=H.clean_env(env))
        parent = made.stdout.strip()
        if made.returncode != 0 or not parent:
            return None, 0
    return (parent, n) if n else (None, 0)


def has_adjudicate_commit(repo, trunk, branch):
    """True when a commit on the branch opens with ``adjudicate(`` — a ruling committed to the
    product repo instead of the record (B-0054)."""
    return any(ADJUDICATE_SUBJECT_RE.match(s) for s in _subjects(repo, trunk, branch))


def merge_commits(repo, trunk, branch):
    r = H.sh(['git', 'log', '--merges', '--format=%h %s', f'origin/{trunk}..origin/{branch}'],
             cwd=repo)
    return [l for l in r.stdout.splitlines() if l.strip()]


def lane_refusal(repo, trunk, branch, item, conv=None):
    """``(kind, text)`` for a branch the lane refuses before any gate, or None: a merge commit
    on it (B-0056), a commit not naming the item, or a line it adds to customer content that
    carries a forbidden marker (:func:`asf.customer_content.refusal`, ``file:line`` each) — each
    a correction back to its session."""
    merges = merge_commits(repo, trunk, branch)
    if merges:
        return 'merge', (f'merge commit on a lane branch: {merges[0]} — a lane branch is straight '
                         f'commits on origin/{trunk}: rebase onto it, never merge origin/{branch} '
                         f'or origin/{trunk} into it; the factory publishes the rebased branch')
    if not item or not commits_name_item(repo, trunk, branch, item):
        return lifecycle.NAMING, (
            f'commits do not name {item or "an item id"}: every commit subject on the branch '
            f'names its item — the lane could not reword them: reword them; the factory '
            f'publishes the rewritten branch')
    if conv is not None:
        return customer_content.refusal(repo, trunk, branch, conv)
    return None


def deliverable_of(conv, branch, item):
    kind = conv.branch_kind(branch)
    if kind in ('spec', 'plan') and item:
        return f'{conv.doc_dir(kind)}/{item.lower()}.md'
    return None


def already_on_trunk(repo, trunk, branch, conv, item):
    """``(landed, extras)`` — B-0057: every file the branch touched is identical on the trunk; or,
    for a spec/plan branch, its one deliverable is (``extras`` names what else it carried)."""
    files = touched_files(repo, trunk, branch)
    r = H.sh(['git', 'diff', '--name-only', '--no-renames', f'origin/{trunk}',
              f'origin/{branch}', '--', *[f':(literal){f}' for f in files]],
             cwd=repo) if files else None
    differ = set(r.stdout.splitlines()) if r is not None and r.returncode == 0 else set(files)
    same = [f for f in files if f not in differ]
    deliverable = deliverable_of(conv, branch, item)
    if deliverable and deliverable in same:
        return True, [f for f in files if f not in same]
    if len(same) == len(files):
        return True, []
    return False, []


def superseded_by(items, item):
    """The state that supersedes a branch, or None: a removed card (B-0065) or a Bug the record
    holds Closed/Resolved (B-0057)."""
    card = (items or {}).get(item or '') or {}
    if card.get('removed'):
        return 'removed'
    if card.get('type') == 'bug' and card.get('state') in ('Closed', 'Resolved'):
        return card['state']
    return None


def archive_commit(repo, branch, message):
    """One empty ``[skip ci]`` commit over ``origin/<branch>`` (B-0066); its sha, or ''."""
    tip = H.sh(['git', 'rev-parse', f'origin/{branch}'], cwd=repo).stdout.strip()
    if not tip:
        return ''
    made = H.sh(['git', 'commit-tree', f'{tip}^{{tree}}', '-p', tip, '-m', message], cwd=repo)
    return made.stdout.strip() if made.returncode == 0 else ''


def api_ref(slug, refspec, lease=None):
    """Make one ref-only ``refspec`` on hosted ``slug`` through the host's API; ``(ok, why)``.
    ``<sha>:refs/heads/<b>`` creates the ref at ``sha`` (one already there at ``sha`` is done);
    ``:refs/heads/<b>`` deletes it — under ``lease`` (``refs/heads/<b>:<sha>``) only while its
    tip is still ``sha``, as retention's hosted delete does."""
    src, _, dst = refspec.partition(':')
    ref = dst[len('refs/'):] if dst.startswith('refs/') else dst
    read = ['api', f'repos/{slug}/git/ref/{ref}', '--jq', '.object.sha']
    if not src:
        if lease:
            want = lease.rpartition(':')[2]
            rc, out, err = H._gh(read)
            if rc != 0:
                return False, H.tail(err or out) or 'ref unreadable'
            if out.strip() != want:
                return False, f'tip moved ({out.strip()[:9]} is not {want[:9]}) — kept'
        rc, out, err = H._gh(['api', '-X', 'DELETE', f'repos/{slug}/git/refs/{ref}'])
        return rc == 0, '' if rc == 0 else (H.tail(err or out) or f'gh exit {rc}')
    rc, out, err = H._gh(['api', '-X', 'POST', f'repos/{slug}/git/refs',
                          '-f', f'ref=refs/{ref}', '-f', f'sha={src}'])
    if rc == 0:
        return True, ''
    rc2, cur, _ = H._gh(read)
    if rc2 == 0 and cur.strip() == src:
        return True, ''
    return False, H.tail(err or out) or f'gh exit {rc}'


def api_archive_commit(slug, repo, tip, message):
    """:func:`archive_commit` made on the host — ``tip``'s tree, ``tip`` its one parent — so the
    archive ref is created there with no push; its sha, or ''."""
    tree = H.sh(['git', 'rev-parse', f'{tip}^{{tree}}'], cwd=repo).stdout.strip()
    if not tree:
        return ''
    rc, out, _err = H._gh(['api', '-X', 'POST', f'repos/{slug}/git/commits',
                           '-f', f'message={message}', '-f', f'tree={tree}',
                           '-f', f'parents[]={tip}', '--jq', '.sha'])
    return out.strip() if rc == 0 else ''


def api_archive_stands(slug, branch, sha, tip):
    """True when ``archive/<branch>`` already stands on the host at ``sha``, or at an earlier
    pass's archive commit over ``tip`` — an archive made before counts as made."""
    rc, cur, _ = H._gh(['api', f'repos/{slug}/git/ref/heads/archive/{branch}',
                        '--jq', '.object.sha'])
    cur = cur.strip() if rc == 0 else ''
    if not cur or cur == sha:
        return bool(cur)
    rc, parents, _ = H._gh(['api', f'repos/{slug}/git/commits/{cur}', '--jq', '.parents[].sha'])
    return rc == 0 and tip in parents.split()


def gate_reader(repo, branch):
    def read(path):
        r = H.sh(['git', 'show', f'origin/{branch}:{path}'], cwd=repo)
        return r.stdout if r.returncode == 0 else None
    return read


def widen_candidates(files, item_writes, touched=(), read=None, own=False):
    """``(needs, tests, exercised)`` — the gate's facts for ``widen_footprint``
    (:mod:`asf.feeder.widen`), read off the files a red gate named."""
    named = [f for f in files or () if read is None or read(f) is not None]
    stems = {}
    for f in named:
        if widen.is_test_path(f):
            stems[f] = widen.import_stems((read(f) if read else '') or '', f)
    exercised = [t for t, s in stems.items() if touched and widen.imported(s, touched)]
    tests = list(stems) if own else exercised
    reached = [f for f in named if f not in stems
               and widen.imported([s for t in tests for s in stems[t]], [f])]
    needs = widen.outside(tests + reached, item_writes) if item_writes else []
    return needs, tests, exercised


def hold_with_correction(state_dir, branch, record, kind, text, out, files=(), item_writes=(),
                         touched=(), conv=None, own=False, read=None):
    """Hold ``branch`` and hand it back to its session (:func:`asf.workers.lifecycle.hold`).
    ``'held'`` — or ``'foreign'`` for a red naming only files outside its footprint (no round),
    or ``'timed-out'`` for a gate that ran out of time (no round). ``own``: the red is this
    branch's whatever files it names (gated on a trunk green alone)."""
    job = record.get('job') or branch
    if kind == 'gate' and text.startswith(H.TIMED_OUT):  # B-0082: a clock is not a defect
        out(f'{H.TIMED_OUT} {branch}: {text} — retried next tick')
        return 'timed-out'
    if kind == 'gate' and touched and landing_class(conv or H.DEFAULTS, touched) == DOCS \
            and not own and not footprint.overlaps(touched, files):
        out(f'foreign {branch}: gate red, its diff is docs only — re-gated next tick')
        return 'foreign'
    item_writes = widen.norm_writes(item_writes)
    reach, what = (item_writes, 'writes') if item_writes else (touched, 'diff')
    needs, tests, exercised = (widen_candidates(files, item_writes, touched, read, own)
                               if kind == 'gate' and files else ([], [], []))
    if kind == 'gate' and not own and not exercised and files and reach \
            and footprint.overlaps(reach, files) is None:
        out(f'foreign {branch}: gate red outside its {what}: {files[0]} — re-gated next tick')
        return 'foreign'
    if needs:
        fact = f'gate: {", ".join(tests) or files[0]} red alone, trunk green'
        fields, line = lifecycle.footprint_hold(
            dict(record, branch=branch, job=job), needs, fact,
            f'{text}\nfootprint: the red is outside writes: — needs {" ".join(needs)}',
            now_iso(), tests=tests)
        H.mark_session(state_dir, job, **fields)
        out(line)
        return 'held'
    fields, line = lifecycle.hold(H.sessions_path(state_dir), dict(record, branch=branch, job=job),
                                  kind, text, now_iso())
    H.mark_session(state_dir, job, **fields)
    out(line)
    return 'held'


# ---- the pure transition ----------------------------------------------------------------------

def incomplete(facts):
    """True when the head's approving review lacks the ``read as the customer`` check row its
    diff owes — the diff touches ``customer_content.paths`` (:mod:`asf.customer_content`)."""
    rv = facts.get('review') or {}
    return bool(facts.get('customer')) and rv.get('verdict') == review_mod.APPROVED \
        and not rv.get('customer_row')


def review_reason(facts):
    """``(round wanted, why)`` for a head no review approved yet."""
    rv = facts.get('review') or {}
    if not rv:
        return 1, 'no ASF review yet'
    nxt = int(rv.get('round') or 0) + 1
    if not rv.get('current'):
        return nxt, f"{rv.get('path')} predates the head"
    if incomplete(facts):
        return nxt, (f"{rv.get('path')} is incomplete: no `{customer_content.CHECK_ROW}` row"
                     f" for the {len(facts['customer'])} customer page(s) the diff touches")
    return nxt, f"{rv.get('path')} reads {rv.get('text') or 'no verdict'}"


def next_state(prev, facts):
    """The pure transition: ``prev`` is the branch's last lane record (``{state, head, pr, at,
    reason}``, or None for a branch with none yet) and ``facts`` that branch's facts from
    :func:`facts`. Returns ``(state, reason)`` — ``state`` one of :data:`LANE_STATES` (None: the
    lane does not hold the branch), equal to ``prev['state']`` when nothing moves. No I/O, no
    clock beyond ``facts['now']``."""
    f = facts or {}
    rec = prev or {}
    s = rec.get('state')
    keep = (s, rec.get('reason', ''))
    head = f.get('head')
    pr = f.get('pr') or {}
    n = pr.get('number')
    if s == REAPED:
        return keep
    # T11 — merged by the host; ours when MERGING/QUEUED came first (R3)
    if pr.get('state') == 'MERGED' and s != MERGED and (
            not head or pr.get('head') in (None, '', head) or s in (MERGING, QUEUED)):
        if s in (MERGING, QUEUED):
            return MERGED, 'method=' + ('queue' if s == QUEUED else rec.get('method') or 'squash')
        return MERGED, 'method=external'
    if s == MERGED:
        return keep
    if f.get('live'):
        return keep
    if s == MERGING and f.get('merging_landed'):  # R8: the push reached the trunk, the line did not
        return MERGED, 'method=' + (rec.get('method') or 'ff')
    if s is None and f.get('orphan'):  # a run-less branch the lane closes (orphan_facts)
        return STALE, f['orphan']
    closed = f.get('closed')
    if s == STALE:
        if not closed and pr.get('state') == 'OPEN' and head:
            return PR_OPEN, f'PR #{n} reopened'  # R6
        return keep
    if f.get('on_trunk') and (s is not None or f.get('ended')):
        return MERGED, 'method=on-trunk'
    if closed and (s is not None or f.get('ended')):
        return STALE, f'{f.get("item")} is {closed} in the record'
    if not head:
        if f.get('gone_merged'):
            return MERGED, 'method=on-trunk'
        return (None, '') if s is None else (STALE, 'branch gone')
    if pr.get('state') == 'CLOSED' and pr.get('head') in (None, '', head) \
            and (s is not None or f.get('ended')):
        return STALE, f'PR #{n} closed unmerged'
    # R4 — a moved head: whatever reviewed or gated the old one is history
    if s in OPEN_STATES and rec.get('head') and head != rec['head']:
        return PUSHED, f"head moved {rec['head'][:7]} → {head[:7]}"
    corr = f.get('correction')
    if s is None or s == BACK:
        if corr:
            return keep if s == BACK else (BACK, f"kind={corr.get('kind') or 'correction'}")
        if s == BACK:
            return PUSHED, 'correction answered'
        if not f.get('ended') or f.get('landed') or not f.get('ahead'):
            return None, ''
        return PUSHED, 'adopted' if f.get('adopt') else 'finished'
    if s == PUSHED:
        at = _parse_at(rec.get('at'))
        if at is not None and f.get('now') and f.get('stale_after') \
                and f['now'] - at > f['stale_after']:
            return STALE, 'unmoved in PUSHED past lane.stale_after'
        if f.get('refusal'):
            return BACK, f"kind={f['refusal'][0]}"
        if f.get('mode') != 'pr':
            return PR_OPEN, 'fast-forward: the branch is its own PR'
        if not f.get('host'):
            return PR_OPEN, 'no PR host: the product lands it'
        if pr.get('state') == 'OPEN':
            return PR_OPEN, f'PR #{n}'
        return PR_OPEN, 'open a PR'
    if s == PR_OPEN and f.get('mode') == 'pr' and not f.get('host'):
        return keep
    if s in (PR_OPEN, REVIEW):  # T3/T4/T5: a review of the head already there counts at once
        rv = f.get('review') or {}
        if not f.get('review_required'):
            return GATE, f"review: none ({f['review_waived']})" if f.get('review_waived') \
                else 'review: none'
        if rv.get('current') and rv.get('verdict') == review_mod.APPROVED \
                and not incomplete(f):
            return GATE, f"{rv.get('path')} approved its head"
        if rv.get('current') and rv.get('verdict') == review_mod.CHANGES:
            return BACK, 'kind=review'
        rnd, why = review_reason(f)
        reason = f'round {rnd} wanted: {why}'
        return (REVIEW, reason) if s != REVIEW or reason != rec.get('reason') else keep
    if s == MERGING:
        return keep if f.get('harvest_running') else (GATE, 'no merge seen after MERGING: gated again')
    if s == QUEUED:
        if pr.get('state') == 'OPEN' and not pr.get('queued'):
            return WAITING, 'queue-rejected'
        return keep
    return keep


def skip_regate(prev, head, moved, product):
    """R25: True when ``prev`` (a WAITING/GATE record) was gated green at ``head`` and the trunk
    has since moved only on docs roots — ``moved`` the files the trunk changed since then (None:
    unknown). A green branch is not gated again for a docs-only trunk move."""
    green = (prev or {}).get('green') or {}
    if not green or green.get('head') != head or moved is None:
        return False
    return not moved or landing_class(product, moved) == DOCS


# ---- the lane: one pass's context --------------------------------------------------------------

class Lane:
    """One pass over a product's lane branches: the product, its state dir, the host, the
    record's items, and where lines go. Read and written through :meth:`gather` and
    :meth:`advance`."""

    def __init__(self, product, state_dir=None, out=print, dry_run=False, items=None,
                 root=None, now=None):
        self.product = product
        self.conv = product.conventions
        self.repo = os.path.abspath(product.repo_dir) if product.repo_dir else None
        self.state_dir = os.path.abspath(state_dir or env.state_dir(product))
        self.path = H.sessions_path(self.state_dir)
        self.out = out
        self.dry_run = dry_run
        self.items = items
        self.root = root
        self.now = now
        self.trunk = self.conv.main
        self.mode = 'pr' if landing(product) == LANDING_PR else 'ff'
        self.slug = repo_slug(product) if self.mode == 'pr' else None
        self.host = GitHubHost(product, self) if self.slug else FastForwardHost(product, self)
        #: ``conventions.merge: auto`` — the lane merges green, reviewed PRs itself, and takes up
        #: the open PRs on the trunk no factory item made (:meth:`foreign_pr`)
        self.auto = approvals.merge_auto(product)
        self.results = {}
        self.opened = 0
        self.orphans = {}
        self.orphans_taken = 0
        self.ref_wt = None
        self.ref_wt_error = None
        self.ref_ok = 0
        self.ref_failures = []
        #: the wave's pass defers its ref pushes (each runs the product's pre-push hook) until
        #: after its launches: ``(kind, facts, why)`` queued here, run by :func:`push_deferred`
        self.defer_pushes = False
        self.deferred = []
        #: the CI start queue (:mod:`asf.ci_queue`) for this pass, read once when first asked
        self.ci_queue = None
        #: the hosted origin's ``owner/name`` the ref pushes go through (:meth:`ref_host`)
        self._ref_slug = None

    # ---- facts --------------------------------------------------------------------------

    def remote_heads(self):
        """``{branch: sha}`` of every head on origin — the pass's one ``git ls-remote``."""
        out = {}
        r = H.sh(['git', 'ls-remote', '--heads', 'origin'], cwd=self.repo)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith('refs/heads/'):
                out[parts[1][len('refs/heads/'):]] = parts[0]
        return out

    def is_ancestor(self, a, b):
        return bool(a) and H.sh(['git', 'merge-base', '--is-ancestor', a, b],
                                cwd=self.repo).returncode == 0

    def gather(self, prs=True):
        """``{branch: facts}`` for every lane branch; ``prs``: read the host's PR list (the
        in-process pass), else take the PR number off the lane record (the gate pass)."""
        conv, trunk = self.conv, self.trunk
        runs = lifecycle.by_branch(self.path)
        heads = self.remote_heads()
        self.trunk_sha = heads.get(trunk) or H.sh(['git', 'rev-parse', f'origin/{trunk}'],
                                                 cwd=self.repo).stdout.strip()
        pr_map = self.host.prs() if prs else {}
        self.orphans = self.orphan_claims(runs, heads, pr_map) if prs else {}
        self.orphans_taken = 0
        running = H.try_lock_held(self.state_dir)
        names = {b for b in heads if conv.branch_kind(b) and b != trunk}
        names |= {b for b, r in runs.items() if b != trunk and b and (
            b in heads or lifecycle.eligible(r) or (r.get('lane') or {}).get('state') in OPEN_STATES)}
        names |= {b for b, p in pr_map.items() if conv.branch_kind(b) and p.get('state') == 'OPEN'}
        if self.auto:  # every open PR on the trunk, whoever opened it (foreign_pr decides)
            names |= {b for b, p in pr_map.items() if b and b != trunk
                      and p.get('state') == 'OPEN' and p.get('base') in (None, trunk)}
        out = {}
        for b in sorted(names):
            f = self.branch_facts(b, runs.get(b), heads.get(b), pr_map.get(b), prs, running)
            if f is not None:
                out[b] = f
        return out

    def branch_facts(self, b, run, head, pr, prs, running):
        conv, trunk, repo = self.conv, self.trunk, self.repo
        rec = (run or {}).get('lane') or {}
        item = item_of(b, run)
        if not prs and rec.get('pr'):
            pr = {'number': rec['pr'], 'state': 'OPEN'}
        f = {'branch': b, 'item': item, 'kind': conv.branch_kind(b), 'head': head, 'run': run,
             'prev': rec or None, 'live': lifecycle.is_live(run) if run else False,
             'ended': bool(run and run.get('ended')), 'landed': lifecycle.landed(run),
             'correction': lifecycle.pending_correction(run, self.path) if run else None,
             'pr': pr, 'mode': self.mode, 'host': self.mode != 'pr' or bool(self.slug),
             'now': self.now or time.time(), 'stale_after': conv.lane_stale_after_s(),
             'harvest_running': running, 'trunk': self.trunk_sha}
        foreign = self.foreign_pr(b, run, pr, item)
        if foreign:  # an open PR a person or a product session opened: adopted for review (T2)
            item = foreign
            f.update(item=item, adopt=True, ended=True)
        elif run is None and b in self.orphans:
            # an orphan: a lane branch no run holds — the pre-record lane's
            # `<prefix><plan>-t3`, a card's second branch — is closed or picked back up
            if self.orphans_taken >= ORPHANS_PER_PASS:
                if self.orphans_taken == ORPHANS_PER_PASS:
                    self.out(f'orphans: {ORPHANS_PER_PASS} taken up this pass — the rest next pass')
                    self.orphans_taken += 1
                return None
            f = self.orphan_facts(f, b, head, pr)
            if f is not None:
                self.orphans_taken += 1
            if f is None or not f.pop('pick_up', False):
                return f
            item = f['item']
        f['foreign'] = is_pr_item(item)
        if run is None and not foreign:  # adoption: a lane branch, or an open PR, no run holds (T2)
            card = (self.items or {}).get(item or '') or {}
            open_pr = (pr or {}).get('state') == 'OPEN'
            # a branch whose name only looks like an id (`…-retro-2026-09-19` → RETRO-2026) is
            # nobody's lane: with no card no session can answer a hold, so it sits in BACK
            unknown = self.items is not None and not card
            if not head or not item or unknown or (not open_pr and (
                    not card or card.get('removed') or card.get('state') in ('Resolved', 'Closed'))):
                return None
            f.update(adopt=True, ended=True)
        if f['landed'] or f['live'] and not (pr or {}).get('state') == 'MERGED':
            return f
        if rec.get('state') == MERGING and rec.get('sha'):
            f['merging_landed'] = self.is_ancestor(rec['sha'], f'origin/{trunk}')
        if not head:
            if rec.get('head'):
                f['gone_merged'] = self.is_ancestor(rec['head'], f'origin/{trunk}')
            elif not rec and lifecycle.eligible(run):
                f['gone_merged'] = True
            return f
        ahead = H.sh(['git', 'rev-list', '--count', f'origin/{trunk}..origin/{b}'],
                     cwd=repo).stdout.strip()
        f['ahead'] = int(ahead) if ahead.isdigit() else 0
        if f['ahead'] == 0:
            f['on_trunk'] = lifecycle.finished(run) or bool(rec)
            return f
        done, extras = already_on_trunk(repo, trunk, b, conv, item)
        f['on_trunk'], f['extras'] = done, extras
        if done:
            return f
        f['closed'] = superseded_by(self.items, item)
        f['files'] = touched_files(repo, trunk, b)
        f['class'] = landing_class(self.product, f['files'])
        # a PR no factory item made merges only on a factory review of its head, whatever the
        # class; and its commits name no item, so the lane's naming refusal is not its to answer
        f['review_required'] = f['foreign'] or conv.review_required(f['class'])
        if f['review_required'] and not f['foreign'] and f['class'] == CODE:
            lines = (changed_lines(repo, trunk, b) if f['kind'] != DIRECT
                     and small_task(self.items, item) else None)
            f['review_waived'] = review_waived(conv, f['kind'], self.items, item, lines)
            f['review_required'] = not f['review_waived']
        if rec.get('state') in (None, PUSHED, BACK) and not f['foreign']:
            f['refusal'] = lane_refusal(repo, trunk, b, item, conv)
        f['customer'] = customer_content.touched(conv, f['files'])
        # a customer page is never landed unread: its diff needs a review whatever its class
        f['review_required'] = f['review_required'] or bool(f['customer'])
        if f['review_required'] and rec.get('state') in (None, PUSHED, BACK, PR_OPEN, REVIEW):
            rv = review_mod.review_at(repo, conv, f'origin/{b}', item)
            if rv:
                rv['current'] = review_mod.is_current(repo, conv, f'origin/{b}', rv, head)
                rv['customer_row'] = customer_content.has_customer_row(rv.pop('body', ''))
            f['review'] = rv
        return f

    # ---- orphans --------------------------------------------------------------------------

    def resolve_item(self, b, pr):
        """The one open-or-done card a run-less branch belongs to, or None: the id token in its
        name when that is a card, else :func:`asf.record.match.match_event` over the record
        (``links.prs``, ``links.branches``, a ``legacy_id`` — the pre-record lane's
        ``<plan>-t3`` names). A code branch takes a Task or Bug, a spec/plan branch a Feature;
        more than one candidate is no answer."""
        items = self.items or {}
        item = item_of(b, None)
        if item and item in items:
            return item
        from asf.record import match
        want = ('feature',) if self.conv.branch_kind(b) in ('spec', 'plan', DIRECT) \
            else ('task', 'bug')
        ids, _why = match.match_event(items, branch=b, pr=(pr or {}).get('number'),
                                      title=(pr or {}).get('title'),
                                      prefixes=match.branch_prefixes(self.product))
        ids = [i for i in ids if (items.get(i) or {}).get('type') in want]
        return ids[0] if len(ids) == 1 else None

    def foreign_pr(self, b, run, pr, item):
        """Under ``conventions.merge: auto``, the :func:`pr_item` id of an open PR on the trunk
        that no run holds and no card claims — one a person or the product's own session opened
        — else None. The lane reviews it (a review session it launches) and merges it on green
        like any lane branch; under ``manual`` it is never touched."""
        pr = pr or {}
        if not self.auto or run is not None or pr.get('state') != 'OPEN' or not pr.get('number'):
            return None
        if pr.get('base') not in (None, self.trunk):
            return None
        if (self.orphans.get(b) or {}).get('item') or (item and item in (self.items or {})):
            return None  # a card claims it: the lane's own adoption (T2) or orphan rule
        if self.items is None and self.conv.branch_kind(b):
            return None  # no record to ask: a lane-prefixed branch stays the lane's own
        return pr_item(pr['number'])

    def orphan_claims(self, runs, heads, pr_map):
        """``{branch: {item, owner}}`` of every orphan — a lane-prefixed head no run holds, with
        a record to judge it by. ``owner``: the branch that answers for the card instead (a run
        on it, else the orphan with the newest PR), or None when this one does."""
        if self.items is None:
            return {}
        def lane_of(b):  # a Feature's spec and plan are two deliverables; code is one
            kind = self.conv.branch_kind(b)
            return kind if kind in ('spec', 'plan') else 'code'

        claimed = {}
        for b, r in runs.items():
            if r.get('item') and b and b != self.trunk:
                claimed.setdefault((str(r['item']), lane_of(b)), b)
        found = {}
        for b in heads:
            if b == self.trunk or b in runs or not self.conv.branch_kind(b):
                continue
            found[b] = self.resolve_item(b, pr_map.get(b))
        newest = {}
        for b, item in sorted(found.items()):
            n = int((pr_map.get(b) or {}).get('number') or 0) \
                if (pr_map.get(b) or {}).get('state') == 'OPEN' else 0
            key = (item, lane_of(b))
            if item and (key not in newest or n > newest[key][0]):
                newest[key] = (n, b)
        out = {}
        for b, item in found.items():
            key = (item, lane_of(b))
            owner = claimed.get(key) if item else None
            if item and not owner and newest[key][1] != b:
                owner = newest[key][1]
            out[b] = {'item': item, 'owner': owner}
        return out

    def head_age_s(self, b):
        """Seconds since ``origin/<b>``'s head was committed, or None."""
        t = H.sh(['git', 'log', '-1', '--format=%ct', f'origin/{b}'], cwd=self.repo).stdout.strip()
        return (self.now or time.time()) - int(t) if t.isdigit() else None

    def merges_clean(self, b):
        """Whether ``origin/<b>`` merges onto the trunk with no conflict."""
        return H.sh(['git', 'merge-tree', '--write-tree', '--quiet', f'origin/{self.trunk}',
                     f'origin/{b}'], cwd=self.repo).returncode == 0

    def orphan_facts(self, f, b, head, pr):
        """An orphan's facts: adopted like any run-less branch (T2) when its card is open and it
        answers for it and can still land; else ``orphan`` names why the lane closes it —
        landed (its PR merged, or its diff on the trunk), its card done or removed, another
        branch answering for the card, no card at all, or too far behind to rebase — once it
        has sat past ``lane.stale_after`` (a done or removed card, or a landing, at once). None:
        leave it this pass."""
        claim = self.orphans[b]
        item, owner = claim['item'], claim['owner']
        card = (self.items or {}).get(item or '') or {}
        pr = pr or {}
        f.update(item=item, adopt=True, ended=True)
        if not head:
            return None
        if pr.get('state') == 'MERGED' and pr.get('head') in (None, '', head):
            return f  # T11: found merged (the host)
        ahead = H.sh(['git', 'rev-list', '--count', f'origin/{self.trunk}..origin/{b}'],
                     cwd=self.repo).stdout.strip()
        f['ahead'] = int(ahead) if ahead.isdigit() else 0
        if f['ahead'] == 0:
            f['on_trunk'] = True
            return f
        done, extras = already_on_trunk(self.repo, self.trunk, b, self.conv, item)
        if done:
            f['on_trunk'], f['extras'] = True, extras
            return f
        closed = superseded_by(self.items, item) or (
            card.get('state') if card.get('state') in ('Resolved', 'Closed') else None)
        if closed:
            f['orphan'] = f'{item} is {closed} in the record'
            return f
        age = self.head_age_s(b)
        stale = age is not None and age > f['stale_after']
        if owner:
            if stale:
                f['orphan'] = f'{item} is answered by {owner}'
            return f if stale else None
        if not card:
            if stale:
                f['orphan'] = 'no card in the record claims it (no id, link or legacy_id)'
            return f if stale else None
        if stale and not self.merges_clean(b):
            f['orphan'] = f'too far behind {self.trunk} to rebase; {item} goes back to the feeder'
            return f
        if stale and pr.get('state') == 'CLOSED':
            f['orphan'] = f'PR #{pr.get("number")} closed unmerged'
            return f
        f['pick_up'] = True  # an open card this branch alone answers for: adopted (T2)
        return f

    # ---- ref-only pushes ----------------------------------------------------------------

    def ref_checkout(self):
        """The clean checkout this pass's ref pushes leave from — detached at ``origin/<trunk>``
        under ``<state>/ref-push/`` (:data:`REF_PUSH_DIR`), made on the first push and removed by
        :meth:`finish_ref_pushes`. ``''`` when it could not be made (:attr:`ref_wt_error`)."""
        if self.ref_wt is not None:
            return self.ref_wt
        holder = os.path.join(self.state_dir, REF_PUSH_DIR)
        try:
            os.makedirs(holder, exist_ok=True)
            now = time.time()
            for name in os.listdir(holder):  # a crashed pass's leftover
                old = os.path.join(holder, name)
                if now - os.path.getmtime(old) > REF_PUSH_LEFTOVER_S:
                    H.sh(['git', 'worktree', 'remove', '--force', old], cwd=self.repo)
                    shutil.rmtree(old, ignore_errors=True)
            path = tempfile.mkdtemp(prefix='wt-', dir=holder)
        except OSError as e:
            self.ref_wt, self.ref_wt_error = '', f'no ref-push checkout: {e}'
            return self.ref_wt
        os.rmdir(path)
        add = H.sh(['git', 'worktree', 'add', '-q', '--detach', path, f'origin/{self.trunk}'],
                   cwd=self.repo)
        if add.returncode != 0:
            self.ref_wt = ''
            self.ref_wt_error = (f'no checkout at origin/{self.trunk} to push from: '
                                 f'{H.tail(add.stderr or add.stdout)}')
        else:
            self.ref_wt = path
        return self.ref_wt

    def ref_host(self):
        """The origin's ``owner/name`` when it is a hosted repo (:func:`repo_slug`), else None —
        read once a pass. Ref pushes go through its API then (:meth:`ref_push`)."""
        if getattr(self, '_ref_slug', None) is None:
            # only an origin that IS a hosted repo takes the API path: a configured PR slug beside
            # a local-path origin (a fixture, a mirror) must still move the origin's own refs
            from asf.init import slug_from_url
            url = H.sh(['git', 'remote', 'get-url', 'origin'], cwd=self.repo).stdout.strip() \
                if getattr(self, 'repo', None) else ''
            hosted = slug_from_url(url)
            self._ref_slug = (hosted and (getattr(self, 'slug', None) or hosted)) or ''
        return self._ref_slug or None

    def ref_gone(self, branch):
        """True when ``origin`` holds no ``branch`` — a delete already done counts as done."""
        r = H.sh(['git', 'ls-remote', '--heads', 'origin', branch], cwd=self.repo)
        return r.returncode == 0 and not r.stdout.strip()

    def ref_push(self, refspec, what, lease=None, done=None):
        """Push the one ref-only ``refspec`` (``<sha>:refs/heads/…`` or ``:refs/heads/…``) from
        :meth:`ref_checkout`, over ``lease`` (``refs/heads/<b>:<sha>``) when given. A refusal is a
        ``ref push failed`` line in the tick's output and a row in status and doctor
        (:meth:`finish_ref_pushes`), never only a log line — unless ``done()`` says the push
        was not needed after all (a delete of a branch already gone). True when it went.

        A hosted origin (:meth:`ref_host`) takes the ref through the host's API instead
        (:func:`api_ref`): a ref carries no content, and each ``git push`` ran the product's
        pre-push hook and took 8-11 s an archive, 2-3 s a delete — one wave step spent 277 s of
        a five-minute tick on them (2026-09-25)."""
        slug = self.ref_host()
        wt = None if slug else self.ref_checkout()
        if slug:
            started = time.monotonic()
            ok, why = api_ref(slug, refspec, lease)
            kind, _, branch = what.partition(' ')
            self.out(f'lane: push {branch or kind} {time.monotonic() - started:.1f}s '
                     f'({kind}, api)')
        elif wt:
            cmd = ['git', 'push', '-q'] + ([f'--force-with-lease={lease}'] if lease else [])
            started = time.monotonic()
            r = H.sh(cmd + ['origin', refspec], cwd=wt)
            kind, _, branch = what.partition(' ')
            self.out(f'lane: push {branch or kind} {time.monotonic() - started:.1f}s ({kind})')
            ok, why = r.returncode == 0, push_why(r.stderr or r.stdout)
        else:
            ok, why = False, self.ref_wt_error
        if ok or (done is not None and done()):
            self.ref_ok += 1
            return True
        self.ref_failures.append({'what': what, 'refspec': refspec, 'why': why, 'at': now_iso()})
        self.out(f'ref push failed: {what} — {why}')
        return False

    def delete_branch(self, f):
        """Delete ``f``'s branch on origin (over its head) after its terminal record was
        written. A failed delete marks the record ``delete: owed`` and the next pass tries again;
        a done one clears the mark. True when the branch is gone."""
        b, head = f['branch'], f.get('head')
        ok = self.ref_push(f':refs/heads/{b}', f'delete {b}',
                           lease=f'refs/heads/{b}:{head}' if head else None,
                           done=lambda: self.ref_gone(b))
        rec = f.get('prev') or {}
        owed = rec.get('delete') == DELETE_OWED
        if rec and ok == owed:  # the mark changes: a new failure, or an owed delete done
            rec = {k: v for k, v in rec.items() if k != 'delete'}
            if not ok:
                rec['delete'] = DELETE_OWED
            self.write(f, rec)
            f['prev'] = rec
        if ok and owed:
            self.out(f'deleted {b} (a delete an earlier pass could not push)')
        return ok

    def push_or_defer(self, kind, f, why=None):
        """A ref-push transition — ``delete`` (:meth:`delete_branch`, after its record) or
        ``archive`` (:meth:`archive`, whose push decides the record) — now, or queued for
        :func:`push_deferred` when this pass defers its pushes. None when queued."""
        if self.defer_pushes:
            self.deferred.append((kind, f, why))
            return None
        return self.archive(f, why) if kind == 'archive' else self.delete_branch(f)

    def finish_ref_pushes(self):
        """Remove the ref-push checkout and keep the pass's failures for the status and doctor
        rows (:data:`REF_PUSH_FAILED`): written when any push failed, cleared when pushes went
        and none failed. One summary line in the tick's output when any failed."""
        if self.ref_wt:
            H.sh(['git', 'worktree', 'remove', '--force', self.ref_wt], cwd=self.repo)
            shutil.rmtree(self.ref_wt, ignore_errors=True)
        self.ref_wt = self.ref_wt_error = None
        if self.dry_run:
            return
        path = os.path.join(self.state_dir, REF_PUSH_FAILED)
        try:
            if self.ref_failures:
                with open(path, 'w', encoding='utf-8') as fh:
                    json.dump({'count': len(self.ref_failures), 'at': now_iso(),
                               'failures': self.ref_failures[-10:]}, fh)
            elif self.ref_ok and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        if self.ref_failures:
            self.out(f'lane: {len(self.ref_failures)} ref push(es) failed this pass — the '
                     f'branches stay on origin and are tried again next pass')

    def ci_admits(self, f, kind):
        """True when the CI start queue lets the run this transition starts go now: ``pr`` (a
        PR opened, whose host run starts on it) or ``trunk`` (a merge or push onto the trunk). A
        product that is not queued always goes. A hold prints the queue's one line."""
        from asf import ci_queue
        if self.ci_queue is None:
            self.ci_queue = ci_queue.Queue(self.product, out=self.out)
        b = f['branch']
        return ci_queue.admit(self.product, f'{kind}:{b}', kind, item=f.get('item') or b,
                              items=self.items, branch=b, files=f.get('files') or (),
                              queue=self.ci_queue).admitted

    # ---- writing ------------------------------------------------------------------------

    def record(self, f, state, reason, **extra):
        pr = (f.get('pr') or {}).get('number') or (f.get('prev') or {}).get('pr')
        rec = {'state': state, 'head': f.get('head'), 'pr': pr, 'at': now_iso(),
               'reason': reason, 'item': f.get('item')}
        rec.update({k: v for k, v in extra.items() if v is not None})
        return rec

    def write(self, f, rec, job=None, **fields):
        """Put ``rec`` on the run that owns the branch (a synthetic run for an adopted one)."""
        if self.dry_run:
            return
        run = f.get('run')
        if run is None:
            stamp = now_iso()
            job = job or f"adopt-{(f.get('item') or f['branch']).lower()}".replace('/', '-')
            kind = BRIEF_KIND.get(f.get('kind'), 'coder')
            run = {'job': job, 'item': f.get('item'), 'branch': f['branch'], 'kind': kind,
                   'started': stamp, 'pid': None, 'ended': stamp,
                   'end_reason': lifecycle.FINISHED, 'adopted': True}
            if kind in ('spec', 'plan'):
                run['feature'] = f.get('item')
            with open(self.path, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(dict(run, lane=rec, **fields), sort_keys=True) + '\n')
            f['run'] = run
            return
        H.mark_session(self.state_dir, run.get('job') or f['branch'], lane=rec, **fields)

    def set(self, f, state, reason, result=None, **extra):
        """Record one transition of ``f``'s branch; ``result`` goes in :attr:`results`."""
        rec = self.record(f, state, reason, **extra)
        was = (f.get('prev') or {}).get('state')
        if self.dry_run:
            self.out(f"lane: DRY {f['branch']} {was or '-'} → {state} ({reason})")
        else:
            self.write(f, rec)
            self.out(f"lane: {f['branch']} {was or '-'} → {state} ({reason})")
        f['prev'] = rec
        if result:
            self.results[f['branch']] = result
        return rec

    # ---- the in-process pass ------------------------------------------------------------

    def advance(self, f):
        """Move ``f``'s branch as far as its facts carry it this pass, performing each
        transition's side effect; the last record written, or None when nothing moved."""
        moved = None
        prev = f.get('prev') or {}
        if prev.get('delete') == DELETE_OWED and not self.dry_run and self.repo \
                and f.get('head') and f['head'] == prev.get('head'):
            self.push_or_defer('delete', f)
        # a branch already held for naming (a session pending, none running): reworded here
        if prev.get('state') == BACK and not f.get('live') \
                and (f.get('correction') or {}).get('kind') == lifecycle.NAMING:
            moved = self.repair_naming(f) or moved
        for _ in range(MAX_STEPS):
            prev = f.get('prev')
            state, reason = next_state(prev, f)
            if state == BACK and reason == f'kind={lifecycle.NAMING}':
                rec = self.repair_naming(f)
                if rec is not None:
                    moved = rec
                    continue
            if state is None or (prev and state == prev.get('state')
                                 and reason == prev.get('reason')):
                break
            if prev and state == prev.get('state') and state != REVIEW:
                break
            rec = self.enter(f, state, reason)
            if rec is None:
                break
            moved = rec
            if state in GATE_STATES or state in TERMINAL_STATES or state == BACK:
                break
        return moved

    def enter(self, f, state, reason):
        b, item = f['branch'], f.get('item')
        prev = f.get('prev') or {}
        pr = f.get('pr') or {}
        if state == MERGED:
            return self.enter_merged(f, reason)
        if state == STALE:
            closed = f.get('closed')
            why = f.get('orphan') or (f'{item} is {closed} in the record' if closed else None)
            if why and f.get('head') and self.repo:
                if self.defer_pushes and not self.dry_run:
                    # the archive push decides the record: the whole transition waits for the
                    # launches, and the branch keeps its state until then (as a refused push would)
                    return self.push_or_defer('archive', f, why)
                return self.archive(f, why)
            self.out(f'stale {b}: {reason}')
            return self.set(f, STALE, reason, result='stale')
        if state == PR_OPEN and reason == 'open a PR':
            if self.dry_run:
                self.out(f'DRY: would open a PR for {b}')
                return self.set(f, PR_OPEN, reason, result='dry')
            from asf.tick import step_prs
            cap = step_prs.prs_per_tick(self.product)
            if self.opened >= cap:
                if self.opened == cap:
                    self.out(f'prs: cap {cap} reached — the rest next tick')
                    self.opened += 1
                return None
            if not self.ci_admits(f, 'pr'):
                return None  # the branch stays PUSHED; the next pass asks the queue again
            self.opened += 1
            number, why = self.host.open(b, item)
            if not number:
                self.out(f'prs: {b} not opened — {why}')
                if prev.get('state') != PUSHED or prev.get('reason') != f'PR not opened: {why}':
                    self.set(f, PUSHED, f'PR not opened: {why}')
                return None
            f['pr'] = {'number': number, 'state': 'OPEN', 'head': f.get('head')}
            self.out(f'prs: opened PR #{number} for {b}')
            return self.set(f, PR_OPEN, f'PR #{number}')
        if state == PR_OPEN and reason.startswith('no PR host'):
            self.out(f'pr-lane {b}')
            return self.set(f, PR_OPEN, reason, result='pr')
        if state == REVIEW:
            rnd, why = review_reason(f)
            what = f"PR #{pr['number']}" if pr.get('number') else 'the head'
            self.out(f'waiting {b}: {what} not approved — {why}: review round {rnd} asked for')
            return self.set(f, REVIEW, reason, result='waiting', round=rnd)
        if state == BACK:
            return self.enter_back(f, reason)
        return self.set(f, state, reason)

    def enter_merged(self, f, reason):
        b, prev, pr = f['branch'], f.get('prev') or {}, f.get('pr') or {}
        method = reason.split('=', 1)[-1]
        if pr.get('state') == 'MERGED':
            sha = pr.get('merge_sha') or f"PR #{pr.get('number')}"
            line = f"landed {b} → PR #{pr.get('number')} {sha} (merged)"
        elif prev.get('state') == MERGING and prev.get('sha') and f.get('merging_landed'):
            sha = prev['sha']
            line = f'landed {b} → {sha} (the push reached {self.trunk})'
        else:
            sha = self.trunk_sha
            extras = f.get('extras') or []
            note = f'; not its deliverable, dropped with the branch: {", ".join(extras)}' \
                if extras else ''
            line = f'landed {b}: already on {self.trunk} at {sha[:7]}{note}'
        if self.dry_run:
            self.out(f'DRY: would mark {b} landed — {line}')
            self.results[b] = 'dry'
            return None
        rec = self.record(f, MERGED, reason, sha=sha, method=method)
        self.write(f, rec, harvested=sha, correction=None)
        f['prev'] = rec
        if f.get('head') and method in ('on-trunk', 'ff'):
            self.close_pr(f, f'Closed by the factory lane: its change is already on '
                             f'{self.trunk} at {str(sha)[:7]}.')
            self.push_or_defer('delete', f)
        elif f.get('head') and f.get('adopt') and method == 'external':
            self.push_or_defer('delete', f)  # merged, left behind
        self.out(line)
        self.results[b] = 'landed'
        return rec

    def close_pr(self, f, why):
        """Close ``f``'s open PR with a comment saying ``why`` (PR mode only)."""
        pr = f.get('pr') or {}
        if pr.get('state') == 'OPEN' and pr.get('number'):
            if self.host.close(pr['number'], why):
                self.out(f"prs: closed PR #{pr['number']} — {why}")

    def archive(self, f, why):
        b, item = f['branch'], f.get('item')
        if self.dry_run:
            self.out(f'DRY: would archive {b} — {why}')
            self.results[b] = 'dry'
            return None
        legacy = self.unclaimed_legacy(b, item)
        if legacy:
            return self.drop_legacy(f, why, legacy)
        label = item or b.rsplit('/', 1)[-1]
        message = f'archive({label}): {b} — {why}; superseded, kept for reference [skip ci]'
        slug = self.ref_host()
        if slug:  # the archive commit is made on the host too: no push at all
            tip = H.sh(['git', 'rev-parse', f'origin/{b}'], cwd=self.repo).stdout.strip()
            sha = api_archive_commit(slug, self.repo, tip, message) if tip else ''
            done = lambda: api_archive_stands(slug, b, sha, tip)  # noqa: E731
        else:
            sha, done = archive_commit(self.repo, b, message), None
        keep = self.ref_push(f'{sha}:refs/heads/archive/{b}', f'archive {b}',
                             done=done) if sha else False
        if not keep:
            self.out(f'held {b}: archive could not be '
                     f'{"pushed" if sha else "made"}')
            self.results[b] = 'held'
            return None
        rec = self.record(f, STALE, why)
        self.write(f, rec, harvested=SUPERSEDED, correction=None)
        f['prev'] = rec
        self.close_pr(f, f'Closed by the factory lane: {why}. The branch is kept as '
                         f'`archive/{b}` ({sha[:7]}).')
        self.delete_branch(f)
        self.out(f'superseded {b}: {why} — archived as archive/{b}')
        self.results[b] = SUPERSEDED
        return rec

    def unclaimed_legacy(self, b, item):
        """The ``branch_retention.legacy_prefixes`` entry ``b`` sits under when no card claims
        it (no card for its item in the record, or a removed one), else None. Such a branch is
        not worth an archive: retention deletes archives after some days anyway."""
        if self.items is None:
            return None
        prefix = next((p for p in self.conv.retention('legacy_prefixes') if b.startswith(p)),
                      None)
        card = self.items.get(item or '') or {}
        return prefix if prefix and (not card or card.get('removed')) else None

    def drop_legacy(self, f, why, prefix):
        """A superseded branch under a legacy prefix no card claims: recorded, its PR closed,
        and deleted outright — no archive."""
        b = f['branch']
        rec = self.record(f, STALE, why)
        self.write(f, rec, harvested=SUPERSEDED, correction=None)
        f['prev'] = rec
        self.close_pr(f, f'Closed by the factory lane: {why}. A legacy branch no card claims — '
                         f'deleted, not archived.')
        self.delete_branch(f)
        self.out(f'superseded {b}: {why} — deleted, not archived (legacy prefix {prefix}, no '
                 f'card claims it)')
        self.results[b] = SUPERSEDED
        return rec

    def repair_naming(self, f):
        """A naming refusal, repaired by the lane with no session: the branch's subjects are
        reworded (:func:`reword_branch`) and pushed from the ref checkout over a lease on the old
        tip, a pending naming correction is cleared, and the branch is PUSHED at its new head —
        the record, or None when it could not be (the caller holds it back to its session, as a
        naming correction that spends no round). A branch under no factory prefix is never
        reworded: it goes back to its session."""
        b, item, old = f['branch'], f.get('item'), f.get('head')
        if (f.get('refusal') or (None,))[0] != lifecycle.NAMING or not item or not old \
                or not self.repo:
            return None
        if not f.get('kind'):
            # a branch under no factory prefix (the registry knows it, B-0067) is someone else's
            # history: the lane never rewrites it — the naming goes back to its session
            self.out(f'reword {b} (naming): under no factory prefix — the lane does not rewrite '
                     f'it, back to its session')
            return None
        if self.dry_run:
            self.out(f'DRY: would reword the subjects on {b} (naming) — no session')
            return None
        kind = githooks.COMMIT_KIND.get(f.get('kind'), 'chore')
        new, n = reword_branch(self.repo, self.trunk, b, item, kind)
        wt = self.ref_checkout() if new else ''
        if not new or not wt:
            self.out(f'reword {b} (naming) failed: '
                     f'{"no commit to reword" if not new else self.ref_wt_error} — back to its session')
            return None
        r = H.sh(['git', 'push', '-q', f'--force-with-lease=refs/heads/{b}:{old}', 'origin',
                  f'{new}:refs/heads/{b}'], cwd=wt)
        if r.returncode != 0:
            self.out(f'reword {b} (naming) push refused: {push_why(r.stderr or r.stdout)} — '
                     f'back to its session')
            return None
        H.sh(['git', 'update-ref', f'refs/remotes/origin/{b}', new], cwd=self.repo)
        self.out(f'reworded {n} subjects on {b} (naming) — no session')
        f['head'] = new
        f['refusal'] = lane_refusal(self.repo, self.trunk, b, item, self.conv)
        if f.get('review'):
            f['review']['current'] = review_mod.is_current(self.repo, self.conv, f'origin/{b}',
                                                           f['review'], new)
        if f.get('correction') and (f['correction'].get('kind') == lifecycle.NAMING):
            H.mark_session(self.state_dir, (f.get('run') or {}).get('job') or b, correction=None)
            f['correction'] = None
        if f.get('run') is None:
            self.write(f, self.record(f, PUSHED, 'adopted'))
        return self.set(f, PUSHED, f'reworded {n} subjects (naming)')

    def enter_back(self, f, reason):
        """BACK: a refusal or a review's changes go back to a session (a hold); a pending
        correction is already that."""
        b = f['branch']
        kind = reason.split('=', 1)[-1]
        if f.get('correction'):
            return self.set(f, BACK, reason)
        if kind == 'review':
            rv = f.get('review') or {}
            text = f"{rv.get('path')} reads {rv.get('text')}: answer its C list on {b}"
        elif f.get('refusal'):
            kind, text = f['refusal']
        else:
            return self.set(f, BACK, reason)
        if self.dry_run:
            self.out(f'DRY: would hold {b}: {text}')
            self.results[b] = 'dry'
            return None
        if f.get('run') is None:
            self.write(f, self.record(f, PUSHED, 'adopted'))
        rec = self.set(f, BACK, reason)
        self.results[b] = hold_with_correction(self.state_dir, b, f['run'], kind, text, self.out)
        f['correction'] = {'kind': kind, 'text': text}
        return rec

    def adopt(self, branch, item, kind, correction=None, job=None):
        """Adopt ``branch`` (no run holds it) as the lane's: a synthetic run, PUSHED — or, with
        ``correction`` (``{kind, text}``), BACK with it, for a session to answer. The run."""
        head = self.remote_heads().get(branch)
        f = {'branch': branch, 'item': item, 'kind': kind, 'head': head, 'run': None,
             'prev': None, 'pr': None}
        if correction:
            rec = self.record(f, BACK, f"kind={correction['kind']}")
            self.write(f, rec, job=job, correction=dict(correction, at=self.now or now_iso()),
                       end_reason=f"held: {correction['kind']}")
        else:
            self.write(f, self.record(f, PUSHED, 'adopted'), job=job)
        return f.get('run')


# ---- the passes -------------------------------------------------------------------------------

def lane_pass(product, state_dir=None, items=None, out=print, dry_run=False, root=None,
              lane=None, defer_pushes=False):
    """The in-process pass (R2): fetch, gather the facts once, and advance every lane branch as
    far as its facts carry it — up to GATE. The gate itself is :func:`gate_pass`'s. Returns
    ``({branch: outcome}, facts)``. ``defer_pushes``: every ref push (a branch delete, an
    archive with the transition it decides) is queued on ``lane`` for :func:`push_deferred`
    instead — the wave's pass, so no launch waits on a product's pre-push hook."""
    lane = lane or Lane(product, state_dir, out, dry_run, items, root)
    if not lane.repo:
        return {}, {}
    lane.defer_pushes = defer_pushes
    H.sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=lane.repo)
    try:
        found = lane.gather(prs=True)
        for b in sorted(found):
            lane.advance(found[b])
    finally:
        lane.finish_ref_pushes()
    from asf import ci_queue  # a queued trunk run a newer one supersedes holds runners for nothing
    ci_queue.cancel_superseded(product, out=lane.out, dry_run=lane.dry_run)
    # the host's queue is FIFO: a trunk run starved behind PR runs gets runs ahead of it cancelled
    ci_queue.relieve_trunk(product, items=lane.items, out=lane.out, dry_run=lane.dry_run)
    return lane.results, found


def push_deferred(lane):
    """The ref pushes a deferring :func:`lane_pass` queued, in its order, each as the pass
    would have done it: an archive (then its record, PR close and delete), a delete after its
    MERGED record (a failure marks it owed, as ever). An archive whose branch a session was
    launched on since is left for the next pass, which sees the run. The ref-push checkout is
    removed and the failures kept for status and doctor (:meth:`Lane.finish_ref_pushes`)."""
    queued, lane.deferred, lane.defer_pushes = lane.deferred, [], False
    if not queued:
        return
    try:
        for kind, f, why in queued:
            b = f['branch']
            if kind == 'archive' and lifecycle.is_live(lifecycle.by_branch(lane.path).get(b)):
                lane.out(f'lane: archive of {b} waits — a session was launched on it this tick')
                continue
            lane.push_or_defer(kind, f, why)
    finally:
        lane.finish_ref_pushes()


def gate_pass(product, state_dir=None, items=None, out=print, dry_run=False, lane=None,
              found=None):
    """The detached harvest's half (R2): every branch in GATE/WAITING/WAITING_CI at the head it
    was recorded at is prechecked (approval, the host's checks, the budget, shared paths) and
    gated together (:func:`gate_set`) — unless this host has no room for a suite at all
    (:func:`held_by_host`). Returns ``{branch: outcome}``."""
    lane = lane or Lane(product, state_dir, out, dry_run, items)
    if found is None:
        H.sh(['git', 'fetch', '-q', '--prune', 'origin'], cwd=lane.repo)
        found = lane.gather(prs=False)
    entries = []
    for b, f in sorted(found.items()):
        rec = f.get('prev') or {}
        if rec.get('state') in GATE_STATES and not f.get('live') and f.get('head') \
                and f['head'] == rec.get('head'):
            entries.append(f)
    entries = H.cap_to_tick(pr_order(entries, items), lane.out, lane.conv)
    ready = held_by_host(lane, precheck(lane, entries))
    if ready:
        try:
            gate_set(lane, ready)
        finally:
            lane.finish_ref_pushes()
    return lane.results


def held_by_host(lane, ready):
    """B-0109: the gate is the product's whole test suite, and it is this host's. Under host
    pressure (:mod:`asf.workers.host`, the same ``config.yaml host_guards`` the wave step reads)
    it is not started at all: every entry that would run it waits (:data:`HOST_PRESSURE`) and is
    re-gated next tick — unknown, never red, nobody blamed. The entries merging on their external
    CI's checks alone (``how == 'ci'``) run no suite here, so they go on. The ones to gate."""
    gating = [f for f in ready if f.get('how') != 'ci']
    if not gating:
        return ready
    try:
        cfg = env.load_config()
    except (env.ConfigError, OSError, ValueError):  # unreadable: the default guards, not none
        cfg = {}
    held, why, _reading = host_mod.pressure(cfg)
    if not held:
        return ready
    lane.out(f'harvest: held: {why} — no gate started this tick, retried next tick')
    for f in gating:
        wait(lane, f, HOST_PRESSURE)
    return [f for f in ready if f.get('how') == 'ci']


def pr_order(entries, items=None):
    """``entries`` hotfix first, then S1, S2, the rest — stable."""
    def rank(f):
        if 'hotfix' in f['branch'].lower():
            return 0
        card = (items or {}).get(f.get('item') or '') or {}
        return {'S1': 1, 'S2': 2}.get(card.get('severity'), 3)
    return sorted(entries, key=rank)


def missing_policy(conv, cls):
    """``conventions.landing_checks_missing`` for landing class ``cls``: one value or a map."""
    value = conv.get('landing_checks_missing')
    if isinstance(value, dict):
        value = value.get(cls, value.get('default'))
    return MISSING_WAIT if str(value or '').strip().lower() == MISSING_WAIT else MISSING_LOCAL_GATE


def external_ci(product):
    """True when the product's code PRs are gated by its external CI, read off the config alone
    (no forge call): it lands through pull requests, and it names its CI gate job
    (``conventions.landing_checks``) or lets CI be the gate for code PRs
    (``landing_checks_missing`` ``wait`` for the ``code`` class)."""
    conv = product.conventions
    if landing(product) != LANDING_PR:
        return False
    return bool(conv.get('landing_checks')) or missing_policy(conv, 'code') == MISSING_WAIT


def landing_wait_s(conv):
    try:
        return max(0.0, float(conv.get('landing_checks_wait_min', DEFAULT_LANDING_WAIT_MIN))) * 60
    except (TypeError, ValueError):
        return DEFAULT_LANDING_WAIT_MIN * 60.0


def trunk_moved_files(lane, since):
    """The files the trunk changed since ``since`` (a sha), or None when unknown."""
    if not since:
        return None
    r = H.sh(['git', 'diff', '--name-only', since, f'origin/{lane.trunk}'], cwd=lane.repo)
    return [l for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else None


def precheck(lane, entries):
    """Each entry's pre-gate answer: an approval hold, the host's checks, a shared path taken —
    WAITING/WAITING_CI/BACK now — else it goes to the gate (``f['how'] = 'gate'``) or merges on
    its checks alone (``'ci'``; R25's docs-only trunk move counts as that). The ready ones."""
    product, conv, out = lane.product, lane.conv, lane.out
    ready, shared_taken = [], None
    for f in entries:
        b, rec = f['branch'], f.get('prev') or {}
        if has_adjudicate_commit(lane.repo, lane.trunk, b):
            out(f'held {b}: ruling belongs in the record')
            wait(lane, f, 'ruling belongs in the record', 'held')
            continue
        marked = customer_content.refusal(lane.repo, lane.trunk, b, conv)
        if marked:  # the marker gate again at the gate: a branch past PUSHED before it existed
            f['refusal'] = marked
            lane.enter_back(f, f'kind={marked[0]}')
            continue
        files = f.get('files') or touched_files(lane.repo, lane.trunk, b)
        cls, matched = approvals.merge_class(product, files)
        level = approvals.merge_level(product, cls)
        hold = f"{f.get('item')}/{cls}"
        if getattr(lane, 'auto', False) and not lane.dry_run and any(
                h.get('hold') == hold for h in approvals.open_holds(product)):
            # merge: auto — a hold a manual pass left open is the lane's to close, not a
            # NEEDS OPERATOR line that outlives the switch
            approvals.resolve(product, hold, 'granted')
            out(f'merge: auto — {hold} released')
        if level != 'auto' and not approvals.is_granted(product, f"{f.get('item')}/{cls}"):
            detail = matched or 'routine'
            if approvals.dropped(product, f"{f.get('item')}/{cls}", detail):
                # a person dropped this merge: it is not asked again, and never merges
                out(f'held {b}: {cls} dropped — {detail} — not asked again')
                wait(lane, f, f'approval {cls} dropped', 'held')
                continue
            if not lane.dry_run:
                approvals.ask(product, f.get('item'), cls, level,
                              (f.get('run') or {}).get('job') or b, 'harvest', detail)
            out(f'held {b}: {cls} ({level}) — {detail}')
            wait(lane, f, f'approval {cls} ({level})', 'held')
            continue
        f['how'] = 'gate'
        number = rec.get('pr')
        if isinstance(lane.host, GitHubHost) and number:
            how = lane.host.check_gate(f, number, files)
            if how is None:
                continue
            f['how'] = how
        if f['how'] == 'gate' and skip_regate(rec, f['head'],
                                              trunk_moved_files(lane, (rec.get('green') or {})
                                                                .get('trunk')), product):
            out(f'harvest: {b} was green at its head, {lane.trunk} moved on docs only — '
                f'not gated again')
            f['how'] = 'ci'
        hits = shared_hits(conv, files)
        if hits:
            if shared_taken:
                out(f'waiting {b}: touches {hits[0]}, a shared path {shared_taken} already '
                    f'takes this tick')
                wait(lane, f, 'shared-path')
                continue
            shared_taken = b
        ready.append(f)
    return ready


def wait(lane, f, reason, result='waiting', state=WAITING, **extra):
    """Record a gate outcome that is no one's fault: WAITING (or WAITING_CI) with ``reason``."""
    rec = f.get('prev') or {}
    if (rec.get('state') == state and rec.get('reason') == reason
            and all(rec.get(k) == v for k, v in extra.items())):
        lane.results[f['branch']] = result
        return rec
    keep = {k: rec.get(k) for k in ('green', 'since', 'timeouts') if rec.get(k) is not None}
    keep.update(extra)
    return lane.set(f, state, reason, result=result, **keep)


# ---- the one gate -----------------------------------------------------------------------------

class TrunkRed(Exception):
    """The modules a combined head is red on are red on the trunk alone too."""

    def __init__(self, modules):
        super().__init__(' '.join(modules))
        self.modules = tuple(modules)


class GateTimeout(Exception):
    """The gate ran out of time: unknown, never red (§12) — nothing is bisected or blamed."""

    def __init__(self, line):
        super().__init__(line)
        self.line = line


def combined_head(tmp, trunk, entries, hold):
    """Reset the throwaway worktree to ``origin/<trunk>`` and replay each entry's branch on top;
    the entries that applied (one that conflicts goes to ``hold``)."""
    H.sh(['git', 'checkout', '-q', '--detach', f'origin/{trunk}'], cwd=tmp)
    stacked = []
    for f in entries:
        head = H.sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
        ok, reason = H.rebase_and_resolve(tmp, trunk, onto=head, tip=f"origin/{f['branch']}")
        if ok:
            stacked.append(f)
        else:
            hold(f, 'conflict', reason)
    return stacked


def red_on_trunk(tmp, trunk, conv, asf_repo, out, modules=None):
    """True when the trunk alone is red (on ``modules`` only, when given)."""
    H.sh(['git', 'checkout', '-q', '--detach', f'origin/{trunk}'], cwd=tmp)
    ok, line, _files, _red = H.product_gate(tmp, conv, asf_repo, out, only=modules)
    if not ok and line.startswith(H.TIMED_OUT):
        raise GateTimeout(line)
    return not ok


def gate_groups(tmp, trunk, entries, conv, asf_repo, hold, out, announce=False, only=None,
                trunk_green=False, ledger=None):
    """Gate ``entries`` as one combined head; on red, bisect (B-0040). The green groups as
    ``[(entries, sha, full)]``. A timeout raises :class:`GateTimeout` — never bisected (§12).
    ``ledger`` (§2.2), when given, is called after every gate here with the stacked branches,
    the head, ``ok``, the seconds spent and the first failing line — a bisect's targeted re-runs
    each write their own line; ``None`` writes nothing."""
    stacked = combined_head(tmp, trunk, entries, hold)
    if not stacked:
        return []
    if announce:
        out(f'harvest: {len(stacked)} branch(es), one gate')
    started = time.monotonic()
    ok, line, files, red = H.product_gate(tmp, conv, asf_repo, out, only)
    head = H.sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
    if ledger:
        ledger([f['branch'] for f in stacked], head, ok, time.monotonic() - started, line)
    if ok:
        return [(stacked, head, not only)]
    if line.startswith(H.TIMED_OUT):
        raise GateTimeout(line)
    if not only and red:
        if red_on_trunk(tmp, trunk, conv, asf_repo, out, red):
            raise TrunkRed(red)
        trunk_green = True
    if len(stacked) == 1:
        hold(stacked[0], 'gate', line, files, own=trunk_green)
        return []
    out(f'harvest: bisecting {len(stacked)} branches')
    mid = len(stacked) // 2
    narrowed = red or only
    return (gate_groups(tmp, trunk, stacked[:mid], conv, asf_repo, hold, out, only=narrowed,
                        trunk_green=trunk_green, ledger=ledger)
            + gate_groups(tmp, trunk, stacked[mid:], conv, asf_repo, hold, out, only=narrowed,
                          trunk_green=trunk_green, ledger=ledger))


def confirmed_group(tmp, trunk, entries, conv, asf_repo, hold, out, announce=True, ledger=None):
    """The one set of ``entries`` green under the *full* gate, as ``(entries, sha, deferred,
    unconfirmed)``; green apart but red together lands the first alone and defers the rest.
    ``ledger`` (§2.2) is passed to every :func:`gate_groups` call."""
    candidates, deferred = list(entries), []
    for _round in range(CONFIRM_ROUNDS):
        groups = gate_groups(tmp, trunk, candidates, conv, asf_repo, hold, out,
                             announce=announce, ledger=ledger)
        if not groups:
            return None, None, deferred, []
        if len(groups) == 1 and groups[0][2]:
            return groups[0][0], groups[0][1], deferred, []
        union = [e for group, _sha, _full in groups for e in group]
        if len(union) == len(candidates) and len(union) > 1:
            out(f"harvest: {len(union)} branches green alone, red together — landing "
                f"{union[0]['branch']} first, the rest re-gate on top of it")
            deferred = union[1:] + deferred
            union = union[:1]
        candidates = union
    return None, None, deferred, candidates


def send_back(lane, f, kind, text, files):
    """Hand a branch the gate refused (the trunk alone green) back to a session. Code: its
    session, a round (:func:`hold_with_correction`). Docs: a :data:`LANDING_GATE` correction,
    which the feeder turns into a STARVED → SPEC/PLAN session on that branch (R7)."""
    b, run = f['branch'], f.get('run') or {}
    if f.get('class') == DOCS and kind in ('gate', 'conflict'):
        doc = lane.conv.branch_kind(b) or 'document'
        what = 'does not rebase cleanly onto' if kind == 'conflict' else 'turns the gate red on'
        note = (f'the {doc} {what} {lane.trunk} — {text}. Change the {doc} so the product gate '
                f'passes on {lane.trunk}; nothing is merged until it does')
        lane.set(f, BACK, f'kind={LANDING_GATE}')
        job = run.get('job') or b
        fields, line = lifecycle.hold(lane.path, dict(run, branch=b, job=job), LANDING_GATE, note,
                                      now_iso())
        H.mark_session(lane.state_dir, job, **fields)
        lane.out(line)
        lane.results[b] = 'held'
        return 'held'
    card = (lane.items or {}).get(f.get('item')) or {}
    lane.set(f, BACK, f'kind={kind}')
    res = hold_with_correction(lane.state_dir, b, run, kind, text, lane.out, files,
                               card.get('writes') or (), f.get('files') or (), lane.conv,
                               own=True, read=gate_reader(lane.repo, b))
    if res != 'held':  # foreign / timed-out: no one's fault after all
        wait(lane, f, res)
    lane.results[b] = res
    return res


def note_timeout(lane, entries, line):
    """§12: a gate timeout is unknown, never red. The set waits (``gate-timeout``), is retried
    next tick, and from the second timeout in a row one ``gate too slow`` line is kept for the
    status and doctor rows. Never a correction, never a bisection."""
    n = 1 + max([int((f.get('prev') or {}).get('timeouts') or 0) for f in entries
                 if (f.get('prev') or {}).get('reason') == 'gate-timeout'] + [0])
    lane.out(f'harvest: {line} — the set waits, retried next tick (timeout {n} in a row)')
    for f in entries:
        wait(lane, f, 'gate-timeout', timeouts=n)
    if not lane.dry_run:
        path = os.path.join(lane.state_dir, GATE_SLOW)
        try:
            if n >= GATE_SLOW_AFTER:
                with open(path, 'w', encoding='utf-8') as fh:
                    json.dump({'timeouts': n, 'gate_timeout_s': H.gate_timeout(lane.conv),
                               'at': now_iso()}, fh)
        except OSError:
            pass


def gate_slow_line(product):
    """``gate too slow: <n>s vs gate_timeout_s`` when the gate timed out twice in a row and has
    not been green since, else None — for the status and doctor rows."""
    try:
        with open(os.path.join(env.state_dir(product), GATE_SLOW), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if int(data.get('timeouts') or 0) < GATE_SLOW_AFTER:
        return None
    return (f"gate too slow: {data.get('gate_timeout_s')}s vs gate_timeout_s — timed out "
            f"{data.get('timeouts')} times in a row; raise harvest.gate_timeout_s or trim the gate")


def push_why(text):
    """What a refused push said, without git's framing lines (``To <url>``, ``error: failed
    to push some refs``, hints): the hook's or the remote's own last words."""
    keep = [ln.strip() for ln in (text or '').splitlines() if ln.strip() and not (
        ln.startswith('To ') or ln.startswith('hint:')
        or ln.startswith('error: failed to push some refs'))]
    return ' / '.join(keep[-2:]) if keep else (H.tail(text) or 'refused')


def ref_push_line(product):
    """``ref pushes failing: …`` when the last lane pass could not push an archive or a
    branch delete (:meth:`Lane.finish_ref_pushes`), else None — for the status and doctor
    rows."""
    try:
        with open(os.path.join(env.state_dir(product), REF_PUSH_FAILED), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    last = (data.get('failures') or [{}])[-1]
    return (f"ref pushes failing: {data.get('count')} at {data.get('at')} — "
            f"{last.get('what')}: {last.get('why')}; the branches stay on origin until one goes")


def gate_ok(lane):
    try:
        os.remove(os.path.join(lane.state_dir, GATE_SLOW))
    except OSError:
        pass


def gate_set(product, entries):
    """The one gate, both landing modes. ``entries`` are the branches in GATE this pass (their
    facts; ``f['how'] == 'ci'`` merges on its checks alone). Docs and code are gated as two
    sets; each builds a combined head, bisects on the red modules, confirms in full, checks the
    trunk alone once when something is red (red there: WAITING ``trunk-red``, no one blamed),
    and merges the green through the product's :class:`Host`, writing MERGING first.
    ``product`` is a :class:`Lane` (or a Product: a default one is made). Returns
    ``[(branch, state, reason)]``."""
    lane = product if isinstance(product, Lane) else Lane(product)
    to_merge = [f for f in entries if f.get('how') == 'ci']
    gated = [f for f in entries if f.get('how') != 'ci']
    for cls in (DOCS, CODE):
        group = [f for f in gated if (f.get('class') or landing_class(lane.product, f.get('files')
                                                                       or ())) == cls]
        if not group:
            continue
        if str(lane.conv.harvest_gate).strip().lower() == H.GATE_PER_BRANCH:
            for f in group:
                gate_one_set(lane, [f], to_merge)
        else:
            gate_one_set(lane, group, to_merge)
    if to_merge and not isinstance(lane.host, FastForwardHost):
        merge_prs(lane, to_merge)
    return [(f['branch'], (f.get('prev') or {}).get('state'), (f.get('prev') or {}).get('reason'))
            for f in entries]


def gate_one_set(lane, group, to_merge):
    """Gate one set (FF: and push it; PR: queue its green for :func:`merge_prs`). A conflict is
    sent back at once; a branch red alone is sent back only once the trunk alone is seen green
    (checked once, and only when the bisection did not already show it)."""
    conv, trunk, out = lane.conv, lane.trunk, lane.out
    asf_repo = H.is_asf_repo(lane.repo)
    announce = str(conv.harvest_gate).strip().lower() != H.GATE_PER_BRANCH
    started = time.monotonic()
    pending, restacked = list(group), False

    def ledger(branches, sha, ok, seconds, line):  # §2.2: every landing gate, one gates.jsonl line
        H.record_gate(lane.state_dir, branches, sha, ok, seconds, line)

    for _attempt in range(len(group) + 2):
        if not pending:
            return
        held = []

        def hold(f, kind, text, files=(), own=False):
            if kind == 'conflict':
                send_back(lane, f, kind, text, files)
            else:
                held.append((f, kind, text, files, own))

        holder = tempfile.mkdtemp(prefix='harvest-')
        tmp = os.path.join(holder, 'wt')
        try:
            add = H.sh(['git', 'worktree', 'add', '--detach', tmp, f'origin/{trunk}'],
                       cwd=lane.repo)
            if add.returncode != 0:
                for f in pending:
                    out(f"held {f['branch']}: worktree add failed: {H.tail(add.stderr)}")
                    lane.results[f['branch']] = 'held'
                return
            try:
                landing_set, sha, deferred, unconfirmed = confirmed_group(
                    tmp, trunk, pending, conv, asf_repo, hold, out, announce, ledger)
                trunk_red = None
                for f, kind, text, files, own in held:
                    if not own:
                        if trunk_red is None:
                            trunk_red = red_on_trunk(tmp, trunk, conv, asf_repo, out)
                        if trunk_red:
                            out(f"waiting {f['branch']}: gate red, and {trunk} is red alone too"
                                f" — gated again next tick")
                            wait(lane, f, 'trunk-red')
                            continue
                    send_back(lane, f, kind, text, files)
            except TrunkRed as red:
                out(f"harvest: red on trunk too — {' '.join(red.modules)}")
                for f in pending:
                    wait(lane, f, 'trunk-red')
                return
            except GateTimeout as t:
                note_timeout(lane, pending, t.line)
                return
            gate_ok(lane)
            for f in unconfirmed:
                out(f"held {f['branch']}: green on the red modules, not confirmed in full — "
                    f"next tick")
                wait(lane, f, 'unconfirmed', 'held')
            if landing_set is not None:
                for f in landing_set:
                    f['green'] = {'head': f['head'], 'trunk': lane.trunk_sha}
                if not isinstance(lane.host, FastForwardHost):
                    to_merge.extend(landing_set)
                    for f in deferred:
                        defer(lane, f)
                    return
                pushed = push_set(lane, landing_set, sha, final=restacked)
                if pushed == 'moved':  # the trunk moved under the push: stack and gate again, once
                    restacked = True
                    pending = landing_set + deferred
                    continue
                if pushed != 'ok':
                    for f in deferred:
                        defer(lane, f)
                    return
            if deferred and regate(lane, started):
                out(f'harvest: re-gating {len(deferred)} branch(es) on the new {trunk}')
                pending = deferred
                continue
            for f in deferred:
                defer(lane, f)
            return
        finally:
            H.sh(['git', 'worktree', 'remove', '--force', tmp], cwd=lane.repo)


def regate(lane, started):
    return not lane.dry_run and time.monotonic() - started < H.gate_timeout(lane.conv)


def defer(lane, f):
    if isinstance(lane.host, FastForwardHost):
        lane.out(f"held {f['branch']}: green alone, one landed ahead of it — re-gated on the new "
                 f"{lane.trunk} next tick")
        wait(lane, f, 'deferred', 'held')
        return
    lane.out(f"waiting {f['branch']}: green alone, one merges ahead of it — gated on the new "
             f"{lane.trunk} next tick")
    wait(lane, f, 'deferred')


def push_set(lane, landing_set, sha, final=False):
    """FF: MERGING (the sha) on every branch of the set, one push of the combined head, then
    MERGED at that sha. ``'ok'``; ``'moved'`` when the trunk moved under the push (the caller
    restacks once; ``final``: it already did); ``'refused'``."""
    if lane.dry_run:
        for f in landing_set:
            lane.out(f"DRY: would land {f['branch']} → {sha}")
            lane.results[f['branch']] = 'dry'
        return 'ok'
    if not lane.ci_admits(landing_set[0], 'trunk'):
        for f in landing_set:
            wait(lane, f, CI_QUEUE, 'held')
        return 'held'
    for f in landing_set:
        lane.set(f, MERGING, f'push {sha[:12]}', sha=sha, method='ff')
    pushed, not_ff = lane.host.merge(landing_set[0]['branch'], None, sha)
    if not pushed:
        if not_ff and not final:
            return 'moved'
        why = f'{lane.trunk} moved again on retry' if not_ff else f'push to {lane.trunk} refused'
        for f in landing_set:
            lane.out(f"held {f['branch']}: {why}")
            wait(lane, f, why, 'held')
        return 'refused'
    for f in landing_set:
        rec = lane.record(f, MERGED, 'method=ff', sha=sha, method='ff')
        lane.write(f, rec, harvested=sha, correction=None)
        f['prev'] = rec
        lane.delete_branch(f)
        lane.out(f"landed {f['branch']} → {sha}")
        lane.results[f['branch']] = 'landed'
    return 'ok'


#: What a ``gh`` with no ``--subject`` says when it is handed one. Degrading to a merge without
#: the subject beats stalling the lane: the evidence still reads a document lane by its paths.
SUBJECT_UNKNOWN_RE = re.compile(r'unknown flag|unknown shorthand|flag provided but not defined|'
                                r'unrecognized (?:flag|argument)', re.I)


def _rejects_subject(err):
    """True when ``gh`` failed because it does not know ``--subject``, not because the merge
    itself was refused."""
    return bool(SUBJECT_UNKNOWN_RE.search(err or '')) and '--subject' in (err or '')


def squash_subject(lane, f, number):
    """The subject a document lane's squash merge writes on the trunk, or None for a code lane —
    whose subject is the host's to compose (B-0114).

    Left to the host, a spec/plan PR lands under its PR title — ``F-0047 — the Stripe mirror
    (#743)`` — and the evidence reads that commit as the Feature's code landing. A spec/plan lane
    names its kind instead, the shape its own commits carry (``plan(F-0047): … (#743)``): its
    branch's newest subject when that is already a document lane's, else prefixed with it. The
    newest, not the newest *conforming* one — a lane whose last commit is ``docs: address review``
    lands as ``plan(F-0047): docs: address review (#743)``, which the evidence reads correctly
    even though the lane's own better subject is further down.

    Not every merge takes this: a merge queue composes its own subject
    (:meth:`GitHubHost.merge` ignores it on ``--auto``, so it is computed here and dropped there),
    and ``--rebase`` writes no commit of its own (:data:`SUBJECT_METHODS`). On a queue repo a document lane is recognised by its
    paths instead (``asf.evidence.evidence.docs_only``), which holds as long as it touches only
    documents."""
    from asf.evidence.evidence import DOC_LANE_KINDS, DOC_LANE_SUBJECT
    kind = f.get('kind')
    if kind not in DOC_LANE_KINDS:
        return None
    branch = f['branch']
    head = next((s.strip() for s in _subjects(lane.repo, lane.trunk, branch) if s.strip()), '')
    if not DOC_LANE_SUBJECT.match(head):
        item = f.get('item') or lane.conv.strip_prefix(branch)
        head = f'{kind}({item}): {head or branch}'
    return f'{head} (#{number})'


#: how many touched files the auto-merge line names before it counts the rest
AUTO_MERGE_FILES = 8


def auto_merge_line(f, number, sha):
    """The one line ``merge: auto`` writes per merge, naming what the merge touched."""
    files = list(f.get('files') or [])
    shown = ', '.join(files[:AUTO_MERGE_FILES]) or 'no files'
    more = f' (+{len(files) - AUTO_MERGE_FILES} more)' if len(files) > AUTO_MERGE_FILES else ''
    return (f"merge: auto — merged PR #{number} ({f['branch']}, {f.get('item') or '—'}) at "
            f"{str(sha)[:12]}; touched {len(files)} file(s): {shown}{more}")


def merge_prs(lane, ready):
    """PR mode: merge every green PR the budget has room for — MERGING first (R3), then ``gh pr
    merge`` (or ``--auto`` into a merge queue: QUEUED)."""
    host = lane.host
    room, why = host.slots()
    for i, f in enumerate(ready):
        b, number = f['branch'], (f.get('prev') or {}).get('pr')
        if room is not None and i >= room:
            lane.out(f'waiting {b}: PR #{number} green — no merge room this tick ({why})')
            wait(lane, f, 'budget', green=f.get('green'))
            continue
        if lane.dry_run:
            lane.out(f'DRY: would merge {b} (PR #{number})')
            lane.results[b] = 'dry'
            continue
        if not lane.ci_admits(f, 'trunk'):
            wait(lane, f, CI_QUEUE, green=f.get('green'))
            continue
        lane.set(f, MERGING, f'PR #{number}', method='squash')
        sha, how = host.merge(b, number, subject=squash_subject(lane, f, number))
        if how == 'queue':
            host.in_queue += 1
            lane.out(f'queued {b}: PR #{number} added to the merge queue')
            lane.set(f, QUEUED, f'PR #{number} in the merge queue', result='queued')
            continue
        if not sha:
            lane.out(f'held {b}: PR #{number} merge refused — {how}')
            wait(lane, f, f'merge refused: {how}', 'held', green=f.get('green'))
            continue
        host.merged += 1
        rec = lane.record(f, MERGED, f'method={how}', sha=sha, method=how)
        lane.write(f, rec, harvested=sha, correction=None)
        f['prev'] = rec
        lane.out(f'landed {b} → PR #{number} {sha}')
        if getattr(lane, 'auto', False):
            lane.out(auto_merge_line(f, number, sha))
        lane.results[b] = 'landed'
        cancelled = host.cancel_ci(b)
        if cancelled:
            lane.out(f'harvest: {b}: cancelled {cancelled} CI run(s) of the merged PR #{number} '
                     f'— the trunk run judges it now')


# ---- the hosts --------------------------------------------------------------------------------

class Host:
    """Where a lane branch lands. :func:`host` picks the adapter from the product's ``landing``;
    the state machine is the same for both."""

    def __init__(self, product, lane=None):
        self.product = product
        self.lane = lane

    def prs(self):
        """``{branch: pr}`` — the host's PRs (none without a PR host)."""
        return {}

    def open(self, branch):
        """Open the branch's PR, or adopt the open one already naming it. Returns the PR number,
        or None (fast-forward: the branch is its own PR, T2 passes at once)."""
        raise NotImplementedError

    def status(self, branch):
        """The host's view of the branch: ``{pr, state, head, checks, merged, merge_sha,
        queued}`` — ``checks`` one of ``none|pending|passed|failed``."""
        raise NotImplementedError

    def close(self, number, comment):
        """Close PR ``number`` with ``comment``; True when it closed (no PR host: nothing)."""
        return False

    def merge(self, branch, pr, subject=None):
        """Land it: ``(sha, method)`` with ``method`` one of ``ff|squash|merge|rebase|queue``, or
        a refusal ``(None, reason)``. ``subject``: the squash subject to write, when the lane
        names one (:func:`squash_subject`). The caller writes MERGING before calling."""
        raise NotImplementedError

    def cancel_ci(self, branch):
        """Cancel the CI runs a merged PR still has going; how many were cancelled. No PR host,
        no runs: 0."""
        return 0


class FastForwardHost(Host):
    """``landing: fast-forward``: no PR host. ``open`` → None; ``status`` → merged when the head
    is on ``origin/<trunk>``; ``merge`` → ``push_ff`` of the gated head, method ``ff``."""

    def open(self, branch, item=None):
        return None, 'fast-forward: no PR'

    def status(self, branch):
        repo, trunk = self.product.repo_dir, self.product.conventions.main
        head = H.sh(['git', 'rev-parse', f'origin/{branch}'], cwd=repo).stdout.strip()
        merged = bool(head) and H.sh(['git', 'merge-base', '--is-ancestor', head,
                                      f'origin/{trunk}'], cwd=repo).returncode == 0
        return {'pr': None, 'state': 'MERGED' if merged else 'OPEN', 'head': head,
                'checks': 'none', 'merged': merged, 'merge_sha': None, 'queued': False}

    def merge(self, branch, pr, sha=None, subject=None):
        """``(pushed, not_ff)`` for the combined head ``sha`` (the lane's MERGING names it); a
        fast-forward writes no commit, so it has no subject to name."""
        repo, trunk = self.product.repo_dir, self.product.conventions.main
        pushed, not_ff = H.push_ff(repo, sha, trunk)
        if not pushed:
            return False, not_ff
        H.sh(['git', 'fetch', '-q', 'origin', trunk], cwd=repo)
        on = H.sh(['git', 'merge-base', '--is-ancestor', sha, f'origin/{trunk}'], cwd=repo)
        return on.returncode == 0, False


class GitHubHost(Host):
    """``landing: pull-request`` on GitHub: ``gh pr create`` / adopt; ``gh pr checks``; ``gh pr
    merge`` (squash first) or ``--auto`` into a merge queue (QUEUED)."""

    def __init__(self, product, lane=None):
        super().__init__(product, lane)
        from asf import capacity
        self.slug = lane.slug if lane else repo_slug(product)
        self.trunk = product.conventions.main
        shape = capacity.batch_shape(product, None)
        self.per_run, self.parallel = shape.get('per_run'), shape.get('parallel')
        self.merged = 0
        self.in_queue = 0
        self._queue = None
        self._required = None
        self._trunk_runs = {}  # sha -> that commit's check runs (None: unreadable), one pass

    def prs(self):
        """One ``gh pr list --state all``: ``{branch: pr}``, an open PR first, else the newest."""
        data = H.gh_json(['pr', 'list', '-R', self.slug, '--state', 'all', '--limit', '500',
                          '--json', 'number,headRefName,headRefOid,state,mergeCommit,'
                                    'autoMergeRequest,title,baseRefName'], [])
        out = {}
        for p in sorted((p for p in data if isinstance(p, dict)),
                        key=lambda p: (p.get('state') == 'OPEN', int(p.get('number') or 0))):
            out[p.get('headRefName')] = {
                'number': p.get('number'), 'state': str(p.get('state') or 'OPEN').upper(),
                'head': p.get('headRefOid') or None,
                'merge_sha': (p.get('mergeCommit') or {}).get('oid') or None,
                'queued': bool(p.get('autoMergeRequest')), 'title': p.get('title') or '',
                'base': p.get('baseRefName') or None}
        if self.lane is not None:
            self.in_queue = sum(1 for r in lifecycle.by_branch(self.lane.path).values()
                                if (r.get('lane') or {}).get('state') == QUEUED)
        return out

    def open(self, branch, item=None):
        from asf.tick import step_prs
        items = (self.lane.items if self.lane else None) or {}
        root = self.lane.root if self.lane else None
        title, body = step_prs.title_and_body(item or '', items.get(item or '') or {}, root, branch)
        if self.product.conventions.branch_kind(branch) == DIRECT:
            note = first_commit_body(self.product.repo_dir, self.trunk, branch)
            if note:  # the direct session's spec+plan note: the PR description carries it
                body = f'{body}\n## What and how\n\n{note}\n'
        rc, stdout, err = H._gh(['pr', 'create', '-R', self.slug, '--base', self.trunk,
                                 '--head', branch, '--title', title, '--body', body])
        m = re.search(r'/pull/(\d+)', f'{stdout}\n{err}')
        if m and (rc == 0 or 'already exists' in err):
            return int(m.group(1)), ''
        return None, ((err or stdout).strip().splitlines() or [f'gh exited {rc}'])[0]

    def close(self, number, comment):
        rc, _stdout, _err = H._gh(['pr', 'close', str(number), '-R', self.slug,
                                   '--comment', comment])
        return rc == 0

    def status(self, branch):
        p = H.gh_json(['pr', 'view', branch, '-R', self.slug, '--json',
                       'number,state,headRefOid,mergeCommit,autoMergeRequest'], {})
        required = self.required_checks(self.lane.state_dir) if p and self.lane else ()
        state, _detail, _checks = (pr_checks(self.slug, p.get('number'), required) if p
                                   else ('none', '', []))
        return {'pr': p.get('number'), 'state': p.get('state'), 'head': p.get('headRefOid'),
                'checks': {'green': 'passed', 'red': 'failed'}.get(state, state),
                'merged': p.get('state') == 'MERGED',
                'merge_sha': (p.get('mergeCommit') or {}).get('oid'),
                'queued': bool(p.get('autoMergeRequest'))}

    def has_queue(self):
        if self._queue is None:
            owner, _, name = self.slug.partition('/')
            q = ('query($o:String!,$n:String!,$b:String!){repository(owner:$o,name:$n)'
                 '{mergeQueue(branch:$b){id}}}')
            data = H.gh_json(['api', 'graphql', '-f', f'query={q}', '-F', f'o={owner}',
                              '-F', f'n={name}', '-F', f'b={self.trunk}'], {})
            self._queue = bool((((data or {}).get('data') or {}).get('repository') or {})
                               .get('mergeQueue'))
        return self._queue

    def required_checks(self, state_dir):
        named = self.product.conventions.get('landing_checks')
        if named:
            return (str(named),) if isinstance(named, str) else tuple(str(n) for n in named)
        if self._required is None:
            self._required = protected_checks(self.slug, self.trunk, state_dir)
        return self._required

    def slots(self):
        """``(n, why)``: how many more PRs may be merged (or queued) this pass; QUEUED counts
        toward the in-queue budget (R5)."""
        room, why = None, None
        if self.per_run is not None:
            room, why = max(0, self.per_run - self.merged), 'capacity.batch.per_run'
        if self.parallel is not None and self.has_queue():
            left = max(0, self.parallel - self.in_queue)
            if room is None or left < room:
                room, why = left, 'capacity.batch.parallel'
        return room, why

    def merge(self, branch, pr, subject=None):
        if self.has_queue():
            rc, _o, err = H._gh(['pr', 'merge', str(pr), '-R', self.slug, '--auto'])
            return (None, 'queue') if rc == 0 else (None, H.tail(err) or f'gh exited {rc}')
        err = ''
        for method in MERGE_METHODS:
            args = ['pr', 'merge', str(pr), '-R', self.slug, method, '--delete-branch']
            if subject and method in SUBJECT_METHODS:
                args += ['--subject', subject]
            rc, _out, err = H._gh(args)
            if rc == 0:
                return H.merged_sha(self.slug, pr) or f'PR #{pr}', method[2:]
            if subject and method in SUBJECT_METHODS and _rejects_subject(err):
                # a gh too old for --subject: merge without it, and say so. The trunk then
                # carries whatever subject the host composes, and only the paths still read as a
                # document lane (`docs_only`) — a degrade worth seeing in the log, not inferring.
                # The flag and its value are the last two args, so they come off by position: a
                # subject that happened to equal another arg would take that arg with it.
                if self.lane is not None:
                    self.lane.out(f'{branch}: merged without --subject — this gh does not know '
                                  'the flag')
                rc, _out, err = H._gh(args[:-2])
                if rc == 0:
                    return H.merged_sha(self.slug, pr) or f'PR #{pr}', method[2:]
            if 'not allowed' not in (err or '').lower():
                break
        return None, H.tail(err) or 'gh pr merge failed'

    def cancel_ci(self, branch):
        """Cancel ``branch``'s ``pull_request`` CI runs that have not finished. Called once its PR
        merged: the lane lands on the required checks alone, so the rest of the PR's matrix is
        still queued or running on the runner pool, judging a head the trunk's own run now
        judges again — moot work that holds runners the open PRs and the trunk are queued for.
        Never raises: an unreadable list or a refused cancel counts as nothing cancelled."""
        try:
            runs = H.gh_json(['run', 'list', '-R', self.slug, '--branch', branch,
                              '--event', 'pull_request', '--limit', '20',
                              '--json', 'databaseId,status'], [])
            n = 0
            for r in runs if isinstance(runs, list) else ():
                if not isinstance(r, dict) or r.get('status') == 'completed':
                    continue
                if r.get('databaseId') and H._gh(['run', 'cancel', str(r['databaseId']),
                                                  '-R', self.slug])[0] == 0:
                    n += 1
            return n
        except OSError:
            return 0

    def check_gate(self, f, number, files):
        """The PR's checks before the gate: ``'gate'`` (gate it locally), ``'ci'`` (its required
        checks passed under ``wait``), or None when it waits or went back."""
        lane, b, rec = self.lane, f['branch'], f.get('prev') or {}
        required = self.required_checks(lane.state_dir)
        state, detail, checks = pr_checks(self.slug, number, required)
        ignored = not_required_red(checks, required)
        if ignored:
            lane.out(f'harvest: {b}: PR #{number} check(s) red but not required — '
                     f'{", ".join(ignored)} (informational)')
        if state == 'pending':
            # how long the PR has waited on a remote run, kept across ticks (``ci_since``, apart
            # from the missing-check clock ``since``): a run queued behind a saturated runner pool
            # shows its age in every tick's line instead of reading as a fresh wait each time
            now = lane.now or time.time()
            since = (rec.get('ci_since') if rec.get('state') == WAITING_CI and rec.get('ci_since')
                     else now)
            lane.out(f'waiting {b}: PR #{number} checks pending — {detail} '
                     f'({int((now - float(since)) // 60)} min)')
            wait(lane, f, f'checks pending: {detail}', state=WAITING_CI, ci_since=since)
            return None
        if state == 'unknown':
            lane.out(f'waiting {b}: PR #{number} checks unreadable — {detail}')
            wait(lane, f, 'checks unreadable')
            return None
        if state == 'red':
            red = [c.get('name') or '?' for c in checks if c.get('bucket') in RED_BUCKETS
                   and (not required or c.get('name') in required)]
            on_trunk = self.trunk_red(red)
            if red and all(n in on_trunk for n in red):
                lane.out(f'waiting {b}: PR #{number} checks red: {detail} — red on {self.trunk} '
                         f'too, not its fault; it lands once {self.trunk} is green')
                wait(lane, f, f'trunk-red: {", ".join(red)}')
                return None
            if lane.dry_run:
                lane.out(f'DRY: would hold {b}: PR #{number} checks red: {detail}')
                lane.results[b] = 'dry'
                return None
            send_back(lane, f, 'gate', f'PR #{number} checks red: {detail}', ())
            return None
        passed = {c.get('name') for c in checks if c.get('bucket') in PASS_BUCKETS}
        on_trunk = self.trunk_red(required or [c.get('name') for c in checks if c.get('name')])
        if on_trunk:
            tip = f.get('head') or f'origin/{b}'
            unfixed = [n for n, sha in on_trunk.items()
                       if n not in passed or not lane.is_ancestor(sha, tip)]
            if unfixed:
                lane.out(f'waiting {b}: {self.trunk} is red on {", ".join(unfixed)} and PR '
                         f'#{number} does not turn it green on top of that red — it lands once '
                         f'{self.trunk} is green')
                wait(lane, f, f'trunk-red: {", ".join(unfixed)}')
                return None
            lane.out(f'harvest: {b}: PR #{number} turns {", ".join(on_trunk)} green on top of '
                     f'the red {self.trunk} — it may land')
        cls = f.get('class') or landing_class(self.product, files)
        if missing_policy(self.product.conventions, cls) == MISSING_LOCAL_GATE:
            return 'gate'
        if not required:
            return 'gate'
        missing = [name for name in required if name not in passed]
        if not missing:
            return 'ci'
        now = lane.now or time.time()
        since = rec.get('since') if rec.get('state') == WAITING_CI and rec.get('since') else now
        waited, limit = now - float(since), landing_wait_s(self.product.conventions)
        if waited < limit:
            lane.out(f'waiting {b}: PR #{number} required check(s) not run — {", ".join(missing)} '
                     f'(landing_checks_missing: wait, {int(waited // 60)}/{int(limit // 60)} min)')
            wait(lane, f, f'required checks missing: {", ".join(missing)}', state=WAITING_CI,
                 since=since)
            return None
        lane.out(f'harvest: {b}: PR #{number} required check(s) never ran in {int(limit // 60)} '
                 f'min — {", ".join(missing)}: gating locally')
        return 'gate'


    def trunk_red(self, names):
        """``{name: sha}`` — each of ``names`` whose latest *completed* run on the trunk failed,
        was cancelled or timed out, with the commit that run judged. The trunk's first-parent
        history is walked newest first (at most :data:`TRUNK_RED_DEPTH` commits) until every
        name has a completed run; a check still running on the newest commit is judged by the
        one before. Unreadable (no ``gh``, no access) reads as not red — the PR's own checks and
        the gate still judge it, as before."""
        want = [n for n in dict.fromkeys(names or ()) if n]
        repo = getattr(self.lane, 'repo', None)
        if not want or not repo:
            return {}
        log = H.sh(['git', 'rev-list', '--first-parent', '-n', str(TRUNK_RED_DEPTH),
                    f'origin/{self.trunk}'], cwd=repo)
        red, left = {}, set(want)
        for sha in log.stdout.split() if log.returncode == 0 else ():
            if sha not in self._trunk_runs:
                data = H.gh_json(['api', f'repos/{self.slug}/commits/{sha}/check-runs'
                                         f'?per_page=100'], None)
                runs = data.get('check_runs') if isinstance(data, dict) else None
                self._trunk_runs[sha] = [r for r in runs if isinstance(r, dict)] \
                    if isinstance(runs, list) else None
            runs = self._trunk_runs[sha]
            if runs is None:
                break
            for name in sorted(left):
                done = [r for r in runs if r.get('name') == name
                        and r.get('status') == 'completed']
                if not done:
                    continue
                left.discard(name)
                last = max(done, key=lambda r: str(r.get('completed_at') or ''))
                if last.get('conclusion') in TRUNK_RED_CONCLUSIONS:
                    red[name] = sha
            if not left:
                break
        return {n: red[n] for n in want if n in red}


def pr_checks(slug, number, required=()):
    """``('green'|'pending'|'red'|'unknown', detail, checks)`` for PR ``number``'s checks. No
    checks at all is green. With ``required`` names (``landing_checks`` or branch protection) only
    those judge: a failed or cancelled required one is red, else an unfinished required one is
    pending — a red check the product does not require (a DCO bot, an advisory test job) never
    turns the PR red. With none required, any failed or cancelled check is red, else any not
    finished is pending. ``checks`` is always the full list."""
    rc, stdout, err = H._gh(['pr', 'checks', str(number), '-R', slug, '--json', 'name,bucket'])
    if 'no checks reported' in f'{stdout}\n{err}':
        return 'green', 'no checks', []
    try:
        checks = [c for c in json.loads(stdout) if isinstance(c, dict)]
    except (json.JSONDecodeError, TypeError):
        return 'unknown', H.tail(err) or f'gh pr checks exited {rc}', []
    judged = [c for c in checks if c.get('name') in required] if required else checks
    red = [c.get('name') or '?' for c in judged if c.get('bucket') in RED_BUCKETS]
    if red:
        return 'red', ', '.join(red), checks
    pending = [c.get('name') or '?' for c in judged if c.get('bucket') == 'pending']
    if pending:
        return 'pending', ', '.join(pending), checks
    return 'green', f'{len(checks)} check(s)', checks


def not_required_red(checks, required):
    """The names of ``checks`` that failed or were cancelled but are not ``required`` — told, never
    acted on. Empty when nothing is required: then every red check already counts."""
    if not required:
        return []
    return [c.get('name') or '?' for c in checks
            if c.get('bucket') in RED_BUCKETS and c.get('name') not in required]


def protected_checks(slug, trunk, state_dir, now=None):
    """The trunk's branch-protection required checks, cached in ``state_dir`` for an hour."""
    now = time.time() if now is None else now
    key = f'{slug}@{trunk}'
    path = os.path.join(state_dir, REQUIRED_CACHE)
    try:
        with open(path, encoding='utf-8') as fh:
            cache = json.load(fh)
        cache = cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(key)
    if isinstance(hit, dict) and now - float(hit.get('at') or 0) < REQUIRED_TTL_S:
        return tuple(hit.get('checks') or ())
    data = H.gh_json(['api', f'repos/{slug}/branches/{trunk}/protection/required_status_checks'],
                     {})
    names = []
    if isinstance(data, dict):
        names = [str(c) for c in data.get('contexts') or []]
        names += [str(c.get('context')) for c in data.get('checks') or []
                  if isinstance(c, dict) and c.get('context')]
    names = list(dict.fromkeys(names))
    cache[key] = {'at': now, 'checks': names}
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path + '.tmp', 'w', encoding='utf-8') as fh:
            json.dump(cache, fh, sort_keys=True)
        os.replace(path + '.tmp', path)
    except OSError:
        pass
    return tuple(names)


def host(product):
    """The :class:`Host` for the product's ``landing``: :class:`GitHubHost` for
    ``pull-request`` with a PR host, else :class:`FastForwardHost`."""
    return Lane(product).host


# ---- the read side ----------------------------------------------------------------------------

def facts(product):
    """Gather the pass's lane facts once (:meth:`Lane.gather`): ``{branch: facts}`` over every
    lane branch — ``head``, ``item``, ``run``, ``pr``, ``review``, ``class``, ``on_trunk``,
    ``now`` and the rest :func:`next_state` reads. Read-only: no git write, no ``gh`` write."""
    return Lane(product).gather(prs=True)


def advance(product, branch, facts):
    """Decide ``branch``'s next state from its facts (:func:`next_state`) and, when it moved,
    write it on the owning run, performing the transition's side effect. Returns the lane record
    written, or None when the state did not change. The only writer of a lane state."""
    lane = product if isinstance(product, Lane) else Lane(product)
    return lane.advance(facts.get(branch) if branch in (facts or {}) else facts)


def snapshot(product):
    """Every lane branch's current record, ``{branch: {state, head, pr, at, reason, item}}``.
    Read-only."""
    return snapshot_at(env.state_dir(product))


def snapshot_at(state_dir):
    """:func:`snapshot` of the registry under ``state_dir``."""
    path = H.sessions_path(state_dir)
    out = {}
    for b, run in lifecycle.by_branch(path).items():
        rec = run.get('lane')
        if rec:
            out[b] = dict(rec, item=rec.get('item') or run.get('item'), job=run.get('job'))
    return out


def state(product, branch):
    """``branch``'s current lane record, or None when no run carries one. Read-only."""
    return snapshot(product).get(branch)


def busy_items(product):
    """The item ids whose branch is in one of :data:`BUSY_STATES`. Read-only."""
    return {r['item'] for r in snapshot(product).values()
            if r.get('item') and r.get('state') in BUSY_STATES}
