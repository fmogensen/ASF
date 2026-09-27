"""asf.credentials — a signed-in tool as a rule: is a credential's session still valid, and when
does it expire.

A **provider** is one operator-configured, read-only probe (``config.yaml: credentials.providers
.<name>``): a command that exits 0 when the session is valid, 1 when it is not, and anything else
(or a timeout) when the probe itself is broken — the same contract
:mod:`asf.rules.rules` already runs every check script under. A probe's own stdout and stderr are
never printed, logged or returned raw: only the last non-empty line survives, scrubbed of
anything secret-shaped and truncated, and that is the only thing this module ever keeps of what a
probe said (:func:`run_probe`).

Nothing in this module has a caller outside its own tests: it is the surface the credential
command, the daily part, the doctor's row and the installer are built against.
"""
import dataclasses
import datetime
import os
import re
import shlex
import subprocess

from asf import env, hermetic, redact
from asf.workers import pool

#: how long one probe may run before it counts as broken; ``$ASF_CREDENTIAL_PROBE_TIMEOUT``
#: overrides it, in the same shape ``asf/rules/rules.py``'s own ``_timeout`` uses — a bad value
#: falls back to the default rather than raising at import.
_DEFAULT_PROBE_TIMEOUT_S = 20
DEFAULT_WINDOW_DAYS = 3
DEFAULT_PROBE_EVERY = '1h'
#: how much of a probe's own last line is ever kept
DETAIL_MAX = 160


def _timeout():
    try:
        return max(1, int(os.environ.get('ASF_CREDENTIAL_PROBE_TIMEOUT') or _DEFAULT_PROBE_TIMEOUT_S))
    except ValueError:
        return _DEFAULT_PROBE_TIMEOUT_S


PROBE_TIMEOUT_S = _timeout()

_UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
_DURATION_RE = re.compile(r'^\s*(\d+)\s*([smhd])\s*$', re.IGNORECASE)


def _seconds(value):
    """``<n>s|m|h|d`` → seconds, or ``None`` when ``value`` does not match that shape."""
    if not isinstance(value, str):
        return None
    m = _DURATION_RE.match(value)
    if not m:
        return None
    n, unit = m.groups()
    return int(n) * _UNIT_SECONDS[unit.lower()]


@dataclasses.dataclass(frozen=True)
class Provider:
    name: str
    probe: str
    renew: str
    account: str = ''  # the worker account whose environment the probe runs under (D5)
    window_days: int = DEFAULT_WINDOW_DAYS


#: a probe's own contract, mirrored from the rule contract (D2)
STATES = ('valid', 'invalid', 'broken')
STATE_VALID, STATE_INVALID, STATE_BROKEN = STATES


@dataclasses.dataclass(frozen=True)
class Result:
    provider: str
    state: str                # one of STATES
    expires: str = ''         # ISO-8601, '' unknown, 'never' no expiry
    detail: str = ''          # the probe's own last line, scrubbed and truncated — never a token
    probed_at: str = ''


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# ---- configuration -----------------------------------------------------------------------

def providers(cfg):
    """``{name: Provider}`` from ``config.yaml``'s ``credentials.providers`` map. A missing or
    misshapen ``credentials:`` section, or a provider missing its ``probe:`` or ``renew:``, is
    silently left out — :func:`config_problems` is where that is reported, not here."""
    section = (cfg or {}).get('credentials')
    if not isinstance(section, dict):
        return {}
    raw = section.get('providers')
    if not isinstance(raw, dict):
        return {}
    default_window = section.get('window_days')
    if not _positive_int(default_window):
        default_window = DEFAULT_WINDOW_DAYS
    out = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or not entry.get('probe') or not entry.get('renew'):
            continue
        window = entry.get('window_days')
        if not _positive_int(window):
            window = default_window
        out[str(name)] = Provider(name=str(name), probe=entry['probe'], renew=entry['renew'],
                                  account=entry.get('account') or '', window_days=window)
    return out


def probe_every(cfg):
    """How stale a cached probe may be, in seconds (D6): ``credentials.probe_every`` through
    :func:`_seconds`; an unparseable value falls back to :data:`DEFAULT_PROBE_EVERY` (named by
    :func:`config_problems`, not raised here)."""
    section = (cfg or {}).get('credentials')
    raw = section.get('probe_every', DEFAULT_PROBE_EVERY) if isinstance(section, dict) else DEFAULT_PROBE_EVERY
    seconds = _seconds(raw)
    return seconds if seconds is not None else _seconds(DEFAULT_PROBE_EVERY)


def config_problems(cfg):
    """``[(dotted key, problem)]`` — every problem in ``config.yaml``'s ``credentials:`` section:
    a provider with no ``probe:``, one with no ``renew:``, a ``window_days`` that is not a
    positive integer, a ``probe_every`` that is not ``<n>s|m|h|d``, an ``account:`` no
    ``worker_pool.accounts`` entry names, and a ``renew:`` that carries a secret-shaped value.
    Sorted by dotted key, so the doctor's row is stable."""
    section = (cfg or {}).get('credentials')
    if not isinstance(section, dict):
        return []
    problems = []
    default_window = section.get('window_days')
    if default_window is not None and not _positive_int(default_window):
        problems.append(('credentials.window_days', f'must be a positive integer, not {default_window!r}'))
    raw_probe_every = section.get('probe_every', DEFAULT_PROBE_EVERY)
    if _seconds(raw_probe_every) is None:
        problems.append(('credentials.probe_every', f"must be '<n>s|m|h|d', not {raw_probe_every!r}"))
    raw_providers = section.get('providers')
    if raw_providers is None:
        raw_providers = {}
    elif not isinstance(raw_providers, dict):
        problems.append(('credentials.providers', f'must be a map of provider settings, not {raw_providers!r}'))
        raw_providers = {}
    account_names = {a.name for a in pool.accounts_from_config(cfg)}
    scrub_patterns = redact.default_patterns(None)
    for name, entry in raw_providers.items():
        prefix = f'credentials.providers.{name}'
        if not isinstance(entry, dict):
            problems.append((prefix, f'must be a map, not {entry!r}'))
            continue
        if not entry.get('probe'):
            problems.append((f'{prefix}.probe', 'is required'))
        if not entry.get('renew'):
            problems.append((f'{prefix}.renew', 'is required'))
        window = entry.get('window_days')
        if window is not None and not _positive_int(window):
            problems.append((f'{prefix}.window_days', f'must be a positive integer, not {window!r}'))
        account = entry.get('account')
        if account and account not in account_names:
            problems.append((f'{prefix}.account', f'{account!r} is not a worker_pool account'))
        renew = entry.get('renew')
        if isinstance(renew, str) and renew and redact.scrub(renew, scrub_patterns) != renew:
            problems.append((f'{prefix}.renew',
                             'carries a secret-shaped value — put the secret in a file'))
    return sorted(problems)


def product_problems(value):
    """``[(dotted key, problem)]`` for a product file's ``credentials:`` value: not a list, an
    entry that is not a non-empty string, or a duplicate name. ``None`` is no problem — a product
    that names none is a product with nothing to check."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [('credentials', f'must be a list of provider names, not {value!r}')]
    problems = []
    seen = set()
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            problems.append(('credentials', f'must be a list of non-empty names, and {entry!r} is not one'))
        elif entry in seen:
            problems.append(('credentials', f'names {entry!r} more than once'))
        else:
            seen.add(entry)
    return problems


def for_product(product, cfg):
    """The :class:`Provider` objects ``product`` names, in the product's own order. A name
    ``credentials.providers`` does not define raises :class:`asf.env.ConfigError`."""
    by_name = providers(cfg)
    out = []
    for name in product.credentials:
        if name not in by_name:
            raise env.ConfigError(
                f'product.yaml credentials: {name} is not a provider in config.yaml credentials.providers')
        out.append(by_name[name])
    return out


def probe_env(provider, cfg, product):
    """The environment a probe runs under (D5): the worker account ``provider.account`` names,
    built exactly as a spawned session's own (:func:`asf.workers.runtime.build_env`, following
    ``asf/doctor.py``'s synthetic ``Job`` for ``worker push auth``), or
    :func:`asf.hermetic.worker_base` when the provider names none. Either way
    ``GIT_TERMINAL_PROMPT=0`` is set last, so a credential the session cannot reach fails at once
    instead of hanging on a prompt. :class:`asf.workers.runtime.AuthEnvError` propagates."""
    from asf.workers import runtime
    if provider.account:
        acct = next((a for a in pool.accounts_from_config(cfg) if a.name == provider.account), None)
        if acct is None:
            raise runtime.AuthEnvError(
                f'credentials.providers.{provider.name}.account: {provider.account!r} is not a '
                'worker_pool account')
        job = runtime.Job(product.name, 'credential-probe', product.repo_dir, None, None,
                          account=acct, product_auth_env=env.product_auth_env(product))
        job_env = runtime.build_env(job)
    else:
        job_env = hermetic.worker_base()
    job_env['GIT_TERMINAL_PROMPT'] = '0'
    return job_env


# ---- the probe and its verdict -----------------------------------------------------------

_EXPIRES_RE = re.compile(
    r'^\s*expires[:=]?\s+(never|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s*$',
    re.IGNORECASE)


def _last_nonempty(text):
    for line in reversed((text or '').splitlines()):
        if line.strip():
            return line
    return ''


def _expires(line):
    """The expiry parsed out of a probe's last stdout line: ``never``, an ISO-8601 timestamp
    re-rendered in UTC, or ``''`` when the line does not match (D3) — parsed from the line
    *before* it is scrubbed, so a probe that prints a token beside its expiry still contributes a
    timestamp."""
    m = _EXPIRES_RE.match(line or '')
    if not m:
        return ''
    value = m.group(1)
    if value.lower() == 'never':
        return 'never'
    iso = value[:-1] + '+00:00' if value[-1] in 'zZ' else value
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return ''
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _detail(line):
    """The scrub applied before the truncation, so a secret cut in half is still redacted whole
    (2.4) — the only place a probe's own output is ever kept."""
    scrubbed = redact.scrub(line or '', redact.default_patterns(None))
    if len(scrubbed) <= DETAIL_MAX:
        return scrubbed
    return scrubbed[:DETAIL_MAX - 1] + '…'


def run_probe(provider, env_map, now, run=subprocess.run):
    """One read-only probe: ``rc 0`` → ``valid``, ``rc 1`` → ``invalid``, any other ``rc``,
    ``OSError``, :class:`subprocess.TimeoutExpired` or
    :class:`asf.workers.runtime.AuthEnvError` → ``broken``. The command is
    ``provider.probe`` split with :func:`shlex.split`, run under ``env_map`` with a
    :data:`PROBE_TIMEOUT_S` timeout. Nothing of the probe's raw stdout or stderr survives this
    function but its last non-empty line, scrubbed and truncated (2.4) — never a token."""
    from asf.workers import runtime
    try:
        proc = run(shlex.split(provider.probe), env=env_map, capture_output=True, text=True,
                  timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired, runtime.AuthEnvError) as e:
        return Result(provider=provider.name, state=STATE_BROKEN, detail=_detail(str(e)),
                     probed_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'))
    stdout_line = _last_nonempty(proc.stdout)
    detail_line = stdout_line or _last_nonempty(proc.stderr)
    if proc.returncode == 0:
        state = STATE_VALID
    elif proc.returncode == 1:
        state = STATE_INVALID
    else:
        state = STATE_BROKEN
    return Result(provider=provider.name, state=state, expires=_expires(stdout_line),
                 detail=_detail(detail_line), probed_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'))


def days_left(result, now):
    """``float | None``: ``None`` when ``result.expires`` is ``''`` or ``'never'``."""
    if result.expires in ('', 'never'):
        return None
    try:
        expires = datetime.datetime.strptime(result.expires, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return None
    expires = expires.replace(tzinfo=datetime.timezone.utc)
    return (expires - now).total_seconds() / 86400.0


def verdict(result, now, window_days):
    """``'ok' | 'expiring' | 'invalid' | 'unknown' | 'broken'``. ``expiring`` only when the state
    is ``valid`` *and* ``days_left`` is not ``None`` *and* it is under ``window_days``; an
    unknown expiry is ``unknown`` and never ``expiring`` (D3)."""
    if result.state == STATE_BROKEN:
        return 'broken'
    if result.state == STATE_INVALID:
        return 'invalid'
    left = days_left(result, now)
    if left is None:
        return 'unknown'
    return 'expiring' if left < window_days else 'ok'


def bad(results, cfg=None, providers_by_name=None, now=None):
    """The results whose verdict is ``invalid`` or ``expiring``, in provider order — what raises
    the operator line."""
    by_name = providers_by_name if providers_by_name is not None else providers(cfg)
    now = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    out = []
    for result in results:
        provider = by_name.get(result.provider)
        window = provider.window_days if provider is not None else DEFAULT_WINDOW_DAYS
        if verdict(result, now, window) in ('invalid', 'expiring'):
            out.append(result)
    return out
