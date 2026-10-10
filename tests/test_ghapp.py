"""asf.ghapp — the factory's own GitHub identity: declaration, JWT, mint, cache, refresh, and
nothing secret in any return value, log line or file. Every case here injects the signer
(``run``, for ``openssl``) and the HTTP call (``http``, for the module's one
``urllib.request.urlopen``), so the whole module's proof runs with no key, no App and no
network."""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest import mock

from asf import env, gh_limit, ghapp, mutation_guard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: a stand-in for a key file's bytes, never read by the module itself (only its *path* is ever
#: passed to ``openssl``) — if this marker ever reached a return value, a log line or a file
#: this module writes, that would be the defect S-120855's secrecy line exists to catch. Not
#: shaped like a real PEM block, so it is not itself a finding for the redaction scanner.
FAKE_KEY = 'not-a-real-key SECRET-KEY-MARKER-LINE not-a-real-key\n'


def fake_run(rc=0, out=b'FAKESIG', err=b''):
    """A ``run`` double for :func:`asf.ghapp.sign`: records every call, answers from one fixed
    reply."""
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return mock.Mock(returncode=rc, stdout=out, stderr=err)
    run.calls = calls
    return run


class FakeResponse:
    """A double for what ``urllib.request.urlopen`` returns: a context manager with ``.status``
    and ``.read()``."""

    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, bytes) else body.encode('utf-8')

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_http(*replies):
    """An ``http`` double for :func:`asf.ghapp._request`: one ``(status, body)`` per call, in
    order; replies past the end repeat the last one. Records every request it was given."""
    calls = []

    def http(req, timeout=None):
        calls.append(req)
        status, body = replies[min(len(calls) - 1, len(replies) - 1)]
        return FakeResponse(status, body)
    http.calls = calls
    return http


def boom(*a, **kw):
    raise AssertionError(f'should not have been called: {a!r} {kw!r}')


MINT_BODY = json.dumps({'token': 'INSTALLATION-TOKEN-XYZ',
                        'expires_at': '2026-01-01T01:00:00Z'})
DISCOVERY_BODY = json.dumps({'id': 555})


class GhappCase(unittest.TestCase):
    """Isolates ``env.ASF_HOME`` so a mint's token/meta files never touch the real one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='asf-ghapp-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.addCleanup(setattr, env, 'ASF_HOME', self._home)
        self.key_file = os.path.join(self.tmp, 'app-key.pem')
        with open(self.key_file, 'w', encoding='utf-8') as f:
            f.write(FAKE_KEY)
        gh_limit.reset()
        self.addCleanup(gh_limit.reset)

    def product(self, app_id=123456, key_file=None, installation_id=None, repo_slug='acme/sample',
               github_app=True):
        data = {'repo_dir': '/tmp/nowhere', 'repo_slug': repo_slug}
        if github_app:
            block = {'app_id': app_id, 'key_file': key_file or self.key_file}
            if installation_id is not None:
                block['installation_id'] = installation_id
            data['conventions'] = {'github_app': block}
        return env.Product('sample', data)


# ----------------------------------------------------------------- declaration (lines 1-3) --

class Declaration(GhappCase):
    def test_no_github_app_is_unconfigured_and_settings_returns_none(self):
        product = self.product(github_app=False)
        self.assertFalse(ghapp.configured(product))
        self.assertIsNone(ghapp.settings(product))

    def test_app_id_and_key_file_come_back_with_the_key_path_tilde_expanded(self):
        product = self.product(app_id=42, key_file='~/.ASF/secrets/sample-app.pem')
        app = ghapp.settings(product)
        self.assertEqual(app.app_id, '42')
        self.assertEqual(app.key_file, os.path.expanduser('~/.ASF/secrets/sample-app.pem'))
        self.assertTrue(ghapp.configured(product))

    def test_a_declared_installation_id_is_an_int_an_absent_one_is_empty(self):
        with_id = ghapp.settings(self.product(installation_id=987654))
        self.assertEqual(with_id.installation_id, 987654)
        self.assertIsInstance(with_id.installation_id, int)
        without_id = ghapp.settings(self.product())
        self.assertIsNone(without_id.installation_id)


# ----------------------------------------------------------------------- signing (lines 4-7) --

class Signing(GhappCase):
    def app(self):
        return ghapp.App('42', self.key_file, None)

    def test_signing_input_is_two_unpadded_base64url_segments_joined_by_a_dot(self):
        data = ghapp.signing_input(self.app(), 1_700_000_000)
        self.assertEqual(data.count(b'.'), 1)
        header_b64, claims_b64 = data.split(b'.')
        for seg in (header_b64, claims_b64):
            self.assertNotIn(b'=', seg)
            self.assertTrue(all(c not in b'+/' for c in seg), seg)  # base64url, not base64

    def test_claims_carry_the_issuer_and_the_iat_exp_window_off_the_given_clock(self):
        import base64
        now = 1_700_000_000
        data = ghapp.signing_input(self.app(), now)
        header_b64, claims_b64 = data.split(b'.')

        def _decode(seg):
            pad = b'=' * (-len(seg) % 4)
            return json.loads(base64.urlsafe_b64decode(seg + pad))
        self.assertEqual(_decode(header_b64), {'alg': 'RS256', 'typ': 'JWT'})
        claims = _decode(claims_b64)
        self.assertEqual(claims['iss'], '42')
        self.assertEqual(claims['iat'], now - 60)
        self.assertEqual(claims['exp'], now + 540)

    def test_the_signer_is_invoked_as_openssl_with_digest_and_key_file_and_data_on_stdin(self):
        run = fake_run()
        data = ghapp.signing_input(self.app(), 1_700_000_000)
        sig, why = ghapp.sign(self.app(), data, run=run)
        self.assertEqual(why, '')
        self.assertEqual(sig, b'FAKESIG')
        self.assertEqual(len(run.calls), 1)
        argv, kw = run.calls[0]
        self.assertEqual(argv, ['openssl', 'dgst', '-sha256', '-sign', self.key_file])
        self.assertEqual(kw['input'], data)

    def test_a_failed_signer_returns_no_token_and_names_only_the_key_path(self):
        run = fake_run(rc=1, err=b'Could not open file or uri for loading private key from '
                              + self.key_file.encode() + b'\n')
        sig, why = ghapp.sign(self.app(), b'x', run=run)
        self.assertIsNone(sig)
        self.assertIn(self.key_file, why)
        self.assertNotIn('SECRET-KEY-MARKER-LINE', why)


# -------------------------------------------------------------------------- minting (8-12) --

class Minting(GhappCase):
    def test_the_minted_token_and_expiry_are_the_mint_responses_own(self):
        product = self.product(installation_id=987654)
        http = fake_http((201, MINT_BODY))
        expires, why = ghapp.mint(product, now=1_700_000_000, http=http, run=fake_run())
        self.assertEqual(why, '')
        self.assertEqual(expires, '2026-01-01T01:00:00Z')
        self.assertEqual(ghapp.token(product), 'INSTALLATION-TOKEN-XYZ')

    def test_the_token_file_is_0600_and_holds_only_the_token(self):
        product = self.product(installation_id=987654)
        ghapp.mint(product, now=1_700_000_000, http=fake_http((201, MINT_BODY)), run=fake_run())
        path = ghapp.token_path(product)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
        with open(path, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'INSTALLATION-TOKEN-XYZ')

    def test_the_meta_file_holds_ids_slug_mint_time_and_expiry_and_no_secret(self):
        product = self.product(app_id=42, installation_id=987654, repo_slug='acme/sample')
        ghapp.mint(product, now=1_700_000_000, http=fake_http((201, MINT_BODY)), run=fake_run())
        with open(ghapp.meta_path(product), encoding='utf-8') as f:
            meta = json.load(f)
        self.assertEqual(meta['app_id'], '42')
        self.assertEqual(meta['installation_id'], 987654)
        self.assertEqual(meta['slug'], 'acme/sample')
        self.assertTrue(meta['minted_at'])
        self.assertEqual(meta['expires_at'], '2026-01-01T01:00:00Z')
        raw = json.dumps(meta)
        self.assertNotIn('INSTALLATION-TOKEN-XYZ', raw)
        self.assertNotIn('SECRET-KEY-MARKER-LINE', raw)

    def test_installation_is_discovered_from_the_repo_slug_when_none_is_declared(self):
        product = self.product(repo_slug='acme/sample')  # no installation_id declared
        http = fake_http((200, DISCOVERY_BODY), (201, MINT_BODY))
        expires, why = ghapp.mint(product, now=1_700_000_000, http=http, run=fake_run())
        self.assertEqual(why, '')
        self.assertEqual(len(http.calls), 2)
        self.assertIn('acme/sample/installation', http.calls[0].full_url)
        with open(ghapp.meta_path(product), encoding='utf-8') as f:
            meta = json.load(f)
        self.assertEqual(meta['installation_id'], 555)

    def test_a_declared_installation_id_is_used_as_declared_with_no_discovery_call(self):
        product = self.product(installation_id=987654)
        http = fake_http((201, MINT_BODY))
        expires, why = ghapp.mint(product, now=1_700_000_000, http=http, run=fake_run())
        self.assertEqual(why, '')
        self.assertEqual(len(http.calls), 1)
        self.assertIn('/installations/987654/access_tokens', http.calls[0].full_url)


# ------------------------------------------------------------------------- refresh (13-18) --

class Refreshing(GhappCase):
    def seed(self, product, token_text, expires_at):
        path = ghapp.token_path(product)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(token_text)
        with open(ghapp.meta_path(product), 'w', encoding='utf-8') as f:
            json.dump({'app_id': '42', 'installation_id': 987654, 'slug': 'acme/sample',
                      'minted_at': '2025-12-31T00:00:00Z', 'expires_at': expires_at, 'error': ''}, f)

    def test_a_cached_token_further_than_the_margin_makes_no_call_at_all(self):
        product = self.product(installation_id=987654)
        self.seed(product, 'OLD-TOKEN', '2026-01-01T01:00:00Z')  # 2000s past `now` below
        now = ghapp._seconds_left('2026-01-01T01:00:00Z', 0) - 2000  # now = expiry - 2000
        expires, why = ghapp.refresh(product, now=now, http=boom, run=boom)
        self.assertEqual(why, '')
        self.assertEqual(expires, '2026-01-01T01:00:00Z')
        self.assertEqual(ghapp.token(product), 'OLD-TOKEN')  # untouched

    def test_a_cached_token_inside_the_margin_mints_a_new_one(self):
        product = self.product(installation_id=987654)
        now = ghapp._seconds_left('2026-01-01T01:00:00Z', 0) - 100  # 100s left, margin is 900
        self.seed(product, 'OLD-TOKEN', '2026-01-01T01:00:00Z')
        http = fake_http((201, MINT_BODY))
        expires, why = ghapp.refresh(product, now=now, http=http, run=fake_run())
        self.assertEqual(why, '')
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(ghapp.token(product), 'INSTALLATION-TOKEN-XYZ')

    def test_a_refresh_with_no_cached_token_mints_one(self):
        product = self.product(installation_id=987654)
        http = fake_http((201, MINT_BODY))
        expires, why = ghapp.refresh(product, now=1_700_000_000, http=http, run=fake_run())
        self.assertEqual(why, '')
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(ghapp.token(product), 'INSTALLATION-TOKEN-XYZ')

    def test_a_refresh_with_no_app_configured_makes_no_call_writes_nothing(self):
        product = self.product(github_app=False)
        expires, why = ghapp.refresh(product, now=1_700_000_000, http=boom, run=boom)
        self.assertEqual(expires, '')
        self.assertEqual(why, 'no app configured')
        self.assertFalse(os.path.exists(ghapp.token_path(product)))
        self.assertFalse(os.path.exists(ghapp.meta_path(product)))

    def test_a_dry_run_mints_nothing_prints_one_would_mint_line_and_gives_the_dry_run_reason(self):
        product = self.product(installation_id=987654, repo_slug='acme/sample')
        out = io.StringIO()
        with mutation_guard.active():
            with redirect_stdout(out):
                expires, why = ghapp.refresh(product, now=1_700_000_000, http=boom, run=boom)
        self.assertEqual(expires, '')
        self.assertEqual(why, 'dry run')
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        self.assertEqual(lines, ['would mint an installation token for acme/sample'])
        self.assertFalse(os.path.exists(ghapp.token_path(product)))

    def test_a_failed_mint_leaves_the_previous_token_file_and_records_the_reason(self):
        product = self.product(installation_id=987654)
        self.seed(product, 'OLD-TOKEN', '2026-01-01T00:05:00Z')
        now = ghapp._seconds_left('2026-01-01T00:05:00Z', 0) - 100  # already inside the margin
        http = fake_http((500, 'server error'))
        expires, why = ghapp.refresh(product, now=now, http=http, run=fake_run())
        self.assertEqual(expires, '')
        self.assertNotEqual(why, '')
        self.assertEqual(ghapp.token(product), 'OLD-TOKEN')  # exactly as it was
        with open(ghapp.meta_path(product), encoding='utf-8') as f:
            meta = json.load(f)
        self.assertEqual(meta['error'], why)
        self.assertEqual(meta['expires_at'], '2026-01-01T00:05:00Z')  # the stale expiry survives

    def test_a_network_error_mints_nothing_and_leaves_the_previous_token_file_exactly_as_it_was(self):
        product = self.product(installation_id=987654)
        self.seed(product, 'OLD-TOKEN', '2026-01-01T00:05:00Z')
        now = ghapp._seconds_left('2026-01-01T00:05:00Z', 0) - 100  # already inside the margin

        def http(req, timeout=None):
            raise urllib.error.URLError('no route to host')

        expires, why = ghapp.refresh(product, now=now, http=http, run=fake_run())
        self.assertEqual(expires, '')
        self.assertNotEqual(why, '')
        self.assertEqual(ghapp.token(product), 'OLD-TOKEN')  # exactly as it was


# ------------------------------------------------------------ rate limit / parsing (19-20) --

class RateLimitAndParsing(GhappCase):
    def test_a_rate_limited_mint_latches_the_process_like_a_refused_gh_call(self):
        product = self.product(installation_id=987654)
        http = fake_http((403, 'API rate limit exceeded for installation ID 9.'))
        self.assertIsNone(gh_limit.latched())
        with self.assertRaises(gh_limit.RateLimited):
            ghapp.mint(product, now=1_700_000_000, http=http, run=fake_run())
        self.assertTrue(gh_limit.latched())
        self.assertFalse(os.path.exists(ghapp.token_path(product)))

    def test_an_unparseable_mint_response_is_a_reason_never_a_token(self):
        product = self.product(installation_id=987654)
        http = fake_http((201, 'not-json-at-all'))
        expires, why = ghapp.mint(product, now=1_700_000_000, http=http, run=fake_run())
        self.assertEqual(expires, '')
        self.assertNotEqual(why, '')
        self.assertFalse(os.path.exists(ghapp.token_path(product)))


# ----------------------------------------------------------------------- secrecy (line 21) --

class Secrecy(GhappCase):
    """No return value, log line or file this module writes carries the token, the JWT, or a
    line of the key file — checked over every failure path: a failed signer, a failed
    discovery, a non-2xx mint, an unparseable mint, a dry run, and a rate-limited mint."""

    def assertNothingSecret(self, *texts):
        for text in texts:
            self.assertNotIn('INSTALLATION-TOKEN-XYZ', text)
            self.assertNotIn('SECRET-KEY-MARKER-LINE', text)

    def run_scenario(self, product, *, http=None, run=None, dry_run=False, now=1_700_000_000):
        out = io.StringIO()
        ctx = mutation_guard.active() if dry_run else _noop()
        with ctx, redirect_stdout(out):
            try:
                expires, why = ghapp.mint(product, now=now, http=http, run=run) \
                    if not dry_run else ghapp.refresh(product, now=now, http=http, run=run)
            except gh_limit.RateLimited as exc:
                expires, why = '', str(exc)
        meta_raw = ''
        if os.path.exists(ghapp.meta_path(product)):
            with open(ghapp.meta_path(product), encoding='utf-8') as f:
                meta_raw = f.read()
        return why, out.getvalue(), meta_raw

    def test_no_secret_leaks_on_any_failure_path(self):
        product = self.product(installation_id=987654)
        # seed a prior successful mint, so a later failure has a real token to not leak
        ghapp.mint(product, now=1_600_000_000, http=fake_http((201, MINT_BODY)), run=fake_run())
        self.assertEqual(ghapp.token(product), 'INSTALLATION-TOKEN-XYZ')

        scenarios = [
            dict(run=fake_run(rc=1, err=b'Could not open file or uri for loading private key '
                                      b'from ' + self.key_file.encode())),
            dict(http=fake_http((500, 'internal error')), run=fake_run()),
            dict(http=fake_http((201, 'not-json')), run=fake_run()),
            dict(http=boom, run=boom, dry_run=True,
                 now=ghapp._seconds_left('2026-01-01T01:00:00Z', 0) - 100),
            dict(http=fake_http((403, 'secondary rate limit')), run=fake_run()),
            dict(http=fake_http((404, 'Not Found')), run=fake_run(), product=self.product()),
        ]
        for scenario in scenarios:
            gh_limit.reset()
            scenario = dict(scenario)
            scenario_product = scenario.pop('product', product)
            dry_run = scenario.get('dry_run', False)
            why, printed, meta_raw = self.run_scenario(scenario_product, **scenario)
            self.assertNothingSecret(why, printed, meta_raw)
            if dry_run:
                self.assertEqual(why, 'dry run')
        # the earlier, still-cached token itself is untouched by every failure above
        self.assertEqual(ghapp.token(product), 'INSTALLATION-TOKEN-XYZ')


class _noop:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------------- the ratchet (22-23) --

class Ratchet(unittest.TestCase):
    """The module holds the only ``urllib.request`` site and the only ``openssl`` call site in
    the package; the client ratchet (``tools/check_clients.py``) names it as that kind's sole
    owner."""

    def py_files(self):
        for base, dirs, files in os.walk(os.path.join(ROOT, 'asf')):
            dirs[:] = sorted(d for d in dirs if d != '__pycache__')
            for name in sorted(files):
                if name.endswith('.py'):
                    yield os.path.relpath(os.path.join(base, name), ROOT).replace(os.sep, '/')

    def test_the_module_holds_the_only_urllib_request_site_in_the_package(self):
        hits = []
        for rel in self.py_files():
            with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
                if 'urllib.request' in f.read() and rel != 'asf/ghapp.py':
                    hits.append(rel)
        self.assertEqual(hits, [])
        with open(os.path.join(ROOT, 'asf/ghapp.py'), encoding='utf-8') as f:
            self.assertIn('urllib.request', f.read())

    def test_the_module_holds_the_only_openssl_site_and_the_ratchet_names_it_as_owner(self):
        hits = []
        for rel in self.py_files():
            with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
                if "'openssl'" in f.read() and rel != 'asf/ghapp.py':
                    hits.append(rel)
        self.assertEqual(hits, [])
        spec = importlib.util.spec_from_file_location(
            'check_clients', os.path.join(ROOT, 'tools', 'check_clients.py'))
        check_clients = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check_clients)
        self.assertEqual(check_clients.OWNERS['openssl'], frozenset({'asf/ghapp.py'}))


if __name__ == '__main__':
    unittest.main()
