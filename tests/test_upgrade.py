"""``asf upgrade --product <p> --to <sha>`` — the per-product move (asf.upgrade.move).

Hermetic: a temp ASF_HOME and PIPX_HOME, a fake ``run`` for pipx/git/gh/pgrep/ps, and fake
clock/hook operations — no launchd job, plist, settings file or real venv is touched."""
import argparse
import contextlib
import fcntl
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env, installs, upgrade
from tests.test_installs import make_venv

URL = 'https://github.com/o/r.git'
SHA = '8d5e25a62' + 'a' * 31
NEW = 'f' * 40
OLD = 'b' * 40
CHECKS = ['tests (3.12)', 'tests (3.13)']


def check_runs(conclusion='success', status='completed', names=CHECKS):
    return json.dumps({'check_runs': [{'id': i + 1, 'name': n, 'status': status,
                                       'conclusion': conclusion if status == 'completed' else None}
                                      for i, n in enumerate(names)]})


class FakeRun:
    """``subprocess.run`` for the move: pipx, gh, pgrep and ps answer from fields; a ``pipx
    install --suffix`` lays a fake venv down where pipx would."""

    def __init__(self, venvs, ci=None, procs=None, pipx_rc=0, installs_sha=None):
        self.venvs = venvs
        self.ci = check_runs() if ci is None else ci
        self.procs = list(procs or [''])  # one pgrep answer per call; the last one repeats
        self.pipx_rc = pipx_rc
        self.installs_sha = installs_sha
        self.calls = []

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        ok = lambda out='': mock.Mock(stderr='', returncode=0, stdout=out)  # noqa: E731
        if cmd[:2] == ['pipx', 'list']:
            return ok(json.dumps({'venvs': {'asf-factory': {'metadata': {'main_package': {
                'package_or_url': f'git+{URL}@1234567'}}}}}))
        if cmd[:2] == ['pipx', 'install']:
            if self.pipx_rc == 0:
                suffix = next(a for a in cmd if a.startswith('--suffix='))[len('--suffix='):]
                sha = cmd[-1].rsplit('@', 1)[1]
                make_venv(os.path.join(self.venvs, f'asf-factory{suffix}'),
                          self.installs_sha or sha)
            return mock.Mock(stderr='', returncode=self.pipx_rc, stdout='')
        if cmd[:2] == ['gh', 'api']:
            if self.ci is False:
                return mock.Mock(stderr='', returncode=1, stdout='')
            return ok(self.ci if 'check-runs' in cmd[2] else json.dumps({'statuses': []}))
        if cmd[:2] == ['pgrep', '-f']:
            answer = self.procs.pop(0) if len(self.procs) > 1 else self.procs[0]
            return mock.Mock(stderr='', returncode=0 if answer else 1, stdout=answer)
        if cmd[:1] == ['ps']:
            asked = cmd[cmd.index('-p') + 1].split(',')
            return ok('\n'.join(f'{pid} 00:10 /v/bin/python -m asf.cli {what}'
                                for pid, what in self.listing() if str(pid) in asked))
        return mock.Mock(stderr='', returncode=1, stdout='')

    def listing(self):
        return [(71, 'ci queue --apply --product alpha'), (72, 'tick --product beta --steps x')]

    def pipx_installs(self):
        return [c for c in self.calls if c[:2] == ['pipx', 'install']]


class FakeOps:
    """The clock and hook side of a move, recorded."""

    def __init__(self, clocks=('tick', 'ci-queue'), paused=(), clocks_rc=0, smoke_failures=()):
        self.clocks, self.already, self.clocks_rc = list(clocks), set(paused), clocks_rc
        self.smoke_failures = list(smoke_failures)
        self.log = []

    def clock_names(self, product):
        return list(self.clocks)

    def paused(self, product):
        return set(self.already)

    def pause(self, product, clocks, by):
        self.log.append(('pause', product, tuple(clocks)))
        return [f'paused {c}' for c in clocks]

    def bootout(self, product, clocks):
        self.log.append(('bootout', product, tuple(clocks)))
        return []

    def install_host(self):
        self.log.append(('host',))
        return []

    def resume(self, product, clocks):
        self.log.append(('resume', product, tuple(clocks)))
        return []

    def install_clocks(self, product):
        rec = installs.read(product)
        self.log.append(('clocks', product, rec.sha if rec else None))
        return self.clocks_rc

    def install_hooks(self, product):
        self.log.append(('hooks', product))
        return 0, 'hooks: ok'

    def smoke(self, product):
        rec = installs.read(product)
        self.log.append(('smoke', product, rec.sha if rec else None))
        return ['tick: gh auth status --active ok'], list(self.smoke_failures)


class MoveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='upgrade_move_test_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = mock.patch.object(env, 'ASF_HOME', os.path.join(self.tmp, 'home'))
        home.start()
        self.addCleanup(home.stop)
        os.makedirs(os.path.join(env.ASF_HOME, 'products'))
        self.pipx_home = os.path.join(self.tmp, 'pipx')
        envp = mock.patch.dict(os.environ, {'PIPX_HOME': self.pipx_home, 'ASF_ACTOR': 'tester'})
        envp.start()
        self.addCleanup(envp.stop)
        self.venvs = os.path.join(self.pipx_home, 'venvs')
        # the factory's own repo, run as a product, names the landing checks the guard reads
        with open(env.product_path('factory'), 'w', encoding='utf-8') as f:
            f.write('product: factory\nrepo_slug: o/r\nmain: main\nconventions:\n'
                    '  landing_checks: ["tests (3.12)", "tests (3.13)"]\n')
        self.slept = []

    def move(self, run, ops=None, **kw):
        out = []
        kw.setdefault('wait_s', 30)
        rc = upgrade.move('alpha', run=run, out=out.append, sleep=self.slept.append,
                          ops=ops or FakeOps(), **kw)
        return rc, out

    def state(self, *parts):
        return os.path.join(env.ASF_HOME, 'state', 'alpha', *parts)


class MoveTest(MoveCase):
    def test_a_new_sha_installs_its_own_suffixed_venv(self):
        run, ops = FakeRun(self.venvs), FakeOps()
        rc, out = self.move(run, ops, to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertEqual(run.pipx_installs(), [['pipx', 'install', '--force',
                                                '--suffix=-alpha-8d5e25a', f'git+{URL}@{SHA}']])
        rec = installs.read('alpha')
        self.assertEqual(os.path.basename(rec.venv), 'asf-factory-alpha-8d5e25a')
        self.assertEqual(rec.sha, SHA)
        self.assertEqual(ops.log, [('pause', 'alpha', ('tick', 'ci-queue')),
                                   ('bootout', 'alpha', ('tick', 'ci-queue')),
                                   ('clocks', 'alpha', SHA), ('hooks', 'alpha'),
                                   ('smoke', 'alpha', SHA), ('host',),
                                   ('resume', 'alpha', ('tick', 'ci-queue'))])
        self.assertFalse(os.path.exists(upgrade.pending_path('alpha')))

    def test_the_first_move_records_the_shared_venv_as_previous(self):
        shared = make_venv(os.path.join(self.venvs, 'asf-factory'), OLD)
        rc, out = self.move(FakeRun(self.venvs), to=SHA)
        self.assertEqual(rc, 0, out)
        rec = installs.read('alpha')
        self.assertEqual(rec.previous, {'sha': OLD, 'venv': shared})

    def test_a_sha_on_disk_is_a_local_switch_with_no_pipx_call(self):
        make_venv(installs.venv_dir('alpha', NEW), NEW)
        installs.write('alpha', SHA, make_venv(installs.venv_dir('alpha', SHA), SHA))
        run = FakeRun(self.venvs)
        rc, out = self.move(run, to=NEW)
        self.assertEqual(rc, 0, out)
        self.assertEqual(run.pipx_installs(), [])
        rec = installs.read('alpha')
        self.assertEqual((rec.sha, rec.previous_sha), (NEW, SHA))

    def test_rollback_swaps_to_previous_offline(self):
        old = make_venv(installs.venv_dir('alpha', OLD), OLD)
        cur = make_venv(installs.venv_dir('alpha', SHA), SHA)
        installs.write('alpha', SHA, cur, previous={'sha': OLD, 'venv': old})
        run = FakeRun(self.venvs, ci=False)  # the network is gone: gh fails
        rc, out = self.move(run, rollback=True)
        self.assertEqual(rc, 0, out)
        rec = installs.read('alpha')
        self.assertEqual((rec.sha, rec.venv, rec.previous_sha, rec.previous_venv),
                         (OLD, old, SHA, cur))
        self.assertFalse([c for c in run.calls
                          if c[0] in ('gh', 'git') or c[:2] == ['pipx', 'install']])
        # and forward again to the sha it ran: a local switch by the record, no CI read
        rc, out = self.move(run, to=SHA[:9])
        self.assertEqual(rc, 0, out)
        self.assertEqual(installs.read('alpha').sha, SHA)
        self.assertFalse([c for c in run.calls if c[0] in ('gh', 'git')])

    def test_rollback_without_a_previous_refuses(self):
        rc, out = self.move(FakeRun(self.venvs), rollback=True)
        self.assertEqual(rc, 2)
        self.assertIn('no previous install', out[0])

    def test_rollback_to_a_venv_gone_from_disk_refuses(self):
        cur = make_venv(installs.venv_dir('alpha', SHA), SHA)
        installs.write('alpha', SHA, cur, previous={'sha': OLD, 'venv': '/gone/venv'})
        rc, out = self.move(FakeRun(self.venvs), rollback=True)
        self.assertEqual(rc, 2)
        self.assertIn('NEEDS OPERATOR', out[0])
        self.assertEqual(installs.read('alpha').sha, SHA)

    def test_already_at_the_sha_moves_nothing(self):
        installs.write('alpha', SHA, make_venv(installs.venv_dir('alpha', SHA), SHA))
        ops = FakeOps()
        rc, out = self.move(FakeRun(self.venvs), ops, to=SHA)
        self.assertEqual(rc, 0)
        self.assertEqual(ops.log, [])
        self.assertIn('nothing to move', out[-1])

    def test_a_pipx_failure_changes_nothing(self):
        ops = FakeOps()
        rc, out = self.move(FakeRun(self.venvs, pipx_rc=1), ops, to=SHA)
        self.assertEqual(rc, 1)
        self.assertIsNone(installs.read('alpha'))
        self.assertEqual(ops.log, [])

    def test_a_venv_at_another_commit_fails_the_verify(self):
        ops = FakeOps()
        rc, out = self.move(FakeRun(self.venvs, installs_sha='e' * 40), ops, to=SHA)
        self.assertEqual(rc, 1)
        self.assertIn('FAILED', out[-1])
        self.assertIsNone(installs.read('alpha'))
        self.assertEqual(ops.log, [])

    def test_a_clock_render_failure_puts_the_pin_back_and_resumes(self):
        old = make_venv(installs.venv_dir('alpha', OLD), OLD)
        installs.write('alpha', OLD, old)
        ops = FakeOps(clocks_rc=2)
        rc, out = self.move(FakeRun(self.venvs), ops, to=SHA)
        self.assertEqual(rc, 1)
        self.assertEqual(installs.read('alpha').sha, OLD)
        self.assertEqual(ops.log[-1], ('resume', 'alpha', ('tick', 'ci-queue')))
        self.assertTrue(any('NEEDS OPERATOR' in ln for ln in out))

    def test_a_clock_paused_before_the_move_stays_paused(self):
        ops = FakeOps(paused=('ci-queue',))
        rc, _out = self.move(FakeRun(self.venvs), ops, to=SHA)
        self.assertEqual(rc, 0)
        self.assertIn(('pause', 'alpha', ('tick',)), ops.log)
        self.assertIn(('resume', 'alpha', ('tick',)), ops.log)

    def test_dry_run_prints_the_steps_and_changes_nothing(self):
        run, ops = FakeRun(self.venvs), FakeOps()
        rc, out = self.move(run, ops, to=SHA, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(run.pipx_installs(), [])
        self.assertEqual(ops.log, [])
        self.assertIsNone(installs.read('alpha'))
        text = '\n'.join(out)
        self.assertIn('--suffix=-alpha-8d5e25a', text)
        self.assertIn('pause clocks tick, ci-queue', text)
        self.assertIn('asf-factory-alpha-8d5e25a', text)


class CiGuardTest(MoveCase):
    def test_ci_unknown_defers(self):
        for ci, why in ((False, 'gh could not read'), ('not json', 'gh could not read'),
                        (check_runs(status='in_progress'), 'tests (3.12) in_progress'),
                        (check_runs(names=['tests (3.12)']), 'tests (3.13) has no run'),
                        (check_runs(conclusion='skipped'), 'skipped')):
            ops = FakeOps()
            rc, out = self.move(FakeRun(self.venvs, ci=ci), ops, to=SHA)
            self.assertEqual(rc, upgrade.MOVE_DEFERRED, (ci, out))
            self.assertIn('upgrade deferred — CI unknown', out[-1])
            self.assertIn(why, out[-1])
            self.assertEqual(ops.log, [])
            self.assertIsNone(installs.read('alpha'))

    def test_no_named_landing_checks_is_unknown(self):
        os.remove(env.product_path('factory'))
        rc, out = self.move(FakeRun(self.venvs), to=SHA)
        self.assertEqual(rc, upgrade.MOVE_DEFERRED)
        self.assertIn('no product names landing_checks for o/r', out[-1])

    def test_ci_red_refuses(self):
        rc, out = self.move(FakeRun(self.venvs, ci=check_runs(conclusion='failure')), to=SHA)
        self.assertEqual(rc, 2)
        self.assertIn('CI is red', out[-1])

    def test_force_ci_moves_with_a_loud_line(self):
        rc, out = self.move(FakeRun(self.venvs, ci=False), to=SHA, force_ci=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(any('WARNING — --force-ci' in ln for ln in out))

    def test_the_guard_reads_the_exact_sha(self):
        run = FakeRun(self.venvs)
        self.move(run, to=SHA)
        gh = [c for c in run.calls if c[:2] == ['gh', 'api']]
        self.assertEqual(gh[0][2], f'repos/o/r/commits/{SHA}/check-runs?per_page=100')


class QuiesceTest(MoveCase):
    def test_the_drain_waits_for_the_products_ci_queue_process(self):
        seen = []
        ops = FakeOps()
        orig_write = installs.write

        def write(*a, **k):
            seen.append(len(self.slept))
            return orig_write(*a, **k)
        run = FakeRun(self.venvs, procs=['71\n72\n', '71\n72\n', '72\n'])
        with mock.patch.object(installs, 'write', write):
            rc, out = self.move(run, ops, to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [2])  # pinned only after the ci queue pass of alpha ended
        self.assertTrue(any('ci queue --apply --product alpha' in ln for ln in out), out)
        self.assertFalse(any('--product beta' in ln for ln in out), out)  # another product's tick

    def test_the_marker_holds_the_product_while_it_drains(self):
        held = []
        run = FakeRun(self.venvs, procs=['71\n', ''])
        out = []
        upgrade.move('alpha', to=SHA, run=run, out=out.append, ops=FakeOps(), wait_s=30,
                     sleep=lambda _s: held.append(upgrade.waiting('alpha', out=lambda _l: None,
                                                                  installed='c' * 40)))
        self.assertEqual(held, [True])
        self.assertFalse(upgrade.waiting('beta', out=lambda _l: None, installed='c' * 40))
        self.assertFalse(os.path.exists(upgrade.pending_path('alpha')))

    def test_a_moves_marker_outlives_the_ttl_and_the_running_builds_commit(self):
        """The drain may wait 15 min, longer than the shared marker's TTL, and the commit the
        reading tick runs says nothing about the product's own venv: the move's marker lives
        exactly as long as the move's process."""
        upgrade._mark('alpha', SHA, now=1.0)  # far past the TTL
        self.assertTrue(upgrade.waiting('alpha', out=lambda _l: None, installed=SHA))
        self.assertIsNotNone(upgrade.held('alpha'))
        data = upgrade.read_pending('alpha')
        data['pid'] = 999999  # the move was killed
        upgrade._write_json(upgrade.pending_path('alpha'), data)
        self.assertFalse(upgrade.waiting('alpha', out=lambda _l: None, installed=SHA))

    def test_a_held_harvest_lock_waits_then_refuses(self):
        os.makedirs(self.state(), exist_ok=True)
        lock = open(self.state('harvest.lock'), 'a')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ops = FakeOps()
        rc, out = self.move(FakeRun(self.venvs), ops, to=SHA, wait_s=12)
        self.assertEqual(rc, upgrade.MOVE_DEFERRED)
        self.assertEqual(sum(self.slept), 12)
        self.assertIn('refused — the floor of alpha is still busy', '\n'.join(out))
        self.assertIn('harvest.lock is held', '\n'.join(out))
        self.assertIsNone(installs.read('alpha'))
        self.assertEqual([e[0] for e in ops.log], ['pause', 'resume'])  # resumed, nothing moved
        self.assertNotIn('bootout', [e[0] for e in ops.log])  # nothing was killed
        self.assertFalse(os.path.exists(upgrade.pending_path('alpha')))

    def test_a_running_tick_is_waited_for_never_booted_out(self):
        """2026-10-04 canary: the pause booted the clocks out before the drain looked, and
        ``launchctl bootout`` kills a job's running process — the drain never saw the tick it had
        just killed mid-wave. The pause is a record; the bootout comes after the floor is quiet."""
        run = FakeRun(self.venvs, procs=['73\n', '73\n', ''])
        run.listing = lambda: [(73, 'tick --product alpha --steps record,wave')]
        seen = []

        class Ops(FakeOps):
            def bootout(inner, product, clocks):
                seen.append((len(self.slept), list(run.procs)))
                return super().bootout(product, clocks)
        ops = Ops()
        rc, out = self.move(run, ops, to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [(2, [''])])  # booted out only once the tick had ended
        names = [e[0] for e in ops.log]
        self.assertEqual(names[:3], ['pause', 'bootout', 'clocks'])
        self.assertTrue(any('tick --product alpha' in ln for ln in out), out)

    def test_a_drain_timeout_boots_nothing_out_and_moves_nothing(self):
        run = FakeRun(self.venvs, procs=['73\n'])
        run.listing = lambda: [(73, 'tick --product alpha --steps record,wave')]
        ops = FakeOps()
        rc, out = self.move(run, ops, to=SHA, wait_s=10)
        self.assertEqual(rc, upgrade.MOVE_DEFERRED)
        self.assertNotEqual(rc, 0)
        self.assertEqual([e[0] for e in ops.log], ['pause', 'resume'])
        self.assertIsNone(installs.read('alpha'))
        self.assertFalse(os.path.exists(upgrade.pending_path('alpha')))

    def test_a_ci_queue_pass_does_not_start_while_the_move_drains(self):
        from asf import ci_queue
        with open(env.product_path('alpha'), 'w', encoding='utf-8') as f:
            f.write('product: alpha\nrepo_slug: o/alpha\nmain: main\n')
        args = argparse.Namespace(product='alpha', apply=True)
        out = []
        upgrade._mark('alpha', SHA)
        with mock.patch.object(ci_queue, 'apply', return_value=0) as apply:
            self.assertEqual(ci_queue.cmd_queue(args, out=out.append), 0)
            self.assertFalse(apply.called)
            self.assertIn('move', '\n'.join(out))
            upgrade.clear_pending('alpha')
            ci_queue.cmd_queue(args, out=out.append)
            self.assertTrue(apply.called)

    def test_a_batch_in_flight_refuses(self):
        os.makedirs(self.state(), exist_ok=True)
        with open(self.state('merge-queue.json'), 'w', encoding='utf-8') as f:
            json.dump({'batches': [{'ref': 'batch/1', 'sha': 'c' * 40, 'members': []}]}, f)
        rc, out = self.move(FakeRun(self.venvs), to=SHA, wait_s=5)
        self.assertEqual(rc, upgrade.MOVE_DEFERRED)
        self.assertIn('merge-queue batch in flight (batch/1)', '\n'.join(out))
        self.assertIsNone(installs.read('alpha'))


class CliTest(MoveCase):
    def parse(self, argv):
        p = argparse.ArgumentParser()
        upgrade.register(p.add_subparsers())
        return p.parse_args(argv)

    def test_the_cli_routes_product_to_the_move(self):
        args = self.parse(['upgrade', '--product', 'alpha', '--to', SHA, '--wait-s', '5',
                           '--force-ci', '--dry-run'])
        self.assertEqual((args.product, args.ref, args.wait_s, args.force_ci, args.dry_run),
                         ('alpha', SHA, 5, True, True))
        with mock.patch.object(upgrade, 'move', return_value=0) as move:
            self.assertEqual(upgrade.cmd_upgrade(args), 0)
        self.assertEqual(move.call_args.kwargs['wait_s'], 5)
        self.assertEqual(move.call_args.args, ('alpha',))

    def test_product_needs_exactly_one_of_to_and_rollback(self):
        for argv in (['upgrade', '--product', 'alpha'],
                     ['upgrade', '--product', 'alpha', '--to', SHA, '--rollback']):
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(upgrade.cmd_upgrade(self.parse(argv)), 2)
            self.assertIn('exactly one of', buf.getvalue())

    def test_the_default_wait_is_fifteen_minutes(self):
        self.assertEqual(upgrade.DEFAULT_MOVE_WAIT_S, 900)
        with mock.patch.object(upgrade, 'move', return_value=0) as move:
            upgrade.cmd_upgrade(self.parse(['upgrade', '--product', 'alpha', '--rollback']))
        self.assertEqual(move.call_args.kwargs['wait_s'], 900)


class MoveSmokeTest(MoveCase):
    """The move's smoke runs after the render and before any clock resumes; a failure puts the
    pin back, renders the clocks from it again and resumes them on what they ran."""

    def test_a_failed_smoke_rolls_the_pin_back_before_the_clocks_resume(self):
        old = make_venv(installs.venv_dir('alpha', OLD), OLD)
        installs.write('alpha', OLD, old)
        make_venv(installs.venv_dir('alpha', SHA), SHA)
        ops = FakeOps(smoke_failures=["asf.alpha.tick: gh not on the plist PATH"])
        rc, out = self.move(FakeRun(self.venvs), ops, to=SHA)
        self.assertEqual(rc, 1, out)
        self.assertEqual(installs.read('alpha').sha, OLD)
        self.assertEqual(ops.log, [('pause', 'alpha', ('tick', 'ci-queue')),
                                   ('bootout', 'alpha', ('tick', 'ci-queue')),
                                   ('clocks', 'alpha', SHA), ('hooks', 'alpha'),
                                   ('smoke', 'alpha', SHA),
                                   ('clocks', 'alpha', OLD), ('hooks', 'alpha'), ('host',),
                                   ('resume', 'alpha', ('tick', 'ci-queue'))])
        text = '\n'.join(out)
        self.assertIn('smoke FAILED asf.alpha.tick: gh not on the plist PATH', text)
        self.assertIn('NEEDS OPERATOR', text)
        self.assertIn('rolled back', text)
        self.assertFalse(os.path.exists(upgrade.pending_path('alpha')))

    def test_a_failed_first_pin_removes_the_record(self):
        make_venv(installs.venv_dir('alpha', SHA), SHA)
        ops = FakeOps(smoke_failures=['x: exit 1'])
        rc, out = self.move(FakeRun(self.venvs), ops, to=SHA)
        self.assertEqual(rc, 1, out)
        self.assertIsNone(installs.read('alpha'))

    def test_the_dry_run_names_the_smoke_step(self):
        rc, out = self.move(FakeRun(self.venvs), to=SHA, dry_run=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(any('smoke:' in line for line in out), out)


class MoveOpsTest(MoveCase):
    """The real clock operations, with launchctl faked: a move pauses and resumes through the
    durable pause record, so a paused clock is never loaded by the render in between."""

    def test_pause_and_resume_go_through_the_pause_record(self):
        from asf import scheduler
        calls = []
        agents = os.path.join(self.tmp, 'LaunchAgents')
        os.makedirs(agents)
        with mock.patch.object(scheduler, '_launchctl',
                               side_effect=lambda a, timeout=30: calls.append(a) or (0, '', '')), \
                mock.patch.object(scheduler, 'launch_agents_dir', return_value=agents), \
                mock.patch.object(scheduler, 'status', return_value={'loaded': False}):
            ops = upgrade.MoveOps()
            ops.pause('alpha', ['tick'], 'tester')
            self.assertEqual(scheduler.read_pauses('alpha')['tick']['reason'], 'upgrade')
            self.assertEqual(ops.paused('alpha'), {'tick'})
            self.assertEqual(calls, [])  # the pause is a record: a running tick is never killed
            ops.bootout('alpha', ['tick'])
            self.assertEqual(calls, [['bootout', f'gui/{os.getuid()}/asf.alpha.tick']])
            ops.resume('alpha', ['tick'])
            self.assertEqual(scheduler.read_pauses('alpha'), {})


if __name__ == '__main__':
    unittest.main()
