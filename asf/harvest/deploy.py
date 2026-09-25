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

**Named targets.** Beside dev and prod, ``deploy_sha.targets`` names any number of further deploy
targets (a marketing site, docs, a second app), each ``{mode, workflow, input, from, paths,
source}``: ``mode`` is ``auto`` | ``manual`` (the default) | ``ci`` (its own workflow deploys it on
push; ASF observes), ``from`` is prod's (``ci`` | ``dev``), and ``paths`` (globs, ``apps/site/**``)
make the target behind only by the trunk commits that touch them. Its deployed sha is the newest
successful run of its ``workflow``, or, with ``source: vercel`` (``project``, ``scope``), the
newest READY production deployment's commit. The rules above hold for every target.

**Customer content.** Before any dispatch the candidate's tree is scanned under
``conventions.customer_content.paths`` (:func:`marker_refusal`): a forbidden marker there — an
internal note, a TODO, a placeholder — refuses the dispatch with one loud line, every tick, until
a trunk commit removes it.

Every outcome is one line per environment — ``deploy:`` for prod, ``deploy <name>:`` for the
rest, dev and the named targets after it. The tick prints them (:func:`tick`); ``asf status`` /
``asf prod`` print the read-only half (:func:`lines`).
"""
import datetime
import json
import os
import subprocess

DEFAULT_INPUT = 'sha'
ENVS = ('dev', 'prod')
#: the modes each environment may take; a named target takes TARGET_MODES
MODES = {'dev': ('auto', 'manual', 'ci'), 'prod': ('auto', 'manual')}
TARGET_MODES = ('auto', 'manual', 'ci')
#: where prod's (and a named target's) candidate comes from
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


def _targets(product):
    t = _cfg(product).get('targets')
    return t if isinstance(t, dict) else {}


def target_names(product):
    """The named targets under ``deploy_sha.targets``, in file order (``dev``/``prod`` are not
    names a target may take)."""
    return [n for n, c in _targets(product).items() if n not in ENVS and isinstance(c, dict)]


def is_target(env):
    """True for a named target (not ``dev`` or ``prod``)."""
    return env not in ENVS


def names(product):
    """Every environment the deploy pass may reason about: dev, prod, then the named targets."""
    return list(ENVS) + target_names(product)


def key(env):
    """The dotted config key of ``env``: ``deploy_sha.prod``, ``deploy_sha.targets.site``."""
    return f'deploy_sha.{env}' if env in ENVS else f'deploy_sha.targets.{env}'


def env_cfg(product, env):
    d = _cfg(product).get(env) if env in ENVS else _targets(product).get(env)
    return d if isinstance(d, dict) else {}


def paths(product, env):
    """The target's ``paths`` globs ([] = every trunk commit counts)."""
    v = env_cfg(product, env).get('paths')
    if isinstance(v, str):
        v = [v]
    return [p for p in v if isinstance(p, str) and p] if isinstance(v, list) else []


def reader(product, env):
    """How a named target's deployed sha is read: ``vercel`` or ``workflow`` (its newest
    successful run). dev and prod always read their workflow."""
    return 'vercel' if is_target(env) and env_cfg(product, env).get('source') == 'vercel' \
        else 'workflow'


def _conv(product):
    return getattr(product, 'conventions', None) or {}


def legacy_auto(product):
    """True when the deprecated ``deploy_sha.auto: true`` is set."""
    return _cfg(product).get('auto') is True


def mode(product, env='prod'):
    """``auto``, ``manual`` or ``ci`` for ``env``; None for a dev that is not managed."""
    m = env_cfg(product, env).get('mode')
    if m in MODES.get(env, TARGET_MODES):
        return m
    if env == 'prod':
        return 'auto' if legacy_auto(product) else 'manual'
    return 'manual' if is_target(env) else None


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
    if env == 'dev' and mode(product, env) == 'ci':
        return ci_workflow(product)
    return env_cfg(product, env).get('workflow')


def source(product, env='prod'):
    """Where prod's (or a named target's) candidate comes from: ``ci`` (the newest green trunk
    sha) or ``dev``."""
    v = env_cfg(product, env).get('from')
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
    if is_target(env):
        return env in target_names(product) and bool(
            workflow(product, env) or reader(product, env) == 'vercel')
    return bool(workflow(product, 'prod'))


def applies(product):
    """True when any environment applies (:func:`env_applies`)."""
    return any(env_applies(product, e) for e in names(product))


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
    targets = []
    for t in target_names(product):
        tm, wf, k = mode(product, t), workflow(product, t), key(t)
        if not wf and reader(product, t) != 'vercel':
            out.append((False, f'{k} names neither a workflow nor source: vercel — {t} is not'
                               ' managed'))
            continue
        if tm in ('auto', 'ci') and not wf:
            out.append((False, f'{k}.mode: {tm} names no {k}.workflow — nothing dispatches or'
                               f' observes a run; {t} reads as manual'))
        if source(product, t) == 'dev' and not env_applies(product, 'dev'):
            out.append((False, f'{k}.from: dev, but dev is not managed — {t} has no candidate'))
        scope = ', '.join(paths(product, t)) or 'every commit'
        targets.append(f"{t} {tm} ({wf or 'no workflow'}, {scope})")
    if workflow(product, 'prod') or dm or targets:
        prod = (f"prod {mode(product, 'prod')} ({workflow(product, 'prod')}, from "
                f"{source(product)})" if workflow(product, 'prod') else 'prod not configured')
        out.append((True, ' · '.join([f"dev {dm or 'not managed'}", prod] + targets)))
    return out


def _age(iso, now):
    try:
        if isinstance(iso, (int, float)):  # epoch milliseconds (vercel's createdAt)
            t = datetime.datetime.fromtimestamp(iso / 1000, tz=datetime.timezone.utc)
        else:
            t = datetime.datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (AttributeError, ValueError, OverflowError, OSError):
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


def pathspecs(globs):
    """``paths`` globs as git pathspecs (``**`` crosses ``/``; a trailing ``/`` is a directory)."""
    return [':(glob)' + (g + '**' if g.endswith('/') else g) for g in globs]


def _behind(product, base, sh, globs=None):
    """Trunk commits ``base`` lacks — only those touching ``globs`` when given."""
    if not base:
        return None
    argv = ['git', '-C', product.repo_dir, 'rev-list', '--count', f'{base}..origin/{product.main}']
    if globs:
        argv += ['--'] + pathspecs(globs)
    count = sh(argv)
    return int(count) if count and count.isdigit() else None


def _vercel_deployed(cfg, sh):
    """(sha, createdAt ms) of the newest READY production deployment of ``cfg.project``, (None,
    None) when there is none, or None when ``vercel ls`` cannot be read."""
    out = sh(['vercel', 'ls', str(cfg.get('project') or ''), '--prod', '--scope',
              str(cfg.get('scope') or ''), '--json'])
    if out is None:
        return None
    parsed = _json(out, (dict, list))
    if parsed is None:
        return None
    deps = parsed.get('deployments', []) if isinstance(parsed, dict) else parsed
    for d in deps:
        if isinstance(d, dict) and d.get('state') == 'READY':
            return (d.get('meta') or {}).get('githubCommitSha'), d.get('createdAt')
    return None, None


RECORDS_FILE = 'deploys.json'


def _records_path(product):
    from asf import env as env_mod
    name = getattr(product, 'name', None)
    return os.path.join(env_mod.state_dir(name), RECORDS_FILE) if name else None


def _load_records(product):
    path = _records_path(product)
    try:
        with open(path, encoding='utf-8') as f:
            got = json.load(f)
    except (TypeError, OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def record(product, env, sha, by='hand', now=None):
    """Keep ``sha`` as ``env``'s deployed sha in ``state/<product>/deploys.json`` — the sha
    source for a target whose provider names none (a CLI deploy carries no commit sha).
    ``by`` is ``hand`` (``asf deploy record``: written after the deploy) or ``asf`` (the tick's
    own dispatch: written before the deploy it starts). Returns the record, None when the
    product has no state home."""
    path = _records_path(product)
    if not path:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    recs = _load_records(product)
    recs[env] = {'sha': sha, 'at': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'by': by}
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(recs, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return recs[env]


def recorded_sha(product, env, deployed_at=None):
    """``env``'s recorded sha when it can be the deployment the provider shows (created at
    ``deployed_at``, epoch ms or ISO): a hand record written at or after it, an ``asf`` dispatch
    written at or before it — else None. With no deployment time, any record stands."""
    rec = _load_records(product).get(env)
    if not isinstance(rec, dict) or not rec.get('sha'):
        return None
    if deployed_at is None:
        return rec['sha']
    try:
        at = datetime.datetime.fromisoformat(str(rec.get('at')).replace('Z', '+00:00'))
        if isinstance(deployed_at, (int, float)):
            dep = datetime.datetime.fromtimestamp(deployed_at / 1000, tz=datetime.timezone.utc)
        else:
            dep = datetime.datetime.fromisoformat(str(deployed_at).replace('Z', '+00:00'))
    except (ValueError, OverflowError, OSError):
        return None
    slack = datetime.timedelta(minutes=2)  # the provider's clock against ours
    ok = at >= dep - slack if rec.get('by') != 'asf' else at <= dep + slack
    return rec['sha'] if ok else None


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
            'failed': None, 'candidate': None, 'main': None, 'behind': None, 'age': None, 'at': None,
            'error': None, 'why': None, 'paths': paths(product, env), 'relevant': None,
            'reader': reader(product, env)}


def _done(f):
    f['prod'] = f['deployed']  # the old name, kept for callers that read prod's facts
    if f['relevant'] is None and not f['paths']:
        f['relevant'] = f['behind']
    return f


def _set_behind(product, f, sh):
    f['behind'] = _behind(product, f['deployed'], sh)
    if f['paths']:
        f['relevant'] = (0 if f['behind'] == 0
                         else _behind(product, f['deployed'], sh, f['paths']))


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
    if env == 'dev' and f['mode'] == 'ci':  # the product's CI deploys it; observe only
        run = _dev_job_run(product, ci, sh) or {}
        f['deployed'], f['at'] = run.get('headSha'), run.get('updatedAt')
        f['age'] = _age(f['at'], now)
        _set_behind(product, f, sh)
        return _done(f)
    runs = _runs(product, f['workflow'], sh) if f['workflow'] else []
    if runs is None:
        f['error'] = f"{f['workflow']} runs unreadable (gh run list)"
        return _done(f)
    if f['reader'] == 'vercel':
        got = _vercel_deployed(env_cfg(product, env), sh)
        if got is None:
            f['error'] = f"{env}'s deployed sha unreadable (vercel ls)"
            return _done(f)
        f['deployed'], f['at'] = got[0], got[1]
        f['age'] = _age(f['at'], now)
        if not f['deployed'] and got[1] is not None:  # a CLI deploy: no commit sha on it
            f['deployed'] = recorded_sha(product, env, got[1])
    else:
        last = next((r for r in runs if r.get('conclusion') == 'success'), {})
        f['deployed'], f['at'] = last.get('headSha'), last.get('updatedAt')
        f['age'] = _age(f['at'], now)
    live = next((r for r in runs if r.get('status') != 'completed'), None)
    if live:
        f['running'] = (live.get('databaseId'), live.get('headSha'))
    _set_behind(product, f, sh)
    if f['mode'] == 'ci' or f['relevant'] == 0:  # observed only, or nothing relevant to deploy
        return _done(f)
    if env != 'dev' and source(product, env) == 'dev':
        if not env_applies(product, 'dev'):
            f['why'] = f'{key(env)}.from is dev, but dev is not managed (deploy_sha.dev.mode)'
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
    """A sha as the line prints it; never a bare ``?`` or ``None`` inside a sentence."""
    return f'`{sha[:9]}`' if sha else 'sha unknown'


def _n(count, rel=''):
    """``N [relevant ]commits``, or ``an unknown number of …`` when the count did not read."""
    return f"{count if count is not None else 'an unknown number of'} {rel}commits"


def _head(env):
    return 'deploy:' if env == 'prod' else f'deploy {env}:'


def _lag(product, f, env):
    """How far ``env`` is behind the trunk: the named targets say the relevant count (the
    commits touching their ``paths``) and their mode; dev and prod keep their plain count."""
    trunk, age = product.main, (f"deployed {f['age']} ago" if f['age'] else None)
    if not f['deployed']:  # no sha: nothing to count behind — say so, never "None commits"
        extra = [age, f"mode {f.get('mode')}" if is_target(env) else None]
        tail = '; '.join(x for x in extra if x)
        return (f"{env} sha unknown — how far behind {trunk} is unknown"
                + (f" ({tail})" if tail else '')
                + (f"; `asf deploy record {env} <sha>` names it" if is_target(env) else ''))
    if not is_target(env):
        return (f"{trunk} is {_n(f['behind'])} ahead of {env} {_s(f['deployed'])}"
                + (f" ({age})" if age else ''))
    rel = 'relevant ' if f['paths'] else ''
    extra = [f"{f['behind']} in all" if f['paths'] and f['behind'] is not None else None, age,
             f"mode {f.get('mode')}"]
    return (f"{env} {_s(f['deployed'])} is {_n(f['relevant'], rel)} behind {trunk}"
            f" ({'; '.join(x for x in extra if x)})")


def _manual(product, f, env):
    """The loud MANUAL clause: the sha waiting on a hand dispatch (a hand deploy, for a target
    with no workflow)."""
    wf, k = f['workflow'], key(env)
    if not is_target(env):
        return (f"MANUAL: green {_s(f['candidate'])} waits on a hand dispatch of {wf}"
                f" ({k}.mode: {f.get('mode')})")
    rel = 'relevant ' if f['paths'] else ''
    how = f'a hand dispatch of {wf}' if wf else f'a hand deploy — no {k}.workflow to dispatch'
    lag = (f"is {_n(f['relevant'], rel)} behind" if f['deployed']
           else 'runs an unknown sha')
    return (f"MANUAL: {env} {lag} — waits on {how}; green"
            f" {_s(f['candidate'])} is the newest candidate ({k}.mode: {f.get('mode')})")


def decide(product, f, env=None):
    """``(dispatch, line)`` from :func:`facts`: whether to dispatch the candidate now, and the one
    line that says what the environment is doing or waiting on."""
    env = env or f.get('env') or 'prod'
    wf, trunk, head = f['workflow'], product.main, _head(env)
    if f['error']:
        return False, f"{head} {f['error']} — {env} state unknown"
    at_main = f['behind'] == 0 or bool(f['main'] and f['main'] == f['deployed'])
    lag = _lag(product, f, env)
    if f.get('mode') == 'ci':
        state = (f"no successful {env} deploy readable on {trunk}" if not f['deployed']
                 else f"{env} {_s(f['deployed'])} is {trunk}" if at_main else lag)
        who = f['ci'] if env == 'dev' else (wf or f['reader'])
        return False, f"{head} {who} deploys {env} on its own (ASF observes) — {state}"
    if at_main:
        return False, f"{head} {env} {_s(f['deployed'])} is {trunk}"
    if f['paths'] and f['relevant'] == 0:
        return False, (f"{head} {env} {_s(f['deployed'])} has every {trunk} commit touching"
                       f" {', '.join(f['paths'])} ({f['behind']} other commits since;"
                       f" mode {f.get('mode')})")
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
    if f.get('mode') != 'auto' or not wf:
        return False, f"{head} {lag} — {_manual(product, f, env)}"
    return True, f"{head} {lag} — dispatching {wf} for green {_s(f['candidate'])}"


def _each(product, sh):
    """[(env, facts)] for every environment that applies — dev, prod, then the named targets;
    the trunk's CI runs are read once and shared."""
    envs = [e for e in names(product) if env_applies(product, e)]
    if not envs:
        return []
    ci = _runs(product, ci_workflow(product), sh, branch=product.main)
    return [(e, facts(product, sh=sh, env=e, _ci=ci) if ci is not None else _unread(product, e))
            for e in envs]


def _unread(product, env):
    f = _blank(product, env)
    f['error'] = f"{f['ci']} runs on {product.main} unreadable (gh run list)"
    return _done(f)


def _read_only(decision):
    """A view's line never says a dispatch happens now: nothing is sent from a view; the tick
    sends it."""
    go, text = decision
    return text.replace(' — dispatching ', ' — the next tick dispatches ', 1) if go else text


def states(product, sh=_sh):
    """[(env, facts)] for every environment that applies, read-only (``asf prod`` reads the
    deployed sha of each target from here)."""
    return _each(product, sh)


def view_line(product, env, f):
    """The read-only line for one environment's facts."""
    return _read_only(decide(product, f, env))


def lines(product, sh=_sh):
    """The read-only lines (no dispatch), one per environment that applies: dev, prod, then the
    named targets."""
    return [view_line(product, e, f) for e, f in _each(product, sh)]


def line(product, sh=_sh):
    """The read-only prod ``deploy:`` line, or None when prod does not apply."""
    if not env_applies(product, 'prod'):
        return None
    return _read_only(decide(product, facts(product, sh=sh, env='prod'), 'prod'))


def dispatch_argv(product, sha, env='prod'):
    argv = ['gh', 'workflow', 'run', workflow(product, env), '-R', product.repo_slug,
            '--ref', product.main]
    name = input_name(product, env)
    if name:
        argv += ['-f', f'{name}={sha}']
    return argv


def marker_refusal(product, sha, env='prod', scan=None):
    """The one loud line refusing a dispatch of ``sha`` to ``env`` when its tree carries a
    forbidden marker under ``conventions.customer_content.paths`` (or cannot be read to tell),
    else None (:func:`asf.customer_content.tree_hits`)."""
    from asf import customer_content as cc
    conv = _conv(product)
    if not cc.paths(conv):
        return None
    hits = (scan or cc.tree_hits)(getattr(product, 'repo_dir', None), sha, conv)
    if hits == []:
        return None
    s = f"`{(sha or '?')[:9]}`"
    if hits is None:
        return (f"{_head(env)} DISPATCH REFUSED — {s}'s tree could not be read to check"
                f" customer_content.paths; not deployed to {env} unchecked")
    return (f"{_head(env)} DISPATCH REFUSED — {s} would ship internal text on customer pages:"
            f" {cc.describe(hits)}; nothing deploys to {env} until a trunk commit removes it")


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
        marked = marker_refusal(product, f['candidate'], e)
        if marked:
            out(marked)
            continue
        if sh(dispatch_argv(product, f['candidate'], e)) is None:
            out(f"{_head(e)} DISPATCH REFUSED — gh workflow run {workflow(product, e)} for"
                f" `{f['candidate'][:9]}` failed; the next tick tries again")
            continue
        sent[e] = f['candidate']
        try:  # the sha source for a target whose provider names none
            record(product, e, f['candidate'], by='asf')
        except OSError:
            pass
    return sent


def cmd_record(args, sh=_sh, out=print):
    """``asf deploy record <target> <sha>``: the deployer names the sha it just deployed, for a
    target whose provider records none (a CLI deploy). The sha is resolved in the product repo
    when it can be; the record stands until a newer deployment shows up without one."""
    from asf import env as env_mod
    product = env_mod.load_product(args.product)
    if args.target not in names(product):
        out(f"deploy record: {args.target!r} is not a deploy target of {product.name}"
            f" ({', '.join(names(product))})")
        return 2
    sha = args.sha
    if getattr(product, 'repo_dir', None):
        sha = sh(['git', '-C', product.repo_dir, 'rev-parse', '--verify', '--quiet',
                  f'{args.sha}^{{commit}}']) or sha
    rec = record(product, args.target, sha, by='hand')
    out(f"deploy record: {args.target} runs `{sha[:9]}` (recorded {rec['at']})")
    return 0


def register(subparsers):
    from asf import env as env_mod
    p = subparsers.add_parser('deploy', help='deploy targets: record what a hand deploy shipped')
    sub = p.add_subparsers(dest='deploy_command', required=True)
    r = sub.add_parser('record', help="record the sha a target now runs (when its provider "
                                      "names none, e.g. a CLI deploy)")
    r.add_argument('target', help='dev, prod or a deploy_sha.targets name')
    r.add_argument('sha', help='the commit deployed (any rev the product repo resolves)')
    env_mod.add_product_arg(r)
    r.set_defaults(run=cmd_record)
    return p
