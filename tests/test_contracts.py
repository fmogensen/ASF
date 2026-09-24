"""The fix package's shared contracts: the new config keys parse, default and refuse; the stub
modules import with their final signatures."""
import inspect
import os
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.conventions import Conventions
from asf.workers import pool

PRODUCT = """product: sample
repo_slug: acme/sample
main: main
conventions:
  specs_dir: docs/specs
{extra}
"""


def product_problems(extra):
    return env.validate_product_text(PRODUCT.format(extra=extra))


class ConventionKeysDefault(unittest.TestCase):
    def test_the_defaults(self):
        c = Conventions()
        self.assertEqual((c.doc_paths, c.shared_paths), ([], []))
        self.assertEqual(c.lane_review, {'docs': 'none', 'code': 'required'})
        self.assertEqual(c.lane_stale_after, '2d')
        self.assertEqual(c.lane_stale_after_s(), 2 * 86400)
        self.assertIsNone(c.worktree_setup)
        self.assertFalse(c.review_required('docs'))
        self.assertTrue(c.review_required('code'))

    def test_mutable_defaults_are_not_shared(self):
        a, b = Conventions(), Conventions()
        a.doc_paths.append('x')
        a.lane_review['docs'] = 'required'
        self.assertEqual((b.doc_paths, b.lane_review['docs']), ([], 'none'))


class ConventionKeysParse(unittest.TestCase):
    def test_the_keys_from_yaml(self):
        data = env.loads(PRODUCT.format(extra=(
            '  doc_paths: [README.md, guide/*]\n'
            '  shared_paths:\n    - uv.lock\n    - package-lock.json\n'
            '  lane:\n    review:\n      docs: required\n    stale_after: 6h\n'
            '  worktree_setup: make deps\n')))
        c = env.Product('sample', data).conventions
        self.assertEqual(c.doc_paths, ['README.md', 'guide/*'])
        self.assertEqual(c.shared_paths, ['uv.lock', 'package-lock.json'])
        # a class the yaml leaves out keeps its default
        self.assertEqual(c.lane_review, {'docs': 'required', 'code': 'required'})
        self.assertEqual(c.lane_stale_after_s(), 6 * 3600)
        self.assertEqual(c.worktree_setup, 'make deps')
        self.assertNotIn('lane', c.extra)

    def test_a_valid_file_has_no_problems(self):
        self.assertEqual(product_problems(
            '  doc_paths: [README.md]\n  shared_paths: []\n'
            '  lane:\n    review:\n      docs: none\n      code: none\n    stale_after: 30m\n'
            '  worktree_setup: make deps\n'), [])


class ConventionKeysRefused(unittest.TestCase):
    def assertRefused(self, extra, key, words):
        problems = product_problems(extra)
        keys = [k for _ln, k, _why in problems]
        self.assertIn(key, keys, problems)
        why = next(w for _ln, k, w in problems if k == key)
        self.assertIn(words, why)
        line = next(ln for ln, k, _w in problems if k == key)
        self.assertGreater(line, 0)

    def test_doc_paths_must_be_a_list(self):
        self.assertRefused('  doc_paths: README.md\n', 'conventions.doc_paths', 'list of paths')

    def test_shared_paths_must_be_a_list(self):
        self.assertRefused('  shared_paths:\n    sub: x\n', 'conventions.shared_paths', 'list of paths')

    def test_a_review_policy_outside_none_required(self):
        self.assertRefused('  lane:\n    review:\n      code: maybe\n',
                           'conventions.lane.review.code', "none or required, not 'maybe'")

    def test_an_unknown_landing_class(self):
        self.assertRefused('  lane:\n    review:\n      tests: none\n',
                           'conventions.lane.review.tests', 'not a landing class')

    def test_a_bad_duration(self):
        self.assertRefused('  lane:\n    stale_after: two days\n',
                           'conventions.lane.stale_after', 'duration')

    def test_an_unknown_lane_key(self):
        self.assertRefused('  lane:\n    stale: 2d\n', 'conventions.lane.stale', 'not a lane key')

    def test_worktree_setup_must_be_a_command(self):
        self.assertRefused('  worktree_setup: [make, deps]\n', 'conventions.worktree_setup', 'command string')

    def test_load_product_refuses_with_the_key(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, 'products'))
            with open(os.path.join(home, 'products', 'sample.yaml'), 'w') as f:
                f.write(PRODUCT.format(extra='  lane:\n    stale_after: soon\n'))
            with mock.patch.object(env, 'ASF_HOME', home):
                with self.assertRaises(env.ConfigError) as cm:
                    env.load_product('sample')
        self.assertIn('conventions.lane.stale_after', str(cm.exception))


CONFIG = """default_product: sample
worker_pool:
  env_passthrough: [HTTPS_PROXY, LANG_EXTRA]
  accounts:
    - name: acct-a
      isolate_home: false
    - name: acct-b
      home: ~/acct-b-home
      home_seed: [~/.gitconfig]
    - name: acct-c
"""


class WorkerPoolKeys(unittest.TestCase):
    def test_parse_and_defaults(self):
        cfg = env.loads(CONFIG)
        self.assertEqual(env.validate_worker_pool(cfg), [])
        self.assertEqual(env.env_passthrough(cfg), ('HTTPS_PROXY', 'LANG_EXTRA'))
        self.assertEqual(env.env_passthrough({}), ())
        a, b, c = cfg['worker_pool']['accounts']
        self.assertIsNone(env.account_home(a))
        self.assertEqual(env.account_home(b), os.path.expanduser('~/acct-b-home'))
        self.assertIsNone(env.account_home(c))
        self.assertEqual([env.isolate_home(x) for x in (a, b, c)], [False, True, True])
        self.assertEqual(env.account_home_seed(b), [os.path.expanduser('~/.gitconfig')])
        self.assertEqual(env.account_home_seed(c), [])

    def test_accounts_carry_the_keys_and_home_stays_a_path(self):
        a, b, c = pool.accounts_from_config(env.loads(CONFIG))
        self.assertEqual([x.isolate_home for x in (a, b, c)], [False, True, True])
        self.assertIsNone(a.home)
        self.assertEqual(b.home, '~/acct-b-home')   # as v0.1.2 reads it
        self.assertEqual(b.home_seed, [os.path.expanduser('~/.gitconfig')])
        self.assertEqual(c.home_seed, [])

    def test_refused(self):
        cases = [
            ('  env_passthrough: HTTPS_PROXY\n', 'worker_pool.env_passthrough', 'list of variable names'),
            ('  env_passthrough: [NOT-A-VAR]\n', 'worker_pool.env_passthrough', "'NOT-A-VAR' is not one"),
            ('  accounts:\n    - name: x\n      home: [a]\n', 'worker_pool.accounts[x].home', 'must be a path'),
            ('  accounts:\n    - name: x\n      isolate_home: sometimes\n',
             'worker_pool.accounts[x].isolate_home', 'true or false'),
            ('  accounts:\n    - name: x\n      home_seed: ~/.gitconfig\n',
             'worker_pool.accounts[x].home_seed', 'list of paths'),
            ('  accounts:\n    - name: x\n      isolate_home: false\n      home_seed: [a]\n',
             'worker_pool.accounts[x].home_seed', 'no home to seed'),
        ]
        for text, key, words in cases:
            with self.subTest(key=key, words=words):
                problems = env.validate_worker_pool(env.loads('worker_pool:\n' + text))
                self.assertTrue(any(k == key and words in why for k, why in problems), problems)

    def test_load_config_refuses_with_the_key(self):
        with tempfile.TemporaryDirectory() as home:
            with open(os.path.join(home, 'config.yaml'), 'w') as f:
                f.write('worker_pool:\n  env_passthrough: nope\n')
            with mock.patch.object(env, 'ASF_HOME', home):
                with self.assertRaises(env.ConfigError) as cm:
                    env.load_config()
        self.assertIn('worker_pool.env_passthrough', str(cm.exception))


class StubsImport(unittest.TestCase):
    def test_lane(self):
        from asf.harvest import lane
        for state in ('PUSHED', 'PR_OPEN', 'REVIEW', 'GATE', 'WAITING_CI', 'WAITING', 'QUEUED',
                      'MERGING', 'BACK', 'MERGED', 'STALE', 'REAPED'):
            self.assertIn(state, lane.LANE_STATES)
        self.assertNotIn(lane.BACK, lane.BUSY_STATES)
        sigs = {'facts': ['product'], 'next_state': ['prev', 'facts'],
                'advance': ['product', 'branch', 'facts'], 'state': ['product', 'branch'],
                'busy_items': ['product'], 'gate_set': ['product', 'entries'],
                'landing_class': ['product', 'files'], 'snapshot': ['product']}
        for name, params in sigs.items():
            self.assertEqual(list(inspect.signature(getattr(lane, name)).parameters), params, name)
        for cls in (lane.FastForwardHost, lane.GitHubHost):
            self.assertTrue(issubclass(cls, lane.Host))
            for meth in ('open', 'status', 'merge'):
                self.assertTrue(callable(getattr(cls, meth)))

    def test_review(self):
        from asf.evidence import review
        self.assertEqual(list(inspect.signature(review.newest).parameters), ['product', 'branch', 'item'])
        self.assertEqual(list(inspect.signature(review.verdict_of).parameters), ['text'])

    def test_invariants(self):
        from asf import invariants
        self.assertEqual(invariants.INVARIANTS, [])
        self.assertEqual(invariants.run({}), [])
        self.assertEqual(invariants.run({}, scope='record'), [])
        with self.assertRaises(ValueError):
            invariants.run({}, scope='nowhere')
        f = invariants.Finding('I1', 'record', 'F-0001', 'lost a key', paths=('features/F-0001.md',))
        inv = invariants.Invariant('I0', 'record', lambda ctx: [f])
        with mock.patch.object(invariants, 'INVARIANTS', [inv]):
            self.assertEqual(invariants.run({}), [f])
            self.assertEqual(invariants.run({}, scope='feeder'), [])

    def test_record_stage(self):
        from asf.record import stage
        sigs = {'stage': ['root', 'writer', 'fn', 'args', 'kwargs'],
                'validate': ['root', 'staged', 'product'],
                'refuse': ['root', 'staged', 'findings'],
                'run_writers': ['root', 'writers', 'product']}
        for name, params in sigs.items():
            self.assertEqual(list(inspect.signature(getattr(stage, name)).parameters), params, name)
        s = stage.Staged('ingest', paths=('features/F-0001.md',), before={'features/F-0001.md': 'x'})
        self.assertEqual(stage.RecordContext('/r', s).staged.writer, 'ingest')
        self.assertIn('ingest', stage.WRITERS)

    def test_occupancy(self):
        from asf.workers import lifecycle
        self.assertEqual(list(inspect.signature(lifecycle.occupancy).parameters),
                         ['path', 'lanes', 'alive', 'result'])


if __name__ == '__main__':
    unittest.main()
