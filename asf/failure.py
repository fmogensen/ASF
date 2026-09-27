"""asf.failure — the class of a failed launch or session, and what the factory may do about it.

A missing credential is not a flaky transport, and retrying it is a promise that something might
change. Four classes, one policy each:

* ``credential`` — a login, a token, a permission or a model label the operator must change.
  Zero relaunches, ever. One needs-operator line naming the product, and the lane is stopped
  until the credential reads (:mod:`asf.workers.stops`).
* ``quota`` — a window is spent. Zero relaunches on this account; the account is stopped until
  its reset and the item goes to another of the pool (:mod:`asf.workers.headroom`). No round.
* ``transport`` — the network dropped. Exactly one relaunch, on the original brief, no round;
  the second in a row is the operator's.
* ``work`` — everything else, and everything unrecognised. The correction, the hold and the
  round the factory writes today.

Two sources, both named by the card: the launcher's own result text (a ``SpawnError``, an
``AuthEnvError``, the text a worktree setup printed) and the session's last beat
(:func:`asf.workers.runtime.last_beat`). Nothing here opens a file, runs git, knows a product or
imports anything of ``asf``: it is a table, three patterns and a policy.

``why`` is always a pattern *name*, never matched text — a session log is not masked and this
string travels into the record (F-0054 D3).
"""
import re

CREDENTIAL, QUOTA, TRANSPORT, WORK = 'credential', 'quota', 'transport', 'work'
CLASSES = (CREDENTIAL, QUOTA, TRANSPORT, WORK)

#: Relaunches each class may have. ``None`` (work): the existing correction path decides
#: (one cold retry, then the B-0062 hold).
RETRIES = {CREDENTIAL: 0, QUOTA: 0, TRANSPORT: 1, WORK: None}

#: An expired login, a rejected key, a model label that does not exist, and an ``auth_env`` file
#: that is missing, empty or unreadable.
AUTH_RE = re.compile(r'invalid api key|please run /login|authentication[_ ]error|oauth token', re.I)
#: A model label the account does not have, or that does not exist.
MODEL_RE = re.compile(r'issue with the selected model|model .* (?:not found|does not exist)', re.I)
#: The permission signature :data:`asf.workers.runtime.FAILURE_SIGNATURES`' ``permission`` entry
#: carries today, byte for byte — kept whole for that table, and **not** folded into
#: :data:`CREDENTIAL_RE`, whose ``permission denied`` half is what a failing test command, a
#: `chmod`, a read-only file or a docker socket print (F-0054 PD7): including it would class an
#: ordinary work failure as ``credential`` and hand it zero relaunches.
PERMISSION_RE = re.compile(r'permission denied|not permitted to use', re.I)
#: An account's or a product's ``auth_env`` file the launcher could not read.
AUTH_ENV_RE = re.compile(
    r'auth_env \S+ file .* (?:does not exist|is empty|cannot be read)', re.I)
#: An expired login, a rejected key, a model label that does not exist, a permission the account
#: does not hold, and an ``auth_env`` file that is missing, empty or unreadable.
CREDENTIAL_RE = re.compile(
    r'invalid api key|please run /login|authentication[_ ]error|oauth token|'
    r'not permitted to use|issue with the selected model|model .* (?:not found|does not exist)|'
    r'auth_env \S+ file .* (?:does not exist|is empty|cannot be read)', re.I)
#: A spent window: the session limit, the usage limit, a rate limit, the API's own 429.
#: :data:`asf.workers.headroom.LIMIT_RE` is this pattern.
QUOTA_RE = re.compile(r"hit your (?:\w+ )?limit|(?:session|usage|weekly) limit|rate[ _]limit|"
                      r"API Error: 429|quota (?:exceeded|exhausted)", re.I)
#: A dropped connection. :data:`asf.workers.lifecycle.NETWORK_RE` is this pattern.
TRANSPORT_RE = re.compile(r'could not resolve host|connection (?:reset|refused|timed out|closed)|'
                          r'network is unreachable|unable to access|operation timed out|early eof|'
                          r'remote end hung up|ssl_error|gnutls', re.I)

#: Every signature name the tree writes → its class. A name absent from this table classifies by
#: text, then by the default: a signature this table does not know is never silently ``work`` —
#: :func:`of_signature` returns None and the caller's text is read.
SIGNATURE_CLASS = {
    'auth': CREDENTIAL, 'permission': CREDENTIAL, 'unknown model': CREDENTIAL,
    'quota-exhausted': QUOTA,
    'network error': TRANSPORT,
    'hook refused': WORK, 'not pushed': WORK, 'unpushed work': WORK, 'empty branch': WORK,
    'token cap': WORK, 'run cap': WORK, 'dead pid': WORK, 'other': WORK,
}

#: The scope an :class:`asf.workers.runtime.AuthEnvError` names when the credential is the
#: account's own, never the product's (`asf/workers/runtime.py:337`).
ACCOUNT_SCOPE_RE = re.compile(r'worker account \S+', re.I)


def of_signature(name):
    """The class of a signature name (:data:`SIGNATURE_CLASS`), or None for one it does not
    know — including ``''``, ``finished`` and ``None``."""
    if not name:
        return None
    return SIGNATURE_CLASS.get(name.strip())


def of_text(text):
    """The class the raw text carries, or None: credential, then quota, then transport. The order
    is the ranking, not an accident — an authentication error is never a blip, and a 429 is a
    window and not a dropped connection."""
    text = text or ''
    if CREDENTIAL_RE.search(text):
        return CREDENTIAL
    if QUOTA_RE.search(text):
        return QUOTA
    if TRANSPORT_RE.search(text):
        return TRANSPORT
    return None


def classify(signature=None, text='', default=WORK):
    """``(cls, why)``. The signature's class first (``why``: ``signature <name>``), else the
    text's (``why``: ``text: <cls>``), else ``default`` (``why``: ``no signature``). Pure."""
    cls = of_signature(signature)
    if cls is not None:
        return cls, f'signature {signature}'
    cls = of_text(text)
    if cls is not None:
        return cls, f'text: {cls}'
    return default, 'no signature'


def retries(cls):
    """:data:`RETRIES` — how many relaunches ``cls`` may have; None: the caller's own rule."""
    return RETRIES.get(cls)


def may_relaunch(cls, tried):
    """True when a failure of ``cls`` that has already been relaunched ``tried`` times may be
    relaunched again. ``work`` is always True: its own path counts its tries."""
    limit = retries(cls)
    return True if limit is None else tried < limit


def label(cls, why):
    """``class credential (signature auth)`` — the suffix every failure line prints, so the
    stall check, the cap line and the launch line cannot word it three ways."""
    return f'class {cls} ({why})'


def operator_text(cls, product, item, job, lane='', why=''):
    """The needs-operator sentence, naming the product — and a lane only as ``lane-N``, never an
    account (F-0054 D3). No path, no value, no matched text: the console line adds the clearing
    command beside this, and the event carries only this."""
    lane_part = f'{lane} cannot start a session — ' if lane else ''
    return f'NEEDS OPERATOR: {product}: {lane_part}{label(cls, why)}; {item} ({job}) was not relaunched'


def account_scoped(text):
    """True when ``text`` carries the ``worker account <name>`` scope an :class:`AuthEnvError`'s
    message already carries (`asf/workers/runtime.py:337`), false for the product's
    ``conventions.auth_env`` scope. The text-only fallback; a caller holding the error itself
    reads its own ``scope`` field instead."""
    return bool(ACCOUNT_SCOPE_RE.search(text or ''))
