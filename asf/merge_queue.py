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

**Stacking.** Up to ``merge_queue.inflight`` batches form a chain: each is cut on the sha of the
one before it, not the trunk, so two batches overlap in CI instead of queueing. The chain's
order is the landing order; a dropped batch drops every batch stacked on it (they contain its
commits). A green batch above a pending one waits for it.

**Priority.** ``asf land --priority`` goes first in line, and a full chain does not hold it: a
batch of the priority requests alone is cut on the trunk's tip beyond ``merge_queue.inflight`` and
put at the front of the chain, so it lands first (the batches behind it are cut again on the new
tip). One such batch at a time. Its CI start has runner priority
(:func:`asf.ci_queue.urgent_batch`): trunk priority, never cancelled by relief, its runners set
aside from the PR starts behind it until its jobs have them — as has any batch once the trunk has
stood still past half ``ci.trunk_stall_hours``.

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

from asf import attestation, ci_queue, flake, gitpush, refguard
from asf.harvest import harvest as H
from asf.harvest import lane as lane_mod
from asf.workers import lifecycle
from asf.workers.pool import now_iso

#: ``state/<product>/merge-queue.json``: ``{batches: [...]}``, the chain lowest first.
QUEUE_FILE = 'merge-queue.json'
#: ``conventions.merge_queue`` defaults: the ref prefix the product's CI triggers on, how many
#: PRs one batch takes, how many batches may be in flight (stacked), and how long a batch may
#: wait for its checks before it is dropped and cut again (a saturated runner pool is slow, not
#: red: the default is generous).
DEFAULTS = {'ref_prefix': 'batch/', 'batch_size': 3, 'inflight': 2, 'timeout_min': 360,
            'start_kind': 'trunk'}
#: ``merge_queue.start_kind``: how the CI start queue (:mod:`asf.ci_queue`) admits the batch
#: push — ``trunk`` (the default: the batch is the trunk's next sha, so it starts as a trunk run
#: does — trunk priority, sized over every runner, never held at the ``capacity.ci`` ceiling or
#: by the PR runner fit) or ``batch`` (the product's batch-step kind: lowest priority, held at
#: the ceiling). Which runners the run lands on is the product's workflow's to say (its
#: ``runs-on`` for a push of the batch ref), not ASF's.
START_KINDS = ('trunk', 'batch')
#: the subject of each member's merge commit on the batch ref: the trail names the members
MERGE_SUBJECT = 'merge-queue: #{pr} ({branch} @ {head})'
#: the trailer on each member's merge commit: what :mod:`asf.trunk_watch` reads a queue landing by
TRAILER = 'Landed-by: asf merge queue'
#: ``state/<product>/land-requests.json``: the PRs ``asf land`` asked the queue to take
REQUESTS_FILE = 'land-requests.json'
#: ``state/<product>/merge-queue-rebuild.json``: ``{ref: {why, at}}`` — batches the CI start
#: queue gave up on (a cancelled run on a stale workflow, or stuck); the next pass rebuilds them
REBUILD_FILE = 'merge-queue-rebuild.json'
#: the commit status context a landed batch sha carries (:func:`attest`)
ATTEST_CONTEXT = attestation.CONTEXT
#: the key the attestation's description names the batch run by (the trailer form)
ATTEST_KEY = 'ASF-Batch-Run'
#: how long a landed member's PR is given to read MERGED on the host before it is closed by hand
PR_MARK_TRIES, PR_MARK_SLEEP_S = 3, 2
#: the merge commits' identity when the checkout has none configured
IDENT = {'GIT_AUTHOR_NAME': 'asf merge queue', 'GIT_AUTHOR_EMAIL': 'asf-merge-queue@localhost',
         'GIT_COMMITTER_NAME': 'asf merge queue', 'GIT_COMMITTER_EMAIL': 'asf-merge-queue@localhost'}


# ---- settings and state -------------------------------------------------------------------------

def settings(conv):
    """``conventions.merge_queue`` over :data:`DEFAULTS`: a positive int per count, a non-empty
    string prefix; anything else keeps its default (a scalar block is the doctor's finding)."""
    raw = conv.map_of('merge_queue') if hasattr(conv, 'map_of') else {}
    out = dict(DEFAULTS)
    for key in ('batch_size', 'inflight', 'timeout_min'):
        v = raw.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            out[key] = v
    prefix = raw.get('ref_prefix')
    if isinstance(prefix, str) and prefix.strip():
        out['ref_prefix'] = prefix.strip()
    kind = str(raw.get('start_kind') or '').strip().lower()
    if kind in START_KINDS:
        out['start_kind'] = kind
    return out


def path(state_dir):
    return os.path.join(state_dir, QUEUE_FILE)


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


def save(state_dir, data):
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


def request_rebuild(state_dir, ref, why):
    """Ask the next pass to drop batch ``ref`` and cut its members again on the trunk's tip —
    a new batch sha whose push starts a fresh run on the trunk's own workflow. The CI start
    queue's answer to a batch run it can neither re-run nor replace
    (:func:`asf.ci_queue._rerun_cancelled`): a stale or stuck batch never blocks the queue.
    True when the chain holds ``ref`` (the request is written), else False."""
    if not holds(state_dir, ref):
        return False
    data = load_rebuilds(state_dir)
    data[ref] = {'why': why, 'at': now_iso()}
    _save_rebuilds(state_dir, data)
    return True


def _clear_rebuild(state_dir, ref):
    data = load_rebuilds(state_dir)
    if data.pop(ref, None) is not None:
        _save_rebuilds(state_dir, data)


# ---- the verdict on one sha ---------------------------------------------------------------------

def check_runs(slug, sha):
    """The check runs on ``sha`` (the latest attempt of each), or None when unreadable."""
    data = H.gh_json(['api', f'repos/{slug}/commits/{sha}/check-runs?per_page=100'], None)
    runs = data.get('check_runs') if isinstance(data, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else None


def verdict(runs, required):
    """``('green'|'pending'|'red', why)`` for check ``runs`` against the ``required`` names. A
    required check is green only when a run of it (its job name up to the first space: a matrix
    leg answers for its job) concluded ``success``; one still running or not created is
    pending; one ``cancelled`` is pending (it judged no code); one that concluded anything else —
    ``skipped`` included — is red. A red check the
    product does not require is never a verdict. No required names: pending, and the line says
    so — a queue with nothing to gate on lands nothing."""
    from asf.harvest import deploy
    if not required:
        return 'pending', 'no required checks named (landing_checks / required_jobs_from)'
    red, pending = [], []
    for name in required:
        mine = [r for r in runs or () if deploy.job_key(r.get('name')) == name]
        if not mine:
            pending.append(f'{name} (not started)')
            continue
        for r in mine:
            if r.get('status') != 'completed':
                pending.append(f"{name} ({r.get('status') or 'pending'})")
            elif r.get('conclusion') == 'cancelled':   # a cut-short run judged no code
                pending.append(f'{name} (cancelled)')
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
    flake triage on the batch sha it names."""
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
    for job in dict.fromkeys(jobs):
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
        H.mark_session(lane.state_dir, run.get('job') or b, correction=None)
        lane.out(f"merge queue: {b} (PR #{rec.get('pr')}) landing-gate hold cleared — {why}; "
                 f"gated again")
        out.append(b)
    return out


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

    chain, groups, loose, settled = [], [], [], set()
    dropped = set()     # the dropped batches: every batch stacked on one of them goes too
    for i, batch in enumerate(data['batches']):
        ref, members = batch['ref'], [_member_facts(batch, m, runs) for m in batch['members']]
        if batch.get('base_ref') in dropped:
            _drop(lane, batch, f"its base batch {batch['base_ref']} was dropped")
            loose.extend(members)
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
        elif state == 'stale':
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
            _drop(lane, batch, f'red on {why}')
            checks, red = detail
            roots = root_failures(checks, why) or [c for c in checks
                                                   if flake.job_key(c.get('name')) in red]
            on_trunk = _red_on_trunk(lane, [c.get('name') for c in roots] or red)
            if on_trunk:
                # the trunk fails the same check: not this batch's defect — nobody is sent back
                # and nobody is split out; the members wait and are gated again
                said = ', '.join(f'{n} @ {s[:9]}' for n, s in sorted(on_trunk.items()))
                lane.out(f'merge queue: {ref} red on {why} — red on {trunk} too ({said}): '
                         f'no member is blamed, the batch waits for {trunk} to go green')
                for f in members:
                    _wait(lane, f, f'merge queue: {", ".join(sorted(on_trunk))} red on {trunk} '
                                   f'too ({said}) — not this PR\'s', green=f.get('green'))
                settled |= {f['branch'] for f in members}
            else:
                found = failure_findings(lane.slug, roots)
                culprits = blame(lane, batch, members, found)
                if len(members) == 1:
                    culprits = culprits or {members[0]['branch']: []}
                fallback = (checks, [c.get('name') for c in roots] or red)
                for f in members:
                    if f['branch'] in culprits:
                        _send_back(lane, f, 'gate',
                                   culprit_text(lane, batch, f, why, found, culprits[f['branch']],
                                                len(members) == 1, fallback),
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
            if chain:   # a green batch above a pending one lands after it
                lane.out(f"merge queue: {ref} green, waits for {chain[-1]['ref']} below it")
                for f in members:
                    lane.results[f['branch']] = 'queued'
                chain.append(batch)
            elif land(lane, batch, members, trunk_sha, runs=detail):
                trunk_sha = batch['sha']
                lane.trunk_sha = trunk_sha
            else:
                chain.append(batch)
        # after every verdict, the unjudged still there: a crash mid-pass loses no batch
        save(lane.state_dir, {'batches': chain + data['batches'][i + 1:]})

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
    # `asf land --priority`: ahead of the factory's members and the other requests (stable: the
    # rest keep their order); only the batches already in CI stay in front of it
    ready = sorted(list(ready) + asked_ready, key=lambda f: not f.get('priority'))
    fresh = []
    for f in sorted(list(ready) + loose, key=lambda f: not f.get('priority')):
        if f['branch'] in taken:
            continue
        taken.add(f['branch'])
        if f in loose and heads.get(f['branch']) != f['head']:   # handed back, and moved since
            _wait(lane, f, 'merge queue: head moved')
            continue
        fresh.append(f)
    halves = len(groups)
    for i, group in enumerate(groups + _pack(fresh, st['batch_size'])):
        jump = (len(chain) >= st['inflight'] and any(f.get('priority') for f in group)
                and not any(m.get('priority') for b in chain for m in b['members']))
        if jump:
            # `asf land --priority` is never held behind a full chain: its requests alone are
            # cut on the trunk beyond merge_queue.inflight and go to the front of the chain
            rest = [f for f in group if not f.get('priority')]
            for f in rest:
                lane.out(f"waiting {f['branch']}: PR #{_pr(f)} green — merge queue full "
                         f"({len(chain)} batch(es) in flight, merge_queue.inflight)")
                _wait(lane, f, f'merge queue: {len(chain)} batch(es) in flight',
                      green=f.get('green'))
            group = [f for f in group if f.get('priority')]
            lane.out(f"merge queue: priority {', '.join(f'#{_pr(f)}' for f in group)} cut "
                     f"ahead of {len(chain)} batch(es) in flight (asf land --priority)")
            batch = cut(lane, group, trunk_sha, None, st)
            if batch:
                chain.insert(0, batch)
                save(lane.state_dir, {'batches': chain})
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
        batch = cut(lane, group, base_sha, base_ref, st,
                    base_members=chain[-1]['members'] if base_ref else ())
        if batch:
            chain.append(batch)
            save(lane.state_dir, {'batches': chain})
    save(lane.state_dir, {'batches': chain})


def judge(lane, batch, members, heads, trunk_sha, st):
    """``(state, why, detail)`` for one batch in flight: ``moved`` (``detail``: the
    ``(member, what)`` pairs that left), ``stale``, ``timeout``, ``red`` (``detail``: the checks
    and the red names), ``pending`` or ``green``. Fetches the batch ref so the required set can
    be read at its sha."""
    ref, sha = batch['ref'], batch['sha']
    moved = []
    for f in members:
        rec = f.get('prev') or {}
        if heads.get(f['branch']) != f['head']:
            moved.append((f, 'head moved'))
        elif f.get('requested'):   # an `asf land` PR: its request is its record
            if str(_pr(f)) not in load_requests(lane.state_dir):
                moved.append((f, 'left the queue (its asf land request was withdrawn)'))
        elif rec.get('state') != lane_mod.QUEUED or rec.get('batch') != ref:
            moved.append((f, f"left the queue ({rec.get('state') or 'no record'})"))
    if moved:
        return 'moved', ', '.join(f"{f['branch']} {w}" for f, w in moved), moved
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
    runs = check_runs(lane.slug, sha)
    if runs is None:
        return 'pending', 'check runs unreadable', None
    state, why = verdict(runs, required)
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
        defects, held = flake.triage(lane.product, lane.state_dir, lane.slug, sha, failed,
                                     where=f'batch {ref}', out=lane.out)
        explained = {flake.job_key(c['name']) for c in failed} | (
            _skipped(why) if failed else set())
        other = [n for n in red if n not in explained]
        if held and not defects and not other:
            state, why = 'pending', f"re-running {', '.join(held)} (flake triage)"
        else:
            return 'red', why, (checks, red)
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
#: how many of a failed step's output lines are read for findings
FINDING_LINES = 400
#: how many finding lines a culprit's correct brief names
BRIEF_FINDINGS = 30


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


def failure_findings(slug, roots):
    """Per failed check in ``roots``: ``{name, link, step, cmd, tests, lines, paths}`` — its
    failed step's output (``lines``) and the ``(path, line, text)`` each ``path:line`` in it names.
    A check whose log does not read keeps ``lines`` and ``paths`` empty. Never raises."""
    out = []
    for c in roots or ():
        link = c.get('link') or ''
        m = lane_mod._JOB_RE.search(link)
        item = {'name': c.get('name'), 'link': link, 'step': None, 'cmd': None, 'tests': [],
                'lines': [], 'paths': []}
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
    return out


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


def blame(lane, batch, members, found):
    """``{branch: [(path, line, text)]}``: the members the failing jobs' logs name — each one a
    finding's file is in its own diff. Empty (no verdict: the batch splits) when no finding maps
    to a member, or — with several members — when every member is named (the log cannot tell
    them apart)."""
    paths = [x for item in found for x in item['paths']]
    if not paths:
        return {}
    named = {}
    for f in members:
        files = _member_files(lane, batch, f)
        mine = [x for x in paths if _owns(x[0], files)]
        if mine:
            named[f['branch']] = [(_owns(p, files), n, t) for p, n, t in mine]
    if not named or (len(members) > 1 and len(named) == len(members)):
        return {}
    return named


def culprit_text(lane, batch, f, why, found, mine, lone, fallback):
    """The correct round's text for the culprit ``f``: the batch and its red verdict, then per
    failed job its link, the log lines naming this PR's files (each under the heading it is listed
    under — a rule's name), else the tail of its failed step, and the named-failure brief
    (:func:`asf.harvest.lane.red_brief`: the step, the tests, the local reproduction). No log
    read: the red checks' evidence as before (``fallback``: ``(checks, names)``)."""
    ref, sha, trunk = batch['ref'], batch['sha'], lane.trunk
    where = (f"this PR alone on {trunk} {batch['base'][:9]}" if lone else
             f"{len(batch['members'])} PRs on {trunk} {batch['base'][:9]}; the failing job's log "
             f"names files only this PR changes, the others are cut again without it")
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


def cut(lane, group, base_sha, base_ref, st, base_members=()):
    """Merge each entry of ``group`` ``--no-ff`` onto ``base_sha`` in a throwaway worktree, push
    the result once as a new batch ref and QUEUE the members on it. The batch record, or None
    when nothing was cut (every member conflicted, the CI queue or the push held it).

    Two entries that conflict with each other never share a batch: the later one (group order,
    priority first) is deferred — ``waits on #N (conflicting files: …)`` — and cut on a later pass,
    after the earlier one landed or went. The same for a conflict with a batch ahead
    (``base_members``): it waits on that batch's PR instead of going red."""
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
        for f in group:
            b, head = f['branch'], f['head']
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
            members.append(f)
        if not members:
            return None
        sha = H.sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
        ref = f"{st['ref_prefix']}{stamp}-{sha[:7]}"   # the sha names it: never a name reused
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
                            timeout=gitpush.push_timeout(lane.conv), log=out)
        out(f'lane: push {ref} {time.monotonic() - started:.1f}s (batch)')
        if push.returncode != 0:
            why = lane_mod.push_why(push.stderr or push.stdout) or 'push refused'
            for f in members:
                out(f"held {f['branch']}: merge queue push of {ref} refused — {why}")
                _wait(lane, f, f'merge queue: push of {ref} refused: {why}', 'held',
                              green=f.get('green'))
            return None
        batch = {'ref': ref, 'sha': sha, 'base': base_sha, 'base_ref': base_ref,
                 'cut_at': now_iso(),
                 'members': [{'branch': f['branch'], 'pr': _pr(f), 'head': f['head'],
                              'item': f.get('item'), 'kind': f.get('kind'),
                              'class': f.get('class'), 'files': list(f.get('files') or ()),
                              'green': f.get('green'),
                              **({'requested': True} if f.get('requested') else {}),
                              **({'priority': True} if f.get('priority') else {})}
                             for f in members]}
        for f in members:
            if f.get('requested'):   # no lane record: the batch and the request are its record
                lane.results[f['branch']] = 'queued'
                continue
            lane.set(f, lane_mod.QUEUED, f'batch {ref}', result='queued', batch=ref, sha=sha,
                     green=f.get('green'))
        out(f"merge queue: cut {ref} @ {sha[:12]} on {base_ref or trunk} {base_sha[:9]} — "
            f"{len(members)} PR(s): {', '.join(f'#{_pr(f)}' for f in members)}")
        return batch
    finally:
        H.sh(['git', 'worktree', 'remove', '--force', tmp], cwd=lane.repo)
        shutil.rmtree(holder, ignore_errors=True)


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


def attest(lane, sha, run_ids):
    """Set :data:`ATTEST_CONTEXT` = ``success`` on ``sha``, its ``target_url`` the batch run
    (``run_ids[0]``) and its description ``ASF-Batch-Run: <id>``: the exact sha a green batch
    run gated. True when the host took it. Never raises; nothing without a run id."""
    if not run_ids or not sha:
        return False
    rid = run_ids[0]
    url = f'https://github.com/{lane.slug}/actions/runs/{rid}'
    more = f' (+{len(run_ids) - 1} run)' if len(run_ids) > 1 else ''
    desc = f'{ATTEST_KEY}: {rid}{more} — required checks green at this exact sha'
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
    attested = attest(lane, sha, runs) if runs else False
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
                            refs_only=True, timeout=gitpush.push_timeout(lane.conv), log=out)
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
        if f.get('requested'):
            drop_request(lane.state_dir, _pr(f))
        else:
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
            'requested': bool(m.get('requested'))}


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
    _clear_rebuild(lane.state_dir, batch['ref'])
    _delete_ref(lane, batch)


def _delete_ref(lane, batch):
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
    reqs = load_requests(lane.state_dir)
    r = reqs.get(str(_pr(f)))
    if r is not None:
        r['red'] = {'head': f.get('head'), 'kind': kind, 'why': text[:400], 'at': now_iso()}
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


def add_request(state_dir, number, branch, by=None, priority=False):
    reqs = load_requests(state_dir)
    reqs[str(int(number))] = {'pr': int(number), 'branch': branch, 'at': now_iso(),
                              **({'by': by} if by else {}),
                              **({'priority': True} if priority else {})}
    save_requests(state_dir, reqs)
    return reqs[str(int(number))]


def drop_request(state_dir, number):
    reqs = load_requests(state_dir)
    if reqs.pop(str(number), None) is not None:
        save_requests(state_dir, reqs)
        return True
    return False


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
    reqs = load_requests(lane.state_dir)
    out, changed = [], False
    for key, r in ordered_requests(reqs):
        b, n = r['branch'], r['pr']
        if b in taken:
            continue
        head = heads.get(b)
        if not head:
            got = H.gh_json(['pr', 'view', str(n), '-R', lane.slug, '--json', 'state'], None)
            state = got.get('state') if isinstance(got, dict) else None
            if state and state != 'OPEN':
                lane.out(f'merge queue: asf land PR #{n} is {state.lower()} — request dropped')
                reqs.pop(key)
                changed = True
            else:
                lane.out(f'merge queue: asf land PR #{n}: {b} is not on origin — waits')
            continue
        if (r.get('red') or {}).get('head') == head:
            continue
        if r.get('red'):
            # the head moved: the old head's red is not this head's verdict
            r.pop('red')
            changed = True
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
        state, why = verdict(admission_runs(runs), required)
        if state == 'pending' and runs and all(r.get('status') == 'completed' for r in runs) \
                and all(w.endswith('(not started)') for w in why.split(', ')):
            # the head's CI is done and never ran these (a path filter, a matrix the filter
            # skipped under its unexpanded name): the batch run judges them
            state, why = 'green', ''
        if state == 'pending' and not lane.conv.branch_kind(b) and conflicts_with_trunk(lane, n):
            # GitHub starts no pull_request CI on a PR that conflicts with the trunk: its checks
            # never arrive, and waiting for them would be silent for good. A factory branch is
            # the lane's own (its conflict path rebuilds it); this one is its author's.
            why = f'conflicts with {lane.trunk} — merge or rebase it'
            lane.out(f'merge queue: asf land PR #{n} red at {head[:12]} — {why}')
            r['red'] = {'head': head, 'kind': 'conflict', 'why': why, 'at': now_iso()}
            changed = True
            continue
        if state == 'red':
            failed = [{'name': c.get('name'), 'link': c.get('html_url')}
                      for c in runs if c.get('status') == 'completed'
                      and c.get('conclusion') not in (None, 'success', 'skipped', 'neutral',
                                                      'cancelled')]
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
            lane.out(f'merge queue: asf land PR #{n} red at its head {head[:12]} — {why}; '
                     f'taken once a new head is green')
            r['red'] = {'head': head, 'kind': 'checks', 'why': why, 'at': now_iso()}
            changed = True
            continue
        if state != 'green':
            lane.out(f'merge queue: asf land PR #{n} pending at {head[:12]} — {why}')
            continue
        files = [l for l in H.sh(['git', 'diff', '--name-only', f'{trunk_sha}...{head}'],
                                 cwd=lane.repo).stdout.splitlines() if l.strip()]
        out.append({'branch': b, 'item': lane_mod.pr_item(n), 'kind': lane.conv.branch_kind(b),
                    'head': head, 'run': None, 'prev': None,
                    'pr': {'number': n, 'state': 'OPEN'}, 'files': files,
                    'class': lane_mod.CODE, 'how': 'ci', 'green': None, 'requested': True,
                    **({'priority': True} if r.get('priority') else {})})
    if changed:
        save_requests(lane.state_dir, reqs)
    return out


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
    if args.withdraw:
        print(f'land: PR #{n} request withdrawn' if drop_request(state_dir, n)
              else f'land: PR #{n} had no request')
        return 0
    slug = lane_mod.repo_slug(product)
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
    add_request(state_dir, n, got['headRefName'], by=os.environ.get('USER'),
                priority=bool(args.priority))
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
request ahead of the factory's PRs and the other requests (behind the batches already in CI). A red head or a conflict marks
the request red until the head moves; a PR closed or merged elsewhere drops it.
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
    p.add_argument('--withdraw', action='store_true', help='take the request back')
    p.set_defaults(func=cmd_land)
    return p
