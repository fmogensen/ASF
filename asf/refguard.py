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

Never list a merge-queue batch prefix here to pause new cuts for a pin move: it also refuses
this guard's own delete of a batch ref once the queue judges that batch dead, so the two
deadlock (#34, #36). ``merge_queue.inflight: 0`` (:func:`asf.merge_queue.paused`) is the
first-class pause instead — it stops the cut, never the cleanup.
"""

import fnmatch
import os
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


# --- the push door's guard (:func:`asf.gitpush.push`) ---------------------------------------

#: ``conventions.flags.refguard``: what the push door does with a push aimed at a protected
#: ref of the repository being pushed. ``off`` (the default): nothing — the per-site checks
#: above stand alone, as before. ``warn``: a ``REF GUARD (warn): …`` line, the push proceeds.
#: ``refuse``: the push is refused before any ``git`` runs.
OFF, WARN, REFUSE = 'off', 'warn', 'refuse'
MODES = (OFF, WARN, REFUSE)
DEFAULT_MODE = OFF


def mode_of(value):
    """``flags.refguard`` as one of :data:`MODES`; anything else is :data:`DEFAULT_MODE`."""
    value = str(value or '').strip().lower() if isinstance(value, str) else ''
    return value if value in MODES else DEFAULT_MODE


class Guard:
    """What one ``git push`` may target, keyed on the repository it pushes: ``main`` (that
    repository's trunk) and ``protected`` (names or globs; None: :data:`DEFAULT_PROTECTED_REFS`)
    are protected; ``mode`` (:data:`MODES`) is what a push at them gets; ``door=True`` is a
    landing path built to advance the trunk (the merge queue's fast-forward, harvest's
    fast-forward, the changelog under ``merge: auto``) and bypasses the check."""

    __slots__ = ('main', 'protected', 'mode', 'door')

    def __init__(self, main=None, protected=None, mode=REFUSE, door=False):
        self.main = main if isinstance(main, str) and main else None
        self.protected = None if protected is None else tuple(protected)
        self.mode = mode if mode in MODES else mode_of(mode)
        self.door = bool(door)

    def __repr__(self):
        return (f'Guard(main={self.main!r}, protected={self.protected!r}, mode={self.mode!r}, '
                f'door={self.door})')

    def hit(self, target):
        """The protected branch ``target`` (a refspec) writes, else ''."""
        if self.door:
            return ''
        name = branch_name(target)
        pats = patterns(self.main, self.protected)
        return name if name and any(name == p or fnmatch.fnmatchcase(name, p)
                                    for p in pats) else ''

    def refusal(self, target):
        """The line this guard says about a push to ``target``: ``REF GUARD: refused …`` under
        ``refuse``, ``REF GUARD (warn): …`` under ``warn``; '' when the target is not protected,
        the mode is ``off`` or the guard is a door."""
        name = self.hit(target) if self.mode != OFF else ''
        if not name:
            return ''
        head = 'REF GUARD: refused' if self.mode == REFUSE else 'REF GUARD (warn):'
        return (f'{head} push → {name}: a protected ref of this repository (its trunk or '
                f'conventions.protected_refs) — only a landing path (a door) advances it; push '
                f'a branch and land it instead')


#: The record repository's guard: it protects nothing. The record's ``HEAD:<trunk>`` push IS
#: its publish (:mod:`asf.tick.shadow`) — the record's trunk is that push's target by design.
RECORD = Guard(main=None, protected=(), mode=OFF)


def guard_from(main, conv=None, door=False):
    """The guard of a product repository whose trunk is ``main`` and whose conventions are
    ``conv``: ``conventions.protected_refs`` (:func:`listed`), the mode from
    ``conventions.flags.refguard`` (:data:`DEFAULT_MODE` when unset)."""
    flag = getattr(conv, 'flag', None)
    try:
        mode = mode_of(flag('refguard', DEFAULT_MODE) if callable(flag) else None)
    except Exception:       # noqa: BLE001 — a conventions stand-in without flags is unset
        mode = DEFAULT_MODE
    return Guard(main, listed(conv), mode, door)


def guard_for(product, repo=None, door=False):
    """The guard of a push made in ``repo`` on ``product``'s behalf, keyed on the repository
    being pushed: the product's record clone (:func:`asf.tick.shadow.shadow_dir`) is
    :data:`RECORD` — its trunk is its publish target; anything else (the product's checkout or
    one of its worktrees) is :func:`guard_from` the product's trunk and conventions."""
    if repo:
        try:
            from asf.tick import shadow
            if os.path.realpath(str(repo)) == os.path.realpath(shadow.shadow_dir(product)):
                return RECORD
        except Exception:   # noqa: BLE001 — a product stand-in without a state dir
            pass
    return guard_from(getattr(product, 'main', None), getattr(product, 'conventions', None),
                      door)
