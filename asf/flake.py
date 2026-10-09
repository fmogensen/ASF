"""asf.flake — flake-vs-defect triage before any correct round on a red required check.

A required job red on a PR's exact head (:meth:`asf.harvest.lane.GitHubHost.head_red`) or on a
merge-queue batch sha (:func:`asf.merge_queue.run`) is first **re-run once** on the same sha
(``gh run rerun --job``) — no correct round, no batch split:

* the re-run is **green**: the job flaked. A **quarantine entry** is written — the job, the step
  and the test that failed when the log names them, the sha, the links, an owner card queued for
  the product's own record (the scorecard's designed path, :func:`asf.scorecard.loop.enqueue`,
  drained into the intake directory by the product's daily step) — with a ``ci.quarantine_days``
  expiry (7). ``asf status`` shows it (the ``Quarantine`` row). No session is sent.
* the re-run is **red again**: a real defect — the correct round (or the batch split) goes as
  before, its job's evidence the re-run's own log.

An **infra red** — a job lost with its runner, not judged by it: every failure annotation the
host wrote on it names runner loss (:data:`INFRA_SIGNATURES` — ``The runner has received a
shutdown signal``, ``lost communication with the server``, ``The operation was canceled`` with
nothing else beside it, …) — is re-run, never sent to a correct round, and never counts toward
the flake budget: no quarantine entry, no attempt spent (:func:`infra_red`). Past
:data:`INFRA_RERUNS` runner losses on one sha it is held, still never a correct round — a session
cannot fix a runner.

A red that **reproduced on two merge refs** is never re-run (2026-10-05: 20 of 21 blind re-runs
stayed red): the same check red on the same head in two different workflow runs — a reopen for a
fresh merge ref (:mod:`asf.stale_ref`) or a new run — failing the same test (when both logs name
one; :func:`reproduced`) is deterministic, and goes straight to its correct round.
``conventions.flags.flake_skip_reproduced: off`` turns it off.

A job **in quarantine** that goes red is re-run, not corrected — up to :data:`QUARANTINE_RERUNS`
times on one sha — until its entry expires; past that cap the red is a defect even so (a real
break in a flaky job is never hidden for a week).

The re-run is ``gh run rerun --job <id>``: GitHub keeps the job's own ``runs-on``, so it runs on
the class that job asks for. ``ci.reference_class`` (e.g. ``reference-heavy``) names the class
whose verdict counts as the reference; each entry records it and the runner the re-run took.

Fail-open: a state directory that cannot be written, a refused re-run or an unreadable check
list is no triage — the red goes to its correct round exactly as before. A re-run refused because
the run is still in progress waits for the next pass (the rest of the matrix is still judging) —
read off the run's own status when the host's words are none :data:`_RUNNING` knows: a refusal on
a live run is never a second red (2026-10-06: a batch dropped on one, its stacked chain with it).
A caller that names its ``required`` jobs (the merge queue) narrows "still judging" to those:
a non-required job left running alone (a product's own ``site`` job, say) never holds a required
job's red waiting on it — that is a defect at once, no matter how long the unrelated job runs
(B-0274).

A **deterministic** red — a registers or pre-cut check (the merge queue names them:
``merge_queue.deterministic_jobs`` and the jobs running ``merge_queue.precut_check``) — answers the
same on every run: it is a defect at once, never re-run (2026-10-06: a duplicate-row red re-run
twice, ~45 min of heavy runs for the same answer).

``gh`` goes through :mod:`asf.github` (a test's own ``gh`` — ``(rc, stdout, stderr)`` or an
:class:`asf.github.Result` — stands in for it); a read that does not answer is Unknown, and
Unknown is today's fail-open: no infra red, no step or test named.

State: ``state/<product>/flake-triage.json`` — ``{reruns: {"<sha>|<check>": {...}},
reds: {"<sha>|<check>": {run, job, link, test, at}}, quarantine: [{job, step, test, sha, ...}]}``; expired entries and day-old re-run records are
pruned on every read.
"""
import datetime
import json
import os
import re

from asf import connectors, github

FILE = 'flake-triage.json'
#: ``ci.quarantine_days`` default: how long a flaked job stays in quarantine
DEFAULT_DAYS = 7
#: re-runs of one quarantined job on one sha before its red is a defect even so
QUARANTINE_RERUNS = 3
#: a re-run record older than this is dropped (its sha is long gone or judged)
RERUN_TTL_S = 2 * 86400
#: the marker line the owner card carries (one card per job per quarantine)
MARKER = 'flake-quarantine:'

_RUN_RE = re.compile(r'/actions/runs/(\d+)')
_JOB_RE = re.compile(r'/job/(\d+)')
#: test names a failing log commonly prints (vitest/jest ``FAIL``/``×``, pytest ``FAILED``, TAP
#: ``not ok``, go ``--- FAIL:``); the first match names the test
_TEST_RES = (
    re.compile(r'^\s*(?:FAIL|FAILED|✗|×|✕)\s+(\S.{2,200}?)\s*$'),
    re.compile(r'^\s*not ok \d+\s*-?\s*(\S.{2,200}?)\s*$'),
    re.compile(r'^\s*--- FAIL:\s+(\S+)'),
)
_RUNNING = ('already running', 'in progress', 'is running', 'not completed')
#: re-runs of one job on one sha lost to its runner before it is held (never corrected)
INFRA_RERUNS = 3
#: the host's own words for a job its runner was lost under (lower case, a substring of a
#: failure annotation): the job never ran to a verdict, so its red says nothing of the change
INFRA_SIGNATURES = (
    'the runner has received a shutdown signal',
    'lost communication with the server',
    'runner has lost communication',
    'lost communication with the runner',
    'the job was not acquired by runner',
    'the hosted runner encountered an error',
    'the operation was canceled',
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(stamp):
    try:
        return datetime.datetime.strptime(str(stamp), '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def job_key(name):
    """The job a check answers for — its name up to the first space (a matrix leg is its job)."""
    return str(name or '').split(' ', 1)[0]


def _flag(value):
    return not (value is False or str(value).strip().lower() in ('off', 'false', 'no', '0'))


def _skip_flag(product):
    conv = getattr(product, 'conventions', None)
    try:
        return conv.flag('flake_skip_reproduced', True) if conv is not None else True
    except AttributeError:
        return True


def settings(product):
    """``{on, reference_class, days, skip_reproduced}`` from ``ci.flake_triage`` (default on),
    ``ci.reference_class``, ``ci.quarantine_days`` and ``conventions.flags.flake_skip_reproduced``
    (default on)."""
    ci = getattr(product, 'ci', None)
    ci = ci if isinstance(ci, dict) else {}
    on = _flag(ci.get('flake_triage', True))
    days = ci.get('quarantine_days')
    days = days if isinstance(days, (int, float)) and not isinstance(days, bool) and days > 0 \
        else DEFAULT_DAYS
    ref = ci.get('reference_class')
    return {'on': on, 'reference_class': str(ref) if ref else None, 'days': days,
            'skip_reproduced': _flag(_skip_flag(product))}


def _path(state_dir):
    return os.path.join(state_dir, FILE)


def load(state_dir, now=None):
    """The triage state, pruned of expired quarantine entries and stale re-run records."""
    now = now or _now()
    try:
        with open(_path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    quarantine = data.get('quarantine') if isinstance(data.get('quarantine'), list) else []

    def fresh(name):
        got = data.get(name) if isinstance(data.get(name), dict) else {}
        keep = {}
        for k, r in got.items():
            at = _parse((r or {}).get('at')) if isinstance(r, dict) else None
            if at is not None and (now - at).total_seconds() <= RERUN_TTL_S:
                keep[k] = r
        return keep
    live = [q for q in quarantine if isinstance(q, dict)
            and (_parse(q.get('expires')) or now) > now]
    return {'reruns': fresh('reruns'), 'reds': fresh('reds'), 'infra': fresh('infra'),
            'quarantine': live}


def save(state_dir, data):
    """True when written. A state directory that is not there writes nothing."""
    if not state_dir or not os.path.isdir(state_dir):
        return False
    tmp = _path(state_dir) + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, sort_keys=True, indent=1)
        os.replace(tmp, _path(state_dir))
        return True
    except OSError:
        return False


def active(data, name, now=None):
    """The live quarantine entry of check ``name``'s job, or None."""
    now = now or _now()
    job = job_key(name)
    for q in data.get('quarantine') or ():
        if q.get('job') == job and (_parse(q.get('expires')) or now) > now:
            return q
    return None


def _ids(link):
    link = link or ''
    r, j = _RUN_RE.search(link), _JOB_RE.search(link)
    return (r.group(1) if r else None), (j.group(1) if j else None)


def _run_ids(checks):
    """The workflow run ids behind ``checks``, first-seen order — a check with no readable run
    id is skipped, never an answer of its own."""
    out = []
    for c in checks or ():
        if not isinstance(c, dict):
            continue
        rid = _ids(c.get('link') or c.get('html_url') or c.get('details_url'))[0]
        if rid and rid not in out:
            out.append(rid)
    return out


def _call(gh, args):
    """``gh <args>`` as an :class:`asf.github.Result`: through :func:`asf.github.gh`, or through
    ``gh`` when one is passed (``(rc, stdout, stderr)`` or a ``Result``). A ``gh`` that cannot
    run is Unknown."""
    if gh is None:
        return connectors.ci().call(args)
    try:
        got = gh(args)
    except OSError as e:
        return github.unknown(f'gh not runnable: {e}')
    if isinstance(got, github.Result):
        return got
    rc, out, err = got
    if rc != 0:
        return github.Result(False, None, rc, out or '', err or '', github.now_iso(), f'rc {rc}')
    return github.Result(True, out, rc, out or '', err or '', github.now_iso(), '')


def is_infra(messages):
    """True when ``messages`` — a failed job's failure annotations — all name runner loss
    (:data:`INFRA_SIGNATURES`) and there is at least one. A test failure's ``Process completed
    with exit code 1`` or a ``timeout-minutes`` verdict beside it is no infra red."""
    msgs = [str(m or '').strip().lower() for m in messages or ()]
    msgs = [m for m in msgs if m]
    return bool(msgs) and all(any(sig in m for sig in INFRA_SIGNATURES) for m in msgs)


def infra_red(slug, job_id, gh=None):
    """The runner-loss annotation of failed job ``job_id`` (one ``gh api`` call: its check run's
    annotations — a job id is its check run id), or None when it is no infra red or unreadable
    (an unreadable job is judged as before). ``gh``: see :func:`_call`."""
    if not job_id or not slug:
        return None
    r = _call(gh, ['api', f'repos/{slug}/check-runs/{job_id}/annotations'])
    try:
        got = json.loads(r.data) if r.ok and r.data else None
    except (ValueError, TypeError):
        return None
    if not isinstance(got, list):
        return None
    fails = [a.get('message') for a in got if isinstance(a, dict)
             and str(a.get('annotation_level') or 'failure').lower() == 'failure']
    return fails[0].strip().splitlines()[0] if is_infra(fails) else None


#: :func:`classify_red`'s answers: two infra reds and the one red that is the code's
PHANTOM, LOST_RUNNER, TEST = 'phantom', 'lost-runner', 'test'
#: the infra classes — re-run once on the head, never a correct round
INFRA_CLASSES = (PHANTOM, LOST_RUNNER)
#: a job state that has not reached a verdict yet
_UNFINISHED = ('queued', 'in_progress', 'waiting', 'pending', 'requested')
#: the claim cause of an infra re-run (:func:`asf.ci_queue.claim_cancel`, ``ci-cancels.json``;
#: written as its literal at the one call site, where tests/test_ci_cancels.py reads it)
INFRA_CAUSE = 'infra'


def _lost_job(job):
    """A failed job that never ran on a runner: no runner named and no step that failed."""
    steps = job.get('steps') if isinstance(job.get('steps'), list) else []
    return not job.get('runner_name') and not any(
        isinstance(s, dict) and s.get('conclusion') == 'failure' for s in steps)


def classify_red(run, jobs, required=None):
    """What a red workflow run is, read off the run and its jobs (``gh api …/runs/<id>/jobs``):

    * ``phantom`` — the run concluded ``failure`` and no job of it failed: some are still
      queued or running, or a ``required`` job (by :func:`job_key`) has no job at all;
    * ``lost-runner`` — every failed job never ran on a runner: no ``runner_name``, no failed
      step (its logs are gone with it);
    * ``test`` — anything else: a job a runner judged. The one red that is the code's.

    Pure: the caller reads the run and its jobs once. The phantom and the lost runner are
    infra (:data:`INFRA_CLASSES`) — re-run once on the head (:func:`infra_rerun`), never a
    correct round, never a red check."""
    run = run if isinstance(run, dict) else {}
    jobs = [j for j in jobs or () if isinstance(j, dict)]
    failed = [j for j in jobs if j.get('conclusion') in ('failure', 'timed_out')]
    if failed:
        return LOST_RUNNER if all(_lost_job(j) for j in failed) else TEST
    if run.get('status') == 'completed' and run.get('conclusion') == 'failure':
        unfinished = any(j.get('status') in _UNFINISHED for j in jobs)
        have = {job_key(j.get('name')) for j in jobs}
        missing = bool(required) and not {job_key(r) for r in required} <= have
        if unfinished or missing or not jobs:
            return PHANTOM
    return TEST


def infra_rerun(state_dir, slug, sha, run_id, cls, where='', out=print, gh=None, now=None,
                attempt=None):
    """Answer an infra red (:func:`classify_red`) on head ``sha``, run ``run_id``: the first is
    re-run once — ``gh run rerun <id> --failed``, head-matched (the caller's ``sha`` is the
    branch's current head) — and claimed with :data:`INFRA_CAUSE` in ``ci-cancels.json``
    (:func:`asf.ci_queue.claim_cancel`); a second on the same head is one watchdog breach, said
    once, and never re-run again. ``'rerun'``, ``'refused'`` (the host said no: judged as
    before), ``'breach'`` or ``'held'`` (a breach already said, or the red read is still the
    attempt the re-run replaces: ``attempt``, the run's ``run_attempt``, not past the one re-run)."""
    now = now or _now()
    data = load(state_dir, now)
    key = str(sha)
    rec = dict(data['infra'].get(key) or {})
    if rec.get('count'):
        if rec.get('breach'):
            return 'held'
        if attempt is not None and str(rec.get('run')) == str(run_id) \
                and int(attempt or 0) <= int(rec.get('attempt') or 0):
            return 'held'   # the re-run has not started its attempt yet: the old red still reads
        rec.update(breach=_iso(now), at=_iso(now), last=cls, run=str(run_id))
        data['infra'][key] = rec
        save(state_dir, data)
        out(f'watchdog: BREACH infra red ({cls}) again on {key[:9]} at {where or "?"} — run '
            f'{run_id}; re-run once already, not again: read the runner pool')
        return 'breach'
    r = _call(gh, ['run', 'rerun', str(run_id), '--failed', '-R', slug])
    if not r.ok:
        out(f'infra red ({cls}) on {key[:9]} at {where or "?"}: re-run of run {run_id} '
            f'refused — {(r.stderr or r.reason or "").strip()[:120]}')
        return 'refused'
    from asf import ci_queue
    ci_queue.claim_cancel(state_dir, run_id, 'infra', sha=key, cls=cls, where=where)
    data['infra'][key] = {'count': 1, 'class': cls, 'run': str(run_id), 'at': _iso(now),
                          **({'attempt': int(attempt)} if attempt is not None else {})}
    save(state_dir, data)
    out(f'infra red ({cls}) on {key[:9]} at {where or "?"}: re-run queued (run {run_id}, '
        f'--failed) — not a red check')
    return 'rerun'


def run_jobs(slug, run_id, gh=None):
    """``(run, jobs)`` for workflow run ``run_id`` (two ``gh api`` reads), or ``(None, None)``
    when either cannot be read."""
    if not slug or not run_id:
        return None, None
    try:
        r = _call(gh, ['api', f'repos/{slug}/actions/runs/{run_id}'])
        run = json.loads(r.data) if r.ok and r.data else None
        j = _call(gh, ['api', f'repos/{slug}/actions/runs/{run_id}/jobs?per_page=100'])
        got = json.loads(j.data) if j.ok and j.data else None
    except (ValueError, TypeError):
        return None, None
    jobs = got.get('jobs') if isinstance(got, dict) else None
    if not isinstance(run, dict) or not isinstance(jobs, list):
        return None, None
    return run, jobs


#: how often one workflow run is read for a phantom while its checks wait (:func:`infra_class`
#: with ``state_dir``): a queue under load keeps checks queued for long, and is no phantom
PHANTOM_PROBE_S = 600


def infra_class(slug, checks, required=None, gh=None, state_dir=None, now=None):
    """``(class, run id, attempt)`` for the first run behind ``checks`` that is an infra red
    (:func:`classify_red`: a phantom or a lost runner), else None — every run reads as a test
    red, no link carries a run id, or none can be read.

    A head carries checks from more than one workflow, so every run id the links name is read,
    in the order they first appear (F-0295 C3). With ``state_dir`` each run is read at most once
    per :data:`PHANTOM_PROBE_S` (the caller polls a pending head every pass); a run inside its
    interval is skipped, and the others are still read."""
    now = now or _now()
    data = load(state_dir, now) if state_dir else None
    answer = None
    for rid in _run_ids(checks):
        if data is not None:
            seen = _parse((data['infra'].get(f'probe|{rid}') or {}).get('at'))
            if seen is not None and (now - seen).total_seconds() < PHANTOM_PROBE_S:
                continue    # read inside its interval: the other runs are still read
            data['infra'][f'probe|{rid}'] = {'at': _iso(now)}
        run, jobs = run_jobs(slug, rid, gh)
        if run is None:
            continue
        cls = classify_red(run, jobs, required)
        if cls in INFRA_CLASSES:
            try:
                attempt = int(run.get('run_attempt') or 1)
            except (TypeError, ValueError):
                attempt = 1
            answer = (cls, rid, attempt)
            break           # one head, one answer, one re-run (C5)
    if data is not None:
        save(state_dir, data)
    return answer


def run_live(slug, run_id, gh=None, required=None):
    """True when workflow run ``run_id`` is still queued or in progress (one ``gh api`` call);
    False when it completed or cannot be read.

    ``required``: judge liveness by the run's own jobs instead of its aggregate status —
    True only when one of those jobs whose key (:func:`job_key`) is in ``required`` is itself
    still queued or in progress, False when the jobs read clean and none is: a non-required job
    (a ``site`` job, say) left running alone never counts, a required job already red is never
    held waiting on a job nobody is gating on (B-0274 — the run stayed live on an unrelated job
    alone for hours). None when the jobs themselves cannot be read — unlike every other case
    here, never read as "not live": the caller's own aggregate answer stands instead."""
    if not slug or not run_id:
        return False
    if required:
        r = _call(gh, ['api', f'repos/{slug}/actions/runs/{run_id}/jobs?per_page=100'])
        try:
            got = json.loads(r.data) if r.ok and r.data else None
        except (ValueError, TypeError):
            return None
        jobs = (got or {}).get('jobs') if isinstance(got, dict) else None
        if not isinstance(jobs, list):
            return None
        return any(job_key(j.get('name')) in required for j in jobs
                  if isinstance(j, dict) and j.get('status') != 'completed')
    r = _call(gh, ['api', f'repos/{slug}/actions/runs/{run_id}'])
    try:
        got = json.loads(r.data) if r.ok and r.data else None
    except (ValueError, TypeError):
        return False
    status = str((got or {}).get('status') or '') if isinstance(got, dict) else ''
    return bool(status) and status != 'completed'


def _prior_reds(data, state_dir, sha, name, run_id):
    """The links of check ``name`` red on ``sha`` in a workflow run other than ``run_id``: the
    triage's own record, and the red a reopen for a fresh merge ref left (:mod:`asf.stale_ref`)."""
    out = []
    rec = data.get('reds', {}).get(f'{sha}|{name}')
    if rec and rec.get('run') and rec.get('run') != run_id:
        out.append(rec)
    try:
        from asf import stale_ref
        for pr in stale_ref.load(state_dir).values():
            if isinstance(pr, dict) and pr.get('head') == sha:
                link = (pr.get('links') or {}).get(name)
                rid, jid = _ids(link)
                if rid and rid != run_id:
                    out.append({'run': rid, 'job': jid, 'link': link})
    except Exception:  # noqa: BLE001 — no reopen record readable: the triage's own only
        pass
    return out


def reproduced(slug, sha, name, job_id, priors, gh=None):
    """True when check ``name`` red on ``sha`` in job ``job_id`` failed the same way in one of
    ``priors`` (earlier runs of it on other merge refs): the same failing test when both logs
    name one, else the same check. A red that reproduced is deterministic — no re-run."""
    if not priors:
        return False
    test = None
    for prior in priors:
        if 'test' not in prior:
            prior['test'] = _failed_detail(slug, prior.get('job'), gh)[1]
        if prior['test'] and test is None:
            test = _failed_detail(slug, job_id, gh)[1] or ''
        if not prior['test'] or not test or prior['test'] == test:
            return True
    return False


def triage(product, state_dir, slug, sha, red, where='', out=print, now=None, gh=None,
           deterministic=(), required=None):
    """Split the red checks ``red`` (dicts with ``name`` and ``link``) on ``sha`` into
    ``(defects, rerun)``: names that go to a correct round now, and names held for a re-run
    (started this pass, or one still running). Never raises: anything unreadable is a defect
    (today's behaviour).

    ``deterministic``: the names (or job keys) whose red is deterministic — a registers or
    pre-cut check answers the same on every run: a defect at once, never re-run. A re-run the
    host refuses while the job's workflow run is still live (:func:`run_live`) waits for the
    next pass, whatever words the refusal used: it is never read as a second red — unless
    ``required`` is given and none of its names is among the run's own still-live jobs (B-0274):
    a non-required job (a ``site`` job, say) left running alone never keeps a required job's red
    waiting, it is a defect at once."""
    fixed = {str(n) for n in deterministic or ()}
    if fixed:
        hard = [c for c in red or () if (c.get('name') or '?') in fixed
                or job_key(c.get('name')) in fixed]
        if hard:
            rest = [c for c in red or () if c not in hard]
            d, h = triage(product, state_dir, slug, sha, rest, where, out, now, gh,
                         required=required) if rest else ([], [])
            for c in hard:
                out(f"flake triage: {where} {c.get('name') or '?'} @ {(sha or '')[:9]} is "
                    'deterministic — a defect at once, never re-run')
            return list(dict.fromkeys([c.get('name') or '?' for c in hard] + d)), h
    names = list(dict.fromkeys(c.get('name') or '?' for c in red or ()))
    cfg = settings(product)
    if not names or not sha or not cfg['on'] or not state_dir or not os.path.isdir(state_dir):
        return names, []
    now = now or _now()
    data = load(state_dir, now)
    defects, held, started = [], [], []
    seen = set()
    for c in red:
        name = c.get('name') or '?'
        if name in seen:
            continue
        seen.add(name)
        run_id, job_id = _ids(c.get('link'))
        key = f'{sha}|{name}'
        rec = data['reruns'].get(key)
        q = active(data, name, now)
        limit = QUARANTINE_RERUNS if q else 1
        if rec and job_id and job_id == rec.get('last_job'):
            held.append(name)       # the host still shows the job that was re-run: wait
            continue
        if not job_id:
            defects.append(name)
            continue
        lost = infra_red(slug, job_id, gh)
        if lost:
            # lost with its runner: re-run, never a correct round, never the flake budget
            n_lost = int((rec or {}).get('infra') or 0)
            if n_lost >= INFRA_RERUNS:
                if not (rec or {}).get('infra_held'):
                    rec['infra_held'] = True
                    started.append(None)    # remember the hold (saved below)
                    out(f'flake triage: {where} {name} @ {sha[:9]} lost its runner '
                        f'{n_lost + 1} times ({lost}) — held for the runner pool, never a '
                        f'correct round')
                held.append(name)
                continue
        elif rec and int(rec.get('attempts') or 0) >= limit:
            defects.append(name)    # red again on its re-run: a real defect
            continue
        elif not q and cfg['skip_reproduced'] and reproduced(
                slug, sha, name, job_id, _prior_reds(data, state_dir, sha, name, run_id), gh):
            out(f'flake triage: {where} {name} @ {sha[:9]} red again on another merge ref — '
                'deterministic: no re-run, straight to its correct round')
            defects.append(name)
            continue
        if run_id and not lost:
            data['reds'].setdefault(f'{sha}|{name}', {'name': name, 'sha': sha, 'run': run_id,
                                                       'job': job_id, 'link': c.get('link'),
                                                       'at': _iso(now)})
        r = _call(gh, ['run', 'rerun', '--job', job_id, '-R', slug])
        if not r.ok:
            text = f'{r.stdout}\n{r.stderr}\n{r.reason}'.lower()
            if lost:
                held.append(name)   # infra: the next pass asks again
                continue
            live = any(w in text for w in _RUNNING) or run_live(slug, run_id, gh)
            if live and required and run_live(slug, run_id, gh, required=required) is False:
                # the per-job read says so positively (never on an unreadable one, which stays
                # held as before): nothing required is why the run looks live, so waiting on it
                # buys nothing (B-0274 — a non-required job alone kept this live for hours)
                defects.append(name)
                continue
            if live:
                held.append(name)   # a required job of its run is still judging: wait
                continue
            defects.append(name)
            continue
        attempts = int((rec or {}).get('attempts') or 0) + (0 if lost else 1)
        data['reruns'][key] = {'name': name, 'sha': sha, 'run': run_id,
                               'job': (rec or {}).get('job') or job_id, 'last_job': job_id,
                               'link': (rec or {}).get('link') or c.get('link') or '',
                               'attempts': attempts, 'at': _iso(now), 'where': where,
                               'quarantined': bool(q),
                               'infra': int((rec or {}).get('infra') or 0) + (1 if lost else 0)}
        held.append(name)
        started.append((name, lost))
    if started and not save(state_dir, data):
        return names, []            # nothing remembers the re-run: correct as before
    for got in started:
        if got is None:
            continue
        name, lost = got
        if lost:
            why = f'lost its runner ({lost}): infra, re-run — no correct round, no flake count'
        elif active(data, name, now):
            why = 'quarantined, re-run instead of corrected'
        else:
            why = 'red once: re-run before any correct round'
        out(f'flake triage: {where} {name} @ {sha[:9]} {why}')
    return defects, held


def _failed_detail(slug, job_id, gh):
    """``(step, test)`` the failed job ``job_id`` names — its failed step from the jobs API, the
    first failing test its log prints; either None when unreadable."""
    step = test = None
    if not job_id:
        return step, test
    try:
        r = _call(gh, ['api', f'repos/{slug}/actions/jobs/{job_id}'])
        if r.ok:
            job = json.loads(r.data)
            for s in job.get('steps') or ():
                if s.get('conclusion') == 'failure':
                    step = s.get('name')
                    break
        r = _call(gh, ['api', f'repos/{slug}/actions/jobs/{job_id}/logs'])
        if r.ok:
            for line in (r.data or '').splitlines():
                line = re.sub(r'^﻿?\d{4}-\d{2}-\d{2}T[\d:.]+Z ?', '', line)
                line = re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', line)
                for rx in _TEST_RES:
                    m = rx.match(line)
                    if m:
                        test = m.group(1).strip()
                        break
                if test:
                    break
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return step, test


def owner_card(product, entry):
    """``(name, text, marker)`` of the inbox card that owns a quarantined job."""
    job, until = entry['job'], entry['expires'][:10]
    marker = f'{MARKER} {job} {until}'
    what = ', '.join(x for x in (f"step `{entry['step']}`" if entry.get('step') else '',
                                 f"test `{entry['test']}`" if entry.get('test') else '') if x)
    lines = [f'# Flaky CI job {job} — quarantined until {until}', 'type: bug',
             f'signature: flaky-ci {job}', '',
             f"`{job}` failed on {entry['sha'][:12]} ({entry.get('where') or '?'}) and passed on "
             f"its re-run of the same commit — a flake, not a defect of the change."
             + (f' It failed at {what}.' if what else ''), '',
             f"- failed: {entry.get('link') or '?'}",
             f"- passed on re-run: {entry.get('rerun_link') or '?'}",
             f"- reference class: {entry.get('reference_class') or 'the job’s own runs-on'}", '',
             f'ASF quarantined `{job}` until {until}: while quarantined a red `{job}` is re-run, '
             'never sent back as a correct round. Make the job deterministic before then.', '',
             marker, '', '## Acceptance',
             f'- [ ] `{job}` has no same-commit red→green flip for 7 days', '']
    return f"flaky-ci-{re.sub(r'[^a-z0-9]+', '-', job.lower()).strip('-') or 'job'}-{until}.md", \
        '\n'.join(lines), marker


def settle(product, state_dir, slug, sha, checks, out=print, now=None, gh=None, enqueue=None):
    """Read the re-runs held on ``sha`` against its ``checks`` now (dicts with ``name``,
    ``bucket`` — ``pass``/``fail``/… — and ``link``): a re-run that went green writes a
    quarantine entry (one per job; a job in quarantine already keeps its entry) and queues its
    owner card. Returns the quarantine entries written. Never raises."""
    if not sha or not state_dir or not os.path.isdir(state_dir):
        return []
    now = now or _now()
    data = load(state_dir, now)
    mine = {k: r for k, r in data['reruns'].items() if r.get('sha') == sha}
    if not mine:
        return []
    cfg = settings(product)
    by_name = {}
    for c in checks or ():
        by_name.setdefault(c.get('name') or '?', []).append(c)
    written = []
    for key, rec in mine.items():
        now_checks = by_name.get(rec.get('name')) or []
        green = [c for c in now_checks if c.get('bucket') == 'pass'
                 and _ids(c.get('link'))[1] != rec.get('job')]
        if not green or any(c.get('bucket') == 'fail' for c in now_checks):
            continue
        del data['reruns'][key]
        if rec.get('infra') and not int(rec.get('attempts') or 0):
            out(f"flake triage: {rec.get('where')} {rec['name']} @ {sha[:9]} green on re-run — "
                'its red was runner loss (infra): no quarantine, no flake count')
            continue
        if active(data, rec['name'], now):
            out(f"flake triage: {rec.get('where')} {rec['name']} @ {sha[:9]} green on re-run — "
                'already quarantined')
            continue
        step, test = _failed_detail(slug, rec.get('job'), gh)
        _run, rerun_job = _ids(green[0].get('link'))
        expires = now + datetime.timedelta(days=cfg['days'])
        entry = {'job': job_key(rec['name']), 'check': rec['name'], 'step': step, 'test': test,
                 'sha': sha, 'where': rec.get('where'), 'link': rec.get('link'),
                 'rerun_link': green[0].get('link'), 'rerun_job': rerun_job,
                 'reference_class': cfg['reference_class'], 'at': _iso(now),
                 'expires': _iso(expires)}
        name, text, marker = owner_card(product, entry)
        entry['owner'] = marker
        try:
            if enqueue is None:
                from asf.scorecard import loop
                loop.enqueue(product.name, {'kind': 'inbox', 'name': name, 'text': text,
                                            'marker': marker}, state_dir=state_dir)
            else:
                enqueue({'kind': 'inbox', 'name': name, 'text': text, 'marker': marker})
        except (OSError, AttributeError) as e:
            entry['owner'] = f'not filed: {e}'
        data['quarantine'].append(entry)
        written.append(entry)
        out(f"flake triage: {rec.get('where')} {rec['name']} @ {sha[:9]} green on re-run — "
            f"a flake: quarantined until {entry['expires'][:10]}, no correct round "
            f"(owner card {name})")
    save(state_dir, data)
    return written


def batch_checks(runs):
    """A merge-queue batch's check runs (the commit's check-runs API shape) as
    ``{name, bucket, link}`` dicts :func:`triage` and :func:`settle` read."""
    out = []
    for r in runs or ():
        concl = r.get('conclusion')
        if r.get('status') != 'completed':
            bucket = 'pending'
        elif concl == 'success':
            bucket = 'pass'
        elif concl in ('failure', 'timed_out'):
            bucket = 'fail'
        else:
            bucket = concl or 'pending'
        out.append({'name': r.get('name'), 'bucket': bucket,
                    'link': r.get('html_url') or r.get('details_url') or ''})
    return out


def status_cell(product, state_dir=None, now=None):
    """The ``Quarantine`` row of ``asf status``: each live entry, or None when there is none."""
    from asf import env
    state_dir = state_dir or env.state_dir(product.name)
    data = load(state_dir, now)
    parts = []
    for q in data['quarantine']:
        what = '; '.join(x for x in (f"step {q['step']}" if q.get('step') else '',
                                     f"test {q['test']}" if q.get('test') else '') if x)
        parts.append(f"{q.get('job')} until {str(q.get('expires'))[:10]} "
                     f"(flaked on {str(q.get('sha'))[:9]}" + (f'; {what}' if what else '') + ')')
    held = len(data['reruns'])
    if held:
        parts.append(f'{held} re-run(s) in triage')
    return ', '.join(parts) if parts else None
