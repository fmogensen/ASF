"""asf.tree_green — a green CI read belongs to a *tree*, not only to the commit that carried it.

A factory push that changes nothing but commit messages — a naming reword, a sign-off trailer —
gives the PR a new head with a byte-identical tree. GitHub throws the old head's checks away and
the merge queue waits on a full rerun of CI that already passed on exactly this content
(B-0275: a reword force-pushed a green, land-requested head and cost it 45–60 min on a saturated
pool, on a product's goal day).

Two halves, and the first matters more than the second:

* the rewrite sites do not do that any more — :meth:`asf.harvest.lane.Lane.reword_held` defers a
  reword while the PR has an ``asf land`` request, a green required check on its head, or a run in
  flight. A document lane's subject is composed at merge time anyway
  (:func:`asf.harvest.lane.squash_subject`), so the reword waits for landing and loses nothing;
* when such a head arrives all the same (a person's force-push, a rewrite from before this rule),
  the green is **carried**: :func:`remember` writes down each head whose required checks were all
  success together with its root tree, and :func:`carried` answers a later head of the same PR
  whose tree is that same object. The gates then read green and say
  ``green carried from <old head> (identical tree)``.

A carry is never a guess about the content: the root tree object is the content. It is only
granted when the remembered head passed every check now required (a required set that grew since
carries nothing), and only within the same PR.

State: ``state/<product>/tree-green.json`` — ``{"<pr>": [{head, tree, passed, at}]}``, newest
first, :data:`MAX_PER_PR` heads a PR, dropped after :data:`KEEP_S`. Never raises.
"""
import json
import os
import time

from asf import gitops

STATE_FILE = 'tree-green.json'
#: a remembered green older than this is dropped: its tree is nobody's head any more
KEEP_S = 7 * 86400
#: how many green heads one PR keeps — a PR reworded more than this has bigger problems
MAX_PER_PR = 20
#: the one line a carried green is recorded with, whatever the gate (B-0275)
CARRIED_FMT = 'green carried from {old} (identical tree)'


def path(state_dir):
    return os.path.join(state_dir, STATE_FILE)


def load(state_dir):
    """``{'<pr>': [{head, tree, passed, at}]}``; an unreadable file remembers nothing."""
    try:
        with open(path(state_dir), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for pr, rows in data.items():
        kept = [r for r in rows if isinstance(r, dict) and r.get('head') and r.get('tree')] \
            if isinstance(rows, list) else []
        if kept:
            out[str(pr)] = kept
    return out


def save(state_dir, data):
    os.makedirs(state_dir, exist_ok=True)
    tmp = path(state_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, sort_keys=True, indent=1)
    os.replace(tmp, path(state_dir))


def tree_at(repo, rev):
    """The root tree object of ``rev`` (:func:`asf.gitops.rev_parse` of ``<rev>^{tree}``), or ''
    — no repo, no such rev, a read that failed. The tree is the content: two heads with this one
    sha carry byte-identical trees, whatever their messages, authors or parents say.

    Unknown collapses into '' here on purpose: every caller asks this to *grant* a carry, and a
    read git could not answer must grant nothing — the same answer as no such rev."""
    if not repo or not rev:
        return ''
    return gitops.rev_parse(repo, f'{rev}^{{tree}}') or ''


def remember(state_dir, pr, head, tree, passed=(), now=None):
    """Write down that ``head`` of PR ``pr`` — root tree ``tree`` — had every required check
    success, ``passed`` naming them. A head already remembered is refreshed, not duplicated.
    False when there is nothing to write down (no pr, head or tree) or the file would not
    write."""
    if not pr or not head or not tree:
        return False
    now = time.time() if now is None else now
    key = str(pr)
    data = load(state_dir)
    rows = [r for r in data.get(key, ()) if r.get('head') != head
            and float(r.get('at') or 0) > now - KEEP_S]
    rows.insert(0, {'head': head, 'tree': tree, 'passed': sorted(str(n) for n in passed),
                    'at': now})
    data[key] = rows[:MAX_PER_PR]
    for other, rows in list(data.items()):
        kept = [r for r in rows if float(r.get('at') or 0) > now - KEEP_S]
        if kept:
            data[other] = kept
        else:
            del data[other]
    try:
        save(state_dir, data)
    except OSError:
        return False
    return True


def green_on(state_dir, pr, head, now=None):
    """The remembered green for exactly this ``head`` of PR ``pr``, or None. The question a
    rewrite site asks before it throws a head away."""
    now = time.time() if now is None else now
    for r in load(state_dir).get(str(pr or ''), ()):
        if r.get('head') == head and float(r.get('at') or 0) > now - KEEP_S:
            return r
    return None


def carried(state_dir, pr, head, tree, required=(), now=None):
    """The remembered green of an *earlier* head of PR ``pr`` whose root tree is ``tree``, or
    None. ``required`` is what must be green now: a remembered head that did not pass every one
    of those names carries nothing — a required set that grew since is a set this green never
    answered."""
    if not tree:
        return None
    now = time.time() if now is None else now
    want = {str(n) for n in required or ()}
    for r in load(state_dir).get(str(pr or ''), ()):
        if r.get('tree') != tree or r.get('head') == head:
            continue
        if float(r.get('at') or 0) <= now - KEEP_S:
            continue
        if want and not want <= set(r.get('passed') or ()):
            continue
        return r
    return None


def carried_line(rec):
    """The :data:`CARRIED_FMT` line for the record :func:`carried` answered with."""
    return CARRIED_FMT.format(old=str((rec or {}).get('head') or '')[:12])
