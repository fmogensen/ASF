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
owns it). The line names that branch too: one job name can hold runs on two branches (a
Feature's spec and plan both corrected as ``correct-f-…``), and the fold puts the line on the
job's run on that branch. Adopting a branch or PR no run made writes a synthetic run. A v0.1.2 binary reading
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
- T2c PUSHED → BACK            PR mode: the branch conflicts with the trunk — GitHub would run
                               no ``pull_request`` workflow on its PR; rebased first
- T3  PR_OPEN → REVIEW         ``conventions.lane.review`` requires a review for the landing
                               class (a state, no round is spent)
- T4  REVIEW → GATE            the review of the current head reads approved (or policy none)
- T5  REVIEW → BACK            the review of the current head reads changes (rounds+1)
- T5c PR_OPEN/REVIEW → BACK    a required check failed on the PR's exact head (any event's run):
                               a correct round against the named failures, before any review
- T5e PR_OPEN/REVIEW → BACK    the open PR conflicts with the trunk: no run will come, no
                               review round is spent; back to be rebased (the lane's own first)
- T5d BACK → BACK              a pending review round on a head whose required checks failed:
                               superseded by the T5c gate correction (the failures go first)
- T5a REVIEW → GATE            ... unless an adjudicate ruling already answered it on the code
                               this head carries (:func:`asf.workers.lifecycle.overruling`): it
                               overruled the C list and pushed nothing — no second hold, no
                               second ruling — or every C item it raises is a point a standing
                               ruling on the card settled (:func:`asf.evidence.rulings.
                               reraised_only`): no BACK, no round; a new defect still blocks
- T6  GATE → WAITING_CI        required checks pending/absent under ``landing_checks_missing``
- T7  WAITING_CI → GATE        re-decided every harvest
- T8  GATE → WAITING           trunk red alone (or a required check red on the trunk's latest
                               completed run that the PR does not turn green on top of that
                               red), no merge budget, deferred, a shared path, a gate
                               timeout, an approval hold, host pressure (:func:`held_by_host`:
                               the gate is a full suite and this host has no room for it, B-0109)
                               — never a correction, never a round
- T9  GATE → BACK              red alone on a green trunk, conflict, a lane refusal
- T9i PUSHED → BACK            a delivery branch (``delivers:``) not whole — a member no commit
                               names while the report does not say ``done``: a crash, a run
                               cap, ``status: partial`` (:func:`incomplete_refusal`) — no PR
                               opens; the lead comes back as a DELIVERY → CODE row and the
                               session continues from the branch's head
- T9c wait → BACK              a pending correction written on a landing wait (PR_OPEN, REVIEW,
                               GATE, WAITING_CI, WAITING) since its head arrived
                               (:func:`correction_turns_back`): kind = the correction's
- T10 GATE → MERGING → MERGED  merged by the host (FF ``push_ff``; PR ``gh pr merge``)
- T10q GATE → QUEUED → MERGED  a merge queue took it; queue-rejected → WAITING (R5)
- T10b GATE → QUEUED → MERGED  ``conventions.merge: queue``: the lane's own serialized queue
                               (:mod:`asf.merge_queue`) cut the green PR into a batch ref
                               (``batch`` and ``sha`` on the record), gated that exact sha on
                               the product's whole required set, and fast-forwarded the trunk
                               to it; a dropped batch → WAITING, cut again; a red one → BACK
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
                               (:meth:`Lane.repair_naming`) — no session, no round, pushed
                               ``--no-verify`` once every tree is identical; a live session,
                               a second lease race or a refused push defers it (no hold); only
                               a rewrite it cannot do goes back to the session (no round)
- T13c any open → PUSHED       copies of trunk commits or a merge of the trunk under a factory
                               branch (a session or a person merged ``origin/<trunk>`` in): the
                               lane rebuilds it as the trunk plus its own commits
                               (:meth:`Lane.drop_copies`), the old tip kept as
                               ``archive/<branch>-copies-<sha9>`` — no session; a pick that
                               conflicts pushes nothing and goes BACK with its files; a live
                               session defers it; a rebuild its guard refuses stays B-0056's hold
- T13r conflict → PUSHED       a branch sent back for a conflict with the trunk (the host refused
                               the merge, the merge queue could not merge it, the gate could not
                               stack it): the lane rebases its own commits onto the trunk itself
                               (:meth:`Lane.rebase_onto_trunk`), runs the product's
                               ``pre_push_check`` on the result and pushes it once — no session.
                               Only a pick git cannot merge (a textual conflict, its files named)
                               or a red pre-push check goes BACK to a session
- T13s gate → PUSHED          the PR's sign-off check (``commit.signoff_check``, "DCO") red:
                               the lane signs the unsigned commits off itself
                               (:meth:`Lane.repair_signoff`) — a factory branch only
- T13t BACK/STALE → PUSHED     approved content held only by git mechanics: the newest review
                               approved the content on a head H, and the branch is held BACK by
                               a mechanical correction (naming, copies, a merge, a conflict, a
                               failed rebase, an unpushed end, a hook/gate red only on generated
                               files or factory artifacts) or STALE for being unmoved — the lane
                               transplants H's version of the Task's own files onto a fresh
                               trunk (:meth:`Lane.transplant`, :mod:`asf.harvest.transplant`):
                               artifacts left out, generated files regenerated by
                               ``conventions.generated`` or kept at the trunk, pre_push_check,
                               ONE push with the old tip archived — no session. The approval is
                               carried to the new head only when the diff for those files is
                               H's; otherwise a review round. A red check is a correction round.
                               Never with a session running on the item, an operator park, an
                               ``asf correct`` instruction or ruling, a reset of H, or past
                               ``transplant.CAP`` transplants of H
- T13m BACK/wait → PUSHED       ``flags.mechanical``: a pending conflict/copies/merge correction
                               (an ``at_cap`` one headed for adjudication too) retried by the
                               table (:func:`asf.harvest.mechanical.retry_pending`) once the head
                               or the trunk moved — rebuilt on the trunk, no session; an approval
                               current on the head it left is carried (``mechanical_from``)
- Th  any open, head moved     → PUSHED(new head) (R4)
- Tp  any open (not MERGING/QUEUED) → PARKED  the PR reads ``isDraft``: the owner parked it —
                               no merge, no review/correction/adjudicate/fix row, no reword or
                               rebase; the feeder shows a WAITS row naming the draft PR
- Tp' PARKED → PR_OPEN         the PR is marked ready for review again — normal transitions
                               resume from there
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

from asf import (approvals, attestation, customer_content, env, gitpush, refguard, reviews,
                 run_cancel)
from asf.evidence import review as review_mod
from asf.evidence import review_store
from asf.evidence import rulings as rulings_mod
from asf.feeder import footprint, widen
from asf.harvest import harvest as H
from asf.harvest import mechanical
from asf.harvest import pr_graph
from asf.harvest import transplant as transplant_mod
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
#: the owner marked the PR a draft: every open state parks here — no merge, no review, no
#: correction/adjudicate, no reword or rebase — until it is marked ready again (Tp/Tp')
PARKED = 'PARKED'
MERGED = 'MERGED'
STALE = 'STALE'
REAPED = 'REAPED'

#: Every lane state, in the order a branch normally passes them.
LANE_STATES = (PUSHED, PR_OPEN, REVIEW, GATE, WAITING_CI, WAITING, QUEUED, MERGING, BACK,
               PARKED, MERGED, STALE, REAPED)
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
#: The states a head's heavy-CI approval (``ci.heavy_after_review``) does not survive: back to a
#: session, a new head, or the branch's end.
HEAVY_DROPS = (BACK, PUSHED) + TERMINAL_STATES
#: The heavy-CI label's colour and description when the lane creates it in a repo.
HEAVY_LABEL_COLOR = '5319e7'
HEAVY_LABEL_DESCRIPTION = 'ASF: the review approved this head; heavy CI may run'
#: How long a head approved for heavy CI may go without any workflow run created since the
#: approval before the lane starts one itself (:meth:`GitHubHost.heavy_kick`): GitHub starts no
#: ``labeled`` run while the PR's test merge ref still carries a workflow without that trigger
#: (2026-09-30, the first product: nine PRs labelled two minutes after the trigger landed, none ran).
HEAVY_KICK_S = 600
#: The landing waits a pending correction turns BACK (:func:`correction_turns_back`): the
#: branch waits on a PR, a review or the gate, and nothing but a session's push changes it.
#: MERGING and QUEUED are a merge under way; PARKED is its owner's.
LANDING_WAITS = (PR_OPEN, REVIEW, GATE, WAITING_CI, WAITING)


def head_since(prev, head, now):
    """When the lane first saw ``head`` on the branch (a lane record's ``head_at``): ``prev``'s
    own while the head has not moved — a record from before ``head_at`` existed, its ``at`` —
    else ``now``."""
    prev = prev or {}
    if head and prev.get('head') == head:
        return prev.get('head_at') or prev.get('at') or now
    return now


def correction_turns_back(rec, corr):
    """True when a pending correction ``corr`` sends a branch the lane holds at ``rec`` BACK to
    a session: ``rec`` is a landing wait (:data:`LANDING_WAITS`) and ``corr`` was written at or
    after the lane first saw the current head (:func:`head_since`) — a correction the head moved
    past is answered. Health's own hold at the round cap (``at_cap``) is not: it goes to
    adjudication, and the gate may still land the branch as it stands (a product's T-0026,
    2026-09-27: a ``redact`` stamped on PR #707 at GATE never reached a session). An operator
    ruling (``asf correct`` at the cap) always does — a CI wait on a head the ruling is there to
    change is no reason to hold it (a product's T-0594: the ruling waited on CI for a known-red
    head and launched nothing)."""
    rec, corr = rec or {}, corr or {}
    if rec.get('state') not in LANDING_WAITS or not corr.get('text') \
            or (corr.get('at_cap') and not corr.get('operator_ruling')):
        return False
    since = rec.get('head_at') or rec.get('at') or ''
    return (corr.get('at') or '') >= since

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
#: A ``cancel`` is never red: a run a newer push or the host cancelled judged no code (the CI
#: queue re-runs it), so :func:`pr_checks` holds it pending, never a correction.
RED_BUCKETS = ('fail',)
CANCEL_BUCKET = 'cancel'
#: the one bucket that is green for a required check: a skipped required check (a path filter,
#: an ``if:``) is not green — it tested nothing (2026-09-26 incident)
PASS_BUCKETS = ('pass',)
SKIP_BUCKET = 'skipping'
#: ``conventions.merge_skipped``: a PR whose CI path-filters suites (a job's ``if:`` false, a
#: workflow that never created a job) merges once its workflow runs on the head all completed,
#: at least one required check concluded success and none is red (``path-filtered``, the
#: default) — or never merges on a skipped or missing required check (``never``). PR merges
#: only: a deploy never counts a skipped job green.
MERGE_SKIPPED_PATH = 'path-filtered'
MERGE_SKIPPED_NEVER = 'never'
#: A trunk check run that ended in one of these is red (:meth:`GitHubHost.trunk_red`); a
#: ``cancelled`` run is no verdict at all and is skipped for the run before it.
TRUNK_RED_CONCLUSIONS = ('failure', 'timed_out', 'startup_failure')
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
#: ``{branch: epoch}`` the gate last took each branch: the capped rotation (:func:`take_for_tick`)
GATE_VISITS = 'gate-visits.json'
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


def review_kind(kind):
    """The `asf.reviews` checklist a branch's `kind` (:func:`conventions.branch_kind`) reads by:
    ``spec``/``plan`` keep their own name, anything else (``code``, ``fix``, ``direct``, a legacy
    prefix, or None) reads as ``code`` — the one review checklist every non-doc branch answers to
    (PS3)."""
    return kind if kind in ('spec', 'plan') else CODE


def shared_hits(conv, files):
    """The files among ``files`` under a ``conventions.shared_paths`` or ``shared_writes`` glob."""
    globs = list(footprint.shared_globs(conv))
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


def docs_only_task(product, items, item_id, files=None):
    """True when ``flags.docs_review: skip`` is set, the Task/Bug's ``writes:`` all lie under a
    docs root (:func:`landing_class`) and ``lane.review.docs`` is not ``required`` — its branch
    needs no review row (W2-PR7). ``files`` (the branch's diff), when given, must also stay inside
    the declared ``writes:``: a branch that wrote elsewhere is still reviewed."""
    conv = _conv(product)
    if str(conv.flag('docs_review') or '').strip().lower() != 'skip' or conv.review_required(DOCS):
        return False
    item = (items or {}).get(item_id) or {}
    from asf.feeder import widen  # local: the feeder imports the lane
    writes = widen.norm_writes(item.get('writes'))  # a plan's `[a b c]` is three paths
    if item.get('type') not in ('task', 'bug') or not writes or landing_class(product, writes) != DOCS:
        return False
    return all(any(_glob_hit(w, f) for w in writes) for f in files or ())


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


#: an item id a commit subject names (``task(T-0491): …``, ``fix(B-0001): …``)
_SUBJECT_ID_RE = re.compile(r'\b([A-Z]-\d{4,})\b')


def commits_note(repo, trunk, branch):
    """The part of a PR body the branch's own commits say (``origin/<trunk>..origin/<branch>``,
    oldest first): ``## Items`` — every item id a subject names — and ``## Proves`` — every
    ``Proves:`` trailer (:func:`asf.proves.parse`). '' when they say neither, or git cannot
    read the range."""
    from asf import gitops, proves
    r = gitops.git(['log', '--reverse', '--no-merges', '--format=%B%x1e',
                    f'origin/{trunk}..origin/{branch}'], repo) if repo else None
    if r is None or not r.ok:
        return ''
    messages = [m.strip() for m in (r.stdout or '').split('\x1e') if m.strip()]
    ids = []
    for m in messages:
        for i in _SUBJECT_ID_RE.findall(m.splitlines()[0]):
            if i not in ids:
                ids.append(i)
    claims = proves.parse('\n'.join(messages))
    out = ''
    if ids:
        out += '\n## Items\n\n' + ''.join(f'- {i}\n' for i in ids)
    if claims:
        out += '\n## Proves\n\n' + proves.render(claims) + '\n'
    return out


def _subjects(repo, trunk, branch):
    return H.sh(['git', 'log', '--no-merges', '--format=%s', f'origin/{trunk}..origin/{branch}'],
                cwd=repo).stdout.splitlines()


def commits_name_items(repo, trunk, branch, ids):
    """True when every commit subject on ``origin/<branch>`` not on ``origin/<trunk>`` names at
    least one id of ``ids`` — a delivery's branch carries a commit per member."""
    subjects = _subjects(repo, trunk, branch)
    return bool(subjects) and all(any(githooks.names_item(s, i) for i in ids) for s in subjects)


def commits_name_item(repo, trunk, branch, item):
    """True when every commit subject on ``origin/<branch>`` not on ``origin/<trunk>`` names
    ``item`` as a token."""
    return commits_name_items(repo, trunk, branch, [item])


def members_named(repo, trunk, branch, ids):
    """``(named, missing)`` — the ids of ``ids`` that some commit subject on ``origin/<branch>``
    names, and those none does, each in the order given."""
    subjects = _subjects(repo, trunk, branch)
    named = [i for i in ids if any(githooks.names_item(s, i) for s in subjects)]
    return named, [i for i in ids if i not in named]


#: the author and committer fields :func:`rebuild_branch` carries onto each rewritten commit
_IDENT = (('GIT_AUTHOR_NAME', '%an'), ('GIT_AUTHOR_EMAIL', '%ae'), ('GIT_AUTHOR_DATE', '%ad'),
          ('GIT_COMMITTER_NAME', '%cn'), ('GIT_COMMITTER_EMAIL', '%ce'),
          ('GIT_COMMITTER_DATE', '%cd'))


def reword_branch(repo, trunk, branch, item, kind=None, why=None):
    """``(new_tip, n)``: the branch's own commits (:func:`own_commits`), each subject not naming
    ``item`` rewritten by :func:`asf.workers.githooks.name_subject` — trees, authors,
    committers and dates identical, body untouched, no checkout and no hook run (``git
    commit-tree``). ``(None, 0)`` when nothing needs a reword or the branch cannot be rewritten
    safely — the reason appended to ``why`` when given."""
    def reword(message, _ident):
        subject, nl, body = message.partition('\n')
        return githooks.name_subject(subject, item, kind) + nl + body
    return rebuild_branch(repo, trunk, branch, reword, why)


def signed_off(message, name, email):
    """True when ``message`` carries ``Signed-off-by: <name> <email>`` as a trailer line."""
    want = f'signed-off-by: {name} <{email}>'.lower()
    return any(line.strip().lower() == want for line in (message or '').splitlines())


def signoff_branch(repo, trunk, branch, why=None):
    """``(new_tip, n)``: the branch's own commits (:func:`own_commits`), each commit its author
    has not signed off given ``Signed-off-by: <author name> <author email>`` (``git
    interpret-trailers``) — trees, authors, committers, dates and the rest of the message
    identical, as :func:`reword_branch`. ``(None, 0)`` when every commit is signed off already or
    the branch cannot be rewritten safely."""
    def sign(message, ident):
        name, email = ident['GIT_AUTHOR_NAME'], ident['GIT_AUTHOR_EMAIL']
        if signed_off(message, name, email):
            return message
        r = subprocess.run(['git', 'interpret-trailers', '--if-exists', 'addIfDifferent',
                            '--trailer', f'Signed-off-by: {name} <{email}>'],
                           input=message, cwd=repo, capture_output=True, text=True,
                           env=H.clean_env(dict(os.environ)))
        return r.stdout if r.returncode == 0 and r.stdout.strip() else None
    return rebuild_branch(repo, trunk, branch, sign, why)


def _add_trailers(repo, message, pairs, if_exists):
    """``message`` with each ``(key, value)`` of ``pairs`` as a trailer (``git
    interpret-trailers --if-exists <if_exists>``), or None when git refused it."""
    if not pairs:
        return message
    args = ['git', 'interpret-trailers', '--if-exists', if_exists]
    for key, value in pairs:
        args += ['--trailer', f'{key}: {value}']
    r = subprocess.run(args, input=message, cwd=repo, capture_output=True, text=True,
                       env=H.clean_env(dict(os.environ)))
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def normalise_message(repo, message, ident, item, signoff=False, trailers=None):
    """``message`` with the trailers the lane writes on publish, or None when git could not
    rewrite it: ``Signed-off-by: <author>`` when ``signoff`` and the author has not signed off;
    each ``trailers`` key (``{item}`` in its value is the item id) set to its value — added when
    missing, replaced when it differs. The subject is :meth:`Lane.repair_naming`'s. A message
    that needs none of it comes back unchanged, byte for byte."""
    if signoff:
        name, email = ident['GIT_AUTHOR_NAME'], ident['GIT_AUTHOR_EMAIL']
        if not signed_off(message, name, email):
            message = _add_trailers(repo, message, [('Signed-off-by', f'{name} <{email}>')],
                                    'addIfDifferent')
            if message is None:
                return None
    want = [(k, v.replace('{item}', item or '')) for k, v in (trailers or {}).items()]
    have = {line.strip().lower() for line in message.splitlines()}
    missing = [(k, v) for k, v in want if f'{k.lower()}: {v.lower()}' not in have]
    return _add_trailers(repo, message, missing, 'replace') if missing else message


def normalise_branch(repo, trunk, branch, item, signoff=False, trailers=None, why=None):
    """``(new_tip, n)``: the branch's own commits (:func:`own_commits` — never a trunk commit,
    never a merge) with every message through :func:`normalise_message` — the sign-off and the
    product's trailers — trees, authors, committers and dates identical, through
    :func:`rebuild_branch`'s guard. ``(None, 0)`` when every message is already right or the
    branch cannot be rewritten safely (the reason appended to ``why``)."""
    return rebuild_branch(repo, trunk, branch,
                          lambda message, ident: normalise_message(
                              repo, message, ident, item, signoff, trailers), why)


def own_commits(repo, trunk, branch):
    """``(base, shas, why)``: the commits that are ``origin/<branch>``'s own, oldest first, on
    ``base`` (its merge-base with ``origin/<trunk>``) — ``git rev-list --no-merges
    origin/<trunk>..origin/<branch>``. Only these may ever be rewritten: a commit reachable from
    the trunk is not the branch's. ``shas`` is None, with ``why`` the one line, when the branch
    is not a straight line of its own commits on the trunk: a merge on it (trunk history merged
    in), a commit whose patch is already on the trunk (``git cherry`` ``-``: a copy of trunk
    history under the branch), or a chain that does not run straight from ``base``."""
    tip = H.sh(['git', 'rev-parse', '--verify', '-q', f'origin/{branch}'], cwd=repo).stdout.strip()
    base = H.sh(['git', 'merge-base', f'origin/{trunk}', f'origin/{branch}'],
                cwd=repo).stdout.strip()
    if not tip or not base:
        return None, None, f'origin/{branch} or its merge-base with origin/{trunk} unreadable'
    merges = H.sh(['git', 'rev-list', '--merges', f'origin/{trunk}..{tip}'], cwd=repo)
    if merges.returncode != 0 or merges.stdout.split():
        return None, None, (f'a merge commit on it ({merges.stdout.split()[0][:9]}) — trunk '
                            f'history merged in; the lane rewrites no merge'
                            if merges.returncode == 0 else 'git rev-list failed')
    r = H.sh(['git', 'log', '--reverse', '--topo-order', '--no-merges', '--format=%H %P',
              f'origin/{trunk}..{tip}'], cwd=repo)
    if r.returncode != 0:
        return None, None, 'git log failed'
    rows = [l.split() for l in r.stdout.splitlines() if l.strip()]
    cherry = H.sh(['git', 'cherry', f'origin/{trunk}', tip], cwd=repo)
    if cherry.returncode != 0:
        return None, None, 'git cherry failed'
    dup = [l.split()[1] for l in cherry.stdout.splitlines() if l.startswith('- ')]
    if dup:
        return None, None, (f'{len(dup)} commit(s) on it are copies of origin/{trunk} commits '
                            f'(first {dup[0][:9]}) — trunk history under the branch: rebase '
                            f'onto origin/{trunk}')
    parent = base
    for row in rows:
        if len(row) != 2 or row[1] != parent:
            return None, None, f'{row[0][:9]} does not sit straight on its own history'
        parent = row[0]
    if parent != tip:
        return None, None, f'origin/{branch} is not a straight line on origin/{trunk}'
    return base, [row[0] for row in rows], ''


def _diff(repo, trunk, rev):
    r = H.sh(['git', 'diff', '--binary', '--full-index', f'origin/{trunk}...{rev}'], cwd=repo)
    return r.stdout if r.returncode == 0 else None


def rebuild_branch(repo, trunk, branch, transform, why=None):
    """``(new_tip, n)``: the branch's own commits (:func:`own_commits` — never a trunk commit,
    never a merge) each rebuilt with its message passed through ``transform(message, ident)``
    (``ident``: the commit's ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``) by ``git commit-tree`` —
    trees, authors, committers and dates identical, no checkout and no hook run. ``n`` counts the
    messages that changed. ``(None, 0)`` when none did, the branch is not its own straight line,
    a commit cannot be rebuilt (a git error, a ``transform`` that answers None), or the rebuilt
    branch fails the guard — its ``origin/<trunk>...`` diff must equal the old one's and it must
    carry exactly as many commits past the trunk as the branch owned. ``why`` (a list, when
    given) gets the one-line reason."""
    note = why.append if why is not None else (lambda _s: None)
    base, revs, reason = own_commits(repo, trunk, branch)
    if revs is None:
        note(reason)
        return None, 0
    parent, n = base, 0
    for sha in revs:
        r = H.sh(['git', 'log', '-1', '--date=raw',
                  '--format=' + '%x00'.join(f for _, f in _IDENT) + '%x00%T%x00%P', sha], cwd=repo)
        fields = r.stdout.rstrip('\n').split('\x00')
        if r.returncode != 0 or len(fields) != len(_IDENT) + 2 or len(fields[-1].split()) != 1:
            note(f'{sha[:9]} unreadable')
            return None, 0
        raw = H.sh(['git', 'cat-file', 'commit', sha], cwd=repo).stdout
        message = raw.split('\n\n', 1)[1] if '\n\n' in raw else ''
        ident = {k: v for (k, _), v in zip(_IDENT, fields)}
        made_msg = transform(message, ident)
        if made_msg is None:
            note(f'{sha[:9]}: its message could not be rewritten')
            return None, 0
        if made_msg == message and fields[-1] == parent:
            parent = sha
            continue
        n += made_msg != message
        env = dict(os.environ, **ident)
        made = subprocess.run(['git', 'commit-tree', fields[-2], '-p', parent],
                              input=made_msg, cwd=repo, capture_output=True, text=True,
                              env=H.clean_env(env))
        parent = made.stdout.strip()
        if made.returncode != 0 or not parent:
            note(f'{sha[:9]}: git commit-tree failed')
            return None, 0
    if not n:
        return None, 0
    # the guard, before anything is pushed: the same change on the trunk, the same commit count
    count = H.sh(['git', 'rev-list', '--count', f'origin/{trunk}..{parent}'], cwd=repo)
    old_diff = _diff(repo, trunk, f'origin/{branch}')
    if count.stdout.strip() != str(len(revs)) or old_diff is None \
            or _diff(repo, trunk, parent) != old_diff:
        note(f'rewrite guard: the rebuilt branch is not the old one\'s {len(revs)} commits with '
             f'the same diff on origin/{trunk} — nothing pushed')
        return None, 0
    return parent, n


#: :meth:`Lane.repair_naming`'s answer when the reword waits for a later pass — a live session
#: holds the branch, the branch moved under two reads, or the push went nowhere (the remote, the
#: network): no session is started and no hold is written
DEFERRED = 'deferred'


def trees_identical(repo, old, new):
    """``(same, why)``: ``new`` is ``old`` with only its messages changed — the same parent under
    the same number of commits, and the same tree on each, oldest first (``git rev-list
    --first-parent``, walked back to where the two histories meet). ``why`` names the first
    difference, else ''."""
    base = H.sh(['git', 'merge-base', old, new], cwd=repo).stdout.strip()
    if not base:
        return False, f'{old[:9]} and {new[:9]} share no history'

    def trees(tip):
        r = H.sh(['git', 'log', '--first-parent', '--reverse', '--format=%T',
                  f'{base}..{tip}'], cwd=repo)
        return r.stdout.split() if r.returncode == 0 else None
    a, b = trees(old), trees(new)
    if a is None or b is None:
        return False, 'the trees could not be read'
    if len(a) != len(b):
        return False, f'{len(a)} commits became {len(b)}'
    for i, (x, y) in enumerate(zip(a, b), 1):
        if x != y:
            return False, f'commit {i} of {len(a)}: tree {x[:9]} became {y[:9]}'
    return True, ''


def push_cause(r):
    """``(cause, text)`` for a refused push: ``'lease'`` (the branch moved since the read: a
    stale lease, a non-fast-forward), ``'timeout'``, ``'remote'`` (the remote declined it) or
    ``'other'`` — ``text`` the push's own words, hook output on stdout included."""
    text = '\n'.join(s for s in (r.stderr, r.stdout) if s)
    low = text.lower()
    if r.returncode == gitpush.TIMED_OUT:
        return 'timeout', push_why(text)
    if 'stale info' in low or 'fetch first' in low or 'non-fast-forward' in low:
        return 'lease', push_why(text)
    if 'remote rejected' in low or 'declined' in low or 'protected' in low:
        return 'remote', push_why(text)
    return 'other', push_why(text)


#: the correction kind of a trunk-history rebuild (:func:`drop_trunk_copies`) that conflicted:
#: its session rebases the branch's own commits onto the trunk, the factory publishes the result
#: (no round spent, never adjudicate — :data:`asf.workers.lifecycle.MECHANICAL`)
COPIES = lifecycle.COPIES


def trunk_history(repo, trunk, branch):
    """``(copies, merges)``: the commits on ``origin/<branch>`` past ``origin/<trunk>`` whose
    patch is already on the trunk (``--cherry-mark`` ``=``: copies of trunk commits, from a
    session rebasing or merging the trunk in, or an old reword), and the merge commits on it."""
    r = H.sh(['git', 'log', '--no-merges', '--cherry-mark', '--right-only', '--format=%m %H',
              f'origin/{trunk}...origin/{branch}'], cwd=repo)
    copies = [l.split()[1] for l in r.stdout.splitlines()
              if r.returncode == 0 and l.startswith('= ')]
    m = H.sh(['git', 'rev-list', '--merges', f'origin/{trunk}..origin/{branch}'], cwd=repo)
    return copies, (m.stdout.split() if m.returncode == 0 else [])


def _tree(repo, rev):
    return H.sh(['git', 'rev-parse', f'{rev}^{{tree}}'], cwd=repo).stdout.strip()


def drop_trunk_copies(repo, trunk, branch, always=False):
    """Rebuild ``origin/<branch>`` as ``origin/<trunk>`` plus its OWN commits — every non-merge
    commit past the trunk whose patch is not already on it — each applied in order by a
    three-way merge against its own parent (``git merge-tree --merge-base``: a cherry-pick with
    no checkout), its message, author and committer kept. Copies of trunk commits and merges are
    dropped; a commit whose change the trunk already holds (an empty pick) too. Nothing is
    pushed or written but objects. A dict: ``old`` (the tip read), ``new`` (the rebuilt tip, or
    None), ``own`` (the shas kept), ``copies``, ``merges``, ``empty``, ``conflict``
    (``(sha, [files])`` of the first pick that conflicts — the rebuild stops there) and ``why``
    (why there is no ``new``). The guard: when the old tip merges cleanly into the trunk, the
    rebuilt tree must be that merge's tree — never a change lost or brought back silently.
    ``always``: rebuild a branch with neither copies nor merges too — a plain rebase onto the
    trunk (:meth:`Lane.rebase_onto_trunk`); a branch already straight on the trunk's tip
    rebuilds to its own tip, and that is no ``new``."""
    res = {'old': '', 'new': None, 'own': [], 'copies': [], 'merges': [], 'empty': [],
           'conflict': None, 'why': ''}
    tip = H.sh(['git', 'rev-parse', '--verify', '-q', f'origin/{branch}'], cwd=repo).stdout.strip()
    base = H.sh(['git', 'rev-parse', '--verify', '-q', f'origin/{trunk}'], cwd=repo).stdout.strip()
    res['old'] = tip
    if not tip or not base:
        res['why'] = f'origin/{branch} or origin/{trunk} unreadable'
        return res
    r = H.sh(['git', 'log', '--reverse', '--topo-order', '--no-merges', '--cherry-mark',
              '--right-only', '--format=%m %H %P', f'{base}...{tip}'], cwd=repo)
    if r.returncode != 0:
        res['why'] = 'git log failed'
        return res
    rows = [l.split() for l in r.stdout.splitlines() if l.strip()]
    res['copies'] = [row[1] for row in rows if row[0] == '=']
    m = H.sh(['git', 'rev-list', '--merges', f'{base}..{tip}'], cwd=repo)
    res['merges'] = m.stdout.split() if m.returncode == 0 else []
    if not (res['copies'] or res['merges'] or always):
        res['why'] = 'no copy of a trunk commit and no merge on the branch'
        return res
    own = [row for row in rows if row[0] != '=']
    parent = base
    for row in own:
        sha, parents = row[1], row[2:]
        if len(parents) != 1:
            res['why'] = f'{sha[:9]} has no single parent'
            return res
        mt = H.sh(['git', 'merge-tree', '--write-tree', '--name-only', '--no-messages',
                   f'--merge-base={parents[0]}', parent, sha], cwd=repo)
        lines = mt.stdout.splitlines()
        if mt.returncode == 1:
            files = []
            for line in lines[1:]:
                if not line.strip():
                    break
                if line not in files:
                    files.append(line)
            res['conflict'] = (sha, files)
            return res
        if mt.returncode != 0 or not lines:
            res['why'] = f'{sha[:9]}: git merge-tree failed: {H.tail(mt.stderr or mt.stdout)}'
            return res
        tree = lines[0].strip()
        if tree == _tree(repo, parent):
            res['empty'].append(sha)  # its change is on the trunk already, by another patch
            continue
        info = H.sh(['git', 'log', '-1', '--date=raw',
                     '--format=' + '%x00'.join(f for _, f in _IDENT), sha], cwd=repo)
        fields = info.stdout.rstrip('\n').split('\x00')
        raw = H.sh(['git', 'cat-file', 'commit', sha], cwd=repo).stdout
        if info.returncode != 0 or len(fields) != len(_IDENT) or '\n\n' not in raw:
            res['why'] = f'{sha[:9]} unreadable'
            return res
        env_ = dict(os.environ, **{k: v for (k, _), v in zip(_IDENT, fields)})
        made = subprocess.run(['git', 'commit-tree', tree, '-p', parent],
                              input=raw.split('\n\n', 1)[1], cwd=repo, capture_output=True,
                              text=True, env=H.clean_env(env_))
        if made.returncode != 0 or not made.stdout.strip():
            res['why'] = f'{sha[:9]}: git commit-tree failed'
            return res
        parent = made.stdout.strip()
        res['own'].append(sha)
    if not res['own']:
        res['why'] = (f'no own commit left: every commit on it is on origin/{trunk} already — '
                      f'nothing to rebuild')
        return res
    whole = H.sh(['git', 'merge-tree', '--write-tree', '--no-messages', base, tip], cwd=repo)
    if whole.returncode == 0 and whole.stdout.split()[:1] != [_tree(repo, parent)]:
        res['why'] = (f'rebuild guard: the rebuilt tree is not origin/{branch} merged into '
                      f'origin/{trunk} — a dropped commit is not the trunk\'s change; nothing '
                      f'pushed')
        return res
    if parent == tip:
        res['why'] = f'already straight on origin/{trunk}: nothing to rebuild'
        return res
    res['new'] = parent
    return res


#: How long each of the product's ``worktree_setup`` and ``pre_push_check`` may run on a head the
#: lane rebuilt, before the check counts as failed.
PRE_PUSH_CHECK_TIMEOUT_S = 900
#: Under the state dir: the throwaway checkouts the lane runs ``pre_push_check`` in.
PRE_PUSH_DIR = 'pre-push-check'
#: The output lines a failed ``pre_push_check`` hands the correction.
PRE_PUSH_LINES = 15


def _run_shell(command, cwd, timeout):
    """``(returncode, output)`` of shell ``command`` in ``cwd``; ``returncode`` None on a
    timeout, its whole process group killed."""
    p = subprocess.Popen(command, shell=True, cwd=cwd, env=H.clean_env(dict(os.environ)),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ''
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, 9)
        except (ProcessLookupError, PermissionError):
            p.kill()
        out, _ = p.communicate()
        return None, out or ''


def pre_push_check_at(repo, state_dir, sha, command, setup=None,
                      timeout=PRE_PUSH_CHECK_TIMEOUT_S):
    """``(ok, line)``: the product's ``pre_push_check`` ``command`` run on ``sha`` in a throwaway
    detached checkout under ``<state>/pre-push-check/`` — after ``setup`` (the product's
    ``worktree_setup``) when given — the same check every session runs before its push. ``ok``
    True when it passed; False when it failed (``line`` names the command, its exit and its last
    :data:`PRE_PUSH_LINES` lines); None when no checkout could be made (nothing was judged)."""
    holder = os.path.join(state_dir, PRE_PUSH_DIR)
    try:
        os.makedirs(holder, exist_ok=True)
        path = tempfile.mkdtemp(prefix='wt-', dir=holder)
        os.rmdir(path)
    except OSError as e:
        return None, f'no checkout to run `{command}` in: {e}'
    add = H.sh(['git', 'worktree', 'add', '-q', '--detach', path, sha], cwd=repo)
    if add.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        return None, f'no checkout of {sha[:9]} to run `{command}` in: {H.tail(add.stderr)}'
    try:
        for what, cmd in (('worktree_setup', setup), ('pre_push_check', command)):
            if not cmd:
                continue
            rc, out = _run_shell(cmd, path, timeout)
            if rc != 0:
                why = f'timed out after {timeout}s' if rc is None else f'exit {rc}'
                lines = [l for l in out.splitlines() if l.strip()][-PRE_PUSH_LINES:]
                return False, (f'`{cmd}` ({what}) failed on {sha[:9]}: {why}'
                               + (':\n' + '\n'.join(lines) if lines else ''))
        return True, ''
    finally:
        H.sh(['git', 'worktree', 'remove', '--force', path], cwd=repo)
        shutil.rmtree(path, ignore_errors=True)


def has_adjudicate_commit(repo, trunk, branch):
    """True when a commit on the branch opens with ``adjudicate(`` — a ruling committed to the
    product repo instead of the record (B-0054)."""
    return any(ADJUDICATE_SUBJECT_RE.match(s) for s in _subjects(repo, trunk, branch))


def merge_commits(repo, trunk, branch):
    r = H.sh(['git', 'log', '--merges', '--format=%h %s', f'origin/{trunk}..origin/{branch}'],
             cwd=repo)
    return [l for l in r.stdout.splitlines() if l.strip()]


def delivery_members(items, item):
    """The ids the card ``item`` delivers (its ``delivers:`` list), else ``()``."""
    return tuple(((items or {}).get(item) or {}).get('delivers') or ())


def feature_delivery(items, item):
    """True when ``item`` leads a Feature delivery's slice (:mod:`asf.record.slice`,
    ``conventions.delivery: feature``): a Task card carrying ``delivers:``."""
    card = (items or {}).get(item) or {}
    return card.get('type') == 'task' and bool(card.get('delivers'))


def item_footprint(items, item):
    """The globs the branch of ``item`` may touch: its card's ``writes:``, plus every member's
    when it leads a delivery (its ``delivers:``, and every card naming it ``delivered_by:``) —
    the union the foreign-red rule measures a delivery against."""
    out = []
    by = [iid for iid, c in sorted((items or {}).items())
          if (c or {}).get('delivered_by') == item and not c.get('removed')]
    for i in dict.fromkeys((item, *delivery_members(items, item), *by)):
        out.extend(g for g in ((items or {}).get(i) or {}).get('writes') or () if g not in out)
    return out


def delivery_note(repo, trunk, branch, members):
    """``: delivered <ids> — no commit for <ids>`` for a delivery branch — the ids some commit
    named, and those none did — else ``''``. Read while ``origin/<branch>`` still exists
    (PD6): the caller reads it before the branch is deleted."""
    if not members:
        return ''
    named, missing = members_named(repo, trunk, branch, members)
    note = f': delivered {", ".join(named) or "nothing"}'
    return note + (f' — no commit for {", ".join(missing)}' if missing else '')


def lane_refusal(repo, trunk, branch, item, conv=None, members=()):
    """``(kind, text)`` for a branch the lane refuses before any gate, or None: a merge commit
    on it (B-0056), a commit not naming the item or one of a delivery's ``members``, or a line
    it adds to customer content that carries a forbidden marker
    (:func:`asf.customer_content.refusal`, ``file:line`` each) — each a correction back to its
    session."""
    merges = merge_commits(repo, trunk, branch)
    if merges:
        return 'merge', (f'merge commit on a lane branch: {merges[0]} — a lane branch is straight '
                         f'commits on origin/{trunk}: rebase onto it, never merge origin/{branch} '
                         f'or origin/{trunk} into it; the factory publishes the rebased branch')
    ids = ([item] if item else []) + [m for m in members if m != item]
    if not item or not commits_name_items(repo, trunk, branch, ids):
        names = f'{item} or one of {", ".join(members)}' if members else item or 'an item id'
        return lifecycle.NAMING, (
            f'commits do not name {names}: every commit subject on the branch '
            f'names its item — the lane could not reword them: reword them; the factory '
            f'publishes the rewritten branch')
    if conv is not None:
        return customer_content.refusal(repo, trunk, branch, conv)
    return None


def report_status(run):
    """The ``status:`` word of ``run``'s typed REPORT (``done`` | ``partial`` | ``blocked``), or
    ``''`` when the run left none — a crash, a run cap, a log that is gone, no run at all."""
    if not run:
        return ''
    from asf.workers import report as report_mod
    result = lifecycle.result_of(run) or {}
    fields = report_mod.parse(result.get('result') if isinstance(result, dict) else '')
    return (fields.get('status') or '').strip().lower().split(' ')[0]


def incomplete_refusal(repo, trunk, branch, members, run):
    """``(kind, text)`` for a delivery branch that is not whole, or None: a member of
    ``members`` no commit on ``origin/<branch>`` names, while the run's report does not say
    ``done`` (``status: partial``, a crash, a run cap — no report at all). A ``done`` report
    with members left out is F-0102 D11: the branch lands without them. No run to read (an
    adopted branch) is no refusal either: the lane cannot know the session meant to go on.
    The hold's text is the session's instruction: the branch's head is the state, a Task whose
    commit is on it is done, the rest follow in order. No PR opens while it holds."""
    if not members or run is None:
        return None
    named, missing = members_named(repo, trunk, branch, members)
    if not missing:
        return None
    status = report_status(run)
    if status == 'done':
        return None
    said = f'the report says {status}' if status else 'the run ended without a report'
    return lifecycle.INCOMPLETE, (
        f"delivery incomplete: no commit on origin/{branch} names {', '.join(missing)} "
        f"(done: {', '.join(named) or 'none'}; {said}). No PR opens for a partial delivery. "
        f"Continue from the head of origin/{branch}: a Task whose commit is on the branch is "
        f"done — do not redo it; build {missing[0]} next, then the rest in order, one commit "
        f"per Task with its id in the subject, and push the same branch.")


def deliverable_of(conv, branch, item):
    kind = conv.branch_kind(branch)
    if kind in ('spec', 'plan') and item:
        return f'{conv.doc_dir(kind)}/{item.lower()}.md'
    return None


def already_on_trunk(repo, trunk, branch, conv, item, files=None):
    """``(landed, extras)`` — B-0057: every file the branch touched is identical on the trunk; or,
    for a spec/plan branch, its one deliverable is (``extras`` names what else it carried).
    ``files``: :func:`touched_files` of the branch when the caller has already read it."""
    if files is None:
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


def item_on_trunk(repo, trunk, item):
    """The first commit on ``origin/<trunk>`` whose subject names ``item`` as a token, or ''."""
    if not item:
        return ''
    r = H.sh(['git', 'log', '--no-merges', '-F', '-i', f'--grep={item}', '--format=%H %s',
              f'origin/{trunk}'], cwd=repo)
    for line in (r.stdout.splitlines() if r.returncode == 0 else []):
        sha, _, subject = line.partition(' ')
        if githooks.names_item(subject, item):
            return sha
    return ''


def empty_branch_landed(repo, trunk, branch, item, pr):
    """``(landed, why)`` for a branch with nothing past the trunk. Landed only when it carries no
    own commit (``--no-merges origin/<trunk>..origin/<branch>`` empty) and its work is on the
    trunk: its PR merged, or a commit naming ``item`` reachable from the trunk with no PR still
    open. A branch reset to the trunk tip — a repair's reset, seconds before its work is pushed
    again — matches neither: it waits, never deleted, its record not advanced."""
    own = H.sh(['git', 'rev-list', '--no-merges', f'origin/{trunk}..origin/{branch}'], cwd=repo)
    if own.returncode != 0 or own.stdout.split():
        return False, f'its own commits past origin/{trunk} unreadable or present'
    pr = pr or {}
    if pr.get('state') == 'MERGED':
        return True, ''
    if pr.get('state') == 'OPEN':
        return False, f"its PR #{pr.get('number')} is still open"
    if not item_on_trunk(repo, trunk, item):
        return False, f'no commit naming {item or "its item"} is on origin/{trunk}'
    return True, ''


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


def api_ref(slug, refspec, lease=None, main=None, protected=None):
    """Make one ref-only ``refspec`` on hosted ``slug`` through the host's API; ``(ok, why)``.
    ``<sha>:refs/heads/<b>`` creates the ref at ``sha`` (one already there at ``sha`` is done);
    ``:refs/heads/<b>`` deletes it — under ``lease`` (``refs/heads/<b>:<sha>``) only while its
    tip is still ``sha``, as retention's hosted delete does. The trunk and a protected ref are
    refused before any call (:mod:`asf.refguard`)."""
    guard = refguard.refusal(refspec, 'api ref write', main, protected)
    if guard:
        return False, guard
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


def _amend_facts(fields, lane, f, branch, touched=()):
    """T-0056: on the correction of an item whose ``writes:`` reach the amendable set, the two
    facts the feeder routes it by (:func:`asf.feeder.rows.correction_amend`) — ``amend_missing``,
    the amendable ``writes:`` the branch's diff does not carry yet (empty: the console's edit is
    on the branch, so the correction is a worker's), and ``behind``, the commits the branch is
    behind the trunk. Nothing without the pass (``lane``/``f``) or an amendable ``writes:``;
    an unreadable count is left out."""
    corr = (fields or {}).get('correction')
    if lane is None or f is None or not isinstance(corr, dict):
        return
    product = getattr(lane, 'product', None)
    card = (getattr(lane, 'items', None) or {}).get(f.get('item') or '') or {}
    writes = list(card.get('writes') or ())
    if product is None or not writes:
        return
    from asf import amendable
    inside, _outside = amendable.partition(product, writes)
    if not inside:
        return
    import fnmatch
    diff = list(f.get('files') or touched or ())
    corr['amend_missing'] = [w for w in inside
                             if not any(d == w or fnmatch.fnmatch(d, w) for d in diff)]
    from asf import gitops
    trunk = getattr(lane, 'trunk', None) or 'main'
    behind = gitops.rev_list_count(getattr(lane, 'repo', None), f'origin/{branch}',
                                   f'origin/{trunk}')
    if behind is not None:
        corr['behind'] = behind


def hold_with_correction(state_dir, branch, record, kind, text, out, files=(), item_writes=(),
                         touched=(), conv=None, own=False, read=None, head=None, finding=None,
                         main=None, lane=None, f=None):
    """Hold ``branch`` and hand it back to its session (:func:`asf.workers.lifecycle.hold`).
    ``'held'`` — or ``'foreign'`` for a red naming only files outside its footprint (no round),
    or ``'timed-out'`` for a gate that ran out of time (no round). ``own``: the red is this
    branch's whatever files it names (gated on a trunk green alone). ``finding``: the hold's
    finding when the caller knows it — a review's C-list files (:func:`lifecycle.finding_of`).
    ``lane``/``f``: the pass and the branch's facts — with them, under ``flags.mechanical: on``,
    a cause in :data:`asf.harvest.mechanical.MECHANICAL` is tried by code first
    (:func:`asf.harvest.mechanical.apply`): ``'mechanical'`` when it resolved it (no hold), else
    the hold's text carries what the lane tried."""
    job = record.get('job') or branch
    if kind == 'gate' and text.startswith(H.TIMED_OUT):  # B-0082: a clock is not a defect
        out(f'{H.TIMED_OUT} {branch}: {text} — retried next tick')
        return 'timed-out'
    if kind == 'gate' and touched and landing_class(conv or H.DEFAULTS, touched) == DOCS \
            and not own and not footprint.overlaps(touched, files):
        out(f'foreign {branch}: gate red, its diff is docs only — re-gated next tick')
        return 'foreign'
    item_writes = widen.norm_writes(item_writes)
    if item_writes:  # the append-only files every Task may touch are inside every footprint
        item_writes += [w for w in footprint.shared_writes(conv) if w not in item_writes]
    reach, what = (item_writes, 'writes') if item_writes else (touched, 'diff')
    needs, tests, exercised = (widen_candidates(files, item_writes, touched, read, own)
                               if kind == 'gate' and files else ([], [], []))
    if kind == 'gate' and not own and not exercised and files and reach \
            and footprint.overlaps(reach, files) is None:
        out(f'foreign {branch}: gate red outside its {what}: {files[0]} — re-gated next tick')
        return 'foreign'
    cause = lifecycle.FOOTPRINT if needs else kind
    if lane is not None and f is not None \
            and mechanical.handles(getattr(lane, 'product', None), cause):
        got = mechanical.apply(lane, f, {'kind': cause, 'text': text, 'files': list(files or ()),
                                         'needs': needs, 'tests': tests})
        if got is not None and got.resolved:
            return 'mechanical'
        if got is not None and got.why:
            text = f'{text}\n{got.why}'
    if needs:
        fact = f'gate: {", ".join(tests) or files[0]} red alone, trunk green'
        fields, line = lifecycle.footprint_hold(
            dict(record, branch=branch, job=job), needs, fact,
            f'{text}\nfootprint: the red is outside writes: — needs {" ".join(needs)}',
            now_iso(), tests=tests)
        _amend_facts(fields, lane, f, branch, touched)
        H.mark_session(state_dir, job, **fields, branch=branch)
        out(line)
        return 'held'
    fields, line = lifecycle.hold(H.sessions_path(state_dir), dict(record, branch=branch, job=job),
                                  kind, text, now_iso(), head=head, finding=finding, main=main)
    _amend_facts(fields, lane, f, branch, touched)
    H.mark_session(state_dir, job, **fields, branch=branch)
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


def reads_red(state, ended, correction):
    """True when a branch's exact-head checks are read this pass (T5c/T5d): a red head goes back
    to a session before any review round. Read in PR_OPEN/REVIEW, and in PUSHED/BACK and a new
    run's first pass once it ended (no state yet) — one pass carries (none)/BACK → PUSHED →
    PR_OPEN → REVIEW on these same facts, and a product's T-0042 got two more review rounds on a
    head with two failed required checks because they were only read in PR_OPEN/REVIEW. Not
    while another correction than a review one is pending (that one goes first)."""
    if correction and (correction or {}).get('kind') != 'review':
        return False
    if state is None:
        return bool(ended)
    return state in (PUSHED, BACK, PR_OPEN, REVIEW)


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
    # a PR no factory item made is its own item: that PR merged or closed ends it, whatever
    # the branch head did after (a file pushed to the dead branch is no new work to review)
    own_pr = bool(f.get('foreign') and n and n == rec.get('pr'))
    # T11 — merged by the host; ours when MERGING/QUEUED came first (R3)
    if pr.get('state') == 'MERGED' and s != MERGED and (
            not head or pr.get('head') in (None, '', head) or s in (MERGING, QUEUED) or own_pr):
        if s in (MERGING, QUEUED):
            return MERGED, 'method=' + ('queue' if s == QUEUED else rec.get('method') or 'squash')
        return MERGED, 'method=external'
    if s == MERGED:
        return keep
    if f.get('live'):
        return keep
    if s == MERGING and f.get('merging_landed'):  # R8: the push reached the trunk, the line did not
        return MERGED, 'method=' + (rec.get('method') or 'ff')
    if f.get('empty'):  # nothing past the trunk and not landed: it waits, its record unmoved
        return keep
    if s is None and f.get('orphan'):  # a run-less branch the lane closes (orphan_facts)
        return STALE, f['orphan']
    closed = f.get('closed')
    if s == STALE:
        if not closed and pr.get('state') == 'OPEN' and head:
            return PR_OPEN, f'PR #{n} reopened'  # R6
        return keep
    if s == PARKED:  # Tp'
        if pr.get('draft'):
            return keep
        return (PR_OPEN, f'PR #{n} ready for review') if head else keep
    if f.get('on_trunk') and (s is not None or f.get('ended')):
        return MERGED, 'method=on-trunk'
    if closed and (s is not None or f.get('ended')):
        return STALE, f'{f.get("item")} is {closed} in the record'
    if not head:
        if f.get('gone_merged'):
            return MERGED, 'method=on-trunk'
        return (None, '') if s is None else (STALE, 'branch gone')
    if pr.get('state') == 'CLOSED' and (pr.get('head') in (None, '', head) or own_pr) \
            and (s is not None or f.get('ended')):
        return STALE, f'PR #{n} closed unmerged'
    if pr.get('draft') and s not in (MERGING, QUEUED):  # Tp: parked by its owner, whatever it was
        return PARKED, (f'PR #{n} is a draft — parked by its owner' if n
                        else 'draft — parked by its owner')
    # R4 — a moved head: whatever reviewed or gated the old one is history
    if s in OPEN_STATES and rec.get('head') and head != rec['head']:
        return PUSHED, f"head moved {rec['head'][:7]} → {head[:7]}"
    corr = f.get('correction')
    if s is None or s == BACK:
        if corr and s == BACK and corr.get('kind') == 'review' and f.get('checks_red'):
            return BACK, 'kind=gate'   # T5d: a red head's failures go before its review round
        if corr:
            return keep if s == BACK else (BACK, f"kind={corr.get('kind') or 'correction'}")
        if s == BACK:
            return PUSHED, 'correction answered'
        if not f.get('ended') or f.get('landed') or not f.get('ahead'):
            return None, ''
        return PUSHED, 'adopted' if f.get('adopt') else 'finished'
    if correction_turns_back(rec, corr):  # T9c: a correction written on a landing wait
        return BACK, f"kind={corr.get('kind') or 'correction'}"
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
        if f.get('conflict'):  # T2c: GitHub runs no pull_request workflow on a conflicting PR
            return BACK, 'kind=conflict'
        if pr.get('state') == 'OPEN':
            return PR_OPEN, f'PR #{n}'
        return PR_OPEN, 'open a PR'
    if s == PR_OPEN and f.get('mode') == 'pr' and not f.get('host'):
        return keep
    if s in (PR_OPEN, REVIEW) and f.get('checks_red'):  # T5c: its exact head failed CI
        return BACK, 'kind=gate'
    if s in (PR_OPEN, REVIEW) and f.get('mode') == 'pr' and f.get('conflict'):
        return BACK, 'kind=conflict'  # T5e: no run will come, no review round is spent on it
    if s in (PR_OPEN, REVIEW):  # T3/T4/T5: a review of the head already there counts at once
        rv = f.get('review') or {}
        if not f.get('review_required'):
            return GATE, f"review: none ({f['review_waived']})" if f.get('review_waived') \
                else 'review: none'
        if rv.get('current') and rv.get('verdict') == review_mod.APPROVED \
                and not incomplete(f):
            return GATE, f"{rv.get('path')} approved its head"
        if rv.get('current') and rv.get('verdict') == review_mod.CHANGES:
            if f.get('overruled'):  # T5a: the ruling on this head already answered the review
                return GATE, f"{rv.get('path')} overruled by {f['overruled']}'s ruling"
            if f.get('ruled'):  # T5a': it only re-raises ruled points — no BACK, no round
                return GATE, (f"{rv.get('path')} only re-raises points ruled by "
                              f"{', '.join(f['ruled'])}")
            if rv.get('asks_nothing') and f.get('nothing_to_correct'):
                # T5n: the reviewer approved and asked for nothing, a correction on this head
                # found nothing to correct — land it, never park it (T-0652, 2026-10-05)
                return GATE, (f"{rv.get('path')} approved with an empty C list: nothing to "
                              f"correct ({f['nothing_to_correct']})")
            if f.get('review_answered'):  # T5b: answered without a commit — review it again
                reason = (f"round {int(rv.get('round') or 0) + 1} wanted: {rv.get('path')} was "
                          f"answered by {f['review_answered']} without a commit")
                return (REVIEW, reason) if s != REVIEW or reason != rec.get('reason') else keep
            return BACK, 'kind=review'
        rnd, why = review_reason(f)
        reason = f'round {rnd} wanted: {why}'
        return (REVIEW, reason) if s != REVIEW or reason != rec.get('reason') else keep
    if s == MERGING:
        return keep if f.get('harvest_running') else (GATE, 'no merge seen after MERGING: gated again')
    if s == QUEUED:
        if rec.get('batch'):  # T10b: the lane's own queue (asf.merge_queue) moves it
            return keep
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
        #: ``{branch: pr}`` the last facts pass read on the host (None: not read this pass) — the
        #: merge queue reads a member's draft flag here
        self.pr_map = None
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

    def full_sha(self, sha):
        """``sha`` (a prefix the repo knows) as its full 40 hex, else as given."""
        from asf import gitops
        return gitops.rev_parse(self.repo, f'{sha}^{{commit}}') or sha

    @staticmethod
    def run_heads(run):
        """The heads a finished run's facts name for its branch, gone from origin: the sha its
        REPORT's ``pushed:`` line names, and its worktree's HEAD while the worktree stands."""
        from asf import gitops
        from asf.workers import report as report_mod
        out = []
        rec = lifecycle.result_of(run) or {}
        text = rec.get('result') if isinstance(rec, dict) else ''
        m = lifecycle.PUSHED_SHA_RE.search(report_mod.parse(text or '').get('pushed') or '')
        if m:
            out.append(m.group(0).lower())
        wt = (run or {}).get('worktree')
        if wt and os.path.isdir(wt):
            head = gitops.rev_parse(wt, 'HEAD')
            if head:
                out.append(head)
        return out

    def gather(self, prs=True):
        """``{branch: facts}`` for every lane branch; ``prs``: read the host's PR list (the
        in-process pass), else take the PR number off the lane record (the gate pass)."""
        conv, trunk = self.conv, self.trunk
        runs = lifecycle.by_branch(self.path)
        heads = self.remote_heads()
        if merge_queued(self):  # the queue's batch refs are its own, never a lane branch
            from asf import merge_queue
            prefix = merge_queue.settings(conv)['ref_prefix']
            heads = {b: s for b, s in heads.items() if not b.startswith(prefix)}
        self.trunk_sha = heads.get(trunk) or H.sh(['git', 'rev-parse', f'origin/{trunk}'],
                                                 cwd=self.repo).stdout.strip()
        pr_map = self.host.prs() if prs else {}
        self.pr_map = pr_map if prs else None
        self.orphans = self.orphan_claims(runs, heads, pr_map) if prs else {}
        self.orphans_taken = 0
        running = H.try_lock_held(self.state_dir)
        names = {b for b in heads if conv.branch_kind(b) and b != trunk}
        names |= {b for b, r in runs.items() if b != trunk and b and (
            b in heads or lifecycle.eligible(r) or lifecycle.lane_of(r).get('state') in OPEN_STATES)}
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

    def land_requested(self, pr, rec):
        """True when the branch's PR (or its lane record's) is an ``asf land`` request: the
        merge queue's to land as it is, never adopted as factory work. Read once a pass."""
        if getattr(self, '_land_prs', None) is None:
            from asf import merge_queue   # local: the merge queue imports the lane
            self._land_prs = {str(r.get('pr')) for r in
                              merge_queue.load_requests(self.state_dir).values()}
        n = (pr or {}).get('number') or (rec or {}).get('pr')
        return bool(n) and str(n) in self._land_prs

    def branch_facts(self, b, run, head, pr, prs, running):
        conv, trunk, repo = self.conv, self.trunk, self.repo
        rec = lifecycle.lane_of(run)
        item = item_of(b, run)
        if (run is None or is_pr_item(item)) and self.land_requested(pr, rec):
            # an `asf land` request is never factory work: no adoption, no review or correct
            # round, no push — and an adoption from before this rule is closed (STALE)
            if run is not None and rec.get('state') not in TERMINAL_STATES and not self.dry_run:
                f = {'branch': b, 'run': run, 'item': item, 'head': head, 'prev': rec,
                     'pr': pr}
                self.write(f, self.record(f, STALE, 'an asf land request: the merge queue '
                                                    'lands it, never factory work'))
                self.out(f'lane: {b} {rec.get("state") or "-"} → {STALE} (an asf land '
                         f'request — the merge queue lands it, never factory work)')
            return None
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
            # a branch gone from origin is MERGED on-trunk only on a head the trunk holds: the
            # record's, else the sha the run's REPORT says it pushed. Never on its word alone (a
            # product's lane wrote head-less on-trunk records for many items on 2026-09-28)
            heads = [rec['head']] if rec.get('head') else (
                self.run_heads(run) if not rec and lifecycle.eligible(run) else [])
            gone = next((h for h in heads if self.is_ancestor(h, f'origin/{trunk}')), None)
            if gone:
                f['gone_merged'], f['trunk_head'] = True, self.full_sha(gone)
            return f
        ahead = H.sh(['git', 'rev-list', '--count', f'origin/{trunk}..origin/{b}'],
                     cwd=repo).stdout.strip()
        f['ahead'] = int(ahead) if ahead.isdigit() else 0
        # PD6: read now, while origin/<trunk>..origin/<b> is still the branch's own diff — once
        # it lands, the trunk catches up to it and the same read would find nothing
        f['delivery_note'] = delivery_note(repo, trunk, b, delivery_members(self.items, item))
        if f['ahead'] == 0:
            landed, why = empty_branch_landed(repo, trunk, b, item, pr)
            if landed:
                f['on_trunk'] = lifecycle.finished(run) or bool(rec)
            else:
                f['empty'] = why
            return f
        files = touched_files(repo, trunk, b)  # read once: nothing fetches before its 2nd use
        done, extras = already_on_trunk(repo, trunk, b, conv, item, files=files)
        f['on_trunk'], f['extras'] = done, extras
        if done:
            return f
        f['closed'] = superseded_by(self.items, item)
        f['files'] = files
        f['class'] = landing_class(self.product, f['files'])
        # a PR no factory item made merges only on a factory review of its head, whatever the
        # class; and its commits name no item, so the lane's naming refusal is not its to answer
        f['review_required'] = f['foreign'] or conv.review_required(f['class'])
        if f['review_required'] and not f['foreign'] and f['class'] == CODE:
            lines = (changed_lines(repo, trunk, b) if f['kind'] != DIRECT
                     and small_task(self.items, item) else None)
            f['review_waived'] = review_waived(conv, f['kind'], self.items, item, lines)
            f['review_required'] = not f['review_waived']
            if f['review_required'] and docs_only_task(self.product, self.items, item, files):
                f['review_waived'] = 'docs-only writes: flags.docs_review skips the review'
                f['review_required'] = False
        if self.mode == 'pr' and self.slug and not f['foreign'] \
                and rec.get('state') in (None, PUSHED, BACK, PR_OPEN, REVIEW):
            # T2c/T5e: GitHub runs no pull_request workflow on a PR that conflicts with the
            # trunk — no PR is opened on it, and an open one goes back before any review
            f['conflict'] = conflict_files(repo, trunk, b, fetch=False)
        if rec.get('state') in (None, PUSHED, BACK) and not f['foreign']:
            members = delivery_members(self.items, item)
            f['refusal'] = lane_refusal(repo, trunk, b, item, conv, members=members)
            if not f['refusal'] and feature_delivery(self.items, item):
                # a Feature delivery not whole: no PR, back to its session (T9i). An F-0102
                # cross-item delivery keeps its D11: the branch lands without a member. A member
                # the delivery leaves out (the console's, one a cycle defers) is never built here
                from asf.feeder import rows as feeder_rows  # local: the feeder imports the briefs
                skip = set().union(*feeder_rows.left_out(self.product, self.items, item))
                built = tuple(m for m in members if m not in skip)
                f['refusal'] = incomplete_refusal(repo, trunk, b, built, run)
                if f['refusal']:  # the hold's finding: the members still missing (progress is a new finding)
                    f['incomplete'] = members_named(repo, trunk, b, built)[1]
        f['customer'] = customer_content.touched(conv, f['files'])
        # a customer page is never landed unread: its diff needs a review whatever its class
        f['review_required'] = f['review_required'] or bool(f['customer'])
        # a CODE branch whose review is waived (or not required) is still read: a review of its
        # exact head that says `changes` is never walked past to GATE as `review: none`
        # (2026-10-04, T-0571 #1058 — STALE → PR_OPEN → GATE over a changes verdict)
        if (f['review_required'] or f['class'] == CODE) \
                and rec.get('state') in (None, PUSHED, BACK, PR_OPEN, REVIEW):
            rv = review_mod.review_at(repo, conv, f'origin/{b}', item,
                                       reviews.required(review_kind(f['kind'])),
                                       store=os.path.join(self.state_dir, review_store.DIRNAME))
            body = ''
            if rv:
                rv['current'] = review_mod.is_current(repo, conv, f'origin/{b}', rv, head,
                                                       trunk=f'origin/{trunk}')
                body = rv.pop('body', '')
                rv['customer_row'] = customer_content.has_customer_row(body)
            if not f['review_required'] and rv and rv.get('current') \
                    and rv.get('verdict') == review_mod.CHANGES:
                f['review_unwaived'] = (f"{rv.get('path')} says changes on this head"
                                        + (f" (waiver: {f['review_waived']})"
                                           if f.get('review_waived') else ''))
                f['review_required'] = True
            if f['review_required']:
                f['review'] = rv
            if rv and rv.get('current') and rv.get('verdict') == review_mod.CHANGES:
                f['review_answered'] = lifecycle.review_answered(self.path, item, rv.get('path'),
                                                                 head)
                rv['asks_nothing'] = review_mod.asks_nothing(body)
                if rv['asks_nothing']:
                    f['nothing_to_correct'] = lifecycle.corrected_on(self.path, item, head)
                # the review's C list an adjudicate ruling already answered on the code this
                # head carries
                f['overruled'] = lifecycle.overruling(
                    self.path, item, head,
                    lambda sha: review_mod.same_code(repo, conv, sha, f'origin/{b}',
                                                      trunk=f'origin/{trunk}'))
                # T5a': a C list that only re-raises points a standing ruling settled
                if not f['overruled']:
                    card = (self.items or {}).get(item or '') or {'id': item}
                    f['ruled'] = rulings_mod.reraised_only(
                        body, rulings_mod.standing(self.product, card))
        mf = rec.get('mechanical_from')
        rv = f.get('review') or {}
        if f.get('review_required') and mf and rec.get('head') == head and rv \
                and not rv.get('current') and rv.get('verdict') == review_mod.APPROVED \
                and mechanical.enabled(self.product) \
                and review_mod.is_current(repo, conv, mf, rv, mf, trunk=f'origin/{trunk}'):
            # flags.mechanical: the approval of the head the table's own move left (a clean
            # rebuild on the trunk, no session) is carried — no review round for code no session
            # touched; the gate runs on this head
            f['review'] = dict(rv, current=True, carried=mf)
        tp = rec.get('transplant') or {}
        if f['review_required'] and tp.get('carried') and tp.get('head') == head \
                and rec.get('state') in (PUSHED, PR_OPEN, REVIEW):
            # T13t: the approval a transplant carried, bound to this head's content — unless a
            # newer review of the head reads otherwise
            rv = f.get('review') or {}
            if not rv.get('current') or int(rv.get('round') or 0) <= int(tp.get('round') or 0):
                f['review'] = transplant_mod.carried_review(tp)
        if prs and not f['foreign'] and (pr or {}).get('state') == 'OPEN' and pr.get('number') \
                and reads_red(rec.get('state'), f.get('ended'), f.get('correction')):
            f['checks_red'] = self.host.head_red(f, pr['number'])
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
            landed, why = empty_branch_landed(self.repo, self.trunk, b, item, pr)
            if landed:
                f['on_trunk'] = True
            else:
                f['empty'] = why
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
        from asf import gitops  # the exact ref: ``archive/<branch>`` is not ``<branch>``
        r = H.sh(['git', 'ls-remote', '--heads', 'origin', gitops.head_ref(branch)],
                 cwd=self.repo)
        return r.returncode == 0 and not gitops.head_sha(r.stdout, branch)

    def guarded(self, target, what):
        """The :mod:`asf.refguard` line refusing a factory write of ``what`` to ``target`` (the
        trunk or a ``conventions.protected_refs`` ref), said loudly, else ''."""
        return refguard.refusal(target, what, self.trunk, refguard.listed(self.conv),
                                out=self.out)

    def fresh_trunk(self):
        """Fetch ``origin/<trunk>`` now: a rewrite reads the branch's own commits against it,
        and a tracking ref older than the trunk the branch was rebased onto counts trunk commits
        as the branch's (2026-09-26: trunk commits reworded onto a factory branch)."""
        H.sh(['git', 'fetch', '-q', 'origin',
              f'+refs/heads/{self.trunk}:refs/remotes/origin/{self.trunk}'], cwd=self.repo)

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
        guard = self.guarded(refspec, what)
        slug = None if guard else self.ref_host()
        wt = None if guard or slug else self.ref_checkout()
        if guard:
            ok, why = False, guard
        elif slug:
            started = time.monotonic()
            ok, why = api_ref(slug, refspec, lease, self.trunk, refguard.listed(self.conv))
            kind, _, branch = what.partition(' ')
            self.out(f'lane: push {branch or kind} {time.monotonic() - started:.1f}s '
                     f'({kind}, api)')
        elif wt:
            # a ref carries no new code: the product's pre-push hook is skipped, and the push is
            # bounded in time (2026-09-26: an archive push sat 8+ min in a product's hook)
            args = ['-q'] + ([f'--force-with-lease={lease}'] if lease else [])
            started = time.monotonic()
            r = gitpush.push(args + ['origin', refspec], wt, refs_only=True,
                             timeout=gitpush.push_timeout(self.conv), log=self.out, guard=refguard.guard_from(self.trunk, self.conv))
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
                              queue=self.ci_queue, sha=f.get('head'),
                              draft=bool((f.get('pr') or {}).get('draft'))).admitted

    def ci_forget(self, f):
        """A draft PR's branch leaves the CI start queue: parked by its owner, it never starts
        and never holds the runners of the starts behind it."""
        from asf import ci_queue
        if self.ci_queue is None:
            self.ci_queue = ci_queue.Queue(self.product, out=self.out)
        ci_queue.forget(self.product, f['branch'], queue=self.ci_queue)

    def ci_drop_pr(self, f):
        """A PR opened past the queue's hold: its ``pr:`` entry leaves the line, so the queue's
        pass never opens it again nor re-runs the run its opening started."""
        from asf import ci_queue
        if self.ci_queue is None:
            self.ci_queue = ci_queue.Queue(self.product, out=self.out)
        if getattr(self.ci_queue, 'mode', 'off') != 'off':
            self.ci_queue.drop([f"pr:{f['branch']}"])

    # ---- writing ------------------------------------------------------------------------

    def record(self, f, state, reason, **extra):
        pr = (f.get('pr') or {}).get('number') or (f.get('prev') or {}).get('pr')
        now = now_iso()
        rec = {'state': state, 'head': f.get('head'), 'pr': pr, 'at': now,
               'head_at': head_since(f.get('prev'), f.get('head'), now),
               'reason': reason, 'item': f.get('item')}
        prev = f.get('prev') or {}
        if prev.get('heavy') and prev['heavy'] == f.get('head') and state not in HEAVY_DROPS:
            # ci.heavy_after_review: the head the lane approved for heavy CI stays approved
            # through its gate's waits, and only there (GitHubHost.heavy_gate)
            rec.update(heavy=prev['heavy'], heavy_at=prev.get('heavy_at'))
            if prev.get('heavy_kick') == prev['heavy']:
                rec['heavy_kick'] = prev['heavy_kick']
        tp = prev.get('transplant') or {}
        if tp and tp.get('head') == f.get('head') and state not in TERMINAL_STATES:
            # T13t: the approval a transplant carried stays bound to the head it was made for
            rec['transplant'] = tp
        if prev.get('mechanical_from') and prev.get('head') == f.get('head') \
                and state not in TERMINAL_STATES:
            # flags.mechanical: the head the table's own move left, while the head it made stands
            rec['mechanical_from'] = prev['mechanical_from']
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
        H.mark_session(self.state_dir, run.get('job') or f['branch'], lane=rec,
                       **dict(fields, branch=f['branch']))

    def set(self, f, state, reason, result=None, **extra):
        """Record one transition of ``f``'s branch; ``result`` goes in :attr:`results`."""
        rec = self.record(f, state, reason, **extra)
        prev = f.get('prev') or {}
        was = prev.get('state')
        if self.dry_run:
            self.out(f"lane: DRY {f['branch']} {was or '-'} → {state} ({reason})")
        else:
            self.write(f, rec)
            self.out(f"lane: {f['branch']} {was or '-'} → {state} ({reason})")
            if prev.get('heavy') and state in HEAVY_DROPS and state not in TERMINAL_STATES:
                # back to a session, or a new head: the approval for heavy CI is history, and
                # the label comes off before the correction's push can run the heavy jobs
                self.host.unmark_heavy(rec.get('pr'), f['branch'])
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
        if (f.get('pr') or {}).get('draft'):
            self.ci_forget(f)       # parked by its owner: never in the CI start queue's line
        if f.get('empty') and next_state(f.get('prev'), f) == (prev.get('state'),
                                                                  prev.get('reason', '')):
            if prev or f.get('ended'):  # a live session's fresh branch says nothing yet
                self.out(f"empty branch — waits: {f['branch']}: {f['empty']}")
                self.results[f['branch']] = 'waiting'
            return None  # never deleted, never landed: an owed delete waits too
        if prev.get('delete') == DELETE_OWED and not self.dry_run and self.repo \
                and f.get('head') and f['head'] == prev.get('head'):
            self.push_or_defer('delete', f)
        # trunk history under a factory branch: rebuilt on the trunk here, before any refusal
        # sends it back to a session that may not force-push
        if self.repo:
            moved = self.drop_copies(f) or moved
            prev = f.get('prev') or {}
        # the lane writes the trailers, not the session: once per new head
        if self.repo:
            self.normalise_commits(f)
        # a branch already held for naming (a session pending, none running): reworded here
        if prev.get('state') == BACK and not f.get('live') \
                and (f.get('correction') or {}).get('kind') == lifecycle.NAMING:
            rec = self.repair_naming(f)
            moved = rec if rec not in (None, DEFERRED) else moved
        # flags.mechanical: a pending correction a rebuild on the moved trunk may settle (a copies
        # hold, a conflict — at_cap ones headed for adjudication too): the table tries it first
        if self.repo:
            got = mechanical.retry_pending(self, f)
            if got is not None and got.resolved:
                moved = f.get('prev') or moved
        # approved content held only by git mechanics: the lane transplants it onto the trunk
        if self.repo:
            moved = self.transplant(f) or moved
        for _ in range(MAX_STEPS):
            prev = f.get('prev')
            state, reason = next_state(prev, f)
            if state == BACK and reason == f'kind={lifecycle.NAMING}':
                rec = self.repair_naming(f)
                if rec == DEFERRED:
                    if (f.get('refusal') or (None,))[0] == lifecycle.NAMING:
                        break   # waits for a later pass: no session, no hold
                    continue    # the subjects name the item now: read the branch on
                if rec is not None:
                    moved = rec
                    continue
            if state is None or (prev and state == prev.get('state')
                                 and reason == prev.get('reason')):
                break
            if prev and state == prev.get('state') and state != REVIEW \
                    and (state, reason) != (BACK, 'kind=gate'):
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
            # The queue is asked so an admitted start is counted as started; its hold never
            # keeps the PR from opening. 2026-10-04, a product: five pushed branches held here
            # ("heavy 0 free, needs 5", runners set aside for a priority batch) sat PUSHED with
            # no PR — no review, no run, "awaiting harvest" — until a human opened them by hand.
            # The PR is what review and the gate wait on; runner order is the host's queue's.
            held = not self.ci_admits(f, 'pr')
            self.opened += 1
            number, why = self.host.open(b, item)
            if not number:
                self.out(f'prs: {b} not opened — {why}')
                if prev.get('state') != PUSHED or prev.get('reason') != f'PR not opened: {why}':
                    self.set(f, PUSHED, f'PR not opened: {why}')
                return None
            f['pr'] = {'number': number, 'state': 'OPEN', 'head': f.get('head')}
            if held:
                self.ci_drop_pr(f)  # the run is on the host now: no "would start" for it
                self.out(f'prs: opened PR #{number} for {b} (the ci queue held its start — '
                         f'the PR opens anyway; its run waits for runners on the host)')
            else:
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
        # read in branch_facts (PD6): the trunk may have caught up to the branch by now
        dnote = f.get('delivery_note') or ''
        if pr.get('state') == 'MERGED':
            sha = pr.get('merge_sha') or f"PR #{pr.get('number')}"
            line = f"landed {b} → PR #{pr.get('number')} {sha} (merged){dnote}"
        elif prev.get('state') == MERGING and prev.get('sha') and f.get('merging_landed'):
            sha = prev['sha']
            line = f'landed {b} → {sha} (the push reached {self.trunk}){dnote}'
        else:
            sha = self.trunk_sha
            extras = f.get('extras') or []
            note = f'; not its deliverable, dropped with the branch: {", ".join(extras)}' \
                if extras else ''
            line = f'landed {b}: already on {self.trunk} at {sha[:7]}{note}{dnote}'
        if self.dry_run:
            self.out(f'DRY: would mark {b} landed — {line}')
            self.results[b] = 'dry'
            return None
        # an on-trunk record carries the head the trunk holds (the branch's, or — the branch
        # gone — the one :meth:`gather` proved an ancestor of the trunk); with none it is no
        # landing at all, and nothing is written
        trunk_head = f.get('head') or f.get('trunk_head')
        if method == 'on-trunk' and not trunk_head:
            self.out(f'not landed {b}: on-trunk with no head the trunk holds — nothing written')
            self.results[b] = 'unproven'
            return None
        rec = self.record(f, MERGED, reason, sha=sha, method=method,
                          head=None if f.get('head') else f.get('trunk_head'))
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
        """A naming refusal, repaired by the lane with no session: the branch's tip is read from
        origin, its subjects reworded (:func:`reword_branch`), every rewritten commit checked to
        carry the same tree as the one it replaces (:func:`trees_identical`), and the result
        pushed from the ref checkout over a lease on the tip read — with ``--no-verify``: the
        push carries no content origin does not have, so the product's pre-push hook has nothing
        to judge (2026-09-27: a product's redaction gate refused 437 such pushes in a day, each a
        hold). A lease race is read again and retried once. A pending naming correction is
        cleared and the branch is PUSHED at its new head — the record.

        :data:`DEFERRED` — no session, no hold, the next pass tries again — while a live session
        holds the branch, when it moved under both reads, or when the push went nowhere (the
        remote declined it, it timed out). None — the caller holds it back to its session, as a
        naming correction that spends no round, the cause in its text — only when the rewrite
        cannot be done: a branch under no factory prefix (never rewritten), a branch that is not
        its own straight line on the trunk, or a rewrite whose trees differ. One attempt a pass:
        the answer is kept on ``f``."""
        if 'naming_repair' in f:
            return f['naming_repair']
        f['naming_repair'] = res = self._repair_naming(f)
        return res

    def _repair_naming(self, f):
        b, item = f['branch'], f.get('item')
        if (f.get('refusal') or (None,))[0] != lifecycle.NAMING or not item \
                or not f.get('head') or not self.repo:
            return None
        if not f.get('kind'):
            # a branch under no factory prefix (the registry knows it, B-0067) is someone else's
            # history: the lane never rewrites it — the naming goes back to its session
            self.out(f'reword {b} (naming): under no factory prefix — the lane does not rewrite '
                     f'it, back to its session')
            return None
        if f.get('live'):
            self.out(f'reword {b}: deferred — a live session holds the branch; the lane rewords '
                     f'it once the session ends')
            return DEFERRED
        if self.dry_run:
            self.out(f'DRY: would reword the subjects on {b} (naming) — no session')
            return None
        kind = githooks.COMMIT_KIND.get(f.get('kind'), 'chore')
        if self.guarded(b, f'reword {b} (naming)'):
            return None
        self.fresh_trunk()
        for attempt in (1, 2):
            H.sh(['git', 'fetch', '-q', 'origin', f'+refs/heads/{b}:refs/remotes/origin/{b}'],
                 cwd=self.repo)
            old = H.sh(['git', 'rev-parse', '--verify', '-q', f'origin/{b}'],
                       cwd=self.repo).stdout.strip()
            if not old:
                self.out(f'reword {b}: deferred — origin/{b} could not be read')
                return DEFERRED
            why = []
            new, n = reword_branch(self.repo, self.trunk, b, item, kind, why)
            if not new:
                if not why:  # every subject names its item now: nothing left to reword
                    f['head'] = old
                    f['refusal'] = lane_refusal(self.repo, self.trunk, b, item, self.conv,
                                                members=delivery_members(self.items, item))
                    if f['refusal'] is None or f['refusal'][0] != lifecycle.NAMING:
                        self.out(f'reword {b}: every subject names {item} already at '
                                 f'{old[:9]} — nothing to push')
                        return DEFERRED
                return self._naming_back(f, why[0] if why else 'no commit to reword')
            same, differ = trees_identical(self.repo, old, new)
            if not same:
                return self._naming_back(f, f'the rewrite changed more than messages — trees '
                                            f'differ ({differ}); nothing pushed')
            wt = self.ref_checkout()
            if not wt:
                self.out(f'reword {b}: deferred — {self.ref_wt_error}')
                return DEFERRED
            r = gitpush.push(['-q', f'--force-with-lease=refs/heads/{b}:{old}', 'origin',
                              f'{new}:refs/heads/{b}'], wt, refs_only=True,
                             timeout=gitpush.push_timeout(self.conv), log=self.out, guard=refguard.guard_from(self.trunk, self.conv))
            if r.returncode == 0:
                break
            cause, text = push_cause(r)
            if cause == 'lease' and attempt == 1:
                self.out(f'reword {b}: the branch moved since {old[:9]} was read ({text}) — '
                         f'read again and retried')
                continue
            if cause == 'lease':
                self.out(f'reword {b}: deferred — the branch moved under both reads ({text}); '
                         f'a writer is pushing it, the next pass reads it again')
            else:
                self.out(f'reword {b}: deferred — the push was refused ({cause}: {text}); '
                         f'trees identical, no session — the next pass tries again')
            return DEFERRED
        H.sh(['git', 'update-ref', f'refs/remotes/origin/{b}', new], cwd=self.repo)
        self.out(f'reword {b}: {n} subjects, trees identical — pushed')
        f['head'] = new
        f['refusal'] = lane_refusal(self.repo, self.trunk, b, item, self.conv,
                                    members=delivery_members(self.items, item))
        if f.get('review'):
            f['review']['current'] = review_mod.is_current(self.repo, self.conv, f'origin/{b}',
                                                           f['review'], new,
                                                           trunk=f'origin/{self.trunk}')
        if f.get('correction') and (f['correction'].get('kind') == lifecycle.NAMING):
            H.mark_session(self.state_dir, (f.get('run') or {}).get('job') or b, correction=None,
                           branch=b)
            f['correction'] = None
        if f.get('run') is None:
            self.write(f, self.record(f, PUSHED, 'adopted'))
        return self.set(f, PUSHED, f'reworded {n} subjects (naming)')

    def _naming_back(self, f, why):
        """The rewrite cannot be done by the lane: the one line, and the session's correction
        carries the cause, not only the symptom (a product's T-0338 was told "reword them" 12
        times while the lane knew its branch carried 22 copies of trunk commits). None."""
        self.out(f'reword {f["branch"]}: {why} — back to its session (naming, no round)')
        f['refusal'] = (lifecycle.NAMING, f"{f['refusal'][1]}. The lane could not because: {why}")
        return None

    def drop_copies(self, f):
        """Trunk history under a factory branch — copies of trunk commits (``git cherry``
        ``-``: a session that merged or rebased the trunk in, an old reword) or a merge of the
        trunk (a session or a person merged ``origin/<trunk>`` in: B-0056) — is
        dropped by the lane with no session (:func:`drop_trunk_copies`): the old tip archived as
        ``archive/<branch>-copies-<sha9>``, the branch rebuilt as ``origin/<trunk>`` plus its
        own commits and pushed over a lease on the archived tip, one line logged, and the branch
        goes on from PUSHED on the new head. A pick that conflicts pushes nothing: the branch is
        kept as is and held back to its session with the conflicting files (:data:`COPIES`).
        Never the trunk or a protected ref, never a branch under no factory prefix or a foreign
        PR, never while a live session holds the branch (deferred until it ends). The product's
        ``pre_push_check`` runs on the rebuilt head before the push; a red one pushes nothing and
        goes back with the check's output (:data:`COPIES`). The record, or None when nothing
        moved."""
        b, old = f['branch'], f.get('head')
        prev = f.get('prev') or {}
        if not self.repo or not old or not f.get('kind') or f.get('foreign') \
                or f.get('landed') or f.get('on_trunk') or (f.get('pr') or {}).get('draft') \
                or prev.get('state') in (MERGING, QUEUED, PARKED) + TERMINAL_STATES:
            return None
        corr = f.get('correction') or {}
        if corr.get('kind') == COPIES and prev.get('head') == old:
            return None  # held for its conflict: the session's rebase moves the head
        copies, merges = trunk_history(self.repo, self.trunk, b)
        if not copies and not merges:
            return None
        what = (f'{len(copies)} copies of origin/{self.trunk} commits' if copies else '') \
            + (' and ' if copies and merges else '') + (f'{len(merges)} merge(s)' if merges else '')
        if f.get('live'):
            self.out(f'trunk history on {b} ({what}): a live session holds it — the rebuild '
                     f'waits for it to end')
            return None
        if self.guarded(b, f'drop trunk copies {b}'):
            return None
        if self.dry_run:
            self.out(f'DRY: would rebuild {b} on origin/{self.trunk} without its {what}')
            return None
        self.fresh_trunk()
        res = drop_trunk_copies(self.repo, self.trunk, b)
        if res['old'] != old:
            return None  # origin moved since the facts: next pass reads it again
        if res['conflict']:
            sha, files = res['conflict']
            text = (f'trunk history under the branch ({what}): the lane rebuilt it on '
                    f'origin/{self.trunk} and {sha[:9]} conflicts in {", ".join(files) or "?"} — '
                    f'rebase the branch\'s own commits onto origin/{self.trunk} '
                    f'(git rebase origin/{self.trunk}) resolving those files, never merge; the '
                    f'factory publishes the rebased branch')
            self.out(f'drop copies {b}: {sha[:9]} conflicts in {", ".join(files)} — the branch '
                     f'is kept as is, back to its session')
            if f.get('run') is None:
                self.write(f, self.record(f, PUSHED, 'adopted'))
            rec = self.set(f, BACK, f'kind={COPIES}')
            self.results[b] = hold_with_correction(self.state_dir, b, f['run'], COPIES, text,
                                                   self.out, head=old, main=self.trunk)
            f['correction'] = {'kind': COPIES, 'text': text}
            # flags.mechanical: this was the table's try — counted, not tried again on this head
            mechanical.note(self, f, mechanical.Outcome(COPIES, False, old, text))
            return rec
        new = res['new']
        if not new:
            self.out(f'drop copies {b} failed: {res["why"]} — the branch is kept as is')
            return None
        ok, line = self.pre_push_ok(new)
        if ok is False:
            text = (f'trunk history under the branch ({what}): the lane rebuilt it on '
                    f'origin/{self.trunk} and the product\'s pre-push check fails on it — '
                    f'{line}\nRebase the branch\'s own commits onto origin/{self.trunk} (git '
                    f'rebase origin/{self.trunk}), fix what the check names, run it, never merge; '
                    f'the factory publishes the rebased branch')
            self.out(f'drop copies {b}: the pre-push check fails on the rebuilt head — the '
                     f'branch is kept as is, back to its session')
            if f.get('run') is None:
                self.write(f, self.record(f, PUSHED, 'adopted'))
            rec = self.set(f, BACK, f'kind={COPIES}')
            self.results[b] = hold_with_correction(self.state_dir, b, f['run'], COPIES, text,
                                                   self.out, head=old, main=self.trunk)
            f['correction'] = {'kind': COPIES, 'text': text}
            # flags.mechanical: this was the table's try — counted, not tried again on this head
            mechanical.note(self, f, mechanical.Outcome(COPIES, False, old, text))
            return rec
        if ok is None:
            self.out(f'drop copies {b}: deferred — {line}')
            return None
        archive = self.publish_rebuilt(f, old, new, 'copies', 'drop copies')
        if not archive:
            return None
        dropped = len(res['copies']) + len(res['empty'])
        self.out(f'dropped {dropped} trunk copies'
                 + (f' and {len(res["merges"])} merge(s)' if res['merges'] else '')
                 + f' from {b}: {old[:9]} → {new[:9]}, {len(res["own"])} own commit(s) on '
                 f'origin/{self.trunk} ({self.trunk_sha_now()[:9]}); old tip kept as {archive}')
        if corr.get('kind') in (lifecycle.NAMING, COPIES, 'merge'):
            H.mark_session(self.state_dir, (f.get('run') or {}).get('job') or b, correction=None,
                           branch=b)
            f['correction'] = None
        if f.get('run') is None:
            self.write(f, self.record(f, PUSHED, 'adopted'))
        state = BACK if f.get('correction') else PUSHED
        on = mechanical.enabled(self.product)
        if on:  # flags.mechanical: counted as the table's, the approval of the old head carried
            mechanical.note(self, f, mechanical.Outcome(COPIES, True, new, 'dropped'))
        return self.set(f, state, f'dropped {dropped} trunk copies',
                        **({'mechanical_from': old} if on else {}))

    def normalise_commits(self, f):
        """The session exit contract's half the lane owns: a factory branch's own commits get
        the sign-off (``commit.signoff``) and the product's ``commit.trailers`` written by the
        lane on publish (:func:`normalise_branch`) — before CI judges the head, so a red DCO
        check and a session sent back for a trailer never happen (the subject's item id is
        :meth:`repair_naming`'s, also with no session). Once per new head (the head the last
        record did not carry), the trunk fetched first (:meth:`fresh_trunk`, once a pass), never
        while a live session holds the branch, never on a foreign PR, a branch
        under no factory prefix, a protected ref (:meth:`guarded`) or one past PUSHED's reach
        (MERGING, QUEUED, PARKED, terminal). Every rewritten commit carries the tree of the one
        it replaces (:func:`trees_identical`) and the push is over a lease on the tip read,
        refs only (no new content for the product's pre-push hook to judge). A branch that is
        not its own straight line is left to :meth:`drop_copies` / the naming refusal. Never a
        state of its own: ``f``'s head, refusal and review currency move to the new head and the
        pass goes on. True when it pushed."""
        b, old, item = f['branch'], f.get('head'), f.get('item')
        prev = f.get('prev') or {}
        if not self.repo or not old or not item or not f.get('kind') or f.get('foreign') \
                or f.get('live') or f.get('landed') or f.get('on_trunk') \
                or (f.get('pr') or {}).get('draft') or prev.get('head') == old \
                or prev.get('state') in (MERGING, QUEUED, PARKED) + TERMINAL_STATES:
            return False
        conv = self.conv
        signoff = bool(conv is not None and conv.signoff())
        trailers = conv.commit_trailers() if conv is not None else {}
        if not signoff and not trailers:
            return False  # nothing the product requires: no git read at all
        if not getattr(self, '_normalise_fetched', False):
            self.fresh_trunk()  # a stale trunk ref would count trunk commits as the branch's
            self._normalise_fetched = True
        new, n = normalise_branch(self.repo, self.trunk, b, item, signoff, trailers)
        if not new:
            return False
        same, differ = trees_identical(self.repo, old, new)
        if not same:
            self.out(f'normalise {b}: the rewrite changed more than messages ({differ}) — '
                     f'nothing pushed')
            return False
        if self.dry_run:
            self.out(f'DRY: would write the trailers on {n} commit(s) of {b}')
            return False
        if self.guarded(b, f'normalise {b}'):
            return False
        wt = self.ref_checkout()
        if not wt:
            self.out(f'normalise {b}: deferred — {self.ref_wt_error}')
            return False
        r = gitpush.push(['-q', f'--force-with-lease=refs/heads/{b}:{old}', 'origin',
                          f'{new}:refs/heads/{b}'], wt, refs_only=True,
                         timeout=gitpush.push_timeout(self.conv), log=self.out, guard=refguard.guard_from(self.trunk, self.conv))
        if r.returncode != 0:
            self.out(f'normalise {b}: deferred — push refused '
                     f'({push_why(r.stderr or r.stdout)})')
            return False
        H.sh(['git', 'update-ref', f'refs/remotes/origin/{b}', new], cwd=self.repo)
        self.out(f'normalised {n} commit message(s) on {b} (trailers): {old[:9]} → '
                 f'{new[:9]}, trees identical — no session')
        f['head'] = new
        f['refusal'] = lane_refusal(self.repo, self.trunk, b, item, self.conv,
                                    members=delivery_members(self.items, item))
        if f.get('review'):
            f['review']['current'] = review_mod.is_current(self.repo, self.conv, f'origin/{b}',
                                                           f['review'], new,
                                                           trunk=f'origin/{self.trunk}')
        return True

    def pre_push_ok(self, sha):
        """``(ok, line)``: the product's ``conventions.pre_push_check`` on ``sha`` — the check a
        session runs before its push, judged before a push of the lane's own that changes what
        the branch builds on, never run inside the pass (``lane.rebuild_check``): ``background``
        hands it to a detached job and says ``None`` (deferred) until a later pass finds its
        result; ``session`` says False (the branch's session rebuilds and checks); ``off`` says
        True. No check configured: ``(True, '')``."""
        command = approvals.pre_push_check(self.product)
        if not command:
            return True, ''
        setup = getattr(self.conv, 'worktree_setup', None)
        mode = self.conv.rebuild_check() if hasattr(self.conv, 'rebuild_check') else 'background'
        if mode == 'off':
            return True, ''
        if mode == 'session':
            return False, (f'`{command}` was not run by the lane on {sha[:9]} '
                           f'(lane.rebuild_check: session) — the session runs it')
        # never inside the tick (lane.rebuild_check: background): a detached job runs it, the
        # next pass reads its result (asf.harvest.rebuild_check)
        from asf.harvest import rebuild_check
        return rebuild_check.check(self.product, self.repo, self.state_dir, sha, command, setup,
                                   dry_run=self.dry_run)

    def publish_rebuilt(self, f, old, new, tag, what):
        """Publish ``new`` — ``f``'s branch rebuilt by the lane — in ONE push: the old tip
        archived as ``archive/<branch>-<tag>-<sha9>``, the branch pushed from the ref checkout
        over a lease on ``old`` (the product's hook runs: the content is new), the tracking ref,
        ``f``'s head, its refusal and its review's currency read again. The archive's name, or
        '' when nothing was pushed (the line says why)."""
        b = f['branch']
        archive = f'archive/{b}-{tag}-{old[:9]}'
        if not self.ref_push(f'{old}:refs/heads/{archive}', f'archive {b}-{tag}'):
            self.out(f'{what} {b}: the old tip could not be archived — nothing pushed')
            return ''
        wt = self.ref_checkout()
        if not wt:
            self.out(f'{what} {b} failed: {self.ref_wt_error}')
            return ''
        r = gitpush.push(['-q', f'--force-with-lease=refs/heads/{b}:{old}', 'origin',
                          f'{new}:refs/heads/{b}'], wt,
                         timeout=gitpush.push_timeout(self.conv), log=self.out, guard=refguard.guard_from(self.trunk, self.conv))
        if r.returncode != 0:
            self.out(f'{what} {b} push refused: {push_why(r.stderr or r.stdout)} — kept')
            return ''
        H.sh(['git', 'update-ref', f'refs/remotes/origin/{b}', new], cwd=self.repo)
        f['head'] = new
        f['refusal'] = lane_refusal(self.repo, self.trunk, b, f.get('item'), self.conv,
                                    members=delivery_members(self.items, f.get('item')))
        if f.get('review'):
            f['review']['current'] = review_mod.is_current(self.repo, self.conv, f'origin/{b}',
                                                           f['review'], new,
                                                           trunk=f'origin/{self.trunk}')
        return archive

    def rebase_onto_trunk(self, f):
        """A branch sent back for a conflict with the trunk, rebased by the lane with no session:
        its own commits picked onto a fresh ``origin/<trunk>`` (:func:`drop_trunk_copies`
        ``always``: copies and merges dropped, no checkout), the product's ``pre_push_check`` run
        on the result, then ONE push over a lease on the old tip (:meth:`publish_rebuilt`).
        Git decides, never a session: a host that calls a branch conflicting which git merges
        clean (a ``merge=union`` file, a stale ``mergeable``) is rebased here. A dict —
        ``{'pushed': sha}``; ``{'conflict': (sha, files)}`` (a real textual conflict, nothing
        pushed); ``{'check': line}`` (the check failed, nothing pushed); ``{'deferred': line}``
        (the check on the rebuilt head runs in the background: a later pass) — or None when the lane
        does not rebase it (not a factory branch, a live session, a draft, a dry run, a
        protected ref, a branch that moved or needs nothing, a push that went nowhere)."""
        b, old = f.get('branch'), f.get('head')
        if not self.repo or not b or not old or not f.get('kind') or f.get('foreign') \
                or f.get('live') or f.get('landed') or (f.get('pr') or {}).get('draft') \
                or self.dry_run:
            return None
        if self.guarded(b, f'rebase {b}'):
            return None
        self.fresh_trunk()
        H.sh(['git', 'fetch', '-q', 'origin', f'+refs/heads/{b}:refs/remotes/origin/{b}'],
             cwd=self.repo)
        res = drop_trunk_copies(self.repo, self.trunk, b, always=True)
        if res['old'] != old:
            return None  # moved since the facts: whatever moved it is read next pass
        if res['conflict']:
            return {'conflict': res['conflict']}
        new = res['new']
        if not new:
            # already straight on the trunk (the conflict is with a branch stacked beside it),
            # or a rebuild the guard refused: the session's round as before, nothing said here
            return None
        ok, line = self.pre_push_ok(new)
        if ok is False:
            return {'check': line}
        if ok is None:
            self.out(f'rebase {b}: {line} — not rebased yet')
            return {'deferred': line}
        archive = self.publish_rebuilt(f, old, new, 'rebase', 'rebase')
        if not archive:
            return None
        self.out(f'rebased {b} onto origin/{self.trunk} ({self.trunk_sha_now()[:9]}): '
                 f'{old[:9]} → {new[:9]}, {len(res["own"])} own commit(s)'
                 + (f', {len(res["copies"]) + len(res["empty"])} trunk copies dropped'
                    if res['copies'] or res['empty'] else '')
                 + (f', {len(res["merges"])} merge(s) dropped' if res['merges'] else '')
                 + f' — no session; old tip kept as {archive}')
        return {'pushed': new}

    def transplant_case(self, f):
        """``(approval, why)`` when ``f``'s branch is a transplant case (:mod:`asf.harvest.
        transplant`), else None: a factory code branch of an open Task or Bug (no delivery lead,
        no foreign PR), held BACK by a mechanical correction (:func:`asf.harvest.transplant.
        mechanical`) or STALE for being unmoved in PUSHED; no session running on the item, no
        operator park (item or branch), no ``asf correct`` instruction or ruling pending, no
        reset at the approved head; the newest review approved the content on a head the lane
        has transplanted fewer than :data:`asf.harvest.transplant.CAP` times. Read only."""
        b, item, head = f.get('branch'), f.get('item'), f.get('head')
        prev = f.get('prev') or {}
        state = prev.get('state')
        if not self.repo or not b or not head or not item or f.get('live') \
                or f.get('foreign') or f.get('landed') or f.get('on_trunk') or f.get('closed') \
                or (f.get('pr') or {}).get('draft'):
            return None
        kind = f.get('kind')
        if not kind or kind in ('spec', 'plan', DIRECT):
            return None
        corr = f.get('correction')
        if state == BACK:
            if not corr:
                return None
        elif not (state == STALE and str(prev.get('reason') or '').startswith('unmoved in PUSHED')):
            return None
        card = (self.items or {}).get(item) if self.items is not None else {'type': 'task'}
        if not card or card.get('removed') or card.get('state') in lifecycle.DONE_STATES \
                or card.get('type', 'task') not in ('task', 'bug') \
                or feature_delivery(self.items, item):
            return None
        if any(lifecycle.is_live(r) for r in lifecycle.item_runs(self.path, item)):
            return None
        if lifecycle.item_park(self.path, item) or any(
                lifecycle.park_holds(p, item, branch=b) for p in lifecycle.parks(self.path)):
            return None
        trunk_ref = f'origin/{self.trunk}'
        if corr is not None:
            if corr.get('kind') == 'operator':  # asf correct: the operator's instruction wins
                return None
            if not transplant_mod.mechanical(corr, lambda p: transplant_mod.artifact(
                    self.conv, p) or transplant_mod.generated(self.repo, self.conv, p,
                                                              (f'origin/{b}', trunk_ref))):
                return None
        if any(r.get('job') == 'operator'
               for r in rulings_mod.standing(self.product, dict(card, id=item))):
            return None  # an operator ruling on the card: its adjudication carries it out
        appr = transplant_mod.approval(
            self.repo, self.conv, f'origin/{b}', item,
            reviews.required(review_kind(kind)),
            store=os.path.join(self.state_dir, review_store.DIRNAME))
        if not appr:
            return None
        reset = lifecycle.resets(self.path).get(item) or {}
        if reset and reset.get('head') in (appr['head'], head):
            return None  # the operator reset this content: it restarts, it is not carried
        if transplant_mod.count(self.state_dir, item, appr['head']) >= transplant_mod.CAP:
            return None
        why = (f"kind={corr.get('kind')}" if corr else prev.get('reason') or state)
        return appr, why

    def transplant(self, f):
        """T13t: approved content held only by git mechanics, moved onto the trunk by the lane
        with no session (:mod:`asf.harvest.transplant`): a commit on a fresh ``origin/<trunk>``
        with the approved head's version of the Task's own files (artifacts left out, generated
        files regenerated or kept at the trunk), the product's ``pre_push_check`` run on it,
        ONE push over a lease with the old tip archived as ``archive/<branch>-transplant-<sha9>``
        (:meth:`publish_rebuilt`), the pending correction cleared, and the branch PUSHED at the
        new head — the approval carried (``transplant`` on the lane record) only when the diff
        for those files is the approved one, else a fresh review round. A red check pushes
        nothing and is a normal correction round (``gate``). The record, or None."""
        case = self.transplant_case(f)
        if not case:
            return None
        appr, why = case
        b, old, item = f['branch'], f['head'], f['item']
        src = appr['head']
        if self.guarded(b, f'transplant {b}'):
            return None
        if self.dry_run:
            self.out(f'DRY: would transplant {b}: {appr["path"]} approved {src[:9]}; held by '
                     f'{why}')
            return None
        self.fresh_trunk()
        trunk_ref = f'origin/{self.trunk}'
        plan = transplant_mod.plan(self.repo, self.conv, trunk_ref, src)
        if not plan['copy'] and not plan['regen']:
            self.out(f'transplant {b}: {src[:9]} carries no file of its own past origin/'
                     f'{self.trunk} — nothing to transplant')
            return None
        card = (self.items or {}).get(item) or {}
        word = 'fix' if card.get('type') == 'bug' else 'task'
        subject = transplant_mod.subject_of(self.repo, trunk_ref, src, item, word)
        ident = transplant_mod.ident_of(self.repo, src)
        msg = transplant_mod.message(subject, item, src, appr['path'], self.trunk, plan)
        conv = self.conv
        msg = normalise_message(self.repo, msg, ident, item,
                                bool(conv is not None and conv.signoff()),
                                conv.commit_trailers() if conv is not None else {}) or msg
        new, err, conflicts = transplant_mod.build(self.repo, self.state_dir, trunk_ref, src,
                                                   plan, msg, ident,
                                                   getattr(conv, 'worktree_setup', None))
        if conflicts:  # content against content: a session's, counted so it is not retried
            transplant_mod.note(self.state_dir, item=item, branch=b, at=now_iso(), **{
                'from': src, 'old': old, 'conflicts': conflicts, 'pushed': False})
        if not new:
            self.out(f'transplant {b}: not built — {err}')
            return None
        if _tree(self.repo, new) == _tree(self.repo, old):
            self.out(f'transplant {b}: the branch already is the approved content on origin/'
                     f'{self.trunk} — nothing to transplant')
            return None
        ok, line = self.pre_push_ok(new)
        if ok is None:
            self.out(f'transplant {b}: deferred — {line}')
            return None
        files = plan['copy']
        carried = transplant_mod.same_diff(self.repo, plan['base'], src, trunk_ref, new, files)
        transplant_mod.note(self.state_dir, item=item, branch=b, at=now_iso(), **{
            'from': src, 'old': old, 'new': new, 'carried': carried, 'pushed': bool(ok)})
        if ok is False:
            text = (f'the lane transplanted the content {appr["path"]} approved at {src[:9]} '
                    f'onto origin/{self.trunk} ({new[:9]}: the approved change to '
                    f'{len(files)} file(s)) and the product\'s pre-push check fails on it — '
                    f'{line}\nRebuild the branch as origin/{self.trunk} plus the approved change '
                    f'(git checkout -B {b} origin/{self.trunk}; git diff {plan["base"][:12]} '
                    f'{src[:12]} -- {" ".join(files)} | git apply -3), fix what the check names, '
                    f'run it, commit naming {item}, never merge; the factory publishes the branch')
            self.out(f'transplant {b}: the pre-push check fails on the transplant — nothing '
                     f'pushed, a correction round')
            if f.get('run') is None:
                self.write(f, self.record(f, PUSHED, 'adopted'))
            if f.get('correction'):  # the mechanical hold gives way to the check's
                H.mark_session(self.state_dir, f['run'].get('job') or b, correction=None, branch=b)
            rec = self.set(f, BACK, 'kind=gate')
            self.results[b] = hold_with_correction(self.state_dir, b, f['run'], 'gate', text,
                                                   self.out, head=old, main=self.trunk)
            f['correction'] = {'kind': 'gate', 'text': text}
            return rec
        archive = self.publish_rebuilt(f, old, new, 'transplant', 'transplant')
        if not archive:
            return None
        if f.get('correction'):
            H.mark_session(self.state_dir, (f.get('run') or {}).get('job') or b, correction=None,
                           branch=b)
            f['correction'] = None
        f['checks_red'] = None
        tp = {'from': src, 'head': new, 'review': appr['path'], 'round': appr['round'],
              'customer_row': appr['customer_row'], 'carried': carried}
        if carried and f.get('review_required', True):
            f['review'] = transplant_mod.carried_review(tp)
        elif f.get('review'):
            f['review'] = dict(f['review'], current=False)
        regen = f', {len(plan["regen"])} regen command(s)' if plan['regen'] else ''
        kept = f', {len(plan["trunk"])} generated file(s) kept at origin/{self.trunk}' \
            if plan['trunk'] else ''
        verdict = (f'{appr["path"]} carried: the diff is the approved one' if carried
                   else 're-review: the diff differs from the approved one')
        self.out(f'transplanted {b} (held by {why}): {appr["path"]} approved {src[:9]} → '
                 f'{new[:9]} on origin/{self.trunk} ({self.trunk_sha_now()[:9]}), '
                 f'{len(files)} file(s){regen}{kept}; {verdict} — no session; old tip kept '
                 f'as {archive}')
        if f.get('run') is None:
            self.write(f, self.record(f, PUSHED, 'adopted'))
        return self.set(f, PUSHED, f'transplanted the approved {src[:9]}', transplant=tp)

    def trunk_sha_now(self):
        return H.sh(['git', 'rev-parse', f'origin/{self.trunk}'], cwd=self.repo).stdout.strip()

    def repair_signoff(self, f, check):
        """A sign-off refusal (the PR's ``check`` — :meth:`Conventions.is_signoff_check` — red),
        repaired by the lane with no session: each commit on the branch its author has not
        signed off gets the trailer (:func:`signoff_branch`, trees unchanged), pushed from the ref
        checkout over a lease on the old tip, and the branch is PUSHED at its new head so its
        checks run again. The record, or None when it could not be (the caller sends the red
        check back as before). A branch under no factory prefix is never rewritten."""
        b, old = f['branch'], f.get('head')
        if not old or not self.repo:
            return None
        if not f.get('kind'):
            self.out(f'sign-off {b} ({check}): under no factory prefix — the lane does not '
                     f'rewrite it')
            return None
        if self.dry_run:
            self.out(f'DRY: would sign off the commits on {b} ({check}) — no session')
            return None
        if self.guarded(b, f'sign-off {b} ({check})'):
            return None
        self.fresh_trunk()
        why = []
        new, n = signoff_branch(self.repo, self.trunk, b, why)
        wt = self.ref_checkout() if new else ''
        if not new or not wt:
            self.out(f'sign-off {b} ({check}) failed: '
                     f'{(why[0] if why else "every commit is signed off already") if not new else self.ref_wt_error}')
            return None
        r = gitpush.push(['-q', f'--force-with-lease=refs/heads/{b}:{old}', 'origin',
                          f'{new}:refs/heads/{b}'], wt,
                         timeout=gitpush.push_timeout(self.conv), log=self.out, guard=refguard.guard_from(self.trunk, self.conv))
        if r.returncode != 0:
            self.out(f'sign-off {b} ({check}) push refused: {push_why(r.stderr or r.stdout)}')
            return None
        H.sh(['git', 'update-ref', f'refs/remotes/origin/{b}', new], cwd=self.repo)
        self.out(f'signed off {n} commits on {b} ({check}) — no session')
        f['head'] = new
        if f.get('review'):
            f['review']['current'] = review_mod.is_current(self.repo, self.conv, f'origin/{b}',
                                                           f['review'], new,
                                                           trunk=f'origin/{self.trunk}')
        if f.get('run') is None:
            self.write(f, self.record(f, PUSHED, 'adopted'))
        return self.set(f, PUSHED, f'signed off {n} commits ({check})')

    def enter_back(self, f, reason):
        """BACK: a refusal or a review's changes go back to a session (a hold); a pending
        correction is already that."""
        b = f['branch']
        kind = reason.split('=', 1)[-1]
        if f.get('correction') and not (kind == 'gate' and f.get('checks_red')
                                        and f['correction'].get('kind') == 'review'):
            return self.set(f, BACK, reason)
        if kind == 'gate' and f.get('checks_red'):
            red, n = f['checks_red'], (f.get('pr') or {}).get('number')
            text = f"PR #{n} checks red: {red['detail']}"
            if self.dry_run:
                self.out(f'DRY: would hold {b}: {text}')
                self.results[b] = 'dry'
                return None
            send_back(self, f, 'gate', text + red_evidence(self.host.slug, red['checks'],
                                                           red['names']), ())
            f['correction'] = {'kind': 'gate', 'text': text}
            return f.get('prev')
        if kind == 'conflict' and f.get('conflict'):
            text = conflict_text((f.get('pr') or {}).get('number'), self.trunk, f['conflict'])
            if self.dry_run:
                self.out(f'DRY: would send {b} back: {text}')
                self.results[b] = 'dry'
                return None
            if send_back(self, f, 'conflict', text, list(f['conflict'])) == 'held':
                f['correction'] = {'kind': 'conflict', 'text': text}
            return f.get('prev')
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
        finding = review_mod.c_items((f.get('review') or {}).get('body')) if kind == 'review' \
            else f.get('incomplete') if kind == lifecycle.INCOMPLETE else None
        # flags.mechanical: a refusal the table holds (naming, merge) is tried by code first
        table = {'lane': self, 'f': f} if kind != 'review' else {}
        self.results[b] = hold_with_correction(self.state_dir, b, f['run'], kind, text, self.out,
                                               head=f.get('head'), finding=finding,
                                               main=self.trunk, **table)
        if self.results[b] == 'mechanical':
            return f.get('prev')
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
        budget = pass_budget_s(lane)
        started = time.monotonic()
        order = sorted(found)
        for i, b in enumerate(order):
            if budget and time.monotonic() - started >= budget:
                rest = order[i:]
                lane.out(f'lane: pass budget {budget:g}s spent — {len(rest)} branch(es) wait for '
                         f'the next pass ({", ".join(rest[:5])}{" …" if len(rest) > 5 else ""})')
                break
            lane.advance(found[b])
    finally:
        lane.finish_ref_pushes()
    # the queue's pass (duplicate pushes, relief and its re-runs), under
    # its own lock: the queue's own scheduler job runs it every minute too, and a pass running
    # there already does this one's work (asf.ci_queue.queue_pass)
    from asf import ci_queue
    if ci_queue.queue_pass(product, items=lane.items, out=lane.out,
                           dry_run=lane.dry_run) is None:
        lane.out('ci queue: its own pass is running — the lane leaves the queue to it')
    return lane.results, found


def pass_budget_s(lane):
    """``lane.pass_budget_s`` (default 300; 0: none): one pass advances branches for at most this
    long — a pass that ran one branch's checks after another held the tick for an hour."""
    conv = getattr(lane, 'conv', None)
    return conv.pass_budget_s() if hasattr(conv, 'pass_budget_s') else 0


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
    entries = take_for_tick(lane, entries, items)
    ready = held_by_host(lane, precheck(lane, entries))
    if ready or merge_queued(lane):
        try:
            if ready:
                gate_set(lane, ready)
            else:   # nothing new to gate: the batches in flight are still judged (T10b)
                from asf import merge_queue
                merge_queue.run(lane, [])
        finally:
            lane.finish_ref_pushes()
    return lane.results


def merge_queued(lane):
    """True when the product lands its PRs through the lane's own merge queue
    (``conventions.merge: queue`` on a PR host: :mod:`asf.merge_queue`)."""
    return lane.mode == 'pr' and not isinstance(lane.host, FastForwardHost) \
        and bool(getattr(lane.conv, 'merge_queue', lambda: False)())


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


def pr_order(entries, items=None, visits=None):
    """``entries`` hotfix first, then S1, S2, the rest; within a rank the branch the gate took
    longest ago first (never taken: first) — ``visits`` ``{branch: epoch}`` (:data:`GATE_VISITS`)
    — else stable. Without the visit order a capped tick took the same first ``branches_per_tick``
    by name every time: twelve WAITING code PRs held the cap and three spec PRs sat in GATE,
    never gated (2026-09-27)."""
    visits = visits or {}

    def rank(f):
        if 'hotfix' in f['branch'].lower():
            sev = 0
        else:
            card = (items or {}).get(f.get('item') or '') or {}
            sev = {'S1': 1, 'S2': 2}.get(card.get('severity'), 3)
        try:
            seen = float(visits.get(f['branch']) or 0.0)
        except (TypeError, ValueError):
            seen = 0.0
        return sev, seen
    return sorted(entries, key=rank)


def read_visits(state_dir):
    """``{branch: epoch}`` the gate last took each branch (:data:`GATE_VISITS`); {} unreadable."""
    try:
        with open(os.path.join(state_dir, GATE_VISITS), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def take_for_tick(lane, entries, items=None, now=None):
    """The gate entries this tick takes: :func:`pr_order` over the recorded visits, capped to
    ``branches_per_tick`` (:func:`asf.harvest.harvest.cap_to_tick`); the taken ones' visits are
    stamped so the next capped tick starts from the rest — no branch starves behind the cap.
    Only branches still eligible are kept in the file."""
    visits = read_visits(lane.state_dir)
    taken = H.cap_to_tick(pr_order(entries, items, visits), lane.out, lane.conv)
    if not lane.dry_run:
        stamp = time.time() if now is None else now
        names = {f['branch'] for f in entries}
        kept = {b: t for b, t in visits.items() if b in names}
        kept.update({f['branch']: stamp for f in taken})
        path = os.path.join(lane.state_dir, GATE_VISITS)
        try:
            with open(path + '.tmp', 'w', encoding='utf-8') as fh:
                json.dump(kept, fh, sort_keys=True)
            os.replace(path + '.tmp', path)
        except OSError:
            pass
    return taken


def missing_policy(conv, cls):
    """``conventions.landing_checks_missing`` for landing class ``cls``: one value or a map."""
    value = conv.get('landing_checks_missing')
    if isinstance(value, dict):
        value = value.get(cls, value.get('default'))
    return MISSING_WAIT if str(value or '').strip().lower() == MISSING_WAIT else MISSING_LOCAL_GATE


def merge_skipped(conv):
    """``conventions.merge_skipped``: ``never`` when so set, else ``path-filtered``."""
    value = str(conv.get('merge_skipped') or '').strip().lower()
    return MERGE_SKIPPED_NEVER if value == MERGE_SKIPPED_NEVER else MERGE_SKIPPED_PATH


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
    for f in shared_path_order(lane, entries):
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
                wait(lane, f, SHARED_PATH)
                continue
            shared_taken = b
        ready.append(f)
    return ready


#: the WAITING reason of a branch a shared path held back this tick (:func:`precheck`)
SHARED_PATH = 'shared-path'


def shared_path_order(lane, entries):
    """``entries`` in the order :func:`precheck` takes them: a branch that has waited on a
    shared path (WAITING ``shared-path``) for at least ``lane.shared_path_aging`` first, the
    oldest wait first; every other entry after them in its own order. Only one branch a tick
    takes a shared file, and the first one to ask won it every tick — a product's green code PR
    (T-0389, #1091) starved behind a stream of document PRs on one shared registry file."""
    from asf.conventions import DEFAULT_LANE_SHARED_PATH_AGING, duration_seconds
    reader = getattr(lane.conv, 'lane_shared_path_aging_s', None)
    aging = reader() if reader else duration_seconds(DEFAULT_LANE_SHARED_PATH_AGING)
    now = getattr(lane, 'now', None) or time.time()
    aged = []
    for i, f in enumerate(entries):
        rec = f.get('prev') or {}
        at = _parse_at(rec.get('at')) if rec.get('state') == WAITING \
            and rec.get('reason') == SHARED_PATH else None
        if at is not None and now - at >= aging:
            aged.append((at, i))
    first = [entries[i] for _at, i in sorted(aged)]
    return first + [f for i, f in enumerate(entries) if i not in {j for _a, j in aged}]


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


def red_on_trunk(tmp, trunk, conv, asf_repo, out, modules=None, timing=True):
    """True when the trunk alone is red (on ``modules`` only, when given)."""
    H.sh(['git', 'checkout', '-q', '--detach', f'origin/{trunk}'], cwd=tmp)
    ok, line, _files, _red = H.product_gate(tmp, conv, asf_repo, out, only=modules, timing=timing)
    if not ok and line.startswith(H.TIMED_OUT):
        raise GateTimeout(line)
    return not ok


def gate_groups(tmp, trunk, entries, conv, asf_repo, hold, out, announce=False, only=None,
                trunk_green=False, ledger=None, timing=True):
    """Gate ``entries`` as one combined head; on red, bisect (B-0040). The green groups as
    ``[(entries, sha, full)]``. A timeout raises :class:`GateTimeout` — never bisected (§12).
    ``ledger`` (§2.2), when given, is called after every gate here with the stacked branches,
    the head, ``ok``, the seconds spent and the first failing line — a bisect's targeted re-runs
    each write their own line; ``None`` writes nothing. ``timing`` is passed on to
    :func:`asf.harvest.harvest.product_gate` and :func:`red_on_trunk`, and to both recursive
    calls below."""
    stacked = combined_head(tmp, trunk, entries, hold)
    if not stacked:
        return []
    if announce:
        out(f'harvest: {len(stacked)} branch(es), one gate')
    started = time.monotonic()
    ok, line, files, red = H.product_gate(tmp, conv, asf_repo, out, only, timing=timing)
    head = H.sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
    if ledger:
        ledger([f['branch'] for f in stacked], head, ok, time.monotonic() - started, line)
    if ok:
        return [(stacked, head, not only)]
    if line.startswith(H.TIMED_OUT):
        raise GateTimeout(line)
    if not only and red:
        if red_on_trunk(tmp, trunk, conv, asf_repo, out, red, timing=timing):
            raise TrunkRed(red)
        trunk_green = True
    if len(stacked) == 1:
        hold(stacked[0], 'gate', line, files, own=trunk_green)
        return []
    out(f'harvest: bisecting {len(stacked)} branches')
    mid = len(stacked) // 2
    narrowed = red or only
    return (gate_groups(tmp, trunk, stacked[:mid], conv, asf_repo, hold, out, only=narrowed,
                        trunk_green=trunk_green, ledger=ledger, timing=timing)
            + gate_groups(tmp, trunk, stacked[mid:], conv, asf_repo, hold, out, only=narrowed,
                          trunk_green=trunk_green, ledger=ledger, timing=timing))


def confirmed_group(tmp, trunk, entries, conv, asf_repo, hold, out, announce=True, ledger=None,
                    timing=True):
    """The one set of ``entries`` green under the *full* gate, as ``(entries, sha, deferred,
    unconfirmed)``; green apart but red together lands the first alone and defers the rest.
    ``ledger`` (§2.2) is passed to every :func:`gate_groups` call."""
    candidates, deferred = list(entries), []
    for _round in range(CONFIRM_ROUNDS):
        groups = gate_groups(tmp, trunk, candidates, conv, asf_repo, hold, out,
                             announce=announce, ledger=ledger, timing=timing)
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


#: the WAITING reason of a branch whose rebuilt head's pre-push check runs in the background
REBUILD_CHECK_WAIT = 'rebuild-check'


def rebuild_waits(lane, f):
    """The rebuilt head's pre-push check runs in the background (``lane.rebuild_check``): the
    branch waits for the pass that reads its result — no session round on a check not judged."""
    wait(lane, f, REBUILD_CHECK_WAIT)
    return 'waiting'


def send_back(lane, f, kind, text, files, rebase=True):
    """Hand a branch the gate refused (the trunk alone green) back to a session. Code: its
    session, a round (:func:`hold_with_correction`). Docs: a :data:`LANDING_GATE` correction,
    which the feeder turns into a STARVED → SPEC/PLAN session on that branch (R7).

    A ``conflict`` with the trunk is git's to settle first, never a session's (``rebase``, the
    default): the lane rebases the branch itself (:meth:`Lane.rebase_onto_trunk`) and, when it
    applies clean and the product's pre-push check passes, pushes it once and the branch goes on
    from PUSHED — ``'rebased'``, no session. Only a textual conflict (its files named in the
    correction) or a red pre-push check (its output named) reaches a session. ``rebase=False``:
    the conflict is not with the trunk (a batch ahead of it in the merge queue), which a rebase
    onto the trunk cannot clear."""
    b, run = f['branch'], f.get('run') or {}
    on = kind == 'conflict' and mechanical.handles(getattr(lane, 'product', None), kind)
    if on and rebase and getattr(lane, 'repo', None) and not getattr(lane, 'dry_run', False):
        # flags.mechanical: the table's rebase, its event written, reason mechanical:conflict
        got = mechanical.apply(lane, f, {'kind': kind, 'text': text, 'files': list(files or ())})
        if got is not None and got.resolved:
            return 'mechanical'
        if got is not None and got.deferred:
            return rebuild_waits(lane, f)
        if got is not None and got.why:
            text = f'{text}. {got.why}'
            files = list(got.files) or list(files or ())
    elif kind == 'conflict' and rebase and getattr(lane, 'repo', None) \
            and not getattr(lane, 'dry_run', False):
        got = lane.rebase_onto_trunk(f) or {}
        if got.get('deferred'):
            return rebuild_waits(lane, f)
        if got.get('pushed'):
            if f.get('run') is None:
                lane.write(f, lane.record(f, PUSHED, 'adopted'))
            lane.set(f, PUSHED, f'rebased onto {lane.trunk} by the lane (no session)')
            corr = f.get('correction') or {}
            if corr.get('kind') in ('conflict', lifecycle.NAMING, COPIES, 'merge'):
                H.mark_session(lane.state_dir, run.get('job') or b, correction=None, branch=b)
                f['correction'] = None
            lane.results[b] = 'rebased'
            return 'rebased'
        if got.get('conflict'):
            sha, cfiles = got['conflict']
            files = list(cfiles) or list(files or ())
            text = (f'{text}. The lane tried the rebase itself and git stops at {sha[:9]}: '
                    f'conflicts in {", ".join(cfiles) or "?"} — resolve exactly those files')
        elif got.get('check'):
            text = (f'{text}. The lane rebased it onto origin/{lane.trunk} cleanly, but the '
                    f'product\'s pre-push check fails on the result (nothing was pushed): '
                    f'{got["check"]}\nRebase, fix what the check names, run it, then push')
    if f.get('class') == DOCS and kind in ('gate', 'conflict'):
        doc = lane.conv.branch_kind(b) or 'document'
        what = 'does not rebase cleanly onto' if kind == 'conflict' else 'turns the gate red on'
        note = (f'the {doc} {what} {lane.trunk} — {text}. Change the {doc} so the product gate '
                f'passes on {lane.trunk}; nothing is merged until it does')
        lane.set(f, BACK, f'kind={LANDING_GATE}')
        job = run.get('job') or b
        fields, line = lifecycle.hold(lane.path, dict(run, branch=b, job=job), LANDING_GATE, note,
                                      now_iso(), head=f.get('head'), main=lane.trunk)
        H.mark_session(lane.state_dir, job, **fields, branch=b)
        lane.out(line)
        lane.results[b] = 'held'
        return 'held'
    lane.set(f, BACK, f'kind={kind}')
    res = hold_with_correction(lane.state_dir, b, run, kind, text, lane.out, files,
                               item_footprint(lane.items, f.get('item')), f.get('files') or (),
                               lane.conv, own=True, read=gate_reader(lane.repo, b),
                               head=f.get('head'), main=lane.trunk,
                               **({'lane': lane, 'f': f} if kind != 'conflict' else {}))
    if res == 'mechanical':  # code settled it after all (flags.mechanical)
        return res
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
    if merge_queued(lane):   # T10b: batched onto the trunk, gated once as that sha
        from asf import merge_queue
        merge_queue.run(lane, to_merge)
    elif to_merge and not isinstance(lane.host, FastForwardHost):
        merge_prs(lane, to_merge)
    return [(f['branch'], (f.get('prev') or {}).get('state'), (f.get('prev') or {}).get('reason'))
            for f in entries]


def gate_one_set(lane, group, to_merge):
    """Gate one set (FF: and push it; PR: queue its green for :func:`merge_prs`). A conflict is
    sent back at once; a branch red alone is sent back only once the trunk alone is seen green
    (checked once, and only when the bisection did not already show it)."""
    conv, trunk, out = lane.conv, lane.trunk, lane.out
    asf_repo = None     # the gate's extra checks are conventions.gate_checks, for every product
    announce = str(conv.harvest_gate).strip().lower() != H.GATE_PER_BRANCH
    timing = not lane.dry_run
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
                    tmp, trunk, pending, conv, asf_repo, hold, out, announce, ledger, timing)
                trunk_red = None
                for f, kind, text, files, own in held:
                    if not own:
                        if trunk_red is None:
                            trunk_red = red_on_trunk(tmp, trunk, conv, asf_repo, out,
                                                     timing=timing)
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
        # read in branch_facts (PD6): the trunk has since caught up to this push
        lane.delete_branch(f)
        lane.out(f"landed {f['branch']} → {sha}{f.get('delivery_note') or ''}")
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


#: a host's merge refusal that is a conflict with the trunk (GitHub: ``Pull Request has merge
#: conflicts``, ``is not mergeable``) — the branch goes BACK to be rebased, never waits
MERGE_CONFLICT_RE = re.compile(r'merge conflict|not mergeable|conflicting', re.I)


def conflict_files(repo, trunk, branch, fetch=True):
    """The files ``origin/<branch>`` conflicts in against a freshly fetched ``origin/<trunk>``
    (the pass may have just merged onto it), or ``[]`` when unknown. ``fetch=False``: the refs
    as the pass's own fetch left them."""
    if not repo:
        return []
    if fetch:
        H.sh(['git', 'fetch', '-q', 'origin', trunk, branch], cwd=repo)
    got = H.sh(['git', 'merge-tree', '--write-tree', '--name-only', '--no-messages',
                f'origin/{trunk}', f'origin/{branch}'], cwd=repo)
    if got.returncode != 1:
        return []
    return [l for l in (got.stdout or '').splitlines()[1:] if l.strip()]


def conflict_text(number, trunk, files):
    """The correction for a branch that conflicts with ``trunk`` in ``files``, PR ``number``
    (None: no PR yet)."""
    what = f'PR #{number}' if number else 'the branch'
    return (f'{what} conflicts with origin/{trunk} in {", ".join(files)} — GitHub runs no '
            f'pull_request workflow on a conflicting PR, so its required checks never start; '
            f'rebase the branch onto origin/{trunk} (git rebase origin/{trunk}), never merge; '
            f'the factory publishes the rebased branch')


def redaction_recheck(lane, f):
    """The landing's own redaction scan of a branch whose commits carry
    :data:`asf.redact.UNCHECKED_TRAILER` — made where no scanner ran (a cloud container's hook
    with no ``asf``, :func:`asf.hooks._git_hook_body`). Every commit the branch adds over
    ``origin/<trunk>`` is scanned (:func:`asf.harvest.harvest.scan_beyond_trunk`) before it may
    merge: a finding sends the branch back (``redact``), a scan that cannot run holds it — never
    a merge on an unchecked commit. None when nothing is marked or the scan is clean (said so,
    loudly); else the branch's result."""
    from asf import redact
    b, head, repo = f['branch'], f.get('head'), getattr(lane, 'repo', None)
    if not head or not repo:
        return None
    try:
        try:
            marked = redact.unchecked_commits(repo, f'origin/{lane.trunk}', head)
        except redact.RedactError:  # the head not fetched yet: fetch the branch once, read again
            H.sh(['git', 'fetch', '-q', 'origin', f'+refs/heads/{b}:refs/remotes/origin/{b}'],
                 cwd=repo)
            marked = redact.unchecked_commits(repo, f'origin/{lane.trunk}', head)
    except redact.RedactError as e:
        lane.out(f'held {b}: redaction re-check could not read the branch — {e}')
        wait(lane, f, f'redaction re-check failed: {e}', 'held', green=f.get('green'))
        return 'held'
    if not marked:
        return None
    lane.out(f'harvest: {b}: {len(marked)} commit(s) marked "{redact.UNCHECKED_TRAILER}" '
             f'({", ".join(s[:9] for s in marked)}) — re-scanning before it merges')
    try:
        findings = H.scan_beyond_trunk(repo, head, lane.trunk, redact.patterns(repo))
    except (OSError, subprocess.SubprocessError, redact.RedactError) as e:
        lane.out(f'held {b}: redaction re-scan failed — {e}')
        wait(lane, f, f'redaction re-scan failed: {e}', 'held', green=f.get('green'))
        return 'held'
    if not findings:
        lane.out(f'harvest: {b}: redaction re-scan clean')
        return None
    lines = redact.format_findings(findings)
    send_back(lane, f, 'redact', H.redaction_reason(lines) + ' — remove each finding, then push',
              sorted({x.path for x in findings if not x.path.startswith('commit ')}),
              rebase=False)
    return 'held'


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
        stale = host.recheck(f, number)
        if stale:
            lane.out(f'held {b}: PR #{number} not merged — {stale}')
            wait(lane, f, stale, 'held', state=WAITING_CI)
            continue
        if redaction_recheck(lane, f):
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
            # a trunk that moved under a green branch: the gate's conflict path, never a wait —
            # its green is kept, so a wait would merge (and be refused) again every pass
            # (a ``merge=union`` file merges clean here and conflicts on the host, which
            # applies no merge driver — the rebase is what clears it either way). The text
            # names it, or the host's ``mergeable`` does — or, when the host still reads
            # UNKNOWN (it computes it lazily after the trunk moves) and the refusal keeps only
            # gh's ``--auto`` hint, git does: a branch that does not merge onto the trunk is a
            # conflict whatever the host managed to say (2026-09-26: 93 refusals, 25 on one PR)
            files = conflict_files(lane.repo, lane.trunk, b)
            if files or MERGE_CONFLICT_RE.search(how or '') or host.conflicting(number):
                send_back(lane, f, 'conflict', f'PR #{number} merge refused — {how}'
                          + (f'; conflicts in {", ".join(files)}' if files else '')
                          + f' — rebase the branch onto origin/{lane.trunk} (git rebase '
                          f'origin/{lane.trunk}), never merge; the factory publishes the rebased '
                          f'branch', files)
                continue
            wait(lane, f, f'merge refused: {how}', 'held', green=f.get('green'))
            continue
        host.merged += 1
        rec = lane.record(f, MERGED, f'method={how}', sha=sha, method=how)
        lane.write(f, rec, harvested=sha, correction=None)
        f['prev'] = rec
        lane.out(f'landed {b} → PR #{number} {sha}{f.get("delivery_note") or ""}')
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

    def recheck(self, f, number):
        """Why PR ``number`` may not merge now after all (its checks read again right before
        MERGING), or None. No PR host: nothing to read."""
        return None

    def merge(self, branch, pr, subject=None):
        """Land it: ``(sha, method)`` with ``method`` one of ``ff|squash|merge|rebase|queue``, or
        a refusal ``(None, reason)``. ``subject``: the squash subject to write, when the lane
        names one (:func:`squash_subject`). The caller writes MERGING before calling."""
        raise NotImplementedError

    def conflicting(self, pr):
        """True when the host says PR ``pr`` conflicts with its base. No PR host: False."""
        return False

    def cancel_ci(self, branch):
        """Cancel the CI runs a merged PR still has going; how many were cancelled. No PR host,
        no runs: 0."""
        return 0

    def unmark_heavy(self, number, branch=None):
        """Take the heavy-CI label off PR ``number`` (``ci.heavy_after_review``). No PR host:
        nothing to take off."""
        return False

    def head_red(self, f, number):
        """The required checks that failed on PR ``number``'s exact head, before its review —
        ``{names, detail, checks}``, or None. No PR host: none."""
        return None


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
        self._attested = {}  # sha -> carries the merge queue's attestation (one read a pass)

    def prs(self):
        """One ``gh pr list --state all``: ``{branch: pr}``, an open PR first, else the newest.
        ``draft`` (``isDraft``): the owner parked it — the lane's one place this is read
        (:func:`next_state`'s Tp parks any open state on it)."""
        data = H.gh_json(['pr', 'list', '-R', self.slug, '--state', 'all', '--limit', '500',
                          '--json', 'number,headRefName,headRefOid,state,mergeCommit,'
                                    'autoMergeRequest,title,baseRefName,isDraft'], [])
        out = {}
        for p in sorted((p for p in data if isinstance(p, dict)),
                        key=lambda p: (p.get('state') == 'OPEN', int(p.get('number') or 0))):
            out[p.get('headRefName')] = {
                'number': p.get('number'), 'state': str(p.get('state') or 'OPEN').upper(),
                'head': p.get('headRefOid') or None,
                'merge_sha': (p.get('mergeCommit') or {}).get('oid') or None,
                'queued': bool(p.get('autoMergeRequest')), 'title': p.get('title') or '',
                'base': p.get('baseRefName') or None, 'draft': bool(p.get('isDraft'))}
        if self.lane is not None:
            self.in_queue = sum(1 for r in lifecycle.by_branch(self.lane.path).values()
                                if lifecycle.lane_of(r).get('state') == QUEUED)
        return out

    def open(self, branch, item=None):
        from asf.tick import step_prs
        items = (self.lane.items if self.lane else None) or {}
        root = self.lane.root if self.lane else None
        title, body = step_prs.title_and_body(item or '', items.get(item or '') or {}, root, branch,
                                              items=items)
        if self.product.conventions.branch_kind(branch) == DIRECT:
            note = first_commit_body(self.product.repo_dir, self.trunk, branch)
            if note:  # the direct session's spec+plan note: the PR description carries it
                body = f'{body}\n## What and how\n\n{note}\n'
        body += commits_note(self.product.repo_dir, self.trunk, branch)
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
        state, _detail, _checks = (pr_checks(self.slug, p.get('number'), required, self.rerun_ids(),
                                             head=p.get('headRefOid'),
                                             attest_context=attestation.context(self.product)) if p
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

    def merge_required(self, state_dir, cls, head=None):
        """``(names, why)``: the checks that must each conclude success on the PR head before it
        merges on its checks alone — :meth:`required_checks`, and for a code PR also the jobs
        that make a trunk sha deployable to prod (``deploy_sha.prod.required_jobs_from`` read at
        ``head``, else ``required_jobs``): a merge the deploy would refuse is not a merge.
        ``why`` is set (names None) when a ``required_jobs_from`` with no list behind it cannot
        be read."""
        from asf.harvest import deploy
        names = list(self.required_checks(state_dir))
        spec = deploy.required_from(self.product, 'prod')
        static = deploy._names(deploy.env_cfg(self.product, 'prod').get('required_jobs'))
        if cls != DOCS and (spec or static):
            jobs, src = deploy.required_jobs(self.product, 'prod', head)
            if jobs is None:
                return None, src
            if spec and src is None:
                # the file did not read at the head (not fetched, moved): its list at the trunk,
                # never the landing checks alone standing in for the deploy-required jobs
                at_trunk, src = deploy.required_jobs(self.product, 'prod', f'origin/{self.trunk}')
                if src:
                    jobs = list(at_trunk) + [j for j in static if j not in at_trunk]
                elif not static:
                    return None, (f'{spec[1]} unreadable in {spec[0]} at the PR head and at '
                                  f'{self.trunk}')
            names += [j for j in jobs if j not in names]
        return tuple(names), None

    def recheck(self, f, number):
        """``None`` when PR ``number`` may still merge, else why not: its checks read again right
        before MERGING, judged on the same required set as :meth:`check_gate` — a required check
        that went red (or, merging on its checks alone, is no longer green) while the pass ran
        never merges on the snapshot the pass started from."""
        lane = self.lane
        cls = f.get('class') or landing_class(self.product, f.get('files') or ())
        required, why = self.merge_required(lane.state_dir, cls, f.get('head'))
        if required is None:
            return f'required checks unknown: {why}'
        state, detail, checks = pr_checks(self.slug, number, required, self.rerun_ids(),
                                          head=exact_head(f),
                                          attest_context=attestation.context(self.product))
        if state == 'red' or (state != 'green' and f.get('how') == 'ci'):
            return f'required checks {state} at merge: {detail}'
        if f.get('on_checks') and required:
            _passed, skipped, missing, _ok, running = self.not_green(f, required, checks)
            if running:
                return f'required checks pending at merge: {running}'
            if skipped or missing:
                return f'required checks not green at merge: {", ".join(skipped + missing)}'
        return None

    def not_green(self, f, required, checks):
        """``(passed, skipped, missing, satisfied, running)`` for a PR whose required checks
        are none of them red or pending: the required names that concluded success, those
        skipped and those missing that still hold it, ``{name: why}`` of those the workflow
        itself skipped (:meth:`path_filtered`), and why its runs are not all done (or None)."""
        passed = passed_names(checks, required)
        skipped = [n for n in required if n not in passed and any(
            c.get('bucket') == SKIP_BUCKET and required_name(c.get('name'), (n,))
            for c in checks)]
        missing = [n for n in required if n not in passed and n not in skipped]
        satisfied, running = {}, None
        since, heavy = None, self.product.conventions.heavy_after_review()
        if heavy:
            rec = f.get('prev') or {}
            since = rec.get('heavy_at') if rec.get('heavy') == f.get('head') else None
        if (skipped or missing) and passed and (since or not heavy) and \
                merge_skipped(self.product.conventions) == MERGE_SKIPPED_PATH:
            satisfied, running = self.path_filtered(f.get('head'), skipped, missing, passed,
                                                    since=since)
        return (passed, [n for n in skipped if n not in satisfied],
                [n for n in missing if n not in satisfied], satisfied, running)

    def path_filtered(self, head, skipped, missing, passed, since=None):
        """``({name: why}, running)``: the ``skipped`` and ``missing`` required checks the PR's
        own workflow decided not to run — read off every GitHub Actions run on ``head`` (with
        ``since``, an ISO stamp — the head's heavy-CI approval under ``ci.heavy_after_review`` —
        only the runs created at or after it: a heavy job the workflow skipped because the head
        was not yet approved is not a job it decided not to run). Only
        once each run completed (``running`` names the rest, and nothing is satisfied): a skipped
        check is satisfied when each job answering for it concluded ``skipped`` (a job ``if:``
        false), a missing one when no completed run has a job of that name (a path filter that
        never created it). Unreadable runs or jobs satisfy nothing — the PR holds as before."""
        if not head:
            return {}, None
        data = H.gh_json(['api', f'repos/{self.slug}/actions/runs?head_sha={head}&per_page=100'],
                         None)
        runs = data.get('workflow_runs') if isinstance(data, dict) else None
        runs = [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []
        if since:
            floor = _parse_at(since)
            runs = [r for r in runs if floor is not None and (_parse_at(r.get('created_at'))
                                                              or 0) >= floor]
        if not runs:
            return {}, None
        open_ = [r for r in runs if r.get('status') != 'completed']
        if open_:
            names = ', '.join(sorted({str(r.get('name') or r.get('id')) for r in open_}))
            return {}, f'workflow run(s) on the head not completed — {names}'
        jobs = []
        for r in runs:
            got = H.gh_json(['api', f'repos/{self.slug}/actions/runs/{r.get("id")}/jobs'
                                    f'?per_page=100&filter=latest'], None)
            listed = got.get('jobs') if isinstance(got, dict) else None
            if not isinstance(listed, list):
                return {}, None
            jobs += [j for j in listed if isinstance(j, dict)]
        gate = ', '.join(sorted(passed))
        out = {}
        for n in skipped:
            mine = [j for j in jobs if required_name(j.get('name'), (n,))]
            if mine and all(j.get('conclusion') == 'skipped' for j in mine):
                out[n] = (f'{n} skipped by the workflow — satisfied (run completed, {gate} '
                          f'success)')
        for n in missing:
            if not any(required_name(j.get('name'), (n,)) for j in jobs):
                out[n] = (f'{n} not created by the workflow — satisfied (run completed, {gate} '
                          f'success)')
        return out, None

    def rerun_ids(self):
        """The runs the CI queue cancelled and holds to re-run: their cancelled checks wait."""
        from asf import ci_queue
        return ci_queue.rerun_ids(self.lane.state_dir) if self.lane else frozenset()

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

    def conflicting(self, pr):
        """True when GitHub reads PR ``pr`` as ``mergeable: CONFLICTING``. A refused merge's
        text keeps only gh's last line — the ``--auto`` hint, not the ``is not mergeable`` above
        it — so the PR's own state is what names a conflict. Unreadable or UNKNOWN: False."""
        got = H.gh_json(['pr', 'view', str(pr), '-R', self.slug, '--json', 'mergeable'], {})
        return isinstance(got, dict) and got.get('mergeable') == 'CONFLICTING'

    def cancel_ci(self, branch):
        """Cancel ``branch``'s ``pull_request`` CI runs that have not finished. Called once its PR
        merged: the lane lands on the required checks alone, so the rest of the PR's matrix is
        still queued or running on the runner pool, judging a head the trunk's own run now
        judges again — moot work that holds runners the open PRs and the trunk are queued for.
        Never raises: an unreadable list or a refused cancel counts as nothing cancelled."""
        try:
            runs = H.gh_json(['run', 'list', '-R', self.slug, '--branch', branch,
                              '--event', 'pull_request', '--limit', '20',
                              '--json', 'databaseId,status,headSha'], [])
            n = 0
            for r in runs if isinstance(runs, list) else ():
                if not isinstance(r, dict) or r.get('status') == 'completed':
                    continue
                if r.get('databaseId') and run_cancel.cancel(
                        _ok_call, self.slug, r['databaseId'], status=r.get('status')):
                    n += 1
                    if self.lane is not None:
                        from asf import ci_queue
                        self.lane.out(
                            f"harvest: cancelled run {r['databaseId']} on {branch} at "
                            f"{str(r.get('headSha') or '?')[:9]} — its PR merged; replaced by "
                            f"the trunk's run of the merge")
                        ci_queue.claim_cancel(self.lane.state_dir, r['databaseId'], 'merged-pr')
            return n
        except OSError:
            return 0

    def mark_heavy(self, number):
        """``(ok, why)``: put :meth:`asf.conventions.Conventions.heavy_label` on PR ``number``,
        creating the label in the repo the first time it is missing."""
        label = self.product.conventions.heavy_label()
        rc, out, err = H._gh(['pr', 'edit', str(number), '-R', self.slug, '--add-label', label])
        if rc != 0 and 'not found' in f'{out}\n{err}'.lower():
            H._gh(['label', 'create', label, '-R', self.slug, '--color', HEAVY_LABEL_COLOR,
                   '--description', HEAVY_LABEL_DESCRIPTION])
            rc, out, err = H._gh(['pr', 'edit', str(number), '-R', self.slug,
                                  '--add-label', label])
        if rc != 0:
            return False, H.tail(err or out) or f'gh pr edit exited {rc}'
        return True, ''

    def head_red(self, f, number):
        """``{names, detail, checks}``: the required checks that concluded failure on PR
        ``number``'s exact head (:func:`exact_head`, runs from every event — a CI-queue dispatched
        run counts), not red on the trunk too, not a sign-off (the gate repairs that) — or None.
        A red head is a correct round against those failures, never another review or a rerun of
        the item's delivery job (a product's T-0042, 2026-09-30: unparked on a head whose
        dispatched run failed two suites, its row was a sixth review). Only ``fail`` counts: a
        cancelled check is no defect of the code. Unreadable: None — the review goes on."""
        head = exact_head(f)
        if not head or not number:
            return None
        cls = f.get('class') or landing_class(self.product, f.get('files') or ())
        required, _why = self.merge_required(self.lane.state_dir, cls, head)
        if required is None:
            return None
        state, _detail, checks = pr_checks(self.slug, number, required, self.rerun_ids(),
                                           head=head,
                                           attest_context=attestation.context(self.product))
        from asf import flake
        out = self.lane.out if callable(getattr(self.lane, 'out', None)) else print
        flake.settle(self.product, self.lane.state_dir, self.slug, head, checks, out=out)
        if state != 'red':
            return None
        conv = self.product.conventions
        failed = [c.get('name') or '?' for c in checks if c.get('bucket') == 'fail'
                  and (not required or required_name(c.get('name'), required))
                  and not conv.is_signoff_check(c.get('name') or '')]
        failed = list(dict.fromkeys(failed))
        on_trunk = self.trunk_red(failed) if failed else {}
        names = [n for n in failed if n not in on_trunk]
        if not names:
            return None
        red = [c for c in checks if c.get('bucket') == 'fail' and c.get('name') in names]
        # first: a red judged on a merge ref of an older trunk is no verdict — never a correct
        # round, never a re-run (it would replay the old ref): a fresh run (asf.stale_ref)
        if self.stale_red(f, number, head, red):
            return None
        # cut short by a cancel (relief, a superseding run, a runner kill): no verdict — re-run
        # or waited on, never a correct round
        if self.cut_short(f, number, red, names, hold=False):
            return None
        # flake-vs-defect triage (asf.flake): a red job is re-run once on this head before any
        # correct round; green on its re-run is a flake (quarantined), red again a defect
        names, _held = flake.triage(self.product, self.lane.state_dir, self.slug, head, red,
                                    where=f'PR #{number}', out=out)
        if not names:
            return None
        # a real red: recorded for the trunk red watch (asf.trunk_red); a check it reads as red
        # on the trunk itself is not this head's, and goes to no correct round
        trunk = self.trunk_red_seen(f, number, head, [c for c in red if c.get('name') in names])
        if trunk:
            out(f"waiting {f['branch']}: PR #{number} checks red: {', '.join(names)} — trunk red "
                f"({', '.join(sorted(trunk))}), not its fault")
            names = [n for n in names if n not in trunk]
            if not names:
                return None
        return {'names': names, 'detail': ', '.join(names), 'checks': checks}

    def stale_red(self, f, number, head, red):
        """True when PR ``number``'s ``red`` checks ran on a merge ref from before the trunk's
        tip arrived and a fresh run was started for it, or is on its way (:mod:`asf.stale_ref`):
        the caller judges nothing this pass. Never raises."""
        from asf import stale_ref
        repo = getattr(self.lane, 'repo', None)
        if not red or not repo:
            return False
        out = self.lane.out if callable(getattr(self.lane, 'out', None)) else print
        try:
            tip = H.sh(['git', 'rev-parse', f'origin/{self.trunk}'], cwd=repo).stdout.strip()
            old = stale_ref.stale(self.product, self.slug, red, tip, repo)
            if not old or not stale_ref.refresh(self.product, self.slug, number, head, tip, old,
                                                out=out):
                return False
        except Exception:  # noqa: BLE001 — no reading: judged as before
            return False
        out(f"waiting {f['branch']}: PR #{number} red on {', '.join(old)} ran on a merge ref "
            f"from before {self.trunk} moved to {tip[:9]} — a fresh run, never a correct round")
        return True

    def trunk_red_seen(self, f, number, head, red):
        """Record PR ``number``'s red ``head`` (its checks ``red``, after triage) with
        :func:`asf.trunk_red.observe` — its diff's files, the files the failing logs name, read
        once per head — and return :func:`asf.trunk_red.held` for those names. Never raises."""
        from asf import trunk_red
        names = [c.get('name') for c in red if c.get('name')]
        if not names or not head:
            return {}
        try:
            key = f'#{number}'
            if not trunk_red.known(self.product, key, head, names):
                from asf import merge_queue
                repo = getattr(self.lane, 'repo', None)
                base = H.sh(['git', 'rev-parse', f'origin/{self.trunk}'],
                            cwd=repo).stdout.strip() if repo else ''
                files = [l for l in H.sh(['git', 'diff', '--name-only', f'{base}...{head}'],
                                         cwd=repo).stdout.splitlines() if l.strip()] \
                    if repo and base else list(f.get('files') or ())
                found = merge_queue.failure_findings(self.slug, red)
                trunk_red.observe(self.product, names, key, [number], head, base, files,
                                  [p for item in found for p, _n, _t in item['paths']],
                                  {trunk_red.job(c.get('name')): c.get('link') for c in red})
            return trunk_red.held(self.product, names)
        except Exception:  # noqa: BLE001 — no reading: the head is judged as before
            return {}

    def unmark_heavy(self, number, branch=None):
        """Take the heavy-CI label off PR ``number`` — only under ``ci.heavy_after_review``;
        True when gh took it off. Never raises."""
        conv = self.product.conventions
        if not number or not conv.heavy_after_review():
            return False
        try:
            rc, _out, err = H._gh(['pr', 'edit', str(number), '-R', self.slug,
                                   '--remove-label', conv.heavy_label()])
        except OSError:
            return False
        if self.lane is not None:
            self.lane.out(f'harvest: {branch or "?"}: PR #{number} heavy CI approval withdrawn'
                          + ('' if rc == 0 else f' — label not removed: {H.tail(err)}'))
        return rc == 0

    def run_in_flight(self, head):
        """``(id, status)`` of a ``pull_request`` run of the product's ``ci.workflow`` (any
        workflow when unset, or when the listing carries no ``path``) on ``head`` that is not
        completed yet, or None — none, or unreadable (the label goes on as before)."""
        data = H.gh_json(['api', f'repos/{self.slug}/actions/runs?head_sha={head}'
                                 f'&event=pull_request&per_page=100'], None)
        runs = data.get('workflow_runs') if isinstance(data, dict) else None
        ci = self.product.ci if isinstance(getattr(self.product, 'ci', None), dict) else {}
        workflow = os.path.basename(str(ci.get('workflow') or ''))
        for r in runs if isinstance(runs, list) else ():
            if not isinstance(r, dict) or r.get('event', 'pull_request') != 'pull_request':
                continue
            if r.get('head_sha', head) != head or r.get('status') in (None, 'completed'):
                continue
            if workflow and r.get('path') and os.path.basename(str(r['path'])) != workflow:
                continue
            return r.get('id') or r.get('databaseId') or '?', r.get('status')
        return None

    def heavy_gate(self, f, number):
        """``ci.heavy_after_review``: None when PR ``number``'s head is approved for heavy CI
        already (its lane record's ``heavy`` is the head) or the product does not ask for it;
        else the head is approved now — :meth:`mark_heavy`, the record's ``heavy``/``heavy_at``
        written — and the PR waits: its heavy jobs start on the label, and no check of the head
        is judged before they could have. Never while a ``pull_request`` run of the head is
        still queued or in progress (:meth:`run_in_flight`): the labelled run is that run's twin
        in the head's concurrency group, and the host cancels the one in flight — a PR's first
        run cut short by the factory itself. The PR waits for it to end; the label goes on then."""
        lane, b, rec = self.lane, f['branch'], f.get('prev') or {}
        head = f.get('head')
        if not self.product.conventions.heavy_after_review() or not head:
            return None
        if rec.get('heavy') == head:
            return None
        flying = self.run_in_flight(head)
        if flying:
            rid, status = flying
            lane.out(f'waiting {b}: PR #{number} approved at {head[:9]} — heavy CI held until '
                     f'run {rid} on the head ends ({status}): the label would cancel it')
            wait(lane, f, f'heavy CI held: run {rid} on {head[:9]} is {status}',
                 state=WAITING_CI)
            return 'waiting'
        if lane.dry_run:
            lane.out(f'DRY: would approve {b} PR #{number} at {head[:9]} for heavy CI')
            lane.results[b] = 'dry'
            return 'dry'
        at = now_iso()  # before the label: the run the label starts is created after it
        ok, why = self.mark_heavy(number)
        if not ok:
            lane.out(f'waiting {b}: PR #{number} approved, heavy CI not requested — {why}')
            wait(lane, f, f'heavy CI not requested: {why}', state=WAITING_CI)
            return 'waiting'
        lane.out(f'harvest: {b}: PR #{number} approved at {head[:9]} — heavy CI requested '
                 f'({self.product.conventions.heavy_label()})')
        wait(lane, f, f'heavy CI requested at {head[:9]}', state=WAITING_CI, heavy=head,
             heavy_at=at)
        return 'waiting'

    def stalled(self, f, number):
        """A PR whose required checks are skipped or missing, and nothing will run them:
        ``'back'`` when its branch conflicts with the trunk — GitHub runs no ``pull_request``
        workflow on a conflicting PR, so its checks never come and a day's missing-check wait
        buys nothing: it goes back to be rebased (sent here). Else :meth:`heavy_kick`'s answer."""
        lane, b = self.lane, f['branch']
        files = conflict_files(lane.repo, lane.trunk, b) if lane.repo else []
        if files:
            if lane.dry_run:
                lane.out(f'DRY: would send {b} back: PR #{number} conflicts in {", ".join(files)}')
                lane.results[b] = 'dry'
                return 'back'
            send_back(lane, f, 'conflict', conflict_text(number, lane.trunk, files), files)
            return 'back'
        return self.heavy_kick(f, number)

    def heavy_kick(self, f, number):
        """``ci.heavy_after_review``: the head PR ``number`` was approved for heavy CI at
        (the record's ``heavy``/``heavy_at``), once :data:`HEAVY_KICK_S` passed with its required
        checks still skipped or missing, is looked at once: when no workflow run on it was created
        since the approval, the label started nothing (a stale test merge ref without the
        ``labeled`` trigger, an event GitHub dropped) and the lane dispatches the product's
        ``ci.workflow`` on the branch — a run on the head, created after the approval, which the
        gate reads like the labelled one. The head (the record's ``heavy_kick``, so it is looked at
        once) or None: not due, not approved, unreadable, or nothing to dispatch."""
        lane, b, rec, head = self.lane, f['branch'], f.get('prev') or {}, f.get('head')
        if not head or not self.product.conventions.heavy_after_review() \
                or rec.get('heavy') != head or rec.get('heavy_kick') == head:
            return None
        at = _parse_at(rec.get('heavy_at'))
        now = lane.now or time.time()
        if at is None or now - at < HEAVY_KICK_S:
            return None
        data = H.gh_json(['api', f'repos/{self.slug}/actions/runs?head_sha={head}&per_page=100'],
                         None)
        runs = data.get('workflow_runs') if isinstance(data, dict) else None
        if not isinstance(runs, list):
            return None
        if any(isinstance(r, dict) and (_parse_at(r.get('created_at')) or 0) >= at
               for r in runs):
            return head  # the approval started a run: the queue and the checks own it from here
        ci = self.product.ci if isinstance(getattr(self.product, 'ci', None), dict) else {}
        workflow = ci.get('workflow')
        if not workflow:
            return None
        mins = int((now - at) // 60)
        if lane.dry_run:
            lane.out(f'DRY: would dispatch {workflow} on {b}: PR #{number} approved at '
                     f'{head[:9]} {mins} min ago, no run since')
            return None
        rc, out, err = H._gh(['workflow', 'run', workflow, '--ref', b, '-R', self.slug])
        if rc != 0:
            lane.out(f'harvest: {b}: PR #{number} no heavy run {mins} min after its approval — '
                     f'dispatch of {workflow} refused: {H.tail(err or out)}')
            return None
        lane.out(f'harvest: {b}: PR #{number} no run on {head[:9]} {mins} min after its heavy-CI '
                 f'approval — the label started nothing; dispatched {workflow} on the head')
        return head

    def cut_short(self, f, number, red_checks, red, hold=True):
        """True (and the branch waits) when every red required check in ``red_checks`` was cut
        short, not failed: its job lost its run under a cancel — CI-queue relief, a superseding
        run, a runner kill — and reads ``failure`` with the host's runner-loss annotation
        (:func:`asf.flake.infra_red`: "The operation was canceled."). It judged no code: re-run
        (:func:`asf.flake.triage`) or waited on, never a correct round — a session sent to
        correct a head nothing is wrong with ends empty and parks the Task (2026-10-05)."""
        from asf import flake
        lane, b = self.lane, f['branch']
        if not red or not red_checks:
            return False
        lost = [c for c in red_checks
                if flake.infra_red(self.slug, flake._ids(c.get('link'))[1], H._gh)]
        if {c.get('name') for c in lost} != set(red):
            return False
        head = exact_head(f) or f.get('head')
        # the re-run is the triage's (once per job, the infra budget); off or refused, the
        # branch still waits — a cut-short check is never a defect
        _defects, held = flake.triage(self.product, lane.state_dir, self.slug, head, lost,
                                      where=f'PR #{number}', out=lane.out, gh=H._gh)
        names = ', '.join(red)
        lane.out(f'waiting {b}: PR #{number} checks {names} cut short (cancelled, no verdict) — '
                 f'{"re-run" if held else "awaiting a re-run"}, never a correct round')
        if hold:   # ``hold`` False: the caller (:meth:`head_red`) only asks whether it is red
            wait(lane, f, f'checks pending: {names} (cancelled, awaiting a re-run)',
                 state=WAITING_CI)
        return True

    def check_gate(self, f, number, files):
        """The PR's checks before the gate: ``'gate'`` (gate it locally), ``'ci'`` (its required
        checks passed under ``wait``), or None when it waits or went back. Under
        ``ci.heavy_after_review`` a head not yet approved for heavy CI is approved first
        (:meth:`heavy_gate`) and waits — a code PR only: a docs PR's required checks are the
        landing checks alone, which no heavy job answers for, so a label would buy a heavy run
        nothing reads."""
        lane, b, rec = self.lane, f['branch'], f.get('prev') or {}
        cls = f.get('class') or landing_class(self.product, files)
        if cls != DOCS and self.heavy_gate(f, number):
            return None
        required, why = self.merge_required(lane.state_dir, cls, f.get('head'))
        if required is None:
            lane.out(f'waiting {b}: PR #{number} required checks unknown — {why}')
            wait(lane, f, f'required checks unknown: {why}', state=WAITING_CI)
            return None
        state, detail, checks = pr_checks(self.slug, number, required, self.rerun_ids(),
                                          head=exact_head(f),
                                          attest_context=attestation.context(self.product))
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
                   and (not required or required_name(c.get('name'), required))]
            conv = self.product.conventions
            unsigned = [n for n in red if conv.is_signoff_check(n)]
            if unsigned and lane.repair_signoff(f, unsigned[0]) is not None:
                return None  # a new head: its checks run again, next pass reads them
            if self.stale_red(f, number, exact_head(f) or f.get('head'),
                              [c for c in checks if c.get('bucket') in RED_BUCKETS
                               and c.get('name') in red]):
                wait(lane, f, f'checks red on a stale merge ref: {detail} — a fresh run')
                return None
            if self.cut_short(f, number, [c for c in checks if c.get('bucket') in RED_BUCKETS
                                          and c.get('name') in red], red):
                return None
            on_trunk = self.trunk_red(red)
            try:
                from asf import trunk_red
                on_trunk = {**trunk_red.held(self.product, red), **on_trunk}
            except Exception:  # noqa: BLE001 — no reading: the host's alone
                pass
            if red and all(n in on_trunk for n in red):
                lane.out(f'waiting {b}: PR #{number} checks red: {detail} — red on {self.trunk} '
                         f'too, not its fault; it lands once {self.trunk} is green')
                wait(lane, f, f'trunk-red: {", ".join(red)}')
                return None
            if lane.dry_run:
                lane.out(f'DRY: would hold {b}: PR #{number} checks red: {detail}')
                lane.results[b] = 'dry'
                return None
            send_back(lane, f, 'gate', f'PR #{number} checks red: {detail}'
                      + red_evidence(self.slug, checks, red), ())
            return None
        passed = passed_names(checks, required)
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
        if missing_policy(self.product.conventions, cls) == MISSING_LOCAL_GATE:
            return 'gate'
        if not required:
            return 'gate'
        _passed, skipped, missing, satisfied, running = self.not_green(f, required, checks)
        now = lane.now or time.time()
        since = rec.get('since') if rec.get('state') == WAITING_CI and rec.get('since') else now
        if running:
            # a path-filtered PR whose runs are not done yet: a check not created so far may still
            # come — it waits, and never reaches the local-gate clock from here
            lane.out(f'waiting {b}: PR #{number} required check(s) skipped or not run — '
                     f'{running}')
            wait(lane, f, f'checks pending: {running}', state=WAITING_CI, since=since)
            return None
        kick = {}
        if skipped or missing:
            stalled = self.stalled(f, number)
            if stalled == 'back':
                return None
            if stalled:
                kick = {'heavy_kick': stalled}
        for why in satisfied.values():
            lane.out(f'harvest: {b}: PR #{number} {why}')
        if skipped:
            # a required check that ran nothing is not green, and no clock turns it into a local
            # gate: it holds until a run of it concludes success on the head, or (merge_skipped:
            # path-filtered) its completed workflow skipped it beside a required success
            lane.out(f'held {b}: PR #{number} required check(s) skipped, not green — '
                     f'{", ".join(skipped)}')
            wait(lane, f, f'required checks skipped: {", ".join(skipped)}', state=WAITING_CI,
                 **kick)
            return None
        if not missing:
            f['on_checks'] = True  # :meth:`recheck` judges the same skips again before MERGING
            return 'ci'
        waited, limit = now - float(since), landing_wait_s(self.product.conventions)
        if waited < limit:
            lane.out(f'waiting {b}: PR #{number} required check(s) not run — {", ".join(missing)} '
                     f'(landing_checks_missing: wait, {int(waited // 60)}/{int(limit // 60)} min)')
            wait(lane, f, f'required checks missing: {", ".join(missing)}', state=WAITING_CI,
                 since=since, **kick)
            return None
        lane.out(f'harvest: {b}: PR #{number} required check(s) never ran in {int(limit // 60)} '
                 f'min — {", ".join(missing)}: gating locally')
        return 'gate'


    def attested(self, sha):
        """True when trunk ``sha`` carries the merge queue's attestation
        (:func:`asf.attestation.product_attested`), read once a pass."""
        if sha not in self._attested:
            self._attested[sha] = attestation.attested(
                self.slug, sha, attestation.context(self.product))
        return self._attested[sha]

    def trunk_red(self, names):
        """``{name: sha}`` — each of ``names`` with a completed run on the trunk that failed or timed
        out (a cancelled run judges nothing), with the commit that run judged. The trunk's first-parent history
        is walked newest first (at most :data:`TRUNK_RED_DEPTH` commits) until every name has a
        completed run; a check still running on the newest commit is judged by the one before. A
        sha with more than one completed run for a name (a rerun) is red if any of them is —
        a retry that flipped a failure to green is flaky, not clean (B-0129), and the flip does
        not erase the failure it needed to overturn. Unreadable (no ``gh``, no access) reads as
        not red — the PR's own checks and the gate still judge it, as before. A check that
        only ``skipped`` judges by the attestation (:mod:`asf.attestation`): on a sha the merge
        queue attested it is green there (the batch run judged that exact sha), on any other it
        is no verdict and the commit before judges; where the attestation does not read
        (Unknown) the check has no verdict at all — never the older commit's red."""
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
                        and r.get('status') == 'completed'
                        and r.get('conclusion') != 'cancelled']
                if not done:
                    continue
                if all(r.get('conclusion') in attestation.ATTESTED_CONCLUSIONS for r in done):
                    att = self.attested(sha)
                    if att is None:
                        left.discard(name)  # attestation Unknown: no verdict, never an older red
                        continue
                    if not att:
                        continue  # skipped on a sha nobody attested: the one before judges
                left.discard(name)
                if any(r.get('conclusion') in TRUNK_RED_CONCLUSIONS for r in done):
                    red[name] = sha
            if not left:
                break
        return {n: red[n] for n in want if n in red}


def exact_head(f):
    """The sha whose check runs judge ``f``'s PR: the branch head, when the PR's head is that
    sha (or unknown) — never a run of another commit."""
    head = f.get('head')
    return head if head and (f.get('pr') or {}).get('head') in (None, '', head) else None


#: the run id in a check's link (``…/actions/runs/<id>/job/<id>``)
_RUN_RE = re.compile(r'/actions/runs/(\d+)')


#: a check run's ``conclusion`` → the ``gh pr checks`` bucket it reads as (anything else that
#: completed — ``failure``, ``timed_out``, ``action_required``, ``startup_failure`` — is ``fail``)
_RUN_BUCKETS = {'success': 'pass', 'skipped': 'skipping', 'neutral': 'skipping',
                'cancelled': 'cancel', 'stale': 'pending'}


def head_runs(slug, sha, rollup=()):
    """The check runs on commit ``sha`` from every event (``commits/<sha>/check-runs``), as
    ``gh pr checks`` rows (``name, bucket, link, workflow, startedAt``). The PR's rollup shows only
    its own events' runs: a ``workflow_dispatch`` run the CI queue started on the head (#321) is
    never in it — green, it could never let the PR merge; red, it was invisible (a product's PR
    #902, 2026-09-30: the rollup still read the cancelled ``pull_request`` run). A run's workflow
    is its twin's in ``rollup`` (same run id, else same check name) — the dispatched workflow
    carries the same job names — so :func:`latest_checks` sets one against the other. ``[]``
    when unreadable."""
    data = H.gh_json(['api', f'repos/{slug}/commits/{sha}/check-runs?per_page=100'], None)
    runs = data.get('check_runs') if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return []
    by_run, by_name = {}, {}
    for c in rollup or ():
        m = _RUN_RE.search(c.get('link') or '')
        if m:
            by_run.setdefault(m.group(1), c.get('workflow') or '')
        by_name.setdefault(c.get('name') or '', c.get('workflow') or '')
    out = []
    for r in runs:
        if not isinstance(r, dict) or not r.get('name'):
            continue
        link = r.get('html_url') or ''
        m = _RUN_RE.search(link)
        done = r.get('status') == 'completed'
        bucket = _RUN_BUCKETS.get(r.get('conclusion'), 'fail') if done else 'pending'
        workflow = by_run.get(m.group(1)) if m and m.group(1) in by_run else \
            by_name.get(r['name'], '')
        out.append({'name': r['name'], 'bucket': bucket, 'link': link, 'workflow': workflow,
                    'startedAt': r.get('started_at') or ''})
    return out


def pr_checks(slug, number, required=(), rerun=(), head=None,
              attest_context=attestation.CONTEXT):
    """``('green'|'pending'|'red'|'unknown', detail, checks)`` for PR ``number``'s checks. No
    checks at all is green. With ``required`` names (``landing_checks`` or branch protection) only
    those judge: a failed required one is red (a cancelled one is pending, never red), else an unfinished required one is
    pending — a red check the product does not require (a DCO bot, an advisory test job) never
    turns the PR red. With none required, any failed check is red, else any not
    finished is pending. ``checks`` is always the full list.

    ``rerun``: ids of runs the CI queue cancelled and holds to re-run
    (:func:`asf.ci_queue.rerun_ids`) — a cancelled check of one is pending, never red: a
    product's B-1377 (2026-09-26) was held on "checks red: gate, gate-tests" the trunk relief
    had cancelled, and another session was launched on a head nothing was wrong with.

    ``head``: the PR's exact head sha — its check runs from every event (:func:`head_runs`) join
    the rollup's, the newest run per workflow and name judging (a CI-queue dispatched run on the
    head counts, green or red; a run on any other sha never does).

    ``attest_context``: a required check that only ``skipped`` on ``head`` passes when ``head``
    carries that status = ``success`` (:mod:`asf.attestation`) — a trunk sha the merge queue
    attested, whose push run skipped the heavy jobs the batch run already passed there. Read
    only when such a check is there; any other bucket is judged as before."""
    rollup = pr_graph.checks_for(slug, number, head)  # one query a tick for every open PR
    if rollup is None:  # the snapshot cannot vouch for this PR: read it the REST way
        rc, stdout, err = H._gh(['pr', 'checks', str(number), '-R', slug, '--json',
                                 'name,bucket,state,link,workflow,startedAt'])
        none = 'no checks reported' in f'{stdout}\n{err}'
        try:
            rollup = [] if none else [c for c in json.loads(stdout) if isinstance(c, dict)]
            for c in rollup:  # `gh` lists a cancelled run's bucket as fail on some versions
                if c.get('bucket') == 'fail' and str(c.get('state') or '').upper() in (
                        'CANCELLED', 'CANCELED'):
                    c['bucket'] = CANCEL_BUCKET
        except (json.JSONDecodeError, TypeError):
            return 'unknown', H.tail(err) or f'gh pr checks exited {rc}', []
        if head:
            seen = {c.get('link') for c in rollup if c.get('link')}
            rollup += [c for c in head_runs(slug, head, rollup) if c['link'] not in seen]
    if not rollup:
        return 'green', 'no checks', []
    checks = latest_checks(rollup)
    judged = ([c for c in checks if required_name(c.get('name'), required)] if required
              else checks)
    skipped = [c for c in judged if c.get('bucket') == SKIP_BUCKET] if required and head else []
    if skipped and attestation.attested(slug, head, attest_context):
        ids = {id(c) for c in skipped}  # copies: the rollup snapshot is never written
        checks = [dict(c, bucket=PASS_BUCKETS[0], attested=attest_context) if id(c) in ids
                  else c for c in checks]
    held = {str(i) for i in rerun or ()}

    def rerun_of(c):
        m = _RUN_RE.search(c.get('link') or '')
        return c.get('bucket') in (CANCEL_BUCKET, *RED_BUCKETS) and bool(m) and m.group(1) in held

    # a run the CI queue cancelled and holds to re-run judged no code, whatever its jobs read:
    # a job cut mid-step reads ``failure`` ("The operation was canceled.") — never red
    red = [c.get('name') or '?' for c in judged if c.get('bucket') in RED_BUCKETS
           and not rerun_of(c)]
    if red:
        return 'red', ', '.join(red), checks
    pending = [c.get('name') or '?' for c in judged if c.get('bucket') == 'pending']
    pending += [f"{c.get('name') or '?'} (cancelled by the CI queue, re-run queued)"
                for c in judged if rerun_of(c)]
    pending += [f"{c.get('name') or '?'} (cancelled, awaiting a re-run)"
                for c in judged if c.get('bucket') == CANCEL_BUCKET and not rerun_of(c)]
    if pending:
        return 'pending', ', '.join(pending), checks
    return 'green', f'{len(checks)} check(s)', checks


def latest_checks(checks):
    """One check per workflow and name — the newest run of it: a head with two runs of one
    workflow (``ci.heavy_after_review``'s labelled run beside the push's light one, a run the
    concurrency group cancelled beside the one that replaced it) is judged on the later, never on
    a stale ``skipped`` or ``cancelled`` twin. A check not started yet (no ``startedAt``) is the
    newest. The list's order is kept."""
    def when(c):
        at = str(c.get('startedAt') or '')
        return '9999' if not at or at.startswith('0001') else at
    best = {}
    for i, c in enumerate(checks):
        key = (c.get('workflow') or '', c.get('name') or '')
        if key not in best or when(c) >= when(checks[best[key]]):
            best[key] = i
    keep = set(best.values())
    return [c for i, c in enumerate(checks) if i in keep]


#: The last lines of a red CI job's log a correction carries, up to and with its error line.
RED_LOG_LINES = 40
_JOB_RE = re.compile(r'/job/(\d+)')
_LOG_TS_RE = re.compile(r'^\ufeff?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?')
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')


def red_log_lines(log, n=RED_LOG_LINES):
    """The last ``n`` lines of a CI job ``log`` up to and with its first ``##[error]`` line (or
    its end): the failing step's own output — after the step's header group (its command and
    env) — with timestamps, colours and the runner's ``##[…]`` markers stripped."""
    lines = [_ANSI_RE.sub('', _LOG_TS_RE.sub('', l)).rstrip() for l in (log or '').splitlines()]
    end = next((i for i, l in enumerate(lines) if l.startswith('##[error]')), len(lines) - 1)
    start = max([i + 1 for i, l in enumerate(lines[:end]) if l.startswith('##[endgroup]')] + [0])
    kept = [l.replace('##[error]', '', 1) for l in lines[start:end + 1]
            if l.strip() and not l.startswith(('##[group]', '##[endgroup]'))]
    return kept[-n:]


#: a failing test's line in a test runner's log: node:test / vitest / jest marks (``✖``, ``✗``,
#: ``×``), TAP ``not ok N - name``, jest / vitest ``FAIL <file> [> name]``
_FAIL_RE = re.compile(r'^\s*(?:[\u2716\u2717\u00d7\u2718]\s+(?P<a>.+)|not ok \d+\s*-?\s*(?P<b>.+)'
                      r'|FAIL\s+(?P<c>\S.*))$')
_FAIL_SKIP = re.compile(r'^(failing tests?|failed tests?|tests? failed|\d+ failed)\b', re.I)
_DURATION_RE = re.compile(r'\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$')
RED_TESTS_MAX = 20


def red_tests(log):
    """The failing test names a job ``log`` names, in order, each once: node:test ``✖ name``,
    vitest / jest ``✗`` / ``×`` / ``FAIL file > name``, TAP ``not ok N - name`` (a trailing
    duration and a ``# SKIP`` / ``# TODO`` directive stripped)."""
    out = []
    for raw in (log or '').splitlines():
        line = _ANSI_RE.sub('', _LOG_TS_RE.sub('', raw)).rstrip()
        m = _FAIL_RE.match(line)
        if not m:
            continue
        name = next(g for g in m.group('a', 'b', 'c') if g).strip()
        if re.search(r'#\s*(SKIP|TODO)\b', name, re.I):
            continue
        name = _DURATION_RE.sub('', name).strip()
        if name and not _FAIL_SKIP.match(name) and name not in out:
            out.append(name)
    return out[:RED_TESTS_MAX]


def _ok_call(args):
    """:func:`asf.harvest.harvest._gh` as ``(ok, stdout)`` — the shape :mod:`asf.run_cancel`
    takes."""
    rc, out, _err = H._gh(args)
    return rc == 0, out or ''


def _gh_json(args):
    try:
        rc, out, _err = H._gh(args)
        return json.loads(out) if rc == 0 and out.strip() else None
    except (OSError, ValueError):
        return None


def red_step(slug, job_id):
    """``(step name, run command)`` of the failing step of job ``job_id``: the name from the job's
    own steps, the ``run:`` read from the workflow file at the job's head (the run's ``path``,
    the workflow job named as the check is, its step named as the failing one). Either is None
    when it cannot be read — never raises."""
    job = _gh_json(['api', f'repos/{slug}/actions/jobs/{job_id}'])
    if not isinstance(job, dict):
        return None, None
    step = next((s.get('name') for s in job.get('steps') or ()
                 if isinstance(s, dict) and s.get('conclusion') == 'failure'), None)
    if not step:
        return None, None
    run = _gh_json(['api', f"repos/{slug}/actions/runs/{job.get('run_id')}"])
    path = str((run or {}).get('path') or '').split('@', 1)[0]
    if not path or not job.get('head_sha'):
        return step, None
    try:
        rc, text, _e = H._gh(['api', '-H', 'Accept: application/vnd.github.raw',
                              f"repos/{slug}/contents/{path}?ref={job['head_sha']}"])
        return step, (workflow_step_run(text, job.get('name'), step) if rc == 0 else None)
    except Exception:
        return step, None


def _unquote(v):
    v = v.strip()
    return v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in '"\'' else v


def workflow_step_run(text, job_name, step_name):
    """The ``run:`` of the step named ``step_name`` in workflow ``text``, preferring the job whose
    key or ``name:`` is ``job_name`` — read line by line (the standard library has no YAML), a
    block scalar (``|`` / ``>``) dedented, else the inline value; None when no such step runs a
    command."""
    lines = (text or '').splitlines()
    found, job, jobs_at = [], None, None
    for i, l in enumerate(lines):
        ind = len(l) - len(l.lstrip())
        if l.strip() == 'jobs:' and ind == 0:
            jobs_at = i
        elif jobs_at is not None and ind == 2 and re.match(r'[\w.-]+:\s*$', l.strip()):
            job = {'key': l.strip()[:-1], 'name': None}
        elif job and ind == 4 and l.strip().startswith('name:'):
            job['name'] = _unquote(l.strip()[5:])
        m = re.match(r'(\s*)-\s+name:\s*(.+)$', l)
        if not m or _unquote(m.group(2)) != step_name:
            continue
        dash = len(m.group(1))
        run = None
        for j in range(i + 1, len(lines) + 1):
            if j == len(lines) or (lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) <= dash):
                break
            k = re.match(r'\s*run:\s*(.*)$', lines[j])
            if not k:
                continue
            kind, body = k.group(1).strip(), []
            if kind[:1] in '|>' and kind:
                keyind = len(lines[j]) - len(lines[j].lstrip())
                for t in lines[j + 1:]:
                    if t.strip() and len(t) - len(t.lstrip()) <= keyind:
                        break
                    body.append(t)
                width = min([len(t) - len(t.lstrip()) for t in body if t.strip()] or [0])
                run = '\n'.join(t[width:].rstrip() for t in body).strip()
            else:
                run = _unquote(kind)
            break
        if run:
            mine = job and job_name in (job['key'], job['name'])
            found.append((not mine, run))
    return sorted(found)[0][1] if found else None


def red_brief(check, step, tests, cmd):
    """The correct brief's block for one red check: the failing step, the failing test names, the
    command that reproduces the step locally (when the workflow's ``run:`` could be read), and
    the rule — nothing is pushed until that and ``pre_push_check`` pass locally."""
    out = [f"Failing step ({check}): {step or 'not read from the job'}"]
    if tests:
        out.append('Failing tests:' + ''.join(f'\n  - {t}' for t in tests))
    if cmd:
        out.append('Reproduce this step locally:\n' + '\n'.join(f'  {l}' for l in cmd.splitlines()))
    out.append("Do not push until " + ('that reproduction and ' if cmd else '')
               + "the product's pre_push_check pass locally.")
    return '\n'.join(out)


def red_evidence(slug, checks, red):
    """What the session needs to answer a red CI check (a product, 2026-09-26: five rounds on
    ``PR #795 checks red: gate`` alone, none told which lint failed where): per ``red`` check,
    its link and the lines its job's log ended on (:func:`red_log_lines`), then the correct
    brief (:func:`red_brief`): the failing step, the failing tests (:func:`red_tests`), the
    command that reproduces the step (:func:`red_step`) and the rule not to push before it
    and the ``pre_push_check`` pass. ``''`` when no red check has a job link or no log reads —
    never raises."""
    out = []
    for c in checks or ():
        link = c.get('link') or ''
        m = _JOB_RE.search(link)
        if c.get('name') not in red or not m:
            continue
        try:
            rc, log, _err = H._gh(['api', f'repos/{slug}/actions/jobs/{m.group(1)}/logs'])
        except OSError:
            continue
        lines = red_log_lines(log) if rc == 0 else []
        if lines:
            step, cmd = red_step(slug, m.group(1))
            tests = red_tests(log)
            block = ''
            if step or tests:
                block = '\n' + red_brief(c.get('name'), step, tests, cmd)
            out.append(f"\n{c.get('name')}: {link}\n" + '\n'.join(lines) + block)
    return ''.join(out)


def required_name(name, required):
    """The name in ``required`` that check ``name`` answers for — itself, or its job name up to
    the first space (a matrix leg ``e2e (shard 1)`` answers for ``e2e``) — else None."""
    name = str(name or '')
    if name in required:
        return name
    key = name.split(' ', 1)[0]
    return key if key in required else None


def passed_names(checks, required=()):
    """The check names that passed; with ``required``, the required names every one of whose
    checks (each matrix leg) concluded success."""
    if not required:
        return {c.get('name') for c in checks if c.get('bucket') in PASS_BUCKETS}
    seen = {}
    for c in checks:
        n = required_name(c.get('name'), required)
        if n:
            seen.setdefault(n, []).append(c.get('bucket'))
    return {n for n, b in seen.items() if b and all(x in PASS_BUCKETS for x in b)}


def not_required_red(checks, required):
    """The names of ``checks`` that failed or were cancelled but are not ``required`` — told, never
    acted on. Empty when nothing is required: then every red check already counts."""
    if not required:
        return []
    return [c.get('name') or '?' for c in checks
            if c.get('bucket') in RED_BUCKETS and not required_name(c.get('name'), required)]


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
        rec = lifecycle.lane_of(run)
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
