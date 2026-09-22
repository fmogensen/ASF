import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from asf import env
from asf.conventions import Conventions
from asf.harvest import harvest
from asf.init import ITEM_FOLDERS, STREAM_FOLDERS

# The product under test: a record repo on trunk `main`, code branches under `worker/`, and a
# test command of its own. Nothing here is a default of the package — harvest reads all three.
CONV = Conventions.from_mapping({
    'test_command': f'{sys.executable} -m unittest discover -s tools -p test_*.py',
})

DEFAULT_BODY = (
    "## Description\n{desc}\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-21: created\n\n## Children\n\n## Backlinks\n"
)

# A minimal stand-in for the record repo's own index tool — just enough of the `index`/`check`
# contract to seed the fixture (an `index.json` at the root is what makes harvest treat a repo as
# the record repo). Harvest itself runs `asf index` / `asf check`, never this stub.
FIXTURE_BACKLOG_PY = '''\
import json
import os
import re
import sys

ROOT = os.getcwd()


def _ids():
    ids = []
    epics_dir = os.path.join(ROOT, 'epics')
    if os.path.isdir(epics_dir):
        for name in sorted(os.listdir(epics_dir)):
            if not name.endswith('.md'):
                continue
            with open(os.path.join(epics_dir, name), encoding='utf-8') as f:
                text = f.read()
            m = re.search(r'^id:\\s*(\\S+)', text, re.M)
            if m:
                ids.append(m.group(1))
    return sorted(ids)


def cmd_index():
    with open(os.path.join(ROOT, 'index.json'), 'w', encoding='utf-8') as f:
        json.dump({'ids': _ids()}, f, indent=2, sort_keys=True)
        f.write('\\n')
    return 0


def cmd_check():
    path = os.path.join(ROOT, 'index.json')
    if not os.path.isfile(path):
        print('no index.json', file=sys.stderr)
        return 1
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if sorted(data.get('ids', [])) != _ids():
        print('index.json is stale', file=sys.stderr)
        return 1
    return 0


def main(argv):
    if not argv:
        print('usage: backlog.py {index|check}', file=sys.stderr)
        return 2
    if argv[0] == 'index':
        return cmd_index()
    if argv[0] == 'check':
        return cmd_check()
    print(f'unknown command: {argv[0]}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
'''


def gate_importable_env():
    """`asf check` runs as a subprocess from the rebased worktree — it finds the package the
    same way the tests do."""
    env = clean_env()
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    return env


def clean_env():
    """The pre-commit hook exports GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE; a `git` child that
    inherits them ignores its `cwd` and writes into the real repo instead of the temp one."""
    env = {**os.environ}
    for var in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE'):
        env.pop(var, None)
    return env


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=clean_env())
    assert r.returncode == 0, f"{cmd} failed:\n{r.stdout}\n{r.stderr}"
    return r


def write_epic(repo, id_, title, desc='Original text'):
    path = os.path.join(repo, 'epics', f'{id_}.md')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    header = '\n'.join([
        f'id: {id_}', 'type: epic', f'title: {title}',
        '# ---- machine ----',
        'schema_version: 1',
        'state: New', 'stage_since: 2026-09-21T00:00:00Z', 'updated: 2026-09-21T00:00:00Z',
    ])
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'---\n{header}\n---\n{DEFAULT_BODY.format(desc=desc)}')
    return path


def index_and_commit(repo, message):
    r = subprocess.run([sys.executable, os.path.join('tools', 'backlog.py'), 'index'], cwd=repo,
                        capture_output=True, text=True, env=clean_env())
    assert r.returncode == 0, r.stdout + r.stderr
    sh(['git', 'add', '-A'], cwd=repo)
    sh(['git', 'commit', '-qm', message], cwd=repo)


def make_repo():
    """A bare `origin`, a `repo` clone (the tick clone / main checkout) seeded with one epic and
    an index, and a state dir with worktrees/ + the session registry."""
    base = tempfile.mkdtemp(prefix='harvest_test_')
    origin = os.path.join(base, 'origin.git')
    repo = os.path.join(base, 'repo')
    # B-0038: the trunk is `main` whatever the host's init.defaultBranch says — the CI runner
    # has none and would otherwise start the clone on `master`, and `add_job_worktree`'s `main`
    # would resolve to a tracking branch instead of the worker branch's base
    sh(['git', 'init', '-q', '--bare', '-b', 'main', origin])
    sh(['git', 'clone', '-q', origin, repo])
    sh(['git', 'symbolic-ref', 'HEAD', 'refs/heads/main'], cwd=repo)
    sh(['git', 'config', 'user.name', 'Test'], cwd=repo)
    sh(['git', 'config', 'user.email', 'test@example.com'], cwd=repo)
    sh(['git', 'config', 'commit.gpgsign', 'false'], cwd=repo)

    # a `.gitkeep` per folder, not a bare `os.makedirs` — git tracks no empty directory, so an
    # untracked one would vanish the moment a job's `git worktree add` checks out a branch
    for f in ITEM_FOLDERS + STREAM_FOLDERS:
        d = os.path.join(repo, f)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, '.gitkeep'), 'w').close()
    os.makedirs(os.path.join(repo, 'tools'), exist_ok=True)
    with open(os.path.join(repo, 'tools', 'backlog.py'), 'w', encoding='utf-8') as f:
        f.write(FIXTURE_BACKLOG_PY)
    with open(os.path.join(repo, 'tools', 'test_fixture.py'), 'w', encoding='utf-8') as f:
        f.write(
            "import unittest\n\n"
            "class FixtureTests(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n"
        )
    write_epic(repo, 'E-0001', 'Seed epic')
    index_and_commit(repo, 'init')
    sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=repo)

    state_dir = os.path.join(base, 'state')
    os.makedirs(os.path.join(state_dir, 'worktrees'))
    return base, origin, repo, state_dir


def dead_pid():
    """A process id that has ended — a finished session's pid, as harvest must find it."""
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


def write_session(state_dir, job, branch, rc=0, ended=True, pid=None):
    """The registry line a launch writes, and the one that ends the session — the same
    append-only shape asf.workers.pool writes (see its `load_sessions`)."""
    with open(os.path.join(state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps({'job': job, 'branch': branch, 'account': 'test',
                            'pid': dead_pid() if pid is None else pid,
                            'started': '2026-09-21T00:00:00Z'}) + '\n')
        if ended:
            f.write(json.dumps({'job': job, 'ended': '2026-09-21T00:05:00Z',
                                'end_reason': 'finished' if rc == 0 else 'failed', 'rc': rc}) + '\n')


def harvested(state_dir, job):
    return bool(harvest.read_sessions(state_dir).get(job, {}).get('harvested'))


def add_job_worktree(repo, state_dir, job, base_ref='main'):
    branch = CONV.branch('code', job)
    wt = os.path.join(state_dir, 'worktrees', job)
    sh(['git', 'worktree', 'add', '-q', '-b', branch, wt, base_ref], cwd=repo)
    sh(['git', 'config', 'user.name', 'Test'], cwd=wt)
    sh(['git', 'config', 'user.email', 'test@example.com'], cwd=wt)
    sh(['git', 'config', 'commit.gpgsign', 'false'], cwd=wt)
    return branch, wt


def run_harvest(repo, state_dir, dry_run=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = harvest.run_harvest(repo, state_dir, dry_run, CONV)
    return rc, buf.getvalue()


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.base, self.origin, self.repo, self.state_dir = make_repo()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def test_b0033_gate_env_drops_the_callers_identity(self):
        # the tick sets ASF_PRODUCT for its own steps; a worker session carries ASF_JOB and
        # BACKLOG_ID_RANGE — none of them may reach the branch's own test run
        with mock.patch.dict(os.environ, {'ASF_PRODUCT': 'asf', 'ASF_JOB': 'fix-bug-b-0002',
                                          'BACKLOG_ID_RANGE': 'B:1-9', 'ASF_HOME': '/x/.ASF'}):
            genv = harvest.gate_env()
        for var in ('ASF_PRODUCT', 'ASF_JOB', 'BACKLOG_ID_RANGE'):
            self.assertNotIn(var, genv, var)
        self.assertEqual(genv.get('ASF_HOME'), '/x/.ASF')  # the operator's home is not identity

    def test_b0033_gate_env_puts_the_gated_worktree_first_on_pythonpath(self):
        # a test that runs `python -m asf.cli` from another cwd must import the branch's code
        with mock.patch.dict(os.environ, {'PYTHONPATH': '/elsewhere'}):
            genv = harvest.gate_env('/tmp/harvest-x/wt')
        parts = genv['PYTHONPATH'].split(os.pathsep)
        self.assertEqual(parts[0], '/tmp/harvest-x/wt')
        self.assertTrue(os.path.isdir(os.path.join(parts[1], 'asf')), parts[1])
        self.assertEqual(parts[-1], '/elsewhere')

    # -- B-0038: the fixture is on `main` whatever the host's init.defaultBranch says ----------
    def test_b0038_fixture_trunk_is_main_without_a_host_default_branch(self):
        # the CI runner has no init.defaultBranch; a clone there starts on `master`, and
        # `git worktree add -b worker/<job> <path> main` then checks out a tracking `main`
        # instead of creating the worker branch — harvest finds nothing and prints nothing
        with tempfile.NamedTemporaryFile('w', suffix='.gitconfig', delete=False) as f:
            f.write('[user]\n\tname = ci\n\temail = ci@localhost\n')
        self.addCleanup(os.unlink, f.name)
        with mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': f.name, 'GIT_CONFIG_NOSYSTEM': '1'}):
            base, origin, repo, state_dir = make_repo()
            self.addCleanup(shutil.rmtree, base, ignore_errors=True)
            head = sh(['git', 'symbolic-ref', '--short', 'HEAD'], cwd=repo).stdout.strip()
            self.assertEqual(head, 'main')
            branch, wt = add_job_worktree(repo, state_dir, 'trunk1')
            self.assertEqual(harvest.worker_branches(repo, CONV), [branch])

    # -- a fast-forwardable branch lands on main with no controller action -----------------
    def test_green_branch_lands_on_main(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 'ff1')
        write_epic(wt, 'E-0002', 'New epic from ff1')
        index_and_commit(wt, 'ff1: add E-0002')
        write_session(self.state_dir, 'ff1', branch)

        rc, out = run_harvest(self.repo, self.state_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST OK ff1', out)

        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        log = sh(['git', 'log', '--oneline', 'origin/main'], cwd=self.repo).stdout
        self.assertIn('ff1: add E-0002', log)

        self.assertFalse(os.path.isdir(os.path.join(self.state_dir, 'worktrees', 'ff1')))
        branches = sh(['git', 'branch', '--list', branch], cwd=self.repo).stdout
        self.assertEqual(branches.strip(), '')
        self.assertTrue(harvested(self.state_dir, 'ff1'))

    # -- B-0009: no reap unless the trunk really holds the branch tip ----------------------
    def test_no_reap_unless_fast_forward_landed(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 'nr1')
        write_epic(wt, 'E-0002', 'New epic from nr1')
        index_and_commit(wt, 'nr1: add E-0002')
        write_session(self.state_dir, 'nr1', branch)

        # a push that claims success while origin/main never received the commit
        with mock.patch.object(harvest, 'push_ff', return_value=(True, False)):
            rc, out = run_harvest(self.repo, self.state_dir)
        self.assertNotIn('HARVEST OK', out)
        self.assertIn('HARVEST HOLD nr1', out)
        self.assertTrue(os.path.isdir(wt))
        self.assertNotEqual(sh(['git', 'branch', '--list', branch], cwd=self.repo).stdout.strip(), '')
        self.assertFalse(harvested(self.state_dir, 'nr1'))

    # -- a non-machine conflict holds the branch and names the file ------------------------
    def test_non_machine_conflict_is_held(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 'conflict1')
        write_epic(wt, 'E-0001', 'Seed epic', desc='Worker edit')
        index_and_commit(wt, 'conflict1: edit description')
        write_session(self.state_dir, 'conflict1', branch)

        # a different edit to the same line lands on main first, out from under the branch
        write_epic(self.repo, 'E-0001', 'Seed epic', desc='Main edit')
        index_and_commit(self.repo, 'main: edit description')
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        main_sha = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()

        rc, out = run_harvest(self.repo, self.state_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST HOLD conflict1', out)
        self.assertIn('conflict in', out)
        self.assertIn('E-0001.md', out)

        # nothing touched: worktree, branch, tsv row and main all unchanged
        self.assertTrue(os.path.isdir(os.path.join(self.state_dir, 'worktrees', 'conflict1')))
        branches = sh(['git', 'branch', '--list', branch], cwd=self.repo).stdout
        self.assertIn(branch, branches)
        self.assertFalse(harvested(self.state_dir, 'conflict1'))
        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        self.assertEqual(sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip(), main_sha)

    # -- a branch whose tests fail is held, never landed ------------------------------------
    def test_failing_tests_are_held(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 'redtest')
        with open(os.path.join(wt, 'tools', 'test_fixture.py'), 'w', encoding='utf-8') as f:
            f.write(
                "import unittest\n\n"
                "class FixtureTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(False)\n"
            )
        sh(['git', 'add', '-A'], cwd=wt)
        sh(['git', 'commit', '-qm', 'redtest: break the gate'], cwd=wt)
        write_session(self.state_dir, 'redtest', branch)

        rc, out = run_harvest(self.repo, self.state_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST HOLD redtest', out)
        self.assertIn('tests failed', out)

        self.assertTrue(os.path.isdir(os.path.join(self.state_dir, 'worktrees', 'redtest')))
        self.assertFalse(harvested(self.state_dir, 'redtest'))

    # -- an index.json-only conflict is resolved by regenerating it ------------------------
    def test_index_json_only_conflict_is_resolved(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 'idxconf')
        write_epic(wt, 'E-0003', 'Worker-side epic')
        index_and_commit(wt, 'idxconf: add E-0003')

        # a different new epic lands on main first — same index.json region, different content
        write_epic(self.repo, 'E-0002', 'Main-side epic')
        index_and_commit(self.repo, 'main: add E-0002')
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)

        write_session(self.state_dir, 'idxconf', branch)

        rc, out = run_harvest(self.repo, self.state_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST OK idxconf', out)

        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        r = sh(['git', 'show', 'origin/main:index.json'], cwd=self.repo)
        self.assertIn('E-0002', r.stdout)
        self.assertIn('E-0003', r.stdout)
        # the landed index is the one `asf index` regenerated — check runs against the checked-out
        # worktree, so fast-forward the local main branch to origin first
        sh(['git', 'checkout', '-q', 'main'], cwd=self.repo)
        sh(['git', 'merge', '-q', '--ff-only', 'origin/main'], cwd=self.repo)
        check = subprocess.run([sys.executable, '-m', 'asf.cli', 'check'], cwd=self.repo,
                                capture_output=True, text=True, env=gate_importable_env())
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    # -- dry-run reports without mutating anything ------------------------------------------
    def test_dry_run_prints_and_does_not_push(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 'dry1')
        write_epic(wt, 'E-0004', 'Dry run epic')
        index_and_commit(wt, 'dry1: add E-0004')
        write_session(self.state_dir, 'dry1', branch)

        before = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        rc, out = run_harvest(self.repo, self.state_dir, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn('DRY: would push dry1', out)

        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        after = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        self.assertEqual(before, after)
        self.assertTrue(os.path.isdir(os.path.join(self.state_dir, 'worktrees', 'dry1')))
        self.assertFalse(harvested(self.state_dir, 'dry1'))

    # -- an unfinished or non-zero-rc job is left alone, not even attempted -----------------
    def test_ineligible_job_is_skipped(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 'running1')
        write_epic(wt, 'E-0005', 'Still running')
        index_and_commit(wt, 'running1: wip')
        write_session(self.state_dir, 'running1', branch, ended=False)

        rc, out = run_harvest(self.repo, self.state_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), '')
        self.assertTrue(os.path.isdir(os.path.join(self.state_dir, 'worktrees', 'running1')))

    # -- B-0010: a reap needs a finished line AND a dead pid; otherwise it holds ------------
    def worktree_survives(self, job, branch):
        self.assertTrue(os.path.isdir(os.path.join(self.state_dir, 'worktrees', job)))
        self.assertNotEqual(sh(['git', 'branch', '--list', branch], cwd=self.repo).stdout.strip(), '')
        self.assertFalse(harvested(self.state_dir, job))

    def test_reap_holds_for_live_pid_and_for_no_finished_line(self):
        live = os.getpid()  # a process that is certainly running
        branch, wt = add_job_worktree(self.repo, self.state_dir, 'live1')
        write_epic(wt, 'E-0006', 'Live pid')
        index_and_commit(wt, 'live1: add E-0006')
        write_session(self.state_dir, 'live1', branch, pid=live)

        rc, out = run_harvest(self.repo, self.state_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out.count('HARVEST HOLD live1'), 1, out)
        self.assertNotIn('HARVEST OK', out)
        self.worktree_survives('live1', branch)

        branch, wt = add_job_worktree(self.repo, self.state_dir, 'open1')
        write_session(self.state_dir, 'open1', branch, ended=False, pid=dead_pid())
        buf = io.StringIO()
        with redirect_stdout(buf):
            harvest.reap(self.repo, self.state_dir, 'open1', branch)
        self.assertEqual(buf.getvalue().count('HARVEST HOLD open1'), 1, buf.getvalue())
        self.worktree_survives('open1', branch)


# ---- the product repo: remote lane branches, sessions keyed by branch --------------------------

PRODUCT_TEST = f'{sys.executable} -m unittest discover -s checks -p test_*.py'
GREEN_TEST = ("import unittest\n\nclass Fx(unittest.TestCase):\n"
              "    def test_ok(self):\n        self.assertTrue(True)\n")
RED_TEST = ("import unittest\n\nclass Fx(unittest.TestCase):\n"
            "    def test_red_gate(self):\n        self.assertTrue(False)\n")


def git_identity(path):
    sh(['git', 'config', 'user.name', 'Test'], cwd=path)
    sh(['git', 'config', 'user.email', 'test@example.com'], cwd=path)
    sh(['git', 'config', 'commit.gpgsign', 'false'], cwd=path)


class ProductHarvestTests(unittest.TestCase):
    """A bare product origin, the product's own checkout (``repo_dir``, which never has the lane
    branch locally), and a worker's clone that pushes ``fix/B-0001`` — as a session does."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='harvest_product_')
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.origin = os.path.join(self.base, 'origin.git')
        self.repo = os.path.join(self.base, 'repo')
        self.worker = os.path.join(self.base, 'worker')
        self.state_dir = os.path.join(self.base, 'state')
        os.makedirs(self.state_dir)
        sh(['git', 'init', '-q', '--bare', '-b', 'main', self.origin])
        sh(['git', 'clone', '-q', self.origin, self.repo])
        git_identity(self.repo)
        self.write(self.repo, 'checks/test_fx.py', GREEN_TEST)
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'init'], cwd=self.repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'clone', '-q', self.origin, self.worker])
        git_identity(self.worker)

    def write(self, root, rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def product(self, **conventions):
        conventions.setdefault('test_command', PRODUCT_TEST)
        return env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                      'conventions': conventions, 'steps': {'batch': 'off'}})

    def push_lane(self, branch, commits):
        """``commits``: ``[(subject, {rel: text})]`` on a fresh ``branch`` off main, pushed."""
        sh(['git', 'checkout', '-q', '-B', branch, 'origin/main'], cwd=self.worker)
        for subject, files in commits:
            for rel, text in files.items():
                self.write(self.worker, rel, text)
            sh(['git', 'add', '-A'], cwd=self.worker)
            sh(['git', 'commit', '-qm', subject], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', branch], cwd=self.worker)

    def session(self, job, item, branch, rc=0):
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': job, 'item': item, 'branch': branch, 'account': 'test',
                                'pid': dead_pid(), 'started': '2026-09-21T00:00:00Z'}) + '\n')
            f.write(json.dumps({'job': job, 'ended': '2026-09-21T00:05:00Z',
                                'end_reason': 'finished' if rc == 0 else 'failed', 'rc': rc}) + '\n')

    def harvest(self, product, bug_root=None):
        lines = []
        results = harvest.run_product_harvest(product, self.state_dir, bug_root=bug_root,
                                              out=lines.append)
        return results, lines

    def origin_main(self):
        return sh(['git', 'rev-parse', 'main'], cwd=self.origin).stdout.strip()

    def origin_has(self, branch):
        return bool(sh(['git', 'branch', '--list', branch], cwd=self.origin).stdout.strip())

    def record(self, branch):
        return harvest.sessions_by_branch(self.state_dir).get(branch) or {}

    def test_remote_only_fix_branch_lands_and_is_gone(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'}),
                                      ('test(B-0001): its test', {'b.txt': 'b\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        self.assertEqual(sh(['git', 'branch', '--list', 'fix/*'], cwd=self.repo).stdout.strip(), '')

        results, lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        sha = self.origin_main()
        self.assertEqual(lines, [f'landed fix/B-0001 → {sha}',
                                 f'harvest: {self.repo} fast-forwarded to {sha}'])
        log = sh(['git', 'log', '--format=%s', 'main'], cwd=self.origin).stdout
        self.assertIn('fix(B-0001): the change', log)
        self.assertFalse(self.origin_has('fix/B-0001'))
        self.assertEqual(self.record('fix/B-0001').get('harvested'), sha)

        # landed once: the next tick has nothing to do
        self.assertEqual(self.harvest(self.product()), ({}, []))

    def test_b0036_landing_fast_forwards_the_checkout(self):
        """The scheduler runs the ``repo_dir`` checkout; a landing that only reached origin left
        it running the code from before its own fix. On the trunk with a clean tree, it is
        fast-forwarded to the landed sha — never reset."""
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = sh(['git', 'rev-parse', 'main'], cwd=self.repo).stdout.strip()
        results, lines = self.harvest(self.product())
        sha = self.origin_main()
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        self.assertNotEqual(sha, before)
        self.assertEqual(sh(['git', 'rev-parse', 'main'], cwd=self.repo).stdout.strip(), sha)
        self.assertEqual(lines[-1], f'harvest: {self.repo} fast-forwarded to {sha}')

    def test_b0036_dirty_or_off_trunk_checkout_is_left_alone(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = sh(['git', 'rev-parse', 'main'], cwd=self.repo).stdout.strip()
        self.write(self.repo, 'checks/test_fx.py', GREEN_TEST + '# local edit\n')
        results, lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        self.assertEqual(sh(['git', 'rev-parse', 'main'], cwd=self.repo).stdout.strip(), before)
        self.assertEqual(lines[-1], f'harvest: {self.repo} not fast-forwarded — working tree has local changes')

    def push_main(self, subject, files):
        """A fix pushed straight to main — not through a lane landing."""
        sh(['git', 'checkout', '-q', '-B', 'main', 'origin/main'], cwd=self.worker)
        for rel, text in files.items():
            self.write(self.worker, rel, text)
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', subject], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)

    def test_b0042_direct_push_to_main_fast_forwards_the_checkout(self):
        """A fix pushed to main any way but a lane landing left the factory on old code: the
        checkout is fast-forwarded whenever origin/<trunk> is ahead, with no landing at all."""
        self.push_main('fix(B-0001): straight to main', {'a.txt': 'a\n'})
        before = sh(['git', 'rev-parse', 'main'], cwd=self.repo).stdout.strip()
        results, lines = self.harvest(self.product())
        sha = self.origin_main()
        self.assertEqual(results, {})
        self.assertNotEqual(sha, before)
        self.assertEqual(sh(['git', 'rev-parse', 'main'], cwd=self.repo).stdout.strip(), sha)
        self.assertEqual(lines, [f'harvest: {self.repo} fast-forwarded to {sha}'])

    def test_b0042_checkout_moves_before_any_landed_line(self):
        self.push_main('fix(B-0002): straight to main', {'m.txt': 'm\n'})
        direct = self.origin_main()
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        results, lines = self.harvest(self.product())
        sha = self.origin_main()
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        self.assertEqual(lines, [f'harvest: {self.repo} fast-forwarded to {direct}',
                                 f'landed fix/B-0001 → {sha}',
                                 f'harvest: {self.repo} fast-forwarded to {sha}'])

    def test_b0042_direct_push_leaves_a_dirty_checkout_alone(self):
        self.push_main('fix(B-0001): straight to main', {'a.txt': 'a\n'})
        before = sh(['git', 'rev-parse', 'main'], cwd=self.repo).stdout.strip()
        self.write(self.repo, 'checks/test_fx.py', GREEN_TEST + '# local edit\n')
        _results, lines = self.harvest(self.product())
        self.assertEqual(sh(['git', 'rev-parse', 'main'], cwd=self.repo).stdout.strip(), before)
        self.assertEqual(lines, [f'harvest: {self.repo} not fast-forwarded — working tree has local changes'])

    def test_session_is_matched_by_branch_not_by_job_name(self):
        self.push_lane('worker/add-thing', [('feat(T-0007): add the thing', {'t.txt': 't\n'})])
        # the job is keyed differently from the branch; the latest start line for the branch wins
        self.session('task-t-0007', 'T-0007', 'worker/add-thing', rc=1)
        self.session('task-t-0007-again', 'T-0007', 'worker/add-thing', rc=0)
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'worker/add-thing': 'landed'})
        self.assertTrue(self.record('worker/add-thing').get('harvested'))

    def test_unfinished_or_red_session_is_not_attempted(self):
        self.push_lane('fix/B-0002', [('fix(B-0002): x', {'x.txt': 'x\n'})])
        self.session('fix-bug-b-0002', 'B-0002', 'fix/B-0002', rc=1)
        before = self.origin_main()
        self.assertEqual(self.harvest(self.product()), ({}, []))
        self.assertEqual(self.origin_main(), before)

    def test_commit_not_naming_the_item_holds(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'}),
                                      ('tidy up', {'c.txt': 'c\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = self.origin_main()
        results, lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('held fix/B-0001: commits do not name B-0001:'), lines)
        self.assertTrue(lines[0].endswith(' — back to its session (round 1)'), lines)
        self.assertEqual(self.record('fix/B-0001')['correction']['kind'], 'naming')
        self.assertEqual(self.origin_main(), before)
        self.assertTrue(self.origin_has('fix/B-0001'))
        self.assertFalse(self.record('fix/B-0001').get('harvested'))

    def test_b0056_a_merge_commit_on_a_lane_branch_is_held_with_the_named_reason(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        # the trunk moves; the session merges it in (what a session did when its rebase could
        # not be pushed) — the branch is no longer straight commits on the trunk
        sh(['git', 'checkout', '-q', 'main'], cwd=self.worker)
        self.write(self.worker, 'm.txt', 'm\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'trunk moves'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)
        sh(['git', 'checkout', '-q', 'fix/B-0001'], cwd=self.worker)
        sh(['git', 'merge', '-q', '--no-edit', 'main', '-m', 'fix(B-0001): merge main'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'fix/B-0001'], cwd=self.worker)
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = self.origin_main()
        results, lines = self.harvest(self.product())
        lines = [l for l in lines if not l.startswith('harvest: ')]  # the checkout's fast-forward
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertTrue(lines[0].startswith('held fix/B-0001: merge commit on a lane branch: '), lines)
        self.assertIn('rebase onto it, never merge origin/fix/B-0001 or origin/main into it', lines[0])
        self.assertTrue(lines[0].endswith(' — back to its session (round 1)'), lines)
        rec = self.record('fix/B-0001')
        self.assertEqual(rec['correction']['kind'], 'merge')
        self.assertEqual(self.origin_main(), before)
        self.assertTrue(self.origin_has('fix/B-0001'))

    def test_b0057_a_spec_branch_whose_document_is_on_the_trunk_is_landed_without_a_gate(self):
        spec = 'docs/specs/f-0001.md'
        self.push_lane('spec/F-0001', [('spec(F-0001): first cut', {spec: 'v1\n'}),
                                       ('spec(F-0001): final', {spec: 'final\n'}),
                                       ('adjudicate(F-0001): a ruling that does not belong here',
                                        {'docs/reviews/1-f-0001-ruling.md': 'no finding\n'})])
        # the document reached the trunk another way (a hand landing), in its final form
        sh(['git', 'checkout', '-q', 'main'], cwd=self.worker)
        self.write(self.worker, spec, 'final\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'spec(F-0001): landed by hand'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)
        self.session('spec-f-0001', 'F-0001', 'spec/F-0001')
        before = self.origin_main()
        results, lines = self.harvest(self.product(branch_prefixes={'spec': 'spec/', 'code': 'worker/'}))
        lines = [l for l in lines if not l.startswith('harvest: ')]
        self.assertEqual(results, {'spec/F-0001': 'landed'})
        self.assertEqual(lines, [f'landed spec/F-0001: already on main at {before[:7]}; not its '
                                 f'deliverable, dropped with the branch: docs/reviews/1-f-0001-ruling.md'])
        self.assertEqual(self.origin_main(), before)  # no gate, no push: nothing to land
        self.assertFalse(self.origin_has('spec/F-0001'))
        rec = self.record('spec/F-0001')
        self.assertEqual(rec['harvested'], before)
        self.assertFalse(rec.get('correction'))
        self.assertEqual(self.harvest(self.product()), ({}, []))

    def test_b0057_a_branch_whose_every_change_is_on_the_trunk_is_landed(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        sh(['git', 'checkout', '-q', 'main'], cwd=self.worker)
        self.write(self.worker, 'a.txt', 'a\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'fix(B-0001): the same change, landed another way'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = self.origin_main()
        results, lines = self.harvest(self.product())
        lines = [l for l in lines if not l.startswith('harvest: ')]
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        self.assertEqual(lines, [f'landed fix/B-0001: already on main at {before[:7]}'])
        self.assertFalse(self.origin_has('fix/B-0001'))

    def test_b0057_a_closed_bugs_branch_is_archived_not_held(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): an older approach', {'old.txt': 'old\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        items = {'B-0001': {'id': 'B-0001', 'type': 'bug', 'state': 'Closed'}}
        before = self.origin_main()
        lines = []
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append, items=items)
        self.assertEqual(results, {'fix/B-0001': 'superseded'})
        self.assertEqual(lines, ['superseded fix/B-0001: B-0001 is Closed in the record — archived as archive/fix/B-0001'])
        self.assertEqual(self.origin_main(), before)
        self.assertFalse(self.origin_has('fix/B-0001'))
        self.assertTrue(self.origin_has('archive/fix/B-0001'))
        self.assertEqual(self.record('fix/B-0001')['harvested'], 'superseded')
        # an open Bug's branch, or a Feature's, is never superseded this way
        self.push_lane('fix/B-0002', [('fix(B-0002): live work', {'live.txt': 'live\n'})])
        self.session('fix-bug-b-0002', 'B-0002', 'fix/B-0002')
        items['B-0002'] = {'id': 'B-0002', 'type': 'bug', 'state': 'Active'}
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append, items=items)
        self.assertEqual(results, {'fix/B-0002': 'landed'})

    def test_b0054_adjudicate_commit_is_held_ruling_belongs_in_record(self):
        """A ruling belongs in the record (a decision or the item's ``## History``), never a
        commit to the product repo — harvest refuses one whose subject opens ``adjudicate(``
        (B-0054), holding instead of landing an invented ruling on the trunk."""
        self.push_lane('fix/B-0001', [('adjudicate(B-0001): the fix stands', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = self.origin_main()
        results, lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertEqual(lines, ['held fix/B-0001: ruling belongs in the record'])
        self.assertEqual(self.origin_main(), before)
        self.assertTrue(self.origin_has('fix/B-0001'))
        self.assertFalse(self.record('fix/B-0001').get('harvested'))

    def test_red_test_command_holds_and_records_a_correction(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): breaks the gate',
                                       {'checks/test_fx.py': RED_TEST})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        record_root = os.path.join(self.base, 'record')
        os.makedirs(os.path.join(record_root, 'bugs'))
        before = self.origin_main()
        results, lines = self.harvest(self.product(), bug_root=lambda: record_root)
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('held fix/B-0001: FAIL: test_red_gate'), lines)
        self.assertTrue(lines[0].endswith(' — back to its session (round 1)'), lines)
        self.assertEqual(self.origin_main(), before)
        self.assertTrue(self.origin_has('fix/B-0001'))
        self.assertEqual(os.listdir(os.path.join(record_root, 'bugs')), [])  # no Bug filed
        rec = self.record('fix/B-0001')
        self.assertEqual(rec['rounds'], 1)
        self.assertEqual(rec['correction']['kind'], 'gate')
        self.assertIn('test_red_gate', rec['correction']['text'])
        self.assertTrue(rec['correction']['at'])
        self.assertFalse(rec.get('harvested'))
        # a second red tick is round 2
        _results, lines = self.harvest(self.product())
        self.assertTrue(lines[0].endswith('(round 2)'), lines)
        self.assertEqual(self.record('fix/B-0001')['rounds'], 2)

    def test_b0048_rounds_cap_at_the_adjudicate_switch_then_flags_the_operator(self):
        """Round 3 is where the feeder switches this item to an ADJUDICATE row instead of
        another CORRECT one (``feeder.rows.CORRECTION_ROUNDS``) — from there the round must not
        keep climbing (B-0048: it climbed past the cap forever). A first hold at the cap is the
        adjudicate row's own attempt failing; only a second one — the adjudicate row cannot land
        either — flags the item for an operator."""
        self.push_lane('fix/B-0001', [('fix(B-0001): breaks the gate',
                                       {'checks/test_fx.py': RED_TEST})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        product = self.product()
        for expected_round in (1, 2, 3):
            _results, lines = self.harvest(product)
            self.assertTrue(lines[0].endswith(f'(round {expected_round})'), lines)
            self.assertEqual(self.record('fix/B-0001')['rounds'], expected_round)

        # capped: the round no longer climbs, and the message names the adjudicate row instead
        _results, lines = self.harvest(product)
        self.assertTrue(lines[0].startswith('held fix/B-0001: FAIL: test_red_gate'), lines)
        self.assertTrue(lines[0].endswith(' — adjudicate pending'), lines)
        self.assertEqual(self.record('fix/B-0001')['rounds'], 3)
        self.assertFalse(self.record('fix/B-0001').get('operator_flagged'))

        # a second hold at the cap: the adjudicate row's own attempt failed too
        _results, lines = self.harvest(product)
        self.assertTrue(lines[0].endswith(' — adjudicate pending'), lines)
        self.assertEqual(self.record('fix/B-0001')['rounds'], 3)
        self.assertEqual(self.record('fix/B-0001').get('operator_flagged'), 1)

    def test_pull_request_landing_never_pushes_main(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        before = self.origin_main()
        results, lines = self.harvest(self.product(landing='pull-request'))
        self.assertEqual(results, {'fix/B-0001': 'pr'})
        self.assertEqual(lines, ['pr-lane fix/B-0001'])
        self.assertEqual(self.origin_main(), before)
        self.assertTrue(self.origin_has('fix/B-0001'))
        rec = self.record('fix/B-0001')
        self.assertEqual(rec.get('harvest'), 'pr')
        self.assertFalse(rec.get('harvested'))
        # marked once: the next tick does not announce it again
        self.assertEqual(self.harvest(self.product(landing='pull-request')), ({}, []))

    def test_landing_default_follows_the_batch_step(self):
        mk = lambda steps, conv=None: env.Product('p', {'steps': steps, 'conventions': conv or {}})
        self.assertEqual(harvest.landing(mk({})), 'fast-forward')
        self.assertEqual(harvest.landing(mk({'batch': 'off'})), 'fast-forward')
        self.assertEqual(harvest.landing(mk({'batch': 'bash q.sh'})), 'pull-request')
        self.assertEqual(harvest.landing(mk({'batch': 'bash q.sh'}, {'landing': 'fast-forward'})),
                         'fast-forward')

    def test_trunk_moved_is_rebased_then_landed(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        self.write(self.repo, 'other.txt', 'o\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'another change'], cwd=self.repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        log = sh(['git', 'log', '--format=%s', 'main'], cwd=self.origin).stdout.splitlines()
        self.assertEqual(log[:2], ['fix(B-0001): the change', 'another change'])

    def test_asf_own_repo_is_recognised(self):
        self.assertTrue(harvest.is_asf_repo(REPO_ROOT))
        self.assertFalse(harvest.is_asf_repo(self.repo))

    # -- B-0031: a tick gates only so many branches — the rest wait for the next -----------
    def test_b0031_caps_branches_gated_per_tick(self):
        for i in range(1, 5):
            item = f'B-000{i}'
            branch = f'fix/{item}'
            self.push_lane(branch, [(f'fix({item}): change {i}', {f'f{i}.txt': f'{i}\n'})])
            self.session(f'fix-bug-{item.lower()}', item, branch)

        results, lines = self.harvest(self.product())
        self.assertEqual(len(results), 3, results)
        self.assertTrue(all(r == 'landed' for r in results.values()), results)
        self.assertTrue(any('cap' in l.lower() for l in lines), lines)

        remaining = [f'fix/B-000{i}' for i in range(1, 5) if f'fix/B-000{i}' not in results]
        self.assertEqual(len(remaining), 1, remaining)
        self.assertTrue(self.origin_has(remaining[0]))

        # the next tick lands what the cap left waiting
        results2, _lines2 = self.harvest(self.product())
        self.assertEqual(results2, {remaining[0]: 'landed'})

    def test_record_repo_keeps_its_own_path(self):
        with mock.patch.object(harvest, 'is_record_repo', return_value=True), \
                mock.patch.object(harvest, 'run_harvest', return_value=0) as old:
            self.assertEqual(harvest.run_product_harvest(self.product(), self.state_dir), {})
        old.assert_called_once()
        self.assertEqual(old.call_args[0][0], os.path.abspath(self.repo))


class RulesSourceMergeTests(unittest.TestCase):
    def test_source_line_union_merge(self):
        ours = 'source: "Ops 2026-09-20: standing authority; memory alpha"'
        theirs = 'source: "Ops 2026-09-20: standing authority; memory beta"'
        merged = harvest.merge_source_hunk(ours, theirs)
        self.assertEqual(len(merged), 1)
        self.assertIn('; memory alpha', merged[0])
        self.assertIn('; memory beta', merged[0])
        self.assertTrue(merged[0].startswith('source: "Ops 2026-09-20: standing authority'))

    def test_conflict_hunk_parsing(self):
        text = (
            "a\n<<<<<<< HEAD\nours line\n=======\ntheirs line\n>>>>>>> branch\nb\n"
        )
        lines, hunks = harvest.find_conflict_hunks(text)
        self.assertEqual(len(hunks), 1)
        self.assertEqual(hunks[0]['ours'], ['ours line'])
        self.assertEqual(hunks[0]['theirs'], ['theirs line'])


if __name__ == '__main__':
    unittest.main()
