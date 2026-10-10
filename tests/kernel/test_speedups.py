"""ASF 0.2 speed-ups: the kernel launches on the product's cloud lane (``kernel.launch.cloud_max``
beside ``local_max``), each brief kind on its own model (``kernel.models``), a docs-only PR gets
a light review, and the kernel's appended brief sections are capped (``kernel.briefs``)."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.kernel import actions as A
from asf.kernel import briefs as KB
from asf.kernel import loop
from asf.kernel import ports as P
from asf.kernel import settings
from asf.kernel.apply import apply
from asf.kernel.decide import rebase_finding_for
from asf.workers import cloud, cloudpid, lifecycle, pool, remote, spawn

try:
    from kernel import builders as B
    from kernel import fakes as F
    from kernel.test_go_live import RECORD, _briefer
except ImportError:  # pragma: no cover - import shape only
    from tests.kernel import builders as B
    from tests.kernel import fakes as F
    from tests.kernel.test_go_live import RECORD, _briefer

State = B.State
CFG = {'cloud': {'enabled': True, 'runtime': 'claude-remote', 'mode': 'primary',
                 'environment_id': 'env_x', 'accounts': ['c1', 'c2'], 'max_creates_per_tick': 3},
       'worker_pool': {'accounts': [{'name': 'l1', 'cap': 4}, {'name': 'c1', 'cap': 2},
                                    {'name': 'c2', 'cap': 2}]}}


def product(kernel=None, **extra):
    data = {'backlog_dir': RECORD, 'main': 'main', 'repo_slug': 'acme/sample',
            'conventions': {'specs_dir': 'docs/specs', 'plans_dir': 'docs/plans',
                            'reviews_dir': 'docs/reviews'}}
    if kernel is not None:
        data['kernel'] = kernel
    data.update(extra)
    return env.Product('sample', data)


class Keys(unittest.TestCase):

    def test_defaults(self):
        k = settings.read(None)
        self.assertEqual((k['launch']['cloud_max'], k['launch']['local_max']), (0, 6))
        self.assertEqual(k['models']['coder'], 'claude-sonnet-5')
        self.assertEqual({k['models'][x] for x in ('review', 'correct', 'fix-bug', 'coder',
                                                   'light-review')}, {'claude-sonnet-5'})
        self.assertEqual({k['models'][x] for x in ('spec', 'plan')}, {'claude-opus-5'})
        self.assertEqual(k['review']['light_paths'], ['docs/**', '*.md'])
        self.assertEqual(k['briefs']['max_appended_chars'], 4000)
        self.assertEqual(settings.seats(settings.read({'launch': {'max_sessions': 9}})), (9, 0))

    def test_malformed_values_refuse_the_load(self):
        text = ('product: sample\nrepo_slug: o/r\nkernel:\n  launch:\n    cloud_max: -1\n'
                '    local_max: many\n  models:\n    coder: 3\n    other: x\n'
                '  review:\n    light_paths: docs\n  briefs:\n    max_appended_chars: lots\n')
        errors, warnings = env.product_problems(text)
        self.assertEqual(sorted(k for _l, k, _w in errors),
                         ['kernel.briefs.max_appended_chars', 'kernel.launch.cloud_max',
                          'kernel.launch.local_max', 'kernel.models.coder',
                          'kernel.review.light_paths'])
        self.assertEqual([k for _l, k, _w in warnings], ['kernel.models.other'])

    def test_a_well_formed_block_loads_clean(self):
        text = ('product: sample\nrepo_slug: o/r\nkernel:\n  launch:\n    max_sessions: 4\n'
                '    local_max: 2\n    cloud_max: 8\n  models:\n    review: heavy\n'
                '  review:\n    light_paths: [guides/**]\n  briefs:\n    max_appended_chars: 0\n')
        self.assertEqual(env.product_problems(text), ([], []))
        p = env.Product('sample', env.loads(text))
        self.assertEqual(settings.seats(p.kernel), (2, 8))
        self.assertEqual(p.kernel['models']['review'], 'heavy')

    def test_max_sessions_is_local_plus_cloud_while_the_lane_is_on(self):
        p = product({'launch': {'max_sessions': 5, 'cloud_max': 8}})
        self.assertEqual(P.config_for(p, cfg=CFG).max_sessions, 13)
        self.assertEqual(P.config_for(p, cfg={'cloud': {'enabled': False}}).max_sessions, 5)
        off = dict(CFG, cloud=dict(CFG['cloud'], mode='off'))
        self.assertEqual(P.config_for(p, cfg=off).max_sessions, 5)
        self.assertEqual(P.config_for(product({'launch': {'local_max': 3}}), cfg={}).max_sessions,
                         3)


class FakeBreaker:
    tripped_why = ''
    failed = []

    def __init__(self, product, s, clock=None):
        pass

    def tripped(self):
        return FakeBreaker.tripped_why

    def fail(self, why):
        FakeBreaker.failed.append(why)

    def ok(self):
        pass


class Launch(unittest.TestCase):
    """The launcher fills the cloud lane up to cloud_max (a launch needing the host stays local),
    then local seats up to local_max."""

    def setUp(self):
        FakeBreaker.tripped_why, FakeBreaker.failed = '', []
        self.live = {}
        self.spawned = []
        self.fail_cloud = False
        self.runtime = remote.RemoteRuntime(cloud.settings(CFG), None)
        for p in (mock.patch.object(pool, 'load_sessions', side_effect=lambda _p: self.live),
                  mock.patch.object(pool, 'update_session'),
                  mock.patch.object(spawn, 'spawn', side_effect=self.spawn),
                  mock.patch.object(cloud, 'lane_runtime', return_value=self.runtime),
                  mock.patch.object(cloud, 'Breaker', FakeBreaker)):
            p.start()
            self.addCleanup(p.stop)
        self.port = P.RealSessions(product({'launch': {'local_max': 1, 'cloud_max': 2}}),
                                   cfg=CFG, log=lambda *_: None)

    def spawn(self, product, row, acct, text, runtime=None, cfg=None, heartbeat=True):
        if runtime is not None and self.fail_cloud:
            raise spawn.SpawnError('create refused')
        self.spawned.append((row.job, acct.name, runtime, row.model, heartbeat))
        tok = remote.token(row.job) if runtime is not None else 4242
        self.live[row.job] = {'job': row.job, 'account': acct.name, 'pid': tok}
        return self.live[row.job]

    def brief(self, model='claude-sonnet-5'):
        return mock.Mock(text='the brief', model=model, add_dirs=(), card_digest='')

    def test_cloud_first_then_local_then_no_seat(self):
        self.port.launch('build', 'T-0001', 'worker/T-0001', self.brief())
        self.port.launch('spec', 'T-0002', 'worker/T-0002', self.brief())
        self.port.launch('build', 'T-0003', 'worker/T-0003', self.brief())
        lanes = [(acct, rt is not None) for _j, acct, rt, _m, _h in self.spawned]
        self.assertEqual(lanes, [('c1', True), ('c2', True), ('l1', False)])
        self.assertIs(self.spawned[0][2], self.runtime)
        self.assertEqual({hb for *_x, hb in self.spawned}, {False})
        self.assertEqual(self.spawned[0][3], 'claude-sonnet-5')
        with self.assertRaisesRegex(P.PortError, r'local 1/1, cloud 2/2'):
            self.port.launch('build', 'T-0004', 'worker/T-0004', self.brief())

    def test_a_review_stays_local_even_with_cloud_seats_free(self):
        self.port.launch('review', 'T-0001', 'worker/T-0001', self.brief())
        self.assertEqual([(a, rt) for _j, a, rt, _m, _h in self.spawned], [('l1', None)])
        with self.assertRaisesRegex(P.PortError, r'review is not in kernel.launch.cloud_kinds'):
            self.port.launch('review', 'T-0002', 'worker/T-0002', self.brief())
        self.port.launch('build', 'T-0003', 'worker/T-0003', self.brief())
        self.assertIs(self.spawned[-1][2], self.runtime)

    def test_cloud_kinds_is_a_list_of_kinds(self):
        errors, _ = settings.problems({'launch': {'cloud_kinds': 'review'}})
        self.assertEqual([k for k, _m in errors], ['kernel.launch.cloud_kinds'])
        k = settings.read({'launch': {'cloud_kinds': ['spec']}})
        self.assertEqual(k['launch']['cloud_kinds'], ['spec'])
        self.assertEqual(settings.read(None)['launch']['cloud_kinds'],
                         ['coder', 'fix-bug', 'spec', 'plan'])

    def test_a_launch_needing_the_host_takes_a_local_seat(self):
        self.port.launch('build', 'T-0001', 'worker/T-0001', self.brief(), {'pr': 3, 'host': True})
        self.assertEqual([(a, rt) for _j, a, rt, _m, _h in self.spawned], [('l1', None)])

    def test_a_failed_cloud_create_trips_the_breaker_and_falls_back_local(self):
        self.fail_cloud = True
        job = self.port.launch('build', 'T-0001', 'worker/T-0001', self.brief())
        self.assertTrue(job.endswith('-local'))
        self.assertEqual([(a, rt) for _j, a, rt, _m, _h in self.spawned], [('l1', None)])
        self.assertEqual(len(FakeBreaker.failed), 1)
        self.assertIn('create refused', FakeBreaker.failed[0])

    def test_a_tripped_breaker_or_no_cloud_max_keeps_launches_local(self):
        FakeBreaker.tripped_why = 'cloud launches erroring'
        self.port.launch('build', 'T-0001', 'worker/T-0001', self.brief())
        port = P.RealSessions(product(), cfg=CFG)
        self.assertEqual(port.lane('build'), 'local')
        self.assertEqual([(a, rt) for _j, a, rt, _m, _h in self.spawned], [('l1', None)])

    def test_creates_per_tick_bound_the_cloud(self):
        port = P.RealSessions(product({'launch': {'local_max': 5, 'cloud_max': 8}}),
                              cfg=dict(CFG, cloud=dict(CFG['cloud'], max_creates_per_tick=1)))
        port.launch('build', 'T-0001', 'worker/T-0001', self.brief())
        port.launch('build', 'T-0002', 'worker/T-0002', self.brief())
        self.assertEqual([rt is not None for _j, _a, rt, _m, _h in self.spawned], [True, False])


class CloudLiveness(unittest.TestCase):
    """A cloud session's liveness is its remote run's status: never a dead pid."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(env, 'ASF_HOME', self.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.tok = remote.token('trig_1')
        self.run = {'job': 'build-t-0001-1', 'item': 'T-0001', 'kind': 'task', 'pid': self.tok,
                    'log': os.path.join(self.tmp, 'none.jsonl'), 'branch': 'worker/T-0001'}

    def sessions(self):
        with mock.patch.object(pool, 'load_sessions', return_value={self.run['job']: self.run}), \
                mock.patch('asf.workers.pushlog.count', return_value=0):
            return P.RealSessions(product(), cfg={}).sessions()

    def test_working_is_alive_and_over_is_ended(self):
        s = self.sessions()[0]
        self.assertEqual((s.alive, s.ended, s.cloud), (True, False, True))
        cloudpid.record(self.tok, cloudpid.DEAD, 'routine run ended without the report commit')
        s = self.sessions()[0]
        self.assertEqual((s.alive, s.ended, s.result), (False, True, 'none'))

    def test_end_records_the_remote_reason_never_a_dead_pid(self):
        cloudpid.record(self.tok, cloudpid.DEAD, 'timed out')
        s = B.session('build-t-0001-1', 'T-0001', pid=self.tok, alive=False, ended=False,
                      worktree='', cloud=True)
        with mock.patch.object(pool, 'update_session') as up:
            P.RealSessions(product(), cfg={}).end(s, True)
        kw = up.call_args.kwargs
        self.assertEqual(kw['end_reason'], lifecycle.NOT_PUSHED)
        self.assertEqual(kw['cloud_why'], 'timed out')

    def test_sync_runs_only_with_a_live_cloud_run(self):
        port = P.RealSessions(product(), cfg={})
        with mock.patch.object(cloud, 'sync', return_value=[('j', 'working', '')]) as sync:
            with mock.patch.object(pool, 'load_sessions', return_value={}):
                self.assertEqual(port.sync(), [])
            with mock.patch.object(pool, 'load_sessions', return_value={'j': self.run}):
                port.sync()
        self.assertEqual(sync.call_count, 1)

    def test_the_tick_syncs_before_it_reads_and_survives_a_failed_sync(self):
        order = []
        sessions = F.FakeSessions()
        sessions.sync = lambda out: order.append('sync') or (_ for _ in ()).throw(OSError('x'))
        ports = F.ports(sessions=sessions)
        lines = []
        with mock.patch.object(loop, 'read_facts',
                               side_effect=lambda p: order.append('read') or B.facts()):
            loop.tick(product(), ports=ports, config=B.config(), state_dir=self.tmp,
                      out=lines.append)
        self.assertEqual(order, ['sync', 'read'])
        self.assertTrue(any('cloud sync failed' in ln for ln in lines))

    def test_the_create_body_sends_no_run_once_at(self):
        body = remote.trigger_body('n', 'p', 'env_x', 'claude-sonnet-5', ['Bash'], 'https://x/y')
        self.assertNotIn('run_once_at', body)


class Models(unittest.TestCase):

    def brief(self, kind, item=None, pr=None, p=None, findings=()):
        item = item or B.task('T-0001', state=State.REVIEW if pr else State.READY)
        return _briefer(p or product())(item, A.Launch(kind, item.id, 'worker/%s' % item.id),
                                        list(findings), pr)

    def test_each_kind_launches_on_its_model(self):
        self.assertEqual(self.brief('build').model, 'claude-sonnet-5')
        self.assertEqual(self.brief('build', pr=B.pr(7, 'T-0001')).model, 'claude-sonnet-5')
        self.assertEqual(self.brief('review', pr=B.pr(7, 'T-0001')).model, 'claude-sonnet-5')
        self.assertEqual(self.brief('spec', item=B.item('F-0001')).model, 'claude-opus-5')
        p = product({'models': {'coder': 'heavy'}})
        self.assertEqual(self.brief('build', p=p).model, 'heavy')

    def test_model_ids_pass_the_spawn_mapping(self):
        cfg = {'worker_pool': {'models': {'heavy': 'big-1', 'light': 'small-1'}}}
        self.assertEqual(spawn.model_arg('claude-sonnet-5', cfg), 'claude-sonnet-5')
        self.assertEqual(spawn.model_arg('heavy', cfg), 'big-1')
        self.assertEqual(spawn.model_arg('small-1', cfg), 'small-1')
        with self.assertRaises(spawn.SpawnError):
            spawn.model_arg('medium', cfg)

    def test_a_docs_only_pr_gets_a_light_review(self):
        docs = B.pr(7, 'T-0001', files=['docs/guide/setup.md', 'README.md', 'docs/specs/F-1.md'])
        b = self.brief('review', pr=docs)
        self.assertEqual((b.kind, b.model), ('light-review', 'claude-sonnet-5'))
        self.assertIn('Light review', b.text)
        self.assertIn('`docs/guide/setup.md`', b.text)
        self.assertIn(KB.VERDICT_RULE, b.text)
        full = self.brief('review', pr=B.pr(7, 'T-0001', files=['docs/a.md', 'src/a.py']))
        self.assertEqual(full.kind, 'review')
        self.assertLess(len(b.text), len(full.text) / 2)
        p = product({'review': {'light_paths': ['guides/**']}})
        self.assertEqual(self.brief('review', pr=B.pr(7, 'T-0001', files=['guides/a.txt']),
                                    p=p).kind, 'light-review')
        self.assertEqual(self.brief('review', pr=B.pr(7, 'T-0001', files=[])).kind, 'review')


class Cap(unittest.TestCase):

    def test_cap_keeps_the_newest_of_each_section(self):
        kept, dropped = KB.cap_sections([['a' * 50, 'b' * 50, 'c' * 50], ['x' * 50, 'y' * 50]],
                                        200)
        self.assertEqual(kept, [['b' * 50, 'c' * 50], ['x' * 50, 'y' * 50]])
        self.assertEqual(dropped, 1)
        self.assertEqual(KB.cap_sections([['a' * 50]], 0), ([['a' * 50]], 0))
        kept, dropped = KB.cap_sections([['z' * 900]], 500)
        self.assertEqual((len(kept[0][0]), dropped), (500, 1))

    def test_a_brief_caps_findings_and_answers_and_logs_it(self):
        lines = []
        item = B.task('T-0001', answers=['old answer ' + 'o' * 3000, 'new answer'])
        findings = ['finding %d %s' % (n, 'f' * 600) for n in range(10)]
        with open(os.path.join(RECORD, 'index.json'), encoding='utf-8') as f:
            import json
            index = json.load(f)
        b = KB.build(product(), A.Launch('build', 'T-0001', 'worker/T-0001'), item, findings,
                     index=index, repo_facts=lambda *_: None, log=lines.append)
        self.assertIn('finding 9 ', b.text)
        self.assertIn('new answer', b.text)
        self.assertNotIn('finding 0 ', b.text)
        self.assertEqual(len(lines), 1)
        self.assertIn('capped at 4000', lines[0])
        added = sum(len(x) for x in findings + list(item.answers) if x[:12] in b.text)
        self.assertLessEqual(added, 4000)


class NoSeatWaits(unittest.TestCase):
    """max_sessions pools local and cloud seats, so the plan can hold a local-only launch (a
    review, a rebase round) while only cloud seats are free: it waits for a local seat — never an
    attempt on the item (three of them would make it Stuck) — and the cloud launches after it
    still go."""

    class Sessions(F.FakeSessions):
        def launch(self, kind, item_id, branch, brief, meta=None):
            if kind == 'review':
                raise P.NoSeat('no free seat: local 10/10, cloud 0/8 (review is not in '
                               'kernel.launch.cloud_kinds)')
            return super().launch(kind, item_id, branch, brief, meta)

    def test_a_local_only_launch_on_a_full_local_lane_waits_without_an_attempt(self):
        rec = F.FakeRecord([B.task('T-0001', state=State.REVIEW), B.task('T-0002')])
        facts = B.facts([B.task('T-0001', state=State.REVIEW), B.task('T-0002')],
                        prs=[B.pr(7, 'T-0001')])
        sessions = self.Sessions()
        plan = A.Plan(actions=[A.Launch('review', 'T-0001', 'worker/T-0001', []),
                               A.Launch('build', 'T-0002', 'worker/T-0002', [])], states={})
        lines = []
        result = apply(plan, facts, F.ports(record=rec, sessions=sessions), now='t',
                       log=lines.append)
        self.assertEqual(result.failed, [])
        self.assertNotIn(P.ATTEMPTS, rec.fields.get('T-0001', {}))
        self.assertEqual([i for _, i, _, _ in sessions.launched], ['T-0002'])
        self.assertTrue(any('T-0001' in x and 'waits for a seat' in x for x in lines), lines)

    def test_the_lane_refusal_is_a_no_seat(self):
        port = P.RealSessions(product({'launch': {'local_max': 0, 'cloud_max': 2}}), cfg=CFG)
        with mock.patch.object(pool, 'load_sessions', return_value={}), \
                mock.patch.object(cloud, 'Breaker', FakeBreaker):
            with self.assertRaises(P.NoSeat):
                port.lane('review')
            self.assertEqual(port.lane('coder'), 'cloud')


class RebaseRoundStaysLocal(unittest.TestCase):

    def test_a_rebase_round_launch_carries_the_host_flag(self):
        pr = B.pr(7, 'T-0001', conflicting=True)
        it = B.task('T-0001', state=State.REVIEW)
        facts = B.facts([it], prs=[pr])
        sessions = F.FakeSessions()
        plan = A.Plan(actions=[A.Launch('build', 'T-0001', pr.branch,
                                        [rebase_finding_for(7)])], states={})
        apply(plan, facts, F.ports(sessions=sessions), now='t', log=lambda *_: None)
        self.assertTrue(sessions.meta[0].get('host'))
        sessions = F.FakeSessions()
        plan = A.Plan(actions=[A.Launch('build', 'T-0001', pr.branch, ['src/a.py:1 — x'])],
                      states={})
        apply(plan, facts, F.ports(sessions=sessions), now='t', log=lambda *_: None)
        self.assertNotIn('host', sessions.meta[0])


if __name__ == '__main__':
    unittest.main()
