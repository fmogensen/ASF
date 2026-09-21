import json
import os
import shutil
import sys
import tempfile
import unittest

from asf.record import core as backlog  # noqa: F401  (pr_hygiene imports it; fail here rather than there)
from asf.harvest import pr_hygiene as ph

DAY = 86400
NOW = ph.ts('2026-09-21T14:30:00Z')

# A fake product, so these tests exercise pr_hygiene without needing a real ~/.ASF/ — see
# asf.env.Product's docstring: tests build one directly from a plain dict.
FAKE_PRODUCT = ph.env.Product('sample', {'repo_slug': 'acme/widgets', 'repo_dir': '/tmp/acme-widgets-checkout'})

ITEMS = {
    'T-0002': {'id': 'T-0002', 'type': 'task', 'links': {'branches': ['cloud/computer-per-bot-t2'], 'prs': [680]}},
    'T-0003': {'id': 'T-0003', 'type': 'task', 'links': {'branches': ['cloud/free-plan-t1'], 'prs': [681]}},
    'T-0004': {'id': 'T-0004', 'type': 'task', 'links': {'branches': ['cloud/vox-t1'], 'prs': [682]}},
    'T-0005': {'id': 'T-0005', 'type': 'task', 'links': {'branches': ['cloud/fact-t3'], 'prs': [683]}},
}


def pull(n, branch, updated, draft=False, base='main'):
    return {'number': n, 'title': f'PR {n}', 'body': '', 'draft': draft, 'base': {'ref': base},
            'head': {'ref': branch, 'sha': f'sha{n}'}, 'updated_at': updated}


def prs_fixture(dirty=('dirty', 'dirty', 'dirty', 'clean'), decisions=None):
    pulls = [pull(680, 'cloud/computer-per-bot-t2', '2026-09-21T13:00:00Z'),   # approved
             pull(681, 'cloud/free-plan-t1', '2026-09-13T10:00:00Z'),          # unreviewed, 8 d
             pull(682, 'cloud/vox-t1', '2026-09-19T10:00:00Z'),                # unreviewed, 2 d
             pull(683, 'cloud/fact-t3', '2026-09-13T10:00:00Z')]               # approved, clean
    details = {p['number']: {'mergeable_state': s} for p, s in zip(pulls, dirty)}
    return ph.normalize(pulls, details, decisions or {})


VERDICTS = {'cloud/computer-per-bot-t2': 'APPROVED', 'cloud/fact-t3': 'APPROVED'}


def run_classify(prs, state=None, now=NOW, verdicts=VERDICTS, items=ITEMS):
    return ph.classify(prs, items, lambda b: verdicts.get(b, ''), state or {}, now)


class ClassifyTest(unittest.TestCase):
    def seen_earlier(self, prs):
        _rows, st = run_classify(prs, now=NOW - 2 * ph.TICK_S)
        return st

    def test_approved_dirty_gives_a_rebase_row_after_one_tick(self):
        prs = prs_fixture()
        rows, _ = run_classify(prs, state=self.seen_earlier(prs))
        rebase = [r for r in rows if r['kind'] == 'REBASE']
        self.assertEqual([r['pr']['n'] for r in rebase], [680])
        line = ph.render(rebase[0])
        self.assertEqual(line, 'CONFLICT → REBASE  #680 cloud/computer-per-bot-t2 (T-0002) approved, dirty since 1h'
                               '   → launch rebase-computer-per-bot-t2 (Sonnet)')

    def test_first_sighting_is_not_yet_a_rebase_row(self):
        rows, _ = run_classify(prs_fixture())
        rebase = [r for r in rows if r['kind'] == 'REBASE']
        self.assertEqual(len(rebase), 1)
        self.assertFalse(rebase[0]['ready'])    # in the lane (R-0032 skips it) but draws no row

    def test_unreviewed_dirty_8_days_gives_a_close_row(self):
        prs = prs_fixture()
        rows, _ = run_classify(prs, state=self.seen_earlier(prs))
        close = [r for r in rows if r['kind'] == 'CLOSE']
        self.assertEqual([r['pr']['n'] for r in close], [681])
        self.assertEqual(ph.render(close[0]), 'STALE → CLOSE  #681 cloud/free-plan-t1 (T-0003) unreviewed, dirty 8d   → close')

    def test_unreviewed_dirty_2_days_gives_nothing(self):
        prs = prs_fixture()
        rows, _ = run_classify(prs, state=self.seen_earlier(prs))
        self.assertNotIn(682, [r['pr']['n'] for r in rows])

    def test_approved_clean_gives_nothing_and_no_state(self):
        prs = prs_fixture()
        rows, st = run_classify(prs, state=self.seen_earlier(prs))
        self.assertNotIn(683, [r['pr']['n'] for r in rows])
        self.assertNotIn('683', st)

    def test_review_decision_stands_in_when_the_branch_has_no_review_file(self):
        prs = prs_fixture(decisions={'681': 'APPROVED'})
        rows, _ = run_classify(prs, state=self.seen_earlier(prs))
        self.assertEqual({r['pr']['n']: r['kind'] for r in rows}, {680: 'REBASE', 681: 'REBASE'})

    def test_a_review_that_asked_for_changes_is_in_neither_lane(self):
        prs = prs_fixture()
        rows, _ = run_classify(prs, state=self.seen_earlier(prs), verdicts={'cloud/free-plan-t1': 'CHANGES REQUESTED'})
        self.assertEqual(rows, [])

    def test_a_push_resets_the_clock(self):
        prs = prs_fixture()
        st = self.seen_earlier(prs)
        st['681']['sha'] = 'older-sha'                     # the branch moved since we last looked …
        prs[1]['updated'] = NOW - 60                       # … and a push bumps updated_at
        rows, st2 = run_classify(prs, state=st)
        self.assertEqual([r['pr']['n'] for r in rows if r['kind'] == 'CLOSE'], [])
        self.assertEqual(st2['681']['first_seen'], NOW)

    def test_stale_pr_with_no_task_is_never_a_close_row(self):
        prs = prs_fixture()
        rows, _ = run_classify(prs, state=self.seen_earlier(prs), items={})
        self.assertEqual([r['kind'] for r in rows if r['pr']['n'] == 681], ['NO TASK'])
        self.assertIn('not closed', ph.render([r for r in rows if r['pr']['n'] == 681][0]))

    def test_drafts_and_other_bases_are_skipped(self):
        pulls = [pull(1, 'cloud/a', '2026-09-01T00:00:00Z', draft=True), pull(2, 'cloud/b', '2026-09-01T00:00:00Z', base='dev')]
        self.assertEqual(ph.normalize(pulls, {1: {'mergeable_state': 'dirty'}, 2: {'mergeable_state': 'dirty'}}, {}), [])

    def test_unknown_mergeable_state_is_not_dirty(self):
        prs = prs_fixture(dirty=('unknown',) * 4)
        self.assertFalse(any(p['dirty'] for p in prs))


class VerdictTest(unittest.TestCase):
    def test_own_verdict_line_beats_earlier_words(self):
        text = 'Rounds: 1 — 7 C, CHANGES REQUESTED. 2 — 0 C, APPROVED\n\n**Verdict: APPROVED**\n'
        self.assertEqual(ph.parse_verdict(text), 'APPROVED')
        self.assertEqual(ph.parse_verdict('# r1\nverdict: changes requested\n'), 'CHANGES REQUESTED')
        self.assertEqual(ph.parse_verdict('nothing here'), '')

    def test_newest_round_is_read_from_origin(self):
        calls = []

        def git(*a):
            calls.append(a)
            if a[0] == 'ls-tree':
                return '.sdd-input/reviews/x-review-r2.md\n.sdd-input/reviews/x-review-r10.md\n.sdd-input/reviews/x-writer-report.md\n'
            return 'Verdict: APPROVED' if a[1].endswith('x-review-r10.md') else 'Verdict: BOUNCE'
        self.assertEqual(ph.branch_verdict('cloud/x', git), 'APPROVED')
        self.assertEqual(calls[-1], ('show', 'origin/cloud/x:.sdd-input/reviews/x-review-r10.md'))

    def test_missing_ref_is_no_verdict(self):
        def git(*a):
            raise RuntimeError('fatal: not a valid object name')
        self.assertEqual(ph.branch_verdict('worker/x', git), '')


class CloseTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='prh_')
        os.makedirs(os.path.join(self.root, 'tasks'))
        with open(os.path.join(self.root, 'tasks', 'T-0003.md'), 'w') as f:
            f.write('---\nid: T-0003\ntype: task\ntitle: Free plan\nparent: F-0001\ndecided: true\n'
                    'links:\n  branches: [cloud/free-plan-t1]\n  prs: [681]\n  plan: "docs/p.md#task-1"\n'
                    '# ---- machine ----\nstate: Active\n---\n## Description\n\n## History\n- 2026-09-01: created\n\n## Children\n')
        self.product = ph.env.Product('sample', {'repo_slug': 'acme/widgets', 'backlog_dir': self.root})

    def tearDown(self):
        shutil.rmtree(self.root)

    def read(self):
        with open(os.path.join(self.root, 'tasks', 'T-0003.md')) as f:
            return f.read()

    def test_close_comments_closes_and_clears_the_task_links(self):
        prs = prs_fixture()
        st = ph.classify(prs, ITEMS, lambda b: '', {}, NOW - 2 * ph.TICK_S)[1]
        row = [r for r in run_classify(prs, state=st)[0] if r['kind'] == 'CLOSE'][0]
        calls = []
        task = ph.close_row(row, gh=lambda *a: calls.append(a), now=NOW, product=self.product)
        self.assertEqual(task, 'T-0003')
        self.assertEqual(calls[0][:2], ('api', 'repos/acme/widgets/issues/681/comments'))
        self.assertIn('T-0003', calls[0][3])
        self.assertIn('never reviewed', calls[0][3])
        self.assertEqual(calls[1], ('api', '-X', 'PATCH', 'repos/acme/widgets/pulls/681', '-f', 'state=closed'))
        meta, body = ph.frontmatter.parse(self.read())
        self.assertEqual(dict(meta['links']), {'plan': 'docs/p.md#task-1'})
        self.assertEqual(meta['state'], 'Active')
        self.assertIn('pr-hygiene: PR #681 (cloud/free-plan-t1) closed', body)

    def test_a_failed_close_leaves_the_links(self):
        row = {'kind': 'CLOSE', 'pr': {'n': 681, 'branch': 'cloud/free-plan-t1'}, 'ids': ['T-0003'], 'tasks': ['T-0003'], 'since': 8 * DAY}

        def gh(*a):
            if 'PATCH' in a:
                raise RuntimeError('403')
        with self.assertRaises(RuntimeError):
            ph.close_row(row, gh=gh, now=NOW, product=self.product)
        meta, _ = ph.frontmatter.parse(self.read())
        self.assertEqual(meta['links']['prs'], [681])


class CacheTest(unittest.TestCase):
    def test_reads_are_cached_for_three_minutes(self):
        d = tempfile.mkdtemp(prefix='prh_cache_')
        os.environ['PRH_CACHE_DIR'] = d
        try:
            n = []
            fn = lambda: n.append(1) or {'v': len(n)}
            self.assertEqual(ph.cached('k', fn), {'v': 1})
            self.assertEqual(ph.cached('k', fn), {'v': 1})
            self.assertEqual(len(n), 1)
        finally:
            del os.environ['PRH_CACHE_DIR']
            shutil.rmtree(d)


if __name__ == '__main__':
    unittest.main()
