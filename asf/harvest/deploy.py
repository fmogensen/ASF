"""asf.harvest.deploy — the prod deploy: dispatch ``deploy_sha.workflow`` for a green trunk, or
say loudly why prod waits.

A product whose prod deploy is a ``workflow_dispatch`` workflow (``deploy_sha.workflow``, read as
``conventions.deploy_workflow``) ships only when something dispatches it. Nothing did: the trunk
ran ahead of prod for days while every view printed only ``main is N commits ahead of prod``.
This module is the one place that decides, from facts, what happens to prod each tick:

* the newest completed ``ci.workflow`` run on the trunk that concluded ``success`` is the
  *candidate* — a trunk sha whose whole CI (the dev deploy job included) is green;
* the candidate deploys when it is a descendant of the prod sha (the newest successful
  ``deploy_sha.workflow`` run), no deploy run is queued or running, and no deploy of that very
  sha has already failed (a failed sha is not retried; the next green trunk sha is);
* the dispatch is ``gh workflow run <workflow> -R <slug> --ref <trunk> -f <input>=<sha>``, the
  input ``deploy_sha.input`` (default ``sha``; ``none`` sends no input) — and only when the
  product opts in with ``deploy_sha.auto: true``. Deploying is the operator's standing decision,
  never a default.

Every outcome is one ``deploy:`` line — the tick prints it (:func:`tick`), and ``asf status`` /
``asf prod`` print the read-only half (:func:`line`), so a prod that waits always names what it
waits on: a red trunk, a running deploy, a failed deploy, or ``deploy_sha.auto`` being off.
"""
import datetime
import json
import subprocess

DEFAULT_INPUT = 'sha'


def _sh(cmd, cwd=None, timeout=60):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _json(text):
    try:
        v = json.loads(text) if text else None
    except ValueError:
        return None
    return v if isinstance(v, list) else None


def _cfg(product):
    d = getattr(product, 'deploy_sha', None)
    return d if isinstance(d, dict) else {}


def workflow(product):
    conv = getattr(product, 'conventions', None) or {}
    return conv.get('deploy_workflow') or _cfg(product).get('workflow')


def ci_workflow(product):
    conv = getattr(product, 'conventions', None) or {}
    return conv.get('ci_workflow')


def auto(product):
    return _cfg(product).get('auto') is True


def input_name(product):
    v = _cfg(product).get('input', DEFAULT_INPUT)
    return None if v in (None, '', 'none') else str(v)


def applies(product):
    """True when the product names a deploy workflow, a CI workflow and a hosted repo: the only
    shape this module can reason about."""
    return bool(workflow(product) and ci_workflow(product) and getattr(product, 'repo_slug', None)
                and getattr(product, 'repo_dir', None))


def _age(iso, now):
    try:
        t = datetime.datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None
    h = (now - t).total_seconds() / 3600
    return f'{h / 24:.1f}d' if h >= 48 else f'{h:.0f}h'


def facts(product, sh=_sh, now=None):
    """The deploy facts, read-only: ``prod``, ``running`` (a deploy run not yet completed),
    ``failed`` (the candidate's own failed deploy run id), ``candidate``, ``main``, ``behind``,
    ``age`` (how long ago prod was deployed) and ``error`` (what could not be read)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    slug, repo, trunk = product.repo_slug, product.repo_dir, product.main
    f = {'workflow': workflow(product), 'ci': ci_workflow(product), 'prod': None, 'running': None,
         'failed': None, 'candidate': None, 'main': None, 'behind': None, 'age': None,
         'error': None}
    runs = _json(sh(['gh', 'run', 'list', '-R', slug, '--workflow', f['workflow'], '--limit', '20',
                     '--json', 'databaseId,headSha,status,conclusion,updatedAt']))
    if runs is None:
        f['error'] = f"{f['workflow']} runs unreadable (gh run list)"
        return f
    last = next((r for r in runs if r.get('conclusion') == 'success'), {})
    f['prod'], f['age'] = last.get('headSha'), _age(last.get('updatedAt'), now)
    live = next((r for r in runs if r.get('status') != 'completed'), None)
    if live:
        f['running'] = (live.get('databaseId'), live.get('headSha'))
    ci = _json(sh(['gh', 'run', 'list', '-R', slug, '--workflow', f['ci'], '--branch', trunk,
                   '--limit', '20', '--json', 'headSha,status,conclusion']))
    if ci is None:
        f['error'] = f"{f['ci']} runs on {trunk} unreadable (gh run list)"
        return f
    green = next((r.get('headSha') for r in ci
                  if r.get('status') == 'completed' and r.get('conclusion') == 'success'), None)
    f['main'] = sh(['git', '-C', repo, 'rev-parse', f'origin/{trunk}'])
    if f['prod']:
        count = sh(['git', '-C', repo, 'rev-list', '--count', f"{f['prod']}..origin/{trunk}"])
        f['behind'] = int(count) if count and count.isdigit() else None
    if green and green != f['prod']:
        ahead = f['prod'] is None or sh(
            ['git', '-C', repo, 'merge-base', '--is-ancestor', f['prod'], green]) is not None
        if ahead:
            f['candidate'] = green
            f['failed'] = next((r.get('databaseId') for r in runs if r.get('headSha') == green
                                and r.get('status') == 'completed'
                                and r.get('conclusion') not in ('success', None)), None)
    return f


def decide(product, f):
    """``(dispatch, line)`` from :func:`facts`: whether to dispatch the candidate now, and the one
    ``deploy:`` line that says what prod is doing or waiting on."""
    wf, trunk = f['workflow'], product.main

    def s(sha):
        return f'`{(sha or "?")[:9]}`'
    if f['error']:
        return False, f"deploy: {f['error']} — prod state unknown"
    lag = (f"{trunk} is {f['behind']} commits ahead of prod {s(f['prod'])}"
           + (f" (deployed {f['age']} ago)" if f['age'] else ''))
    if f['behind'] == 0 or (f['main'] and f['main'] == f['prod']):
        return False, f"deploy: prod {s(f['prod'])} is {trunk}"
    if f['running']:
        rid, sha = f['running']
        return False, f"deploy: {wf} run {rid} for {s(sha)} is queued or running — {lag}"
    if not f['candidate']:
        return False, (f"deploy: {lag} — no green {f['ci']} run on {trunk} newer than prod;"
                       f" prod waits on a green {trunk}")
    if f['failed']:
        return False, (f"deploy: {wf} for {s(f['candidate'])} FAILED (run {f['failed']}) — not"
                       f" retried; {lag}; the next green {trunk} sha dispatches again")
    if not auto(product):
        return False, (f"deploy: {lag} — green {s(f['candidate'])} would deploy, but nothing"
                       f" dispatches {wf}: deploy_sha.auto is off, so prod waits on a hand dispatch")
    return True, f"deploy: {lag} — dispatching {wf} for green {s(f['candidate'])}"


def line(product, sh=_sh):
    """The read-only ``deploy:`` line (no dispatch), or None when the module does not apply."""
    if not applies(product):
        return None
    return decide(product, facts(product, sh=sh))[1]


def dispatch_argv(product, sha):
    argv = ['gh', 'workflow', 'run', workflow(product), '-R', product.repo_slug,
            '--ref', product.main]
    name = input_name(product)
    if name:
        argv += ['-f', f'{name}={sha}']
    return argv


def tick(product, out=print, sh=_sh):
    """The tick's deploy pass: print the ``deploy:`` line and, when :func:`decide` says so,
    dispatch the deploy workflow. A refused dispatch is one loud line, and the next tick tries
    again. Returns the dispatched sha, or None."""
    if not applies(product):
        return None
    f = facts(product, sh=sh)
    go, text = decide(product, f)
    out(text)
    if not go:
        return None
    if sh(dispatch_argv(product, f['candidate'])) is None:
        out(f"deploy: DISPATCH REFUSED — gh workflow run {workflow(product)} for"
            f" `{f['candidate'][:9]}` failed; the next tick tries again")
        return None
    return f['candidate']
