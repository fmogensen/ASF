"""asf.conventions — every path, prefix, file name and id a product may differ on.

One dataclass, one documented default per field. This module is the **only** place in the
package where such a default may appear as a literal: `tools/check_conventions.sh` fails the
build when one shows up anywhere else under ``asf/``, with two more exceptions — the pre-asf log
adapter, ``asf.metrics.import_sessions``, whose literals describe a foreign file format, and
``asf.hooks``, which writes the worker runtime's own settings file — neither describes this
factory's own conventions.

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
                                                          # of these globs is merge_amendable_set;
                                                          # unset = the defaults in asf/amendable.py,
                                                          # [] = opt out (the set is empty)
      doc_paths: [README.md, docs/guide/*]   # more docs roots beside the specs/plans/reviews dirs
      shared_paths: [uv.lock]                # lockfiles: no footprint overlap, one per merge
      lane:
        review:                   # which landing class needs an ASF review before the gate
          docs: none              # none | required (default none)
          code: required          # none | required (default required)
        stale_after: 2d           # an open lane state older than this is stale (<n>s|m|h|d)
      worktree_setup: make deps   # run in every fresh worker worktree (unset = nothing)
      merge: auto                 # auto | manual (default manual): under auto the lane merges
                                  # every open PR on the trunk whose required checks are green
                                  # and whose factory review approved it — no operator click
      branch_retention:           # origin's heads nothing owns any more (asf.workers.retention)
        archive_days: 14          # the lane's archive/<b> heads, deleted this many days on
        legacy_prefixes: [hb/]    # heads under these prefixes (default none) …
        legacy_days: 7            # … deleted once their tip is older than this
        per_tick: 50              # the most deletes one pass makes

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
#: The most documents and tests whose line counts the preamble measures per launch (F-0022).
DEFAULT_PREAMBLE_MAX_FILES = 12
DEFAULT_PRS_PER_TICK = 6
#: How many leading path segments make one area, for the groom's split proposals (F-0086 D5).
DEFAULT_AREA_DEPTH = 2
#: The most globs a Task may write and still count as small enough to batch (F-0086 D3).
DEFAULT_BATCH_MAX_GLOBS = 2
#: The most paths the ``widen_footprint`` rule adds to a Task's ``writes:`` in one widening; more
#: is a reshape of the Task, not a wider one (:mod:`asf.feeder.widen`).
DEFAULT_WIDEN_MAX_FILES = 5
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

#: The window `asf status`'s Features row measures time-to-land over.
DEFAULT_LAND_WINDOW_DAYS = 7

#: The keys of the yaml's ``harvest:`` block and the field each one is.
HARVEST_KEYS = {'gate': 'harvest_gate', 'branches_per_tick': 'branches_per_tick',
                'gate_timeout_s': 'gate_timeout_s'}

#: Paths (globs) that count as documentation beside ``specs_dir``, ``plans_dir`` and
#: ``reviews_dir``: a branch touching only docs roots is the ``docs`` landing class
#: (:func:`asf.harvest.lane.landing_class`), and a trunk that moved only there does not re-gate.
DEFAULT_DOC_PATHS = ()
#: Paths (globs) many Tasks may touch without owning them — lockfiles. They are left out of
#: footprint overlap in the feeder and serialised at merge in the lane (one per tick).
DEFAULT_SHARED_PATHS = ()
#: The two landing classes a lane branch falls in, and what a review policy may say of each.
LANDING_CLASSES = ('docs', 'code')
REVIEW_POLICIES = ('none', 'required')
#: ``lane: {review: …}``: per landing class, whether an ASF review of the head must read
#: approved before the gate (T3/T4 of the lane).
DEFAULT_LANE_REVIEW = {'docs': 'none', 'code': 'required'}
#: ``lane: {stale_after: …}``: an open lane state (not MERGED/STALE/REAPED) whose head has not
#: moved and that no session holds for longer than this is STALE (T12), ``<n>s|m|h|d``.
DEFAULT_LANE_STALE_AFTER = '2d'
#: A shell command run in every fresh worker worktree before its session starts (dependency
#: install, codegen). None → nothing runs.
DEFAULT_WORKTREE_SETUP = None
#: ``conventions.merge``: who clicks merge on a green, reviewed PR — ``manual`` (the operator: the
#: merge-time approval holds stay as the matrix sets them) or ``auto`` (the lane: those holds are
#: ``auto`` for the product, and an open PR no factory item made gets a factory review first).
MERGE_AUTO = 'auto'
MERGE_MANUAL = 'manual'
MERGE_MODES = (MERGE_AUTO, MERGE_MANUAL)
DEFAULT_MERGE = MERGE_MANUAL

#: ``customer_content: {paths, forbidden_markers}`` — the pages a customer reads (a site's
#: legal pages, its marketing copy) and the text that must never reach them
#: (:mod:`asf.customer_content`). ``paths`` are globs; unset or empty, nothing is checked.
#: ``forbidden_markers`` are Python regexes, one per line matched; unset, these defaults apply:
#: a bracketed internal note (``[legal: …]``, ``[TODO: …]``), TODO/FIXME/XXX, lorem ipsum, and an
#: unresolved template placeholder (``{{COMPANY_NAME}}``, ``[Insert date]``, ``<<NAME>>``).
DEFAULT_CUSTOMER_CONTENT_PATHS = ()
DEFAULT_FORBIDDEN_MARKERS = (
    r'(?i)\[\s*(legal|todo|tbd|note|internal|lawyer|fixme)\s*:',
    r'\b(TODO|FIXME|XXX)\b',
    r'(?i)\blorem\s+ipsum\b',
    r'\{\{\s*[A-Z][A-Z0-9_]*\s*\}\}',
    r'(?i)\[\s*(insert|placeholder|tbd|your company|company name)\b[^\]]*\]',
    r'<<\s*[A-Z][A-Z0-9_ ]*\s*>>',
)

#: ``branch_retention:`` — how long origin's heads nobody owns any more are kept
#: (:mod:`asf.workers.retention`). ``archive_days``: the lane's ``archive/<b>`` heads (a superseded
#: branch, kept for reference) go this many days after they were archived. ``legacy_prefixes``:
#: heads under these prefixes — a retired worker system's, a hand-made one's — go once their tip
#: is ``legacy_days`` old. ``per_tick``: the most deletes one pass makes. A head an open PR, an
#: open record item or an in-flight session names is never deleted, nor the trunk, a protected
#: branch or a release branch.
DEFAULT_BRANCH_RETENTION = {'archive_days': 14, 'legacy_prefixes': [], 'legacy_days': 7,
                            'per_tick': 50}

#: The keys of the yaml's ``lane:`` block and the field each one is.
LANE_KEYS = {'review': 'lane_review', 'stale_after': 'lane_stale_after'}

#: The conventions whose value is a map. A value of any other shape — a string the reader kept,
#: a scalar written by hand — is read as the default (:meth:`Conventions.map_of`) so no reader
#: raises on it (a ``models: light`` string once failed every launch for forty minutes), and it
#: fails loud: :meth:`Conventions.shape_findings` names it, and the doctor's ``conventions`` row
#: is red with the key and the line.
MAP_CONVENTIONS = ('models', 'branch_prefixes', 'harvest', 'branch_retention')
#: The conventions that take one word or a map of those words per landing class (``default:``
#: for the rest). Any other value — ``'{docs: wait}'`` quoted into a string — is never a silent
#: default: it is a red doctor finding.
WORD_OR_MAP_CONVENTIONS = {'landing_checks_missing': ('wait', 'local-gate')}



def model_value_ok(value):
    """``conventions.models.<kind>`` is one label, or a map of labels by class
    (``{S1: heavy, S2: light, feature: heavy, default: light}``); anything else is misshapen."""
    if isinstance(value, dict):
        return all(isinstance(v, str) and v.strip() for v in value.values())
    return isinstance(value, str) and bool(value.strip())


DURATION_RE = re.compile(r'^(\d+)([smhd])$')
DURATION_UNITS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}

DEFAULT_EVALS_DIR = 'evals'      # where a product keeps its evals (F-0024: part of the amendable set)
DEFAULT_BRIEFS_DIR = None        # where a product keeps brief documents on its trunk
DEFAULT_MATRIX_PATH = None       # the parity matrix file, read for Story status
DEFAULT_DESIGN_SPEC_NAME = None  # the one spec `asf migrate` reads as the design spec
DEFAULT_DECISIONS_FILE = None    # a decisions file `asf migrate` mines for D-rows
DEFAULT_REPORTS_DIR = None       # a directory of hotfix / diagnostic reports `asf migrate` adopts
DEFAULT_REPORT_PATTERN = None    # regex over a file name in reports_dir; None → every .md
DEFAULT_CI_WORKFLOW = None       # the workflow whose runs on the trunk are the green evidence
DEFAULT_CI_DEV_JOB = None        # the job in that workflow whose success marks the dev sha
DEFAULT_DEPLOY_WORKFLOW = None   # the workflow whose newest success marks the prod sha
#: A product with no deploy (B-0077): the file in its repo the rollup files each version's
#: release notes in, newest first. Read from the yaml's ``changelog_file:``.
DEFAULT_CHANGELOG_FILE = 'CHANGELOG.md'
#: A product with no deploy: the least time between two releases of its trunk (``<n>s|m|h|d``).
#: The yaml's ``release_min_interval:`` overrides it.
DEFAULT_RELEASE_MIN_INTERVAL = '60m'
#: The "Upgrade" line of a version's release notes: ``{repo_slug}`` and ``{tag}`` substituted.
#: The yaml's ``release_install:`` overrides it; an empty value leaves the line out.
DEFAULT_RELEASE_INSTALL = 'pipx install --force "git+https://github.com/{repo_slug}.git@{tag}"'


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


def duration_seconds(value):
    """``<n>s|m|h|d`` → seconds; ValueError on anything else."""
    m = DURATION_RE.match(str(value).strip())
    if not m:
        raise ValueError(f'not a duration (<n>s|m|h|d): {value!r}')
    return int(m.group(1)) * DURATION_UNITS[m.group(2)]


def _path_list_problem(value):
    if not isinstance(value, list):
        return f'must be a list of paths, not {value!r}'
    bad = [v for v in value if not isinstance(v, str) or not v.strip()]
    if bad:
        return f'must be a list of paths, and {bad[0]!r} is not one'
    return None


#: A bare variable name (``auth_env``, ``worker_pool.accounts[].auth_env`` in ``asf.env``): a
#: leading letter/underscore, then letters, digits or underscores — what a shell accepts on the
#: left of ``export``.
_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def validate_mapping(data):
    """The shaped keys of a ``conventions:`` mapping checked: ``[(dotted key, problem)]``, empty
    when they are well-formed. Only ``doc_paths``, ``shared_paths``, ``lane``, ``worktree_setup``,
    ``auth_env``, ``full_suite_commands``, ``customer_content`` and ``feeder`` are checked — every other key is kept
    verbatim (see the module doc), so a product file written for a newer ``asf`` still loads."""
    problems = []
    if not isinstance(data, dict):
        return problems
    for key in ('doc_paths', 'shared_paths'):
        if data.get(key) is not None:
            why = _path_list_problem(data[key])
            if why:
                problems.append((key, why))
    setup = data.get('worktree_setup')
    if setup is not None and (isinstance(setup, (dict, list, bool)) or not str(setup).strip()):
        problems.append(('worktree_setup', f'must be a command string, not {setup!r}'))
    suite = data.get('full_suite_commands')
    if suite is not None:
        if not isinstance(suite, list):
            problems.append(('full_suite_commands', f'must be a list of regexes, not {suite!r}'))
        else:
            for pattern in suite:
                if not isinstance(pattern, str) or not pattern.strip():
                    problems.append(('full_suite_commands',
                                     f'must be a list of regexes, and {pattern!r} is not one'))
                    continue
                try:
                    re.compile(pattern)
                except re.error as e:
                    problems.append(('full_suite_commands', f'{pattern!r} is not a regex ({e})'))
    auth = data.get('auth_env')
    if auth is not None:
        if not isinstance(auth, dict):
            problems.append(('auth_env', f'must map variable names to files, not {auth!r}'))
        else:
            for name, path in auth.items():
                if not isinstance(name, str) or not _VAR_RE.match(name):
                    problems.append(('auth_env',
                                     f'must map variable names to files, and {name!r} is not one'))
                elif not isinstance(path, str) or not path.strip():
                    problems.append(('auth_env', f'{name} must name a file, not {path!r}'))
    cc = data.get('customer_content')
    if cc is not None:
        if not isinstance(cc, dict):
            problems.append(('customer_content',
                             f'must be a map (paths, forbidden_markers), not {cc!r}'))
        else:
            if cc.get('paths') is not None:
                why = _path_list_problem(cc['paths'])
                if why:
                    problems.append(('customer_content.paths', why))
            marks = cc.get('forbidden_markers')
            if marks is not None and not isinstance(marks, list):
                problems.append(('customer_content.forbidden_markers',
                                 f'must be a list of regexes, not {marks!r}'))
            for pattern in (marks if isinstance(marks, list) else ()):
                try:
                    re.compile(str(pattern))
                except re.error as e:
                    problems.append(('customer_content.forbidden_markers',
                                     f'{pattern!r} is not a regex ({e})'))
    feeder = data.get('feeder')
    if feeder is not None:
        cap = feeder.get('max_specs_in_flight') if isinstance(feeder, dict) else None
        if not isinstance(feeder, dict):
            problems.append(('feeder', f'must be a map (max_specs_in_flight), not {feeder!r}'))
        elif cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
            problems.append(('feeder.max_specs_in_flight',
                             f'must be a whole number >= 0, not {cap!r}'))
    problems.extend(_retention_problems(data.get('branch_retention')))
    lane = data.get('lane')
    if lane is None:
        return problems
    if not isinstance(lane, dict):
        return problems + [('lane', f'must be a map (review, stale_after), not {lane!r}')]
    for key in lane:
        if key not in LANE_KEYS:
            problems.append((f'lane.{key}', 'is not a lane key (review, stale_after)'))
    review = lane.get('review')
    if review is not None:
        if not isinstance(review, dict):
            problems.append(('lane.review', f'must be a map (docs, code), not {review!r}'))
        else:
            for cls, policy in review.items():
                if cls not in LANDING_CLASSES:
                    problems.append((f'lane.review.{cls}',
                                     f"is not a landing class ({', '.join(LANDING_CLASSES)})"))
                elif policy not in REVIEW_POLICIES:
                    problems.append((f'lane.review.{cls}',
                                     f"must be {' or '.join(REVIEW_POLICIES)}, not {policy!r}"))
    stale = lane.get('stale_after')
    if stale is not None:
        try:
            if duration_seconds(stale) <= 0:
                problems.append(('lane.stale_after', f'must be longer than zero, not {stale!r}'))
        except ValueError:
            problems.append(('lane.stale_after', f'must be a duration <n>s|m|h|d, not {stale!r}'))
    return problems


def _retention_problems(value):
    """``branch_retention:`` checked: ``[(dotted key, problem)]``."""
    if not isinstance(value, dict):
        return []  # absent, or misshapen: the latter is a shape finding (MAP_CONVENTIONS)
    problems = []
    for key, v in value.items():
        where = f'branch_retention.{key}'
        least = 0 if key == 'per_tick' else 1
        if key not in DEFAULT_BRANCH_RETENTION:
            problems.append((where, 'is not a retention key '
                                    f"({', '.join(DEFAULT_BRANCH_RETENTION)})"))
        elif key == 'legacy_prefixes':
            if not isinstance(v, list) or any(not isinstance(p, str) or not p.strip() for p in v):
                problems.append((where, f'must be a list of branch prefixes, not {v!r}'))
        elif isinstance(v, bool) or not isinstance(v, int) or v < least:
            problems.append((where, f'must be a whole number >= {least}, not {v!r}'))
    return problems


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
    #: The most paths whose line counts a brief's preamble measures (F-0022).
    preamble_max_files: int = DEFAULT_PREAMBLE_MAX_FILES
    prs_per_tick: int = DEFAULT_PRS_PER_TICK
    land_window_days: int = DEFAULT_LAND_WINDOW_DAYS
    area_depth: int = DEFAULT_AREA_DEPTH
    batch_max_globs: int = DEFAULT_BATCH_MAX_GLOBS
    #: The most paths one footprint widening may add (:mod:`asf.feeder.widen`).
    widen_max_files: int = DEFAULT_WIDEN_MAX_FILES
    harvest_gate: str = DEFAULT_HARVEST_GATE
    branches_per_tick: int = DEFAULT_BRANCHES_PER_TICK
    gate_timeout_s: int = DEFAULT_GATE_TIMEOUT_S
    briefs_dir: str = DEFAULT_BRIEFS_DIR
    evals_dir: str = DEFAULT_EVALS_DIR
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
    #: `merge_amendable_set` rather than `merge_routine_pr` — the factory's own rules. Three
    #: states (F-0024): unset (``None``) is the defaults in `asf/amendable.py`, a list is that
    #: list, and ``[]`` is an opt-out — the set is empty.
    amendable_paths: list = None
    #: More docs roots (globs) beside the specs/plans/reviews dirs (:data:`DEFAULT_DOC_PATHS`).
    doc_paths: list = field(default_factory=lambda: list(DEFAULT_DOC_PATHS))
    #: Lockfile-like globs outside footprint overlap (:data:`DEFAULT_SHARED_PATHS`).
    shared_paths: list = field(default_factory=lambda: list(DEFAULT_SHARED_PATHS))
    #: ``lane.review``: ``{docs: none|required, code: none|required}``, merged over
    #: :data:`DEFAULT_LANE_REVIEW` (a class the yaml leaves out keeps its default).
    lane_review: dict = field(default_factory=lambda: dict(DEFAULT_LANE_REVIEW))
    #: ``lane.stale_after``: ``<n>s|m|h|d`` (:data:`DEFAULT_LANE_STALE_AFTER`).
    lane_stale_after: str = DEFAULT_LANE_STALE_AFTER
    #: The command run in every fresh worker worktree (:data:`DEFAULT_WORKTREE_SETUP`).
    worktree_setup: str = DEFAULT_WORKTREE_SETUP
    #: ``merge``: ``auto`` | ``manual`` (:data:`DEFAULT_MERGE`); any other value is a red doctor
    #: finding and reads as the default.
    merge: str = DEFAULT_MERGE
    #: ``branch_retention``: merged over :data:`DEFAULT_BRANCH_RETENTION` (see there).
    branch_retention: dict = field(default_factory=lambda: dict(DEFAULT_BRANCH_RETENTION))
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
        misshapen = {key: data.pop(key) for key in MAP_CONVENTIONS
                     if data.get(key) is not None and not isinstance(data.get(key), dict)}
        for key, words in WORD_OR_MAP_CONVENTIONS.items():
            value = data.get(key)
            values = value.values() if isinstance(value, dict) else [value]
            if value is not None and any(str(v).strip().lower() not in words for v in values):
                misshapen[key] = value
        merge = data.get('merge')
        if merge is not None and str(merge).strip().lower() not in MERGE_MODES:
            misshapen['merge'] = data.pop('merge')
        elif merge is not None:
            data['merge'] = str(merge).strip().lower()
        models = data.get('models')
        for kind, value in (models.items() if isinstance(models, dict) else ()):
            # ``models.<kind>``: a label, or a map of labels by class (asf.briefs.build)
            if not model_value_ok(value):
                misshapen[f'models.{kind}'] = value
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
        retention = data.pop('branch_retention', None)
        if isinstance(retention, dict):
            kwargs['branch_retention'] = {**DEFAULT_BRANCH_RETENTION,
                                          **{k: v for k, v in retention.items() if v is not None}}
        lane = data.pop('lane', None)
        if isinstance(lane, dict):  # ``lane: {review, stale_after}`` → lane_review/lane_stale_after
            rest = {}
            for key, value in lane.items():
                name = LANE_KEYS.get(key)
                if name == 'lane_review' and isinstance(value, dict):
                    kwargs[name] = {**DEFAULT_LANE_REVIEW, **value}
                elif name and value is not None:
                    kwargs[name] = value
                elif not name:
                    rest[key] = value
            if rest:
                data['lane'] = rest
        elif lane is not None:
            data['lane'] = lane
        for key in list(data):
            if key in known:
                value = data.pop(key)
                if value is not None:
                    kwargs[key] = value
        conv = cls(extra=data, **kwargs)
        conv._misshapen = misshapen
        return conv

    def map_of(self, key):
        """A map-valued convention (:data:`MAP_CONVENTIONS`) as a dict: its value when it is a
        map, else ``{}`` — the reader's defaults apply, never an exception."""
        value = self.get(key)
        return value if isinstance(value, dict) else {}

    def shape_findings(self):
        """``[(key, problem)]`` for every map-valued convention the product wrote in another
        shape (:data:`MAP_CONVENTIONS`, :data:`WORD_OR_MAP_CONVENTIONS`) — the doctor's red
        ``conventions`` row."""
        out = []
        for key, value in sorted(getattr(self, '_misshapen', {}).items()):
            words = WORD_OR_MAP_CONVENTIONS.get(key)
            want = (f"one of {', '.join(words)} or a map of them per landing class" if words
                    else f"one of {', '.join(MERGE_MODES)}" if key == 'merge'
                    else 'a model label or a map of labels by class' if key.startswith('models.')
                    else 'a map')
            out.append((key, f'must be {want}, not {value!r}'))
        return out

    # ---- the lane ------------------------------------------------------------

    def review_required(self, landing_class):
        """True when ``lane.review`` says a branch of ``landing_class`` (``docs``/``code``) needs
        an approved ASF review of its head before the gate."""
        policy = self.lane_review.get(landing_class, DEFAULT_LANE_REVIEW.get(landing_class))
        return policy == 'required'

    def lane_stale_after_s(self):
        """``lane.stale_after`` in seconds."""
        return duration_seconds(self.lane_stale_after)

    def merge_auto(self):
        """True under ``merge: auto`` — the lane merges a green, reviewed PR itself."""
        return str(self.merge or '').strip().lower() == MERGE_AUTO

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

    def retention(self, key):
        """One ``branch_retention`` value (:data:`DEFAULT_BRANCH_RETENTION`); a malformed one
        reads as its default — the doctor's ``conventions`` row names it."""
        default = DEFAULT_BRANCH_RETENTION[key]
        held = self.branch_retention if isinstance(self.branch_retention, dict) else {}
        value = held.get(key)
        if key == 'legacy_prefixes':
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                return tuple(default)
            return tuple(p.strip() for p in value if isinstance(p, str) and p.strip())
        least = 0 if key == 'per_tick' else 1
        if isinstance(value, bool) or not isinstance(value, int) or value < least:
            return default
        return value

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
