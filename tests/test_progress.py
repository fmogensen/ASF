"""asf.progress — the leaf: a sample, a store, a judge (F-0066 §2.1-§2.3, §2.5's result_record).

Hermetic: every test builds its own tmpdir, git fixture and log; every clock is injected.
"""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from asf import env
from asf import progress
from asf.workers import pool as pool_mod
from asf.workers import stall as stall_mod

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
AT_FMT = '%Y-%m-%dT%H:%M:%SZ'


def _at(minute):
    return time.strftime(AT_FMT, time.gmtime(minute * 60))


def _tool_use(name, input_=None):
    return {'type': 'tool_use', 'name': name, 'input': input_ if input_ is not None else {}}


def _assistant(blocks):
    return {'type': 'assistant', 'message': {'content': blocks}}


def _as_prev(scan_result):
    keys = ('bytes', 'calls', 'novel', 'classes', 'seen', 'ring', 'result')
    return dict(zip(keys, scan_result))


def _git(args, cwd):
    subprocess.run(['git'] + args, cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    _git(['init', '-q', '-b', 'main', '.'], path)
    _git(['config', 'user.email', 'a@example.com'], path)
    _git(['config', 'user.name', 'a'], path)
    _git(['commit', '-q', '--allow-empty', '-m', 'init'], path)


def _init_repo_no_commit(path):
    """A worktree that never changes and has nothing to probe a commit off of."""
    os.makedirs(path, exist_ok=True)
    _git(['init', '-q', '-b', 'main', '.'], path)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = os.path.join(self.tmp, 'job.jsonl')

    def _write(self, *recs):
        with open(self.log, 'a', encoding='utf-8') as f:
            for r in recs:
                f.write(json.dumps(r) + '\n')

    def test_one_call_per_tool_use_block_and_class_named(self):
        self._write(_assistant([_tool_use('Bash', {'command': 'ls'}),
                                _tool_use('Read', {'file': 'a.py'})]))
        _, calls, novel, classes, seen, ring, result = progress.scan(self.log)
        self.assertEqual(calls, 2)
        self.assertEqual(classes, 2)
        self.assertEqual(seen, ['Bash', 'Read'])
        self.assertEqual(novel, 2)
        self.assertEqual(len(ring), 2)
        self.assertFalse(result)

    def test_class_capped_at_40_and_missing_name_is_question_mark(self):
        long_name = 'X' * 60
        self._write(_assistant([_tool_use(long_name), {'type': 'tool_use', 'input': {}}]))
        _, _, _, _, seen, _, _ = progress.scan(self.log)
        self.assertEqual(seen[0], long_name[:40])
        self.assertEqual(seen[1], '?')

    def test_repeat_inside_ring_not_novel_changed_input_is_novel(self):
        self._write(_assistant([_tool_use('Bash', {'command': 'ls'})]))
        prev = _as_prev(progress.scan(self.log))
        self.assertEqual(prev['novel'], 1)

        self._write(_assistant([_tool_use('Bash', {'command': 'ls'})]))
        result = progress.scan(self.log, prev=prev)
        self.assertEqual(result[1], 2)   # calls
        self.assertEqual(result[2], 1)   # novel: unchanged, the repeat is not novel
        prev = _as_prev(result)

        self._write(_assistant([_tool_use('Bash', {'command': 'pwd'})]))
        result = progress.scan(self.log, prev=prev)
        self.assertEqual(result[1], 3)
        self.assertEqual(result[2], 2)   # a changed input is a new digest: novel

    def test_system_init_boundary_resets_everything(self):
        self._write(_assistant([_tool_use('Bash')]))
        self._write({'type': 'system', 'subtype': 'init'})
        self._write(_assistant([_tool_use('Read')]))
        _, calls, novel, classes, seen, ring, result = progress.scan(self.log)
        self.assertEqual(calls, 1)
        self.assertEqual(classes, 1)
        self.assertEqual(seen, ['Read'])
        self.assertEqual(novel, 1)

    def test_result_record_seen(self):
        self._write(_assistant([_tool_use('Bash')]))
        self._write({'type': 'result', 'result': 'done'})
        result = progress.scan(self.log)
        self.assertTrue(result[6])

    def test_stops_at_the_last_complete_line(self):
        self._write(_assistant([_tool_use('Bash')]))
        with open(self.log, 'a', encoding='utf-8') as f:
            f.write('{"type": "assistant", "mess')   # a partial tail, no trailing newline
        offset, calls, *_ = progress.scan(self.log)
        with open(self.log, 'rb') as f:
            data = f.read()
        self.assertEqual(offset, data.rfind(b'\n') + 1)
        self.assertEqual(calls, 1)

    def test_incremental_scan_equals_one_whole_scan(self):
        prev = None
        for name in ('Bash', 'Read', 'Edit'):
            self._write(_assistant([_tool_use(name, {'n': name})]))
            prev = _as_prev(progress.scan(self.log, prev=prev))
        whole = _as_prev(progress.scan(self.log))
        self.assertEqual(prev, whole)

    def test_unreadable_log_returns_prev_unchanged(self):
        prev = {'bytes': 5, 'calls': 2, 'novel': 1, 'classes': 1, 'seen': ['Bash'],
               'ring': ['abc'], 'result': False}
        result = progress.scan(os.path.join(self.tmp, 'does-not-exist.jsonl'), prev=prev)
        self.assertEqual(result, (5, 2, 1, 1, ['Bash'], ['abc'], False))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        _init_repo(self.tmp)

    def test_clean_worktree(self):
        commit, files, digest = progress.probe(self.tmp)
        self.assertTrue(commit)
        self.assertEqual(files, 0)
        self.assertEqual(digest, hashlib.sha1(b'').hexdigest()[:8])

    def test_digest_moves_with_a_changed_file_not_with_the_clock(self):
        commit1, files1, digest1 = progress.probe(self.tmp)
        with open(os.path.join(self.tmp, 'f.txt'), 'w', encoding='utf-8') as f:
            f.write('one\n')
        commit2, files2, digest2 = progress.probe(self.tmp)
        self.assertEqual(commit1, commit2)
        self.assertEqual(files2, 1)
        self.assertNotEqual(digest1, digest2)
        # a second read with nothing changed gives the same digest — it is not time-based
        _, _, digest3 = progress.probe(self.tmp)
        self.assertEqual(digest2, digest3)

    def test_missing_worktree_never_raises(self):
        self.assertEqual(progress.probe(os.path.join(self.tmp, 'nope')), ('', 0, ''))
        self.assertEqual(progress.probe(None), ('', 0, ''))
        self.assertEqual(progress.probe(''), ('', 0, ''))


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        self.product = env.Product('sample', {})
        self.worktree = os.path.join(self.tmp, 'wt')
        _init_repo(self.worktree)
        self.log = os.path.join(self.tmp, 'job.jsonl')
        self.run_ = {'job': 'task-t-0001', 'log': self.log, 'worktree': self.worktree}

    def tearDown(self):
        env.ASF_HOME = self._home

    def _write_log(self, *recs):
        with open(self.log, 'a', encoding='utf-8') as f:
            for r in recs:
                f.write(json.dumps(r) + '\n')

    def test_appends_one_line(self):
        self._write_log(_assistant([_tool_use('Bash')]))
        rec = progress.sample(self.product, self.run_, now=1000)
        stored = progress.read(self.product, 'task-t-0001')
        self.assertEqual(stored, [rec])
        self.assertEqual(rec['calls'], 1)
        self.assertEqual(rec['last_call_at'], rec['at'])

    def test_last_call_at_holds_across_a_sample_with_no_new_calls(self):
        self._write_log(_assistant([_tool_use('Bash')]))
        rec1 = progress.sample(self.product, self.run_, now=1000)
        rec2 = progress.sample(self.product, self.run_, now=1060)
        self.assertEqual(rec2['calls'], rec1['calls'])
        self.assertEqual(rec2['last_call_at'], rec1['last_call_at'])
        self.assertNotEqual(rec2['at'], rec1['at'])


class JudgeTests(unittest.TestCase):
    def _s(self, minute, commit='abc', digest='d1', classes=1, novel=1):
        at = _at(minute)
        return {'at': at, 'commit': commit, 'digest': digest, 'classes': classes,
               'novel': novel, 'files': 0, 'seen': [], 'last_call_at': at}

    def test_stuck_when_identical_twenty_minutes_apart(self):
        v = progress.judge([self._s(0), self._s(20)], 20 * 60, 20)
        self.assertEqual(v.cls, progress.STUCK)
        self.assertEqual(v.minutes, 20)

    def test_moving_when_any_one_signal_differs(self):
        for field, value in (('commit', 'zzz'), ('digest', 'zzz'), ('classes', 9), ('novel', 9)):
            s2 = self._s(20)
            s2[field] = value
            v = progress.judge([self._s(0), s2], 20 * 60, 20)
            self.assertEqual(v.cls, progress.MOVING, field)

    def test_unknown_with_one_sample(self):
        self.assertEqual(progress.judge([self._s(0)], 0, 20).cls, progress.UNKNOWN)

    def test_unknown_with_a_nineteen_minute_span(self):
        v = progress.judge([self._s(0), self._s(19)], 19 * 60, 20)
        self.assertEqual(v.cls, progress.UNKNOWN)

    def test_unknown_with_a_newest_sample_twenty_one_minutes_old(self):
        now = (1 + 21) * 60
        v = progress.judge([self._s(0), self._s(1)], now, 20)
        self.assertEqual(v.cls, progress.UNKNOWN)

    def test_failed_probe_never_turns_a_moving_run_stuck_on_its_own(self):
        s1 = self._s(0, commit='', classes=2, novel=3)
        s2 = self._s(20, commit='', classes=3, novel=4)
        v = progress.judge([s1, s2], 20 * 60, 20)
        self.assertEqual(v.cls, progress.MOVING)


class WindowTests(unittest.TestCase):
    def _s(self, minute, names):
        return {'at': _at(minute), 'seen': names}

    def test_union_over_last_ten_minutes_first_seen_order(self):
        samples = [self._s(0, ['Bash']), self._s(5, ['Read', 'Bash']), self._s(9, ['Edit'])]
        out = progress.window_classes(samples, 9 * 60, minutes=10)
        self.assertEqual(out, ['Bash', 'Read', 'Edit'])

    def test_samples_outside_the_window_dropped(self):
        samples = [self._s(0, ['Old']), self._s(15, ['New'])]
        out = progress.window_classes(samples, 15 * 60, minutes=10)
        self.assertEqual(out, ['New'])


class LimitTests(unittest.TestCase):
    def test_minutes_of_grammar(self):
        self.assertEqual(progress.minutes_of(20, 99), 20)
        self.assertEqual(progress.minutes_of('20m', 99), 20)
        self.assertEqual(progress.minutes_of('90s', 99), 1.5)
        self.assertEqual(progress.minutes_of('1h', 99), 60)
        self.assertEqual(progress.minutes_of('1d', 99), 1440)
        self.assertEqual(progress.minutes_of('nonsense', 99), 99)
        self.assertEqual(progress.minutes_of(True, 99), 99)

    def test_progress_min_default_and_zero(self):
        self.assertEqual(progress.progress_min(env.Product('sample', {})),
                         progress.DEFAULT_PROGRESS_MIN)
        zero = env.Product('sample', {'stage_limits': {'progress_min': 0}})
        self.assertEqual(progress.progress_min(zero), 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        self.product = env.Product('sample', {})

    def tearDown(self):
        env.ASF_HOME = self._home

    def test_no_file_is_empty_list(self):
        self.assertEqual(progress.read(self.product, 'nope'), [])

    def test_malformed_line_dropped(self):
        path = progress.store_path(self.product, 'job')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"at": "x"}\n')
            f.write('not json\n')
            f.write('{"at": "y"}\n')
        self.assertEqual(progress.read(self.product, 'job'), [{'at': 'x'}, {'at': 'y'}])

    def test_ring_rewritten_past_two_keep(self):
        for i in range(5):
            progress.append(self.product, 'job', {'n': i}, keep=2)
        samples = progress.read(self.product, 'job')
        self.assertEqual([s['n'] for s in samples], [3, 4])

    def test_sweep_removes_only_what_is_older_than_days(self):
        old_path = progress.store_path(self.product, 'old')
        new_path = progress.store_path(self.product, 'new')
        for p in (old_path, new_path):
            with open(p, 'w', encoding='utf-8') as f:
                f.write('{}\n')
        old_time = time.time() - 8 * 86400
        os.utime(old_path, (old_time, old_time))
        swept = progress.sweep(self.product, days=7)
        self.assertEqual(swept, ['old'])
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(new_path))


class ResultRecordTests(unittest.TestCase):
    def test_shape(self):
        rec = progress.result_record('loop', 23, 'no commit, 0 files changed', '2026-01-01T00:00:00Z')
        self.assertEqual(rec['type'], 'result')
        self.assertEqual(rec['subtype'], 'error')
        self.assertTrue(rec['is_error'])
        self.assertIn('23m', rec['result'])
        self.assertIsInstance(rec['asf']['no_progress'], dict)
        self.assertEqual(rec['asf']['no_progress'],
                         {'kind': 'loop', 'minutes': 23, 'evidence': 'no commit, 0 files changed',
                          'at': '2026-01-01T00:00:00Z'})


class DeniedLoopTests(unittest.TestCase):
    """F-0066's card, as a fixture: a live run whose every tool call is denied — the fifth rung
    (F-0066 §2.5, T-0349's own acceptance)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        self.product = env.Product('sample', {})
        self.worktree = os.path.join(self.tmp, 'wt')
        _init_repo_no_commit(self.worktree)
        self.log = os.path.join(self.tmp, 'job.jsonl')
        self.base = 1_700_000_000
        self.run_ = {'job': 'loop', 'log': self.log, 'worktree': self.worktree, 'pid': 4242,
                    'started': time.strftime(AT_FMT, time.gmtime(self.base))}
        pool_mod.append_session(self.product, self.run_)
        self.alive = lambda pid: True

    def tearDown(self):
        env.ASF_HOME = self._home

    def _deny(self, minute, arg='ls'):
        at = self.base + minute * 60
        with open(self.log, 'a', encoding='utf-8') as f:
            f.write(json.dumps(_assistant([_tool_use('Bash', {'command': arg})])) + '\n')
            f.write(json.dumps({'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'is_error': True, 'content': 'denied'}]}}) + '\n')
        os.utime(self.log, (at, at))
        return at

    def test_stuck_at_twenty_minutes_not_before(self):
        for minute in range(20):
            now = self._deny(minute)
            progress.sample(self.product, self.run_, now=now)
        now19 = self.base + 19 * 60
        found = stall_mod.stall(self.product, now=now19, alive=self.alive, out=lambda s: None,
                                sample=False)
        self.assertEqual(found, [])
        now20 = self._deny(20)
        progress.sample(self.product, self.run_, now=now20)
        lines = []
        found = stall_mod.stall(self.product, now=now20, alive=self.alive, out=lines.append,
                                sample=False)
        self.assertEqual(found, [('loop', 'STUCK', 20)])
        self.assertEqual(len(lines), 1)
        self.assertIn('no commit', lines[0])
        self.assertIn('1 tool class', lines[0])
        self.assertIn('no new call', lines[0])

    def test_a_changing_argument_is_moving_at_twenty_and_after(self):
        for minute in range(22):
            now = self._deny(minute, arg=f'cmd-{minute}')
            progress.sample(self.product, self.run_, now=now)
            if minute >= 20:
                found = stall_mod.stall(self.product, now=now, alive=self.alive,
                                        out=lambda s: None, sample=False)
                self.assertEqual(found, [])

    def test_silence_past_silent_min_is_stall_never_stuck(self):
        for minute in range(5):
            now = self._deny(minute)
            progress.sample(self.product, self.run_, now=now)
        later = self.base + 4 * 60 + 31 * 60   # the log has gone silent since minute 4
        found = stall_mod.stall(self.product, now=later, alive=self.alive, out=lambda s: None,
                                sample=False)
        self.assertEqual([(j, st) for j, st, _ in found], [('loop', 'STALL')])


class LeafImportTests(unittest.TestCase):
    """PD13: the stdlib and ``asf.env``, exactly the licence :mod:`asf.tokens` takes."""

    def test_no_asf_import_but_asf_env(self):
        path = os.path.join(REPO_ROOT, 'asf', 'progress.py')
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split('.')[0], 'asf', alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split('.')[0] == 'asf':
                    self.assertEqual(node.module, 'asf')
                    self.assertEqual({a.name for a in node.names}, {'env'})


if __name__ == '__main__':
    unittest.main()
