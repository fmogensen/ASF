"""tools/merge-pr.sh against a fake gh and a fake git: nothing touches the network."""
import os
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
                   MERGE_PR_POLL='0', MERGE_PR_TIMEOUT='0', MERGE_PR_ATTEMPTS=str(attempts))
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
        self.assertEqual(log.count('git merge-base'), 3)

    def test_a_refused_merge_loops(self):
        r, log = run(merge_rc=1, attempts=2)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(log.count('pr merge'), 2)


if __name__ == '__main__':
    unittest.main()
