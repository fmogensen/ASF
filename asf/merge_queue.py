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
4. **splits** a red batch: one member goes BACK to its session with the red checks and their log
   tails (the gate's own path); several are cut again as two halves, each on the trunk alone
   (not stacked: stacked, the upper half would only re-run the red whole), so every run of a
   bisection answers for its own half — whichever lands first makes the other stale, and that
   one is cut again on the new tip.

**Stacking.** Up to ``merge_queue.inflight`` batches form a chain: each is cut on the sha of the
one before it, not the trunk, so two batches overlap in CI instead of queueing. The chain's
order is the landing order; a dropped batch drops every batch stacked on it (they contain its
commits). A green batch above a pending one waits for it.

**State.** ``state/<product>/merge-queue.json`` holds the chain; each member's lane record
(QUEUED, ``batch``, ``sha``) is on its run line as every other lane state. A product not on
``merge: queue`` never reads or writes either: :func:`asf.harvest.lane.merge_prs` is unchanged.

The batch ref carries only merge commits of heads that already passed the product's pre-push
hook on their own pushes, so it is pushed ``--no-verify``; the trunk push is refs-only by nature
(the sha is already on origin). Both go from the operator's checkout, as the fast-forward landing
does, and neither goes through :mod:`asf.refguard`: the trunk push is the lane's one sanctioned
landing, and a batch ref that matches a protected pattern is refused before it is made.
"""
import datetime
import json
import os
import shutil
import tempfile
import time

from asf import ci_queue, gitpush, refguard
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
    if not data['batches'] and not ready and not any(
            lifecycle.lane_of(r).get('state') == lane_mod.QUEUED and lifecycle.lane_of(r).get('batch')
            for r in runs.values()):
        return
    lane.fresh_trunk()
    trunk_sha = H.sh(['git', 'rev-parse', f'origin/{trunk}'], cwd=lane.repo).stdout.strip()
    lane.trunk_sha = trunk_sha
    heads = lane.remote_heads()

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
                    lane_mod.wait(lane, f, f'merge queue: {what} in batch {ref}')
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
                lane_mod.wait(lane, f, f'merge queue: batch {ref} {why}')
            settled |= {f['branch'] for f in members}
            dropped.add(ref)
        elif state == 'red':
            _ledger(lane, batch, False, f'red: {why}')
            _drop(lane, batch, f'red on {why}')
            if len(members) == 1:
                checks, red = detail
                lane_mod.send_back(lane, members[0], 'gate',
                                   f"batch {ref} @ {batch['sha'][:12]} (this PR alone on {trunk} "
                                   f"{batch['base'][:9]}) checks red: {why}"
                                   + lane_mod.red_evidence(lane.slug, checks, red), ())
            else:
                half = len(members) // 2
                lane.out(f'merge queue: {ref} splits — {len(members[:half])} and '
                         f'{len(members[half:])} PR(s), each cut on {trunk} alone')
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
            elif land(lane, batch, members, trunk_sha):
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
            lane_mod.wait(lane, f, f"merge queue: batch {rec['batch']} is not in flight — gated again")

    # ---- cut what is ready, the halves of a split first
    fresh = []
    for f in list(ready) + loose:
        if f['branch'] in taken:
            continue
        taken.add(f['branch'])
        if f in loose and heads.get(f['branch']) != f['head']:   # handed back, and moved since
            lane_mod.wait(lane, f, 'merge queue: head moved')
            continue
        fresh.append(f)
    halves = len(groups)
    for i, group in enumerate(groups + _pack(fresh, st['batch_size'])):
        if len(chain) >= st['inflight']:
            for f in group:
                lane.out(f"waiting {f['branch']}: PR #{_pr(f)} green — merge queue full "
                         f"({len(chain)} batch(es) in flight, merge_queue.inflight)")
                lane_mod.wait(lane, f, f'merge queue: {len(chain)} batch(es) in flight',
                              green=f.get('green'))
            continue
        # a split's halves are cut on the trunk, not on each other: each run then answers for
        # its own half (stacked, the upper half would re-run the red whole)
        stack = chain and i >= halves
        base_sha, base_ref = (chain[-1]['sha'], chain[-1]['ref']) if stack else (trunk_sha, None)
        batch = cut(lane, group, base_sha, base_ref, st)
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
        elif rec.get('state') != lane_mod.QUEUED or rec.get('batch') != ref:
            moved.append((f, f"left the queue ({rec.get('state') or 'no record'})"))
    if moved:
        return 'moved', ', '.join(f"{f['branch']} {w}" for f, w in moved), moved
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
    if state == 'pending' and _age_s(batch) > st['timeout_min'] * 60:
        return 'timeout', f"timed out after {st['timeout_min']} min — {why}", None
    if state == 'red':
        red = [n.split(' ', 1)[0] for n in why.split(', ')]
        checks = [{'name': r.get('name'), 'link': r.get('html_url') or r.get('details_url') or ''}
                  for r in runs]
        return 'red', why, (checks, red)
    return state, why, None


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


def cut(lane, group, base_sha, base_ref, st):
    """Merge each entry of ``group`` ``--no-ff`` onto ``base_sha`` in a throwaway worktree, push
    the result once as a new batch ref and QUEUE the members on it. The batch record, or None
    when nothing was cut (every member conflicted, the CI queue or the push held it)."""
    trunk, out = lane.trunk, lane.out
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    holder = tempfile.mkdtemp(prefix='merge-queue-')
    tmp = os.path.join(holder, 'wt')
    try:
        add = H.sh(['git', 'worktree', 'add', '-q', '--detach', tmp, base_sha], cwd=lane.repo)
        if add.returncode != 0:
            for f in group:
                out(f"held {f['branch']}: merge queue worktree failed: {H.tail(add.stderr)}")
                lane_mod.wait(lane, f, 'merge queue: worktree add failed', 'held')
            return None
        ident = _ident(lane.repo)
        members = []
        for f in group:
            b, head = f['branch'], f['head']
            subject = MERGE_SUBJECT.format(pr=_pr(f), branch=b, head=head)
            merge = H.sh(['git', 'merge', '--no-ff', '--no-edit', '-m', subject, head],
                         cwd=tmp, env=ident)
            if merge.returncode != 0:
                files = H.sh(['git', 'diff', '--name-only', '--diff-filter=U'],
                             cwd=tmp).stdout.split()
                H.sh(['git', 'merge', '--abort'], cwd=tmp)
                partner = next((m['branch'] for m in members
                                if set(m.get('files') or ()) & set(files)), None)
                on = f'{base_ref} (a batch ahead of it)' if base_ref else trunk
                lane_mod.send_back(
                    lane, f, 'conflict',
                    f"PR #{_pr(f)} does not merge onto {on} in the merge queue"
                    + (f' beside {partner}' if partner else '')
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
                lane_mod.wait(lane, f, f'merge queue: {guard}', 'held')
            return None
        # the CI start queue's key is the first member's: a batch held and cut again next pass
        # keeps its place in line
        kind = st['start_kind']
        if not _admits(lane, f"{kind}:{members[0]['branch']}", kind, members[0], sha):
            for f in members:
                lane_mod.wait(lane, f, lane_mod.CI_QUEUE, green=f.get('green'))
            return None
        started = time.monotonic()
        push = gitpush.push(['-q', 'origin', f'{sha}:refs/heads/{ref}'], tmp, refs_only=True,
                            timeout=gitpush.push_timeout(lane.conv), log=out)
        out(f'lane: push {ref} {time.monotonic() - started:.1f}s (batch)')
        if push.returncode != 0:
            why = lane_mod.push_why(push.stderr or push.stdout) or 'push refused'
            for f in members:
                out(f"held {f['branch']}: merge queue push of {ref} refused — {why}")
                lane_mod.wait(lane, f, f'merge queue: push of {ref} refused: {why}', 'held',
                              green=f.get('green'))
            return None
        batch = {'ref': ref, 'sha': sha, 'base': base_sha, 'base_ref': base_ref,
                 'cut_at': now_iso(),
                 'members': [{'branch': f['branch'], 'pr': _pr(f), 'head': f['head'],
                              'item': f.get('item'), 'kind': f.get('kind'),
                              'class': f.get('class'), 'files': list(f.get('files') or ()),
                              'green': f.get('green')} for f in members]}
        for f in members:
            lane.set(f, lane_mod.QUEUED, f'batch {ref}', result='queued', batch=ref, sha=sha,
                     green=f.get('green'))
        out(f"merge queue: cut {ref} @ {sha[:12]} on {base_ref or trunk} {base_sha[:9]} — "
            f"{len(members)} PR(s): {', '.join(f'#{_pr(f)}' for f in members)}")
        return batch
    finally:
        H.sh(['git', 'worktree', 'remove', '--force', tmp], cwd=lane.repo)
        shutil.rmtree(holder, ignore_errors=True)


def land(lane, batch, members, trunk_sha):
    """Fast-forward the trunk to the green batch sha — MERGING on every member first, MERGED at
    that sha after — then close what the host did not mark merged, delete the member branches
    and the batch ref. False when the CI queue holds the trunk start or the push was refused
    (the trunk moved under it: next pass reads the batch as stale and cuts it again)."""
    trunk, out, ref, sha = lane.trunk, lane.out, batch['ref'], batch['sha']
    if not _admits(lane, f"trunk:{members[0]['branch']}", 'trunk', members[0], sha):
        for f in members:
            lane.results[f['branch']] = 'queued'
        return False
    for f in members:
        lane.set(f, lane_mod.MERGING, f'batch {ref} → {trunk} {sha[:12]}', sha=sha,
                 method='queue', batch=ref)
    if not lane.is_ancestor(trunk_sha, sha):    # read again just before the push
        why = f'{trunk} moved under the batch'
        push = None
    else:
        started = time.monotonic()
        push = gitpush.push(['-q', 'origin', f'{sha}:refs/heads/{trunk}'], lane.repo,
                            refs_only=True, timeout=gitpush.push_timeout(lane.conv), log=out)
        out(f'lane: push {trunk} {time.monotonic() - started:.1f}s (merge queue)')
        why = lane_mod.push_why(push.stderr or push.stdout) or 'push refused'
    if push is None or push.returncode != 0:
        out(f'merge queue: {ref} not landed — {why}; judged again next pass')
        for f in members:
            lane.set(f, lane_mod.QUEUED, f'batch {ref}', result='queued', batch=ref, sha=sha,
                     green=f.get('green'))
        return False
    lane.fresh_trunk()
    _ledger(lane, batch, True, f'green: fast-forwarded {trunk} to {sha[:12]}')
    for f in members:
        b = f['branch']
        rec = lane.record(f, lane_mod.MERGED, 'method=queue', sha=sha, method='queue', batch=ref)
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


# ---- helpers ------------------------------------------------------------------------------------

def _pr(f):
    return (f.get('pr') or {}).get('number') or (f.get('prev') or {}).get('pr')


def _member_facts(batch, m, runs):
    run = runs.get(m['branch'])
    return {'branch': m['branch'], 'item': m.get('item'), 'kind': m.get('kind'),
            'head': m.get('head'), 'run': run, 'prev': lifecycle.lane_of(run) or None,
            'pr': {'number': m.get('pr'), 'state': 'OPEN'}, 'files': list(m.get('files') or ()),
            'class': m.get('class'), 'how': 'ci', 'green': m.get('green'), 'batch': batch['ref']}


def _pack(entries, size):
    return [entries[i:i + size] for i in range(0, len(entries), max(1, size))]


def _age_s(batch):
    at = lane_mod._parse_at(batch.get('cut_at'))
    return max(0.0, time.time() - at) if at is not None else 0.0


def _ident(repo):
    """Identity env for the merge commits when the checkout has none configured, else None."""
    has = H.sh(['git', 'var', 'GIT_COMMITTER_IDENT'], cwd=repo)
    return None if has.returncode == 0 else dict(os.environ, **IDENT)


def _admits(lane, key, kind, f, sha):
    """The CI start queue's answer for a batch or trunk start (:mod:`asf.ci_queue`), asked as
    the first member ``f`` (its item ranks the start)."""
    if lane.ci_queue is None:
        lane.ci_queue = ci_queue.Queue(lane.product, out=lane.out)
    return ci_queue.admit(lane.product, key, kind, item=f.get('item') or f['branch'],
                          items=lane.items, branch=f['branch'], files=f.get('files') or (),
                          queue=lane.ci_queue, sha=sha).admitted


def _drop(lane, batch, why):
    lane.out(f"merge queue: batch {batch['ref']} dropped — {why}")
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
