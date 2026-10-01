"""asf.trunk_watch — one door to the trunk: every commit on it came through the merge queue.

Under ``conventions.merge: queue`` the trunk only ever moves by the queue fast-forwarding it to
a green batch sha (:mod:`asf.merge_queue`), so every commit on its first-parent line is one of
the queue's own merge commits: subject ``merge-queue: #<pr> (…)`` and the trailer
:data:`asf.merge_queue.TRAILER`. Anything else — a ``gh pr merge`` of a hotfix, a direct push —
landed a sha nobody gated. The designed way in for such a PR is ``asf land <pr>``.

Each tick (:func:`tick`, from the harvest step) reads the first-parent commits the trunk gained
since the last look (the first look: the last :data:`DEFAULT_DAYS` days, ``merge_queue.
watch_days``, from the queue's oldest commit in them), writes one log line per commit that did not come through the queue, and keeps
them in ``state/<product>/trunk-watch.json`` for the window. ``asf status`` and ``asf doctor``
read that file (no git, no host call) and show each one red with its sha, author and PR.

Detection only: nothing is ever reverted.
"""
import json
import os
import re
import subprocess
import time

STATE_FILE = 'trunk-watch.json'
#: how far back a bypass is shown, and how far the first look reads (``merge_queue.watch_days``)
DEFAULT_DAYS = 3
#: the most bypass commits ``asf doctor`` lists one row each (the rest are counted)
DOCTOR_ROWS = 10
#: the most ``asf status`` names in its one cell
STATUS_NAMES = 5
QUEUE_SUBJECT = re.compile(r'^merge-queue: #\d+ ')
_PR_RES = (re.compile(r'^Merge pull request #(\d+) '), re.compile(r'\(#(\d+)\)\s*$'),
           re.compile(r'^merge-queue: #(\d+) '))
_SEP, _END = '\x1f', '\x1e'


def _trailer():
    from asf import merge_queue
    return merge_queue.TRAILER


def is_queue_commit(subject, body=''):
    """True for one of the queue's own merge commits: its subject, or its trailer."""
    if QUEUE_SUBJECT.match(subject or ''):
        return True
    t = _trailer().lower()
    return any(line.strip().lower() == t for line in (body or '').splitlines())


def pr_of(subject):
    """The PR number a commit subject names (``Merge pull request #N``, ``… (#N)``), or None."""
    for rx in _PR_RES:
        m = rx.search(subject or '')
        if m:
            return int(m.group(1))
    return None


def watched(product):
    """True when the product lands through the merge queue and has a checkout to read."""
    conv = getattr(product, 'conventions', None)
    return bool(getattr(product, 'repo_dir', None) and conv is not None
                and getattr(conv, 'merge_queue', lambda: False)())


def window_days(product):
    raw = product.conventions.map_of('merge_queue') if hasattr(product.conventions, 'map_of') else {}
    v = (raw or {}).get('watch_days')
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else DEFAULT_DAYS


def _state_dir(product):
    from asf import env
    return env.state_dir(product)


def path(state_dir):
    return os.path.join(state_dir, STATE_FILE)


def load(state_dir):
    try:
        with open(path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(state_dir, data):
    os.makedirs(state_dir, exist_ok=True)
    tmp = path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, path(state_dir))


def _git(repo, args):
    try:
        p = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def first_parent(repo, trunk, since=None, days=DEFAULT_DAYS):
    """``[{sha, author, at, subject, body}]`` newest first: the first-parent commits on
    ``origin/<trunk>`` after ``since`` (a sha the trunk still contains), else of the last
    ``days`` days. None when git cannot read them."""
    rng = [f'{since}..origin/{trunk}'] if since else [f'--since={days} days ago', f'origin/{trunk}']
    text = _git(repo, ['log', '--first-parent', f'--format=%H{_SEP}%an{_SEP}%ct{_SEP}%s{_SEP}%b{_END}',
                       *rng])
    if text is None:
        return None
    out = []
    for rec in text.split(_END):
        parts = rec.strip('\n').split(_SEP)
        if len(parts) < 5 or not parts[0].strip():
            continue
        sha, author, at, subject, body = parts[0].strip(), parts[1], parts[2], parts[3], parts[4]
        out.append({'sha': sha, 'author': author, 'at': int(at) if at.isdigit() else 0,
                    'subject': subject, 'body': body})
    return out


def scan(repo, trunk, state, days=DEFAULT_DAYS, now=None):
    """``(state, new)``: ``state`` advanced to the trunk's tip with the bypass commits it gained
    (older than ``days`` dropped), and ``new`` — the bypasses this look found. Pure but for git."""
    now = time.time() if now is None else now
    tip = (_git(repo, ['rev-parse', f'origin/{trunk}']) or '').strip()
    if not tip:
        return state, []
    since = state.get('checked')
    if since and _git(repo, ['merge-base', '--is-ancestor', since, tip]) is None:
        since = None    # the trunk was rewritten under the last look: read the window again
    commits = first_parent(repo, trunk, since, days) if since != tip else []
    if commits is None:
        return state, []
    if since is None:
        # the first look: the queue's history starts at its oldest commit in the window — what
        # landed before the queue was on is no bypass
        first = max((i for i, c in enumerate(commits) if is_queue_commit(c['subject'], c['body'])),
                    default=None)
        commits = commits[:first] if first is not None else []
    known = {b['sha'] for b in state.get('bypass') or ()}
    new = [{'sha': c['sha'], 'author': c['author'], 'at': c['at'],
            'subject': c['subject'][:120], 'pr': pr_of(c['subject'])}
           for c in commits if not is_queue_commit(c['subject'], c['body'])
           and c['sha'] not in known]
    keep = [b for b in list(state.get('bypass') or ()) + new if b.get('at', 0) >= now - days * 86400]
    keep.sort(key=lambda b: b.get('at', 0), reverse=True)
    return {'checked': tip, 'bypass': keep, 'at': int(now), 'days': days}, new


def line(b):
    pr = f"PR #{b['pr']}" if b.get('pr') else 'no PR'
    return (f"{b['sha'][:9]} by {b.get('author') or '?'} ({pr}): {b.get('subject') or ''}")


def tick(product, out=print, now=None):
    """The harvest step's look: one ``trunk watch:`` line per new bypass commit. Never raises,
    never reverts. The bypasses now in the window (a list), or None when not watched."""
    if not watched(product):
        return None
    trunk = product.conventions.main
    sd = _state_dir(product)
    try:
        state, new = scan(os.path.expanduser(product.repo_dir), trunk, load(sd),
                          window_days(product), now)
        save(sd, state)
    except OSError as e:
        out(f'trunk watch: unreadable — {e}')
        return None
    for b in reversed(new):
        out(f'trunk watch: {line(b)} landed on {trunk} outside the merge queue — '
            f'asf land <pr> is the way in (detection only, nothing reverted)')
    return state.get('bypass') or []


def bypasses(product, now=None):
    """The bypass commits in the window, newest first, off the state file; None before the
    first look."""
    state = load(_state_dir(product))
    if 'checked' not in state:
        return None
    now = time.time() if now is None else now
    days = window_days(product)
    return [b for b in state.get('bypass') or () if b.get('at', 0) >= now - days * 86400]


def status_cell(product, now=None):
    """``RED N commit(s) …`` naming the newest bypasses, or None (no row) when there are none."""
    if not watched(product):
        return None
    got = bypasses(product, now)
    if not got:
        return None
    days = window_days(product)
    named = '; '.join(line(b) for b in got[:STATUS_NAMES])
    more = f'; +{len(got) - STATUS_NAMES} more' if len(got) > STATUS_NAMES else ''
    return (f"RED {len(got)} commit(s) on {product.conventions.main} in {days}d bypassed the "
            f"merge queue — {named}{more} (asf land <pr> is the way in)")


def doctor_rows(product, now=None):
    """``[(required, ok, detail)]``: one red row per bypass commit (at most :data:`DOCTOR_ROWS`,
    then a count), one ok row when there is none, an unknown one before the first look; ``[]``
    for a product not on the merge queue."""
    if not watched(product):
        return []
    got = bypasses(product, now)
    days = window_days(product)
    trunk = product.conventions.main
    if got is None:
        return [(True, None, f'{trunk} not read yet — the next tick looks')]
    if not got:
        return [(True, True, f'every commit on {trunk} in {days}d came through the merge queue')]
    rows = [(True, False, f'{line(b)} — landed outside the queue') for b in got[:DOCTOR_ROWS]]
    if len(got) > DOCTOR_ROWS:
        rows.append((True, False, f'and {len(got) - DOCTOR_ROWS} more in {days}d'))
    return rows
