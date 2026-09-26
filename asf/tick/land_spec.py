"""asf.tick.land_spec — an approved spec that is not on the trunk is landed, as written.

Coders read the spec from the trunk, so a Feature whose spec review is APPROVED but whose spec
still sits on a branch stays ``spec-approved`` (:func:`asf.evidence.evidence.feature_stage`) and
its feeder row is APPROVED → LAND, which launches nothing. This is what that row stands for: the
lane pass (:func:`asf.tick.step_wave.lane_pass`), before it moves anything, adopts every such
branch no run speaks for — through the lane (:meth:`asf.harvest.lane.Lane.adopt`).

* The branch can land as it stands — a spec/plan lane branch whose diff is documents only and
  that merges into the trunk cleanly: the lane adopts it PUSHED on a synthetic run (no session,
  no pid) and lands it like any finished spec branch.
* Otherwise (it conflicts, it carries more than documents, or it is no lane branch at all): the
  adopted run is BACK with a ``landing-gate`` correction, and the feeder hands it to a
  STARVED → SPEC session that lands the existing approved spec — never rewrites it — on the
  lane's own branch.

A branch whose latest run is live, pushed and waiting, held by an open lane state or still owes
a correction is left alone: something already speaks for it.
"""
import subprocess

from asf.feeder import rows as feeder_rows
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

JOB_PREFIX = 'land-spec-'


def wanted(items):
    """``[(feature id, branch its approved spec is on)]`` for every open, decided, unblocked
    Feature at ``spec-approved`` whose spec is on a branch, not the trunk."""
    out = []
    for f in sorted(items.values(), key=lambda v: v.get('id') or ''):
        if f.get('type') != 'feature' or f.get('decided') is not True or f.get('blocked') \
                or not feeder_rows.is_open(f) or f.get('removed') or f.get('moved_to'):
            continue
        if str(f.get('stage') or '').split(' ')[0] != 'spec-approved':
            continue
        carrier = feeder_rows.spec_carrier(f)
        if carrier:
            out.append((f['id'], carrier))
    return out


def spoken_for(run, path):
    """Something already speaks for ``run``'s branch: a live run, one pushed and waiting to land,
    one handed to the PR lane, or a correction still owed."""
    if not run:
        return False
    from asf.harvest import lane
    return bool(lifecycle.is_live(run) or lifecycle.eligible(run) or run.get('harvest') == 'pr'
                or lifecycle.lane_of(run).get('state') in lane.OPEN_STATES
                or lifecycle.pending_correction(run, path))


def _git(repo, args):
    return subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)


def why_not_as_is(product, branch, item):
    """'' when ``origin/<branch>`` can land as it stands — a spec/plan lane branch, documents
    only, straight commits naming ``item`` (harvest's lane refusal), merging into the trunk
    without a conflict — else why not."""
    from asf.harvest import lane
    conv, repo, trunk = product.conventions, product.repo_dir, product.main
    if _git(repo, ['rev-parse', '--verify', '-q', f'origin/{branch}']).returncode != 0:
        return f'{branch} is not on origin'
    if conv.branch_kind(branch) not in ('spec', 'plan'):
        return f'{branch} is no spec/plan lane branch'
    files = lane.touched_files(repo, trunk, branch)
    if not files:
        return f'{branch} carries nothing past the trunk'
    if lane.landing_class(product, files) != lane.DOCS:
        return f'{branch} changes more than documents'
    refusal = lane.lane_refusal(repo, trunk, branch, item)
    if refusal:
        return f'{branch} is refused by the lane ({refusal[0]})'
    merged = _git(repo, ['merge-tree', '--write-tree', f'origin/{trunk}', f'origin/{branch}'])
    if merged.returncode != 0:
        return f'{branch} conflicts with the trunk'
    return ''


def adopt(product, items, now=None, out=print):
    """Adopt every approved spec branch :func:`wanted` names and no run speaks for; returns the
    ``[(feature id, branch, why-not-as-is)]`` it wrote a run for."""
    from asf.harvest import lane as lane_mod
    if not product.repo_dir:
        return []
    host = lane_mod.Lane(product, items=items, out=out, now=now)
    path = pool_mod.sessions_path(product)
    by_branch = lifecycle.by_branch(path)
    owed = lifecycle.corrections(path)
    done = []
    for fid, carrier in wanted(items):
        lane = feeder_rows.branch_for(product, 'spec', fid)
        if fid in owed or spoken_for(by_branch.get(carrier), path) \
                or spoken_for(by_branch.get(lane), path):
            continue
        why = why_not_as_is(product, carrier, fid)
        job = f'{JOB_PREFIX}{fid}'.lower()
        if not why:
            host.adopt(carrier, fid, 'spec', job=job)
            out(f'land-spec: {fid} — approved spec on {carrier} adopted by the lane')
        else:
            branch = carrier if product.conventions.branch_kind(carrier) == 'spec' else lane
            text = (f'The spec for {fid} is approved but not on the trunk: it is on {carrier}, '
                    f'and {why}. Land the existing approved spec on the trunk from {branch} — '
                    f'bring it over from {carrier} as written, resolve what stops it merging, '
                    f"and don't rewrite it.")
            host.adopt(branch, fid, 'spec', job=job,
                       correction={'kind': feeder_rows.LANDING_GATE, 'text': text})
            out(f'land-spec: {fid} — approved spec on {carrier} cannot land as it stands '
                f'({why}): a session lands it on {branch}')
        done.append((fid, carrier, why))
    return done
