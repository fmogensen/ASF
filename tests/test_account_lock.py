"""A wedged account-manager usage lock is named, and — opt-in — reclaimed (inbox: detect a wedged
usage lock and opt-in reclaim it).

The shape seen four times on 2026-09-27: a node wrapper → the native account-manager binary
(parent of the interactive claude session) → childless pollers, every one holding the manager's
lock file (``account_lock.path``). Refresh timed out, quota went stale, launches throttled.

* Detect: ``asf status`` Quota row and ``asf doctor`` say ``account lock wedged N min — held by
  pid X (child of Y)`` — only when ``account_lock.path`` names a lock.
* Reclaim, only with ``account_lock.reclaim: true`` (or the older ``quota_guards.reclaim_cux_lock``)
  and ``account_lock.process`` set: SIGTERM a holder only when it is that process, a child of it,
  not a claude process, 15 min old or more, and has no children. A holder that fails that is never
  signalled; the lock moves aside only when every other holder is the parent of a target.

Every test runs over a fake process table; nothing here signals a real process.
"""
import os
import signal
import tempfile
import unittest

from asf.workers import account_lock as lockmod

MGR = 'acctmgr'   # a made-up account manager's program name (account_lock.process)
NODE = 'node /usr/local/bin/acctmgr --dangerously-skip-permissions'
MGR_BIN = '/usr/local/lib/node_modules/acctmgr/bin/acctmgr --dangerously-skip-permissions'
CLAUDE = '/home/someone/.local/bin/claude'
SHELL = '-zsh'
H = 3600


def proc(pid, ppid, age_s, command, start=None):
    return lockmod.Proc(pid=pid, ppid=ppid, age_s=age_s, command=command,
                        start=start or f'start-{pid}')


def wedge_table(poller_age=40 * 60, extra=()):
    """The incident's tree: zsh → node wrapper → manager binary → (claude, 2 manager pollers)."""
    rows = [proc(100, 1, 3 * 86400, SHELL),
            proc(200, 100, 14 * H, NODE),
            proc(300, 200, 14 * H, MGR_BIN),
            proc(400, 300, 14 * H, CLAUDE),
            proc(501, 300, poller_age, MGR_BIN),
            proc(502, 300, poller_age, MGR_BIN)]
    rows.extend(extra)
    return {p.pid: p for p in rows}


HOLDERS = [300, 501, 502]


class TestParsing(unittest.TestCase):
    def test_etime_forms(self):
        self.assertEqual(lockmod.parse_etime('00:47'), 47)
        self.assertEqual(lockmod.parse_etime('01:39:47'), 5987)
        self.assertEqual(lockmod.parse_etime('02-11:09:13'), 2 * 86400 + 11 * 3600 + 9 * 60 + 13)
        self.assertIsNone(lockmod.parse_etime('garbage'))

    def test_ps_rows(self):
        text = (' 87609 87589    14:07:29 Sun Sep 27 10:29:01 2026     ' + MGR_BIN + '\n'
                ' 45543 87609       00:55 Mon Sep 28 00:36:22 2026     ' + MGR_BIN + '\n'
                'junk\n')
        t = lockmod.parse_ps(text)
        self.assertEqual(sorted(t), [45543, 87609])
        self.assertEqual(t[45543].ppid, 87609)
        self.assertEqual(t[45543].age_s, 55)
        self.assertEqual(t[45543].start, 'Mon Sep 28 00:36:22 2026')
        self.assertEqual(t[45543].command, MGR_BIN)

    def test_lsof_pids(self):
        self.assertEqual(lockmod.parse_lsof('p45543\ncacctmgr\nf10\np87609\ncacctmgr\nf10\n'),
                         [45543, 87609])
        self.assertEqual(lockmod.parse_lsof(''), [])

    def test_names(self):
        self.assertTrue(lockmod.is_manager(proc(1, 0, 0, MGR_BIN), MGR))
        self.assertTrue(lockmod.is_manager(proc(1, 0, 0, NODE), MGR))
        self.assertFalse(lockmod.is_manager(proc(1, 0, 0, CLAUDE), MGR))
        self.assertTrue(lockmod.is_claude(proc(1, 0, 0, CLAUDE)))
        self.assertTrue(lockmod.is_claude(proc(1, 0, 0, 'node /usr/lib/claude-code/cli.js')))
        # the flag's own text is not a claude process
        self.assertFalse(lockmod.is_claude(proc(1, 0, 0, MGR_BIN)))


class TestDiagnose(unittest.TestCase):
    def test_a_long_hold_is_wedged_and_named_by_its_leaf(self):
        w = lockmod.diagnose(HOLDERS, wedge_table(poller_age=65 * 60))
        self.assertTrue(w.wedged)
        self.assertEqual(w.label, 'account lock wedged 65 min — held by pid 501 (child of 300), '
                                  '+2 more')

    def test_a_short_hold_is_not_wedged(self):
        w = lockmod.diagnose(HOLDERS, wedge_table(poller_age=30))
        self.assertFalse(w.wedged)
        self.assertIn('held', w.label)

    def test_no_holder_is_free(self):
        w = lockmod.diagnose([], wedge_table())
        self.assertFalse(w.wedged)
        self.assertEqual(w.label, 'account lock free')


class TestDecide(unittest.TestCase):
    def test_flag_off_touches_nothing(self):
        d = lockmod.decide(HOLDERS, wedge_table(), enabled=False)
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())
        self.assertIn('account_lock.reclaim', d.why)

    def test_the_incident_shape_reclaims_only_the_childless_pollers(self):
        d = lockmod.decide(HOLDERS, wedge_table(), enabled=True, name=MGR)
        self.assertTrue(d.act, d.why)
        self.assertEqual(sorted(t.pid for t in d.targets), [501, 502])
        self.assertIn(300, d.refused)                    # has children: never signalled
        self.assertIn('children', d.refused[300])

    def test_too_young_is_refused(self):
        d = lockmod.decide(HOLDERS, wedge_table(poller_age=14 * 60 + 59), enabled=True, name=MGR)
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())
        self.assertIn('younger', d.refused[501])

    def test_a_poller_with_a_child_is_refused(self):
        t = wedge_table(extra=[proc(600, 501, 60, '/bin/sh -c something')])
        d = lockmod.decide(HOLDERS, t, enabled=True, name=MGR)
        self.assertNotIn(501, [p.pid for p in d.targets])
        self.assertIn('children', d.refused[501])
        # 502 alone is eligible, but 501 holds too and is no target's parent: no action at all
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())

    def test_the_wrapper_is_refused(self):
        # the node wrapper itself holding the lock: its parent is a shell, not the manager
        t = wedge_table()
        t[200] = proc(200, 100, 14 * H, NODE)
        del t[300], t[400], t[501], t[502]
        d = lockmod.decide([200], t, enabled=True, name=MGR)
        self.assertFalse(d.act)
        self.assertIn(200, d.refused)

    def test_a_childless_wrapper_is_still_refused(self):
        # a manager under a shell, childless and old: not its child → the wrapper, never touched
        t = {100: proc(100, 1, H, SHELL), 200: proc(200, 100, H, MGR_BIN)}
        d = lockmod.decide([200], t, enabled=True, name=MGR)
        self.assertFalse(d.act)
        self.assertIn('not a child of an account-manager', d.refused[200])

    def test_a_claude_process_is_refused(self):
        t = wedge_table()
        d = lockmod.decide([400], t, enabled=True, name=MGR)          # claude, child of the manager, childless, old
        self.assertFalse(d.act)
        self.assertIn('claude', d.refused[400])

    def test_a_non_manager_holder_is_refused(self):
        t = wedge_table(extra=[proc(700, 300, H, '/usr/bin/python3 x.py')])
        d = lockmod.decide([700], t, enabled=True, name=MGR)
        self.assertFalse(d.act)
        self.assertIn('not an account-manager', d.refused[700])

    def test_an_unrelated_holder_blocks_the_move(self):
        # a young refresh from a shell holds too: the lock is in use, not wedged by the pollers alone
        t = wedge_table(extra=[proc(800, 100, 20, 'acctmgr usage refresh')])
        d = lockmod.decide(HOLDERS + [800], t, enabled=True, name=MGR)
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())

    def test_a_holder_missing_from_the_table_blocks(self):
        d = lockmod.decide(HOLDERS + [999], wedge_table(), enabled=True, name=MGR)
        self.assertFalse(d.act)
        self.assertIn(999, d.refused)

    def test_own_process_and_ancestors_are_never_targets(self):
        t = wedge_table()
        d = lockmod.decide(HOLDERS, t, enabled=True, protect={501}, name=MGR)
        self.assertNotIn(501, [p.pid for p in d.targets])
        self.assertFalse(d.act)

    def test_pid_one_is_never_a_target(self):
        t = {0: proc(0, 0, H, MGR_BIN), 1: proc(1, 0, H, MGR_BIN)}
        d = lockmod.decide([1], t, enabled=True, name=MGR)
        self.assertFalse(d.act)

    def test_without_a_process_name_nothing_is_a_target(self):
        d = lockmod.decide(HOLDERS, wedge_table(), enabled=True)
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())
        self.assertIn('account_lock.process', d.why)

    def test_enabled_needs_a_real_true(self):
        self.assertTrue(lockmod.reclaim_enabled({'account_lock': {'reclaim': True}}))
        for v in ('true', 1, 'yes', None, False):
            self.assertFalse(lockmod.reclaim_enabled({'account_lock': {'reclaim': v}}))
        # the older spelling is still honoured
        self.assertTrue(lockmod.reclaim_enabled({'quota_guards': {'reclaim_cux_lock': True}}))
        for v in ('true', 1, 'yes', None, False):
            self.assertFalse(lockmod.reclaim_enabled({'quota_guards': {'reclaim_cux_lock': v}}))
        self.assertFalse(lockmod.reclaim_enabled({}))
        self.assertFalse(lockmod.reclaim_enabled({'quota_guards': 'x'}))


class FakeProbe:
    def __init__(self, holders, table):
        self.holders, self.table = holders, table

    def __call__(self, path):
        return list(self.holders), dict(self.table)


class TestReclaim(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.lock = os.path.join(self.dir, '.lock')
        open(self.lock, 'w').close()
        self.killed, self.runs = [], []

    def kill(self, pid, sig):
        self.killed.append((pid, sig))

    def run_refresh(self):
        self.runs.append(1)
        return 0

    def reclaim(self, table, enabled=True, holders=HOLDERS, probe=None):
        return lockmod.reclaim({'account_lock': {'reclaim': enabled, 'process': MGR}}, lock=self.lock,
                               probe=probe or FakeProbe(holders, table), kill=self.kill,
                               refresh=self.run_refresh, protect=set(),
                               guard_dir=self.dir)

    def test_flag_off_never_signals_or_moves(self):
        rec = self.reclaim(wedge_table(), enabled=False)
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self.lock))
        self.assertEqual(self.runs, [])
        self.assertFalse(rec['acted'])

    def test_the_incident_is_reclaimed_with_sigterm_move_and_one_refresh(self):
        rec = self.reclaim(wedge_table())
        self.assertEqual(sorted(self.killed), [(501, signal.SIGTERM), (502, signal.SIGTERM)])
        self.assertFalse(os.path.exists(self.lock))
        moved = [f for f in os.listdir(self.dir) if f.startswith('.lock.stale-')]
        self.assertEqual(len(moved), 1)
        self.assertEqual(self.runs, [1])
        self.assertTrue(rec['acted'])
        self.assertEqual(sorted(t['pid'] for t in rec['terminated']), [501, 502])
        self.assertEqual({t['ppid'] for t in rec['terminated']}, {300})

    def test_nothing_wedged_does_nothing(self):
        self.reclaim(wedge_table(poller_age=60))
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self.lock))

    def test_a_pid_reused_between_looks_is_not_signalled(self):
        first = wedge_table()
        second = wedge_table()
        second[501] = proc(501, 300, 40 * 60, MGR_BIN, start='another-start')

        class Two:
            n = 0

            def __call__(self, path):
                Two.n += 1
                return list(HOLDERS), dict(first if Two.n == 1 else second)
        self.reclaim(None, probe=Two())
        self.assertNotIn(501, [p for p, _ in self.killed])

    def test_a_second_look_that_no_longer_qualifies_stops_everything(self):
        first = wedge_table()
        second = wedge_table(extra=[proc(600, 501, 5, '/bin/sh')])   # 501 grew a child

        class Two:
            n = 0

            def __call__(self, path):
                Two.n += 1
                return list(HOLDERS), dict(first if Two.n == 1 else second)
        rec = self.reclaim(None, probe=Two())
        self.assertEqual(self.killed, [])
        self.assertTrue(os.path.exists(self.lock))
        self.assertFalse(rec['acted'])

    def test_no_lock_file_is_nothing(self):
        os.remove(self.lock)
        rec = self.reclaim(wedge_table())
        self.assertEqual(self.killed, [])
        self.assertFalse(rec['acted'])


class TestConfig(unittest.TestCase):
    """The lock is an interface the operator names; with nothing configured nothing is probed."""

    def setUp(self):
        self._env = os.environ.pop('ASF_ACCOUNT_LOCK', None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._env is not None:
            os.environ['ASF_ACCOUNT_LOCK'] = self._env

    def test_unconfigured_is_off(self):
        self.assertIsNone(lockmod.lock_path({}))
        self.assertIsNone(lockmod.probe_wedge({}))
        self.assertIsNone(lockmod.refresh_command({}))
        self.assertEqual(lockmod.process_name({}), '')

    def test_configured_path_expands_home(self):
        self.assertEqual(lockmod.lock_path({'account_lock': {'path': '~/.mgr/.lock'}}),
                         os.path.expanduser('~/.mgr/.lock'))

    def test_refresh_command_string_or_list(self):
        self.assertEqual(lockmod.refresh_command({'account_lock': {'refresh_command': 'mgr usage refresh'}}),
                         ['mgr', 'usage', 'refresh'])
        self.assertEqual(lockmod.refresh_command({'account_lock': {'refresh_command': ['mgr', 'r']}}),
                         ['mgr', 'r'])
        self.assertEqual(lockmod.run_refresh(None), 'none')


class TestSurfaces(unittest.TestCase):
    def test_status_quota_cell_names_the_wedge(self):
        from unittest import mock
        from asf.views import status
        w = lockmod.Wedge(True, 'account lock wedged 65 min — held by pid 501 (child of 300)', 65)
        cfg = {'worker_pool': {'quota_command': 'q {account}', 'accounts': []}}
        with mock.patch.object(lockmod, 'probe_wedge', return_value=w):
            self.assertEqual(status.quota_lock_prefix(cfg),
                             'account lock wedged 65 min — held by pid 501 (child of 300); ')

    def test_doctor_row(self):
        from unittest import mock
        from asf import doctor
        w = lockmod.Wedge(True, 'account lock wedged 65 min — held by pid 501 (child of 300)', 65)
        with mock.patch.object(lockmod, 'probe_wedge', return_value=w):
            ok, detail = doctor.check_account_lock({})
        self.assertFalse(ok)
        self.assertIn('account lock wedged 65 min', detail)
        self.assertIn('account_lock.reclaim', detail)
        with mock.patch.object(lockmod, 'probe_wedge', return_value=None):
            self.assertIsNone(doctor.check_account_lock({}))


if __name__ == '__main__':
    unittest.main()
