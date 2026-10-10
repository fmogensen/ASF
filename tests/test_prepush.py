"""F-0301 S-77504/S-77505 (Task 1): the matcher, the runner, the ledger and the door
(``asf pre-push``) — one case per bullet this Task owns. The shim's own live behaviour is
``tests/test_session_push_guard.py``'s, the module that already drives it."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import cli, env, prepush
from asf.env import Product


def _git(args, cwd, e=None):
    env_ = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
               GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
               GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
    if e:
        env_.update(e)
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=env_)


def product(checks=None, writes=None):
    conv = {'branch_prefixes': {'code': 'cloud/'}}
    if checks is not None:
        conv['pre_push_checks'] = checks
    return Product('p', {'repo_slug': 'o/p', 'main': 'main', 'conventions': conv})


class RulesMatchedReaching(unittest.TestCase):
    def test_rules_reads_the_products_declared_list(self):
        p = product(checks=[{'paths': ['a/**'], 'run': 'true', 'why': 'w'}])
        self.assertEqual(prepush.rules(p.conventions),
                         [{'paths': ['a/**'], 'run': 'true', 'why': 'w'}])
        self.assertEqual(prepush.rules(None), [])

    def test_matched_fires_in_declaration_order_and_drops_a_rule_with_no_hit(self):
        rules_ = [{'paths': ['docs/**'], 'run': 'r1', 'why': 'w1'},
                 {'paths': ['asf/**'], 'run': 'r2', 'why': 'w2'},
                 {'paths': ['tests/**'], 'run': 'r3', 'why': 'w3'}]
        out = prepush.matched(rules_, ['asf/x.py', 'tests/test_x.py'])
        self.assertEqual([r['run'] for r, _ in out], ['r2', 'r3'])
        self.assertEqual(out[0][1], ['asf/x.py'])
        self.assertEqual(out[1][1], ['tests/test_x.py'])

    def test_matched_uses_customer_content_matches_dir_prefix_and_star_crosses_slash(self):
        rules_ = [{'paths': ['apps/web/'], 'run': 'r1', 'why': 'w'},
                 {'paths': ['*.md'], 'run': 'r2', 'why': 'w'}]
        out = prepush.matched(rules_, ['apps/web/sub/file.ts', 'docs/x/y.md'])
        self.assertEqual([r['run'] for r, _ in out], ['r1', 'r2'])

    def test_a_push_whose_paths_match_no_rule_matches_nothing(self):
        rules_ = [{'paths': ['docs/**'], 'run': 'r1', 'why': 'w1'}]
        self.assertEqual(prepush.matched(rules_, ['asf/x.py']), [])

    def test_reaching_uses_globs_overlap_against_writes(self):
        rules_ = [{'paths': ['asf/prepush.py'], 'run': 'r1', 'why': 'w1'},
                 {'paths': ['docs/**'], 'run': 'r2', 'why': 'w2'}]
        self.assertEqual(prepush.reaching(rules_, ['asf/prepush.py', 'tests/test_prepush.py']),
                         [rules_[0]])
        self.assertEqual(prepush.reaching(rules_, []), [])


class BoundaryTests(unittest.TestCase):
    def test_boundary_env_and_boundary_round_trip(self):
        globs = ['asf/tune.py', 'tests/test_tune.py']
        e = prepush.boundary_env(globs)
        self.assertEqual(e, {'ASF_WRITES': 'asf/tune.py tests/test_tune.py'})
        self.assertEqual(prepush.boundary(e['ASF_WRITES']), globs)

    def test_unset_and_empty_both_read_as_empty_list(self):
        self.assertEqual(prepush.boundary(None), [])
        self.assertEqual(prepush.boundary(''), [])
        self.assertEqual(prepush.boundary_env([]), {'ASF_WRITES': ''})

    def test_an_empty_boundary_refuses_nothing(self):
        self.assertEqual(prepush.outside(['a/b.py'], prepush.boundary(None)), [])

    def test_outside_delegates_to_widen_outside(self):
        from asf.feeder import widen
        paths = ['a/b.py', 'c/d.py']
        writes = ['a/**']
        self.assertEqual(prepush.outside(paths, writes), widen.outside(paths, writes))
        self.assertEqual(prepush.outside(paths, writes), ['c/d.py'])


class AddedPathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='prepush_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'r')
        _git(['init', '-q', '-b', 'main', self.repo], self.tmp)
        with open(os.path.join(self.repo, 'a.txt'), 'w', encoding='utf-8') as f:
            f.write('a')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'root'], self.repo)
        self.base = _git(['rev-parse', 'HEAD'], self.repo).stdout.strip()
        with open(os.path.join(self.repo, 'b.txt'), 'w', encoding='utf-8') as f:
            f.write('b')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'add b'], self.repo)
        self.head = _git(['rev-parse', 'HEAD'], self.repo).stdout.strip()

    def test_returns_the_paths_base_to_sha_adds(self):
        self.assertEqual(prepush.added_paths(self.repo, self.head, self.base), ['b.txt'])

    def test_none_for_a_base_git_cannot_resolve(self):
        self.assertIsNone(prepush.added_paths(self.repo, self.head, 'not-a-real-ref'))


class RunRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='prepush_run_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_green_rule_returns_ok_true(self):
        ok, tail = prepush.run_rule({'run': 'exit 0'}, self.tmp)
        self.assertEqual((ok, tail), (True, ''))

    def test_a_red_rule_returns_its_last_tail_lines(self):
        cmd = 'for i in $(seq 1 20); do echo "line $i"; done; exit 1'
        ok, tail = prepush.run_rule({'run': cmd}, self.tmp)
        self.assertFalse(ok)
        lines = tail.splitlines()
        self.assertEqual(len(lines), prepush.TAIL_LINES)
        self.assertEqual(lines[0], 'line 6')
        self.assertEqual(lines[-1], 'line 20')

    def test_a_rule_that_outruns_its_timeout_is_killed_and_refuses_with_timed_out(self):
        ok, tail = prepush.run_rule({'run': 'sleep 5'}, self.tmp, timeout=0.2)
        self.assertFalse(ok)
        self.assertIn('timed out', tail)

    def test_the_whole_process_group_is_killed_on_timeout(self):
        marker = os.path.join(self.tmp, 'child_alive')
        cmd = f'(sleep 5; touch {marker}) & wait'
        prepush.run_rule({'run': cmd}, self.tmp, timeout=0.3)
        import time
        time.sleep(1)
        self.assertFalse(os.path.exists(marker))


class RefusalTests(unittest.TestCase):
    def test_refusal_carries_the_rules_why_the_command_and_the_tail(self):
        rule = {'run': 'make test', 'why': 'every touched module, whole'}
        text = prepush.refusal(rule, 'the last line')
        self.assertIn(rule['why'], text)
        self.assertIn('make test', text)
        self.assertIn('exited 1', text)
        self.assertIn('the last line', text)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='prepush_ledger_')
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.object(env, 'ASF_HOME', self.home)
        p.start()
        self.addCleanup(p.stop)

    def test_path_is_shaped_like_pushlogs_and_basenamed(self):
        p = prepush.path('p', 'some/job')
        self.assertTrue(p.endswith(os.path.join('gates', 'job.prepush')))

    def test_env_for_clear_record_caught_round_trip(self):
        e = prepush.env_for('p', 'job1')
        self.assertIn('ASF_PREPUSH_LOG', e)
        self.assertEqual(prepush.caught('p', 'job1'), 0)
        with mock.patch.dict(os.environ, {'ASF_PREPUSH_LOG': e['ASF_PREPUSH_LOG']}):
            prepush.record('rule', {'run': 'make test'}, ['a.py', 'b.py'])
            prepush.record('footprint', None, ['c.py'])
        self.assertEqual(prepush.caught('p', 'job1'), 2)
        with open(e['ASF_PREPUSH_LOG'], encoding='utf-8') as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(lines[0]['kind'], 'rule')
        self.assertEqual(lines[0]['rule'], 'make test')
        self.assertEqual(lines[0]['paths'], ['a.py', 'b.py'])
        self.assertEqual(lines[1]['kind'], 'footprint')
        self.assertIsNone(lines[1]['rule'])
        prepush.clear('p', 'job1')
        self.assertEqual(prepush.caught('p', 'job1'), 0)

    def test_a_write_that_fails_never_raises(self):
        with mock.patch.dict(os.environ, {'ASF_PREPUSH_LOG': '/no/such/dir/x.prepush'}):
            prepush.record('rule', {'run': 'x'}, ['a'])  # must not raise
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ASF_PREPUSH_LOG', None)
            prepush.record('rule', {'run': 'x'}, ['a'])  # no key at all: must not raise

    def test_caught_counts_lines_zero_for_no_file(self):
        self.assertEqual(prepush.caught('p', 'no-such-job'), 0)


class CmdPrePushFixture(unittest.TestCase):
    """A real repo whose ``main`` adds files the rules below match, pushed as a head."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='prepush_cmd_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, 'r')
        _git(['init', '-q', '-b', 'main', self.repo], self.tmp)
        with open(os.path.join(self.repo, 'root.txt'), 'w', encoding='utf-8') as f:
            f.write('root')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'root'], self.repo)
        _git(['branch', 'origin/main', 'main'], self.repo)  # a local ref standing in for origin/main
        os.makedirs(os.path.join(self.repo, 'asf'), exist_ok=True)
        with open(os.path.join(self.repo, 'asf', 'tune.py'), 'w', encoding='utf-8') as f:
            f.write('x = 1')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'task(T-1): add tune.py'], self.repo)
        self.sha = _git(['rev-parse', 'HEAD'], self.repo).stdout.strip()
        self.stdin = f'refs/heads/cloud/T-1 {self.sha} refs/heads/cloud/T-1 ' + '0' * 40 + '\n'

    def _product(self, checks=None):
        conv = {'main': 'main'}
        if checks is not None:
            conv['pre_push_checks'] = checks
        return Product('p', {'repo_slug': 'o/p', 'main': 'main', 'conventions': conv})

    def _run(self, checks=None, writes_env=None, cwd=None):
        import io
        out = io.StringIO()
        args = mock.Mock(product='p')
        env_patch = {}
        if writes_env is not None:
            env_patch['ASF_WRITES'] = writes_env
        cwd = cwd or self.repo
        with mock.patch('os.getcwd', return_value=cwd), \
                mock.patch.object(env, 'load_product', return_value=self._product(checks)), \
                mock.patch.dict(os.environ, env_patch, clear=False):
            rc = cli.cmd_pre_push(args, stdin=io.StringIO(self.stdin), out=out)
        return rc, out.getvalue()


class NoRuleNoProduct(CmdPrePushFixture):
    def test_a_push_matching_no_rule_runs_no_command_and_exits_0(self):
        with mock.patch.object(prepush, 'run_rule') as rr:
            rc, out = self._run(checks=[{'paths': ['docs/**'], 'run': 'exit 1', 'why': 'w'}])
        rr.assert_not_called()
        self.assertEqual(rc, 0)

    def test_a_product_that_declares_no_rule_reaches_no_command(self):
        with mock.patch.object(prepush, 'run_rule') as rr:
            rc, out = self._run(checks=None)
        rr.assert_not_called()
        self.assertEqual(rc, 0)

    def test_no_product_reaches_no_command_and_is_not_checked(self):
        import io
        out = io.StringIO()
        args = mock.Mock(product=None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ASF_PRODUCT', None)
            with mock.patch.object(env, 'load_product', side_effect=env.ConfigError('no product')):
                rc = cli.cmd_pre_push(args, stdin=io.StringIO(self.stdin), out=out)
        self.assertEqual(rc, 0)
        self.assertIn('not checked', out.getvalue())


class GreenRule(CmdPrePushFixture):
    def test_a_matched_green_rule_exits_0_and_names_the_globs_command_and_seconds(self):
        rc, out = self._run(checks=[{'paths': ['asf/**'], 'run': 'exit 0', 'why': 'w'}])
        self.assertEqual(rc, 0)
        self.assertIn('asf/**', out)
        self.assertIn('exit 0', out)
        self.assertIn('passes', out)


class RedRule(CmdPrePushFixture):
    def test_a_red_rule_exits_1_with_why_command_paths_and_tail(self):
        rc, out = self._run(checks=[
            {'paths': ['asf/**'], 'run': 'echo boom && exit 1', 'why': 'the module must pass'}])
        self.assertEqual(rc, 1)
        self.assertIn('the module must pass', out)
        self.assertIn('asf/tune.py', out)
        self.assertIn('boom', out)
        self.assertIn('exited 1', out)

    def test_a_second_rule_after_a_red_one_does_not_run(self):
        with mock.patch.object(prepush, 'run_rule',
                               side_effect=[(False, 'first tail')]) as rr:
            rc, out = self._run(checks=[
                {'paths': ['asf/**'], 'run': 'r1', 'why': 'w1'},
                {'paths': ['asf/**'], 'run': 'r2', 'why': 'w2'}])
        self.assertEqual(rc, 1)
        self.assertEqual(rr.call_count, 1)

    def test_matched_paths_are_capped_at_max_shown_the_rest_counted(self):
        for i in range(prepush.MAX_SHOWN + 3):
            with open(os.path.join(self.repo, 'asf', f'f{i}.py'), 'w', encoding='utf-8') as f:
                f.write('x')
        _git(['add', '-A'], self.repo)
        _git(['commit', '-q', '-m', 'task(T-1): more'], self.repo)
        self.sha = _git(['rev-parse', 'HEAD'], self.repo).stdout.strip()
        self.stdin = f'refs/heads/cloud/T-1 {self.sha} refs/heads/cloud/T-1 ' + '0' * 40 + '\n'
        rc, out = self._run(checks=[{'paths': ['asf/**'], 'run': 'exit 1', 'why': 'w'}])
        self.assertEqual(rc, 1)
        self.assertIn('more', out)

    def test_a_rule_that_times_out_refuses_with_timed_out_and_kills_the_group(self):
        marker = os.path.join(self.tmp, 'alive')
        cmd = f'(sleep 5; touch {marker}) & wait'
        with mock.patch.object(prepush, 'TIMEOUT_S', 0.3):
            rc, out = self._run(checks=[{'paths': ['asf/**'], 'run': cmd, 'why': 'w'}])
        self.assertEqual(rc, 1)
        self.assertIn('timed out', out)
        import time
        time.sleep(1)
        self.assertFalse(os.path.exists(marker))


class FootprintRefusal(CmdPrePushFixture):
    def test_a_pushed_path_outside_the_boundary_refuses_naming_paths_and_needs_writes(self):
        rc, out = self._run(checks=None, writes_env='docs/**')
        self.assertEqual(rc, 1)
        self.assertIn('asf/tune.py', out)
        self.assertIn('needs writes: asf/tune.py', out)
        self.assertIn('status: partial', out)

    def test_an_empty_boundary_refuses_nothing(self):
        with mock.patch.object(prepush, 'run_rule') as rr:
            rc, out = self._run(checks=None, writes_env='')
        self.assertEqual(rc, 0)

    def test_a_path_inside_the_boundary_is_not_refused(self):
        rc, out = self._run(checks=None, writes_env='asf/**')
        self.assertEqual(rc, 0)

    def test_the_footprint_is_tested_before_any_rule_runs(self):
        with mock.patch.object(prepush, 'run_rule') as rr:
            rc, out = self._run(checks=[{'paths': ['asf/**'], 'run': 'exit 0', 'why': 'w'}],
                                writes_env='docs/**')
        rr.assert_not_called()
        self.assertEqual(rc, 1)
        self.assertIn('needs writes:', out)


class CannotDiff(CmdPrePushFixture):
    def test_an_unresolvable_base_prints_not_checked_and_exits_0(self):
        with mock.patch('asf.prepush.added_paths', return_value=None):
            rc, out = self._run(checks=[{'paths': ['asf/**'], 'run': 'exit 1', 'why': 'w'}])
        self.assertEqual(rc, 0)
        self.assertIn('not checked', out)


if __name__ == '__main__':
    unittest.main()
