"""asf.ci — the CI class of a push: what it *owes CI*, never what may *land*.

A class answers one question: given the files a push touches, which checks must run on it?
``FACTORY_ONLY`` — every touched file matches one of the operator's globs, so lint and the
factory tests are enough. ``FULL`` — anything else, the whole matrix. It is deliberately not
harvest's gate, which decides what may be merged; a class only says how much CI a push owes,
and the two must not be folded into one.

Pure functions over plain lists: no filesystem, no configuration, no product convention. The
caller passes repo-relative, forward-slash paths (what ``git diff --name-only`` prints) and the
globs; a glob with no slash is matched against the base name, one with a slash against the
whole path. An empty push and an empty glob list are both ``FULL`` — when nothing says a push
is small, it is not.
"""
import fnmatch
import os

FACTORY_ONLY = 'FACTORY_ONLY'
FULL = 'FULL'
CLASSES = (FACTORY_ONLY, FULL)

# The workflow's copy of the operator's glob list is a block scalar under this key; the
# workflow, the doctor row and the mirror all spell it through this constant.
WORKFLOW_KEY = 'FACTORY_ONLY_PATHS'


def _strip(path):
    return path[2:] if path.startswith('./') else path


def _match_glob(glob, relpath):
    # The rule's home is asf.approvals._match_glob; copied, not imported, so the classifier does
    # not inherit the approval matrix's configuration reach.
    target = relpath if '/' in glob else os.path.basename(relpath)
    return fnmatch.fnmatchcase(target, glob)


def matches(path, globs):
    """True when ``path`` matches any of ``globs``."""
    path = _strip(path)
    return any(_match_glob(g, path) for g in globs)


def classify(paths, factory_only_globs):
    """``FACTORY_ONLY`` when ``paths`` is non-empty and every path matches; else ``FULL``."""
    paths = list(paths)
    if paths and all(matches(p, factory_only_globs) for p in paths):
        return FACTORY_ONLY
    return FULL


def unmatched(paths, factory_only_globs):
    """The paths that forced ``FULL``, in first-seen order."""
    return [p for p in paths if not matches(p, factory_only_globs)]


def globs_from_workflow(text):
    """The globs of the workflow's block scalar: stripped, comments and blanks dropped.

    Returns ``[]`` when the key is absent — a workflow without the block has no class.
    """
    lines = text.splitlines()
    start = key_indent = None
    for i, line in enumerate(lines):
        head = line.strip()
        if head.startswith(WORKFLOW_KEY + ':') and head[len(WORKFLOW_KEY) + 1:].strip()[:1] in ('|', '>'):
            start, key_indent = i + 1, len(line) - len(line.lstrip())
            break
    if start is None:
        return []
    out = []
    block_indent = None
    for line in lines[start:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if block_indent is None:
            if indent <= key_indent:
                break
            block_indent = indent
        elif indent < block_indent:
            break
        item = line.strip()
        if not item.startswith('#'):
            out.append(item)
    return out
