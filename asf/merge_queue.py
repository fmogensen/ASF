"""asf.merge_queue — ``conventions.merge: queue``: the lane's serialized merge queue.

**The invariant.** Nothing reaches the trunk unless *that exact sha* has green required checks.
A direct ``gh pr merge`` cannot promise it: the merge commit is a sha nobody gated, made of a
head whose checks ran against an older trunk. Under ``merge: queue`` the lane instead

1. **cuts a batch** — the green, approved PRs :func:`asf.harvest.lane.gate_set` hands it, at
   most ``merge_queue.batch_size`` of them, each merged ``--no-ff`` onto the trunk's tip in a
   throwaway worktree (a PR that does not merge goes BACK through the gate's own conflict path,
   naming the member it collides with; the batch goes on without it) and pushed once as
   ``<merge_queue.ref_prefix><stamp>`` — a ref the product's CI runs its full matrix on. Every
   member is QUEUED with the batch's ref and sha on its lane record;
2. **judges it** every harvest, in this order: a member whose head moved or left the queue
   drops the batch (the moved member waits, the rest are cut again); a batch ref gone or moved
   on origin drops it; a trunk tip the batch no longer contains drops it — the members are cut
   again on the new tip, and the old sha, green or not, is never pushed; then the required
   checks at the batch sha decide: **every** required check must have concluded ``success`` —
   ``skipped``, ``neutral``, ``cancelled`` or missing is not green (a path-filtered matrix that
   skipped the suites landed a code PR once). The required set is the product's whole one
   (:meth:`asf.harvest.lane.GitHubHost.merge_required`: ``landing_checks`` plus
   ``deploy_sha.prod.required_jobs_from`` read *at the batch sha* — one source, the product's
   own file, never a copied list);
3. **lands** a green batch by fast-forwarding the trunk to the batch sha — the gated sha *is* the
   landed sha, and git itself refuses anything but a fast-forward (never a force). Each member
   is MERGING (R3/R8) before the push and MERGED at that sha after it; a PR the host has not
   marked merged within a few seconds (each member head is a parent of the batch, so it
   normally does) is closed with a comment naming the sha; the member branches and the batch
   ref are deleted;
4. **isolates the culprit** of a red batch — once flake triage (:mod:`asf.flake`) re-ran the
   failed job and it went red again, and never when the trunk fails the same check (then nobody
   is blamed: the members wait). The failed job is the one that *failed*, a ``needs:`` upstream
   the product does not require included (it is what skipped the required ones). Its log's
   ``path:line`` findings are mapped to each member's own diff: the member(s) they name go BACK
   with the job, the failing step, those lines and the local reproduction (:func:`blame`,
   :func:`culprit_text`), and the rest are cut again at once. A lone member is the culprit. When
   the log names no file only some members changed, the batch is cut again as two halves, each
   on the trunk alone (not stacked: stacked, the upper half would only re-run the red whole), so
   every run of a bisection answers for its own half — whichever lands first makes the other
   stale, and that one is cut again on the new tip.

**Rebuild.** A batch whose run the CI start queue (:mod:`asf.ci_queue`) cancelled and can
neither re-run nor replace — its run made on a workflow the trunk has changed since, or STUCK
(re-run and fresh dispatch refused) — is not waited out: the start queue asks for a rebuild
(:func:`request_rebuild`), and the next pass drops the batch and cuts its members again on the
trunk's tip, a new sha whose push starts a fresh run.

**No verdict.** A required check that concluded ``cancelled``, ``timed_out``, ``stale`` or
``startup_failure`` (:data:`NONVERDICT`: an operator cancelling a hung run, a timeout, a
superseding workflow) judged no code: never red against the members, never green. With nothing
queued or in progress on the batch sha to replace it, its run is re-run once per (batch sha,
job), the claim kept in ``ci-cancels.json`` (cause :data:`MQ_RERUN`); a second non-verdict on that
sha drops the batch and cuts its members again, with an ``ALARM`` line and a ``queue cancels``
doctor row. A batch pending past ``merge_queue.stuck_min`` (default 90) with a required check
never started and no run queued or in progress on its sha is handled the same (2026-10-04: a
batch whose ``gate`` was cancelled sat "pending" 3 h, the green batch behind it held by a full
chain). A cancel the CI start queue made itself (its ``relief`` record) is its own to re-run. A required
job lost with its runner — a kill or an OOM the host reports as ``failure`` with ``The operation
was canceled.`` (:func:`asf.flake.infra_red`) — is the same non-verdict (:func:`_runner_lost`).
Only the required jobs are re-run (``gh run rerun --job``), never the whole run's red jobs: a
red job the product does not require never judges a batch and is never re-run by the queue.
The verdict reads every attempt on the sha (:func:`batch_runs`): per check, the newest attempt
that completed and judged code — a cancelled re-run never hides an earlier green attempt.

**The pre-cut check.** ``merge_queue.precut_check`` (one command or a list; unset: none) runs on the
batch tree in the cut's worktree as each member merges, before anything is pushed
(:func:`precut_check_red`). A member whose merge turns it red is taken out of the cut again, named
(``PR #n … dropped from the cut``) and sent back with the check's output; the batch goes on
without it. A base already red on it judges nobody. Unset, the cut is as it always was. A file
merged by union (``merge=union``) merges clean in git even when two members add rows claiming the
same id: only a check over the merged tree sees that, and here it is seen before any heavy run.
When such a red reaches CI anyway, a log line that names a file but no line is mapped by the rows
it names to the member whose diff added them (:func:`blame`). Such a red is deterministic: a job
``merge_queue.deterministic_jobs`` names, or one whose failed step runs a ``precut_check`` command
(:func:`deterministic`), is never re-run by flake triage — the member its log names goes back at
once and the rest are cut again (2026-10-06: a duplicate-row red re-run twice, ~45 min).

**A red the triage will re-run keeps the chain.** A required job red while one of the run's own
*required* jobs is still live cannot be re-run yet: the batch stays pending — never red on the
host's refusal — and every batch stacked on it stays in flight. Only the re-run's own red drops
the batch, and the chain with it (2026-10-06: one such red dropped a stacked chain, ~1.5 h of
heavy runs). A non-required job alone still running (a ``site`` job, say) never counts: the
triage (:func:`asf.flake.triage`, given the batch's own ``required`` set) drops the batch at
once instead of waiting on a job nobody is gating on (B-0274: an unrelated job alone kept a red
``gate-tests`` — and every batch stacked above it — pending for hours).

**A drop cancels its run.** Whenever a batch is dropped or replaced (moved, stale, red, timed
out, re-cut), its ref's runs still queued or in progress at its sha are cancelled in the same
step (:func:`_cancel_live`, claimed as :data:`MQ_DROPPED`): nothing will land that sha. A run
still live on a batch ref no batch holds any more — a landed batch's job the product does not
require, a drop whose cancel did not take — is reaped every pass (:func:`reap_leftover_runs`,
:data:`MQ_REAPED`); one whose sha is the trunk's or a live batch's only once every required check
on that sha is success.

**A landed member is pruned.** A member whose head the trunk already contains is taken off its
batch (:func:`_prune_landed`) — its branch leaving origin never drops the batch — and a head
already on the base is never cut into a batch.

**Stacking.** Up to ``merge_queue.inflight`` batches form a chain: each is cut on the sha of the
one before it, not the trunk, so two batches overlap in CI instead of queueing. The chain's
order is the landing order; a dropped batch drops every batch stacked on it (they contain its
commits). A green batch stacked on a pending one waits for it; a green batch that contains the
trunk's tip and is stacked on no pending batch lands at once, before anything below it in the
chain — those are cut again on the new tip. A green chain is never made stale by a later cut.

**Priority.** ``asf land --priority`` orders the *next* cut: its PRs go first in line, ahead of
the factory's members and the plain requests. It never re-bases or stales a batch already cut and
never takes the chain past ``merge_queue.inflight`` — a full chain holds it as it holds every PR
(2026-10-04: a priority batch cut on the trunk at the front of the chain held a green batch 50+
min and starved the plain PRs). Priority is a property of the request, read again every pass:
``asf land <pr>`` without ``--priority`` (or ``asf land <pr> --demote``) demotes it, a batch
already cut included. A batch holding a priority request has runner priority for its CI start
(:func:`asf.ci_queue.urgent_batch`): trunk priority, never cancelled by relief, its runners set
aside from the PR starts behind it until its jobs have them — as has any batch once the trunk has
stood still past half ``ci.trunk_stall_hours``.

**Out of the queue.** A member whose head moved, whose PR turned draft, or whose PR the operator
withdrew (``asf land <pr> --withdraw``, a factory PR included: :data:`WITHDRAWN_FILE` holds it out
until ``asf land <pr>`` asks again or its head moves) drops its batch — it counts as moved, the
rest are cut again. A request is consumed when its PR lands, whoever queued it, and dropped when
its head is already on the trunk: a landed PR is never cut again.

**One tested tree lands once.** A batch of one PR cut on the trunk's tip whose tree is the PR
head's own tree (the head contains the tip) is the very tree the PR's own run judged: when every
required check concluded ``success`` on that head, the batch inherits that verdict — no batch ref
is pushed, no second heavy run, and the attestation names the PR's run.

**State.** ``state/<product>/merge-queue.json`` holds the chain; each member's lane record
(QUEUED, ``batch``, ``sha``) is on its run line as every other lane state. A product not on
``merge: queue`` never reads or writes either: :func:`asf.harvest.lane.merge_prs` is unchanged.

**One door.** A PR no factory item made — a hotfix, a CI change, a product session's own —
enters the same way: ``asf land <pr>`` (:func:`cmd_land`) records a request, and the pass takes
the PR once its required checks are success on its exact head (:func:`requested_ready`). Each
member's merge commit carries :data:`TRAILER`; :mod:`asf.trunk_watch` flags every trunk commit
that does not.

**Attestation.** Landing fast-forwards the trunk to the very sha the batch run judged, so the
trunk's own push run would judge that sha a second time. Before the push, :func:`attest` sets a
commit status on the sha — context :data:`ATTEST_CONTEXT` (``asf/attested``), state
``success``, ``target_url`` the batch run, description ``ASF-Batch-Run: <run id> …`` — and only
for a batch whose required checks all concluded ``success`` at that exact sha (the green verdict
:func:`land` is called on). The run id cannot be a trailer of the merge commit: the commit is
made before the run exists, and amending it would change the sha the run gated. A product's CI
reads the status on its ``push`` to the trunk and skips the heavy matrix, deploying from the
attestation (``docs/guide/product-config.md``, *Main as attestation*). A refused status is told
and never blocks the landing: the trunk run then judges the sha as before.

The batch ref carries only merge commits of heads that already passed the product's pre-push
hook on their own pushes, so it is pushed ``--no-verify``; the trunk push is refs-only by nature
(the sha is already on origin). Both go from the operator's checkout, as the fast-forward landing
does, and neither goes through :mod:`asf.refguard`: the trunk push is the lane's one sanctioned
landing, and a batch ref that matches a protected pattern is refused before it is made.
"""
import datetime
import json
import os
import re
import shutil
import tempfile
import time

from asf import (attestation, ci_queue, connectors, env, flake, gh_limit, gitops, gitpush,
                 refguard, run_cancel, stale_ref, tree_green)
from asf.harvest import harvest as H
from asf.harvest import lane as lane_mod
from asf.state import store
from asf.workers import lifecycle
from asf.workers.pool import now_iso

#: ``state/<product>/merge-queue.json``: ``{batches: [...]}``, the chain lowest first.
QUEUE_FILE = 'merge-queue.json'
#: ``conventions.merge_queue`` defaults: the ref prefix the product's CI triggers on, how many
#: PRs one batch takes, how many batches may be in flight (stacked), and how long a batch may
#: wait for its checks before it is dropped and cut again (a saturated runner pool is slow, not
#: red: the default is generous).
DEFAULTS = {'ref_prefix': 'batch/', 'batch_size': 3, 'inflight': 2, 'timeout_min': 360,
            'stuck_min': 90, 'start_kind': 'trunk', 'precut_check': (), 'precut_check_timeout_s': 120,
            'deterministic_jobs': (), 'hold_culprit': True}
#: ``state/<product>/merge-queue-culprits.json``: ``{branch: {head, checks, at}}`` — the members a
#: deterministic red named, held out of every cut until their head moves (:func:`hold_culprits`)
CULPRITS_FILE = 'merge-queue-culprits.json'
#: the conclusions of a required check that judged no code (:func:`_nonverdict`): a cancel (an
#: operator, a superseding workflow, a timeout the host turned into one), a check the host gave
#: up on (``timed_out``/``stale``), a workflow that never started its jobs. Never red, never green.
NONVERDICT = ('cancelled', 'timed_out', 'stale', 'startup_failure')
#: the lane states an `asf land` request on a lane-held branch may be cut in: the review's
#: verdict is in (the gate's states) or the branch is queued already. PR_OPEN, REVIEW, BACK and
#: PUSHED wait — their head is still to move (F-0287)
VERDICT_STATES = lane_mod.GATE_STATES + (lane_mod.QUEUED,)
#: the claims (:func:`asf.ci_queue.claim_cancel`, ``ci-cancels.json``) of a batch's non-verdict:
#: the one re-run per (batch sha, job), a re-run the host refused, and the re-cut that follows a
#: second one — the ALARM the doctor's ``queue cancels`` row reads
MQ_RERUN, MQ_REFUSED, MQ_ALARM = 'mq-cancel-rerun', 'mq-cancel-refused', 'mq-cancel-alarm'
#: the claim of a live run the queue cancelled because it dropped or replaced its batch ref
#: (:func:`_cancel_live`): a run judging a sha nobody will land holds runners for nothing
MQ_DROPPED = 'mq-dropped'
#: the claim of a leftover run the queue reaped (:func:`reap_leftover_runs`): a run on a batch ref
#: no batch in flight holds any more (landed or dropped), still queued or in progress
MQ_REAPED = 'mq-reaped'
#: the reaper's file and how often it lists the live runs (two host reads each time)
REAP_FILE, REAP_EVERY_S = 'merge-queue-reap.json', 300
#: how long a re-run asked of the host may show nothing new before its claim counts as spent
RERUN_GRACE_S = 600
#: how long an ALARM stays a red doctor row
ALARM_WINDOW_S = 6 * 3600
#: ``merge_queue.start_kind``: how the CI start queue (:mod:`asf.ci_queue`) admits the batch
#: push — ``trunk`` (the default: the batch is the trunk's next sha, so it starts as a trunk run
#: does — trunk priority, sized over every runner, never held at the ``capacity.ci`` ceiling or
#: by the PR runner fit) or ``batch`` (the product's batch-step kind: lowest priority, held at
#: the ceiling). Which runners the run lands on is the product's workflow's to say (its
#: ``runs-on`` for a push of the batch ref), not ASF's.
START_KINDS = ('trunk', 'batch')
#: how many lines of a red ``merge_queue.precut_check`` (:func:`precut_check_red`) go into the culprit's text
PRECUT_CHECK_LINES = 30
#: the subject of each member's merge commit on the batch ref: the trail names the members
MERGE_SUBJECT = 'merge-queue: #{pr} ({branch} @ {head})'
#: the trailer on each member's merge commit: what :mod:`asf.trunk_watch` reads a queue landing by
TRAILER = 'Landed-by: asf merge queue'
#: ``state/<product>/land-requests.json``: the PRs ``asf land`` asked the queue to take
REQUESTS_FILE = 'land-requests.json'
#: ``state/<product>/land-withdrawn.json``: ``{'<pr>': {at, head?, by?}}`` — the PRs the operator
#: withdrew (``asf land --withdraw``), held out of every batch until asked again or moved
WITHDRAWN_FILE = 'land-withdrawn.json'
#: ``state/<product>/merge-queue-rebuild.json``: ``{ref: {why, at}}`` — batches the CI start
#: queue gave up on (a cancelled run on a stale workflow, or stuck); the next pass rebuilds them
REBUILD_FILE = 'merge-queue-rebuild.json'
#: the commit status context a landed batch sha carries (:func:`attest`)
ATTEST_CONTEXT = attestation.CONTEXT
#: the key the attestation's description names the batch run by (the trailer form)
ATTEST_KEY = 'ASF-Batch-Run'
#: how long a landed member's PR is given to read MERGED on the host before it is closed by hand
PR_MARK_TRIES, PR_MARK_SLEEP_S = 3, 2
#: ``conventions.flags.queue_store``: ``on`` moves every write of the three files above to
#: :mod:`asf.state.store` — a per-file lock around each read-modify-write and a temp file of its
#: own (never the fixed ``.tmp`` two writers shared), so an ``asf land`` or a CI-queue rebuild
#: request written while a pass runs is never lost; ``off`` (the default) writes as before. The
#: files keep their shape either way: every reader, and an older venv's, reads them unchanged.
STORE_FLAG = 'queue_store'
#: the merge commits' identity when the checkout has none configured
IDENT = {'GIT_AUTHOR_NAME': 'asf merge queue', 'GIT_AUTHOR_EMAIL': 'asf-merge-queue@localhost',
         'GIT_COMMITTER_NAME': 'asf merge queue', 'GIT_COMMITTER_EMAIL': 'asf-merge-queue@localhost'}


# ---- settings and state -------------------------------------------------------------------------

def settings(conv):
    """``conventions.merge_queue`` over :data:`DEFAULTS`: a positive int per count, a non-empty
    string prefix; anything else keeps its default (a scalar block is the doctor's finding).

    ``inflight`` alone also takes ``0``: the first-class pause for a pin move (#36, #34) — cut
    no new batch (:func:`run`'s ``len(chain) >= st['inflight']`` holds at once), land what is
    already in flight (the chain's own judging never reads ``inflight``), drop what is dead
    (:func:`judge` on a red verdict, same as ever) — instead of the old
    ``conventions.protected_refs: ['<batch prefix>*']`` workaround, which also refused refguard's
    own cleanup of a dropped batch's ref (:func:`paused`)."""
    raw = conv.map_of('merge_queue') if hasattr(conv, 'map_of') else {}
    out = dict(DEFAULTS)
    for key in ('batch_size', 'inflight', 'timeout_min', 'stuck_min'):
        v = raw.get(key)
        if not isinstance(v, int) or isinstance(v, bool):
            continue
        if v >= 1 or (key == 'inflight' and v == 0):
            out[key] = v
    prefix = raw.get('ref_prefix')
    if isinstance(prefix, str) and prefix.strip():
        out['ref_prefix'] = prefix.strip()
    kind = str(raw.get('start_kind') or '').strip().lower()
    if kind in START_KINDS:
        out['start_kind'] = kind
    check = raw.get('precut_check')
    if isinstance(check, str):
        check = [check]
    if isinstance(check, (list, tuple)):
        out['precut_check'] = tuple(c.strip() for c in check if isinstance(c, str) and c.strip())
    t = raw.get('precut_check_timeout_s')
    if isinstance(t, int) and not isinstance(t, bool) and t >= 1:
        out['precut_check_timeout_s'] = t
    jobs = raw.get('deterministic_jobs')
    if isinstance(jobs, str):
        jobs = [jobs]
    if isinstance(jobs, (list, tuple)):
        out['deterministic_jobs'] = tuple(j.strip() for j in jobs if isinstance(j, str) and j.strip())
    hold = raw.get('hold_culprit')
    if hold is False or str(hold).strip().lower() in ('off', 'false', 'no', '0'):
        out['hold_culprit'] = False
    return out


def paused(conv):
    """True under ``merge_queue.inflight: 0`` (:func:`settings`) — a pin move pauses new cuts
    this way (``asf doctor``'s ``queue pause`` row, :func:`asf.doctor.check_queue_pause`), never
    by adding the batch ref prefix to ``conventions.protected_refs`` (that also refuses
    refguard's own delete of a dead batch's ref, #34 — the deadlock #36 fixes)."""
    return settings(conv)['inflight'] == 0


def load_culprits(state_dir):
    try:
        with open(os.path.join(state_dir, CULPRITS_FILE), encoding='utf-8') as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        got = {}
    return got if isinstance(got, dict) else {}


def save_culprits(state_dir, data):
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = os.path.join(state_dir, CULPRITS_FILE + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, sort_keys=True, indent=1)
        os.replace(tmp, os.path.join(state_dir, CULPRITS_FILE))
    except OSError:
        pass


def hold_culprits(lane, culprits, checks):
    """Remember each member of ``culprits`` (facts with ``branch`` and ``head``) a deterministic
    red (``checks``) named: the same tree answers the same, so it is never batched again at that
    head (2026-10-05: one duplicate-row collision re-batched 5× over 3 h)."""
    data = load_culprits(lane.state_dir)
    for f in culprits:
        if f.get('branch') and f.get('head'):
            data[f['branch']] = {'head': f['head'], 'checks': sorted(checks), 'at': now_iso()}
    save_culprits(lane.state_dir, data)


def held_culprit(lane, f, head):
    """The held record of member ``f`` when its ``head`` is the one a deterministic red named,
    else None (a moved head clears the hold)."""
    data = load_culprits(lane.state_dir)
    rec = data.get(f.get('branch'))
    if not rec:
        return None
    if head and rec.get('head') == head:
        return rec
    data.pop(f['branch'], None)
    save_culprits(lane.state_dir, data)
    return None


def deterministic(lane, st, failed):
    """The names of the ``failed`` checks whose red is deterministic — never re-run by flake
    triage: each job ``merge_queue.deterministic_jobs`` names, and each whose failed step runs a
    ``merge_queue.precut_check`` command (the product's cheap tree check: the same tree answers the
    same). The step is read only when a pre-cut check is set; an unreadable one is not deterministic."""
    named = set(st.get('deterministic_jobs') or ())
    cmds = [c for c in st.get('precut_check') or () if c]
    out = []
    for c in failed or ():
        name = c.get('name') or '?'
        if name in named or flake.job_key(name) in named:
            out.append(name)
            continue
        if not cmds:
            continue
        job = flake._ids(c.get('link'))[1]
        if not job:
            continue
        try:
            _step, cmd = lane_mod.red_step(lane.slug, job)
        except Exception:   # noqa: BLE001 — an unreadable step is judged as before
            continue
        if cmd and any(k in cmd for k in cmds):
            out.append(name)
    return out


def precut_check_red(st, cwd):
    """``None`` when every ``merge_queue.precut_check`` command passes in ``cwd`` (or none is set);
    else the failing command's output (its last :data:`PRECUT_CHECK_LINES` lines, the command
    first). A command that times out or cannot start is no verdict: ``None``, never red.

    The product's cheap repository checks over the batch tree, run before the batch ref is
    pushed: a file merged by union (``merge=union``) merges clean in git, so two rows claiming
    the same id pass the cut and only a check of the merged tree sees them — else on a heavy
    run, red (2026-10-05: a batch red on a member's stale rows in such a file)."""
    for cmd in st.get('precut_check') or ():
        try:
            rc, out, err = H.sh_timed(['bash', '-c', cmd], cwd, None, st.get('precut_check_timeout_s'))
        except OSError:
            continue
        if rc is None or rc == 0:
            continue
        lines = [l for l in ((out or '') + '\n' + (err or '')).splitlines() if l.strip()]
        return '\n'.join([f'$ {cmd}'] + lines[-PRECUT_CHECK_LINES:])
    return None


def path(state_dir):
    return os.path.join(state_dir, QUEUE_FILE)


def store_on(product):
    """True when ``product`` sets ``flags.queue_store: on`` (:data:`STORE_FLAG`); no product (a
    caller that has none to hand) is off."""
    flag = getattr(product, 'flag', None)
    value = flag(STORE_FLAG, 'off') if callable(flag) else 'off'
    return value is True or str(value).strip().lower() in ('on', 'true', 'yes', '1')


def _owner(lane):
    """The product a lane pass runs for (its flags), or None."""
    return getattr(lane, 'product', None)


def _store_update(target, fn, default, out=None):
    """:func:`asf.state.store.update_at` on ``target``; True when written. A file the store will
    not touch — corrupt (copied aside, never overwritten), or its lock held past the timeout — is
    said in one line and left alone: the request stays as the file holds it, and nothing is
    wiped."""
    try:
        store.update_at(target, fn, default=default)
        return True
    except store.StoreError as e:
        line = f'merge queue: {os.path.basename(target)} not written — {e}'
        (out or (lambda s: print(s, flush=True)))(line)
        return False


def load(state_dir):
    """The chain as the file holds it; an unreadable or misshapen file is an empty chain."""
    try:
        with open(path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    batches = data.get('batches') if isinstance(data, dict) else None
    return {'batches': [b for b in (batches or []) if isinstance(b, dict) and b.get('ref')
                        and b.get('sha') and isinstance(b.get('members'), list)]}


def save(state_dir, data, product=None):
    if store_on(product):
        store.write_at(path(state_dir), data)
        return
    os.makedirs(state_dir, exist_ok=True)
    tmp = path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, path(state_dir))


def load_rebuilds(state_dir):
    """``{ref: {why, at}}``: the batches asked to be rebuilt (:func:`request_rebuild`)."""
    try:
        with open(os.path.join(state_dir, REBUILD_FILE), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _save_rebuilds(state_dir, data):
    os.makedirs(state_dir, exist_ok=True)
    target = os.path.join(state_dir, REBUILD_FILE)
    tmp = target + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, target)


def holds(state_dir, ref):
    """True while the chain holds the batch ``ref``."""
    return any(b.get('ref') == ref for b in load(state_dir)['batches'])


def request_rebuild(state_dir, ref, why, product=None):
    """Ask the next pass to drop batch ``ref`` and cut its members again on the trunk's tip —
    a new batch sha whose push starts a fresh run on the trunk's own workflow. The CI start
    queue's answer to a batch run it can neither re-run nor replace
    (:func:`asf.ci_queue._rerun_cancelled`): a stale or stuck batch never blocks the queue.
    True when the chain holds ``ref`` (the request is written), else False."""
    if not holds(state_dir, ref):
        return False
    if store_on(product):
        def ask(data):
            data = data if isinstance(data, dict) else {}
            data[ref] = {'why': why, 'at': now_iso()}
            return data
        return _store_update(os.path.join(state_dir, REBUILD_FILE), ask, {})
    data = load_rebuilds(state_dir)
    data[ref] = {'why': why, 'at': now_iso()}
    _save_rebuilds(state_dir, data)
    return True


def _clear_rebuild(state_dir, ref, product=None):
    if store_on(product):
        target = os.path.join(state_dir, REBUILD_FILE)
        if ref in load_rebuilds(state_dir):     # lock-free look first: most drops asked nothing
            def clear(data):
                data = data if isinstance(data, dict) else {}
                data.pop(ref, None)
                return data
            _store_update(target, clear, {})
        return
    data = load_rebuilds(state_dir)
    if data.pop(ref, None) is not None:
        _save_rebuilds(state_dir, data)


# ---- the verdict on one sha ---------------------------------------------------------------------

def check_runs(slug, sha):
    """The check runs on ``sha`` (the latest attempt of each), or None when unreadable."""
    data = H.gh_json(['api', f'repos/{slug}/commits/{sha}/check-runs?per_page=100'], None)
    runs = data.get('check_runs') if isinstance(data, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else None


def batch_runs(slug, sha):
    """The check runs on batch ``sha`` as the verdict reads them, or None when unreadable: every
    attempt (``filter=all``), one per check name — the newest that *completed* with a conclusion
    other than :data:`NONVERDICT`, else the newest of all. The host's default (``latest``) shows
    only the newest attempt, so a re-run cancelled under a runner kill hid an earlier attempt
    that was already green on the very same sha (2026-10-05)."""
    data = H.gh_json(['api', f'repos/{slug}/commits/{sha}/check-runs?per_page=100&filter=all'],
                     None)
    runs = data.get('check_runs') if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return None
    return settled_runs([r for r in runs if isinstance(r, dict)])


def settled_runs(runs):
    """One check run per name out of every attempt in ``runs``: the newest completed one whose
    conclusion judged code (not :data:`NONVERDICT`), else the newest. Newest is the higher
    check-run id (ids grow with each attempt), then the later in the list; a newer attempt still
    running over a judged red is taken instead (pending). Order kept."""
    def newer(i):
        r = runs[i]
        try:
            rid = int(r.get('id') or 0)
        except (TypeError, ValueError):
            rid = 0
        return rid, i
    best = {}
    for i, r in enumerate(runs):
        k = r.get('name')
        judged = r.get('status') == 'completed' and r.get('conclusion') not in NONVERDICT
        have = best.get(k)
        if have is None:
            best[k] = (judged, i)
            continue
        hj, hi = have
        if (judged, newer(i)) > (hj, newer(hi)):
            best[k] = (judged, i)
    # a re-run in flight on top of a judged red is pending (its answer is coming), never the
    # old red; on top of a green it is green already — the same sha was judged
    for k, (judged, i) in list(best.items()):
        if not judged or runs[i].get('conclusion') == 'success':
            continue
        live = [j for j, r in enumerate(runs) if r.get('name') == k
                and r.get('status') != 'completed' and newer(j) > newer(i)]
        if live:
            best[k] = (False, max(live, key=newer))
    keep = {i for _j, i in best.values()}
    return [r for i, r in enumerate(runs) if i in keep]


def verdict(runs, required, nonverdict=('cancelled',)):
    """``('green'|'pending'|'red', why)`` for check ``runs`` against the ``required`` names. A
    required check is green only when a run of it (its job name up to the first space: a matrix
    leg answers for its job) concluded ``success``; one still running or not created is
    pending; one ``cancelled`` (any conclusion in ``nonverdict``; the batch judge passes
    :data:`NONVERDICT`) is pending (it judged no code); one that concluded anything else —
    ``skipped`` included — is red, unless its workflow run still has a job in flight (then a
    ``skipped``/``cancelled`` is pending: it waits on an unfinished upstream). A red check the
    product does not require is never a verdict. No required names: pending, and the line says
    so — a queue with nothing to gate on lands nothing."""
    from asf.harvest import deploy
    if not required:
        return 'pending', 'no required checks named (landing_checks / required_jobs_from)'
    red, pending = [], []
    # F-0328: a workflow run still in flight (a job of it queued or running) has not settled its
    # skipped/cancelled jobs — a job that ``needs:`` an unfinished upstream can read ``skipped``
    # while it waits (a product batch 053bccd, dropped mid-``rules``): such a required job is
    # pending, never red. Only a real conclusion of a settled run is a verdict.
    live_ids = {_run_id(r) for r in runs or () if r.get('status') != 'completed'}
    for name in required:
        mine = [r for r in runs or () if deploy.job_key(r.get('name')) == name]
        if not mine:
            pending.append(f'{name} (not started)')
            continue
        for r in mine:
            if r.get('status') != 'completed':
                pending.append(f"{name} ({r.get('status') or 'pending'})")
            elif r.get('conclusion') in nonverdict:   # a cut-short run judged no code
                pending.append(f"{name} ({r.get('conclusion')})")
            elif r.get('conclusion') in ('skipped', 'cancelled') and live_ids and (
                    _run_id(r) in live_ids or _run_id(r) is None or None in live_ids):
                pending.append(f"{name} ({r.get('conclusion')}, its run still in progress)")
            elif r.get('conclusion') != 'success':
                red.append(f"{name} ({r.get('conclusion') or 'no conclusion'})")
    if red:
        return 'red', ', '.join(red)
    if pending:
        return 'pending', ', '.join(pending)
    return 'green', ''


# ---- the pass -----------------------------------------------------------------------------------

# ---- a landing-gate hold the triage never judged: gated again -----------------------------------
#
# A spec/plan PR sent BACK as ``landing-gate`` over a red batch job asks its session to change a
# document so a product job passes — right only when that job's red is the document's. A hold
# written before flake triage existed (#603, #621) named jobs nobody re-ran, a job the product does
# not require, or runner loss. Such a hold is no defect of the document: it is cleared and the
# branch goes back through the gate (and so the next batch, whose red is triaged first).

_BATCH_SHA_RE = re.compile(r'@ ([0-9a-f]{7,40})\b')


def _hold_regate_why(lane, run, head, trunk_sha):
    """Why the landing-gate hold of ``run`` is no document defect (a sentence), or None when it
    stands: every job it names must be unrequired, lost with its runner, or never re-run by the
    flake triage on the batch sha it names. A required ``merge_queue.deterministic_jobs`` job is
    never re-run: its hold stands."""
    corr = (run or {}).get('correction') or {}
    jobs = [flake.job_key(str(n).split(' (')[0]) for n in corr.get('finding') or ()]
    m = _BATCH_SHA_RE.search(corr.get('text') or '')
    if corr.get('kind') != lane_mod.LANDING_GATE or not jobs or not m:
        return None
    short = m.group(1)
    required, _why = required_set(lane, head, trunk_sha)
    if required is None:
        return None
    triaged = set()
    for key in flake.load(lane.state_dir).get('reruns', {}):
        sha, _, name = key.partition('|')
        if sha.startswith(short) or short.startswith(sha):
            triaged.add(name)
    runs = None
    reasons = []
    fixed = set(settings(lane.conv).get('deterministic_jobs') or ())
    for job in dict.fromkeys(jobs):
        if job in fixed and job in required:
            return None     # deterministic: never re-run, its red is the defect it names
        if job not in required:
            reasons.append(f'{job} is not a required check')
            continue
        if runs is None:
            runs = check_runs(lane.slug, short) or []
        lost = next((flake.infra_red(lane.slug, flake._ids(r.get('html_url'))[1] or r.get('id'), H._gh) for r in runs
                     if flake.job_key(r.get('name')) == job
                     and r.get('conclusion') not in (None, 'success', 'skipped', 'neutral')), None)
        if lost:
            reasons.append(f'{job} lost its runner ({lost})')
        elif job not in triaged:
            reasons.append(f'{job} was never re-run by flake triage')
        else:
            return None
    return '; '.join(reasons)


def regate_holds(lane, heads, trunk_sha):
    """Clear each landing-gate hold :func:`_hold_regate_why` finds no document defect in, at the
    branch's current head, and put the branch back at PUSHED: the gate takes it again. Returns the
    branches cleared."""
    out = []
    for b, run in lifecycle.by_branch(lane.path).items():
        rec = lifecycle.lane_of(run)
        corr = (run or {}).get('correction') or {}
        if (rec.get('state') == lane_mod.BACK and corr.get('kind') == 'conflict'
                and '(a batch ahead of it)' in (corr.get('text') or '')
                and heads.get(b) and heads[b] == rec.get('head')):
            # told it conflicts with a batch ahead, rebuild skipped: when it conflicts with the
            # trunk itself, the lane's mechanical rebuild (#570) is owed
            tfiles = _trunk_conflict(lane, rec['head'])
            if tfiles is not None:
                f = {'branch': b, 'run': run, 'prev': rec, 'head': rec['head'],
                     'item': rec.get('item'), 'pr': {'number': rec.get('pr'), 'state': 'OPEN'},
                     'class': lane_mod.CODE}
                lane.out(f"merge queue: {b} (PR #{rec.get('pr')}) conflicts with {lane.trunk} "
                         f"itself, not a batch ahead — the lane rebuilds it")
                lane_mod.send_back(
                    lane, f, 'conflict',
                    f"PR #{rec.get('pr')} does not merge onto {lane.trunk}"
                    + (f'; conflicts in {", ".join(tfiles)}' if tfiles else '')
                    + f' — rebase the branch onto origin/{lane.trunk} (git rebase '
                    f'origin/{lane.trunk}), never merge; the factory publishes the rebased branch',
                    tfiles)
                out.append(b)
            continue
        if rec.get('state') != lane_mod.BACK or corr.get('kind') != lane_mod.LANDING_GATE:
            continue
        if not heads.get(b) or heads[b] != rec.get('head'):
            continue    # moved: its own session's answer is in flight
        why = _hold_regate_why(lane, run, rec['head'], trunk_sha)
        if not why:
            continue
        f = {'branch': b, 'run': run, 'prev': rec, 'head': rec['head'], 'item': rec.get('item')}
        lane.set(f, lane_mod.PUSHED, f'gated again: the landing-gate hold was no document defect — {why}')
        H.mark_session(lane.state_dir, run.get('job') or b, correction=None, branch=b)
        lane.out(f"merge queue: {b} (PR #{rec.get('pr')}) landing-gate hold cleared — {why}; "
                 f"gated again")
        out.append(b)
    return out


def own_pass(product, items=None, out=print, lane_cls=None):
    """The queue's pass on its own cadence (the product's queue job, every
    :data:`asf.scheduler.QUEUE_EVERY_S` seconds — :func:`asf.ci_queue.apply`), never tied to the
    tick: 2026-10-06 a green batch waited for the next detached harvest, which only a main tick
    starts, so landing took as long as a tick ran (25–60 min). Judges the batches in flight —
    lands the green, drops or splits the red — and cuts from what waits (:func:`run` with nothing
    new from a gate), under the product's harvest lock, so it never races a harvest: one that
    holds the lock runs this same pass itself. Returns True when it ran, None when skipped."""
    if not product.conventions.merge_queue() or not product.repo_dir:
        return None
    state_dir = os.path.abspath(env.state_dir(product))
    lock = H.try_lock(state_dir)
    if lock is None:
        out('merge queue: a harvest holds the lock — its gate pass judges the batches')
        return None
    try:
        lane = (lane_cls or lane_mod.Lane)(product, state_dir, out, False, items)
        if not lane_mod.merge_queued(lane):
            return None
        try:
            run(lane, [])
        finally:
            lane.finish_ref_pushes()
        return True
    finally:
        lock.close()


def run(lane, ready):
    """The queue's one pass, from the detached harvest: judge the batches in flight (landing,
    dropping or splitting each), then cut new ones from ``ready`` — the green, approved PR
    entries :func:`asf.harvest.lane.gate_set` would have merged — and from the members a dropped
    batch handed back. Each entry's outcome goes in ``lane.results`` (``queued``, ``landed``,
    ``waiting``, ``held``, ``back``)."""
    trunk = lane.trunk
    if lane.dry_run:
        for f in ready:
            lane.out(f"DRY: would queue {f['branch']} (PR #{_pr(f)}) into a merge-queue batch")
            lane.results[f['branch']] = 'dry'
        return
    st = settings(lane.conv)
    data = load(lane.state_dir)
    try:   # a landed or dropped batch's leftover runs, whether or not anything else is to do
        reap_leftover_runs(lane, st, data['batches'])
    except gh_limit.RateLimited:
        raise
    except Exception as e:   # noqa: BLE001 — a reap is housekeeping, never the pass's failure
        lane.out(f'merge queue: leftover-run reap skipped — {e}')
    runs = lifecycle.by_branch(lane.path)
    asked = load_requests(lane.state_dir)
    gated = any(lifecycle.lane_of(r).get('state') == lane_mod.BACK
                and ((r or {}).get('correction') or {}).get('kind') in (lane_mod.LANDING_GATE,
                                                                        'conflict')
                for r in runs.values())
    if not data['batches'] and not ready and not asked and not gated and not any(
            lifecycle.lane_of(r).get('state') == lane_mod.QUEUED and lifecycle.lane_of(r).get('batch')
            for r in runs.values()):
        return
    lane.fresh_trunk()
    trunk_sha = H.sh(['git', 'rev-parse', f'origin/{trunk}'], cwd=lane.repo).stdout.strip()
    lane.trunk_sha = trunk_sha
    heads = lane.remote_heads()
    regate_holds(lane, heads, trunk_sha)
    # the operator's withdraws and the draft PRs, read once a pass: either takes a member out
    # of its batch, whoever queued it (judge), and keeps it out of the next cut
    withdrawn = lane.mq_withdrawn = withdrawn_now(lane, heads)
    drafts = lane.mq_drafts = _drafts(lane, data['batches'])

    chain, groups, loose, settled = [], [], [], set()
    dropped = set()     # the dropped batches: every batch stacked on one of them goes too
    for i, batch in enumerate(data['batches']):
        for m in batch['members']:   # priority is the request's, as it reads this pass
            _sync_priority(m, asked)
        ref, members = batch['ref'], [_member_facts(batch, m, runs) for m in batch['members']]
        if batch.get('base_ref') in dropped:
            _drop(lane, batch, f"its base batch {batch['base_ref']} was dropped")
            loose.extend(members)
            dropped.add(ref)
            continue
        members, landed = _prune_landed(lane, batch, members, trunk_sha)
        settled |= {f['branch'] for f in landed}
        if not members:
            _drop(lane, batch, f'every member is on {trunk} already')
            dropped.add(ref)
            continue
        state, why, detail = judge(lane, batch, members, heads, trunk_sha, st)
        if state == 'moved':
            _drop(lane, batch, why)
            for f, what in detail:
                # a member the lane already moved on (PUSHED, MERGED, STALE …) keeps that
                # record; only one still QUEUED here, its head moved under it, is written
                if (f.get('prev') or {}).get('state') == lane_mod.QUEUED:
                    _wait(lane, f, f'merge queue: {what} in batch {ref}')
            gone = {f['branch'] for f, _w in detail}
            settled |= gone
            loose.extend(f for f in members if f['branch'] not in gone)
            dropped.add(ref)
        elif state in ('stale', 'recut'):
            # recut: a non-verdict its one re-run did not clear (_nonverdict) — no member is
            # blamed, nothing is ledgered red: the members are cut again as a stale batch's are
            _drop(lane, batch, why)
            loose.extend(members)
            dropped.add(ref)
        elif state == 'timeout':
            _drop(lane, batch, why)
            for f in members:
                _wait(lane, f, f'merge queue: batch {ref} {why}')
            settled |= {f['branch'] for f in members}
            dropped.add(ref)
        elif state == 'red':
            _ledger(lane, batch, False, f'red: {why}')
            checks, red = detail
            roots = root_failures(checks, why) or [c for c in checks
                                                   if flake.job_key(c.get('name')) in red]
            names = [c.get('name') for c in roots] or red
            on_trunk = _red_on_trunk(lane, names)
            found = None
            if not on_trunk:
                # the same check red on unrelated landings is the trunk's (asf.trunk_red): this
                # red is recorded first, so the second such landing is already spared
                found = failure_findings(lane.slug, roots)
                on_trunk = _trunk_red_seen(lane, batch, members, names, found)
            if on_trunk:
                # the trunk fails the same check: not this batch's defect — nobody is sent back
                # and nobody is split out; the members wait and are gated again
                _drop(lane, batch, f'red on {why}')
                said = ', '.join(f'{n} @ {s[:9]}' for n, s in sorted(on_trunk.items()))
                lane.out(f'merge queue: {ref} red on {why} — red on {trunk} too ({said}): '
                         f'no member is blamed, the batch waits for {trunk} to go green')
                for f in members:
                    _wait(lane, f, f'merge queue: {", ".join(sorted(on_trunk))} red on {trunk} '
                                   f'too ({said}) — not this PR\'s', green=f.get('green'))
                settled |= {f['branch'] for f in members}
            else:
                cover_tests = {}
                named, covered = blame(lane, batch, members, found, cover_tests)
                culprits = named
                if len(members) == 1:
                    culprits = culprits or {members[0]['branch']: []}
                fallback = (checks, [c.get('name') for c in roots] or red)
                fixed = deterministic(lane, st, roots) if st.get('hold_culprit') else []
                if fixed and culprits and not covered:
                    hold_culprits(lane, [f for f in members if f['branch'] in culprits], fixed)
                why_drop = f'red on {why}'
                if len(named) == 1 and not on_trunk:
                    (only,) = [f for f in members if f['branch'] in named]
                    tp = cover_tests.get(only['branch'])
                    how = (f'the failing test {tp} covers its files' if covered else
                           "its files, named in the failing job's log")
                    why_drop = f"red on {why} — #{_pr(only)}'s ({how})"
                _drop(lane, batch, why_drop)
                for f in members:
                    if f['branch'] in culprits:
                        _send_back(lane, f, 'gate',
                                   culprit_text(lane, batch, f, why, found, culprits[f['branch']],
                                                len(members) == 1, fallback, covered,
                                                cover_tests.get(f['branch'])),
                                   sorted({p for p, _n, _t in culprits[f['branch']]}))
                innocent = [f for f in members if f['branch'] not in culprits]
                if culprits and innocent:
                    lane.out(f"merge queue: {ref} red on {why} — the failure is "
                             f"{', '.join(f'#{_pr(f)}' for f in members if f['branch'] in culprits)}"
                             f"'s (its files, named in the failing job's log); "
                             f"{', '.join(f'#{_pr(f)}' for f in innocent)} cut again without it")
                    loose.extend(innocent)
                elif not culprits:
                    half = len(members) // 2
                    lane.out(f'merge queue: {ref} splits — {len(members[:half])} and '
                             f'{len(members[half:])} PR(s), each cut on {trunk} alone (the failing '
                             f'job\'s log names no file only some of them changed)')
                    groups.extend([members[:half], members[half:]])
            dropped.add(ref)
        elif state == 'pending':
            age = int(_age_s(batch) // 60)
            lane.out(f"merge queue: {ref} @ {batch['sha'][:12]} pending — {why} ({age} min)")
            for f in members:
                lane.results[f['branch']] = 'queued'
            chain.append(batch)
        else:  # green
            below = [b for b in chain if b['ref'] == batch.get('base_ref')]
            if below:   # stacked on a pending batch: it holds that batch's commits, lands after
                lane.out(f"merge queue: {ref} green, waits for {below[0]['ref']} below it")
                for f in members:
                    lane.results[f['branch']] = 'queued'
                chain.append(batch)
            elif land(lane, batch, members, trunk_sha, runs=detail):
                trunk_sha = batch['sha']
                lane.trunk_sha = trunk_sha
                # green on the trunk's tip, it went first: what was below it in the chain no
                # longer contains the trunk — each is cut again on the new tip this pass
                for b in chain:
                    _drop(lane, b, f'{ref} was green on {trunk} and landed first')
                    loose.extend(_member_facts(b, m, runs) for m in b['members'])
                    dropped.add(b['ref'])
                chain = []
            else:
                chain.append(batch)
        # after every verdict, the unjudged still there: a crash mid-pass loses no batch
        save(lane.state_dir, {'batches': chain + data['batches'][i + 1:]}, _owner(lane))

    # a member QUEUED on a batch the chain no longer holds (a file lost, a record from before
    # a drop that died mid-write) would sit QUEUED for good: it waits, and is cut again
    taken = {m['branch'] for b in chain for m in b['members']}
    handed = settled | {f['branch'] for f in loose} | {f['branch'] for g in groups for f in g}
    for b, run in lifecycle.by_branch(lane.path).items():   # as written by this pass
        rec = lifecycle.lane_of(run)
        if rec.get('state') == lane_mod.QUEUED and rec.get('batch') and b not in taken | handed:
            f = _member_facts({'ref': rec['batch']}, {'branch': b, 'head': rec.get('head'),
                                                      'pr': rec.get('pr'), 'item': rec.get('item')},
                              runs)
            _wait(lane, f, f"merge queue: batch {rec['batch']} is not in flight — gated again")

    # ---- cut what is ready, the halves of a split first; then the PRs `asf land` asked for
    asked_ready = requested_ready(lane, heads, trunk_sha,
                                  taken | {f['branch'] for f in ready} | handed)
    # `asf land --priority` orders the next cut — ahead of the factory's members and the other
    # requests (stable: the rest keep their order). It never jumps the chain: the batches already
    # cut keep their place, and merge_queue.inflight holds it as it holds every PR. Priority is
    # the request's as it reads now (a demoted request is plain, a cut member included).
    for f in list(ready) + loose:
        _sync_priority(f, asked)
    ready = sorted(list(ready) + asked_ready, key=lambda f: not f.get('priority'))
    fresh = []
    for f in sorted(list(ready) + loose, key=lambda f: not f.get('priority')):
        if f['branch'] in taken:
            continue
        taken.add(f['branch'])
        if f in loose and heads.get(f['branch']) != f['head']:   # handed back, and moved since
            _wait(lane, f, 'merge queue: head moved')
            continue
        if str(_pr(f)) in withdrawn:
            lane.out(f"merge queue: {f['branch']} (PR #{_pr(f)}) withdrawn by asf land --withdraw "
                     f"— held out until asf land {_pr(f)} asks again or its head moves")
            _wait(lane, f, 'merge queue: withdrawn (asf land --withdraw)', green=f.get('green'))
            continue
        if f['branch'] in drafts:
            _wait(lane, f, 'merge queue: its PR is a draft')
            continue
        held = held_culprit(lane, f, heads.get(f['branch']) or f.get('head')) \
            if st.get('hold_culprit') else None
        if held:
            lane.out(f"merge queue: {f['branch']} (PR #{_pr(f)}) held out — deterministic red "
                     f"({', '.join(held.get('checks') or ())}) at {str(held['head'])[:9]}: not "
                     f"batched again until its head changes")
            _wait(lane, f, 'merge queue: deterministic red at this head — not batched again '
                           'until its head changes', green=f.get('green'))
            continue
        fresh.append(f)
    halves = len(groups)
    from asf import upgrade
    drain = upgrade.draining(lane.product.name)   # a move drains: the chain lands, nothing is cut
    for i, group in enumerate(groups + _pack(fresh, st['batch_size'])):
        if drain:
            for f in group:
                lane.out(f"waiting {f['branch']}: PR #{_pr(f)} green — a move of "
                         f"{lane.product.name} drains (asf upgrade --product); no new batch")
                _wait(lane, f, 'merge queue: a move drains — no new batch is cut',
                      green=f.get('green'))
            continue
        if len(chain) >= st['inflight']:
            for f in group:
                lane.out(f"waiting {f['branch']}: PR #{_pr(f)} green — merge queue full "
                         f"({len(chain)} batch(es) in flight, merge_queue.inflight)")
                _wait(lane, f, f'merge queue: {len(chain)} batch(es) in flight',
                              green=f.get('green'))
            continue
        # a split's halves are cut on the trunk, not on each other: each run then answers for
        # its own half (stacked, the upper half would re-run the red whole)
        stack = chain and i >= halves
        base_sha, base_ref = (chain[-1]['sha'], chain[-1]['ref']) if stack else (trunk_sha, None)
        # one tested tree lands once: a lone PR on the trunk's tip whose head already contains
        # the tip is the tree its own green run judged
        inherit = inherited_runs(lane, group[0], trunk_sha) \
            if len(group) == 1 and base_ref is None else None
        batch = cut(lane, group, base_sha, base_ref, st,
                    base_members=chain[-1]['members'] if base_ref else (), inherit=inherit)
        if batch and batch.get('inherited'):
            members = [_member_facts(batch, m, lifecycle.by_branch(lane.path))
                       for m in batch['members']]
            if land(lane, batch, members, trunk_sha, runs=batch['inherited']['runs']):
                trunk_sha = batch['sha']
                lane.trunk_sha = trunk_sha
                save(lane.state_dir, {'batches': chain}, _owner(lane))
                continue
        if batch:
            chain.append(batch)
            save(lane.state_dir, {'batches': chain}, _owner(lane))
    save(lane.state_dir, {'batches': chain}, _owner(lane))


def judge(lane, batch, members, heads, trunk_sha, st):
    """``(state, why, detail)`` for one batch in flight: ``moved`` (``detail``: the
    ``(member, what)`` pairs that left), ``stale``, ``timeout``, ``red`` (``detail``: the checks
    and the red names), ``pending`` or ``green``. Fetches the batch ref so the required set can
    be read at its sha."""
    ref, sha = batch['ref'], batch['sha']
    moved = []
    drafts = getattr(lane, 'mq_drafts', None) or set()
    withdrawn = getattr(lane, 'mq_withdrawn', None) or {}
    for f in members:
        rec = f.get('prev') or {}
        now_head = heads.get(f['branch'])
        if now_head and now_head != f['head'] and lane_mod.marker_move(lane.repo, f['head'],
                                                                       now_head):
            # a report commit on the member: the tree the batch tested is the one it lands —
            # the member's head advances, the batch stands (F-0287)
            _advance_member(lane, batch, f, now_head)
            rec = f.get('prev') or {}
        if heads.get(f['branch']) != f['head']:
            moved.append((f, 'head moved'))
        elif f['branch'] in drafts:   # whoever queued it: its owner parked it
            moved.append((f, 'left the queue (its PR is a draft)'))
        elif str(_pr(f)) in withdrawn:
            moved.append((f, 'left the queue (withdrawn: asf land --withdraw)'))
        elif f.get('requested'):   # an `asf land` PR: its request is its record
            if str(_pr(f)) not in load_requests(lane.state_dir):
                moved.append((f, 'left the queue (its asf land request was withdrawn)'))
        elif rec.get('state') != lane_mod.QUEUED or rec.get('batch') != ref:
            moved.append((f, f"left the queue ({rec.get('state') or 'no record'})"))
    if moved:
        return 'moved', ', '.join(f"{f['branch']} {w}" for f, w in moved), moved
    if batch.get('inherited'):
        # one tested tree (cut()): no batch ref, no batch run — the PR head's green verdict
        if not gitops.git(['cat-file', '-e', f'{sha}^{{commit}}'], lane.repo).ok:
            return 'stale', f'its commit {sha[:9]} is gone from the checkout', None
        if not lane.is_ancestor(trunk_sha, sha):
            return 'stale', f'{lane.trunk} moved to {trunk_sha[:9]}, which the batch does not contain', None
        return 'green', '', list(batch['inherited'].get('runs') or ())
    asked = load_rebuilds(lane.state_dir).get(ref)
    if asked:   # the CI start queue gave up on its run: cut again on the tip, a fresh run
        return 'stale', f"rebuilt on {lane.trunk} — {asked.get('why') or 'its run is stuck'}", None
    tip = heads.get(ref)
    if not tip:
        return 'stale', f'{ref} is gone from origin', None
    if tip != sha:
        return 'stale', f'{ref} moved on origin ({tip[:9]} is not the gated {sha[:9]})', None
    H.sh(['git', 'fetch', '-q', 'origin', f'+refs/heads/{ref}:refs/remotes/origin/{ref}'],
         cwd=lane.repo)
    if not lane.is_ancestor(trunk_sha, sha):
        return 'stale', f'{lane.trunk} moved to {trunk_sha[:9]}, which the batch does not contain', None
    required, why = required_set(lane, sha, trunk_sha)
    if required is None:
        return 'pending', f'required checks unknown — {why}', None
    runs = batch_runs(lane.slug, sha)
    if runs is None:
        return 'pending', 'check runs unreadable', None
    state, why = verdict(runs, required, NONVERDICT)
    if state == 'red':
        # a required job lost with its runner (a kill, an OOM: "The operation was canceled.")
        # concluded failure on the host but judged no code: a non-verdict like a cancel — its
        # jobs re-run once on the sha, then the batch is cut again; never red, never held
        lost = _runner_lost(lane, runs, required, why)
        wf = _workflow_runs(lane.slug, sha) if lost is not None else None
        if wf is not None:     # unreadable: the flake triage below re-runs it, as before
            marked, jobs, names = lost
            got = _nonverdict(lane, batch, marked, required, st, rerun_jobs=jobs, wf=wf)
            if got:
                return got
            return 'pending', f"{', '.join(names)} lost its runner — no verdict", None
    # a required job skipped behind a ``needs:`` upstream the product does not require: the
    # upstream is the cause, read off the workflow's own graph — cut short (a cancel, a
    # timeout) it judged no code: a non-verdict, re-run once then cut again; failed, it is the
    # real cause, said, and the only failure the triage and the blame below read
    behind = skipped_behind(lane, runs, required, why, sha) if state == 'red' else None
    causes = {}
    if behind:
        from asf.harvest import deploy
        causes = {r.get('name'): r.get('conclusion') for rs in behind.values() for r in rs}
        said = (f"{', '.join(sorted(behind))} skipped behind "
                + ' '.join(f'{n} ({c})' for n, c in sorted(causes.items())))
        if all(c in NONVERDICT for c in causes.values()):
            marked = [dict(r, conclusion='cancelled')
                      if deploy.job_key(r.get('name')) in behind
                      and r.get('conclusion') == 'skipped' else r for r in runs]
            jobs = sorted({_job_id(r) for rs in behind.values() for r in rs} - {None})
            got = _nonverdict(lane, batch, marked, required, st, rerun_jobs=jobs or None)
            if got:
                return got
            return 'pending', f'{said} — not required, cut short: no verdict', None
        lane.out(f"merge queue: {ref} {said} — the real cause, a job the product does not "
                 f"require; {', '.join(sorted(behind))} judged no code")
    checks = flake.batch_checks(runs)
    flake.settle(lane.product, lane.state_dir, lane.slug, sha, checks, out=lane.out)
    if state == 'green':
        batch['run_url'] = run_url(runs)
    if state == 'red':
        red = [n.split(' ', 1)[0] for n in why.split(', ')]
        # flake-vs-defect triage (asf.flake): a failed job is re-run once on the batch sha before
        # the batch is split or a member sent back — a failed required job, and a failed job a
        # required one was skipped behind (a ``needs:`` upstream: its re-run re-runs them)
        failed = root_failures(checks, why)
        if causes:      # the needs: graph named the upstream: no unrelated red rides along
            failed = [c for c in failed if c.get('name') in causes]
        defects, held = flake.triage(lane.product, lane.state_dir, lane.slug, sha, failed,
                                     where=f'batch {ref}', out=lane.out,
                                     deterministic=deterministic(lane, st, failed),
                                     required=required)
        explained = {flake.job_key(c['name']) for c in failed} | (
            _skipped(why) if failed else set())
        other = [n for n in red if n not in explained]
        if held and not defects and not other:
            state, why = 'pending', f"re-running {', '.join(held)} (flake triage)"
        else:
            return 'red', why, (checks, red)
    if state == 'pending':
        # a required check that judged no code, nothing running to replace it: re-run once,
        # then re-cut — an absent verdict is never waited out
        got = _nonverdict(lane, batch, runs, required, st)
        if got:
            return got
    if state == 'pending' and _age_s(batch) > st['timeout_min'] * 60:
        return 'timeout', f"timed out after {st['timeout_min']} min — {why}", None
    if state == 'green':
        return state, why, attested_runs(runs, required)
    return state, why, None


# ---- a red batch: whose is it -------------------------------------------------------------------
#
# A red batch used to be bisected blind: one member's real defect failed every batch it was in,
# each level of the split costing a full CI run, and the innocent members waited out every one.
# The failing job's own log usually names the defect's files (``path:line``): mapped to each
# member's diff, it names the culprit at once — the culprit goes back with the job, the step and
# those lines, the rest are cut again on the spot. Only when the log names no file that only some
# members changed does the batch split in halves. Never on a flake (``asf.flake`` re-ran the job
# and it went red again first), and never when the trunk fails the same check.

#: ``path:line`` in a CI log line: a repo path (a dot extension) and the line it names
_PATH_LINE_RE = re.compile(r'(?<![\w@./-])((?:[\w@.+-]+/)*[\w@.+-]+\.[A-Za-z0-9]{1,8}):(\d+)')
#: a repo path with no ``:line`` (at least one directory: a bare word with a dot is no path)
_BARE_PATH_RE = re.compile(r'(?<![\w@./-])((?:[\w@.+-]+/)+[\w@.+-]+\.[A-Za-z0-9]{1,8})(?!:\d)(?![\w/])')
#: a Python traceback frame — ``_PATH_LINE_RE`` cannot see it: a traceback writes ``", line 42"``,
#: never ``:42``
_TB_FILE_RE = re.compile(r'File "(?P<p>[^"\n]+\.[A-Za-z0-9]{1,8})", line (?P<n>\d+)')
#: a unittest ``FAIL``/``ERROR`` header's dotted test id — ``_BARE_PATH_RE`` cannot see it: it
#: names no path at all
_TEST_DOTTED_RE = re.compile(r'^(?:FAIL|ERROR):\s+\S+\s+\(([\w.]+)\)')
#: the tokens an added row and a finding line are compared by (:func:`_row_owners`)
_ROW_TOKEN_RE = re.compile(r'[A-Za-z0-9]{3,}')
#: how many of a failed step's output lines are read for findings
FINDING_LINES = 400
#: how many finding lines a culprit's correct brief names
BRIEF_FINDINGS = 30
#: how many distinct :func:`test_paths` candidates one check's findings keep — a stack frame per
#: level, a ``cat-file`` per segment, is the cost this bounds (:data:`asf.harvest.lane.
#: RED_TESTS_MAX` is the precedent)
TEST_PATHS_MAX = 20


def _run_id(r):
    """The workflow run id behind check run ``r`` (its details or html link), or None."""
    for link in (r.get('details_url'), r.get('html_url')):
        m = _RUN_LINK_RE.search(str(link or ''))
        if m:
            return m.group(1)
    return None


_RUN_LINK_RE = re.compile(r'/actions/runs/(\d+)')


def _workflow_runs(slug, sha):
    """The workflow runs on ``sha`` (``[{id, status, conclusion}]``), or None when unreadable."""
    data = H.gh_json(['api', f'repos/{slug}/actions/runs?head_sha={sha}&per_page=100'], None)
    runs = data.get('workflow_runs') if isinstance(data, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else None


def _ci_queue_holds(lane, ref):
    """True while the CI start queue holds a re-run of a run on ``ref`` (its ``relief`` record:
    it cancelled the run itself and re-runs it when its turn comes) — that cancel is its own."""
    try:
        relief = ci_queue.load(lane.product.name).get('relief') or ()
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return any(isinstance(r, dict) and r.get('branch') == ref for r in relief)


def _advance_member(lane, batch, f, head):
    """Move batch member ``f``'s head to ``head`` (a marker-only move, :func:`asf.harvest.lane.
    marker_move`) in the batch record and, for a lane-held member still QUEUED on this batch, on
    its lane record — so neither this judge nor the lane's own moved-head rule (R4) drops it."""
    old = f['head']
    for m in batch['members']:
        if m['branch'] == f['branch']:
            m['head'] = head
    f['head'] = head
    lane.out(f"merge queue: {f['branch']} head {old[:9]} → {head[:9]} in batch {batch['ref']} — "
             f"a report commit, same tree: the batch stands")
    rec = f.get('prev') or {}
    if rec.get('state') == lane_mod.QUEUED and rec.get('batch') == batch['ref'] \
            and f.get('run') is not None:
        lane.set(f, lane_mod.QUEUED, rec.get('reason') or f"batch {batch['ref']}",
                 batch=batch['ref'], sha=rec.get('sha'), green=rec.get('green'))


def _runner_lost(lane, runs, required, why):
    """``(runs, job ids, names)`` when every red required check of ``why`` is a job lost with
    its runner (:func:`asf.flake.infra_red`: its failure annotations all name runner loss) or a
    required job skipped behind one — ``runs`` with those checks marked ``cancelled`` (the
    non-verdict they are), the lost jobs' ids to re-run; None when any red is a real one."""
    from asf.harvest import deploy
    checks = flake.batch_checks(runs)
    failed = root_failures(checks, why)
    if not failed:
        return None
    red = {n.split(' ', 1)[0] for n in (why or '').split(', ') if n}
    skipped = _skipped(why)
    lost, jobs, read = set(), [], {}
    for c in failed:
        rid, job = flake._ids(c.get('link'))
        if not job or not (flake.infra_red(lane.slug, job, H._gh)
                           or _lost_runner(lane, rid, job, read)):
            return None
        lost.add(c.get('name'))
        jobs.append(job)
    keys = {flake.job_key(n) for n in lost}
    if not red <= (keys | skipped):
        return None
    marked = [dict(r, conclusion='cancelled')
              if r.get('name') in lost or (deploy.job_key(r.get('name')) in skipped
                                           and r.get('conclusion') == 'skipped'
                                           and deploy.job_key(r.get('name')) in required)
              else r for r in runs]
    return marked, sorted(set(jobs)), sorted(lost)


def _lost_runner(lane, rid, job, read):
    """True when job ``job`` of workflow run ``rid`` failed without ever running on a runner
    (:func:`asf.flake.classify_red`'s lost runner: no runner named, no failed step) — the jobs
    of one run read once per pass through ``read``."""
    if not rid:
        return False
    if rid not in read:
        read[rid] = flake.run_jobs(lane.slug, rid, H._gh)[1] or []
    got = [j for j in read[rid] if str(j.get('id')) == str(job)]
    return bool(got) and got[0].get('conclusion') == 'failure' and flake._lost_job(got[0])


def _job_id(r):
    """The job id of check run ``r`` (its ``/job/<id>`` link: a job id is its check run id)."""
    for link in (r.get('html_url'), r.get('details_url')):
        got = flake._ids(link)[1]
        if got:
            return got
    return None


def _nonverdict(lane, batch, runs, required, st, rerun_jobs=None, wf=None):
    """A pending batch whose required checks judged no code, with nothing queued or running on
    its sha to replace them — a cancelled (or ``timed_out``/``stale``/``startup_failure``,
    :data:`NONVERDICT`) required check, or a batch pending past ``merge_queue.stuck_min`` with
    no run at all — is never waited out. Its runs are re-run ONCE per (batch sha, job), the claim
    written to ``ci-cancels.json`` (:func:`asf.ci_queue.claim_cancel`, cause :data:`MQ_RERUN`)
    before the host is asked; a second non-verdict on that sha (the re-run cancelled again, or
    showing nothing new past :data:`RERUN_GRACE_S`) is ``('recut', why, None)``: the batch is
    dropped and its members cut again, an ALARM line and a :data:`MQ_ALARM` claim (the doctor's
    ``queue cancels`` row). Never red (no member blamed, nothing sent back) and never green.
    None: leave the batch pending (something runs, the CI start queue owns the cancel, the host
    is unreadable, a re-run was just asked)."""
    from asf.harvest import deploy
    ref, sha = batch['ref'], batch['sha']
    gone, missing = {}, []
    for name in required:
        mine = [r for r in runs or () if deploy.job_key(r.get('name')) == name]
        if not mine:
            missing.append(name)
        elif any(r.get('status') != 'completed' for r in mine):
            return None                      # a run of this job still queued or in progress
        else:
            off = [r for r in mine if r.get('conclusion') in NONVERDICT]
            if off:
                gone[name] = off
    age = _age_s(batch)
    stuck = not gone and missing and age > st['stuck_min'] * 60
    if not gone and not stuck:
        return None
    if _ci_queue_holds(lane, ref):
        return None                          # the CI start queue cancelled it: its re-run
    wf = _workflow_runs(lane.slug, sha) if wf is None else wf
    if wf is None:
        return None                          # unreadable: asked again next pass
    if any(r.get('status') != 'completed' for r in wf):
        return None                          # a newer run is queued or in progress on the sha
    jobs = sorted(gone) or sorted(missing)
    what = (', '.join(f"{n} {'/'.join(sorted({r.get('conclusion') for r in gone[n]}))}"
                      for n in sorted(gone)) if gone
            else f"{', '.join(missing)} never started — pending {int(age // 60)} min "
                 f"(merge_queue.stuck_min {st['stuck_min']}), no run queued or in progress")
    state_dir, now = lane.state_dir, ci_queue._now()
    claims = ci_queue.load_claims(state_dir)
    mine = [c for c in claims.values() if isinstance(c, dict)
            and c.get('cause') in (MQ_RERUN, MQ_REFUSED, MQ_ALARM) and c.get('sha') == sha
            and set(c.get('jobs') or ()) & set(jobs)]
    if mine:
        asked = max((ci_queue._parse(c.get('at')) for c in mine
                     if ci_queue._parse(c.get('at'))), default=None)
        ended = [ci_queue._parse(r.get('completed_at')) for n in gone for r in gone[n]]
        again = asked is not None and any(t and t > asked for t in ended)
        waited = asked is None or (now - asked).total_seconds() > RERUN_GRACE_S
        if not again and not waited:
            return None                      # the re-run was just asked: the host catches up
        members = ', '.join(f"#{m.get('pr')}" for m in batch['members'])
        why = (f'{what} again after its one re-run — no verdict on {sha[:9]}; cut again '
               f'({members}), no member blamed')
        lane.out(f'merge queue: ALARM {ref} {why}')
        ci_queue.claim_cancel(state_dir, f'mq:{sha}', MQ_ALARM, now, sha=sha, ref=ref, jobs=jobs,
                              prs=[m.get('pr') for m in batch['members']], why=what)
        return 'recut', f'no verdict — {what}, cancelled again after its one re-run', None
    gone_ids = {_run_id(r) for n in gone for r in gone[n]} - {None}
    if gone:
        ids = sorted(gone_ids) or sorted(str(r.get('id')) for r in wf
                                         if r.get('conclusion') in NONVERDICT and r.get('id'))
    else:
        ids = sorted(str(r.get('id')) for r in wf if r.get('id'))
    if not ids:
        # nothing on the host to re-run (no run was ever made on the sha): a fresh cut pushes a
        # new sha, and that push starts the run
        lane.out(f'merge queue: ALARM {ref} {what} — no run on {sha[:9]} to re-run; cut again')
        ci_queue.claim_cancel(state_dir, f'mq:{sha}', MQ_ALARM, now, sha=sha, ref=ref, jobs=jobs,
                              prs=[m.get('pr') for m in batch['members']], why=what)
        return 'recut', f'no verdict — {what}, no run on its sha', None
    # the claim first: a re-run nothing remembers would be asked again every pass
    for rid in ids:
        ci_queue.claim_cancel(state_dir, rid, MQ_RERUN, now, sha=sha, ref=ref, jobs=jobs)
    held = ci_queue.load_claims(state_dir)
    if not all((held.get(rid) or {}).get('cause') == MQ_RERUN for rid in ids):
        lane.out(f'merge queue: {ref} {what} — ci-cancels.json not written, no re-run asked')
        return None
    # only the required jobs that judged no code are re-run (``--job``): a whole-run
    # ``--failed`` would re-run every red job beside them, the ones the product does not
    # require included. A job's re-run re-runs the jobs that need it.
    jobs_of = {}
    for rid in ids:
        jobs_of[rid] = sorted({j for n in gone for r in gone[n] if _run_id(r) == rid
                               for j in [_job_id(r)] if j}) if gone else []
    if rerun_jobs:
        jobs_of = {rid: list(rerun_jobs) if i == 0 else [] for i, rid in enumerate(ids)}
    refused = []
    for rid in ids:
        if jobs_of.get(rid):
            calls = [['run', 'rerun', '--job', j, '-R', lane.slug] for j in jobs_of[rid]]
        elif rerun_jobs:
            continue
        else:
            calls = [['run', 'rerun', rid, '-R', lane.slug] + (['--failed'] if gone else [])]
        if [args for args in calls if H._gh(args)[0] != 0]:
            refused.append(rid)
    for rid in refused:
        ci_queue.claim_cancel(state_dir, rid, MQ_REFUSED, now, sha=sha, ref=ref, jobs=jobs)
    lane.out(f"merge queue: {ref} {', '.join(jobs)} "
             f"{'cancelled' if gone else 'stuck pending'} — rerun once ({what}; run(s) "
             f"{', '.join(ids)}" + (f", refused: {', '.join(refused)}" if refused else '') + ')')
    return None


def doctor_rows(product, now=None):
    """[(required, ok, detail)] — one red row per merge-queue ALARM in the last
    :data:`ALARM_WINDOW_S` (a batch whose required check judged no code twice on one sha and was
    cut again, :func:`_nonverdict`), off ``ci-cancels.json``; no ``gh`` call. No rows for a
    product not on ``merge: queue``, or with no alarm."""
    conv = getattr(product, 'conventions', None)
    if conv is None or not conv.merge_queue():
        return []
    from asf import env
    now = now or ci_queue._now()
    rows = []
    for _k, c in sorted(ci_queue.load_claims(env.state_dir(product.name)).items()):
        if isinstance(c, dict) and c.get('cause') == MQ_ALARM \
                and ci_queue._age(c.get('at'), now) <= ALARM_WINDOW_S:
            prs = ', '.join(f'#{p}' for p in c.get('prs') or ())
            rows.append((True, False, f"merge queue ALARM {c.get('ref')} @ "
                                      f"{str(c.get('sha'))[:9]}: {c.get('why')} — no verdict "
                                      f"after one re-run, cut again ({prs})"))
    return rows


#: a ``${{ … }}`` expression in a workflow value (a matrix suffix in a job's ``name:``)
_EXPR_RE = re.compile(r'\$\{\{.*?\}\}')
_JOB_KEY_RE = re.compile(r'^([A-Za-z0-9_-]+)\s*:\s*(?:#.*)?$')
_FIELD_RE = re.compile(r'^(name|needs)\s*:\s*(.*)$')


def _scalar(v):
    v = v.strip()
    if ' #' in v and not v.startswith(('"', "'")):
        v = v.split(' #', 1)[0].strip()
    return v.strip('\'"')


def workflow_jobs(text):
    """``{job id: {name, needs}}`` from a workflow file's top-level ``jobs:`` block, read line by
    line (no YAML library): each job's key at the first indent under ``jobs:``, its ``name:``
    (else its id) and ``needs:`` — a scalar, a ``[flow, list]`` or a block ``- list`` — at the
    indent under that. ``{}`` when there is no ``jobs:`` block."""
    jobs, in_jobs, job_ind, key_ind, cur, in_needs = {}, False, None, None, None, False
    for raw in str(text or '').splitlines():
        s = raw.strip()
        if not s or s.startswith('#'):
            continue
        ind = len(raw) - len(raw.lstrip(' '))
        if ind == 0:
            in_jobs, cur = s.split('#', 1)[0].strip() == 'jobs:', None
            continue
        if not in_jobs:
            continue
        if job_ind is None:
            job_ind = ind
        if ind <= job_ind:
            m = _JOB_KEY_RE.match(s) if ind == job_ind else None
            cur = m.group(1) if m else None
            if cur:
                jobs[cur] = {'name': cur, 'needs': []}
            key_ind, in_needs = None, False
            continue
        if cur is None:
            continue
        if key_ind is None:
            key_ind = ind
        if in_needs and ind >= key_ind and s.startswith('- '):
            jobs[cur]['needs'].append(_scalar(s[2:]))
            continue
        if ind != key_ind:
            continue
        in_needs = False
        m = _FIELD_RE.match(s)
        if not m:
            continue
        v = m.group(2).strip()
        if m.group(1) == 'name':
            jobs[cur]['name'] = _scalar(v) or cur
        elif v.startswith('['):
            jobs[cur]['needs'] = [_scalar(x) for x in v.split(']', 1)[0].strip('[').split(',')
                                  if x.strip()]
        elif _scalar(v):
            jobs[cur]['needs'] = [_scalar(v)]
        else:
            in_needs = True
    return jobs


def job_of(jobs, check_name):
    """The job id of :func:`workflow_jobs` a check run named ``check_name`` is a run of: its
    ``name:`` (expressions dropped) or its id, up to the first space (a matrix leg is its job);
    None when no job answers."""
    from asf.harvest import deploy
    key = deploy.job_key(check_name)
    for jid, j in jobs.items():
        label = _EXPR_RE.sub('', j.get('name') or '').strip() or jid
        if deploy.job_key(label) == key:
            return jid
    return key if key in jobs else None


def upstream_jobs(jobs, jid):
    """Every job ``jid`` needs, directly or through the jobs it needs."""
    seen, todo = set(), list((jobs.get(jid) or {}).get('needs') or ())
    while todo:
        j = todo.pop()
        if j not in seen:
            seen.add(j)
            todo.extend((jobs.get(j) or {}).get('needs') or ())
    return seen


def skipped_behind(lane, runs, required, why, sha, wf=None):
    """``{required name: [upstream check runs]}`` for a red verdict ``why`` whose every red is a
    required check ``skipped``: per skipped job, the jobs its ``needs:`` reach (the workflow file
    at ``sha``, :func:`workflow_jobs`) that completed neither green nor skipped — the cause of
    the skip. None when a red is no skip, a workflow or its graph does not read, or a skipped
    job has no such upstream (skipped on its own ``if:``): judged as before."""
    skipped = _skipped(why)
    red = {n.split(' ', 1)[0] for n in (why or '').split(', ') if n}
    if not skipped or red - skipped:
        return None
    from asf.harvest import deploy
    wf = _workflow_runs(lane.slug, sha) if wf is None else wf
    paths = {str(r.get('id')): r.get('path') for r in wf or () if r.get('path')}
    graphs, out = {}, {}
    for name in sorted(skipped):
        mine = [r for r in runs or () if deploy.job_key(r.get('name')) == name
                and r.get('conclusion') == 'skipped']
        rid = next((_run_id(r) for r in mine if _run_id(r)), None)
        path = paths.get(rid)
        if not path:
            return None
        if path not in graphs:
            got = gitops.git(['show', f'{sha}:{path}'], lane.repo)
            graphs[path] = workflow_jobs(got.stdout) if got.ok else {}
        jobs = graphs[path]
        jid = job_of(jobs, name) if jobs else None
        if not jid:
            return None
        ups = upstream_jobs(jobs, jid)
        bad = [r for r in runs or () if _run_id(r) == rid and r.get('status') == 'completed'
               and r.get('conclusion') not in ('success', 'skipped', 'neutral')
               and job_of(jobs, r.get('name')) in ups]
        if not bad:
            return None
        out[name] = bad
    return out


def _skipped(why):
    """The required names a verdict's ``why`` reads as ``skipped``."""
    return {n.split(' ', 1)[0] for n in (why or '').split(', ') if n.endswith('(skipped)')}


def root_failures(checks, why):
    """The failed checks (``bucket`` ``fail``) behind a red verdict ``why``: each failed required
    job, and — when a required job was ``skipped`` — every failed job, since a ``needs:`` upstream
    that failed (a ``rules`` job the product does not require) is what skipped it. The skipped
    jobs judged no code: the upstream's failure is the defect, and its log the evidence."""
    red = {n.split(' ', 1)[0] for n in (why or '').split(', ') if n}
    skipped = _skipped(why)
    return [c for c in checks or () if c.get('bucket') == 'fail'
            and (flake.job_key(c.get('name')) in red or skipped)]


def _red_on_trunk(lane, names):
    """``{name: sha}``: the ``names`` red on the trunk too (the host's own reading); ``{}`` when
    the host cannot say."""
    reader = getattr(getattr(lane, 'host', None), 'trunk_red', None)
    if not reader or not names:
        return {}
    try:
        return dict(reader(list(dict.fromkeys(n for n in names if n))) or {})
    except Exception:   # a reading that fails is no verdict: the batch is judged as before
        return {}


def _trunk_red_seen(lane, batch, members, names, found):
    """Record the red batch with :func:`asf.trunk_red.observe` (its PRs, its members' files, the
    files the failing logs name), then ``{name: trunk sha}`` for the ``names``
    :func:`asf.trunk_red.held` reads as trunk red — every one of them, else ``{}``. Never raises."""
    from asf import trunk_red
    product = getattr(lane, 'product', None)
    if product is None:
        return {}
    try:
        files = set()
        for f in members:
            files |= _member_files(lane, batch, f)
        trunk_red.observe(product, names, f"batch {batch['ref']}", [_pr(f) for f in members],
                          batch['sha'], batch.get('base'), files,
                          [p for item in found or () for p, _n, _t in item['paths']],
                          {trunk_red.job(i['name']): i.get('link') for i in found or ()},
                          tests={trunk_red.job(i['name']): i.get('tests') or ()
                                 for i in found or ()})
        got = trunk_red.held(product, names)
    except Exception:   # noqa: BLE001 — no reading: the batch is judged as before
        return {}
    return got if got and all(n in got for n in names) else {}


def failure_findings(slug, roots):
    """Per failed check in ``roots``: ``{name, link, step, cmd, tests, lines, paths, test_paths}``
    — its failed step's output (``lines``), the ``(path, line, text)`` each ``path:line`` in it
    names (``paths``), and, read separately (:func:`test_paths`), the failing test's own file —
    a traceback frame or a unittest header, neither of which ``paths``' two patterns can see. A
    check whose log does not read keeps all three empty. Never raises."""
    out = []
    for c in roots or ():
        link = c.get('link') or ''
        m = lane_mod._JOB_RE.search(link)
        item = {'name': c.get('name'), 'link': link, 'step': None, 'cmd': None, 'tests': [],
                'lines': [], 'paths': [], 'test_paths': []}
        out.append(item)
        if not m:
            continue
        try:
            rc, log, _e = H._gh(['api', f'repos/{slug}/actions/jobs/{m.group(1)}/logs'])
        except OSError:
            continue
        if rc != 0 or not log:
            continue
        item['lines'] = lane_mod.red_log_lines(log, FINDING_LINES)
        item['tests'] = lane_mod.red_tests(log)
        try:
            item['step'], item['cmd'] = lane_mod.red_step(slug, m.group(1))
        except Exception:
            pass
        for text in item['lines']:
            for p, n in _PATH_LINE_RE.findall(text):
                item['paths'].append((p[2:] if p.startswith('./') else p, int(n), text.strip()))
        if not item['paths']:
            # no ``path:line`` at all (a duplicate-id check names the file and the rows, never
            # a line): the bare paths, line 0 — :func:`blame` maps them by the rows each diff adds
            for text in item['lines']:
                for p in _BARE_PATH_RE.findall(text):
                    item['paths'].append((p[2:] if p.startswith('./') else p, 0, text.strip()))
        item['test_paths'] = test_paths(item)
    return out


def test_paths(item):
    """``(path, line, text)`` for every failing test's own file ``item['lines']`` names through a
    Python traceback frame or a unittest ``FAIL``/``ERROR`` header's dotted id — read separately
    from ``item['paths']`` (PD3) and never resolved against a tree here: a traceback frame is kept
    verbatim, absolute or relative, and :func:`repo_path` is the only thing that consults the
    tree, since the package may name no test directory (P14). A dotted id gives every
    ``<prefix>.py`` candidate, longest first (``tests.test_audit.AuditGap.test_x`` ->
    ``tests/test_audit/AuditGap/test_x.py``, ``tests/test_audit/AuditGap.py``,
    ``tests/test_audit.py``); a one-segment id yields none. De-duplicated on the path, first-seen
    order, capped at :data:`TEST_PATHS_MAX`. Pure: no git, no host, cannot raise."""
    out = []
    seen = set()

    def add(path, line, text):
        if path in seen or len(out) >= TEST_PATHS_MAX:
            return
        seen.add(path)
        out.append((path, line, text))

    for text in item['lines']:
        for m in _TB_FILE_RE.finditer(text):
            add(m.group('p'), int(m.group('n')), text.strip())
        m = _TEST_DOTTED_RE.match(text)
        if m:
            parts = m.group(1).split('.')
            for i in range(len(parts), 1, -1):
                add('/'.join(parts[:i]) + '.py', 0, text.strip())
    return out


def repo_path(lane, batch, path, memo=None):
    """``path`` reduced to the batch tree's own spelling: the suffixes of ``path`` (split on
    ``/``, empty segments dropped), tried from the longest, against ``git cat-file -e
    <base>:<suffix>`` — the first that is a blob at ``batch['base']`` is the answer, ``None`` when
    no suffix is. The tree is the only authority for what a path is (the package may name no test
    directory, P14); no base or no path is ``None``. Memoised on ``(lane.repo, batch['base'],
    path)`` in ``memo`` when one is given."""
    base = batch.get('base')
    if not base or not path:
        return None
    key = (lane.repo, base, path)
    if memo is not None and key in memo:
        return memo[key]
    segments = [s for s in path.split('/') if s]
    answer = None
    for i in range(len(segments)):
        suffix = '/'.join(segments[i:])
        if gitops.git(['cat-file', '-e', f'{base}:{suffix}'], lane.repo).ok:
            answer = suffix
            break
    if memo is not None:
        memo[key] = answer
    return answer


def _member_files(lane, batch, f):
    """The files member ``f`` changes against the batch's base: its own diff (``base...head``),
    else the file list the batch recorded for it."""
    base, head = batch.get('base'), f.get('head')
    if base and head:
        got = H.sh(['git', 'diff', '--name-only', f'{base}...{head}'], cwd=lane.repo)
        if got.returncode == 0:
            return {l.strip() for l in got.stdout.splitlines() if l.strip()}
    return set(f.get('files') or ())


def _owns(path, files):
    """The file of ``files`` a log ``path`` names: itself, or a file it ends with (a runner's
    absolute checkout path)."""
    if path in files:
        return path
    return next((x for x in files if path.endswith('/' + x)), None)


def _added_lines(lane, batch, f, path):
    """The lines member ``f``'s own diff (``base...head``) adds to ``path``."""
    base, head = batch.get('base'), f.get('head')
    if not (base and head):
        return []
    got = gitops.git(['diff', '--unified=0', f'{base}...{head}', '--', path], lane.repo)
    if not got.ok:
        return []
    return [l[1:] for l in (got.stdout or '').splitlines()
            if l.startswith('+') and not l.startswith('+++')]


def _row_owners(lane, batch, owners, path, text):
    """Of the members in ``owners`` (each changed ``path``), the ones whose added rows the
    finding ``text`` names: a duplicate-id check says *which rows* collide (an id, an owner, a
    number), never a line. Each member scores the tokens its added rows share with ``text`` —
    a token every owner's rows carry (a column word) tells nobody apart and counts for none; the
    best score above 0 is named. All of ``owners`` when no score tells them apart."""
    if len(owners) < 2:
        return owners
    said = set(_ROW_TOKEN_RE.findall(text.replace(path, ' ')))
    rows = {f['branch']: set(_ROW_TOKEN_RE.findall(' '.join(_added_lines(lane, batch, f, path))))
            for f in owners}
    common = set.intersection(*rows.values()) if rows else set()
    score = {b: len((r - common) & said) for b, r in rows.items()}
    best = max(score.values(), default=0)
    if best <= 0:
        return owners
    return [f for f in owners if score[f['branch']] == best]


def covers(text, test_path, files):
    """The files of ``files`` the test ``text`` (its own path ``test_path``) covers: a source
    module (a dotted extension, at least one ``/`` — no extension allow-list, PD10: the shapes
    this covers are not only ``.py``) is covered when ``text`` names its dotted module, or
    imports its leaf from its parent package, or ``test_path``'s own stem is ``test_<leaf>`` or
    starts with ``test_<leaf>_``. :func:`tools.run_tests.touched_modules`'s predicate read
    backwards (P13), reimplemented here (not called) because the package may not name the
    product's own tooling (P14) — the symmetry is the argument, not an import. Returned in
    ``files``' own sorted order. Pure: no git, no host."""
    stem = os.path.basename(test_path)
    stem = stem.rsplit('.', 1)[0] if '.' in stem else stem
    out = []
    for f in sorted(files):
        leaf_name = f.rsplit('/', 1)[-1]
        if '/' not in f or '.' not in leaf_name:
            continue
        dotted = f.rsplit('.', 1)[0].replace('/', '.')
        if dotted.endswith('.__init__'):
            dotted = dotted[:-len('.__init__')]
        parent, _, leaf = dotted.rpartition('.')
        if re.search(rf'\b{re.escape(dotted)}\b', text):
            out.append(f)
        elif parent and re.search(rf'^\s*from {re.escape(parent)} import [^\n]*\b{re.escape(leaf)}\b',
                                  text, re.M):
            out.append(f)
        elif stem == f'test_{leaf}' or stem.startswith(f'test_{leaf}_'):
            out.append(f)
    return out


def cover_blame(lane, batch, members, found):
    """``({branch: [(member file, 0, the finding's line)]}, {branch: the test path that named
    it})`` — the fallback :func:`blame` takes when the direct reading named nobody: a test file no
    member changed, read off the batch's base tree, covers one or more members' own files. Never
    raising. A ``test_paths`` candidate that is itself in any member's own diff is a direct hit
    and is skipped here — the direct reading had its say (D4). One ``memo`` for the whole call
    (PD4)."""
    named, tests = {}, {}
    memo = {}
    files_of = {f['branch']: _member_files(lane, batch, f) for f in members}
    for item in found or ():
        for path, _line, text in item.get('test_paths') or ():
            tp = repo_path(lane, batch, path, memo)
            if tp is None:
                continue
            if any(tp in files for files in files_of.values()):
                continue
            got = gitops.git(['show', f"{batch['base']}:{tp}"], lane.repo)
            if not got.ok:
                continue
            for f in members:
                for hit in covers(got.stdout, tp, files_of[f['branch']]):
                    entry = (hit, 0, text)
                    lst = named.setdefault(f['branch'], [])
                    if entry not in lst:
                        lst.append(entry)
                        tests[f['branch']] = tp
    return named, tests


def blame(lane, batch, members, found, tests=None):
    """``({branch: [(path, line, text)]}, covered)``: the members the failing jobs' logs name —
    each one a finding's file is in its own diff. When the direct reading names nobody, the
    fallback hop (:func:`cover_blame`) is tried instead — never as a peer, so ``covered`` tells
    the caller which one answered; when it is the hop, ``tests`` (given by the caller, as
    :func:`repo_path`'s ``memo``) is filled with ``{branch: the test path that named it}`` for the
    drop line, since the recorded entries hold the member's own files, never the test's. Both the
    direct reading and the hop share the same two no-verdict rules: empty (the batch splits) when
    no finding maps to a member, or — with several members — when every member is named (neither
    reading can tell them apart). An empty ``paths`` (an absolute-path-only traceback, P4) never
    short-circuits: ``named`` stays ``{}`` and falls through to the hop below."""
    paths = [x for item in found for x in item['paths']]
    named = {}
    files_of = {f['branch']: _member_files(lane, batch, f) for f in members}
    for x in paths:
        owners = [f for f in members if _owns(x[0], files_of[f['branch']])]
        if not x[1] and len(owners) > 1:   # no line: the rows the finding names say whose
            owners = _row_owners(lane, batch, owners,
                                 _owns(x[0], files_of[owners[0]['branch']]), x[2])
        for f in owners:
            hit = (_owns(x[0], files_of[f['branch']]), x[1], x[2])
            if hit not in named.setdefault(f['branch'], []):
                named[f['branch']].append(hit)
    covered = False
    if not named:
        named, hop_tests = cover_blame(lane, batch, members, found)
        covered = bool(named)
        if tests is not None:
            tests.update(hop_tests)
    if not named or (len(members) > 1 and len(named) == len(members)):
        return {}, False
    return named, covered


def culprit_text(lane, batch, f, why, found, mine, lone, fallback, covered=False, tp=None):
    """The correct round's text for the culprit ``f``: the batch and its red verdict, then per
    failed job its link, the log lines naming this PR's files (each under the heading it is listed
    under — a rule's name), else the tail of its failed step, and the named-failure brief
    (:func:`asf.harvest.lane.red_brief`: the step, the tests, the local reproduction). No log
    read: the red checks' evidence as before (``fallback``: ``(checks, names)``). ``covered``
    (and the test path ``tp`` that found it) is :func:`blame`'s own answer: a hop culprit's
    ``where`` says a test covers its files, never that the log names them (D4)."""
    ref, sha, trunk = batch['ref'], batch['sha'], lane.trunk
    if lone:
        where = f"this PR alone on {trunk} {batch['base'][:9]}"
    elif covered:
        where = (f"{len(batch['members'])} PRs on {trunk} {batch['base'][:9]}; the failing test "
                 f"{tp} covers files only this PR changes, the others are cut again without it")
    else:
        where = (f"{len(batch['members'])} PRs on {trunk} {batch['base'][:9]}; the failing job's "
                 f"log names files only this PR changes, the others are cut again without it")
    head = f"batch {ref} @ {sha[:12]} ({where}) checks red: {why}"
    mine_text = {t for _p, _n, t in mine}
    blocks = []
    for item in found:
        lines = item['lines']
        if not lines:
            continue
        named = []
        for i, l in enumerate(lines):
            if l.strip() not in mine_text:
                continue
            ind = len(l) - len(l.lstrip())
            top = next((h for h in reversed(lines[:i]) if h.strip()
                        and len(h) - len(h.lstrip()) < ind), None) if ind else None
            if top and top not in named:
                named.append(top)
            named.append(l)
        shown = (["Findings in this PR's files:"] + named[:BRIEF_FINDINGS]) if named \
            else lines[-lane_mod.RED_LOG_LINES:]
        brief = lane_mod.red_brief(item['name'], item['step'], item['tests'], item['cmd'])
        blocks.append(f"\n{item['name']} failed: {item['link']}\n" + '\n'.join(shown)
                      + '\n' + brief)
    if blocks:
        return head + ''.join(blocks)
    checks, names = fallback
    return head + lane_mod.red_evidence(lane.slug, checks, names)


def required_set(lane, sha, trunk_sha):
    """The checks the batch at ``sha`` must be green on: the product's whole required set
    (:meth:`asf.harvest.lane.GitHubHost.merge_required`: ``landing_checks`` and the deploy's
    ``required_jobs_from`` file) read at the batch sha **and** at the trunk tip, united — a
    member may add a required check, never take one away. ``(names, None)``, or ``(None, why)``
    when either reading is unreadable."""
    at_batch, why = lane.host.merge_required(lane.state_dir, lane_mod.CODE, sha)
    if at_batch is None:
        return None, why
    at_trunk, why = lane.host.merge_required(lane.state_dir, lane_mod.CODE, trunk_sha)
    if at_trunk is None:
        return None, why
    return tuple(dict.fromkeys(list(at_batch) + list(at_trunk))), None


def _trunk_conflict(lane, head):
    """``None`` when ``head`` merges onto the trunk tip alone; else the files git names in the
    conflict (a list, possibly empty when git names none). A test merge (``git merge-tree``)
    in the repo: no worktree, nothing written to a ref."""
    tip = getattr(lane, 'trunk_sha', None) or f'origin/{lane.trunk}'
    got = H.sh(['git', 'merge-tree', '--write-tree', '--name-only', '--no-messages', tip, head],
               cwd=lane.repo)
    if got.returncode != 1:     # 0 merges clean; anything else: git could not judge, no verdict
        return None
    lines = (got.stdout or '').splitlines()[1:]
    return sorted({l.strip() for l in lines if l.strip()})


def cut(lane, group, base_sha, base_ref, st, base_members=(), inherit=None):
    """Merge each entry of ``group`` ``--no-ff`` onto ``base_sha`` in a throwaway worktree, push
    the result once as a new batch ref and QUEUE the members on it. The batch record, or None
    when nothing was cut (every member conflicted, the CI queue or the push held it).

    Two entries that conflict with each other never share a batch: the later one (group order,
    priority first) is deferred — ``waits on #N (conflicting files: …)`` — and cut on a later pass,
    after the earlier one landed or went. The same for a conflict with a batch ahead
    (``base_members``): it waits on that batch's PR instead of going red.

    ``inherit`` (:func:`inherited_runs`: the green runs of a lone member's head) and a batch tree
    that is the head's own tree: the batch inherits that verdict — nothing is pushed, no CI start
    is asked, and the record carries ``inherited`` (judged green at once, :func:`judge`)."""
    trunk, out = lane.trunk, lane.out
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    holder = tempfile.mkdtemp(prefix='merge-queue-')
    tmp = os.path.join(holder, 'wt')
    try:
        add = H.sh(['git', 'worktree', 'add', '-q', '--detach', tmp, base_sha], cwd=lane.repo)
        if add.returncode != 0:
            for f in group:
                out(f"held {f['branch']}: merge queue worktree failed: {H.tail(add.stderr)}")
                _wait(lane, f, 'merge queue: worktree add failed', 'held')
            return None
        ident = _ident(lane.repo)
        members = []
        # the product's cut check (merge_queue.precut_check) judges each member as it merges — only
        # when the base itself passes it: a base already red blames nobody (the trunk's)
        checking = bool(st.get('precut_check'))
        if checking:
            base_red = precut_check_red(st, tmp)
            if base_red:
                checking = False
                out(f"merge queue: pre-cut check red on {base_ref or trunk} {base_sha[:9]} itself — "
                    f"no member is judged by it this cut ({H.tail(base_red)})")
        for f in group:
            b, head = f['branch'], f['head']
            if head and gitops.is_ancestor(lane.repo, head, base_sha) is True:
                # landed already: a merge of it is empty, and its branch leaves origin next
                out(f"merge queue: {b} (PR #{_pr(f)}) is on {base_ref or trunk} already "
                    f"({head[:12]}) — not cut again")
                if f.get('requested'):
                    drop_request(lane.state_dir, _pr(f), _owner(lane), out=out)
                continue
            subject = MERGE_SUBJECT.format(pr=_pr(f), branch=b, head=head)
            merge = H.sh(['git', 'merge', '--no-ff', '--no-edit', '-m', subject, '-m', TRAILER, head],
                         cwd=tmp, env=ident)
            if merge.returncode != 0:
                files = H.sh(['git', 'diff', '--name-only', '--diff-filter=U'],
                             cwd=tmp).stdout.split()
                H.sh(['git', 'merge', '--abort'], cwd=tmp)
                mine = set(files)
                # the trunk first: a branch that does not merge onto the trunk alone is the
                # lane's mechanical rebuild's (#570), whatever batch it was cut behind
                tfiles = _trunk_conflict(lane, head)
                if tfiles is not None:
                    out(f"merge queue: {b} (PR #{_pr(f)}) conflicts with {trunk} itself"
                        f"{' (' + ', '.join(tfiles) + ')' if tfiles else ''} — not with a batch "
                        f"ahead of it; the lane rebuilds it on {trunk}")
                    _send_back(
                        lane, f, 'conflict',
                        f"PR #{_pr(f)} does not merge onto {trunk}"
                        + (f'; conflicts in {", ".join(tfiles)}' if tfiles else '')
                        + f' — rebase the branch onto origin/{trunk} (git rebase origin/{trunk}), '
                        f'never merge; the factory publishes the rebased branch', tfiles)
                    continue
                partner = next((m for m in members if set(m.get('files') or ()) & mine), None)
                if partner is None and members:   # not by its own file list: by what the batch changed
                    changed = set(H.sh(['git', 'diff', '--name-only', base_sha, 'HEAD'],
                                       cwd=tmp).stdout.split())
                    partner = members[-1] if changed & mine else None
                ahead = next((m for m in base_members if set(m.get('files') or ()) & mine), None) \
                    if base_ref and partner is None else None
                other = partner or ahead
                if other is not None:
                    n = _pr(other) if partner else other.get('pr')
                    why = (f"waits on #{n} (conflicting files: {', '.join(files) or '?'})"
                           + (f' in batch {base_ref}' if ahead else ''))
                    out(f"merge queue: {b} (PR #{_pr(f)}) {why} — kept out of this batch, "
                        f"cut after it")
                    _wait(lane, f, f'merge queue: {why}', green=f.get('green'))
                    continue
                on = f'{base_ref} (a batch ahead of it)' if base_ref else trunk
                _send_back(
                    lane, f, 'conflict',
                    f"PR #{_pr(f)} does not merge onto {on} in the merge queue"
                    + (f'; conflicts in {", ".join(files)}' if files else '')
                    + f' — rebase the branch onto origin/{trunk} (git rebase origin/{trunk}), '
                    f'never merge; the factory publishes the rebased branch', files,
                    # a batch ahead of it is what it conflicts with: a rebase onto the trunk
                    # alone cannot clear that, so the lane does not try one
                    rebase=not base_ref)
                continue
            red = precut_check_red(st, tmp) if checking else None
            if red:
                H.sh(['git', 'reset', '-q', '--hard', 'HEAD~1'], cwd=tmp)  # client-exempt: the cut's own worktree
                on = base_ref or trunk
                if members:
                    on += f" with {', '.join(f'#{_pr(m)}' for m in members)} merged"
                out(f"merge queue: PR #{_pr(f)} ({b}) dropped from the cut — the pre-cut check is red "
                    f"once it merges onto {on} ({H.tail(red)}); the batch goes on without it")
                _send_back(
                    lane, f, 'gate',
                    f"PR #{_pr(f)} turns the product's pre-cut check red when merged onto {on} — "
                    f"dropped from the merge queue's batch before any CI run. Rebase onto "
                    f"origin/{trunk} (git rebase origin/{trunk}) and fix what it names (an id another PR or "
                    f"{trunk} already claims is taken: claim a free one):\n{red}",
                    sorted(_member_files(lane, {'base': base_sha}, f)))
                continue
            members.append(f)
        if not members:
            return None
        sha = H.sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
        ref = f"{st['ref_prefix']}{stamp}-{sha[:7]}"   # the sha names it: never a name reused
        if inherit and len(members) == 1 and _same_tree(lane.repo, sha, members[0]['head']):
            f = members[0]
            batch = _record(ref, sha, base_sha, base_ref, members)
            batch['inherited'] = {'head': f['head'], 'runs': list(inherit)}
            _queue_members(lane, members, ref, sha)
            out(f"merge queue: cut {ref} @ {sha[:12]} on {trunk} {base_sha[:9]} — PR #{_pr(f)}: "
                f"its tree is the head {f['head'][:9]}'s, green on its own run "
                f"{inherit[0]} — one tested tree lands once, no batch run")
            return batch
        guard = refguard.refusal(ref, f'push batch {ref}', trunk, refguard.listed(lane.conv),
                                 out=out)
        if guard:
            for f in members:
                _wait(lane, f, f'merge queue: {guard}', 'held')
            return None
        # the CI start queue's key is the first member's: a batch held and cut again next pass
        # keeps its place in line
        kind = st['start_kind']
        urgent = ci_queue.urgent_batch(lane.product, ref, members=members)
        if not _admits(lane, f"{kind}:{members[0]['branch']}", kind, members[0], sha,
                       run_branch=ref, urgent=urgent):
            for f in members:
                _wait(lane, f, lane_mod.CI_QUEUE, green=f.get('green'))
            return None
        started = time.monotonic()
        push = gitpush.push(['-q', 'origin', f'{sha}:refs/heads/{ref}'], tmp, refs_only=True,
                            timeout=gitpush.push_timeout(lane.conv), log=out, guard=refguard.guard_from(trunk, lane.conv))
        out(f'lane: push {ref} {time.monotonic() - started:.1f}s (batch)')
        if push.returncode != 0:
            why = lane_mod.push_why(push.stderr or push.stdout) or 'push refused'
            for f in members:
                out(f"held {f['branch']}: merge queue push of {ref} refused — {why}")
                _wait(lane, f, f'merge queue: push of {ref} refused: {why}', 'held',
                              green=f.get('green'))
            return None
        batch = _record(ref, sha, base_sha, base_ref, members)
        _queue_members(lane, members, ref, sha)
        out(f"merge queue: cut {ref} @ {sha[:12]} on {base_ref or trunk} {base_sha[:9]} — "
            f"{len(members)} PR(s): {', '.join(f'#{_pr(f)}' for f in members)}")
        return batch
    finally:
        H.sh(['git', 'worktree', 'remove', '--force', tmp], cwd=lane.repo)
        shutil.rmtree(holder, ignore_errors=True)


def _record(ref, sha, base_sha, base_ref, members):
    """The chain's record of a batch just cut."""
    return {'ref': ref, 'sha': sha, 'base': base_sha, 'base_ref': base_ref, 'cut_at': now_iso(),
            'members': [{'branch': f['branch'], 'pr': _pr(f), 'head': f['head'],
                         'item': f.get('item'), 'kind': f.get('kind'),
                         'class': f.get('class'), 'files': list(f.get('files') or ()),
                         'green': f.get('green'),
                         **({'requested': True} if f.get('requested') else {}),
                         **({'priority': True} if f.get('priority') else {})}
                        for f in members]}


def _queue_members(lane, members, ref, sha):
    for f in members:
        if f.get('requested'):   # no lane record: the batch and the request are its record
            lane.results[f['branch']] = 'queued'
            continue
        lane.set(f, lane_mod.QUEUED, f'batch {ref}', result='queued', batch=ref, sha=sha,
                 green=f.get('green'))


def _same_tree(repo, a, b):
    """True when commits ``a`` and ``b`` carry the same tree (False when either is unreadable)."""
    ta, tb = gitops.rev_parse(repo, f'{a}^{{tree}}'), gitops.rev_parse(repo, f'{b}^{{tree}}')
    return bool(ta) and ta == tb


def inherited_runs(lane, f, trunk_sha):
    """The green runs a lone member ``f`` brings into its batch, or None (the batch runs CI as
    ever). Its head must contain the trunk's tip — the merge onto the tip is then the head's own
    tree, the very tree the PR's run judged (a pull_request run's merge ref of an older trunk is
    that tree too: every trunk since is an ancestor of the head) — and every required check
    (:func:`required_set`, read at the head and the tip) must have concluded ``success`` on the
    head's newest runs: strict, never the admission's reading of a skipped job."""
    head = f.get('head')
    if not head or not trunk_sha or gitops.is_ancestor(lane.repo, trunk_sha, head) is not True:
        return None
    required, _why = required_set(lane, head, trunk_sha)
    if not required:
        return None
    runs = check_runs(lane.slug, head)
    if not runs:
        return None
    runs = newest(runs)
    if verdict(runs, required)[0] != 'green':
        return None
    return attested_runs(runs, required) or None


def attested_runs(runs, required):
    """The ids of the workflow runs whose jobs carry the ``required`` checks, every one of them
    concluded ``success`` — most required checks first. ``[]`` when any required check is not
    green or names no run (no attestation then)."""
    from asf.harvest import deploy
    count, order = {}, []
    for name in required or ():
        mine = [r for r in runs or () if deploy.job_key(r.get('name')) == name]
        if not mine or any(r.get('status') != 'completed' or r.get('conclusion') != 'success'
                           for r in mine):
            return []
        for r in mine:
            m = lane_mod._RUN_RE.search(r.get('html_url') or r.get('details_url') or '')
            if not m:
                return []
            if m.group(1) not in count:
                order.append(m.group(1))
            count[m.group(1)] = count.get(m.group(1), 0) + 1
    return sorted(order, key=lambda i: (-count[i], order.index(i)))


def attest(lane, sha, run_ids, tree_of=None):
    """Set :data:`ATTEST_CONTEXT` = ``success`` on ``sha``, its ``target_url`` the batch run
    (``run_ids[0]``) and its description ``ASF-Batch-Run: <id>``: the exact sha a green batch
    run gated — or, with ``tree_of`` (an inherited verdict, :func:`inherited_runs`), the PR head
    whose run judged this very tree. True when the host took it. Never raises; nothing without a
    run id."""
    if not run_ids or not sha:
        return False
    rid = run_ids[0]
    url = f'https://github.com/{lane.slug}/actions/runs/{rid}'
    more = f' (+{len(run_ids) - 1} run)' if len(run_ids) > 1 else ''
    desc = (f'{ATTEST_KEY}: {rid}{more} — required checks green on this exact tree '
            f'(PR head {tree_of[:9]})' if tree_of else
            f'{ATTEST_KEY}: {rid}{more} — required checks green at this exact sha')
    context = attestation.context(getattr(lane, 'product', None))
    try:
        rc, _o, err = H._gh(['api', '-X', 'POST', f'repos/{lane.slug}/statuses/{sha}',
                             '-f', 'state=success', '-f', f'context={context}',
                             '-f', f'target_url={url}', '-f', f'description={desc[:140]}'])
    except OSError as e:
        rc, err = 1, str(e)
    if rc != 0:
        lane.out(f'merge queue: {sha[:12]} not attested — {H.tail(err) or f"gh exited {rc}"}; '
                 'the trunk run judges it')
        return False
    lane.out(f'merge queue: attested {sha[:12]} — {context} success, {ATTEST_KEY}: {rid}')
    return True


def land(lane, batch, members, trunk_sha, runs=None):
    """Fast-forward the trunk to the green batch sha — MERGING on every member first, MERGED at
    that sha after — then close what the host did not mark merged, delete the member branches
    and the batch ref. False when the CI queue holds the trunk start or the push was refused
    (the trunk moved under it: next pass reads the batch as stale and cuts it again)."""
    trunk, out, ref, sha = lane.trunk, lane.out, batch['ref'], batch['sha']
    if not _admits(lane, f"trunk:{members[0]['branch']}", 'trunk', members[0], sha):
        for f in members:
            lane.results[f['branch']] = 'queued'
        return False
    # the attestation goes on the sha before the trunk points at it: the trunk's push run reads
    # it at its start (a status after the push would race it)
    attested = attest(lane, sha, runs, tree_of=(batch.get('inherited') or {}).get('head')) \
        if runs else False
    if attested:
        batch['attested'] = runs[0]
    for f in members:
        if not f.get('requested'):
            lane.set(f, lane_mod.MERGING, f'batch {ref} → {trunk} {sha[:12]}', sha=sha,
                     method='queue', batch=ref)
    if not lane.is_ancestor(trunk_sha, sha):    # read again just before the push
        why = f'{trunk} moved under the batch'
        push = None
    else:
        post_queue_status(lane, batch)
        started = time.monotonic()
        push = gitpush.push(['-q', 'origin', f'{sha}:refs/heads/{trunk}'], lane.repo,
                            refs_only=True, timeout=gitpush.push_timeout(lane.conv), log=out, guard=refguard.guard_from(trunk, lane.conv, door=True))
        out(f'lane: push {trunk} {time.monotonic() - started:.1f}s (merge queue)')
        why = lane_mod.push_why(push.stderr or push.stdout) or 'push refused'
    if push is None or push.returncode != 0:
        out(f'merge queue: {ref} not landed — {why}; judged again next pass')
        for f in members:
            if f.get('requested'):
                lane.results[f['branch']] = 'queued'
                continue
            lane.set(f, lane_mod.QUEUED, f'batch {ref}', result='queued', batch=ref, sha=sha,
                     green=f.get('green'))
        return False
    lane.fresh_trunk()
    _ledger(lane, batch, True, f'green: fast-forwarded {trunk} to {sha[:12]}'
            + (f" ({ATTEST_KEY}: {batch['attested']})" if batch.get('attested') else ''))
    for f in members:
        b = f['branch']
        # a landed PR's request is consumed, whoever queued it: never cut again
        drop_request(lane.state_dir, _pr(f), _owner(lane), out=lane.out)
        if not f.get('requested'):
            rec = lane.record(f, lane_mod.MERGED, 'method=queue', sha=sha, method='queue',
                              batch=ref)
            lane.write(f, rec, harvested=sha, correction=None)
            f['prev'] = rec
        lane.host.merged += 1
        out(f'landed {b} → PR #{_pr(f)} {sha} (batch {ref}){f.get("delivery_note") or ""}')
        lane.results[b] = 'landed'
        _close_if_open(lane, f, ref, sha)
        lane.delete_branch(f)
        cancelled = lane.host.cancel_ci(b)
        if cancelled:
            out(f'harvest: {b}: cancelled {cancelled} CI run(s) of the landed PR #{_pr(f)} — '
                f'the trunk run judges it now')
    _delete_ref(lane, batch)
    return True


def run_url(runs):
    """The workflow run the batch's check runs belong to (a job link cut at ``/job/``), or ''."""
    for r in runs or ():
        link = str(r.get('details_url') or r.get('html_url') or '')
        if '/actions/runs/' in link:
            return link.split('/job/', 1)[0]
    return ''


def post_queue_status(lane, batch):
    """Post :meth:`asf.conventions.Conventions.queue_status` = success on the batch sha, just
    before the trunk push: the one door. The trunk's ruleset requires that status, so a sha the
    queue did not land (a direct ``gh pr merge``, a hand push) is refused by the host itself —
    every session shares one GitHub user, so no bypass actor could tell them apart. A failed post
    is a line, not a verdict: the push goes on, and the host's refusal (if a ruleset requires the
    status) holds the batch for the next pass."""
    sha, ref = batch['sha'], batch['ref']
    context = lane.conv.queue_status()
    url = batch.get('run_url') or f'https://github.com/{lane.slug}/commit/{sha}'
    desc = f"batch {ref}: {len(batch.get('members') or ())} PR(s) green"[:140]
    rc, out, err = H._gh(['api', '-X', 'POST', f'repos/{lane.slug}/statuses/{sha}',
                          '-f', 'state=success', '-f', f'context={context}',
                          '-f', f'description={desc}', '-f', f'target_url={url}'])
    if rc != 0:
        lane.out(f'merge queue: {context} not posted on {sha[:12]} — '
                 f'{H.tail(err or out) or f"gh exit {rc}"}')
    return rc == 0


# ---- helpers ------------------------------------------------------------------------------------

def _pr(f):
    return (f.get('pr') or {}).get('number') or (f.get('prev') or {}).get('pr')


def _member_facts(batch, m, runs):
    run = runs.get(m['branch'])
    return {'branch': m['branch'], 'item': m.get('item'), 'kind': m.get('kind'),
            'head': m.get('head'), 'run': run, 'prev': lifecycle.lane_of(run) or None,
            'pr': {'number': m.get('pr'), 'state': 'OPEN'}, 'files': list(m.get('files') or ()),
            'class': m.get('class'), 'how': 'ci', 'green': m.get('green'), 'batch': batch['ref'],
            'requested': bool(m.get('requested')),
            **({'priority': True} if m.get('priority') else {})}


def _sync_priority(m, asked):
    """Set ``m``'s ``priority`` (a batch member's record, or an entry) to its request's as the
    requests file reads now: a request asked again plain, or ``--demote``d, is plain."""
    n = m.get('pr')
    n = n.get('number') if isinstance(n, dict) else n
    if n is None:
        n = (m.get('prev') or {}).get('pr')
    if n is not None and (asked.get(str(n)) or {}).get('priority'):
        m['priority'] = True
    else:
        m.pop('priority', None)


def _drafts(lane, batches):
    """The branches whose open PR is a draft: the lane's own PR read this pass when it made one
    (``lane.pr_map``), else one ``gh pr list`` while batches are in flight; unreadable is none."""
    pr_map = getattr(lane, 'pr_map', None)
    if pr_map:
        return {b for b, p in pr_map.items()
                if p.get('draft') and str(p.get('state') or 'OPEN').upper() == 'OPEN'}
    if not batches or not lane.slug:
        return set()
    got = connectors.forge().open_prs(lane.slug, fields=('headRefName', 'isDraft'))
    return {p.get('headRefName') for p in (got.data or []) if isinstance(p, dict)
            and p.get('isDraft')} if got.ok and isinstance(got.data, list) else set()


def _pack(entries, size):
    return [entries[i:i + size] for i in range(0, len(entries), max(1, size))]


def _age_s(batch):
    at = lane_mod._parse_at(batch.get('cut_at'))
    return max(0.0, time.time() - at) if at is not None else 0.0


def _ident(repo):
    """Identity env for the merge commits when the checkout has none configured, else None."""
    has = H.sh(['git', 'var', 'GIT_COMMITTER_IDENT'], cwd=repo)
    return None if has.returncode == 0 else dict(os.environ, **IDENT)


def _admits(lane, key, kind, f, sha, run_branch=None, urgent=''):
    """The CI start queue's answer for a batch or trunk start (:mod:`asf.ci_queue`), asked as
    the first member ``f`` (its item ranks the start); ``run_branch`` the batch ref its run is
    on, ``urgent`` why it has runner priority (:func:`asf.ci_queue.urgent_batch`)."""
    if lane.ci_queue is None:
        lane.ci_queue = ci_queue.Queue(lane.product, out=lane.out)
    return ci_queue.admit(lane.product, key, kind, item=f.get('item') or f['branch'],
                          items=lane.items, branch=f['branch'], files=f.get('files') or (),
                          queue=lane.ci_queue, sha=sha, run_branch=run_branch,
                          urgent=urgent).admitted


def _drop(lane, batch, why):
    lane.out(f"merge queue: batch {batch['ref']} dropped — {why}")
    _clear_rebuild(lane.state_dir, batch['ref'], _owner(lane))
    _cancel_live(lane, batch, why)
    _delete_ref(lane, batch)


def _ok_call(args):
    """:func:`asf.harvest.harvest._gh` as ``(ok, stdout)`` — the shape :mod:`asf.run_cancel`
    takes."""
    rc, out, _err = H._gh(args)
    return rc == 0, out or ''


def _cancel_live(lane, batch, why):
    """Cancel the batch ref's CI runs still queued or in progress — in the step that drops or
    replaces it: nothing will land that sha, and its run held heavy runners 10–80 min after the
    drop (2026-10-05). Each cancel is claimed first (:data:`MQ_DROPPED`, ``ci-cancels.json``) so
    the CI start queue never re-runs it. Only runs on this ref (or with no branch named) at the
    batch sha; an unreadable list or a refused cancel is a line, never a failure of the drop."""
    if batch.get('inherited') or not lane.slug:
        return 0
    ref, sha = batch['ref'], batch['sha']
    wf = _workflow_runs(lane.slug, sha)
    if not wf:
        return 0
    n = 0
    for r in wf:
        rid = r.get('id')
        if not rid or r.get('status') == 'completed' \
                or (r.get('head_branch') or ref) != ref:
            continue
        ci_queue.claim_cancel(lane.state_dir, rid, MQ_DROPPED, sha=sha, ref=ref, why=why[:200])
        done = run_cancel.cancel(_ok_call, lane.slug, rid, status=r.get('status'))
        if not done:
            lane.out(f'merge queue: cancel of run {rid} on dropped {ref} refused')
            continue
        if run_cancel.unconfirmed(done):
            lane.out(f'merge queue: {run_cancel.unconfirmed(done)}')
        lane.out(f"merge queue: cancelled run {rid} on {ref} at {sha[:9]} "
                 f"({r.get('status') or 'live'}) — the batch was dropped, nothing lands that sha")
        n += 1
    return n


def _reap_due(state_dir, now):
    """True (and the stamp written) when :data:`REAP_EVERY_S` passed since the last reap."""
    p = os.path.join(state_dir, REAP_FILE)
    try:
        with open(p, encoding='utf-8') as fh:
            last = float((json.load(fh) or {}).get('at') or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        last = 0
    if now - last < REAP_EVERY_S:
        return False
    try:
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump({'at': now}, fh)
    except OSError:
        pass
    return True


def _required_done(lane, sha, trunk_sha):
    """True when every required check on ``sha`` has its newest settled attempt ``success``
    (:func:`required_set`, :func:`batch_runs`): no run on that sha can still add a verdict
    anything waits for. False when unreadable."""
    names, _why = required_set(lane, sha, trunk_sha)
    runs = batch_runs(lane.slug, sha) if names else None
    if not names or runs is None:
        return False
    return verdict(runs, names)[0] == 'green'


def reap_leftover_runs(lane, st, batches, now=None):
    """Cancel the runs still queued or in progress on a batch ref (``merge_queue.ref_prefix``)
    no batch in flight holds — a landed or a dropped batch's leftovers (2026-10-05: a job the
    product does not require held a heavy runner 4 h after its batch ref was gone). A run whose
    head sha is the trunk's or a live batch's is cancelled only when every required check on that
    sha already concluded success (:func:`_required_done`): a landed batch's own leftover, never
    a run a verdict still waits on. Each cancel is claimed first (:data:`MQ_REAPED`) and made
    through :mod:`asf.run_cancel`. At most once per :data:`REAP_EVERY_S`; the number cancelled.
    Never raises but a rate limit."""
    if not lane.slug or not st.get('ref_prefix'):
        return 0
    now = time.time() if now is None else now
    if not _reap_due(lane.state_dir, now):
        return 0
    prefix, live = st['ref_prefix'], {b.get('ref'): b.get('sha') for b in batches or ()}
    trunk_sha = getattr(lane, 'trunk_sha', None) or gitops.rev_parse(lane.repo,
                                                                     f'origin/{lane.trunk}')
    guarded = {trunk_sha, *live.values()} - {None, ''}
    listed = []
    for status in ('queued', 'in_progress'):
        data = H.gh_json(['api', f'repos/{lane.slug}/actions/runs?status={status}&per_page=100'],
                         None)
        got = data.get('workflow_runs') if isinstance(data, dict) else None
        listed += [r for r in got or () if isinstance(r, dict)]
    n, done_on = 0, {}
    for r in listed:
        ref, sha, rid = r.get('head_branch') or '', r.get('head_sha') or '', r.get('id')
        if not rid or not ref.startswith(prefix) or ref in live \
                or r.get('status') == 'completed':
            continue
        if sha in guarded:
            if sha not in done_on:
                done_on[sha] = _required_done(lane, sha, trunk_sha)
            if not done_on[sha]:
                lane.out(f'merge queue: leftover run {rid} on {ref} kept — {sha[:9]} is '
                         f'{lane.trunk}\'s or a live batch\'s, and a required check there is '
                         f'not success yet')
                continue
        why = f'{ref} is no batch in flight (landed or dropped)'
        ci_queue.claim_cancel(lane.state_dir, rid, MQ_REAPED, sha=sha, ref=ref, why=why)
        done = run_cancel.cancel(_ok_call, lane.slug, rid, status=r.get('status'))
        if not done:
            lane.out(f'merge queue: cancel of leftover run {rid} on {ref} refused')
            continue
        if run_cancel.unconfirmed(done):
            lane.out(f'merge queue: {run_cancel.unconfirmed(done)}')
        lane.out(f"merge queue: reaped leftover run {rid} on {ref} at {sha[:9]} "
                 f"({r.get('status') or 'live'}) — {why}")
        n += 1
    return n


def _prune_landed(lane, batch, members, trunk_sha):
    """The ``members`` still to land: a member whose head the trunk already contains (an
    earlier batch landed it) is taken off the batch record — its branch deleted or its record
    moved on is no reason to drop the batch (2026-10-05: a batch dropped as "head moved" for a
    member merged minutes before). Its ``asf land`` request is consumed."""
    keep, gone = [], []
    for f in members:
        head = f.get('head')
        if head and trunk_sha and gitops.is_ancestor(lane.repo, head, trunk_sha) is True:
            gone.append(f)
        else:
            keep.append(f)
    if not gone:
        return members, []
    names = {f['branch'] for f in gone}
    batch['members'] = [m for m in batch['members'] if m.get('branch') not in names]
    for f in gone:
        lane.out(f"merge queue: {f['branch']} (PR #{_pr(f)}) pruned from {batch['ref']} — its "
                 f"head {str(f.get('head'))[:9]} is on {lane.trunk} already")
        drop_request(lane.state_dir, _pr(f), _owner(lane), out=lane.out)
    return keep, gone


def _delete_ref(lane, batch):
    if batch.get('inherited'):   # an inherited verdict pushed no batch ref
        return
    ref, sha = batch['ref'], batch['sha']
    lane.ref_push(f':refs/heads/{ref}', f'delete {ref}', lease=f'refs/heads/{ref}:{sha}',
                  done=lambda: lane.ref_gone(ref))


def _ledger(lane, batch, ok, line):
    H.record_gate(lane.state_dir, [m['branch'] for m in batch['members']], batch['sha'], ok,
                  int(_age_s(batch)), f"merge queue {batch['ref']}: {line}")


def _close_if_open(lane, f, ref, sha):
    """Close the member's PR with the landed sha unless the host marks it merged first (each
    member head is a parent of the batch, so it normally does within seconds). An unreadable
    state leaves it: the next lane pass reads the head on the trunk and closes it (T11)."""
    n = _pr(f)
    if not n:
        return
    for i in range(PR_MARK_TRIES):
        got = H.gh_json(['pr', 'view', str(n), '-R', lane.slug, '--json', 'state'], None)
        state = got.get('state') if isinstance(got, dict) else None
        if state != 'OPEN':
            return
        if i + 1 < PR_MARK_TRIES:
            time.sleep(PR_MARK_SLEEP_S)
    if lane.host.close(n, f'Landed by the factory merge queue: batch `{ref}` fast-forwarded '
                          f'{lane.trunk} to {sha[:12]}, which contains this head.'):
        lane.out(f'prs: closed PR #{n} — landed in batch {ref} at {sha[:12]}')


def _wait(lane, f, reason, *args, **kw):
    """:func:`asf.harvest.lane.wait` for a lane branch; for an ``asf land`` PR (no lane record)
    a line and a result only — its request stays, and the next pass takes it up again."""
    if f.get('requested'):
        lane.out(f"merge queue: {f['branch']} (PR #{_pr(f)}, asf land) waits — {reason}")
        lane.results[f['branch']] = 'waiting'
        return
    lane_mod.wait(lane, f, reason, *args, **kw)


def _send_back(lane, f, kind, text, files, rebase=True):
    """:func:`asf.harvest.lane.send_back` for a lane branch. An ``asf land`` PR has no session to
    go back to: its request is marked ``red`` at this head (``kind``: ``gate`` or ``conflict``)
    and is not taken again until the head moves — the PR's own author answers it."""
    if not f.get('requested'):
        lane_mod.send_back(lane, f, kind, text, files, rebase=rebase)
        return
    lane.out(f"merge queue: {f['branch']} (PR #{_pr(f)}, asf land) {kind} at "
             f"{(f.get('head') or '')[:12]} — {text}; taken again once its head moves")
    lane.results[f['branch']] = 'back'
    red = {'head': f.get('head'), 'kind': kind, 'why': text[:400], 'at': now_iso()}
    if store_on(_owner(lane)):
        _apply_requests(lane.state_dir, [('red', str(_pr(f)), red)], out=lane.out)
        return
    reqs = load_requests(lane.state_dir)
    r = reqs.get(str(_pr(f)))
    if r is not None:
        r['red'] = red
        save_requests(lane.state_dir, reqs)


# ---- `asf land <pr>`: the designed way in for a PR no factory item made ------------------------
#
# A hotfix, a CI change or a product session's own PR used to land with a direct `gh pr merge`:
# a sha nobody gated, beside the queue. `asf land <pr>` writes a request instead; the queue's next
# pass takes the PR once its required checks are success **on its exact head** (the same set and
# the same verdict as every batch, :func:`required_set` / :func:`verdict`), cuts it into a batch
# with the rest, and lands it only when the batch sha is green — the one door to the trunk. The
# request is the PR's only record (no lane record, no review session): a red head or a conflict
# marks it red until the head moves; a withdrawn request (`asf land --withdraw`) or a PR closed
# or merged elsewhere drops it.

def requests_path(state_dir):
    return os.path.join(state_dir, REQUESTS_FILE)


def load_requests(state_dir):
    """``{'<pr>': {pr, branch, at, by?, red?}}``; an unreadable file is no request."""
    try:
        with open(requests_path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    reqs = data.get('requests') if isinstance(data, dict) else None
    return {str(k): v for k, v in (reqs or {}).items()
            if isinstance(v, dict) and v.get('branch') and v.get('pr')}


def save_requests(state_dir, reqs):
    os.makedirs(state_dir, exist_ok=True)
    tmp = requests_path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'requests': reqs}, fh, sort_keys=True, indent=1)
    os.replace(tmp, requests_path(state_dir))


def _change_requests(state_dir, changes):
    """Apply ``changes`` to the requests file in one :func:`asf.state.store.update_at`, on what
    the file holds *now* — never on a copy read before a pass's host calls, which would put back
    what another process dropped and drop what it added. Each change names its request by key:
    ``('add', key, request)``, ``('drop', key)``, ``('red', key, red)``, ``('unred', key)``; a
    change to a request no longer there is skipped (withdrawn or landed meanwhile). The keys it
    changed; a :class:`asf.state.store.StoreError` propagates."""
    done = set()

    def apply(data):
        data = data if isinstance(data, dict) else {}
        reqs = data.get('requests')
        reqs = reqs if isinstance(reqs, dict) else {}
        for change in changes:
            verb, key = change[0], change[1]
            if verb == 'add':
                reqs[key] = change[2]
            elif verb == 'drop':
                if reqs.pop(key, None) is None:
                    continue
            elif not isinstance(reqs.get(key), dict):
                continue
            elif verb == 'red':
                reqs[key]['red'] = change[2]
            elif verb == 'unred':
                reqs[key].pop('red', None)
            done.add(key)
        data['requests'] = reqs
        return data
    if changes:
        store.update_at(requests_path(state_dir), apply, default={'requests': {}})
    return done


def _apply_requests(state_dir, changes, out=None):
    """:func:`_change_requests` for a pass: a file the store will not write (corrupt — copied
    aside, never overwritten — or its lock held past the timeout) is one line, and the requests
    stay as the file holds them."""
    try:
        return _change_requests(state_dir, changes)
    except store.StoreError as e:
        (out or (lambda s: print(s, flush=True)))(
            f'merge queue: {REQUESTS_FILE} not written — {e}')
        return set()


def add_request(state_dir, number, branch, by=None, priority=False, product=None):
    """Record the ``asf land`` request for PR ``number``. Under ``flags.queue_store`` a file the
    store refuses raises :class:`asf.state.store.StoreError` (the caller says so)."""
    key = str(int(number))
    req = {'pr': int(number), 'branch': branch, 'at': now_iso(), **({'by': by} if by else {}),
           **({'priority': True} if priority else {})}
    if store_on(product):
        _change_requests(state_dir, [('add', key, req)])
        return req
    reqs = load_requests(state_dir)
    reqs[key] = req
    save_requests(state_dir, reqs)
    return req


def drop_request(state_dir, number, product=None, out=None):
    """Drop PR ``number``'s request; True when there was one. Under ``flags.queue_store`` with
    ``out`` (a pass: a landing must not fail on its bookkeeping) a file the store refuses is a
    line through ``out``; without it, the refusal raises."""
    if store_on(product):
        key = str(number)
        if key not in load_requests(state_dir):   # lock-free look: nothing to drop
            return False
        changes = [('drop', key)]
        done = _apply_requests(state_dir, changes, out) if out else \
            _change_requests(state_dir, changes)
        return key in done
    reqs = load_requests(state_dir)
    if reqs.pop(str(number), None) is not None:
        save_requests(state_dir, reqs)
        return True
    return False


def demote_request(state_dir, number, product=None):
    """Make PR ``number``'s request plain (``asf land --demote``), keeping its place in time;
    True when it was a priority one. The batch it is in reads the request again next pass."""
    key = str(int(number))
    if store_on(product):
        done = []

        def demote(data):
            data = data if isinstance(data, dict) else {}
            r = (data.get('requests') or {}).get(key)
            if isinstance(r, dict) and r.pop('priority', None):
                done.append(key)
            return data
        store.update_at(requests_path(state_dir), demote, default={'requests': {}})
        return bool(done)
    reqs = load_requests(state_dir)
    if key in reqs and reqs[key].pop('priority', None):
        save_requests(state_dir, reqs)
        return True
    return False


def withdrawn_path(state_dir):
    return os.path.join(state_dir, WITHDRAWN_FILE)


def load_withdrawn(state_dir):
    """``{'<pr>': {at, head?, by?}}``: the operator's withdraws; unreadable is none."""
    try:
        with open(withdrawn_path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} \
        if isinstance(data, dict) else {}


def _write_withdrawn(state_dir, fn, product=None):
    if store_on(product):
        def apply(data):
            return fn(data if isinstance(data, dict) else {})
        store.update_at(withdrawn_path(state_dir), apply, default={})
        return
    data = fn(load_withdrawn(state_dir))
    os.makedirs(state_dir, exist_ok=True)
    tmp = withdrawn_path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, withdrawn_path(state_dir))


def mark_withdrawn(state_dir, number, head=None, by=None, product=None):
    """Hold PR ``number`` out of every batch (``asf land --withdraw``), whoever queued it — until
    ``asf land <pr>`` asks again or its head moves past ``head``."""
    mark = {'at': now_iso(), **({'head': head} if head else {}), **({'by': by} if by else {})}

    def put(data):
        data[str(int(number))] = mark
        return data
    _write_withdrawn(state_dir, put, product)


def clear_withdrawn(state_dir, numbers, product=None):
    keys = {str(n) for n in numbers}
    if not keys & set(load_withdrawn(state_dir)):
        return

    def drop(data):
        for k in keys:
            data.pop(k, None)
        return data
    _write_withdrawn(state_dir, drop, product)


def withdrawn_now(lane, heads):
    """``{'<pr>': mark}``: the withdraws that hold this pass. A mark whose PR's head moved past
    the one it was written at is spent (dropped): new work is gated again from the start."""
    marks = load_withdrawn(lane.state_dir)
    if not marks:
        return {}
    by_pr = {}
    for b, run in lifecycle.by_branch(lane.path).items():
        n = lifecycle.lane_of(run).get('pr')
        if n:
            by_pr[str(n)] = b
    for k, r in load_requests(lane.state_dir).items():
        by_pr.setdefault(k, r.get('branch'))
    spent = [k for k, m in marks.items() if m.get('head') and by_pr.get(k)
             and heads.get(by_pr[k]) and heads[by_pr[k]] != m['head']]
    if spent:
        lane.out(f"merge queue: withdraw of {', '.join(f'#{k}' for k in spent)} spent — its head "
                 f"moved since")
        try:
            clear_withdrawn(lane.state_dir, spent, _owner(lane))
        except store.StoreError as e:
            lane.out(f'merge queue: {WITHDRAWN_FILE} not written — {e}')
    return {k: m for k, m in marks.items() if k not in spent}


def ordered_requests(reqs):
    """``[(key, request)]`` in line order: the ``--priority`` requests first, then each group in
    the order it was asked."""
    return sorted(reqs.items(), key=lambda kv: (not kv[1].get('priority'),
                                                kv[1].get('at') or '', kv[0]))


def conflicts_with_trunk(lane, n):
    """True when the host reads PR ``n`` as ``mergeable: CONFLICTING`` (one lean read; UNKNOWN or
    unreadable is False)."""
    got = H.gh_json(['pr', 'view', str(n), '-R', lane.slug, '--json', 'mergeable'], None)
    return isinstance(got, dict) and got.get('mergeable') == 'CONFLICTING'


def red_requests(state_dir, head_of=None):
    """``[(pr, why)]``: the ``asf land`` requests marked red, by PR number. A red belongs to the
    head it was judged at: with ``head_of`` (``pr -> current head sha or None``) a request whose
    current head is known and is not the red's head is stale and not listed."""
    reqs = load_requests(state_dir)
    out = []
    for k, r in sorted(reqs.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
        red = r.get('red')
        if not red:
            continue
        cur = head_of(int(k)) if head_of else None
        if cur and red.get('head') and cur != red.get('head'):
            continue
        out.append((int(k), red.get('why') or 'red'))
    return out


def host_head(slug):
    """``pr -> the PR's current head sha`` read on the host (None when unreadable)."""
    def head_of(n):
        got = H.gh_json(['pr', 'view', str(n), '-R', slug, '--json', 'headRefOid'], None)
        return got.get('headRefOid') if isinstance(got, dict) else None
    return head_of


def requested_ready(lane, heads, trunk_sha, taken=()):
    """The entries for the ``asf land`` PRs whose required checks are success on their exact
    head now — in request order, none already in a batch (``taken``). A PR whose branch left
    origin is read once on the host: closed or merged elsewhere drops the request. A head marked
    red stays out until it moves; a pending one waits with a line."""
    from asf.harvest import deploy
    reqs = load_requests(lane.state_dir)
    # every change is collected here and applied once, at the end, on the file as it is then
    # (flags.queue_store): a request `asf land` adds while this loop waits on the host survives
    out, changes = [], []
    lane_runs = lifecycle.by_branch(lane.path) if reqs else {}
    for key, r in ordered_requests(reqs):
        b, n = r['branch'], r['pr']
        if b in taken:
            continue
        run = lane_runs.get(b)
        held = lifecycle.lane_of(run).get('state')
        head = heads.get(b)
        if not head:
            got = H.gh_json(['pr', 'view', str(n), '-R', lane.slug, '--json', 'state'], None)
            state = got.get('state') if isinstance(got, dict) else None
            if state and state != 'OPEN':
                lane.out(f'merge queue: asf land PR #{n} is {state.lower()} — request dropped')
                changes.append(('drop', key))
            else:
                lane.out(f'merge queue: asf land PR #{n}: {b} is not on origin — waits')
            continue
        if gitops.is_ancestor(lane.repo, head, trunk_sha) is True:
            # landed already (a batch, or a door of its own): its request is consumed — a
            # landed PR is never cut again
            lane.out(f'merge queue: asf land PR #{n} is on {lane.trunk} already ({head[:12]}) — '
                     f'request dropped')
            changes.append(('drop', key))
            continue
        if b in (getattr(lane, 'mq_drafts', None) or ()):
            lane.out(f'merge queue: asf land PR #{n} is a draft — waits until it is ready')
            continue
        if held and held not in VERDICT_STATES:
            # a branch the lane holds is cut only once its verdict is in (F-0287): a PR still
            # under review, or back with a session, moves its head again — the batch cut on it
            # would be dropped for that move, its CI run spent on a head that never lands
            lane.out(f'merge queue: asf land PR #{n} waits — {b} is {held} in the lane; a batch '
                     f'is cut only after its verdict')
            continue
        red = r.get('red') or {}
        if red.get('head') == head and not (red.get('kind') == 'checks'
                                             and _judged_before_trunk(lane, red, trunk_sha)):
            continue
        if r.get('red'):
            # the head moved: the old head's red is not this head's verdict
            r.pop('red')
            changes.append(('unred', key))
        H.sh(['git', 'fetch', '-q', 'origin', f'+refs/heads/{b}:refs/remotes/origin/{b}'],
             cwd=lane.repo)
        required, why = required_set(lane, head, trunk_sha)
        if required is None:
            lane.out(f'merge queue: asf land PR #{n} waits — required checks unknown: {why}')
            continue
        runs = check_runs(lane.slug, head)
        if runs is None:
            lane.out(f'merge queue: asf land PR #{n} waits — check runs unreadable')
            continue
        runs = newest(runs)     # a fresh run (asf.stale_ref) supersedes the stale one it replaced
        state, why = verdict(admission_runs(runs), required)
        earned = state == 'green'
        if state == 'pending' and all(w.endswith('(not started)') for w in why.split(', ')) \
                and deploy.never_started(runs, required):
            # the head's CI is done and never ran these (a path filter, a matrix the filter
            # skipped under its unexpanded name): the batch run judges them
            state, why = 'green', ''
        if state == 'pending' and lane.conv.branch_kind(b) and run is not None \
                and held != lane_mod.BACK and conflicts_with_trunk(lane, n):
            # a factory branch the host reads CONFLICTING gets no CI either: the lane's own
            # conflict correction sends it to its rebuild (a rebase on the trunk), never a wait
            # on checks that will not start (F-0284)
            _conflict_back(lane, b, n, head, run)
            continue
        if state == 'pending' and not lane.conv.branch_kind(b) and conflicts_with_trunk(lane, n):
            # GitHub starts no pull_request CI on a PR that conflicts with the trunk: its checks
            # never arrive, and waiting for them would be silent for good. A factory branch is
            # the lane's own (its conflict path rebuilds it); this one is its author's.
            why = f'conflicts with {lane.trunk} — merge or rebase it'
            lane.out(f'merge queue: asf land PR #{n} red at {head[:12]} — {why}')
            r['red'] = {'head': head, 'kind': 'conflict', 'why': why, 'at': now_iso()}
            changes.append(('red', key, r['red']))
            continue
        if state == 'pending':
            # a reword, a sign-off trailer: a new head with a byte-identical tree, whose CI an
            # earlier head of this PR already passed. That green is this content's green — carried
            # rather than waited for all over again (asf.tree_green, B-0275)
            carry = tree_green.carried(lane.state_dir, n, head,
                                       tree_green.tree_at(lane.repo, head), required)
            if carry is not None:
                lane.out(f'merge queue: asf land PR #{n} green at {head[:12]} — '
                         f'{tree_green.carried_line(carry)}')
                state, why, earned = 'green', '', True
        if state == 'red':
            failed = [{'name': c.get('name'), 'link': c.get('html_url')}
                      for c in runs if c.get('status') == 'completed'
                      and c.get('conclusion') not in (None, 'success', 'skipped', 'neutral',
                                                      'cancelled')]
            # first: a red judged on a merge ref of an older trunk is no verdict — never kept
            # red, never re-run (a re-run replays the old ref): a fresh run on today's trunk
            old = stale_ref.stale(lane.product, lane.slug, failed, trunk_sha, lane.repo)
            if old and stale_ref.refresh(lane.product, lane.slug, n, head, trunk_sha, old,
                                         out=lane.out, red=failed, repo=lane.repo):
                lane.out(f'merge queue: asf land PR #{n} pending at {head[:12]} — a fresh run on '
                         f'{lane.trunk} {trunk_sha[:9]} (its red {", ".join(old)} ran on a merge '
                         f'ref from before {lane.trunk} moved)')
                if r.pop('red', None) is not None:
                    changes.append(('unred', key))
                continue
            if _land_infra(lane, n, head, failed, required):
                continue
            lost = failed and all(
                flake.infra_red(lane.slug, flake._ids(c['link'])[1], H._gh) for c in failed)
            defects, held = ([], [])
            if lost:    # only runner loss is re-run here; any other red is the head's own
                defects, held = flake.triage(lane.product, lane.state_dir, lane.slug, head,
                                             failed, where=f'asf land #{n}', out=lane.out)
            if held and not defects:
                # lost with its runner: re-run, not a red head
                lane.out(f"merge queue: asf land PR #{n} pending at {head[:12]} — re-running "
                         f"{', '.join(held)} (infra, flake triage)")
                continue
        if state == 'red':
            trunk_red = _land_red_seen(lane, n, head, trunk_sha, failed, required)
            if trunk_red:
                why = f'{why} — trunk red too ({trunk_red}), not this PR\'s'
            lane.out(f'merge queue: asf land PR #{n} red at its head {head[:12]} — {why}; '
                     f'taken once a new head is green')
            r['red'] = {'head': head, 'kind': 'checks', 'why': why, 'at': now_iso()}
            changes.append(('red', key, r['red']))
            continue
        if state != 'green':
            waiting = [c for c in runs if c.get('status') != 'completed']
            if waiting and _land_infra(lane, n, head, waiting, required, probe=True):
                continue    # a phantom run: its jobs queued or running under a run that failed
            lane.out(f'merge queue: asf land PR #{n} pending at {head[:12]} — {why}')
            continue
        if earned:  # this head's green is the next identical tree's too (B-0275)
            tree_green.remember(lane.state_dir, n, head, tree_green.tree_at(lane.repo, head),
                                required)
        files = [l for l in H.sh(['git', 'diff', '--name-only', f'{trunk_sha}...{head}'],
                                 cwd=lane.repo).stdout.splitlines() if l.strip()]
        out.append({'branch': b, 'item': lane_mod.pr_item(n), 'kind': lane.conv.branch_kind(b),
                    'head': head, 'run': None, 'prev': None,
                    'pr': {'number': n, 'state': 'OPEN'}, 'files': files,
                    'class': lane_mod.CODE, 'how': 'ci', 'green': None, 'requested': True,
                    **({'priority': True} if r.get('priority') else {})})
    if changes:
        if store_on(_owner(lane)):
            _apply_requests(lane.state_dir, changes, out=lane.out)
        else:
            for change in changes:   # the dropped ones left the loop's copy as they went
                if change[0] == 'drop':
                    reqs.pop(change[1], None)
            save_requests(lane.state_dir, reqs)
    return out


def _land_infra(lane, n, head, checks, required, probe=False):
    """True when ``checks`` of `asf land` PR ``n`` at ``head`` are one infra red
    (:func:`asf.flake.infra_class`: a phantom run or a lost runner) and the queue answered it —
    re-run once on the head, or a watchdog breach on the second — with one line, never a red
    request. ``probe``: a pending head, read at most once per probe interval. False otherwise
    (a refused re-run included): the request is judged as before."""
    try:
        infra = flake.infra_class(lane.slug, checks, required, gh=H._gh,
                                  state_dir=lane.state_dir if probe else None)
    except Exception:   # noqa: BLE001 — unreadable: judged as before
        return False
    if not infra:
        return False
    cls, rid, attempt = infra
    got = flake.infra_rerun(lane.state_dir, lane.slug, head, rid, cls, where=f'asf land #{n}',
                            out=lane.out, gh=H._gh, attempt=attempt)
    if got == 'refused':
        return False
    said = {'rerun': 're-run queued', 'held': 're-run queued, awaiting it',
            'breach': 'watchdog breach — re-run once already, not again'}
    lane.out(f'merge queue: asf land PR #{n} pending at {head[:12]} — infra red ({cls}): '
             f'{said.get(got, got)}')
    return True


def _conflict_back(lane, b, n, head, run):
    """Send factory branch ``b`` (PR ``n`` at ``head``, its lane run ``run``), which the host
    reads ``mergeable: CONFLICTING``, BACK with the lane's own ``kind=conflict`` correction
    (:meth:`asf.harvest.lane.Lane.enter_back`): the files the merge names, else the host-only
    dirty read (#44). Its rebuild rebases it on the trunk; the request stays and is judged
    again on the new head."""
    rec = lifecycle.lane_of(run)
    files = lane_mod.conflict_files(lane.repo, lane.trunk, b, fetch=False)
    f = {'branch': b, 'item': rec.get('item') or (run or {}).get('item'),
         'kind': lane.conv.branch_kind(b), 'head': head, 'run': run, 'prev': rec or None,
         'pr': {'number': n, 'state': 'OPEN'}, 'conflict': files, 'host_dirty': not files,
         'correction': None}
    lane.out(f'merge queue: asf land PR #{n} conflicts with {lane.trunk} at {head[:12]} — '
             f'back to the lane for a rebase')
    lane.enter_back(f, 'kind=conflict')


def _land_red_seen(lane, n, head, trunk_sha, failed, required):
    """Record an ``asf land`` head red on its required checks (runner loss already excluded) with
    :func:`asf.trunk_red.observe` — its diff's files, the files its failing logs name — and the
    names :func:`asf.trunk_red.held` reads as trunk red, as one string (empty when none). Its logs
    are read once per head. Never raises."""
    from asf import trunk_red
    product = getattr(lane, 'product', None)
    if product is None:
        return ''
    try:
        names = [c['name'] for c in failed
                 if not required or lane_mod.required_name(c.get('name'), required)]
        key = f'#{n}'
        if names and not trunk_red.known(product, key, head, names):
            files = [l for l in H.sh(['git', 'diff', '--name-only', f'{trunk_sha}...{head}'],
                                     cwd=lane.repo).stdout.splitlines() if l.strip()]
            roots = [c for c in failed if c['name'] in names]
            found = failure_findings(lane.slug, roots)
            trunk_red.observe(product, names, key, [n], head, trunk_sha, files,
                              [p for item in found for p, _n, _t in item['paths']],
                              {trunk_red.job(c['name']): c.get('link') for c in roots},
                              tests={trunk_red.job(i['name']): i.get('tests') or ()
                                     for i in found})
        got = trunk_red.held(product, names)
    except Exception:   # noqa: BLE001 — no reading: the request is judged as before
        return ''
    return ', '.join(sorted(got))


def _judged_before_trunk(lane, red, trunk_sha):
    """True when a request's red (``red['at']``) was judged before the trunk's tip ``trunk_sha``
    arrived: the trunk moved since, so the head is read again — a red on an old merge ref gets a
    fresh run (:mod:`asf.stale_ref`), never a red kept until the head moves."""
    try:
        at = stale_ref._epoch(red.get('at'))
        moved = stale_ref.arrival(lane.product, lane.repo, trunk_sha)
    except Exception:   # noqa: BLE001 — unreadable: the red stands
        return False
    return bool(at and moved and at < moved)


def newest(runs):
    """One check run per name — the newest (by ``started_at``; one not started yet is the
    newest): a fresh run of a check supersedes an older run of it on the same sha, the way
    :func:`asf.harvest.lane.latest_checks` judges a PR. The list's order is kept."""
    def when(r):
        at = str(r.get('started_at') or '')
        return '9999' if not at else at
    best = {}
    for i, r in enumerate(runs or ()):
        k = r.get('name')
        if k not in best or (when(r), i) >= (when(runs[best[k]]), best[k]):
            best[k] = i
    keep = set(best.values())
    return [r for i, r in enumerate(runs or ()) if i in keep]


def admission_runs(runs):
    """A land request's head runs as admission reads them: a required job the PR's own CI
    ``skipped`` (a path filter: a scripts-only change runs no suites on its PR) reads as passed,
    and so does one that never started once every run on the head has completed.
    Admission is not landing — the batch ref runs the whole matrix, and there a skipped required
    check is red (:func:`verdict`); without this a path-filtered PR could never enter the queue."""
    return [dict(r, conclusion='success') if r.get('status') == 'completed'
            and r.get('conclusion') == 'skipped' else r for r in runs or ()]


def cmd_land(args):
    """``asf land <pr>``: ask the merge queue to land PR ``<pr>`` (see the parser's help)."""
    from asf import env
    product = env.load_product(args.product)
    state_dir = env.state_dir(product)
    conv = product.conventions
    if not conv.merge_queue():
        print(f"land: {product.name} is not on conventions.merge: queue — no queue to enter")
        return 2
    n = int(args.pr)
    slug = lane_mod.repo_slug(product)
    if args.withdraw:
        # whoever queued it: a factory PR in a batch leaves it too, and stays out until asked
        # again or its head moves
        got = connectors.forge().pr(slug, n, ('headRefOid',)) if slug else None
        head = got.data.get('headRefOid') if got is not None and got.ok \
            and isinstance(got.data, dict) else None
        try:
            dropped = drop_request(state_dir, n, product)
            mark_withdrawn(state_dir, n, head=head, by=os.environ.get('USER'), product=product)
        except store.StoreError as e:
            print(f'land: PR #{n} request not withdrawn — {e}')
            return 1
        print(f"land: PR #{n} {'request ' if dropped else ''}withdrawn — out of its batch and "
              f"every next cut until `asf land {n}` asks again"
              + (f' or its head moves past {head[:9]}' if head else ''))
        return 0
    if getattr(args, 'demote', False):
        try:
            was = demote_request(state_dir, n, product)
        except store.StoreError as e:
            print(f'land: PR #{n} not demoted — {e}')
            return 1
        print(f'land: PR #{n} demoted to a plain request — its batch, if cut, reads it next pass'
              if was else f'land: PR #{n} had no priority request')
        return 0
    got = H.gh_json(['pr', 'view', str(n), '-R', slug, '--json',
                     'number,state,baseRefName,headRefName,isCrossRepository'], None)
    if not isinstance(got, dict) or not got.get('headRefName'):
        print(f'land: PR #{n} not readable on {slug}')
        return 1
    if got.get('state') != 'OPEN':
        print(f"land: PR #{n} is {str(got.get('state')).lower()}, not open")
        return 1
    if got.get('baseRefName') != conv.main:
        print(f"land: PR #{n} targets {got.get('baseRefName')}, not {conv.main}")
        return 1
    if got.get('isCrossRepository'):
        print(f'land: PR #{n} is from a fork — the queue merges branches on {slug} only')
        return 1
    try:
        add_request(state_dir, n, got['headRefName'], by=os.environ.get('USER'),
                    priority=bool(args.priority), product=product)
        clear_withdrawn(state_dir, [n], product)   # asked again: an earlier withdraw is spent
    except store.StoreError as e:
        print(f'land: PR #{n} not requested — {e}')
        return 1
    print(f"land: PR #{n} ({got['headRefName']}) requested"
          f"{' at the front of the line' if args.priority else ''} — the merge queue takes it once its "
          f"required checks are success on its exact head, and lands it when its batch is green "
          f"(asf land {n} --withdraw takes the request back)")
    return 0


LAND_HELP = ('land a PR no factory item made (a hotfix, a CI change, a session\'s own PR) through '
             'the merge queue — the one door to the trunk')
LAND_DESCRIPTION = """\
Every landing on the trunk goes through the merge queue (conventions.merge: queue).
`asf land <pr>` asks the queue to take PR <pr>: on its next pass, once the PR's required
checks (landing_checks plus the deploy's required jobs) are success on its exact head, the
queue cuts it into a batch with the factory's PRs, runs the batch's full CI, and
fast-forwards the trunk only when the batch sha is green. `--priority` (or `--front`) puts the
request ahead of the factory's PRs and the other requests in the next cut (behind the batches
already in CI, within merge_queue.inflight); `asf land <pr>` again without it, or `--demote`,
makes it plain. `--withdraw` takes the PR out of the queue and out of its batch, whoever queued
it, until asked again or its head moves. A red head or a conflict marks the request red until
the head moves; a PR closed, merged, or already on the trunk drops it.
Never `gh pr merge` onto the trunk: asf status and asf doctor flag every trunk commit
that did not come through the queue."""


def register(sub):
    import argparse
    p = sub.add_parser('land', help=LAND_HELP, description=LAND_DESCRIPTION,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('pr', type=int, help='the PR number')
    p.add_argument('--product')
    p.add_argument('--priority', '--front', dest='priority', action='store_true',
                   help='go to the front of the line: ahead of the factory\'s PRs and the other '
                        'land requests, behind batches already in CI')
    how = p.add_mutually_exclusive_group()
    how.add_argument('--withdraw', action='store_true',
                     help='take the PR out of the queue — its request, and its batch whoever '
                          'queued it — until asked again or its head moves')
    how.add_argument('--demote', action='store_true',
                     help='make a --priority request plain (a batch already cut included)')
    p.set_defaults(func=cmd_land)
    return p
