"""asf.ghapp — the factory's own GitHub identity: a GitHub App installation token.

Every other GitHub call the factory makes is made as the operator, on one shared personal
token's 5,000/hour bucket. A GitHub App installation has its own bucket, scaled to the
installation, and this module is the only place that mints one: it reads the App an operator
declared in the product file (``conventions.github_app``), signs a JWT with ``openssl`` (the
package is stdlib-only — no JWT or crypto library is a dependency), exchanges that JWT for an
installation access token, caches it with its expiry under ``env.state_dir(product)``, and
refreshes it once the cache is within :data:`REFRESH_MARGIN_S` of expiring.

:func:`refresh` is the only entry a caller outside this module needs, and it never raises except
:class:`asf.gh_limit.RateLimited` — a rate-limited mint latches this process exactly as a refused
``gh`` call would, which is why the one ``except`` in this module names ``Exception`` and never
``BaseException`` (``RateLimited`` is one on purpose). A failure of any other kind returns
``('', <why>)``, leaves a previously cached token file exactly as it was, and records the reason
in the meta file; nothing this module returns, prints or writes ever carries the token, the JWT,
or a line of the key file (the key's path is the most any reason or log line ever names).
"""
import base64
import calendar
import collections
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from asf import env, gh_limit, mutation_guard

#: the environment variable the minted token is carried under (``env.product_auth_env``, Task 2;
#: it is also the variable that gives a session's git its HTTPS credential — ``runtime.py:363``)
TOKEN_VAR = 'GH_TOKEN'

#: a cached token closer to its expiry than this is refreshed rather than reused
REFRESH_MARGIN_S = 900

#: seconds the signer or one HTTP call may run
_SIGN_TIMEOUT_S = 30
_HTTP_TIMEOUT_S = 30

App = collections.namedtuple('App', 'app_id key_file installation_id')


# ---------------------------------------------------------------------- declaration --

def settings(product):
    """The product's declared :class:`App`, or ``None`` when ``conventions.github_app`` is
    unset, not a map, or missing its ``app_id``/``key_file``. ``app_id`` is kept as the string
    the JWT's ``iss`` needs (it is commonly a bare number in the yaml, which parses as an
    ``int``); ``key_file`` is expanded from a leading ``~``; ``installation_id`` is an ``int``
    when declared, else ``None``."""
    if product is None:
        return None
    conv = product.conventions if hasattr(product, 'conventions') else None
    raw = conv.get('github_app') if conv is not None else None
    if not isinstance(raw, dict):
        return None
    app_id = raw.get('app_id')
    if app_id is None or isinstance(app_id, (dict, list)) or not str(app_id).strip():
        return None
    key_file = raw.get('key_file')
    if not isinstance(key_file, str) or not key_file.strip():
        return None
    installation_id = raw.get('installation_id')
    if isinstance(installation_id, bool):
        installation_id = None
    try:
        installation_id = int(installation_id) if installation_id is not None else None
    except (TypeError, ValueError):
        installation_id = None
    return App(str(app_id).strip(), os.path.expanduser(key_file), installation_id)


def configured(product):
    """True when the product declares a usable App (:func:`settings`)."""
    return settings(product) is not None


def _state_name(product):
    return product.name if isinstance(product, env.Product) else (product or env.default_product_name())


def token_path(product):
    """``<ASF_HOME>/state/<product>/github-app.token`` — never through :func:`env.state_dir`
    (that makes the directory, and this is read on every pre-push redaction scan)."""
    return os.path.join(env.ASF_HOME, 'state', str(_state_name(product)), 'github-app.token')


def meta_path(product):
    """``<ASF_HOME>/state/<product>/github-app.json`` — ids, expiry and the last reason; never
    a secret."""
    return os.path.join(env.ASF_HOME, 'state', str(_state_name(product)), 'github-app.json')


def token(product):
    """The cached installation token, or ``''`` when there is none. No network."""
    path = token_path(product)
    try:
        if not (os.path.isfile(path) and os.path.getsize(path) > 0):
            return ''
        with open(path, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _read_meta(product):
    try:
        with open(meta_path(product), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def expires_at(product):
    """The cached token's ISO expiry from the meta file, or ``''`` when unknown. No network."""
    return str(_read_meta(product).get('expires_at') or '')


def _seconds_left(iso, now):
    if not iso:
        return None
    try:
        epoch = calendar.timegm(time.strptime(iso, '%Y-%m-%dT%H:%M:%SZ'))
    except (ValueError, TypeError):
        return None
    return epoch - now


# --------------------------------------------------------------------------- signing --

def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=')


def signing_input(app, now):
    """Two base64url segments joined by ``.``, neither padded: the JWT header, then claims
    ``{"iat": now-60, "exp": now+540, "iss": app_id}`` — measured off ``now``, never
    ``time.time()`` read inside."""
    now = int(now)
    header = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    claims = _b64url(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': app.app_id},
                                separators=(',', ':')).encode())
    return header + b'.' + claims


def sign(app, data, run=None):
    """``openssl dgst -sha256 -sign <key_file>``, ``data`` on stdin: ``(signature_bytes, '')``, or
    ``(None, why)`` naming the key path and carrying no key material (``P7``)."""
    try:
        p = (run or subprocess.run)(
            ['openssl', 'dgst', '-sha256', '-sign', app.key_file],
            input=data, capture_output=True, timeout=_SIGN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, f'openssl sign failed for {app.key_file}: timeout'
    except OSError as exc:
        return None, f'openssl sign failed for {app.key_file}: {exc}'
    if p.returncode != 0:
        err = p.stderr
        if isinstance(err, bytes):
            err = err.decode('utf-8', 'replace')
        first = next((ln.strip() for ln in (err or '').splitlines() if ln.strip()), '')
        return None, f'openssl sign failed for {app.key_file}' + (f': {first}' if first else '')
    return p.stdout, ''


def jwt(app, now, run=None):
    """``(token, '')`` or ``(None, why)`` — :func:`signing_input` signed by :func:`sign`."""
    data = signing_input(app, now)
    sig, why = sign(app, data, run=run)
    if sig is None:
        return None, why
    return (data + b'.' + _b64url(sig)).decode('ascii'), ''


# ------------------------------------------------------------------------------- http --

def _headers(bearer):
    return {
        'Authorization': f'Bearer {bearer}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'asf-ghapp',
    }


def _request(http, url, *, method='GET', headers=None):
    """One call through ``http`` (default :func:`urllib.request.urlopen`, the only
    ``urllib.request`` site in the package): ``(status, body_bytes)``, never raising for a
    non-2xx response — the caller decides."""
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    opener = http or urllib.request.urlopen
    try:
        resp = opener(req, timeout=_HTTP_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    with resp:
        return getattr(resp, 'status', None) or resp.getcode(), resp.read()


def _check_rate_limit(args, status, body):
    """A 403/429 (or any other non-2xx) body goes through :func:`gh_limit.inspect` before
    anything else is read from it — latches and raises :class:`gh_limit.RateLimited` on a
    rate-limit answer, same as a refused ``gh`` call."""
    if status and status >= 400:
        text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body or '')
        gh_limit.inspect(args, status, '', text)


def installation(app, slug, bearer, http=None):
    """``GET /repos/<slug>/installation`` → ``(installation_id, '')`` or ``(None, why)``."""
    status, body = _request(http, f'https://api.github.com/repos/{slug}/installation',
                            headers=_headers(bearer))
    _check_rate_limit(['ghapp', 'installation', slug], status, body)
    if not 200 <= status < 300:
        return None, f'installation lookup for {slug} failed: HTTP {status}'
    try:
        data = json.loads(body)
        return int(data['id']), ''
    except (ValueError, KeyError, TypeError):
        return None, f'installation lookup for {slug}: unparseable response'


# --------------------------------------------------------------------- mint / refresh --

def _iso(now):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))


def _write_token_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(path, 0o600)


def _write_meta(product, meta):
    with open(meta_path(product), 'w', encoding='utf-8') as f:
        json.dump(meta, f)


def _fail(product, app, slug, why, installation_id=None):
    """A failed mint: the previous token file is left exactly as it was; the reason is merged
    into the meta file (its ``expires_at``/``minted_at``, if any, survive); one log line."""
    meta = _read_meta(product)
    meta['app_id'] = app.app_id
    meta['slug'] = slug
    if installation_id is not None:
        meta['installation_id'] = installation_id
    meta['error'] = why
    _write_meta(product, meta)
    print(f'github app: {why}')
    return '', why


def _succeed(product, app, slug, installation_id, access_token, expires, now):
    _write_token_file(token_path(product), access_token)
    _write_meta(product, {
        'app_id': app.app_id,
        'installation_id': installation_id,
        'slug': slug,
        'minted_at': _iso(now),
        'expires_at': expires,
        'error': '',
    })
    print(f'github app: installation token minted for {slug} (app {app.app_id}, installation '
         f'{installation_id}), expires {expires}')
    return expires, ''


def mint(product, now=None, http=None, run=None):
    """Mint a fresh installation token unconditionally: ``jwt`` → ``installation`` (skipped when
    the product declares one, ``D2``) → ``POST /app/installations/<id>/access_tokens`` through
    the module's one ``urllib.request`` call. ``(expires_at, '')`` on success, ``('', why)`` on
    any other failure; raises :class:`gh_limit.RateLimited` and nothing else."""
    app = settings(product)
    slug = getattr(product, 'repo_slug', None) or ''
    if app is None:
        return '', 'no app configured'
    env.state_dir(product)  # the only call in this module that makes the directory (PD11)
    now = time.time() if now is None else now
    bearer, why = jwt(app, now, run=run)
    if bearer is None:
        return _fail(product, app, slug, why)
    installation_id = app.installation_id
    if installation_id is None:
        installation_id, why = installation(app, slug, bearer, http=http)
        if installation_id is None:
            return _fail(product, app, slug, why)
    status, body = _request(
        http, f'https://api.github.com/app/installations/{installation_id}/access_tokens',
        method='POST', headers=_headers(bearer))
    _check_rate_limit(['ghapp', 'mint', slug], status, body)
    if not 200 <= status < 300:
        return _fail(product, app, slug, f'mint for {slug} failed: HTTP {status}',
                     installation_id=installation_id)
    try:
        data = json.loads(body)
        access_token = str(data['token'])
        expires = str(data['expires_at'])
    except (ValueError, KeyError, TypeError):
        return _fail(product, app, slug, 'mint response unparseable',
                     installation_id=installation_id)
    return _succeed(product, app, slug, installation_id, access_token, expires, now)


def refresh(product, now=None, http=None, run=None):
    """The only entry a caller outside this module needs. No App declared →
    ``('', 'no app configured')``, nothing read or written. A cached token with more than
    :data:`REFRESH_MARGIN_S` left → ``(expires_at, '')``, no network. A dry run in progress →
    one ``would mint …`` line and ``('', 'dry run')`` (:mod:`asf.mutation_guard`, same contract
    every mutating ``gh`` call already honours). Otherwise :func:`mint`. Never raises except
    :class:`gh_limit.RateLimited` (``PD7``)."""
    app = settings(product)
    if app is None:
        return '', 'no app configured'
    now = time.time() if now is None else now
    cached = token(product)
    if cached:
        left = _seconds_left(expires_at(product), now)
        if left is not None and left > REFRESH_MARGIN_S:
            return expires_at(product), ''
    if mutation_guard.is_active():
        slug = getattr(product, 'repo_slug', None) or ''
        print(f'would mint an installation token for {slug}')
        return '', 'dry run'
    return mint(product, now=now, http=http, run=run)
