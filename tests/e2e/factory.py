"""The end-to-end lane harness's driver: one sample product, a fake PR host, scripted sessions,
and the real tick — in process.

``Factory(tmp, landing=...).setup()`` lays the product down under ``tmp``:

* ``origins/repo.git`` and ``origins/record.git`` — bare origins seeded from
  ``tests/e2e/product/repo`` and ``.../record`` (plus a stage overlay, ``stages/<stage>/``, for a
  scenario that starts later than a card), with the checkouts the product yaml names;
* ``home/`` — the operator home (``ASF_HOME``): ``products/sample.yaml`` (``landing:`` filled
  in) and ``config.yaml``;
* ``bin/`` — first on ``PATH``: the fake ``gh`` (``tests/e2e/fakes/gh``) over
  ``bin/gh-state.json``, and an instant ``asf`` stub for the git hooks the factory installs.

:meth:`Factory.tick` runs the real ``asf tick`` (:func:`asf.tick.tick.cmd_tick`, every step) in
this process, with three seams and no others: the worker runtime is the
:class:`~fakes.session.ScriptedRuntime` (``worker_pool.backend``'s pick, patched), the harvest
step's background process is run inline (:func:`asf.tick.step_harvest.background`, the very
function the detached process runs), and the feeder's rows are recorded as the wave plans them.
Caches the factory keeps for minutes (the evidence's PR list, PR hygiene's) are aged between
ticks, as the minutes between two real ticks would. :meth:`Factory.gh` moves the forge the way
CI or a person would; :meth:`Factory.snapshot` reads the ledger, the record and the rows;
:meth:`Factory.fork` copies a factory as it stands, for scenarios that start from one state.

One call reaches past the seams: ``asf.harvest.pr_hygiene.run`` puts ``/opt/homebrew/bin`` ahead
of ``PATH``, so where a real ``gh`` lives there, PR hygiene runs it instead of the fake. The
harness points ``GH_CONFIG_DIR`` at an empty directory and blanks the tokens, so that ``gh`` has
no login and fails at once, asking no network: in the harness, hygiene sees no PRs.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import time
from unittest import mock

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_e2e_lane` does not
    from gitfixture import publish
    from e2e.fakes.session import ScriptedRuntime, load_scripts
except ImportError:  # pragma: no cover - import shape only
    from tests.gitfixture import publish
    from tests.e2e.fakes.session import ScriptedRuntime, load_scripts

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCT = os.path.join(HERE, 'product')
SCRIPTS = os.path.join(HERE, 'scripts')
FAKES = os.path.join(HERE, 'fakes')
NAME = 'sample'
SLUG = 'example/sample'
IDENTITY = {'GIT_AUTHOR_NAME': 'sample', 'GIT_AUTHOR_EMAIL': 'sample@example.com',
            'GIT_COMMITTER_NAME': 'sample', 'GIT_COMMITTER_EMAIL': 'sample@example.com'}
#: The caches a tick keeps for minutes: aged past their TTL before every tick.
AGED_CACHES = ('cache-prs.json', 'cache-evidence.json')
AGED_PREFIXES = ('pr-hygiene-',)


def _git(args, cwd, check=True):
    p = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, **IDENTITY))
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {p.stderr.strip()}")
    return p.stdout.strip()


def _fill(src, dest, **marks):
    with open(src, encoding='utf-8') as f:
        text = f.read()
    for k, v in marks.items():
        text = text.replace(f'@{k}@', v)
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(text)


def _copy_over(src, dest):
    """Copy the tree ``src`` onto ``dest``, file by file (an overlay)."""
    for dirpath, _dirs, files in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(dirpath, name), os.path.join(dest, rel, name))


#: Directories whose files never carry a path (git's object store): not read by a fork.
_NO_PATHS = ('objects',)


def _rewrite_tree(root, pairs):
    """Replace each ``(old, new)`` prefix in every text file under ``root``."""
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _NO_PATHS]
        for name in files:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            try:
                with open(path, encoding='utf-8') as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            new = text
            for old, repl in pairs:
                new = new.replace(old, repl)
            if new != text:
                st = os.stat(path)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(new)
                os.utime(path, (st.st_atime, st.st_mtime))


class Tick:
    """What one tick did: its printed ``lines``, the jobs it ``launched``, the feeder's
    ``rows`` as the wave planned them, and the :class:`Snapshot` after it."""

    def __init__(self, n, rc, lines, launched, rows, snap):
        self.n, self.rc, self.lines, self.launched, self.rows, self.snap = (
            n, rc, lines, launched, rows, snap)

    def find(self, prefix):
        return [ln for ln in self.lines if ln.startswith(prefix)]

    def __repr__(self):
        return f'Tick({self.n}, launched={self.launched})'


class Snapshot:
    """``ledger``: ``{job: run}`` folded off ``sessions.jsonl``; ``record``: ``{id: {state,
    stage, evidence, ...}}`` off the record's ``index.json``; ``rows``: the feeder's rows."""

    def __init__(self, ledger, record, rows):
        self.ledger, self.record, self.rows = ledger, record, rows

    def state(self, iid):
        return (self.record.get(iid) or {}).get('state')

    def stage(self, iid):
        return (self.record.get(iid) or {}).get('stage')

    def runs(self, item=None, kind=None):
        return [r for r in self.ledger.values()
                if (item is None or r.get('item') == item) and (kind is None or r.get('kind') == kind)]

    def of_type(self, type_):
        return {i: v for i, v in self.record.items() if v.get('type') == type_}


class Factory:
    def __init__(self, tmp, landing='pull-request', stage=None, scripts=None):
        self.tmp = os.path.realpath(tmp)
        self.landing = landing
        self.stage = stage
        self.runtime = ScriptedRuntime(scripts or load_scripts(SCRIPTS))
        self.ticks = []
        self.home = os.path.join(self.tmp, 'home')
        self.bin = os.path.join(self.tmp, 'bin')
        self.repo = os.path.join(self.tmp, 'product', 'repo')
        self.record_dir = os.path.join(self.tmp, 'product', 'record')
        self.repo_origin = os.path.join(self.tmp, 'origins', 'repo.git')
        self.record_origin = os.path.join(self.tmp, 'origins', 'record.git')
        self.environ = {}

    # ---- setup -------------------------------------------------------------------------------

    def setup(self):
        from asf.record.index import do_index
        shutil.copytree(os.path.join(PRODUCT, 'repo'), self.repo)
        shutil.copytree(os.path.join(PRODUCT, 'record'), self.record_dir)
        stages = (self.stage,) if isinstance(self.stage, str) else tuple(self.stage or ())
        for name in stages:  # overlays, in order: a later stage writes over an earlier one
            stage = os.path.join(PRODUCT, 'stages', name)
            for part, dest in (('repo', self.repo), ('record', self.record_dir)):
                if os.path.isdir(os.path.join(stage, part)):
                    _copy_over(os.path.join(stage, part), dest)
        with contextlib.redirect_stdout(io.StringIO()):
            do_index(self.record_dir)
        publish(self.repo, self.repo_origin, name='sample', email='sample@example.com')
        publish(self.record_dir, self.record_origin, name='sample', email='sample@example.com')
        os.makedirs(os.path.join(self.home, 'products'))
        _fill(os.path.join(PRODUCT, 'product.yaml'),
              os.path.join(self.home, 'products', f'{NAME}.yaml'),
              REPO=self.repo, RECORD=self.record_dir, LANDING=self.landing)
        shutil.copy2(os.path.join(PRODUCT, 'config.yaml'), os.path.join(self.home, 'config.yaml'))
        self._install_bin()
        os.makedirs(os.path.join(self.tmp, 'gh-config'))
        self.environ = self._environ()
        return self

    def _environ(self):
        """The variables a tick runs under: the home, the identity, ``bin/`` first on ``PATH``,
        and ``GH_CONFIG_DIR`` an empty dir — a real ``gh`` that runs anyway (one reached by a
        hard-coded ``PATH`` prefix) finds no login and asks no network."""
        return dict(IDENTITY, ASF_HOME=self.home, GH_TOKEN='', GITHUB_TOKEN='',
                    GH_CONFIG_DIR=os.path.join(self.tmp, 'gh-config'), GH_PROMPT_DISABLED='1',
                    PATH=self.bin + os.pathsep + os.environ.get('PATH', ''))

    def fork(self, tmp):
        """A copy of this factory, as it stands, under ``tmp`` (an existing empty directory):
        every file copied, every absolute path of this one rewritten to the copy's — the git
        configs and worktree links, the ledger, the yaml, the forge's state — and the scripted
        runtime's history with it. A scenario that starts where another stands (a branch ready
        to land) forks one built once instead of ticking there again."""
        import copy
        dest = os.path.realpath(tmp)
        for name in os.listdir(self.tmp):
            src = os.path.join(self.tmp, name)
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, os.path.join(dest, name), symlinks=True)
            else:
                shutil.copy2(src, os.path.join(dest, name), follow_symlinks=False)
        pairs = [(self.tmp, dest)]
        if self.tmp.startswith('/private/'):  # the same dir by its /var alias
            pairs.append((self.tmp[len('/private'):], dest))
        _rewrite_tree(dest, pairs)
        other = Factory(dest, landing=self.landing, stage=self.stage, scripts={})
        other.runtime = copy.deepcopy(self.runtime)
        for call in other.runtime.calls:
            for key in ('cwd', 'brief'):
                for old, new in pairs:
                    call[key] = call[key].replace(old, new)
        other.environ = other._environ()
        return other

    def _install_bin(self):
        os.makedirs(self.bin)
        gh = os.path.join(self.bin, 'gh')
        shutil.copy2(os.path.join(FAKES, 'gh'), gh)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with open(os.path.join(self.bin, 'gh-state.json'), 'w', encoding='utf-8') as f:
            json.dump({'origin': self.repo_origin, 'slug': SLUG, 'trunk': 'main', 'next': 1,
                       'prs': [], 'default_checks': [], 'merge_queue': False, 'queue': [],
                       'methods': ['squash', 'merge', 'rebase'], 'required': []}, f)
        # the redaction hooks the factory installs in the product repo call `asf redact
        # --pre-commit|--pre-push`: an instant stub, which refuses a push — printing why — while
        # `bin/refuse-push` exists (:meth:`refuse_pushes`)
        stub = os.path.join(self.bin, 'asf')
        with open(stub, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\nhere=$(dirname "$0")\n'
                    'if [ "$2" = "--pre-push" ] && [ -f "$here/refuse-push" ]; then\n'
                    '    cat "$here/refuse-push" >&2\n    exit 1\nfi\nexit 0\n')
        os.chmod(stub, 0o755)

    def refuse_pushes(self, why):
        """From now on the product repo's pre-push hook refuses every push, printing ``why``;
        ``why=None`` lifts it."""
        path = os.path.join(self.bin, 'refuse-push')
        if why is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, 'w', encoding='utf-8') as f:
            f.write(why + '\n')

    # ---- the tick ----------------------------------------------------------------------------

    @property
    def state_dir(self):
        return os.path.join(self.home, 'state', NAME)

    @contextlib.contextmanager
    def seams(self, rows_seen=None):
        """The harness's environment and its three seams, for a tick or a reader."""
        from asf import env
        from asf.feeder import rows as feeder_rows
        from asf.tick import step_harvest
        from asf.workers import runtime as runtime_mod
        real_plan_rows = feeder_rows.plan_rows

        def plan_rows(*a, **kw):
            out = real_plan_rows(*a, **kw)
            if rows_seen is not None and not rows_seen:
                rows_seen.extend(out)
            return out
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, self.environ))
            for var in ('ASF_PRODUCT', 'ASF_JOB', 'ASF_SESSION', 'BACKLOG_ID_RANGE'):
                os.environ.pop(var, None)
            stack.enter_context(mock.patch.object(env, 'ASF_HOME', self.home))
            stack.enter_context(mock.patch.object(runtime_mod, 'from_config',
                                                  lambda cfg: self.runtime))
            stack.enter_context(mock.patch.object(step_harvest, 'spawn_background',
                                                  self._harvest_inline))
            stack.enter_context(mock.patch.object(feeder_rows, 'plan_rows', plan_rows))
            yield

    @staticmethod
    def _harvest_inline(product, items_file=None):
        """What the detached harvest process runs, run here: this tick's landing is its own."""
        from asf.tick import step_harvest
        step_harvest.background(product, items_file, out=print)
        rec = step_harvest.read_status(product)
        step_harvest.write_status(product, dict(rec, reported=True))
        return os.getpid()

    def _age_caches(self):
        """The minutes between two ticks: every minutes-long cache is past its TTL."""
        if not os.path.isdir(self.state_dir):
            return
        old = time.time() - 3600
        for name in os.listdir(self.state_dir):
            if name in AGED_CACHES or name.startswith(AGED_PREFIXES):
                os.utime(os.path.join(self.state_dir, name), (old, old))

    def tick(self):
        """One whole ``asf tick`` of the product, in process. Returns a :class:`Tick`."""
        from asf.tick import tick as tick_mod
        self._age_caches()
        rows = []
        buf, err = io.StringIO(), io.StringIO()
        before = len(self.runtime.calls)
        with self.seams(rows), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = tick_mod.cmd_tick(argparse.Namespace(
                product=NAME, fresh=True, steps=None, shadow=False, manifest=False, daily=False))
        lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        launched = [c['job'] for c in self.runtime.calls[before:]]
        t = Tick(len(self.ticks) + 1, rc, lines, launched, rows, self.snapshot())
        t.stderr = err.getvalue()
        t.calls = self.runtime.calls[before:]  # the sessions this tick launched, in full
        self.ticks.append(t)
        return t

    # ---- the forge ---------------------------------------------------------------------------

    def gh(self, *argv):
        """Run the fake ``gh`` as a person or CI would (``pr merge 3 --squash``, ``e2e checks 3
        ci pass``). Returns the CompletedProcess."""
        return subprocess.run([os.path.join(self.bin, 'gh'), *argv], capture_output=True,
                              text=True, env=dict(os.environ, **self.environ))

    def forge(self):
        with open(os.path.join(self.bin, 'gh-state.json'), encoding='utf-8') as f:
            return json.load(f)

    def prs(self, branch=None):
        return [p for p in self.forge()['prs'] if branch is None or p['headRefName'] == branch]

    def gh_calls(self, *prefix):
        path = os.path.join(self.bin, 'gh-calls.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            calls = [json.loads(ln) for ln in f if ln.strip()]
        return [c for c in calls if c['argv'][:len(prefix)] == list(prefix)]

    def open_pr(self, branch, title, body=''):
        """A PR opened by hand, before or beside the factory."""
        p = self.gh('pr', 'create', '-R', SLUG, '--base', 'main', '--head', branch,
                    '--title', title, '--body', body)
        if p.returncode != 0:
            raise RuntimeError(p.stderr)
        return int(p.stdout.strip().rsplit('/', 1)[1])

    # ---- the repo, by hand -------------------------------------------------------------------

    def push(self, ref, files, subject, base='main'):
        """A commit of ``files`` (``{path: text | None}``, None deletes) pushed to ``ref`` of the
        product's origin by someone outside the factory, on top of ``base``. Returns its sha."""
        work = os.path.join(self.tmp, 'hand')
        if not os.path.isdir(work):
            _git(['clone', '-q', self.repo_origin, work], cwd=self.tmp)
        _git(['fetch', '-q', 'origin'], cwd=work)
        _git(['checkout', '-q', '-B', 'hand', f'origin/{base}'], cwd=work)
        for rel, text in files.items():
            path = os.path.join(work, rel)
            if text is None:
                os.remove(path)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        _git(['add', '-A'], cwd=work)
        _git(['commit', '-q', '-m', subject], cwd=work)
        _git(['push', '-q', 'origin', f'HEAD:refs/heads/{ref}'], cwd=work)
        return _git(['rev-parse', 'HEAD'], cwd=work)

    def trunk_log(self):
        """``[(sha, subject)]`` of the origin's trunk, newest first."""
        out = _git(['log', '--format=%H %s', 'main'], cwd=self.repo_origin)
        return [tuple(ln.split(' ', 1)) for ln in out.splitlines()]

    def on_origin(self, branch):
        return bool(_git(['rev-parse', '--verify', '-q', f'refs/heads/{branch}'],
                         cwd=self.repo_origin, check=False))

    def is_ancestor(self, a, b):
        return subprocess.run(['git', 'merge-base', '--is-ancestor', a, b], cwd=self.repo_origin,
                              capture_output=True).returncode == 0

    # ---- reading -----------------------------------------------------------------------------

    def snapshot(self):
        from asf.workers import lifecycle
        ledger = lifecycle.latest(os.path.join(self.state_dir, 'sessions.jsonl')) \
            if os.path.exists(os.path.join(self.state_dir, 'sessions.jsonl')) else {}
        record = {}
        index = os.path.join(self.state_dir, 'record', 'index.json')
        if os.path.exists(index):
            with open(index, encoding='utf-8') as f:
                data = json.load(f)
            items = data.get('items') if isinstance(data.get('items'), dict) else data
            record = {k: v for k, v in items.items() if isinstance(v, dict)}
        rows = self.ticks[-1].rows if self.ticks else []
        return Snapshot(ledger, record, rows)

    def next_rows(self):
        """The rows the feeder would plan now (``asf next``), off the tick's own record clone."""
        from asf import capacity as capacity_mod, env
        from asf.feeder import rows as feeder_rows
        from asf.tick import step_wave
        from asf.views import index_reader
        with self.seams():
            product = env.load_product(NAME)
            root = os.path.join(self.state_dir, 'record')
            items, _generated = index_reader.load(root)
            running = step_wave.inflight(product)
            return feeder_rows.plan_rows(items, product, running,
                                         capacity_mod.resolve(product).sessions,
                                         **step_wave.plan_inputs(product, root))
