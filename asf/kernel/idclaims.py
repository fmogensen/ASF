"""asf.kernel.idclaims — a session's question about an id claim, answered from facts (ASF 0.2).

A cloud spec or plan session cannot read the record repo's ``refs/asf/ids/*`` claims
(:mod:`asf.record.idclaim`), so a session that kept ids an earlier session minted sometimes ends
on a ``NEEDS OPERATOR:`` question whether the claim still covers them. That fact is code's to
check, not a person's: :func:`is_claim_question` recognises such a question (narrowly — it names an
id claim *and* offers a re-mint, or names a claim ref or a claimed block), :func:`cited` lists the ids it asks about,
the facts reader looks each up on the record's origin (``Facts.id_claims``: id -> ``(ref, sha)``
of the claim covering it, ``''`` when none does; an id it could not read is absent), and
:func:`answer` writes the kernel's answer, or None when it is unsure — then the question stays an
operator Stuck. Pure: nothing here reads a disk or a network.
"""
import re

#: a question about an id claim: it names a claim (``id claim``, ``idclaim``, ``the claim``) …
CLAIM_RE = re.compile(r'\b(?:id[ -]?)?claim(?:s|ed)?\b', re.I)

#: … and offers a re-mint, names a claim ref, or names a claimed block (``S:99305-99354``: a
#: recorded Stuck reason is cut at 200 characters, often before the re-mint)
REMINT_RE = re.compile(r'\bre-?mint|refs/asf/ids/|\b[A-Z]:\d{4,}-\d{4,}\b', re.I)

#: one id (``S-99255``), or a run of them (``S-91805..S-91808``, ``S-91805–S-91808``); a block in
#: the claim notation (``S:99255-99304``) is not an id
ID_RE = re.compile(r'(?<![\w:/.-])([A-Z])-(\d{4,})(?:\s*(?:\.\.\.?|–|—|\bto\b)\s*(?:\1-)?(\d{4,}))?'
                   r'(?![\w-])')
REF_ID_RE = re.compile(r'refs/asf/ids/([A-Z])-(\d{4,})\b')

#: the most ids one question may cite and still be answered (a longer run is not this question)
MAX_CITED = 50

#: the answers (the relaunch carries them as its finding)
STANDS = ('id claim verified by the kernel: the claim stands — keep the ids you declared, do not '
          're-mint. %s. No record item collides with them.')
REMINT = ('id claim checked by the kernel: %s — re-mint from your current block (your '
          'BACKLOG_ID_RANGE) and use only ids from it.')


def is_claim_question(text):
    """Whether ``text`` asks only whether an id claim covers ids: it names a claim and offers a
    re-mint (or names a ``refs/asf/ids/`` ref, or a claimed block)."""
    text = str(text or '')
    return bool(CLAIM_RE.search(text) and REMINT_RE.search(text))


def _fmt(prefix, n, width):
    return '%s-%0*d' % (prefix, width, n)


def cited(text, prefixes, exclude=()):
    """The ids ``text`` cites with a prefix in ``prefixes``, runs expanded, in order of first
    mention, ``exclude`` left out; None when it cites more than :data:`MAX_CITED`."""
    text = str(text or '')
    out = []
    spans = [(m.group(1), m.group(2), m.group(3)) for m in ID_RE.finditer(text)]
    spans += [(m.group(1), m.group(2), None) for m in REF_ID_RE.finditer(text)]
    for prefix, lo, hi in spans:
        if prefix not in prefixes:
            continue
        a = int(lo)
        b = int(hi) if hi else a
        if b < a or b - a >= MAX_CITED:
            return None
        for n in range(a, b + 1):
            iid = _fmt(prefix, n, len(lo))
            if iid not in out and iid not in exclude:
                out.append(iid)
        if len(out) > MAX_CITED:
            return None
    return out


def _lineage(iid, items):
    """``iid`` and every ancestor through ``parent`` (cycle-safe)."""
    seen = []
    while iid and iid not in seen:
        seen.append(iid)
        it = items.get(iid)
        iid = it.parent if it is not None else None
    return seen


def _under(iid, owner, items):
    """Whether record item ``iid`` is ``owner`` or sits under it through ``parent``."""
    return owner in _lineage(iid, items)


def answer(item, text, facts, prefixes):
    """The kernel's answer to ``item``'s question ``text`` when it only asks whether an id claim
    covers the ids it cites, else None (not such a question, no id cited, or a claim unread)."""
    if not is_claim_question(text):
        return None
    ids = cited(text, prefixes, exclude=_lineage(item.id, facts.items))
    claims = facts.id_claims or {}
    if not ids or any(i not in claims for i in ids):
        return None
    collide = [i for i in ids if i in facts.items and not _under(i, item.id, facts.items)]
    uncovered = [i for i in ids if not claims[i]]
    if uncovered or collide:
        why = []
        if uncovered:
            why.append('%s %s covered by no claim on the record\'s origin'
                       % (', '.join(uncovered), 'is' if len(uncovered) == 1 else 'are'))
        if collide:
            why.append('%s already %s on the record under another item'
                       % (', '.join(collide), 'is' if len(collide) == 1 else 'are'))
        return REMINT % '; '.join(why)
    by_ref = {}
    for i in ids:
        ref, sha = claims[i]
        by_ref.setdefault((ref, sha), []).append(i)
    verified = '; '.join('%s by %s %s' % (', '.join(group), ref, (sha or '?')[:7])
                         for (ref, sha), group in by_ref.items())
    return STANDS % ('Verified: %s' % verified)
