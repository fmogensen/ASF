"""``asf set <id> reshape_applied=current --why "…"``: a reshape the record cannot apply, carried
out by hand, is recorded the way the replan applier records one — ``reshape_applied: <digest>``,
``reshape_applied_at`` and a History line — so the feeder's REPLAN row ends instead of a hand
edit of the typed block (a product's F-1144 reshape, applied by hand on 2026-10-07)."""
import shutil
import unittest

from asf.record import frontmatter, replan
from tests.test_backlog import make_repo, run, write_item

HOW = 'cut the plan by directory'


class ReshapeAppliedIsSettable(unittest.TestCase):

    def setUp(self):
        self.root = make_repo()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        write_item(self.root, 'E-0001', 'epic', 'Factory')
        self.f = write_item(self.root, 'F-10001', 'feature', 'Feat', parent='E-0001',
                            typed_lines=(f'reshape: {HOW}',))
        self.bare = write_item(self.root, 'F-10002', 'feature', 'Bare', parent='E-0001')

    def meta(self, path):
        with open(path, encoding='utf-8') as f:
            return frontmatter.parse(f.read())

    def test_current_writes_the_digest_the_time_and_a_history_line(self):
        r = run(['set', 'F-10001', 'reshape_applied=current', '--why', 'applied by hand: T-51302..'],
                self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        meta, body = self.meta(self.f)
        self.assertEqual(meta['reshape_applied'], replan.digest(HOW))
        self.assertTrue(meta['reshape_applied_at'])
        self.assertEqual(replan.pending(meta), '')
        self.assertIn(f'set: reshape_applied {replan.digest(HOW)} — applied by hand: T-51302..',
                      body)

    def test_it_wants_a_why(self):
        with open(self.f, encoding='utf-8') as f:
            before = f.read()
        r = run(['set', 'F-10001', 'reshape_applied=current'], self.root)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('--why', r.stderr)
        with open(self.f, encoding='utf-8') as f:
            self.assertEqual(f.read(), before)

    def test_it_takes_only_current_and_a_card_with_a_reshape(self):
        r = run(['set', 'F-10001', 'reshape_applied=abc123', '--why', 'x'], self.root)
        self.assertEqual(r.returncode, 2, r.stdout)
        r = run(['set', 'F-10002', 'reshape_applied=current', '--why', 'x'], self.root)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('no reshape', r.stderr)


if __name__ == '__main__':
    unittest.main()
