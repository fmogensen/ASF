"""asf.debug_toggles — a branch never lands a check its session switched off.

A debug toggle is a focused or disabled test, a breakpoint, a debug flag left ``True``: the
session's own aid, invisible in a green result, and a smaller suite forever after. The landing
gate refuses a branch that *adds* one — before review (PUSHED) and again before the gate — and
hands it back with every ``file:line`` (:func:`refusal`).

On for every product; documentation is excluded through the product's own docs rule
(:func:`asf.harvest.lane.is_doc`), and one line is made legal by a waiver that carries a reason::

    it.only('the one case', ...)   # debug-ok: pinned while T-0001 bisects the flake

A product narrows, extends or switches the gate off in ``conventions.debug_toggles``
(:data:`asf.conventions.DEFAULT_DEBUG_TOGGLES`). ``asf doctor`` names what is checked, and a
declared block that checks nothing (:func:`findings`).
"""
import re

from asf.conventions import (DEFAULT_DEBUG_TOGGLE_PATHS, DEFAULT_DEBUG_TOGGLE_WAIVER,
                             DEFAULT_DEBUG_TOGGLES)
from asf.customer_content import SHOW_HITS, added_lines, describe, diff, matches, scan_line

#: The correction kind a branch the debug-toggle gate refuses goes back with.
KIND = 'debug-toggle'


def _block(conv):
    v = (conv.get('debug_toggles') if conv is not None and hasattr(conv, 'get') else None)
    return v if isinstance(v, dict) else {}


def configured(conv):
    """True when the product wrote a ``debug_toggles`` block at all."""
    return conv is not None and hasattr(conv, 'get') and conv.get('debug_toggles') is not None


def paths(conv):
    """The ``debug_toggles.paths`` globs — :data:`DEFAULT_DEBUG_TOGGLE_PATHS` when unset, ``[]``
    when the product wrote ``paths: []`` (D8: that switches the gate off)."""
    v = _block(conv).get('paths', DEFAULT_DEBUG_TOGGLE_PATHS)
    if isinstance(v, str):
        v = [v]
    return [p.strip() for p in v or () if isinstance(p, str) and p.strip()]


def markers(conv):
    """The compiled ``debug_toggles.markers`` (the defaults when unset); a pattern that does not
    compile is left out here — ``asf doctor`` names it."""
    v = _block(conv).get('markers')
    raw = DEFAULT_DEBUG_TOGGLES if v is None else (v if isinstance(v, list) else [])
    out = []
    for p in raw:
        try:
            out.append(re.compile(str(p)))
        except re.error:
            continue
    return out


def waiver(conv):
    """The compiled ``debug_toggles.waiver`` (:data:`DEFAULT_DEBUG_TOGGLE_WAIVER` when unset);
    None when it does not compile — and a None waiver waives nothing."""
    v = _block(conv).get('waiver', DEFAULT_DEBUG_TOGGLE_WAIVER)
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return re.compile(v)
    except re.error:
        return None


def waived(pat, text):
    """True when ``text`` carries the waiver with a reason — a None ``pat`` waives nothing."""
    return bool(pat) and bool(pat.search(text))


def scanned(conv, path):
    """True when ``path`` is checked: under ``debug_toggles.paths`` and not documentation
    (the product's own docs rule, :func:`asf.harvest.lane.is_doc`)."""
    from asf.harvest import lane
    return matches(paths(conv), path) and not lane.is_doc(conv, path)


def added_hits(repo, base, head, conv):
    """``[(path, line, marker)]``: each line ``head`` adds since it left ``base`` under
    ``debug_toggles.paths``, documentation excluded, that matches a marker and is not waived.
    ``[]`` when ``paths(conv)`` is empty or the diff cannot be read."""
    globs = paths(conv)
    if not globs or not repo:
        return []
    pats, allow = markers(conv), waiver(conv)
    hits = []
    for path, n, text in added_lines(diff(repo, base, head)):
        if not scanned(conv, path):
            continue
        m = scan_line(pats, text)
        if m and not waived(allow, text):
            hits.append((path, n, m))
    return hits


def refusal(repo, trunk, branch, conv):
    """``(KIND, text)`` for a lane branch that adds a debug toggle, else None, over
    ``origin/<trunk>...origin/<branch>``."""
    hits = added_hits(repo, f'origin/{trunk}', f'origin/{branch}', conv)
    if not hits:
        return None
    return KIND, (f'a check switched off: {describe(hits, SHOW_HITS)} — a debug toggle makes a '
                  f'green result smaller than it looks: remove the focused/disabled test, the '
                  f'breakpoint and the debug flag, or mark the line `debug-ok: <why it stays>`, '
                  f'then push')


# ---- the doctor ---------------------------------------------------------------------------

def findings(product):
    """[(ok, detail)] — the doctor's ``debug toggles`` row: what the gate checks on this
    product, and red when a declared block checks nothing, or names a marker that does not
    compile."""
    conv = getattr(product, 'conventions', None)
    out = []
    raw = _block(conv).get('markers')
    for p in (raw if isinstance(raw, list) else ()):
        try:
            re.compile(str(p))
        except re.error as e:
            out.append((False, f'conventions.debug_toggles.markers: {p!r} is not a regex ({e})'))
    globs = paths(conv)
    if not globs and configured(conv):
        out.append((False, 'conventions.debug_toggles names no paths — no branch is checked for '
                           'a switched-off test, breakpoint or debug flag'))
    else:
        n = len(markers(conv))
        out.append((True, f"{', '.join(globs)} · {n} marker(s) · waiver: debug-ok: <reason>"))
    return out
