"""asf.tick.step_prs — the tick's ``prs`` step: a PR for every finished, pushed worker branch.

A branch qualifies when its session in the ledger has ended ``finished`` (the session's result
was a success) and is not ``harvested``, it is on the product repo's origin with commits past the
trunk, its name starts ``worker/`` or one of the product's ``conventions.branch_prefixes``, and
``gh pr list --head`` shows no open PR for it. At most ``conventions.prs_per_tick`` (default 6)
are opened per tick, oldest session first.

A product whose ``landing`` is ``fast-forward`` (:func:`asf.harvest.harvest.landing`: no merge
queue) opens nothing — the ``harvest`` step lands its branches on the trunk, and a PR on a
branch that then vanishes is noise at best (B-0029: a branch already at the trunk head was
offered to ``gh``, which refused, and the step failed every tick). The step says so in one line
and still runs PR hygiene.

The PR: ``gh pr create -R <slug> --base <main> --head <branch>`` (the command harvest prints for
a product repo), titled ``<id> — <title>`` from the item, its body the card's path (a link when
the record's origin is a hosted repo) and the card's acceptance lines as checkboxes. Then PR
hygiene runs once (``--close``: the stale, unreviewed, conflicting PRs). Every ``gh`` call goes
through :func:`_gh`. A product with no PR host — no ``repo_slug`` and an origin that is not a
hosted repo — gets one line and no ``gh`` call at all.
"""
import json
import os
import re
import subprocess

from asf.workers import lifecycle
from asf.workers import pool as pool_mod

DEFAULT_PRS_PER_TICK = 6
ACCEPT_RE = re.compile(r'^\s*[-*]\s*(?:\[[ xX]\]\s*)?(.+?)\s*$')


def _gh(args):
    """Run ``gh`` with ``args``; ``(rc, stdout, stderr)``."""
    p = subprocess.run(['gh', *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _hygiene(product):
    from asf.harvest import pr_hygiene
    return pr_hygiene.main(['--product', product.name, '--close'])


def _git(repo, args):
    p = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else ''


def prs_per_tick(product):
    v = (product.conventions or {}).get('prs_per_tick')
    return v if isinstance(v, int) and v >= 0 else DEFAULT_PRS_PER_TICK


def branch_prefixes(product):
    return list(product.conventions.all_prefixes())


def remote_heads(repo):
    """The branch names on the product repo's origin."""
    heads = set()
    for line in _git(repo, ['ls-remote', '--heads', 'origin']).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith('refs/heads/'):
            heads.add(parts[1][len('refs/heads/'):])
    return heads


def repo_slug(product):
    """``repo_slug`` from the product yaml, else ``owner/name`` off the repo's origin url; None
    when the origin is no hosted repo (a local path) — there is no PR host to open PRs on."""
    if product.repo_slug:
        return product.repo_slug
    if not product.repo_dir:
        return None
    from asf.init import slug_from_url
    return slug_from_url(_git(product.repo_dir, ['remote', 'get-url', 'origin']).strip())


def has_open_pr(slug, branch):
    rc, out, err = _gh(['pr', 'list', '-R', slug, '--head', branch, '--state', 'open',
                        '--json', 'number'])
    if rc != 0:
        raise RuntimeError(f'gh pr list --head {branch}: {err.strip() or rc}')
    try:
        return bool(json.loads(out or '[]'))
    except json.JSONDecodeError:
        return True  # unreadable: never open a duplicate


def ahead_of_trunk(repo, trunk, branch):
    """True when ``origin/<branch>`` carries commits not on ``origin/<trunk>``. A branch at the
    trunk head (landed by fast-forward, or never committed to) has nothing a PR could hold."""
    n = _git(repo, ['rev-list', '--count', f'origin/{trunk}..origin/{branch}']).strip()
    return n not in ('', '0')


def candidates(product, out=None):
    """``[(branch, session)]``: finished, unharvested sessions whose branch is pushed with
    commits past the trunk and carries a PR prefix. A pushed branch at the trunk head gets one
    ``prs: <branch> at trunk — nothing to open`` line through ``out`` and is left out."""
    prefixes = tuple(branch_prefixes(product))
    repo = product.repo_dir
    heads = remote_heads(repo) if repo else set()
    if repo:
        subprocess.run(['git', '-C', repo, 'fetch', '-q', '--prune', 'origin'],
                       capture_output=True, text=True)
    out_list = []
    for s in pool_mod.load_sessions(product).values():
        branch = s.get('branch') or ''
        if not (lifecycle.finished(s) and not lifecycle.landed(s)
                and branch.startswith(prefixes) and branch in heads):
            continue
        if not ahead_of_trunk(repo, product.main, branch):
            if out:
                out(f'prs: {branch} at trunk — nothing to open')
            continue
        out_list.append((branch, s))
    out_list.sort(key=lambda bs: bs[1].get('started') or '')
    return out_list


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


def pr_create_argv(product, slug, branch, title, body):
    return ['pr', 'create', '-R', slug, '--base', product.main, '--head', branch,
            '--title', title, '--body', body]


def run(ctx, out=print):
    from asf.harvest.harvest import LANDING_FF, landing
    from asf.views import index_reader
    from asf.tick import land_spec
    product = ctx.product
    if product.repo_dir:  # an approved spec off the trunk is adopted before anything is opened
        land_spec.adopt(product, index_reader.load(ctx.record_root())[0], out=out)
    if landing(product) == LANDING_FF:
        out('prs: landing is fast-forward — harvest lands the branches')
        _hygiene(product)
        return 0
    cap = prs_per_tick(product)
    todo = candidates(product, out)
    items, root = {}, None
    if todo:
        root = ctx.record_root()
        items, _generated = index_reader.load(root)
    slug = repo_slug(product)
    if not slug:
        out(f'prs: no PR host (no repo_slug, origin is not a hosted repo) — '
            f'{len(todo)} finished branch(es) left as they are')
        return 0
    opened, failed = 0, []
    for branch, s in todo:
        if opened >= cap:
            out(f'prs: cap {cap} reached — the rest next tick')
            break
        if has_open_pr(slug, branch):
            continue
        item_id = s.get('item') or ''
        title, body = title_and_body(item_id, items.get(item_id) or {}, root, branch)
        rc, stdout, err = _gh(pr_create_argv(product, slug, branch, title, body))
        if rc != 0:
            failed.append(branch)
            why = ((err or stdout).strip().splitlines() or [f'gh exited {rc}'])[0]
            out(f'prs: {branch} not opened — {why}')
            continue
        opened += 1
        out(f'prs: opened {stdout.strip() or branch} — {title}')
    if not opened and not failed:
        out('prs: none to open')
    _hygiene(product)
    if failed:
        raise RuntimeError(f"{len(failed)} PR(s) not opened: {', '.join(failed)}")
    return 0
