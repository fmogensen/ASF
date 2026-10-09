"""F-0277: a landing makes the PR list stale at once, and a merge-queue landing is never an
open PR.

A Task's PR landed through the merge queue; the PR cache, read minutes before, still said OPEN,
and ingest kept the Task Active for 40 min. The cache is now keyed on the trunk's sha as well as
its age, and a PR whose ``merge-queue: #<n>`` commit is on the trunk is not counted open."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, trunk_watch
from asf.evidence import closing, evidence, sources
from asf.record import frontmatter, ingest

ID = ['-c', 'user.name=cache', '-c', 'user.email=cache@example.com']


def git(cwd, *args):
    return subprocess.run(['git', *ID, *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


class TrunkKeyedCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-pr-cache-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'repo')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        git(self.tmp, 'clone', '-q', self.origin, self.repo)
        git(self.repo, 'config', 'core.hooksPath', os.devnull)
        git(self.repo, 'checkout', '-q', '-b', 'main')
        self.land('seed')
        name = self.id().rsplit('.', 1)[-1]
        self.product = env.Product(name, {'repo_slug': 'org/repo', 'repo_dir': self.repo,
                                          'main': 'main'})
        self.answers = [[{'number': 7, 'state': 'OPEN'}], [{'number': 7, 'state': 'MERGED'}]]
        self.calls = []

    def land(self, subject):
        with open(os.path.join(self.repo, 'f.txt'), 'a') as f:
            f.write(subject + '\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', subject)
        git(self.repo, 'push', '-q', 'origin', 'main')
        git(self.repo, 'fetch', '-q', 'origin')

    def fake_gh(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(self.answers[len(self.calls) - 1]),
                                           '')

    def test_a_young_cache_read_at_the_same_trunk_is_used(self):
        host = sources.GitHubHost(self.product, run=self.fake_gh)
        self.assertEqual(host.prs(), [{'number': 7, 'state': 'OPEN'}])
        self.assertEqual(host.prs(), [{'number': 7, 'state': 'OPEN'}])
        self.assertEqual(len(self.calls), 1)

    def test_a_moved_trunk_invalidates_a_young_cache(self):
        host = sources.GitHubHost(self.product, run=self.fake_gh)
        self.assertEqual(host.prs()[0]['state'], 'OPEN')
        self.land('merge-queue: #7 (cloud/T-10001 @ abc)')
        self.assertEqual(host.prs(), [{'number': 7, 'state': 'MERGED'}])
        self.assertEqual(len(self.calls), 2)

    def test_the_cache_file_stays_a_list_of_rows(self):
        """Other readers (the lane's ended-PR view) read the file as it always was."""
        sources.GitHubHost(self.product, run=self.fake_gh).prs()
        with open(os.path.join(env.state_dir(self.product), 'cache-prs.json')) as f:
            data = json.load(f)
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]['number'], 7)

    def test_a_cache_from_before_the_trunk_key_is_read_afresh(self):
        path = os.path.join(env.state_dir(self.product), 'cache-prs.json')
        with open(path, 'w') as f:
            json.dump([{'number': 7, 'state': 'OPEN'}], f)
        host = sources.GitHubHost(self.product, run=self.fake_gh)
        self.assertEqual(host.prs(), [{'number': 7, 'state': 'OPEN'}])
        self.assertEqual(len(self.calls), 1)

    def test_no_readable_trunk_keeps_the_age_rule(self):
        product = env.Product(self.product.name + '-norepo', {'repo_slug': 'org/repo'})
        host = sources.GitHubHost(product, run=self.fake_gh)
        host.prs()
        host.prs()
        self.assertEqual(len(self.calls), 1)


class QueueLandedIsNotOpenTests(unittest.TestCase):
    def test_a_pr_the_merge_queue_landed_is_not_counted_open(self):
        commits = [('a' * 40, 'merge-queue: #1046 (cloud/T-10485 @ 243ce531)'),
                   ('b' * 40, 'feat: something else')]
        self.assertEqual(evidence.queue_landed(commits), {1046})

    def test_open_prs_leaves_out_a_queue_landed_number(self):
        product = env.Product('queue-landed', {'repo_slug': 'org/repo', 'ci': 'none'})
        prs = [{'number': 1046, 'state': 'OPEN', 'title': 'T-10485: the thing',
                'headRefName': 'cloud/T-10485'},
               {'number': 1047, 'state': 'OPEN', 'title': 'T-10486: another',
                'headRefName': 'cloud/T-10486'}]
        commits = [('a' * 40, 'merge-queue: #1046 (cloud/T-10485 @ 243ce531)', [])]
        out = evidence.id_evidence(product, [], prs, commits=commits, green=[])
        self.assertEqual((out.get('T-10485') or {}).get('open_prs') or [], [])
        self.assertEqual(out['T-10486']['open_prs'], [1047])


class TrunkLandedRowTests(unittest.TestCase):
    SHA = 'a' * 40

    def test_landed_prs_reads_all_three_subject_forms(self):
        for subject in ('merge-queue: #1046 (cloud/T-10485 @ 243ce531)',
                        'feat: a thing (#1046)',
                        'Merge pull request #1046 from org/b'):
            self.assertEqual(trunk_watch.landed_prs([{'sha': self.SHA, 'subject': subject}]),
                             {1046: self.SHA})

    def test_landed_prs_is_empty_off_a_subject_naming_no_pr(self):
        self.assertEqual(
            trunk_watch.landed_prs([{'sha': self.SHA, 'subject': 'chore: nothing'}]), {})

    def test_landed_prs_takes_both_row_shapes(self):
        subject = 'merge-queue: #1046 (cloud/T-10485 @ 243ce531)'
        self.assertEqual(trunk_watch.landed_prs([{'sha': self.SHA, 'subject': subject}]),
                         {1046: self.SHA})
        self.assertEqual(trunk_watch.landed_prs([(self.SHA, subject, ['f.txt'])]),
                         {1046: self.SHA})

    def test_the_newest_row_wins_when_two_rows_name_one_number(self):
        newest, oldest = 'n' * 40, 'o' * 40
        rows = [{'sha': newest, 'subject': 'merge-queue: #1046 (cloud/T-10485 @ 243ce531)'},
                {'sha': oldest, 'subject': 'feat: a thing (#1046)'}]
        self.assertEqual(trunk_watch.landed_prs(rows), {1046: newest})

    def test_chosen_pr_prefers_the_merged_candidate_over_a_trunk_landed_one(self):
        cand_prs = [{'number': 1046, 'state': 'MERGED'}]
        merged = {1046: 'mergedsha'}
        landed = {1046: 'trunksha'}
        self.assertEqual(evidence.chosen_pr(cand_prs, merged, landed), (1046, 'MERGED', 'mergedsha'))

    def test_chosen_pr_reads_a_trunk_landed_candidate_as_merged(self):
        cand_prs = [{'number': 1046, 'state': 'OPEN'}]
        self.assertEqual(evidence.chosen_pr(cand_prs, {}, {1046: self.SHA}),
                         (1046, 'MERGED', self.SHA))

    def test_chosen_pr_falls_back_to_the_newest_not_closed_candidate(self):
        self.assertEqual(evidence.chosen_pr([{'number': 1046, 'state': 'OPEN'}], {}, {}),
                         (1046, 'OPEN', None))
        self.assertEqual(evidence.chosen_pr([{'number': 1046, 'state': 'CLOSED'}], {}, {}),
                         (None, None, None))
        self.assertEqual(evidence.chosen_pr([], {}, {}), (None, None, None))

    def test_derived_states_before_and_after(self):
        self.assertEqual(
            closing.state_of('task', closing.Ev(merged_sha=self.SHA, green=True,
                                                pr_state='MERGED')),
            closing.Closing('Closed', 'landed-green'))
        self.assertEqual(closing.state_of('task', closing.Ev(pr_state='OPEN')),
                         closing.Closing('Active', 'in-flight'))
        self.assertEqual(closing.state_of('task', closing.Ev(open_prs=(1046,))),
                         closing.Closing('New', 'planned'))

    def test_discover_reads_a_trunk_landed_pr_as_merged_not_open(self):
        tmp = tempfile.mkdtemp(prefix='asf-pr-landed-')
        self.addCleanup(shutil.rmtree, tmp, True)
        origin = os.path.join(tmp, 'origin.git')
        repo = os.path.join(tmp, 'repo')
        git(tmp, 'init', '-q', '--bare', '-b', 'main', origin)
        git(tmp, 'clone', '-q', origin, repo)
        git(repo, 'config', 'core.hooksPath', os.devnull)

        def land(path, content, subject):
            full = os.path.join(repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'a') as f:
                f.write(content + '\n')
            git(repo, 'add', '-A')
            git(repo, 'commit', '-q', '-m', subject)
            git(repo, 'push', '-q', 'origin', 'main')
            return git(repo, 'rev-parse', 'HEAD')

        land('docs/plans/sample.md', '### Task 1\nthe first task.\n### Task 2\nthe second task.',
             'seed: plan')
        merge_sha = land('f.txt', 'x', 'merge-queue: #1046 (cloud/sample-t1 @ 243ce531)')

        name = self.id().rsplit('.', 1)[-1]
        product = env.Product(name, {'repo_slug': 'org/repo', 'repo_dir': repo, 'main': 'main',
                                     'ci': 'none'})
        prs = [{'number': 1046, 'state': 'OPEN', 'headRefName': 'worker/sample-t1'},
              {'number': 2000, 'state': 'OPEN', 'headRefName': 'worker/sample-t2'}]
        checked_file = os.path.join(tmp, 'none.txt')
        with mock.patch.object(evidence, 'pr_list', return_value=prs):
            ev1 = evidence.discover(product=product, checked_file=checked_file)
            ev2 = evidence.discover(product=product, checked_file=checked_file)

        landed_row = ev1['features']['sample']['tasks']['T1']
        self.assertEqual(landed_row['pr_state'], 'MERGED')
        self.assertEqual(landed_row['merged_sha'], merge_sha)
        self.assertEqual(landed_row['pr'], 1046)

        # a PR the trunk does not name keeps today's pr, pr_state and merged_sha
        untouched_row = ev1['features']['sample']['tasks']['T2']
        self.assertEqual(untouched_row['pr'], 2000)
        self.assertEqual(untouched_row['pr_state'], 'OPEN')
        self.assertIsNone(untouched_row['merged_sha'])

        # a second pass over the same trunk reads the same row
        self.assertEqual(ev2['features']['sample']['tasks']['T1'], landed_row)
        self.assertEqual(ev2['features']['sample']['tasks']['T2'], untouched_row)

        meta = frontmatter.FrontmatterDict(
            {'id': 'T-10485', 'type': 'task', 'title': 'T-10485 item',
            'links': {'prs': [1046]}, 'state': 'New'})
        meta.machine_keys = {'state'}
        canonical = {'T-10485': {'meta': meta}}
        _new_state, closings, _derived, _stage, _task_ev, _evs = ingest.derive(
            canonical, ev1, product=product)
        self.assertEqual(closings['T-10485'].lines, (f"PR #1046 merged ({merge_sha[:9]})",))


if __name__ == '__main__':
    unittest.main()
