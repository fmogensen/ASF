"""asf.channels — F-0308: the two release channels, ``edge`` and ``stable``.

Task 1 of ``docs/plans/f-0308.md`` writes the resolver half only: ``ResolveTest``, over the
lines a real git remote prints for a real bare repo with real **annotated** tags (never a
hand-typed line: the trap this module exists to avoid is a filtered listing that silently hides
the peel — see the module's own docstring and PD9's note that every later Task adds its own
classes to this one file and runs it whole).

Task 2 adds the log half: ``LogTest``, over ``asf.state.store`` and a registered
``channels.json`` — one case per checkbox line of S-81207.

Task 3 adds the edge half: ``EdgeTest`` (``edge_candidate``, bound to ``upgrade.ci_verdict``'s
own answers — PD10, this module writes no verdict logic of its own) and ``PublishTest``
(``publish``, one fast-forward push through the guard — real git, real refusals, never a hand
built ``CompletedProcess``, the same discipline ``tests.test_gitpush`` holds its own fixture to)
— one case per checkbox line of S-81205."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import channels, cli, env, mutation_guard
from asf.state import registry, store


def _git(args, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE')}
    return subprocess.run(['git', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false'] + args,
                          cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def tagged_remote(tmp):
    """A real bare repo under ``tmp``, with real **annotated** version tags — the fixture the
    trap this Task exists to avoid needs: a stray lightweight tag would print no peel line at
    all, and a listing built from anything but real git could hide that (PD9's trap note).

    * ``v0.1.0`` — annotated, alone on its own commit.
    * ``v0.2.0``, ``v0.10.0`` and the stray ``v0.2.0-rc1`` — all annotated, all on one later
      commit, so the fixture also carries the "two version tags on one commit" and "a stray is
      ignored" cases without a second remote. ``refs/heads/releases/stable`` is pushed there.
    * one later commit with no tag at all, carrying ``refs/heads/releases/preview`` — a
      published head whose commit no version tag names.
    * ``refs/heads/releases/edge`` is never created: querying it is the "unpublished channel"
      case.

    Returns ``(remote, shas)`` — ``shas`` keyed ``'v0.1.0'``, ``'v0.2.0'`` (the commit carrying
    ``v0.2.0``/``v0.10.0``/the stray) and ``'untagged'``.
    """
    remote = os.path.join(tmp, 'remote.git')
    _git(['init', '-q', '-b', 'main', remote])

    def commit(msg):
        _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
             '-m', msg], remote)
        return _git(['rev-parse', 'HEAD'], remote)

    def tag(name):
        _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'tag', '-a', name, '-m', name], remote)

    c1 = commit('v0.1.0')
    tag('v0.1.0')
    c2 = commit('v0.2.0 and v0.10.0')
    tag('v0.2.0')
    tag('v0.10.0')
    tag('v0.2.0-rc1')
    _git(['update-ref', 'refs/heads/releases/stable', c2], remote)
    c3 = commit('untagged')
    _git(['update-ref', 'refs/heads/releases/preview', c3], remote)
    return remote, {'v0.1.0': c1, 'v0.2.0': c2, 'untagged': c3}


def _lines(remote, tmp):
    """The exact lines ``channels.resolve`` asks for, over ``remote`` — real git, every time
    (the trap: a pattern narrowed to an exact ref, or ``--refs`` added back, hides the peel)."""
    text = _git(['ls-remote', remote, 'refs/heads/' + channels.PREFIX + '*', 'refs/tags/v*'], tmp)
    return text.splitlines()


class ResolveTest(unittest.TestCase):
    """S-81204 — one case per checkbox line, over the lines real git prints for
    :func:`tagged_remote`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='channels_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote, self.shas = tagged_remote(self.tmp)
        self.lines = _lines(self.remote, self.tmp)

    def test_the_channel_head_resolves_to_the_tag_whose_peel_is_its_commit(self):
        tag, commit = channels.channel_tag(self.lines, 'stable')
        self.assertEqual((tag, commit), ('v0.10.0', self.shas['v0.2.0']))

    def test_resolve_asks_ls_remote_once_for_both_patterns(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.run(cmd, **kw)

        tag, commit = channels.resolve(self.remote, 'stable', run=fake_run)
        self.assertEqual((tag, commit), ('v0.10.0', self.shas['v0.2.0']))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ['git', 'ls-remote', self.remote,
                                    'refs/heads/' + channels.PREFIX + '*', 'refs/tags/v*'])

    def test_an_unpublished_channel_resolves_to_no_tag_and_no_commit(self):
        tag, commit = channels.channel_tag(self.lines, 'edge')
        self.assertIsNone(tag)
        self.assertIsNone(commit)
        # and the listing itself exits 0 rather than erroring
        r = subprocess.run(['git', 'ls-remote', self.remote,
                            'refs/heads/' + channels.PREFIX + '*', 'refs/tags/v*'],
                           cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)

    def test_a_head_at_a_commit_no_version_tag_names_resolves_to_that_commit_with_no_tag(self):
        tag, commit = channels.channel_tag(self.lines, 'preview')
        self.assertIsNone(tag)
        self.assertEqual(commit, self.shas['untagged'])

    def test_the_tag_object_sha_is_never_returned(self):
        tag, commit = channels.channel_tag(self.lines, 'stable')
        tag_object = _git(['rev-parse', 'v0.10.0'], self.remote)
        self.assertNotEqual(commit, tag_object)
        self.assertEqual(commit, self.shas['v0.2.0'])

    def test_a_stray_tag_that_is_not_v_major_minor_patch_is_ignored(self):
        self.assertFalse(cli.RELEASE_TAG.fullmatch('v0.2.0-rc1'))
        tag, _commit = channels.channel_tag(self.lines, 'stable')
        self.assertNotEqual(tag, 'v0.2.0-rc1')

    def test_two_version_tags_on_one_commit_resolve_to_the_higher_one_by_integer_tuple(self):
        # lexically 'v0.10.0' < 'v0.2.0' ('1' < '2'); by integer tuple (0, 10, 0) > (0, 2, 0)
        self.assertLess('v0.10.0', 'v0.2.0')
        tag, _commit = channels.channel_tag(self.lines, 'stable')
        self.assertEqual(tag, 'v0.10.0')

    def test_a_listing_that_could_not_be_read_at_all_resolves_to_no_tag_and_no_commit(self):
        def failing_run(cmd, **kw):
            raise OSError('git not found')

        self.assertEqual(channels.resolve(self.remote, 'stable', run=failing_run), (None, None))

        def nonzero_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr='fatal')

        self.assertEqual(channels.resolve(self.remote, 'stable', run=nonzero_run), (None, None))


def log(**rows):
    """A channel log at whatever ages a case needs — :func:`channels._empty_log` overlaid with
    ``rows`` (``log(edge_log=[...])``, ``log(stable={...})``), against a frozen ``now`` the case
    picks for itself."""
    base = channels._empty_log()
    base.update(rows)
    return base


class ChannelsStateHome(unittest.TestCase):
    """A fresh ``ASF_HOME`` per test, so the registered ``channels.json`` is read and written for
    real through :mod:`asf.state.store` — never faked, the same discipline ``test_state_store``
    holds its own fixtures to."""

    P = 'channels-test-product'

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='channels_log_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.addCleanup(self._restore_home)
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def _restore_home(self):
        env.ASF_HOME = self._home

    def target(self):
        return store.path(self.P, channels.LOG_NAME)


class LogTest(ChannelsStateHome):
    """S-81207 — one case per checkbox line: the log is the record of what was offered when."""

    def test_the_log_name_is_registered_with_no_ttl(self):
        spec = registry.spec(channels.LOG_NAME)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.owner, 'asf.channels')
        self.assertIsNone(spec.ttl_days)

    def test_an_absent_log_reads_as_no_channel_published_yet_and_the_first_run_logs_edge(self):
        self.assertEqual(channels.read_log(self.P), {'edge': None, 'stable': None, 'edge_log': []})
        ok, detail = channels.write_log(
            self.P, lambda l: channels.note_edge(l, 'v0.1.1', 'c1', 'T1'))
        self.assertTrue(ok)
        self.assertEqual(detail, '')
        got = channels.read_log(self.P)
        self.assertEqual(got['edge'], {'tag': 'v0.1.1', 'commit': 'c1', 'at': 'T1'})
        self.assertEqual(got['edge_log'], [{'tag': 'v0.1.1', 'commit': 'c1', 'at': 'T1'}])

    def test_a_publication_writes_the_tag_commit_and_time_and_prepends_one_edge_entry(self):
        before = log(edge_log=[{'tag': 'v0.1.1', 'commit': 'c1', 'at': 'T1'}])
        channels.note_edge(before, 'v0.1.2', 'c2', 'T2')
        self.assertEqual(before['edge'], {'tag': 'v0.1.2', 'commit': 'c2', 'at': 'T2'})
        self.assertEqual(before['edge_log'], [{'tag': 'v0.1.2', 'commit': 'c2', 'at': 'T2'},
                                               {'tag': 'v0.1.1', 'commit': 'c1', 'at': 'T1'}])

    def test_the_edge_log_keeps_at_most_log_keep_entries_newest_first(self):
        built = log()
        for i in range(5):
            channels.note_edge(built, f'v0.1.{i}', f'c{i}', f'T{i}', log_keep=3)
        self.assertEqual([e['tag'] for e in built['edge_log']], ['v0.1.4', 'v0.1.3', 'v0.1.2'])

    def test_a_corrupt_log_is_not_written_over_silently(self):
        os.makedirs(os.path.dirname(self.target()), exist_ok=True)
        with open(self.target(), 'w', encoding='utf-8') as f:
            f.write('not json')
        ok, detail = channels.write_log(
            self.P, lambda l: channels.note_edge(l, 'v0.1.1', 'c1', 'T1'))
        self.assertFalse(ok)
        self.assertIn(channels.LOG_NAME, detail)
        with open(self.target(), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'not json')  # never overwritten

    def test_a_stable_promotion_records_which_criteria_it_was_promoted_for(self):
        built = log()
        channels.note_stable(built, 'v0.1.1', 'c1', 'T1',
                             ['cadence', 'dwell', 'rehearsal', 'no_s1'])
        self.assertEqual(built['stable'], {'tag': 'v0.1.1', 'commit': 'c1', 'at': 'T1',
                                           'promoted_for': ['cadence', 'dwell', 'rehearsal', 'no_s1']})

    def test_the_dwell_is_measured_from_the_log_entry_time_never_the_tag_own_creation_date(self):
        built = log()
        with mock.patch('subprocess.run', side_effect=AssertionError('note_edge must not touch git')):
            channels.note_edge(built, 'v0.1.1', 'c1', 'the-logged-time')
        self.assertEqual(built['edge']['at'], 'the-logged-time')
        self.assertEqual(built['edge_log'][0]['at'], 'the-logged-time')


# --- Task 3: edge — the newest green tag, and one fast-forward through the guard -----------

#: a github-shaped url, so ``upgrade._gh_slug`` resolves it and ``ci_verdict`` asks ``gh`` —
#: never a real one: every answer below comes from :class:`FakeGh`
CI_URL = 'https://github.com/o/r.git'
#: the two landing checks every :class:`FakeGh` case answers for (the shape
#: ``tests.test_upgrade.CHECKS`` already uses)
CI_CHECKS = ['tests (3.12)', 'tests (3.13)']


class FakeGh:
    """``run`` for :func:`asf.upgrade.ci_verdict`, reached through :func:`asf.channels.edge_candidate`
    — ``gh api .../check-runs`` and ``.../status`` answered per commit, by one of the shapes
    S-81205 is proved against: ``'green'``, ``'red'``, ``'pending'`` (every check still running),
    ``'gh-failure'`` (the call itself fails — an unreadable ``gh``), and ``'no-run'`` (the
    check-runs list and the commit status are both empty — ``clear``, never read as green here,
    PD10). Any commit not named in ``states`` answers ``'green'``."""

    def __init__(self, states=None):
        self.states = dict(states or {})
        self.calls = []

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        if cmd[:2] != ['gh', 'api']:
            return mock.Mock(returncode=1, stdout='', stderr=f'unexpected call: {cmd}')
        m = re.search(r'/commits/([0-9a-fA-F]+)/', cmd[2])
        state = self.states.get(m.group(1) if m else None, 'green')
        if state == 'gh-failure':
            # never a rate-limit-shaped message (asf.gh_limit.is_rate_limited): that would latch
            # this whole test process against every later gh call, this suite's included
            return mock.Mock(returncode=1, stdout='', stderr='gh: authentication required')
        if 'check-runs' not in cmd[2]:
            return mock.Mock(returncode=0, stdout=json.dumps({'statuses': []}), stderr='')
        if state == 'no-run':
            runs = []
        elif state == 'pending':
            runs = [{'id': i + 1, 'name': n, 'status': 'in_progress', 'conclusion': None}
                    for i, n in enumerate(CI_CHECKS)]
        else:
            runs = [{'id': i + 1, 'name': n, 'status': 'completed',
                    'conclusion': 'success' if state == 'green' else 'failure'}
                    for i, n in enumerate(CI_CHECKS)]
        return mock.Mock(returncode=0, stdout=json.dumps({'check_runs': runs}), stderr='')


class EdgeTest(unittest.TestCase):
    """S-81205 — ``edge_candidate`` asks :func:`asf.upgrade.ci_verdict` one tag at a time, newest
    first, and takes the first green; every case here is a verdict shape ``ci_verdict`` already
    answers (PD10) — this module asserts the answers, it writes no verdict logic of its own."""

    P = env.Product('edge-test-product', {'main': 'main'})

    def test_the_newest_green_tag_is_the_candidate_and_older_green_tags_are_not(self):
        tags = [('v0.1.3', 'c3'), ('v0.1.2', 'c2'), ('v0.1.1', 'c1')]
        fake = FakeGh({'c3': 'green', 'c2': 'green', 'c1': 'green'})
        with mock.patch('asf.upgrade.landing_checks_for', return_value=CI_CHECKS):
            tag, commit, detail = channels.edge_candidate(self.P, CI_URL, tags, run=fake)
        self.assertEqual((tag, commit), ('v0.1.3', 'c3'))
        self.assertIn('v0.1.3', detail)
        self.assertIn('green', detail)
        # the scan stopped at the first green: the older green tags were never asked
        self.assertEqual(len(fake.calls), 1)

    def test_a_tag_whose_ci_is_red_is_skipped_and_the_next_older_one_is_examined(self):
        tags = [('v0.1.3', 'c3'), ('v0.1.2', 'c2'), ('v0.1.1', 'c1')]
        fake = FakeGh({'c3': 'red', 'c2': 'green', 'c1': 'green'})
        with mock.patch('asf.upgrade.landing_checks_for', return_value=CI_CHECKS):
            tag, commit, detail = channels.edge_candidate(self.P, CI_URL, tags, run=fake)
        self.assertEqual((tag, commit), ('v0.1.2', 'c2'))
        self.assertIn('v0.1.2', detail)
        self.assertIn('green', detail)
        # c3 was asked (and skipped, red) and c1 (older than the chosen c2) never was
        self.assertEqual(len(fake.calls), 2)

    def test_when_nothing_is_green_the_detail_names_every_verdict_examined(self):
        tags = [('v0.1.3', 'c3'), ('v0.1.2', 'c2')]
        fake = FakeGh({'c3': 'red', 'c2': 'red'})
        with mock.patch('asf.upgrade.landing_checks_for', return_value=CI_CHECKS):
            tag, commit, detail = channels.edge_candidate(self.P, CI_URL, tags, run=fake)
        self.assertIsNone(tag)
        self.assertIsNone(commit)
        self.assertIn('v0.1.3 red', detail)
        self.assertIn('v0.1.2 red', detail)

    def test_a_tag_with_no_run_at_all_is_not_a_candidate(self):
        tags = [('v0.1.1', 'c1')]
        fake = FakeGh({'c1': 'no-run'})
        with mock.patch('asf.upgrade.landing_checks_for', return_value=CI_CHECKS):
            tag, commit, detail = channels.edge_candidate(self.P, CI_URL, tags, run=fake)
        self.assertIsNone(tag)
        self.assertIsNone(commit)
        self.assertIn('v0.1.1 unknown', detail)
        self.assertIn('has no run', detail)

    def test_pending_checks_and_an_unreadable_gh_call_are_both_not_a_candidate(self):
        tags = [('v0.1.2', 'c2'), ('v0.1.1', 'c1')]
        fake = FakeGh({'c2': 'pending', 'c1': 'gh-failure'})
        with mock.patch('asf.upgrade.landing_checks_for', return_value=CI_CHECKS):
            tag, commit, detail = channels.edge_candidate(self.P, CI_URL, tags, run=fake)
        self.assertIsNone(tag)
        self.assertIsNone(commit)
        self.assertIn('v0.1.2 unknown', detail)
        self.assertIn('v0.1.1 unknown', detail)
        self.assertIn('could not read the check runs', detail)

    def test_a_repo_whose_product_names_no_landing_checks_advances_nothing_and_says_why(self):
        tags = [('v0.1.1', 'c1')]
        fake = FakeGh({'c1': 'green'})
        with mock.patch('asf.upgrade.landing_checks_for', return_value=[]):
            tag, commit, detail = channels.edge_candidate(self.P, CI_URL, tags, run=fake)
        self.assertIsNone(tag)
        self.assertIsNone(commit)
        self.assertIn('no product names landing_checks', detail)
        self.assertEqual(fake.calls, [])  # unknown before any gh call: nothing is named to ask

    def test_the_scan_never_examines_more_than_lookback_tags(self):
        tags = [(f'v0.1.{i}', f'c{i}') for i in range(15, 0, -1)]  # 15 tags, newest first
        fake = FakeGh({f'c{i}': 'red' for i in range(15, 0, -1)})
        with mock.patch('asf.upgrade.landing_checks_for', return_value=CI_CHECKS):
            tag, commit, detail = channels.edge_candidate(self.P, CI_URL, tags, run=fake)
        self.assertIsNone(tag)
        self.assertEqual(len(fake.calls), channels.DEFAULTS['lookback_tags'])
        self.assertNotIn('v0.1.1 ', detail)  # the oldest five tags were never reached

    def test_a_configured_limit_bounds_the_scan_before_a_later_green_tag(self):
        tags = [('v0.1.3', 'c3'), ('v0.1.2', 'c2'), ('v0.1.1', 'c1')]
        fake = FakeGh({'c3': 'red', 'c2': 'green', 'c1': 'green'})
        with mock.patch('asf.upgrade.landing_checks_for', return_value=CI_CHECKS):
            tag, commit, detail = channels.edge_candidate(self.P, CI_URL, tags, run=fake, limit=1)
        self.assertIsNone(tag)
        self.assertEqual(len(fake.calls), 1)


def _repo_fixture(tmp):
    """A bare remote plus a local clone with ``origin`` set to it and one commit — the shape
    :func:`channels.publish` pushes against, built the same way :class:`tests.test_gitpush.PushTest`
    builds its own, so ``PublishTest`` needs nothing that suite does not already prove."""
    bare, local = os.path.join(tmp, 'origin.git'), os.path.join(tmp, 'repo')
    _git(['init', '-q', '--bare', '-b', 'main', bare])
    _git(['clone', '-q', bare, local])
    _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'seed'],
        local)
    _git(['push', '-q', 'origin', 'HEAD:main'], local)
    return bare, local


def _ref_sha(repo, ref):
    """``ref``'s sha in ``repo``, or ``None`` when it does not exist — never raises."""
    r = subprocess.run(['git', 'rev-parse', '-q', '--verify', ref], cwd=repo,
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


class PublishTest(ChannelsStateHome):
    """S-81205 — ``publish`` is one fast-forward push through the guard: a guard refusal, a dry
    run and git's own non-fast-forward are each a detail, never an exception, and each leaves
    both the remote and the channel log exactly as they were (D1)."""

    def setUp(self):
        super().setUp()
        self.repo_tmp = tempfile.mkdtemp(prefix='channels_publish_')
        self.addCleanup(shutil.rmtree, self.repo_tmp, ignore_errors=True)
        self.bare, self.local = _repo_fixture(self.repo_tmp)
        self.c1 = _git(['rev-parse', 'HEAD'], self.local)
        self.c2 = self._commit('second')
        self.product = env.Product(self.P, {'repo_dir': self.local, 'main': 'main'})

    def _commit(self, msg):
        _git(['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty',
             '-m', msg], self.local)
        return _git(['rev-parse', 'HEAD'], self.local)

    def test_a_publish_is_one_fast_forward_push_through_the_guard(self):
        lines = []
        ok, detail = channels.publish(self.product, 'edge', self.c1, out=lines.append)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, '')
        self.assertEqual(_ref_sha(self.bare, 'refs/heads/releases/edge'), self.c1)

    def test_a_guard_refusal_pushes_nothing_and_leaves_the_log_unchanged(self):
        channels.write_log(self.P, lambda l: channels.note_edge(l, 'v0.1.0', self.c1, 'T0'))
        before = channels.read_log(self.P)
        protected = env.Product(self.P, {'repo_dir': self.local, 'main': 'main',
                                         'conventions': {'protected_refs': ['releases/*']}})
        ok, detail = channels.publish(protected, 'edge', self.c1)
        self.assertFalse(ok)
        self.assertIn('REF GUARD', detail)
        self.assertIsNone(_ref_sha(self.bare, 'refs/heads/releases/edge'))
        self.assertEqual(channels.read_log(self.P), before)

    def test_a_dry_run_publishes_nothing_and_says_what_it_would_have_pushed(self):
        with mutation_guard.active():
            ok, detail = channels.publish(self.product, 'edge', self.c1)
        self.assertFalse(ok)
        self.assertIn('would run', detail)
        self.assertIn('releases/edge', detail)
        self.assertIsNone(_ref_sha(self.bare, 'refs/heads/releases/edge'))

    def test_a_non_fast_forward_refuses_and_the_head_is_never_moved_backwards(self):
        channels.write_log(self.P, lambda l: channels.note_edge(l, 'v0.1.1', self.c2, 'T1'))
        before = channels.read_log(self.P)
        ok, _detail = channels.publish(self.product, 'edge', self.c2)
        self.assertTrue(ok, _detail)
        ok2, detail2 = channels.publish(self.product, 'edge', self.c1)  # c1 is c2's own ancestor
        self.assertFalse(ok2)
        self.assertEqual(_ref_sha(self.bare, 'refs/heads/releases/edge'), self.c2)  # unmoved
        self.assertEqual(channels.read_log(self.P), before)  # the refused push wrote nothing
