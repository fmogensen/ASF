"""A merge fact stands only when the branch's tip is really on the trunk
(:func:`asf.evidence.evidence.merge_facts`).

2026-10-05: evidence closed a Task on "merge 062ad27 of <its branch>" — a lane record saying the
branch was ``on-trunk`` at the trunk's tip of the day, with no head behind it. 062ad27 was another
PR's merge, and the branch's tip was on no trunk commit; ``asf reopen`` could not undo it because
evidence derived Closed again from the same record.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf.evidence import evidence

PREFIXES = {'code': 'cloud/', 'fix': 'cloud/fix-', 'spec': 'cloud/spec-',
            'plan': 'cloud/plan-', 'legacy': []}
IDENT = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't',
         'GIT_COMMITTER_EMAIL': 't@x'}


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**os.environ, **IDENT}).stdout.strip()


class TipOnMain(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='ev-tip-')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        origin = os.path.join(self.base, 'origin.git')
        self.repo = os.path.join(self.base, 'repo')
        git(self.base, 'init', '-q', '--bare', '-b', 'main', origin)
        git(self.base, 'clone', '-q', origin, self.repo)
        git(self.repo, 'checkout', '-q', '-b', 'main')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'root')
        git(self.repo, 'push', '-q', 'origin', 'main')
        # the Task's branch: work that never landed
        git(self.repo, 'checkout', '-q', '-b', 'cloud/T-0071')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'task(T-0071): work')
        self.unlanded = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'push', '-q', 'origin', 'cloud/T-0071')
        # another PR's merge lands on main: the trunk tip of the day
        git(self.repo, 'checkout', '-q', 'main')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'Merge pull request #927 from o/hotfix')
        self.trunk = git(self.repo, 'rev-parse', 'HEAD')
        # a landed branch: its tip is on main
        git(self.repo, 'checkout', '-q', '-b', 'cloud/T-0006')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'task(T-0006): work')
        self.landed = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', '-q', 'main')
        git(self.repo, 'merge', '-q', '--ff-only', 'cloud/T-0006')
        git(self.repo, 'push', '-q', 'origin', 'main', 'cloud/T-0006')
        git(self.repo, 'fetch', '-q', 'origin')
        self.path = os.path.join(self.base, 'sessions.jsonl')
        self.product = mock.Mock(repo_dir=self.repo, main='main', conventions=mock.Mock())

    def ledger(self, *runs):
        with open(self.path, 'a', encoding='utf-8') as fh:
            for item, branch, lane in runs:
                job = f'coder-{item.lower()}'
                fh.write(json.dumps({'job': job, 'item': item, 'kind': 'coder', 'pid': 1,
                                     'branch': branch, 'started': '2026-09-28T00:00:00Z'}) + '\n')
                fh.write(json.dumps({'job': job, 'ended': '2026-09-28T01:00:00Z',
                                     'end_reason': 'finished', 'harvested': lane['sha'],
                                     'lane': dict(lane, state='MERGED')}) + '\n')

    def facts(self):
        with mock.patch.object(evidence, 'branch_prefixes', lambda _p: PREFIXES):
            return evidence.merge_facts(self.product, path=self.path)['code']

    def test_an_on_trunk_record_whose_branch_tip_is_not_on_main_is_no_merge_fact(self):
        self.ledger(('T-0071', 'cloud/T-0071',
                     {'method': 'on-trunk', 'head': None, 'pr': None, 'sha': self.trunk}))
        self.assertNotIn('T-0071', self.facts())

    def test_an_on_trunk_record_whose_named_head_is_not_on_main_is_no_merge_fact(self):
        self.ledger(('T-0071', 'cloud/T-0071',
                     {'method': 'on-trunk', 'head': self.unlanded, 'pr': None,
                      'sha': self.trunk}))
        self.assertNotIn('T-0071', self.facts())

    def test_an_on_trunk_record_whose_tip_is_on_main_stands(self):
        self.ledger(('T-0006', 'cloud/T-0006',
                     {'method': 'on-trunk', 'head': self.landed, 'pr': None,
                      'sha': self.landed}))
        self.assertEqual(self.facts()['T-0006']['sha'], self.landed)

    def test_a_branch_gone_from_origin_with_no_head_proves_nothing(self):
        self.ledger(('T-0099', 'cloud/T-0099',
                     {'method': 'on-trunk', 'head': None, 'pr': None, 'sha': self.trunk}))
        self.assertNotIn('T-0099', self.facts())

    def test_a_pr_the_host_merged_stands_as_before(self):
        self.ledger(('T-0072', 'cloud/T-0072',
                     {'method': 'squash', 'head': 'f' * 40, 'pr': 966, 'sha': self.trunk}))
        self.assertEqual(self.facts()['T-0072']['pr'], 966)


if __name__ == '__main__':
    unittest.main()
