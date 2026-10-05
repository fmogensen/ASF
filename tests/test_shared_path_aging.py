"""The shared-path hold ages (round E #22).

One branch a tick takes a shared file (``conventions.shared_paths``/``shared_writes``); the
first entry to ask won it every tick, so a product's green code PR (T-0389, #1091) starved behind
a stream of document PRs on one shared registry file. A branch that has waited on the shared path
for ``lane.shared_path_aging`` now goes first, oldest wait first."""
import datetime
import unittest
from types import SimpleNamespace
from unittest import mock

from asf import conventions
from asf.conventions import Conventions
from asf.harvest import lane as lane_mod

NOW = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


def iso(minutes_ago):
    return datetime.datetime.fromtimestamp(NOW - minutes_ago * 60, datetime.timezone.utc) \
        .strftime('%Y-%m-%dT%H:%M:%SZ')


def entry(branch, waited=None, reason='shared-path'):
    prev = {'state': lane_mod.WAITING, 'reason': reason, 'at': iso(waited)} \
        if waited is not None else {'state': lane_mod.PR_OPEN, 'at': iso(1)}
    return {'branch': branch, 'item': branch.upper(), 'prev': prev, 'head': 'h' * 40,
            'files': ['docs/decisions/bands.md']}


def fake_lane(aging=None):
    conv = Conventions.from_mapping({'shared_paths': ['docs/decisions/bands.md'],
                                     **({'lane': {'shared_path_aging': aging}} if aging else {})})
    return SimpleNamespace(conv=conv, product=SimpleNamespace(conventions=conv), out=lambda s: None,
                           now=NOW, repo='/nonexistent', trunk='main', host=object(),
                           dry_run=True, auto=False)


class OrderTests(unittest.TestCase):

    def test_an_aged_wait_goes_before_the_newcomers(self):
        es = [entry('docs-1'), entry('docs-2'), entry('code-1', waited=45)]
        got = [f['branch'] for f in lane_mod.shared_path_order(fake_lane(), es)]
        self.assertEqual(got, ['code-1', 'docs-1', 'docs-2'])

    def test_oldest_wait_first_and_a_young_wait_keeps_its_place(self):
        es = [entry('young', waited=5), entry('a', waited=40), entry('b', waited=90)]
        got = [f['branch'] for f in lane_mod.shared_path_order(fake_lane(), es)]
        self.assertEqual(got, ['b', 'a', 'young'])

    def test_the_config_key_sets_the_age(self):
        es = [entry('docs-1'), entry('code-1', waited=45)]
        got = [f['branch'] for f in lane_mod.shared_path_order(fake_lane('2h'), es)]
        self.assertEqual(got, ['docs-1', 'code-1'])
        got = [f['branch'] for f in lane_mod.shared_path_order(fake_lane('0s'), es)]
        self.assertEqual(got, ['code-1', 'docs-1'])

    def test_another_wait_reason_does_not_age(self):
        es = [entry('docs-1'), entry('code-1', waited=300, reason='approval code (manual)')]
        got = [f['branch'] for f in lane_mod.shared_path_order(fake_lane(), es)]
        self.assertEqual(got, ['docs-1', 'code-1'])


class PrecheckTests(unittest.TestCase):

    def test_the_starved_code_branch_takes_the_shared_path_this_tick(self):
        lane = fake_lane()
        waited = []
        with mock.patch.object(lane_mod, 'has_adjudicate_commit', lambda *a: False), \
                mock.patch.object(lane_mod.customer_content, 'refusal', lambda *a: None), \
                mock.patch.object(lane_mod.approvals, 'merge_class', lambda *a: ('code', '')), \
                mock.patch.object(lane_mod.approvals, 'merge_level', lambda *a: 'auto'), \
                mock.patch.object(lane_mod, 'skip_regate', lambda *a: False), \
                mock.patch.object(lane_mod, 'wait',
                                  lambda lane, f, reason, *a, **k: waited.append(
                                      (f['branch'], reason))):
            ready = lane_mod.precheck(lane, [entry('docs-9'), entry('code-1', waited=120)])
        self.assertEqual([f['branch'] for f in ready], ['code-1'])
        self.assertEqual(waited, [('docs-9', 'shared-path')])


class ConfigTests(unittest.TestCase):

    def test_default_and_validation(self):
        self.assertEqual(Conventions().lane_shared_path_aging_s(), 30 * 60)
        self.assertEqual(conventions.DEFAULT_LANE_SHARED_PATH_AGING, '30m')
        problems = conventions.validate_mapping({'lane': {'shared_path_aging': 'soon'}})
        self.assertIn('lane.shared_path_aging', [k for k, _ in problems])
        self.assertEqual(conventions.validate_mapping({'lane': {'shared_path_aging': '2h'}}), [])


if __name__ == '__main__':
    unittest.main()
