"""tools/merge-pr.sh against a fake gh and a fake git: nothing touches the network."""
import contextlib
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'merge-pr.sh')

# State lives in files in $FAKE: head (sha), behind_rounds (how many ancestor checks fail),
# checks (the check-runs rows), log (every call).
FAKE_GIT = r'''#!/usr/bin/env bash
echo "git $*" >> "$FAKE/log"
case "$1" in
  fetch) exit 0 ;;
  merge-base)
    # $FAKE/behind: one rc per call (1 = behind), the last one repeats
    rc=$(head -n1 "$FAKE/behind"); [ "$(wc -l < "$FAKE/behind")" -gt 1 ] && sed -i.bak 1d "$FAKE/behind"; exit "$rc" ;;
esac
exit 0
'''
FAKE_GH = r'''#!/usr/bin/env bash
echo "gh $*" >> "$FAKE/log"
case "$1 $2" in
  "pr view")
    case "$*" in
      *mergeCommit*) echo mergedsha000 ;;
      *) cat "$FAKE/head" ;;
    esac ;;
  "pr update-branch") echo newhead111 > "$FAKE/head"; cat "$FAKE/checks_after" > "$FAKE/checks" ;;
  "pr merge") exit "$(cat "$FAKE/merge_rc")" ;;
  "api repos/{owner}/{repo}/commits/"*) ;;
esac
if [ "$1" = api ]; then cat "$FAKE/checks"; fi
exit 0
'''
GREEN = 'tests (3.12)\tcompleted\tsuccess\ntests (3.13)\tcompleted\tsuccess\n'
RED = 'tests (3.12)\tcompleted\tsuccess\ntests (3.13)\tcompleted\tfailure\n'


def setup(d, merge_sleep=0):
    """fake gh/git in d/bin; returns the env. merge_sleep makes `gh pr merge` slow."""
    bindir = os.path.join(d, 'bin')
    os.makedirs(bindir, exist_ok=True)
    gh = FAKE_GH.replace('"pr merge") exit', '"pr merge") echo "merge-start $3" >> "$FAKE/log"; sleep %s; '
                         'echo "merge-end $3" >> "$FAKE/log"; exit' % merge_sleep)
    for name, body in (('git', FAKE_GIT), ('gh', gh)):
        p = os.path.join(bindir, name)
        with open(p, 'w') as f:
            f.write(body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    for name, val in (('head', 'oldhead000\n'), ('behind', '0\n'), ('checks', GREEN),
                      ('checks_after', GREEN), ('merge_rc', '0\n')):
        with open(os.path.join(d, name), 'w') as f:
            f.write(val)
    return dict(os.environ, FAKE=d, PATH=bindir + os.pathsep + os.environ['PATH'],
                XDG_CACHE_HOME=os.path.join(d, 'cache'), MERGE_PR_LOCK_POLL='0.2',
                MERGE_PR_POLL='0', MERGE_PR_TIMEOUT='0')


def run(behind='0', checks=GREEN, checks_after=GREEN, merge_rc=0, attempts=3):
    with tempfile.TemporaryDirectory() as d:
        bindir = os.path.join(d, 'bin')
        os.mkdir(bindir)
        for name, body in (('git', FAKE_GIT), ('gh', FAKE_GH)):
            p = os.path.join(bindir, name)
            with open(p, 'w') as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        for name, val in (('head', 'oldhead000'), ('behind', behind), ('checks', checks),
                          ('checks_after', checks_after), ('merge_rc', str(merge_rc))):
            with open(os.path.join(d, name), 'w') as f:
                f.write(val + ('\n' if name in ('head', 'behind', 'merge_rc') else ''))
        env = dict(os.environ, FAKE=d, PATH=bindir + os.pathsep + os.environ['PATH'],
                   MERGE_PR_POLL='0', MERGE_PR_TIMEOUT='0', MERGE_PR_LOCK_POLL='0.2',
                   XDG_CACHE_HOME=os.path.join(d, 'cache'), MERGE_PR_ATTEMPTS=str(attempts))
        r = subprocess.run(['bash', SCRIPT, '7'], env=env, capture_output=True, text=True)
        with open(os.path.join(d, 'log')) as f:
            log = f.read()
        return r, log


class MergePrTests(unittest.TestCase):
    def test_up_to_date_merges_the_exact_head(self):
        r, log = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('update-branch', log)
        self.assertIn('pr merge 7 --squash --delete-branch --match-head-commit oldhead000', log)
        self.assertIn('mergedsha000', r.stdout)
        self.assertIn('git pull --ff-only', r.stdout)
        self.assertIn('asf upgrade --wait', r.stdout)

    def test_behind_updates_waits_then_merges_the_new_head(self):
        r, log = run(behind='1\n0')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('update-branch 7 --rebase', log)
        self.assertIn('--match-head-commit newhead111', log)
        self.assertNotIn('--match-head-commit oldhead000', log)

    def test_pending_checks_are_waited_for(self):
        pending = 'tests (3.12)\tcompleted\tsuccess\ntests (3.13)\tin_progress\t\n'
        r, log = run(behind='1\n0', checks_after=pending)
        # the fake never turns green: the wait ends in the timeout, never in a merge
        self.assertNotIn('pr merge', log)

    def test_red_checks_refuse(self):
        r, log = run(checks=RED)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('red', r.stderr)
        self.assertNotIn('pr merge', log)

    def test_main_moving_again_loops_then_gives_up(self):
        # behind on round 1 and again after the wait on every round
        r, log = run(behind='1', attempts=3)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(log.count('update-branch'), 3)
        self.assertIn('gave up after 3 rounds', r.stderr)
        self.assertNotIn('pr merge', log)

    def test_main_moving_between_check_and_merge_loops_and_merges(self):
        # up to date at the start, behind at the pre-merge re-check, fine from then on
        r, log = run(behind='0\n1\n0')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.count('pr merge'), 1)
        self.assertEqual(log.count('git merge-base'), 4)

    def test_a_refused_merge_loops(self):
        r, log = run(merge_rc=1, attempts=2)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(log.count('pr merge'), 2)


def _write_lock(d, pid):
    lock = os.path.join(d, 'cache', 'asf', 'merge-pr.lock')
    os.makedirs(lock)
    for n, v in (('pid', str(pid)), ('pr', '99'), ('since', '0')):
        with open(os.path.join(lock, n), 'w') as f:
            f.write(v + '\n')
    return lock


def setup_gated(d):
    """setup() whose `gh pr merge` announces itself on the fifo d/entered (with the lock holder's
    pid in the log) and then blocks on the fifo d/gate until the test opens it."""
    env = setup(d)
    for n in ('entered', 'gate'):
        os.mkfifo(os.path.join(d, n))
    gh = os.path.join(d, 'bin', 'gh')
    with open(gh) as f:
        body = f.read()
    body = body.replace(
        '"pr merge") echo "merge-start $3" >> "$FAKE/log"; sleep 0; ',
        '"pr merge") echo "merge-start $3 $(cat "$XDG_CACHE_HOME/asf/merge-pr.lock/pid")" >> "$FAKE/log"; '
        'echo "$3" > "$FAKE/entered"; read -r _ < "$FAKE/gate"; ')
    assert 'entered' in body
    with open(gh, 'w') as f:
        f.write(body)
    return env


def _popen(env, pr):
    return subprocess.Popen(['bash', SCRIPT, pr], env=env, stdout=subprocess.PIPE, text=True)


def _entered(d):
    """blocks until some invocation is inside `gh pr merge`; returns its PR number"""
    with open(os.path.join(d, 'entered')) as f:
        return f.read().strip()


def _open_gate(d):
    with open(os.path.join(d, 'gate'), 'w') as f:
        f.write('go\n')


@contextlib.contextmanager
def _deadline(seconds):
    """a hang (a fifo nobody opens) fails the test instead of the run"""
    def boom(*_):
        raise AssertionError('merge-pr lock test hung for %ss' % seconds)
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class MergePrLockTests(unittest.TestCase):
    def test_two_concurrent_invocations_serialize(self):
        # No timing: A's merge parks on a fifo gate while holding the lock; B is started only
        # after A is inside its merge and is shown blocked on the lock (its "waiting" line); A is
        # released only then. Every merge records the lock holder it ran under.
        with tempfile.TemporaryDirectory() as d:
            env = setup_gated(d)
            with _deadline(60):
                a = _popen(env, '7')
                self.assertEqual(_entered(d), '7')
                b = _popen(env, '8')
                self.assertIn('waiting for the merge lock', b.stdout.readline())
                _open_gate(d)                      # A finishes its merge and releases
                self.assertEqual(_entered(d), '8')  # only now does B get in
                _open_gate(d)
                for p in (a, b):
                    p.communicate()
            self.assertEqual([a.returncode, b.returncode], [0, 0])
            with open(os.path.join(d, 'log')) as f:
                marks = [l.split() for l in f if l.startswith(('merge-start', 'merge-end'))]
            self.assertEqual([m[:2] for m in marks],
                             [['merge-start', '7'], ['merge-end', '7'], ['merge-start', '8'], ['merge-end', '8']])
            # each merge ran while its own invocation held the lock
            self.assertEqual(marks[0][2], str(a.pid))
            self.assertEqual(marks[2][2], str(b.pid))
            self.assertFalse(os.path.exists(os.path.join(d, 'cache', 'asf', 'merge-pr.lock')))

    def test_live_holder_still_writing_its_lock_is_not_broken(self):
        # the CI flake: a holder between `pid` and `since` read as since=0, i.e. ancient, and
        # a waiter broke a live lock and merged alongside it
        with tempfile.TemporaryDirectory() as d:
            env = setup(d)
            lock = os.path.join(d, 'cache', 'asf', 'merge-pr.lock')
            os.makedirs(lock)
            with open(os.path.join(lock, 'pid'), 'w') as f:
                f.write('%d\n' % os.getpid())
            with _deadline(60):
                p = _popen(env, '7')
                self.assertIn('waiting for the merge lock', p.stdout.readline())
                shutil.rmtree(lock)                # the holder releases
                out, _ = p.communicate()
            self.assertEqual(p.returncode, 0)
            self.assertNotIn('breaking', out)
            self.assertIn('merged PR #7', out)

    def test_stale_lock_with_dead_pid_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            env = setup(d)
            dead = subprocess.Popen(['true'])
            dead.wait()
            lock = _write_lock(d, dead.pid)
            r = subprocess.run(['bash', SCRIPT, '7'], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('breaking a stale merge lock', r.stdout)
            self.assertFalse(os.path.exists(lock))

    def test_live_lock_older_than_timeout_plus_600_is_broken(self):
        with tempfile.TemporaryDirectory() as d:
            env = setup(d)
            _write_lock(d, os.getpid())  # alive, but since=0
            r = subprocess.run(['bash', SCRIPT, '7'], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('breaking a stale merge lock', r.stdout)

    def test_lock_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            env = setup(d)
            env['MERGE_PR_LOCK'] = '0'
            lock = _write_lock(d, os.getpid())
            with open(os.path.join(lock, 'since'), 'w') as f:
                f.write(str(2 ** 31) + '\n')
            r = subprocess.run(['bash', SCRIPT, '7'], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(lock))


if __name__ == '__main__':
    unittest.main()
