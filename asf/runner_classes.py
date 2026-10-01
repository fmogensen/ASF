"""asf.runner_classes — the ``asf doctor`` rows that keep a required job on one kind of box.

Two read-only checks over the declared pool (``ci.pool``, :mod:`asf.ci_pool`) and the CI host:

* **one class per required job.** Each required CI job (``landing_checks`` plus the deploy's
  required jobs, the set the merge queue gates on) is resolved from its workflow's ``runs-on``
  to the registered runners that carry every label it asks for, and those runners to their
  class: the declared ``class:`` when the pool names one, else ``<provider>/<role>``. Exactly one
  class is green. Two or more is red — the same job then runs on boxes of different speed and
  provider, so its timing (and its timeouts) depend on which one picked it up; none is red too.
  A ``runs-on`` this reader cannot resolve is an unknown row, never a guess.
* **no contradicting provider label.** A runner's labels may name a provider only if it is the
  one the pool declares for it: a box declared ``globex`` carrying ``acme`` (or
  ``acme-heavy``, ``provider-acme``) routes a job that asks for acme onto globex.

Red rows name the job, the runner and the label. Nothing is written: labels are the reconcile's
(``asf ci reconcile``) and the operator's, never this check's.
"""
import json
import re

from asf import ci_pool
from asf.ci_pool import BackendError, PROVIDER_PREFIX, _norm


def runner_class(entry):
    """A pool entry's class: its declared ``class:``, else ``<provider>/<role>``."""
    return entry.cls or f'{_norm(entry.provider)}/{entry.role}'


def provider_names(pool):
    return sorted({_norm(e.provider) for e in pool if e.provider})


def names_provider(label, provider):
    """True when ``label`` names ``provider``: the provider itself, ``provider-<p>``, or a
    ``<p>-…`` / ``…-<p>`` compound (``acme-heavy``)."""
    label, p = _norm(label), _norm(provider)
    return bool(p) and (label == p or label == PROVIDER_PREFIX + p or label.startswith(p + '-')
                        or label.endswith('-' + p))


def provider_conflicts(pool, runners):
    """``[(runner, declared provider, label)]`` — every label on a declared runner that names a
    provider other than its own."""
    by_name = {r.name: r for r in runners}
    providers = provider_names(pool)
    out = []
    for e in pool:
        r = by_name.get(e.runner)
        if r is None:
            continue
        own = _norm(e.provider)
        for label in sorted(r.norm_labels()):
            if names_provider(label, own):
                continue
            if any(names_provider(label, p) for p in providers if p != own):
                out.append((e.runner, own, label))
    return out


def job_for(name, runs_on, workflow=None):
    """The :class:`asf.ci_pool.RunsOn` a required check ``name`` runs as: the job of that id, else
    the longest job id ``name`` extends with ``-`` (a matrix cell's check ``p1-e2e-b`` of job
    ``p1-e2e``). Only ``workflow``'s jobs when one is named. None when no job matches."""
    jobs = [ro for ro in runs_on if workflow is None or ro.workflow == workflow]
    exact = [ro for ro in jobs if ro.job == name]
    if exact:
        return exact[0]
    longer = [ro for ro in jobs if name.startswith(ro.job + '-')]
    return max(longer, key=lambda ro: len(ro.job)) if longer else None


def classes_of(ro, pool, runners):
    """``{class: [runner names]}`` of the registered runners that satisfy ``ro`` (a runner the
    pool does not declare is class ``undeclared``)."""
    declared = {e.runner: e for e in pool}
    out = {}
    for r in sorted(runners, key=lambda r: r.name):
        if ro.labels is not None and ro.labels <= r.norm_labels():
            cls = runner_class(declared[r.name]) if r.name in declared else 'undeclared'
            out.setdefault(cls, []).append(r.name)
    return out


def judge(required, runs_on, pool, runners, workflow=None):
    """``[(ok, detail)]`` for the required jobs and the provider labels: a red row per job on no
    or several classes and per contradicting label, an unknown (``None``) row per job this
    reader cannot resolve, one ok row summing the jobs that resolve to one class."""
    rows, single = [], []
    for name in required:
        ro = job_for(name, runs_on, workflow)
        where = f'{workflow}:{name}' if workflow else name
        if ro is None:
            rows.append((None, f'required job {where}: no job in the workflow runs it — not judged'))
            continue
        if ro.labels is None:
            why = (f"asks for {', '.join('vars.' + v for v in sorted(ro.needs_vars))}, which the "
                   f"host did not name" if ro.needs_vars else 'its runs-on is an expression')
            rows.append((None, f'required job {where}: runs-on unresolved — {why}; not judged'))
            continue
        got = classes_of(ro, pool, runners)
        if not got and not ro.self_hosted:   # a host-provided image (ubuntu-latest, …)
            single.append(f"{name}=hosted")
            continue
        asks = ', '.join(sorted(ro.labels))
        if not got:
            rows.append((False, f'required job {where} [{asks}]: no registered runner carries '
                                f'all of it'))
        elif len(got) > 1:
            parts = '; '.join(f"{c}: {', '.join(names)}" for c, names in got.items())
            rows.append((False, f'required job {where} [{asks}] resolves to {len(got)} runner '
                                f'classes — {parts}'))
        else:
            single.append(f'{name}={next(iter(got))}')
    for runner, own, label in provider_conflicts(pool, runners):
        rows.append((False, f"runner {runner} is declared provider {own} but carries the label "
                            f"'{label}' — a job asking for it lands on the wrong provider"))
    if single:
        rows.insert(0, (True, f"{len(single)} required job(s) on one runner class each: "
                              f"{', '.join(single)}"))
    return rows


def required_names(product):
    """The required checks the merge queue gates on: ``landing_checks`` plus the deploy's
    required jobs read at the trunk tip. ``[]`` when none is named."""
    conv = product.conventions
    named = conv.get('landing_checks') if hasattr(conv, 'get') else None
    names = [str(named)] if isinstance(named, str) else [str(n) for n in named or ()]
    try:
        from asf.harvest import deploy
        jobs, _src = deploy.required_jobs(product, 'prod', f'origin/{conv.main}')
    except Exception:  # noqa: BLE001 — an unreadable deploy list leaves the landing checks
        jobs = None
    names += [j for j in jobs or () if j not in names]
    return names


def resolved_runs_on(backend):
    """The host's ``runs-on`` for every job, with each ``vars.X`` the host does not name taken
    as unset — its ``|| 'default'`` — but only when both the repo's and the organisation's
    variables were read (absent from both is unset; :func:`asf.ci_pool._label_token` keeps
    absent-unknown for the reconcile, which removes labels). Any other backend: its own reading."""
    if not isinstance(backend, ci_pool.GitHubBackend):
        return backend.runs_on()
    try:
        variables = backend.variables()
    except BackendError:
        return backend.runs_on()
    if backend.base.startswith('orgs/'):
        try:
            org = backend._lines(['--paginate', f'{backend.base}/actions/variables?per_page=100',
                                  '--jq', '.variables[]'])
        except BackendError:
            return backend.runs_on()
        variables = {**{str(v['name']): str(v.get('value') or '') for v in org if v.get('name')},
                     **variables}
    out = []
    for name, text in backend._workflow_texts():
        ros = ci_pool.parse_runs_on(name, text, variables)
        missing = set().union(*(ro.needs_vars for ro in ros)) if ros else set()
        if missing:
            variables = {**variables, **{m: '' for m in missing}}
            ros = ci_pool.parse_runs_on(name, text, variables)
        out += with_formats(ros, text, variables)
    return out


_FORMAT = re.compile(r"^\$\{\{\s*fromJSON\(\s*format\(\s*'([^']*)'\s*,(.*)\)\s*\)\s*\}\}$")


def _split_args(text):
    out, depth, cur, quote = [], 0, '', None
    for c in text:
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif c == ',' and depth == 0:
            out.append(cur.strip())
            cur = ''
            continue
        cur += c
    if cur.strip():
        out.append(cur.strip())
    return out


def _eval_arg(expr, variables):
    """One ``format`` argument as a push run of the trunk or a batch ref sees it (the runs the
    queue gates on): ``a || b || 'c'`` left to right, ``cond && 'x'`` taken only when ``cond`` is
    known true — ``github.event_name == 'pull_request'`` is false on a push, any other condition
    (a ``needs.*`` output) is read as false, the job's ordinary path. None when unknowable."""
    known = {k.upper(): v for k, v in (variables or {}).items()}
    for alt in (a.strip() for a in expr.split('||')):
        if '&&' in alt:
            cond, _sep, val = alt.partition('&&')
            if 'pull_request' in cond and '!=' in cond and 'event_name' in cond:
                alt = val.strip()   # not a PR: true on a push
            else:
                continue
        m = re.fullmatch(r"'([^']*)'", alt)
        if m:
            if m.group(1):
                return m.group(1)
            continue
        m = re.fullmatch(r'vars\.([A-Za-z_][A-Za-z0-9_-]*)', alt)
        if m:
            if m.group(1).upper() not in known:
                return None
            if known[m.group(1).upper()]:
                return known[m.group(1).upper()]
            continue
        return None
    return ''


def format_labels(value, variables):
    """The labels a ``${{ fromJSON(format('[...]', …)) }}`` runs-on asks for on a push run, or
    None when it is not that form or an argument is unknowable."""
    m = _FORMAT.match(value.strip())
    if not m:
        return None
    args = [_eval_arg(a, variables) for a in _split_args(m.group(2))]
    if any(a is None for a in args):
        return None
    text = m.group(1)
    for i, a in enumerate(args):
        text = text.replace('{%d}' % i, a)
    try:
        got = json.loads(text)
    except ValueError:
        return None
    return frozenset(_norm(x) for x in got) if isinstance(got, list) else None


def with_formats(ros, text, variables):
    """``ros`` with each ``fromJSON(format(…))`` runs-on read in full (:func:`format_labels`):
    the line reader takes such a line as one token. Matched to ``ros`` by file order; a count
    that differs leaves ``ros`` as read."""
    values = []
    for raw in text.splitlines():
        stripped = ci_pool._strip_comment(raw).strip()
        if stripped.startswith('runs-on:'):
            values.append(stripped[len('runs-on:'):].strip())
    if len(values) != len(ros):
        return ros
    out = []
    for ro, value in zip(ros, values):
        labels = format_labels(value, variables) if 'fromJSON' in value else None
        out.append(ci_pool.RunsOn(ro.workflow, ro.job, labels) if labels else ro)
    return out


def doctor_rows(product, backend=None, required=None):
    """``[(required, ok, detail)]`` for the doctor's ``ci classes`` rows; ``[]`` without a
    declared pool. A host that cannot be read is one unknown row."""
    pool = ci_pool.load_pool(product) if ci_pool.pool_mode(product) == 'declared' else []
    if not pool:
        return []
    backend = backend or ci_pool.backend_for(product)
    if backend is None:
        return []
    try:
        runners, runs_on = backend.runners(), resolved_runs_on(backend)
    except BackendError as e:
        return [(True, None, f'cannot read the CI host — {e}')]
    names = required_names(product) if required is None else list(required)
    ci = product.ci if isinstance(getattr(product, 'ci', None), dict) else {}
    workflow = ci.get('workflow') or None
    return [(True, ok, detail) for ok, detail in judge(names, runs_on, pool, runners, workflow)]
