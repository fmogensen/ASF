"""asf.upgrade's floor split on a kernel product (F-0342): one installer per owner
(:func:`asf.upgrade.on_kernel`, :meth:`asf.upgrade.MoveOps.install_floor`), the refusal when
both halves are loaded at once (S-124906, S-124907), and the rollback/refusal guarantee that the
floor is always left loaded on the pin that was running (S-124909).

Hermetic: :mod:`tests.test_upgrade`'s ``MoveCase`` (a temp ``ASF_HOME``/``PIPX_HOME``, a fake
pipx/gh/pgrep/ps) plus one faked ``launchctl`` — no real plist is ever loaded, and nothing here
touches a host's launchd."""
import argparse
import contextlib
import io
import os
import plistlib
from unittest import mock

from asf import env, installs, scheduler, upgrade
from asf.kernel import host
from tests.test_upgrade import FakeOps, FakeRun, MoveCase, NEW, OLD, SHA, make_venv


class FakeLaunchctl:
    """``launchctl`` as a dict of loaded labels: ``bootstrap`` loads, ``bootout`` unloads,
    ``print``/``kickstart`` ask, and ``list`` answers in the three-column shape
    :func:`asf.scheduler.parse_list` reads."""

    def __init__(self, loaded=()):
        self.loaded = set(loaded)
        self.calls = []

    def __call__(self, args, timeout=30):
        self.calls.append(list(args))
        verb = args[0]
        if verb == 'list':
            return 0, ''.join(f'-\t0\t{label}\n' for label in sorted(self.loaded)), ''
        target = args[-1]
        label = os.path.basename(target)[:-len('.plist')] if target.endswith('.plist') \
            else target.rpartition('/')[2]
        if verb == 'bootstrap':
            self.loaded.add(label)
        elif verb == 'bootout':
            if label not in self.loaded:
                return 3, '', 'not loaded'
            self.loaded.discard(label)
        elif verb in ('print', 'kickstart') and label not in self.loaded:
            return 113, '', 'Could not find service'
        return 0, '', ''


class KernelFloorCase(MoveCase):
    """A kernel product ``alpha``: a ``kernel:`` block, both kernel jobs on disk and loaded
    (naming the OLD venv's interpreter), a paused 0.1 ``tick`` plist (written, not loaded), two
    fake venvs (OLD, NEW) and a fake launchctl."""

    def setUp(self):
        super().setUp()
        self.agents = os.path.join(self.tmp, 'LaunchAgents')
        os.makedirs(self.agents)
        self.lc = FakeLaunchctl()
        for p in (mock.patch.object(scheduler, '_launchctl', self.lc),
                  mock.patch.object(scheduler, 'launch_agents_dir', return_value=self.agents)):
            p.start()
            self.addCleanup(p.stop)
        self.old_venv = make_venv(installs.venv_dir('alpha', OLD), OLD)
        self.new_venv = make_venv(installs.venv_dir('alpha', NEW), NEW)
        self.write_product()
        installs.write('alpha', OLD, self.old_venv)
        self.install_kernel_jobs(self.old_venv)
        # a reason other than upgrade.PAUSE_REASON: an operator's retiring pause, not a killed
        # move's — upgrade.lift_stale_pauses only ever lifts the latter (stale_pauses)
        scheduler._write_pauses('alpha', {'tick': {'reason': 'retired for the kernel floor',
                                                    'by': 'tester', 'at': 'then'}})
        self.install_clock('tick')

    def write_product(self, clocks=True):
        """The product file: a ``kernel:`` block always, a ``clocks:`` block declaring
        ``tick`` unless ``clocks=False``."""
        text = ('product: alpha\nrepo_slug: o/alpha\nmain: main\nkernel:\n'
                '  tick:\n    interval_s: 90\n  watch:\n    interval_s: 300\n')
        if clocks:
            text += 'clocks:\n  tick:\n    steps: [record]\n    every: 5m\n'
        with open(env.product_path('alpha'), 'w', encoding='utf-8') as f:
            f.write(text)

    def install_kernel_jobs(self, venv):
        """Render and install both kernel jobs from ``venv``'s interpreter — the real renderer
        and the real writer, never a renderer of this test's own."""
        product = env.load_product('alpha')
        for job in host.jobs(product, cfg={}, python=installs.interpreter(venv)):
            scheduler.install(job)

    def install_clock(self, name):
        """``asf scheduler install --product alpha --clock <name>`` in this process: paused
        clocks write their plist but are never loaded."""
        args = argparse.Namespace(scheduler_command='install', product='alpha', clock=name,
                                  json=False, label=None, reason=None, by=None)
        with contextlib.redirect_stdout(io.StringIO()):
            scheduler.cmd_scheduler(args)

    def plist_bytes(self, clock):
        with open(scheduler.plist_path(scheduler.label_for('alpha', clock)), 'rb') as f:
            return f.read()

    def target_venv(self, sha):
        """The venv a move to ``sha`` lays down (``FakeRun``'s ``pipx install --suffix``)."""
        return os.path.join(self.venvs, f'asf-factory-alpha-{sha[:7]}')


class SilentOps(upgrade.MoveOps):
    """The real clock operations (pause, bootout, resume, install_floor — through the faked
    launchctl), with the hooks/smoke/host-clock side effects silenced: this card leaves them
    unchanged, and they are not what any Story here is about."""

    def install_hooks(self, product_name):
        return 0, ''

    def verify_hooks(self, product_name):
        return []

    def smoke(self, product_name):
        return [], []

    def install_host(self):
        return []


class PinMovesTests(KernelFloorCase):
    """S-124906: a move on a kernel product re-renders the kernel's two jobs at the new pin."""

    def test_a_product_with_a_kernel_tick_job_definition_on_disk_reads_as_on_the_kernel(self):
        self.assertTrue(upgrade.on_kernel('alpha'))

    def test_a_product_with_no_kernel_tick_job_definition_on_disk_reads_as_not_on_the_kernel(self):
        os.remove(scheduler.plist_path(scheduler.label_for('alpha', 'kernel')))
        self.assertFalse(upgrade.on_kernel('alpha'))

    def test_a_kernel_block_in_the_product_file_alone_does_not_make_it_read_as_on_the_kernel(self):
        with open(env.product_path('solo'), 'w', encoding='utf-8') as f:
            f.write('product: solo\nrepo_slug: o/solo\nmain: main\nkernel:\n'
                    '  tick:\n    interval_s: 90\n')
        self.assertFalse(upgrade.on_kernel('solo'))

    def test_a_kernel_product_whose_jobs_are_booted_out_still_reads_as_on_the_kernel(self):
        scheduler.bootout_clocks('alpha', ['kernel', 'kernel-watch'])
        self.assertTrue(upgrade.on_kernel('alpha'))

    def test_the_kernel_job_names_are_the_two_the_kernel_host_module_declares(self):
        self.assertEqual(upgrade.KERNEL_CLOCKS, (host.TICK, host.WATCH))

    def test_a_move_of_a_kernel_product_writes_both_kernel_job_definitions(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        for clock in upgrade.KERNEL_CLOCKS:
            with open(scheduler.plist_path(scheduler.label_for('alpha', clock)), 'rb') as f:
                data = plistlib.load(f)
            self.assertEqual(data['ProgramArguments'][0], installs.interpreter(self.target_venv(SHA)))

    def test_both_written_kernel_jobs_name_the_pinned_venv_interpreter(self):
        # the process moving it (this test runner) is not asf-factory-alpha-<sha>'s interpreter
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        for clock in upgrade.KERNEL_CLOCKS:
            with open(scheduler.plist_path(scheduler.label_for('alpha', clock)), 'rb') as f:
                data = plistlib.load(f)
            self.assertEqual(data['ProgramArguments'][0], installs.interpreter(self.target_venv(SHA)))
            self.assertNotEqual(data['ProgramArguments'][0], __file__)

    def test_both_written_kernel_jobs_carry_the_product_files_intervals(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        with open(scheduler.plist_path(scheduler.label_for('alpha', 'kernel')), 'rb') as f:
            self.assertEqual(plistlib.load(f)['StartInterval'], 90)
        with open(scheduler.plist_path(scheduler.label_for('alpha', 'kernel-watch')), 'rb') as f:
            self.assertEqual(plistlib.load(f)['StartInterval'], 300)

    def test_both_written_kernel_jobs_are_loaded_at_the_end_of_the_move(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertIn(scheduler.label_for('alpha', 'kernel'), self.lc.loaded)
        self.assertIn(scheduler.label_for('alpha', 'kernel-watch'), self.lc.loaded)

    def test_the_kernel_jobs_are_rendered_by_the_kernel_host_renderer(self):
        with mock.patch.object(host, 'jobs', wraps=host.jobs) as jobs:
            rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertTrue(jobs.called)
        self.assertEqual(jobs.call_args.kwargs['python'], installs.interpreter(self.target_venv(SHA)))

    def test_the_pin_the_kernel_jobs_render_from_is_the_one_on_record_at_that_moment(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertEqual(installs.read('alpha').sha, SHA)
        with open(scheduler.plist_path(scheduler.label_for('alpha', 'kernel')), 'rb') as f:
            self.assertEqual(plistlib.load(f)['ProgramArguments'][0],
                             installs.interpreter(self.target_venv(SHA)))

    def test_a_move_that_only_quiesced_the_kernel_tick_writes_only_the_kernel_tick(self):
        scheduler.pause('alpha', ['kernel-watch'], reason='operator pause', by='tester')
        watch_before = self.plist_bytes('kernel-watch')
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.plist_bytes('kernel-watch'), watch_before)
        with open(scheduler.plist_path(scheduler.label_for('alpha', 'kernel')), 'rb') as f:
            self.assertEqual(plistlib.load(f)['ProgramArguments'][0],
                             installs.interpreter(self.target_venv(SHA)))

    def test_a_move_of_a_product_not_on_the_kernel_installs_its_clocks_as_it_does_today(self):
        with open(env.product_path('beta'), 'w', encoding='utf-8') as f:
            f.write('product: beta\nrepo_slug: o/beta\nmain: main\n'
                    'clocks:\n  tick:\n    steps: [record]\n    every: 5m\n')
        ops = FakeOps(clocks=('tick', 'ci-queue'))
        outl = []
        rc = upgrade.move('beta', run=FakeRun(self.venvs), out=outl.append,
                          sleep=self.slept.append, ops=ops, wait_s=30, to=SHA)
        self.assertEqual(rc, 0, outl)
        self.assertIn(('clocks', 'beta', SHA), ops.log)

    def test_a_move_prints_one_line_naming_the_kernel_floor_it_found(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertTrue(any('runs the kernel floor (kernel, kernel-watch)' in line
                            for line in out), out)

    def test_the_dry_run_step_list_names_the_kernel_renderer(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA, dry_run=True)
        self.assertEqual(rc, 0, out)
        text = '\n'.join(out)
        self.assertIn(f'render kernel, kernel-watch from {installs.interpreter(self.target_venv(SHA))}',
                      text)

    def test_the_dry_run_of_a_kernel_product_writes_no_plist_and_loads_nothing(self):
        kernel_before = self.plist_bytes('kernel')
        watch_before = self.plist_bytes('kernel-watch')
        loaded_before = set(self.lc.loaded)
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA, dry_run=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.plist_bytes('kernel'), kernel_before)
        self.assertEqual(self.plist_bytes('kernel-watch'), watch_before)
        self.assertEqual(self.lc.loaded, loaded_before)
        self.assertEqual(installs.read('alpha').sha, OLD)


class RetiredFloorTests(KernelFloorCase):
    """S-124907: a floor the product retired is not written, not loaded and not retired by a
    move."""

    def test_a_move_of_a_kernel_product_writes_no_plist_for_a_paused_0_1_clock(self):
        before = self.plist_bytes('tick')
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.plist_bytes('tick'), before)

    def test_a_paused_0_1_clock_plist_is_byte_for_byte_unchanged(self):
        before = self.plist_bytes('tick')
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.plist_bytes('tick'), before)

    def test_a_paused_0_1_clock_is_not_loaded_by_the_move(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(scheduler.label_for('alpha', 'tick'), self.lc.loaded)

    def test_a_paused_0_1_clock_keeps_its_pause_record_through_the_move(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertIn('tick', scheduler.read_pauses('alpha'))

    def test_a_move_of_a_kernel_product_with_no_clocks_block_succeeds(self):
        self.write_product(clocks=False)
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)

    def test_a_move_of_a_kernel_product_never_asks_for_a_clocks_block(self):
        self.write_product(clocks=False)
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertFalse(any('declares no clocks' in line for line in out), out)

    def test_a_move_prints_one_line_naming_the_retired_0_1_clocks_it_leaves_alone(self):
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertTrue(any('0.1 clocks (tick) are paused' in line for line in out), out)

    def test_a_move_with_no_retired_0_1_clocks_prints_no_such_line(self):
        self.write_product(clocks=False)
        scheduler._write_pauses('alpha', {})
        os.remove(scheduler.plist_path(scheduler.label_for('alpha', 'tick')))
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertFalse(any('are paused' in line for line in out), out)

    def test_each_0_1_clock_the_move_quiesced_is_installed_by_its_own_per_clock_call(self):
        seen = []
        real = scheduler.cmd_scheduler

        def spy(args, root=None):
            seen.append(args)
            return real(args, root=root)
        with mock.patch.object(scheduler, 'cmd_scheduler', side_effect=spy), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = upgrade.MoveOps().install_floor('alpha', ['kernel', 'kernel-watch', 'daily'])
        self.assertEqual([a.clock for a in seen], ['daily'])

    def test_the_move_makes_no_whole_file_scheduler_install_call(self):
        seen = []
        real = scheduler.cmd_scheduler

        def spy(args, root=None):
            seen.append(args)
            return real(args, root=root)
        with mock.patch.object(scheduler, 'cmd_scheduler', side_effect=spy), \
                contextlib.redirect_stdout(io.StringIO()):
            upgrade.MoveOps().install_floor('alpha', ['kernel', 'kernel-watch', 'daily'])
        self.assertFalse(any(a.clock is None for a in seen), seen)

    def test_a_0_1_clock_the_move_did_not_quiesce_is_not_written_by_the_per_clock_calls(self):
        seen = []
        real = scheduler.cmd_scheduler

        def spy(args, root=None):
            seen.append(args)
            return real(args, root=root)
        with mock.patch.object(scheduler, 'cmd_scheduler', side_effect=spy):
            upgrade.MoveOps().install_floor('alpha', ['kernel', 'kernel-watch'])
        self.assertEqual(seen, [])

    def test_retire_candidates_never_names_the_kernel_tick_job(self):
        self.assertNotIn(scheduler.label_for('alpha', 'kernel'),
                         scheduler.retire_candidates('alpha', [scheduler.label_for('alpha', 'tick')]))

    def test_retire_candidates_never_names_the_kernel_watch_job(self):
        self.assertNotIn(scheduler.label_for('alpha', 'kernel-watch'),
                         scheduler.retire_candidates('alpha', [scheduler.label_for('alpha', 'tick')]))

    def test_retire_candidates_still_names_a_loaded_undeclared_0_1_label(self):
        self.install_clock('tick')  # already on disk; make sure it is also loaded for this check
        extra = scheduler.label_for('alpha', 'ci-queue')
        self.lc.loaded.add(extra)
        self.assertIn(extra, scheduler.retire_candidates('alpha', [scheduler.label_for(
            'alpha', 'tick')]))

    def test_a_kernel_product_with_a_loaded_0_1_clock_refuses_with_an_operator_line(self):
        scheduler.resume('alpha', ['tick'])  # the 0.1 clock is loaded, not paused
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 2, out)
        self.assertTrue(any('NEEDS OPERATOR' in line and 'scheduler pause' in line
                            for line in out), out)

    def test_that_refusal_pauses_nothing_writes_no_pin_installs_no_venv_and_exits_two(self):
        scheduler.resume('alpha', ['tick'])
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 2, out)
        self.assertEqual(installs.read('alpha').sha, OLD)
        self.assertEqual(scheduler.read_pauses('alpha'), {})
        self.assertFalse(os.path.isdir(self.target_venv(SHA)))

    def test_that_refusal_is_printed_by_a_dry_run_too(self):
        scheduler.resume('alpha', ['tick'])
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA, dry_run=True)
        self.assertEqual(rc, 2, out)
        self.assertTrue(any('NEEDS OPERATOR' in line and 'scheduler pause' in line
                            for line in out), out)


class RollbackTests(KernelFloorCase):
    """S-124909: every refusal and every rollback leaves the floor loaded on the pin that was
    running."""

    def test_a_failed_floor_install_puts_the_pin_back_with_an_operator_line(self):
        ops = SilentOps()
        with mock.patch.object(host, 'jobs', side_effect=scheduler.SchedulerError('boom')):
            rc, out = self.move(FakeRun(self.venvs), ops, to=SHA)
        self.assertEqual(rc, 1, out)
        self.assertEqual(installs.read('alpha').sha, OLD)
        self.assertTrue(any('NEEDS OPERATOR' in line for line in out), out)

    def test_a_failed_floor_install_re_renders_the_kernel_jobs_from_the_restored_pin(self):
        # the render raises before a single kernel plist is touched (D1: the kernel half
        # renders last), so what is on disk once the pin is put back is still OLD's — the pin
        # it put back, never the failed target's
        ops = SilentOps()
        with mock.patch.object(host, 'jobs', side_effect=scheduler.SchedulerError('boom')):
            rc, out = self.move(FakeRun(self.venvs), ops, to=SHA)
        self.assertEqual(rc, 1, out)
        self.assertEqual(installs.read('alpha').sha, OLD)
        with open(scheduler.plist_path(scheduler.label_for('alpha', 'kernel')), 'rb') as f:
            self.assertEqual(plistlib.load(f)['ProgramArguments'][0],
                             installs.interpreter(self.old_venv))

    def test_a_failed_smoke_puts_the_pin_back_and_re_renders_from_it(self):
        class FailSmoke(SilentOps):
            def smoke(self, product_name):
                return [], ['tick: gh missing']
        rc, out = self.move(FakeRun(self.venvs), FailSmoke(), to=SHA)
        self.assertEqual(rc, 1, out)
        self.assertEqual(installs.read('alpha').sha, OLD)
        with open(scheduler.plist_path(scheduler.label_for('alpha', 'kernel')), 'rb') as f:
            self.assertEqual(plistlib.load(f)['ProgramArguments'][0],
                             installs.interpreter(self.old_venv))

    def test_a_failed_smoke_leaves_both_kernel_jobs_loaded(self):
        class FailSmoke(SilentOps):
            def smoke(self, product_name):
                return [], ['tick: gh missing']
        rc, out = self.move(FakeRun(self.venvs), FailSmoke(), to=SHA)
        self.assertEqual(rc, 1, out)
        self.assertIn(scheduler.label_for('alpha', 'kernel'), self.lc.loaded)
        self.assertIn(scheduler.label_for('alpha', 'kernel-watch'), self.lc.loaded)

    def test_a_first_pin_whose_floor_install_fails_removes_the_record(self):
        os.remove(installs.record_path('alpha'))
        self.install_kernel_jobs(self.old_venv)
        with mock.patch.object(host, 'jobs', side_effect=scheduler.SchedulerError('boom')):
            rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 1, out)
        self.assertIsNone(installs.read('alpha'))
        self.assertIn(scheduler.label_for('alpha', 'kernel'), self.lc.loaded)

    def test_a_rollback_re_renders_the_kernel_jobs_from_the_previous_pin(self):
        installs.write('alpha', NEW, self.new_venv, previous={'sha': OLD, 'venv': self.old_venv})
        rc, out = self.move(FakeRun(self.venvs, ci=False), SilentOps(), rollback=True)
        self.assertEqual(rc, 0, out)
        with open(scheduler.plist_path(scheduler.label_for('alpha', 'kernel')), 'rb') as f:
            self.assertEqual(plistlib.load(f)['ProgramArguments'][0],
                             installs.interpreter(self.old_venv))

    def test_a_rollback_never_prunes_the_venv_the_next_rollback_would_need(self):
        installs.write('alpha', NEW, self.new_venv, previous={'sha': OLD, 'venv': self.old_venv})
        rc, out = self.move(FakeRun(self.venvs, ci=False), SilentOps(), rollback=True)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(self.new_venv))

    def test_the_move_resumes_exactly_the_jobs_it_paused_on_every_exit(self):
        class FailSmoke(SilentOps):
            def smoke(self, product_name):
                return [], ['tick: gh missing']
        rc, out = self.move(FakeRun(self.venvs), FailSmoke(), to=SHA)
        self.assertEqual(rc, 1, out)
        self.assertEqual(scheduler.read_pauses('alpha'), {'tick': mock.ANY})

    def test_a_drain_that_times_out_boots_nothing_out_and_leaves_every_kernel_job_loaded(self):
        run = FakeRun(self.venvs, procs=['51'])
        run.listing = lambda: [(51, 'ci queue --apply --product alpha')]
        rc, out = self.move(run, SilentOps(), to=SHA, wait_s=1)
        self.assertEqual(rc, upgrade.MOVE_DEFERRED, out)
        self.assertIn(scheduler.label_for('alpha', 'kernel'), self.lc.loaded)
        self.assertIn(scheduler.label_for('alpha', 'kernel-watch'), self.lc.loaded)
        self.assertEqual(scheduler.read_pauses('alpha'), {'tick': mock.ANY})

    def test_a_signal_mid_drain_resumes_the_kernel_jobs_it_paused(self):
        import signal
        ops = SilentOps()
        run = FakeRun(self.venvs, procs=['51'])
        run.listing = lambda: [(51, 'ci queue --apply --product alpha')]

        def sleep_then_signal(_s):
            raise SystemExit(128 + signal.SIGTERM)
        out = []
        with self.assertRaises(SystemExit):
            upgrade.move('alpha', to=SHA, run=run, out=out.append,
                        sleep=sleep_then_signal, ops=ops, wait_s=30)
        self.assertEqual(scheduler.read_pauses('alpha'), {'tick': mock.ANY})
        self.assertIn(scheduler.label_for('alpha', 'kernel'), self.lc.loaded)
        self.assertIn(scheduler.label_for('alpha', 'kernel-watch'), self.lc.loaded)

    def test_a_killed_move_leaves_pause_records_the_next_move_lifts_for_the_kernel_jobs_too(self):
        scheduler.pause('alpha', ['kernel', 'kernel-watch'], reason=upgrade.PAUSE_REASON,
                        by='tester', bootout=False, pid=999999)
        self.assertIn('kernel', scheduler.read_pauses('alpha'))
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertIn(scheduler.label_for('alpha', 'kernel'), self.lc.loaded)
        self.assertIn(scheduler.label_for('alpha', 'kernel-watch'), self.lc.loaded)

    def test_a_move_that_has_nothing_to_move_touches_no_plist(self):
        installs.write('alpha', OLD, self.old_venv)
        kernel_before = self.plist_bytes('kernel')
        rc, out = self.move(FakeRun(self.venvs), SilentOps(), to=OLD)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.plist_bytes('kernel'), kernel_before)

    def test_the_hooks_install_and_host_clocks_step_are_unchanged(self):
        ops = FakeOps(clocks=('kernel', 'kernel-watch'))
        rc, out = self.move(FakeRun(self.venvs), ops, to=SHA)
        self.assertEqual(rc, 0, out)
        self.assertIn(('hooks', 'alpha'), ops.log)
        self.assertIn(('host',), ops.log)


if __name__ == '__main__':
    import unittest
    unittest.main()
