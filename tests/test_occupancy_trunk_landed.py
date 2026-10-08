"""F-0277 Task 3: the lane releases an item whose PR the trunk carries, so the Feature's next
Task launches. ``lifecycle.ended_prs(state_dir, landed=…)`` ends a PR the trunk already landed
even while the evidence pass's own cache still calls it OPEN — the incident's own before-picture
is the first test below — and ``step_wave.occupancy`` reads that straight off the trunk with one
``git log``, no ``gh`` call."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, trunk_watch
from asf.tick import step_wave
from asf.workers import lifecycle

HEAD = 'a' * 40

GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), check=True,
                          capture_output=True, text=True).stdout.strip()


class EndedPrsTrunkLandedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-ended-prs-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def cache(self, rows):
        with open(os.path.join(self.tmp, lifecycle.PR_CACHE), 'w') as fh:
            json.dump(rows, fh)

    def test_an_hour_old_open_row_is_not_ended_without_landed(self):
        """The incident's own before-picture: nothing here reads the trunk at all."""
        self.cache([{'number': 1046, 'state': 'OPEN', 'headRefOid': HEAD}])
        self.assertEqual(lifecycle.ended_prs(self.tmp), {})

    def test_landed_ends_the_open_row_as_merged_with_its_cached_head(self):
        self.cache([{'number': 1046, 'state': 'OPEN', 'headRefOid': HEAD}])
        out = lifecycle.ended_prs(self.tmp, landed={1046: 'b' * 40})
        self.assertEqual(out, {1046: {'state': 'MERGED', 'head': HEAD}})
        self.assertEqual(lifecycle.pr_ended({'pr': 1046, 'head': HEAD}, out), 'MERGED')

    def test_landed_ends_a_number_the_cache_never_held(self):
        self.cache([{'number': 1046, 'state': 'OPEN', 'headRefOid': HEAD}])
        out = lifecycle.ended_prs(self.tmp, landed={2000: 'b' * 40})
        self.assertEqual(out[2000], {'state': 'MERGED', 'head': None})

    def test_landed_overrides_a_cached_closed_row_to_merged(self):
        self.cache([{'number': 1046, 'state': 'CLOSED', 'headRefOid': HEAD}])
        out = lifecycle.ended_prs(self.tmp, landed={1046: 'b' * 40})
        self.assertEqual(out, {1046: {'state': 'MERGED', 'head': HEAD}})

    def test_landed_empty_is_todays_answer_unchanged(self):
        self.cache([{'number': 1046, 'state': 'MERGED', 'headRefOid': HEAD},
                    {'number': 7, 'state': 'CLOSED', 'headRefOid': None}])
        self.assertEqual(lifecycle.ended_prs(self.tmp, landed=()), lifecycle.ended_prs(self.tmp))

    def test_an_unreadable_cache_with_landed_is_the_trunks_entries_alone(self):
        with open(os.path.join(self.tmp, lifecycle.PR_CACHE), 'w') as fh:
            fh.write('{not json')
        self.assertEqual(lifecycle.ended_prs(self.tmp, landed={1046: 'b' * 40}),
                         {1046: {'state': 'MERGED', 'head': None}})
        self.assertEqual(lifecycle.ended_prs(self.tmp), {})


class OccupancyTrunkLandedTests(unittest.TestCase):
    """The hole this card was filed for: a lane record naming a PR the trunk already carries
    held an item in `busy` or `waiting_landing` until the next evidence pass caught up, 40
    minutes later. Refer to the lane by `lane-1`, never an account name or a machine path."""
    BRANCH = 'lane-1'
    SHA = 'c' * 40

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-occupancy-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, 'sessions.jsonl')
        with open(self.path, 'w') as fh:
            fh.write(json.dumps({
                'job': 't-0485', 'item': 'T-0485', 'kind': 'task', 'branch': self.BRANCH,
                'started': '2026-10-08T10:00:00Z', 'pid': 1,
                'ended': '2026-10-08T10:30:00Z', 'end_reason': 'finished', 'harvest': 'pr',
                'lane': {'pr': 1046, 'sha': self.SHA},
            }) + '\n')

    def occ(self, landed):
        # no cache-prs.json in this directory: ``landed`` is the whole answer, as it is when
        # the lane record is still the only thing naming the PR at all.
        ended = lifecycle.ended_prs(self.tmp, landed=landed)
        return lifecycle.occupancy(self.path, ended=ended, alive=lambda _p: False)

    def test_the_trunks_landing_releases_the_item(self):
        occ = self.occ({1046: 'd' * 40})
        self.assertNotIn('T-0485', occ['busy'])
        self.assertNotIn('T-0485', occ['waiting_landing'])
        self.assertEqual(occ['landed']['T-0485'], self.SHA)
        self.assertEqual(occ['landed_on']['T-0485'], self.BRANCH)

    def test_with_no_landed_the_item_is_still_held(self):
        occ = self.occ({})
        self.assertTrue('T-0485' in occ['busy'] or 'T-0485' in occ['waiting_landing'])


class TrunkLandedTests(unittest.TestCase):
    """``step_wave.trunk_landed`` over a real trunk clone — the fact :func:`step_wave.occupancy`
    hands to :func:`lifecycle.ended_prs` as its ``landed`` argument."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-trunk-landed-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'repo')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(self.tmp, 'clone', '-q', self.origin, self.repo)
        git(self.repo, 'checkout', '-q', '-b', 'main')
        self.land('init')
        self.sha = self.land('merge-queue: #1046 (lane-1/T-10485 @ 243ce531)')
        self.product = env.Product('trunk-landed', {'repo_slug': 'org/repo',
                                                     'repo_dir': self.repo, 'main': 'main'})

    def land(self, subject):
        with open(os.path.join(self.repo, 'f.txt'), 'a') as f:
            f.write(subject + '\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', subject)
        git(self.repo, 'push', '-q', 'origin', 'main')
        git(self.repo, 'fetch', '-q', 'origin')
        return git(self.repo, 'rev-parse', 'HEAD')

    def test_reads_landed_prs_off_the_trunk_clone(self):
        self.assertEqual(step_wave.trunk_landed(self.product), {1046: self.sha})

    def test_no_repo_dir_is_empty(self):
        product = env.Product('trunk-landed-norepo', {'repo_slug': 'org/repo'})
        self.assertEqual(step_wave.trunk_landed(product), {})

    def test_first_parent_returning_none_is_empty(self):
        with mock.patch.object(trunk_watch, 'first_parent', return_value=None):
            self.assertEqual(step_wave.trunk_landed(self.product), {})

    def test_occupancy_passes_trunk_landed_through_to_ended_prs(self):
        """No new state file, no new ``gh`` call: the trunk read is the one ``git log`` in
        :func:`step_wave.trunk_landed`, and ``lifecycle.ended_prs``/``occupancy`` are both
        file-and-git reads with no host call of their own."""
        home = os.path.join(self.tmp, 'asf-home')
        with mock.patch.object(env, 'ASF_HOME', home), \
             mock.patch.object(step_wave, 'trunk_landed', return_value={1046: self.sha}) as landed, \
             mock.patch.object(lifecycle, 'ended_prs', wraps=lifecycle.ended_prs) as ended_prs:
            step_wave.occupancy(self.product)
        landed.assert_called_once_with(self.product)
        self.assertEqual(ended_prs.call_args.kwargs['landed'], {1046: self.sha})


if __name__ == '__main__':
    unittest.main()
