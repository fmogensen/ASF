"""asf.security.paths — which of a product's own named classes a diff's files fall under
(``conventions.security.paths``: ``{class: [glob]}``, globs matched as
:func:`asf.customer_content.matches` matches them). No block, no classes: nothing is sensitive
and no check violates. A file under two classes' globs is listed under both — the pass owes a
row per class, and a diff that is both is exactly the diff that needs both read."""
from asf.customer_content import matches

#: The most files a class's row in :func:`describe` names before the rest are counted.
SHOW_HITS = 10


def classes(conv):
    """``{class: [glob]}`` from ``conventions.security.paths`` ({} = nothing is sensitive, and
    for a ``conv`` that is None)."""
    return conv.security_paths() if conv is not None else {}


def touched(conv, files):
    """``{class: [file]}`` — the files among ``files`` under each class's globs, a class with no
    hit left out, class names in configuration order."""
    out = {}
    for name, globs in classes(conv).items():
        hit = [f for f in files or () if f and matches(globs, f)]
        if hit:
            out[name] = hit
    return out


def describe(hit, limit=SHOW_HITS):
    """``cls1: a.py, b.py · cls2: c.py`` — each class's files capped at ``limit`` with the
    rest counted, classes in the order ``hit`` (:func:`touched`'s) carries them."""
    parts = []
    for name, files in hit.items():
        shown = files[:limit]
        more = len(files) - len(shown)
        line = f"{name}: {', '.join(shown)}"
        if more > 0:
            line += f' and {more} more'
        parts.append(line)
    return ' · '.join(parts)
