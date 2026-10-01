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

A job **in quarantine** that goes red is re-run, not corrected — up to :data:`QUARANTINE_RERUNS`
times on one sha — until its entry expires; past that cap the red is a defect even so (a real
break in a flaky job is never hidden for a week).

The re-run is ``gh run rerun --job <id>``: GitHub keeps the job's own ``runs-on``, so it runs on
the class that job asks for. ``ci.reference_class`` (e.g. ``reference-heavy``) names the class
whose verdict counts as the reference; each entry records it and the runner the re-run took.

Fail-open: a state directory that cannot be written, a refused re-run or an unreadable check
list is no triage — the red goes to its correct round exactly as before. A re-run refused because
the run is still in progress waits for the next pass (the rest of the matrix is still judging).

State: ``state/<product>/flake-triage.json`` — ``{reruns: {"<sha>|<check>": {...}},
quarantine: [{job, step, test, sha, ...}]}``; expired entries and day-old re-run records are
pruned on every read.
"""
import datetime
import json
import os
import re

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


def settings(product):
    """``{on, reference_class, days}`` from ``ci.flake_triage`` (default on),
    ``ci.reference_class`` and ``ci.quarantine_days``."""
    ci = getattr(product, 'ci', None)
    ci = ci if isinstance(ci, dict) else {}
    on = ci.get('flake_triage', True)
    on = not (on is False or str(on).strip().lower() in ('off', 'false', 'no', '0'))
    days = ci.get('quarantine_days')
    days = days if isinstance(days, (int, float)) and not isinstance(days, bool) and days > 0 \
        else DEFAULT_DAYS
    ref = ci.get('reference_class')
    return {'on': on, 'reference_class': str(ref) if ref else None, 'days': days}


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
    reruns = data.get('reruns') if isinstance(data.get('reruns'), dict) else {}
    quarantine = data.get('quarantine') if isinstance(data.get('quarantine'), list) else []
    keep = {}
    for k, r in reruns.items():
        at = _parse((r or {}).get('at')) if isinstance(r, dict) else None
        if at is not None and (now - at).total_seconds() <= RERUN_TTL_S:
            keep[k] = r
    live = [q for q in quarantine if isinstance(q, dict)
            and (_parse(q.get('expires')) or now) > now]
    return {'reruns': keep, 'quarantine': live}


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


def _gh():
    from asf.harvest import harvest as H
    return H._gh


def triage(product, state_dir, slug, sha, red, where='', out=print, now=None, gh=None):
    """Split the red checks ``red`` (dicts with ``name`` and ``link``) on ``sha`` into
    ``(defects, rerun)``: names that go to a correct round now, and names held for a re-run
    (started this pass, or one still running). Never raises: anything unreadable is a defect
    (today's behaviour)."""
    names = list(dict.fromkeys(c.get('name') or '?' for c in red or ()))
    cfg = settings(product)
    if not names or not sha or not cfg['on'] or not state_dir or not os.path.isdir(state_dir):
        return names, []
    gh = gh or _gh()
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
        if rec and int(rec.get('attempts') or 0) >= limit:
            defects.append(name)    # red again on its re-run: a real defect
            continue
        if not job_id:
            defects.append(name)
            continue
        try:
            rc, o, err = gh(['run', 'rerun', '--job', job_id, '-R', slug])
        except OSError as e:
            rc, o, err = 1, '', str(e)
        if rc != 0:
            text = f'{o}\n{err}'.lower()
            if any(w in text for w in _RUNNING):
                held.append(name)   # the run is still judging the rest: next pass
                continue
            defects.append(name)
            continue
        attempts = int((rec or {}).get('attempts') or 0) + 1
        data['reruns'][key] = {'name': name, 'sha': sha, 'run': run_id,
                               'job': (rec or {}).get('job') or job_id, 'last_job': job_id,
                               'link': (rec or {}).get('link') or c.get('link') or '',
                               'attempts': attempts, 'at': _iso(now), 'where': where,
                               'quarantined': bool(q)}
        held.append(name)
        started.append(name)
    if started and not save(state_dir, data):
        return names, []            # nothing remembers the re-run: correct as before
    for name in started:
        why = 'quarantined, re-run instead of corrected' if active(data, name, now) \
            else 'red once: re-run before any correct round'
        out(f'flake triage: {where} {name} @ {sha[:9]} {why}')
    return defects, held


def _failed_detail(slug, job_id, gh):
    """``(step, test)`` the failed job ``job_id`` names — its failed step from the jobs API, the
    first failing test its log prints; either None when unreadable."""
    step = test = None
    if not job_id:
        return step, test
    try:
        rc, o, _e = gh(['api', f'repos/{slug}/actions/jobs/{job_id}'])
        if rc == 0:
            job = json.loads(o)
            for s in job.get('steps') or ():
                if s.get('conclusion') == 'failure':
                    step = s.get('name')
                    break
        rc, log, _e = gh(['api', f'repos/{slug}/actions/jobs/{job_id}/logs'])
        if rc == 0:
            for line in (log or '').splitlines():
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
    gh = gh or _gh()
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
