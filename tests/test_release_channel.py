import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf import env, upgrade
from asf.harvest import lane


class ProductUpgradePolicyTests(unittest.TestCase):
    """F-0112/T-0431: the ``conventions.flags.upgrade`` switch's three words, its default, and
    the raw word the doctor must quote back for a typo."""

    def _product(self, declared):
        data = {'repo_slug': 'a/b'}
        if declared is not None:
            data['conventions'] = {'flags': {'upgrade': declared}}
        return env.Product('p', data)

    def test_the_three_words_round_trip(self):
        for word in env.UPGRADE_POLICIES:
            self.assertEqual(self._product(word).upgrade, word)

    def test_case_and_whitespace_are_folded(self):
        for declared in ('AUTO', ' auto ', 'Auto'):
            self.assertEqual(self._product(declared).upgrade, 'auto')

    def test_a_typo_reads_as_notify_and_is_quoted_back_raw(self):
        product = self._product('atuo')
        self.assertEqual(product.upgrade, env.UPGRADE_DEFAULT)
        self.assertEqual(product.upgrade_declared, 'atuo')

    def test_an_absent_key_reads_notify_and_declares_none(self):
        product = self._product(None)
        self.assertEqual(product.upgrade, env.UPGRADE_DEFAULT)
        self.assertIsNone(product.upgrade_declared)


class VersionComparisonTests(unittest.TestCase):
    """D5/D6: ``version_tuple`` as a plain tuple (never a string compare), ``+N`` counting as
    ahead of the bare tag, and an unreadable side never read as newer."""

    def test_older_and_newer_tags(self):
        self.assertTrue(upgrade.newer('v0.1.62', 'v0.1.9'))
        self.assertFalse(upgrade.newer('v0.1.9', 'v0.1.62'))

    def test_newer_than_both_a_tag_and_its_dev_form(self):
        self.assertTrue(upgrade.newer('v0.1.63', 'v0.1.62'))
        self.assertTrue(upgrade.newer('v0.1.63', 'v0.1.62+7'))

    def test_commits_past_a_tag_count_as_ahead_of_it(self):
        self.assertFalse(upgrade.newer('v0.1.62', 'v0.1.62+7'))
        self.assertTrue(upgrade.newer('v0.1.62+7', 'v0.1.62'))

    def test_an_unreadable_side_is_never_newer(self):
        self.assertFalse(upgrade.newer('v0.1.62', None))
        self.assertFalse(upgrade.newer(None, 'v0.1.62'))
        self.assertFalse(upgrade.newer('main', 'v0.1.62'))


class InstalledReleaseTests(unittest.TestCase):
    """``installed_release`` prepends the ``v`` :func:`asf.cli._release` leaves off, so its tag
    is what :func:`version_tuple`/:func:`newer` actually parse (``RELEASE_RE`` requires it)."""

    def test_a_bare_release_is_returned_v_prefixed_and_sha_matched(self):
        from asf import cli, drift
        with mock.patch.object(cli, '_release', return_value='0.1.62'), \
                mock.patch.object(drift, 'installed_commit', return_value='a' * 40):
            tag, sha = upgrade.installed_release()
        self.assertEqual(tag, 'v0.1.62')
        self.assertTrue(upgrade.RELEASE_RE.match(tag))
        self.assertEqual(sha, 'a' * 40)

    def test_no_release_at_all_is_none_not_a_bare_v(self):
        from asf import cli, drift
        with mock.patch.object(cli, '_release', return_value=None), \
                mock.patch.object(drift, 'installed_commit', return_value=None):
            tag, sha = upgrade.installed_release()
        self.assertIsNone(tag)
        self.assertIsNone(sha)


class _HomeCase(unittest.TestCase):
    """A temp ``ASF_HOME`` so the release cache and a product's lane registry never touch the
    operator's real one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='release_channel_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)


class LatestReleaseIsReadOnceAnHourTests(_HomeCase):
    """D4/D12: one cache, keyed by url, shared by every product and by ``asf status`` — the hour
    is the remote's, not each caller's."""

    def _listing(self, tag, sha, peeled=None):
        """A ``git ls-remote --tags <url> 'v*'`` line for ``tag``, with its peeled ``^{}`` line
        when ``peeled`` (an annotated tag) is given — a lightweight tag has none."""
        lines = [f'{sha}\trefs/tags/{tag}']
        if peeled:
            lines.append(f'{peeled}\trefs/tags/{tag}^{{}}')
        return '\n'.join(lines) + '\n'

    def _run(self, text, calls):
        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=text, stderr='')
        return run

    def test_two_products_three_calls_inside_the_hour_run_ls_remote_once(self):
        calls = []
        run = self._run(self._listing('v0.1.62', 'a' * 40, 'b' * 40), calls)
        now = 1_000_000.0
        self.assertEqual(upgrade.latest_release('url', now=now, run=run), ('v0.1.62', 'b' * 40))
        self.assertEqual(upgrade.latest_release('url', now=now + 10, run=run),
                         ('v0.1.62', 'b' * 40))
        self.assertEqual(upgrade.latest_release('url', now=now + 20, run=run),
                         ('v0.1.62', 'b' * 40))
        self.assertEqual(len(calls), 1)

    def test_a_call_past_the_hour_reads_again(self):
        calls = []
        run = self._run(self._listing('v0.1.62', 'a' * 40, 'b' * 40), calls)
        now = 1_000_000.0
        upgrade.latest_release('url', now=now, run=run)
        upgrade.latest_release('url', now=now + upgrade.RELEASE_POLL_S + 1, run=run)
        self.assertEqual(len(calls), 2)

    def test_a_failed_read_keeps_the_previous_answer_and_stamps_at(self):
        now = 1_000_000.0
        ok = self._run(self._listing('v0.1.62', 'a' * 40, 'b' * 40), [])
        self.assertEqual(upgrade.latest_release('url', now=now, run=ok), ('v0.1.62', 'b' * 40))

        def failing(cmd, **kw):
            raise OSError('no network')

        self.assertEqual(
            upgrade.latest_release('url', now=now + upgrade.RELEASE_POLL_S + 1, run=failing),
            ('v0.1.62', 'b' * 40))
        # the failed attempt still stamped `at`: the very next call, within the hour of *that*
        # attempt, reads nothing from the remote either
        calls = []
        again = self._run(self._listing('v0.1.63', 'c' * 40, 'd' * 40), calls)
        self.assertEqual(
            upgrade.latest_release('url', now=now + upgrade.RELEASE_POLL_S + 2, run=again),
            ('v0.1.62', 'b' * 40))
        self.assertEqual(calls, [])

    def test_a_first_ever_failed_read_is_none_none(self):
        def failing(cmd, **kw):
            raise OSError('no network')
        self.assertEqual(upgrade.latest_release('url', now=1_000_000.0, run=failing),
                         (None, None))

    def test_the_peeled_line_not_the_tag_objects_is_the_sha_returned(self):
        calls = []
        run = self._run(self._listing('v0.1.62', 'a' * 40, 'b' * 40), calls)
        tag, sha = upgrade.latest_release('url', now=1_000_000.0, run=run)
        self.assertEqual(tag, 'v0.1.62')
        self.assertEqual(sha, 'b' * 40)   # the peeled commit, never the tag object's own sha


class MidLandingTests(_HomeCase):
    """D7/PD14: ``occupancy(...)['landing']`` is what "mid-landing" means; an unreadable ledger
    holds the install — it returns ``[]``, the same shape as "nothing is landing", but the
    install never clears on it."""

    def _write(self, product, *lines):
        path = os.path.join(env.state_dir(product), 'sessions.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for line in lines:
                f.write(json.dumps(line) + '\n')

    def _landing_row(self, item, branch):
        return {'job': f'coder-{item.lower()}', 'item': item, 'branch': branch, 'kind': 'coder',
                'pid': None, 'started': '2026-09-21T00:00:00Z', 'ended': '2026-09-21T00:05:00Z',
                'end_reason': 'finished',
                'lane': {'state': lane.GATE, 'item': item, 'head': 'a' * 40, 'pr': None,
                         'at': '2027-01-15T08:00:00Z', 'reason': ''}}

    def test_a_pushed_to_land_row_is_returned(self):
        self._write('p', self._landing_row('T-0001', 'worker/T-0001'))
        self.assertEqual(upgrade.mid_landing('p'), [('T-0001', 'worker/T-0001')])

    def test_an_empty_registry_is_empty(self):
        self._write('p')
        self.assertEqual(upgrade.mid_landing('p'), [])

    def test_an_unreadable_registry_is_empty_too(self):
        path = os.path.join(env.state_dir('p'), 'sessions.jsonl')
        os.makedirs(path)   # a directory where a file is expected: unreadable, never raises out
        self.assertEqual(upgrade.mid_landing('p'), [])


class TagInstallVerifiesByShaTests(unittest.TestCase):
    """PD3/PD5: the install's CI guard and its post-install verification must ask about the
    commit a tag names, never the tag string itself — pinned here so the regression PD3 found
    (every release-channel install failing verification after pipx had already replaced the
    package) cannot come back."""

    SHA = 'a1b2c3d4e5' + '0' * 30

    def _install(self, ref, sha, installed=None, ci_state=('clear', '')):
        out = []

        def run(cmd, **kw):
            return mock.Mock(returncode=0)

        with mock.patch.object(upgrade, 'repo_url',
                               return_value='https://example.invalid/x.git'), \
                mock.patch.object(upgrade, 'ci_state', return_value=ci_state) as m_ci, \
                mock.patch.object(upgrade, 'new_install_commit',
                                  return_value=installed if installed is not None else sha), \
                mock.patch.object(upgrade, 'reload_clocks', return_value=[]):
            rc = upgrade._install(ref, run, out.append, sha=sha)
        return rc, out, m_ci

    def test_a_tag_install_asks_ci_about_the_sha_and_verifies_against_it(self):
        rc, out, m_ci = self._install('v0.1.63', self.SHA)
        self.assertEqual(rc, 0)
        self.assertEqual(m_ci.call_args.args[1], self.SHA)
        self.assertTrue(any('git+https://example.invalid/x.git@v0.1.63' in line for line in out))
        self.assertTrue(any('installed' in line for line in out))

    def test_the_same_call_with_no_sha_fails_verification(self):
        rc, out, _m_ci = self._install('v0.1.63', None, installed=self.SHA)
        self.assertEqual(rc, 1)
        self.assertTrue(any('FAILED' in line for line in out))

    def test_a_bare_sha_with_no_sha_kwarg_is_todays_path(self):
        rc, out, m_ci = self._install(self.SHA, None, installed=self.SHA)
        self.assertEqual(rc, 0)
        self.assertEqual(m_ci.call_args.args[1], self.SHA)


class ReleaseReportPolicyTests(_HomeCase):
    """T-0433/S-69307: the factory line names current/BEHIND/release unreadable on every policy
    — `off` included — a tag no newer than the install is `'none'`, and every exception is
    caught rather than stopping the tick."""

    def _product(self, policy='notify'):
        return env.Product('p', {'repo_slug': 'a/b', 'conventions': {'flags': {'upgrade': policy}}})

    def _run(self, policy, tag, old, sha='a' * 40, old_sha='b' * 40):
        ctx = types.SimpleNamespace(product=self._product(policy), upgrade_line=None)
        out = []
        with mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=(tag, sha)), \
                mock.patch.object(upgrade, 'installed_release', return_value=(old, old_sha)):
            result = upgrade.release_report(ctx, out=out.append)
        return result, out, ctx

    def test_an_off_policy_still_prints_the_factory_line(self):
        result, out, _ctx = self._run('off', 'v0.1.63', 'v0.1.62')
        self.assertEqual(result, 'none')
        self.assertIn('factory: asf v0.1.62 @ bbbbbbb · release v0.1.63 · BEHIND', out)
        self.assertFalse(any('UPGRADE AVAILABLE' in ln for ln in out), out)

    def test_the_factory_line_names_current_behind_or_unreadable(self):
        _, out, _ = self._run('off', 'v0.1.62', 'v0.1.62')
        self.assertIn('factory: asf v0.1.62 @ bbbbbbb · release v0.1.62 · current', out)

        _, out, _ = self._run('off', 'v0.1.63', 'v0.1.62')
        self.assertIn('factory: asf v0.1.62 @ bbbbbbb · release v0.1.63 · BEHIND', out)

        _, out, _ = self._run('off', 'v0.1.63', None, old_sha=None)
        self.assertIn('factory: asf unknown @ unknown · release v0.1.63 · release unreadable', out)

    def test_a_tag_no_newer_than_installed_is_none(self):
        result, out, ctx = self._run('notify', 'v0.1.62', 'v0.1.62')
        self.assertEqual(result, 'none')
        self.assertFalse(any('UPGRADE AVAILABLE' in ln for ln in out), out)
        self.assertIsNone(ctx.upgrade_line)

    def test_every_exception_is_caught_and_reported(self):
        ctx = types.SimpleNamespace(product=self._product('notify'), upgrade_line=None)
        out = []
        with mock.patch.object(upgrade, 'repo_url', side_effect=ValueError('boom')):
            result = upgrade.release_report(ctx, out=out.append)
        self.assertEqual(result, 'none')
        self.assertEqual(out, ['factory: release check failed (boom)'])


class NotifiesOncePerTagTests(_HomeCase):
    """T-0433/S-69307: the channel state round-trips; a fresh tag notifies once then falls
    silent; a newer tag notifies again."""

    def _product(self, policy='notify'):
        return env.Product('p', {'repo_slug': 'a/b', 'conventions': {'flags': {'upgrade': policy}}})

    def _ctx(self, policy='notify'):
        return types.SimpleNamespace(product=self._product(policy), upgrade_line=None)

    def _run(self, ctx, tag, old):
        out = []
        with mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=(tag, 'a' * 40)), \
                mock.patch.object(upgrade, 'installed_release', return_value=(old, 'b' * 40)):
            result = upgrade.release_report(ctx, out=out.append)
        return result, out

    def test_the_state_file_round_trips(self):
        product = self._product()
        self.assertEqual(upgrade._read_release_state(product), {})
        upgrade._write_release_state(product, {'notified': 'v0.1.63'})
        self.assertEqual(upgrade._read_release_state(product), {'notified': 'v0.1.63'})

    def test_a_fresh_tag_notifies_once_then_falls_silent(self):
        ctx = self._ctx()
        result, out = self._run(ctx, 'v0.1.63', 'v0.1.62')
        self.assertEqual(result, 'notified')
        self.assertIn('UPGRADE AVAILABLE v0.1.62 → v0.1.63', out)
        self.assertEqual(ctx.upgrade_line, 'UPGRADE AVAILABLE v0.1.62 → v0.1.63')

        ctx2 = self._ctx()
        result2, out2 = self._run(ctx2, 'v0.1.63', 'v0.1.62')
        self.assertEqual(result2, 'none')
        self.assertFalse(any('UPGRADE AVAILABLE' in ln for ln in out2), out2)
        self.assertIsNone(ctx2.upgrade_line)

    def test_a_newer_tag_notifies_again(self):
        self._run(self._ctx(), 'v0.1.63', 'v0.1.62')
        ctx2 = self._ctx()
        result, out = self._run(ctx2, 'v0.1.64', 'v0.1.62')
        self.assertEqual(result, 'notified')
        self.assertIn('UPGRADE AVAILABLE v0.1.62 → v0.1.64', out)


class TheTwoChannelsAreExclusiveTests(_HomeCase):
    """D3: the fork in `asf.tick.tick._run_steps` is chosen by `drift.is_factory_source`, never
    by a product's own declaration — and `auto` still falls through to `notify` until Task 4
    replaces it (PD15)."""

    def _product(self, repo_dir=None, policy=None):
        data = {'repo_slug': 'a/b', 'ci': {'provider': 'none'}}
        if repo_dir is not None:
            data['repo_dir'] = repo_dir
        if policy is not None:
            data['conventions'] = {'flags': {'upgrade': policy}}
        return env.Product('p', data)

    def _repo(self, package_name):
        repo = os.path.join(self.tmp, f'repo-{package_name}')
        os.makedirs(repo, exist_ok=True)
        with open(os.path.join(repo, 'pyproject.toml'), 'w', encoding='utf-8') as f:
            f.write(f'[project]\nname = "{package_name}"\n')
        return repo

    def _run(self, product):
        from asf.tick import tick as tick_mod
        ctx = tick_mod.Context(product)
        with mock.patch('asf.drift.report') as dr, \
                mock.patch.object(upgrade, 'release_report', return_value='none') as rr, \
                mock.patch('asf.tick.summary.run'):
            tick_mod._run_steps(mock.Mock(), product, ctx, [], None)
        return dr, rr

    def test_the_fork_is_chosen_by_is_factory_source(self):
        dr, rr = self._run(self._product(repo_dir=self._repo('asf-factory')))
        dr.assert_called_once()
        rr.assert_not_called()

        dr, rr = self._run(self._product(repo_dir=self._repo('customer')))
        dr.assert_not_called()
        rr.assert_called_once()

        # `is_factory_source('')` reads a relative `pyproject.toml` off the cwd (`os.path.join('',
        # 'pyproject.toml')`) — a bare directory with none, so this product with no `repo_dir` at
        # all is unambiguously not the factory's own source.
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            dr, rr = self._run(self._product(repo_dir=None))
        finally:
            os.chdir(cwd)
        dr.assert_not_called()
        rr.assert_called_once()

    def test_auto_falls_through_to_notify_for_now(self):
        product = self._product(repo_dir=self._repo('customer'), policy='auto')
        out = []
        ctx = types.SimpleNamespace(product=product, upgrade_line=None)
        with mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=('v0.1.63', 'a' * 40)), \
                mock.patch.object(upgrade, 'installed_release', return_value=('v0.1.62', 'b' * 40)):
            result = upgrade.release_report(ctx, out=out.append)
        self.assertEqual(result, 'notified')
        self.assertIn('UPGRADE AVAILABLE v0.1.62 → v0.1.63', out)


if __name__ == '__main__':
    unittest.main()
