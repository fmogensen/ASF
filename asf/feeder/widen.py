"""asf.feeder.widen — the rule that widens an under-scoped Task's ``writes:``, over facts only.

A plan under-scopes a Task's footprint: the coder finishes its own part green, but files outside
``writes:`` must change too — a sibling suite the new behaviour turns red, a constant that
belongs in another module. The coder may not touch them (the footprint is the boundary), so a
plain correction loops: the session can never satisfy it inside ``writes:``.

Two facts name the missing paths, and neither is a judgement:

* the session's own REPORT — its ``needs writes:`` field, else the repo paths its ``left out:``
  names (:func:`asf.workers.report.footprint_claim`);
* the harvest gate — the branch red alone on a trunk green on the same modules, and the failing
  test files (and the files they import that the output names) sitting outside ``writes:``
  (:func:`asf.harvest.harvest.widen_candidates`).

:func:`decide` is the rule ``widen_footprint``. The paths are added to the Task's ``writes:`` and
the same run goes back as a correction only when all hold: at most ``conventions.widen_max_files``
paths, none under an approvals-protected glob (those go to their approval class), none
overlapping an open Task's ``writes:`` — every Task Active in the record or with a run in play,
by ``asf check``'s own intersection test, and the paths an earlier widening of the same pass
added (the Task waits on that owner) — and no earlier widening of this Task. A second widening, or too many paths, is a RESHAPE of the Task. Pure functions over plain
values — no filesystem, no git.
"""
import dataclasses
import fnmatch
import posixpath
import re

from asf.conventions import DEFAULT_WIDEN_MAX_FILES
from asf.feeder import footprint
from asf.record.core import writes_intersect

WIDEN = 'widen'
RESHAPE = 'reshape'
APPROVAL = 'approval'
WAITS = 'waits'

#: ``conventions.widen_max_files`` when the product sets none.
MAX_FILES = DEFAULT_WIDEN_MAX_FILES

#: The History line a widening files on the Task (the fact names where the paths came from).
HISTORY = 'footprint widened: +{paths} ({fact})'
#: The History line that undoes a widening whose paths intersect an open Task's ``writes:``.
REVERTED = 'footprint widening reverted: overlaps {owner}'
#: A widening's History line, read back: the paths it added.
WIDENED_RE = re.compile(r'footprint widened: \+(.+?) \(')
#: The reshape reason (the RESHAPE row's reason, and the card's ``reshape:`` value).
RESHAPE_REASON = 'footprint: needs {paths}'

TEST_NAME_RE = re.compile(r'(^|/)(test_[^/]+\.py|[^/]+_test\.py|[^/]+\.(test|spec)\.[cm]?[jt]sx?)$')
TEST_DIR_RE = re.compile(r'(^|/)(tests?|__tests__)/')
#: Path-like tokens: at least one extension, no whitespace, no quote or backtick.
PATH_TOKEN_RE = re.compile(r'(?<![\w./@-])((?:[\w.()\[\]@+-]+/)*[\w.()\[\]@+-]*\w\.[A-Za-z0-9]{1,6})(?![\w/])')
SKIP_DIRS = ('node_modules/', '.git/', '.venv/', 'dist/', 'build/', '.next/')


@dataclasses.dataclass(frozen=True)
class Verdict:
    """What the rule says: ``widen`` | ``reshape`` | ``approval`` | ``waits``, the paths it is
    about, and the detail — the reshape reason, the approval class, the Task waited on."""
    kind: str
    paths: tuple
    detail: str = ''
    level: str = ''


def norm_writes(writes):
    """``writes:`` as a flat list of paths: a value that carries several whitespace-separated
    paths in one entry (a plan's ``writes: [a b c]``) is split into them."""
    out = []
    for w in writes or ():
        for part in str(w).split():
            if part not in out:
                out.append(part)
    return out


def covered(path, writes):
    """True when ``path`` is inside the footprint: named exactly, matched by a glob, or under a
    directory the footprint names. A literal ``[id]`` segment is compared as text first, so a
    route directory is never read as a character class."""
    for w in norm_writes(writes):
        if path == w or (w.endswith('/') and path.startswith(w)):
            return True
        if any(ch in w for ch in '*?') and fnmatch.fnmatchcase(path, w):
            return True
    return False


def outside(paths, writes):
    """``paths`` not inside ``writes``, in order, de-duplicated."""
    out = []
    for p in paths or ():
        if p and p not in out and not covered(p, writes):
            out.append(p)
    return out


def is_test_path(path):
    """A test file by name or by directory: ``test_x.py``, ``x_test.py``, ``x.test.ts``,
    ``x.spec.tsx``, or anything under ``tests/`` / ``test/`` / ``__tests__/``."""
    path = str(path or '')
    return bool(TEST_NAME_RE.search(path) or TEST_DIR_RE.search(path))


def path_tokens(text):
    """Every path-like token in ``text`` (backticks and quotes stripped), first-seen order."""
    out = []
    for m in PATH_TOKEN_RE.finditer(str(text or '').replace('`', ' ')):
        token = m.group(1).strip('.').lstrip('./')
        if token and '/' + token not in out and token not in out \
                and not token.startswith(SKIP_DIRS) and not token.startswith(('origin/', 'http')):
            out.append(token)
    return out


def resolve(tokens, tracked):
    """Each token as the one tracked repo path it names: itself when tracked, else the single
    tracked path it is the tail of (``sibling.test.ts`` → ``apps/…/sibling.test.ts``).
    A token that names no tracked path, or more than one, is dropped: no guess. With no
    ``tracked`` list (no repo to read), tokens that look like repo paths (a ``/`` in them) stand."""
    out = []
    for token in tokens:
        if tracked is None:
            hit = token if '/' in token else None
        elif token in tracked:
            hit = token
        else:
            tails = [p for p in tracked if p.endswith('/' + token)]
            hit = tails[0] if len(tails) == 1 else None
        if hit and hit not in out:
            out.append(hit)
    return out


# ---- what a test imports ------------------------------------------------------

PY_IMPORT_RE = re.compile(r'^\s*(?:from\s+([.\w]+)\s+import|import\s+([\w.]+))', re.M)
JS_IMPORT_RE = re.compile(r'''(?:\bfrom\s+|\bimport\s*\(?\s*|\brequire\s*\(\s*|\bvi\.mock\s*\(\s*|\bjest\.mock\s*\(\s*)['"]([^'"]+)['"]''')
EXT_RE = re.compile(r'\.[cm]?[jt]sx?$|\.py$')


def _stem(path):
    stem = EXT_RE.sub('', path)
    return stem[:-len('/index')] if stem.endswith('/index') else \
        (stem[:-len('/__init__')] if stem.endswith('/__init__') else stem)


def import_stems(text, path):
    """The module stems (repo paths without extension) a test file imports: relative specs
    resolved against the file's directory; bare specs kept as tails (``@/lib/send`` → ``lib/send``,
    ``asf.feeder.rows`` → ``asf/feeder/rows``) to match by suffix."""
    base = posixpath.dirname(path or '')
    out = []
    if str(path).endswith('.py'):
        for m in PY_IMPORT_RE.finditer(text or ''):
            mod = m.group(1) or m.group(2)
            if mod.startswith('.'):
                dots = len(mod) - len(mod.lstrip('.'))
                up = base
                for _ in range(dots - 1):
                    up = posixpath.dirname(up)
                rest = mod.lstrip('.').replace('.', '/')
                out.append(posixpath.join(up, rest) if rest else up)
            else:
                out.append(mod.replace('.', '/'))
    else:
        for m in JS_IMPORT_RE.finditer(text or ''):
            spec = m.group(1)
            if spec.startswith('.'):
                out.append(_stem(posixpath.normpath(posixpath.join(base, spec))))
            elif spec.startswith(('@/', '~/')):
                out.append(_stem(spec[2:]))
            elif '/' in spec and not spec.startswith('@'):
                out.append(_stem(spec))
    return [s for s in dict.fromkeys(out) if s]


def imported(stems, files):
    """The ``files`` (repo paths) some stem of ``stems`` names — equal, or a path-suffix match."""
    out = []
    for f in files or ():
        fs = _stem(f)
        if any(fs == s or fs.endswith('/' + s) for s in stems) and f not in out:
            out.append(f)
    return out


# ---- the rule ------------------------------------------------------------------

def max_files(product):
    """``conventions.widen_max_files`` (default :data:`MAX_FILES`)."""
    conv = getattr(product, 'conventions', None) if product is not None else None
    v = conv.get('widen_max_files') if conv is not None else None
    return v if isinstance(v, int) and v >= 0 else MAX_FILES


def decide(task_id, paths, limit=MAX_FILES, protected=None, running=(), widened_before=0):
    """The ``widen_footprint`` verdict for ``task_id`` needing ``paths`` outside its ``writes:``.

    ``protected``: ``{path: (class, level)}`` — the paths under an approvals-protected glob whose
    class is not granted. ``running``: ``[(task_id, writes)]`` of the open Tasks — Active in the
    record or with a run in play — and the widenings already made this pass.
    ``widened_before``: how many times this Task was already widened."""
    paths = tuple(dict.fromkeys(p for p in paths or () if p))
    if widened_before or len(paths) > limit:
        return Verdict(RESHAPE, paths, RESHAPE_REASON.format(paths=' '.join(paths)))
    for p in paths:
        if (protected or {}).get(p):
            cls, level = protected[p]
            return Verdict(APPROVAL, paths, cls, level)
    other = footprint.first_conflict(list(paths), [(t, norm_writes(w)) for t, w in running or ()
                                                   if t != task_id])
    if other:
        return Verdict(WAITS, paths, other)
    return Verdict(WIDEN, paths)


# ---- a widening that overlaps: undone ------------------------------------------

def widened_paths(body):
    """Every path a ``footprint widened: +…`` History line in ``body`` added, first-seen order."""
    out = []
    for m in WIDENED_RE.finditer(body or ''):
        for p in m.group(1).split():
            if p not in out:
                out.append(p)
    return out


def overlapping_widenings(writes, widened, others):
    """``(owner, [paths])``: the paths of ``writes`` a widening added (``widened``) that intersect
    an open Task's ``writes:`` by ``asf check``'s test — the first such owner of ``others``
    (``[(task_id, writes)]``) — else None. A plan-declared path is never named."""
    for owner, other in others:
        hit = [w for w in writes if w in widened
               and any(writes_intersect(w, o) for o in other)]
        if hit:
            return owner, hit
    return None
