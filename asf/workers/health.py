"""asf.workers.health — reconcile the session ledger with what is actually running.

For every live session in ``sessions.jsonl``:

* its log's last line is a result → ended, ``end_reason: finished`` — but only once the job's
  branch is actually pushed (``origin/<branch>`` exists and contains the worktree's HEAD); an
  ``ok`` result that never pushed ends ``failed: not pushed: <n> uncommitted file(s), <m>
  unpushed commit(s)`` instead, so the item it worked stays open rather than looking done forever
  with nothing for harvest to land (B-0051);
* its pid is dead and there is no result → ended, ``end_reason: dead pid``;
* a ``dead pid`` session whose log later carries a result → ``re-judged`` finished/failed, under
  the same push rule (B-0028, B-0051);
* a run that says ok with a clean tree but commits origin lacks — spawn rebased its branch onto
  the trunk before the session started, the session finished a conflicted rebase, or it never
  pushed — is **published by the factory** first (``--force-with-lease`` against the tip the
  evidence saw when the branch is on origin, a plain push when not) and judged again, so a
  session never needs the force the rules forbid (B-0056);
* a run ended ``failed: not pushed`` is held like a red gate — a ``correction`` on the run and a
  round on the item — so the feeder's FIX → CORRECT row sends the next session back to the same
  worktree to commit and push what is there (B-0051, B-0052).

Then the worktrees under ``~/.ASF/state/<product>/worktrees/``: one with no session at all is an
``orphan``; one whose session has ended is a reap candidate. With ``fix=True`` a worktree is
removed under any of three rules:

* the session is ``harvested`` — harvest lands a rebased tip from its own throwaway worktree and
  deletes the remote branch, so this worktree's HEAD never shows up on origin; the landing itself
  (a sha on the ledger) is the evidence, so it is reaped regardless of what ``pushed()`` would say
  (B-0049);
* the session finished (or there is none), its pid is dead, the tree is clean, HEAD is already on
  the pushed branch, and the branch carries at least one commit of its own that is contained in
  ``origin/<main>`` (fast-forwarded) — a fresh branch with no commits is an ancestor of the trunk
  too, and that is "opening", never "merged" (B-0019);
* any other ended session (stopped by an operator, a dead pid never re-judged, …) whose worktree
  has no commits ahead of ``origin/<main>`` and no uncommitted changes — there is nothing in it to
  lose (B-0025, B-0049).

Then the checkout's own lane branches (B-0063): a local branch no worktree holds whose work is
on the trunk, or whose run the lane landed or archived, is ``pruned`` with ``fix`` (``stale``
without); one with work the trunk lacks is named ``stray`` and kept.

A live session whose worktree has no commits yet is listed ``opening``. Anything else is kept and
listed with why. A reap also releases the job's ``BACKLOG_ID_RANGE`` reservation (B-0007) —
nothing can mint against it once the worktree is gone, and prints ``reaped <job> (<what landed, or
empty>)``.
"""
import json
import os
import re
import subprocess

from asf import gitpush, refguard
from asf.workers import cloud
from asf.workers import headroom
from asf.workers import observe
from asf.workers import pool as pool_mod
from asf.workers import report as report_mod
from asf.workers import runtime as runtime_mod
from asf.workers import lifecycle
from asf.workers import spawn as spawn_mod

#: A run's ``end_reason`` names the branch-not-pushed failure two ways: git evidence
#: (``lifecycle.push_gap``, "not pushed: …") and a session's own honest report ("pushed: no",
#: ``report.UNPUSHED`` = "unpushed work"). Both must be held for correction the same way, or an
#: honest report is a dead end nobody comes back to (B-0075).
UNPUSHED_REASON_PREFIXES = ('failed: not pushed', f'failed: {report_mod.UNPUSHED}')


pid_alive = lifecycle.pid_alive


def record_items(product):
    """``{id: card}`` from the product's record clone's ``index.json`` — removed cards included,
    since a removed card is exactly what :func:`asf.workers.lifecycle.closed_state` must see — or
    None when there is no index to read."""
    from asf.tick import shadow
    path = os.path.join(shadow.record_dir(product), 'index.json')
    try:
        with open(path, encoding='utf-8') as f:
            index = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    raw = index.get('items') if isinstance(index.get('items'), dict) else index
    return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else None


def alive_for(product, runs, session_source=None):
    """``callable(pid) -> bool`` (F-0076 D11): a run is alive only while an observed session
    still sits at its pid carrying that run's own ``session`` id — falls back to
    :func:`pid_alive` when observation is unreadable (D10)."""
    cfg = spawn_mod.load_cfg()
    observed, why = observe.read(cfg, pool_mod.accounts_from_config(cfg), source=session_source)
    if why:
        return pid_alive
    return observe.identity_alive(observed, runs)


def _git(args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)


def pushed(worktree, branch):
    """(ok, why): clean tree, and HEAD contained in the remote's ``branch``."""
    if not branch:
        head = _git(['rev-parse', '--abbrev-ref', 'HEAD'], worktree)
        branch = head.stdout.strip() if head.returncode == 0 else ''
    if not branch or branch == 'HEAD':
        return False, 'no branch'
    st = _git(['status', '--porcelain'], worktree)
    if st.returncode != 0:
        return False, 'not a git worktree'
    if st.stdout.strip():
        return False, 'uncommitted changes'
    ls = _git(['ls-remote', '--heads', 'origin', branch], worktree)
    remote = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ''
    if not remote:
        return False, 'branch not pushed'
    if _git(['merge-base', '--is-ancestor', 'HEAD', remote], worktree).returncode != 0:
        return False, 'local commits not pushed'
    return True, ''


def push_gap(worktree, branch, main):
    """(ok, detail): a session's own branch, actually on origin and holding its HEAD. ``detail``
    counts what is missing — uncommitted files, and the session's own commits whose patch is not
    on the branch's remote (or, when the branch was never pushed at all, the commits above
    ``origin/<main>``) — as ``not pushed: <n> uncommitted file(s), <m> unpushed commit(s)``
    (B-0051: a result that says ok is not "finished" until this is (0, 0)). The count is
    :func:`asf.workers.lifecycle.unpushed_commits`, by patch and above the trunk, so a branch
    harvest rebased after the session pushed it is not held "unpushed" forever (B-0053)."""
    st = _git(['status', '--porcelain'], worktree)
    n = len([line for line in st.stdout.splitlines() if line.strip()]) if st.returncode == 0 else 0
    remote = ''
    if branch:
        ls = _git(['ls-remote', '--heads', 'origin', branch], worktree)
        remote = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ''
    m = lifecycle.unpushed_commits(worktree, remote, main)
    if n == 0 and m == 0 and remote:
        return True, ''
    return False, f'not pushed: {n} uncommitted file(s), {m} unpushed commit(s)'


def result_reason(worktree, branch, main, rec):
    """'finished' only when the result says ok AND the branch is actually pushed — a session
    that exits clean but never pushes must not read as done, or the item it was working on is
    blocked forever (health says finished, harvest sees nothing to land) (B-0051)."""
    if not runtime_mod.result_ok(rec):
        sig = runtime_mod.failure_reason(rec)
        return f'failed: {sig}' if sig else 'failed'
    ok, why = push_gap(worktree, branch, main)
    return 'finished' if ok else f'failed: {why}'


def has_commits(worktree, branch):
    """True when the branch was ever committed to: its reflog holds more than its creation.

    A branch cut from the trunk and never committed to sits on a commit the trunk already
    contains, so ancestry alone cannot tell it from a landed one. Unknown counts as "no".
    """
    if not branch:
        head = _git(['rev-parse', '--abbrev-ref', 'HEAD'], worktree)
        branch = head.stdout.strip() if head.returncode == 0 else ''
    if not branch or branch == 'HEAD':
        return False
    log = _git(['reflog', 'show', '--format=%gs', f'refs/heads/{branch}'], worktree)
    if log.returncode != 0:
        return False
    return any(not line.startswith('branch: Created from')
               for line in log.stdout.splitlines() if line.strip())


def in_trunk(worktree, main):
    return _git(['merge-base', '--is-ancestor', 'HEAD', f'origin/{main}'], worktree).returncode == 0


def remove_worktree(product, path, branch=None):
    """The worktree out of git and into the trash (a tree with changes is refused), deleted in
    the background (:mod:`asf.workers.trash`); then its ``branch``, when given, deleted."""
    from asf import env
    from asf.workers import trash
    ok, _ = trash.discard(product.repo_dir, env.state_dir(product), path)
    if not ok:
        return False
    if branch:
        _git(['branch', '-D', branch], product.repo_dir)
    return True


def worktree_empty(worktree, main):
    """No commits ahead of ``origin/<main>`` and no uncommitted changes: nothing here that a
    reap would lose (B-0025, B-0049)."""
    st = _git(['status', '--porcelain'], worktree)
    return st.returncode == 0 and not st.stdout.strip() and in_trunk(worktree, main)


def publish_gap(product, run, ev, reason, alive=pid_alive):
    """B-0056: a run judged ``failed: not pushed`` with a clean tree and commits origin lacks —
    a rebase the factory started or the session finished, or work committed and never pushed —
    is published by the factory (:func:`asf.workers.lifecycle.publish`), then judged again.
    Uncommitted files of an ok run are committed first (:func:`asf.workers.lifecycle.commit_leftovers`,
    B-0094); a commit the repo's hooks refuse stays a hold. Returns
    ``(reason, evidence, line)``; ``line`` is None when nothing was attempted."""
    wt, branch = run.get('worktree'), run.get('branch')
    result_ok = reason == lifecycle.FINISHED or (reason or '').startswith(UNPUSHED_REASON_PREFIXES)
    if not result_ok or not branch:
        return reason, ev, None
    if not wt or not os.path.isdir(wt):
        return reason, ev, None
    lines = []
    if ev.uncommitted:
        ok, line = lifecycle.commit_leftovers(wt, branch)  # B-0094
        if not ok:
            return reason, ev, line
        lines.append(line)
        ev = lifecycle.gather(product, run, alive=alive, worktree=wt)
    if not (ev.unpushed or (ev.remote_sha and not ev.head_on_remote)):
        return reason, ev, '; '.join(lines) or None
    ok, line = lifecycle.publish(wt, branch, ev.remote_sha, main=product.main,
                                 protected=refguard.listed(product.conventions),
                                 push_timeout_s=gitpush.push_timeout(product.conventions))
    if not ok:
        return reason, ev, '; '.join(lines + [line])
    ev = lifecycle.gather(product, run, alive=alive, worktree=wt)
    landing = lifecycle.lands(run, pool_mod.sessions_path(product))
    return lifecycle.judge(run, ev, landing=landing), ev, '; '.join(lines + [line])


#: a prose `pushed:` line that opens with one of these says the same as the typed `pushed: no`
PUSHED_NO_RE = re.compile(r'^\s*(no|none|not pushed|unpushed)\b', re.I)


def push_retry(ev, reason, line):
    """``(class, detail)`` when an unpushed run's push failed on the network or was refused by
    the repo's hook — read off its REPORT's ``why:`` field (when ``pushed: no``), or, when the
    result carries no fenced report at all, the prose ``pushed:`` line it stands in for — and
    the factory's own publish line — else None (:func:`asf.workers.lifecycle.push_failure`)."""
    if not (reason or '').startswith(UNPUSHED_REASON_PREFIXES):
        return None
    result_text = str((ev.result or {}).get('result') or '')
    said = ''
    if report_mod.fence(result_text) is not None:
        try:
            rep = report_mod.typed(result_text, None)
        except report_mod.ReportError:
            rep = {}
        if rep.get('pushed') == 'no':
            said = rep.get('why') or ''
    else:
        prose_pushed = report_mod._prose(result_text).get('pushed') or ''
        if PUSHED_NO_RE.match(prose_pushed):
            said = prose_pushed
    m = re.search(r'\bpublish \S+ refused: (.*)', line or '')  # the factory's own push
    published = m.group(1) if m else ''
    for text in (published, said):
        cls = lifecycle.push_failure(text)
        if cls:
            detail = ' '.join(f'{said} {line or ""}'.split()) or text
            return cls, detail[:600]
    return None


def _lane_branches(product):
    """Every local branch under a lane prefix of the product's checkout, with the worktree that
    holds it (or None)."""
    repo = product.repo_dir
    prefixes = product.conventions.all_prefixes() if hasattr(product.conventions, 'all_prefixes') else ()
    held = {}
    path = None
    for line in _git(['worktree', 'list', '--porcelain'], repo).stdout.splitlines():
        if line.startswith('worktree '):
            path = line[len('worktree '):]
        elif line.startswith('branch refs/heads/'):
            held[line[len('branch refs/heads/'):]] = path
    out = []
    for b in _git(['branch', '--list', '--format=%(refname:short)'], repo).stdout.splitlines():
        b = b.strip()
        if b and b != product.main and any(b.startswith(p) for p in prefixes):
            out.append((b, held.get(b)))
    return out


def _branch_on_trunk(repo, main, branch):
    """The branch's patches are all on the trunk (``git cherry``), or every file it touched
    reads the same on both — a branch landed by a rebase or by another commit."""
    cherry = _git(['cherry', f'origin/{main}', branch], repo)
    if cherry.returncode == 0 and not any(l.startswith('+') for l in cherry.stdout.splitlines()):
        return True
    files = [l for l in _git(['diff', '--name-only', f'origin/{main}...{branch}'], repo).stdout.splitlines() if l.strip()]
    return all(_git(['diff', '--quiet', f'origin/{main}', branch, '--', f], repo).returncode == 0
               for f in files)


def prune_branches(product, registry, fix=False):
    """B-0063: the local lane branches of the product's checkout that no worktree holds. One
    whose work is on the trunk, or whose run the lane has landed or archived (``harvested`` on
    the registry), is stale: deleted with ``fix``, else named ``stale``; one with work the trunk
    lacks and no landing is named ``stray`` and kept — nothing is deleted unseen. Returns
    ``[(branch, what, detail)]``."""
    repo, main = product.repo_dir, product.main
    landed = {b for b, r in lifecycle.by_branch(registry).items() if r.get('harvested')}
    found = []
    for branch, wt in _lane_branches(product):
        if wt:
            continue
        if branch in landed:
            why = f'landed by the lane ({lifecycle.by_branch(registry)[branch]["harvested"]})'
        elif _branch_on_trunk(repo, main, branch):
            why = f'every change on origin/{main}'
        else:
            n = len([l for l in _git(['cherry', f'origin/{main}', branch], repo).stdout.splitlines() if l.startswith('+')])
            found.append((branch, 'stray', f'{n} patch(es) not on origin/{main}, never landed — no worktree'))
            continue
        if fix and _git(['branch', '-D', branch], repo).returncode == 0:
            found.append((branch, 'pruned', why))
        else:
            found.append((branch, 'stale', why))
    return found


def health(product, fix=False, alive=None, session_source=None, out=print, items=None):
    """Returns a list of ``(job, what, detail)`` transitions/findings. Every judgement is
    :mod:`asf.workers.lifecycle`'s: :func:`~asf.workers.lifecycle.judge` for the ``ended`` line,
    :func:`~asf.workers.lifecycle.reap_verdict` for the worktrees; this function gathers the
    evidence, writes the one recorded transition and prints.

    ``items`` is the record's index (``{id: card}``, removed cards included; default: the
    product's record clone). A run whose item is removed or done is ended and reaped, never held:
    no correction is written on it, and one still pending is dropped (``released``) — a session
    sent back to a card nobody wants work on has nothing to do, and its row only waits."""
    found = []
    registry = pool_mod.sessions_path(product)
    if any(cloud.is_cloud(s) and not s.get('ended')
           for s in pool_mod.load_sessions(product).values()):
        try:  # the cloud lane's runs first: their state is what every check below reads
            cloud.sync(product, out=out)
        except Exception as e:  # noqa: BLE001 — a failed sync leaves the runs working
            out(f'cloud: sync failed — {(str(e) or type(e).__name__).splitlines()[0]}')
    sessions = pool_mod.load_sessions(product)
    if items is None:
        items = record_items(product)
    if alive is None:
        alive = alive_for(product, sessions.values(), session_source)
    for job, s in sessions.items():
        closed = lifecycle.closed_state(items, s.get('item'))
        if closed and s.get('ended') and lifecycle.pending_correction(s, registry):
            pool_mod.update_session(product, job, correction=None)
            s.pop('correction', None)
            found.append((job, 'released', f'{s.get("item")} is {closed}: no correction'))
        if s.get('ended'):
            landed_sha = lifecycle.empty_on_a_landed_lane(registry, s)
            if landed_sha:
                # an empty run on a branch an earlier run already landed (a squash-merged lane
                # PR): its work is on the trunk — landed, with no correction left to answer
                pool_mod.update_session(product, job, harvested=landed_sha, correction=None)
                s.update(harvested=landed_sha)
                s.pop('correction', None)
                found.append((job, 'landed', f'its branch landed at {landed_sha[:9]}: '
                                             'nothing to push'))
                continue
            if s.get('end_reason') == lifecycle.STOPPED and not s.get('correction'):
                ev = lifecycle.gather(product, s, alive=alive)
                if lifecycle.pushed_after_stop(s, ev):
                    fields, line = lifecycle.hold(registry, s, lifecycle.PUSHED_AFTER_STOP,
                                                  lifecycle.PUSHED_AFTER_STOP, pool_mod.now_iso())
                    pool_mod.update_session(product, job, **fields)
                    found.append((job, 'held', line.split(': ', 1)[1]))
            # a `dead pid` judgement is revisited: the result may have landed after the check,
            # or a correction may have finished the run (B-0028)
            if s.get('end_reason') == lifecycle.DEAD_PID and not s.get('harvested'):
                ev = lifecycle.gather(product, s, alive=alive)
                if ev.result is not None:
                    reason = lifecycle.judge(s, ev, landing=lifecycle.lands(s, registry))
                    reason, ev, line = publish_gap(product, s, ev, reason, alive)
                    if line:
                        found.append((job, 'published', line))
                    ok = reason == lifecycle.FINISHED
                    pool_mod.update_session(product, job, end_reason=reason, rc=0 if ok else 1)
                    s.update(end_reason=reason, rc=0 if ok else 1)
                    found.append((job, 're-judged', reason))
                    if lifecycle.quota_exhausted(s):
                        found.append((job, 'quota', headroom.note_exhausted(product, s, ev.result)))
            continue
        ev = lifecycle.gather(product, s, alive=alive)
        reason = lifecycle.judge(s, ev, landing=lifecycle.lands(s, registry))
        if reason is None:
            continue
        reason, ev, line = publish_gap(product, s, ev, reason, alive)  # B-0056
        if line:
            found.append((job, 'published', line))
        retry = push_retry(ev, reason, line)
        if retry and retry[0] == lifecycle.NETWORK_ERROR:  # B-0097: a blip — retried, no round
            found.append((job, 'retry', f'{retry[0]}: {retry[1]} — published again next pass'))
            continue
        if retry:  # the repo's own pre-push hook refused it: its output, no round spent
            reason = f'failed: {retry[0]}: {retry[1]}'
        now = pool_mod.now_iso()
        pool_mod.update_session(product, job, ended=now, end_reason=reason,
                                runtime_session=runtime_mod.runtime_session(s.get('log')) or None)
        s.update(ended=now, end_reason=reason)
        found.append((job, 'ended', reason))
        if lifecycle.quota_exhausted(s):
            # the account's window, not the work: its account stops until the reset, and the
            # item relaunches — no hold, no round (asf.workers.headroom)
            found.append((job, 'quota', headroom.note_exhausted(product, s, ev.result)))
            continue
        if closed:
            continue  # nothing to send back: the worktree is reaped below when it is empty
        if retry:
            text = (f'the push was refused by the repo\'s own hook — {retry[1]} — fix what it '
                    f'names, commit, and push again')
            pool_mod.update_session(product, job, correction={
                'kind': lifecycle.HOOK_REFUSED, 'text': text, 'at': now})
            found.append((job, 'held', f'{s.get("branch") or job}: {text} (no round spent)'))
        elif reason.startswith(UNPUSHED_REASON_PREFIXES):
            # the run's own work is the correction's input: the next session on the branch
            # commits and pushes it, or says why not (B-0051, B-0052)
            text = lifecycle.unpushed_text(reason)
            if lifecycle.rebase_conflict(line):  # the factory's rebase conflicted: files named
                text = lifecycle.rebase_conflict_text(s.get('branch') or job, line)
                fields, line = lifecycle.rebase_conflict_hold(registry, s, text, now)
                pool_mod.update_session(product, job, **fields)
                found.append((job, 'held', line.split(': ', 1)[1]))
                continue
            if lifecycle.stale_head(line):  # origin holds commits this head lacks: a rebase
                text = lifecycle.stale_head_text(s.get('branch') or job, line)
            fields, line = lifecycle.hold(registry, s, lifecycle.UNPUSHED, text, now)
            pool_mod.update_session(product, job, **fields)
            found.append((job, 'held', line.split(': ', 1)[1]))
        elif reason == f'failed: {lifecycle.EMPTY_BRANCH}' and lifecycle.landed_earlier(registry, s):
            landed_sha = lifecycle.landed_earlier(registry, s)
            pool_mod.update_session(product, job, harvested=landed_sha)
            s.update(harvested=landed_sha)
            found.append((job, 'landed', f'its branch landed at {landed_sha[:9]}: nothing to push'))
        elif reason == f'failed: {lifecycle.EMPTY_BRANCH}':
            # a pushed branch with nothing on it: the same loop, sent back to commit real work
            # or say why there is none (B-0076)
            fields, line = lifecycle.hold(registry, s, lifecycle.UNPUSHED,
                                          lifecycle.empty_branch_text(), now)
            pool_mod.update_session(product, job, **fields)
            found.append((job, 'held', line.split(': ', 1)[1]))
    _git(['fetch', '-q', 'origin', product.main], product.repo_dir)
    wdir = spawn_mod.worktrees_dir(product)
    owners = lifecycle.by_worktree(registry)
    heads = lifecycle.RemoteHeads()  # one ls-remote for the pass; nothing below pushes
    for name in sorted(os.listdir(wdir)):
        path = os.path.join(wdir, name)
        if not os.path.isdir(path):
            continue
        s = owners.get(lifecycle.path_key(path)) or sessions.get(name)
        ev = lifecycle.gather(product, s or {}, alive=alive, worktree=path, heads=heads)
        if s is None and not ev.remote_sha:
            # an orphan carries no branch on its record: read the one checked out
            ev = lifecycle.gather(product, {'branch': _head_branch(path)}, alive=alive,
                                  worktree=path, heads=heads)
        what, detail = lifecycle.reap_verdict(s, ev, product.main, alive)
        if what is None:
            continue
        job = s.get('job', name) if s else name
        # a landed or empty worktree takes its local branch with it: nothing in it is anywhere
        # else, and a branch left behind blocks the next launch on it (`worktree add -b`)
        gone = (s or {}).get('branch') if (lifecycle.landed(s) or detail == 'empty') else None
        if what == 'reapable' and fix and remove_worktree(product, path, branch=gone):
            spawn_mod.release_id_range(product, job)
            found.append((name, 'reaped', detail))
        else:
            found.append((name, what, detail))
    found.extend(prune_branches(product, registry, fix=fix))  # B-0063
    for job, what, detail in found:
        if what in ('reaped', 'pruned'):
            out(f'{what} {job} ({detail})')
        elif what == 'published':
            out(detail)
        else:
            out(f'{what:<9} {job:<24} {detail}')
    if not found:
        out('health: clean')
    return found


def _head_branch(worktree):
    head = _git(['rev-parse', '--abbrev-ref', 'HEAD'], worktree)
    b = head.stdout.strip() if head.returncode == 0 else ''
    return b if b and b != 'HEAD' else None
