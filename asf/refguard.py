"""The one guard every factory ref write goes through: no push, force or delete made for a
factory branch may target the trunk or a protected ref.

A product's host may enforce no branch protection at all (a plan that answers 403 on it), so
the factory's own code is the only thing between a factory ref write and the trunk. Every such
write — the lane's ref pushes and API ref writes, its naming and sign-off repairs, a worker's
publish, retention's deletes, harvest's branch push — asks :func:`refusal` first. The trunk is
advanced only by the landing paths built for it (a fast-forward push after the gate), never by
a write aimed at a branch.

``conventions.protected_refs``: a list of branch names or globs (``release/*``); unset is
:data:`DEFAULT_PROTECTED_REFS`. The product's trunk is always protected, listed or not.
"""

import fnmatch
import sys

#: the refs protected when ``conventions.protected_refs`` is unset
DEFAULT_PROTECTED_REFS = ('main', 'master', 'release/*')


def branch_name(target):
    """``refs/heads/<b>``, ``+<sha>:refs/heads/<b>``, ``:<b>`` or ``<b>`` → ``<b>``."""
    target = str(target or '').strip()
    if ':' in target:
        target = target.rpartition(':')[2]
    target = target.lstrip('+')
    return target[len('refs/heads/'):] if target.startswith('refs/heads/') else target


def patterns(main=None, protected=None):
    """The protected names and globs: ``main`` plus ``protected`` (None: the defaults)."""
    pats = list(DEFAULT_PROTECTED_REFS if protected is None else protected)
    if main:
        pats.append(str(main))
    return [str(p).strip() for p in pats if str(p or '').strip()]


def listed(conv=None):
    """A product's ``conventions.protected_refs`` when it wrote a list, else None (the
    defaults)."""
    value = conv.get('protected_refs') if conv is not None and hasattr(conv, 'get') else None
    return list(value) if isinstance(value, (list, tuple)) else None


def refusal(target, what, main=None, protected=None, out=None):
    """The loud line refusing a factory write of ``what`` to ``target`` when that ref is the
    trunk or protected, else ''. The line goes to ``out`` (and stderr when ``out`` is None)."""
    name = branch_name(target)
    pats = patterns(main, protected)
    if not name or not any(name == p or fnmatch.fnmatchcase(name, p) for p in pats):
        return ''
    line = (f'REF GUARD: refused {what} → {name}: a protected ref (the trunk or '
            f'conventions.protected_refs) — a factory write never pushes, forces or deletes it')
    (out or (lambda s: print(s, file=sys.stderr)))(line)
    return line
