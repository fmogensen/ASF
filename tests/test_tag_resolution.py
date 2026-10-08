"""F-0303 — ``resolve_ref`` asks ``git ls-remote`` for the peel by name, so an annotated tag
resolves to the commit it points at and not to the tag object; and ``ci_verdict``'s Unknown
detail carries ``gh``'s own reason. Four Stories (S-81154..S-81157), one module: the real-git
fixture and the one fake allowed to answer ``ls-remote`` are shared by all of them. No network.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import env, installs, upgrade
from tests import gitfixture
from tests.test_install import FakeRun as InstallFakeRun, HomeCase
from tests.test_upgrade import CHECKS, FakeOps, FakeRun as MoveFakeRun, MoveCase, URL

#: the card's own annotated tag, used throughout — a real one in RealGitTagTest's temp repo, a
#: faked one everywhere else.
TAG = 'v0.1.254'
#: the commit the tag points at, under :class:`PeelingFake` — distinct from TAG_OBJ, the way a
#: real annotated tag's commit always differs from the tag object that names it.
COMMIT = '5' * 40
#: the tag object's own sha under :class:`PeelingFake` — what the defect resolved to.
TAG_OBJ = '7' * 40


def tagged_repo(tmp):
    """A real repo in ``tmp``, one empty commit, an annotated tag :data:`TAG` on it —
    ``(path, commit, tag_object)``. The two shas differ, which the first caller asserts before
    anything else: the fixture must not quietly stop testing what it is for."""
    path = os.path.join(tmp, 'origin')
    os.makedirs(path)

    def git(*args, check=True):
        return subprocess.run(['git', '-C', path, *args], check=check, capture_output=True,
                              text=True).stdout.strip()

    subprocess.run(['git', 'init', '-q', '-b', 'main', path], check=True)
    gitfixture.identity(path, 'sample', 'sample@example.com')
    git('commit', '-q', '--allow-empty', '-m', 'x')
    git('tag', '-a', TAG, '-m', 'x')
    commit = git('rev-parse', 'HEAD')
    tag_obj = git('rev-parse', TAG)
    return path, commit, tag_obj


class PeelingFake:
    """Wraps an existing fake ``run`` (:class:`tests.test_upgrade.FakeRun` or
    :class:`tests.test_install.FakeRun`) and answers ``git ls-remote`` the way real git answers
    it (measured in ``docs/specs/f-0303.md`` P3, P5): the tag object's own line always, the peel
    line only when ``<ref>^{}`` is among the patterns asked for. Every other command falls
    through to the wrapped fake, which still answers ``pipx``, ``gh``, ``pgrep`` and ``ps`` — so
    a fake that cannot tell the two commands apart can never make this one pass."""

    def __init__(self, inner, tag=TAG, commit=COMMIT, tag_obj=TAG_OBJ):
        self.inner, self.tag, self.commit, self.tag_obj = inner, tag, commit, tag_obj

    def __call__(self, cmd, **kw):
        if cmd[:2] == ['git', 'ls-remote']:
            self.inner.calls.append(list(cmd))
            patterns = cmd[3:]
            lines = [f'{self.tag_obj}\trefs/tags/{self.tag}']
            if f'{self.tag}{upgrade.PEEL}' in patterns:
                lines.append(f'{self.commit}\trefs/tags/{self.tag}{upgrade.PEEL}')
            return mock.Mock(stderr='', returncode=0, stdout='\n'.join(lines) + '\n')
        return self.inner(cmd, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class RealGitTagTest(unittest.TestCase):
    """S-81154 — the test that could not have passed on the assumption: real git, over a real
    annotated tag, with real ``subprocess.run`` (D7). No fake in this suite can tell
    ``ls-remote <ref>`` from ``ls-remote <ref> <ref>^{}``, so only real git can prove this."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='tag_resolution_test_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_an_annotated_tag_resolves_to_its_commit_not_the_tag_object(self):
        path, commit, tag_obj = tagged_repo(self.tmp)
        self.assertNotEqual(commit, tag_obj)
        self.assertEqual(upgrade.resolve_ref(path, TAG), commit)

    def test_the_peel_is_asked_for_by_name(self):
        path, commit, _tag_obj = tagged_repo(self.tmp)
        calls = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            return subprocess.run(cmd, **kw)

        upgrade.resolve_ref(path, TAG, run=run)
        self.assertEqual(calls, [['git', 'ls-remote', path, TAG, f'{TAG}{upgrade.PEEL}']])

        # a ref that already ends in the peel suffix is asked for once, not twice — appending it
        # again would match nothing, which would silently resolve to the unpeeled line (D3)
        calls.clear()
        peeled = f'{TAG}{upgrade.PEEL}'
        self.assertEqual(upgrade.resolve_ref(path, peeled, run=run), commit)
        self.assertEqual(calls, [['git', 'ls-remote', path, peeled]])

    def test_a_lightweight_tag_a_branch_and_a_missing_ref_are_unchanged(self):
        path, commit, _tag_obj = tagged_repo(self.tmp)
        subprocess.run(['git', '-C', path, 'tag', 'lightweight'], check=True)
        self.assertEqual(upgrade.resolve_ref(path, 'lightweight'), commit)
        self.assertEqual(upgrade.resolve_ref(path, 'main'), commit)
        self.assertEqual(upgrade.resolve_ref(path, 'HEAD'), commit)
        self.assertIsNone(upgrade.resolve_ref(path, 'v9.9.9'))

    def test_resolve_target_through_the_remote_names_the_commit(self):
        path, commit, _tag_obj = tagged_repo(self.tmp)
        with mock.patch('asf.drift.factory_root', return_value=None):
            self.assertEqual(upgrade.resolve_target(TAG, None, path), commit)

    def test_resolve_target_through_a_checkout_that_has_the_tag_names_the_commit(self):
        path, commit, _tag_obj = tagged_repo(self.tmp)

        def no_remote_read(cmd, **kw):
            if cmd[:2] == ['git', 'ls-remote']:
                raise AssertionError(f'unexpected remote read: {cmd}')
            return subprocess.run(cmd, **kw)

        with mock.patch('asf.drift.factory_root', return_value=path):
            got = upgrade.resolve_target(TAG, None, 'https://example.invalid/o/r.git',
                                         run=no_remote_read)
        self.assertEqual(got, commit)


class MoveAtATagTest(MoveCase):
    """S-81155 — the move's CI guard reads the commit, so ``--to`` a tag no longer 422s. P15: no
    existing move test reaches a tag, because the move fake refuses (rc 1) any command it does
    not know, ``git ls-remote`` among them."""

    def test_the_guard_reads_the_commit_the_tag_points_at(self):
        run = PeelingFake(MoveFakeRun(self.venvs))
        rc, out = self.move(run, to=TAG)
        self.assertEqual(rc, 0, out)
        gh_calls = [c for c in run.calls if c[:2] == ['gh', 'api']]
        self.assertTrue(gh_calls, out)
        self.assertEqual(gh_calls[0][2], f'repos/o/r/commits/{COMMIT}/check-runs?per_page=100')
        self.assertNotIn(TAG_OBJ, gh_calls[0][2])

    def test_the_tag_is_resolved_before_the_record_is_written(self):
        run = PeelingFake(MoveFakeRun(self.venvs))
        rc, out = self.move(run, to=TAG)
        self.assertEqual(rc, 0, out)
        self.assertEqual(installs.read('alpha').sha, COMMIT)


class SharedInstallAtATagTest(HomeCase):
    """S-81156 — the shared ``asf upgrade --to`` a tag stops reporting FAILED on a good install
    (the post-install comparison is commit against commit once ``ref`` is), and the pending
    marker it writes for the other products names the commit, which is the sha the post-install
    build can match."""

    def setUp(self):
        super().setUp()
        for name in ('factory', 'other'):  # two unpinned products: both run the shared install
            self.write(env.product_path(name), 'backlog_dir: /nonexistent\n')

    def test_the_install_at_an_annotated_tag_succeeds(self):
        parked = PeelingFake(InstallFakeRun(ticks='4242\n'))
        out = []
        rc = upgrade.install(TAG, run=parked, out=out.append, owner='factory')
        self.assertEqual(rc, upgrade.DEFERRED, out)
        self.assertEqual(upgrade.read_pending('other')['sha'], COMMIT)

        run = PeelingFake(InstallFakeRun(installed=COMMIT))
        out = []
        rc = upgrade.install(TAG, run=run, out=out.append)
        self.assertEqual(rc, 0, out)
        text = '\n'.join(out)
        self.assertIn('upgrade: installed', text)
        self.assertNotIn('FAILED', text)
        self.assertEqual([c for c in run.calls if c[:2] == ['pipx', 'install']],
                         [['pipx', 'install', '--force', f'git+{URL}@{TAG}']])


class UnknownNamesItsReasonTest(MoveCase):
    """S-81157 — a CI-unknown deferral names why ``gh`` could not answer: the detail carries
    ``gh``'s own reason (already on the ``Result``), never a fixed string."""

    def test_the_detail_carries_ghs_own_reason(self):
        sha = 'c' * 40
        stderr = f'gh: No commit found for SHA: {sha} (HTTP 422)\n'

        def failing_gh(cmd, **kw):
            return mock.Mock(returncode=1, stdout='', stderr=stderr)

        verdict, detail = upgrade.ci_verdict(URL, sha, run=failing_gh, checks=CHECKS)
        self.assertEqual(verdict, 'unknown')
        self.assertIn('gh could not read the check runs', detail)  # P16's assertion still holds
        self.assertIn('HTTP 422', detail)
        self.assertIn(sha, detail)

        def not_check_runs(cmd, **kw):
            return mock.Mock(returncode=0, stdout='[]', stderr='')

        verdict2, detail2 = upgrade.ci_verdict(URL, sha, run=not_check_runs, checks=CHECKS)
        self.assertEqual(verdict2, 'unknown')
        self.assertNotIn('()', detail2)  # never an empty pair of parentheses
        self.assertIn('the answer is not a check-runs object', detail2)

        def move_run(cmd, **kw):
            if cmd[:2] == ['pipx', 'list']:
                return mock.Mock(returncode=0, stdout=json.dumps({'venvs': {'asf-factory': {
                    'metadata': {'main_package': {'package_or_url': f'git+{URL}@1234567'}}}}}))
            if cmd[:2] == ['gh', 'api']:
                return mock.Mock(returncode=1, stdout='', stderr=stderr)
            return mock.Mock(returncode=1, stdout='')

        rc, out = self.move(move_run, FakeOps(), to=sha)
        self.assertEqual(rc, upgrade.MOVE_DEFERRED, out)
        self.assertIn('gh could not read the check runs', out[-1])
        self.assertIn('HTTP 422', out[-1])
        self.assertIn(sha, out[-1])
