"""asf.security.alerts — the code host's own alarms, read as ASF's own rule (R-0009): open
secret-scanning alerts and open Dependabot alerts, cached only so staleness is detectable
(``conventions.security.alerts.max_age_h``). :func:`violations` never passes on a feed it could
not read (D9) — the cache buys silence while it is fresh, and a stale or absent cache is itself
a violation.

No alert's *value* is ever read, cached or printed (D11, P20): the secret-scanning API's own
``secret`` field is never named in a ``--jq`` selection, the same rule :mod:`asf.redact` applies
to a commit applied here to someone else's API.
"""
import datetime
import json
import os
import subprocess

from asf import env
from asf.security import gh_env

#: How long ``gh api`` gets before the call counts as failed.
TIMEOUT_S = 30

_TS_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


class Host:
    """The alert source. A test passes a fake; :class:`GitHubHost` is the only real one."""

    def secrets(self):
        """Every open secret-scanning alert: ``[{number, state, kind, url, created_at,
        locations_count}]``."""
        raise NotImplementedError

    def dependencies(self):
        """Every open Dependabot alert: ``[{number, state, severity, package, ecosystem, url,
        created_at}]``."""
        raise NotImplementedError


class HostError(Exception):
    pass


class GitHubHost(Host):
    """``gh api`` against the product's own repo, the environment :func:`asf.security.gh_env`
    builds. Never selects the secret-scanning alert's own ``secret`` field (P20)."""

    #: Kept fields only — never ``secret``, the matched text.
    _SECRETS_JQ = ('.[] | {number, state, kind: .secret_type, url: .html_url, created_at, '
                   'locations_count: (.locations_count // 0)}')
    _DEPS_JQ = ('.[] | {number, state, severity: .security_advisory.severity, '
                'package: .dependency.package.name, ecosystem: .dependency.package.ecosystem, '
                'url: .html_url, created_at}')

    def __init__(self, product, run=None):
        self.product = product
        self.slug = product.repo_slug
        self._run = run or subprocess.run
        self._env = None

    def _api(self, path, jq):
        if not self.slug:
            raise HostError('the product has no repo_slug')
        if self._env is None:
            self._env = gh_env(self.product)
        args = ['gh', 'api', f'repos/{self.slug}/{path}', '--jq', jq]
        try:
            p = self._run(args, capture_output=True, text=True, timeout=TIMEOUT_S, env=self._env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HostError(f'gh api {path}: {e}') from e
        if p.returncode != 0:
            lines = (p.stderr or p.stdout or '').strip().splitlines()
            raise HostError(f"gh api {path}: {lines[-1] if lines else 'failed'}")
        try:
            return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        except json.JSONDecodeError as e:
            raise HostError(f'gh api {path}: bad output ({e})') from e

    def secrets(self):
        return self._api('secret-scanning/alerts?state=open&per_page=100', self._SECRETS_JQ)

    def dependencies(self):
        return self._api('dependabot/alerts?state=open&per_page=100', self._DEPS_JQ)


def cache_path(product):
    """``state/<product>/security/alerts.json`` — the directory from :func:`asf.env.state_dir`,
    no path literal beyond ``security/alerts.json``."""
    return os.path.join(env.state_dir(product), 'security', 'alerts.json')


def _read_cache(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _age_h(ts, now):
    try:
        at = datetime.datetime.strptime(ts, _TS_FORMAT).replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    return (now - at).total_seconds() / 3600.0


def read(product, host=None, now=None):
    """``(feeds, age_h, why)``: the live feeds — ``{secrets, dependencies}`` — when both can be
    read, the cache rewritten under them; else the cache (``age_h`` its age in hours, ``why`` the
    live read's failure), or, with no cache either, ``(None, None, why)``."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    host = host or GitHubHost(product)
    path = cache_path(product)
    try:
        secrets = host.secrets()
        dependencies = host.dependencies()
    except HostError as e:
        cache = _read_cache(path)
        if cache is None:
            return None, None, str(e)
        feeds = {'secrets': cache.get('secrets') or [], 'dependencies': cache.get('dependencies') or []}
        return feeds, _age_h(cache.get('ts'), now), str(e)
    feeds = {'secrets': secrets, 'dependencies': dependencies}
    _write_cache(path, {'ts': now.strftime(_TS_FORMAT), 'secrets': secrets, 'dependencies': dependencies})
    return feeds, 0.0, None


def violations(product, host=None, now=None):
    """The check's lines (R-0009): one per open secret alert (``sev=S1``), one per open
    dependency alert (``sev=S2``), and — only when the feeds could not be read and the last good
    read is stale past ``security.alerts.max_age_h`` — one ``sig=feed-stale`` line naming the
    reason. A failed read behind a still-fresh cache reports nothing: the cache's alerts are
    already filed, and reporting them again would refile them."""
    max_age_h = product.conventions.security_alerts()['max_age_h']
    feeds, age_h, why = read(product, host=host, now=now)
    if why is not None:
        if age_h is not None and age_h <= max_age_h:
            return []
        if age_h is None:
            return [f'R-0009 the alert feeds have never been read ({why}) sev=S2 sig=feed-stale']
        return [f'R-0009 the alert feeds have not been read for {int(age_h)}h ({why}) sev=S2 sig=feed-stale']

    slug = product.repo_slug or ''
    lines = []
    for a in feeds['secrets']:
        lines.append(
            f"R-0009 secret alert open {a.get('kind')} {slug}#{a['number']} since {a.get('created_at')} "
            f"sev=S1 sig=secret-{slug}-{a['number']}")
    for a in feeds['dependencies']:
        lines.append(
            f"R-0009 dependency alert open {a.get('severity')} {a.get('package')} ({a.get('ecosystem')}) "
            f"{slug}#{a['number']} since {a.get('created_at')} sev=S2 sig=dep-{slug}-{a['number']}")
    return lines
