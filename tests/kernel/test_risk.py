"""The coarse risk flag (Stage 1, part 1c): from the product's config only — ``kernel.risk.high``
(path globs) and ``kernel.risk.large_lines`` (default 800). An item is high when its ``writes``
(or its PR's files) hit a glob, or its PR's diff is over ``large_lines``. Code applies to high
items:

- its review runs on ``strong_model``;
- no second high PR with overlapping writes is in the landing lane at the same time;
- after a high merge, the next high merge waits until the trunk's required checks on that merge
  are green.
"""
import unittest

from asf.kernel import actions as A
from asf.kernel import ports as P
from asf.kernel import settings
from asf.kernel.decide import high_risk, decide

try:
    from kernel import builders as B
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B

State = B.State
M = B.M

GLOBS = ('asf/record/**', '.github/**', 'asf/kernel/**', 'tools/**')


def cfg(**kw):
    kw.setdefault('risk_high', GLOBS)
    kw.setdefault('risk_large_lines', 800)
    kw.setdefault('strong_model', 'strong-1')
    kw.setdefault('required_checks', ('test',))
    return B.config(**kw)


def landing(iid, number, writes, files=None, auto=False):
    it = B.task(iid, state=State.LANDING, writes=list(writes))
    pr = B.pr(number, iid, files=files or writes, auto_merge=auto)
    return it, pr


def approved(*pairs, main=()):
    items = [it for it, _ in pairs]
    prs = [pr for _, pr in pairs]
    return B.facts(items, prs=prs, reviews=[B.review(it.id) for it in items], main=list(main))


def merged_on_main(sha, number, item_id, files, conclusion='success', status='completed'):
    return M.MainCommit(sha=sha, pr=number, branch='worker/%s' % item_id, item_id=item_id,
                        files=list(files),
                        checks=[B.check('test', conclusion=conclusion, status=status)])


def auto_merged(plan):
    return sorted(a.pr for a in B.of(plan, A.EnableAutoMerge))


class Flag(unittest.TestCase):

    def test_writes_or_files_hitting_a_glob_or_a_large_diff_is_high(self):
        c = cfg()
        self.assertTrue(high_risk(B.task('T-1', writes=['asf/kernel/decide.py']), None, c))
        self.assertTrue(high_risk(B.task('T-1', writes=['.github/workflows/ci.yml']), None, c))
        self.assertTrue(high_risk(B.task('T-1', writes=['asf/a.py']),
                                  B.pr(1, 'T-1', files=['tools/x.sh']), c))
        self.assertTrue(high_risk(B.task('T-1', writes=['asf/a.py']),
                                  B.pr(1, 'T-1', files=['asf/a.py'], lines=801), c))
        self.assertFalse(high_risk(B.task('T-1', writes=['asf/a.py']),
                                   B.pr(1, 'T-1', files=['asf/a.py'], lines=800), c))
        self.assertFalse(high_risk(B.task('T-1', writes=['asf/kernel/x.py']), None, B.config()),
                         'no globs: off')

    def test_the_diff_size_is_read_off_the_pr_listing(self):
        self.assertIn('additions', P.PR_FIELDS)
        self.assertIn('deletions', P.PR_FIELDS)
        self.assertEqual(P._lines({'additions': 700, 'deletions': 150}), 850)
        self.assertEqual(P._lines({}), 0)
        self.assertIn('additions deletions', P.MAIN_QUERY)

    def test_settings_default(self):
        k = settings.read(None)
        self.assertEqual(k['risk'], {'high': (), 'large_lines': 800})
        got = settings.read({'risk': {'high': list(GLOBS)}})['risk']['high']
        self.assertEqual(got, list(GLOBS))


class Review(unittest.TestCase):

    def test_a_high_items_review_runs_on_the_strong_model(self):
        items = [B.task('T-1', state=State.REVIEW, writes=['asf/kernel/x.py']),
                 B.task('T-2', state=State.REVIEW, writes=['asf/a.py'])]
        f = B.facts(items, prs=[B.pr(1, 'T-1', files=['asf/kernel/x.py']),
                                B.pr(2, 'T-2', files=['asf/a.py'])])
        plan = decide(f, cfg())
        models = {a.item_id: a.model for a in B.of(plan, A.Launch)}
        self.assertEqual(models, {'T-1': 'strong-1', 'T-2': ''})


class Landing(unittest.TestCase):

    def test_two_high_prs_with_overlapping_writes_land_one_at_a_time(self):
        a = landing('T-1', 1, ['asf/kernel/decide.py'])
        b = landing('T-2', 2, ['asf/kernel/decide.py', 'tests/kernel/test_x.py'])
        plan = decide(approved(a, b), cfg())
        self.assertEqual(auto_merged(plan), [1])
        self.assertEqual(B.state(plan, 'T-2'), State.LANDING)
        self.assertIn('risk: high, overlapping writes: waits for #1 (T-1) to land',
                      plan.notes['T-2'])
        self.assertNotIn('T-2', plan.limbo)

    def test_the_lane_goes_to_the_pr_whose_auto_merge_is_already_on(self):
        a = landing('T-1', 1, ['asf/kernel/decide.py'])
        b = landing('T-2', 2, ['asf/kernel/decide.py'], auto=True)
        plan = decide(approved(a, b), cfg())
        self.assertEqual(auto_merged(plan), [])
        self.assertIn('waits for #2', plan.notes['T-1'][0])

    def test_high_prs_on_disjoint_paths_and_low_ones_land_together(self):
        a = landing('T-1', 1, ['asf/kernel/decide.py'])
        b = landing('T-2', 2, ['.github/workflows/ci.yml'])
        c = landing('T-3', 3, ['asf/a.py'])
        d = landing('T-4', 4, ['asf/a.py'])
        self.assertEqual(auto_merged(decide(approved(a, b, c, d), cfg())), [1, 2, 3, 4])

    def test_after_a_high_merge_the_next_high_waits_for_a_green_trunk(self):
        a = landing('T-1', 1, ['asf/record/core.py'])
        c = landing('T-3', 3, ['asf/a.py'])
        for status, conclusion, want in (('in_progress', None, [3]),
                                         ('completed', 'failure', [3]),
                                         ('completed', 'success', [1, 3])):
            main = [merged_on_main('abc123456', 9, 'T-9', ['asf/kernel/apply.py'],
                                   conclusion=conclusion, status=status)]
            plan = decide(approved(a, c, main=main), cfg())
            self.assertEqual(auto_merged(plan), want, (status, conclusion))
            if want == [3]:
                self.assertIn('risk: after high merge abc123456 (#9) the trunk is not green yet',
                              plan.notes['T-1'])

    def test_a_newer_green_trunk_commit_proves_the_high_merge_green(self):
        # F-0337 (2026-10-10): the high merge's own CI never ran (a newer push superseded it);
        # the newer trunk commit carries that change and is green, so the next high PR lands
        a = landing('T-1', 1, ['asf/record/core.py'])
        main = [merged_on_main('def456789', 10, 'T-10', ['asf/a.py']),
                M.MainCommit(sha='abc123456', pr=9, branch='worker/T-9', item_id='T-9',
                             files=['asf/kernel/apply.py'], checks=[])]
        self.assertEqual(auto_merged(decide(approved(a, main=main), cfg())), [1])

    def test_a_low_merge_on_main_holds_nothing(self):
        a = landing('T-1', 1, ['asf/record/core.py'])
        main = [merged_on_main('abc', 9, 'T-9', ['asf/a.py'], status='in_progress')]
        self.assertEqual(auto_merged(decide(approved(a, main=main), cfg())), [1])

    def test_a_held_behind_pr_gets_no_train_update(self):
        a = landing('T-1', 1, ['asf/kernel/decide.py'], auto=True)
        b = landing('T-2', 2, ['asf/kernel/decide.py'], auto=True)
        b[1].behind = True
        plan = decide(approved(a, b), cfg())
        self.assertEqual(B.of(plan, A.UpdateBranch), [])


if __name__ == '__main__':
    unittest.main()
