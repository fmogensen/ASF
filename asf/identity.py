"""asf.identity — who a session commits as, and who it must not (F-0116)."""
from asf import env

#: What a worker session authors and signs off as when no account overrides it. Not an account
#: name (asf.redact builds its name patterns from those), not a person, not a host: the shape
#: asf.merge_queue.IDENT and asf.workers.spawn already mint for the factory's own commits.
AGENT_NAME = 'asf worker'
AGENT_EMAIL = 'asf-worker@localhost'


def agent_identity(acct=None):
    """``(name, email)`` a session of ``acct`` commits as: the account's ``identity:`` when it
    names one, else the defaults. A half the account leaves empty falls back to that half's
    default rather than to empty — an author with a name and no e-mail is a commit git will
    refuse, and a config that got halfway is not a reason to strand a wave. Never the operator's,
    and never the account's own ``name`` (D2/P6)."""
    identity = getattr(acct, 'identity', None) if acct is not None else None
    name = identity.get('name') if isinstance(identity, dict) else None
    email = identity.get('email') if isinstance(identity, dict) else None
    return (name if name else AGENT_NAME, email if email else AGENT_EMAIL)


def operator_identity(cfg=None, operator_home=None):
    """``(name, email)`` the operator commits as: ``operator:`` in the shared config when it
    names one, else the operator's global ``user.name``/``user.email``
    (:func:`asf.workers.runtime.operator_git_identity`) — which is the identity a session was
    given before this card. Either half may be ``''``; an empty half matches nothing."""
    if cfg is None:
        cfg = env.load_config()
    operator = env.operator_config(cfg)
    if operator is not None:
        name = operator.get('name')
        email = operator.get('email')
        return (name if isinstance(name, str) else '', email if isinstance(email, str) else '')
    from asf.workers import runtime  # local: runtime imports this module
    return runtime.operator_git_identity(operator_home)


def git_config_pairs(acct=None):
    """``[('user.name', …), ('user.email', …)]`` for :func:`asf.workers.runtime.build_env`'s
    ``git_config`` channel. A pair whose value is empty is not emitted: ``GIT_CONFIG_VALUE_n=''``
    sets the key to empty, which is worse than not setting it."""
    name, email = agent_identity(acct)
    pairs = []
    if name:
        pairs.append(('user.name', name))
    if email:
        pairs.append(('user.email', email))
    return pairs
