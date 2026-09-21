import json
import os
import shutil
import tempfile
import unittest

from asf.record import match


def fixture():
    """A small index: E-0010 > F-0042 (FREE-1) > S-0187, T-0913 (FREE-1/T3), T-0914 (FREE-1/T4);
    D-0292 a decision; B-0007 a bug on the feature."""
    return {
        'E-0010': {'id': 'E-0010', 'type': 'epic', 'title': 'Billing', 'children': ['F-0042']},
        'F-0042': {'id': 'F-0042', 'type': 'feature', 'title': 'Free plan', 'parent': 'E-0010',
                   'legacy_id': 'FREE-1', 'children': ['S-0187', 'T-0913', 'B-0007'],
                   'links': {'prs': [601], 'branches': ['cloud/spec-free-plan']}},
        'S-0187': {'id': 'S-0187', 'type': 'story', 'title': 'Sign-up without a card', 'parent': 'F-0042',
                   'children': ['T-0914']},
        'T-0913': {'id': 'T-0913', 'type': 'task', 'title': 'Plan door copy', 'parent': 'F-0042',
                   'legacy_id': 'FREE-1/T3', 'children': [], 'links': {'prs': [623], 'branches': ['cloud/free-plan-t3']}},
        'T-0914': {'id': 'T-0914', 'type': 'task', 'title': 'No legacy', 'parent': 'S-0187', 'children': []},
        'B-0007': {'id': 'B-0007', 'type': 'bug', 'title': 'Trial banner shows on Free', 'parent': 'F-0042',
                   'children': []},
        'D-0292': {'id': 'D-0292', 'type': 'decision', 'title': 'Ruling', 'children': []},
    }


class MatchRules(unittest.TestCase):
    def setUp(self):
        self.items = fixture()

    def m(self, **kw):
        return match.match_event(self.items, **kw)

    def test_rule1_id_token_in_title_body_branch_task(self):
        self.assertEqual(self.m(title='Fix T-0913 copy')[0], ['T-0913'])
        self.assertEqual(self.m(body='closes B-0007\nand S-0187')[0], ['B-0007', 'S-0187'])
        self.assertEqual(self.m(branch='cloud/S-0187-signup')[0], ['S-0187'])
        self.assertEqual(self.m(task='fix-T-0913-r1')[0], ['T-0913'])

    def test_rule1_never_invents_an_id(self):
        ids, why = self.m(title='see T-9999 and X-0001')
        self.assertEqual(ids, [])
        self.assertIn('no rule matched', why)

    def test_rule1_beats_later_rules(self):
        # the PR is in F-0042's links.prs but the title names T-0914: the token wins
        self.assertEqual(self.m(pr=601, title='T-0914 do it')[0], ['T-0914'])

    def test_decision_token_alone_is_a_fallback_only(self):
        self.assertEqual(self.m(pr=623, body='per [[D-0292]]')[0], ['T-0913'])
        ids, why = self.m(body='per D-0292')
        self.assertEqual(ids, ['D-0292'])
        self.assertIn('decision', why)

    def test_rule2_links_prs(self):
        self.assertEqual(self.m(pr=623, branch='whatever')[0], ['T-0913'])
        self.assertEqual(self.m(pr='601')[0], ['F-0042'])

    def test_rule3_links_branches(self):
        self.assertEqual(self.m(branch='origin/cloud/spec-free-plan')[0], ['F-0042'])
        self.assertEqual(self.m(branch='cloud/free-plan-t3')[0], ['T-0913'])

    def test_rule4_legacy_spec_slug(self):
        self.items['F-0042']['links'] = {}
        ids, why = self.m(branch='cloud/spec-free-plan')
        self.assertEqual((ids, why), (['F-0042'], 'legacy_id'))
        self.assertEqual(self.m(task='review-spec-free-plan-r2')[0], ['F-0042'])

    def test_rule4_legacy_id_itself_and_task_slug(self):
        self.items['T-0913']['links'] = {}
        self.assertEqual(self.m(branch='cloud/free-1')[0], ['F-0042'])
        self.assertEqual(self.m(branch='cloud/free-plan-t3')[0], ['T-0913'])
        self.assertEqual(self.m(task='fix-free-plan-t3-r1')[0], ['T-0913'])

    def test_rule4_task_missing_falls_back_to_feature(self):
        self.items['T-0913']['links'] = {}
        self.assertEqual(self.m(branch='cloud/free-plan-t9')[0], ['F-0042'])

    def test_rule5_review_file(self):
        self.items['F-0042']['links'] = {}
        for f in ('.sdd-input/reviews/free-plan-review-r2.md', '.sdd-input/reviews/spec-free-plan-r1.md'):
            self.assertEqual(self.m(branch='cloud/unrelated', files=[f])[0], ['F-0042'], f)

    def test_no_match_says_why(self):
        ids, why = self.m(branch='cloud/nothing-here', task='code-x')
        self.assertEqual(ids, [])
        self.assertIn('branch', why)
        self.assertEqual(self.m()[1], 'nothing to match on')

    def test_batch_is_the_union_of_its_prs(self):
        ids, why = self.m(prs=[623, 601, 999], branch='worktree-m-batch-20260921-0101')
        self.assertEqual(ids, ['F-0042', 'T-0913'])
        self.assertTrue(why.startswith('batch'))

    def test_batch_uses_pr_titles(self):
        info = {700: {'title': 'B-0007 banner', 'body': '', 'branch': 'cloud/x'}}
        self.assertEqual(self.m(prs=[700], pr_info=info)[0], ['B-0007'])

    def test_batch_with_no_match(self):
        ids, why = self.m(prs=[5, 6])
        self.assertEqual(ids, [])
        self.assertIn('2 PR', why)


class LoadIndex(unittest.TestCase):
    def test_load_index(self):
        d = tempfile.mkdtemp()
        try:
            self.assertEqual(match.load_index(d), {})
            with open(os.path.join(d, 'index.json'), 'w') as f:
                json.dump({'items': {'E-0001': {'id': 'E-0001'}}}, f)
            self.assertEqual(list(match.load_index(d)), ['E-0001'])
        finally:
            shutil.rmtree(d)


if __name__ == '__main__':
    unittest.main()
