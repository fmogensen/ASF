"""asf.probe.config — the ``probe:`` block (F-0051 §2.1): validated, defaulted, and documented.

Unset, the whole Feature is off: :func:`load` returns ``None`` and nothing downstream of it ever
runs — the same "unset means nothing is checked" rule ``customer_content`` follows.
"""
import dataclasses
import re
import types

#: ``probe.<key>``'s declared shape — the nested-field map ``asf.env.NESTED_FIELDS['probe']``
#: reads, so an unknown key is ``probe.<key> is not a field`` from the validator that already
#: says that for every other section.
_MAP, _LIST, _STR = 'a map', 'a list', 'a scalar'
PROBE_FIELDS = {
    'workflow': _STR, 'role': _STR, 'identity': _STR, 'secrets': _MAP, 'mailbox': _MAP,
    'journeys': _LIST, 'origins': _LIST, 'artifact': _STR, 'timeout': _STR,
}

#: the two secrets the job reads; ASF holds neither value, only the repository secret's name.
SECRET_KEYS = ('identity', 'mailbox')
#: the mailbox rule's required keys (``max_age_s`` is optional, §2.4).
MAILBOX_REQUIRED = ('from', 'subject', 'code')
#: a repository secret's name — the same shape ``asf.workers.cloud.SECRET_RE`` already checks.
_SECRET_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_URL_RE = re.compile(r'^https://\S+$')
_TIMEOUT_RE = re.compile(r'^\d+[smh]$')


@dataclasses.dataclass(frozen=True)
class Probe:
    """One product's ``probe:`` block, loaded (:func:`load`). ``secrets`` and ``mailbox`` are
    read-only mappings; ``journeys`` and ``origins`` are tuples — every field here is as
    immutable as the dataclass itself."""
    workflow: str
    role: str
    identity: str
    secrets: 'types.MappingProxyType'
    mailbox: 'types.MappingProxyType'
    journeys: tuple
    origins: tuple
    artifact: str
    timeout: str


def load(product):
    """``None`` for a product with no ``probe:`` block — the whole Feature is off. Else a
    :class:`Probe`, ``origins`` defaulting to ``(product.app_host,)`` when the block names
    none."""
    block = product._get('probe') if hasattr(product, '_get') else None
    if not isinstance(block, dict):
        return None
    origins = block.get('origins')
    if origins:
        origins = tuple(origins)
    else:
        app_host = getattr(product, 'app_host', None)
        origins = (app_host,) if app_host else ()
    secrets = types.MappingProxyType(dict(block.get('secrets') or {}))
    mailbox = types.MappingProxyType(dict(block.get('mailbox') or {}))
    return Probe(
        workflow=block.get('workflow'),
        role=block.get('role'),
        identity=block.get('identity'),
        secrets=secrets,
        mailbox=mailbox,
        journeys=tuple(block.get('journeys') or ()),
        origins=origins,
        artifact=block.get('artifact'),
        timeout=block.get('timeout'),
    )


def config_problems(block, app_host=None, pool=()):
    """``[(dotted key, problem)]`` for a ``probe:`` block the loader cannot read — checked on
    every product load (:func:`asf.env.validate_product_text`), the shape ``ci.pool`` and
    ``ci.queue`` already have.

    ``app_host`` is the product's own ``app_host`` (a ``probe:`` block with neither ``origins``
    nor an ``app_host`` names nothing to probe); ``pool`` is the role names a declared
    ``ci.pool`` runner carries (:func:`asf.ci_pool.roles`) — the probe block alone cannot see
    either, so both are the caller's to supply."""
    if not isinstance(block, dict):
        return [] if block is None else [('probe', f'must be a map, not {block!r}')]
    out = []
    if not block.get('workflow'):
        out.append(('probe.workflow', 'is required'))
    role = block.get('role')
    if role is not None and role not in pool:
        out.append(('probe.role', f'{role!r} is not a role any ci.pool runner carries'))
    out.extend(_secrets_problems(block.get('secrets')))
    out.extend(_mailbox_problems(block.get('mailbox')))
    out.extend(_journeys_problems(block.get('journeys')))
    out.extend(_origins_problems(block.get('origins')))
    timeout = block.get('timeout')
    if timeout is not None and not (isinstance(timeout, str) and _TIMEOUT_RE.match(timeout)):
        out.append(('probe.timeout', f'must be <n>s|m|h, not {timeout!r}'))
    if not block.get('origins') and not app_host:
        out.append(('probe.origins', 'there is nothing to probe: no origins and no app_host'))
    return out


def _secrets_problems(secrets):
    if not isinstance(secrets, dict):
        return [('probe.secrets', f'must be a map {{identity, mailbox}}, not {secrets!r}')]
    out = []
    for key in SECRET_KEYS:
        v = secrets.get(key)
        if not v:
            out.append((f'probe.secrets.{key}', 'is required'))
        elif not isinstance(v, str) or not _SECRET_RE.match(v):
            out.append((f'probe.secrets.{key}', f'must be a repo secret name, not {v!r}'))
    return out


def _mailbox_problems(mailbox):
    if not isinstance(mailbox, dict):
        return [('probe.mailbox', f'must be a map {{from, subject, code, max_age_s}}, not {mailbox!r}')]
    out = []
    for key in MAILBOX_REQUIRED:
        if not mailbox.get(key):
            out.append((f'probe.mailbox.{key}', 'is required'))
    code = mailbox.get('code')
    if code:
        try:
            compiled = re.compile(code)
        except re.error:
            out.append(('probe.mailbox.code', f'does not compile as a regex: {code!r}'))
        else:
            if compiled.groups < 1:
                out.append(('probe.mailbox.code', 'has no capturing group'))
    max_age = mailbox.get('max_age_s')
    if max_age is not None and (isinstance(max_age, bool) or not isinstance(max_age, int)
                                 or max_age <= 0):
        out.append(('probe.mailbox.max_age_s', f'must be a positive integer, not {max_age!r}'))
    return out


def _journeys_problems(journeys):
    if journeys is None:
        return []
    if not isinstance(journeys, list) or not all(isinstance(j, str) and j.strip() for j in journeys):
        return [('probe.journeys', f'must be a list of non-empty strings, not {journeys!r}')]
    return []


def _origins_problems(origins):
    if origins is None:
        return []
    if not isinstance(origins, list):
        return [('probe.origins', f'must be a list of https:// URLs, not {origins!r}')]
    out = []
    for i, o in enumerate(origins):
        if not isinstance(o, str) or not _URL_RE.match(o):
            out.append((f'probe.origins[{i}]', f'must be an https:// URL, not {o!r}'))
    return out
