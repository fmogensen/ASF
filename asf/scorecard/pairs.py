"""asf.scorecard.pairs — the lane experiment's pairs, and the one thing that spoils one: overlap.

A pair is two Features carrying the same ``ab_pair: <name>`` — one on ``lane: direct``, one on
the full pipeline — built side by side so the scorecard compares like with like
(:func:`asf.scorecard.score.pair_table`). Two Features whose work touches the same files are not
like with like: whichever lands second rebases onto the first, pays its conflicts and its CI
reruns, and the comparison measures the collision, not the lane. :func:`overlaps` finds that —
the paths both Features' branches touch — for the doctor's ``ab pairs`` row and the
``--by-lane`` view.

A Feature's touched files are the union, over the Feature and every card under it, of what its
branches on origin change against the trunk (``git diff --name-only trunk...branch``) and of
what the trunk's commits naming those ids changed (a landed branch is often deleted). Read-only:
``git diff`` and ``git log`` on the product checkout, nothing fetched.
"""
import subprocess

#: How far back the trunk's history is read for commits naming a pair's ids.
LOG_SINCE = '90.days'


def pairs(items):
    """``{name: [feature id, …]}`` of every live Feature carrying ``ab_pair``, ids sorted."""
    out = {}
    for iid, it in sorted((items or {}).items()):
        if not isinstance(it, dict) or it.get('type') != 'feature' or it.get('removed'):
            continue
        name = str(it.get('ab_pair') or '').strip()
        if name:
            out.setdefault(name, []).append(iid)
    return out


def subtree_ids(items, fid):
    """The Feature and every card under it (by ``parent``)."""
    kids = {}
    for iid, it in (items or {}).items():
        if isinstance(it, dict) and it.get('parent'):
            kids.setdefault(it['parent'], []).append(iid)
    out, todo = [], [fid]
    while todo:
        i = todo.pop()
        if i not in out:
            out.append(i)
            todo.extend(kids.get(i, ()))
    return sorted(out)


def branches_of(items, ids, conv):
    """The branches the cards ``ids`` work on: their ``links.branches``, the direct lane's for a
    Feature, the code lane's for a Task, the spec/plan lane's are documents and left out."""
    out = []
    for iid in ids:
        it = (items or {}).get(iid) or {}
        out += [b for b in (it.get('links') or {}).get('branches') or () if isinstance(b, str)]
        if it.get('type') == 'feature':
            out.append(conv.branch('direct', iid))
        elif it.get('type') == 'task':
            out.append(conv.branch('code', iid))
    return list(dict.fromkeys(out))


def _git(repo, args):
    try:
        r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def touched(repo, trunk, items, fid, conv):
    """Every path the Feature ``fid``'s work changes: on its branches still on origin, and in the
    trunk's commits whose message names one of its ids."""
    ids = subtree_ids(items, fid)
    doc_roots = tuple(str(conv.get(k)).strip('/') + '/'
                      for k in ('specs_dir', 'plans_dir', 'reviews_dir') if conv.get(k))
    paths = set()
    for b in branches_of(items, ids, conv):
        out = _git(repo, ['diff', '--name-only', f'origin/{trunk}...origin/{b}'])
        paths.update(line for line in (out or '').splitlines() if line.strip())
    greps = []
    for iid in ids:
        greps += ['--grep', f'(^|[^A-Za-z0-9-]){iid}([^0-9]|$)']  # ids are [A-Z]-dddd
    out = _git(repo, ['log', f'origin/{trunk}', f'--since={LOG_SINCE}', '-E', '--format=',
                      '--name-only', *greps])
    paths.update(line for line in (out or '').splitlines() if line.strip())
    # a spec, a plan or a review is the pipeline's paperwork, not the product's code
    return {p for p in paths if not p.startswith(doc_roots)}


def overlaps(product, items, touched_fn=None):
    """``[(pair, (fid, …), [shared path, …])]`` for every pair whose Features' touched files
    intersect. ``touched_fn(fid)`` stands in for :func:`touched` (tests)."""
    if touched_fn is None:
        repo = getattr(product, 'repo_dir', None)
        if not repo:
            return []
        conv, trunk = product.conventions, product.conventions.main

        def touched_fn(fid):
            return touched(repo, trunk, items, fid, conv)
    out = []
    for name, fids in pairs(items).items():
        if len(fids) < 2:
            continue
        sets = {fid: touched_fn(fid) for fid in fids}
        shared = set()
        for i, a in enumerate(fids):
            for b in fids[i + 1:]:
                shared |= sets[a] & sets[b]
        if shared:
            out.append((name, tuple(fids), sorted(shared)))
    return out


def overlap_lines(found, limit=3):
    """One warning line per overlapping pair."""
    lines = []
    for name, fids, shared in found:
        more = f' +{len(shared) - limit}' if len(shared) > limit else ''
        lines.append(f"pair {name} ({' / '.join(fids)}) overlaps in {len(shared)} file(s): "
                     f"{', '.join(shared[:limit])}{more} — the comparison measures the collision")
    return lines


def load_items(root):
    """The live ``{id: item}`` of the record's ``index.json`` under ``root``; ``{}`` unread."""
    import json
    import os
    try:
        with open(os.path.join(root, 'index.json'), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, TypeError, ValueError):
        return {}
    items = data.get('items') if isinstance(data, dict) else None
    return {k: v for k, v in (items or {}).items() if isinstance(v, dict) and not v.get('removed')}


def doctor_rows(product, root=None):
    """``[(ok, detail)]`` — the doctor's ``ab pairs`` row: one warning per pair whose Features
    touch the same files, else one line naming the pairs; nothing when no card names a pair."""
    items = load_items(root or getattr(product, 'backlog_dir', None))
    named = pairs(items)
    if not named:
        return []
    found = overlaps(product, items)
    if found:
        return [(False, line) for line in overlap_lines(found)]
    return [(True, f"{len(named)} pair(s), no overlap in touched files: "
                   + ', '.join(f"{n} ({'/'.join(f)})" for n, f in sorted(named.items())))]


def status_cell(root, product):
    """``A/B pairs``: the overlap warnings joined, or None — the row only speaks up when a pair's
    Features touch the same files."""
    bad = [detail for ok, detail in doctor_rows(product, root) if not ok]
    return '; '.join(bad) if bad else None
