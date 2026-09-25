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
                      'F-0002': {'state': 'Closed', 'note': 'kept on docs/closed'}}
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
        self.assertEqual(res['unowned'], {'analysis/': 1})
        self.assertIn('retention: unowned branches: 1 (prefixes analysis/ 1)', lines)
        ok, line = doctor.check_branches(self.product)
        self.assertFalse(ok)
        self.assertTrue(line.startswith('unowned branches: 1 (prefixes analysis/ 1)'), line)

    def test_no_doctor_row_before_a_sweep(self):
        self.assertIsNone(doctor.check_branches(self.product))

    def test_the_fetch_namespace_is_left_empty(self):
        self.sweep(fix=False)
        refs = subprocess.run(['git', 'for-each-ref', retention.FETCH_NS], cwd=self.wt,
                              capture_output=True, text=True).stdout
        self.assertEqual(refs.strip(), '')


class RetentionConventions(unittest.TestCase):
    def test_defaults(self):
        conv = conventions.Conventions.from_mapping({})
        self.assertEqual(conv.retention('archive_days'), 14)
        self.assertEqual(conv.retention('legacy_days'), 7)
        self.assertEqual(conv.retention('per_tick'), 50)
        self.assertEqual(conv.retention('legacy_prefixes'), ())

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


if __name__ == '__main__':
    unittest.main()
