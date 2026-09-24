"""asf.tick.step_prs — the tick's ``prs`` step: the lane opens (or adopts) each finished
branch's PR.

Opening a PR is the lane's transition T2 (PUSHED → PR_OPEN, :mod:`asf.harvest.lane`): a finished
branch ahead of the trunk gets its PR through the host (``gh pr create -R <slug> --base <main>
--head <branch>``, titled ``<id> — <title>``, its body the card's path — a link when the record's
origin is a hosted repo — and the card's acceptance lines as checkboxes), or adopts the one
already open on it; at most ``conventions.prs_per_tick`` a pass. The lane runs that pass
in-process before the wave (R2), so this step only runs it when the wave did not (the wave step
off, or ``asf tick --steps prs``). A product whose ``landing`` is ``fast-forward`` opens nothing:
the branch is its own PR. This module keeps the PR's title and body.
"""
import os
import re
import subprocess

DEFAULT_PRS_PER_TICK = 6
ACCEPT_RE = re.compile(r'^\s*[-*]\s*(?:\[[ xX]\]\s*)?(.+?)\s*$')


def _git(repo, args):
    p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ''


def prs_per_tick(product):
    v = (product.conventions or {}).get('prs_per_tick')
    return v if isinstance(v, int) and v >= 0 else DEFAULT_PRS_PER_TICK


def branch_prefixes(product):
    return list(product.conventions.all_prefixes())


def repo_slug(product):
    """``repo_slug`` from the product yaml, else ``owner/name`` off the repo's origin url; None
    when the origin is no hosted repo (:func:`asf.harvest.lane.repo_slug`)."""
    from asf.harvest import lane
    return lane.repo_slug(product)


# ---- title and body ------------------------------------------------------------

def card_relpath(item):
    return f"{item['folder']}/{item['id']}.md" if item.get('folder') and item.get('id') else None


def acceptance(root, relpath):
    """The card's ``## Acceptance`` bullets, their own checkbox stripped."""
    if not root or not relpath:
        return []
    try:
        with open(os.path.join(root, relpath), encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return []
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith('## '):
            inside = line.strip().lower() == '## acceptance'
            continue
        m = ACCEPT_RE.match(line) if inside else None
        if m:
            lines.append(m.group(1))
    return lines


def card_link(root, relpath):
    """``relpath``, as a web link when the record's origin names a hosted ``owner/repo``."""
    url = _git(root, ['remote', 'get-url', 'origin']).strip() if root else ''
    m = re.search(r'(?:^https://|@)([\w.-]+)[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$', url)
    if not m:
        return relpath
    return f'https://{m.group(1)}/{m.group(2)}/blob/HEAD/{relpath}'


def title_and_body(item_id, item, root, branch):
    title = f"{item_id} — {item.get('title') or branch}" if item_id else branch
    rel = card_relpath(item) if item else None
    lines = []
    if rel:
        lines.append(f'Card: [{item_id}]({card_link(root, rel)})')
    else:
        lines.append(f'Card: {item_id or "(no item)"}')
    accept = acceptance(root, rel)
    if accept:
        lines += ['', '## Acceptance'] + [f'- [ ] {a}' for a in accept]
    lines += ['', f'Opened by the tick from `{branch}`.']
    return title, '\n'.join(lines) + '\n'


def run(ctx, out=print):
    """The step: the lane pass, unless the wave already ran it this tick."""
    from asf.harvest import lane
    from asf.tick import step_wave
    product = ctx.product
    if not product.repo_dir:
        out('prs: no repo_dir — nothing to open')
        return 0
    if getattr(ctx, 'lane_passed', False):
        out('prs: the lane pass ran before the wave — PRs opened there')
        return 0
    results = step_wave.lane_pass(ctx, out)
    if lane.landing(product) == lane.LANDING_FF:
        out('prs: landing is fast-forward — the branch is its own PR')
    elif not results:
        out('prs: none to open')
    return 0
