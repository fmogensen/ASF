"""The amendable set — the factory's own rules, named once (F-0024 §2.1).

No session edits this set; the factory proposes, a person approves and merges. Six kinds, every
glob derived from the module that owns the path so none is spelled twice: rule cards, the checks
they run, the hooks that fire at all, the role agents every session is launched with, the
product's briefs and its evals.

``conventions.amendable_paths`` has three states: unset (``None``) is the defaults here, a list
is that list, and ``[]`` is an opt-out — the set is empty.
"""
import dataclasses
import importlib
import os
import re

from asf import conventions as conv_mod
from asf import hooks
from asf.record import core
build = importlib.import_module('asf.briefs.build')  # `asf.briefs.build` the attribute is a function

#: The repository root, so the package-relative form of a package directory can be built.
_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclasses.dataclass(frozen=True)
class Kind:
    name: str      # rule_cards | checks | hooks | role_agents | briefs | evals
    why: str       # one line, printed by a refusal and by `asf approvals`
    globs: tuple   # repo-relative, the §P3 glob rule


#: The kind of a path the product's own ``conventions.amendable_paths`` names but no built-in
#: kind's globs cover (a process doc, say): still in the set, and a refusal names it this way.
LISTED = Kind('listed', "named in the product's own conventions.amendable_paths", ())


def _conventions(product):
    return getattr(product, 'conventions', None) or conv_mod.Conventions()


def kinds(product):
    """The six kinds, their globs derived from the modules that own those paths (§1.4)."""
    conv = _conventions(product)
    rules = core.TYPES['rule'][0]
    templates = os.path.relpath(build.TEMPLATES_DIR, _PACKAGE_ROOT).replace(os.sep, '/')
    return (
        Kind('rule_cards', 'the rules the factory is judged by, and the index every check reads',
             (f'{rules}/*.md', f'{rules}/index.json')),
        Kind('checks', "what a rule card's `check:` runs",
             (f'{rules}/*.sh', f'{hooks.CHECKS_DIR}/*')),
        Kind('hooks', 'which hooks fire at all — the gate over every other gate',
             tuple(hooks.RUNTIME_SETTINGS_GLOBS) + tuple(hooks.GIT_HOOK_GLOBS)),
        Kind('role_agents', 'the standing instructions every session is launched with',
             (f'{templates}/*.md',) + tuple(hooks.RUNTIME_AGENT_GLOBS)),
        Kind('briefs', "the product's own briefs",
             (f'{conv.briefs_dir}/*',) if conv.briefs_dir else ()),
        Kind('evals', 'empty in 0.1; named now so 0.2 has nothing to decide',
             (f'{conv.evals_dir}/*',)),
    )


def paths(product):
    """Every glob of the set. `conventions.amendable_paths` when the product named one — an
    empty list is a named one, and means the set is empty — else every kind's globs. A
    `!glob` entry is an exclusion (:func:`excluded`), never a member."""
    named = _conventions(product).amendable_paths
    if named is not None:
        return tuple(g for g in named if not str(g).startswith('!'))
    return tuple(g for kind in kinds(product) for g in kind.globs)


def excluded(product):
    """The `!glob` entries of `conventions.amendable_paths`, without the `!`: a path they match
    is outside the set even when a member glob matches it — `docs/process/*` with
    `!docs/process/evidence/*` protects the process rules, not every Task's evidence file."""
    named = _conventions(product).amendable_paths or ()
    return tuple(str(g)[1:] for g in named if str(g).startswith('!'))


def source(product):
    """``'yaml'`` when the product named its own list, else ``'default'``."""
    return 'default' if _conventions(product).amendable_paths is None else 'yaml'


def kind_of(product, relpath):
    """The first kind whose globs match ``relpath``, else ``None``. Consulted for a path
    :func:`paths` already matched; it reads the kinds, not the product's own list."""
    from asf import approvals  # local: asf.approvals imports this module at load (PD6)
    for kind in kinds(product):
        if any(approvals._match_glob(g, relpath) for g in kind.globs):
            return kind
    return None


def _in_set(product, relpath):
    from asf import approvals
    if any(approvals._match_glob(g, relpath) for g in excluded(product)):
        return False
    return any(approvals._match_glob(g, relpath) for g in paths(product))


def write_target(product, tool_name, tool_input, cwd):
    """`(relpath, kind)` this call would write in the set, or None — the path tools by their
    path argument, `Bash` by a word next to a `approvals._WRITING_TOKENS` token, exactly as
    `approvals.operator_config_target` reads one."""
    from asf import approvals  # local: see kind_of
    tool_input = tool_input or {}
    candidates = []
    if tool_name in ('Write', 'Edit', 'MultiEdit'):
        candidates = [tool_input.get('file_path')]
    elif tool_name == 'NotebookEdit':
        candidates = [tool_input.get('notebook_path')]
    elif tool_name == 'Bash':
        command = tool_input.get('command') or ''
        if any(re.search(t, command) for t in approvals._WRITING_TOKENS):
            candidates = approvals._BASH_WORD.findall(command)
    for path in candidates:
        if not path:
            continue
        relpath = approvals._repo_relpath(path, cwd)
        if relpath == '..' or relpath.startswith('../') or os.path.isabs(relpath):
            continue
        if _in_set(product, relpath):
            return relpath, kind_of(product, relpath) or LISTED
    return None


def format_set(product):
    """``kind | globs | why`` rows, aligned — what `asf approvals` prints."""
    rows = [(k.name, ', '.join(k.globs) or '(none)', k.why) for k in kinds(product)]
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    return [f'{a:<{w0}} | {b:<{w1}} | {c}' for a, b, c in rows]


def _reach(a, b):
    import fnmatch
    return a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)


def reaches(product, globs):
    """The first of `globs` (a Task's `writes:`) that overlaps the set — a glob intersection,
    not a file test: `rules/*` and `rules/R-0042.md` both reach `rules/*.md`."""
    import fnmatch
    out = excluded(product)
    for g in globs:
        if any(fnmatch.fnmatch(g, x) for x in out):
            continue
        if any(_reach(g, s) for s in paths(product)):
            return g
    return None
