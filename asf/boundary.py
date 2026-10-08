"""asf.boundary — the footprint sizes the wave; the grant is what a session may write.

Two words, two readings (F-0026). ``writes:`` is the **footprint**: what a card reserves, read
by the feeder's wave gate and the groom's merge/batch/split proposals — widening it costs
parallelism. ``grant:`` is the **grant**: what a session on that branch may actually write, read
by harvest's boundary screen and the brief — absent, it defaults to the footprint (a Task) or to
the lane's one document and its own review files (a spec/plan Feature). This module is about the
grant: the matcher (:func:`covers`, :func:`outside`), the containment law (:func:`within`) that
keeps ``grant ⊆ writes`` true, the resolver (:func:`grant_for`) and the one-line hold
(:func:`refusal`). A leaf module: ``re`` and :mod:`asf.conventions` only, no git, no filesystem.
"""
import re
from functools import lru_cache

#: The correction kind a boundary hold writes (:func:`refusal`, :mod:`asf.workers.lifecycle`).
BOUNDARY_KIND = 'boundary'

#: ``conventions.boundary``'s three values (:func:`mode`).
HOLD = 'hold'
WARN = 'warn'
OFF = 'off'
MODES = (HOLD, WARN, OFF)

#: At most this many paths are named in a refusal's text before ``(+N more)``; the full list is
#: still carried on the correction's ``outside`` key (:func:`refusal`).
MAX_NAMED_PATHS = 5


def _translate(glob):
    """One regex body for ``glob``: ``**`` → ``.*`` (crosses ``/``), ``*`` → ``[^/]*`` (does
    not), ``?`` → ``[^/]``, a ``[...]`` character class kept as one, every other character
    escaped."""
    out = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == '*':
            if glob[i:i + 2] == '**':
                out.append('.*')
                i += 2
            else:
                out.append('[^/]*')
                i += 1
        elif c == '?':
            out.append('[^/]')
            i += 1
        elif c == '[':
            j = i + 1
            if j < n and glob[j] == '!':
                j += 1
            if j < n and glob[j] == ']':
                j += 1
            while j < n and glob[j] != ']':
                j += 1
            if j >= n:  # an unterminated '[': not a class, just a literal character
                out.append(re.escape(c))
                i += 1
            else:
                cls = glob[i + 1:j]
                if cls.startswith('!'):
                    cls = '^' + cls[1:]
                out.append('[' + cls + ']')
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return ''.join(out)


@lru_cache(maxsize=None)
def _regex(glob):
    return re.compile('^' + _translate(glob) + '$')


def covers(glob, path):
    """True when ``path`` (repo-relative) is inside ``glob``.

    ``*`` matches within one segment, ``**`` matches across ``/``, ``?`` one character, and a
    wildcard-free glob that is a prefix of ``path`` at a ``/`` boundary is a directory
    (``asf/harvest`` and ``asf/harvest/`` both cover ``asf/harvest/harvest.py``; ``asf/x`` covers
    none of ``asf/xy.py`` — a prefix that is not a whole segment covers nothing). Errs toward
    False: what this returns True for is what may land unexamined.
    """
    if not glob or not path:
        return False
    if _regex(glob).match(path):
        return True
    if not any(ch in glob for ch in '*?['):
        prefix = glob.rstrip('/')
        return path == prefix or path.startswith(prefix + '/')
    return False


def outside(paths, grant):
    """The members of ``paths`` no glob of ``grant`` covers, in ``paths`` order, deduplicated.
    ``grant`` empty or None is the caller's problem, not this function's — it returns every path
    (:func:`refusal` is what never asks with a ``None`` grant)."""
    grant = grant or ()
    seen = set()
    out = []
    for p in paths or ():
        if p in seen:
            continue
        seen.add(p)
        if not any(covers(g, p) for g in grant):
            out.append(p)
    return out


def within(inner, outer):
    """The first glob of ``inner`` that no glob of ``outer`` contains, or None — the containment
    law. A glob contains another when it covers its literal head and is no narrower in its
    wildcards; the conservative test is that :func:`covers` holds for the inner glob's own text
    (so ``asf/harvest/**`` is inside ``asf/**``, and ``asf/other.py`` is not inside
    ``asf/harvest/**``)."""
    outer = outer or ()
    for g in inner or ():
        if not any(covers(o, g) for o in outer):
            return g
    return None


def deliverable_of(conv, branch, item):
    """The one document a ``spec``/``plan`` branch exists to write (``docs/specs/t-0080.md``),
    or None for any other kind."""
    kind = conv.branch_kind(branch)
    if kind in ('spec', 'plan') and item:
        return f'{conv.doc_dir(kind)}/{item.lower()}.md'
    return None


def grant_for(conv, branch, item, card):
    """The globs a session on ``branch`` may write, or None when none is declared.

    1. ``card['grant']``, when the card carries one — a Task's or a Feature's, as typed.
    2. else, for a lane whose deliverable is one document (``spec``, ``plan``):
       ``[deliverable_of(...), conv.review_path(slug, '*')]`` — the document it exists to write,
       and the review files of its own item, which later rounds write on the same branch.
    3. else, ``card['writes']`` — the footprint, which is what every Task card today means.
    4. else None: nothing in the record says what this branch may write, so nothing is asserted
       about it.

    ``None`` and ``[]`` are different answers and stay so: ``None`` is *undeclared* and holds
    nothing; ``[]`` would be *may write nothing*, which no card can currently express.
    """
    card = card or {}
    if card.get('grant'):
        return list(card['grant'])
    deliverable = deliverable_of(conv, branch, item)
    if deliverable:
        return [deliverable, conv.review_path((item or '').lower(), '*')]
    if card.get('writes'):
        return list(card['writes'])
    return None


class Edge(tuple):
    """``(kind, text)``, with the full list of out-of-grant paths on ``.outside`` — so
    ``hold_with_correction(..., *edge, ...)`` still expands to exactly the pair its call site
    reads, while :func:`outside_of` reaches the whole list the text's ``(+N more)`` abbreviates."""

    def __new__(cls, kind, text, outside=()):
        self = super().__new__(cls, (kind, text))
        self.outside = tuple(outside)
        return self


def outside_of(edge):
    """The full list of out-of-grant paths an :class:`Edge` carries, or ``()`` for a plain
    ``(kind, text)`` pair with no such attribute."""
    return getattr(edge, 'outside', ())


def refusal(conv, branch, item, card, touched):
    """``Edge(BOUNDARY_KIND, text)`` when ``touched`` leaves ``branch``'s grant, else None."""
    grant = grant_for(conv, branch, item, card)
    if grant is None:
        return None
    extra = outside(touched, grant)
    if not extra:
        return None
    shown = extra[:MAX_NAMED_PATHS]
    more = len(extra) - len(shown)
    named = ', '.join(shown) + (f' (+{more} more)' if more else '')
    hint = f'`grant: <glob>` on {item}' if item else '`grant: <glob>`'
    text = (f'writes outside its grant: {named} — the grant is {", ".join(grant)}. Nothing is '
            f'reverted and nothing is lost: move those files inside the grant, or leave them and '
            f'ask the groom to widen it ({hint})')
    return Edge(BOUNDARY_KIND, text, outside=extra)


def mode(conv):
    """``conventions.boundary``, normalised: one of :data:`MODES`. Anything else — including an
    unset or misspelled value — reads as :data:`HOLD`, never :data:`OFF` (an unknown value must
    not silently turn the check off)."""
    value = str(getattr(conv, 'boundary', None) or '').strip().lower()
    return value if value in MODES else HOLD


def mode_warning(conv):
    """One line naming a ``conventions.boundary`` value that is not one of :data:`MODES`, or
    None when it is (or is unset)."""
    raw = getattr(conv, 'boundary', None)
    value = str(raw or '').strip().lower()
    if raw is not None and value not in MODES:
        return (f"conventions.boundary: {raw!r} is not one of {', '.join(MODES)} — "
                f"reading it as {HOLD!r}")
    return None
