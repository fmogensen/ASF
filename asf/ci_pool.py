"""asf.ci_pool — the declared CI runner pool: inventory, drift, a safe reconcile, and trials.

A product declares the runners its CI jobs run on in ``ci.pool:`` of its product file::

    ci:
      pool:
        - {runner: ci-1,  box: box-1, provider: acme, size: 8c-16g, role: heavy, slots: 1}
        - {runner: ci-1b, box: box-1, provider: acme, size: 8c-16g, role: light, class: light-fast}

``runner`` is the CI host's runner name; ``box`` the machine it runs on; ``provider`` and
``size`` are inventory; ``role`` is the one routing label the runner answers to; ``slots`` (default
1) is how many jobs it takes at once. That list is the single source of truth: labels on the CI
host are derived from it, never edited by hand.

**Routing labels describe capability only** (``heavy``, ``light``, …). Provider and size are
inventory and never appear in a job's ``runs-on``: a job that names a provider strands every
box of another provider that could run it. A runner may carry one informational label,
``provider-<name>``, which no ``runs-on`` may ask for.

**Runner class.** A runner may declare ``class: <name>`` (``heavy-fast``, ``heavy-slow``, …): a
finer grain than the role, for a product's own scripts (a test split, a timeout) — never for
routing. The CI host's API sets labels, not a runner's environment, so the class travels as one
more derived label, ``class:<name>``. A product script reads ``$RUNNER_CLASS`` when the box's
runner ``.env`` sets it, else the ``class:`` label of ``$RUNNER_NAME`` from the runners API.
Reconcile keeps exactly the declared ``class:`` label on each runner (dry-run by default, like
every label); the doctor counts runners per class and warns on a runner with no class while
others have one; the capacity view groups the pool by class.

Three readers:

- :func:`drift` — the ``asf doctor`` rows, read-only: a runner online that no job can reach, a
  ``runs-on`` no runner satisfies, a runner whose labels differ from its declared role, a
  ``runs-on`` naming a provider-like label, a declared runner missing or offline.
- :func:`plan` / :func:`apply` — ``asf ci reconcile``: per runner, the labels it carries, the
  labels it should, and the action. Adds happen before removes, across the whole pool, and a label
  a current ``runs-on`` still needs is never removed ("blocked until workflows migrate").
- :func:`check_trials` — a runner newly enabled for a role is on trial for one job: the next
  reconcile or tick reads that job's result, keeps the runner, or rolls its labels back and leaves
  a Bug for the tick to file (:func:`tick`).

The CI host is behind :class:`Backend`; :class:`GitHubBackend` is the first one (runners and
workflow files through ``gh api``). A test passes a fake.
"""
import dataclasses
import datetime
import json
import os
import re
import subprocess

from asf import env

PROVIDER_PREFIX = 'provider-'
CLASS_PREFIX = 'class:'
#: the variable a product script reads the class from (else the ``class:`` label)
CLASS_ENV = 'RUNNER_CLASS'
POOL_FIELDS = ('runner', 'box', 'provider', 'size', 'role', 'slots', 'class')
POOL_REQUIRED = ('runner', 'provider', 'role')
#: labels a CI host puts on every self-hosted runner itself: never declared, never removed.
DEFAULT_LABELS = frozenset({'self-hosted', 'linux', 'windows', 'macos',
                            'x64', 'x86', 'arm', 'arm64'})
LABEL_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
TRIALS_FILE = 'ci-trials.json'
TRIAL_HISTORY = 'ci-trials.jsonl'
#: conclusions a trial is judged on; anything else (cancelled, skipped) waits for the next job.
PASS = frozenset({'success'})
FAIL = frozenset({'failure', 'timed_out', 'startup_failure'})
GH_TIMEOUT_S = 30


# ---- the declared pool ------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class PoolEntry:
    runner: str
    provider: str
    role: str
    box: str = ''
    size: str = ''
    slots: int = 1
    #: ``class:`` in the product file; '' when the runner declares none
    cls: str = ''

    @property
    def provider_label(self):
        return PROVIDER_PREFIX + _norm(self.provider)

    @property
    def class_label(self):
        """``class:<name>``, or '' when the runner declares no class."""
        return CLASS_PREFIX + self.cls if self.cls else ''

    def labels(self):
        """The labels ci.pool derives for this runner: role, provider, and class if any."""
        return {l for l in (self.role, self.provider_label, self.class_label) if l}


def _norm(label):
    return str(label).strip().lower()


def pool_problems(ci):
    """``[(dotted key, problem)]`` for a ``ci.pool`` the readers cannot use — checked on every
    product load (:func:`asf.env.validate_product_text`), like ``deploy_sha``'s modes."""
    if not isinstance(ci, dict) or ci.get('pool') is None:
        return []
    pool = ci['pool']
    if not isinstance(pool, list):
        return [('ci.pool', f'must be a list of runners, not {pool!r}')]
    out, seen = [], set()
    for i, entry in enumerate(pool):
        key = f'ci.pool[{i}]'
        if not isinstance(entry, dict):
            out.append((key, f'must be a map {{runner, box, provider, size, role, slots}}, not {entry!r}'))
            continue
        for k in entry:
            if k not in POOL_FIELDS:
                out.append((f'{key}.{k}', f"is not a field of a pool runner ({', '.join(POOL_FIELDS)})"))
        for k in POOL_REQUIRED:
            if entry.get(k) in (None, ''):
                out.append((f'{key}.{k}', 'is required'))
        for k in ('runner', 'box', 'provider', 'size', 'role', 'class'):
            if isinstance(entry.get(k), (dict, list)):
                out.append((f'{key}.{k}', f'must be a scalar, not {entry[k]!r}'))
        role = entry.get('role')
        if isinstance(role, str) and role:
            r = _norm(role)
            if not LABEL_RE.match(r):
                out.append((f'{key}.role', f'must be one label (letters, digits, . _ -), not {role!r}'))
            elif r.startswith(PROVIDER_PREFIX) or r in DEFAULT_LABELS:
                out.append((f'{key}.role', f'{role!r} is not a capability: a role names what a '
                                           'runner can do (heavy, light), never its provider or OS'))
            elif isinstance(entry.get('provider'), str) and r == _norm(entry['provider']):
                out.append((f'{key}.role', f'{role!r} is the provider — a role names a capability'))
        cls = entry.get('class')
        if isinstance(cls, str) and cls and not LABEL_RE.match(_norm(cls)):
            out.append((f'{key}.class', f'must be one label (letters, digits, . _ -), not {cls!r}'))
        slots = entry.get('slots')
        if slots is not None and (isinstance(slots, bool) or not isinstance(slots, int) or slots < 1):
            out.append((f'{key}.slots', f'must be a whole number >= 1, not {slots!r}'))
        name = entry.get('runner')
        if isinstance(name, str) and name:
            if name in seen:
                out.append((f'{key}.runner', f'{name!r} is declared twice'))
            seen.add(name)
    return out


def load_pool(product):
    """The product's ``ci.pool`` as :class:`PoolEntry` rows, ``[]`` when none is declared."""
    ci = product.ci if isinstance(getattr(product, 'ci', None), dict) else {}
    out = []
    for entry in ci.get('pool') or ():
        if not isinstance(entry, dict) or not entry.get('runner'):
            continue
        slots = entry.get('slots')
        out.append(PoolEntry(runner=str(entry['runner']), provider=str(entry.get('provider') or ''),
                             role=_norm(entry.get('role') or ''), box=str(entry.get('box') or ''),
                             size=str(entry.get('size') or ''),
                             slots=slots if isinstance(slots, int) and slots >= 1 else 1,
                             cls=_norm(entry.get('class') or '')))
    return out


def by_class(pool):
    """``{class: [PoolEntry]}`` in name order, the unclassed under ``''`` last; ``{}`` when no
    runner declares a class."""
    if not any(e.cls for e in pool):
        return {}
    out = {}
    for e in pool:
        out.setdefault(e.cls, []).append(e)
    return {k: out[k] for k in sorted(out, key=lambda k: (k == '', k))}


def class_rows(pool):
    """``[(ok, detail)]`` from the declared pool alone: each class's runner count, and one
    warning per runner with no class while others have one. ``[]`` when no class is declared."""
    groups = by_class(pool)
    if not groups:
        return []
    counts = ', '.join(f"{k} {len(v)} runner{'s' if len(v) != 1 else ''}"
                       for k, v in groups.items() if k)
    out = [(True, f'classes: {counts}')]
    for e in groups.get('', []):
        out.append((False, f"class: {e.runner} declares no class while others do — "
                           f"{CLASS_ENV} is empty on it"))
    return out


def roles(pool):
    return sorted({e.role for e in pool if e.role})


def slots_by_role(pool):
    out = {}
    for e in pool:
        out[e.role] = out.get(e.role, 0) + e.slots
    return dict(sorted(out.items()))


def pool_ci(product):
    """``(slots, detail)`` — the CI ceiling the declared pool implies, the sum of every role's
    slots, e.g. ``(19, 'ci.pool: heavy 12, light 7')``; ``(None, None)`` with no pool."""
    pool = load_pool(product)
    if not pool:
        return None, None
    by_role = slots_by_role(pool)
    return sum(by_role.values()), 'ci.pool: ' + ', '.join(f'{r} {n}' for r, n in by_role.items())


# ---- what the CI host says --------------------------------------------------------------------

@dataclasses.dataclass
class Runner:
    name: str
    online: bool
    labels: list
    id: object = None
    busy: bool = False
    #: the labels the host itself set (read-only on the host), lower-case
    fixed: frozenset = frozenset()

    def norm_labels(self):
        return {_norm(l) for l in self.labels}


@dataclasses.dataclass(frozen=True)
class RunsOn:
    """One job's ``runs-on``: ``labels`` lower-case, or None when it is an expression this
    reader cannot resolve (``${{ matrix.os }}``) — such a job is never judged."""
    workflow: str
    job: str
    labels: object

    @property
    def self_hosted(self):
        return self.labels is not None and 'self-hosted' in self.labels

    def text(self):
        return f"{self.workflow}:{self.job} [{', '.join(sorted(self.labels or ()))}]"


def is_default(label, runner=None):
    n = _norm(label)
    return n in DEFAULT_LABELS or (runner is not None and n in runner.fixed)


class Backend:
    """The CI host behind the pool. ``runners`` and ``runs_on`` are read-only; the two label
    writes are only ever called by :func:`apply` and the trial rollback."""

    def runners(self):
        raise NotImplementedError

    def runs_on(self):
        raise NotImplementedError

    def add_labels(self, runner, labels):
        raise NotImplementedError

    def remove_label(self, runner, label):
        raise NotImplementedError

    def jobs_on(self, runner_name, since):
        """The jobs ``runner_name`` started at or after ``since`` (ISO ``Z``), oldest first:
        ``{'name', 'status', 'conclusion', 'url', 'started_at'}``."""
        raise NotImplementedError


class BackendError(Exception):
    pass


# -- workflow files: the runs-on of every job ---------------------------------------------------

_EXPR_DEFAULT = re.compile(r"^\$\{\{.*\|\|\s*['\"]([^'\"]+)['\"]\s*\}\}$")


def _label_token(tok):
    """A ``runs-on`` token's label: quotes stripped; ``${{ … || 'x' }}`` is its default ``x``;
    any other expression is None (unresolvable)."""
    t = tok.strip().strip('"').strip("'").strip()
    if '${{' in t:
        m = _EXPR_DEFAULT.match(t)
        return _norm(m.group(1)) if m else None
    return _norm(t) if t else None


def _split_flow(text):
    """``a, "b", ${{ x || 'y' }}`` → tokens, commas inside ``${{ }}`` kept together."""
    out, depth, cur = [], 0, ''
    i = 0
    while i < len(text):
        if text.startswith('${{', i):
            depth += 1
            cur += '${{'
            i += 3
            continue
        if text.startswith('}}', i) and depth:
            depth -= 1
            cur += '}}'
            i += 2
            continue
        c = text[i]
        if c == ',' and not depth:
            out.append(cur)
            cur = ''
        else:
            cur += c
        i += 1
    if cur.strip():
        out.append(cur)
    return out


def _strip_comment(line):
    quote = None
    for i, c in enumerate(line):
        if c in '"\'' and quote in (None, c):
            quote = None if quote else c
        elif c == '#' and quote is None and (i == 0 or line[i - 1] in ' \t'):
            return line[:i]
    return line


def parse_runs_on(workflow, text):
    """Every job's :class:`RunsOn` in one workflow file, in file order. A line reader, not a YAML
    parser: ``runs-on:`` as a scalar, a flow list, or a block list under it; the job is the
    nearest key one level under ``jobs:``."""
    lines = text.splitlines()
    out = []
    in_jobs, jobs_indent, job_indent, job = False, 0, None, '?'
    i = 0
    while i < len(lines):
        raw = _strip_comment(lines[i]).rstrip()
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(' '))
        i += 1
        if not stripped:
            continue
        if indent == 0:
            in_jobs = stripped.startswith('jobs:')
            jobs_indent, job_indent = 0, None
            continue
        if not in_jobs:
            continue
        m = re.match(r'^([A-Za-z0-9_.-]+|"[^"]+"|\'[^\']+\'):(\s|$)', stripped)
        if m and (job_indent is None or indent == job_indent) and indent > jobs_indent:
            job_indent = indent
            job = m.group(1).strip('"\'')
            continue
        if not stripped.startswith('runs-on:'):
            continue
        value = stripped[len('runs-on:'):].strip()
        if value.startswith('['):
            body = value[1:value.rfind(']')] if ']' in value else value[1:]
            toks = [_label_token(t) for t in _split_flow(body)]
        elif value.startswith('{'):
            toks = [None]                       # group/labels map: not judged
        elif value:
            toks = [_label_token(value)]
        else:
            toks = []
            while i < len(lines):
                nxt = _strip_comment(lines[i]).rstrip()
                if not nxt.strip():
                    i += 1
                    continue
                if not nxt.strip().startswith('- ') or len(nxt) - len(nxt.lstrip(' ')) < indent:
                    break
                toks.append(_label_token(nxt.strip()[2:]))
                i += 1
            if not toks:
                toks = [None]
        labels = None if any(t is None for t in toks) else frozenset(toks)
        out.append(RunsOn(workflow, job, labels))
    return out


# -- GitHub Actions -----------------------------------------------------------------------------

def _gh_env(product):
    """The environment ``gh`` runs with: this process's, with the product's ``auth_env`` files
    (``GH_TOKEN``) read over it; an unreadable file leaves the ambient login in place."""
    out = dict(os.environ)
    for var, path in env.product_auth_env(product).items():
        try:
            with open(path, encoding='utf-8') as f:
                value = f.read().strip()
        except OSError:
            continue
        if value:
            out[var] = value
    return out


class GitHubBackend(Backend):
    """Runners of the product's ``ci.runner_org`` (an organisation's runners), else of its repo;
    workflow files from the repo's trunk. Every call is ``gh api``."""

    WORKFLOW_DIR = '.github/workflows'

    def __init__(self, product, run=None):
        self.product = product
        ci = product.ci if isinstance(product.ci, dict) else {}
        org = ci.get('runner_org')
        self.slug = product.repo_slug
        self.base = f'orgs/{org}' if org else f'repos/{self.slug}'
        self._run = run or subprocess.run
        self._env = None

    def _api(self, args, stdin=None):
        if self._env is None:
            self._env = _gh_env(self.product)
        try:
            p = self._run(['gh', 'api', *args], input=stdin, capture_output=True, text=True,
                          timeout=GH_TIMEOUT_S, env=self._env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackendError(f'gh api {args[0]}: {e}') from e
        if p.returncode != 0:
            lines = (p.stderr or p.stdout or '').strip().splitlines()
            raise BackendError(f"gh api {args[0]}: {lines[-1] if lines else 'failed'}")
        return p.stdout

    def _lines(self, args):
        return [json.loads(l) for l in self._api(args).splitlines() if l.strip()]

    def runners(self):
        rows = self._lines(['--paginate', f'{self.base}/actions/runners?per_page=100',
                            '--jq', '.runners[]'])
        return [Runner(name=r['name'], online=r.get('status') == 'online', id=r.get('id'),
                       busy=bool(r.get('busy')), labels=[l['name'] for l in r.get('labels', [])],
                       fixed=frozenset(_norm(l['name']) for l in r.get('labels', [])
                                       if l.get('type') == 'read-only'))
                for r in rows]

    def runs_on(self):
        if not self.slug:
            raise BackendError('the product has no repo_slug')
        main = getattr(self.product, 'main', 'main')
        files = self._lines([f'repos/{self.slug}/contents/{self.WORKFLOW_DIR}?ref={main}',
                             '--jq', '.[] | select(.type == "file") | {path}'])
        out = []
        for path in (f.get('path', '') for f in files):
            if not str(path).endswith(('.yml', '.yaml')):
                continue
            text = self._api([f'repos/{self.slug}/contents/{path}?ref={main}',
                              '-H', 'Accept: application/vnd.github.raw'])
            out += parse_runs_on(os.path.basename(path), text)
        return out

    def add_labels(self, runner, labels):
        self._api(['-X', 'POST', f'{self.base}/actions/runners/{runner.id}/labels', '--input', '-'],
                  stdin=json.dumps({'labels': sorted(labels)}))

    def remove_label(self, runner, label):
        self._api(['-X', 'DELETE', f'{self.base}/actions/runners/{runner.id}/labels/{label}'])

    def jobs_on(self, runner_name, since):
        if not self.slug:
            return []
        runs = self._lines([f'repos/{self.slug}/actions/runs?created=%3E%3D{since}&per_page=100',
                            '--jq', '.workflow_runs[].id'])
        jobs = []
        for run_id in runs:
            jobs += self._lines([f'repos/{self.slug}/actions/runs/{run_id}/jobs?per_page=100',
                                 '--jq', f'.jobs[] | select(.runner_name == {json.dumps(runner_name)})'
                                         ' | {name, status, conclusion, url: .html_url, started_at}'])
        return sorted((j for j in jobs if (j.get('started_at') or '') >= since),
                      key=lambda j: j.get('started_at') or '')


def backend_for(product):
    """The product's CI host backend, or None when this release has none for its provider."""
    ci = product.ci if isinstance(product.ci, dict) else {}
    provider = str(ci.get('provider') or 'github-actions').lower()
    if provider in ('github-actions', 'github'):
        return GitHubBackend(product)
    return None


# ---- drift: the doctor rows -------------------------------------------------------------------

def _satisfies(runner, ro):
    return ro.labels is not None and ro.labels <= runner.norm_labels()


def drift(pool, runners, runs_on):
    """``[(ok, detail)]`` — one finding per drift, ``[(True, summary)]`` when there is none."""
    declared = {e.runner: e for e in pool}
    role_set = set(roles(pool))
    by_name = {r.name: r for r in runners}
    jobs = [ro for ro in runs_on if ro.self_hosted]
    online = [r for r in runners if r.online]
    out = []

    for r in online:
        if not any(_satisfies(r, ro) for ro in jobs):
            want = f" (its role {declared[r.name].role})" if r.name in declared else ''
            out.append((False, f"stranded: {r.name} is online but no job's runs-on matches its "
                               f"labels [{', '.join(sorted(r.norm_labels()))}]{want}"))

    seen = set()
    for ro in jobs:
        if ro.labels in seen:
            continue
        if not any(_satisfies(r, ro) for r in online):
            seen.add(ro.labels)
            out.append((False, f"unsatisfiable: {ro.text()} — no online runner carries all of it"))

    flagged = set()
    for ro in jobs:
        for label in sorted(ro.labels):
            if is_default(label) or label in role_set or (ro.workflow, label) in flagged:
                continue
            flagged.add((ro.workflow, label))
            why = ('a provider label' if label.startswith(PROVIDER_PREFIX)
                   else 'not a declared role (' + ', '.join(sorted(role_set)) + ')')
            out.append((False, f"provider-like label in runs-on: {ro.workflow} asks for "
                               f"'{label}', {why} — ask for a role"))

    for e in pool:
        r = by_name.get(e.runner)
        if r is None:
            out.append((False, f"missing: {e.runner} ({e.box or '?'}, {e.provider}) is declared "
                               f"but not registered on the CI host"))
            continue
        if not r.online:
            out.append((False, f"offline: {e.runner} ({e.box or '?'}, {e.provider}) — "
                               f"{e.slots} {e.role} slot(s) lost"))
        have = r.norm_labels()
        other_roles = sorted((have & role_set) - {e.role})
        if e.role not in have or other_roles:
            parts = []
            if e.role not in have:
                parts.append(f"lacks its role '{e.role}'")
            if other_roles:
                parts.append(f"carries another role {', '.join(other_roles)}")
            out.append((False, f"role: {e.runner} {' and '.join(parts)} "
                               f"(labels [{', '.join(sorted(have))}])"))
        stray = sorted(l for l in have if l.startswith(CLASS_PREFIX) and l != e.class_label)
        if (e.class_label and e.class_label not in have) or stray:
            want = f"'{e.class_label}'" if e.class_label else 'no class label'
            out.append((False, f"class label: {e.runner} should carry {want} "
                               f"(labels [{', '.join(sorted(have))}]) — asf ci reconcile"))

    for r in runners:
        if r.name not in declared:
            out.append((False, f"undeclared: {r.name} is registered but not in ci.pool"))

    if not out:
        by_role = slots_by_role(pool)
        out.append((True, f"{len(pool)} runners declared, all online and reachable "
                          f"({', '.join(f'{k} {v}' for k, v in by_role.items())})"))
    return out


def doctor_rows(product, backend=None):
    """``[(required, ok, detail)]`` for the doctor's ``ci pool`` rows; ``[]`` without a pool —
    no CI host call at all then. A host that cannot be read is one unknown row. The class rows
    (:func:`class_rows`) follow, advisory (not required)."""
    pool = load_pool(product)
    if not pool:
        return []
    classes = [(False, ok, detail) for ok, detail in class_rows(pool)]  # no host call needed
    backend = backend or backend_for(product)
    if backend is None:
        return [(False, None, f"ci.pool: no runner backend for ci.provider "
                              f"{(product.ci or {}).get('provider')!r} in this release")] + classes
    try:
        runners, runs_on = backend.runners(), backend.runs_on()
    except BackendError as e:
        return [(False, None, f'ci.pool: cannot read the CI host — {e}')] + classes
    return [(True, ok, detail) for ok, detail in drift(pool, runners, runs_on)] + classes


# ---- reconcile: the plan and its apply --------------------------------------------------------

@dataclasses.dataclass
class Step:
    runner: str
    current: list
    target: list
    add: list
    remove: list
    blocked: list
    note: str = ''
    trial: bool = False
    entry: object = None
    host: object = None

    @property
    def action(self):
        if self.note:
            return self.note
        parts = []
        if self.add:
            parts.append('add ' + ', '.join(self.add))
        if self.remove:
            parts.append('remove ' + ', '.join(self.remove))
        if self.blocked:
            parts.append('keep ' + ', '.join(self.blocked) + ' — blocked until workflows migrate')
        if self.trial:
            parts.append(f'trial: one {self.entry.role} job')
        return '; '.join(parts) or 'ok'


def plan(pool, runners, runs_on, trials=None):
    """One :class:`Step` per declared runner, then one per undeclared one (left untouched).

    The target is the host's own labels plus the role, ``provider-<name>`` and, when declared,
    ``class:<name>``; every other label
    goes — unless a current self-hosted ``runs-on`` needs it on this runner (the job's labels are
    all on the runner once the adds are in), in which case it is ``blocked``."""
    trials = trials or {}
    by_name = {r.name: r for r in runners}
    jobs = [ro for ro in runs_on if ro.self_hosted]
    out = []
    for e in pool:
        r = by_name.get(e.runner)
        if r is None:
            out.append(Step(e.runner, [], sorted(e.labels()), [], [], [],
                            note='missing on the CI host — register it', entry=e))
            continue
        have = r.norm_labels()
        keep = {l for l in have if is_default(l, r)}
        target = keep | e.labels()
        add = sorted(target - have)
        after = have | set(add)
        remove, blocked = [], []
        for label in sorted(have - target):
            needed = any(label in ro.labels and ro.labels <= after for ro in jobs)
            (blocked if needed else remove).append(label)
        trial = e.role in add and e.runner not in trials
        out.append(Step(e.runner, _ordered(r.labels), _ordered(target), add, remove, blocked,
                        trial=trial, entry=e, host=r))
    declared = {e.runner for e in pool}
    for r in runners:
        if r.name not in declared:
            out.append(Step(r.name, _ordered(r.labels), [], [], [], [],
                            note='not in ci.pool — untouched', host=r))
    return out


def _ordered(labels):
    """Labels for display: the host's defaults first (as the host lists them), then the rest."""
    labels = [_norm(l) for l in labels]
    return sorted(set(labels), key=lambda l: (not is_default(l), l))


def plan_table(steps):
    rows = [(s.runner, ', '.join(s.current) or '—', ', '.join(s.target) or '—', s.action)
            for s in steps]
    return _table(('runner', 'current labels', 'target labels', 'action'), rows)


def _table(headers, rows):
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    fmt = lambda cells: '  '.join(c if i == len(cells) - 1 else c.ljust(widths[i])  # noqa: E731
                                  for i, c in enumerate(cells))
    return '\n'.join([fmt(headers)] + [fmt(r) for r in rows])


def apply(steps, backend, product_name, now=None, out=print):
    """Carry the plan out, never losing capacity: every runner's adds first, then the removes.
    A runner whose role label is added starts a trial (:func:`start_trial`). Returns the number
    of label writes that failed."""
    failed = 0
    for s in steps:
        if s.add and s.host is not None:
            try:
                backend.add_labels(s.host, s.add)
                out(f"ci reconcile: {s.runner} + {', '.join(s.add)}")
                if s.trial:
                    start_trial(product_name, s, now=now)
                    out(f"ci reconcile: {s.runner} is on trial for one {s.entry.role} job")
            except BackendError as e:
                failed += 1
                out(f"ci reconcile: {s.runner} add failed — {e}")
    for s in steps:
        for label in s.remove:
            try:
                backend.remove_label(s.host, label)
                out(f"ci reconcile: {s.runner} - {label}")
            except BackendError as e:
                failed += 1
                out(f"ci reconcile: {s.runner} remove {label} failed — {e}")
    return failed


# ---- trials -----------------------------------------------------------------------------------

def _now_iso(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%SZ')


def _trials_path(product_name):
    return os.path.join(env.state_dir(product_name), TRIALS_FILE)


def load_trials(product_name):
    try:
        with open(_trials_path(product_name), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_trials(product_name, trials):
    path = _trials_path(product_name)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(trials, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _history(product_name, rec):
    with open(os.path.join(env.state_dir(product_name), TRIAL_HISTORY), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, sort_keys=True) + '\n')


def start_trial(product_name, step, now=None):
    trials = load_trials(product_name)
    trials[step.runner] = {'role': step.entry.role, 'since': _now_iso(now), 'state': 'pending',
                           'labels_before': list(step.current), 'added': list(step.add)}
    save_trials(product_name, trials)


def _set_labels(backend, runner, want):
    """Bring ``runner`` to exactly ``want`` (host defaults aside): add, then remove."""
    have = runner.norm_labels()
    add = sorted(set(want) - have)
    if add:
        backend.add_labels(runner, add)
    for label in sorted(have - set(want)):
        if not is_default(label, runner):
            backend.remove_label(runner, label)


def check_trials(product_name, backend, apply_changes=False, now=None, out=print):
    """Judge every pending trial by its runner's first job since the trial began.

    No job yet: it waits. A job running: with ``apply_changes`` the role label is withdrawn so
    no second job lands before the verdict. Passed: the role label is back (if withdrawn) and the
    trial ends. Failed: the runner's labels go back to what they were before the reconcile and
    the trial stays as ``rolled_back`` until :func:`tick` files its Bug. Returns
    ``[(runner, verdict, detail)]``; without ``apply_changes`` nothing is written anywhere."""
    trials = load_trials(product_name)
    pending = {k: v for k, v in trials.items() if v.get('state') in ('pending', 'running')}
    if not pending:
        return [(k, v.get('state'), v.get('job', {}).get('url', ''))
                for k, v in trials.items()]
    try:
        hosts = {r.name: r for r in backend.runners()}
    except BackendError as e:
        return [(k, 'unknown', f'cannot read the CI host — {e}') for k in pending]
    verdicts = []
    for name, t in sorted(pending.items()):
        host = hosts.get(name)
        try:
            jobs = [j for j in backend.jobs_on(name, t['since'])
                    if j.get('status') != 'completed' or j.get('conclusion') in PASS | FAIL]
        except BackendError as e:
            verdicts.append((name, 'unknown', str(e)))
            continue
        if not jobs:
            verdicts.append((name, 'waiting', f"no job on {name} since {t['since']}"))
            continue
        job = jobs[0]
        label = f"{job.get('name')} {job.get('url') or ''}".strip()
        if job.get('status') != 'completed':
            if apply_changes and host is not None and t['state'] == 'pending':
                try:
                    backend.remove_label(host, t['role'])
                    t.update(state='running', withdrawn=True, job=job)
                    out(f"ci trial: {name} runs {label} — '{t['role']}' withdrawn until it ends")
                except BackendError as e:
                    out(f"ci trial: {name} could not withdraw '{t['role']}' — {e}")
            verdicts.append((name, 'running', label))
            continue
        if job.get('conclusion') in PASS:
            if apply_changes:
                if t.get('withdrawn') and host is not None:
                    try:
                        backend.add_labels(host, [t['role']])
                    except BackendError as e:
                        out(f"ci trial: {name} passed, '{t['role']}' not restored — {e}")
                        verdicts.append((name, 'passed', label))
                        continue
                trials.pop(name)
                _history(product_name, dict(t, runner=name, state='passed', job=job,
                                            at=_now_iso(now)))
                out(f"ci trial: {name} passed ({label}) — kept as {t['role']}")
            verdicts.append((name, 'passed', label))
            continue
        if apply_changes:
            if host is not None:
                try:
                    _set_labels(backend, host, [_norm(l) for l in t.get('labels_before', [])])
                except BackendError as e:
                    out(f"ci trial: {name} failed, rollback failed — {e}")
                    verdicts.append((name, 'failed', label))
                    continue
            t.update(state='rolled_back', job=job, at=_now_iso(now), bug_filed=False)
            _history(product_name, dict(t, runner=name))
            out(f"ci trial: {name} failed ({label}) — labels rolled back to "
                f"[{', '.join(t.get('labels_before', []))}]")
        verdicts.append((name, 'failed', label))
    if apply_changes:
        save_trials(product_name, trials)
    return verdicts


def trial_bug(name, trial):
    """``(signature, info)`` for :func:`asf.tick.file_bugs._file_or_bump_bug`."""
    job = trial.get('job') or {}
    sig = f"ci trial failed: {name} as {trial.get('role')}"
    info = {
        'title': f"CI runner {name} failed its trial as a {trial.get('role')} runner and was rolled back",
        'severity': 'S2', 'runs': [job['url']] if job.get('url') else [],
        'evidence': [f"trial since {trial.get('since')}: first job {job.get('name')} ended "
                     f"{job.get('conclusion')} {job.get('url') or ''}".strip(),
                     f"labels rolled back to [{', '.join(trial.get('labels_before', []))}]"],
        'acceptance': [f"{name} passes a trial job as {trial.get('role')} "
                       f"(`asf ci reconcile --apply` starts one)"],
    }
    return sig, info


def tick(ctx, backend=None, out=print):
    """The tick's half (the ``health`` step): judge pending trials, apply their verdicts, and file
    one Bug per rolled-back trial in the record clone. No trials file, no CI host call."""
    product = ctx.product
    trials = load_trials(product.name)
    if not trials:
        return
    if any(t.get('state') in ('pending', 'running') for t in trials.values()):
        backend = backend or backend_for(product)
        if backend is None:
            return
        check_trials(product.name, backend, apply_changes=True, out=out)
        trials = load_trials(product.name)
    unfiled = {k: v for k, v in trials.items()
               if v.get('state') == 'rolled_back' and not v.get('bug_filed')}
    if not unfiled:
        return
    from asf import approvals
    from asf.record.core import canonicalize, load_items, today
    from asf.record.index import do_index
    from asf.tick import file_bugs
    if approvals.level_of(product, 'file_bug') != 'auto':
        for name in unfiled:
            out(f"held file_bug on {trial_bug(name, unfiled[name])[0]}")
        return
    root = ctx.record_root()
    by_id, _errors = load_items(root)
    canonical, _dupes = canonicalize(by_id)
    epic, _why = file_bugs.usable_bug_epic(canonical, product.conventions.get('default_bug_epic'))
    for name, t in sorted(unfiled.items()):
        sig, info = trial_bug(name, t)
        outcome = file_bugs._file_or_bump_bug(root, canonical, sig, info, today(),
                                              default_bug_epic=epic)
        out(f"ci trial: {name} — Bug {outcome} ({sig})")
        trials.pop(name)
        by_id, _errors = load_items(root)
        canonical, _dupes = canonicalize(by_id)
    do_index(root)
    save_trials(product.name, trials)


# ---- the command ------------------------------------------------------------------------------

def cmd_reconcile(args):
    product = env.load_product(args.product)
    pool = load_pool(product)
    if not pool:
        print(f"ci reconcile: {product.name} declares no ci.pool — nothing to reconcile")
        return 0
    backend = backend_for(product)
    if backend is None:
        print(f"ci reconcile: no runner backend for ci.provider "
              f"{(product.ci or {}).get('provider')!r} in this release")
        return 2
    try:
        verdicts = check_trials(product.name, backend, apply_changes=args.apply)
        runners, runs_on = backend.runners(), backend.runs_on()
    except BackendError as e:
        print(f"ci reconcile: cannot read the CI host — {e}")
        return 2
    steps = plan(pool, runners, runs_on, trials=load_trials(product.name))
    mode = 'APPLY' if args.apply else 'DRY RUN — nothing changed; --apply writes the labels'
    print(f"== CI RECONCILE {product.name} ({mode})")
    print(plan_table(steps))
    for name, verdict, detail in verdicts:
        print(f"trial {name}: {verdict} {detail}".rstrip())
    if not args.apply:
        return 0
    return 1 if apply(steps, backend, product.name) else 0


def register(subparsers):
    p = subparsers.add_parser('ci', help='the CI runner pool: reconcile the host with ci.pool')
    sub = p.add_subparsers(dest='ci_command', required=True)
    r = sub.add_parser('reconcile', help='plan (default) or --apply the labels ci.pool declares')
    env.add_product_arg(r)
    r.add_argument('--apply', action='store_true',
                   help='write the labels (adds before removes); default is a dry-run plan')
    r.set_defaults(run=cmd_reconcile)
    return p
