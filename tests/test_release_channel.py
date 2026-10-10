import contextlib
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from asf import env, upgrade
from asf.harvest import lane
from asf.record import frontmatter
from asf.record.core import canonicalize, load_items
from asf.tick import file_bugs as file_bugs_mod
from asf.tick import tick


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


class NotifiesOncePerTagTests(_HomeCase):
    """D3/step 4: ``notify`` says ``UPGRADE AVAILABLE`` once per tag, in the log and on
    ``ctx.upgrade_line``; ``off`` never says it; a ``release_report`` whose every read raises
    still returns and the tick carries on."""

    def _product(self, policy='notify'):
        return env.Product('p', {'repo_slug': 'a/b', 'conventions': {'flags': {'upgrade': policy}}})

    def _ctx(self, policy='notify'):
        return types.SimpleNamespace(product=self._product(policy), upgrade_line=None)

    def _run(self, ctx, tag='v0.1.63', old='v0.1.62', old_sha='a' * 40):
        lines = []
        with mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=(tag, 'c' * 40)), \
                mock.patch.object(upgrade, 'installed_release', return_value=(old, old_sha)):
            result = upgrade.release_report(ctx, out=lines.append)
        return result, lines

    def test_the_first_tick_at_a_new_tag_notifies(self):
        ctx = self._ctx()
        result, lines = self._run(ctx)
        self.assertEqual(result, 'notified')
        self.assertIn('UPGRADE AVAILABLE v0.1.62 → v0.1.63', lines)
        self.assertEqual(ctx.upgrade_line, 'UPGRADE AVAILABLE v0.1.62 → v0.1.63')

    def test_the_second_tick_at_the_same_tag_is_silent(self):
        self._run(self._ctx())
        result, lines = self._run(self._ctx())
        self.assertEqual(result, 'none')
        self.assertFalse(any('UPGRADE AVAILABLE' in ln for ln in lines), lines)
        self.assertTrue(any(ln.startswith('factory:') for ln in lines), lines)

    def test_a_newer_tag_notifies_again(self):
        self._run(self._ctx())
        result, lines = self._run(self._ctx(), tag='v0.1.64')
        self.assertEqual(result, 'notified')
        self.assertIn('UPGRADE AVAILABLE v0.1.62 → v0.1.64', lines)

    def test_off_never_notifies(self):
        result, lines = self._run(self._ctx('off'))
        self.assertEqual(result, 'none')
        self.assertFalse(any('UPGRADE AVAILABLE' in ln for ln in lines), lines)
        self.assertTrue(any(ln.startswith('factory:') for ln in lines), lines)

    def test_a_release_report_whose_every_read_raises_still_returns(self):
        ctx = self._ctx()
        lines = []
        with mock.patch.object(upgrade, 'repo_url', side_effect=RuntimeError('boom')):
            result = upgrade.release_report(ctx, out=lines.append)
        self.assertEqual(result, 'none')
        self.assertTrue(any('release check failed' in ln for ln in lines), lines)


class TheTwoChannelsAreExclusiveTests(unittest.TestCase):
    """D3: which channel a product's tick takes is chosen by structure — whether its ``repo_dir``
    holds the factory's own source — never by a flag a product file could get wrong."""

    def _run(self, repo_dir):
        product = env.Product('p', {'repo_dir': repo_dir, 'main': 'main',
                                     'ci': {'provider': 'none'}})
        ctx = tick.Context(product)
        with mock.patch('asf.drift.report') as m_drift, \
                mock.patch('asf.upgrade.release_report', return_value='none') as m_release, \
                mock.patch('asf.tick.summary.run'):
            tick._run_steps(mock.Mock(), product, ctx, [], None)
        return m_drift, m_release

    def test_factory_source_takes_drift_never_release(self):
        tmp = tempfile.mkdtemp(prefix='exclusive_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with open(os.path.join(tmp, 'pyproject.toml'), 'w', encoding='utf-8') as f:
            f.write('[project]\nname = "asf-factory"\n')
        m_drift, m_release = self._run(tmp)
        m_drift.assert_called_once()
        m_release.assert_not_called()

    def test_a_customer_repo_takes_release_never_drift(self):
        tmp = tempfile.mkdtemp(prefix='exclusive_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        m_drift, m_release = self._run(tmp)
        m_drift.assert_not_called()
        m_release.assert_called_once()

    def test_no_repo_dir_at_all_takes_the_release_channel(self):
        # `is_factory_source('')` opens `pyproject.toml` relative to the cwd: run from outside a
        # factory checkout — as a real tick's would be — '' is not factory source either.
        tmp = tempfile.mkdtemp(prefix='exclusive_test_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            m_drift, m_release = self._run(None)
        finally:
            os.chdir(cwd)
        m_drift.assert_not_called()
        m_release.assert_called_once()


class AutoInstallsTheNewestTagTests(_HomeCase):
    """D7/D8/D9: ``auto`` reinstalls at the newest tag (unless a session is mid-landing),
    re-renders the clocks and runs the doctor through the new binary — never in-process — and
    logs both lines (PD9)."""

    SHA = 'b' * 40
    OLD_SHA = 'a' * 40

    def _product(self):
        return env.Product('p', {'repo_slug': 'a/b', 'conventions': {'flags': {'upgrade': 'auto'}}})

    def _ctx(self):
        return types.SimpleNamespace(product=self._product(), upgrade_line=None)

    def _run(self, doctor_rc=0, doctor_out='== DOCTOR p\nupgrade  ok    fine\n'):
        calls = []
        venv = os.path.join(self.tmp, 'venvs')
        binary = os.path.join(venv, upgrade.PACKAGE_NAME, 'bin', 'asf')

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ['pipx', 'environment']:
                return mock.Mock(returncode=0, stdout=venv + '\n', stderr='')
            if cmd[0] == binary and cmd[1] == 'doctor':
                return mock.Mock(returncode=doctor_rc, stdout=doctor_out, stderr='')
            return mock.Mock(returncode=0, stdout='', stderr='')

        ctx = self._ctx()
        with mock.patch.object(upgrade, 'repo_url', return_value='https://example.invalid/x.git'), \
                mock.patch.object(upgrade, 'latest_release', return_value=('v0.1.63', self.SHA)), \
                mock.patch.object(upgrade, 'installed_release', return_value=('v0.1.62', self.OLD_SHA)), \
                mock.patch.object(upgrade, 'mid_landing', return_value=[]), \
                mock.patch.object(upgrade, 'other_ticks', return_value=[]), \
                mock.patch.object(upgrade, 'ci_state', return_value=('clear', '')), \
                mock.patch.object(upgrade, 'new_install_commit', return_value=self.SHA), \
                mock.patch.object(upgrade, 'reload_clocks', return_value=[]):
            lines = []
            result = upgrade.release_report(ctx, out=lines.append, run=run)
        return result, lines, calls, binary

    def test_installs_renders_clocks_and_runs_doctor_in_order_through_the_new_binary(self):
        result, lines, calls, binary = self._run()
        self.assertEqual(result, 'installed')
        pipx_install = next(c for c in calls if c[:2] == ['pipx', 'install'])
        self.assertEqual(pipx_install, ['pipx', 'install', '--force',
                                        'git+https://example.invalid/x.git@v0.1.63'])
        scheduler_call = next(c for c in calls if c[0] == binary and c[1] == 'scheduler')
        self.assertEqual(scheduler_call, [binary, 'scheduler', 'install', '--product', 'p'])
        doctor_call = next(c for c in calls if c[0] == binary and c[1] == 'doctor')
        self.assertEqual(doctor_call, [binary, 'doctor', '--product', 'p'])
        self.assertLess(calls.index(pipx_install), calls.index(scheduler_call))
        self.assertLess(calls.index(scheduler_call), calls.index(doctor_call))
        self.assertIn('upgrade: v0.1.62 → v0.1.63', lines)
        self.assertIn(upgrade._tick_upgrade_line('v0.1.62', self.OLD_SHA, 'v0.1.63', self.SHA),
                      lines)

    def test_a_session_mid_landing_holds_and_installs_nothing(self):
        ctx = self._ctx()
        lines = []
        with mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=('v0.1.63', self.SHA)), \
                mock.patch.object(upgrade, 'installed_release', return_value=('v0.1.62', self.OLD_SHA)), \
                mock.patch.object(upgrade, 'mid_landing',
                                  return_value=[('T-0001', 'worker/T-0001')]), \
                mock.patch.object(upgrade, 'install') as m_install:
            result = upgrade.release_report(ctx, out=lines.append)
        self.assertEqual(result, 'held')
        m_install.assert_not_called()
        self.assertTrue(any('upgrade: held' in ln and 'T-0001 on worker/T-0001' in ln
                            for ln in lines), lines)

    def test_a_deferred_install_is_held_and_records_nothing(self):
        ctx = self._ctx()
        with mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=('v0.1.63', self.SHA)), \
                mock.patch.object(upgrade, 'installed_release', return_value=('v0.1.62', self.OLD_SHA)), \
                mock.patch.object(upgrade, 'mid_landing', return_value=[]), \
                mock.patch.object(upgrade, 'install', return_value=upgrade.DEFERRED):
            result = upgrade.release_report(ctx, out=lambda *_a: None)
        self.assertEqual(result, 'held')
        self.assertEqual(upgrade._read_release_state(ctx.product), {})

    def test_a_non_zero_install_fails_and_records_failed(self):
        ctx = self._ctx()
        lines = []
        with mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=('v0.1.63', self.SHA)), \
                mock.patch.object(upgrade, 'installed_release', return_value=('v0.1.62', self.OLD_SHA)), \
                mock.patch.object(upgrade, 'mid_landing', return_value=[]), \
                mock.patch.object(upgrade, 'install', return_value=1):
            result = upgrade.release_report(ctx, out=lines.append)
        self.assertEqual(result, 'failed')
        self.assertTrue(any('upgrade: FAILED v0.1.62 → v0.1.63' in ln for ln in lines), lines)
        self.assertEqual(upgrade._read_release_state(ctx.product)['last']['result'], 'failed')

    def test_an_unreadable_installed_release_never_installs(self):
        ctx = self._ctx()
        with mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=('v0.1.63', self.SHA)), \
                mock.patch.object(upgrade, 'installed_release', return_value=(None, None)), \
                mock.patch.object(upgrade, 'install') as m_install:
            result = upgrade.release_report(ctx, out=lambda *_a: None)
        self.assertEqual(result, 'none')
        m_install.assert_not_called()


class RedDoctorRollsBackTests(_HomeCase):
    """D10/D11/PD4: a RED doctor after the install puts the previous *tag* back (verified by its
    sha, never installed by sha — which would strand the product on an unreadable pin), files
    one S1 Bug per ``(tag, doctor RED)``, and pushes the record clone that Bug lives in from the
    channel itself."""

    SHA = 'b' * 40
    OLD_SHA = 'a' * 40
    DOCTOR_RED_OUT = '== DOCTOR p\nupgrade  RED   asf v0.1.63 fails its doctor\n'

    def _product(self, name='p'):
        return env.Product(name, {'repo_slug': 'a/b', 'conventions': {'flags': {'upgrade': 'auto'}}})

    def _root(self):
        root = tempfile.mkdtemp(prefix='release_rollback_test_')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def _ctx(self, product=None, root=None):
        product = product or self._product()
        root = root if root is not None else self._root()
        return types.SimpleNamespace(product=product, upgrade_line=None, record_root=lambda: root)

    def _run(self, ctx, push_result=True, err=None):
        lines = []
        with contextlib.redirect_stderr(err if err is not None else io.StringIO()), \
                mock.patch.object(upgrade, 'repo_url', return_value='url'), \
                mock.patch.object(upgrade, 'latest_release', return_value=('v0.1.63', self.SHA)), \
                mock.patch.object(upgrade, 'installed_release', return_value=('v0.1.62', self.OLD_SHA)), \
                mock.patch.object(upgrade, 'mid_landing', return_value=[]), \
                mock.patch.object(upgrade, 'install', return_value=0), \
                mock.patch.object(upgrade, '_install') as m_install, \
                mock.patch.object(upgrade, '_new_asf',
                                  side_effect=[(0, ''), (1, self.DOCTOR_RED_OUT), (0, '')]), \
                mock.patch('asf.tick.shadow.commit_local', return_value=True) as m_commit, \
                mock.patch('asf.tick.shadow.push', return_value=push_result) as m_push:
            result = upgrade.release_report(ctx, out=lines.append)
        return result, lines, m_install, m_commit, m_push

    def _bugs(self, root, sig='upgrade v0.1.63: doctor RED'):
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
        return [r for r in canonical.values()
                if frontmatter.split_machine(r['meta'])[0].get('signature') == sig]

    def test_a_red_doctor_rolls_back_and_files_one_bug(self):
        ctx = self._ctx()
        result, lines, m_install, m_commit, m_push = self._run(ctx)
        self.assertEqual(result, 'rolled-back')
        self.assertIn('rollback: doctor RED after v0.1.63 — reinstalling v0.1.62', lines)
        m_install.assert_called_once_with('v0.1.62', mock.ANY, mock.ANY, sha=self.OLD_SHA)
        self.assertTrue(any('file-bugs: upgrade v0.1.63 doctor RED — filed' in ln for ln in lines),
                        lines)
        m_commit.assert_called_once()
        m_push.assert_called_once()
        self.assertEqual(upgrade._read_release_state(ctx.product)['last']['result'], 'rolled-back')

        bugs = self._bugs(ctx.record_root())
        self.assertEqual(len(bugs), 1)
        typed, _machine = frontmatter.split_machine(bugs[0]['meta'])
        self.assertEqual(typed.get('severity'), 'S1')
        self.assertTrue(typed.get('decided'))
        self.assertIn('v0.1.63', typed.get('title') or '')
        self.assertIn('v0.1.62', typed.get('title') or '')

    def test_a_second_red_tick_at_the_same_tag_bumps_not_files(self):
        root = self._root()
        with mock.patch.object(file_bugs_mod, 'today', return_value='2026-10-10'):
            self._run(self._ctx(root=root))
        with mock.patch.object(file_bugs_mod, 'today', return_value='2026-10-11'):
            result, lines, *_rest = self._run(self._ctx(root=root))
        self.assertEqual(result, 'rolled-back')
        self.assertTrue(any('file-bugs: upgrade v0.1.63 doctor RED — bumped' in ln for ln in lines),
                        lines)
        self.assertEqual(len(self._bugs(root)), 1)

    def test_a_second_product_hitting_the_same_release_bumps_the_same_signature(self):
        root = self._root()
        with mock.patch.object(file_bugs_mod, 'today', return_value='2026-10-10'):
            self._run(self._ctx(product=self._product('p'), root=root))
        with mock.patch.object(file_bugs_mod, 'today', return_value='2026-10-11'):
            result, lines, *_rest = self._run(self._ctx(product=self._product('q'), root=root))
        self.assertEqual(result, 'rolled-back')
        self.assertTrue(any('bumped' in ln for ln in lines), lines)
        self.assertEqual(len(self._bugs(root)), 1)

    def test_a_push_that_fails_does_not_raise_and_does_not_stop_the_rollback(self):
        # shadow.push returning False is the documented refusal (no exception), and it says so on
        # stderr — the convention asf/tick/tick.py's commit_and_push already follows for the same
        # call, so a filed Bug that never reached origin is never silent
        ctx, err = self._ctx(), io.StringIO()
        result, lines, m_install, _m_commit, m_push = self._run(ctx, push_result=False, err=err)
        self.assertEqual(result, 'rolled-back')
        m_install.assert_called_once()
        m_push.assert_called_once()
        self.assertIn('upgrade: pushing the filed Bug failed — the next RED tick bumps it',
                      err.getvalue())
        self.assertEqual(upgrade._read_release_state(ctx.product)['last']['result'], 'rolled-back')

    def test_a_push_that_is_taken_says_nothing_on_stderr(self):
        err = io.StringIO()
        result, _lines, _m_install, _m_commit, m_push = self._run(self._ctx(), err=err)
        self.assertEqual(result, 'rolled-back')
        m_push.assert_called_once()
        self.assertEqual(err.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
