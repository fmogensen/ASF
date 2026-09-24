"""``asf tick --dry-run`` (:mod:`asf.tick.dry_run`) — the rollout's rehearsal (plan §6).

Reuses the lane's own e2e harness (:mod:`tests.test_e2e_lane`): a red trunk holds a
ready-to-land branch open and stable (:class:`tests.test_e2e_lane.RedTrunk`), so there is
something real for the dry run to print, and the real tick that put it there has already
happened — a dry run must never move it further.

Asserts the two things the package gate requires of it (plan §6, package gate step 3): it never
writes the real state directory (a checksum before and after), and two runs back to back print
the same thing (idempotence).
"""
import hashlib
import os
import tempfile
import types
import unittest
from unittest import mock

from tests.test_e2e_lane import PR, FF, LaneCase


def _checksum(path):
    """One hash of every file's path and bytes under ``path`` — any write anywhere below it,
    even one that leaves the byte count unchanged, changes this."""
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(root, name)
            rel = os.path.relpath(p, path)
            h.update(rel.encode())
            try:
                with open(p, 'rb') as f:
                    h.update(f.read())
            except OSError:
                pass
    return h.hexdigest()


class DryRun:
    """T-0001's branch is ready to land, then the trunk turns red (:class:`RedTrunk`'s own
    setup): a real tick already decided ``WAITING reason=trunk-red`` before either dry run runs."""

    stage = 'planned'
    start = 'ready'

    def dry_runs(self):
        """``(before, after1, after2, lines1, lines2)`` — two ``dry_run.run`` calls back to
        back, under the factory's own environment (:meth:`Factory.seams`), and the real state
        directory's checksum before and after each."""
        from asf import env
        from asf.tick import dry_run
        with self.f.seams():
            product = env.load_product('sample')
            real_state = env.state_dir(product)
            before = _checksum(real_state)
            lines1 = []
            rc1 = dry_run.run(product, out=lines1.append)
            after1 = _checksum(real_state)
            lines2 = []
            rc2 = dry_run.run(product, out=lines2.append)
            after2 = _checksum(real_state)
        self.assertEqual(rc1, 0, lines1)
        self.assertEqual(rc2, 0, lines2)
        return before, after1, after2, lines1, lines2

    def test_never_touches_real_state_and_is_idempotent(self):
        self.ready_to_land('T-0001')
        self.red_trunk()
        self.tick()  # real: decides WAITING (trunk-red) for feature/T-0001 — see RedTrunk

        before, after1, after2, lines1, lines2 = self.dry_runs()

        self.assertEqual(before, after1, 'a dry run wrote to the real state directory')
        self.assertEqual(after1, after2, 'a second dry run wrote to the real state directory')
        self.assertEqual(lines1, lines2, 'two dry runs printed different things')
        self.assertFalse([l for l in lines1 if l.startswith('INVARIANT')], lines1)
        # T-0001's branch, still open and still waiting — never landed, never touched
        self.assertTrue(any('feature/T-0001' in l for l in lines1), lines1)
        self.assertTrue(self.f.on_origin('feature/T-0001'))
        self.assertFalse(self.harvested('T-0001'))


class DryRunReadyPR(DryRun, LaneCase):
    landing = PR


class DryRunReadyFF(DryRun, LaneCase):
    landing = FF


class DryRunHostPressure(unittest.TestCase):
    """Under host pressure the dry run's wave says what a live tick would: every launching row
    waits, held by the host guard, and the section ends on ``wave: held: …`` — nothing launched."""

    def wave_lines(self, reading):
        from asf import capacity as capacity_mod
        from asf import invariants
        from asf.feeder import rows as feeder_rows
        from asf.tick import dry_run, step_wave
        from asf.views import index_reader
        row = feeder_rows.Row(0, 'BUG → FIX', 'B-0001', '', 'would launch fix-bug-b-0001',
                              'fix-bug', 'fix/B-0001', '')
        product = types.SimpleNamespace(name='sample', repo_dir='')
        root = tempfile.mkdtemp(prefix='asf-dry-wave-')
        open(os.path.join(root, 'index.json'), 'w').close()
        lines = []
        with mock.patch.object(index_reader, 'load', return_value=({}, None)), \
                mock.patch.object(step_wave, 'inflight', return_value={}), \
                mock.patch.object(capacity_mod, 'resolve',
                                  return_value=types.SimpleNamespace(sessions=2)), \
                mock.patch.object(step_wave, 'plan_inputs', return_value={}), \
                mock.patch.object(feeder_rows, 'plan_rows', lambda *a, **kw: [row]), \
                mock.patch.object(invariants, 'feeder_gate', lambda p, rows, *a, **kw: rows), \
                mock.patch.dict(os.environ, {'ASF_HOST_READING': reading}):
            dry_run._wave_rows(product, root, lines.append)
        return lines

    def test_a_loaded_host_holds_every_launch(self):
        lines = self.wave_lines('90 12 87')
        self.assertFalse([l for l in lines if l.startswith('would launch')], lines)
        self.assertTrue(any(l.startswith('waits        fix-bug-b-0001') and
                            l.endswith('— held: host pressure load 90/cores 12, swap 87%')
                            for l in lines), lines)
        self.assertEqual(lines[-1], 'wave: held: host pressure load 90/cores 12, swap 87% — '
                                    'no new session this tick; running sessions go on')

    def test_a_quiet_host_would_launch(self):
        lines = self.wave_lines('0 1 0')
        self.assertTrue(any(l.startswith('would launch fix-bug-b-0001') for l in lines), lines)
        self.assertFalse([l for l in lines if l.startswith('wave: held')], lines)
