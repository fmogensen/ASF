"""asf.workers.retention — origin's heads nothing owns any more, expired.

Two rules over the product's remote heads (``conventions.branch_retention``,
:data:`asf.conventions.DEFAULT_BRANCH_RETENTION`):

  archive  ``archive/<b>`` — the lane's superseded branches (:meth:`asf.harvest.lane.Lane.archive`),
           kept for reference — goes ``archive_days`` (14) after it was archived. Its tip is the
           lane's own archive commit, so the tip's commit date is the day it was archived.
  legacy   a head under one of ``legacy_prefixes`` (none by default: a retired worker system's, a
           hand-made one's) goes once its tip is ``legacy_days`` (7) old.

A candidate is kept while an open PR carries it, an open record item names it, or a run the
registry still holds (not ended, or its lane state open) is on it — for ``archive/<b>``, on
``<b>`` too. The trunk, a protected branch and a release branch are never candidates. When the
PR list or the protected list cannot be read, nothing is deleted: the pass only reports.

A delete is an ordinary ``git push origin --delete <b>`` over a lease on the tip judged, at most
``per_tick`` (50) a pass, one line each; a dry pass (``fix=False``) prints ``would delete``.

Every pass also counts the heads no configured pattern owns — not the trunk, no branch prefix,
no ``branch_patterns`` glob, not ``archive/``, no retention prefix — and writes them to
:data:`STATE_FILE`; the doctor's ``branches`` row reads it (``unowned branches: N (prefixes …)``),
so clutter the rules do not cover is always visible.
"""
import datetime
import fnmatch
import json
import os
import re
import time

from asf import env, refguard
from asf.harvest import harvest as H

#: The lane's archive namespace (:meth:`asf.harvest.lane.Lane.archive` pushes ``archive/<b>``).
ARCHIVE_PREFIX = 'archive/'
#: Heads that are releases, never clutter.
RELEASE_PREFIXES = ('release/', 'release-', 'releases/')
#: Where a pass fetches its candidates' tips to read their dates; emptied after each pass.
FETCH_NS = 'refs/asf-retention/'
#: The pass's findings, for the doctor.
STATE_FILE = 'branch-retention.json'
DAY_S = 86400


def remote_heads(repo):
    """``{branch: sha}`` of every head on origin, or None when ``ls-remote`` failed."""
    r = H.sh(['git', 'ls-remote', '--heads', 'origin'], cwd=repo)
    if r.returncode != 0:
        return None
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith('refs/heads/'):
            out[parts[1][len('refs/heads/'):]] = parts[0]
    return out


def tip_dates(repo, prefixes):
    """``{branch: (sha, committer unix time)}`` for origin's heads under ``prefixes`` — one fetch
    into :data:`FETCH_NS`, one ``for-each-ref``, then the namespace is emptied."""
    _clear_ns(repo)
    specs = [f'+refs/heads/{p}*:{FETCH_NS}{p}*' for p in dict.fromkeys(prefixes)]
    if not specs:
        return {}
    H.sh(['git', 'fetch', '-q', '--no-tags', 'origin', *specs], cwd=repo)
    r = H.sh(['git', 'for-each-ref', '--format=%(refname) %(objectname) %(committerdate:unix)',
              FETCH_NS], cwd=repo)
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].isdigit():
            out[parts[0][len(FETCH_NS):]] = (parts[1], int(parts[2]))
    _clear_ns(repo)
    return out


def _clear_ns(repo):
    refs = H.sh(['git', 'for-each-ref', '--format=%(refname)', FETCH_NS], cwd=repo).stdout.split()
    for ref in refs:
        H.sh(['git', 'update-ref', '-d', ref], cwd=repo)


def host_facts(product):
    """``(open PR heads, protected branches)`` off the PR host, each None when unreadable; two
    empty sets when the product has no hosted origin (nothing there can hold a PR)."""
    from asf.harvest.lane import repo_slug
    slug = repo_slug(product)
    if not slug:
        return set(), set()
    prs = H.gh_json(['pr', 'list', '-R', slug, '--state', 'open', '--limit', '1000',
                     '--json', 'headRefName'], None)
    heads = ({p.get('headRefName') for p in prs if isinstance(p, dict)}
             if isinstance(prs, list) else None)
    rc, stdout, _err = H._gh(['api', f'repos/{slug}/branches?protected=true&per_page=100',
                              '--paginate', '--jq', '.[].name'])
    protected = set(stdout.split()) if rc == 0 else None
    return heads, protected


def in_flight(product):
    """The branches a run the registry still holds is on: not ended, or its lane state open."""
    from asf.harvest.lane import TERMINAL_STATES
    from asf.workers import pool as pool_mod
    from asf.workers.lifecycle import lane_of
    out = set()
    for run in pool_mod.load_sessions(product).values():
        state = lane_of(run).get('state')
        if run.get('branch') and (not run.get('ended') or (state and state not in TERMINAL_STATES)):
            out.add(run['branch'])
    return out


def open_item_text(items):
    """One text of every open record card (not removed, not done) — a branch it names is held."""
    from asf.workers import lifecycle
    return '\n'.join(json.dumps(card, default=str) for iid, card in (items or {}).items()
                     if not lifecycle.closed_state(items, iid))


def _names(text, branch):
    return re.search(r'(?<![\w/.-])' + re.escape(branch) + r'(?![\w/.-])', text) is not None


def owned_patterns(conv):
    """``(prefixes, globs)`` every head a product owns falls under: its branch prefixes, the
    lane's archive, the retention prefixes, and its ``branch_patterns`` (``<slug>`` → ``*``)."""
    prefixes = set(conv.all_prefixes()) | {ARCHIVE_PREFIX} | set(conv.retention('legacy_prefixes'))
    globs = []
    patterns = conv.get('branch_patterns')
    for value in (patterns.values() if isinstance(patterns, dict) else ()):
        if isinstance(value, str) and value.strip():
            globs.append(re.sub(r'<[^>]*>', '*', value.strip()))
    return prefixes, globs


def group(branch):
    """The prefix a head is counted under: up to its first ``/``, else its first ``-``."""
    for sep in ('/', '-'):
        if sep in branch:
            return branch.split(sep, 1)[0] + sep
    return branch


def unowned(conv, heads):
    """``{prefix: count}`` of the heads no configured pattern owns."""
    prefixes, globs = owned_patterns(conv)
    out = {}
    for b in heads:
        if conv.is_trunk(b) or b.startswith(RELEASE_PREFIXES):
            continue
        if any(b.startswith(p) for p in prefixes) or any(fnmatch.fnmatchcase(b, g) for g in globs):
            continue
        out[group(b)] = out.get(group(b), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def unowned_line(counts):
    n = sum(counts.values())
    return (f"unowned branches: {n} (prefixes {', '.join(f'{p} {c}' for p, c in counts.items())})"
            if n else 'unowned branches: 0')


def _longest(branch, prefixes):
    return max((len(p) for p in prefixes if branch.startswith(p)), default=0)


def candidates(conv, heads, dates, now, prs, protected, flight, item_text):
    """``(due, kept)``: ``due`` is ``[(branch, sha, rule, age_days)]`` oldest first, ``kept`` is
    ``[(branch, why)]`` for the expired heads something still holds."""
    archive_s = conv.retention('archive_days') * DAY_S
    legacy_s = conv.retention('legacy_days') * DAY_S
    legacy = conv.retention('legacy_prefixes')
    active = {conv.prefix(k) for k in conv.kinds()}
    due, kept = [], []
    for b, sha in sorted(heads.items()):
        if b.startswith(ARCHIVE_PREFIX):
            rule, limit, also = 'archive', archive_s, b[len(ARCHIVE_PREFIX):]
        elif any(b.startswith(p) for p in legacy):
            rule, limit, also = 'legacy', legacy_s, None
        else:
            continue
        if conv.is_trunk(b) or b.startswith(RELEASE_PREFIXES) or b in (protected or ()):
            continue
        if rule == 'legacy' and _longest(b, active) >= _longest(b, legacy):
            continue  # under a prefix the product still mints: never a retired head
        tip = dates.get(b)
        if tip is None or tip[0] != sha:
            continue  # no date read, or it moved since the listing: judged next pass
        age = now - tip[1]
        if age < limit:
            continue
        names = [n for n in (b, also) if n]
        if prs is not None and any(n in prs for n in names):
            kept.append((b, 'open PR'))
        elif any(n in flight for n in names):
            kept.append((b, 'in-flight run'))
        elif _names(item_text, b):
            kept.append((b, 'named by an open item'))
        else:
            due.append((b, sha, rule, age // DAY_S))
    due.sort(key=lambda d: (-d[3], d[0]))
    return due, kept


def delete(repo, branch, sha, slug=None, main=None, protected=None):
    """Delete ``branch`` on origin only while its tip is still ``sha``; ``(ok, why)``.

    With a hosted origin (``slug``) the ref is deleted through the host's API: a delete carries
    no content, and a ``git push --delete`` would run the product's own pre-push hook — one
    product's hook ran a whole-tree lint per delete and refused every one (2026-09-25). Without
    a host, ``git push origin --delete`` over a lease on ``sha``. The trunk and a protected ref
    are refused before anything is sent (:mod:`asf.refguard`)."""
    from asf import refguard
    guard = refguard.refusal(branch, f'retention delete {branch}', main, protected)
    if guard:
        return False, guard
    if slug:
        ref = f'repos/{slug}/git/refs/heads/{branch}'
        rc, out, err = H._gh(['api', ref, '--jq', '.object.sha'])
        if rc != 0:
            return False, H.tail(err or out) or 'ref unreadable'
        if out.strip() != sha:
            return False, f'tip moved ({out.strip()[:9]} is not {sha[:9]}) — kept'
        rc, out, err = H._gh(['api', '-X', 'DELETE', ref])
        return rc == 0, '' if rc == 0 else (H.tail(err or out) or f'gh exit {rc}')
    r = H.sh(['git', 'push', '-q', f'--force-with-lease=refs/heads/{branch}:{sha}', 'origin',
              '--delete', branch], cwd=repo)
    return r.returncode == 0, H.tail(r.stderr or r.stdout) if r.returncode else ''


def write_state(product, data):
    path = os.path.join(env.state_dir(product), STATE_FILE)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_state(product):
    try:
        with open(os.path.join(env.state_dir(product), STATE_FILE), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def sweep(product, fix=False, out=print, items=None, host=None, now=None):
    """One pass: report the unowned heads, delete (``fix``) or name (dry) the expired ones.
    ``host`` is ``(open PR heads, protected)`` (default: :func:`host_facts`). Returns
    ``{'deleted': [...], 'due': [...], 'kept': [...], 'unowned': {...}, 'failed': [...]}``."""
    repo, conv = product.repo_dir, product.conventions
    result = {'deleted': [], 'due': [], 'kept': [], 'unowned': {}, 'failed': []}
    if not repo or not os.path.isdir(repo):
        return result
    heads = remote_heads(repo)
    if heads is None:
        out('retention: origin unreadable — nothing judged')
        return result
    now = int(now if now is not None else time.time())
    result['unowned'] = unowned(conv, heads)
    if items is None:
        from asf.workers.health import record_items
        items = record_items(product)
    prefixes = [p for p in (ARCHIVE_PREFIX, *conv.retention('legacy_prefixes'))
                if any(b.startswith(p) for b in heads)]
    dates = tip_dates(repo, prefixes) if prefixes else {}
    prs, protected = host if host is not None else (host_facts(product) if dates else (set(), set()))
    due, kept = candidates(conv, heads, dates, now, prs, protected, in_flight(product),
                           open_item_text(items))
    result['kept'] = kept
    blind = prs is None or protected is None
    cap = conv.retention('per_tick')
    from asf.harvest.lane import repo_slug
    slug = repo_slug(product)   # None for an unhosted origin: the git path below
    for b, sha, rule, days in due[:cap]:
        what = f'{b} ({rule}, {days}d old)'
        if not fix or blind:
            out(f'retention: would delete {what}')
            result['due'].append(b)
            continue
        ok, why = delete(repo, b, sha, slug=slug, main=product.main,
                         protected=refguard.listed(conv))
        if not ok and len(result['failed']) >= 2 and not result['deleted']:
            result['failed'].append(b)
            out(f'retention: delete failed {what} — {why}; stopping this pass after 3 failures')
            break
        if ok:
            out(f'retention: deleted {what}')
            result['deleted'].append(b)
        else:
            out(f'retention: delete failed {what} — {why}')
            result['failed'].append(b)
    result['due'].extend(b for b, *_ in due[cap:])
    if blind and due:
        out('retention: the PR host did not answer (open PRs / protected branches) — nothing '
            'deleted this pass')
    if due[cap:]:
        out(f'retention: {len(due) - cap} more expired, left for the next pass (per_tick {cap})')
    if result['unowned']:
        out(f"retention: {unowned_line(result['unowned'])}")
    write_state(product, {
        'at': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime(
            '%Y-%m-%dT%H:%M:%SZ'),
        'heads': len(heads), 'unowned': result['unowned'], 'deleted': len(result['deleted']),
        'due': len(result['due']), 'kept': len(kept)})
    return result


def doctor_line(product):
    """``(ok, 'unowned branches: N (prefixes …)')`` from the last pass, or None before one ran."""
    data = read_state(product)
    if not isinstance(data, dict) or not isinstance(data.get('unowned'), dict):
        return None
    counts = data['unowned']
    line = unowned_line(counts) + f" — of {data.get('heads', '?')} heads at {data.get('at', '?')}"
    if data.get('due'):
        line += f"; {data['due']} expired, awaiting delete"
    return not counts, line
