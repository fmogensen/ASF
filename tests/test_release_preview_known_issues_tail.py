import unittest

from asf import release_preview as rp


class KnownIssuesHandTailTests(unittest.TestCase):
    def test_a_regeneration_keeps_the_hand_kept_tail(self):
        old = '# Known issues\n\nold generated\n\n' + rp.HAND_MARKER + '\n\n## Installing\n\nhand text\n'
        page = rp.known_issues([], [], tail=rp.hand_tail(old))
        self.assertIn(rp.HAND_MARKER, page)
        self.assertIn('## Installing\n\nhand text', page)
        self.assertNotIn('old generated', page)

    def test_no_marker_means_no_tail(self):
        self.assertEqual(rp.hand_tail('# Known issues\n\nonly generated\n'), '')
        self.assertNotIn(rp.HAND_MARKER, rp.known_issues([], []))


if __name__ == '__main__':
    unittest.main()
