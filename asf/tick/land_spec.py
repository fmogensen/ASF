"""asf.tick.land_spec — an approved spec or plan that is not on the trunk is landed, as written.

Coders read the spec and the plan from the trunk, so a Feature whose spec review is APPROVED but
whose spec still sits on a branch stays ``spec-approved`` (:func:`asf.evidence.evidence.feature_stage`),
and one whose plan is on a branch stays ``plan-approved``/``building`` with no carrier for
``task_rows`` — both give a feeder row that launches nothing (APPROVED → LAND). This is what that
row stands for: the lane pass (:func:`asf.tick.step_wave.lane_pass`), before it moves anything,
adopts every such branch no run speaks for — through the lane (:meth:`asf.harvest.lane.Lane.adopt`).

* The branch can land as it stands — a spec/plan lane branch whose diff is documents only and
  that merges into the trunk cleanly: the lane adopts it PUSHED on a synthetic run (no session,
  no pid) and lands it like any finished spec or plan branch, under the job ``land-spec-<id>`` or
  ``land-plan-<id>``.
* Otherwise (it conflicts, it carries more than documents, or it is no lane branch at all): the
  adopted run is BACK with a ``landing-gate`` correction naming the refusal
  (:data:`asf.feeder.rows.LANDING_WHY`), and the feeder hands it to a BACK → REBASE session that
  lands the existing approved document — never rewrites it — on the lane's own branch.

A branch whose latest run is live, pushed and waiting, held by an open lane state or still owes
a correction is left alone: something already speaks for it.
"""
import subprocess

from asf.feeder import rows as feeder_rows
from asf.workers import lifecycle
from asf.workers import pool as pool_mod

JOB_PREFIX = 'land-spec-'
PLAN_JOB_PREFIX = 'land-plan-'

#: (document kind, the stage words it is wanted at, the row's carrier reader)
DOCS = (
    ('spec', ('spec-approved',), feeder_rows.spec_carrier),
    ('plan', ('plan-approved', 'building'), feeder_rows.plan_carrier),
)


def wanted(items):
    """``[(feature id, branch the document is on, doc)]`` for every open, decided, unblocked
    Feature whose approved spec — or whose plan — is on a branch, not the trunk."""
    out = []
    for f in sorted(items.values(), key=lambda v: v.get('id') or ''):
        if f.get('type') != 'feature' or f.get('decided') is not True or f.get('blocked') \
                or not feeder_rows.is_open(f) or f.get('removed') or f.get('moved_to'):
            continue
        stage_word = str(f.get('stage') or '').split(' ')[0]
        for doc, stages, read in DOCS:
            if stage_word in stages and (carrier := read(f)):
                out.append((f['id'], carrier, doc))
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
    """``('', '')`` when ``origin/<branch>`` can land as it stands — a spec/plan lane branch,
    documents only, straight commits naming ``item`` (harvest's lane refusal), merging into the
    trunk without a conflict — else ``(why, text)``: the refusal's name
    (:data:`asf.feeder.rows.LANDING_WHY`) and why, in words."""
    from asf.harvest import lane
    conv, repo, trunk = product.conventions, product.repo_dir, product.main
    if _git(repo, ['rev-parse', '--verify', '-q', f'origin/{branch}']).returncode != 0:
        return 'not on origin', f'{branch} is not on origin'
    if conv.branch_kind(branch) not in ('spec', 'plan'):
        return 'not a lane branch', f'{branch} is no spec/plan lane branch'
    files = lane.touched_files(repo, trunk, branch)
    if not files:
        return 'nothing past the trunk', f'{branch} carries nothing past the trunk'
    if lane.landing_class(product, files) != lane.DOCS:
        return 'more than documents', f'{branch} changes more than documents'
    refusal = lane.lane_refusal(repo, trunk, branch, item)
    if refusal:
        return 'lane refusal', f'{branch} is refused by the lane ({refusal[0]})'
    merged = _git(repo, ['merge-tree', '--write-tree', f'origin/{trunk}', f'origin/{branch}'])
    if merged.returncode != 0:
        return 'conflict', f'{branch} conflicts with the trunk'
    return '', ''


def adopt(product, items, now=None, out=print):
    """Adopt every approved spec or plan branch :func:`wanted` names and no run speaks for;
    returns the ``[(feature id, branch, why-not-as-is)]`` it wrote a run for."""
    from asf.harvest import lane as lane_mod
    if not product.repo_dir:
        return []
    host = lane_mod.Lane(product, items=items, out=out, now=now)
    path = pool_mod.sessions_path(product)
    by_branch = lifecycle.by_branch(path)
    owed = lifecycle.corrections(path)
    done = []
    for fid, carrier, doc in wanted(items):
        lane = feeder_rows.branch_for(product, doc, fid)
        if fid in owed or spoken_for(by_branch.get(carrier), path) \
                or spoken_for(by_branch.get(lane), path):
            continue
        why, detail = why_not_as_is(product, carrier, fid)
        job = f'{JOB_PREFIX if doc == "spec" else PLAN_JOB_PREFIX}{fid}'.lower()
        if not why:
            host.adopt(carrier, fid, doc, job=job)
            out(f'land-{doc}: {fid} — approved {doc} on {carrier} adopted by the lane')
        else:
            branch = carrier if product.conventions.branch_kind(carrier) == doc else lane
            text = (f'The {doc} for {fid} is approved but not on the trunk: it is on {carrier}, '
                    f'and {detail}. Land the existing approved {doc} on the trunk from {branch} — '
                    f'bring it over from {carrier} as written, resolve what stops it merging, '
                    f"and don't rewrite it.")
            host.adopt(branch, fid, doc, job=job,
                       correction={'kind': feeder_rows.LANDING_GATE, 'text': text, 'why': why})
            out(f'land-{doc}: {fid} — approved {doc} on {carrier} cannot land as it stands '
                f'({detail}): a session lands it on {branch}')
        done.append((fid, carrier, detail))
    return done
