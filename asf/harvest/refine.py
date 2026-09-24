"""asf.harvest.refine — is a spec or plan branch a refinement, or a regeneration? (F-0023)

A request that names a document already in the record refines it; it never regenerates it. The
prototype's failure was *named things disappearing* — eight Tasks — so the verdict looks for
exactly that: an anchor the trunk's document carries and the branch's does not (a ``##``–``####``
heading, an ``S-``/``T-`` id), and, as a backstop for a rewrite that keeps its headings, more
than ``ratio`` of the trunk's lines deleted. Pure text in, ``(kind, text)`` or None out: git and
the correction loop live in :mod:`asf.harvest.harvest`."""
import difflib
import re

from asf.conventions import DEFAULT_REWRITE_RATIO

ANCHOR_HEADING_RE = re.compile(r'^(#{2,4})\s+(.+?)\s*$', re.M)
ANCHOR_ID_RE = re.compile(r'\b([ST]-\d+)\b')
#: How many lost anchors the correction quotes.
QUOTED = 5


def anchors(text):
    """The named things a document carries: its headings (``## Records``, whitespace
    normalised) and every ``S-``/``T-`` id it mentions."""
    found = {f"{hashes} {' '.join(title.split())}"
             for hashes, title in ANCHOR_HEADING_RE.findall(text or '')}
    found.update(ANCHOR_ID_RE.findall(text or ''))
    return found


def _quoted(lost):
    """Up to five lost anchors, quoted — headings in document order of their text, ids sorted
    by number."""
    def order(anchor):
        m = re.fullmatch(r'([ST])-(\d+)', anchor)
        return (1, anchor[0], int(m.group(2))) if m else (0, anchor, 0)
    names = sorted(lost, key=order)
    text = ', '.join(f'"{a}"' for a in names[:QUOTED])
    return text + (', …' if len(names) > QUOTED else '')


def verdict(before, after, ratio=DEFAULT_REWRITE_RATIO, path='the document', trunk='main'):
    """``('rewrite', text)`` when ``after`` regenerates ``before``, else None. ``before`` empty →
    None: there is nothing on the trunk to refine."""
    if not (before or '').strip():
        return None
    lost = anchors(before) - anchors(after)
    if lost:
        return 'rewrite', (
            f"{len(lost)} anchor(s) the document on {trunk} carries are gone: {_quoted(lost)} — "
            f"{len(anchors(before))} anchors on {trunk}, {len(anchors(after))} on the branch — "
            f"a request that names an existing document refines it and never regenerates it; "
            f"restore them or say on the card why they are wrong")
    old = before.splitlines()
    deleted = sum(1 for line in difflib.unified_diff(old, (after or '').splitlines(), n=0,
                                                     lineterm='')
                  if line.startswith('-') and not line.startswith('---'))
    if old and deleted / len(old) >= ratio:
        return 'rewrite', (
            f"{deleted} of {len(old)} lines of {path} on {trunk} are deleted "
            f"({round(100 * deleted / len(old))} %) — that is a regeneration, not a refinement")
    return None
