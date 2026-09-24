"""asf.workers.continuation — same-session correction (F-0039). One class per Task's mechanism."""
import json
import os
import unittest
from unittest import mock

from asf import env
from asf.feeder import rows as feeder_rows
from asf.workers import continuation
from asf.workers import health as health_mod
from asf.workers import lifecycle
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod
from asf.workers import spawn as spawn_mod

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_continuation` does not
    from test_workers import Home, feature_row, git
except ImportError:  # pragma: no cover - import shape only
    from tests.test_workers import Home, feature_row, git


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


class RuleTests(Home):
    """§3.2 — who is resumable, and why not."""

    NOW = 1_800_000_000.0

    def make(self, branch='spec/F-0039', silent_min=None, **kw):
        """A run on ``branch`` with a real worktree dir and a log last written ``silent_min``
        minutes before ``NOW``; ``kw`` overrides the ledger line."""
        wt = os.path.join(self.tmp, 'wt')
        os.makedirs(wt, exist_ok=True)
        log = os.path.join(self.tmp, 'run.jsonl')
        _write(log, init('rs-1'), result('done'))
        mtime = self.NOW - 60 * (silent_min or 0)
        os.utime(log, (mtime, mtime))
        run = {'job': 'spec-f-0039', 'item': 'F-0039', 'branch': branch, 'worktree': wt,
               'log': log, 'session': 'sample/spec-f-0039@t1', 'account': 'acct-a',
               'started': 't1', 'ended': 't2', 'runtime_session': 'rs-1'}
        run.update(kw)
        return {k: v for k, v in run.items() if v is not None}

    def limits(self, **stage_limits):
        self.product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                              'stage_limits': stage_limits})

    def resumable(self, run, **kw):
        return continuation.resumable(self.product, run, self.NOW, cfg=self.cfg, **kw)

    def push_commit(self, branch, session=None):
        """A commit on ``origin/<branch>`` above the trunk, trailed as ``session`` (or not) — the
        push-gate hook stamps ``$ASF_SESSION`` on every commit, so it is cleared for this one."""
        env_ = mock.patch.dict(os.environ)
        env_.start()
        self.addCleanup(env_.stop)
        os.environ.pop('ASF_SESSION', None)
        for k, v in (('user.email', 'ci@example.com'), ('user.name', 'ci')):
            git('config', k, v, cwd=self.repo)
        git('checkout', '-q', '-B', branch, 'origin/main', cwd=self.repo)
        with open(os.path.join(self.repo, 'f.txt'), 'a') as f:
            f.write(f'{session}\n')
        git('add', 'f.txt', cwd=self.repo)
        msg = 'work' + (f'\n\nASF-Session: {session}' if session else '')
        git('commit', '-q', '-m', msg, cwd=self.repo)
        git('push', '-q', 'origin', branch, cwd=self.repo)
        git('checkout', '-q', 'main', cwd=self.repo)

    def test_no_run_on_the_branch(self):
        self.assertEqual(self.resumable(None, branch='spec/F-0039'),
                         (None, 'no session on spec/F-0039'))

    def test_writer_is_the_latest_run_on_the_branch(self):
        self.assertIsNone(continuation.writer(self.product, 'spec/F-0039'))
        open(pool_mod.sessions_path(self.product), 'w').close()
        pool_mod.append_session(self.product, {'job': 'a', 'branch': 'spec/F-0039', 'started': 't1'})
        pool_mod.append_session(self.product, {'job': 'b', 'branch': 'spec/F-0039', 'started': 't2'})
        self.assertEqual(continuation.writer(self.product, 'spec/F-0039')['job'], 'b')

    def test_a_live_run_silent_past_the_heartbeat_is_dead(self):
        self.limits(heartbeat_min=6)
        run = self.make(silent_min=14, ended=None)
        self.assertEqual(continuation.dead(run, self.product, self.NOW),
                         (True, 'heartbeat stale (14m > 6m)'))
        self.assertEqual(self.resumable(run), (None, 'heartbeat stale (14m > 6m)'))

    def test_the_same_run_silent_two_minutes_is_still_running(self):
        self.limits(heartbeat_min=6)
        run = self.make(silent_min=2, ended=None)
        self.assertEqual(continuation.dead(run, self.product, self.NOW), (False, ''))
        self.assertEqual(self.resumable(run), (None, 'still running'))

    def test_an_ended_run_is_never_dead(self):
        run = self.make(silent_min=600)
        self.assertEqual(continuation.dead(run, self.product, self.NOW), (False, ''))

    def test_no_runtime_session_recorded(self):
        self.assertEqual(self.resumable(self.make(runtime_session=None)),
                         (None, 'no runtime session id recorded'))

    def test_worktree_reaped(self):
        self.assertEqual(self.resumable(self.make(worktree=os.path.join(self.tmp, 'gone'))),
                         (None, 'worktree reaped'))
        self.assertEqual(self.resumable(self.make(worktree=None)), (None, 'worktree reaped'))

    def test_another_session_moved_the_branch(self):
        self.push_commit('spec/F-0039', session='sample/other@t9')
        self.assertEqual(self.resumable(self.make(), repo=self.repo),
                         (None, 'another session moved the branch'))

    def test_a_commit_without_a_trailer_does_not_count(self):
        self.push_commit('spec/F-0039')
        sid, why = self.resumable(self.make(), repo=self.repo)
        self.assertEqual((sid, why), ('rs-1', ''))

    def test_already_landed(self):
        self.assertEqual(self.resumable(self.make(harvested='t3')), (None, 'already landed'))

    def test_an_account_no_longer_in_the_pool(self):
        self.assertEqual(self.resumable(self.make(account='acct-gone')),
                         (None, 'account acct-gone no longer in the pool'))

    def test_the_run_that_passes(self):
        self.push_commit('spec/F-0039', session='sample/spec-f-0039@t1')
        self.assertEqual(self.resumable(self.make(), repo=self.repo), ('rs-1', ''))

    def test_heartbeat_min(self):
        def hb(**limits):
            self.limits(**limits)
            return continuation.heartbeat_min(self.product)
        self.assertEqual(hb(heartbeat_min=6), 6.0)
        self.assertEqual(hb(heartbeat_min='6m'), 6.0)
        self.assertEqual(hb(heartbeat_min='90s'), 1.5)
        self.assertEqual(hb(heartbeat_min='1h'), 60.0)
        self.assertEqual(hb(heartbeat_min='soon'), float(continuation.DEFAULT_HEARTBEAT_MIN))
        self.assertEqual(hb(silent_min=30), 6.0)
        self.product = env.Product('sample', {'repo_dir': self.repo, 'main': 'main'})
        self.assertEqual(continuation.heartbeat_min(self.product), 6.0)

    def test_the_fences(self):
        self.assertEqual(continuation.ANSWERING, ('correct', 'spec', 'plan'))
        self.assertEqual(continuation.NEVER, ('adjudicate', 'review', 'groom', 'reshape', 'rebase',
                                              'close', 'fix-bug', 'task'))
        self.assertEqual(continuation.DEFAULT_HEARTBEAT_MIN, 6)

    def target(self, row, stage='spec-review r1', run=None):
        root = os.path.join(self.tmp, 'record')
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, 'index.json'), 'w') as f:
            json.dump({'items': {'F-0039': {'id': 'F-0039', 'type': 'feature', 'stage': stage}}}, f)
        runs = {'spec/F-0039': run or self.make()}
        return continuation.target(self.product, row, root, runs=runs, now=self.NOW, cfg=self.cfg)

    def row(self, brief_kind, **kw):
        row = feeder_rows.Row(tier=2, kind='STARVED → SPEC', item_id='F-0039', feature_id='F-0039',
                              action=feeder_rows.LAUNCH, brief_kind=brief_kind,
                              branch='spec/F-0039', reason='r')
        for k, v in kw.items():
            setattr(row, k, v)
        return row

    def test_target_names_the_round_just_written_and_its_review(self):
        run, sid, rnd, path = self.target(self.row('spec'))
        self.assertEqual((sid, rnd), ('rs-1', 1))
        self.assertEqual(run['job'], 'spec-f-0039')
        self.assertEqual(path, self.product.conventions.review_path('f-0039', 1))

    def test_target_refuses_a_kind_that_never_continues(self):
        for kind in continuation.NEVER:
            self.assertEqual(self.target(self.row(kind)),
                             (None, None, 0, f'kind {kind} never continues'))

    def test_target_needs_a_review_round(self):
        for stage in ('card', 'spec-draft', 'plan-review r1'):
            self.assertEqual(self.target(self.row('spec'), stage=stage),
                             (None, None, 0, 'no review round'))

    def test_target_carries_the_resumable_reason(self):
        self.assertEqual(self.target(self.row('spec'), run=self.make(runtime_session=None)),
                         (None, None, 0, 'no runtime session id recorded'))


if __name__ == '__main__':
    unittest.main()
