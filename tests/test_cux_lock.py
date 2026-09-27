"""A wedged account-manager usage lock is named, and — opt-in — reclaimed (inbox: detect a wedged
cux usage lock and opt-in reclaim it).

The shape seen four times on 2026-09-27: a node wrapper → the native cux binary (parent of the
interactive claude session) → childless cux pollers, every one holding ``~/.cux/.lock``. Refresh
timed out, quota went stale, launches throttled.

* Detect: ``asf status`` Quota row and ``asf doctor`` say ``cux lock wedged N min — held by pid X
  (child of Y)``.
* Reclaim, only with ``quota_guards.reclaim_cux_lock: true``: SIGTERM a holder only when it is a
  cux process, a child of a cux process, not a claude process, 15 min old or more, and has no
  children. A holder that fails that is never signalled; the lock moves aside only when every
  other holder is the parent of a target.

Every test runs over a fake process table; nothing here signals a real process.
"""
import os
import signal
import tempfile
import unittest

from asf.workers import cuxlock

NODE = 'node /opt/homebrew/bin/cux --dangerously-skip-permissions'
CUX = '/opt/homebrew/lib/node_modules/@inulute/cux/bin/cux --dangerously-skip-permissions'
CLAUDE = '/Users/op/.local/bin/claude'
SHELL = '-zsh'
H = 3600


def proc(pid, ppid, age_s, command, start=None):
    return cuxlock.Proc(pid=pid, ppid=ppid, age_s=age_s, command=command,
                        start=start or f'start-{pid}')


def wedge_table(poller_age=40 * 60, extra=()):
    """The incident's tree: zsh → node wrapper → cux binary → (claude, 2 cux pollers)."""
    rows = [proc(100, 1, 3 * 86400, SHELL),
            proc(200, 100, 14 * H, NODE),
            proc(300, 200, 14 * H, CUX),
            proc(400, 300, 14 * H, CLAUDE),
            proc(501, 300, poller_age, CUX),
            proc(502, 300, poller_age, CUX)]
    rows.extend(extra)
    return {p.pid: p for p in rows}


HOLDERS = [300, 501, 502]


class TestParsing(unittest.TestCase):
    def test_etime_forms(self):
        self.assertEqual(cuxlock.parse_etime('00:47'), 47)
        self.assertEqual(cuxlock.parse_etime('01:39:47'), 5987)
        self.assertEqual(cuxlock.parse_etime('02-11:09:13'), 2 * 86400 + 11 * 3600 + 9 * 60 + 13)
        self.assertIsNone(cuxlock.parse_etime('garbage'))

    def test_ps_rows(self):
        text = (' 87609 87589    14:07:29 Sun Sep 27 10:29:01 2026     ' + CUX + '\n'
                ' 45543 87609       00:55 Mon Sep 28 00:36:22 2026     ' + CUX + '\n'
                'junk\n')
        t = cuxlock.parse_ps(text)
        self.assertEqual(sorted(t), [45543, 87609])
        self.assertEqual(t[45543].ppid, 87609)
        self.assertEqual(t[45543].age_s, 55)
        self.assertEqual(t[45543].start, 'Mon Sep 28 00:36:22 2026')
        self.assertEqual(t[45543].command, CUX)

    def test_lsof_pids(self):
        self.assertEqual(cuxlock.parse_lsof('p45543\nccux\nf10\np87609\nccux\nf10\n'),
                         [45543, 87609])
        self.assertEqual(cuxlock.parse_lsof(''), [])

    def test_names(self):
        self.assertTrue(cuxlock.is_cux(proc(1, 0, 0, CUX)))
        self.assertTrue(cuxlock.is_cux(proc(1, 0, 0, NODE)))
        self.assertFalse(cuxlock.is_cux(proc(1, 0, 0, CLAUDE)))
        self.assertTrue(cuxlock.is_claude(proc(1, 0, 0, CLAUDE)))
        self.assertTrue(cuxlock.is_claude(proc(1, 0, 0, 'node /usr/lib/claude-code/cli.js')))
        # the flag's own text is not a claude process
        self.assertFalse(cuxlock.is_claude(proc(1, 0, 0, CUX)))


class TestDiagnose(unittest.TestCase):
    def test_a_long_hold_is_wedged_and_named_by_its_leaf(self):
        w = cuxlock.diagnose(HOLDERS, wedge_table(poller_age=65 * 60))
        self.assertTrue(w.wedged)
        self.assertEqual(w.label, 'cux lock wedged 65 min — held by pid 501 (child of 300), '
                                  '+2 more')

    def test_a_short_hold_is_not_wedged(self):
        w = cuxlock.diagnose(HOLDERS, wedge_table(poller_age=30))
        self.assertFalse(w.wedged)
        self.assertIn('held', w.label)

    def test_no_holder_is_free(self):
        w = cuxlock.diagnose([], wedge_table())
        self.assertFalse(w.wedged)
        self.assertEqual(w.label, 'cux lock free')


class TestDecide(unittest.TestCase):
    def test_flag_off_touches_nothing(self):
        d = cuxlock.decide(HOLDERS, wedge_table(), enabled=False)
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())
        self.assertIn('reclaim_cux_lock', d.why)

    def test_the_incident_shape_reclaims_only_the_childless_pollers(self):
        d = cuxlock.decide(HOLDERS, wedge_table(), enabled=True)
        self.assertTrue(d.act, d.why)
        self.assertEqual(sorted(t.pid for t in d.targets), [501, 502])
        self.assertIn(300, d.refused)                    # has children: never signalled
        self.assertIn('children', d.refused[300])

    def test_too_young_is_refused(self):
        d = cuxlock.decide(HOLDERS, wedge_table(poller_age=14 * 60 + 59), enabled=True)
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())
        self.assertIn('younger', d.refused[501])

    def test_a_poller_with_a_child_is_refused(self):
        t = wedge_table(extra=[proc(600, 501, 60, '/bin/sh -c something')])
        d = cuxlock.decide(HOLDERS, t, enabled=True)
        self.assertNotIn(501, [p.pid for p in d.targets])
        self.assertIn('children', d.refused[501])
        # 502 alone is eligible, but 501 holds too and is no target's parent: no action at all
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())

    def test_the_wrapper_is_refused(self):
        # the node wrapper itself holding the lock: its parent is a shell, not cux
        t = wedge_table()
        t[200] = proc(200, 100, 14 * H, NODE)
        del t[300], t[400], t[501], t[502]
        d = cuxlock.decide([200], t, enabled=True)
        self.assertFalse(d.act)
        self.assertIn(200, d.refused)

    def test_a_childless_wrapper_is_still_refused(self):
        # a cux under a shell, childless and old: not a child of cux → the wrapper, never touched
        t = {100: proc(100, 1, H, SHELL), 200: proc(200, 100, H, CUX)}
        d = cuxlock.decide([200], t, enabled=True)
        self.assertFalse(d.act)
        self.assertIn('not a child of a cux', d.refused[200])

    def test_a_claude_process_is_refused(self):
        t = wedge_table()
        d = cuxlock.decide([400], t, enabled=True)          # claude, child of cux, childless, old
        self.assertFalse(d.act)
        self.assertIn('claude', d.refused[400])

    def test_a_non_cux_holder_is_refused(self):
        t = wedge_table(extra=[proc(700, 300, H, '/usr/bin/python3 x.py')])
        d = cuxlock.decide([700], t, enabled=True)
        self.assertFalse(d.act)
        self.assertIn('not a cux', d.refused[700])

    def test_an_unrelated_holder_blocks_the_move(self):
        # a young refresh from a shell holds too: the lock is in use, not wedged by the pollers alone
        t = wedge_table(extra=[proc(800, 100, 20, 'cux usage refresh')])
        d = cuxlock.decide(HOLDERS + [800], t, enabled=True)
        self.assertFalse(d.act)
        self.assertEqual(d.targets, ())

    def test_a_holder_missing_from_the_table_blocks(self):
        d = cuxlock.decide(HOLDERS + [999], wedge_table(), enabled=True)
        self.assertFalse(d.act)
        self.assertIn(999, d.refused)

    def test_own_process_and_ancestors_are_never_targets(self):
        t = wedge_table()
        d = cuxlock.decide(HOLDERS, t, enabled=True, protect={501})
        self.assertNotIn(501, [p.pid for p in d.targets])
        self.assertFalse(d.act)

    def test_pid_one_is_never_a_target(self):
        t = {0: proc(0, 0, H, CUX), 1: proc(1, 0, H, CUX)}
        d = cuxlock.decide([1], t, enabled=True)
        self.assertFalse(d.act)

    def test_enabled_needs_a_real_true(self):
        self.assertTrue(cuxlock.reclaim_enabled({'quota_guards': {'reclaim_cux_lock': True}}))
        for v in ('true', 1, 'yes', None, False):
            self.assertFalse(cuxlock.reclaim_enabled({'quota_guards': {'reclaim_cux_lock': v}}))
        self.assertFalse(cuxlock.reclaim_enabled({}))
        self.assertFalse(cuxlock.reclaim_enabled({'quota_guards': 'x'}))


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
        return cuxlock.reclaim({'quota_guards': {'reclaim_cux_lock': enabled}}, lock=self.lock,
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
        second[501] = proc(501, 300, 40 * 60, CUX, start='another-start')

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


class TestSurfaces(unittest.TestCase):
    def test_status_quota_cell_names_the_wedge(self):
        from unittest import mock
        from asf.views import status
        w = cuxlock.Wedge(True, 'cux lock wedged 65 min — held by pid 501 (child of 300)', 65)
        cfg = {'worker_pool': {'quota_command': 'q {account}', 'accounts': []}}
        with mock.patch.object(cuxlock, 'probe_wedge', return_value=w):
            self.assertEqual(status.quota_lock_prefix(cfg),
                             'cux lock wedged 65 min — held by pid 501 (child of 300); ')

    def test_doctor_row(self):
        from unittest import mock
        from asf import doctor
        w = cuxlock.Wedge(True, 'cux lock wedged 65 min — held by pid 501 (child of 300)', 65)
        with mock.patch.object(cuxlock, 'probe_wedge', return_value=w):
            ok, detail = doctor.check_cux_lock()
        self.assertFalse(ok)
        self.assertIn('cux lock wedged 65 min', detail)
        self.assertIn('reclaim_cux_lock', detail)
        with mock.patch.object(cuxlock, 'probe_wedge', return_value=None):
            self.assertIsNone(doctor.check_cux_lock())


if __name__ == '__main__':
    unittest.main()
