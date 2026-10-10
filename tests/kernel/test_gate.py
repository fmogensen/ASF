"""``asf kernel gate``: each criterion from a saved plan, a session ledger and a fake GitHub."""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.kernel import gate as G

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 10, 14, 0, tzinfo=UTC)


class FakeGitHub:

    def __init__(self, prs, heads=None, checks=None):
        self._prs, self.heads, self._checks = prs, heads or {}, checks or {}
        self.calls = []

    def prs(self, since):
        self.calls.append(('prs', since))
        return self._prs

    def first_head(self, pr):
        self.calls.append(('head', pr['number']))
        return self.heads.get(pr['number'])

    def checks(self, sha):
        self.calls.append(('checks', sha))
        return self._checks.get(sha, [])


def ok(name='gate'):
    return {'name': name, 'status': 'completed', 'conclusion': 'success'}


def red(name='gate'):
    return {'name': name, 'status': 'completed', 'conclusion': 'failure'}


def row(item, started, job=None, branch=None):
    return {'job': job or 'build-%s-%d' % (item.lower(), 1), 'item': item,
            'branch': branch or 'worker/%s' % item, 'started': started}


def pr(n, item, created, merged=None):
    return {'number': n, 'headRefName': 'worker/%s' % item, 'createdAt': created,
            'mergedAt': merged}


class Gate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.product = env.Product('sample', {'repo_slug': 'o/r', 'conventions': {
            'landing_checks': ['gate']}, 'kernel': {'gate': {
                'since': '2026-10-09T14:00:00Z', 'landed_min': 2, 'first_push_green_min': 0.5}}})

    def evaluate(self, gh, rows, plan=None, pushes=None):
        table, _start, _land = G.evaluate(self.product, gh, rows, plan or {}, self.tmp, now=NOW,
                                          pushes=pushes)
        return {c: (v, ok) for c, v, _t, ok in table}

    def test_every_criterion_passes_and_fails_on_its_own_numbers(self):
        rows = [row('T-0001', '2026-10-10T01:00:00Z'), row('B-0002', '2026-10-10T02:00:00Z'),
                row('T-0003', '2026-10-10T03:00:00Z'),
                row('T-0009', '2026-10-08T03:00:00Z'),          # before since: not counted
                {'job': 'w-legacy-7', 'item': 'T-0004', 'branch': 'worker/T-0004',
                 'started': '2026-10-10T04:00:00Z'}]             # not a kernel session
        prs = [pr(1, 'T-0001', '2026-10-10T01:30:00Z', '2026-10-10T05:00:00Z'),
               pr(2, 'B-0002', '2026-10-10T02:30:00Z', '2026-10-10T06:00:00Z'),
               pr(3, 'T-0003', '2026-10-10T03:30:00Z'),
               pr(4, 'T-0004', '2026-10-10T04:30:00Z', '2026-10-10T07:00:00Z')]
        gh = FakeGitHub(prs, heads={1: 'a', 2: 'b', 3: 'c', 4: 'd'},
                        checks={'a': [ok()], 'b': [red(), ok('lint')], 'c': [ok(), red('lint')],
                                'd': [ok()]})
        plan = {'states': {'T-0010': {'state': 'stuck', 'reason': 'conflict', 'owner': 'operator'},
                           'T-0011': {'state': 'stuck', 'reason': '', 'owner': 'loop'},
                           'T-0012': {'state': 'ready'}}}
        got = self.evaluate(gh, rows, plan)
        self.assertEqual(got['silent stuck'], ('1', False))
        self.assertEqual(got['owned stuck'], ('1', None))
        self.assertEqual(got['first-push green'], ('2/3 = 67%', True))
        self.assertEqual(got['landed Tasks/Bugs'], ('2', True))

    def test_the_first_logged_push_is_the_head_and_a_verdict_is_cached(self):
        rows = [row('T-0001', '2026-10-10T01:00:00Z')]
        gh = FakeGitHub([pr(1, 'T-0001', '2026-10-10T01:30:00Z')], heads={1: 'later'},
                        checks={'first': [red()], 'later': [ok()]})
        got = self.evaluate(gh, rows, pushes=lambda job: ['first', 'second'])
        self.assertEqual(got['first-push green'], ('0/1 = 0%', False))
        self.assertNotIn(('head', 1), gh.calls)
        with open(os.path.join(self.tmp, G.CACHE_FILE)) as f:
            self.assertEqual(json.load(f)['1']['sha'], 'first')
        gh.calls.clear()
        self.evaluate(gh, rows)
        self.assertEqual([c for c in gh.calls if c[0] == 'checks'], [])

    def test_a_head_still_running_is_not_judged_nor_cached(self):
        rows = [row('T-0001', '2026-10-10T01:00:00Z')]
        running = {'name': 'gate', 'status': 'in_progress', 'conclusion': None}
        gh = FakeGitHub([pr(1, 'T-0001', '2026-10-10T01:30:00Z')], heads={1: 'a'},
                        checks={'a': [running]})
        got = self.evaluate(gh, rows)
        self.assertEqual(got['first-push green'], ('0/0 = 0%', False))
        with open(os.path.join(self.tmp, G.CACHE_FILE)) as f:
            self.assertEqual(json.load(f), {})

    def test_no_named_landing_check_means_every_check(self):
        self.assertTrue(G.head_verdict([ok('a'), ok('b'), {'name': 'c', 'status': 'completed',
                                                          'conclusion': 'skipped'}], []))
        self.assertFalse(G.head_verdict([ok('a'), red('b')], []))
        self.assertIsNone(G.head_verdict([], ['gate']))

    def test_gate_prints_the_table_and_exits_by_the_verdict(self):
        lines = []
        with mock.patch.object(env, 'ASF_HOME', self.tmp):
                rc = G.gate(self.product, out=lines.append, gh=FakeGitHub([]), now=NOW,
                        state_dir=self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn('| landed Tasks/Bugs | 0 | ≥ 2 | FAIL |', lines[0])
        self.assertIn('| silent stuck | 0 | ≤ 0 | PASS |', lines[0])


if __name__ == '__main__':
    unittest.main()
