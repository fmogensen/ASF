"""Origin's expired heads are deleted by rule, never by hand (:mod:`asf.workers.retention`).

One fixture: a bare origin whose heads carry tips of known ages — the lane's ``archive/*``, a
retired worker system's ``hb/*``, retired ``docs/`` and ``worker/`` heads, an active lane head,
a release, one head nothing owns — and a clone the sweep runs in."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from asf import conventions, doctor, env
from asf.workers import pool as pool_mod
from asf.workers import retention

try:
    from tests.gitfixture import Template
except ImportError:  # run from inside tests/
    from gitfixture import Template

DAY = 86400
NOW = int(time.time())

#: head → age in days of its tip
HEADS = {
    'archive/cloud/old': 20,      # archive past 14d: due
    'archive/cloud/fresh': 3,     # archive, young: kept
    'archive/cloud/held': 20,     # its branch has a run in flight: kept
    'hb/dead': 10,                # legacy past 7d: due
    'hb/live': 1,                 # legacy, young: kept
    'hb/protected': 30,           # protected on the host: never
    'worker/pr': 10,              # an open PR carries it: kept
    'docs/named': 10,             # an open item names it: kept
    'docs/closed': 12,            # named only by a closed item: due
    'cloud/active': 30,           # the product's own lane prefix: never a legacy head
    'release/1.0': 30,            # a release: never
    'analysis/x': 30,             # no pattern owns it: reported, not deleted
    'legacy/T-0009': 30,          # an id whose card is Closed, under no owned prefix: settled
    'legacy/T-0010': 30,          # an id whose card is Open: not settled, not held → kept
    'legacy/T-0011': 2,           # a Closed card but a young tip: under settled_days → kept
    'integration/backfill': 30,   # no id, no card; the trunk does not carry it → never settled
}
DUE = ['archive/cloud/old', 'docs/closed', 'hb/dead']


def _build(root):
    def git(*args, cwd, env_=None):
        return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True,
                              text=True, env=env_).stdout.strip()
    origin, wt = os.path.join(root, 'origin.git'), os.path.join(root, 'wt')
    git('init', '-q', '--bare', '-b', 'main', origin, cwd=root)
    git('clone', '-q', origin, wt, cwd=root)
    for k, v in (('user.name', 'T'), ('user.email', 't@example.com'), ('commit.gpgsign', 'false')):
        git('config', k, v, cwd=wt)
    with open(os.path.join(wt, 'f'), 'w') as f:
        f.write('base\n')
    git('add', '-A', cwd=wt)
    git('commit', '-qm', 'seed', cwd=wt)
    git('push', '-q', 'origin', 'HEAD:main', cwd=wt)
    tree = git('rev-parse', 'HEAD^{tree}', cwd=wt)
    for head, days in HEADS.items():
        stamp = f'{NOW - days * DAY} +0000'
        e = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        sha = git('commit-tree', tree, '-p', 'HEAD', '-m', head, cwd=wt, env_=e)
        git('push', '-q', 'origin', f'{sha}:refs/heads/{head}', cwd=wt)


REPO = Template(_build, prefix='retention_')


class RetentionSweep(unittest.TestCase):
    def setUp(self):
        self.root = REPO.fresh()
        self.wt = os.path.join(self.root, 'wt')
        self.home = tempfile.mkdtemp(prefix='retention_home_')
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.object(env, 'ASF_HOME', self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.product = self.make({})
        pool_mod.append_session(self.product, {'job': 'j1', 'item': 'T-0001', 'pid': 1,
                                               'branch': 'cloud/held', 'started': 't'})
        self.items = {'F-0001': {'state': 'Open', 'branch': 'docs/named'},
                      'F-0002': {'state': 'Closed', 'note': 'kept on docs/closed'},
                      'T-0009': {'state': 'Closed'},
                      'T-0010': {'state': 'Open', 'note': 'still open'},
                      'T-0011': {'state': 'Closed'}}
        self.host = ({'worker/pr'}, {'hb/protected'})

    def make(self, retention_over):
        rules = {'legacy_prefixes': ['hb/', 'docs/', 'worker/'], **retention_over}
        return env.Product('sample', {
            'repo_dir': self.wt, 'main': 'main',
            'conventions': {'branch_prefixes': {'code': 'cloud/'}, 'branch_retention': rules,
                            'branch_patterns': {'hotfix': '*hotfix*'}}})

    def heads(self):
        out = subprocess.run(['git', 'ls-remote', '--heads', 'origin'], cwd=self.wt,
                             capture_output=True, text=True, check=True).stdout
        return {l.split()[1][len('refs/heads/'):] for l in out.splitlines()}

    def sweep(self, product=None, fix=True, host='default'):
        lines = []
        res = retention.sweep(product or self.product, fix=fix, out=lines.append,
                              items=self.items, host=self.host if host == 'default' else host,
                              now=NOW)
        return res, lines

    def test_a_dry_run_names_what_would_go_and_deletes_nothing(self):
        before = self.heads()
        res, lines = self.sweep(fix=False)
        self.assertEqual(self.heads(), before)
        self.assertEqual(sorted(res['due']), DUE)
        would = sorted(l.split()[3] for l in lines if l.startswith('retention: would delete'))
        self.assertEqual(would, DUE)
        self.assertEqual(res['deleted'], [])

    def test_expired_archive_and_legacy_heads_are_deleted_one_line_each(self):
        res, lines = self.sweep()
        self.assertEqual(sorted(res['deleted']), DUE)
        left = self.heads()
        for b in DUE:
            self.assertNotIn(b, left)
            self.assertEqual(sum(1 for l in lines if l.startswith(f'retention: deleted {b} (')), 1)
        for kept in HEADS.keys() - set(DUE):
            self.assertIn(kept, left)
        self.assertIn('main', left)
        why = dict(res['kept'])
        self.assertEqual(why['archive/cloud/held'], 'in-flight run')
        self.assertEqual(why['worker/pr'], 'open PR')
        self.assertEqual(why['docs/named'], 'named by an open item')

    def test_archive_days_is_the_archive_rule(self):
        res, _ = self.sweep(product=self.make({'archive_days': 30}), fix=False)
        self.assertNotIn('archive/cloud/old', res['due'])
        res, _ = self.sweep(product=self.make({'archive_days': 2}), fix=False)
        self.assertIn('archive/cloud/fresh', res['due'])
        self.assertNotIn('archive/cloud/held', res['due'])  # still its run's

    def test_legacy_heads_go_only_under_a_configured_prefix(self):
        res, _ = self.sweep(product=self.make({'legacy_prefixes': ['hb/']}), fix=False)
        self.assertEqual(sorted(res['due']), ['archive/cloud/old', 'hb/dead'])

    def test_a_prefix_the_product_still_mints_is_never_legacy(self):
        res, _ = self.sweep(product=self.make({'legacy_prefixes': ['cloud/']}), fix=False)
        self.assertNotIn('cloud/active', res['due'])

    def test_per_tick_caps_the_deletes_oldest_first(self):
        res, lines = self.sweep(product=self.make({'per_tick': 1}))
        self.assertEqual(res['deleted'], ['archive/cloud/old'])  # 20d, the oldest due
        self.assertEqual(sorted(res['due']), ['docs/closed', 'hb/dead'])
        self.assertTrue(any('2 more expired, left for the next pass' in l for l in lines))
        res, _ = self.sweep(product=self.make({'per_tick': 1}))
        self.assertEqual(res['deleted'], ['docs/closed'])

    def test_an_unreadable_pr_host_deletes_nothing(self):
        before = self.heads()
        res, lines = self.sweep(host=(None, set()))
        self.assertEqual(self.heads(), before)
        self.assertEqual(res['deleted'], [])
        self.assertTrue(any('did not answer' in l for l in lines))

    def test_the_trunk_release_and_protected_heads_are_never_candidates(self):
        res, _ = self.sweep(product=self.make({'legacy_prefixes': ['hb/', 'release/'],
                                               'legacy_days': 1}), fix=False)
        self.assertNotIn('hb/protected', res['due'])
        self.assertNotIn('release/1.0', res['due'])
        self.assertNotIn('main', res['due'])

    def test_unowned_heads_are_counted_and_the_doctor_names_them(self):
        res, lines = self.sweep(fix=False)
        self.assertEqual(res['unowned'], {'legacy/': 3, 'analysis/': 1, 'integration/': 1})
        self.assertIn('retention: unowned branches: 5 (prefixes legacy/ 3, analysis/ 1, '
                      'integration/ 1)', lines)
        ok, line = doctor.check_branches(self.product)
        self.assertFalse(ok)
        self.assertEqual(line, retention.census_line(retention.read_state(self.product)))
        self.assertIn('15 of 17 heads carry no open PR', line)
        self.assertIn('3 unowned (prefixes analysis/ 1, integration/ 1, legacy/ 1)', line)

    def test_no_doctor_row_before_a_sweep(self):
        self.assertIsNone(doctor.check_branches(self.product))

    def test_the_census_lands_in_the_state_file_and_deletes_nothing_new(self):
        self.sweep(fix=True)
        census = retention.read_state(self.product)['census']
        self.assertTrue({'total', 'no_pr', 'by_rule', 'unowned'} <= set(census))
        self.assertEqual(census['by_rule']['settled'], 2)  # legacy/T-0009, legacy/T-0011
        left = self.heads()
        for b in ('legacy/T-0009', 'legacy/T-0010', 'legacy/T-0011', 'integration/backfill'):
            self.assertIn(b, left)

    def test_the_fetch_namespace_is_left_empty(self):
        self.sweep(fix=False)
        refs = subprocess.run(['git', 'for-each-ref', retention.FETCH_NS], cwd=self.wt,
                              capture_output=True, text=True).stdout
        self.assertEqual(refs.strip(), '')

    def test_a_string_lane_never_crashes_in_flight_or_the_sweep(self):
        """A launch line written before the cloud runtimes' marker moved off the ``lane`` key
        (F-lane-collision) carries ``"lane": "cloud"`` — a bare string where every other reader
        expects the harvest lane state machine's own map. ``in_flight`` (and the sweep that calls
        it) must read that as no lane state, never raise, and still hold the branch in flight
        because the run has not ended."""
        pool_mod.append_session(self.product, {'job': 'remote-1', 'item': 'T-0002', 'pid': 2,
                                               'branch': 'cloud/remote-held',
                                               'started': 't', 'lane': 'cloud'})
        flight = retention.in_flight(self.product)
        self.assertIn('cloud/remote-held', flight)
        res, _lines = self.sweep(fix=False)  # must run to completion — never raise
        self.assertEqual(sorted(res['due']), DUE)


class Census(unittest.TestCase):
    """:func:`asf.workers.retention.census` and :func:`census_line`, driven with literal dicts —
    no fixture, no git, no clock."""

    #: The same 17 names the fixture's origin carries (``main`` plus every module-level
    #: :data:`HEADS` entry); the shas stand in for shas nothing here reads.
    _NAMES = ['main'] + list(HEADS)
    HEADS = {b: f's{i}' for i, b in enumerate(_NAMES)}
    #: legacy/T-0009 and legacy/T-0011 carry Closed cards; legacy/T-0010's is Open.
    SETTLED = {'legacy/T-0009', 'legacy/T-0011'}

    def conv(self, **retention_over):
        return conventions.Conventions.from_mapping({
            'branch_prefixes': {'code': 'cloud/'},
            'branch_retention': {'legacy_prefixes': ['hb/', 'docs/', 'worker/'],
                                 **retention_over},
            'branch_patterns': {'hotfix': '*hotfix*'}})

    def settled_of(self, b):
        return f'{b} is Closed in the record' if b in self.SETTLED else None

    def census(self, **retention_over):
        conv = self.conv(**retention_over)
        return conv, retention.census(conv, self.HEADS, {'worker/pr'}, {'hb/protected'},
                                     {'cloud/held', 'docs/named'}, self.settled_of)

    def test_every_head_lands_in_exactly_one_class(self):
        _conv, data = self.census()
        self.assertEqual(data['total'], 17)
        self.assertEqual(sum(data['by_rule'].values()), 17)
        self.assertEqual(data['by_rule'], {'trunk': 1, 'release': 1, 'protected': 1, 'held': 3,
                                          'archive': 2, 'legacy': 3, 'settled': 2, 'lane': 1,
                                          'unowned': 3})

    def test_no_pr_is_every_head_but_the_trunk_that_carries_no_open_pr(self):
        _conv, data = self.census()
        self.assertEqual(data['no_pr'], 15)  # 17 heads, minus the trunk, minus worker/pr
        conv = self.conv()
        blind = retention.census(conv, self.HEADS, None, None, set(), lambda b: None)
        self.assertIsNone(blind['no_pr'])

    def test_the_unowned_map_and_the_unowned_class_are_different_counts(self):
        conv, data = self.census()
        self.assertEqual(data['unowned'], retention.unowned(conv, self.HEADS))
        self.assertEqual(data['unowned'], {'legacy/': 3, 'analysis/': 1, 'integration/': 1})
        self.assertEqual(data['by_rule']['unowned'], 3)  # legacy/T-0009, T-0011 are settled
        self.assertNotEqual(sum(data['unowned'].values()), data['by_rule']['unowned'])

    def test_an_archive_head_something_holds_is_held_not_archive(self):
        conv = self.conv()
        data = retention.census(conv, {'archive/x': 's'}, set(), set(), {'x'}, lambda b: None)
        self.assertEqual(data['by_rule'], {'held': 1})

    def test_a_settled_head_is_classed_settled_whatever_its_settled_of_reports(self):
        """``census`` takes no age at all — a caller whose ``settled_of`` ignores age (as
        :func:`asf.workers.retention.settled_reason` does) still lands the head as settled."""
        conv = self.conv(settled_days=0)
        data = retention.census(conv, {'young': 's'}, set(), set(), set(), lambda b: 'closed')
        self.assertEqual(data['by_rule'], {'settled': 1})

    def test_census_line_matches_the_designs_own_example(self):
        data = {'census': {'total': 712, 'no_pr': 711,
                           'by_rule': {'settled': 402, 'archive': 120, 'lane': 88,
                                      'unowned': 51, 'held': 25},
                           'unowned_groups': {'hotfix-': 40, 'analysis/': 11}},
               'deleted': 50, 'due': 352}
        self.assertEqual(retention.census_line(data),
                         'branches: 711 of 712 heads carry no open PR — 402 settled, 120 archive,'
                         ' 88 lane, 51 unowned (prefixes hotfix- 40, analysis/ 11), 25 held; '
                         '50 deleted, 352 expired awaiting delete')

    def test_census_line_when_the_pr_host_did_not_answer(self):
        data = {'census': {'total': 5, 'no_pr': None, 'by_rule': {'unowned': 5},
                           'unowned_groups': {'x/': 5}}, 'deleted': 0, 'due': 0}
        self.assertEqual(retention.census_line(data),
                         'branches: 5 heads, the PR host did not answer — 5 unowned '
                         '(prefixes x/ 5); 0 deleted, 0 expired awaiting delete')

    def test_doctor_line_is_ok_exactly_when_no_head_is_unowned(self):
        home = tempfile.mkdtemp(prefix='census_home_')
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.object(env, 'ASF_HOME', home):
            product = env.Product('sample', {})
            self.assertIsNone(retention.doctor_line(product))
            retention.write_state(product, {'at': 't', 'heads': 1, 'unowned': {}, 'deleted': 0,
                                            'due': 0, 'kept': 0,
                                            'census': {'total': 1, 'no_pr': 1,
                                                      'by_rule': {'trunk': 1},
                                                      'unowned_groups': {}}})
            ok, line = retention.doctor_line(product)
            self.assertTrue(ok)
            self.assertEqual(line, retention.census_line(retention.read_state(product)))
            retention.write_state(product, {'at': 't', 'heads': 1, 'unowned': {'x/': 1},
                                            'deleted': 0, 'due': 0, 'kept': 0,
                                            'census': {'total': 1, 'no_pr': 1,
                                                      'by_rule': {'unowned': 1},
                                                      'unowned_groups': {'x/': 1}}})
            ok, _line = retention.doctor_line(product)
            self.assertFalse(ok)

    def test_a_state_file_with_no_census_key_still_renders_todays_line(self):
        home = tempfile.mkdtemp(prefix='census_home_')
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.object(env, 'ASF_HOME', home):
            product = env.Product('sample', {})
            retention.write_state(product, {'at': 't', 'heads': 3, 'unowned': {'x/': 2},
                                            'deleted': 0, 'due': 0, 'kept': 0})
            ok, line = retention.doctor_line(product)
            self.assertFalse(ok)
            self.assertTrue(line.startswith('unowned branches: 2 (prefixes x/ 2)'), line)


class RetentionConventions(unittest.TestCase):
    def test_defaults(self):
        conv = conventions.Conventions.from_mapping({})
        self.assertEqual(conv.retention('archive_days'), 14)
        self.assertEqual(conv.retention('legacy_days'), 7)
        self.assertEqual(conv.retention('per_tick'), 50)
        self.assertEqual(conv.retention('legacy_prefixes'), ())
        self.assertEqual(conv.retention('settled_days'), 7)

    def test_a_partial_block_keeps_the_other_defaults(self):
        conv = conventions.Conventions.from_mapping(
            {'branch_retention': {'archive_days': 30, 'legacy_prefixes': ['hb/']}})
        self.assertEqual(conv.retention('archive_days'), 30)
        self.assertEqual(conv.retention('legacy_days'), 7)
        self.assertEqual(conv.retention('legacy_prefixes'), ('hb/',))

    def test_a_bad_value_is_named_and_reads_as_the_default(self):
        data = {'branch_retention': {'archive_days': 'soon', 'legacy_prefixes': 'hb/',
                                     'keep': 1}}
        problems = dict(conventions.validate_mapping(data))
        self.assertIn('branch_retention.archive_days', problems)
        self.assertIn('branch_retention.legacy_prefixes', problems)
        self.assertIn('branch_retention.keep', problems)
        conv = conventions.Conventions.from_mapping(data)
        self.assertEqual(conv.retention('archive_days'), 14)

    def test_a_scalar_block_is_a_shape_finding(self):
        conv = conventions.Conventions.from_mapping({'branch_retention': 'yes'})
        self.assertIn('branch_retention', dict(conv.shape_findings()))
        self.assertEqual(conv.retention('archive_days'), 14)

    def test_settled_days_zero_is_off_not_malformed(self):
        conv = conventions.Conventions.from_mapping(
            {'branch_retention': {'settled_days': 0}})
        self.assertEqual(conv.retention('settled_days'), 0)
        problems = dict(conventions.validate_mapping(
            {'branch_retention': {'settled_days': 0}}))
        self.assertNotIn('branch_retention.settled_days', problems)
        conv = conventions.Conventions.from_mapping(
            {'branch_retention': {'per_tick': 0}})
        self.assertEqual(conv.retention('per_tick'), 0)
        problems = dict(conventions.validate_mapping(
            {'branch_retention': {'per_tick': 0}}))
        self.assertNotIn('branch_retention.per_tick', problems)

    def test_a_bad_settled_days_is_named_and_reads_as_seven(self):
        for bad in (-1, True, 'soon'):
            data = {'branch_retention': {'settled_days': bad}}
            problems = dict(conventions.validate_mapping(data))
            self.assertIn('branch_retention.settled_days', problems)
            self.assertEqual(problems['branch_retention.settled_days'],
                              f'must be a whole number >= 0, not {bad!r}')
            conv = conventions.Conventions.from_mapping(data)
            self.assertEqual(conv.retention('settled_days'), 7)

    def test_a_partial_block_setting_only_settled_days_keeps_the_other_four(self):
        conv = conventions.Conventions.from_mapping(
            {'branch_retention': {'settled_days': 3}})
        self.assertEqual(conv.retention('settled_days'), 3)
        self.assertEqual(conv.retention('archive_days'), 14)
        self.assertEqual(conv.retention('legacy_days'), 7)
        self.assertEqual(conv.retention('per_tick'), 50)
        self.assertEqual(conv.retention('legacy_prefixes'), ())


if __name__ == '__main__':
    unittest.main()


class HostedDelete(unittest.TestCase):
    """A hosted origin's refs are deleted through the host API — never a push that runs the
    product's pre-push hook — and only while the tip is still the one judged."""

    def _run(self, answers):
        from asf.workers import retention
        calls = []

        def fake(args):
            calls.append(args)
            return answers.pop(0)
        with mock.patch.object(retention.H, '_gh', side_effect=fake), \
                mock.patch.object(retention.H, 'sh') as sh:
            ok, why = retention.delete('/repo', 'hb/x', 'a' * 40, slug='o/r')
            sh.assert_not_called()
        return ok, why, calls

    def test_deletes_through_the_api_when_the_tip_is_unchanged(self):
        ok, why, calls = self._run([(0, 'a' * 40 + '\n', ''), (0, '', '')])
        self.assertTrue(ok, why)
        self.assertEqual(calls[1], ['api', '-X', 'DELETE', 'repos/o/r/git/refs/heads/hb/x'])

    def test_a_moved_tip_is_kept(self):
        ok, why, calls = self._run([(0, 'b' * 40 + '\n', '')])
        self.assertFalse(ok)
        self.assertIn('tip moved', why)
        self.assertEqual(len(calls), 1)
