"""asf.workers.continuation — same-session correction (F-0039). One class per Task's mechanism."""
import json
import os
import unittest

from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_continuation` does not
    from test_workers import Home, feature_row
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home, feature_row


def _write(path, *recs):
    with open(path, 'w', encoding='utf-8') as f:
        for r in recs:
            f.write((r if isinstance(r, str) else json.dumps(r)) + '\n')


def init(sid):
    return {'type': 'system', 'subtype': 'init', 'session_id': sid}


def result(text):
    return {'type': 'result', 'subtype': 'success', 'result': text}


class RuntimeTests(Home):
    """§3.1 — the runtime's own session id, the ``resume`` flag, and the ``continue_run`` seam."""

    def log(self):
        return os.path.join(self.tmp, 'run.jsonl')

    def job(self, **kw):
        brief = os.path.join(self.tmp, 'brief.md')
        with open(brief, 'w', encoding='utf-8') as f:
            f.write('the correction')
        return runtime_mod.Job('sample', 'j', self.tmp, brief, 'opus', log_path=self.log(), **kw)

    def test_one_run(self):
        _write(self.log(), init('abc'), result('done'))
        self.assertEqual(runtime_mod.init_line(self.log())['session_id'], 'abc')
        self.assertEqual(runtime_mod.runtime_session(self.log()), 'abc')

    def test_two_runs_the_second_init_wins(self):
        _write(self.log(), init('one'), result('a'), 'not json', '', init('two'), result('b'))
        self.assertEqual(runtime_mod.runtime_session(self.log()), 'two')

    def test_no_init_line(self):
        _write(self.log(), result('done'))
        self.assertIsNone(runtime_mod.init_line(self.log()))
        self.assertEqual(runtime_mod.runtime_session(self.log()), '')

    def test_missing_file(self):
        self.assertIsNone(runtime_mod.init_line(self.log()))
        self.assertEqual(runtime_mod.runtime_session(self.log()), '')
        self.assertEqual(runtime_mod.runtime_session(None), '')

    def test_build_command_resume(self):
        plain = runtime_mod.build_command(self.job(add_dirs=['/g']))
        self.assertEqual(plain, ['claude', '-p', '--permission-mode', 'bypassPermissions',
                                 '--add-dir', '/g', '--model', 'opus',
                                 '--output-format', 'stream-json', '--verbose'])
        resumed = runtime_mod.build_command(self.job(add_dirs=['/g'], resume='abc'))
        self.assertEqual(resumed[4:6], ['--resume', 'abc'])
        self.assertEqual(resumed[:4] + resumed[6:], plain)

    def test_base_runtime_declines(self):
        self.assertIsNone(runtime_mod.Runtime().continue_run(self.job(resume='abc')))

    def test_fake_continue_run_appends_a_second_run(self):
        rt = runtime_mod.FakeRuntime([{'ok': True, 'result': 'first'},
                                      {'ok': True, 'result': 'second'}])
        rt.run(self.job(), wait=True)
        self.assertEqual(runtime_mod.runtime_session(self.log()), 'fake:j:1')
        res = rt.continue_run(self.job(resume='fake:j:1'))
        self.assertEqual(res.text, 'second')
        self.assertEqual(runtime_mod.read_result(self.log())['result'], 'second')
        self.assertEqual(runtime_mod.runtime_session(self.log()), 'fake:j:2')
        self.assertEqual(rt.calls[-1][1], 'the correction')

    def test_fake_continue_run_can_decline(self):
        rt = runtime_mod.FakeRuntime([{'decline': True}])
        self.assertIsNone(rt.continue_run(self.job(resume='x')))
        self.assertEqual(rt.calls, [])

    def test_health_records_the_runtime_session_on_the_ended_line(self):
        rt = runtime_mod.FakeRuntime([{'ok': True, 'pid': 40}])
        spawn_mod.spawn(self.product, feature_row('j'), self.acct(), 'b', runtime=rt, cfg=self.cfg)
        health_mod.health(self.product, alive=lambda pid: False, out=lambda s: None)
        run = pool_mod.load_sessions(self.product)['j']
        self.assertTrue(run.get('ended'))
        self.assertEqual(run['runtime_session'], 'fake:j:1')

    def test_the_run_fields_do_not_fold_into_the_next_launch(self):
        for f in ('runtime_session', 'resumed', 'continued'):
            self.assertIn(f, lifecycle.RUN_FIELDS)
        lines = [{'job': 'j', 'started': 't1', 'pid': 1},
                 {'job': 'j', 'ended': 't2', 'runtime_session': 'abc'},
                 {'job': 'j', 'started': 't3', 'pid': 2}]
        first, second = lifecycle.fold(lines)['j']
        self.assertEqual(first['runtime_session'], 'abc')
        self.assertNotIn('runtime_session', second)


if __name__ == '__main__':
    unittest.main()
