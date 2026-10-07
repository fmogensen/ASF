"""T-0691 (F-0228): health.refusal_heads and health.refusal_stands — the pair a refused publish
is remembered by, so republish_steps skips a publish whose inputs cannot have changed.

No repository, no network, no clock: hand-built asf.workers.lifecycle.Evidence and plain run
dicts over the two pure functions."""
import unittest

from asf.workers import health
from asf.workers import lifecycle


class RefusalHeadsTest(unittest.TestCase):
    def test_head_and_tip(self):
        ev = lifecycle.Evidence(head='1a2b3c4', remote_sha='9f8e7d6')
        self.assertEqual(health.refusal_heads(ev), '1a2b3c4 9f8e7d6')

    def test_sentinel_when_branch_not_on_origin(self):
        ev = lifecycle.Evidence(head='1a2b3c4', remote_sha='')
        self.assertEqual(health.refusal_heads(ev), '1a2b3c4 -')


class RefusalStandsTest(unittest.TestCase):
    def test_recorded_pair_matches_evidence(self):
        ev = lifecycle.Evidence(head='1a2b3c4', remote_sha='9f8e7d6')
        run = {'publish_refused': 'publish b refused: pre-push hook declined',
               'publish_refused_heads': '1a2b3c4 9f8e7d6'}
        self.assertTrue(health.refusal_stands(run, ev))

    def test_new_local_commit_head_moved(self):
        ev = lifecycle.Evidence(head='newhead1', remote_sha='9f8e7d6')
        run = {'publish_refused': 'publish b refused: pre-push hook declined',
               'publish_refused_heads': '1a2b3c4 9f8e7d6'}
        self.assertFalse(health.refusal_stands(run, ev))

    def test_origin_tip_moved(self):
        ev = lifecycle.Evidence(head='1a2b3c4', remote_sha='newtip22')
        run = {'publish_refused': 'publish b refused: pre-push hook declined',
               'publish_refused_heads': '1a2b3c4 9f8e7d6'}
        self.assertFalse(health.refusal_stands(run, ev))

    def test_both_moved(self):
        ev = lifecycle.Evidence(head='newhead1', remote_sha='newtip22')
        run = {'publish_refused': 'publish b refused: pre-push hook declined',
               'publish_refused_heads': '1a2b3c4 9f8e7d6'}
        self.assertFalse(health.refusal_stands(run, ev))

    def test_no_publish_refused_recorded(self):
        ev = lifecycle.Evidence(head='1a2b3c4', remote_sha='9f8e7d6')
        run = {'publish_refused_heads': '1a2b3c4 9f8e7d6'}
        self.assertFalse(health.refusal_stands(run, ev))

    def test_no_publish_refused_heads_recorded_yet(self):
        # a refusal recorded by a build before this card: it publishes once more, records the
        # pair, and is quiet after
        ev = lifecycle.Evidence(head='1a2b3c4', remote_sha='9f8e7d6')
        run = {'publish_refused': 'publish b refused: pre-push hook declined'}
        self.assertFalse(health.refusal_stands(run, ev))

    def test_unreadable_head_never_stands(self):
        # the one way an unreadable head could compare equal by accident
        ev = lifecycle.Evidence(head='', remote_sha='9f8e7d6')
        run = {'publish_refused': 'publish b refused: pre-push hook declined',
               'publish_refused_heads': ' 9f8e7d6'}
        self.assertFalse(health.refusal_stands(run, ev))

    def test_uncommitted_files_never_stand(self):
        ev = lifecycle.Evidence(head='1a2b3c4', remote_sha='9f8e7d6', uncommitted=1)
        run = {'publish_refused': 'publish b refused: pre-push hook declined',
               'publish_refused_heads': '1a2b3c4 9f8e7d6'}
        self.assertFalse(health.refusal_stands(run, ev))

    def test_branch_not_on_origin_refused_and_unchanged(self):
        ev = lifecycle.Evidence(head='1a2b3c4', remote_sha='')
        run = {'publish_refused': 'publish b refused: pre-push hook declined',
               'publish_refused_heads': '1a2b3c4 -'}
        self.assertTrue(health.refusal_stands(run, ev))


if __name__ == '__main__':
    unittest.main()
