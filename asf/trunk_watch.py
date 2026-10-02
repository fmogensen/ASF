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

**Since the door was shut.** A commit is counted only once the trunk ruleset that requires the
queue's status was in force (:func:`since`): ``merge_queue.watch_since`` (an ISO date or time)
when set, else the creation time of the active ruleset requiring
:meth:`asf.conventions.Conventions.queue_status`, read off the host at most once a day
(:data:`RULESET_READ_S`) and kept in the state file. Merges from before the ruleset (the ones
that made it necessary) never keep the row red.

**Trunk stall.** The same look records when the trunk tip last moved (``moved_at``). A trunk that
has not moved for more than ``conventions.ci.trunk_stall_hours`` (default 4) while landings wait
— a batch in the merge queue, or an ``asf land`` request — is one ``trunk watch: STALL`` line
every tick and a red ``Trunk stall`` row in ``asf status`` and ``asf doctor`` (:func:`stall`).

Detection only: nothing is ever reverted.
"""
import datetime
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
#: how often the tick reads the trunk ruleset's activation time off the host
RULESET_READ_S = 86400
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
    moved = state.get('moved_at')
    if tip != state.get('checked') or not moved:
        # a move this look saw is dated now; a tip first read (or read before moves were
        # recorded) by its commit time
        ct = (_git(repo, ['log', '-1', '--format=%ct', tip]) or '').strip()
        seen = state.get('checked') and tip != state.get('checked')
        moved = int(now) if seen or not ct.isdigit() else int(ct)
    out = dict(state)
    out.update({'checked': tip, 'bypass': keep, 'at': int(now), 'days': days, 'moved_at': moved})
    return out, new


def line(b):
    pr = f"PR #{b['pr']}" if b.get('pr') else 'no PR'
    return (f"{b['sha'][:9]} by {b.get('author') or '?'} ({pr}): {b.get('subject') or ''}")


def _epoch(value):
    """An ISO date / date-time (``2026-10-01``, ``2026-10-01T11:48:42Z``, ``…+02:00``), a
    ``datetime``/``date`` (YAML parses one unquoted) or an epoch number, as epoch seconds; None
    for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, datetime.date):
        dt = datetime.datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def configured_since(product):
    """``merge_queue.watch_since`` as epoch seconds, or None when unset or unreadable."""
    conv = product.conventions
    raw = conv.map_of('merge_queue') if hasattr(conv, 'map_of') else {}
    return _epoch((raw or {}).get('watch_since'))


def ruleset_since(product, gh=None):
    """``(epoch, ruleset id)``: when the active trunk ruleset requiring the queue's status was
    created, read off the host (two calls); ``(None, None)`` when none is active or the host is
    unreadable."""
    from asf import trunk_ruleset
    from asf.harvest import harvest as H
    gh = gh or H._gh
    slug, trunk = product.repo_slug, product.conventions.main
    rc, text, _err = gh(['api', f'repos/{slug}/rules/branches/{trunk}'])
    rules = json.loads(text) if rc == 0 and (text or '').strip() else None
    ids = [i for i in trunk_ruleset.requiring(rules, trunk_ruleset.context_of(product)) if i]
    if not ids:
        return None, None
    rc, text, _err = gh(['api', f'repos/{slug}/rulesets/{ids[0]}'])
    got = json.loads(text) if rc == 0 and (text or '').strip() else {}
    return _epoch((got or {}).get('created_at')), ids[0]


def since(product, state=None):
    """``(epoch, why)``: the start of what the watch counts — ``merge_queue.watch_since`` when
    set, else the trunk ruleset's activation (off the state file); ``(None, '')`` when neither
    is known (the whole window counts)."""
    got = configured_since(product)
    if got is not None:
        return got, 'merge_queue.watch_since'
    state = load(_state_dir(product)) if state is None else state
    got = state.get('ruleset_since')
    if isinstance(got, (int, float)) and not isinstance(got, bool):
        return int(got), f"ruleset {state.get('ruleset_id') or '?'} active"
    return None, ''


def _refresh_ruleset(product, state, now, gh=None):
    """Read the ruleset's activation onto ``state`` at most once per :data:`RULESET_READ_S`
    (never when ``merge_queue.watch_since`` names it). Never raises."""
    if configured_since(product) is not None or not getattr(product, 'repo_slug', None):
        return
    if now - (state.get('ruleset_read') or 0) < RULESET_READ_S and 'ruleset_since' in state:
        return
    try:
        at, rid = ruleset_since(product, gh)
    except Exception:  # noqa: BLE001 — an unreadable host leaves the last reading
        return
    state['ruleset_read'] = int(now)
    if at is not None:
        state['ruleset_since'], state['ruleset_id'] = at, rid


def tick(product, out=print, now=None, gh=None):
    """The harvest step's look: one ``trunk watch:`` line per new bypass commit (after
    :func:`since`), and one ``trunk watch: STALL`` line while :func:`stall` holds. Never
    raises, never reverts. The bypasses now counted (a list), or None when not watched."""
    if not watched(product):
        return None
    trunk = product.conventions.main
    sd = _state_dir(product)
    now = time.time() if now is None else now
    try:
        state, new = scan(os.path.expanduser(product.repo_dir), trunk, load(sd),
                          window_days(product), now)
        _refresh_ruleset(product, state, now, gh)
        save(sd, state)
    except OSError as e:
        out(f'trunk watch: unreadable — {e}')
        return None
    start, _why = since(product, state)
    for b in reversed(new):
        if start is not None and b.get('at', 0) < start:
            continue
        out(f'trunk watch: {line(b)} landed on {trunk} outside the merge queue — '
            f'asf land <pr> is the way in (detection only, nothing reverted)')
    got = stall(product, now, state)
    if got:
        out(f'trunk watch: STALL {got}')
    return bypasses(product, now, state) or []


def bypasses(product, now=None, state=None):
    """The bypass commits in the window and after :func:`since`, newest first, off the state
    file; None before the first look."""
    state = load(_state_dir(product)) if state is None else state
    if 'checked' not in state:
        return None
    now = time.time() if now is None else now
    days = window_days(product)
    start, _why = since(product, state)
    floor = max(now - days * 86400, start if start is not None else 0)
    return [b for b in state.get('bypass') or () if b.get('at', 0) >= floor]


def waiting(product):
    """``[what]``: the landings waiting on the trunk — each batch in the merge queue, each
    ``asf land`` request — off the queue's own files."""
    from asf import merge_queue
    sd = _state_dir(product)
    out = []
    for b in merge_queue.load(sd)['batches']:
        prs = ', '.join(f"#{m.get('pr')}" for m in b.get('members') or ())
        out.append(f"batch {b['ref']} ({prs})")
    for n in sorted(merge_queue.load_requests(sd), key=lambda k: int(k) if str(k).isdigit() else 0):
        out.append(f'asf land #{n}')
    return out


def stall(product, now=None, state=None):
    """The stall alarm's text — ``<trunk> has not moved for Xh (> Nh, …) while K landing(s)
    wait: …`` — when the trunk tip has stood still longer than
    :meth:`asf.conventions.Conventions.trunk_stall_hours` while :func:`waiting` names
    something; else None. Off the state files only."""
    if not watched(product):
        return None
    state = load(_state_dir(product)) if state is None else state
    moved = state.get('moved_at')
    if not isinstance(moved, (int, float)) or isinstance(moved, bool):
        return None
    now = time.time() if now is None else now
    limit = product.conventions.trunk_stall_hours()
    hours = (now - moved) / 3600
    if hours <= limit:
        return None
    wait = waiting(product)
    if not wait:
        return None
    named = '; '.join(wait[:STATUS_NAMES]) + (f'; +{len(wait) - STATUS_NAMES} more'
                                              if len(wait) > STATUS_NAMES else '')
    tip = str(state.get('checked') or '?')[:9]
    return (f'{product.conventions.main} has not moved for {hours:.1f}h (at {tip}; > {limit}h, '
            f'conventions.ci.trunk_stall_hours) while {len(wait)} landing(s) wait: {named}')


def stall_cell(product, now=None):
    """``RED <stall>`` while :func:`stall` holds, else None (no row)."""
    got = stall(product, now)
    return f'RED {got}' if got else None


def stall_rows(product, now=None):
    """``[(required, ok, detail)]`` for ``asf doctor``: red while :func:`stall` holds, green
    otherwise, unknown before the first look; ``[]`` off the merge queue."""
    if not watched(product):
        return []
    state = load(_state_dir(product))
    if 'moved_at' not in state:
        return [(True, None, f'{product.conventions.main} not read yet — the next tick looks')]
    got = stall(product, now, state)
    if got:
        return [(True, False, got)]
    return [(True, True, f'{product.conventions.main} moving, or nothing waits to land '
                         f'(alarm after {product.conventions.trunk_stall_hours()}h)')]


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
    start, why = since(product)
    after = (f" since {datetime.datetime.fromtimestamp(start, datetime.timezone.utc):%Y-%m-%d %H:%M}Z"
             f" ({why})") if start is not None else ''
    if not got:
        return [(True, True, f'every commit on {trunk} in {days}d{after} came through the '
                             f'merge queue')]
    rows = [(True, False, f'{line(b)} — landed outside the queue') for b in got[:DOCTOR_ROWS]]
    if len(got) > DOCTOR_ROWS:
        rows.append((True, False, f'and {len(got) - DOCTOR_ROWS} more in {days}d'))
    return rows
