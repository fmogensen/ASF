"""asf.views.status — the ``FACTORY STATUS`` table (``asf status``).

Every row is filled from what exists, or says which key would fill it —
``— (not configured: <key>)`` — never a bare ``—``:

* **Version** — the ``asf`` running (``asf --version``) and asf's newest release tag, with its age;
* **Runners** — the CI provider's runner pool (``ci.runner_org``, read with ``gh``);
* **Prod** — how far ``main`` is ahead of the last successful ``deploy_sha.workflow`` run, and
  what prod waits on (:func:`asf.harvest.deploy.lines`: a red trunk, a running or failed deploy,
  a green sha waiting in ``manual`` mode), then each managed dev environment's own line;
* **Agents** — the workers' session registry, ``~/.ASF/state/<product>/sessions.jsonl``;
* **Capacity** — the session and CI ceilings the resolver (``asf.capacity.resolve``) hands back;
* **Record** — the record's counts from ``index.json``: open, Active, blocked, and the items no
  closing rule sees (``rule: no-rule``, §2.7 of the closing spec; ``asf check`` names each);
* **Ready to launch** — what ``asf next --json`` would print (the feeder over the record's
  ``index.json``, less the sessions in flight);
* **Decisions** — the undecided cards (D6's one ranking) and the first few to decide;
* **Quota 5h/7d** — each account through the quota source (``worker_pool.quota_command``), the
  cell naming the band when it is not ``free``, and a stopped account's reset (``— resets
  15:20``): the one a session limit named (:mod:`asf.workers.headroom`), else the source's
  ``five_h_resets_at`` when it prints one;
* **Cron** — the scheduler adapter's ``status()`` of this product's loaded jobs.
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


def runners_cell(product):
    ci = _ci(product)
    if str(ci.get('provider')).lower() == 'none':
        return not_configured('ci.provider (none)')
    org = ci.get('runner_org')
    if not org:
        return not_configured('ci.runner_org')
    out = _sh(['gh', 'api', f'/orgs/{org}/actions/runners', '--paginate', '-q',
               '.runners[] | "\\(.status) \\(.busy)"'])
    lines = [ln for ln in out.splitlines() if ln]
    if not lines:
        return f"? (no runners readable for {org})"
    on = sum(1 for ln in lines if ln.startswith('online'))
    busy = sum(1 for ln in lines if ln.startswith('online true'))
    off = len(lines) - on
    return f"{on} online, {busy} busy, {on - busy} idle" + (f", {off} offline" if off else "")


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
    """§2.5's row: ``sessions <inflight>/<ceiling> (operator total <n>), ci <inflight>/<ci>`` —
    the clauses that do not resolve are dropped — or ``not_configured('capacity')`` when neither
    file carries a ``capacity:`` block and no deprecated key is in use."""
    from asf import capacity as capacity_mod
    prod_cap = product._get('capacity')
    cfg_cap = (cfg or {}).get('capacity')
    configured = (isinstance(prod_cap, dict) and prod_cap) or (isinstance(cfg_cap, dict) and cfg_cap) \
        or capacity_mod.deprecations(cfg)
    if not configured:
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
    if r.ci is not None:
        parts.append(f"ci {r.ci_inflight if r.ci_inflight is not None else '?'}/{r.ci}")
    return ', '.join(parts)


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
    return f"{len(launching)} — first: {first.kind} {first.item_id}"


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
        if not until and five is not None and float(five) >= guards['stop']['five_h']:
            until = uu.get('five_h_resets_at')  # the source's own reset, for a full 5h window
        part = (f"{a.name} {five if five is not None else '?'}%/"
                f"{seven if seven is not None else '?'}%")
        if state != quota_mod.FREE:
            part += f" {state}"
        if state == quota_mod.STOP and headroom.parse_ts(until):
            part += f" — resets {headroom.reset_label(until)}"
        parts.append(part)
    return ', '.join(parts)


def cron_cell(cfg, product):
    from asf import scheduler
    kind = scheduler.kind(cfg)
    if kind != 'launchd':
        return not_configured(f'scheduler.kind ({kind} has no status adapter)')
    mine = [j for j in scheduler.loaded_jobs(cfg=cfg)
            if f'.{product.name}.' in j.get('label', '')]
    if not mine:
        return f"no job loaded for {product.name} — `asf scheduler install --product {product.name}`"
    parts = []
    for job in sorted(mine, key=lambda j: j['label']):
        info = scheduler.status(job['label'])
        exit_text = 'never exited' if info.get('never_exited') else f"exit {info.get('last_exit')}"
        parts.append(f"{job['label']} {info.get('state') or '?'} ({exit_text})")
    return '; '.join(parts)


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


def merge_cell(product):
    """``auto`` or ``manual`` — ``conventions.merge``: whether the lane merges a green, reviewed
    PR itself or the operator clicks merge."""
    conv = getattr(product, 'conventions', None)
    return 'auto' if conv is not None and conv.merge_auto() else 'manual'


def value_cell(root, product):
    """``Value``: Features on prod and landed over 7 days, median lead time, $ per feature all-in,
    repair sessions per feature (:mod:`asf.views.scorecard`; the full table is ``asf scorecard``)."""
    from asf.views.scorecard import value_cell as cell
    return cell(root, product)


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
    for name, cell in (('Version', version_cell),
                       ('Runners', lambda: runners_cell(product)),
                       ('Prod', lambda: prod_cell(product)),
                       ('Agents', lambda: agents_cell(product)),
                       ('Merge', lambda: merge_cell(product)),
                       ('Capacity', lambda: capacity_cell(cfg, product)),
                       ('Value', lambda: value_cell(root, product)),
                       ('Release', lambda: release_cell(root, product)),
                       ('Record', lambda: record_cell(root)),
                       ('Ready to launch', lambda: ready_cell(root, product)),
                       ('Decisions', lambda: decisions_cell(root, product)),
                       ('Quota 5h/7d', lambda: quota_cell(cfg)),
                       ('Cron', lambda: cron_cell(cfg, product)),
                       ('Groom', lambda: groom_cell(root, product)),
                       ('Gate', lambda: gate_cell(product)),
                       ('Lane pushes', lambda: lane_push_cell(product))):
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
