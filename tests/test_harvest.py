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
RUNNER = os.path.join(REPO_ROOT, 'tools', 'run_tests.py')

from asf import env
from asf.conventions import Conventions
from asf.harvest import harvest
from asf.workers import lifecycle
from asf.workers import observe
from asf.init import ITEM_FOLDERS, STREAM_FOLDERS

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_harvest` does not
    from gitfixture import Template
except ImportError:  # pragma: no cover - import shape only
    from tests.gitfixture import Template

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
    an index, and a state dir with worktrees/ + the session registry. Built once, copied per
    test (B-0071)."""
    base = RECORD_REPO.fresh()
    return (base, os.path.join(base, 'origin.git'), os.path.join(base, 'repo'),
            os.path.join(base, 'state'))


def _build_record_repo(base):
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

    os.makedirs(os.path.join(base, 'state', 'worktrees'))


RECORD_REPO = Template(_build_record_repo, prefix='harvest_test_')


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

        # the test runner's own pid, at a session-less (legacy) run: observed alive only when a
        # source reports it (F-0076 D4/D11) — the real process table never does, since this
        # process is not the runtime binary a real source would match
        fake_source = observe.FakeSource([{'pid': live, 'ppid': 1, 'env': {}}])
        with mock.patch('asf.workers.observe.source_from_config', return_value=fake_source):
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

    @staticmethod
    def build(base):
        origin, repo, worker = (os.path.join(base, n) for n in ('origin.git', 'repo', 'worker'))
        os.makedirs(os.path.join(base, 'state'))
        sh(['git', 'init', '-q', '--bare', '-b', 'main', origin])
        sh(['git', 'clone', '-q', origin, repo])
        git_identity(repo)
        ProductHarvestTests.write(None, repo, 'checks/test_fx.py', GREEN_TEST)
        sh(['git', 'add', '-A'], cwd=repo)
        sh(['git', 'commit', '-qm', 'init'], cwd=repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=repo)
        sh(['git', 'clone', '-q', origin, worker])
        git_identity(worker)

    def setUp(self):
        self.base = PRODUCT_REPOS.fresh()  # built once, copied per test (B-0071)
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.origin = os.path.join(self.base, 'origin.git')
        self.repo = os.path.join(self.base, 'repo')
        self.worker = os.path.join(self.base, 'worker')
        self.state_dir = os.path.join(self.base, 'state')

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

    def harvest(self, product, bug_root=None, timings=False):
        """``(results, lines)``; the gate's own timing lines only with ``timings``."""
        lines = []
        results = harvest.run_product_harvest(product, self.state_dir, bug_root=bug_root,
                                              out=lines.append)
        return results, (lines if timings else [l for l in lines if not l.startswith('gate: ')])

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
        self.assertEqual(lines, ['harvest: 1 branch(es), one gate',
                                 f'landed fix/B-0001 → {sha}',
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
                                 'harvest: 1 branch(es), one gate',
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

    def test_b0079_a_red_session_does_not_hide_the_branch_it_pushed(self):
        # was: `unfinished_or_red_session_is_not_attempted`. The run's verdict is not the test
        # (B-0079) — five branches sat ahead of the trunk for two days while `harvest: none to
        # land` printed every tick, because their sessions had ended `failed: not pushed` with
        # the commits already on origin. The gate and the naming check decide, as for any branch.
        self.push_lane('fix/B-0002', [('fix(B-0002): x', {'x.txt': 'x\n'})])
        self.session('fix-bug-b-0002', 'B-0002', 'fix/B-0002', rc=1)
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0002': 'landed'})

    def test_b0079_a_branch_awaiting_its_correction_is_left_to_that_round(self):
        self.push_lane('fix/B-0004', [('fix(B-0004): x', {'y.txt': 'y\n'})])
        self.session('fix-bug-b-0004', 'B-0004', 'fix/B-0004', rc=1)
        self.record('fix/B-0004')  # the run exists; now give it an unanswered correction
        import json as _json, os as _os
        path = _os.path.join(self.state_dir, 'sessions.jsonl')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(_json.dumps({'job': 'fix-bug-b-0004',
                                 'correction': {'text': 'commit and push what you have',
                                                'at': '2026-09-23T09:00:00Z'}}) + '\n')
        before = self.origin_main()
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {})
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

    def test_a_finished_branch_pushed_straight_to_main_is_marked_landed(self):
        """A finished run whose branch reached main by a direct push is 0 ahead: it is marked
        harvested at main's tip, or its item stays busy and its footprint blocks for ever."""
        self.push_lane('task/T-0001', [('task(T-0001): the change', {'a.txt': 'a\n'})])
        sh(['git', 'push', '-q', 'origin', 'task/T-0001:main'], cwd=self.worker)
        self.session('task-t-0001', 'T-0001', 'task/T-0001')
        results, lines = self.harvest(self.product())
        sha = self.origin_main()
        self.assertEqual(results, {'task/T-0001': 'landed'})
        self.assertIn(f'landed task/T-0001: already on main at {sha[:7]}', lines)
        self.assertEqual(self.record('task/T-0001').get('harvested'), sha)
        self.assertEqual(self.harvest(self.product()), ({}, []))

    def test_a_finished_run_whose_branch_is_gone_is_marked_landed(self):
        self.push_lane('task/T-0001', [('task(T-0001): the change', {'a.txt': 'a\n'})])
        sh(['git', 'push', '-q', 'origin', 'task/T-0001:main'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', '--delete', 'task/T-0001'], cwd=self.worker)
        self.session('task-t-0001', 'T-0001', 'task/T-0001')
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'task/T-0001': 'landed'})
        self.assertEqual(self.record('task/T-0001').get('harvested'), self.origin_main())

    def test_a_live_or_unfinished_run_on_a_merged_branch_is_left_open(self):
        self.push_lane('task/T-0001', [('task(T-0001): the change', {'a.txt': 'a\n'})])
        sh(['git', 'push', '-q', 'origin', 'task/T-0001:main'], cwd=self.worker)
        self.session('task-t-0001', 'T-0001', 'task/T-0001', rc=1)
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {})
        self.assertFalse(self.record('task/T-0001').get('harvested'))

    def test_b0061_content_decides_whatever_the_run_ended_as(self):
        # a run marked by hand "superseded: the work is on main" (or a failed retry) is not
        # finished, so the branch was never looked at: its worktree and branch stayed for ever
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        sh(['git', 'checkout', '-q', 'main'], cwd=self.worker)
        self.write(self.worker, 'a.txt', 'a\n')
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'fix(B-0001): landed another way'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001', rc=1)  # ended, not finished
        self.push_lane('fix/B-0002', [('fix(B-0002): an older approach', {'old.txt': 'old\n'})])
        self.session('fix-bug-b-0002', 'B-0002', 'fix/B-0002', rc=1)
        items = {'B-0002': {'id': 'B-0002', 'type': 'bug', 'state': 'Closed'}}
        lines = []
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append, items=items)
        self.assertEqual(results, {'fix/B-0001': 'landed', 'fix/B-0002': 'superseded'})
        self.assertFalse(self.origin_has('fix/B-0001'))
        self.assertTrue(self.origin_has('archive/fix/B-0002'))
        self.assertTrue(self.record('fix/B-0001').get('harvested'))
        # …and under B-0079 an unfinished run with real work is gated like any other: the branch
        # is what is judged, never the sentence the session wrote about itself
        self.push_lane('fix/B-0003', [('fix(B-0003): more work', {'more.txt': 'more\n'})])
        self.session('fix-bug-b-0003', 'B-0003', 'fix/B-0003', rc=1)
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append, items=items)
        self.assertEqual(results, {'fix/B-0003': 'landed'})

    def test_b0065_a_removed_cards_branch_is_archived_whatever_its_type_and_state(self):
        # the groom marked three cards `removed: superseded …`; items_of() drops removed cards,
        # so harvest saw no card and held their branches for ever
        self.push_lane('fix/B-0001', [('fix(B-0001): older work', {'old.txt': 'old\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        root = os.path.join(self.base, 'record')
        os.makedirs(root)
        with open(os.path.join(root, 'index.json'), 'w') as f:
            json.dump({'items': {'B-0001': {'id': 'B-0001', 'type': 'bug', 'state': 'Active',
                                            'removed': 'superseded: on main — operator ruling'}}}, f)
        items = harvest.record_items(root)
        self.assertEqual(harvest.superseded_by(items, 'B-0001'), 'removed')
        lines = []
        results = harvest.run_product_harvest(self.product(), self.state_dir, out=lines.append, items=items)
        self.assertEqual(results, {'fix/B-0001': 'superseded'})
        self.assertEqual(lines, ['superseded fix/B-0001: B-0001 is removed in the record — archived as archive/fix/B-0001'])
        self.assertTrue(self.origin_has('archive/fix/B-0001'))

    def test_b0067_a_branch_the_registry_knows_is_harvested_whatever_its_prefix(self):
        # eight finished coder branches under task/ — a prefix no convention named — were never
        # looked at: harvest scanned the conventions' prefixes only
        self.push_lane('task/T-0001', [('task(T-0001): the change', {'a.txt': 'a\n'})])
        self.session('coder-t-0001', 'T-0001', 'task/T-0001')
        results, lines = self.harvest(self.product())
        self.assertEqual(results, {'task/T-0001': 'landed'})
        self.assertFalse(self.origin_has('task/T-0001'))
        # a branch on origin the registry does not know, under no prefix, is still not the lane's
        self.push_lane('scratch/x', [('scratch', {'x.txt': 'x\n'})])
        self.assertEqual(self.harvest(self.product()), ({}, []))

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
        # B-0066: the archive ref's tip is an empty [skip ci] commit over the branch's own tip,
        # so the push runs no workflow; the branch's history is intact underneath
        tip = sh(['git', 'rev-parse', 'archive/fix/B-0001'], cwd=self.origin).stdout.strip()
        subject = sh(['git', 'log', '-1', '--format=%s', tip], cwd=self.origin).stdout.strip()
        self.assertTrue(subject.startswith('archive(B-0001): fix/B-0001 — B-0001 is Closed'), subject)
        self.assertTrue(subject.endswith('[skip ci]'), subject)
        self.assertEqual(sh(['git', 'log', '-1', '--format=%s', f'{tip}~1'], cwd=self.origin).stdout.strip(),
                         'fix(B-0001): an older approach')
        self.assertEqual(sh(['git', 'diff', '--stat', f'{tip}~1', tip], cwd=self.origin).stdout.strip(), '')
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
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[0], 'harvest: 1 branch(es), one gate')
        self.assertTrue(lines[1].startswith('held fix/B-0001: FAIL: test_red_gate'), lines)
        self.assertTrue(lines[1].endswith(' — back to its session (round 1)'), lines)
        self.assertEqual(self.origin_main(), before)
        self.assertTrue(self.origin_has('fix/B-0001'))
        self.assertEqual(os.listdir(os.path.join(record_root, 'bugs')), [])  # no Bug filed
        rec = self.record('fix/B-0001')
        self.assertEqual(rec['rounds'], 1)
        self.assertEqual(rec['correction']['kind'], 'gate')
        self.assertIn('test_red_gate', rec['correction']['text'])
        self.assertTrue(rec['correction']['at'])
        self.assertFalse(rec.get('harvested'))
        # the next tick does NOT re-gate it (B-0079): the correction has not been answered, so
        # the branch belongs to the round to come. Re-gating an untouched branch bumped the round
        # with no session having tried anything, and marched the item to adjudication for nothing.
        _results, lines = self.harvest(self.product())
        self.assertEqual(lines, [])
        self.assertEqual(self.record('fix/B-0001')['rounds'], 1)
        # once a session has answered — a new run on the branch — a red gate is round 2
        self.session('correct-b-0001', 'B-0001', 'fix/B-0001')
        _results, lines = self.harvest(self.product())
        self.assertTrue(lines[-1].endswith('(round 2)'), lines)
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
        # each round costs a session: the gate holds, a session answers, the gate holds again.
        # Ticks in between do nothing (B-0079) — an unanswered correction owns the branch.
        for expected_round in (1, 2, 3):
            _results, lines = self.harvest(product)
            self.assertTrue(lines[-1].endswith(f'(round {expected_round})'), lines)
            self.assertEqual(self.record('fix/B-0001')['rounds'], expected_round)
            if expected_round < 3:
                # the quiet tick between rounds: an unanswered correction owns the branch, so
                # nothing is gated until a session has answered it (B-0079)
                self.assertEqual(self.harvest(product)[1], [])
                self.session(f'correct-b-0001-r{expected_round}', 'B-0001', 'fix/B-0001')

        # capped: the round no longer climbs, and the message names the adjudicate row instead
        _results, lines = self.harvest(product)
        self.assertTrue(lines[-1].startswith('held fix/B-0001: FAIL: test_red_gate'), lines)
        self.assertTrue(lines[-1].endswith(' — adjudicate pending'), lines)
        self.assertEqual(self.record('fix/B-0001')['rounds'], 3)
        self.assertFalse(self.record('fix/B-0001').get('operator_flagged'))

        # a second hold at the cap: the adjudicate row's own attempt failed too
        _results, lines = self.harvest(product)
        self.assertTrue(lines[-1].endswith(' — adjudicate pending'), lines)
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

        results, lines = self.harvest(self.product(harvest={'branches_per_tick': 3}))
        self.assertEqual(len(results), 3, results)
        self.assertTrue(all(r == 'landed' for r in results.values()), results)
        self.assertTrue(any('cap' in l.lower() for l in lines), lines)

        remaining = [f'fix/B-000{i}' for i in range(1, 5) if f'fix/B-000{i}' not in results]
        self.assertEqual(len(remaining), 1, remaining)
        self.assertTrue(self.origin_has(remaining[0]))

        # the next tick lands what the cap left waiting
        results2, _lines2 = self.harvest(self.product())
        self.assertEqual(results2, {remaining[0]: 'landed'})

    # -- B-0040: one gate per tick over the combined head, bisecting on red ----------------
    def lanes(self, n, red=()):
        """``n`` fix branches with a finished session each; those in ``red`` break the gate."""
        for i in range(1, n + 1):
            item = f'B-{i:04d}'
            branch = f'fix/{item}'
            files = {f'f{i}.txt': f'{i}\n'}
            if i in red:
                files[f'checks/test_red{i}.py'] = RED_TEST.replace('test_red_gate', f'test_red_{i}')
            self.push_lane(branch, [(f'fix({item}): change {i}', files)])
            self.session(f'fix-bug-{item.lower()}', item, branch)
        return [f'fix/B-{i:04d}' for i in range(1, n + 1)]

    def gated(self):
        """Patch the gate to count its runs (the gate itself still runs)."""
        return mock.patch.object(harvest, 'product_gate', wraps=harvest.product_gate)

    def test_b0040_several_branches_land_in_one_gate(self):
        branches = self.lanes(4)
        with self.gated() as gate:
            results, lines = self.harvest(self.product())
        self.assertEqual(gate.call_count, 1, lines)
        sha = self.origin_main()
        self.assertEqual(results, {b: 'landed' for b in branches})
        self.assertEqual(lines, ['harvest: 4 branch(es), one gate']
                         + [f'landed {b} → {sha}' for b in branches]
                         + [f'harvest: {self.repo} fast-forwarded to {sha}'])
        log = sh(['git', 'log', '--format=%s', 'main'], cwd=self.origin).stdout.splitlines()
        self.assertEqual(log[:4], [f'fix(B-{i:04d}): change {i}' for i in (4, 3, 2, 1)])
        for b in branches:
            self.assertFalse(self.origin_has(b), b)
            self.assertEqual(self.record(b).get('harvested'), sha, b)
        self.assertEqual(self.harvest(self.product()), ({}, []))

    def test_b0040_red_gate_bisects_and_holds_only_the_bad_branch(self):
        branches = self.lanes(4, red=(3,))
        before = self.origin_main()
        with self.gated() as gate:
            results, lines = self.harvest(self.product())
        # all four (red) → [1,2] green, [3,4] red → [3] red, [4] green → 1+2+4 together, green
        self.assertEqual(gate.call_count, 6, lines)
        sha = self.origin_main()
        self.assertNotEqual(sha, before)
        self.assertEqual(results, {'fix/B-0001': 'landed', 'fix/B-0002': 'landed',
                                   'fix/B-0003': 'held', 'fix/B-0004': 'landed'})
        self.assertIn('harvest: 4 branch(es), one gate', lines)
        self.assertIn('harvest: bisecting 4 branches', lines)
        self.assertIn('harvest: bisecting 2 branches', lines)
        held = [l for l in lines if l.startswith('held ')]
        self.assertEqual(len(held), 1, lines)
        self.assertTrue(held[0].startswith('held fix/B-0003: FAIL: test_red_3'), held)
        self.assertTrue(held[0].endswith(' — back to its session (round 1)'), held)
        log = sh(['git', 'log', '--format=%s', 'main'], cwd=self.origin).stdout
        self.assertNotIn('change 3', log)
        for b in ('fix/B-0001', 'fix/B-0002', 'fix/B-0004'):
            self.assertIn(f'landed {b} → {sha}', lines)
            self.assertFalse(self.origin_has(b), b)
            self.assertEqual(self.record(b).get('harvested'), sha, b)
        self.assertTrue(self.origin_has('fix/B-0003'))
        rec = self.record('fix/B-0003')
        self.assertEqual((rec['rounds'], rec['correction']['kind']), (1, 'gate'))
        self.assertIn('test_red_3', rec['correction']['text'])
        self.assertFalse(rec.get('harvested'))

    def test_b0040_every_branch_red_holds_each_with_its_own_line(self):
        self.lanes(2, red=(1, 2))
        before = self.origin_main()
        results, lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0001': 'held', 'fix/B-0002': 'held'})
        self.assertEqual(self.origin_main(), before)
        held = [l for l in lines if l.startswith('held ')]
        self.assertTrue(held[0].startswith('held fix/B-0001: FAIL: test_red_1'), lines)
        self.assertTrue(held[1].startswith('held fix/B-0002: FAIL: test_red_2'), lines)
        self.assertEqual(self.record('fix/B-0001')['correction']['kind'], 'gate')
        self.assertEqual(self.record('fix/B-0002')['correction']['kind'], 'gate')

    def test_b0040_a_conflicting_branch_never_enters_the_set(self):
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'shared.txt': 'one\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        self.push_lane('fix/B-0002', [('fix(B-0002): the other', {'shared.txt': 'two\n'})])
        self.session('fix-bug-b-0002', 'B-0002', 'fix/B-0002')
        self.push_lane('fix/B-0003', [('fix(B-0003): apart', {'apart.txt': 'three\n'})])
        self.session('fix-bug-b-0003', 'B-0003', 'fix/B-0003')
        with self.gated() as gate:
            results, lines = self.harvest(self.product())
        self.assertEqual(gate.call_count, 1, lines)
        sha = self.origin_main()
        self.assertEqual(results, {'fix/B-0001': 'landed', 'fix/B-0002': 'held',
                                   'fix/B-0003': 'landed'})
        self.assertEqual(lines[0], 'held fix/B-0002: conflict in shared.txt — back to its session (round 1)')
        self.assertEqual(lines[1], 'harvest: 2 branch(es), one gate')
        self.assertEqual(self.record('fix/B-0002')['correction']['kind'], 'conflict')
        self.assertTrue(self.origin_has('fix/B-0002'))
        self.assertEqual(self.record('fix/B-0001').get('harvested'), sha)
        self.assertEqual(self.record('fix/B-0003').get('harvested'), sha)

    def test_b0040_trunk_moved_under_the_combined_head_is_restacked_once(self):
        branches = self.lanes(2)
        real_push = harvest.push_ff
        moved = []

        def push_then(repo, sha, trunk='main'):
            if not moved:  # the trunk moves between the gate and the push, once
                moved.append(sha)
                self.write(self.repo, 'other.txt', 'o\n')
                sh(['git', 'add', '-A'], cwd=self.repo)
                sh(['git', 'commit', '-qm', 'another change'], cwd=self.repo)
                sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
            return real_push(repo, sha, trunk)
        with mock.patch.object(harvest, 'push_ff', side_effect=push_then), self.gated() as gate:
            results, lines = self.harvest(self.product())
        self.assertEqual(results, {b: 'landed' for b in branches}, lines)
        self.assertEqual(gate.call_count, 2, lines)
        log = sh(['git', 'log', '--format=%s', 'main'], cwd=self.origin).stdout.splitlines()
        self.assertEqual(log[:3], ['fix(B-0002): change 2', 'fix(B-0001): change 1', 'another change'])

    def test_b0040_dry_run_gates_once_and_pushes_nothing(self):
        branches = self.lanes(3)
        before = self.origin_main()
        lines = []
        with self.gated() as gate:
            results = harvest.run_product_harvest(self.product(), self.state_dir, dry_run=True,
                                                  out=lines.append)
        self.assertEqual(gate.call_count, 1)
        self.assertEqual(results, {b: 'dry' for b in branches})
        self.assertEqual(self.origin_main(), before)
        self.assertTrue(all(l.startswith('DRY: would land ') for l in lines[2:]), lines)
        for b in branches:
            self.assertTrue(self.origin_has(b))
            self.assertFalse(self.record(b).get('harvested'))

    def test_b0040_per_branch_gate_stays_reachable(self):
        branches = self.lanes(3)
        with self.gated() as gate:
            results, lines = self.harvest(self.product(harvest={'gate': 'per-branch'}))
        self.assertEqual(gate.call_count, 3, lines)
        self.assertEqual(results, {b: 'landed' for b in branches})
        self.assertFalse(any('one gate' in l for l in lines), lines)
        shas = {self.record(b).get('harvested') for b in branches}
        self.assertEqual(len(shas), 3, shas)  # one landing each, three pushes
        self.assertIn(self.origin_main(), shas)

    # -- the bisection re-runs only the modules the full gate found red ------------------
    def runner_product(self):
        """A product gated by ``tools/run_tests.py`` over ``checks/`` (made a package on main):
        its ``red:`` line names the red modules and it honours ``ASF_GATE_MODULES``."""
        self.write(self.repo, 'checks/__init__.py', '')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'checks as a package'], cwd=self.repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        return self.product(test_command=f'{sys.executable} {RUNNER} -s checks --shards 2')

    @staticmethod
    def onlys(gate):
        return [c.kwargs.get('only', c.args[4] if len(c.args) > 4 else None)
                for c in gate.call_args_list]

    def test_red_modules_are_read_from_the_runners_last_red_line(self):
        self.assertEqual(harvest.red_modules('red: inner\n...\nFAILED\nred: test_a, test_b\n'),
                         ('test_a', 'test_b'))
        self.assertEqual(harvest.red_modules('red: test_x+test_y'), ('test_x', 'test_y'))
        self.assertEqual(harvest.red_modules('FAILED (failures=1)\n'), ())

    def test_bisection_reruns_only_the_red_modules_and_confirms_the_landing_in_full(self):
        product = self.runner_product()
        self.lanes(4, red=(3,))
        with self.gated() as gate:
            results, lines = self.harvest(product, timings=True)
        red = ('test_red3',)
        # full (red: test_red3) → trunk alone on test_red3 (green) → [1,2] [3,4] [3] [4] on
        # test_red3 only → 1+2+4 stacked again and gated in full before the push
        self.assertEqual(self.onlys(gate), [None, red, red, red, red, red, None], lines)
        self.assertEqual(results, {'fix/B-0001': 'landed', 'fix/B-0002': 'landed',
                                   'fix/B-0003': 'held', 'fix/B-0004': 'landed'})
        timings = [l for l in lines if l.startswith('gate: ')]
        self.assertEqual(len(timings), 7, lines)
        self.assertRegex(timings[0], r'^gate: 2 modules, \d+s, red: test_red3$')
        with open(os.path.join(env.log_dir(), harvest.GATE_RED_LOG), encoding='utf-8') as f:
            kept = f.read()  # why it was red, not only that it was
        self.assertIn('gate red: test_red3 (only: test_red3)', kept)
        self.assertIn('FAIL: test_red_3', kept)
        self.assertRegex(timings[1], r'^gate: 0 modules, \d+s, green$')  # not on the trunk
        self.assertRegex(timings[3], r'^gate: 1 modules, \d+s, red: test_red3$')  # [3,4]
        self.assertRegex(timings[-1], r'^gate: 1 modules, \d+s, green$')
        held = [l for l in lines if l.startswith('held ')]
        self.assertEqual(len(held), 1, lines)
        self.assertTrue(held[0].startswith('held fix/B-0003: '), held)
        self.assertIn('test_red3', held[0])
        sha = self.origin_main()
        for b in ('fix/B-0001', 'fix/B-0002', 'fix/B-0004'):
            self.assertEqual(self.record(b).get('harvested'), sha, b)

    def test_a_branch_red_alone_on_a_test_it_never_wrote_is_its_own_red_not_foreign(self):
        # the trunk is green on the red module and the branch alone turns it red: the red is the
        # branch's even though the output names only a test file outside its diff. Called
        # foreign, it was re-gated and re-bisected every tick and never handed back.
        product = self.runner_product()
        self.write(self.repo, 'value.txt', '1\n')
        self.write(self.repo, 'checks/test_value.py',
                   'import os, unittest\n\nclass V(unittest.TestCase):\n'
                   '    def test_value(self):\n'
                   '        with open(os.path.join(os.path.dirname(__file__), "..", "value.txt")) as f:\n'
                   '            self.assertEqual(f.read(), "1\\n", "checks/test_value.py")\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'a value and its test'], cwd=self.repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        for i, files in ((1, {'f1.txt': '1\n'}), (2, {'value.txt': '2\n'})):
            self.push_lane(f'fix/B-000{i}', [(f'fix(B-000{i}): change {i}', files)])
            self.session(f'fix-bug-b-000{i}', f'B-000{i}', f'fix/B-000{i}')
        results, lines = self.harvest(product)  # the red names checks/test_value.py alone
        self.assertEqual(results, {'fix/B-0001': 'landed', 'fix/B-0002': 'held'}, lines)
        self.assertFalse(any(l.startswith('foreign ') for l in lines), lines)
        self.assertTrue(any(l.startswith('held fix/B-0002: ') and 'test_value' in l
                            for l in lines), lines)
        self.assertTrue((self.record('fix/B-0002').get('correction') or {}).get('text'), lines)

    def test_a_candidate_red_in_full_does_not_land(self):
        product = self.runner_product()
        self.lanes(2, red=(2,))
        before = self.origin_main()
        real = harvest.product_gate
        fulls = []

        def gate(tmp, conv, asf_repo, out=None, only=None):
            ok, line, files, red = real(tmp, conv, asf_repo, out, only)
            if not only:
                fulls.append(ok)
                if len(fulls) > 1:  # every confirmation is red on a module nobody named
                    return False, 'FAIL: test_elsewhere', [], ()
            return ok, line, files, red

        with mock.patch.object(harvest, 'product_gate', side_effect=gate):
            results, lines = self.harvest(product)
        self.assertEqual(self.origin_main(), before, lines)
        self.assertEqual(results['fix/B-0002'], 'held')
        self.assertNotEqual(results.get('fix/B-0001'), 'landed', lines)
        self.assertFalse(self.record('fix/B-0001').get('harvested'))

    def test_red_on_trunk_too_bisects_nothing_and_holds_nothing(self):
        product = self.runner_product()
        self.write(self.repo, 'checks/test_broken.py', RED_TEST)
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'a red trunk'], cwd=self.repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        branches = self.lanes(3)
        before = self.origin_main()
        with self.gated() as gate:
            results, lines = self.harvest(product)
        self.assertEqual(self.onlys(gate), [None, ('test_broken',)], lines)
        self.assertIn('harvest: red on trunk too — test_broken', lines)
        self.assertFalse(any(l.startswith(('held ', 'foreign ', 'harvest: bisecting')) for l in lines),
                         lines)
        self.assertEqual(results, {})
        self.assertEqual(self.origin_main(), before)
        for b in branches:
            self.assertTrue(self.origin_has(b), b)
            rec = self.record(b)
            self.assertFalse(rec.get('rounds') or rec.get('correction'), rec)

    # -- a docs-only branch cannot turn a test red ---------------------------------------
    def plan_lane(self):
        self.push_lane('plan/F-0001', [('plan(F-0001): the tasks',
                                        {'docs/plans/f-0001.md': '# plan\n'})])
        self.session('plan-f-0001', 'F-0001', 'plan/F-0001')
        return 'plan/F-0001'

    def test_a_docs_only_branch_lands_alone_when_the_rest_is_red(self):
        branch = self.plan_lane()
        self.lanes(1, red=(1,))
        with self.gated() as gate:
            results, lines = self.harvest(self.product())
        self.assertEqual(results, {branch: 'landed', 'fix/B-0001': 'held'}, lines)
        self.assertIsNone(gate.call_args_list[0].args[1].test_command)  # the docs ran no test
        self.assertTrue(self.record(branch).get('harvested'))
        self.assertFalse(self.record(branch).get('rounds'))
        self.assertTrue(self.record('fix/B-0001').get('correction'))

    def test_a_docs_only_branch_is_never_held_on_a_red_test_command(self):
        branch = self.plan_lane()
        red = f'{sys.executable} -c "import sys; sys.exit(1)"'
        results, lines = self.harvest(self.product(test_command=red,
                                                   harvest={'gate': 'per-branch'}))
        self.assertEqual(results, {branch: 'landed'}, lines)
        self.assertFalse(self.record(branch).get('rounds'))

    # -- B-0072: a hanging gate is held, not waited for ----------------------------------
    def test_b0072_a_hanging_gate_is_held_with_the_timeout_line_and_its_children_are_gone(self):
        import time
        self.lanes(1)
        mark = os.path.join(self.base, 'grandchild-ran')
        script = os.path.join(self.base, 'hang.py')
        with open(script, 'w', encoding='utf-8') as f:  # a gate that never ends, with a child of its own
            f.write('import subprocess, sys, time\n'
                    f'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2); open({mark!r}, \'w\').close()"])\n'
                    'time.sleep(30)\n')
        hang = f'{sys.executable} {script}'
        before = self.origin_main()
        t0 = time.monotonic()
        results, lines = self.harvest(self.product(test_command=hang, harvest={'gate_timeout_s': 1}))
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(results, {'fix/B-0001': 'timed-out'})
        line = [l for l in lines if l.startswith('gate timed out ')][0]
        self.assertTrue(line.startswith('gate timed out fix/B-0001: gate timed out after 1 s: '), line)
        self.assertTrue(line.endswith(' — retried next tick'), line)
        self.assertEqual(self.origin_main(), before)
        time.sleep(2.5)  # the grandchild would have written its mark by now — its group was killed
        self.assertFalse(os.path.exists(mark))

    def test_b0082_a_gate_that_times_out_leaves_rounds_unchanged(self):
        self.lanes(1)
        hang = f'{sys.executable} -c "import time; time.sleep(30)"'
        for _ in range(2):  # twice over: a clock never climbs toward adjudication
            results, lines = self.harvest(self.product(test_command=hang, harvest={'gate_timeout_s': 1}))
            self.assertEqual(results, {'fix/B-0001': 'timed-out'})
            rec = self.record('fix/B-0001')
            self.assertFalse(rec.get('rounds'))
            self.assertFalse((rec.get('correction') or {}).get('text'))
            self.assertFalse((rec.get('correction') or {}).get('at_cap'))

    def test_record_repo_keeps_its_own_path(self):
        with mock.patch.object(harvest, 'is_record_repo', return_value=True), \
                mock.patch.object(harvest, 'run_harvest', return_value=0) as old:
            self.assertEqual(harvest.run_product_harvest(self.product(), self.state_dir), {})
        old.assert_called_once()
        self.assertEqual(old.call_args[0][0], os.path.abspath(self.repo))


class GateFilesTests(unittest.TestCase):
    OUTPUT = (
        'FAIL: test_x (tests.test_feeder.FeederTests.test_x)\n'
        'Traceback (most recent call last):\n'
        '  File "asf/feeder/rows.py", line 12, in rows\n'
        '  File "tests/test_feeder.py", line 40, in test_x\n'
        'FAIL: test_y (tests.test_feeder.Other.test_y)\n'
        'FAILED (failures=2)\n')

    def test_paths_and_dotted_ids_resolve_dedupe_in_first_seen_order(self):
        self.assertEqual(harvest.gate_files(self.OUTPUT),
                         ['tests/test_feeder.py', 'asf/feeder/rows.py'])

    def test_cap(self):
        text = ' '.join(f'src/m{i}.py' for i in range(30))
        files = harvest.gate_files(text)
        self.assertEqual(len(files), 20)
        self.assertEqual(files[0], 'src/m0.py')
        self.assertEqual(len(harvest.gate_files(text, cap=3)), 3)

    def test_a_bare_verdict_names_no_file(self):
        self.assertEqual(harvest.gate_files('FAILED (failures=1)'), [])
        self.assertEqual(harvest.gate_files(''), [])
        self.assertEqual(harvest.gate_files(None), [])


class ForeignRedTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='foreign_')
        self.addCleanup(shutil.rmtree, self.state, True)
        self.record = {'job': 'code-t-0080', 'item': 'T-0080', 'branch': 'worker/T-0080'}
        self.lines = []

    def hold(self, files, writes, kind='gate', text='FAILED (failures=1)'):
        return harvest.hold_with_correction(self.state, 'worker/T-0080', self.record, kind, text,
                                            self.lines.append, files, writes)

    def registry(self):
        path = harvest.sessions_path(self.state)
        if not os.path.exists(path):
            return ''
        with open(path) as f:
            return f.read()

    def test_red_outside_the_writes_is_foreign_and_costs_no_round(self):
        self.assertEqual(self.hold(['asf/other.py'], ['asf/harvest/harvest.py']), 'foreign')
        self.assertEqual(self.registry(), '')
        self.assertEqual(self.lines, ['foreign worker/T-0080: gate red outside its writes: '
                                      'asf/other.py — re-gated next tick'])

    def test_red_inside_the_writes_holds_as_today(self):
        self.assertEqual(self.hold(['asf/harvest/harvest.py'], ['asf/harvest/*.py']), 'held')
        self.assertIn('"correction"', self.registry())

    def test_no_writes_or_no_files_holds_as_today(self):
        self.assertEqual(self.hold(['asf/other.py'], []), 'held')
        self.assertEqual(self.hold([], ['asf/harvest/harvest.py']), 'held')

    def hold_diff(self, files, touched):
        return harvest.hold_with_correction(self.state, 'plan/F-0039', self.record, 'gate',
                                            'FAILED (failures=1)', self.lines.append, files, (),
                                            touched)

    def test_with_no_writes_the_diff_is_the_footprint(self):
        self.assertEqual(self.hold_diff(['asf/other.py'], ['asf/mine.py']), 'foreign')
        self.assertEqual(self.lines, ['foreign plan/F-0039: gate red outside its diff: '
                                      'asf/other.py — re-gated next tick'])
        self.assertEqual(self.hold_diff(['asf/mine.py'], ['asf/mine.py']), 'held')

    def test_a_docs_only_diff_is_never_held_for_a_red_test(self):
        docs = ['docs/plans/f-0039.md', 'README.md']
        self.assertEqual(self.hold_diff([], docs), 'foreign')  # a bare verdict names no file
        self.assertEqual(self.hold_diff(['tests/test_stop.py'], docs), 'foreign')
        self.assertEqual(self.registry(), '')
        self.assertTrue(all(l.startswith('foreign plan/F-0039: ') for l in self.lines), self.lines)

    def test_a_red_naming_the_doc_itself_holds(self):
        """The checks read documents too: a red that names the branch's own doc is its own."""
        self.assertEqual(self.hold_diff(['docs/plans/f-0039.md'], ['docs/plans/f-0039.md']),
                         'held')

    def test_what_counts_as_docs_only(self):
        conv = harvest.DEFAULTS
        self.assertTrue(harvest.is_inert(conv, ['docs/plans/f-1.md', 'docs/reviews/1-x.md',
                                                'README.md']))
        for files in ([], ['docs/config.example.yaml'], ['asf/briefs/templates/plan.md'],
                      ['plugin/skills/x/SKILL.md'], ['docs/plans/f-1.md', 'asf/x.py']):
            self.assertFalse(harvest.is_inert(conv, files), files)

    def test_only_a_gate_red_can_be_foreign(self):
        self.assertEqual(self.hold(['asf/other.py'], ['asf/harvest/harvest.py'], kind='conflict'),
                         'held')


PRODUCT_REPOS = Template(ProductHarvestTests.build, prefix='harvest_product_')


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


class EveryBranchAheadIsOwned(unittest.TestCase):
    """B-0079: a branch with commits ahead of the trunk that no live run owns is work, whatever
    the session that made it said about itself. Sessions end `failed: empty branch` or
    `failed: not pushed` with their commits already on origin, and those branches were invisible
    to harvest for ever while `harvest: none to land` printed every tick."""

    def test_a_failed_run_does_not_hide_its_branch(self):
        for reason in ('failed: empty branch: nothing to land',
                       'failed: not pushed: 3 uncommitted file(s), 0 unpushed commit(s)',
                       'failed'):
            self.assertTrue(harvest.is_eligible({'ended': 'x', 'end_reason': reason}), reason)

    def test_a_finished_run_is_still_eligible(self):
        self.assertTrue(harvest.is_eligible({'ended': 'x', 'end_reason': 'finished'}))

    def test_a_live_run_is_never_gated(self):
        self.assertFalse(harvest.is_eligible({'started': 'x', 'pid': os.getpid()}))

    def test_a_landed_run_is_not_gated_again(self):
        self.assertFalse(harvest.is_eligible({'ended': 'x', 'harvested': 'abc1234'}))

    def test_the_pr_lane_keeps_its_branch(self):
        self.assertFalse(harvest.is_eligible({'ended': 'x', 'harvest': 'pr'}))

    def test_a_branch_waiting_for_its_correction_is_left_alone(self):
        run = {'ended': 'x', 'end_reason': 'failed: not pushed',
               'correction': {'text': 'commit and push what you have', 'at': '2026-09-23T09:00:00Z'}}
        self.assertFalse(harvest.is_eligible(run))

    def test_an_answered_correction_does_not_hold_the_branch_for_ever(self):
        # the correction text never goes away, so without the registry path a branch held once is
        # skipped for ever — a regression that held three green branches on 2026-09-23
        import json as _json, tempfile as _tf, os as _os
        d = _tf.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = _os.path.join(d, 'sessions.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_json.dumps({'job': 'coder-t-1', 'item': 'T-0001', 'branch': 'worker/T-0001',
                                 'started': '2026-09-23T09:00:00Z', 'pid': 1}) + '\n')
            f.write(_json.dumps({'job': 'coder-t-1', 'ended': '2026-09-23T09:05:00Z',
                                 'correction': {'text': 'push it', 'at': '2026-09-23T09:05:00Z'}}) + '\n')
            f.write(_json.dumps({'job': 'correct-t-1', 'item': 'T-0001', 'branch': 'worker/T-0001',
                                 'started': '2026-09-23T09:10:00Z', 'pid': 2}) + '\n')
            f.write(_json.dumps({'job': 'correct-t-1', 'ended': '2026-09-23T09:20:00Z',
                                 'end_reason': 'finished'}) + '\n')
        held = lifecycle.latest(path)['coder-t-1']
        self.assertFalse(harvest.is_eligible(held), 'without the path it looks pending')
        self.assertTrue(harvest.is_eligible(held, path), 'a later run answered the correction')

    def test_no_record_at_all_is_not_gated(self):
        self.assertFalse(harvest.is_eligible(None))
