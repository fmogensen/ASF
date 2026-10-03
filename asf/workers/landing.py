"""asf.workers.landing — when a trunk commit is landing evidence for one item.

A session's report may name any sha; that the sha is an ancestor of ``origin/<main>`` proves only
that it is on the trunk, not that the item's work is. A product's T-0042 (2026-09-30): its branch
merged ``origin/main`` in to resolve a conflict, the correct session named the trunk head it
merged (another item's merge-queue commit), and the relaunch cap parked the row as "the work it
names is on origin/main (verified): close T-0042" while its own 14 commits sat on an OPEN PR with
red checks. A park reason carrying that text is closed automatically (:mod:`asf.workers.trunkclose`).

So a trunk commit counts for item X only when it is **attributable** to X
(:func:`attributable`): its subject names X by the evidence naming rules
(:func:`asf.evidence.evidence.naming_ids`, a document lane's commit aside) or a trailer names X,
or it is the merge of one of X's own PRs, or its diff covers X's ``writes:`` footprint. And an
item whose work still sits unmerged — a branch of its runs carrying commits the trunk does not,
or an open PR naming it with such commits — is never closed as landed (:func:`open_work`).
"""
import fnmatch
import json
import re
import subprocess

from asf.evidence import evidence as ev_mod
from asf.workers import lifecycle


def _git(repo, args):
    p = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None


def on_trunk(repo, main, sha):
    """True when ``sha`` is an ancestor of ``origin/<main>``."""
    return bool(repo and sha) and subprocess.run(
        ['git', 'merge-base', '--is-ancestor', sha, f'origin/{main}'], cwd=repo,
        capture_output=True).returncode == 0


def names(repo, main, sha, item, prs=()):
    """True when commit ``sha``'s message names ``item``: its subject by the evidence naming
    rules (a spec/plan/review commit names its item by convention, never its landing), a trailer
    value, or the merge of one of ``prs`` (``… (#902)``, ``Merge pull request #902``,
    ``merge-queue: #902 (…)``)."""
    item = (item or '').upper()
    body = _git(repo, ['log', '-1', '--format=%B', sha]) or ''
    subject = body.split('\n', 1)[0].strip()
    if not item or not subject:
        return False
    if not ev_mod.lands_nothing(subject) and item in ev_mod.naming_ids(subject, main):
        return True
    trailers = _git(repo, ['log', '-1', '--format=%(trailers:only,unfold)', sha]) or ''
    for line in trailers.splitlines():
        _key, _, value = line.partition(':')
        if item in ev_mod.id_tokens(value):
            return True
    for pr in prs or ():
        n = str(pr).lstrip('#')
        if n.isdigit() and re.search(
                rf'(\(#{n}\)\s*$|^Merge pull request #{n}\b|^merge-queue: #{n}\b)', subject):
            return True
    return False


def changed(repo, sha):
    """The paths commit ``sha`` changed against its first parent (a root commit: all of it)."""
    out = _git(repo, ['diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-m',
                      '--first-parent', sha])
    return [p for p in (out or '').splitlines() if p]


def covers(repo, sha, writes):
    """True when ``writes`` is non-empty and every entry of it (a path or a glob) matches a path
    commit ``sha`` changed."""
    writes = [w for w in (writes or ()) if w]
    if not writes:
        return False
    paths = changed(repo, sha)
    return bool(paths) and all(
        any(p == w or fnmatch.fnmatch(p, w) or p.startswith(w.rstrip('/') + '/') for p in paths)
        for w in writes)


def attributable(repo, main, sha, item, writes=(), prs=()):
    """True when ``sha`` is on ``origin/<main>`` AND is ``item``'s: named by it (:func:`names`) or
    covering its ``writes:`` (:func:`covers`). A trunk commit that is merely an ancestor — the
    trunk head a branch merged in, another item's merge-queue commit — never counts."""
    if not on_trunk(repo, main, sha):
        return False
    return names(repo, main, sha, item, prs) or covers(repo, sha, writes)


def unlanded(repo, main, branch):
    """True when ``origin/<branch>`` carries a commit ``origin/<main>`` does not. A branch gone
    from origin, or none, holds nothing."""
    if not branch or branch == main:
        return False
    if _git(repo, ['rev-parse', '--verify', '-q', f'refs/remotes/origin/{branch}']) is None:
        return False
    n = _git(repo, ['rev-list', '--count', f'origin/{main}..origin/{branch}'])
    return n is None or not n.isdigit() or int(n) > 0


def run_branches(path, item):
    """Every branch the item's runs worked on."""
    runs = lifecycle.item_runs(path, item) if (path and item) else []
    return list(dict.fromkeys(r.get('branch') for r in runs if r.get('branch')))


def run_prs(path, item):
    """Every PR number the item's runs' lanes recorded."""
    out = []
    for r in (lifecycle.item_runs(path, item) if (path and item) else []):
        lane = r.get('lane') if isinstance(r.get('lane'), dict) else {}
        if lane.get('pr'):
            out.append(str(lane['pr']))
    return list(dict.fromkeys(out))


#: The open PRs ``gh`` is asked for: a list that comes back this long may be cut short.
PR_LIMIT = 300


def open_prs(repo, item):
    """``[head branch]`` of every open PR that names ``item`` (title or head branch), or
    ``None`` — *unknown* — when ``gh`` cannot answer (no remote, offline, a failed call, an
    unreadable answer) or answers with :data:`PR_LIMIT` PRs (the list may be cut short). A
    rate-limit answer raises :class:`asf.gh_limit.RateLimited`: no GitHub decision this pass."""
    from asf import gh_limit
    args = ['pr', 'list', '--state', 'open', '--limit', str(PR_LIMIT), '--json',
            'number,title,headRefName']
    gh_limit.guard(args)
    try:
        p = subprocess.run(['gh', *args], cwd=repo, capture_output=True, text=True, timeout=60)
    except Exception:  # noqa: BLE001 — gh not there or hung: unknown, never "no open PR"
        return None
    gh_limit.inspect_proc(args, p)
    if p.returncode != 0:
        return None
    try:
        prs = json.loads(p.stdout) if p.stdout.strip() else []
    except ValueError:
        return None
    if not isinstance(prs, list) or len(prs) >= PR_LIMIT:
        return None
    item = (item or '').upper()
    return [pr.get('headRefName') for pr in prs
            if item in ev_mod.naming_ids(pr.get('title') or '')
            or item in ev_mod.branch_ids(pr.get('headRefName') or '')]


def open_work(repo, main, path, item, branches=(), ask_gh=True):
    """The first branch holding ``item``'s unmerged work — one of ``branches``, of its runs, or
    the head of an open PR naming it (a PR head not fetched counts: its commits are unknown) —
    or ''. An item with such a branch is never closed as landed. ``None`` — *unknown* — when
    git found nothing and ``ask_gh`` could not read the open PRs (:func:`open_prs`): an
    unknown is never read as "no open work"."""
    for b in dict.fromkeys([*(branches or ()), *run_branches(path, item)]):
        if b and unlanded(repo, main, b):
            return b
    if ask_gh:
        heads = open_prs(repo, item)
        if heads is None:
            return None
        for b in heads:
            if not b:
                continue
            if _git(repo, ['rev-parse', '--verify', '-q', f'refs/remotes/origin/{b}']) is None:
                return b
            if unlanded(repo, main, b):
                return b
    return ''


def item_writes(product, item):
    """``item``'s ``writes:`` footprint from the product's record index, or []."""
    root = getattr(product, 'backlog_dir', None)
    if not isinstance(root, str) or not root or not item:
        return []
    try:
        from asf.views import index_reader
        items, _gen = index_reader.load(root)
    except Exception:  # noqa: BLE001 — no index, no footprint
        return []
    it = items.get(item) or items.get(item.upper()) or {}
    w = it.get('writes') if isinstance(it, dict) else None
    return [str(x) for x in w] if isinstance(w, list) else []


def verify_landings(product, occupancy, items, path=None, repo=None, main=None):
    """``(verified, unverified)`` over the open Tasks and Bugs the lifecycle records as landed
    (:func:`asf.workers.lifecycle.occupancy`'s ``landed``, its branch in ``landed_on``) while the
    record still holds them open.

    ``verified`` — ``{item: (sha, subject)}``, the feeder's ``landed_shas``: the sha is on
    ``origin/<main>`` AND :func:`attributable` to the item (named by it, its PR's merge, or
    covering its ``writes:`` — never a mere ancestor, never another PR's merge-queue commit), and
    the branch is not a document lane. An ``after:`` on such an item no longer waits for the
    ingest to catch up (#560's rules, the same the relaunch cap and trunk close apply).

    ``unverified`` — ``{item: why}``: a landing that does not hold up as the item's own — a
    spec/plan-lane merge (a reshape's split, a plan: a document, not the item's work), or a
    sha on the trunk that is not attributable to it. A landing whose sha the trunk does not
    carry yet (origin not fetched) is neither: it is left as the lifecycle says."""
    from asf.evidence import evidence as ev
    occ = occupancy or {}
    landed, on = occ.get('landed') or {}, occ.get('landed_on') or {}
    repo = repo or getattr(product, 'repo_dir', None)
    main = main or getattr(getattr(product, 'conventions', None), 'main', None) or 'main'
    prefixes = ev.branch_prefixes(product)
    verified, unverified = {}, {}
    for iid, sha in sorted(landed.items()):
        card = (items or {}).get(iid) or {}
        if card.get('type') not in ('task', 'bug') or card.get('state', 'New') in ('Resolved',
                                                                                  'Closed'):
            continue
        branch = on.get(iid) or ''
        kind = ev.lane_kind(branch, prefixes)
        if kind:
            unverified[iid] = (f'its landing ({sha[:9] or "no sha"}) was recorded on the {kind} '
                               f'lane ({branch}): a document merged, not its work')
            continue
        if not repo or not sha or not on_trunk(repo, main, sha):
            continue
        writes = [str(w) for w in card.get('writes') or ()]
        if attributable(repo, main, sha, iid, writes, run_prs(path, iid)):
            verified[iid] = (sha, _git(repo, ['log', '-1', '--format=%s', sha]) or '')
        else:
            unverified[iid] = (f'its recorded landing {sha[:9]} on origin/{main} is not its '
                               f'commit (not named by it, not its PR merge, not its writes:)')
    return verified, unverified
