"""asf.record.decisions — the decision register: which ``D-nnnn`` ids exist.

A decision exists when the record holds it (a ``decisions/D-nnnn.md`` card) or the product's
own ``docs/decisions/`` carries it — a file named ``D-nnnn.md``, a file whose front matter says
``id: D-nnnn``, or a numbered record ``nnnn-<slug>.md`` (read as ``D-nnnn``). Two readers:
the ingest, where a Story's acceptance line counts as deferred only by a decision the register
holds, and the plan-tasks step, which refuses a plan whose Tasks cite a decision it lacks.
"""
import os
import re

ID_RE = re.compile(r'\bD-\d{4,}\b')
_NUMBERED_RE = re.compile(r'^(\d{4,})-')
_FRONT_ID_RE = re.compile(r'^id:\s*(D-\d{4,})\s*$', re.M)
#: a fenced block or an inline code span — a decision id inside one is an example, not a citation
_CODE_RE = re.compile(r'```.*?```|`[^`\n]*`', re.S)
DOCS_DIR = os.path.join('docs', 'decisions')


def _repo_dir(product):
    try:
        return getattr(product, 'repo_dir', None) if product is not None else None
    except Exception:  # an unset repo_dir raises on some Product shapes: no docs, no ids
        return None


def docs_ids(repo_dir):
    """The decision ids ``<repo_dir>/docs/decisions/`` carries."""
    out = set()
    if not repo_dir:
        return out
    folder = os.path.join(repo_dir, DOCS_DIR)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if not name.endswith('.md'):
            continue
        stem = name[:-3]
        if ID_RE.fullmatch(stem.upper()):
            out.add(stem.upper())
        m = _NUMBERED_RE.match(stem)
        if m:
            out.add(f'D-{m.group(1)}')
        try:
            with open(os.path.join(folder, name), encoding='utf-8') as f:
                head = f.read(2048)
        except OSError:
            continue
        m = _FRONT_ID_RE.search(head)
        if m:
            out.add(m.group(1).upper())
    return out


def register(canonical, product=None):
    """Every decision id that exists: the record's decision cards and the product's docs."""
    ids = {str(iid).upper() for iid, rec in (canonical or {}).items()
           if (rec.get('meta') or {}).get('type') == 'decision'}
    return ids | docs_ids(_repo_dir(product))


def cited(text):
    """The decision ids ``text`` cites, code spans and fenced blocks left out, in order."""
    return list(dict.fromkeys(ID_RE.findall(_CODE_RE.sub('', text or ''))))


def unknown(text, known):
    """The ids ``text`` cites that ``known`` lacks."""
    known = {k.upper() for k in known}
    return [d for d in cited(text) if d.upper() not in known]
