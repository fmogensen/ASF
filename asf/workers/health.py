"""asf.workers.health — reconcile the session ledger with what is actually running.

For every live session in ``sessions.jsonl``:

* its log's last line is a result → ended, ``end_reason: finished`` — but only once the job's
  branch is actually pushed (``origin/<branch>`` exists and contains the worktree's HEAD); an
  ``ok`` result that never pushed ends ``failed: not pushed: <n> uncommitted file(s), <m>
  unpushed commit(s)`` instead, so the item it worked stays open rather than looking done forever
  with nothing for harvest to land (B-0051);
* its pid is dead and there is no result → ended, ``end_reason: dead pid``;
* a ``dead pid`` session whose log later carries a result → ``re-judged`` finished/failed, under
  the same push rule (B-0028, B-0051).

Then the worktrees under ``~/.ASF/state/<product>/worktrees/``: one with no session at all is an
``orphan``; one whose session has ended is a reap candidate. With ``fix=True`` a worktree is
removed under any of four rules:

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
  lose (B-0025, B-0049);
* an ``orphan`` — a worktree with no session line at all, left when a launch died between
  ``make_worktree`` and the ledger write — whose tree is likewise empty. No ``pushed()`` check can
  ever clear an orphan (there is no branch to have pushed), so without this rule it is kept
  forever and ``spawn`` refuses its job for good (B-0025).

A live session whose worktree has no commits yet is listed ``opening``. Anything else is kept and
listed with why. A reap also releases the job's ``BACKLOG_ID_RANGE`` reservation (B-0007) —
nothing can mint against it once the worktree is gone, and prints ``reaped <job> (<what landed, or
empty>)``.
"""
import os
import subprocess

from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


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
    counts what is missing — uncommitted files, and commits not on the branch's remote (or, when
    the branch was never pushed at all, not on ``origin/<main>``) — as ``not pushed: <n>
    uncommitted file(s), <m> unpushed commit(s)`` (B-0051: a result that says ok is not "finished"
    until this is (0, 0))."""
    st = _git(['status', '--porcelain'], worktree)
    n = len([line for line in st.stdout.splitlines() if line.strip()]) if st.returncode == 0 else 0
    remote = ''
    if branch:
        ls = _git(['ls-remote', '--heads', 'origin', branch], worktree)
        remote = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ''
    base = f'origin/{branch}' if remote else f'origin/{main}'
    rc = _git(['rev-list', '--count', f'{base}..HEAD'], worktree)
    m = int(rc.stdout.strip()) if rc.returncode == 0 and rc.stdout.strip().isdigit() else 0
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
    p = _git(['worktree', 'remove', path], product.repo_dir)
    if p.returncode != 0:
        return False
    if branch:
        _git(['branch', '-D', branch], product.repo_dir)
    return True


def worktree_branch(worktree):
    """The branch a worktree sits on, or ''. An orphan has no ledger line to read it from, and
    leaving its (empty) branch behind would still fail the next ``worktree add -b`` (B-0025)."""
    p = _git(['rev-parse', '--abbrev-ref', 'HEAD'], worktree)
    name = p.stdout.strip() if p.returncode == 0 else ''
    return '' if name == 'HEAD' else name


def worktree_empty(worktree, main):
    """No commits ahead of ``origin/<main>`` and no uncommitted changes: nothing here that a
    reap would lose (B-0025, B-0049)."""
    st = _git(['status', '--porcelain'], worktree)
    return st.returncode == 0 and not st.stdout.strip() and in_trunk(worktree, main)


def health(product, fix=False, alive=pid_alive, out=print):
    """Returns a list of ``(job, what, detail)`` transitions/findings."""
    found = []
    sessions = pool_mod.load_sessions(product)
    for job, s in sessions.items():
        if s.get('ended'):
            # a `dead pid` judgement is revisited: the result may have landed after the check,
            # or a correction may have finished the run (B-0028)
            if s.get('end_reason') == 'dead pid' and not s.get('harvested'):
                rec = runtime_mod.read_result(s.get('log'))
                if rec is not None:
                    reason = result_reason(s.get('worktree'), s.get('branch'), product.main, rec)
                    ok = reason == 'finished'
                    pool_mod.update_session(product, job, end_reason=reason, rc=0 if ok else 1)
                    s.update(end_reason=reason, rc=0 if ok else 1)
                    found.append((job, 're-judged', reason))
            continue
        rec = runtime_mod.read_result(s.get('log'))
        if rec is not None:
            reason = result_reason(s.get('worktree'), s.get('branch'), product.main, rec)
        elif not alive(s.get('pid')):
            reason = 'dead pid'
        else:
            continue
        pool_mod.update_session(product, job, ended=pool_mod.now_iso(), end_reason=reason)
        s.update(ended=True, end_reason=reason)
        found.append((job, 'ended', reason))
    _git(['fetch', '-q', 'origin', product.main], product.repo_dir)
    wdir = spawn_mod.worktrees_dir(product)
    for name in sorted(os.listdir(wdir)):
        path = os.path.join(wdir, name)
        if not os.path.isdir(path):
            continue
        s = sessions.get(name)
        if s is not None and not s.get('ended'):
            if not has_commits(path, s.get('branch')):
                found.append((name, 'opening', 'live session, no commits yet'))
            continue
        what = 'orphan' if s is None else 'ended'
        if s is not None and alive(s.get('pid')):
            found.append((name, 'keep', f'{what}: pid still alive'))
            continue
        if s is not None and s.get('harvested'):
            # harvest lands a rebased tip from its own throwaway worktree and deletes the
            # remote branch, so this worktree's HEAD never appears on origin — `pushed()`
            # would refuse it forever. The landing itself is the evidence (B-0049).
            detail = f'landed {s["harvested"]}'
            if fix and remove_worktree(product, path, branch=s.get('branch')):
                spawn_mod.release_id_range(product, name)
                found.append((name, 'reaped', detail))
            else:
                found.append((name, 'reapable', detail))
            continue
        if s is not None and s.get('end_reason') != 'finished':
            if worktree_empty(path, product.main):
                if fix and remove_worktree(product, path):
                    spawn_mod.release_id_range(product, name)
                    found.append((name, 'reaped', 'empty'))
                else:
                    found.append((name, 'reapable', 'empty'))
            else:
                found.append((name, 'keep', f'{what}: session {s.get("end_reason")}, not finished'))
            continue
        if (s is None and not has_commits(path, worktree_branch(path))
                and worktree_empty(path, product.main)):
            # an orphan that was never committed to has no branch anyone could push, so the
            # `pushed()` check below keeps it forever and spawn refuses its job forever. It
            # holds nothing to lose (B-0025). An orphan that *did* commit is not this case:
            # unlanded it is kept, landed it is reaped as 'orphan' by the rule below — being
            # contained in the trunk is what landing means, so `in_trunk` alone cannot tell
            # the two apart and `has_commits` is the discriminator (B-0019).
            if fix and remove_worktree(product, path, branch=worktree_branch(path)):
                spawn_mod.release_id_range(product, name)
                found.append((name, 'reaped', 'empty orphan'))
            else:
                found.append((name, 'reapable', 'empty orphan'))
            continue
        ok, why = pushed(path, (s or {}).get('branch'))
        if not ok:
            found.append((name, 'keep', f'{what}: {why}'))
        elif not has_commits(path, (s or {}).get('branch')):
            found.append((name, 'keep', f'{what}: no commits yet'))
        elif not in_trunk(path, product.main):
            found.append((name, 'keep', f'{what}: not in origin/{product.main}'))
        elif fix and remove_worktree(product, path):
            spawn_mod.release_id_range(product, name)
            found.append((name, 'reaped', what))
        else:
            found.append((name, 'reapable', what))
    for job, what, detail in found:
        if what == 'reaped':
            out(f'reaped {job} ({detail})')
        else:
            out(f'{what:<9} {job:<24} {detail}')
    if not found:
        out('health: clean')
    return found
