"""asf.workers.health — reconcile the session ledger with what is actually running.

For every live session in ``sessions.jsonl``:

* its log's last line is a result → ended, ``end_reason: finished`` (or ``failed``);
* its pid is dead and there is no result → ended, ``end_reason: dead pid``;
* a ``dead pid`` session whose log later carries a result → ``re-judged`` finished/failed (B-0028).

Then the worktrees under ``~/.ASF/state/<product>/worktrees/``: one with no session at all is an
``orphan``; one whose session has ended is a reap candidate. With ``fix=True`` a worktree is
removed only under the reap rule — the session finished (or there is none), its pid is dead,
the tree is clean, and HEAD is already on the pushed branch (fast-forwarded + pushed). Anything
else is kept and listed with why. The one exception (B-0025): a worktree whose session has ended
with a dead pid and whose branch has no commit beyond ``origin/<main>`` (and a clean tree) holds
nothing to lose, so it is reaped — worktree and branch — whether or not it was pushed; otherwise
the next ``spawn`` of that job would refuse ``worktree already exists``.
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


def remove_worktree(product, path):
    p = _git(['worktree', 'remove', path], product.repo_dir)
    return p.returncode == 0


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
                    ok = runtime_mod.result_ok(rec)
                    reason = 'finished' if ok else 'failed'
                    pool_mod.update_session(product, job, end_reason=reason, rc=0 if ok else 1)
                    s.update(end_reason=reason, rc=0 if ok else 1)
                    found.append((job, 're-judged', reason))
            continue
        rec = runtime_mod.read_result(s.get('log'))
        if rec is not None:
            reason = 'finished' if runtime_mod.result_ok(rec) else 'failed'
        elif not alive(s.get('pid')):
            reason = 'dead pid'
        else:
            continue
        pool_mod.update_session(product, job, ended=pool_mod.now_iso(), end_reason=reason)
        s.update(ended=True, end_reason=reason)
        found.append((job, 'ended', reason))
    wdir = spawn_mod.worktrees_dir(product)
    for name in sorted(os.listdir(wdir)):
        path = os.path.join(wdir, name)
        if not os.path.isdir(path):
            continue
        s = sessions.get(name)
        if s is not None and not s.get('ended'):
            continue
        what = 'orphan' if s is None else 'ended'
        if s is not None and alive(s.get('pid')):
            found.append((name, 'keep', f'{what}: pid still alive'))
            continue
        if s is not None and s.get('end_reason') != 'finished':
            ok, why = False, f'session {s.get("end_reason")}, not finished'
        else:
            ok, why = pushed(path, (s or {}).get('branch'))
        if not ok and s is not None and spawn_mod.unused_worktree(product, path, s.get('branch'))[0]:
            # B-0025: a dead session that committed nothing leaves nothing to lose
            if fix:
                spawn_mod.discard_worktree(product, path, s.get('branch'))
            found.append((name, 'reaped' if fix else 'reapable', f'{what}: nothing committed'))
        elif not ok:
            found.append((name, 'keep', f'{what}: {why}'))
        elif fix and remove_worktree(product, path):
            found.append((name, 'reaped', what))
        else:
            found.append((name, 'reapable', what))
    for job, what, detail in found:
        out(f'{what:<9} {job:<24} {detail}')
    if not found:
        out('health: clean')
    return found
