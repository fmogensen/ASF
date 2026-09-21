"""asf.views.status — the ``FACTORY STATUS`` table (``asf status``).

A reduced port. The operator's original script's rows split into two kinds: product/CI state
(Runners, Batches in CI, Merged today) — derivable from ``Product.ci``/``repo_slug`` — and
operator-fleet state (Agents, Ready to launch, Quota, Cron) that reads the launcher's own
ledgers (the in-flight job ledger, the next-work cache, the quota probe, the cron log). Only the
first kind is ported here; the second has no product-config home
yet — see ``asf shadow-diff`` for the one-line note.
"""
import datetime
import json
import subprocess


def _sh(cmd, timeout=30):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip() if out.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


def _runner_counts(product):
    org = (product.ci or {}).get('runner_org')
    if not org:
        return None
    out = _sh(['gh', 'api', f'/orgs/{org}/actions/runners', '--paginate', '-q',
               '.runners[] | "\\(.status) \\(.busy)"'])
    lines = [l for l in out.splitlines() if l]
    on = sum(1 for l in lines if l.startswith('online'))
    busy = sum(1 for l in lines if l.startswith('online true'))
    off = len(lines) - on
    return on, busy, off


def render(root, product):
    now = datetime.datetime.now().strftime('%H:%M')
    out = [f"**FACTORY STATUS {now}**", ""]
    out.append("| Metric | Now |")
    out.append("|---|---|")
    counts = _runner_counts(product)
    if counts:
        on, busy, off = counts
        out.append(f"| Runners | {on} online, {busy} busy, {on - busy} idle"
                    + (f", {off} offline" if off else "") + " |")
    else:
        out.append("| Runners | — (no `ci.runner_org` in product config) |")

    repo_dir = product.repo_dir
    workflow = (product.ci or {}).get('deploy_workflow')
    prod_sha = ''
    if repo_dir and workflow and product.repo_slug:
        out_j = _sh(['gh', 'run', 'list', '-R', product.repo_slug, '--workflow', workflow,
                     '--limit', '8', '--json', 'headSha,conclusion,updatedAt'])
        try:
            runs = json.loads(out_j) if out_j else []
        except json.JSONDecodeError:
            runs = []
        for r in runs:
            if r.get('conclusion') == 'success':
                prod_sha = r.get('headSha', '')
                break
    behind = '?'
    if prod_sha and repo_dir:
        behind = _sh(['git', '-C', repo_dir, 'rev-list', '--count', f'{prod_sha}..origin/main']) or '?'
    out.append(f"| Merged today | — (needs the runner log's per-day boundary; not product config) "
               f"· main is {behind} commits ahead of prod `{prod_sha[:9] if prod_sha else '?'}` |")
    out.append("| Agents | — (operator launch ledger, no product-config home yet) |")
    out.append("| Ready to launch | — (see `asf next-work`) |")
    out.append("| Quota 5h/7d | — (operator-wide account state, no product-config home yet) |")
    out.append("| Cron | — (operator scheduler state, no product-config home yet) |")
    return "\n".join(out) + "\n"


def cmd_status(args, root):
    from asf import env
    product = env.load_product(getattr(args, 'product', None))
    print(render(root, product), end='')
    return 0
