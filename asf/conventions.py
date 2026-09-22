"""asf.conventions — every path, prefix, file name and id a product may differ on.

One dataclass, one documented default per field. This module is the **only** place in the
package where such a default may appear as a literal: `tools/check_conventions.sh` fails the
build when one shows up anywhere else under ``asf/`` (the pre-asf log adapter,
``asf.metrics.import_sessions``, is the second and last exception — its literals describe a
foreign file format, not this factory's conventions).

The product yaml carries the overrides::

    conventions:
      specs_dir: specs
      plans_dir: plans
      branch_prefixes:
        code: feature/
        fix: bugfix/
      default_bug_epic: E-0042
      test_command: make test
      briefs_dir: docs/briefs
      harvest:
        gate: per-branch          # default combined: one gate per tick (B-0040)
        branches_per_tick: 3
        gate_timeout_s: 900       # default 600: a gate past it is killed and red (B-0072)
      amendable_paths: [rules/*, docs/CONSTITUTION.md]  # F-0031: a landed branch touching one
                                                          # of these globs is merge_amendable_set

Unknown keys are kept (in :attr:`Conventions.extra`) rather than rejected: a product yaml is
written by an operator and may carry conventions a module older than it does not read yet, and
losing them on load would silently change behaviour. ``.get()``/``[]`` see the fields and the
extras alike, so the callers that treat the conventions as a mapping keep working.
"""
import re
from dataclasses import dataclass, field, fields

#: The branch a job of each kind pushes. A value ending in ``/`` or ``-`` is used as written;
#: anything else gets a ``/`` (so ``code: feature`` and ``code: feature/`` mean the same thing).
#: ``legacy`` is a list of prefixes the product used before and still has branches under — they
#: are recognised, never minted.
DEFAULT_BRANCH_PREFIXES = {
    'code': 'worker/',
    'fix': 'fix/',
    'spec': 'spec/',
    'plan': 'plan/',
    'legacy': [],
}

DEFAULT_SPECS_DIR = 'docs/specs'
DEFAULT_PLANS_DIR = 'docs/plans'
DEFAULT_REVIEWS_DIR = 'docs/reviews'
#: ``{reviews_dir}``, ``{n}`` (round) and ``{slug}`` are substituted.
DEFAULT_REVIEW_PATTERN = '{reviews_dir}/{n}-{slug}.md'
#: The heading a brief's task section carries in a spec or plan.
DEFAULT_TASK_HEADING = '### Task'
#: Where a human drops a card for the groom to intake, relative to the record repo.
DEFAULT_INTAKE_DIR = 'inbox'
#: The file the product's goals are read from, relative to the record repo. No convention by
#: default: a product without one simply has no goals file, and nothing looks for it.
DEFAULT_GOALS_FILE = None
#: The Epic a filed Bug is parented under. None → the Bug is filed with no parent and `asf
#: check` asks a human once, rather than the factory guessing an id.
DEFAULT_BUG_EPIC = None
#: The trunk branch. Mirrors the product yaml's top-level ``main:``.
DEFAULT_MAIN = 'main'
#: The command that gates a branch before it lands. None → no test gate for this product.
DEFAULT_TEST_COMMAND = None
DEFAULT_PREAMBLE_MAX_LINES = 120
DEFAULT_PRS_PER_TICK = 6
#: How harvest gates a tick's eligible branches (B-0040): ``combined`` — every branch rebased in
#: turn onto one throwaway head, one gate, one fast-forward push, bisecting on red — or
#: ``per-branch``, one gate and one push per landing. Spelt ``harvest: {gate: …}`` in the yaml.
DEFAULT_HARVEST_GATE = 'combined'
#: The most branches one tick's harvest gates; the rest wait for the next tick. A safety valve on
#: the tick's clock (B-0031), not the cost driver once the gate is one per tick. Spelt
#: ``harvest: {branches_per_tick: …}`` in the yaml.
DEFAULT_BRANCHES_PER_TICK = 12

#: The most seconds one gate run (the test command, each check) may take before harvest kills
#: its process group and holds the branch as red (B-0072: a test that recursed through the
#: pre-commit hook hung the tick, and every tick after it). The tick's clock, by default.
DEFAULT_GATE_TIMEOUT_S = 600

#: The keys of the yaml's ``harvest:`` block and the field each one is.
HARVEST_KEYS = {'gate': 'harvest_gate', 'branches_per_tick': 'branches_per_tick',
                'gate_timeout_s': 'gate_timeout_s'}

DEFAULT_BRIEFS_DIR = None        # where a product keeps brief documents on its trunk
DEFAULT_MATRIX_PATH = None       # the parity matrix file, read for Story status
DEFAULT_DESIGN_SPEC_NAME = None  # the one spec `asf migrate` reads as the design spec
DEFAULT_DECISIONS_FILE = None    # a decisions file `asf migrate` mines for D-rows
DEFAULT_REPORTS_DIR = None       # a directory of hotfix / diagnostic reports `asf migrate` adopts
DEFAULT_REPORT_PATTERN = None    # regex over a file name in reports_dir; None → every .md
DEFAULT_CI_WORKFLOW = None       # the workflow whose runs on the trunk are the green evidence
DEFAULT_CI_DEV_JOB = None        # the job in that workflow whose success marks the dev sha
DEFAULT_DEPLOY_WORKFLOW = None   # the workflow whose newest success marks the prod sha


def _normalise_prefix(value):
    """``feature`` → ``feature/``; ``feature/`` and ``m-`` are already prefixes."""
    text = str(value)
    return text if text.endswith(('/', '-')) else text + '/'


def forbidden_patterns():
    """One extended regex per path-shaped string default, for tools/check_conventions.sh — a
    copy of a default in code is a convention in code.

    Walks the string values of :data:`DEFAULT_BRANCH_PREFIXES` (``legacy`` is a list and is
    skipped) and every module-level ``DEFAULT_*`` string, keeps the ones containing ``/``,
    ``{`` or ``#``, and returns ``['"]`` + the escaped default + a trailing ``\\b`` when the
    default ends in a word character (so it anchors to a code literal, not to prose)."""
    values = [v for k, v in DEFAULT_BRANCH_PREFIXES.items() if k != 'legacy']
    values += [v for k, v in globals().items() if k.startswith('DEFAULT_') and isinstance(v, str)]
    patterns = []
    for value in values:
        if not any(sep in value for sep in ('/', '{', '#')):
            continue
        suffix = r'\b' if value[-1].isalnum() or value[-1] == '_' else ''
        patterns.append("['\"]" + re.escape(value) + suffix)
    return patterns


@dataclass
class Conventions:
    """One product's conventions. Build with :meth:`from_mapping`; read as an object
    (``conv.specs_dir``) or as a mapping (``conv.get('specs_dir')``)."""

    branch_prefixes: dict = field(default_factory=lambda: dict(DEFAULT_BRANCH_PREFIXES))
    specs_dir: str = DEFAULT_SPECS_DIR
    plans_dir: str = DEFAULT_PLANS_DIR
    reviews_dir: str = DEFAULT_REVIEWS_DIR
    review_pattern: str = DEFAULT_REVIEW_PATTERN
    task_heading: str = DEFAULT_TASK_HEADING
    intake_dir: str = DEFAULT_INTAKE_DIR
    goals_file: str = DEFAULT_GOALS_FILE
    default_bug_epic: str = DEFAULT_BUG_EPIC
    main: str = DEFAULT_MAIN
    test_command: str = DEFAULT_TEST_COMMAND
    preamble_max_lines: int = DEFAULT_PREAMBLE_MAX_LINES
    prs_per_tick: int = DEFAULT_PRS_PER_TICK
    harvest_gate: str = DEFAULT_HARVEST_GATE
    branches_per_tick: int = DEFAULT_BRANCHES_PER_TICK
    gate_timeout_s: int = DEFAULT_GATE_TIMEOUT_S
    briefs_dir: str = DEFAULT_BRIEFS_DIR
    matrix_path: str = DEFAULT_MATRIX_PATH
    design_spec_name: str = DEFAULT_DESIGN_SPEC_NAME
    decisions_file: str = DEFAULT_DECISIONS_FILE
    reports_dir: str = DEFAULT_REPORTS_DIR
    report_pattern: str = DEFAULT_REPORT_PATTERN
    ci_workflow: str = DEFAULT_CI_WORKFLOW
    ci_dev_job: str = DEFAULT_CI_DEV_JOB
    deploy_workflow: str = DEFAULT_DEPLOY_WORKFLOW
    stage_limits: dict = field(default_factory=dict)
    #: Globs (F-0031 §2.1) whose match makes a landed branch's merge class
    #: `merge_amendable_set` rather than `merge_routine_pr` — the factory's own rules. Empty by
    #: default: no branch is treated specially until an operator names one.
    amendable_paths: list = field(default_factory=list)
    #: Everything the yaml carried that is not a field above, kept verbatim.
    extra: dict = field(default_factory=dict)

    # ---- construction --------------------------------------------------------

    @classmethod
    def field_names(cls):
        return tuple(f.name for f in fields(cls) if f.name != 'extra')

    @classmethod
    def from_mapping(cls, data):
        """A ``conventions:`` mapping → a Conventions. Extra keys are kept, not rejected.

        ``branch_prefixes`` is taken as the product wrote it — it stays readable as the mapping
        the yaml carried (a brief prints it; a PR step iterates it) — and a kind the product
        does not name falls back to the default in :meth:`prefix`, not here."""
        data = dict(data or {})
        known = set(cls.field_names())
        kwargs = {}
        harvest = data.pop('harvest', None)
        if isinstance(harvest, dict):  # ``harvest: {gate, branches_per_tick}`` → the two fields
            rest = {}
            for key, value in harvest.items():
                name = HARVEST_KEYS.get(key)
                if name and value is not None:
                    kwargs[name] = value
                elif not name:
                    rest[key] = value
            if rest:
                data['harvest'] = rest
        elif harvest is not None:
            data['harvest'] = harvest
        for key in list(data):
            if key in known:
                value = data.pop(key)
                if value is not None:
                    kwargs[key] = value
        return cls(extra=data, **kwargs)

    # ---- branches ------------------------------------------------------------

    def prefix(self, kind):
        """The branch prefix for a job kind: the product's, else the documented default for
        that kind, else ``<kind>/``."""
        value = self.branch_prefixes.get(kind, DEFAULT_BRANCH_PREFIXES.get(kind))
        if isinstance(value, (list, tuple)) or not value:
            return _normalise_prefix(kind)
        return _normalise_prefix(value)

    def branch(self, kind, name):
        """The branch a ``kind`` job called ``name`` works on: ``worker/<name>`` by default."""
        return f'{self.prefix(kind)}{name}'

    def legacy_prefixes(self):
        value = self.branch_prefixes.get('legacy') or []
        if isinstance(value, str):
            value = [value]
        return tuple(_normalise_prefix(v) for v in value)

    def all_prefixes(self):
        """Every prefix this product's branches can carry, longest first (so ``fix/`` wins over
        a hypothetical ``f/``). Deduplicated, order otherwise by kind name."""
        out = []
        for kind in sorted(self.branch_prefixes):
            if kind == 'legacy':
                continue
            out.append(self.prefix(kind))
        out.extend(self.legacy_prefixes())
        return tuple(sorted(dict.fromkeys(out), key=lambda p: (-len(p), p)))

    def branch_kind(self, branch):
        """Which kind a branch name belongs to (``'legacy'`` for a retired prefix), or None."""
        branch = branch or ''
        best = None
        for kind in sorted(self.branch_prefixes):
            if kind == 'legacy':
                continue
            p = self.prefix(kind)
            if branch.startswith(p) and (best is None or len(p) > len(best[1])):
                best = (kind, p)
        for p in self.legacy_prefixes():
            if branch.startswith(p) and (best is None or len(p) > len(best[1])):
                best = ('legacy', p)
        return best[0] if best else None

    def strip_prefix(self, branch):
        """``feature/add-login`` → ``add-login``; a branch under no known prefix is returned
        as it is."""
        branch = branch or ''
        for p in self.all_prefixes():
            if branch.startswith(p):
                return branch[len(p):]
        return branch

    def is_trunk(self, branch):
        return (branch or '') == self.main

    # ---- paths ---------------------------------------------------------------

    def review_path(self, slug, n):
        """Where round ``n`` of a review of ``slug`` lives."""
        return (str(self.review_pattern).replace('{reviews_dir}', self.reviews_dir)
                .replace('{n}', str(n)).replace('{slug}', str(slug)))

    def doc_dir(self, key):
        """``spec`` → ``specs_dir``, ``plan`` → ``plans_dir``, ``review`` → ``reviews_dir``."""
        return self.get(f'{key}s_dir') or self.get(key + '_dir') or key + 's'

    # ---- the mapping face ----------------------------------------------------

    def as_dict(self):
        out = {name: getattr(self, name) for name in self.field_names()}
        out.update(self.extra)
        return out

    def get(self, key, default=None):
        if key in self.field_names():
            value = getattr(self, key)
            return default if value is None else value
        return self.extra.get(key, default)

    def __getitem__(self, key):
        if key in self.field_names():
            return getattr(self, key)
        return self.extra[key]

    def __contains__(self, key):
        return key in self.field_names() or key in self.extra

    def keys(self):
        return self.as_dict().keys()

    def items(self):
        return self.as_dict().items()

    def values(self):
        return self.as_dict().values()


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--forbidden':
        for pattern in forbidden_patterns():
            print(pattern)
    else:
        for name, value in list(globals().items()):
            if name.startswith('DEFAULT_'):
                print(f'{name} = {value}')
    sys.exit(0)
