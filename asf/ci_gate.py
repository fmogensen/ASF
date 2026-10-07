"""asf.ci_gate — ``conventions.harvest.gate_where: ci``: the fast-forward landing gate run on the
product's own CI instead of a throwaway worktree on this host.

The fast-forward gate (:func:`asf.harvest.lane.gate_one_set`) normally builds a combined head in a
throwaway worktree and runs the product's whole test suite on it, right here, before it ever
pushes anything (:func:`asf.harvest.harvest.product_gate`). On a product whose suite takes
15-25 minutes that is 15-25 minutes of this host's CPU, next to however many sessions are running
beside it — and the product already has a CI that runs exactly this suite on hardware nobody else
is using.

Under this switch the same combined head is built the same way, but instead of gating it here it
is pushed once as a gate ref (``<ref_prefix><stamp>-<sha9>``, a sibling of
:mod:`asf.merge_queue`'s batch ref) and every member is left ``WAITING_CI`` — no test process is
started on this host at all. A product that does not set ``gate_where: ci`` never reaches this
module: :func:`enabled` is false and :func:`asf.harvest.lane.gate_one_set` runs exactly as it
always has.

**This module only cuts.** Judging the run in flight — a green gate landing the gated sha, a red
one holding its members with the correction a local gate would have given them — is the next
Task's (:func:`judge`, :func:`land`, not here). Here, a class that already holds a gate simply
stays ``WAITING_CI``: a gate cut and never judged holds its members forever, which is invisible
on the trunk only because the switch defaults off.

**State.** ``state/<product>/ci-gate.json`` — ``{"gates": {"<class>": {ref, sha, cut_at, members,
split}}}``, at most one gate per landing class. A product on ``local`` never creates this file.
"""
import datetime
import json
import os
import shutil
import tempfile
import time

from asf import gitpush, refguard
from asf.harvest import harvest as H
from asf.harvest import lane as lane_mod
from asf.workers.pool import now_iso

#: ``conventions.ci_gate`` defaults: the ref prefix the product's CI triggers on (a plain module
#: literal, never a ``DEFAULT_*`` in :mod:`asf.conventions` — PD5: a path-shaped ``DEFAULT_*``
#: there would forbid this very literal everywhere under ``asf/**/*.py``), how long a pushed ref
#: may wait for its run to start, and how long a run may stay unconcluded before it is dropped and
#: re-cut (D6: this gate's own clock, never ``gate_timeout_s`` — there is no process here to kill).
DEFAULTS = {'ref_prefix': 'gate/', 'run_wait_min': 10, 'timeout_min': 120}

#: ``state/<product>/ci-gate.json``.
STATE_FILE = 'ci-gate.json'


def settings(conv):
    """``conventions.ci_gate`` over :data:`DEFAULTS`, exactly as
    :func:`asf.merge_queue.settings` reads ``conventions.merge_queue``: a non-empty string
    prefix, a positive int per minute count; anything else keeps its default (a scalar block is
    the doctor's finding, :data:`asf.conventions.MAP_CONVENTIONS`)."""
    raw = conv.map_of('ci_gate') if hasattr(conv, 'map_of') else {}
    out = dict(DEFAULTS)
    prefix = raw.get('ref_prefix')
    if isinstance(prefix, str) and prefix.strip():
        out['ref_prefix'] = prefix.strip()
    for key in ('run_wait_min', 'timeout_min'):
        v = raw.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
            out[key] = v
    return out


def enabled(conv):
    """True under ``harvest.gate_where: ci`` — the same ``str(...).strip().lower()`` shape
    :func:`asf.harvest.lane.gate_set` already reads ``harvest_gate`` with."""
    return str(conv.gate_where).strip().lower() == 'ci'


def path(state_dir):
    return os.path.join(state_dir, STATE_FILE)


def load(state_dir):
    """The gate state as the file holds it; an unreadable or misshapen file is empty."""
    try:
        with open(path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    gates = data.get('gates') if isinstance(data, dict) else None
    return {'gates': gates if isinstance(gates, dict) else {}}


def save(state_dir, data):
    os.makedirs(state_dir, exist_ok=True)
    tmp = path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, path(state_dir))


def cut(lane, group, cls, st):
    """Push ``group``'s combined head as a new gate ref, or None when nothing was cut (every
    branch conflicted, the ref was refused, or the push was). No test process is started here:
    this is :func:`asf.harvest.lane.combined_head` (P3), pure git, and one push.

    Every conflict goes through the gate's own ``hold`` → :func:`asf.harvest.lane.send_back`
    path, so a conflicting branch goes back on the same tick with today's words. Under
    ``lane.dry_run`` nothing is cut: the worktree is never made (P9)."""
    trunk, out = lane.trunk, lane.out
    if lane.dry_run:
        out(f"DRY: would cut a {cls} gate for {len(group)} branch(es): "
            f"{', '.join(f['branch'] for f in group)}")
        return None
    holder = tempfile.mkdtemp(prefix='ci-gate-')
    tmp = os.path.join(holder, 'wt')
    try:
        add = H.sh(['git', 'worktree', 'add', '--detach', tmp, f'origin/{trunk}'], cwd=lane.repo)
        if add.returncode != 0:
            for f in group:
                out(f"held {f['branch']}: worktree add failed: {H.tail(add.stderr)}")
                lane.results[f['branch']] = 'held'
            return None

        def hold(f, kind, text, files=()):
            lane_mod.send_back(lane, f, kind, text, files)

        members = lane_mod.combined_head(tmp, trunk, group, hold)
        if not members:
            return None
        sha = H.sh(['git', 'rev-parse', 'HEAD'], cwd=tmp).stdout.strip()
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
        ref = f"{st['ref_prefix']}{stamp}-{sha[:9]}"
        guard = lane.guarded(ref, f'push gate {ref}')
        if guard:
            for f in members:
                lane_mod.wait(lane, f, f'gate: {guard}', 'held')
            return None
        started = time.monotonic()
        push = gitpush.push(['-q', 'origin', f'{sha}:refs/heads/{ref}'], tmp, refs_only=True,
                            timeout=gitpush.push_timeout(lane.conv), log=out,
                            guard=refguard.guard_from(trunk, lane.conv))
        out(f'lane: push {ref} {time.monotonic() - started:.1f}s (gate)')
        if push.returncode != 0:
            why = lane_mod.push_why(push.stderr or push.stdout) or 'push refused'
            for f in members:
                out(f"held {f['branch']}: gate push of {ref} refused — {why}")
                lane_mod.wait(lane, f, f'gate: push of {ref} refused: {why}', 'held')
            return None
        gate = {'ref': ref, 'sha': sha, 'cut_at': now_iso(),
                'members': [{'branch': f['branch'], 'head': f.get('head'), 'item': f.get('item'),
                             'class': f.get('class'), 'files': list(f.get('files') or ())}
                            for f in members],
                'split': None}
        for f in members:
            lane_mod.wait(lane, f, f'gate {ref} cut, waiting for CI', state=lane_mod.WAITING_CI)
        return gate
    finally:
        H.sh(['git', 'worktree', 'remove', '--force', tmp], cwd=lane.repo)
        shutil.rmtree(holder, ignore_errors=True)


def pass_for(lane, group, cls):
    """The gate's one entry, called from :func:`asf.harvest.lane.gate_one_set`. A class that
    already holds a gate leaves every member ``WAITING_CI`` and returns — an interim the next
    Task replaces with :func:`judge` over the run in flight; a gate cut this very pass leaves the
    same two facts (``WAITING_CI``, no second ref pushed) through the branch below instead."""
    data = load(lane.state_dir)
    existing = data['gates'].get(cls)
    if existing:
        for f in group:
            lane_mod.wait(lane, f, f"gate {existing['ref']} cut, waiting for CI",
                          state=lane_mod.WAITING_CI)
        return
    gate = cut(lane, group, cls, settings(lane.conv))
    if gate is None:
        return
    data['gates'][cls] = gate
    save(lane.state_dir, data)
