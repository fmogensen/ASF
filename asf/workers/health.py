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
import signal
import subprocess
import time

from asf import gitpush, refguard
from asf.evidence import review_store
from asf.record import replan as replan_mod
from asf.workers import account_auth
from asf.workers import cloud
from asf.workers import cloudpid
from asf.workers import headroom
from asf.workers import heartbeat
from asf.workers import observe
from asf.workers import pool as pool_mod
from asf.workers import pushlog
from asf.workers import refusals
from asf.workers import relaunch as relaunch_mod
from asf.workers import report as report_mod
from asf.workers import runtime as runtime_mod
from asf.workers import lifecycle
from asf.workers import spawn as spawn_mod

#: A run's ``end_reason`` names the branch-not-pushed failure two ways: git evidence
#: (``lifecycle.push_gap``, "not pushed: …") and a session's own honest report ("pushed: no",
#: ``report.UNPUSHED`` = "unpushed work"). Both must be held for correction the same way, or an
#: honest report is a dead end nobody comes back to (B-0075).
UNPUSHED_REASON_PREFIXES = ('failed: not pushed', f'failed: {report_mod.UNPUSHED}')


def ruling_ready(run):
    """B-0064's ruling, read as a publish instruction (F-0176): an ``adjudicate`` run whose REPORT
    carries a ``ruling:`` and claims no ``blocked_on:`` has declared its branch ready. The session
    cannot put it on origin — it has no push credential, and a rebased lane branch needs the force
    the rules forbid it (B-0056) — so the factory publishes the head the ruling ruled on, whatever
    the run's own ``end_reason`` class. Read off the log, not off git: no subprocess runs here."""
    if (run or {}).get('kind') != 'adjudicate':
        return False
    rec = runtime_mod.read_result((run or {}).get('log'))
    text = str(rec.get('result') or '') if isinstance(rec, dict) else ''
    return bool(report_mod.ruling(text)) and report_mod.ruling_fields(text)['blocked_on'] is None


pid_alive = lifecycle.pid_alive
#: the one number (:data:`asf.workers.lifecycle.LINGER_GRACE_S`) — see there for why
LINGER_GRACE_S = lifecycle.LINGER_GRACE_S
#: seconds between the SIGTERM and the SIGKILL of a lingering process
STOP_WAIT_S = 5


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


def _measurements(product, runs):
    """The three keyword measurements :func:`asf.workers.observe.liveness_for` asks for only
    when a pid is otherwise ``UNKNOWN`` (F-0234): ``is_ours`` (is the process there one of the
    factory's headless workers, off its command line), ``silent_min`` (the product's configured
    bound on a quiet log, or the default when there is no product to configure it) and
    ``silent_for`` (minutes since the run recorded at a pid last moved its own log)."""
    from asf.workers import stall as stall_mod
    silent_min = (stall_mod.silent_minutes(product) if product is not None
                  else stall_mod.DEFAULT_SILENT_MIN)
    log_by_pid = {}
    for run in runs:
        pid = run.get('pid')
        if pid is not None and pid not in log_by_pid:
            log_by_pid[pid] = run.get('log')

    def silent_for(pid):
        log = log_by_pid.get(pid)
        if not log:
            return None
        try:
            return (time.time() - os.path.getmtime(log)) / 60
        except OSError:
            return None

    return {'is_ours': lambda pid: is_print_worker(command_line(pid)),
            'silent_min': silent_min, 'silent_for': silent_for}


def _observe(product, runs, session_source=None):
    """One ``observe.read`` for :func:`alive_for` (and, later, a verdict callable) to share:
    ``(observed, why, kw)`` where ``kw`` is :func:`_measurements`'s dict — so a health pass pays
    for the tick's one ``ps axeww`` call once, not once per callable it builds."""
    cfg = spawn_mod.load_cfg()
    observed, why = observe.read(cfg, pool_mod.accounts_from_config(cfg), source=session_source)
    return observed, why, _measurements(product, runs)


def alive_for(product, runs, session_source=None):
    """``callable(pid) -> bool`` (F-0076 D11): a run is alive only while an observed session
    still sits at its pid carrying that run's own ``session`` id — falls back to
    :func:`pid_alive` when observation is unreadable (D10)."""
    observed, why, kw = _observe(product, runs, session_source)
    if why:
        return pid_alive
    return observe.identity_alive(observed, runs, **kw)


def liveness_for(product, runs, session_source=None):
    """``callable(pid) -> one of lifecycle.LIVENESS`` (F-0234): the class a run's pid answers to,
    on the same observation :func:`alive_for` reads. When the source is unreadable the fallback
    is the OS answer in the card's own vocabulary (PD6) — never ``UNKNOWN``, which would defer
    every run in the factory for as long as ``ps`` stays unreadable, and agrees exactly with
    :func:`alive_for`'s own ``pid_alive`` fallback so the two callables never disagree on a pid."""
    observed, why, kw = _observe(product, runs, session_source)
    if why:
        return lambda pid: lifecycle.ALIVE if pid_alive(pid) else lifecycle.GONE
    return observe.liveness_for(observed, runs, **kw)


def dead_census(product, days=14, now=None):
    """``{'days', 'runs', 'by_class': {word: n}, 'unclassified': n}`` over the registry's
    ``dead pid`` runs ended within the last ``days`` days (F-0234) — the ledger folded the way
    :func:`asf.workers.retention.census` folds branches (P17), every run of every job, not just
    each job's latest. ``unclassified`` counts a death with no ``dead_class``: a row written
    before this card landed, and not an error. ``by_refusal`` counts each death's last ASF
    refusal by kind (:func:`asf.workers.refusals.last`, F-0266 C15) — a second axis, never a
    second count of runs."""
    now = time.time() if now is None else now
    since = now - days * 86400
    by_class = {word: 0 for word in lifecycle.LIVENESS}
    by_refusal = {}
    unclassified = runs = 0
    for rs in lifecycle._folded(pool_mod.sessions_path(product)).view.values():
        for r in rs:
            if r.get('end_reason') != lifecycle.DEAD_PID:
                continue
            ended = cloud._parse_ts(r.get('ended'))
            if ended is None or ended < since:
                continue
            runs += 1
            cls = r.get('dead_class')
            if cls in by_class:
                by_class[cls] += 1
            else:
                unclassified += 1
            refusal = refusals.last(product, r.get('job'), r)
            if refusal is not None:
                by_refusal[refusal.kind] = by_refusal.get(refusal.kind, 0) + 1
    return {'days': days, 'runs': runs, 'by_class': {k: v for k, v in by_class.items() if v},
            'unclassified': unclassified, 'by_refusal': by_refusal}


def dead_census_line(data):
    """``(ok, 'dead sessions: 11 in 14 days — gone 6, reused 1, unknown 4')`` off a
    :func:`dead_census` dict. ``ok`` is False while any death in the window is ``unknown`` — the
    class this card exists to drive to zero, so a row that reads ok while it stands would hide
    the card's own remaining work. Formats the stored data; measures nothing."""
    days, runs = data['days'], data['runs']
    if not runs:
        return True, f'dead sessions: 0 in {days} days'
    by_class, unclassified = data.get('by_class') or {}, data.get('unclassified') or 0
    order = {word: i for i, word in enumerate(lifecycle.LIVENESS)}
    parts = [f'{cls} {n}' for cls, n in sorted(by_class.items(), key=lambda cn: order[cn[0]])]
    if unclassified:
        parts.append(f'unclassified {unclassified}')
    ok = not by_class.get(lifecycle.UNKNOWN)
    line = f"dead sessions: {runs} in {days} days — {', '.join(parts)}"
    by_refusal = data.get('by_refusal') or {}
    if by_refusal:  # what ASF itself refused them, most first (F-0266 C15)
        line += '; refusals: ' + ', '.join(
            f'{k} {n}' for k, n in sorted(by_refusal.items(), key=lambda kn: (-kn[1], kn[0])))
    return ok, line


def remote_retire(run):
    """Disable an ended ``claude-remote`` run's routine — :func:`asf.workers.remote.retire`, the
    one call :func:`asf.workers.cloud.stop` already makes (once per token, a refusal is False)."""
    from asf.workers import remote
    return remote.retire(run)


def command_line(pid):
    """The process ``pid``'s command line (``ps -o command=``), ``''`` when it is gone."""
    try:
        p = subprocess.run(['ps', '-o', 'command=', '-p', str(int(pid))],
                           capture_output=True, text=True)
    except (OSError, ValueError, TypeError):
        return ''
    return p.stdout.strip() if p.returncode == 0 else ''


def is_print_worker(cmd, binary=None):
    """``cmd`` is a headless worker the factory launches: the runtime binary as ``argv[0]``,
    ``-p``/``--print`` among its arguments. An interactive session never carries ``-p``."""
    argv = (cmd or '').split()
    binary = os.path.basename(binary or runtime_mod.DEFAULT_BINARY)
    return bool(argv) and os.path.basename(argv[0]) == binary and (
        '-p' in argv[1:] or '--print' in argv[1:])


def _exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def stop_process(pid, wait_s=STOP_WAIT_S):
    """SIGTERM, then SIGKILL when ``pid`` is still there after ``wait_s``. The signal that
    ended it (``'SIGTERM'`` | ``'SIGKILL'``), or ``'gone'`` when it had already exited."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return 'gone'
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not _exists(pid):
            return 'SIGTERM'
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return 'SIGTERM'
    return 'SIGKILL'


def settle_ended(product, sessions, now=None, fix=True, session_source=None, retire=None,
                 cmdline=None, wait_s=STOP_WAIT_S, grace_s=LINGER_GRACE_S):
    """T-0196: an ended run is finished, whatever its pid still answers. ``[(job, what, detail)]``.

    * a cloud run (``pid: actions:…`` / ``remote:…``) whose token the status file still reads
      ``working`` is settled (:func:`asf.workers.cloud.settle_ended`) — :func:`cloud.sync` never
      looks at an ended run again, so the token otherwise answers alive for ever; with ``fix`` a
      ``claude-remote`` run's routine is disabled too (:func:`remote_retire`) — ``settled``;
    * a local run ended at least ``grace_s`` ago whose process is still up is stopped (SIGTERM,
      then SIGKILL after ``wait_s``) — ``stopped`` — only when the process is provably the one
      the factory launched for it: the observed session at the run's pid carries the run's own
      ``ASF_SESSION``, and its command line is the runtime binary with ``-p``. A run with no
      ``session``, a process with another or no ``ASF_SESSION`` (an interactive session) or any
      other command line is never touched. Without ``fix`` it is only listed ``lingering``."""
    now = time.time() if now is None else now
    retire = retire or remote_retire
    found, local = [], []
    for job, run in sessions.items():
        pid = run.get('pid')
        if not run.get('ended') or not pid:
            continue
        if cloudpid.is_token(pid):
            status = cloud.settle_ended(run)
            if status is None:
                continue
            detail = f'{pid} {status}: run ended {run.get("end_reason") or ""}'.rstrip()
            if fix and pid.startswith(cloudpid.REMOTE_PREFIX):
                try:
                    ok = retire(run)
                except Exception:  # noqa: BLE001 — the token is settled either way
                    ok = False
                detail += '; routine disabled' if ok else '; routine left as it is'
            found.append((job, 'settled', detail))
            continue
        if not run.get('session') or not lifecycle.linger_spent(run, now, grace_s):
            continue
        try:
            local.append((job, run, int(pid)))
        except (TypeError, ValueError):
            continue
    if not local:
        return found
    cfg = spawn_mod.load_cfg()
    observed, why = observe.read(cfg, [], source=session_source)
    if why:
        return found
    by_pid = {}
    for o in observed:
        try:
            by_pid[int(o.pid)] = o
        except (TypeError, ValueError):
            continue
    cmdline = cmdline or command_line
    binary = (cfg.get('worker_pool') or {}).get('binary') or runtime_mod.DEFAULT_BINARY
    for job, run, pid in local:
        o = by_pid.get(pid)
        if o is None or o.owner != 'asf' or o.session != run.get('session'):
            continue
        if not is_print_worker(cmdline(pid), binary):
            continue
        mins = int((now - cloud._parse_ts(run['ended'])) // 60)
        if not fix:
            found.append((job, 'lingering', f'pid {pid}: ended {mins}m ago, still running'))
            continue
        how = stop_process(pid, wait_s)
        found.append((job, 'stopped', f'pid {pid} ({how}): ended {run.get("end_reason")} '
                                      f'{mins}m ago, its process still running'))
    return found


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
    remote = lifecycle.remote_head(worktree, branch)
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
    return lifecycle.unpublished(worktree, branch, main)


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


def account_fault(product, job, run, ev):
    """The ``found`` entry for a run its account cut short (:func:`lifecycle.quota_exhausted`):
    an auth error blocks the account (:func:`asf.workers.account_auth.note`, the one ALARM),
    a spent window stops it until its reset (:func:`asf.workers.headroom.note_exhausted`)."""
    if lifecycle.auth_failed(run):
        text = str((ev.result or {}).get('result') or '') or account_auth.log_text(run.get('log'))
        return job, 'auth', account_auth.note(product, run, text)
    return job, 'quota', headroom.note_exhausted(product, run, ev.result)


#: The kinds whose session writes a review: filed off the branch when the run ends.
REVIEW_FILING_KINDS = ('review',)


def file_review(product, job, run, ev, alive, found, status=None):
    """A finished review run's review, filed off its branch (:func:`asf.evidence.review_store.take`)
    before anything is judged or committed: the session writes the file in its worktree and
    commits nothing, because a push to a PR branch restarts the PR's whole CI and moves the head
    a merge watcher is pinned to. The run records ``review_filed`` (its judgement then needs no
    commit of its own) and the evidence is gathered again. Returns the evidence to judge.
    ``status`` (a :class:`lifecycle.WorktreeStatus`), when given, answers the re-gather's
    ``git status``."""
    if run.get('kind') not in REVIEW_FILING_KINDS or ev.result is None or ev.alive:
        return ev
    wt, branch, item = run.get('worktree'), run.get('branch'), run.get('item')
    try:
        filed = review_store.take(review_store.root(product), product.conventions, wt, branch,
                                  item, ev.remote_sha)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        found.append((job, 'filed', f'review not filed: {(str(e) or type(e).__name__)[:200]}'))
        return ev
    if not filed:
        return ev
    n, path = filed[-1]
    pool_mod.update_session(product, job, review_filed=path)
    run['review_filed'] = path
    found.append((job, 'filed', f'review round {n} of {item} filed off {branch} '
                                f'({os.path.basename(path)})'))
    return lifecycle.gather(product, run, alive=alive, status=status)


def publish_gap(product, run, ev, reason, alive=pid_alive, status=None):
    """B-0056: a run judged ``failed: not pushed`` with a clean tree and commits origin lacks —
    a rebase the factory started or the session finished, or work committed and never pushed —
    is published by the factory (:func:`asf.workers.lifecycle.publish`), then judged again.
    Uncommitted files of an ok run are committed first (:func:`asf.workers.lifecycle.commit_leftovers`,
    B-0094); a commit the repo's hooks refuse stays a hold. Returns
    ``(reason, evidence, line)``; ``line`` is None when nothing was attempted. ``status`` (a
    :class:`lifecycle.WorktreeStatus`), when given, answers the re-gathers' ``git status``."""
    wt, branch = run.get('worktree'), run.get('branch')
    ready = ruling_ready(run)
    result_ok = (reason == lifecycle.FINISHED
                 or (reason or '').startswith(UNPUSHED_REASON_PREFIXES) or ready)
    if not result_ok or not branch:
        return reason, ev, None
    if not wt or not os.path.isdir(wt):
        return reason, ev, None
    lines = []
    if ev.uncommitted and not ready:   # F-0176: the ruling ruled on the committed HEAD
        ok, line = lifecycle.commit_leftovers(wt, branch)  # B-0094
        if not ok:
            return reason, ev, line
        lines.append(line)
        ev = lifecycle.gather(product, run, alive=alive, worktree=wt, status=status)
    if not (ev.unpushed or (ev.remote_sha and not ev.head_on_remote)):
        return reason, ev, '; '.join(lines) or None
    from asf.harvest import mechanical  # the lane's table; harvest imports this module
    publish_args = dict(main=product.main, protected=refguard.listed(product.conventions),
                        push_timeout_s=gitpush.push_timeout(product.conventions))
    if mechanical.enabled(product):
        # flags.mechanical (W2-PR3b): review/notes rounds origin holds are dropped, a rewritten
        # origin is replayed onto — the event on the run, as the lane's table writes it
        declared = report_mod.rebased(str((ev.result or {}).get('result') or ''))
        ok, line, out = mechanical.publish_worktree(product, wt, branch, ev.remote_sha,
                                                    declared=declared, **publish_args)
        was = run.get('mechanical') or {}
        if run.get('job') and (was.get('kind'), was.get('head'), was.get('resolved')) \
                != (out.kind, out.head, out.resolved):  # once per outcome, not every pass
            run['mechanical'] = mechanical.event(out)
            pool_mod.update_session(product, run['job'], mechanical=run['mechanical'])
    else:
        ok, line = lifecycle.publish(wt, branch, ev.remote_sha, **publish_args)
    if not ok:
        # the refusal may have moved HEAD before refusing — a rebase that succeeded, an
        # account-name rewrite — so the evidence is re-read: the caller remembers this pair
        ev = lifecycle.gather(product, run, alive=alive, worktree=wt, status=status)
        return reason, ev, '; '.join(lines + [line])
    ev = lifecycle.gather(product, run, alive=alive, worktree=wt, status=status)
    landing = lifecycle.lands(run, pool_mod.sessions_path(product))
    return lifecycle.judge(run, ev, landing=landing), ev, '; '.join(lines + [line])


#: publishes the health pass makes at once. Each is a push the product's pre-push hook gates, and
#: the hook's run is most of a tick's health step (one product, 2026-09-27: five publishes, 376 s of
#: a 490 s step, one after another). Sessions push from their own worktrees side by side all
#: day; the factory's publishes, one per branch, do the same.
PUBLISH_WORKERS = 3


def _groups(entries):
    """Index lists of the runs that must be judged one after another, in the ledger's order:
    runs sharing a branch, a worktree or an item. A run's evidence is gathered before its publish,
    so a second run on the same branch must see the first one's push; runs of one item count
    rounds off the same ledger lines. Everything else is independent."""
    parent = list(range(len(entries)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, (job, run, _gen) in enumerate(entries):
        run = run or {}
        wt = run.get('worktree')
        keys = [('branch', run.get('branch') or job), ('item', run.get('item')),
                ('worktree', lifecycle.path_key(wt) if wt else None)]
        for key in keys:
            if key[1] is None or key[1] == '':
                continue
            if key in seen:
                parent[find(i)] = find(seen[key])
            else:
                seen[key] = i
    groups = {}
    for i in range(len(entries)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def run_steps(entries, publish, workers=None):
    """Drives each run's pass — ``entries`` is ``[(job, run, generator)]``, the generator yielding
    :func:`publish_gap`'s arguments and resumed with its result — to its end. The runs of one group
    (:func:`_groups`) go strictly one after another in the ledger's order, exactly as a serial
    loop would. The head run of every group runs to its next publish, those publishes are made at
    once (at most ``workers``), and the heads are resumed one by one on this thread: every
    judgement and registry write stays serial, only the publishes — a rebase and a push in the
    run's own worktree — run beside each other."""
    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, int(workers or PUBLISH_WORKERS))
    queues = [[entries[i][2] for i in g] for g in _groups(entries)]
    sends = [None] * len(queues)
    pool = None
    try:
        while True:
            asks = []
            for qi, queue in enumerate(queues):
                while queue:
                    try:
                        asks.append((qi, queue[0].send(sends[qi])))
                        break
                    except StopIteration:
                        queue.pop(0)
                        sends[qi] = None
            if not asks:
                return
            if len(asks) == 1 or workers == 1:
                results = [publish(*ask) for _qi, ask in asks]
            else:
                pool = pool or ThreadPoolExecutor(max_workers=workers)
                results = list(pool.map(lambda qa: publish(*qa[1]), asks))
            for (qi, _ask), result in zip(asks, results):
                sends[qi] = result
    finally:
        if pool is not None:
            pool.shutdown(wait=True)


def refusal_text(branch, line):
    """The correction a publish the factory could not make hands the next session: the refusal
    itself — the conflicting files, the lost commits, the ``redact: <file>:<line>`` finding or the
    hook's own last line — never the generic "commit and push what you have"."""
    if lifecycle.rebase_conflict(line):
        return lifecycle.rebase_conflict_text(branch, line)
    if lifecycle.stale_head(line):
        return lifecycle.stale_head_text(branch, line)
    why = (line or '').split('refused: ', 1)[-1]
    return (f'your commits are on the branch; the factory could not publish them: {why} — fix '
            f'what it names and commit; the factory publishes, never a push of your own')


#: The pair a refused publish is remembered by: the worktree's own HEAD and origin's tip for the
#: branch. Both halves are what the refusal was a function of — the hook reads the commits being
#: pushed, the lease and the rebase read the tip — so while neither has moved the next push has
#: the same inputs and earns the same refusal. The tip is `-` when the branch is not on origin, so
#: the value is two tokens whatever the evidence says.
def refusal_heads(ev):
    return f'{ev.head} {ev.remote_sha or "-"}'


def refusal_stands(run, ev):
    """Whether ``run``'s recorded refusal still describes the worktree, so publishing again would
    run the product's pre-push hook (16-182 s) for inputs that cannot have changed the answer.
    Pure: no git, no clock. False when nothing was refused, when the head could not be read, when
    either half of the pair has moved, and when the worktree holds uncommitted work the factory
    would commit first (B-0094, D6)."""
    if not run.get('publish_refused') or not ev.head or ev.uncommitted:
        return False
    return bool(run.get('publish_refused_heads')) \
        and run['publish_refused_heads'] == refusal_heads(ev)


def republish_steps(product, registry, job, run, alive, found, items=None, status=None):
    """A product's F-0094: a run judged ``failed: not pushed`` whose publish the factory refused
    (its rebase conflicted, a redaction, a hook) kept its commits in its worktree, and no later
    pass ever tried again — the refusal's cause fixed, the worktree publishable, the item still
    waited for a whole new session. Each pass publishes an ended unpushed run's worktree again
    (:func:`publish_gap`), while the run still owns it and no live session is in it. Published,
    the run is re-judged and a ``finished`` one drops its correction; refused again, a pending
    unpushed correction carries the refusal (:func:`refusal_text`), and a refusal already
    recorded is not printed again.

    B-0039: a ``finished`` republish is the park's only cause (its one ``correction`` was
    :data:`lifecycle.UNPUSHED` — nothing else was ever wrong with the item) cleared the moment
    the branch is on origin, so this same pass also opens the branch's PR, or adopts the open one
    already naming it (:func:`asf.ci_queue._open_pr`) — the item is never left pushed with an
    unopened PR until some later pass happens to notice (``items``: the record's index, for the
    item the PR's title and body are rendered from; no PR host, or a conflict with the trunk, is
    one more finding, never a reason to leave the correction parked again). ``conventions.flags.
    health_opens_pr: false`` opts a product out (default on). ``status`` (a
    :class:`lifecycle.WorktreeStatus`), when given, answers the gather below's ``git status``."""
    reason = run.get('end_reason') or ''
    wt, branch = run.get('worktree'), run.get('branch')
    if not (reason.startswith(UNPUSHED_REASON_PREFIXES) or ruling_ready(run)) \
            or run.get('harvested') or not branch:
        return
    if not wt or not os.path.isdir(wt) or alive(run.get('pid')):
        return
    owner = lifecycle.by_worktree(registry).get(lifecycle.path_key(wt))
    if owner is not None and owner.get('job') != job:
        return  # a later run took the worktree: its own judgement owns the branch
    ev = lifecycle.gather(product, run, alive=alive, status=status)
    if refusal_stands(run, ev):
        return  # F-0228: same head, same tip — the same refusal, and the hook costs minutes
    before = ev.remote_sha
    reason, ev, line = yield (product, run, ev, reason, alive)  # publish_gap, by run_steps
    if not line:
        return
    if ' refused: ' not in line:
        found.append((job, 'published', line))
        fields = {'end_reason': reason, 'rc': 0 if reason == lifecycle.FINISHED else 1,
                  'publish_refused': None, 'publish_refused_heads': None}
        sha = lifecycle.published_sha(before, ev.remote_sha, line)
        if sha:
            fields['published_head'] = sha
        was_unpushed_park = isinstance(run.get('correction'), dict) \
            and run['correction'].get('kind') == lifecycle.UNPUSHED
        if reason == lifecycle.FINISHED:
            fields['correction'] = None
        pool_mod.update_session(product, job, **fields)
        run.update(end_reason=reason)
        if sha:
            run.update(published_head=sha)
        found.append((job, 're-judged', reason))
        if reason == lifecycle.FINISHED and was_unpushed_park \
                and product.conventions.flag('health_opens_pr', True) is not False:
            from asf.ci_queue import _open_pr
            number, why = _open_pr(product, branch, run.get('item'), items)
            found.append((job, 'pr-opened', f'#{number}') if number
                         else (job, 'pr-not-opened', why))
        return
    if line == run.get('publish_refused'):
        return
    why = line.split('refused: ', 1)[-1]  # the hook's own text, never this function's wrapper
    cls = lifecycle.push_failure(why)
    fields = {'publish_refused': line,
              'publish_refused_heads': None if cls == lifecycle.NETWORK_ERROR
              else refusal_heads(ev)}            # a blip is never remembered (D5)
    corr = run.get('correction')
    held = None
    if isinstance(corr, dict) and corr.get('kind') == lifecycle.UNPUSHED:
        fields['correction'] = dict(corr, text=refusal_text(branch, line))
    elif lifecycle.pending_correction(run, registry) is None and cls != lifecycle.NETWORK_ERROR:
        # nothing pending carries this refusal, and with the retry gone nothing else will
        text, now = refusal_text(branch, line), pool_mod.now_iso()
        if cls == lifecycle.HOOK_REFUSED:
            more, held = lifecycle.hook_refusal_hold(registry, run, text, now)
        elif lifecycle.rebase_conflict(line):
            more, held = lifecycle.rebase_conflict_hold(registry, run, text, now,
                                                        main=product.main)
        else:
            more, held = lifecycle.hold(registry, run, lifecycle.UNPUSHED, text, now,
                                        main=product.main)
        fields.update(more)
    pool_mod.update_session(product, job, **fields)
    run.update(fields)
    found.append((job, 'published', line))
    if held:
        found.append((job, 'held', held.split(': ', 1)[1]))


def push_retry(ev, reason, line):
    """``(class, detail)`` when an unpushed run's push failed on the network or was refused by
    the repo's hook — read off its REPORT's ``pushed:`` line and the factory's own publish line
    — else None (:func:`asf.workers.lifecycle.push_failure`)."""
    if not (reason or '').startswith(UNPUSHED_REASON_PREFIXES):
        return None
    said = report_mod.parse(str((ev.result or {}).get('result') or '')).get('pushed') or ''
    if not report_mod.NO_RE.match(said):
        said = ''
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


def health(product, fix=False, alive=None, session_source=None, out=print, items=None,
           spare=()):
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
    beats = heartbeat.Beats(product)  # every run's beat and branch: one ls-remote for the pass
    if any(cloud.is_cloud(s) and not s.get('ended')
           for s in pool_mod.load_sessions(product).values()):
        try:  # the cloud lane's runs first: their state is what every check below reads
            cloud.sync(product, out=out, beats=beats)
        except Exception as e:  # noqa: BLE001 — a failed sync leaves the runs working
            out(f'cloud: sync failed — {(str(e) or type(e).__name__).splitlines()[0]}')
    try:  # a local run that stopped beating: ended and continued before anything judges it
        heartbeat.sweep(product, beats=beats, alive=alive, out=out)
    except Exception as e:  # noqa: BLE001 — a failed sweep leaves the runs as they are
        out(f'heartbeat: sweep failed — {(str(e) or type(e).__name__).splitlines()[0]}')
    sessions = pool_mod.load_sessions(product)
    # ``spare``: the runs this tick's own wave just launched — judged by the next tick
    sessions = {job: s for job, s in sessions.items() if job not in spare}
    if items is None:
        items = record_items(product)
    liveness = None
    if alive is None:
        # one `_observe` (one `ps axeww`) feeds both callables (PD5) — a second, independent
        # `liveness_for` call here would read the tick's one most expensive thing twice
        observed, why, kw = _observe(product, sessions.values(), session_source)
        if why:
            alive = pid_alive
            liveness = lambda pid: lifecycle.ALIVE if pid_alive(pid) else lifecycle.GONE  # noqa: E731
        else:
            alive = observe.identity_alive(observed, sessions.values(), **kw)
            liveness = observe.liveness_for(observed, sessions.values(), **kw)
    # one `git status --porcelain` per worktree per version of that worktree for the whole pass
    # (I2) — built here, above `run_steps`, so every site below it shares one snapshot; not
    # beside `heads` below, which is built after the publish phase on purpose (PD2)
    status = lifecycle.WorktreeStatus()
    def steps(job, s, found):
        # one run's pass, each publish_gap a yield: run_steps makes the publishes of runs on
        # different branches and worktrees at once, and resumes every run in the ledger's order
        closed = lifecycle.closed_state(items, s.get('item'))
        if closed and s.get('ended') and lifecycle.pending_correction(s, registry):
            pool_mod.update_session(product, job, correction=None)
            s.pop('correction', None)
            found.append((job, 'released', f'{s.get("item")} is {closed}: no correction'))
        corr = lifecycle.pending_correction(s, registry)
        if corr and corr.get('kind') in (lifecycle.BLOCKED, lifecycle.RELAUNCH_CAP) \
                and corr.get('card') and items is not None \
                and corr['card'] != lifecycle.card_fingerprint(product, s.get('item'), items):
            pool_mod.update_session(product, job, correction=None)
            s.pop('correction', None)
            found.append((job, 'released', f'{s.get("item")}: the card changed — the park lifts'))
        corr = lifecycle.pending_correction(s, registry)
        if corr and corr.get('parked') and items is not None \
                and replan_mod.replanned_since(items, s.get('item'), corr.get('at')):
            # the park held the item on the plan a replan has since replaced (its Tasks, their
            # after:): the work it waited on was re-cut, so the park lifts — no hand unpark
            pool_mod.update_session(product, job, correction=None, unparked=pool_mod.now_iso(),
                                    unpark_why='its Feature was re-planned')
            s.pop('correction', None)
            found.append((job, 'released', f'{s.get("item")}: its Feature was re-planned — '
                                           'the park lifts'))
        corr = s.get('correction') or {}
        if corr.get('text') and not corr.get('parked'):
            # the raw stored correction, not :func:`pending_correction`: a later run already
            # answers it there (silently, nothing cleared) once one exists, whatever became of
            # it — this checks the record's own landing fact instead, and clears the field for
            # good so a later run that itself ends without landing does not resurrect it
            other = lifecycle.correction_superseded_by_landing(
                registry, s.get('item'), corr.get('branch') or s.get('branch'))
            if other:
                # the correction or ruling asked for work on this branch; a different branch of
                # the same item landed instead — carrying it out here now would just redo it (#46)
                pool_mod.update_session(product, job, correction=None,
                                        dropped=pool_mod.now_iso(), drop_why=f'{other} landed instead')
                s.pop('correction', None)
                found.append((job, 'released', f'{s.get("item")}: {other} landed instead — '
                                               'the correction drops'))
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
                return
            if s.get('end_reason') == lifecycle.STOPPED and not s.get('correction'):
                ev = lifecycle.gather(product, s, alive=alive, status=status)
                if lifecycle.pushed_after_stop(s, ev):
                    fields, line = lifecycle.hold(registry, s, lifecycle.PUSHED_AFTER_STOP,
                                                  lifecycle.PUSHED_AFTER_STOP, pool_mod.now_iso(),
                                                  head=ev.remote_sha, main=product.main)
                    pool_mod.update_session(product, job, **fields)
                    found.append((job, 'held', line.split(': ', 1)[1]))
            # a `dead pid` judgement is revisited: the result may have landed after the check,
            # or a correction may have finished the run (B-0028)
            if s.get('end_reason') == lifecycle.DEAD_PID and not s.get('harvested'):
                ev = lifecycle.gather(product, s, alive=alive, status=status)
                ev = file_review(product, job, s, ev, alive, found, status=status)
                if ev.result is not None:
                    reason = lifecycle.judge(s, ev, landing=lifecycle.lands(s, registry))
                    before = ev.remote_sha
                    reason, ev, line = yield (product, s, ev, reason, alive)
                    if line:
                        found.append((job, 'published', line))
                    ok = reason == lifecycle.FINISHED
                    sha = lifecycle.published_sha(before, ev.remote_sha, line)
                    fields = {'end_reason': reason, 'rc': 0 if ok else 1}
                    if sha:
                        fields['published_head'] = sha
                    pool_mod.update_session(product, job, **fields)
                    s.update(**fields)
                    found.append((job, 're-judged', reason))
                    if lifecycle.quota_exhausted(s):
                        found.append(account_fault(product, job, s, ev))
            if not closed:
                yield from republish_steps(product, registry, job, s, alive, found, items,
                                           status=status)
            return
        ev = lifecycle.gather(product, s, alive=alive, liveness=liveness, status=status)
        ev = file_review(product, job, s, ev, alive, found, status=status)
        reason = lifecycle.judge(s, ev, landing=lifecycle.lands(s, registry))
        if reason is None:
            return
        before = ev.remote_sha
        reason, ev, line = yield (product, s, ev, reason, alive)  # B-0056
        if line:
            found.append((job, 'published', line))
        sha = lifecycle.published_sha(before, ev.remote_sha, line)
        retry = push_retry(ev, reason, line)
        if retry and retry[0] == lifecycle.NETWORK_ERROR:  # B-0097: a blip — retried, no round
            found.append((job, 'retry', f'{retry[0]}: {retry[1]} — published again next pass'))
            return
        if retry:  # the repo's own pre-push hook refused it: its output, no round spent
            reason = f'failed: {retry[0]}: {retry[1]}'
        now = pool_mod.now_iso()
        # the class is only ever the class of a death (F-0234): a retry or a hook refusal above
        # may have turned `reason` into something other than DEAD_PID since `ev` was gathered
        dead_class = (ev.liveness or None) if reason == lifecycle.DEAD_PID else None
        # the cloud sync's own account of the death, kept on the run (F-0266 C13): end_reason
        # is matched by equality (is_dead_reason) and never carries it; '' for a local run
        dead_why = (cloudpid.why(s.get('pid')) or None) if dead_class else None
        fields = {'ended': now, 'end_reason': reason,
                  'runtime_session': runtime_mod.runtime_session(s.get('log')) or None,
                  'dead_class': dead_class, 'dead_why': dead_why}
        if sha:
            fields['published_head'] = sha
        pool_mod.update_session(product, job, **fields)
        s.update(ended=now, end_reason=reason, **({'published_head': sha} if sha else {}))
        found.append((job, 'ended', reason))
        if reason == lifecycle.FINISHED:
            back = account_auth.proved(s)
            if back:
                found.append((job, 'auth', back))
        pushes, defect = pushlog.defect(product, s)
        if pushes:  # one push per correction round: a second one is this run's defect
            extra = {'defect': defect} if defect else {}
            pool_mod.update_session(product, job, pushes=pushes, **extra)
            if defect:
                found.append((job, 'defect', defect))
        result_text = str((ev.result or {}).get('result') or '')
        question = report_mod.needs_input(result_text)
        if question and relaunch_mod.ended_on_heartbeat(str((ev.result or {}).get('result'))):
            # the sandbox refused the session's beat loop: nothing about the work, so the
            # relaunch cap does not count this run (F-0279) — marked while the log is its own
            pool_mod.update_session(product, job, heartbeat_refused=1)
            s['heartbeat_refused'] = 1
        if lifecycle.quota_exhausted(s):
            # the account's window or its auth, not the work: its account stops (until the
            # reset, or until re-enabled), and the item relaunches — no hold, no round
            # (asf.workers.headroom, asf.workers.account_auth)
            found.append(account_fault(product, job, s, ev))
            return
        if closed:
            return  # nothing to send back: the worktree is reaped below when it is empty
        if retry:
            text = (f'the push was refused by the repo\'s own hook — {retry[1]} — fix what it '
                    f'names, commit, and push again')
            # one parse, bound to a local, feeding `retried` the way `question` above already
            # reads the same result text (F-0235, PD4): `push_retry`'s own parse is untouched
            rep = report_mod.parse(result_text)
            retried = report_mod.hook_retried(rep)
            fields, line = lifecycle.hook_refusal_hold(registry, s, text, now, retried=retried)
            pool_mod.update_session(product, job, **fields)
            found.append((job, 'held', line.split(': ', 1)[1]))
        elif reason == f'failed: {lifecycle.EMPTY_BRANCH}' and lifecycle.landed_earlier(registry, s):
            landed_sha = lifecycle.landed_earlier(registry, s)
            pool_mod.update_session(product, job, harvested=landed_sha)
            s.update(harvested=landed_sha)
            found.append((job, 'landed', f'its branch landed at {landed_sha[:9]}: nothing to push'))
        elif question and items is not None and not (ev.unpushed or ev.uncommitted) and (
                reason.startswith(UNPUSHED_REASON_PREFIXES)
                or reason == f'failed: {lifecycle.EMPTY_BRANCH}'):
            # nothing to land, and the run's own report declared a question for a person:
            # relaunching it buys the same report again, so the item is parked (F-0126). A run
            # with commits or files in its worktree has something to land — never this park (a
            # product's T-0349: an approved transplant parked "nothing to land" because its
            # report asked a person to publish it); it is held below with its work as the input
            probe = None
            command = report_mod.operator_command(question)
            if command:
                from asf.harvest import harvest as harvest_mod
                ran, output = harvest_mod.run_operator_command(command, product.repo_dir, product)
                if ran:
                    probe = {'command': command, 'output': output}
            fields, line = lifecycle.blocked_park(
                question, s.get('item'), lifecycle.card_fingerprint(product, s.get('item'), items),
                now, probe=probe)
            pool_mod.update_session(product, job, **fields)
            found.append((job, 'parked', line))
        elif reason == f'failed: {lifecycle.EMPTY_BRANCH}' and lifecycle.delivered_off_branch(
                registry, s, str((ev.result or {}).get('result') or '')):
            why = lifecycle.delivered_off_branch(registry, s, str((ev.result or {}).get('result') or ''))
            pool_mod.update_session(product, job, end_reason=lifecycle.NOTHING_TO_LAND)
            s.update(end_reason=lifecycle.NOTHING_TO_LAND)
            found.append((job, 're-judged', f'{lifecycle.NOTHING_TO_LAND} — {why}'))
        elif reason == lifecycle.NOTHING_UNPUSHED \
                and s.get('kind') in lifecycle.ON_A_PUSHED_BRANCH:
            # 0 uncommitted, 0 unpushed: nothing here origin lacks — a hold would buy a session
            # to push nothing ("nothing to correct"), so the run is re-judged, no hold, no round
            pool_mod.update_session(product, job, end_reason=lifecycle.NOTHING_TO_LAND)
            s.update(end_reason=lifecycle.NOTHING_TO_LAND)
            found.append((job, 're-judged', f'{lifecycle.NOTHING_TO_LAND} — 0 uncommitted, '
                                            f'0 unpushed: nothing to push, no hold'))
        elif reason.startswith(UNPUSHED_REASON_PREFIXES):
            # the run's own work is the correction's input: the next session on the branch
            # commits and pushes it, or says why not (B-0051, B-0052)
            text = lifecycle.unpushed_text(reason)
            if lifecycle.rebase_conflict(line):  # the factory's rebase conflicted: files named
                text = lifecycle.rebase_conflict_text(s.get('branch') or job, line)
                fields, line = lifecycle.rebase_conflict_hold(registry, s, text, now,
                                                              main=product.main)
                pool_mod.update_session(product, job, **fields)
                found.append((job, 'held', line.split(': ', 1)[1]))
                return
            if lifecycle.stale_head(line):  # origin holds commits this head lacks: a rebase
                text = lifecycle.stale_head_text(s.get('branch') or job, line)
            fields, line = lifecycle.hold(registry, s, lifecycle.UNPUSHED, text, now,
                                          head=ev.remote_sha, main=product.main)
            pool_mod.update_session(product, job, **fields)
            found.append((job, 'held', line.split(': ', 1)[1]))
        elif reason == f'failed: {lifecycle.EMPTY_BRANCH}':
            # a pushed branch with nothing on it: the same loop, sent back to commit real work
            # or say why there is none (B-0076) — its second genuine empty end parks it instead
            # (EMPTY_CAP)
            fields, line = lifecycle.hold(registry, s, lifecycle.EMPTY,
                                          lifecycle.empty_branch_text(), now,
                                          head=ev.remote_sha, main=product.main)
            pool_mod.update_session(product, job, **fields)
            what = 'parked' if fields['correction'].get('parked') else 'held'
            found.append((job, what, line.split(': ', 1)[1]))

    per_run = [(job, s, []) for job, s in sessions.items()]
    run_steps([(job, s, steps(job, s, own)) for job, s, own in per_run],
              lambda *args: publish_gap(*args, status=status))
    for _job, _s, own in per_run:
        found.extend(own)
    # T-0196: an ended run is finished — its cloud token settled, its lingering process stopped
    settled = settle_ended(product, sessions, fix=fix, session_source=session_source)
    found.extend(settled)
    gone_pids = {int(d.split()[1]) for _, what, d in settled if what == 'stopped'}
    if gone_pids:
        was_alive = alive
        alive = lambda pid: (not (isinstance(pid, int) and pid in gone_pids)  # noqa: E731
                             and was_alive(pid))
    _git(['fetch', '-q', 'origin', product.main], product.repo_dir)
    wdir = spawn_mod.worktrees_dir(product)
    owners = lifecycle.by_worktree(registry)
    heads = lifecycle.RemoteHeads()  # one ls-remote for the pass; nothing below pushes
    for name in sorted(os.listdir(wdir)):
        path = os.path.join(wdir, name)
        if not os.path.isdir(path):
            continue
        s = owners.get(lifecycle.path_key(path)) or sessions.get(name)
        ev = lifecycle.gather(product, s or {}, alive=alive, worktree=path, heads=heads,
                              status=status)
        if s is None and not ev.remote_sha:
            # an orphan carries no branch on its record: read the one checked out
            ev = lifecycle.gather(product, {'branch': _head_branch(path)}, alive=alive,
                                  worktree=path, heads=heads, status=status)
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
