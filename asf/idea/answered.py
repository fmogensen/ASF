"""asf.idea.answered — the record before the person.

A node of an idea tree the record already carries is not filed: a filed card becomes a groom
line, and a groom line is a question a person answers. This is the whole of "no question is put
to a person that the record could have answered" (F-0023)."""
from collections import namedtuple

from asf.conventions import DEFAULT_ANSWER_OVERLAP
from asf.record import frontmatter
from asf.record.core import is_open, jaccard, tokenize

Answered = namedtuple('Answered', 'node source text')


def answer_from_record(node, canonical, overlap=DEFAULT_ANSWER_OVERLAP):
    """The open item of the node's type whose title shares at least ``overlap`` of its tokens
    with the node's, as ``Answered(node, id, 'F-0031 — Approvals as code (feature, Active)')``;
    the highest overlap wins, ties by id. None when the record answers nothing."""
    wanted = node.type.lower()
    tokens = tokenize(node.title)
    best = None
    for iid, rec in canonical.items():
        meta = rec['meta']
        if meta.get('type') != wanted or not is_open(rec):
            continue
        score = jaccard(tokens, tokenize(meta.get('title')))
        if score >= overlap and (best is None or (-score, iid) < (-best[0], best[1])):
            best = (score, iid, rec)
    if best is None:
        return None
    _, iid, rec = best
    _, machine = frontmatter.split_machine(rec['meta'])
    return Answered(node, iid, f"{iid} — {rec['meta'].get('title')} "
                               f"({wanted}, {machine.get('state', 'New')})")
