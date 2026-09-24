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
