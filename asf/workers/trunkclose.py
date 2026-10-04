"""asf.workers.trunkclose — a Task whose work is already on the trunk is closed, never relaunched.

A Task cut after its work landed under another card's commit has no commit naming it, so no
evidence rule closes it; its build session reads the trunk, says so (``status: done``, "already on
origin/main under <sha>") and ends with nothing to push. Before this module the next tick
launched the same row again (11 coder sessions in 7 days on one product, two loops of 69 and 76
launches), and once the relaunch cap (:mod:`asf.workers.relaunch`) parked such a row it waited
for a person to groom the card.

The evidence is the one the relaunch cap verifies (:func:`asf.workers.relaunch.on_trunk`), made
strict (:func:`trunk_sha`): the run's own REPORT ends ``status: done`` and names a commit that
``git merge-base --is-ancestor`` finds on ``origin/<main>`` and that is not the run's own output
(its launch head, a sha its ``pushed:``/``commits:`` name, a commit it pushed — a reshape whose
split landed proves nothing about the Task's work), and its branch carries nothing past the
trunk — no work of its own is left unlanded. With it:

* :func:`closes_before_launch` — a coder, delivery-code or reshape row is not launched: the
  item's newest ended run is closed on that sha instead;
* :func:`close_parked` — a park whose reason carries the same verified evidence closes its card
  instead of waiting for a person;
* :func:`close` — the one write: ``harvested: <sha>`` on the run (the lane's own landing mark,
  which :func:`asf.evidence.evidence.merge_facts` reads as the item's merge fact, so the next
  ingest closes the card by its ordinary rule), ``trunk_closed: <why>`` beside it, and any
  pending correction on it cleared. The record is never written here.
"""
import re

from asf import gitops
from asf.workers import landing
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import relaunch
from asf.workers import report as report_mod

#: The brief kinds whose launch is skipped when the Task's work is verified on the trunk.
KINDS = ('coder', 'delivery-code', 'reshape')
#: The report claim that says the session found nothing left to do.
DONE = 'status done'
FULL_SHA_RE = re.compile(r'[0-9a-f]{40}')


class Unknown:
    """:func:`evidence` when the open PRs could not be read: not evidence (falsy — nothing
    closes on it) and not "no evidence" either — a launch the evidence would have stopped waits
    (:func:`closes_before_launch`)."""

    def __init__(self, why):
        self.why = why

    def __bool__(self):
        return False

    def __repr__(self):
        return f'Unknown({self.why!r})'


#: The wait a row shows while :func:`evidence` is :class:`Unknown`.
WAITS_UNKNOWN = 'WAITS ON gh (unknown)'
LEAD_SHA_RE = re.compile(r'\s*([0-9a-f]{7,40})\b')


def _git(repo, args):
    """``git <args>``'s stdout, stripped, or ``None`` when it failed or could not answer."""
    r = gitops.git(args, repo)
    return r.data if r.ok else None


def full_sha(repo, sha):
    """``sha`` spelled in full, or '' when git does not know it."""
    out = _git(repo, ['rev-parse', '--verify', '-q', f'{sha}^{{commit}}']) if sha else None
    return out if out and FULL_SHA_RE.fullmatch(out) else ''


def unlanded(repo, main, branch):
    """True when ``origin/<branch>`` carries a commit ``origin/<main>`` does not: the branch still
    holds work of its own (:func:`asf.workers.landing.unlanded`)."""
    return landing.unlanded(repo, main, branch)


def newest_ended(path, item):
    """The item's newest ended run since its last ``asf unpark`` (a spent window's run aside)."""
    runs = lifecycle.item_runs(path, item) if item else []
    since = max((r.get('unparked') or '' for r in runs), default='')
    ended = [r for r in runs if r.get('ended') and not lifecycle.quota_exhausted(r)
             and (r.get('started') or '') >= since]
    return max(ended, key=lambda r: r.get('started') or '') if ended else None


def own_shas(text):
    """The shas the REPORT itself says the run made: every sha of its ``pushed:`` field, and the
    leading sha of each ``commits:`` entry (``<sha> <subject>``; a subject may name the evidence
    — "already landed in <sha>" — and is not read)."""
    rep = report_mod.parse(text or '')
    out = relaunch.SHA_RE.findall(rep.get('pushed') or '')
    for part in re.split(r'[\n;,]', rep.get('commits') or ''):
        m = LEAD_SHA_RE.match(part)
        if m:
            out.append(m.group(1))
    return out


def _is_ancestor(repo, sha, ref):
    """True/False, or ``None`` when git could not tell (:func:`asf.gitops.is_ancestor`) — every
    caller below reads ``None`` the way that closes nothing."""
    if not (sha and ref):
        return False
    return gitops.is_ancestor(repo, sha, ref)


def trunk_sha(repo, main, run, text, item='', writes=(), prs=()):
    """The first commit ``text`` names that is on ``origin/<main>`` and is not the run's own
    work: not its launch head, not a sha its REPORT's ``pushed:``/``commits:`` name
    (:func:`own_shas`), not a commit between its launch head and the head it pushed, and not a
    trunk commit whose message names the run's branch (its merge) — and that is attributable to
    ``item`` (:func:`asf.workers.landing.attributable`: named by it, its PR's merge, or covering its
    ``writes:``). '' when there is none — a session whose own output landed (a reshape's split), or
    one naming the trunk head its branch merged in, proves nothing about the Task's work."""
    if not repo or not text:
        return ''
    launch = run.get('launch_head') or ''
    branch = run.get('branch') or ''
    skip = {s[:7] for s in own_shas(text) + [launch] if s}
    pushed = next(iter(relaunch.SHA_RE.findall(report_mod.parse(text).get('pushed') or '')), '')
    for sha in dict.fromkeys(relaunch.SHA_RE.findall(text)):
        if sha[:7] in skip or not _is_ancestor(repo, sha, f'origin/{main}'):
            continue
        if pushed and _is_ancestor(repo, sha, pushed) and not _is_ancestor(repo, sha, launch):
            continue  # a commit the run itself made
        if branch:
            body = gitops.log1(repo, sha, '%B')
            if body is None or branch in body:
                continue  # the trunk's merge of the run's own branch (a merge queue's commit),
                # or a message git could not read: unknown is never evidence
        if not landing.attributable(repo, main, sha, item or run.get('item') or '', writes, prs):
            continue  # merely an ancestor of the trunk: another item's commit, the head merged in
        return sha
    return ''


def evidence(path, item, repo, main='main', writes=(), ask_gh=True):
    """``(sha, run, claim)`` when ``item``'s work is verified on the trunk, else None: its newest
    ended run's REPORT ends ``status: done`` (:func:`asf.workers.relaunch.terminal`) and names a
    commit on ``origin/<main>`` that is not the run's own work (:func:`trunk_sha`), and the run's
    branch holds nothing past the trunk (:func:`unlanded`), nor does any other branch of the item's
    runs or an open PR naming it (:func:`asf.workers.landing.open_work`). A run already closed
    here is no new evidence."""
    if not repo or not item:
        return None
    run = newest_ended(path, item)
    # a run already closed here is no new evidence; a run the lane marked harvested is not
    # enough to skip — a plan or spec lane's merge lands its document, never its item
    if run is None or run.get('trunk_closed'):
        return None
    text = relaunch._result_text(run)
    claim = relaunch.terminal(text)
    if not claim.startswith(DONE):
        return None
    sha = trunk_sha(repo, main, run, text, item, writes, landing.run_prs(path, item))
    if not sha or unlanded(repo, main, run.get('branch')):
        return None
    if lifecycle.voided_sha(path, item, sha):
        return None  # the operator voided that landing (`asf reset`): it closes nothing
    work = landing.open_work(repo, main, path, item, [run.get('branch')], ask_gh=ask_gh)
    if work is None:
        return Unknown('the open PRs could not be read')  # never a close on an unknown
    if work:
        return None  # the item's own work is unmerged: nothing on the trunk closes it
    return full_sha(repo, sha) or sha, run, claim


def close(product, job, sha, why):
    """Close ``job``'s latest run on ``sha``: the landing mark the ingest reads as the item's
    merge fact, the reason beside it, and its pending correction (a park included) cleared."""
    pool_mod.update_session(product, job, harvested=sha, trunk_closed=why, correction=None)


def closes_before_launch(product, row_kind, item, out=print, dry_run=False):
    """True when a ``row_kind`` row on ``item`` is not launched because the item's work is
    verified on the trunk (:func:`evidence`); the run is closed on it (never in ``dry_run``)."""
    if row_kind not in KINDS or not getattr(product, 'repo_dir', None):
        return False
    path = pool_mod.sessions_path(product)
    try:
        hit = evidence(path, item, product.repo_dir, product.main,
                       landing.item_writes(product, item))
    except Exception as e:  # noqa: BLE001 — the check never blocks a wave by failing
        out(f'trunk: evidence check failed for {item} — {e}')
        return False
    if isinstance(hit, Unknown):
        # the evidence would stop this launch, and the open PRs could not be read: neither a
        # launch nor a close this tick
        verb = 'would wait' if dry_run else 'waits   '
        out(f'{verb} {row_kind}-{item.lower():<17} {item:<10} — {WAITS_UNKNOWN}: {hit.why}')
        return True
    if not hit:
        return False
    sha, run, claim = hit
    why = (f'already on origin/{product.main} at {sha[:9]} (verified), per {run.get("job")}: '
           f'{claim[:200]}')
    if dry_run:
        out(f'would close {row_kind}-{item.lower():<17} {item:<10} — {why}')
        return True
    close(product, run['job'], sha, why)
    out(f'closed   {row_kind}-{item.lower():<17} {item:<10} — not launched: {why}')
    return True


def parked(path):
    """``[(item, job, corr)]`` for every item whose pending correction is a park."""
    out = []
    for item in sorted(lifecycle.corrections(path)):
        held = [(r, lifecycle.pending_correction(r, path)) for r in lifecycle.item_runs(path, item)]
        held = [(r, c) for r, c in held if c]
        if not held:
            continue
        run, corr = max(held, key=lambda rc: rc[1].get('at') or '')
        if corr.get('parked'):
            out.append((item, run.get('job'), corr))
    return out


def close_parked(product, out=print, dry_run=False):
    """Close every parked item whose park reason carries verified trunk evidence
    (:func:`asf.workers.relaunch.landed_in`), re-verified now by :func:`evidence` (the sha on
    ``origin/<main>``, not the run's own work, its branch holding nothing past it). Returns the
    items."""
    repo = getattr(product, 'repo_dir', None)
    if not repo:
        return []
    path = pool_mod.sessions_path(product)
    done = []
    for item, job, corr in parked(path):
        if not relaunch.landed_in(corr.get('reason') or corr.get('text')):
            continue
        hit = evidence(path, item, repo, product.main, landing.item_writes(product, item))
        if not hit:
            continue
        sha, run, _claim = hit
        why = f'parked on verified trunk evidence: on origin/{product.main} at {sha[:9]}'
        if dry_run:
            out(f'would close {job:<24} {item:<10} — {why}')
        else:
            close(product, run['job'], sha, why)
            if run['job'] != job:
                pool_mod.update_session(product, job, correction=None)
            out(f'closed   {job:<24} {item:<10} — {why} (landed: {sha[:9]})')
        done.append(item)
    return done
