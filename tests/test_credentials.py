"""A credential probe is read-only and never prints a token — the surface Tasks 2, 4, 5 and 6 (the
command, the daily part, the doctor's row, the installer) are all built against
(:mod:`asf.credentials`)."""
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from asf import credentials, env, redact
from asf.tick import file_bugs
from asf.workers import pool as pool_mod
from asf.workers import runtime as runtime_mod

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


def fake_run(stdout='', stderr='', returncode=0):
    def run(cmd, env=None, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run


def counting_run(stdout='', stderr='', returncode=0):
    """A fake ``run=`` that records every command it was called with, so a test can assert how
    many times (and whether) a probe actually ran."""
    calls = []

    def run(cmd, env=None, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    run.calls = calls
    return run


class StubProduct:
    """What :func:`asf.credentials.for_product` and :func:`asf.credentials.probe_env` need — a
    stand-in for :class:`asf.env.Product` with no product file on disk."""

    def __init__(self, name='sample', repo_dir='/nowhere', credentials=()):
        self.name = name
        self.repo_dir = repo_dir
        self.credentials = list(credentials)


class ProbeTest(unittest.TestCase):
    def provider(self, probe='check-provider-a'):
        return credentials.Provider('provider-a', probe, 'renew-provider-a')

    def test_exit_0_with_an_expiry_line_is_valid_with_that_expiry(self):
        result = credentials.run_probe(self.provider(), {}, NOW,
                                       run=fake_run('expires 2026-12-01T00:00:00Z\n'))
        self.assertEqual(result.state, 'valid')
        self.assertEqual(result.expires, '2026-12-01T00:00:00Z')

    def test_exit_0_with_expires_never_has_no_days_left(self):
        result = credentials.run_probe(self.provider(), {}, NOW, run=fake_run('expires never\n'))
        self.assertEqual(result.state, 'valid')
        self.assertEqual(result.expires, 'never')
        self.assertIsNone(credentials.days_left(result, NOW))

    def test_exit_0_with_an_unreadable_line_is_valid_with_an_unknown_expiry(self):
        result = credentials.run_probe(self.provider(), {}, NOW, run=fake_run('ready\n'))
        self.assertEqual(result.state, 'valid')
        self.assertEqual(result.expires, '')

    def test_exit_1_is_invalid(self):
        result = credentials.run_probe(self.provider(), {}, NOW,
                                       run=fake_run('not signed in\n', returncode=1))
        self.assertEqual(result.state, 'invalid')

    def test_exit_2_is_broken(self):
        result = credentials.run_probe(self.provider(), {}, NOW,
                                       run=fake_run(stderr='boom\n', returncode=2))
        self.assertEqual(result.state, 'broken')

    def test_a_probe_that_sleeps_past_the_timeout_is_broken_and_returns_promptly(self):
        provider = self.provider(probe='sleep 5')
        env_map = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        start = time.monotonic()
        with mock.patch.object(credentials, 'PROBE_TIMEOUT_S', 0.2):
            result = credentials.run_probe(provider, env_map, NOW)
        elapsed = time.monotonic() - start
        self.assertEqual(result.state, 'broken')
        self.assertLess(elapsed, 3.0)   # well inside the real 5s sleep: timeout=, not ignored


class VerdictTest(unittest.TestCase):
    def result(self, expires='', state='valid'):
        return credentials.Result(provider='provider-a', state=state, expires=expires)

    def _in(self, days):
        return (NOW + datetime.timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def test_expiry_in_two_days_is_expiring_at_the_default_window(self):
        self.assertEqual(credentials.verdict(self.result(self._in(2)), NOW, 3), 'expiring')

    def test_expiry_in_four_days_is_ok_at_the_default_window(self):
        self.assertEqual(credentials.verdict(self.result(self._in(4)), NOW, 3), 'ok')

    def test_expiry_in_two_days_is_ok_at_a_one_day_window(self):
        self.assertEqual(credentials.verdict(self.result(self._in(2)), NOW, 1), 'ok')

    def test_an_unknown_expiry_is_unknown_never_expiring(self):
        self.assertEqual(credentials.verdict(self.result(''), NOW, 3), 'unknown')
        self.assertIsNone(credentials.days_left(self.result(''), NOW))

    def test_never_expires_is_unknown_too(self):
        self.assertEqual(credentials.verdict(self.result('never'), NOW, 3), 'unknown')
        self.assertIsNone(credentials.days_left(self.result('never'), NOW))

    def test_invalid_and_broken_pass_through_regardless_of_expiry(self):
        self.assertEqual(credentials.verdict(self.result(self._in(2), state='invalid'), NOW, 3),
                         'invalid')
        self.assertEqual(credentials.verdict(self.result('', state='broken'), NOW, 3), 'broken')

    def test_bad_collects_invalid_and_expiring_in_provider_order(self):
        expiring = self.result(self._in(2))
        ok = credentials.Result(provider='provider-b', state='valid', expires=self._in(30))
        invalid = credentials.Result(provider='provider-c', state='invalid')
        by_name = {
            'provider-a': credentials.Provider('provider-a', 'p', 'r', window_days=3),
            'provider-b': credentials.Provider('provider-b', 'p', 'r', window_days=3),
            'provider-c': credentials.Provider('provider-c', 'p', 'r', window_days=3),
        }
        self.assertEqual(
            credentials.bad([expiring, ok, invalid], providers_by_name=by_name, now=NOW),
            [expiring, invalid])


class ConfigTest(unittest.TestCase):
    def cfg(self, **credentials_section):
        return {'credentials': credentials_section} if credentials_section else {}

    def test_providers_reads_the_map_and_its_defaults(self):
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': 'login-a'},
            'provider-b': {'probe': 'check-b', 'renew': 'login-b', 'window_days': 7,
                          'account': 'lane-1'},
        })
        found = credentials.providers(cfg)
        self.assertEqual(set(found), {'provider-a', 'provider-b'})
        self.assertEqual(found['provider-a'].window_days, credentials.DEFAULT_WINDOW_DAYS)
        self.assertEqual(found['provider-a'].account, '')
        self.assertEqual(found['provider-b'].window_days, 7)
        self.assertEqual(found['provider-b'].account, 'lane-1')
        self.assertEqual(credentials.probe_every({}), credentials._seconds('1h'))
        self.assertEqual(credentials.probe_every(self.cfg(probe_every='30m')), 30 * 60)

    def test_a_missing_or_misshapen_section_is_no_providers_never_a_raise(self):
        self.assertEqual(credentials.providers(None), {})
        self.assertEqual(credentials.providers({'credentials': 'TODO'}), {})
        self.assertEqual(credentials.providers(self.cfg(providers='TODO')), {})

    def test_config_problems_names_a_provider_with_no_probe(self):
        cfg = self.cfg(providers={'provider-a': {'renew': 'login-a'}})
        self.assertIn(('credentials.providers.provider-a.probe', 'is required'),
                      credentials.config_problems(cfg))

    def test_config_problems_names_a_provider_with_no_renew(self):
        cfg = self.cfg(providers={'provider-a': {'probe': 'check-a'}})
        self.assertIn(('credentials.providers.provider-a.renew', 'is required'),
                      credentials.config_problems(cfg))

    def test_config_problems_names_a_bad_window_days(self):
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': 'login-a', 'window_days': 0}})
        self.assertIn(
            ('credentials.providers.provider-a.window_days', "must be a positive integer, not 0"),
            credentials.config_problems(cfg))

    def test_config_problems_names_a_bad_probe_every(self):
        cfg = self.cfg(probe_every='soon')
        self.assertIn(("credentials.probe_every", "must be '<n>s|m|h|d', not 'soon'"),
                      credentials.config_problems(cfg))

    def test_config_problems_names_an_account_no_pool_entry_names(self):
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': 'login-a', 'account': 'lane-9'}})
        self.assertIn(
            ('credentials.providers.provider-a.account', "'lane-9' is not a worker_pool account"),
            credentials.config_problems(cfg))

    def test_config_problems_names_a_secret_shaped_renew(self):
        secret_renew = 'echo ghp_' + 'A' * 40
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': secret_renew}})
        self.assertIn(
            ('credentials.providers.provider-a.renew',
             'carries a secret-shaped value — put the secret in a file'),
            credentials.config_problems(cfg))

    def test_for_product_returns_the_products_own_order(self):
        cfg = self.cfg(providers={
            'provider-a': {'probe': 'check-a', 'renew': 'login-a'},
            'provider-b': {'probe': 'check-b', 'renew': 'login-b'},
        })
        product = StubProduct(credentials=['provider-b', 'provider-a'])
        found = credentials.for_product(product, cfg)
        self.assertEqual([p.name for p in found], ['provider-b', 'provider-a'])

    def test_for_product_raises_on_a_name_config_does_not_define(self):
        cfg = self.cfg(providers={'provider-a': {'probe': 'check-a', 'renew': 'login-a'}})
        product = StubProduct(credentials=['provider-z'])
        with self.assertRaises(env.ConfigError) as cm:
            credentials.for_product(product, cfg)
        self.assertEqual(str(cm.exception),
                         'product.yaml credentials: provider-z is not a provider in '
                         'config.yaml credentials.providers')

    def test_probe_env_without_an_account_uses_the_worker_base(self):
        provider = credentials.Provider('provider-a', 'check-a', 'login-a')
        env_map = credentials.probe_env(provider, {}, StubProduct())
        self.assertEqual(env_map['GIT_TERMINAL_PROMPT'], '0')

    def test_probe_env_with_an_account_builds_the_accounts_own_environment(self):
        provider = credentials.Provider('provider-a', 'check-a', 'login-a', account='lane-1')
        cfg = {'worker_pool': {'accounts': [{'name': 'lane-1'}]}}
        env_map = credentials.probe_env(provider, cfg, StubProduct())
        self.assertEqual(env_map['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(env_map['ASF_PRODUCT'], 'sample')

    def test_probe_env_raises_on_an_account_no_pool_entry_names(self):
        provider = credentials.Provider('provider-a', 'check-a', 'login-a', account='lane-9')
        with self.assertRaises(runtime_mod.AuthEnvError):
            credentials.probe_env(provider, {}, StubProduct())


class CheckTest(unittest.TestCase):
    """``check()``'s cache decision (D6), its thread pool, and the command built on it:
    ``render``/``as_json``/``quiet``/``cmd_check`` (§2.3)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='credentials_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, probe_every=None, window_days=None):
        provider = {'probe': 'check-a', 'renew': 'login-a'}
        if window_days is not None:
            provider['window_days'] = window_days
        section = {'providers': {'provider-a': provider}}
        if probe_every is not None:
            section['probe_every'] = probe_every
        return {'credentials': section}

    def test_a_cached_result_is_not_reprobed(self):
        cfg = self.cfg()
        product = StubProduct(credentials=['provider-a'])
        run = counting_run(stdout='expires 2026-12-01T00:00:00Z\n')

        credentials.check(product, cfg, now=NOW, run=run)
        self.assertEqual(len(run.calls), 1)

        # the same instant again: the cache is fresh (default probe_every is 1h) — no reprobe
        credentials.check(product, cfg, now=NOW, run=run)
        self.assertEqual(len(run.calls), 1)

        # fresh=True bypasses the cache even though it is still fresh
        credentials.check(product, cfg, fresh=True, now=NOW, run=run)
        self.assertEqual(len(run.calls), 2)

        # two hours later the cached probed_at is stale (> the default 1h probe_every):
        # a plain call (no fresh) reprobes on its own
        later = NOW + datetime.timedelta(hours=2)
        credentials.check(product, cfg, now=later, run=run)
        self.assertEqual(len(run.calls), 3)

    def test_the_window_is_recomputed_from_the_cache(self):
        cfg = self.cfg(probe_every='7d', window_days=3)
        product = StubProduct(credentials=['provider-a'])
        expiry = (NOW + datetime.timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        run = counting_run(stdout=f'expires {expiry}\n')

        results = credentials.check(product, cfg, now=NOW, run=run)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(credentials.verdict(results[0], NOW, 3), 'ok')

        # three days on, the cache (still fresh under a 7d probe_every) is not re-probed, but
        # its stored expiry now ticks the provider into its window
        later = NOW + datetime.timedelta(days=3)
        results = credentials.check(product, cfg, now=later, run=run)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(results[0].expires, expiry)
        self.assertEqual(credentials.verdict(results[0], later, 3), 'expiring')

    def test_quiet_is_the_rule_contract(self):
        cfg = self.cfg()
        product = StubProduct(credentials=['provider-a'])

        def result(state, expires=''):
            return credentials.Result(provider='provider-a', state=state, expires=expires)

        with mock.patch.object(credentials, '_now', return_value=NOW):
            out = []
            rc = credentials.quiet([result('valid', expires='never')], product, cfg,
                                   out=out.append)
            self.assertEqual(rc, 0)
            self.assertEqual(out, [])

            out = []
            expiring = result('valid', expires=(NOW + datetime.timedelta(days=1))
                              .strftime('%Y-%m-%dT%H:%M:%SZ'))
            rc = credentials.quiet([expiring], product, cfg, out=out.append)
            self.assertEqual(rc, 1)
            self.assertEqual(len(out), 1)
            self.assertIn('provider-a', out[0])
            self.assertIn('login-a', out[0])

            out = []
            rc = credentials.quiet([result('invalid')], product, cfg, out=out.append)
            self.assertEqual(rc, 1)
            self.assertIn('login-a', out[0])

            out, err_lines = [], []

            class _Err:
                def write(self, text):
                    err_lines.append(text)

            rc = credentials.quiet([result('broken')], product, cfg, out=out.append,
                                   err=_Err())
            self.assertEqual(rc, 2)
            self.assertEqual(out, [])  # a broken probe raises no bad verdict line
            self.assertTrue(any('provider-a' in line for line in err_lines))

    def test_the_table_names_no_provider_twice(self):
        cfg = self.cfg()
        product = StubProduct(credentials=['provider-a'])
        dup = [credentials.Result(provider='provider-a', state='valid', expires='never'),
              credentials.Result(provider='provider-a', state='valid', expires='never')]
        out = []
        with mock.patch.object(credentials, '_now', return_value=NOW):
            credentials.render(dup, product, cfg, out=out.append)
        body = [line for line in out if line.startswith('provider-a')]
        self.assertEqual(len(body), 1)

    def test_a_broken_probe_does_not_overwrite_a_known_expiry(self):
        cfg = self.cfg(probe_every='7d', window_days=3)
        product = StubProduct(credentials=['provider-a'])
        good_expiry = (NOW + datetime.timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        good_run = counting_run(stdout=f'expires {good_expiry}\n')
        credentials.check(product, cfg, now=NOW, run=good_run)

        broken_run = counting_run(returncode=2, stderr='boom\n')
        broken_results = credentials.check(product, cfg, fresh=True, now=NOW, run=broken_run)
        self.assertEqual(broken_results[0].state, 'broken')

        # the cache on disk still carries the good expiry, recomputed as `expiring` next call
        later = NOW + datetime.timedelta(days=1)
        results = credentials.check(product, cfg, now=later, run=broken_run)
        self.assertEqual(results[0].expires, good_expiry)
        self.assertEqual(credentials.verdict(results[0], later, 3), 'expiring')


class OperatorLineTest(unittest.TestCase):
    """§2.7 from the day's side (T-0313): `credentials.daily()`'s once-a-day operator line,
    raised through `file_bugs.report_operator_rules`'s ledger — never a Bug, never a second
    copy keyed differently (PD10)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='credentials_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        self.probe = os.path.join(self.tmp, 'probe.sh')
        with open(self.probe, 'w', encoding='utf-8') as f:
            f.write("#!/usr/bin/env bash\necho 'expires 2026-09-29T08:00:00Z'\nexit 0\n")
        os.chmod(self.probe, 0o755)
        self.record = tempfile.mkdtemp(prefix='credentials_record_')
        from asf import init
        init.lay_down(self.record)

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.record, ignore_errors=True)

    def cfg(self):
        return {'credentials': {'providers': {'provider-a': {
            'probe': self.probe, 'renew': 'login provider-a'}}}}

    def _daily(self, product):
        with mock.patch.object(env, 'load_config', return_value=self.cfg()):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = credentials.daily(product, self.record, now=NOW)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def _operator_ledger(self, product):
        with open(file_bugs.ledger_path(product.name), encoding='utf-8') as f:
            return json.load(f).get('operator', {})

    def test_an_expired_session_is_one_line_a_day(self):
        product = StubProduct(credentials=['provider-a'])
        out = self._daily(product)
        lines = [l for l in out.splitlines() if l.startswith('NEEDS OPERATOR:')]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith('login provider-a'))
        ledger = self._operator_ledger(product)
        self.assertEqual(list(ledger), ['credentials'])  # no card installed: the fallback key
        self.assertEqual(ledger['credentials']['day'], NOW.strftime('%Y-%m-%d'))

        second = self._daily(product)
        self.assertEqual([l for l in second.splitlines() if l.startswith('NEEDS OPERATOR:')], [])

    def test_the_key_follows_the_installed_cards_id(self):
        from asf.record.index import do_index
        card_path = os.path.join(self.record, 'rules', 'R-0001.md')
        os.makedirs(os.path.dirname(card_path), exist_ok=True)
        with open(card_path, 'w', encoding='utf-8') as f:
            f.write("---\nid: R-0001\ntype: rule\ntitle: t\ncheck: rules/credentials.sh\n"
                   "owner: operator\n# ---- machine ----\nstate: New\n"
                   "updated: 2026-09-21T00:00:00Z\n---\n"
                   "## Statement\n\n## Why\n\n## Check\n\n## Source\n\n## Children\n\n"
                   "## Backlinks\n")
        do_index(self.record)

        product = StubProduct(credentials=['provider-a'])
        self._daily(product)
        ledger = self._operator_ledger(product)
        self.assertEqual(list(ledger), ['R-0001'])


class NoTokenTest(unittest.TestCase):
    """§2.4: nothing of a probe's own raw output survives but its last line, scrubbed and
    truncated — checked against every artefact that line can reach."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='credentials_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        self.record = tempfile.mkdtemp(prefix='credentials_record_')
        from asf import init
        init.lay_down(self.record)

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.record, ignore_errors=True)

    def _secret(self):
        """A value shaped like one of :data:`redact.SECRET_RULES` ('github-token'), built at
        run time — no secret shape is written into this file itself."""
        pattern = dict(redact.SECRET_RULES)['github-token']
        value = 'ghp_' + ('a1B2c3D4e5' * 4)
        self.assertRegex(value, pattern)
        return value

    def test_no_token_anywhere(self):
        secret = self._secret()
        probe = os.path.join(self.tmp, 'probe.sh')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write("#!/usr/bin/env bash\n"
                   f"echo 'expires 2026-12-01T00:00:00Z token={secret}'\n"
                   "exit 0\n")
        os.chmod(probe, 0o755)
        cfg = {'credentials': {'providers': {'provider-a': {
            'probe': probe, 'renew': 'login-a'}}}}
        product = StubProduct(credentials=['provider-a'])

        events = []
        with mock.patch.object(env, 'load_config', return_value=cfg):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                credentials.daily(product, self.record,
                                  event=lambda kind, **f: events.append((kind, f)), now=NOW)

        results = credentials.check(product, cfg, now=NOW)
        result = results[0]
        self.assertEqual(result.expires, '2026-12-01T00:00:00Z')
        self.assertNotIn(secret, result.detail)
        self.assertIn(redact.SCRUB_TOKEN, result.detail)

        table = []
        with mock.patch.object(credentials, '_now', return_value=NOW):
            credentials.render(results, product, cfg, out=table.append)
        self.assertNotIn(secret, '\n'.join(table))

        payload = json.dumps(credentials.as_json(results, product, cfg))
        self.assertNotIn(secret, payload)

        with open(credentials.cache_path(product), encoding='utf-8') as f:
            cache_text = f.read()
        self.assertNotIn(secret, cache_text)
        self.assertIn(redact.SCRUB_TOKEN, cache_text)

        events_text = json.dumps(events)
        self.assertNotIn(secret, events_text)

    def test_a_20kb_probe_is_truncated_to_detail_max(self):
        probe = os.path.join(self.tmp, 'probe.sh')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write("#!/usr/bin/env bash\npython3 -c \"print('x' * 20000)\"\nexit 0\n")
        os.chmod(probe, 0o755)
        provider = credentials.Provider('provider-a', probe, 'login-a')
        result = credentials.run_probe(provider, {'PATH': os.environ.get('PATH', '')}, NOW)
        self.assertLessEqual(len(result.detail), credentials.DETAIL_MAX)


class InstallTest(unittest.TestCase):
    """§2.8: the rule card and its check script are the two artefacts this Feature cannot
    have a session write (D9) — minted and written from the operator's own shell instead."""

    def setUp(self):
        self.record = tempfile.mkdtemp(prefix='credentials_install_record_')
        self.core = tempfile.mkdtemp(prefix='credentials_install_core_')
        self.repo = tempfile.mkdtemp(prefix='credentials_install_repo_')
        from asf import init
        init.lay_down(self.record)
        self._orig_core = os.environ.get('ASF_CORE_RULES_DIR')
        os.environ['ASF_CORE_RULES_DIR'] = self.core
        self._orig_job = os.environ.pop('ASF_JOB', None)

    def tearDown(self):
        if self._orig_core is None:
            os.environ.pop('ASF_CORE_RULES_DIR', None)
        else:
            os.environ['ASF_CORE_RULES_DIR'] = self._orig_core
        if self._orig_job is not None:
            os.environ['ASF_JOB'] = self._orig_job
        shutil.rmtree(self.record, ignore_errors=True)
        shutil.rmtree(self.core, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_install_writes_the_card_and_the_script(self):
        from asf.rules import rules
        rc = credentials.install(self.record, self.repo, job=None)
        self.assertEqual(rc, 0)
        cards = rules.load_rules(self.record)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card['check'], 'rules/credentials.sh')
        self.assertEqual(card['owner'], 'operator')
        script_path = os.path.join(self.core, 'credentials.sh')
        self.assertTrue(os.path.isfile(script_path))
        self.assertEqual(os.stat(script_path).st_mode & 0o777, 0o755)
        with open(script_path, encoding='utf-8') as f:
            self.assertIn(card['id'], f.read())

    def test_the_installed_card_runs_in_the_rules_row(self):
        from asf.rules import rules
        credentials.install(self.record, self.repo, job=None)
        card = rules.load_rules(self.record)[0]

        stub_dir = tempfile.mkdtemp(prefix='credentials_install_stub_')
        self.addCleanup(shutil.rmtree, stub_dir, ignore_errors=True)
        with open(os.path.join(stub_dir, 'asf'), 'w', encoding='utf-8') as f:
            f.write("#!/usr/bin/env bash\n"
                    "echo 'credential provider-c expires 2026-09-29T08:00:00Z "
                    "(2d, window 3d) — renew: login provider-c'\n"
                    "exit 1\n")
        os.chmod(os.path.join(stub_dir, 'asf'), 0o755)
        old_path = os.environ.get('PATH', '')
        os.environ['PATH'] = stub_dir + os.pathsep + old_path
        try:
            lines, broken = rules.run_check(self.record, card)
        finally:
            os.environ['PATH'] = old_path
        self.assertEqual(broken, [])
        self.assertEqual(len(lines), 1)
        self.assertIn('credential provider-c expires', lines[0])

    def test_install_refuses_inside_a_session(self):
        from asf.rules import rules
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = credentials.install(self.record, self.repo, job='spec-f-0042')
        self.assertEqual(rc, 2)
        self.assertIn('NEEDS OPERATOR:', buf.getvalue())
        self.assertEqual(rules.load_rules(self.record), [])
        self.assertFalse(os.path.isfile(os.path.join(self.core, 'credentials.sh')))

    def test_the_daily_step_names_credentials(self):
        from asf.tick import step_daily
        names = [n for n, _ in step_daily.parts(StubProduct(), self.record)]
        self.assertIn('credentials', names)

    def test_print_writes_nothing(self):
        from asf.rules import rules
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = credentials.install(self.record, self.repo, print_only=True, job=None)
        self.assertEqual(rc, 0)
        self.assertEqual(rules.load_rules(self.record), [])
        self.assertFalse(os.path.isfile(os.path.join(self.core, 'credentials.sh')))
        self.assertIn('credentials.sh', buf.getvalue())
        self.assertIn('owner: operator', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
