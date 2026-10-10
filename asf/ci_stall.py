"""asf.ci_stall — the CI stall watch: a job silent past its own measured length is cancelled,
claimed, and re-run once (F-0286).

Each self-hosted CI box writes one heartbeat file per runner slot under
``/var/lib/ci-heartbeat/`` (the box-side timer): the slot's runner, whether it is busy, its job,
run and job id, the job's start, its last output, and the CPU seconds its process tree has used.
Until this module the host read those files with an operator script outside the package, whose
box list, repository and per-job limits were hand-kept copies — its limits a JSON file edited by
hand when a job grew (2026-10-07: a busy ``gate-tests`` cancelled at 1206 s because its hand
limit had not followed it). This is that watch, inside the package:

* **The boxes** are the product's ``ci.pool`` (:func:`asf.ci_heartbeat.boxes`), each read by one
  ssh to its target (:func:`asf.ci_heartbeat.target`). A box that answers records its beat
  (:func:`asf.ci_heartbeat.record`), so ``asf doctor``'s heartbeat row reads this watch too.
* **A stall** is a busy slot silent for :data:`SILENCE_S` *and* past its job's max *and* using
  less CPU than :data:`CPU_FLOOR` cores since the last pass. A job burning CPU is working, however
  quiet its log: CPU above the floor is never a stall. A slot first seen this pass has no CPU
  rate yet and is never a stall either — the next pass decides.
* **The max** is the job kind's measured p95 (:mod:`asf.ci_measure`'s green readings off the
  ``ci`` metrics stream: the runner's own, else the fleet's for the kind) times :data:`FACTOR`,
  re-measured once a day (:data:`REFRESH_S`) into ``<state>/ci-stall.json``; a kind with fewer
  than :data:`MIN_READINGS` readings falls back to :data:`DEFAULT_MAX_S`.
* **A cancel** is claimed in this watch's own file before it is made (never a loop), then made
  the queue's one way (:func:`asf.ci_queue._cancel_run`) and claimed in ``ci-cancels.json`` as
  ``stall`` (:func:`asf.ci_queue.claim_cancel`), so the run never reads as a host cancel or a
  verdict. Once the run has completed, its failed jobs are re-run once (``gh run rerun
  --failed``); a run that has not completed :data:`GIVE_UP_S` after its cancel is given up on.
* **The mode** is the same file the operator script reads, ``<ASF home>/state/ci-heartbeat/mode``
  (``act`` or ``report``; env ``CI_HB_MODE`` over it; default ``report``). In ``report`` a stall
  is printed and nothing is cancelled.
* **One watcher at a time.** While the operator script still runs, its ``claims.json`` in the
  same directory moves every minute; this watch refuses to act while that file moved in the
  last :data:`LEGACY_QUIET_S` seconds, and says so — both acting on one stall would cancel and
  re-run it twice. Moving the host's timer from the script to ``asf ci stall-watch --apply`` is
  the operator's step; this module never touches the script or its timer.

``asf ci stall-watch`` prints the pass (``--apply``: act per the mode; without it, report only).
"""
import calendar
import json
import math
import os
import subprocess
import time

from asf import ci_heartbeat, ci_measure, config_keys, env

#: a busy slot whose job printed nothing for this long may be stalled
SILENCE_S = 5 * 60
#: a heartbeat file older than this is a dead heartbeat, not a slot to judge
HEARTBEAT_STALE_S = 150
#: a job's max is its measured p95 times this
FACTOR = 2.0
#: a job kind with too few readings for a p95 gets this max
DEFAULT_MAX_S = 60 * 60
#: green readings of a kind a p95 is measured on
MIN_READINGS = 5
#: a job's process tree using at least this many cores since the last pass is working
CPU_FLOOR = 0.05
#: the max table is measured again this often
REFRESH_S = 24 * 60 * 60
#: the operator script's claims file moved this recently: it still runs, so this watch reports
LEGACY_QUIET_S = 5 * 60
#: a cancelled run not completed this long after its cancel is not re-run
GIVE_UP_S = 30 * 60
#: one ssh read of one box
SSH_TIMEOUT_S = 30
#: no stall-watch pass inside this long, with a non-empty pool, is nobody watching
PASS_STALE_S = 15 * 60
#: a claim whose cancel or re-run was refused is retried at most this many times
RERUN_TRIES = 3
#: a claim still in a bad state is held this long, long past the resolved claims' prune
BAD_KEEP_S = 14 * 24 * 60 * 60
#: the claim states that mean a cancel was made and its re-run did not happen
BAD_STATES = ('cancel-failed', 'rerun-failed', 'gave-up')

TUNABLES = {'SILENCE_S': 'ci.stall_watch.silence_s',
            'HEARTBEAT_STALE_S': 'ci.stall_watch.heartbeat_stale_s',
            'FACTOR': 'ci.stall_watch.factor', 'DEFAULT_MAX_S': 'ci.stall_watch.default_max_s',
            'CPU_FLOOR': 'ci.stall_watch.cpu_floor', 'PASS_STALE_S': 'ci.stall_watch.pass_stale_s'}

#: this watch's own record, under the product's state dir
STATE_FILE = 'ci-stall.json'
#: the box-side heartbeat files, one JSON object per runner slot
REMOTE = ('for f in /var/lib/ci-heartbeat/*.json; do [ -f "$f" ] && cat "$f" && echo; done; '
          'true')


def tunable(name):
    """The constant ``name`` of :data:`TUNABLES` with its config key over it."""
    return config_keys.value(TUNABLES[name], globals()[name])


def _now():
    return time.time()


def _ts(stamp):
    """Epoch seconds of a ``YYYY-mm-ddTHH:MM:SSZ`` stamp, or None."""
    try:
        return float(calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')))
    except (TypeError, ValueError):
        return None


# ---- the shared directory: the mode and the operator script's claims ----------------------

def hb_dir():
    """The directory the operator script and this watch share: env ``CI_HB_STATE`` (the
    script's own override), else ``<ASF home>/state/ci-heartbeat``."""
    return os.environ.get('CI_HB_STATE') or os.path.join(env.ASF_HOME, 'state', 'ci-heartbeat')


def mode():
    """``act`` or ``report``: env ``CI_HB_MODE``, else the ``mode`` file, else ``report``."""
    m = os.environ.get('CI_HB_MODE')
    if not m:
        try:
            with open(os.path.join(hb_dir(), 'mode'), encoding='utf-8') as f:
                m = f.read().strip()
        except OSError:
            m = 'report'
    return 'act' if m == 'act' else 'report'


def legacy_active(now=None):
    """Seconds since the operator script last wrote its ``claims.json`` when that is under
    :data:`LEGACY_QUIET_S` (it still runs), else None."""
    try:
        age = (now if now is not None else _now()) - os.path.getmtime(
            os.path.join(hb_dir(), 'claims.json'))
    except OSError:
        return None
    return age if age < LEGACY_QUIET_S else None


# ---- this watch's own record ---------------------------------------------------------------

def _path(product):
    return os.path.join(env.state_dir(product.name), STATE_FILE)


def load(product):
    try:
        with open(_path(product), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    for k in ('max', 'cpu', 'claims', 'pass'):
        if not isinstance(data.get(k), dict):
            data[k] = {}
    return data


def save(product, data):
    path = _path(product)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ---- the max: measured p95 x factor, refreshed daily ---------------------------------------

def _p95(values):
    """Nearest rank, as :func:`asf.ci_measure._p50`: a length some run actually reached."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else None


def measure(readings):
    """``{'runner': {runner: {kind: p95}}, 'kind': {kind: p95}}`` over green readings
    (:func:`asf.ci_measure.readings`), each with :data:`MIN_READINGS` or more."""
    per, fleet = {}, {}
    for (runner, kind), series in (readings or {}).items():
        greens = [r.seconds for r in series if r.conclusion in ci_measure.MEASURED]
        fleet.setdefault(kind, []).extend(greens)
        if len(greens) >= MIN_READINGS:
            per.setdefault(runner, {})[kind] = _p95(greens)
    return {'runner': per, 'kind': {k: _p95(v) for k, v in fleet.items()
                                    if len(v) >= MIN_READINGS}}


def _events(product):
    from asf.metrics import metrics
    from asf.tick import shadow
    try:
        return metrics.read_stream(shadow.record_dir(product.name), 'ci', metrics.days_back(
            metrics.today(), ci_measure.tunable('WINDOW_DAYS')))
    except Exception:  # noqa: BLE001 — no stream is no measure: the default max applies
        return []


def refresh_max(product, data, now, events=None):
    """Measure the max table again when it is older than :data:`REFRESH_S` (or absent)."""
    taken = (data['max'] or {}).get('taken')
    if isinstance(taken, (int, float)) and now - taken < REFRESH_S:
        return False
    events = _events(product) if events is None else events
    data['max'] = dict(measure(ci_measure.readings(events)), taken=now)
    return True


def job_max(data, job, runner):
    """``(seconds, source)``: the job kind's p95 on this runner, else across the fleet, times
    :data:`FACTOR`; else :data:`DEFAULT_MAX_S`."""
    kind = ci_measure.job_kind(job or '')
    table = data.get('max') or {}
    p95 = ((table.get('runner') or {}).get(runner) or {}).get(kind)
    src = 'runner p95'
    if not p95:
        p95, src = (table.get('kind') or {}).get(kind), 'fleet p95'
    if not p95:
        return float(tunable('DEFAULT_MAX_S')), 'default'
    return float(p95) * float(tunable('FACTOR')), src


# ---- the boxes -------------------------------------------------------------------------------

def fetch(box, target):
    """``(heartbeats, None)`` off one box, or ``(None, why)``."""
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', target, REMOTE]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=SSH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, 'ssh timeout'
    except OSError as e:
        return None, f'ssh did not run ({e})'
    if p.returncode != 0:
        tail = (p.stderr.strip().splitlines() or [''])[-1][:160]
        return None, f'ssh rc={p.returncode} {tail}'.strip()
    hbs = []
    for line in p.stdout.splitlines():
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            hbs.append(got)
    return hbs, None


def judge(hb, prev_cpu, now, data):
    """``(stalled, facts)`` for one busy heartbeat. ``facts`` carries elapsed, silence, the max
    and its source, and the CPU rate (None when unknown)."""
    start, last = _ts(hb.get('started_at')), _ts(hb.get('last_output_at'))
    elapsed = int(now - start) if start else 0
    silence = int(now - (last or start or now))
    mx, src = job_max(data, hb.get('job'), hb.get('runner'))
    rate = None
    cpu = hb.get('tree_cpu_s')
    if isinstance(prev_cpu, dict) and isinstance(cpu, (int, float)) \
            and prev_cpu.get('job_id') == hb.get('job_id'):
        dt = now - float(prev_cpu.get('at') or now)
        if dt > 0:
            rate = max(0.0, (float(cpu) - float(prev_cpu.get('cpu_s') or 0)) / dt)
    facts = {'elapsed': elapsed, 'silence': silence, 'max': int(mx), 'max_from': src,
             'cpu': rate}
    stalled = (silence >= tunable('SILENCE_S') and elapsed > mx
               and rate is not None and rate < tunable('CPU_FLOOR'))
    return stalled, facts


# ---- the pass --------------------------------------------------------------------------------

def _gh(src, args):
    out, why = src.gh_try(args)
    return (out is not None), (out if out is not None else why)


def _advance(product, src, data, now, out):
    """Every cancelled claim whose run has completed is re-run once (its failed jobs); one not
    completed :data:`GIVE_UP_S` after its cancel is given up on."""
    for key, c in data['claims'].items():
        if c.get('state') != 'cancelled':
            continue
        ok, status = _gh(src, ['run', 'view', str(c['run']), '-R', product.repo_slug,
                               '--json', 'status', '-q', '.status'])
        if ok and str(status).strip() == 'completed':
            ok2, why = _gh(src, ['run', 'rerun', str(c['run']), '-R', product.repo_slug,
                                 '--failed'])
            c.update(state='rerun' if ok2 else 'rerun-failed', rerun_at=now)
            out(f"ci stall: re-ran the failed jobs of run {c['run']} ({c.get('job')})"
                if ok2 else f"ci stall: re-run of run {c['run']} refused — {why}")
        elif now - float(c.get('cancelled_at') or now) > GIVE_UP_S:
            c['state'] = 'gave-up'
            out(f"ci stall: gave up on run {c['run']} — still {status or '?'} "
                f'{GIVE_UP_S // 60} min after its cancel')


def watch(product, apply=False, fetch_fn=None, src=None, now=None, out=print, events=None):
    """One pass over every pool box (see the module doc). ``apply``: act per :func:`mode`
    (without it nothing is cancelled). Returns ``(stalls, cancels)``."""
    from asf import ci_queue
    now = now if now is not None else _now()
    fetch_fn = fetch_fn or fetch
    data = load(product)
    refresh_max(product, data, now, events)
    md = mode() if apply else 'report'
    legacy = legacy_active(now)
    if md == 'act' and legacy is not None:
        out(f'ci stall: report only — the operator watchdog wrote claims.json {int(legacy)}s '
            f'ago and still runs; one watcher acts at a time')
        md = 'report'
    src = src or ci_queue.GitHubSource(product)
    slug = product.repo_slug
    stalls = cancels = boxes_read = unreachable = 0
    cpu = {}
    for box in ci_heartbeat.boxes(product):
        hbs, err = fetch_fn(box, ci_heartbeat.target(box))
        if err:
            unreachable += 1
            out(f'ci stall: ALARM box {box} unreachable — {err}')
            continue
        if not hbs:
            unreachable += 1
            out(f'ci stall: ALARM box {box} has no heartbeat files')
            continue
        boxes_read += 1
        ci_heartbeat.record(product.name, box, when=now)
        for hb in hbs:
            r = hb.get('runner') or '?'
            age = now - (_ts(hb.get('written_at')) or 0)
            if age > tunable('HEARTBEAT_STALE_S'):
                out(f'ci stall: ALARM heartbeat dead on {r} ({box}) — {int(age)}s old')
                continue
            if not hb.get('busy') or (hb.get('repo') and slug and hb.get('repo') != slug):
                continue
            stalled, f = judge(hb, data['cpu'].get(r), now, data)
            if isinstance(hb.get('tree_cpu_s'), (int, float)):
                cpu[r] = {'at': now, 'cpu_s': hb['tree_cpu_s'], 'job_id': hb.get('job_id')}
            if not stalled:
                continue
            stalls += 1
            run_id, job = hb.get('run_id'), hb.get('job') or '?'
            rate = f"{f['cpu']:.2f}" if f['cpu'] is not None else '?'
            out(f"ci stall: STALL {job} on {r} ({box}), run {run_id} — silent {f['silence']}s, "
                f"{f['elapsed']}s in (max {f['max']}s, {f['max_from']} x {tunable('FACTOR'):g}), "
                f'cpu {rate} cores; mode {md}')
            if md != 'act' or not run_id:
                continue
            key = f"{run_id}:{hb.get('job_id') or job}"
            if key in data['claims'] or any(c.get('run') == run_id and c.get('state') in
                                            ('cancelled', 'rerun') for c in
                                            data['claims'].values()):
                continue                            # once per run: never a loop
            data['claims'][key] = {'run': run_id, 'job': job, 'runner': r, 'claimed_at': now,
                                   'state': 'claimed'}
            save(product, data)                     # claim before acting
            done = ci_queue._cancel_run(src, product, run_id, 'in_progress')
            if not done:
                data['claims'][key].update(state='cancel-failed')
                out(f'ci stall: cancel of run {run_id} refused — {done.detail}')
                continue
            data['claims'][key].update(state='cancelled', cancelled_at=now)
            ci_queue.claim_cancel(env.state_dir(product.name), run_id, 'stall', job=job,
                                  runner=r, by='stall-watch')
            cancels += 1
            out(f'ci stall: cancelled run {run_id} ({job} on {r}); its failed jobs re-run once '
                f'it completes')
    data['cpu'] = cpu
    data['pass'] = {'at': now, 'mode': md, 'stalls': stalls, 'cancels': cancels,
                    'boxes': boxes_read, 'unreachable': unreachable,
                    'legacy': None if legacy is None else int(legacy)}
    if md == 'act':
        _advance(product, src, data, now, out)
    data['claims'] = {k: c for k, c in data['claims'].items()
                      if now - float(c.get('claimed_at') or now) < 2 * REFRESH_S}
    save(product, data)
    return stalls, cancels


# ---- the doctor row: which watcher acts, when the last pass ran, and every cancel still owed
#      a re-run (asf.doctor.check_ci_stall) -----------------------------------------------------

def doctor_rows(product, now=None):
    """``[(required, ok, detail)]``: the held timer while the operator watchdog still runs, no
    stall-watch pass for too long over a non-empty pool, one row per claim stuck in a bad state
    (:data:`BAD_STATES`) with its re-run still owed, else one ok row off the last ``pass``.
    ``[]`` for a product with no ``ci.pool`` and no ``pass`` block — not asked about. Reads only
    :func:`load`, :func:`asf.ci_heartbeat.boxes`, :func:`mode` and :func:`legacy_active`: no ssh,
    no ``gh`` call."""
    now = now if now is not None else _now()
    boxes = ci_heartbeat.boxes(product)
    data = load(product)
    blk = data['pass']
    if not boxes and not blk:
        return []
    rows = []
    legacy = legacy_active(now)
    held = legacy if legacy is not None else blk.get('legacy')
    if mode() == 'act' and held is not None:
        rows.append((True, False,
                     f'the operator watchdog still holds the timer (claims.json {int(held)}s '
                     f'ago) — not claimed in ci-cancels.json, not re-run; asf ci stall-watch '
                     f'--apply once it quiets'))
    if boxes and (not blk or now - blk['at'] > tunable('PASS_STALE_S')):
        limit_min = tunable('PASS_STALE_S') / 60
        when = ('never' if not blk else
                f"for {int((now - blk['at']) / 60)} min (limit {limit_min:g})")
        rows.append((True, False, f'no stall-watch pass {when} — {len(boxes)} box(es) of '
                                  f'ci.pool are unwatched'))
    for _, c in sorted(data['claims'].items()):
        if c.get('state') not in BAD_STATES:
            continue
        age_min = int((now - float(c.get('claimed_at') or now)) / 60)
        rows.append((True, False,
                     f"run {c.get('run')} ({c.get('job')} on {c.get('runner')}) is "
                     f"{c['state']} after {c.get('tries', 1)} tries ({age_min} min ago) — it "
                     f"has not been re-run"))
    if rows:
        return rows
    mode_word = blk.get('mode', 'report')
    note = ' (reporting only, nothing is cancelled)' if mode_word == 'report' else ''
    age_min = int((now - float(blk.get('at') or now)) / 60)
    return [(True, True,
             f"mode {mode_word}, last pass {age_min} min ago{note}: {blk.get('stalls', 0)} "
             f"stalled, {blk.get('cancels', 0)} cancelled over {blk.get('boxes', 0)} box(es)")]


def cmd_stall_watch(args, out=print):
    """``asf ci stall-watch``: one pass; ``--apply`` acts per the mode file."""
    product = env.load_product(args.product)
    stalls, cancels = watch(product, apply=getattr(args, 'apply', False), out=out)
    out(f'ci stall: {stalls} stalled, {cancels} cancelled')
    return 0


def register(sub):
    p = sub.add_parser('stall-watch', help="cancel and re-run once a CI job silent past its "
                                           "measured p95 x factor (the heartbeat stall watch)")
    env.add_product_arg(p)
    p.add_argument('--apply', action='store_true',
                   help="act per <ASF home>/state/ci-heartbeat/mode (act|report); without it, "
                        "report only")
    p.set_defaults(run=cmd_stall_watch)
    return p
