"""asf.views.status — the ``FACTORY STATUS`` table (``asf status``).

Every row is filled from what exists, or says which key would fill it —
``— (not configured: <key>)`` — never a bare ``—``:

* **Stale** — B-0124: the record step's own health, or the index's age against twice the record
  clock's period when that stamp itself has gone quiet; no row when the record is current;
* **Version** — the ``asf`` running (``asf --version``) and asf's newest release tag, with its age;
* **Runners** — the CI provider's runner pool (``ci.runner_org``), busy per class of ``ci.pool``,
  from the same live read and count as the CI queue's ``free`` (:mod:`asf.ci_queue`);
* **Prod** — how far ``main`` is ahead of the last successful ``deploy_sha.workflow`` run, and
  what prod waits on (:func:`asf.harvest.deploy.lines`: a red trunk, a running or failed deploy,
  a green sha waiting in ``manual`` mode), then each managed dev environment's own line;
* **Agents** — the workers' session registry, ``~/.ASF/state/<product>/sessions.jsonl``;
* **Capacity** — the session and CI ceilings the resolver (``asf.capacity.resolve``) hands back;
* **Features in build** — X / N: the Features in build against ``feeder.max_features_in_build``
  and the inputs ``auto`` sized it from (:func:`asf.feeder.rows.build_state`) — "no new Feature
  starts" only while the cap holds a launching row, else "the cap holds no row now";
* **Record** — the record's counts from ``index.json``: open, Active, blocked, and the items no
  closing rule sees (``rule: no-rule``, §2.7 of the closing spec; ``asf check`` names each);
* **Ready to launch** — what ``asf next --json`` would print (the feeder over the record's
  ``index.json``, less the sessions in flight);
* **Decisions** — the undecided cards (D6's one ranking) and the first few to decide;
* **Quota 5h/7d** — each account through the quota source (``worker_pool.quota_command``), the
  cell naming the band when it is not ``free``, and a stopped account's reset (``— resets
  15:20``): the one a session limit named (:mod:`asf.workers.headroom`), else the source's
  ``five_h_resets_at`` when it prints one; a reading older than ``quota_guards.stale_after_min``
  (by the source's ``polled_at``) says ``stale since HH:MM``, and one that was at or over a stop
  when read stays ``stop`` to that window's reset;
* **PAUSED** — the clocks ``asf scheduler pause`` holds unloaded (reason, who, since when);
  no row while none is;
* **Cron** — the scheduler adapter's ``status()`` of this product's loaded jobs, behind
  ``waiting on upgrade to <sha> since <time> (owner <product>)`` while a pending upgrade marker
  parks every tick (:func:`asf.upgrade.held`).
"""
import datetime
import json
import os
import re
import subprocess

from asf import env


def not_configured(key):
    return f"— (not configured: {key})"


_DIGEST_FILE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})-digest\.md$')
_DIGEST_SUMMARY_RE = re.compile(
    r'^(?P<rule>\d+) answered by rule · (?P<adjudicator>\d+) ruled by the adjudicator · '
    r'(?P<spoken>\d+) spoken for · (?P<for_you>\d+) for you(?: · \d+ housekeeping)?$')


def groom_cell(root, product):
    """§2.7: the newest ``groom/<date>-digest.md``'s summary line, reduced to the three counts
    the operator reads at a glance — the fourth (spoken for) is already reflected in the printed
    ``groom`` tick line, not this row."""
    from asf.groom import policy
    if not policy.groom_auto(product):
        return not_configured('approvals.groom')
    groom_dir = os.path.join(root or '', 'groom')
    dates = [m.group(1) for name in (os.listdir(groom_dir) if root and os.path.isdir(groom_dir) else [])
             for m in [_DIGEST_FILE_RE.match(name)] if m]
    if not dates:
        return "no digest yet — `asf groom` has not run"
    date = sorted(dates)[-1]
    with open(os.path.join(groom_dir, f"{date}-digest.md"), encoding='utf-8') as f:
        text = f.read()
    for line in text.splitlines():
        m = _DIGEST_SUMMARY_RE.match(line.strip())
        if m:
            return (f"{date}: {m.group('rule')} by rule, {m.group('adjudicator')} ruled, "
                    f"{m.group('for_you')} for you")
    return f"{date}: (digest unreadable)"


def _sh(cmd, timeout=30):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def _ci(product):
    return product.ci if isinstance(product.ci, dict) else {'provider': product.ci}


def runners_cell(product, source=None):
    """The runner pool, read live once through the CI queue's own source
    (:class:`asf.ci_queue.GitHubSource`, the product's ``gh`` login) and counted per class by
    the same function the queue's ``free`` is (:func:`asf.ci_queue.runners_text`)."""
    ci = _ci(product)
    if str(ci.get('provider')).lower() == 'none':
        return not_configured('ci.provider (none)')
    from asf import ci_pool, ci_queue
    pool = ci_pool.load_pool(product)
    org = ci.get('runner_org')
    if not org and not pool:
        return not_configured('ci.runner_org')
    src = source or ci_queue.GitHubSource(product)
    try:
        runners = src.runners()
    except ci_pool.BackendError:
        runners = []
    if not runners:
        return f"? (no runners readable for {org or product.repo_slug})"
    return ci_queue.runners_text(runners, pool, product=product)


#: The one documented key the Prod row reads: the deploy workflow whose newest success is prod.
#: ``conventions.deploy_workflow`` and ``ci.deploy_workflow`` are read-only aliases of it
#: (:attr:`asf.env.Product.conventions` folds all three into ``deploy_workflow``).
DEPLOY_WORKFLOW_KEY = 'deploy_sha.workflow'


def prod_cell(product):
    workflow = product.conventions.get('deploy_workflow')
    if not workflow:
        return not_configured(DEPLOY_WORKFLOW_KEY)
    if not product.repo_slug or not product.repo_dir:
        return not_configured('repo_slug')
    out_j = _sh(['gh', 'run', 'list', '-R', product.repo_slug, '--workflow', workflow,
                 '--limit', '8', '--json', 'headSha,conclusion,updatedAt'])
    try:
        runs = json.loads(out_j) if out_j else []
    except json.JSONDecodeError:
        runs = []
    prod_sha = next((r.get('headSha', '') for r in runs if r.get('conclusion') == 'success'), '')
    if not prod_sha:
        return f"? (no successful {workflow} run readable)"
    behind = _sh(['git', '-C', product.repo_dir, 'rev-list', '--count',
                  f'{prod_sha}..origin/{product.main}']) or '?'
    from asf.harvest import deploy  # what each environment waits on, or that the tick deploys it
    said = deploy.lines(product) if deploy.applies(product) else []
    prod = next((s for s in said if s.startswith('deploy: ')), None)
    if not prod or prod.startswith(f'deploy: {workflow} runs unreadable'):
        prod = f"{product.main} is {behind} commits ahead of prod `{prod_sha[:9]}`"
    return ' · '.join([prod.removeprefix('deploy: ')]
                      + [s.removeprefix('deploy ') for s in said if not s.startswith('deploy: ')])


def agents_cell(product):
    from asf.views import sessions
    working, finished, dead = sessions.live_groups(product)
    if not working and not finished and not dead and not os.path.exists(_sessions_path(product)):
        return "0 (no session registry yet — nothing launched)"
    return (f"{len(working)} working"
            + (f", {len(finished)} finished (awaiting harvest)" if finished else "")
            + (f", {len(dead)} dead" if dead else ""))


def _sessions_path(product):
    from asf.workers import pool as pool_mod
    return pool_mod.sessions_path(product)


def capacity_cell(cfg, product):
    """§2.5's row: ``sessions <inflight>/<ceiling> (operator total <n>), ci <n> runs in flight
    (batch starts below <ci>)`` (:func:`ci_clause`) —
    the clauses that do not resolve are dropped — or ``not_configured('capacity')`` when neither
    file carries a ``capacity:`` block and no deprecated key is in use."""
    from asf import capacity as capacity_mod
    prod_cap = product._get('capacity')
    cfg_cap = (cfg or {}).get('capacity')
    configured = (isinstance(prod_cap, dict) and prod_cap) or (isinstance(cfg_cap, dict) and cfg_cap) \
        or capacity_mod.deprecations(cfg)
    from asf import ci_queue
    if not configured and ci_queue.mode(product) == 'off':
        return not_configured('capacity')
    r = capacity_mod.resolve(product, cfg)
    inflight = capacity_mod.inflight_sessions(product.name)
    total = capacity_mod.total_sessions(cfg)
    from asf.workers import cloud
    lane = cloud.capacity_clause(cfg, product)
    if lane:  # the cloud lane's runs hold no local seat: each lane its own count
        inflight -= cloud.inflight(product.name)
    parts = [f"sessions {inflight}/{r.sessions}" + (f" (operator total {total})" if total is not None else "")]
    if lane:
        parts.append(lane)
    if r.ci is not None and ci_queue.mode(product) != 'off':
        # a queued product admits its batch by free runner capacity (asf.ci_queue.ceiling_gate):
        # the ceiling only stands in when the runners cannot be read — the queue clause says so
        parts.append(f"ci {capacity_mod.ci_inflight_text(r.ci_inflight)} (batch admitted by "
                     f"free runners; ceiling {r.ci} only when they are unreadable)")
    elif r.ci is not None:
        parts.append(ci_clause(r.ci_inflight, r.ci))
    # the CI start queue: its depth and the head's decision now, computed live by the function
    # `asf ci queue` prints (one runner read, the cached estimate, this row's own in-flight
    # count), in the current mode; the last tick's snapshot, dated, when the host is unreadable
    queued = ci_queue.status_clause(product, inflight=r.ci_inflight, ceiling=r.ci)
    if queued:
        parts.append(queued)
    return ', '.join(parts)


def quarantine_cell(product):
    """``Quarantine``: the CI jobs flake triage quarantined (:mod:`asf.flake`) — each with its
    expiry, the sha it flaked on and its failed step/test — and the re-runs still in triage.
    None (no row) when there are none."""
    from asf import flake
    return flake.status_cell(product)


def ci_clause(inflight, gate):
    """The CI clause of the Capacity row. ``capacity.ci`` is a *start gate*, not a ceiling on
    every run: the tick holds its ``batch`` step (and the CI start queue, :mod:`asf.ci_queue`,
    its batch starts) while that many runs are in flight; PR starts are governed by the queue's
    runner fit alone, and S1, hotfix, trunk and deploy starts are exempt. The count is
    :func:`asf.capacity.ci_runs_in_flight` — every ``ci.workflow`` run not completed (PR, trunk
    and batch alike), the same one the queue's hold names — so an ``x/y`` fraction read as a
    breached cap; the clause names the total and what the gate holds instead."""
    from asf import capacity as capacity_mod
    held = ' — batch waits' if isinstance(inflight, int) and inflight >= gate else ''
    return f"ci {capacity_mod.ci_inflight_text(inflight)} (batch starts below {gate}{held})"


#: The record clock's period when none is configured (``sample/product.yaml``'s own convention):
#: the threshold for "snapshot older than two clock periods" falls back to this.
DEFAULT_RECORD_CLOCK_S = 300


def _record_period_s(product):
    from asf import scheduler
    try:
        clocks = scheduler.clocks(product)
    except scheduler.SchedulerError:
        return DEFAULT_RECORD_CLOCK_S
    return next((c.interval_s for c in clocks if 'record' in (c.steps or []) and c.interval_s),
                DEFAULT_RECORD_CLOCK_S)


def stale_cell(root, product):
    """B-0124: the record step's own health first (:func:`asf.tick.record_health.line`) — the
    one artifact that survives when the push that would carry a ``metrics/ticks`` line can't
    reach origin, which is exactly when a failing record step needs to be readable. When that
    stamp says the last tick landed (or none has run on this host), the fallback is the index's
    own age against twice the record clock's period — the tick may not even be firing. ``None``
    (no row) when neither says the table is stale."""
    from asf.tick import record_health
    from asf.views import index_reader as ix
    line = record_health.line(product)
    if line:
        return line
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        return None
    try:
        _items, generated = ix.load(root)
    except (OSError, ValueError, KeyError):
        return None
    when = ix.parse_ts(generated)
    if when is None:
        return None
    age_s = (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()
    if age_s <= 2 * _record_period_s(product):
        return None
    return f"STALE since {ix.local_stamp(generated, '%H:%M')} — no fresh record in {ix.span(age_s)}"


def record_cell(root):
    """``<n> open · <n> Active · <n> blocked · <n> no rule`` from ``index.json``'s live items —
    ``no rule`` is an ``evidence:`` list whose last entry is ``rule: no-rule``, the residue."""
    from asf.views import index_reader as ix
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        return not_configured('backlog_dir (no index.json)')
    try:
        items, _generated = ix.load(root)
        live = [v for v in items.values() if isinstance(v, dict)]
    except (OSError, ValueError, KeyError, AttributeError):
        return not_configured('backlog_dir (index.json unreadable)')
    opened = [v for v in live if v.get('state', 'New') != 'Closed']
    active = sum(1 for v in opened if v.get('state') == 'Active')
    blocked = sum(1 for v in opened if v.get('blocked') is True)
    no_rule = sum(1 for v in live if _residue(v))
    return f"{len(opened)} open · {active} Active · {blocked} blocked · {no_rule} no rule"


def _residue(item):
    evidence = item.get('evidence')
    return isinstance(evidence, list) and bool(evidence) and evidence[-1] == 'rule: no-rule'


def ready_cell(root, product):
    """``asf next --json``'s rows: how many would launch, and the first of them."""
    from asf.feeder import rows as feeder_rows
    from asf.tick.step_wave import capacity, inflight, plan_inputs
    from asf.views import index_reader as ix
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        return not_configured('backlog_dir (no index.json)')
    items, _generated = ix.load(root)
    rows = feeder_rows.plan_rows(items, product, inflight(product), capacity(product),
                                 **plan_inputs(product, root))
    launching = [r for r in rows if r.launches]
    if not launching:
        return f"0 ({len(rows)} row(s) waiting)" if rows else "0"
    first = launching[0]
    failing = sum(1 for r in launching if feeder_rows.FAILING_TO_SPAWN in r.action)
    return (f"{len(launching)} — first: {first.kind} {first.item_id}"
            + (f"; {failing} {feeder_rows.FAILING_TO_SPAWN.lower()}" if failing else ''))


def build_cell(root, product):
    """``3 / 6 (auto: sessions 3, quota-stopped 2/5, CI free 1)`` — what ``asf next`` says under
    its table (:func:`asf.feeder.rows.build_load`)."""
    from asf.feeder import rows as feeder_rows
    from asf.tick.step_wave import capacity, inflight, plan_inputs
    from asf.views import index_reader as ix
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        return not_configured('backlog_dir (no index.json)')
    items, _generated = ix.load(root)
    inputs = plan_inputs(product, root)
    x, n, why, binds = feeder_rows.build_state(
        items, product, capacity(product), inflight(product), inputs.get('occupancy'),
        landed_shas=inputs.get('landed_shas'), bandwidth=inputs.get('bandwidth'),
        attempts=inputs.get('attempts'), groom_state=inputs.get('groom_state'),
        held=inputs.get('held'), gate=inputs.get('gate'), adjudicated=inputs.get('adjudicated'),
        unverified_landed=inputs.get('unverified_landed'),
        unverified_on_trunk=inputs.get('unverified_on_trunk'))
    return f"{x} / {n} ({why})" + feeder_rows.build_binds_note(x, n, binds)


def decisions_cell(root, product):
    """The decision debt: how many open cards wait for ``decided: true``, and the ids to spend it
    on first — the same ranking (``undecided_rows``) and the same ``busy`` the wave plans with."""
    from asf.feeder import rows as feeder_rows
    from asf.tick.step_wave import inflight, plan_inputs
    from asf.views import index_reader as ix
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        return not_configured('backlog_dir (no index.json)')
    items, _generated = ix.load(root)
    held = feeder_rows.inflight_ids(inflight(product)) | {
        i for i in feeder_rows.occupied(plan_inputs(product, root)['occupancy'])
        if feeder_rows.is_open(items.get(i) or {})}
    rows = feeder_rows.undecided_rows(items, product, held, limit=0)
    if not rows:
        return "0"
    shown = ', '.join(r.item_id for r in rows[:feeder_rows.decision_rows(product)])
    return f"{len(rows)} undecided — next: {shown}" if shown else f"{len(rows)} undecided"


def parked_cell(product):
    """What a park holds, each with its scope (a pending correction carrying ``parked``: the
    relaunch cap, an empty or blocked end, a security hold, an item park by hand — the item; a
    branch or job park by hand — that branch or job alone) — each waits on a person or a state
    change, not a session. Silent (None) when nothing is parked."""
    from asf.workers import lifecycle
    from asf.workers import pool as pool_mod
    path = pool_mod.sessions_path(product)
    parked = [(i, 'item' if c.get('scope') == 'item' else f"{c.get('kind')} on job {c.get('job')}",
               c.get('reason') or c.get('kind') or '')
              for i, c in sorted(lifecycle.corrections(path).items()) if c.get('parked')]
    parked += [(p['item'], f"{p['scope']} {p.get('branch') if p['scope'] == 'branch' else p.get('on_job')}",
                p.get('reason') or '')
               for p in lifecycle.parks(path) if p.get('scope') in ('branch', 'job')]
    if not parked:
        return None
    shown = '; '.join(f"{i} [{scope}]: {why[:120]}" for i, scope, why in parked[:3])
    more = f' (+{len(parked) - 3} more)' if len(parked) > 3 else ''
    return f"{len(parked)} — {shown}{more} · `asf unpark <item|branch|job>` releases one"


def quota_cell(cfg):
    from asf.workers import pool as pool_mod
    from asf.workers import quota as quota_mod
    if not ((cfg.get('worker_pool') or {}).get('quota_command')):
        return not_configured('worker_pool.quota_command')
    accounts = pool_mod.accounts_from_config(cfg)
    if not accounts:
        return not_configured('worker_pool.accounts')
    from asf.workers import headroom
    source = quota_mod.source_from_config(cfg)
    guards = quota_mod.guards_from_config(cfg)
    limits = headroom.active_limits()
    parts = []
    for a in accounts:
        try:
            u = source.read(a)
        except Exception:  # noqa: BLE001 — an unreadable account is shown, not raised
            u = None
        uu = u or {}
        five, seven = uu.get('five_h_pct'), uu.get('seven_d_pct')
        state, _why = quota_mod.band(u, guards)
        until = (limits.get(a.name) or {}).get('until')
        if until:
            state = quota_mod.STOP  # a session limit stopped it, whatever the reading says
        if not until:
            until = quota_mod.stale_stop_until(u, guards)  # a stale reading's stop holds to its reset
        if not until and five is not None and float(five) >= guards['stop']['five_h']:
            until = uu.get('five_h_resets_at')  # the source's own reset, for a full 5h window
        part = (f"{a.name} {five if five is not None else '?'}%/"
                f"{seven if seven is not None else '?'}%")
        if state != quota_mod.FREE:
            part += f" {state}"
        since = quota_mod.stale_since(u, guards)
        if since is not None:
            part += f" {quota_mod.stale_label(since)}"
        if state == quota_mod.STOP and headroom.parse_ts(until):
            part += f" — resets {headroom.reset_label(until)}"
        parts.append(part)
    return quota_lock_prefix(cfg) + ', '.join(parts)


def quota_lock_prefix(cfg):
    """``cux lock wedged N min — held by pid X (child of Y); `` when the account manager's usage
    lock is wedged (:mod:`asf.workers.cuxlock`) — the cause behind a row of stale readings."""
    from asf.workers import cuxlock
    try:
        w = cuxlock.probe_wedge()
    except Exception:  # noqa: BLE001 — a probe that fails says nothing
        return ''
    return f'{w.label}; ' if w is not None and w.wedged else ''


def paused_cell(cfg, product):
    """``PAUSED``: each clock ``asf scheduler pause`` holds unloaded, with its reason, who and
    when — no row while none is paused."""
    from asf import scheduler
    pauses = scheduler.read_pauses(product.name)
    if not pauses:
        return None
    return '; '.join(f'{scheduler.label_for(product.name, clock, cfg)} '
                     f'{scheduler.pause_text(record)}' for clock, record in sorted(pauses.items()))


def cron_cell(cfg, product):
    from asf import scheduler, upgrade
    # a loaded, on-time clock says nothing about whether ticks run: while an upgrade marker is
    # pending this product's ticks exit at their start (B-0141), so the row names the wait first.
    # The marker's owner keeps ticking — it installs the upgrade — so its row claims no wait.
    marker = upgrade.held(product.name)
    prefix = f'{upgrade.held_label(marker)}; ' if marker is not None else ''
    kind = scheduler.kind(cfg)
    if kind != 'launchd':
        return prefix + not_configured(f'scheduler.kind ({kind} has no status adapter)')
    mine = [j for j in scheduler.loaded_jobs(cfg=cfg)
            if f'.{product.name}.' in j.get('label', '')]
    loaded_labels = {j['label'] for j in mine}
    try:
        declared = scheduler.clocks(product)
    except scheduler.SchedulerError:
        declared = []
    # a clock the product declares that launchd does not currently hold: loaded_jobs() never
    # mentions it, so without this it just vanishes from the row instead of naming itself (B-0136)
    missing = sorted(scheduler.label_for(product.name, c.name, cfg) for c in declared
                     if scheduler.label_for(product.name, c.name, cfg) not in loaded_labels)
    if not mine and not missing:
        return (prefix + f"no job loaded for {product.name} — "
                f"`asf scheduler install --product {product.name}`")
    parts = []
    for job in sorted(mine, key=lambda j: j['label']):
        info = scheduler.status(job['label'])
        exit_text = 'never exited' if info.get('never_exited') else f"exit {info.get('last_exit')}"
        parts.append(f"{job['label']} {info.get('state') or '?'} ({exit_text})")
    parts.extend(f'clock {label} paused' if scheduler.pause_record(label, cfg) is not None
                 else f'clock {label} not loaded' for label in missing)
    return prefix + '; '.join(parts)


def _age(seconds):
    seconds = max(int(seconds), 0)
    if seconds >= 86400:
        return f"{seconds // 86400}d ago"
    if seconds >= 3600:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 60}m ago"


def version_cell(now=None):
    """``running <asf --version>`` · ``latest release <newest v* tag> (<age>)``."""
    from asf.cli import latest_release, version_string
    latest = latest_release()
    if latest is None:
        tail = "—"
    else:
        tag, when = latest
        now = now or datetime.datetime.now(datetime.timezone.utc)
        tail = f"{tag} ({_age((now - when).total_seconds())})" if when else tag
    return f"running {version_string()} · latest release {tail}"


def gate_cell(product):
    """``gate too slow: …`` after two gate timeouts in a row (§12), else None (no row)."""
    from asf.harvest import lane
    return lane.gate_slow_line(product)


def lane_push_cell(product):
    """``ref pushes failing: …`` when the last lane pass could not push an archive or a branch
    delete, else None (no row)."""
    from asf.harvest import lane
    return lane.ref_push_line(product)


def bypass_cell(product):
    """``RED N commit(s) … bypassed the merge queue`` when a trunk commit did not come through
    it (:mod:`asf.trunk_watch`, off its state file), else None (no row)."""
    from asf import trunk_watch
    return trunk_watch.status_cell(product)


def trunk_stall_cell(product):
    """``RED <trunk> has not moved for Xh … while N landing(s) wait`` past
    ``conventions.ci.trunk_stall_hours`` (:func:`asf.trunk_watch.stall`, off its state files),
    else None (no row)."""
    from asf import trunk_watch
    return trunk_watch.stall_cell(product)


def trunk_red_cell(product):
    """``RED TRUNK RED: <check> (seen on #a, #b) — …`` while a required check is red on the
    trunk itself (:func:`asf.trunk_red.status_cell`, off its state file), else None (no row)."""
    from asf import trunk_red
    return trunk_red.status_cell(product)


def land_red_cell(product):
    """``RED #N <why>`` for each ``asf land`` request the queue marked red (a conflict with the
    trunk, red checks) — it waits for a new head, so it must show; None (no row) when none."""
    from asf import merge_queue
    if not getattr(product.conventions, 'merge_queue', lambda: False)():
        return None
    got = merge_queue.red_requests(env.state_dir(product),
                                    merge_queue.host_head(product.repo_slug)
                                    if product.repo_slug else None)
    if not got:
        return None
    return 'RED ' + '; '.join(f'#{n} {why}' for n, why in got)


def merge_cell(product):
    """``auto``, ``queue`` or ``manual`` — ``conventions.merge``: whether the lane merges a green,
    reviewed PR itself (directly, or through its merge queue) or the operator clicks merge."""
    conv = getattr(product, 'conventions', None)
    if conv is None:
        return 'manual'
    if getattr(conv, 'merge_queue', lambda: False)():
        return 'queue'
    return 'auto' if conv.merge_auto() else 'manual'


def value_cell(root, product):
    """``Value``: Features on prod and landed over 7 days, median lead time, $ per feature all-in,
    repair sessions per feature (:mod:`asf.views.scorecard`; the full table is ``asf scorecard``)."""
    from asf.views.scorecard import value_cell as cell
    return cell(root, product)


def ab_pairs_cell(root, product):
    """``A/B pairs``: only when the two Features of a lane-experiment pair touch the same files
    (:func:`asf.scorecard.pairs.status_cell`) — the overlap spoils the comparison."""
    from asf.scorecard import pairs
    return pairs.status_cell(root, product)


def release_cell(root, product):
    """``Release``: the release-readiness verdict (:mod:`asf.release`; the full table is
    ``asf release-readiness``) — only for the product whose repo is the factory's own source, or
    one that sets a ``release:`` block."""
    from asf.drift import is_factory_source
    if not (getattr(product, 'release', None) or (product.repo_dir and is_factory_source(product.repo_dir))):
        return None
    from asf.release import cell
    return cell(root, product)


def render(root, product, cfg=None):
    cfg = env.load_config() if cfg is None else cfg
    now = datetime.datetime.now().strftime('%H:%M')
    out = [f"**FACTORY STATUS {now}**", ""]
    out.append("| Metric | Now |")
    out.append("|---|---|")
    for name, cell in (('Stale', lambda: stale_cell(root, product)),
                       ('Version', version_cell),
                       ('Runners', lambda: runners_cell(product)),
                       ('Prod', lambda: prod_cell(product)),
                       ('Agents', lambda: agents_cell(product)),
                       ('Merge', lambda: merge_cell(product)),
                       ('Capacity', lambda: capacity_cell(cfg, product)),
                       ('Quarantine', lambda: quarantine_cell(product)),
                       ('Features in build', lambda: build_cell(root, product)),
                       ('Value', lambda: value_cell(root, product)),
                       ('A/B pairs', lambda: ab_pairs_cell(root, product)),
                       ('Release', lambda: release_cell(root, product)),
                       ('Record', lambda: record_cell(root)),
                       ('Ready to launch', lambda: ready_cell(root, product)),
                       ('Decisions', lambda: decisions_cell(root, product)),
                       ('Parked', lambda: parked_cell(product)),
                       ('Quota 5h/7d', lambda: quota_cell(cfg)),
                       ('PAUSED', lambda: paused_cell(cfg, product)),
                       ('Cron', lambda: cron_cell(cfg, product)),
                       ('Groom', lambda: groom_cell(root, product)),
                       ('Gate', lambda: gate_cell(product)),
                       ('Lane pushes', lambda: lane_push_cell(product)),
                       ('Queue bypass', lambda: bypass_cell(product)),
                       ('Land requests', lambda: land_red_cell(product)),
                       ('Trunk stall', lambda: trunk_stall_cell(product)),
                       ('Trunk red', lambda: trunk_red_cell(product))):
        try:
            text = cell()
        except Exception as e:  # noqa: BLE001 — one unreadable row never loses the table
            text = f"? ({type(e).__name__}: {e})"
        if text is None:  # a row that only speaks up when something is wrong
            continue
        out.append(f"| {name} | {text} |")
    return "\n".join(out) + "\n"


def cmd_status(args, root):
    product = env.load_product(getattr(args, 'product', None))
    print(render(root, product), end='')
    return 0
