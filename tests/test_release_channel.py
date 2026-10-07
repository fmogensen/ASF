"""asf.upgrade's release primitives (F-0112 Task 2): the hourly shared cache, version compare,
mid-landing, and the tag-safe install — readers, before any channel or policy exists."""
import json
import unittest
from unittest import mock

from asf import upgrade
from tests.test_install import FakeRun, HomeCase

URL = 'https://github.com/o/r.git'


def ls_remote(entries):
    """``git ls-remote --tags`` output: ``entries`` is ``[(sha, tag, peeled)]``."""
    lines = []
    for sha, tag, peeled in entries:
        ref = f'refs/tags/{tag}' + ('^{}' if peeled else '')
        lines.append(f'{sha}\t{ref}')
    return '\n'.join(lines) + '\n'


class FakeGitRun:
    """``subprocess.run`` for ``git ls-remote`` alone."""

    def __init__(self, output=''):
        self.output = output
        self.calls = []

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        return mock.Mock(returncode=0, stdout=self.output, stderr='')


class VersionComparisonTests(unittest.TestCase):
    def test_version_tuple_reads_major_minor_patch_and_past(self):
        self.assertEqual(upgrade.version_tuple('v0.1.62'), (0, 1, 62, 0))
        self.assertEqual(upgrade.version_tuple('v0.1.62+7'), (0, 1, 62, 7))

    def test_an_older_tag_is_not_newer(self):
        self.assertFalse(upgrade.newer('v0.1.9', 'v0.1.62'))

    def test_a_newer_tag_beats_a_plus_build_too(self):
        self.assertTrue(upgrade.newer('v0.1.63', 'v0.1.62'))
        self.assertTrue(upgrade.newer('v0.1.63', 'v0.1.62+7'))

    def test_a_bare_tag_is_not_newer_than_its_own_plus_build(self):
        self.assertFalse(upgrade.newer('v0.1.62', 'v0.1.62+7'))

    def test_a_plus_build_is_newer_than_its_own_bare_tag(self):
        self.assertTrue(upgrade.newer('v0.1.62+7', 'v0.1.62'))

    def test_an_unreadable_side_is_never_newer(self):
        self.assertFalse(upgrade.newer('v0.1.62', None))
        self.assertFalse(upgrade.newer(None, 'v0.1.62'))
        self.assertFalse(upgrade.newer('main', 'v0.1.62'))


class LatestReleaseIsReadOnceAnHourTests(HomeCase):
    SHA = 'a' * 40
    SHA2 = 'b' * 40

    def test_three_calls_inside_the_hour_run_ls_remote_once(self):
        run = FakeGitRun(ls_remote([(self.SHA, 'v0.1.62', False)]))
        now = 1_000_000.0
        for _ in range(3):
            tag, sha = upgrade.latest_release(URL, now=now, run=run)
        self.assertEqual((tag, sha), ('v0.1.62', self.SHA))
        self.assertEqual(len(run.calls), 1)

    def test_a_different_url_is_its_own_cache_entry(self):
        run = FakeGitRun(ls_remote([(self.SHA, 'v0.1.62', False)]))
        upgrade.latest_release(URL, now=1000.0, run=run)
        run2 = FakeGitRun(ls_remote([(self.SHA2, 'v0.1.1', False)]))
        tag2, sha2 = upgrade.latest_release('https://github.com/o/other.git', now=1000.0, run=run2)
        self.assertEqual((tag2, sha2), ('v0.1.1', self.SHA2))
        self.assertEqual(len(run2.calls), 1)

    def test_a_call_past_the_poll_interval_reads_again(self):
        run = FakeGitRun(ls_remote([(self.SHA, 'v0.1.62', False)]))
        upgrade.latest_release(URL, now=1000.0, run=run)
        run.output = ls_remote([(self.SHA2, 'v0.1.63', False)])
        tag, sha = upgrade.latest_release(URL, now=1000.0 + upgrade.RELEASE_POLL_S + 1, run=run)
        self.assertEqual((tag, sha), ('v0.1.63', self.SHA2))
        self.assertEqual(len(run.calls), 2)

    def test_a_failed_read_keeps_the_previous_tag_and_sha_but_stamps_at(self):
        run = FakeGitRun(ls_remote([(self.SHA, 'v0.1.62', False)]))
        upgrade.latest_release(URL, now=1000.0, run=run)
        failing = mock.Mock(side_effect=OSError())
        later = 1000.0 + upgrade.RELEASE_POLL_S + 1
        tag, sha = upgrade.latest_release(URL, now=later, run=failing)
        self.assertEqual((tag, sha), ('v0.1.62', self.SHA))
        with open(upgrade.releases_path(), encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data[URL]['at'], later)

    def test_a_first_ever_failed_read_is_none_none(self):
        failing = mock.Mock(side_effect=OSError())
        self.assertEqual(upgrade.latest_release(URL, now=1000.0, run=failing), (None, None))

    def test_the_peeled_line_not_the_tag_objects_is_the_sha_returned(self):
        output = ls_remote([(self.SHA, 'v0.1.62', False), (self.SHA2, 'v0.1.62', True)])
        run = FakeGitRun(output)
        tag, sha = upgrade.latest_release(URL, now=1000.0, run=run)
        self.assertEqual((tag, sha), ('v0.1.62', self.SHA2))


class MidLandingTests(unittest.TestCase):
    def test_a_pushed_to_land_row_returns_its_item_and_branch(self):
        occ = {'landing': {'T-0432': {'branch': 'worker/T-0432', 'state': 'LAND', 'pr': 1,
                                      'why': ''}}}
        with mock.patch('asf.workers.lifecycle.occupancy', return_value=occ):
            self.assertEqual(upgrade.mid_landing('asf'), [('T-0432', 'worker/T-0432')])

    def test_several_rows_come_back_sorted_by_item(self):
        occ = {'landing': {'T-2': {'branch': 'worker/T-2'}, 'T-1': {'branch': 'worker/T-1'}}}
        with mock.patch('asf.workers.lifecycle.occupancy', return_value=occ):
            self.assertEqual(upgrade.mid_landing('asf'),
                             [('T-1', 'worker/T-1'), ('T-2', 'worker/T-2')])

    def test_an_empty_registry_returns_nothing(self):
        with mock.patch('asf.workers.lifecycle.occupancy', return_value={'landing': {}}):
            self.assertEqual(upgrade.mid_landing('asf'), [])

    def test_an_unreadable_path_returns_nothing(self):
        with mock.patch('asf.workers.lifecycle.occupancy', side_effect=OSError()):
            self.assertEqual(upgrade.mid_landing('asf'), [])

    def test_it_reads_the_products_own_session_ledger(self):
        with mock.patch('asf.workers.lifecycle.occupancy', return_value={'landing': {}}) as occ, \
                mock.patch('asf.workers.pool.sessions_path', return_value='/x/sessions.jsonl') as sp:
            upgrade.mid_landing('asf')
        sp.assert_called_once_with('asf')
        occ.assert_called_once_with('/x/sessions.jsonl')


class TagInstallVerifiesByShaTests(HomeCase):
    TAG = 'v0.1.63'
    SHA = 'c' * 40

    def test_a_tag_with_its_sha_installs_the_tag_and_verifies_the_sha(self):
        run = FakeRun(installed=self.SHA)
        lines = []
        rc = upgrade._install(self.TAG, run, lines.append, sha=self.SHA)
        self.assertEqual(rc, 0)
        self.assertEqual(run.installs(),
                         [['pipx', 'install', '--force', f'git+https://github.com/o/r.git@{self.TAG}']])
        gh_calls = [c for c in run.calls if c[:1] == ['gh']]
        self.assertEqual(gh_calls, [['gh', 'run', 'list', '--repo', 'o/r', '--commit', self.SHA,
                                     '--limit', '20', '--json', 'conclusion']])
        self.assertIn('upgrade: installed', '\n'.join(lines))

    def test_the_same_call_with_no_sha_fails_verification(self):
        """PD3: an install of a tag with no ``sha`` can never verify — pinned so it cannot come
        back once the channel starts calling this with one."""
        run = FakeRun(installed=self.SHA)
        lines = []
        rc = upgrade._install(self.TAG, run, lines.append)
        self.assertEqual(rc, 1)
        self.assertIn('upgrade: FAILED', '\n'.join(lines))

    def test_a_sha_ref_with_no_sha_kwarg_is_todays_path(self):
        run = FakeRun(installed=self.SHA)
        lines = []
        rc = upgrade._install(self.SHA, run, lines.append)
        self.assertEqual(rc, 0)
        self.assertEqual(run.installs(),
                         [['pipx', 'install', '--force', f'git+https://github.com/o/r.git@{self.SHA}']])


if __name__ == '__main__':
    unittest.main()
