"""asf.views.status — the ``FACTORY STATUS`` table (``asf status``).

Every row is filled from what exists, or says which key would fill it —
``— (not configured: <key>)`` — never a bare ``—``:

* **Runners** — the CI provider's runner pool (``ci.runner_org``, read with ``gh``);
* **Prod** — how far ``main`` is ahead of the last successful ``ci.deploy_workflow`` run;
* **Agents** — the workers' session registry, ``~/.ASF/state/<product>/sessions.jsonl``;
* **Capacity** — the session and CI ceilings the resolver (``asf.capacity.resolve``) hands back;
* **Ready to launch** — what ``asf next --json`` would print (the feeder over the record's
  ``index.json``, less the sessions in flight);
* **Quota 5h/7d** — each account through the quota source (``worker_pool.quota_command``), the
  cell naming the band when it is not ``free``;
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
    r'(?P<spoken>\d+) spoken for · (?P<for_you>\d+) for you$')


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


def prod_cell(product):
    workflow = _ci(product).get('deploy_workflow')
    if not workflow:
        return not_configured('ci.deploy_workflow')
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
    return f"{product.main} is {behind} commits ahead of prod `{prod_sha[:9]}`"


def agents_cell(product):
    from asf.views import sessions
    working, dead = sessions.live_rows(product)
    if not working and not dead and not os.path.exists(_sessions_path(product)):
        return "0 (no session registry yet — nothing launched)"
    return f"{len(working)} working" + (f", {len(dead)} dead" if dead else "")


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
    parts = [f"sessions {inflight}/{r.sessions}" + (f" (operator total {total})" if total is not None else "")]
    if r.ci is not None:
        parts.append(f"ci {r.ci_inflight if r.ci_inflight is not None else '?'}/{r.ci}")
    return ', '.join(parts)


def ready_cell(root, product):
    """``asf next --json``'s rows: how many would launch, and the first of them."""
    from asf.feeder import rows as feeder_rows
    from asf.tick.step_wave import attempts, capacity, inflight
    from asf.views import index_reader as ix
    if not root or not os.path.exists(os.path.join(root, 'index.json')):
        return not_configured('backlog_dir (no index.json)')
    items, _generated = ix.load(root)
    rows = feeder_rows.plan_rows(items, product, inflight(product), capacity(product), attempts=attempts(product))
    launching = [r for r in rows if r.launches]
    if not launching:
        return f"0 ({len(rows)} row(s) waiting)" if rows else "0"
    first = launching[0]
    return f"{len(launching)} — first: {first.kind} {first.item_id}"


def quota_cell(cfg):
    from asf.workers import pool as pool_mod
    from asf.workers import quota as quota_mod
    if not ((cfg.get('worker_pool') or {}).get('quota_command')):
        return not_configured('worker_pool.quota_command')
    accounts = pool_mod.accounts_from_config(cfg)
    if not accounts:
        return not_configured('worker_pool.accounts')
    source = quota_mod.source_from_config(cfg)
    guards = quota_mod.guards_from_config(cfg)
    parts = []
    for a in accounts:
        try:
            u = source.read(a)
        except Exception:  # noqa: BLE001 — an unreadable account is shown, not raised
            u = None
        uu = u or {}
        five, seven = uu.get('five_h_pct'), uu.get('seven_d_pct')
        state, _why = quota_mod.band(u, guards)
        part = (f"{a.name} {five if five is not None else '?'}%/"
                f"{seven if seven is not None else '?'}%")
        if state != quota_mod.FREE:
            part += f" {state}"
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


def render(root, product, cfg=None):
    cfg = env.load_config() if cfg is None else cfg
    now = datetime.datetime.now().strftime('%H:%M')
    out = [f"**FACTORY STATUS {now}**", ""]
    out.append("| Metric | Now |")
    out.append("|---|---|")
    for name, cell in (('Runners', lambda: runners_cell(product)),
                       ('Prod', lambda: prod_cell(product)),
                       ('Agents', lambda: agents_cell(product)),
                       ('Capacity', lambda: capacity_cell(cfg, product)),
                       ('Ready to launch', lambda: ready_cell(root, product)),
                       ('Quota 5h/7d', lambda: quota_cell(cfg)),
                       ('Cron', lambda: cron_cell(cfg, product)),
                       ('Groom', lambda: groom_cell(root, product))):
        try:
            text = cell()
        except Exception as e:  # noqa: BLE001 — one unreadable row never loses the table
            text = f"? ({type(e).__name__}: {e})"
        out.append(f"| {name} | {text} |")
    return "\n".join(out) + "\n"


def cmd_status(args, root):
    product = env.load_product(getattr(args, 'product', None))
    print(render(root, product), end='')
    return 0
