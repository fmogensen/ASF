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
      git:
        push_timeout_s: 300       # default 120: a factory git push past it is killed, the ref
                                  # logged and left as it was (asf.gitpush)
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
      review:
        skip_under_lines: 80      # a size: s Feature's Task under this many changed lines lands
                                  # on CI and the gate, no review session (0 = always review)
      worktree_setup: make deps   # run in every fresh worker worktree (unset = nothing)
      merge: auto                 # auto | manual (default manual): under auto the lane merges
                                  # every open PR on the trunk whose required checks are green
                                  # and whose factory review approved it — no operator click
      branch_retention:           # origin's heads nothing owns any more (asf.workers.retention)
        archive_days: 14          # the lane's archive/<b> heads, deleted this many days on
        legacy_prefixes: [hb/]    # heads under these prefixes (default none) …
        legacy_days: 7            # … deleted once their tip is older than this
        per_tick: 50              # the most deletes one pass makes
      outcome_share_pct: 10       # a failing outcome class over this share of 24 h files a Bug
      outcome_min_sessions: 20    # no rate below this many ended sessions in the window
      repeat_failure_n: 2         # the same item, the same class, this many times → a Bug
      idle_wave_ticks: 6          # consecutive idle waves with New Tasks → a Bug
      heavy_share_pct: 50         # the share of labelled 7-day spend on the heavy model above
                                  # which the rollup files a Bug (F-0101 §2.7)
      budget:
        sessions: 3        # ended sessions an item may take before it stops (off: no limit)
        usd: 10            # dollars an item may take before it stops (off: no limit)
        run_minutes: 180   # one run's wall clock; over it the run is ended `run cap`
        run_turns: 600     # one run's assistant turns; over it the run is ended `run cap`
        run_turns_by_kind: # per-kind override of run_turns (F-0062 §2.5); a kind not named
          close: 60        # here keeps run_turns
      roles:
        asf-coder:         # a TABLE row's effort and permission_mode only — widening tools,
          effort: medium   # writes or connections is an operator action (asf/amendable.py:53),
                           # not a product setting (F-0062 §2.1)
      sequences:                              # `asf reserve <name>` (B-0151): one run of N's is
        migrations: db/migrations/NNNN_*.sql  # the zero-padded number field in the path
        bands: docs/decisions/NNNN-*.md       # a directory sequence too — one number per file,
                                               # never one file holding every band (unsupported)

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
    #: a ``lane: direct`` Feature's one branch — the whole Feature, code and tests, one PR
    'direct': 'cloud/direct-',
    'legacy': [],
}

#: Kinds a product's branches fall in whether or not its ``branch_prefixes`` names them — ones
#: added after products wrote their prefix maps (a product naming ``code:`` alone still has a
#: direct lane, under the default prefix).
RECOGNISED_KINDS = ('direct',)

#: The operator's two model labels. worker_pool.models maps them onto real model ids
#: (asf.workers.spawn.model_arg), so no vendor's model id is written down in this repo.
HEAVY = 'heavy'
LIGHT = 'light'

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
#: The share of the last 24 h's ended sessions (in percent) one failing outcome class may take
#: before the tick files a Bug for it (F-0103). Compared unrounded, strictly over.
DEFAULT_OUTCOME_SHARE_PCT = 10
#: The fewest ended sessions the 24 h window must hold before any outcome rate is read: a share
#: of three sessions is noise, not a rate (F-0103).
DEFAULT_OUTCOME_MIN_SESSIONS = 20
#: How many times the same item may end in the same failing class inside the window before the
#: tick files a Bug for the repeat (F-0103).
DEFAULT_REPEAT_FAILURE_N = 2
#: How many consecutive ticks may launch nothing while New Tasks exist before the tick files a
#: Bug for the stalled wave (F-0103).
DEFAULT_IDLE_WAVE_TICKS = 6
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

#: The most seconds one factory ``git push`` may take (:mod:`asf.gitpush`) before its process
#: group is killed: the ref is logged and left as it was, and the tick goes on. Spelt
#: ``git: {push_timeout_s: …}`` in the yaml.
DEFAULT_PUSH_TIMEOUT_S = 120

#: The window `asf status`'s Features row measures time-to-land over.
DEFAULT_LAND_WINDOW_DAYS = 7

#: The share of labelled 7-day session spend on HEAVY above which the rollup calls the mix a
#: defect (F-0101 §2.7).
DEFAULT_HEAVY_SHARE_PCT = 50

#: The keys of the yaml's ``harvest:`` block and the field each one is.
HARVEST_KEYS = {'gate': 'harvest_gate', 'branches_per_tick': 'branches_per_tick',
                'gate_timeout_s': 'gate_timeout_s'}
#: The keys of the yaml's ``git:`` block and the field each one is.
GIT_KEYS = {'push_timeout_s': 'push_timeout_s'}

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
#: ``queue`` is ``auto`` through the lane's own serialized merge queue (:mod:`asf.merge_queue`):
#: green PRs are batched onto the trunk tip, the batch sha is gated on the product's whole
#: required set, and the trunk is fast-forwarded to that exact sha — never a ``gh pr merge``.
MERGE_AUTO = 'auto'
MERGE_MANUAL = 'manual'
MERGE_QUEUE = 'queue'
MERGE_MODES = (MERGE_AUTO, MERGE_MANUAL, MERGE_QUEUE)
DEFAULT_MERGE = MERGE_MANUAL
#: ``conventions.merge_queue: {ref_prefix, batch_size, inflight, timeout_min}`` — the queue's
#: shape under ``merge: queue`` (:data:`asf.merge_queue.DEFAULTS`); ``ref_prefix`` is what the
#: product's CI triggers its full matrix on.

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

#: ``security: {paths, alerts, ports}`` — the three parts of the security pass
#: (:mod:`asf.security`): which of the product's own named classes a diff's files fall under,
#: how long the host's own secret- and dependency-scanning feeds may go unread, and the nightly
#: probe of every box's exposed ports. Unset, nothing is sensitive and no check violates.
#: ``paths`` is ``{class: [glob]}``, globs matched as ``customer_content``'s are; ``alerts`` is
#: ``{max_age_h}``; ``ports`` is ``{boxes, workflow, artifact, max_age_h, ports}`` — the boxes
#: beyond ``ci.pool``'s own, the workflow and artifact that carry the probe's result, how old
#: that result may be, and the ports checked (:data:`DEFAULT_PROBE_PORTS` unless named).
DEFAULT_ALERT_MAX_AGE_H = 24
DEFAULT_PROBE_MAX_AGE_H = 30
#: The common database and daemon ports the nightly probe checks unless a product names its own.
DEFAULT_PROBE_PORTS = (
    445,    # smb
    1433,
    1521,
    2049,   # nfs
    2375,
    2376,   # docker
    2379,   # etcd
    3306,
    3389,   # rdp
    4369,   # epmd
    5432,
    5672,
    5900,   # vnc
    5984,
    6379,
    6443,   # kube-apiserver
    7000,
    8086,
    9000,
    9042,
    9092,
    9200,
    9300,
    10250,  # kubelet
    11211,
    15672,
    27017,
    27018,
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

#: ``review: {skip_under_lines: …}``: a Task of a ``size: s`` Feature whose diff adds and removes
#: fewer lines than this lands on CI and the gate alone — no review session. 0 turns it off.
DEFAULT_REVIEW_SKIP_UNDER_LINES = 80

#: ``delivery``: the unit the factory builds and lands a Feature in. ``task`` (the default) is
#: one branch, one PR, one review per Task. ``feature`` makes the whole Feature one delivery
#: (:mod:`asf.record.slice`): its Tasks one branch, one PR, one CI run and one review — cut into
#: ordered slices only when the plan is larger than ``slice_max_tasks`` Tasks or its union
#: footprint larger than ``size.medium_max_files`` files. Any other word is a red doctor finding
#: and reads as the default.
DELIVERY_TASK = 'task'
DELIVERY_FEATURE = 'feature'
DELIVERY_UNITS = (DELIVERY_TASK, DELIVERY_FEATURE)
DEFAULT_DELIVERY = DELIVERY_TASK
#: ``slice_max_tasks``: the most Tasks one Feature delivery carries before the plan is cut into
#: slices along its ``after:`` boundaries (``delivery: feature`` only).
DEFAULT_SLICE_MAX_TASKS = 6

#: The keys of the yaml's ``lane:`` block and the field each one is.
LANE_KEYS = {'review': 'lane_review', 'stale_after': 'lane_stale_after'}

#: The conventions whose value is a map. A value of any other shape — a string the reader kept,
#: a scalar written by hand — is read as the default (:meth:`Conventions.map_of`) so no reader
#: raises on it (a ``models: light`` string once failed every launch for forty minutes), and it
#: fails loud: :meth:`Conventions.shape_findings` names it, and the doctor's ``conventions`` row
#: is red with the key and the line.
MAP_CONVENTIONS = ('models', 'branch_prefixes', 'harvest', 'git', 'branch_retention', 'commit',
                   'budget', 'merge_queue', 'roles', 'sequences')
#: ``commit.signoff_check``'s default: a PR check whose name contains it is the sign-off check.
DEFAULT_SIGNOFF_CHECK = 'DCO'
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
#: The README's own path, relative to the product's repo dir (`asf readme`, `asf/views/readme.py`).
DEFAULT_README = 'README.md'
#: The README's committed facts file, relative to the product's repo dir (`asf readme`).
DEFAULT_README_FACTS = 'docs/readme-numbers.json'
#: The "Upgrade" line of a version's release notes: ``{repo_slug}`` and ``{tag}`` substituted.
#: The yaml's ``release_install:`` overrides it; an empty value leaves the line out.
DEFAULT_RELEASE_INSTALL = 'pipx install --force "git+https://github.com/{repo_slug}.git@{tag}"'

#: The savings pass's window and thresholds (F-0100 §2.4). A product overrides any key;
#: a key it does not name keeps the default here.
DEFAULT_SAVINGS = {
    'window_days': 7, 'min_landings': 5, 'preamble_ratio': 2.0, 'gate_minutes_max': 20.0,
    'rounds_per_landing': 1.3, 'step_duration_ratio': 1.5, 'spend_ratio': 1.5,
    'failure_class_count': 3,
}

#: An item's budget and one run's caps (F-0092). A product overrides any key; a key it does not
#: name keeps the default here. ``off`` on any key is no limit for that measure.
#: ``run_turns_by_kind`` (F-0062 §2.5) overrides ``run_turns`` per brief kind, read through
#: :func:`asf.budget.run_caps` — a kind it does not name keeps ``run_turns``.
DEFAULT_BUDGET = {'sessions': 3, 'usd': 10, 'run_minutes': 180, 'run_turns': 600,
                  'run_turns_by_kind': {}}

#: ``conventions.roles``: a product's per-role override of a launch table row's ``effort`` and
#: ``permission_mode`` only (F-0062 §2.1, :func:`asf.roles.launch.launch_for`). Empty by default
#: — a role a product does not name launches exactly as :data:`asf.roles.launch.TABLE` has it.
DEFAULT_ROLES = {}

#: ``conventions.sequences``: a product's numbered file sequences (a migration, a decision band)
#: whose next number `asf reserve <name>` claims (B-0151, :mod:`asf.reserve`) — a directory path
#: with one run of ``N``s standing in for the zero-padded number each file in it carries, e.g.
#: ``{migrations: 'db/migrations/NNNN_*.sql', bands: 'docs/decisions/NNNN-*.md'}``. Only that one
#: shape (one file, one number) is a sequence; numbers recorded as rows inside a single shared
#: file are not (:func:`asf.reserve.pattern_regex` raises `ValueError` on a pattern with no
#: ``N`` run — the same error a plain, unnumbered filename like ``bands.md`` would raise). Empty
#: by default — a product names none, `asf reserve` refuses any name.
DEFAULT_SEQUENCES = {}


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


def _positive_number_problem(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return f'must be a positive number, not {value!r}'
    return None


#: A bare variable name (``auth_env``, ``worker_pool.accounts[].auth_env`` in ``asf.env``): a
#: leading letter/underscore, then letters, digits or underscores — what a shell accepts on the
#: left of ``export``.
_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def validate_mapping(data):
    """The shaped keys of a ``conventions:`` mapping checked: ``[(dotted key, problem)]``, empty
    when they are well-formed. Only ``doc_paths``, ``shared_paths``, ``lane``, ``worktree_setup``,
    ``auth_env``, ``full_suite_commands``, ``customer_content``, ``security`` and ``feeder`` are
    checked — every other key is kept verbatim (see the module doc), so a product file written
    for a newer ``asf`` still loads."""
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
    sec = data.get('security')
    if sec is not None:
        if not isinstance(sec, dict):
            problems.append(('security', f'must be a map (paths, alerts, ports), not {sec!r}'))
        else:
            paths = sec.get('paths')
            if paths is not None:
                if not isinstance(paths, dict):
                    problems.append(('security.paths',
                                     f'must be a map of class name to path list, not {paths!r}'))
                else:
                    for name, globs in paths.items():
                        if not isinstance(name, str) or not name.strip():
                            problems.append(('security.paths', f'{name!r} is not a class name'))
                            continue
                        why = _path_list_problem(globs)
                        if why:
                            problems.append((f'security.paths.{name}', why))
            alerts = sec.get('alerts')
            if alerts is not None:
                if not isinstance(alerts, dict):
                    problems.append(('security.alerts', f'must be a map (max_age_h), not {alerts!r}'))
                elif alerts.get('max_age_h') is not None:
                    why = _positive_number_problem(alerts['max_age_h'])
                    if why:
                        problems.append(('security.alerts.max_age_h', why))
            ports = sec.get('ports')
            if ports is not None:
                if not isinstance(ports, dict):
                    problems.append(('security.ports', f'must be a map (boxes, workflow, artifact,'
                                                        f' max_age_h, ports), not {ports!r}'))
                else:
                    if ports.get('max_age_h') is not None:
                        why = _positive_number_problem(ports['max_age_h'])
                        if why:
                            problems.append(('security.ports.max_age_h', why))
                    port_list = ports.get('ports')
                    if port_list is not None:
                        bad = (not isinstance(port_list, list)
                               or not all(isinstance(p, int) and not isinstance(p, bool)
                                          and 1 <= p <= 65535 for p in port_list))
                        if bad:
                            problems.append(('security.ports.ports',
                                             f'must be a list of ports 1-65535, not {port_list!r}'))
                    boxes = ports.get('boxes')
                    if boxes is not None:
                        bad = (not isinstance(boxes, list)
                               or not all(isinstance(b, str) and b.strip() for b in boxes))
                        if bad:
                            problems.append(('security.ports.boxes',
                                             f'must be a list of labels, not {boxes!r}'))
                    for key in ('workflow', 'artifact'):
                        value = ports.get(key)
                        if value is not None and (not isinstance(value, str) or not value.strip()):
                            problems.append((f'security.ports.{key}',
                                             f'must be a non-empty string, not {value!r}'))
    feeder = data.get('feeder')
    if feeder is not None:
        cap = feeder.get('max_specs_in_flight') if isinstance(feeder, dict) else None
        if not isinstance(feeder, dict):
            problems.append(('feeder', f'must be a map (max_specs_in_flight), not {feeder!r}'))
        elif cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
            problems.append(('feeder.max_specs_in_flight',
                             f'must be a whole number >= 0, not {cap!r}'))
        if isinstance(feeder, dict):
            wip = feeder.get('max_features_in_build')
            if wip is not None and wip != 'auto' and (
                    isinstance(wip, bool) or not isinstance(wip, int) or wip < 1):
                problems.append(('feeder.max_features_in_build',
                                 f'must be auto or a whole number >= 1, not {wip!r}'))
            per = feeder.get('features_per_session')
            if per is not None and (isinstance(per, bool) or not isinstance(per, (int, float))
                                    or per <= 0):
                problems.append(('feeder.features_per_session',
                                 f'must be a number > 0, not {per!r}'))
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
    outcome_share_pct: int = DEFAULT_OUTCOME_SHARE_PCT
    outcome_min_sessions: int = DEFAULT_OUTCOME_MIN_SESSIONS
    repeat_failure_n: int = DEFAULT_REPEAT_FAILURE_N
    idle_wave_ticks: int = DEFAULT_IDLE_WAVE_TICKS
    harvest_gate: str = DEFAULT_HARVEST_GATE
    branches_per_tick: int = DEFAULT_BRANCHES_PER_TICK
    gate_timeout_s: int = DEFAULT_GATE_TIMEOUT_S
    #: ``git.push_timeout_s`` (:data:`DEFAULT_PUSH_TIMEOUT_S`).
    push_timeout_s: int = DEFAULT_PUSH_TIMEOUT_S
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
    #: ``readme`` / ``readme_facts``: the README's own path and its committed facts file, relative
    #: to the product's repo dir (:data:`DEFAULT_README`, :data:`DEFAULT_README_FACTS`).
    readme: str = DEFAULT_README
    readme_facts: str = DEFAULT_README_FACTS
    stage_limits: dict = field(default_factory=dict)
    #: ``heavy_share_pct``: the share (of labelled 7-day spend) on HEAVY above which the rollup
    #: files a Bug (:data:`DEFAULT_HEAVY_SHARE_PCT`).
    heavy_share_pct: int = DEFAULT_HEAVY_SHARE_PCT
    #: ``savings``: the savings pass's window and six thresholds (F-0100 §2.9), merged per key
    #: through :func:`savings_for` — a product overriding one threshold keeps the other seven.
    savings: dict = field(default_factory=lambda: dict(DEFAULT_SAVINGS))
    #: ``budget``: an item's budget and one run's caps (F-0092 §2.1), merged per key through
    #: :func:`budget_for` — a product overriding one key keeps the other three. ``'budget'`` is
    #: in :data:`MAP_CONVENTIONS`, so a misshapen block is a red doctor row, never a silent
    #: default.
    budget: dict = field(default_factory=lambda: dict(DEFAULT_BUDGET))
    #: ``roles``: a product's per-role override of a launch table row's ``effort`` and
    #: ``permission_mode`` only (F-0062 §2.1), read through
    #: :func:`asf.roles.launch.launch_for`. ``'roles'`` is in :data:`MAP_CONVENTIONS`, so a
    #: misshapen block is a red doctor row, never a silent default.
    roles: dict = field(default_factory=lambda: dict(DEFAULT_ROLES))
    #: ``sequences``: a product's numbered file sequences, name -> ``NNNN``-shaped path
    #: (:data:`DEFAULT_SEQUENCES`, :mod:`asf.reserve`). ``'sequences'`` is in
    #: :data:`MAP_CONVENTIONS`, so a misshapen block is a red doctor row, never a silent default.
    sequences: dict = field(default_factory=lambda: dict(DEFAULT_SEQUENCES))
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
    #: ``delivery``: ``task`` | ``feature`` (:data:`DEFAULT_DELIVERY`) — the unit a Feature is
    #: built and landed in; any other value is a red doctor finding and reads as the default.
    delivery: str = DEFAULT_DELIVERY
    #: ``slice_max_tasks`` (:data:`DEFAULT_SLICE_MAX_TASKS`): the most Tasks one Feature
    #: delivery carries whole.
    slice_max_tasks: int = DEFAULT_SLICE_MAX_TASKS
    #: ``branch_retention``: merged over :data:`DEFAULT_BRANCH_RETENTION` (see there).
    branch_retention: dict = field(default_factory=lambda: dict(DEFAULT_BRANCH_RETENTION))
    #: ``protected_refs``: branch names or globs no factory write may push, force or delete
    #: (:mod:`asf.refguard`); unset is :data:`asf.refguard.DEFAULT_PROTECTED_REFS`. The trunk
    #: is always protected.
    protected_refs: list = None
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
        delivery = data.get('delivery')
        if delivery is not None and str(delivery).strip().lower() not in DELIVERY_UNITS:
            misshapen['delivery'] = data.pop('delivery')
        elif delivery is not None:
            data['delivery'] = str(delivery).strip().lower()
        models = data.get('models')
        for kind, value in (models.items() if isinstance(models, dict) else ()):
            # ``models.<kind>``: a label, or a map of labels by class (asf.briefs.build)
            if not model_value_ok(value):
                misshapen[f'models.{kind}'] = value
        for block, block_keys in (('harvest', HARVEST_KEYS), ('git', GIT_KEYS)):
            value_ = data.pop(block, None)
            if isinstance(value_, dict):  # ``harvest: {gate, …}`` / ``git: {…}`` → the fields
                rest = {}
                for key, value in value_.items():
                    name = block_keys.get(key)
                    if name and value is not None:
                        kwargs[name] = value
                    elif not name:
                        rest[key] = value
                if rest:
                    data[block] = rest
            elif value_ is not None:
                data[block] = value_
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
                    else f"one of {', '.join(DELIVERY_UNITS)}" if key == 'delivery'
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

    def review_skip_under_lines(self):
        """``review.skip_under_lines`` (:data:`DEFAULT_REVIEW_SKIP_UNDER_LINES`): below this many
        changed lines a small Feature's Task needs no review. A malformed value is the default."""
        value = self.map_of('review').get('skip_under_lines')
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return DEFAULT_REVIEW_SKIP_UNDER_LINES
        return value

    def lane_stale_after_s(self):
        """``lane.stale_after`` in seconds."""
        return duration_seconds(self.lane_stale_after)

    def merge_auto(self):
        """True under ``merge: auto`` or ``queue`` — the lane merges a green, reviewed PR
        itself (directly, or through its own merge queue)."""
        return str(self.merge or '').strip().lower() in (MERGE_AUTO, MERGE_QUEUE)

    def merge_queue(self):
        """True under ``merge: queue`` — the lane lands green PRs as gated batches
        (:mod:`asf.merge_queue`), never by a direct host merge."""
        return str(self.merge or '').strip().lower() == MERGE_QUEUE

    # ---- the delivery unit ---------------------------------------------------

    def delivery_feature(self):
        """True under ``delivery: feature`` — a Feature's Tasks are built and landed as one
        delivery (:mod:`asf.record.slice`); the default ``task`` leaves every lane as it is."""
        return str(self.delivery or '').strip().lower() == DELIVERY_FEATURE

    def slice_max_tasks_n(self):
        """``slice_max_tasks`` (:data:`DEFAULT_SLICE_MAX_TASKS`); a malformed value is the default."""
        v = self.slice_max_tasks
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 \
            else DEFAULT_SLICE_MAX_TASKS

    def slice_max_files(self):
        """``size.medium_max_files`` — the file count above which a Feature delivery is cut
        (the same threshold :mod:`asf.size` classes a footprint ``large`` by); the size module's
        own default when the product's ``size:`` block does not name it."""
        from asf.size import SizeConfig  # local: size imports the feeder's footprint rule
        size = self.get('size')
        v = size.get('medium_max_files') if isinstance(size, dict) else None
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 \
            else SizeConfig().medium_max_files

    # ---- commits -------------------------------------------------------------

    def signoff(self):
        """``commit.signoff: true`` — every commit a worker makes carries a ``Signed-off-by:``
        trailer (a DCO check the product requires): the ``commit-msg`` hook adds it
        (:mod:`asf.workers.githooks`) and the lane re-signs a factory branch whose sign-off check
        fails (:meth:`asf.harvest.lane.Lane.repair_signoff`). False unless set."""
        value = self.map_of('commit').get('signoff')
        return value is True or str(value).strip().lower() in ('true', 'yes', 'on', '1')

    def signoff_check(self):
        """``commit.signoff_check``: the text (case-insensitive) a PR check's name contains when
        it is the sign-off check — :data:`DEFAULT_SIGNOFF_CHECK` unless set."""
        value = self.map_of('commit').get('signoff_check')
        return str(value).strip() if isinstance(value, str) and value.strip() \
            else DEFAULT_SIGNOFF_CHECK

    def is_signoff_check(self, name):
        """True when a PR check called ``name`` is the product's sign-off check."""
        return self.signoff_check().lower() in str(name or '').lower()

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

    def kinds(self):
        """The branch kinds this product's branches fall in: every kind its ``branch_prefixes``
        names, plus :data:`RECOGNISED_KINDS` whether it names them or not, sorted."""
        named = set(self.branch_prefixes) if isinstance(self.branch_prefixes, dict) else set()
        return tuple(sorted((named | set(RECOGNISED_KINDS)) - {'legacy'}))

    def all_prefixes(self):
        """Every prefix this product's branches can carry, longest first (so ``fix/`` wins over
        a hypothetical ``f/``). Deduplicated, order otherwise by kind name."""
        out = []
        for kind in self.kinds():
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
        for kind in self.kinds():
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

    # ---- security --------------------------------------------------------------

    def security_paths(self):
        """``security.paths``: ``{class: [glob]}`` — a value that is not a map reads as ``{}``
        (nothing is sensitive); ``validate_mapping`` is where a malformed one is a loud problem,
        not a silent default."""
        value = self.map_of('security').get('paths')
        return value if isinstance(value, dict) else {}

    def security_alerts(self):
        """``security.alerts``: ``{max_age_h}`` — :data:`DEFAULT_ALERT_MAX_AGE_H` when unset or
        unusable."""
        value = self.map_of('security').get('alerts')
        value = value if isinstance(value, dict) else {}
        max_age_h = value.get('max_age_h')
        if isinstance(max_age_h, bool) or not isinstance(max_age_h, (int, float)) or max_age_h <= 0:
            max_age_h = DEFAULT_ALERT_MAX_AGE_H
        return {'max_age_h': max_age_h}

    def security_ports(self):
        """``security.ports``: ``{boxes, workflow, artifact, max_age_h, ports}`` — ``max_age_h``
        and ``ports`` defaulted (:data:`DEFAULT_PROBE_MAX_AGE_H`, :data:`DEFAULT_PROBE_PORTS`)
        when unset or unusable, ``boxes`` a list, ``workflow`` and ``artifact`` as configured."""
        value = self.map_of('security').get('ports')
        value = value if isinstance(value, dict) else {}
        max_age_h = value.get('max_age_h')
        if isinstance(max_age_h, bool) or not isinstance(max_age_h, (int, float)) or max_age_h <= 0:
            max_age_h = DEFAULT_PROBE_MAX_AGE_H
        ports = value.get('ports')
        if (not isinstance(ports, list)
                or not all(isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535
                          for p in ports)):
            ports = list(DEFAULT_PROBE_PORTS)
        boxes = value.get('boxes')
        boxes = boxes if isinstance(boxes, list) else []
        return {'boxes': boxes, 'workflow': value.get('workflow'), 'artifact': value.get('artifact'),
                'max_age_h': max_age_h, 'ports': ports}

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


def savings_for(conv, key):
    """The savings pass's value for ``key`` (F-0100 §2.9): the product's own
    ``conventions.savings`` entry when it named one, else :data:`DEFAULT_SAVINGS`'s — a product
    overriding one threshold keeps the other seven."""
    savings = getattr(conv, 'savings', None) or {}
    return savings[key] if key in savings else DEFAULT_SAVINGS[key]


def budget_for(conv, key):
    """An item's budget or a run's cap for ``key`` (F-0092 §2.1): the product's own
    ``conventions.budget`` entry when it named one, else :data:`DEFAULT_BUDGET`'s."""
    budget = getattr(conv, 'budget', None) or {}
    return budget[key] if key in budget else DEFAULT_BUDGET[key]


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
