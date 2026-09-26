"""asf.workers.actions — the cloud lane's ``actions`` runtime: a worker session as a CI job.

The product carries one workflow ASF ships (:func:`render_workflow`; ``asf cloud install
--product <p>`` writes it into the product checkout and prints what to commit — it pushes
nothing). It is ``workflow_dispatch`` with the inputs ``job``, ``branch``, ``base``, ``model``,
``brief_ref`` and ``run_name``, runs on ``cloud.runs_on`` (the product's self-hosted runners, or
``ubuntu-latest``), checks the branch out, installs the runtime CLI, and runs ``claude -p`` with
the brief. The runtime CLI authenticates from the repo secret ``cloud.token_secret``.

A launch (:class:`ActionsRuntime`):

1. The brief goes to origin as a tree — ``brief.md``, ``env`` (the worker environment: the caps,
   ``ASF_SESSION``, ``BACKLOG_ID_RANGE``) and ``setup`` (the product's worktree setup command) —
   under ``refs/asf/briefs/<job>``, by a plain ``git push`` (:func:`push_brief`).
2. ``gh workflow run <workflow> -R <slug> --ref <trunk> -f …`` dispatches it.
3. The run id is read off ``gh run list --workflow <workflow> --json …``, matched on the run's
   title — the unique ``run_name`` input; the run records ``pid: actions:<run id>``.

Health reads ``gh run view <id> --json status,conclusion`` and the report commit on the branch
(:func:`asf.workers.cloud.sync`); a run past ``cloud.timeout_min`` gets ``gh run cancel``. Doctor
checks the workflow is on the default branch, the secret is set (``gh secret list``: names only)
and, for self-hosted labels, that a runner carrying them is online.
"""
import json
import os
import re
import subprocess
import time

from asf import env
from asf.workers import cloud
from asf.workers import cloudpid
from asf.workers import runtime as runtime_mod

WORKFLOW_DIR = os.path.join('.github', 'workflows')
BRIEF_REF_PREFIX = 'refs/asf/briefs/'
GH_TIMEOUT_S = 60
#: labels of GitHub's own hosted runners: no runner of the product's own needs to be online
HOSTED_RE = re.compile(r'^(ubuntu|windows|macos)-', re.I)
_ENV_NAME_RE = re.compile(r'^[A-Z_][A-Z0-9_]*$')
#: variables a job may not set through its environment file
_ENV_SKIP_RE = re.compile(r'^(GITHUB_|RUNNER_|NODE_OPTIONS$)')

WORKFLOW = r"""# asf-worker — one ASF worker session as a CI job, off the factory host.
# Written by `asf cloud install`; ASF dispatches it (cloud.runtime: actions). ASF pushes the brief
# to the ref `brief_ref` before the dispatch; the session commits and pushes `branch`, its last
# commit carrying the REPORT and an ASF-Report trailer. Reinstall after changing cloud.runs_on,
# cloud.token_secret or cloud.timeout_min.
name: asf-worker
run-name: ${{ inputs.run_name || inputs.job }}

on:
  workflow_dispatch:
    inputs:
      job: {description: 'the ASF job', required: true, type: string}
      branch: {description: 'the branch the session works on', required: true, type: string}
      base: {description: 'the trunk the branch starts from', required: true, type: string}
      model: {description: 'the model id', required: false, type: string}
      brief_ref: {description: 'the ref that holds the brief', required: true, type: string}
      run_name: {description: 'the unique run title ASF finds the run by', required: false, type: string}

permissions:
  contents: write

jobs:
  session:
    runs-on: @RUNS_ON@
    timeout-minutes: @TIMEOUT@
    env:
      @SECRET@: ${{ secrets.@SECRET@ }}
      ASF_JOB: ${{ inputs.job }}
      ASF_BRANCH: ${{ inputs.branch }}
      ASF_BASE: ${{ inputs.base }}
      ASF_MODEL: ${{ inputs.model }}
      ASF_BRIEF_REF: ${{ inputs.brief_ref }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ inputs.branch }}
          fetch-depth: 0
      - name: brief
        run: |
          set -euo pipefail
          git fetch --no-tags -q origin "+$ASF_BRIEF_REF:refs/asf/brief"
          mkdir -p "$RUNNER_TEMP/asf"
          git cat-file -p refs/asf/brief:brief.md > "$RUNNER_TEMP/asf/brief.md"
          git cat-file -p refs/asf/brief:setup > "$RUNNER_TEMP/asf/setup" 2>/dev/null || true
          { git cat-file -p refs/asf/brief:env 2>/dev/null || true; } \
            | grep -E '^[A-Z_][A-Z0-9_]*=' | grep -vE '^(GITHUB_|RUNNER_|NODE_OPTIONS=)' \
            >> "$GITHUB_ENV" || true
          git config user.name "asf worker"
          git config user.email "asf-worker@users.noreply.github.com"
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - name: runtime
        run: npm i -g @anthropic-ai/claude-code
      - name: setup
        run: if [ -s "$RUNNER_TEMP/asf/setup" ]; then bash -eo pipefail "$RUNNER_TEMP/asf/setup"; fi
      - name: session
        run: |
          set -o pipefail
          args=(-p --permission-mode bypassPermissions --output-format json)
          if [ -n "$ASF_MODEL" ]; then args+=(--model "$ASF_MODEL"); fi
          claude "${args[@]}" < "$RUNNER_TEMP/asf/brief.md" | tee "$RUNNER_TEMP/asf/result.json"
      - name: push what the session left
        if: always()
        run: |
          git fetch -q origin "$ASF_BRANCH" || true
          if [ -n "$(git rev-list "origin/$ASF_BRANCH..HEAD" 2>/dev/null)" ]; then
            git push origin "HEAD:refs/heads/$ASF_BRANCH"
          fi
"""


# ---- the workflow template --------------------------------------------------------------------

def _runs_on(labels):
    labels = list(labels) or list(cloud.DEFAULT_RUNS_ON)
    return labels[0] if len(labels) == 1 else json.dumps(labels)


def render_workflow(s):
    """The workflow file for settings ``s`` (golden-tested): the runs-on labels, the timeout (ten
    minutes past ``cloud.timeout_min``, so ASF's cancel comes first) and the token secret."""
    return (WORKFLOW.replace('@RUNS_ON@', _runs_on(s.runs_on))
            .replace('@TIMEOUT@', str(int(s.timeout_min) + 10))
            .replace('@SECRET@', s.token_secret))


def workflow_path(s):
    return os.path.join(WORKFLOW_DIR, s.workflow)


def install(product, s, out=print):
    """Write the workflow into the product checkout and print what to commit. Pushes nothing."""
    repo = getattr(product, 'repo_dir', None)
    if not repo or not os.path.isdir(repo):
        raise env.ConfigError(f'{product.name}: repo_dir {repo!r} is not a checkout')
    rel = workflow_path(s)
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = render_workflow(s)
    try:
        with open(path, encoding='utf-8') as f:
            same = f.read() == text
    except OSError:
        same = False
    if not same:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    out(f"cloud install: {'unchanged' if same else 'wrote'} {path}")
    out(f'commit it to {product.main} (the workflow must be on the default branch to dispatch):')
    out(f'  git -C {repo} add {rel}')
    out(f'  git -C {repo} commit -m "ci: the asf-worker workflow (the ASF cloud lane)"')
    out(f'  git -C {repo} push origin {product.main}')
    out(f'the repo secret {s.token_secret} must hold the cloud-lane account\'s own token '
        f'(repo {product.repo_slug or "?"}, Settings → Secrets → Actions).')
    return path


def cmd_install(args):
    product = env.load_product(args.product)
    s = cloud.settings(env.load_config(), product)
    install(product, s)
    return 0


def register(subparsers):
    p = subparsers.add_parser('cloud', help='the cloud lane: its CI workflow')
    sub = p.add_subparsers(dest='cloud_command', required=True)
    i = sub.add_parser('install', help='write the asf-worker workflow into the product checkout '
                                       'and print what to commit (pushes nothing)')
    env.add_product_arg(i)
    i.set_defaults(run=cmd_install)
    d = sub.add_parser('doctor', help='is the cloud lane ready: one ok/gap line per check '
                                      '(on/default, secret name, workflow on the trunk, an online '
                                      'runner, accounts)')
    env.add_product_arg(d)
    d.set_defaults(run=cmd_doctor)
    return p


def cmd_doctor(args):
    product = env.load_product(args.product)
    return cloud.doctor(env.load_config(), product)


# ---- gh ---------------------------------------------------------------------------------------

class Gh:
    """The ``gh`` calls the lane makes for one product; ``run`` is injectable (tests)."""

    def __init__(self, product, run=None):
        self.product = product
        self.slug = getattr(product, 'repo_slug', None)
        self._run = run or subprocess.run
        self._env = None

    def call(self, args):
        """``(ok, stdout, last error line)``."""
        if self._env is None:
            from asf import ci_pool
            self._env = ci_pool._gh_env(self.product)
        try:
            p = self._run(['gh', *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S,
                          env=self._env)
        except (OSError, subprocess.SubprocessError) as e:
            return False, '', f'gh {args[0]}: {type(e).__name__}'
        lines = (p.stderr or p.stdout or '').strip().splitlines()
        return p.returncode == 0, p.stdout or '', (lines[-1] if lines else f'exit {p.returncode}')

    def dispatch(self, workflow, ref, fields):
        args = ['workflow', 'run', workflow, '-R', self.slug, '--ref', ref]
        for k, v in fields.items():
            args += ['-f', f'{k}={v}']
        ok, _out, err = self.call(args)
        return ok, err

    def find_run(self, workflow, run_name):
        """``{id, url, status}`` of the dispatched run titled ``run_name``, else None."""
        ok, out, _err = self.call(['run', 'list', '-R', self.slug, '--workflow', workflow,
                                   '--event', 'workflow_dispatch', '-L', '50', '--json',
                                   'databaseId,displayTitle,status,url'])
        if not ok:
            return None
        try:
            runs = json.loads(out or '[]')
        except ValueError:
            return None
        for r in runs if isinstance(runs, list) else ():
            if r.get('displayTitle') == run_name:
                return {'id': str(r.get('databaseId')), 'url': r.get('url'),
                        'status': r.get('status')}
        return None

    def view(self, run_id):
        """``{status, conclusion}`` of the run, or None when gh cannot say."""
        ok, out, _err = self.call(['run', 'view', str(run_id), '-R', self.slug, '--json',
                                   'status,conclusion'])
        if not ok:
            return None
        try:
            v = json.loads(out)
        except ValueError:
            return None
        return v if isinstance(v, dict) else None

    def cancel(self, run_id):
        ok, _out, err = self.call(['run', 'cancel', str(run_id), '-R', self.slug])
        return ok, f'run {run_id} cancelled' if ok else f'run {run_id}: cancel refused ({err})'

    def workflow_on(self, s, ref):
        ok, _out, err = self.call(['api', f'repos/{self.slug}/contents/'
                                          f'{workflow_path(s).replace(os.sep, "/")}?ref={ref}',
                                   '--jq', '.path'])
        return ok, err

    def secret_names(self):
        """The repo's secret names, or None when gh cannot list them."""
        ok, out, _err = self.call(['secret', 'list', '-R', self.slug, '--json', 'name'])
        if not ok:
            return None
        try:
            return {r.get('name') for r in json.loads(out or '[]')}
        except (ValueError, AttributeError):
            return None


# ---- the brief ref ----------------------------------------------------------------------------

def _git(args, cwd, stdin=None):
    return subprocess.run(['git', *args], cwd=cwd, input=stdin, capture_output=True, text=True)


def brief_ref(job_name):
    return f'{BRIEF_REF_PREFIX}{job_name}'


def env_file(job_env):
    """The worker environment as ``NAME=value`` lines — names a job may set, one-line values."""
    return ''.join(f'{k}={v}\n' for k, v in sorted((job_env or {}).items())
                   if _ENV_NAME_RE.match(str(k)) and not _ENV_SKIP_RE.match(str(k))
                   and '\n' not in str(v))


def push_brief(worktree, job_name, brief_text, job_env, setup=None):
    """Push a tree of ``brief.md``, ``env`` and ``setup`` to origin's ``refs/asf/briefs/<job>``
    (a plain push, the ref replaced). ``(ok, ref or error)``."""
    files = {'brief.md': brief_text, 'env': env_file(job_env)}
    if setup:
        files['setup'] = str(setup).rstrip('\n') + '\n'
    entries = []
    for name in sorted(files):
        p = _git(['hash-object', '-w', '--stdin'], worktree, stdin=files[name])
        if p.returncode != 0:
            return False, f'brief: git hash-object failed ({p.stderr.strip()})'
        entries.append(f'100644 blob {p.stdout.strip()}\t{name}\n')
    p = _git(['mktree'], worktree, stdin=''.join(entries))
    if p.returncode != 0:
        return False, f'brief: git mktree failed ({p.stderr.strip()})'
    ref = brief_ref(job_name)
    p = _git(['push', '-q', '--no-verify', 'origin', f'+{p.stdout.strip()}:{ref}'], worktree)
    if p.returncode != 0:
        lines = (p.stderr or '').strip().splitlines()
        return False, f"brief: push to {ref} refused ({lines[-1] if lines else 'failed'})"
    return True, ref


def delete_brief(worktree, ref):
    """Delete a finished run's brief ref on origin: nothing of a run outlives it there."""
    if not worktree or not os.path.isdir(worktree) or not str(ref).startswith(BRIEF_REF_PREFIX):
        return False
    return _git(['push', '-q', '--no-verify', 'origin', f':{ref}'], worktree).returncode == 0


# ---- the runtime ------------------------------------------------------------------------------

class ActionsRuntime(runtime_mod.Runtime):
    """``actions``: push the brief, dispatch the workflow, find the run. ``run`` returns a
    :class:`~asf.workers.runtime.Result` whose ``pid`` is the run's token."""
    name = cloud.RUNTIME_ACTIONS
    lane = 'cloud'

    def __init__(self, s, product, gh=None, sleep=time.sleep, clock=time.monotonic):
        self.settings = s
        self.product = product
        self.gh = gh or Gh(product)
        self._sleep = sleep
        self._clock = clock

    def run(self, job, wait=False):
        from asf.workers.spawn import SpawnError  # local: spawn imports the runtimes
        s, product = self.settings, self.product
        if not self.gh.slug:
            raise SpawnError('cloud lane: the product has no repo_slug to dispatch on')
        log_path = job.log_path or runtime_mod.job_log_path(job.product, job.name)
        with open(job.brief_path, encoding='utf-8') as f:
            text = cloud.cloud_brief(f.read(), job)
        ok, ref = push_brief(job.cwd, job.name, text, job.env, getattr(job, 'setup', None))
        if not ok:
            raise SpawnError(f'cloud lane: {ref}')
        run_name = f'asf {job.name} {job.session or ""}'.strip()
        fields = {'job': job.name, 'branch': job.branch, 'base': job.base or product.main,
                  'model': job.model or '', 'brief_ref': ref, 'run_name': run_name}
        ok, err = self.gh.dispatch(s.workflow, product.main, fields)
        if not ok:
            delete_brief(job.cwd, ref)
            raise SpawnError(f'cloud lane: gh workflow run {s.workflow} refused ({err})')
        hit = self.gh.find_run(s.workflow, run_name)
        deadline = self._clock() + max(0.0, s.launch_wait_s)
        while hit is None and self._clock() < deadline:
            self._sleep(2.0)
            hit = self.gh.find_run(s.workflow, run_name)
        run_id = hit['id'] if hit else None
        tok = cloudpid.token(run_id or job.session or job.name)
        line = runtime_mod._session_line(job)
        with open(log_path, 'a', encoding='utf-8') as log:
            if line is not None:
                log.write(line + '\n')
            log.write(json.dumps({'type': 'asf', 'subtype': 'cloud', 'runtime': self.name,
                                  'dispatched': s.workflow, 'run': run_id, 'run_name': run_name,
                                  'brief_ref': ref}) + '\n')
        cloudpid.record(tok, cloud.WORKING, 'dispatched', run=run_id,
                        url=(hit or {}).get('url'))
        result = runtime_mod.Result(pid=tok, log_path=log_path)
        result.extra = {'lane': 'cloud', 'cloud_runtime': self.name, 'actions_run_id': run_id,
                        'actions_run_name': run_name, 'cloud_url': (hit or {}).get('url'),
                        'brief_ref': ref}
        return result

    def continue_run(self, job, wait=False):
        return None  # a CI job is not resumed from here: the caller falls back


# ---- doctor -----------------------------------------------------------------------------------

def checks(s, product, run_cmd=None):
    """``[(name, required, ok, detail)]``, one per check, passing or not: the repo, the workflow
    on the default branch, the secret (``gh secret list``: names only, never values), and an
    online runner for self-hosted labels."""
    gh = Gh(product, run=run_cmd)
    if not gh.slug:
        return [('repo', True, False, 'the product has no repo_slug: the lane dispatches a '
                                      'workflow of the product repo')]
    rows = []
    ok, err = gh.workflow_on(s, product.main)
    if ok:
        rows.append(('workflow', True, True, f'{workflow_path(s)} on {product.main} of {gh.slug}'))
    else:
        rows.append(('workflow', True, False,
                     f'{workflow_path(s)} is not on {product.main} of {gh.slug} ({err}): '
                     f'`asf cloud install --product {product.name}`, then commit and push it'))
    names = gh.secret_names()
    if names is None:
        rows.append(('secret', False, False, f'cannot list the secrets of {gh.slug}: is repo '
                                             f'secret {s.token_secret} set?'))
    elif s.token_secret not in names:
        rows.append(('secret', True, False, f'repo secret {s.token_secret} is missing on '
                                            f'{gh.slug}: the job has no token for the runtime CLI'))
    else:
        rows.append(('secret', True, True, f'repo secret {s.token_secret} is set on {gh.slug}'))
    labels = [l for l in s.runs_on if l]
    if all(HOSTED_RE.match(l) for l in labels):
        rows.append(('runner', True, True, f'runs_on [{", ".join(labels)}]: GitHub-hosted, no '
                                           'runner of the product needed'))
    else:
        from asf import ci_pool
        try:
            runners = ci_pool.GitHubBackend(product, run=run_cmd).runners()
        except ci_pool.BackendError as e:
            rows.append(('runner', False, False, f'cannot list runners: {e}'))
        else:
            want = {l.lower() for l in labels}
            hits = sorted(r.name for r in runners if r.online and want <= r.norm_labels())
            if hits:
                rows.append(('runner', True, True, f'online runner {", ".join(hits)} carries '
                                                   f'[{", ".join(labels)}]'))
            else:
                rows.append(('runner', True, False,
                             f'no online runner carries [{", ".join(labels)}]'))
    return rows


def doctor_rows(s, product, run_cmd=None):
    """``[(required, ok, detail)]``: the failing :func:`checks`."""
    return [(req, ok, d) for _n, req, ok, d in checks(s, product, run_cmd=run_cmd) if not ok]
