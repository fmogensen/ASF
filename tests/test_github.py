"""asf.github — the public gh client: a call is ``ok`` with ``data`` or Unknown with a reason,
never an empty answer that reads like "nothing there". A rate limit raises; a dry run refuses a
write; JSON is parsed inside; the timeout is per call. And tools/check_clients.py, the ratchet that
keeps new raw gh/git sites out."""
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from asf import gh_limit, github, mutation_guard
from asf.harvest import harvest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    'check_clients', os.path.join(ROOT, 'tools', 'check_clients.py'))
check_clients = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_clients)

LIMIT = 'gh: API rate limit exceeded for user ID 1.'


def proc(rc=0, out='', err=''):
    return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, err)


class Recorder:
    """A fake ``subprocess.run``: answers from ``replies`` in order, records argv and kwargs."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        rc, out, err = self.replies.pop(0)
        return subprocess.CompletedProcess(argv, rc, out, err)


class Base(unittest.TestCase):
    def setUp(self):
        gh_limit.reset()
        self.addCleanup(gh_limit.reset)
        self.err = io.StringIO()
        ctx = redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)


class Results(Base):
    def test_a_success_is_ok_with_stdout_as_data_and_an_as_of(self):
        r = github.gh(['pr', 'view', '7', '-R', 'o/r'], run=proc(0, 'text\n'))
        self.assertTrue(r.ok)
        self.assertFalse(r.unknown)
        self.assertEqual(r.data, 'text\n')
        self.assertTrue(r.as_of)
        self.assertEqual(r.triple(), (0, 'text\n', ''))

    def test_a_failure_is_unknown_with_the_rc_and_the_first_stderr_line(self):
        r = github.gh(['pr', 'view', '7', '-R', 'o/r'], run=proc(1, '', '\nHTTP 404: Not Found\nmore'))
        self.assertFalse(r.ok)
        self.assertTrue(r.unknown)
        self.assertIsNone(r.data)
        self.assertEqual(r.reason, 'rc 1: HTTP 404: Not Found')
        self.assertEqual(r.get('dflt'), 'dflt')
        self.assertTrue(r.as_of)

    def test_json_is_parsed_inside(self):
        r = github.gh(['pr', 'list'], json=True, run=proc(0, '[{"number": 7}]'))
        self.assertEqual(r.data, [{'number': 7}])
        self.assertEqual(r.get([]), [{'number': 7}])

    def test_bad_json_is_unknown_never_a_default(self):
        r = github.gh(['pr', 'list'], json=True, run=proc(0, '[{"number": 7'))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, 'bad json')
        self.assertIsNone(r.data)

    def test_an_empty_json_answer_is_ok_and_none(self):
        r = github.gh(['api', 'x', '--jq', '.y'], json=True, run=proc(0, '  \n'))
        self.assertTrue(r.ok)
        self.assertIsNone(r.data)

    def test_a_gh_that_cannot_spawn_is_unknown(self):
        def missing(argv, **kw):
            raise FileNotFoundError('gh')
        r = github.gh(['pr', 'list'], run=missing)
        self.assertTrue(r.unknown)
        self.assertIn('gh not runnable', r.reason)


class RateLimit(Base):
    def test_a_rate_limit_raises_and_is_never_a_result(self):
        rec = Recorder((1, '', LIMIT))
        with self.assertRaises(gh_limit.RateLimited):
            github.gh(['pr', 'list'], json=True, run=rec)
        with self.assertRaises(gh_limit.RateLimited):  # latched: nothing spawns
            github.gh(['pr', 'view', '1'], run=rec)
        self.assertEqual(len(rec.calls), 1)


class Timeout(Base):
    def test_json_reads_default_to_60s_and_other_reads_to_300s(self):
        rec = Recorder((0, '[]', ''), (0, 'log', ''))
        github.gh(['pr', 'list'], json=True, run=rec)
        github.gh(['run', 'view', '1', '--log'], run=rec)
        self.assertEqual(rec.calls[0][1]['timeout'], github.JSON_TIMEOUT_S)
        self.assertEqual(rec.calls[1][1]['timeout'], github.LOG_TIMEOUT_S)
        self.assertEqual((github.JSON_TIMEOUT_S, github.LOG_TIMEOUT_S), (60, 300))

    def test_none_waits_for_ever_and_an_explicit_value_is_passed(self):
        rec = Recorder((0, '', ''), (0, '', ''))
        github.gh(['pr', 'list'], timeout=None, run=rec)
        github.gh(['pr', 'list'], timeout=5, run=rec)
        self.assertNotIn('timeout', rec.calls[0][1])
        self.assertEqual(rec.calls[1][1]['timeout'], 5)

    def test_past_the_timeout_the_answer_is_unknown(self):
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw['timeout'])
        r = github.gh(['pr', 'list'], json=True, run=slow)
        self.assertTrue(r.unknown)
        self.assertEqual(r.reason, 'timeout')

    def test_the_env_is_hermetic(self):
        rec = Recorder((0, '', ''))
        with mock.patch.dict(os.environ, {'GIT_DIR': '/elsewhere'}):
            github.gh(['pr', 'list'], run=rec)
        self.assertNotIn('GIT_DIR', rec.calls[0][1]['env'])


class DryRun(Base):
    def test_a_mutating_call_is_refused_and_a_read_runs(self):
        rec = Recorder((0, '[]', ''))
        out = io.StringIO()
        with mutation_guard.active(), redirect_stdout(out):
            w = github.gh(['pr', 'merge', '7', '-R', 'o/r'], run=rec)
            r = github.gh(['pr', 'list', '-R', 'o/r'], json=True, run=rec)
        self.assertTrue(w.unknown)
        self.assertEqual(w.reason, 'dry run')
        self.assertEqual(w.rc, 1)
        self.assertIn('would', out.getvalue())
        self.assertEqual(r.data, [])
        self.assertEqual([c[0][1:3] for c in rec.calls], [['pr', 'list']])


class Escapes(Base):
    def test_an_api_read_refused_for_its_escapes_is_retried_with_the_flag(self):
        rec = Recorder((1, '', f'use {github.GH_ESCAPES}'), (0, 'log', ''))
        r = github.gh(['api', 'repos/o/r/actions/jobs/1/logs'], run=rec)
        self.assertEqual(r.data, 'log')
        self.assertEqual(rec.calls[1][0][:3], ['gh', 'api', github.GH_ESCAPES])


class Readers(Base):
    def test_merge_commit_reads_the_oid_and_empty_when_unmerged(self):
        r = github.merge_commit('o/r', 7, run=proc(0, '{"mergeCommit": {"oid": "abc"}}'))
        self.assertEqual(r.data, 'abc')
        r = github.merge_commit('o/r', 7, run=proc(0, '{"mergeCommit": null}'))
        self.assertTrue(r.ok)
        self.assertEqual(r.data, '')
        self.assertTrue(github.merge_commit('o/r', 7, run=proc(1, '', 'x')).unknown)

    def test_checks_is_the_check_run_list_and_a_wrong_shape_is_unknown(self):
        r = github.checks('o/r', 'abc', run=proc(0, '{"check_runs": [{"name": "t"}]}'))
        self.assertEqual(r.data, [{'name': 't'}])
        self.assertEqual(github.checks('o/r', 'abc', run=proc(0, '{"x": 1}')).reason, 'bad json')

    def test_runs_builds_the_filters_and_open_prs_the_list(self):
        rec = Recorder((0, '[]', ''), (0, '[]', ''))
        github.runs('o/r', workflow='tests.yml', branch='main', run=rec)
        github.open_prs('o/r', run=rec)
        argv = rec.calls[0][0]
        self.assertEqual(argv[argv.index('--workflow') + 1], 'tests.yml')
        self.assertEqual(argv[argv.index('--branch') + 1], 'main')
        self.assertIn('open', rec.calls[1][0])

    def test_api_passes_method_and_fields(self):
        rec = Recorder((0, '{}', ''))
        github.api('repos/o/r/x', method='PATCH', fields={'a': 1}, run=rec)
        self.assertEqual(rec.calls[0][0], ['gh', 'api', 'repos/o/r/x', '-X', 'PATCH', '-f', 'a=1'])

    def test_run_log_is_a_long_read(self):
        rec = Recorder((0, 'log', ''))
        self.assertEqual(github.run_log('o/r', 9, run=rec).data, 'log')
        self.assertEqual(rec.calls[0][1]['timeout'], github.LOG_TIMEOUT_S)
        self.assertIn('--log-failed', rec.calls[0][0])


class HarvestShim(Base):
    """harvest._gh keeps its shape: (rc, stdout, stderr), no timeout, the same env."""

    def test_the_shim_returns_the_triple_with_no_timeout(self):
        rec = Recorder((3, 'o', 'e'))
        with mock.patch.object(subprocess, 'run', rec):
            self.assertEqual(harvest._gh(['pr', 'view', '1']), (3, 'o', 'e'))
        self.assertNotIn('timeout', rec.calls[0][1])
        self.assertEqual(rec.calls[0][0], ['gh', 'pr', 'view', '1'])

    def test_a_gh_that_cannot_spawn_still_raises_through_the_shim(self):
        with mock.patch.object(subprocess, 'run', side_effect=FileNotFoundError('gh')):
            with self.assertRaises(FileNotFoundError):
                harvest._gh(['pr', 'list'])


class Ratchet(unittest.TestCase):
    """tools/check_clients.py: per-file counts may fall, never rise."""

    def tree(self, files, baseline=''):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for rel, text in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(text)
        os.makedirs(os.path.join(root, 'tools'), exist_ok=True)
        with open(os.path.join(root, check_clients.BASELINE), 'w') as f:
            f.write(baseline)
        return root

    def run_check(self, root):
        lines = []
        return check_clients.check(root, out=lines.append), lines

    def test_a_new_raw_gh_site_fails_and_names_the_file(self):
        root = self.tree({'asf/a.py': "subprocess.run(['gh', 'pr', 'list'])\n"})
        rc, lines = self.run_check(root)
        self.assertEqual(rc, 1)
        self.assertIn('asf/a.py', lines[0])

    def test_sites_at_or_below_the_baseline_pass(self):
        root = self.tree({'asf/a.py': "x(['gh', 'a'])\nx([\"git\", 'b'])\n"},
                         'gh asf/a.py 1\ngit asf/a.py 2\n')
        rc, lines = self.run_check(root)
        self.assertEqual(rc, 0)
        self.assertTrue(any('lower' in ln for ln in lines))

    def test_the_clients_and_exempt_lines_are_not_counted(self):
        root = self.tree({'asf/github.py': "x(['gh', 'a'])\n",
                          'asf/gitpush.py': "x(['git', 'push'])\n",
                          'asf/b.py': "x(['git', 'a'])  # client-exempt: a fixture\n"})
        self.assertEqual(self.run_check(root)[0], 0)

    def test_the_guard_rule_waits_for_the_door_then_holds(self):
        files = {'asf/gitpush.py': 'def push(args, cwd, guard=None): pass\n',
                 'asf/a.py': 'from asf import gitpush\ngitpush.push(["x"], ".")\n'
                             'gitpush.push(["y"], ".", guard=g)\n'}
        self.assertEqual(self.run_check(self.tree(files))[0], 0)
        files['asf/gitpush.py'] = '__gitpush_door__ = True\n' + files['asf/gitpush.py']
        rc, lines = self.run_check(self.tree(files))
        self.assertEqual(rc, 1)
        self.assertEqual([ln for ln in lines if 'guard=' in ln],
                         ['check_clients: asf/a.py:2: gitpush.push( without guard='])

    def test_the_repository_is_within_its_baseline(self):
        self.assertEqual(self.run_check(ROOT)[0], 0)


if __name__ == '__main__':
    unittest.main()
