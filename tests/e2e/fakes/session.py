"""The ScriptedRuntime: a worker session played from a script, in the job's real worktree.

It is plugged in where ``config.yaml worker_pool.backend`` would pick the runtime
(:func:`asf.workers.runtime.from_config`, patched by the harness for the length of a tick). A
launch runs its step at once, synchronously, the way a session would have by the next tick:

1. the step's ``writes`` (``{path: text}``) into the job's worktree;
2. ``git add -A`` and ``git commit -m <commit>`` under the job's own environment
   (:func:`asf.workers.runtime.build_env`), the ``ASF-Session`` trailer the session's hook dir
   would stamp written by ``--trailer`` instead — the hook shims (``reference-transaction`` runs
   on every ref update) cost more than the rest of the tick, and the product's own hooks here
   are the harness's ``asf`` stub anyway;
3. ``git push origin <branch>`` unless ``push`` is false (hooks off, as above);
4. the job log as the real runtime writes it: the ``asf``/``session`` line, a ``system``/``init``
   line and a ``result`` line whose text ends with the typed REPORT
   (:mod:`asf.workers.report`) — built from the step unless it carries its own ``report``.

A step is a dict (all keys optional)::

    {"writes": {"specs/{item_lower}.md": "..."}, "commit": "spec({item}): ...",
     "push": true, "status": "done", "needs_writes": "none", "ok": true,
     "running": false, "hooks": false, "report": "<the whole result text>"}

(``hooks: true`` pushes through the session's hook dir and so through the product repo's own
``pre-push``; a refused push is reported as ``pushed: no — the push was refused: <its output>``.)

Every string is formatted with the job's facts: ``item`` (``F-0001``), ``item_lower``,
``branch``, ``kind`` (the brief kind), ``spec_path``, ``plan_path``, ``review_path`` and
``round`` read off the brief, ``w0``..``w3`` (the Task's ``writes:`` paths, off a coder's brief),
``job``. The script is ``{kind: step | [step, ...]}`` — a list is
played in order, its last step repeating. :meth:`ScriptedRuntime.queue` puts a step for one
job (or one kind) ahead of the script, for a scenario that needs one session to act differently.
"""
import json
import os
import re
import subprocess

from asf.workers import runtime as runtime_mod

JOB_RE = re.compile(r'^(?P<kind>.+)-(?P<item>[a-z]+-\d{4})(?:-correction)?$')
DELIVERABLE_RE = re.compile(r'DELIVERABLE:\s*`([^`]+)`')
REVIEW_PATH_RE = re.compile(r'Write\s+`([^`]+)`')
ROUND_RE = re.compile(r'round\s+(\d+)', re.I)
WRITES_RE = re.compile(r'THE BOUNDARY IS `writes:` — (.+)$', re.M)


def load_scripts(directory):
    """``{kind: step | [steps]}`` from every ``<kind>.json`` in ``directory``."""
    out = {}
    for name in sorted(os.listdir(directory)):
        if name.endswith('.json'):
            with open(os.path.join(directory, name), encoding='utf-8') as f:
                out[name[:-len('.json')]] = json.load(f)
    return out


def _fmt(value, facts):
    if isinstance(value, str):
        return value.format(**facts)
    if isinstance(value, dict):
        return {_fmt(k, facts): _fmt(v, facts) for k, v in value.items()}
    if isinstance(value, list):
        return [_fmt(v, facts) for v in value]
    return value


def _dead_pid():
    """A pid that has already exited: the session is over by the time the tick looks."""
    p = subprocess.Popen(['true'])
    p.wait()
    return p.pid


class ScriptedRuntime(runtime_mod.Runtime):
    name = 'e2e'

    def __init__(self, scripts):
        self.scripts = scripts
        self.cursor = {}
        self.queued = []   # [(job or kind, step)], first match wins
        self.calls = []    # [{job, kind, item, branch, brief, step}], in launch order
        self.push_error = ''

    def queue(self, key, step):
        """Play ``step`` for the next launch whose job or kind is ``key``, instead of the script."""
        self.queued.append((key, step))

    def _step(self, job_name, kind):
        for i, (key, step) in enumerate(self.queued):
            if key in (job_name, kind):
                del self.queued[i]
                return step
        script = self.scripts.get(kind)
        if script is None:
            return {'push': False, 'status': 'blocked',
                    'report_note': f'no script for the brief kind {kind}'}
        if isinstance(script, dict):
            return script
        n = self.cursor.get(kind, 0)
        self.cursor[kind] = n + 1
        return script[min(n, len(script) - 1)]

    def run(self, job, wait=False):
        with open(job.brief_path, encoding='utf-8') as f:
            brief = f.read()
        m = JOB_RE.match(job.name)
        kind = m.group('kind') if m else job.name
        item = m.group('item').upper() if m else ''
        env = runtime_mod.build_env(job)
        branch = self._git(['rev-parse', '--abbrev-ref', 'HEAD'], job.cwd, env).stdout.strip()
        facts = {'item': item, 'item_lower': item.lower(), 'branch': branch, 'kind': kind,
                 'job': job.name, 'spec_path': '', 'plan_path': '', 'review_path': '',
                 'round': ''}
        d = DELIVERABLE_RE.search(brief)
        if d and kind == 'spec':
            facts['spec_path'] = d.group(1)
        if d and kind == 'plan':
            facts['plan_path'] = d.group(1)
        r = REVIEW_PATH_RE.search(brief)
        if r:
            facts['review_path'] = r.group(1)
        rnd = ROUND_RE.search(brief)
        facts['round'] = rnd.group(1) if rnd else '1'
        w = WRITES_RE.search(brief)
        paths = [p.strip() for p in w.group(1).split(',')] if w else []
        for i in range(4):
            facts[f'w{i}'] = paths[i] if i < len(paths) else f'src/{item.lower()}_{i}.py'
        step = _fmt(self._step(job.name, kind), facts)
        self.calls.append({'job': job.name, 'kind': kind, 'item': item, 'branch': branch,
                           'brief': brief, 'step': step, 'cwd': job.cwd})
        sha, pushed = self._play(step, job, env, branch)
        text = step.get('report') or self._report(step, facts, sha, pushed)
        return self._log(job, step, text)

    # ---- the session's own work --------------------------------------------------------------

    @staticmethod
    def _git(args, cwd, env, check=False):
        p = subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} in {cwd}: {p.stderr.strip()}")
        return p

    def _play(self, step, job, env, branch):
        for rel, text in (step.get('writes') or {}).items():
            path = os.path.join(job.cwd, rel)
            os.makedirs(os.path.dirname(path) or job.cwd, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        quiet = ['-c', 'core.hooksPath=/dev/null']
        if step.get('commit'):
            self._git(['add', '-A'], job.cwd, env, check=True)
            trailer = [f'--trailer=ASF-Session: {job.session}'] if job.session else []
            self._git([*quiet, 'commit', '-q', '-m', step['commit'], *trailer], job.cwd, env,
                      check=True)
        sha = self._git(['rev-parse', 'HEAD'], job.cwd, env).stdout.strip()
        pushed = False
        self.push_error = ''
        if step.get('push', True) and step.get('commit'):
            # `hooks: true` pushes through the session's hook dir, which chains to the product
            # repo's own pre-push — the slow path, for a scenario about a refused push
            p = self._git([*([] if step.get('hooks') else quiet), 'push', '-q', 'origin',
                           f'HEAD:refs/heads/{branch}'], job.cwd, env)
            pushed = p.returncode == 0
            self.push_error = p.stderr.strip()
        return sha, pushed

    def _report(self, step, facts, sha, pushed):
        if pushed:
            pushed_line = f'yes {sha}'
        elif step.get('commit') and step.get('push', True):  # the push was refused: say why
            pushed_line = 'no — the push was refused: ' + ' '.join(self.push_error.split())
        else:
            pushed_line = 'no — the script does not push'
        commits = f"{sha} {step['commit']}" if step.get('commit') else 'none'
        lines = [step.get('summary') or f"{facts['kind']} {facts['item']}: scripted session.",
                 '', 'REPORT', f"item: {facts['item']}", f"kind: {facts['kind']}",
                 f"status: {step.get('status', 'done')}", f"branch: {facts['branch']}",
                 f'pushed: {pushed_line}', f'commits: {commits}',
                 f"tests: {step.get('tests', 'python3 -m unittest discover -s tests — OK')}",
                 f"left out: {step.get('left_out', 'none')}",
                 f"needs writes: {step.get('needs_writes', 'none')}"]
        if step.get('report_note'):
            lines.insert(0, step['report_note'])
        return '\n'.join(lines) + '\n'

    def _log(self, job, step, text):
        log_path = job.log_path or runtime_mod.job_log_path(job.product, job.name)
        # a finished session's pid is gone; one still `running` is this process, which lives
        # for as long as the scenario does
        pid = os.getpid() if step.get('running') else _dead_pid()
        with open(log_path, 'a', encoding='utf-8') as log:
            line = None if job.resume else runtime_mod._session_line(job)
            if line is not None:
                log.write(line + '\n')
            log.write(json.dumps({'type': 'system', 'subtype': 'init', 'model': job.model,
                                  'session_id': f'e2e:{job.name}:{len(self.calls)}'}) + '\n')
            if step.get('running'):
                return runtime_mod.Result(pid=pid, log_path=log_path)
            ok = bool(step.get('ok', True))
            log.write(json.dumps({'type': 'result', 'subtype': 'success' if ok else 'error',
                                  'is_error': not ok, 'result': text}) + '\n')
        return runtime_mod.Result(ok=ok, pid=pid, returncode=0 if ok else 1, text=text,
                                  log_path=log_path)

    def continue_run(self, job, wait=False):
        return self.run(job, wait=wait)
