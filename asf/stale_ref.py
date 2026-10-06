"""asf.stale_ref — a PR red on a merge ref the trunk has moved past is no verdict: a fresh run.

A ``pull_request`` run tests the PR's merge with the trunk **as the trunk was when the run was
created**, and a re-run (``gh run rerun``, the flake triage's) replays that same merge ref. Once
the trunk has moved, such a red judges an old trunk, not the PR on today's: a fix that landed on
the trunk since is not in it (2026-10-02, a product: two hotfix PRs stayed red ~12 h on an e2e
check the trunk had fixed at 13:26Z — their runs predated the fix, and every re-run reused the old
merge ref; the stall alarm then read the trunk as broken).

So the first step on a red PR head — an ``asf land`` request (:func:`asf.merge_queue.
requested_ready`) or a factory PR (:meth:`asf.harvest.lane.GitHubHost.head_red`, the gate) —
before any flake re-run, any correct round, any trunk-red count (:mod:`asf.trunk_red`):

* :func:`stale` — the red checks whose run is a ``pull_request`` run created before the trunk's
  current tip arrived (:func:`arrival`: when the trunk watch saw it move, else its commit time);
* :func:`refresh` — such a PR is closed and reopened (``gh pr close`` / ``gh pr reopen``): the
  ``reopened`` event starts a fresh run on a **new merge ref** with the current trunk, on the same
  head — no commit on anyone's branch, and the newest run per check is what the gate reads
  (:func:`asf.harvest.lane.latest_checks`, :func:`asf.merge_queue.newest`). While its fresh run
  has not shown the PR waits (pending, never red). A reopen that starts no run within
  :data:`WAIT_S` is given up on: the red is judged as it stands.

A reopen is a replay of every workflow on the head, so it is spent sparingly (2026-10-05: one PR
reopened 4 times in 35 min, red on the same deterministic test each time, because the trunk moves
every few minutes):

* **at most** ``conventions.flags.stale_ref_reopens`` (:data:`REOPENS_PER_HEAD`, 1) reopens per PR head commit —
  past that, a red on an older merge ref is judged as it stands; a new head starts afresh;
* **only when the trunk changed what the failing job reads** since the red run was created
  (:func:`trunk_touched`): the job's own globs in ``conventions.flags.stale_ref_job_paths`` (``{job: [glob]}``)
  when set, else the files the failing log names (``path:line``); with neither readable, any
  trunk change counts (as before).

Only a red on a fresh merge ref is a verdict. State: ``state/<product>/stale-ref.json``
(``{"<pr>": {head, tip, at, reopens, links, …}}``). Never raises.

``gh`` goes through :mod:`asf.github`, ``git`` through :mod:`asf.gitops`. A run's ``(event,
created)`` is read once a **pass** — kept in :mod:`asf.facts.cache`, which the tick, the detached
harvest and the ci-queue pass each clear at their entry — never once a process: no run read
(nor an Unknown one) outlives the pass that read it.
"""
import datetime
import fnmatch
import json
import os
import time

from asf import connectors, gitops
from asf.facts import cache as facts_cache

STATE_FILE = 'stale-ref.json'
#: how long a reopened PR's fresh run may take to show before the red is judged as it stands
WAIT_S = 1800
#: records older than this are dropped
KEEP_S = 3 * 86400
#: the :mod:`asf.facts.cache` fact a run's ``(event, created epoch)`` is kept under, a pass
RUN_FACT = 'workflow_run'
#: ``conventions.flags.stale_ref_reopens`` default: reopens for a fresh merge ref per PR head commit
REOPENS_PER_HEAD = 1


def _conv_flag(product, name, default):
    conv = getattr(product, 'conventions', None)
    try:
        return conv.flag(name, default) if conv is not None else default
    except AttributeError:
        return default


def settings(product):
    """``(reopens per head, {job: [glob]})`` from ``conventions.flags.stale_ref_reopens``
    (default :data:`REOPENS_PER_HEAD`; 0 turns reopening off) and
    ``conventions.flags.stale_ref_job_paths``."""
    n = _conv_flag(product, 'stale_ref_reopens', REOPENS_PER_HEAD)
    try:
        n = int(n) if not isinstance(n, bool) else REOPENS_PER_HEAD
    except (TypeError, ValueError):
        n = REOPENS_PER_HEAD
    n = n if n >= 0 else REOPENS_PER_HEAD
    paths = _conv_flag(product, 'stale_ref_job_paths', None)
    paths = {str(k): [str(g) for g in (v if isinstance(v, (list, tuple)) else [v]) if g]
             for k, v in paths.items()} if isinstance(paths, dict) else {}
    return n, paths


def _sd(product):
    from asf import env
    return env.state_dir(product)


def _epoch(iso):
    try:
        return int(datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp())
    except (TypeError, ValueError):
        return None


def arrival(product, repo, tip):
    """Epoch the trunk's tip ``tip`` arrived: when the trunk watch saw the trunk move to it (or,
    the watch having last read an older tip, its last look), and never before the tip's own
    commit time. None when nothing reads."""
    from asf import trunk_watch
    times = []
    if repo and tip:
        ct = gitops.log1(repo, tip, '%ct') or ''
        if ct.isdigit():
            times.append(int(ct))
    tw = trunk_watch.load(_sd(product))
    if tw.get('checked') == tip and isinstance(tw.get('moved_at'), (int, float)):
        times.append(int(tw['moved_at']))
    elif tw.get('checked') and tw.get('checked') != tip and isinstance(tw.get('at'), (int, float)):
        times.append(int(tw['at']))
    return max(times) if times else None


def run_of(slug, link):
    """``(event, created epoch)`` of the workflow run a check's link names, or None (no run
    named, or its read Unknown). Read once a pass (:data:`RUN_FACT`)."""
    from asf.harvest import lane as lane_mod
    m = lane_mod._RUN_RE.search(link or '')
    if not m:
        return None
    rid = m.group(1)
    got = facts_cache.get(slug, RUN_FACT, rid)
    if got is facts_cache.MISS:
        r = connectors.ci().run(slug, rid)
        got = facts_cache.put(slug, RUN_FACT, rid, '',
                              (r.data.get('event'), _epoch(r.data.get('created_at')))
                              if r.ok and isinstance(r.data, dict) else None)
    return got


def stale(product, slug, red, tip, repo):
    """The names of the ``red`` checks (dicts with ``name`` and ``link`` or ``html_url``) whose
    run is a ``pull_request`` run created before the trunk's tip ``tip`` arrived — a merge ref of
    an older trunk. ``[]`` when the arrival or a run does not read (judged as before)."""
    at = arrival(product, repo, tip)
    if not at or not red:
        return []
    out = []
    for c in red:
        got = run_of(slug, c.get('link') or c.get('html_url'))
        if got and got[0] == 'pull_request' and got[1] is not None and got[1] < at:
            out.append(c.get('name') or '?')
    return list(dict.fromkeys(out))


def _job(name):
    return str(name or '').split(' ', 1)[0]


def _matches(changed, globs):
    for f in changed:
        for g in globs:
            if fnmatch.fnmatch(f, g) or (g.endswith('/') and f.startswith(g)):
                return True
    return False


def trunk_changes(repo, tip, since):
    """The files the trunk changed from its last first-parent commit before epoch ``since`` up
    to ``tip``, or None when git cannot tell (no such commit, a read refused)."""
    r = gitops.git(['rev-list', '-1', '--first-parent', f'--before=@{int(since)}', tip], repo)
    base = r.data.strip() if r.ok else ''
    if not base:
        return None
    r = gitops.git(['diff', '--name-only', base, tip], repo)
    return [l for l in r.data.splitlines() if l.strip()] if r.ok else None


def trunk_touched(product, slug, repo, tip, red, names, findings=None):
    """True when, since a stale red's run was created, the trunk changed a file its job reads:
    a ``stale_ref_job_paths`` glob of the job, else a file the failing log names. True too
    when that cannot be told (fail-open: the reopen goes as before); False only when every stale
    red was read and the trunk touched none of what it reads. ``findings``: a stand-in for
    :func:`asf.merge_queue.failure_findings`."""
    _n, job_paths = settings(product)
    for c in red or ():
        if (c.get('name') or '?') not in names:
            continue
        got = run_of(slug, c.get('link') or c.get('html_url'))
        changed = trunk_changes(repo, tip, got[1]) if got and got[1] is not None else None
        if changed is None:
            return True
        globs = job_paths.get(_job(c.get('name')))
        if globs:
            if _matches(changed, globs):
                return True
            continue
        if findings is None:
            from asf import merge_queue
            findings = merge_queue.failure_findings
        found = findings(slug, [{'name': c.get('name'),
                                 'link': c.get('link') or c.get('html_url')}])
        named = {p for item in found or () for p, _l, _t in item.get('paths') or ()}
        if not named:
            return True
        from asf import trunk_red
        if trunk_red._touches(changed, named):
            return True
    return False


def _shown_since(slug, red, at):
    """True when one of the ``red`` checks' runs was created at or after epoch ``at``: the
    fresh run a reopen asked for has shown."""
    for c in red or ():
        got = run_of(slug, c.get('link') or c.get('html_url'))
        if got and got[1] is not None and got[1] >= at:
            return True
    return False


def path(state_dir):
    return os.path.join(state_dir, STATE_FILE)


def load(state_dir):
    try:
        with open(path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def save(state_dir, data):
    os.makedirs(state_dir, exist_ok=True)
    tmp = path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, path(state_dir))


def in_flight_run(slug, head):
    """``(run id, status)`` of a workflow run on commit ``head`` the host has not completed, or
    None (none, or the list does not read — judged as before). A reopen starts the head's runs
    again and the concurrency group cancels the one still going: a first run cut that way never
    reaches a verdict (2026-10-05), so the reopen waits for it."""
    r = connectors.ci().runs_for_sha(slug, head)
    runs = r.data.get('workflow_runs') if r.ok and isinstance(r.data, dict) else None
    for run in runs if isinstance(runs, list) else ():
        if isinstance(run, dict) and run.get('status') not in (None, 'completed'):
            return run.get('id'), run.get('status')
    return None


def refresh(product, slug, number, head, tip, names, out=print, now=None, red=None,
            repo=None):
    """A fresh run on a new merge ref for PR ``number`` red at ``head`` on ``names`` judged
    against an older trunk: ``'fresh'`` (closed and reopened now), ``'waiting'`` (reopened for
    this head already, its run not shown yet — or a run on the head is still going,
    :func:`in_flight_run`, and the reopen waits for its verdict), or None — not refreshed: the
    reopen was refused, its run never showed within :data:`WAIT_S`, this head has had its
    ``stale_ref_reopens`` already, or (``red`` — the red check dicts — and ``repo`` given) the
    trunk changed nothing the failing job reads since its run (:func:`trunk_touched`). The
    caller then judges the red as it stands. Never raises."""
    now = time.time() if now is None else now
    sd = _sd(product)
    try:
        data = {k: v for k, v in load(sd).items()
                if isinstance(v, dict)
                and now - (v.get('at') or v.get('judged_at') or 0) < KEEP_S}
        rec = data.get(str(number))
        same = bool(rec) and rec.get('head') == head
        if same and rec.get('at'):
            fresh = red is not None and _shown_since(slug, red, rec['at'])
            if not fresh:
                if rec.get('tip') == tip or red is not None:
                    if now - rec['at'] < WAIT_S:
                        return 'waiting'
                    if not rec.get('gave_up'):
                        rec['gave_up'] = True
                        save(sd, data)
                        out(f'stale merge ref: PR #{number} reopened '
                            f'{int((now - rec["at"]) // 60)} min ago and no fresh run showed — '
                            f'its red is judged as it stands')
                    return None
        cap, _paths = settings(product)
        done = int(rec.get('reopens', 1 if rec.get('at') else 0)) if same else 0
        if same and rec.get('judged') == tip:
            return None
        if done >= cap:
            if same and rec.get('capped') != tip:
                rec['capped'] = tip
                rec['judged_at'] = int(now)
                save(sd, data)
                out(f'stale merge ref: PR #{number} red on {", ".join(names)} at {head[:9]} was '
                    f'reopened {done}× for this head already (flags.stale_ref_reopens {cap}) — '
                    f'judged as it stands, no reopen')
            return None
        if getattr(product, 'conventions', None) is None:
            return None
        live = in_flight_run(slug, head)
        if live:
            out(f'stale merge ref: PR #{number} waits — run {live[0]} on {head[:9]} is still '
                f'{live[1]}; a reopen now would supersede it before its verdict')
            return 'waiting'
        links = {c.get('name'): c.get('link') or c.get('html_url') for c in red or ()
                 if c.get('name') in names}
        if red is not None and repo and not trunk_touched(product, slug, repo, tip, red, names):
            keep = dict(rec) if same else {'head': head, 'reopens': 0}
            keep.update({'judged': tip, 'judged_at': int(now), 'links': links,
                         'names': list(names)})
            data[str(number)] = keep
            save(sd, data)
            out(f'stale merge ref: PR #{number} red on {", ".join(names)} at {head[:9]} — the '
                f'trunk changed nothing the failing job reads since its run: judged as it '
                f'stands, no reopen')
            return None
        trunk = product.conventions.main
        why = (f'ASF: the red {", ".join(names)} ran on a merge ref of {trunk} from before '
               f'{trunk} moved to {tip[:9]}; a re-run would replay it. Closed and reopened for a '
               f'fresh run on today\'s {trunk} (asf.stale_ref).')
        r = connectors.forge().close_pr(slug, number, comment=why)
        if not r.ok:
            out(f'stale merge ref: closing PR #{number} for a fresh run refused — '
                f'{(r.stderr or r.reason).strip()[-200:]}')
            return None
        r = connectors.forge().reopen_pr(slug, number)
        if not r.ok:
            r = connectors.forge().reopen_pr(slug, number)
        if not r.ok:
            out(f'stale merge ref: PR #{number} closed but its reopen was refused — '
                f'{(r.stderr or r.reason).strip()[-200:]}; reopen it')
            return None
        data[str(number)] = {'head': head, 'tip': tip, 'at': int(now), 'names': list(names),
                             'reopens': done + 1, 'links': links}
        save(sd, data)
        out(f'stale merge ref: PR #{number} red on {", ".join(names)} at {head[:9]} ran on a '
            f'merge ref from before {trunk} moved to {tip[:9]} — closed and reopened for a fresh '
            f'run (never a re-run of the old ref)')
        return 'fresh'
    except Exception as e:  # noqa: BLE001 — no fresh run: judged as before
        out(f'stale merge ref: PR #{number} — {type(e).__name__}: {e}')
        return None


def in_flight(product, now=None):
    """The PR numbers reopened for a fresh run whose run may still show (within
    :data:`WAIT_S`) — what the stall reaction waits on before it reads the trunk as broken."""
    now = time.time() if now is None else now
    return sorted(int(k) for k, v in load(_sd(product)).items()
                  if isinstance(v, dict) and str(k).isdigit() and not v.get('gave_up')
                  and not v.get('capped') and not v.get('judged')
                  and now - (v.get('at') or 0) < WAIT_S)
