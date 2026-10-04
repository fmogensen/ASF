""":mod:`asf.security.alerts` (T-0362): the host's own alarms read as R-0009's lines, a cache
that exists only so staleness is detectable, and a blind read that never passes (D9) — an
unreadable feed behind a stale or absent cache is a violation, never silence."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from asf import env
from asf.security import alerts


def product(**conv):
    data = {'repo_slug': 'acme/widgets'}
    if conv:
        data['conventions'] = {'security': {'alerts': conv}}
    return env.Product('p', data)


class FakeHost(alerts.Host):
    def __init__(self, secrets=None, dependencies=None):
        self._secrets = secrets if secrets is not None else []
        self._dependencies = dependencies if dependencies is not None else []

    def secrets(self):
        return self._secrets

    def dependencies(self):
        return self._dependencies


class RaisingHost(alerts.Host):
    def __init__(self, why='gh api: timed out'):
        self.why = why

    def secrets(self):
        raise alerts.HostError(self.why)

    def dependencies(self):
        raise alerts.HostError(self.why)


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp

    def tearDown(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)


ONE_SECRET = [{'number': 7, 'state': 'open', 'kind': 'github_pat', 'url': 'https://x/7',
              'created_at': '2026-01-01T00:00:00Z', 'locations_count': 1}]
TWO_DEPS = [
    {'number': 3, 'state': 'open', 'severity': 'high', 'package': 'lodash', 'ecosystem': 'npm',
     'url': 'https://x/3', 'created_at': '2026-01-02T00:00:00Z'},
    {'number': 4, 'state': 'open', 'severity': 'moderate', 'package': 'requests',
     'ecosystem': 'pip', 'url': 'https://x/4', 'created_at': '2026-01-03T00:00:00Z'},
]


class FeedTests(unittest.TestCase):
    """:class:`alerts.GitHubHost` — the command it builds, never the secret-scanning alert's
    own ``secret`` field."""

    def _calls(self, stdout=''):
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr='')

        return calls, fake_run

    def test_an_unknown_read_is_a_host_error_never_an_empty_feed(self):
        from asf import gh_limit

        def failed(args, **_kw):
            return subprocess.CompletedProcess(args, 1, stdout='', stderr='HTTP 404\n')

        def timed_out(args, **_kw):
            raise subprocess.TimeoutExpired('gh', alerts.TIMEOUT_S)

        def limited(args, **_kw):
            return subprocess.CompletedProcess(args, 1, stdout='', stderr='API rate limit exceeded')
        try:
            for fake_run, why in ((failed, 'HTTP 404'), (timed_out, 'timeout'),
                                  (limited, 'rate limited')):
                with self.assertRaises(alerts.HostError) as ctx:
                    alerts.GitHubHost(product(), run=fake_run).secrets()
                self.assertIn(why, str(ctx.exception))
        finally:
            gh_limit.reset()

    def test_the_jq_selection_never_names_the_secret_field(self):
        calls, fake_run = self._calls()
        host = alerts.GitHubHost(product(), run=fake_run)
        host.secrets()
        jq = calls[0][calls[0].index('--jq') + 1]
        self.assertNotRegex(jq, r'\bsecret\b')

    def test_secrets_hits_the_secret_scanning_endpoint_with_state_open(self):
        calls, fake_run = self._calls()
        host = alerts.GitHubHost(product(), run=fake_run)
        host.secrets()
        self.assertIn('repos/acme/widgets/secret-scanning/alerts?state=open&per_page=100', calls[0])

    def test_dependencies_hits_the_dependabot_endpoint_with_state_open(self):
        calls, fake_run = self._calls()
        host = alerts.GitHubHost(product(), run=fake_run)
        host.dependencies()
        self.assertIn('repos/acme/widgets/dependabot/alerts?state=open&per_page=100', calls[0])

    def test_secrets_parses_one_json_object_per_line(self):
        _calls, fake_run = self._calls(stdout='{"number": 1}\n{"number": 2}\n')
        host = alerts.GitHubHost(product(), run=fake_run)
        self.assertEqual([r['number'] for r in host.secrets()], [1, 2])

    def test_a_nonzero_exit_raises_host_error(self):
        def fake_run(args, **kw):
            return subprocess.CompletedProcess(args, 1, stdout='', stderr='gh: not found\n')

        host = alerts.GitHubHost(product(), run=fake_run)
        with self.assertRaises(alerts.HostError):
            host.secrets()

    def test_a_timeout_raises_host_error(self):
        def fake_run(args, **kw):
            raise subprocess.TimeoutExpired(cmd=args, timeout=alerts.TIMEOUT_S)

        host = alerts.GitHubHost(product(), run=fake_run)
        with self.assertRaises(alerts.HostError):
            host.dependencies()

    def test_host_base_class_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            alerts.Host().secrets()
        with self.assertRaises(NotImplementedError):
            alerts.Host().dependencies()


class ViolationTests(Home):
    """:func:`alerts.violations` — the three line shapes of §2.6, and a read that fails never
    passing a stale or absent cache in silence (D9)."""

    def test_one_secret_and_two_dependency_alerts_give_three_lines(self):
        host = FakeHost(secrets=ONE_SECRET, dependencies=TWO_DEPS)
        lines = alerts.violations(product(), host=host)
        self.assertEqual(len(lines), 3)
        secret_line = lines[0]
        self.assertIn('sev=S1', secret_line)
        self.assertIn('sig=secret-acme/widgets-7', secret_line)
        self.assertIn('github_pat', secret_line)
        for line in lines[1:]:
            self.assertIn('sev=S2', line)
        self.assertIn('sig=dep-acme/widgets-3', lines[1])
        self.assertIn('sig=dep-acme/widgets-4', lines[2])

    def test_no_line_carries_a_secret_value_even_if_the_host_hands_one_over(self):
        planted = dict(ONE_SECRET[0], secret='ghp_totallyRealToken')
        host = FakeHost(secrets=[planted])
        for line in alerts.violations(product(), host=host):
            self.assertNotIn('ghp_totallyRealToken', line)

    def test_an_empty_feed_is_zero_lines(self):
        self.assertEqual(alerts.violations(product(), host=FakeHost()), [])

    def test_a_host_that_raises_with_a_fresh_cache_gives_no_line(self):
        p = product()
        now = _dt(2026, 1, 10)
        alerts.violations(p, host=FakeHost(secrets=ONE_SECRET), now=now)  # seeds the cache
        stale_now = now + _hours(2)
        lines = alerts.violations(p, host=RaisingHost('rate limited'), now=stale_now)
        self.assertEqual(lines, [])

    def test_a_host_that_raises_with_a_stale_cache_names_the_reason(self):
        p = product()
        now = _dt(2026, 1, 10)
        alerts.violations(p, host=FakeHost(secrets=ONE_SECRET), now=now)  # seeds the cache
        stale_now = now + _hours(30)
        lines = alerts.violations(p, host=RaisingHost('rate limited'), now=stale_now)
        self.assertEqual(len(lines), 1)
        self.assertIn('sev=S2', lines[0])
        self.assertIn('sig=feed-stale', lines[0])
        self.assertIn('rate limited', lines[0])
        self.assertIn('30h', lines[0])

    def test_a_host_that_raises_with_no_cache_at_all_is_a_violation_not_a_pass(self):
        lines = alerts.violations(product(), host=RaisingHost('no network'))
        self.assertEqual(len(lines), 1)
        self.assertIn('sig=feed-stale', lines[0])
        self.assertIn('no network', lines[0])

    def test_max_age_h_is_read_from_the_product_convention(self):
        p = product(max_age_h=1)
        now = _dt(2026, 1, 10)
        alerts.violations(p, host=FakeHost(), now=now)
        lines = alerts.violations(p, host=RaisingHost('down'), now=now + _hours(2))
        self.assertEqual(len(lines), 1)


class CacheTests(Home):
    def test_the_cache_is_written_through_a_temp_file_and_one_replace(self):
        p = product()
        path = alerts.cache_path(p)
        replaced = []
        real_replace = os.replace

        def counting_replace(src, dst):
            replaced.append((src, dst))
            real_replace(src, dst)

        os.replace = counting_replace
        try:
            alerts.violations(p, host=FakeHost(secrets=ONE_SECRET))
        finally:
            os.replace = real_replace
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0], (path + '.tmp', path))
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.isfile(path + '.tmp'))

    def test_a_malformed_cache_file_reads_as_no_cache_not_an_exception(self):
        p = product()
        path = alerts.cache_path(p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('not json')
        feeds, age_h, why = alerts.read(p, host=RaisingHost('down'))
        self.assertIsNone(feeds)
        self.assertIsNone(age_h)
        self.assertEqual(why, 'down')

    def test_the_cache_never_holds_a_field_the_jq_did_not_select(self):
        p = product()
        alerts.violations(p, host=FakeHost(secrets=ONE_SECRET, dependencies=TWO_DEPS))
        with open(alerts.cache_path(p), encoding='utf-8') as f:
            cached = json.load(f)
        self.assertEqual(set(cached['secrets'][0]), set(ONE_SECRET[0]))
        self.assertEqual(set(cached['dependencies'][0]), set(TWO_DEPS[0]))
        self.assertNotIn('secret', cached['secrets'][0])

    def test_a_live_read_rewrites_the_cache(self):
        p = product()
        now = _dt(2026, 1, 10)
        alerts.read(p, host=FakeHost(secrets=ONE_SECRET), now=now)
        with open(alerts.cache_path(p), encoding='utf-8') as f:
            cached = json.load(f)
        self.assertEqual(cached['ts'], '2026-01-10T00:00:00Z')
        self.assertEqual(cached['secrets'], ONE_SECRET)


def _dt(*args):
    import datetime
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


def _hours(n):
    import datetime
    return datetime.timedelta(hours=n)


if __name__ == '__main__':
    unittest.main()
