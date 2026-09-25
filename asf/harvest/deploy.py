"""asf.harvest.deploy — the deploy pass, per environment: dispatch an environment's deploy
workflow for a green trunk sha, or say loudly what that environment waits on.

Each environment under ``deploy_sha`` (``dev``, ``prod``) carries a ``mode``:

* ``auto`` — the tick dispatches the environment's ``workflow`` for its candidate sha;
* ``manual`` — nothing dispatches; every tick, ``asf status`` and ``asf prod`` name the sha that
  waits on a hand dispatch of that workflow;
* ``ci`` (dev only) — the product's own CI deploys dev (``ci.dev_job`` in ``ci.workflow``); ASF
  only observes which trunk sha dev runs.

``prod`` defaults to ``manual``; ``dev`` without a ``mode`` is not managed at all. The legacy
``deploy_sha.auto: true`` is read as ``deploy_sha.prod.mode: auto`` (``asf doctor`` names it as
deprecated), and the legacy top-level ``deploy_sha.workflow`` / ``input`` are prod's.

The *candidate* of an environment:

* dev — the newest completed ``ci.workflow`` run on the trunk that concluded ``success``;
* prod — the same (``prod.from: ci``, the default), or the sha dev runs (``prod.from: dev``,
  promote dev to prod) provided that sha's own ``ci.workflow`` run is green.

The rules are the same for every environment: the candidate deploys only when it descends from
the environment's deployed sha (the newest successful run of its ``workflow``), no run of that
workflow is queued or running, no deploy of that very sha has already failed (a failed sha is
not retried; the next green sha is), and it is green — a red trunk never deploys. The dispatch is
``gh workflow run <workflow> -R <slug> --ref <trunk> -f <input>=<sha>`` (``input`` default
``sha``; ``none`` sends no input).

Every outcome is one line per environment — ``deploy:`` for prod, ``deploy dev:`` for dev. The
tick prints them (:func:`tick`); ``asf status`` / ``asf prod`` print the read-only half
(:func:`lines`).
"""
import datetime
import json
import subprocess

DEFAULT_INPUT = 'sha'
ENVS = ('dev', 'prod')
#: the modes each environment may take
MODES = {'dev': ('auto', 'manual', 'ci'), 'prod': ('auto', 'manual')}
#: where prod's candidate comes from
SOURCES = ('ci', 'dev')


def _sh(cmd, cwd=None, timeout=60):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _json(text, kind=list):
    try:
        v = json.loads(text) if text else None
    except ValueError:
        return None
    return v if isinstance(v, kind) else None


def _cfg(product):
    d = getattr(product, 'deploy_sha', None)
    return d if isinstance(d, dict) else {}


def env_cfg(product, env):
    d = _cfg(product).get(env)
    return d if isinstance(d, dict) else {}


def _conv(product):
    return getattr(product, 'conventions', None) or {}


def legacy_auto(product):
    """True when the deprecated ``deploy_sha.auto: true`` is set."""
    return _cfg(product).get('auto') is True


def mode(product, env='prod'):
    """``auto``, ``manual`` or ``ci`` for ``env``; None for a dev that is not managed."""
    m = env_cfg(product, env).get('mode')
    if m in MODES.get(env, ()):
        return m
    if env == 'prod':
        return 'auto' if legacy_auto(product) else 'manual'
    return None


def auto(product, env='prod'):
    return mode(product, env) == 'auto'


def ci_workflow(product):
    return _conv(product).get('ci_workflow')


def workflow(product, env='prod'):
    """The environment's deploy workflow: whose newest success is its deployed sha, and what
    ``auto`` dispatches. For a ``ci`` dev it is ``ci.workflow``."""
    if env == 'prod':
        return (_conv(product).get('deploy_workflow') or env_cfg(product, 'prod').get('workflow')
                or _cfg(product).get('workflow'))
    if mode(product, env) == 'ci':
        return ci_workflow(product)
    return env_cfg(product, env).get('workflow')


def source(product):
    """Where prod's candidate comes from: ``ci`` (the newest green trunk sha) or ``dev``."""
    v = env_cfg(product, 'prod').get('from')
    return v if v in SOURCES else 'ci'


def input_name(product, env='prod'):
    c = env_cfg(product, env)
    if 'input' in c:
        v = c['input']
    elif env == 'prod':
        v = _cfg(product).get('input', DEFAULT_INPUT)
    else:
        v = DEFAULT_INPUT
    return None if v in (None, '', 'none') else str(v)


def _hosted(product):
    return bool(ci_workflow(product) and getattr(product, 'repo_slug', None)
                and getattr(product, 'repo_dir', None))


def env_applies(product, env):
    """True when ``env`` is managed and names what this module needs to reason about it."""
    if not _hosted(product):
        return False
    if env == 'dev':
        return mode(product, 'dev') is not None and bool(workflow(product, 'dev'))
    return bool(workflow(product, 'prod'))


def applies(product):
    """True when any environment applies (:func:`env_applies`)."""
    return any(env_applies(product, e) for e in ENVS)


def findings(product):
    """[(ok, detail)] — the doctor's ``deploy`` row: the resolved modes, and every config the
    module reads differently from how it is written (a deprecated key, a dev without a
    workflow, ``prod.from: dev`` with no managed dev)."""
    if not _cfg(product):
        return []
    out = []
    pm = env_cfg(product, 'prod').get('mode')
    if 'auto' in _cfg(product):
        out.append((False, 'deploy_sha.auto is deprecated — write deploy_sha.prod.mode: '
                           + ('auto' if legacy_auto(product) else 'manual')
                           + (f' (prod.mode: {pm} is set and wins)' if pm else '')))
    dm = mode(product, 'dev')
    if dm in ('auto', 'manual') and not env_cfg(product, 'dev').get('workflow'):
        out.append((False, f'deploy_sha.dev.mode: {dm} names no deploy_sha.dev.workflow — dev'
                           ' is not managed'))
    if source(product) == 'dev' and not env_applies(product, 'dev'):
        out.append((False, 'deploy_sha.prod.from: dev, but dev is not managed'
                           ' (deploy_sha.dev.mode) — prod has no candidate'))
    if workflow(product, 'prod') or dm:
        prod = (f"prod {mode(product, 'prod')} ({workflow(product, 'prod')}, from "
                f"{source(product)})" if workflow(product, 'prod') else 'prod not configured')
        out.append((True, f"dev {dm or 'not managed'} · {prod}"))
    return out


def _age(iso, now):
    try:
        t = datetime.datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    h = (now - t).total_seconds() / 3600
    return f'{h / 24:.1f}d' if h >= 48 else f'{h:.0f}h'


def _runs(product, wf, sh, branch=None):
    argv = ['gh', 'run', 'list', '-R', product.repo_slug, '--workflow', wf]
    if branch:
        argv += ['--branch', branch]
    return _json(sh(argv + ['--limit', '20', '--json',
                            'databaseId,headSha,status,conclusion,updatedAt']))


def _green(run):
    return run.get('status') == 'completed' and run.get('conclusion') == 'success'


def _behind(product, base, sh):
    if not base:
        return None
    count = sh(['git', '-C', product.repo_dir, 'rev-list', '--count',
                f'{base}..origin/{product.main}'])
    return int(count) if count and count.isdigit() else None


def _ahead(product, base, sha, sh):
    """True when ``sha`` descends from ``base`` (or there is no base yet)."""
    return base is None or sh(['git', '-C', product.repo_dir, 'merge-base', '--is-ancestor',
                               base, sha]) is not None


def _dev_job_run(product, ci_runs, sh, limit=5):
    """The run whose trunk sha a ``ci`` dev runs: the newest completed ``ci.workflow`` run whose
    ``ci.dev_job`` succeeded (the newest green run when no ``dev_job`` is named)."""
    job = _conv(product).get('ci_dev_job')
    done = [r for r in ci_runs if r.get('status') == 'completed']
    if not job:
        return next((r for r in done if r.get('conclusion') == 'success'), None)
    for r in done[:limit]:
        if r.get('conclusion') == 'success':
            return r  # a green run is green in every job, its dev job included
        view = _json(sh(['gh', 'run', 'view', str(r.get('databaseId')), '-R', product.repo_slug,
                         '--json', 'jobs']), dict) or {}
        if any(j.get('name') == job and j.get('conclusion') == 'success'
               for j in view.get('jobs') or []):
            return r
    return None


def _sha_green(product, sha, ci_runs, sh):
    """True when ``sha`` has a green ``ci.workflow`` run (looked up by commit when it is older
    than the trunk runs already read)."""
    if any(r.get('headSha') == sha and _green(r) for r in ci_runs):
        return True
    runs = _json(sh(['gh', 'run', 'list', '-R', product.repo_slug, '--workflow',
                     ci_workflow(product), '--commit', sha, '--json', 'status,conclusion'])) or []
    return any(_green(r) for r in runs)


def _blank(product, env):
    return {'env': env, 'mode': mode(product, env), 'workflow': workflow(product, env),
            'ci': ci_workflow(product), 'deployed': None, 'prod': None, 'running': None,
            'failed': None, 'candidate': None, 'main': None, 'behind': None, 'age': None,
            'error': None, 'why': None}


def _done(f):
    f['prod'] = f['deployed']  # the old name, kept for callers that read prod's facts
    return f


def facts(product, sh=_sh, now=None, env='prod', _ci=None):
    """The facts of one environment, read-only: ``deployed`` (the sha it runs), ``running`` (a
    run of its workflow not yet completed), ``failed`` (the candidate's own failed deploy run
    id), ``candidate``, ``main``, ``behind``, ``age`` (since ``deployed`` landed), ``why`` (why
    there is no candidate, beyond a red trunk) and ``error`` (what could not be read)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    trunk = product.main
    f = _blank(product, env)
    ci = _ci if _ci is not None else _runs(product, f['ci'], sh, branch=trunk)
    if ci is None:
        f['error'] = f"{f['ci']} runs on {trunk} unreadable (gh run list)"
        return _done(f)
    f['main'] = sh(['git', '-C', product.repo_dir, 'rev-parse', f'origin/{trunk}'])
    if f['mode'] == 'ci':  # the product's CI deploys it; observe only
        run = _dev_job_run(product, ci, sh) or {}
        f['deployed'], f['age'] = run.get('headSha'), _age(run.get('updatedAt'), now)
        f['behind'] = _behind(product, f['deployed'], sh)
        return _done(f)
    runs = _runs(product, f['workflow'], sh)
    if runs is None:
        f['error'] = f"{f['workflow']} runs unreadable (gh run list)"
        return _done(f)
    last = next((r for r in runs if r.get('conclusion') == 'success'), {})
    f['deployed'], f['age'] = last.get('headSha'), _age(last.get('updatedAt'), now)
    live = next((r for r in runs if r.get('status') != 'completed'), None)
    if live:
        f['running'] = (live.get('databaseId'), live.get('headSha'))
    f['behind'] = _behind(product, f['deployed'], sh)
    if env == 'prod' and source(product) == 'dev':
        if not env_applies(product, 'dev'):
            f['why'] = 'deploy_sha.prod.from is dev, but dev is not managed (deploy_sha.dev.mode)'
            return _done(f)
        dev = facts(product, sh=sh, now=now, env='dev', _ci=ci)
        if dev['error']:
            f['error'] = dev['error']
            return _done(f)
        pick = dev['deployed']
        if not pick:
            f['why'] = 'dev runs no readable sha yet'
            return _done(f)
        if not _sha_green(product, pick, ci, sh):
            f['why'] = f"dev's `{pick[:9]}` has no green {f['ci']} run"
            return _done(f)
    else:
        pick = next((r.get('headSha') for r in ci if _green(r)), None)
    if pick and pick != f['deployed'] and _ahead(product, f['deployed'], pick, sh):
        f['candidate'] = pick
        f['failed'] = next((r.get('databaseId') for r in runs if r.get('headSha') == pick
                            and r.get('status') == 'completed'
                            and r.get('conclusion') not in ('success', None)), None)
    return _done(f)


def _s(sha):
    return f'`{(sha or "?")[:9]}`'


def _head(env):
    return 'deploy:' if env == 'prod' else f'deploy {env}:'


def decide(product, f, env=None):
    """``(dispatch, line)`` from :func:`facts`: whether to dispatch the candidate now, and the one
    line that says what the environment is doing or waiting on."""
    env = env or f.get('env') or 'prod'
    wf, trunk, head = f['workflow'], product.main, _head(env)
    if f['error']:
        return False, f"{head} {f['error']} — {env} state unknown"
    at_main = f['behind'] == 0 or bool(f['main'] and f['main'] == f['deployed'])
    lag = (f"{trunk} is {f['behind']} commits ahead of {env} {_s(f['deployed'])}"
           + (f" (deployed {f['age']} ago)" if f['age'] else ''))
    if f.get('mode') == 'ci':
        state = (f"no successful {env} deploy readable on {trunk}" if not f['deployed']
                 else f"{env} {_s(f['deployed'])} is {trunk}" if at_main else lag)
        return False, f"{head} {f['ci']} deploys {env} on its own (ASF observes) — {state}"
    if at_main:
        return False, f"{head} {env} {_s(f['deployed'])} is {trunk}"
    if f['running']:
        rid, sha = f['running']
        return False, f"{head} {wf} run {rid} for {_s(sha)} is queued or running — {lag}"
    if not f['candidate']:
        if f.get('why'):
            return False, f"{head} {lag} — {f['why']}; {env} waits"
        return False, (f"{head} {lag} — no green {f['ci']} run on {trunk} newer than {env};"
                       f" {env} waits on a green {trunk}")
    if f['failed']:
        return False, (f"{head} {wf} for {_s(f['candidate'])} FAILED (run {f['failed']}) — not"
                       f" retried; {lag}; the next green sha dispatches again")
    if f.get('mode') != 'auto':
        return False, (f"{head} {lag} — MANUAL: green {_s(f['candidate'])} waits on a hand"
                       f" dispatch of {wf} (deploy_sha.{env}.mode: manual)")
    return True, f"{head} {lag} — dispatching {wf} for green {_s(f['candidate'])}"


def _each(product, sh):
    """[(env, facts)] for every environment that applies, dev first; the trunk's CI runs are
    read once and shared."""
    envs = [e for e in ENVS if env_applies(product, e)]
    if not envs:
        return []
    ci = _runs(product, ci_workflow(product), sh, branch=product.main)
    return [(e, facts(product, sh=sh, env=e, _ci=ci) if ci is not None else _unread(product, e))
            for e in envs]


def _unread(product, env):
    f = _blank(product, env)
    f['error'] = f"{f['ci']} runs on {product.main} unreadable (gh run list)"
    return _done(f)


def lines(product, sh=_sh):
    """The read-only lines (no dispatch), one per environment that applies, dev first."""
    return [decide(product, f, e)[1] for e, f in _each(product, sh)]


def line(product, sh=_sh):
    """The read-only prod ``deploy:`` line, or None when prod does not apply."""
    if not env_applies(product, 'prod'):
        return None
    return decide(product, facts(product, sh=sh, env='prod'), 'prod')[1]


def dispatch_argv(product, sha, env='prod'):
    argv = ['gh', 'workflow', 'run', workflow(product, env), '-R', product.repo_slug,
            '--ref', product.main]
    name = input_name(product, env)
    if name:
        argv += ['-f', f'{name}={sha}']
    return argv


def tick(product, out=print, sh=_sh):
    """The tick's deploy pass: print one line per environment and, where :func:`decide` says so,
    dispatch its deploy workflow. A refused dispatch is one loud line, and the next tick tries
    again. Returns ``{env: dispatched sha}``, empty when nothing was dispatched."""
    sent = {}
    for e, f in _each(product, sh):
        go, text = decide(product, f, e)
        out(text)
        if not go:
            continue
        if sh(dispatch_argv(product, f['candidate'], e)) is None:
            out(f"{_head(e)} DISPATCH REFUSED — gh workflow run {workflow(product, e)} for"
                f" `{f['candidate'][:9]}` failed; the next tick tries again")
            continue
        sent[e] = f['candidate']
    return sent
