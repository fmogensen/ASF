"""asf.trunk_red — a required check red on the trunk itself: seen, confirmed by a full run, owned.

Under ``conventions.merge: queue`` an attested trunk push skips the heavy jobs (the batch run
already judged that exact sha, :mod:`asf.attestation`), so nothing runs them on the trunk's tip
afterwards. A check that breaks on the trunk itself — a clock-bound test, a dependency that moved,
an external service — then shows only on the landings: every batch and every ``asf land`` head
fails it, each one blamed and sent to a correct round for a defect it does not carry, while the
trunk stands still (2026-10-02, a product: 11.5 h, two unrelated hotfixes red on the same e2e
check). Three parts answer it:

0. **A fresh merge ref first** (:mod:`asf.stale_ref`). A PR red on a run whose merge ref predates
   the trunk's tip judged an old trunk: it is reopened for a fresh run, never kept red, never
   re-run, never counted here — and the stall reaction waits for those fresh runs
   (:func:`stale_first`) before it dispatches anything.
1. **Suspicion** (:func:`observe`, :func:`suspects`). Each landing that goes red on a required
   check *after triage* — a merge-queue batch (:func:`asf.merge_queue.run`, after the flake
   re-run), an ``asf land`` head (runner loss excluded), a factory PR's head
   (:meth:`asf.harvest.lane.GitHubHost.head_red`, after the flake re-run) — is recorded with the
   files its diff touches and the files the failing job's log names (``path:line``). The same
   check red on **two unrelated landings** — no PR in common, and neither diff touching a file the
   failing logs name (with no file named: the two diffs share no file) — since the trunk last
   moved is **trunk red, suspected**. :func:`held` then reads the check as red on the trunk: a
   batch splits and blames no member, a PR is not sent to a correct round — they wait, as for a
   check the trunk's own run failed (#616).
2. **The full run** (:func:`tick`, from :func:`asf.trunk_watch.tick`). When the stall alarm fires
   (``ci.trunk_stall_hours``) or a trunk red is suspected, the trunk's workflow
   (:func:`asf.ci_queue.workflow_for` ``trunk``) is dispatched on the trunk — ``gh workflow run
   <wf> --ref <trunk>``, through :mod:`asf.github` (the product's ``gh`` auth) — **once per tip**. A
   ``workflow_dispatch`` run is never a ``push``, so it never takes the attested skip: every heavy
   job runs. Its verdict, its failed required jobs flake-triaged (:func:`asf.flake.triage`, one
   re-run), decides: **red** confirms trunk red — one ``trunk watch: TRUNK RED`` line, and one S1
   fix card through the intake (:func:`asf.groom.inbox.file_card`: the check, the test, the log
   tail, the run, the first red sha; an open card or intake file with the same signature is linked
   instead of a second); **green** clears the suspicion at that tip — the landings' reds are their
   own and normal culprit handling resumes.
3. **The safety net**. The trunk's full workflow runs at least every
   ``ci.trunk_full_every_hours`` (default 6): when no ``schedule`` or ``workflow_dispatch`` run of
   it on the trunk is that recent, one is dispatched the same way, so a break on the trunk is
   caught within that many hours even with nothing landing.

``asf status`` and ``asf doctor`` show ``TRUNK RED: <check> (seen on #a, #b)`` while one holds —
off the state file, no host call. A trunk move clears it: a batch lands only with every required
check green at its sha. State: ``state/<product>/trunk-red.json``. Never raises into the tick.

A ``gh`` read that does not answer is **Unknown** (:class:`asf.github.Result`), never an empty
listing: an unread run list neither loses a dispatched run nor finds "no full run on record" and
dispatches the safety net on it — the next read decides.
"""
import datetime
import json
import os
import time

from asf import gh_limit, github

STATE_FILE = 'trunk-red.json'
#: how long a landing's red counts towards a suspicion
SEEN_HOURS = 24
#: unrelated landings red on one check before it reads as trunk red
SEEN_MIN = 2
#: how often a dispatched full run is read while it runs
READ_EVERY_S = 300
#: a dispatched run not listed this long after the dispatch is lost (dispatched again, at most
#: :data:`MAX_TRIES` times on one tip)
FIND_S = 1800
MAX_TRIES = 2
#: how often the safety net lists the trunk's runs while it is due and nothing was found
LIST_EVERY_S = 600
#: a job that ended in one of these failed
RED = ('failure', 'timed_out', 'startup_failure')
#: lines of the failing step's log the fix card carries
TAIL_LINES = 30
#: the signature the fix card carries, per check: one open card per check
SIGNATURE = 'trunk-red {check}'


def _now():
    return time.time()


def _sd(product):
    from asf import env
    return env.state_dir(product)


def path(state_dir):
    return os.path.join(state_dir, STATE_FILE)


def load(state_dir):
    try:
        with open(path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    for k, kind in (('seen', list), ('confirmed', dict), ('filed', dict)):
        if not isinstance(data.get(k), kind):
            data[k] = kind()
    return data


def save(state_dir, data):
    os.makedirs(state_dir, exist_ok=True)
    tmp = path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, path(state_dir))


def job(name):
    """The job a check answers for (a matrix leg is its job)."""
    from asf import flake
    return flake.job_key(name)


def watched(product):
    from asf import trunk_watch
    return trunk_watch.watched(product)


def _trunk(state_dir):
    """``(tip, moved_at)`` of the trunk as the trunk watch last read it."""
    from asf import trunk_watch
    tw = trunk_watch.load(state_dir)
    moved = tw.get('moved_at')
    return tw.get('checked'), (moved if isinstance(moved, (int, float)) else 0)


# ---- 1. suspicion: the same check red on unrelated landings -------------------------------------

def known(product, key, head, names):
    """True when every one of ``names`` is recorded for landing ``key`` at ``head`` already (the
    caller then spares its log reads)."""
    if not watched(product):
        return True
    seen = load(_sd(product))['seen']
    have = {(s.get('key'), s.get('head'), s.get('check')) for s in seen}
    return all((key, head, job(n)) in have for n in names)


def observe(product, names, key, prs, head, base, files, fail_files=(), links=None, now=None):
    """Record that landing ``key`` (``#<pr>`` or ``batch <ref>``; its PRs ``prs``) went red at
    ``head``, cut on trunk sha ``base``, on the checks ``names`` — after triage: a real red. Its
    diff's ``files`` and the files the failing logs name (``fail_files``) are what
    :func:`unrelated` compares. A landing already recorded on a check at that head is not
    recorded twice. Never raises."""
    if not watched(product) or not names:
        return
    now = _now() if now is None else now
    sd = _sd(product)
    try:
        data = load(sd)
        have = {(s.get('key'), s.get('head'), s.get('check')) for s in data['seen']}
        for n in dict.fromkeys(job(n) for n in names if n):
            if (key, head, n) in have:
                continue
            data['seen'].append({'check': n, 'key': key, 'prs': sorted({int(p) for p in prs or ()
                                                                         if p}),
                                 'head': head, 'base': base, 'at': int(now),
                                 'files': sorted(set(files or ())),
                                 'fail_files': sorted(set(fail_files or ())),
                                 'link': (links or {}).get(n)})
        data['seen'] = [s for s in data['seen'] if s.get('at', 0) >= now - SEEN_HOURS * 3600]
        save(sd, data)
    except OSError:
        pass


def _touches(files, named):
    """True when a diff's ``files`` holds a file a log names (itself, or a path it ends with)."""
    for p in named:
        if any(f == p or p.endswith('/' + f) for f in files):
            return True
    return False


def unrelated(a, b):
    """True when two red landings share no PR, and neither diff touches a file the failing logs
    name — or, with no file named in either log, the two diffs share no file."""
    if set(a.get('prs') or ()) & set(b.get('prs') or ()) or a.get('key') == b.get('key'):
        return False
    named = set(a.get('fail_files') or ()) | set(b.get('fail_files') or ())
    fa, fb = set(a.get('files') or ()), set(b.get('files') or ())
    if named:
        return not _touches(fa, named) and not _touches(fb, named)
    return not (fa & fb)


def suspects(product, now=None, data=None):
    """``{check: [the landings red on it]}``: each check red on at least :data:`SEEN_MIN`
    unrelated landings since the trunk last moved (and within :data:`SEEN_HOURS`) — none once a
    full run judged the trunk's current tip green. Off the state files only."""
    if not watched(product):
        return {}
    sd = _sd(product)
    data = load(sd) if data is None else data
    now = _now() if now is None else now
    tip, moved = _trunk(sd)
    if not tip:
        return {}   # the trunk watch has not read the trunk yet: nothing to compare with
    floor = max(now - SEEN_HOURS * 3600, moved or 0)
    if (data.get('green') or {}).get('sha') == tip:
        return {}   # a full run judged this tip green: the landings' reds are their own
    by = {}
    for s in data['seen']:
        if s.get('at', 0) >= floor:
            by.setdefault(s.get('check'), []).append(s)
    out = {}
    for check, seen in by.items():
        pair = next(((a, b) for i, a in enumerate(seen) for b in seen[i + 1:] if unrelated(a, b)),
                    None)
        if pair:
            out[check] = [a for a in seen if any(unrelated(a, b) for b in seen if b is not a)]
    return out


def confirmed(product, data=None):
    """``{check: record}``: the checks a full run found red on the trunk's current tip."""
    if not watched(product):
        return {}
    sd = _sd(product)
    data = load(sd) if data is None else data
    tip, _moved = _trunk(sd)
    return {k: c for k, c in data['confirmed'].items() if tip and c.get('sha') == tip}


def held(product, names, now=None):
    """``{name: trunk sha}``: each of ``names`` whose job is trunk red — confirmed by a full run
    on the current tip, or suspected (:func:`suspects`). The caller treats it as red on the trunk:
    no member blamed, no correct round. ``{}`` off the merge queue or when nothing reads."""
    if not names or not watched(product):
        return {}
    try:
        sd = _sd(product)
        data = load(sd)
        tip, _moved = _trunk(sd)
        red = set(confirmed(product, data)) | set(suspects(product, now, data))
    except OSError:
        return {}
    return {n: tip for n in names if n and job(n) in red}


def seen_on(landings):
    """``#a, #b``: the PRs of the landings red on a check (a batch: its members)."""
    prs = sorted({p for s in landings for p in s.get('prs') or ()})
    return ', '.join(f'#{p}' for p in prs) or ', '.join(sorted({s.get('key') or '?'
                                                                 for s in landings}))


# ---- 2 and 3. the full run on the trunk ---------------------------------------------------------

def _auth_env(product):
    from asf import ci_pool
    return ci_pool._gh_env(product)


class Door:
    """The trunk watch's ``gh`` door: :func:`asf.github.gh` under the product's ``gh`` auth
    (``auth_env``). A test passes its own object with the same ``gh(args, json=)``."""

    def __init__(self, product):
        self.product = product

    def gh(self, args, json=False):
        from asf import mutation_guard
        if mutation_guard.is_mutating_gh(args):
            gh_limit.forget()   # a write can change any listing this process read before it
        return github.gh(args, json=json, timeout=github.JSON_TIMEOUT_S,
                         env=_auth_env(self.product))


def _source(product, src):
    return src if src is not None else Door(product)


def _read(src, args, kind):
    """``src``'s JSON answer to ``args`` when it is a ``kind`` — else None: Unknown (the read
    did not answer, or answered something else)."""
    r = src.gh(args, json=True)
    return r.data if r.ok and isinstance(r.data, kind) else None


def _why(r):
    """The reason a refused call gives: ``gh``'s own last line, else the client's reason."""
    lines = [ln.strip() for ln in (r.stderr or '').splitlines() if ln.strip()]
    return lines[-1] if lines else (r.reason or 'refused')


def _epoch(iso):
    try:
        return int(datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp())
    except (TypeError, ValueError):
        return None


def workflow(product):
    from asf import ci_queue
    return ci_queue.workflow_for(product, 'trunk')


def _short(sha):
    return str(sha or '?')[:9]


def dispatch(product, data, tip, why, now, src, out):
    """``gh workflow run <wf> --ref <trunk>``: the trunk's full workflow on its tip — a
    ``workflow_dispatch`` run, so never the attested ``push`` skip. One line; the run is recorded
    as ``full`` and read by :func:`read_full`."""
    trunk, wf = product.conventions.main, workflow(product)
    prev = data.get('full') or {}
    tries = (prev.get('tries') or 0) + 1 if prev.get('sha') == tip else 1
    r = src.gh(['workflow', 'run', wf, '--ref', trunk, '-R', product.repo_slug])
    if not r.ok:
        err = _why(r)
        out(f'trunk watch: full run of {wf} on {trunk} @ {_short(tip)} refused — {err} '
            f'({why}); asked again next tick')
        data['full'] = {'sha': tip, 'at': int(now), 'why': why, 'state': 'refused',
                        'tries': prev.get('tries') or 0 if prev.get('sha') == tip else 0,
                        'error': str(err)[:200]}
        return False
    out(f'trunk watch: dispatched the full {wf} run on {trunk} @ {_short(tip)} — {why}')
    data['full'] = {'sha': tip, 'at': int(now), 'why': why, 'state': 'dispatched',
                    'tries': tries}
    data['full_at'] = int(now)
    return True


#: what :func:`_find_run` returns when the listing did not read
UNKNOWN = object()


def _find_run(product, src, full):
    """The ``workflow_dispatch`` run of the trunk's workflow made for ``full`` (its sha, created
    after the dispatch), None when the listing holds none, :data:`UNKNOWN` when it did not
    read."""
    trunk, wf = product.conventions.main, workflow(product)
    runs = _read(src, ['run', 'list', '-R', product.repo_slug, '-w', wf, '-b', trunk,
                       '-e', 'workflow_dispatch', '-L', '10', '--json',
                       'databaseId,headSha,status,conclusion,createdAt,url'], list)
    if runs is None:
        return UNKNOWN
    for r in runs:
        made = _epoch(r.get('createdAt')) if isinstance(r, dict) else None
        if r.get('headSha') == full.get('sha') and made is not None \
                and made >= (full.get('at') or 0) - 120:
            return r
    return None


def _required(product, sha):
    """The trunk's required set at ``sha`` (the landing checks and the deploy's jobs), or None."""
    try:
        from asf.harvest import lane as lane_mod
        names, _why = lane_mod.GitHubHost(product).merge_required(_sd(product), lane_mod.CODE, sha)
        return names
    except Exception:  # noqa: BLE001 — unreadable: every failed job counts
        return None


def red_jobs(jobs, required):
    """The failed jobs (``{name, link}``) of a run that judge the trunk: each failed required
    job; and when a required job was skipped, every failed job (a ``needs:`` upstream). With no
    required set read, every failed job."""
    from asf.harvest import lane as lane_mod
    failed = [j for j in jobs if j.get('conclusion') in RED]
    if required is None:
        pick = failed
    else:
        pick = [j for j in failed if lane_mod.required_name(j.get('name'), required)]
        skipped = [j for j in jobs if j.get('conclusion') == 'skipped'
                   and lane_mod.required_name(j.get('name'), required)]
        if skipped:
            pick = failed
    return [{'name': j.get('name'), 'link': j.get('url') or ''} for j in pick]


def read_full(product, data, now, src, out):
    """Read the dispatched full run (at most every :data:`READ_EVERY_S`): ``running`` while it
    runs; on completion its red required jobs are flake-triaged, then ``red`` (confirmed: line and
    fix card) or ``green`` (the suspicion at that tip cleared). A run never listed within
    :data:`FIND_S` is ``lost``."""
    full = data.get('full') or {}
    if full.get('state') not in ('dispatched', 'running'):
        return
    if now - (full.get('read_at') or 0) < READ_EVERY_S:
        return
    full['read_at'] = int(now)
    trunk, slug = product.conventions.main, product.repo_slug
    if not full.get('run'):
        r = _find_run(product, src, full)
        if r is UNKNOWN:
            return      # the listing did not read: never "lost" on it — read again
        if r is None:
            if now - (full.get('at') or now) > FIND_S:
                full['state'] = 'lost'
                out(f'trunk watch: the full run dispatched on {trunk} @ {_short(full.get("sha"))} '
                    f'was never listed in {FIND_S // 60} min — lost')
            return
        full['run'], full['url'] = str(r.get('databaseId')), r.get('url')
        full['state'] = 'running'
    got = _read(src, ['run', 'view', full['run'], '-R', slug, '--json',
                      'status,conclusion,url,jobs'], dict)
    if got is None or got.get('status') != 'completed':
        return
    full['url'] = got.get('url') or full.get('url')
    sha = full.get('sha')
    jobs = [j for j in got.get('jobs') or () if isinstance(j, dict)]
    red = red_jobs(jobs, _required(product, sha))
    from asf import flake
    defects, rerun = flake.triage(product, _sd(product), slug, sha, red,
                                  where=f'{trunk} full run', out=out) if red else ([], [])
    if rerun and not defects:
        return      # re-running a red job once (flake triage): judged when it ends
    if not defects:
        full['state'] = 'green'
        data['green'] = {'sha': sha, 'at': int(now), 'run': full['run']}
        data['confirmed'] = {}
        out(f"trunk watch: the full run on {trunk} @ {_short(sha)} is green ({full.get('url')}) — "
            f"the landings' reds are their own; normal culprit handling resumes")
        return
    full['state'] = 'red'
    by_name = {c['name']: c for c in red}
    findings = _findings(slug, [by_name[n] for n in defects if n in by_name])
    for n in defects:
        k = job(n)
        f = findings.get(n) or {}
        first = _first_red(data, k, sha)
        rec = {'sha': sha, 'name': n, 'run': full['run'], 'url': full.get('url'),
               'link': (by_name.get(n) or {}).get('link'), 'at': int(now), 'first': first,
               'test': (f.get('tests') or [None])[0], 'step': f.get('step'),
               'tail': (f.get('lines') or [])[-TAIL_LINES:],
               'seen': [s.get('key') for s in data['seen'] if s.get('check') == k]}
        data['confirmed'][k] = rec
        card = _file(product, data, k, rec, out)
        landings = [s for s in data['seen'] if s.get('check') == k]
        out(f"trunk watch: TRUNK RED {n} on {trunk} @ {_short(sha)} — confirmed by the full run "
            f"{full.get('url')}" + (f'; seen on {seen_on(landings)}' if landings else '')
            + f"; no landing is blamed for it; fix card: {card or 'not filed'}")


def _findings(slug, checks):
    try:
        from asf import merge_queue
        return {i['name']: i for i in merge_queue.failure_findings(slug, checks)}
    except Exception:  # noqa: BLE001 — no log read: the card names the run
        return {}


def _first_red(data, k, sha):
    """The first trunk sha the check is known red on: the oldest base a landing red on it was cut
    on, else the sha the full run judged."""
    seen = sorted((s for s in data['seen'] if s.get('check') == k and s.get('base')),
                  key=lambda s: s.get('at', 0))
    return seen[0]['base'] if seen else sha


def card_text(product, rec):
    """``(title, rest)`` of the S1 fix card for a confirmed trunk red."""
    trunk = product.conventions.main
    name, sha = rec.get('name'), rec.get('sha')
    title = f"Trunk red: {name} fails on {trunk} @ {_short(sha)}"
    lines = ['type: bug', 'severity: S1', f"signature: {SIGNATURE.format(check=job(name))}", '',
             f"The required check `{name}` fails on `{trunk}` itself: the full run of the trunk's "
             f"workflow on {sha} is red ({rec.get('url') or '?'}). Every landing fails it until "
             f"`{trunk}` is fixed; ASF blames none of them and sends none to a correct round.", '',
             f"- check: `{name}`" + (f" — step `{rec['step']}`" if rec.get('step') else ''),
             f"- test: `{rec['test']}`" if rec.get('test') else '- test: (the log names none)',
             f"- failed job: {rec.get('link') or rec.get('url') or '?'}",
             f"- first red sha: {rec.get('first') or sha}"]
    if rec.get('seen'):
        lines.append(f"- landings red on it: {', '.join(rec['seen'])}")
    if rec.get('tail'):
        lines += ['', 'Log tail:', '', '```', *[str(l) for l in rec['tail']], '```']
    lines += ['', '## Acceptance', f"- [ ] `{name}` is green on a full run of `{trunk}`", '']
    return title, '\n'.join(lines)


def existing_card(product, k):
    """The open card (id) or intake file (name) carrying the trunk-red signature of job ``k``, or
    None — a fix already filed (by ASF or by hand) is linked, never filed twice."""
    root = os.path.expanduser(getattr(product, 'backlog_dir', None) or '')
    if not root or not os.path.isdir(root):
        return None
    sig = SIGNATURE.format(check=k)
    intake = os.path.join(root, product.conventions.intake_dir)
    if os.path.isdir(intake):
        for name in sorted(os.listdir(intake)):
            p = os.path.join(intake, name)
            if name.endswith('.md') and os.path.isfile(p):
                try:
                    with open(p, encoding='utf-8') as fh:
                        if f'signature: {sig}' in fh.read():
                            return name
                except OSError:
                    continue
    try:
        from asf.record.core import is_open, load_items
        items, _errors = load_items(root)
    except Exception:  # noqa: BLE001 — an unreadable record: nothing found
        return None
    for iid, rec in sorted(items.items()):
        if str((rec.get('meta') or {}).get('signature') or '').strip() == sig and is_open(rec):
            return iid
    return None


def _file(product, data, k, rec, out):
    """File the S1 fix card once per check and first red sha, through the intake; an open card
    with the same signature is linked instead. The card's id or file name, or None."""
    done = data['filed'].get(k) or {}
    if done.get('first') == rec.get('first') and done.get('card'):
        return done['card']
    found = existing_card(product, k)
    if found:
        data['filed'][k] = {'first': rec.get('first'), 'card': found, 'at': rec['at'],
                            'linked': True}
        return found
    root = os.path.expanduser(getattr(product, 'backlog_dir', None) or '')
    if not root or not os.path.isdir(root):
        out(f'trunk watch: no record to file the {k} fix card into (backlog_dir)')
        return None
    try:
        from asf.groom.inbox import file_card
        title, rest = card_text(product, rec)
        p = file_card(root, product.conventions.intake_dir, title, rest)
    except Exception as e:  # noqa: BLE001 — the line still says it; the next confirm files again
        out(f'trunk watch: filing the {k} fix card failed — {e}')
        return None
    name = os.path.basename(p)
    data['filed'][k] = {'first': rec.get('first'), 'card': name, 'at': rec['at']}
    return name


def _last_full(product, src):
    """Epoch of the newest ``schedule`` or ``workflow_dispatch`` run of the trunk's workflow on
    the trunk (every heavy job ran), None when the listing holds none, :data:`UNKNOWN` when it
    did not read."""
    trunk, wf = product.conventions.main, workflow(product)
    runs = _read(src, ['run', 'list', '-R', product.repo_slug, '-w', wf, '-b', trunk,
                       '-L', '30', '--json', 'event,createdAt,headSha'], list)
    if runs is None:
        return UNKNOWN
    times = [_epoch(r.get('createdAt')) for r in runs if isinstance(r, dict)
             and r.get('event') in ('schedule', 'workflow_dispatch')]
    times = [t for t in times if t]
    return max(times) if times else None


def stale_first(product, tip, now=None):
    """What the stall reaction refreshes before it suspects the trunk: the PRs reopened for a
    fresh run that has not shown yet (:func:`asf.stale_ref.in_flight`), and each ``asf land``
    request marked red on its checks before the trunk's tip arrived (the queue's next pass reads
    it again on a fresh merge ref), for :data:`asf.stale_ref.WAIT_S` after that. ``''`` when
    none."""
    from asf import merge_queue, stale_ref
    sd = _sd(product)
    prs = set(stale_ref.in_flight(product, now))
    moved = stale_ref.arrival(product, os.path.expanduser(product.repo_dir or ''), tip)
    now = _now() if now is None else now
    for k, r in merge_queue.load_requests(sd).items():
        red = r.get('red') or {}
        at = stale_ref._epoch(red.get('at'))
        # bounded: a request the queue never reads again (its branch gone) holds nothing for long
        if red.get('kind') == 'checks' and at and moved and at < moved \
                and now - moved < stale_ref.WAIT_S:
            prs.add(int(k))
    return ('PR ' + ', '.join(f'#{n}' for n in sorted(prs)) + ' red on a merge ref from before '
            f'{product.conventions.main} moved') if prs else ''


def tick(product, stall=None, out=print, now=None, src=None):
    """The trunk watch's act: read the full run in flight; then dispatch one on the trunk's tip
    when the stall alarm fires (``stall``, its text) or a trunk red is suspected — once per tip —
    or when no full run is ``ci.trunk_full_every_hours`` recent. Never raises."""
    if not watched(product) or not getattr(product, 'repo_slug', None) or not workflow(product):
        return
    now = _now() if now is None else now
    sd = _sd(product)
    tip, _moved = _trunk(sd)
    if not tip:
        return
    try:
        src = _source(product, src)
        data = load(sd)
        data['confirmed'] = {k: c for k, c in data['confirmed'].items() if c.get('sha') == tip}
        read_full(product, data, now, src, out)
        full = data.get('full') or {}
        busy = full.get('state') in ('dispatched', 'running')
        why = None
        sus = suspects(product, now, data)
        if sus:
            why = 'trunk red suspected: ' + '; '.join(
                f'{k} (seen on {seen_on(v)})' for k, v in sorted(sus.items()))
        elif stall:
            first = stale_first(product, tip, now)
            if first:
                # the stall's first step: a landing red on a merge ref of an older trunk gets a
                # fresh run (asf.stale_ref) before the trunk itself is suspected
                if data.get('deferred') != first:
                    out(f'trunk watch: stall — {first}: fresh runs on '
                        f'{product.conventions.main} first, the full run waits for them')
                data['deferred'] = first
            else:
                why = f'stall alarm: {stall}'
                data.pop('deferred', None)
        if why and not busy:
            tried = full.get('sha') == tip and (
                full.get('state') in ('red', 'green')
                or (full.get('tries') or 0) >= MAX_TRIES)
            if not tried and (full.get('state') != 'refused' or now - full.get('at', 0)
                              >= LIST_EVERY_S):
                busy = dispatch(product, data, tip, why, now, src, out)
        every = product.conventions.trunk_full_every_hours()
        if every and not busy:
            last = data.get('full_at') or 0
            if now - last >= every * 3600 and now - (data.get('listed') or 0) >= LIST_EVERY_S:
                data['listed'] = int(now)
                seen = _last_full(product, src)
                if seen is UNKNOWN:
                    out(f'trunk watch: the runs of {workflow(product)} on '
                        f'{product.conventions.main} unreadable — the safety net is asked '
                        f'again in {LIST_EVERY_S // 60} min, never dispatched on an unread list')
                    seen = None
                    every = None
                if seen and seen > last:
                    data['full_at'] = last = seen
                if every and now - last >= every * 3600:
                    dispatch(product, data, tip, f'safety net: no full run of '
                             f'{product.conventions.main} in {every}h '
                             f'(conventions.ci.trunk_full_every_hours)', now, src, out)
        save(sd, data)
    except Exception as e:  # noqa: BLE001 — the watch never breaks the tick
        out(f'trunk watch: trunk red pass failed — {type(e).__name__}: {e}')


# ---- the rows -----------------------------------------------------------------------------------

def lines(product, now=None):
    """``TRUNK RED: <check> (seen on #a, #b) — …`` per trunk-red check, confirmed first. Off
    the state files."""
    if not watched(product):
        return []
    sd = _sd(product)
    data = load(sd)
    trunk = product.conventions.main
    full = data.get('full') or {}
    out = []
    conf = confirmed(product, data)
    for k, c in sorted(conf.items()):
        landings = [s for s in data['seen'] if s.get('check') == k]
        card = (data['filed'].get(k) or {}).get('card')
        out.append(f"TRUNK RED: {c.get('name') or k}"
                   + (f' (seen on {seen_on(landings)})' if landings else '')
                   + f" — confirmed by the full run on {trunk} @ {_short(c.get('sha'))} "
                     f"({c.get('url') or '?'}); fix card {card or 'not filed'}; no landing blamed")
    for k, v in sorted(suspects(product, now, data).items()):
        if k in conf:
            continue
        state = (f"full run on {trunk} @ {_short(full.get('sha'))} {full.get('state')}"
                 if full.get('sha') else f'full run on {trunk} not dispatched yet')
        out.append(f'TRUNK RED: {k} (seen on {seen_on(v)}) — suspected; {state}; '
                   f'no landing blamed')
    return out


def status_cell(product, now=None):
    """``RED TRUNK RED: …`` while a trunk red holds, else None (no row)."""
    got = lines(product, now)
    return 'RED ' + '; '.join(got) if got else None


def doctor_rows(product, now=None):
    """``[(required, ok, detail)]``: one red row per trunk-red check; else one green row naming
    the last full run on the trunk; ``[]`` off the merge queue."""
    if not watched(product):
        return []
    got = lines(product, now)
    if got:
        return [(True, False, l) for l in got]
    data = load(_sd(product))
    now = _now() if now is None else now
    every = product.conventions.trunk_full_every_hours()
    last = data.get('full_at')
    net = (f'a full run every {every}h (conventions.ci.trunk_full_every_hours)' if every
           else 'the safety net is off (conventions.ci.trunk_full_every_hours: 0)')
    if not last:
        return [(True, True, f'no check red on {product.conventions.main}; no full run read yet '
                             f'— {net}')]
    return [(True, True, f'no check red on {product.conventions.main}; last full run '
                         f'{(now - last) / 3600:.1f}h ago — {net}')]
