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
from asf.evidence import evidence
from asf.harvest import harvest, lane
from asf.workers import host
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

    # -- B-0110: a green gate is not re-run for a trunk move that landed only docs ----------
    def gated(self):
        """Patch the gate to count its runs (the gate itself still runs)."""
        return mock.patch.object(harvest, 'run_gate', wraps=harvest.run_gate)

    def race_push(self, land):
        """Patch ``push_ff`` so its first call lands ``land()``'s commit on origin/main first
        (the trunk moves between the gate and the push, exactly once), then defers to the real
        push — same shape as the B-0040 restack test above, one push_ff down."""
        real_push = harvest.push_ff
        moved = []

        def push_then(repo, sha, trunk='main'):
            if not moved:
                moved.append(sha)
                land()
            return real_push(repo, sha, trunk)
        return mock.patch.object(harvest, 'push_ff', side_effect=push_then)

    def test_b0110_docs_only_trunk_move_lands_without_re_gating(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 't68')
        write_epic(wt, 'E-0007', 'Green branch')
        index_and_commit(wt, 't68: add E-0007')
        write_session(self.state_dir, 't68', branch)

        def land_docs_commit():
            path = os.path.join(self.repo, 'docs', 'specs', 'b-9999.md')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write('a spec, no code\n')
            sh(['git', 'add', '-A'], cwd=self.repo)
            sh(['git', 'commit', '-qm', 'spec(B-9999): a spec, no code'], cwd=self.repo)
            sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)

        with self.race_push(land_docs_commit), self.gated() as gate:
            rc, out = run_harvest(self.repo, self.state_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST OK t68', out)
        self.assertIn('moved on docs only — not gated again', out)
        self.assertEqual(gate.call_count, 1, out)  # the green gate is not re-run

        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        log = sh(['git', 'log', '--format=%s', 'origin/main'], cwd=self.repo).stdout
        self.assertIn('t68: add E-0007', log)
        self.assertIn('spec(B-9999): a spec, no code', log)
        self.assertTrue(harvested(self.state_dir, 't68'))

    def test_b0110_code_trunk_move_still_re_gates(self):
        branch, wt = add_job_worktree(self.repo, self.state_dir, 't86')
        write_epic(wt, 'E-0008', 'Green branch, code moves under it')
        index_and_commit(wt, 't86: add E-0008')
        write_session(self.state_dir, 't86', branch)

        def land_code_commit():
            path = os.path.join(self.repo, 'tools', 'other.txt')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('unrelated tool change\n')
            sh(['git', 'add', '-A'], cwd=self.repo)
            sh(['git', 'commit', '-qm', 'tools: unrelated change'], cwd=self.repo)
            sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)

        with self.race_push(land_code_commit), self.gated() as gate:
            rc, out = run_harvest(self.repo, self.state_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST OK t86', out)
        self.assertNotIn('not gated again', out)
        self.assertEqual(gate.call_count, 2, out)  # code moved under it: re-gated

        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        log = sh(['git', 'log', '--format=%s', 'origin/main'], cwd=self.repo).stdout
        self.assertIn('t86: add E-0008', log)
        self.assertIn('tools: unrelated change', log)
        self.assertTrue(harvested(self.state_dir, 't86'))

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
        # the gate's own behaviour, a branch at a time: no review session stands before it
        conventions.setdefault('lane', {'review': {'code': 'none'}})
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
        return results, (lines if timings else [l for l in lines
                                                if not l.startswith(('gate: ', 'lane: '))])

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
        self.assertEqual(lane.superseded_by(items, 'B-0001'), 'removed')
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

        # capped: the round no longer climbs, and the message names the adjudicate row instead.
        # An unanswered correction owns the branch (BACK) — the adjudicate row's session runs
        # first, and it is that attempt the gate holds
        self.assertEqual(self.harvest(product)[1], [])
        self.session('adjudicate-b-0001', 'B-0001', 'fix/B-0001')
        _results, lines = self.harvest(product)
        self.assertTrue(lines[-1].startswith('held fix/B-0001: FAIL: test_red_gate'), lines)
        self.assertTrue(lines[-1].endswith(' — adjudicate pending'), lines)
        path = harvest.sessions_path(self.state_dir)
        self.assertEqual(lifecycle.rounds_of(path, 'B-0001'), 3)
        self.assertFalse(self.record('fix/B-0001').get('operator_flagged'))

        # a second hold at the cap: the adjudicate row's own attempt failed too
        self.session('adjudicate-b-0001-r2', 'B-0001', 'fix/B-0001')
        _results, lines = self.harvest(product)
        self.assertTrue(lines[-1].endswith(' — adjudicate pending'), lines)
        self.assertEqual(lifecycle.rounds_of(path, 'B-0001'), 3)
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
        self.assertEqual((rec['lane']['state'], rec['lane']['reason']),
                         ('PR_OPEN', 'no PR host: the product lands it'))
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
        # all four (red) → [1,2] green, [3,4] red → [3] red, [4] green → 1+2+4 together, green;
        # and the trunk alone, once, before [3] is blamed (T8: a red trunk is no branch's fault)
        self.assertEqual(gate.call_count, 7, lines)
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
        lines = [l for l in lines if not l.startswith('lane: ')]  # the lane's own dry lines
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

    def test_red_alone_in_a_test_outside_writes_that_imports_a_changed_file_goes_to_widening(self):
        # main is green; the Task's branch changes checks/value.py (its writes:) and the sibling
        # test that imports it goes red. Not foreign, not a round its session cannot pass inside
        # its footprint: held for widen_footprint, the test named as the path it needs.
        product = self.runner_product()
        self.write(self.repo, 'checks/value.py', 'VALUE = 1\n')
        self.write(self.repo, 'checks/test_value.py',
                   'import unittest\nfrom checks.value import VALUE\n\nclass V(unittest.TestCase):\n'
                   '    def test_value(self):\n'
                   '        self.assertEqual(VALUE, 1, "checks/test_value.py")\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'a value and its test'], cwd=self.repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        # a length of its own: a same-size, same-second source reuses the trunk check's .pyc
        self.push_lane('worker/T-0001', [('task(T-0001): the value is 2',
                                          {'checks/value.py': 'VALUE = 2  # the new value\n'})])
        self.session('coder-t-0001', 'T-0001', 'worker/T-0001')
        items = {'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Active',
                            'writes': ['checks/value.py']}}
        lines = []
        results = harvest.run_product_harvest(product, self.state_dir, out=lines.append,
                                              items=items)
        self.assertEqual(results, {'worker/T-0001': 'held'}, lines)
        self.assertFalse(any(l.startswith('foreign ') for l in lines), lines)
        self.assertIn('held worker/T-0001: footprint needs checks/test_value.py', '\n'.join(lines))
        rec = self.record('worker/T-0001')
        self.assertEqual((rec['correction']['kind'], rec['correction']['needs']),
                         ('footprint', ['checks/test_value.py']))
        self.assertFalse(rec.get('rounds'), rec)  # the footprint was the plan's: no round spent

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
        self.assertEqual(results, {b: 'waiting' for b in branches})  # WAITING trunk-red
        self.assertEqual(self.origin_main(), before)
        for b in branches:
            self.assertTrue(self.origin_has(b), b)
            rec = self.record(b)
            self.assertFalse(rec.get('rounds') or rec.get('correction'), rec)

    # -- green alone, red together: they land one at a time, never held all ---------------
    def red_first_full_gate(self):
        """``product_gate`` whose first *full* gate is red on ``test_fx`` (a module that only
        times out under the combined gate's load); every other run is the real gate."""
        real = harvest.product_gate
        fulls = []

        def gate(tmp, conv, asf_repo, out=None, only=None):
            if not only:
                fulls.append(1)
                if len(fulls) == 1:
                    return False, 'ERROR: setUpClass (checks.test_fx.Fx)', [], ('test_fx',)
            return real(tmp, conv, asf_repo, out, only)
        return mock.patch.object(harvest, 'product_gate', side_effect=gate)

    @staticmethod
    def green_alone_holds(lines):
        return [l for l in lines if l.startswith('held ') and 'green alone' in l]

    def test_green_alone_red_together_lands_the_first_and_regates_the_rest(self):
        product = self.runner_product()
        branches = self.lanes(3)
        with self.red_first_full_gate():
            results, lines = self.harvest(product)
        self.assertEqual(results, {b: 'landed' for b in branches}, lines)
        self.assertEqual(self.green_alone_holds(lines), [], lines)
        self.assertIn('harvest: 3 branches green alone, red together — landing fix/B-0001 '
                      'first, the rest re-gate on top of it', lines)
        self.assertIn('harvest: re-gating 2 branch(es) on the new main', lines)
        first = self.record('fix/B-0001').get('harvested')
        rest = {self.record(b).get('harvested') for b in branches[1:]}
        self.assertEqual(rest, {self.origin_main()})
        self.assertNotEqual(first, self.origin_main())  # the first landed on its own, ahead
        log = sh(['git', 'log', '--format=%s', 'main'], cwd=self.origin).stdout.splitlines()
        self.assertEqual(log[:3], [f'fix(B-{i:04d}): change {i}' for i in (3, 2, 1)])

    def test_green_alone_with_no_time_left_lands_the_first_and_the_rest_next_tick(self):
        product = self.runner_product()
        branches = self.lanes(3)
        with self.red_first_full_gate(), mock.patch.object(lane, 'regate', return_value=False):
            results, lines = self.harvest(product)
        self.assertEqual(results, {'fix/B-0001': 'landed', 'fix/B-0002': 'held',
                                   'fix/B-0003': 'held'}, lines)
        self.assertFalse(any('red with the others' in l for l in lines), lines)
        for b in branches[1:]:
            self.assertIn(f'held {b}: green alone, one landed ahead of it — re-gated on the new '
                          f'main next tick', lines)
            rec = self.record(b)
            self.assertFalse(rec.get('rounds') or rec.get('correction'), rec)  # no round spent
        # the next tick gates the rest on top of the first — no green-alone hold again
        results, lines = self.harvest(product)
        self.assertEqual(results, {'fix/B-0002': 'landed', 'fix/B-0003': 'landed'}, lines)
        self.assertEqual(self.green_alone_holds(lines), [], lines)

    def test_a_truly_incompatible_branch_goes_red_alone_on_the_new_main(self):
        product = self.runner_product()
        self.write(self.repo, 'checks/test_all.py',
                   'import os, unittest\n\nclass A(unittest.TestCase):\n'
                   '    def test_not_all_three(self):\n'
                   '        root = os.path.join(os.path.dirname(__file__), "..")\n'
                   '        self.assertFalse(all(os.path.exists(os.path.join(root, f"f{i}.txt"))'
                   ' for i in (1, 2, 3)), "f1+f2+f3 together")\n')
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'no three at once'], cwd=self.repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
        self.lanes(3)
        results, lines = self.harvest(product)
        # {1,2,3} red, each half green → 1 lands; {2,3} on it red, each green → 2 lands; 3 alone
        # on the new main is red: back to its session, the normal way
        self.assertEqual(results, {'fix/B-0001': 'landed', 'fix/B-0002': 'landed',
                                   'fix/B-0003': 'held'}, lines)
        self.assertEqual(self.green_alone_holds(lines), [], lines)
        held = [l for l in lines if l.startswith('held fix/B-0003: ')]
        self.assertEqual(len(held), 1, lines)
        self.assertTrue(held[0].endswith(' — back to its session (round 1)'), held)
        self.assertEqual(self.record('fix/B-0003')['correction']['kind'], 'gate')

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
        # the docs set is gated on its own — by the product gate (R7: the checks read documents)
        self.assertEqual(gate.call_args_list[0].args[1].test_command, PRODUCT_TEST)
        self.assertTrue(self.record(branch).get('harvested'))
        self.assertFalse(self.record(branch).get('rounds'))
        self.assertTrue(self.record('fix/B-0001').get('correction'))

    def test_r7_a_docs_branch_on_a_red_test_command_never_lands_unchecked(self):
        """Fault 1: "docs cannot turn a test red" merged a plan the product gate refused. A docs
        branch is gated like any other; a command red on the trunk alone too is no one's round."""
        branch = self.plan_lane()
        red = f'{sys.executable} -c "import sys; sys.exit(1)"'
        before = self.origin_main()
        results, lines = self.harvest(self.product(test_command=red,
                                                   harvest={'gate': 'per-branch'}))
        self.assertEqual(results, {branch: 'waiting'}, lines)
        self.assertEqual(self.origin_main(), before)
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
        self.assertEqual(results, {'fix/B-0001': 'waiting'})
        line = [l for l in lines if l.startswith('harvest: gate timed out ')][0]
        self.assertTrue(line.startswith('harvest: gate timed out after 1 s: '), line)
        self.assertIn('retried next tick', line)
        self.assertEqual(self.record('fix/B-0001')['lane']['reason'], 'gate-timeout')
        self.assertEqual(self.origin_main(), before)
        time.sleep(2.5)  # the grandchild would have written its mark by now — its group was killed
        self.assertFalse(os.path.exists(mark))

    def test_b0082_a_gate_that_times_out_leaves_rounds_unchanged(self):
        self.lanes(1)
        hang = f'{sys.executable} -c "import time; time.sleep(30)"'
        for _ in range(2):  # twice over: a clock never climbs toward adjudication
            results, lines = self.harvest(self.product(test_command=hang, harvest={'gate_timeout_s': 1}))
            self.assertEqual(results, {'fix/B-0001': 'waiting'})
            rec = self.record('fix/B-0001')
            self.assertFalse(rec.get('rounds'))
            self.assertFalse((rec.get('correction') or {}).get('text'))
            self.assertFalse((rec.get('correction') or {}).get('at_cap'))

    def test_record_repo_keeps_its_own_path(self):
        with mock.patch.object(harvest, 'is_record_repo', return_value=True), \
                mock.patch.object(harvest, 'is_tracked', return_value=True), \
                mock.patch.object(harvest, 'run_harvest', return_value=0) as old:
            self.assertEqual(harvest.run_product_harvest(self.product(), self.state_dir), {})
        old.assert_called_once()
        self.assertEqual(old.call_args[0][0], os.path.abspath(self.repo))

    def test_a_stray_untracked_index_does_not_make_a_product_the_record(self):
        """A product checkout with an untracked ``index.json`` (an index run from the wrong cwd)
        was harvested as the record: every lane branch unseen, "none to land" for ever."""
        self.write(self.repo, 'index.json', '{"items": {}}\n')
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'a.txt': 'a\n'})])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')
        results, _lines = self.harvest(self.product(landing='pull-request'))
        self.assertEqual(results, {'fix/B-0001': 'pr'})

    # ---- the PR lane, native: a docs-only spec/plan branch is merged by harvest ----------

    def pr_product(self, **extra):
        data = {'repo_dir': self.repo, 'repo_slug': 'o/p', 'main': 'main',
                'conventions': {'test_command': PRODUCT_TEST, 'landing': 'pull-request',
                                'specs_dir': 'docs/specs', 'plans_dir': 'docs/plans',
                                'reviews_dir': '.in/reviews'},
                'steps': {'batch': 'bash q.sh'}}
        data.update(extra)
        return env.Product('sample', data)

    def fake_gh(self, checks, number=41, queue=False, auto=False, required=None, trunk=None):
        """A ``gh`` answering ``checks`` (a ``gh pr checks --json`` list, or ``None``: no checks
        reported) for the one open PR ``number``, on the worker's current branch; a merge lands
        that branch on origin's main as the host would. ``queue``: the trunk has a merge queue;
        ``auto``: the PR is already in it; ``required``: the checks the trunk's branch protection
        requires (None: no protection); ``trunk``: ``{check: conclusion}`` every trunk commit's
        completed check runs read as (None: the check-runs read fails, as a host without them).
        Returns the list the calls are recorded in."""
        calls = []
        state = {'merged': None}

        def gh(args):
            calls.append(list(args))
            head = sh(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=self.worker).stdout.strip()
            if args[:1] == ['api'] and '/check-runs' in args[1] and trunk is not None:
                return 0, json.dumps({'check_runs': [
                    {'name': n, 'status': 'completed', 'conclusion': c,
                     'completed_at': '2026-09-25T00:00:00Z'} for n, c in trunk.items()]}), ''
            if args[:1] == ['api'] and args[1].endswith('/required_status_checks'):
                if required is None:
                    return 1, '', 'gh: Branch not protected (HTTP 404)\n'
                return 0, json.dumps({'contexts': list(required), 'checks': []}), ''
            if args[:2] == ['pr', 'list'] and 'merged' in args:
                return 0, json.dumps([{'number': number, 'mergeCommit': {'oid': state['merged']}}]
                                     if state['merged'] else []), ''
            if args[:2] == ['pr', 'list'] and 'all' in args:  # the lane's one read of the host
                if state['merged']:
                    return 0, json.dumps([{'number': number, 'headRefName': head,
                                           'state': 'MERGED', 'headRefOid': None,
                                           'mergeCommit': {'oid': state['merged']}}]), ''
                return 0, json.dumps([{'number': number, 'headRefName': head, 'state': 'OPEN',
                                       'autoMergeRequest': {'enabledAt': 'x'} if auto else None}]), ''
            if args[:2] == ['pr', 'list']:
                return 0, json.dumps([] if state['merged'] else [
                    {'number': number, 'headRefName': head,
                     'autoMergeRequest': {'enabledAt': 'x'} if auto else None}]), ''
            if args[:2] == ['api', 'graphql']:
                return 0, json.dumps({'data': {'repository': {
                    'mergeQueue': {'id': 'MQ'} if queue else None}}}), ''
            if args[:2] == ['pr', 'checks']:
                if checks is None:
                    return 1, '', "no checks reported on the 'x' branch\n"
                return (1 if any(c['bucket'] != 'pass' for c in checks) else 0), json.dumps(checks), ''
            if args[:2] == ['pr', 'merge'] and '--auto' in args:
                return 0, '', ''
            if args[:2] == ['pr', 'merge']:
                sh(['git', 'push', '-q', 'origin', f'{head}:main'], cwd=self.worker)
                sh(['git', 'push', '-q', 'origin', '--delete', head], cwd=self.worker)
                state['merged'] = self.origin_main()
                return 0, '', ''
            if args[:2] == ['pr', 'view']:
                return 0, (state['merged'] or '') + '\n', ''
            return 1, '', f'unexpected gh {args}'
        patcher = mock.patch.object(harvest, '_gh', side_effect=gh)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def fake_gh_prs(self, prs, required=None):
        """A ``gh`` answering for several open PRs at once: ``prs`` maps a branch to its
        ``(number, checks)``. :meth:`fake_gh` keys its one PR off the worker's current branch and
        so cannot hold two; this one keys off the branch names. A merge lands that branch on
        origin's main as the host would. Returns the list the calls are recorded in."""
        calls, merged = [], {}
        by_number = {str(n): (b, checks) for b, (n, checks) in prs.items()}

        def gh(args):
            calls.append(list(args))
            if args[:1] == ['api'] and args[1].endswith('/required_status_checks'):
                if required is None:
                    return 1, '', 'gh: Branch not protected (HTTP 404)\n'
                return 0, json.dumps({'contexts': list(required), 'checks': []}), ''
            if args[:2] == ['api', 'graphql']:
                return 0, json.dumps({'data': {'repository': {'mergeQueue': None}}}), ''
            if args[:2] == ['pr', 'list']:
                return 0, json.dumps([
                    {'number': n, 'headRefName': b, 'state': 'MERGED' if b in merged else 'OPEN',
                     'headRefOid': None, 'autoMergeRequest': None,
                     'mergeCommit': {'oid': merged[b]} if b in merged else None}
                    for b, (n, _checks) in prs.items()]), ''
            if args[:2] == ['pr', 'checks']:
                checks = by_number.get(str(args[2]), (None, []))[1]
                return (1 if any(c['bucket'] != 'pass' for c in checks) else 0,
                        json.dumps(checks), '')
            if args[:2] == ['pr', 'merge']:
                b = by_number[str(args[2])][0]
                sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)
                sh(['git', 'push', '-q', 'origin', f'origin/{b}:main'], cwd=self.worker)
                sh(['git', 'push', '-q', 'origin', '--delete', b], cwd=self.worker)
                merged[b] = self.origin_main()
                return 0, '', ''
            if args[:2] == ['pr', 'view']:
                b = by_number.get(str(args[2]), (None, []))[0]
                return 0, (merged.get(b) or '') + '\n', ''
            return 1, '', f'unexpected gh {args}'
        patcher = mock.patch.object(harvest, '_gh', side_effect=gh)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def push_plan(self, files=None):
        self.push_lane('plan/F-0001', [('plan(F-0001): the plan',
                                        files or {'docs/plans/f-0001.md': '# plan\n'})])
        self.session('plan-f-0001', 'F-0001', 'plan/F-0001')

    def merges(self, calls):
        return [c for c in calls if c[:2] == ['pr', 'merge']]

    def test_docs_only_plan_branch_with_green_checks_is_merged_and_harvested(self):
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}])
        self.push_plan({'docs/plans/f-0001.md': '# plan\n', '.in/reviews/1-f-0001.md': 'ok\n'})
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'plan/F-0001': 'landed'})
        self.assertEqual(self.merges(calls),
                         [['pr', 'merge', '41', '-R', 'o/p', '--squash', '--delete-branch',
                           '--subject', 'plan(F-0001): the plan (#41)']])
        sha = self.origin_main()
        self.assertIn(f'landed plan/F-0001 → PR #41 {sha}', lines)
        self.assertEqual(self.record('plan/F-0001').get('harvested'), sha)
        self.assertNotEqual(self.record('plan/F-0001').get('harvest'), 'pr')
        self.assertEqual(self.harvest(self.pr_product())[0], {})  # harvested once

    def test_native_landing_writes_the_doc_lane_squash_subject(self):
        """B-0114: the host composes a squash subject from the PR title (`F-0001 — …`), which
        the evidence reads as the Feature landing. A spec/plan lane names its kind at merge,
        whatever the branch's own subjects say; a code lane keeps the host's subject."""
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}])
        self.push_lane('plan/F-0001', [('docs: F-0001 — the plan, cut against head',
                                        {'docs/plans/f-0001.md': '# plan\n'})])
        self.session('plan-f-0001', 'F-0001', 'plan/F-0001')
        self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'landed'})
        merge = self.merges(calls)[0]
        subject = merge[merge.index('--subject') + 1]
        self.assertTrue(evidence.DOC_LANE_SUBJECT.match(subject), subject)
        self.assertEqual(subject,
                         'plan(F-0001): docs: F-0001 — the plan, cut against head (#41)')

    def test_a_gh_that_does_not_know_subject_still_lands_the_doc_lane(self):
        """B-0114: --subject is what makes the trunk subject readable, but a gh too old for the
        flag must degrade to a plain merge, not stall the lane — the evidence still reads a
        document lane by its paths."""
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}])
        inner = harvest._gh.side_effect

        def gh(args):
            if args[:2] == ['pr', 'merge'] and '--subject' in args:
                calls.append(list(args))
                return 1, '', 'unknown flag: --subject\n'
            return inner(args)
        harvest._gh.side_effect = gh
        self.push_plan()
        self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'landed'})
        tried = self.merges(calls)
        self.assertIn('--subject', tried[0])           # it asks for the subject first
        self.assertNotIn('--subject', tried[1])        # then lands without it, same method
        self.assertEqual(tried[0][5], tried[1][5])

    def with_pr_runs(self, calls, runs):
        """Wrap the installed ``gh`` fake so ``gh run list`` answers ``runs`` (None: the list
        fails) and ``gh run cancel`` succeeds; both calls land in ``calls``."""
        inner = harvest._gh.side_effect

        def gh(args):
            if args[:2] == ['run', 'list']:
                calls.append(list(args))
                return (1, '', 'HTTP 502\n') if runs is None else (0, json.dumps(runs), '')
            if args[:2] == ['run', 'cancel']:
                calls.append(list(args))
                return 0, '', ''
            return inner(args)
        harvest._gh.side_effect = gh

    def test_a_merged_pr_cancels_its_ci_runs_still_going(self):
        """A PR lands on its required checks alone; the rest of its matrix still queued on the
        runner pool is moot once it merged (the trunk run judges it again), so the lane cancels
        the runs not yet completed — and only those, only for that branch's PR runs."""
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'pass'},
                              {'name': 'e2e', 'bucket': 'pending'}], required=['gate'])
        self.with_pr_runs(calls, [{'databaseId': 7, 'status': 'queued'},
                                  {'databaseId': 8, 'status': 'in_progress'},
                                  {'databaseId': 6, 'status': 'completed'}])
        self.push_plan()
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'plan/F-0001': 'landed'})
        listed = [c for c in calls if c[:2] == ['run', 'list']]
        self.assertEqual(len(listed), 1)
        self.assertIn('plan/F-0001', listed[0])
        self.assertIn('pull_request', listed[0])
        self.assertEqual([c[2] for c in calls if c[:2] == ['run', 'cancel']], ['7', '8'])
        self.assertIn('harvest: plan/F-0001: cancelled 2 CI run(s) of the merged PR #41 — the '
                      'trunk run judges it now', lines)

    def test_no_run_is_cancelled_before_the_merge_or_when_the_list_fails(self):
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'pending'}], required=['gate'])
        self.with_pr_runs(calls, [{'databaseId': 7, 'status': 'queued'}])
        self.push_plan()
        self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'waiting'})
        self.assertEqual([c for c in calls if c[:1] == ['run']], [])  # waiting: CI still judges
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}])
        self.with_pr_runs(calls, None)
        self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'landed'})
        self.assertEqual([c for c in calls if c[:2] == ['run', 'cancel']], [])

    def test_no_checks_at_all_counts_as_green(self):
        calls = self.fake_gh(None)
        self.push_plan()
        self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'landed'})
        self.assertEqual(len(self.merges(calls)), 1)

    def test_squash_refused_falls_back_to_an_allowed_method(self):
        calls = self.fake_gh([])
        inner = harvest._gh.side_effect

        def gh(args):
            if args[:2] == ['pr', 'merge'] and '--squash' in args:
                calls.append(list(args))
                return 1, '', 'Squash merges are not allowed on this repository\n'
            return inner(args)
        harvest._gh.side_effect = gh
        self.push_plan()
        self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'landed'})
        self.assertEqual([c[5] for c in self.merges(calls)], ['--squash', '--merge'])

    def test_a_pending_remote_run_shows_its_age_across_ticks(self):
        """A remote run queued behind a saturated runner pool is not a fresh wait every tick:
        the lane keeps when the PR started waiting on it and names the minutes, without a
        transition line or a record rewrite per tick."""
        self.fake_gh([{'name': 'gate', 'bucket': 'pending'}], required=['gate'])
        self.push_plan()
        with mock.patch.object(lane.time, 'time', return_value=1_000_000.0):
            self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'waiting'})
        with mock.patch.object(lane.time, 'time', return_value=1_000_000.0 + 63 * 60):
            results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'plan/F-0001': 'waiting'})
        self.assertEqual(lines, ['waiting plan/F-0001: PR #41 checks pending — gate (63 min)'])

    def test_docs_only_pending_checks_wait_for_the_next_tick(self):
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}, {'name': 'ci', 'bucket': 'pending'}])
        self.push_plan()
        before = self.origin_main()
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'plan/F-0001': 'waiting'})
        self.assertEqual(lines, ['waiting plan/F-0001: PR #41 checks pending — ci (0 min)'])
        self.assertEqual(self.merges(calls), [])
        self.assertEqual(self.origin_main(), before)
        rec = self.record('plan/F-0001')
        self.assertFalse(rec.get('harvested'))
        self.assertNotEqual(rec.get('harvest'), 'pr')  # looked at again next tick
        self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'waiting'})

    def test_docs_only_red_checks_hold_and_go_back_to_the_session(self):
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'fail'}])
        self.push_plan()
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'plan/F-0001': 'held'})
        self.assertEqual(lines, ['held plan/F-0001: the plan turns the gate red on main — PR #41 '
                                 'checks red: DCO. Change the plan so the product gate passes on '
                                 'main; nothing is merged until it does — back to its session '
                                 '(round 1)'])
        self.assertEqual(self.merges(calls), [])
        rec = self.record('plan/F-0001')
        self.assertEqual(rec['correction']['kind'], lane.LANDING_GATE)
        self.assertEqual(rec['rounds'], 1)
        self.assertFalse(rec.get('harvested'))

    def test_docs_only_human_now_merge_is_held_for_the_operator(self):
        calls = self.fake_gh([])
        product = self.pr_product(approvals={'merge_routine_pr': 'human-now'})
        os.makedirs(env.state_dir(product), exist_ok=True)
        self.push_plan()
        results, lines = self.harvest(product)
        self.assertEqual(results, {'plan/F-0001': 'held'})
        self.assertEqual(lines, ['held plan/F-0001: merge_routine_pr (human-now) — routine'])
        self.assertEqual(self.merges(calls), [])
        from asf import approvals
        self.assertEqual([h['class'] for h in approvals.open_holds(product)
                          if h['item'] == 'F-0001'], ['merge_routine_pr'])
        out = []
        approvals.raise_holds(mock.Mock(product=product), out.append)
        self.assertTrue(any(l.startswith('NEEDS OPERATOR: held merge_routine_pr on F-0001')
                            for l in out), out)

    def test_a_plan_branch_touching_code_needs_a_review_like_code(self):
        calls = self.fake_gh([])
        self.push_plan({'docs/plans/f-0001.md': '# plan\n', 'src/app.py': 'x = 1\n'})
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'plan/F-0001': 'waiting'})
        self.assertEqual(lines, ['waiting plan/F-0001: PR #41 not approved — no ASF review yet: '
                                 'review round 1 asked for'])
        self.assertEqual(self.merges(calls), [])

    def test_no_pr_host_keeps_the_old_pr_lane(self):
        self.push_plan()
        product = self.pr_product(repo_slug=None)
        with mock.patch.object(harvest, '_gh') as gh:
            results, lines = self.harvest(product)
        self.assertEqual(results, {'plan/F-0001': 'pr'})
        self.assertEqual(lines, ['pr-lane plan/F-0001'])
        gh.assert_not_called()

    # ---- the PR lane, native: a code branch needs the ASF review's approval --------------

    REVIEW = ('| check | result | evidence |\n| --- | --- | --- |\n| scope | pass | a.txt |\n\n'
              'verdict: {v}\n')

    def push_fix(self, verdicts=(), extra=None):
        """``fix/B-0001`` with a change and one review file per verdict (round 1, 2, …)."""
        files = {'src/a.py': 'a = 1\n'}
        files.update({f'.in/reviews/{n}-b-0001.md': self.REVIEW.format(v=v)
                      for n, v in enumerate(verdicts, 1)})
        files.update(extra or {})
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', files)])
        self.session('fix-bug-b-0001', 'B-0001', 'fix/B-0001')

    def lane_rows_now(self, items):
        from asf.feeder import rows as feeder_rows
        occ = lifecycle.occupancy(harvest.sessions_path(self.state_dir))
        return [r for r in feeder_rows.candidates(items, self.pr_product(), [], occupancy=occ)
                if r.launches]

    def feeder_rows_now(self, items):
        from asf.feeder import rows as feeder_rows
        corrections = lifecycle.corrections(harvest.sessions_path(self.state_dir))
        return feeder_rows.correction_rows(items, self.pr_product(), set(), corrections)[0]

    BUG = {'B-0001': {'id': 'B-0001', 'type': 'bug', 'state': 'Active', 'severity': 'S1'}}

    def test_code_branch_without_a_review_asks_for_one(self):
        """In pull-request landing nothing else launches a review: harvest asks for one, and the
        feeder launches it on the PR's branch — no round spent."""
        from asf.feeder import rows as feeder_rows
        calls = self.fake_gh([{'name': 'ci', 'bucket': 'pass'}])
        self.push_fix()
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'waiting'})
        self.assertEqual(lines, ['waiting fix/B-0001: PR #41 not approved — no ASF review yet: '
                                 'review round 1 asked for'])
        self.assertEqual(self.merges(calls), [])
        rec = self.record('fix/B-0001')
        self.assertEqual((rec['lane']['state'], rec['lane']['round']), ('REVIEW', 1))
        self.assertFalse(rec.get('correction'))
        self.assertFalse(rec.get('rounds'))
        rows = self.lane_rows_now(self.BUG)
        self.assertEqual([(r.kind, r.brief_kind, r.branch, r.review_round, r.tier) for r in rows],
                         [(feeder_rows.PUSHED_REVIEW, 'review', 'fix/B-0001', 1, 0)])
        # asked once: until a review session runs, the branch is the request's, not the gate's
        self.assertEqual(self.harvest(self.pr_product())[0], {})
        # the brief points the reviewer at round 1's file on that branch
        from asf.briefs import preamble
        facts = preamble.collect(self.pr_product(), rows[0], {'items': self.BUG})
        self.assertEqual(facts['review_path'], '.in/reviews/1-b-0001.md')

    def test_the_asked_review_approves_and_the_next_harvest_merges(self):
        calls = self.fake_gh([{'name': 'ci', 'bucket': 'pass'}])
        self.push_fix()
        self.assertEqual(self.harvest(self.pr_product())[0], {'fix/B-0001': 'waiting'})
        # the review session: its verdict file lands on the branch
        sh(['git', 'checkout', '-q', 'fix/B-0001'], cwd=self.worker)
        self.write(self.worker, '.in/reviews/1-b-0001.md', self.REVIEW.format(v='approved'))
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'review(B-0001): round 1'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'fix/B-0001'], cwd=self.worker)
        from asf.workers.pool import now_iso
        with open(os.path.join(self.state_dir, 'sessions.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'job': 'review-b-0001', 'item': 'B-0001', 'branch': 'fix/B-0001',
                                'account': 'test', 'pid': dead_pid(), 'started': now_iso()}) + '\n')
            f.write(json.dumps({'job': 'review-b-0001', 'ended': now_iso(),
                                'end_reason': 'finished', 'rc': 0}) + '\n')
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'landed'}, lines)
        self.assertIn('harvest: 1 branch(es), one gate', lines)
        self.assertEqual(len(self.merges(calls)), 1)

    def test_a_review_older_than_the_head_asks_for_the_next_round(self):
        calls = self.fake_gh([])
        self.push_fix(['approved'])
        sh(['git', 'checkout', '-q', 'fix/B-0001'], cwd=self.worker)
        self.write(self.worker, 'src/a.py', 'a = 2\n')
        sh(['git', 'commit', '-qam', 'fix(B-0001): after the review'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'fix/B-0001'], cwd=self.worker)
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'waiting'})
        self.assertEqual(lines, ['waiting fix/B-0001: PR #41 not approved — '
                                 '.in/reviews/1-b-0001.md predates the head: review round 2 '
                                 'asked for'])
        self.assertEqual(self.merges(calls), [])
        self.assertEqual(self.record('fix/B-0001')['lane']['round'], 2)

    def test_s1_reviews_go_first(self):
        from asf.feeder import rows as feeder_rows
        product = self.pr_product()
        review = {'T-0002': {'round': 1, 'branch': 'worker/T-0002'},
                  'B-0001': {'round': 1, 'branch': 'fix/B-0001'},
                  'B-0003': {'round': 1, 'branch': 'fix/B-0003'}}
        items = {'T-0002': {'id': 'T-0002', 'type': 'task', 'state': 'Active'},
                 'B-0001': dict(self.BUG['B-0001']),
                 'B-0003': {'id': 'B-0003', 'type': 'bug', 'state': 'Active'}}
        rows = feeder_rows.lane_rows(items, product, set(), {'review': review})
        tiers = {r.item_id: r.tier for r in rows}
        self.assertEqual(tiers, {'B-0001': 0, 'B-0003': 1, 'T-0002': 2})

    # ---- the feeder reads the open PRs, not the ledger ------------------------------------

    OPEN_PR = [{'number': 41, 'headRefName': 'fix/B-0001', 'state': 'OPEN'},
               {'number': 40, 'headRefName': 'spec/F-0001', 'state': 'OPEN'},
               {'number': 39, 'headRefName': 'fix/B-0009', 'state': 'MERGED'}]
    NEW_BUG = {'B-0001': {'id': 'B-0001', 'type': 'bug', 'state': 'New', 'severity': 'S1',
                          'decided': True}}

    def plan_now(self, items):
        """The feeder's rows as the wave plans them: the in-process lane pass first (it adopts a
        PR no run holds, and reads each head's review), then the one occupancy answer."""
        from asf.feeder import rows as feeder_rows
        path = harvest.sessions_path(self.state_dir)
        self.fake_gh([{'name': 'ci', 'bucket': 'pass'}])
        lane.lane_pass(self.pr_product(), self.state_dir, items=items, out=lambda _l: None)
        return feeder_rows.candidates(items, self.pr_product(), lifecycle.inflight(path),
                                      occupancy=lifecycle.occupancy(path))

    def test_a_pre_existing_pr_with_no_ledger_mark_gets_a_review_row(self):
        """A PR opened before any review request existed — no harvest mark, no correction, no
        session at all — still gets its review, S1 first; the Bug gets no second fix."""
        from asf.feeder import rows as feeder_rows
        self.push_lane('fix/B-0001', [('fix(B-0001): the change', {'src/a.py': 'a = 1\n'})])
        rows = self.plan_now(self.NEW_BUG)
        self.assertEqual([(r.kind, r.item_id, r.branch, r.review_round, r.tier, r.launches)
                          for r in rows],
                         [(feeder_rows.PUSHED_REVIEW, 'B-0001', 'fix/B-0001', 1, 0, True)])

    def test_a_bug_whose_finished_fix_has_an_open_pr_gets_a_review_not_a_fix(self):
        from asf.feeder import rows as feeder_rows
        self.push_fix()  # its fix-bug run finished and pushed; nothing harvested it yet
        rows = self.plan_now(self.NEW_BUG)
        self.assertNotIn(feeder_rows.BUG_FIX, [r.kind for r in rows])
        self.assertEqual([(r.kind, r.item_id) for r in rows],
                         [(feeder_rows.PUSHED_REVIEW, 'B-0001')])

    def test_a_pr_approved_at_its_head_waits_to_land(self):
        from asf.feeder import rows as feeder_rows
        self.push_fix(['approved'])
        rows = self.plan_now(self.NEW_BUG)
        self.assertEqual([(r.kind, r.launches) for r in rows], [(feeder_rows.PUSHED_LAND, False)])
        self.assertTrue(rows[0].action.startswith(feeder_rows.WAITS_LANDING))

    def test_a_review_older_than_the_head_wants_the_next_round(self):
        self.push_fix(['approved'])
        sh(['git', 'checkout', '-q', 'fix/B-0001'], cwd=self.worker)
        self.write(self.worker, 'src/a.py', 'a = 2\n')
        sh(['git', 'commit', '-qam', 'fix(B-0001): after the review'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'fix/B-0001'], cwd=self.worker)
        rows = self.plan_now(self.NEW_BUG)
        self.assertEqual([(r.item_id, r.review_round) for r in rows], [('B-0001', 2)])

    def test_code_branch_whose_newest_review_requests_changes_goes_back_as_a_correction(self):
        from asf.feeder import rows as feeder_rows
        calls = self.fake_gh([])
        self.push_fix(['approved', 'changes requested'])
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertEqual(lines, ['held fix/B-0001: .in/reviews/2-b-0001.md reads changes '
                                 'requested: answer its C list on fix/B-0001 — back to its '
                                 'session (round 1)'])
        self.assertEqual(self.merges(calls), [])
        rows = self.feeder_rows_now(self.BUG)
        self.assertEqual([(r.kind, r.brief_kind, r.branch) for r in rows],
                         [(feeder_rows.FIX_CORRECT, 'correct', 'fix/B-0001')])
        self.assertIn('reads changes requested', rows[0].correction)

    def test_code_branch_approved_and_green_is_merged(self):
        calls = self.fake_gh([{'name': 'ci', 'bucket': 'pass'}])
        self.push_fix(['changes requested', 'approved'])
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        self.assertEqual(self.merges(calls),
                         [['pr', 'merge', '41', '-R', 'o/p', '--squash', '--delete-branch']])
        sha = self.origin_main()
        self.assertIn(f'landed fix/B-0001 → PR #41 {sha}', lines)
        self.assertEqual(self.record('fix/B-0001').get('harvested'), sha)

    def test_code_branch_approved_but_red_goes_back_to_its_session(self):
        calls = self.fake_gh([{'name': 'ci', 'bucket': 'fail'}])
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertEqual(lines, ['held fix/B-0001: PR #41 checks red: ci — back to its session (round 1)'])
        self.assertEqual(self.merges(calls), [])
        self.assertEqual(self.record('fix/B-0001')['correction']['kind'], 'gate')

    def test_merge_queue_enqueues_with_auto_once(self):
        calls = self.fake_gh([], queue=True)
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'queued'})
        self.assertEqual(self.merges(calls), [['pr', 'merge', '41', '-R', 'o/p', '--auto']])
        self.assertEqual(lines[-1], 'queued fix/B-0001: PR #41 added to the merge queue')
        self.assertFalse(self.record('fix/B-0001').get('harvested'))
        self.assertEqual(self.record('fix/B-0001')['lane']['state'], 'QUEUED')
        # in the queue: the next tick does not enqueue it again (QUEUED until the host merges it)
        calls = self.fake_gh([], queue=True, auto=True)
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {})
        self.assertEqual(self.merges(calls), [])
        self.assertEqual(self.record('fix/B-0001')['lane']['state'], 'QUEUED')

    def test_merge_queue_respects_capacity_parallel(self):
        calls = self.fake_gh([], queue=True)
        self.push_fix(['approved'])
        product = self.pr_product(capacity={'batch': {'parallel': 0}})
        results, lines = self.harvest(product)
        self.assertEqual(results, {'fix/B-0001': 'waiting'})
        self.assertEqual(self.merges(calls), [])
        self.assertIn('capacity.batch.parallel', lines[-1])

    def test_capacity_per_run_caps_merges_per_tick(self):
        calls = self.fake_gh([])
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_product(capacity={'batch': {'per_run': 0}}))
        self.assertEqual(results, {'fix/B-0001': 'waiting'})
        self.assertEqual(self.merges(calls), [])
        self.assertIn('capacity.batch.per_run', lines[-1])

    def test_code_branch_human_now_is_held_for_the_operator(self):
        calls = self.fake_gh([])
        product = self.pr_product(approvals={'merge_routine_pr': 'human-now'})
        os.makedirs(env.state_dir(product), exist_ok=True)
        self.push_fix(['approved'])
        results, lines = self.harvest(product)
        self.assertEqual(results, {'fix/B-0001': 'held'})
        self.assertEqual(lines, ['held fix/B-0001: merge_routine_pr (human-now) — routine'])
        self.assertEqual(self.merges(calls), [])
        out = []
        from asf import approvals
        approvals.raise_holds(mock.Mock(product=product), out.append)
        self.assertTrue(any(l.startswith('NEEDS OPERATOR: held merge_routine_pr on B-0001')
                            for l in out), out)

    def test_a_pr_merged_elsewhere_closes_its_session(self):
        """Merged by the product's batch step or a person: the PR is no longer open, the branch
        is still on origin — the session is closed at the merge commit."""
        calls = []

        def gh(args):
            calls.append(list(args))
            if args[:2] == ['pr', 'list'] and 'all' in args:
                return 0, json.dumps([{'number': 41, 'headRefName': 'fix/B-0001',
                                       'state': 'MERGED', 'mergeCommit': {'oid': 'abc123'}}]), ''
            return 0, '[]', ''
        patcher = mock.patch.object(harvest, '_gh', side_effect=gh)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.push_fix()
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'landed'})
        self.assertIn('landed fix/B-0001 → PR #41 abc123 (merged)', lines)
        self.assertEqual(self.record('fix/B-0001').get('harvested'), 'abc123')
        self.assertEqual(self.merges(calls), [])

    def test_a_dry_run_writes_no_hold(self):
        self.fake_gh([{'name': 'ci', 'bucket': 'fail'}])
        self.push_lane('fix/B-0002', [('the change, unnamed', {'src/b.py': 'b = 1\n'})])
        self.session('fix-bug-b-0002', 'B-0002', 'fix/B-0002')
        self.push_fix(['approved'])
        lines = []
        results = harvest.run_product_harvest(self.pr_product(), self.state_dir, dry_run=True,
                                              out=lines.append)
        self.assertEqual(results, {'fix/B-0001': 'dry', 'fix/B-0002': 'dry'})
        self.assertFalse(self.record('fix/B-0001').get('correction'))
        self.assertFalse(self.record('fix/B-0002').get('correction'))

    def test_pr_order_puts_hotfix_then_s1_first(self):
        items = {'B-0001': {'severity': 'S2'}, 'B-0002': {'severity': 'S1'}}
        entries = [('worker/F-0009', {}), ('fix/B-0001', {}), ('fix/B-0002', {}),
                   ('worker/hotfix-T-0003', {})]
        entries = [{'branch': b, 'item': lane.item_of(b, r)} for b, r in entries]
        self.assertEqual([f['branch'] for f in lane.pr_order(entries, items)],
                         ['worker/hotfix-T-0003', 'fix/B-0002', 'fix/B-0001', 'worker/F-0009'])

    # ---- the PR lane, native: green checks are not a green trunk ----------------------------

    #: A trunk-only rule, as a product's CI runs it on the trunk alone: no plan may cite a name
    #: the product does not know.
    TRUNK_RULE = ("import glob, unittest\n\nclass Rule(unittest.TestCase):\n"
                  "    def test_plans_cite_known_names(self):\n"
                  "        for p in glob.glob('docs/plans/*.md'):\n"
                  "            self.assertNotIn('unknown-name', open(p).read(), p)\n")

    def add_trunk_rule(self):
        sh(['git', 'checkout', '-q', '-B', 'rule', 'origin/main'], cwd=self.worker)
        self.write(self.worker, 'checks/test_rule.py', self.TRUNK_RULE)
        sh(['git', 'add', '-A'], cwd=self.worker)
        sh(['git', 'commit', '-qm', 'the trunk rule'], cwd=self.worker)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.worker)

    def pr_conv(self, **extra):
        conv = {'test_command': PRODUCT_TEST, 'landing': 'pull-request',
                'specs_dir': 'docs/specs', 'plans_dir': 'docs/plans', 'reviews_dir': '.in/reviews'}
        conv.update(extra)
        return self.pr_product(conventions=conv)

    def test_a_docs_pr_green_on_a_trivial_check_but_red_on_the_local_gate_is_sent_back(self):
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}])
        self.add_trunk_rule()
        before = self.origin_main()
        self.push_plan({'docs/plans/f-0001.md': '# plan\ncites unknown-name\n'})
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'plan/F-0001': 'held'}, lines)
        self.assertEqual(self.merges(calls), [])
        self.assertEqual(self.origin_main(), before)
        rec = self.record('plan/F-0001')
        self.assertEqual(rec['correction']['kind'], 'landing-gate')
        self.assertIn('the plan turns the gate red on main', rec['correction']['text'])
        self.assertIn('test_plans_cite_known_names', rec['correction']['text'])
        self.assertFalse(rec.get('harvested'))
        # the feeder hands it to a STARVED → PLAN session on that branch, the gate line in its brief
        from asf.feeder import rows as feeder_rows
        items = {'F-0001': {'id': 'F-0001', 'type': 'feature', 'state': 'Active'}}
        corrections = lifecycle.corrections(harvest.sessions_path(self.state_dir))
        rows, spoken = feeder_rows.correction_rows(items, self.pr_product(), set(), corrections)
        self.assertEqual([(r.kind, r.brief_kind, r.branch) for r in rows],
                         [(feeder_rows.STARVED_PLAN, 'plan', 'plan/F-0001')])
        self.assertIn('test_plans_cite_known_names', rows[0].correction)
        import importlib
        brief_build = importlib.import_module('asf.briefs.build')
        self.assertIn('test_plans_cite_known_names', brief_build.correction_text(rows[0], 'plan'))

    def test_a_docs_pr_green_on_the_local_gate_is_merged(self):
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}])
        self.add_trunk_rule()
        self.push_plan({'docs/plans/f-0001.md': '# plan\ncites known names only\n'})
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'plan/F-0001': 'landed'}, lines)
        self.assertIn('harvest: 1 branch(es), one gate', lines)
        self.assertEqual(len(self.merges(calls)), 1)

    def test_a_red_trunk_is_no_prs_fault_it_waits(self):
        self.fake_gh([])
        self.push_plan()
        results, lines = self.harvest(self.pr_conv(
            test_command=f'{sys.executable} -c "import sys; sys.exit(1)"'))
        self.assertEqual(results, {'plan/F-0001': 'waiting'}, lines)
        self.assertIn('waiting plan/F-0001: gate red, and main is red alone too — gated again '
                      'next tick', lines)
        self.assertFalse(self.record('plan/F-0001').get('correction'))

    def test_a_code_pr_follows_the_same_rule(self):
        calls = self.fake_gh([{'name': 'ci', 'bucket': 'pass'}])
        self.push_fix(['approved'], extra={'checks/test_fx.py': RED_TEST})
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'held'}, lines)
        self.assertEqual(self.merges(calls), [])
        rec = self.record('fix/B-0001')
        self.assertEqual(rec['correction']['kind'], 'gate')
        self.assertEqual(rec['rounds'], 1)

    def test_a_required_check_missing_under_wait_waits(self):
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}])
        self.push_fix(['approved'])
        product = self.pr_conv(landing_checks=['build'], landing_checks_missing='wait')
        results, lines = self.harvest(product)
        self.assertEqual(results, {'fix/B-0001': 'waiting'})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('waiting fix/B-0001: PR #41 required check(s) not '
                                            'run — build (landing_checks_missing: wait, 0/30 min'),
                        lines)
        self.assertEqual(self.merges(calls), [])
        self.assertEqual(self.harvest(product)[0], {'fix/B-0001': 'waiting'})  # still inside 30

    def test_a_required_check_that_never_comes_falls_back_to_the_local_gate(self):
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}])
        self.push_fix(['approved'])
        product = self.pr_conv(landing_checks=['build'], landing_checks_missing='wait',
                               landing_checks_wait_min=0)
        results, lines = self.harvest(product)
        self.assertEqual(results, {'fix/B-0001': 'landed'}, lines)
        self.assertIn('harvest: fix/B-0001: PR #41 required check(s) never ran in 0 min — build: '
                      'gating locally', lines)
        self.assertIn('harvest: 1 branch(es), one gate', lines)
        self.assertEqual(len(self.merges(calls)), 1)

    def test_under_wait_a_passed_required_check_merges_without_a_local_gate(self):
        calls = self.fake_gh([{'name': 'build', 'bucket': 'pass'}], required=['build'])
        self.push_fix(['approved'], extra={'checks/test_fx.py': RED_TEST})  # CI is the gate
        results, lines = self.harvest(self.pr_conv(landing_checks_missing='wait'))
        self.assertEqual(results, {'fix/B-0001': 'landed'}, lines)
        self.assertFalse([l for l in lines if 'one gate' in l], lines)
        self.assertEqual(len(self.merges(calls)), 1)

    def test_landing_checks_missing_per_class(self):
        """``{docs: local-gate, code: wait}``: path-filtered CI never runs its gate on a docs PR,
        so that one is gated locally; a code PR waits for CI."""
        policy = {'docs': 'local-gate', 'code': 'wait'}
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}], required=['build'])
        self.push_plan()
        results, lines = self.harvest(self.pr_conv(landing_checks_missing=policy))
        self.assertEqual(results, {'plan/F-0001': 'landed'}, lines)
        self.assertEqual(len(self.merges(calls)), 1)
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}], required=['build'])
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_conv(landing_checks_missing=policy))
        self.assertEqual(results, {'fix/B-0001': 'waiting'}, lines)
        self.assertEqual(self.merges(calls), [])

    def test_branch_protection_is_read_once_and_cached(self):
        calls = self.fake_gh([{'name': 'DCO', 'bucket': 'pass'}], required=['build'])
        self.push_fix(['approved'])
        product = self.pr_conv(landing_checks_missing='wait')
        self.harvest(product)
        self.harvest(product)
        self.assertEqual(len([c for c in calls if c[0] == 'api' and 'protection' in c[1]]), 1)

    # ---- only required checks judge red/green -----------------------------------------

    def test_a_red_check_that_is_not_required_does_not_send_a_docs_pr_back(self):
        """The product requires ``gate``; ``gate-tests`` and DCO are red but advisory. The PR
        is green on what the product requires: it lands, and the red ones are only told."""
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'pass'},
                              {'name': 'gate-tests', 'bucket': 'fail'},
                              {'name': 'DCO', 'bucket': 'fail'}])
        self.push_plan()
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results, {'plan/F-0001': 'landed'}, lines)
        self.assertIn('harvest: plan/F-0001: PR #41 check(s) red but not required — '
                      'gate-tests, DCO (informational)', lines)
        self.assertEqual(len(self.merges(calls)), 1)
        self.assertFalse(self.record('plan/F-0001').get('correction'))

    def test_branch_protection_required_checks_judge_too(self):
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'pass'},
                              {'name': 'gate-tests', 'bucket': 'fail'}], required=['gate'])
        self.push_fix(['approved'], extra={'checks/test_fx.py': RED_TEST})  # CI is the gate
        results, lines = self.harvest(self.pr_conv(landing_checks_missing='wait'))
        self.assertEqual(results, {'fix/B-0001': 'landed'}, lines)
        self.assertFalse([l for l in lines if 'one gate' in l], lines)
        self.assertEqual(len(self.merges(calls)), 1)

    def test_a_red_required_check_still_sends_the_pr_back(self):
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'fail'},
                              {'name': 'DCO', 'bucket': 'fail'}])
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results, {'fix/B-0001': 'held'}, lines)
        self.assertIn('held fix/B-0001: PR #41 checks red: gate — back to its session (round 1)',
                      lines)
        self.assertEqual(self.merges(calls), [])
        self.assertEqual(self.record('fix/B-0001')['correction']['kind'], 'gate')

    def test_a_pending_check_that_is_not_required_does_not_hold_the_pr(self):
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'pass'},
                              {'name': 'gate-tests', 'bucket': 'pending'}])
        self.push_plan()
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results, {'plan/F-0001': 'landed'}, lines)
        self.assertEqual(len(self.merges(calls)), 1)

    def test_with_no_required_checks_any_red_check_still_sends_the_pr_back(self):
        """Today's behaviour, kept: no ``landing_checks`` and no branch protection (the 404) —
        nothing names what matters, so every check judges and any red one sends the PR back."""
        calls = self.fake_gh([{'name': 'ci', 'bucket': 'pass'}, {'name': 'DCO', 'bucket': 'fail'}])
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_product())
        self.assertEqual(results, {'fix/B-0001': 'held'}, lines)
        self.assertIn('held fix/B-0001: PR #41 checks red: DCO — back to its session (round 1)',
                      lines)
        self.assertFalse([l for l in lines if 'informational' in l], lines)
        self.assertEqual(self.merges(calls), [])

    def test_pr_checks_judges_only_the_required_names(self):
        out = json.dumps([{'name': 'gate', 'bucket': 'pass'},
                          {'name': 'gate-tests', 'bucket': 'fail'},
                          {'name': 'lint', 'bucket': 'pending'}])
        with mock.patch.object(harvest, '_gh', return_value=(1, out, '')):
            self.assertEqual(lane.pr_checks('o/p', 1, ('gate',))[:2], ('green', '3 check(s)'))
            self.assertEqual(lane.pr_checks('o/p', 1, ('lint',))[:2], ('pending', 'lint'))
            self.assertEqual(lane.pr_checks('o/p', 1, ('gate-tests',))[:2], ('red', 'gate-tests'))
            self.assertEqual(lane.pr_checks('o/p', 1)[:2], ('red', 'gate-tests'))  # none named
        checks = json.loads(out)
        self.assertEqual(lane.not_required_red(checks, ('gate',)), ['gate-tests'])
        self.assertEqual(lane.not_required_red(checks, ()), [])

    # ---- a red trunk: never the PR's fault, never landed on -----------------------------

    def push_trunk(self, rel='teach.txt', text='x\n'):
        """A commit straight on origin's main — the one the trunk's CI turned red on."""
        sh(['git', 'pull', '-q', '--ff-only', 'origin', 'main'], cwd=self.repo)
        self.write(self.repo, rel, text)
        sh(['git', 'add', '-A'], cwd=self.repo)
        sh(['git', 'commit', '-qm', 'feat: on the trunk'], cwd=self.repo)
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.worker)  # a branch pushed next starts here
        return self.origin_main()

    def test_a_pr_red_on_a_check_the_trunk_is_red_on_too_waits_and_is_not_sent_back(self):
        """B-1372: the trunk's latest completed ``gate`` run failed; the PR's ``gate`` is red on
        that same failure. Not its fault: WAITING ``trunk-red``, no round, nothing merged."""
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'fail'}], trunk={'gate': 'failure'})
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results, {'fix/B-0001': 'waiting'}, lines)
        self.assertIn('waiting fix/B-0001: PR #41 checks red: gate — red on main too, not its '
                      'fault; it lands once main is green', lines)
        self.assertEqual(self.merges(calls), [])
        rec = self.record('fix/B-0001')
        self.assertFalse(rec.get('correction'))
        self.assertEqual((rec['lane']['state'], rec['lane']['reason']),
                         ('WAITING', 'trunk-red: gate'))

    def test_a_pr_red_on_a_check_the_trunk_is_green_on_is_still_sent_back(self):
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'fail'}],
                             trunk={'gate': 'success', 'lint': 'failure'})
        self.push_fix(['approved'])
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results, {'fix/B-0001': 'held'}, lines)
        self.assertEqual(self.record('fix/B-0001')['correction']['kind'], 'gate')
        self.assertEqual(self.merges(calls), [])

    def test_a_green_pr_does_not_land_on_a_trunk_red_it_does_not_fix(self):
        """The PR's ``gate`` went green on a base from before the trunk turned red — that green
        proves nothing about the red. It waits, and lands once the trunk is green again."""
        self.push_fix(['approved'])
        self.push_trunk()
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'pass'}], trunk={'gate': 'failure'})
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results, {'fix/B-0001': 'waiting'}, lines)
        self.assertIn('waiting fix/B-0001: main is red on gate and PR #41 does not turn it green '
                      'on top of that red — it lands once main is green', lines)
        self.assertEqual(self.merges(calls), [])
        self.assertFalse(self.record('fix/B-0001').get('correction'))

    def test_a_pr_that_turns_the_red_trunk_check_green_lands(self):
        """The fix for the red: branched off the red trunk, its ``gate`` passes — it lands."""
        self.push_trunk()
        self.push_fix(['approved'])
        calls = self.fake_gh([{'name': 'gate', 'bucket': 'pass'}], trunk={'gate': 'failure'})
        results, lines = self.harvest(self.pr_conv(landing_checks=['gate']))
        self.assertEqual(results, {'fix/B-0001': 'landed'}, lines)
        self.assertIn('harvest: fix/B-0001: PR #41 turns gate green on top of the red main — it '
                      'may land', lines)
        self.assertEqual(len(self.merges(calls)), 1)

    def test_trunk_red_judges_the_latest_completed_run(self):
        """A check still running on the trunk's head is judged by its run one commit back; a
        check green on the head is green whatever came before; an unreadable host is not red."""
        older = self.push_trunk('a.txt')
        newest = self.push_trunk('b.txt')
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        runs = {newest: [{'name': 'gate', 'status': 'in_progress', 'conclusion': None},
                         {'name': 'lint', 'status': 'completed', 'conclusion': 'success'}],
                older: [{'name': 'gate', 'status': 'completed', 'conclusion': 'failure'},
                        {'name': 'lint', 'status': 'completed', 'conclusion': 'failure'}]}

        def gh(args):
            sha = args[1].split('/commits/')[1].split('/')[0]
            return 0, json.dumps({'check_runs': runs.get(sha, [])}), ''
        host_ = lane.Lane(self.pr_conv(), self.state_dir).host
        with mock.patch.object(harvest, '_gh', side_effect=gh):
            self.assertEqual(host_.trunk_red(['gate', 'lint', 'docs']), {'gate': older})
        with mock.patch.object(harvest, '_gh', return_value=(1, '', 'HTTP 404')):
            self.assertEqual(lane.Lane(self.pr_conv(), self.state_dir).host.trunk_red(['gate']),
                             {})

    def test_missing_policy_reads_one_value_or_a_map(self):
        conv = Conventions.from_mapping
        self.assertEqual(lane.missing_policy(conv({}), 'docs'), 'local-gate')
        self.assertEqual(lane.missing_policy(conv({'landing_checks_missing': 'wait'}), 'docs'),
                         'wait')
        both = conv({'landing_checks_missing': {'docs': 'local-gate', 'code': 'wait'}})
        self.assertEqual(lane.missing_policy(both, 'docs'), 'local-gate')
        self.assertEqual(lane.missing_policy(both, 'code'), 'wait')
        self.assertEqual(lane.missing_policy(conv({'landing_checks_missing': 'bogus'}), 'code'),
                         'local-gate')

    def test_every_mergeable_pr_is_gated_once_together(self):
        """One local gate per landing class per pass over all the PRs ready to merge, never one
        each; a PR whose required checks passed merges without one (``gate_set``)."""
        ln = lane.Lane(self.pr_product(), self.state_dir, out=lambda _l: None)
        entries = [{'branch': 'plan/F-0001', 'class': lane.DOCS, 'how': 'gate'},
                   {'branch': 'fix/B-0001', 'class': lane.CODE, 'how': 'gate'},
                   {'branch': 'fix/B-0003', 'class': lane.CODE, 'how': 'gate'},
                   {'branch': 'fix/B-0002', 'class': lane.CODE, 'how': 'ci'}]
        with mock.patch.object(lane, 'gate_one_set') as gate, \
                mock.patch.object(lane, 'merge_prs') as merge:
            lane.gate_set(ln, entries)
        self.assertEqual([[f['branch'] for f in c.args[1]] for c in gate.call_args_list],
                         [['plan/F-0001'], ['fix/B-0001', 'fix/B-0003']])
        self.assertEqual([f['branch'] for f in merge.call_args.args[1]], ['fix/B-0002'])

    def test_host_pressure_holds_the_local_gate_and_lets_the_ci_prs_through(self):
        """B-0109: under host pressure no local suite is started — but a PR merging on its own
        CI's checks alone (``how == 'ci'``) runs none here, so it goes on. Holding those too
        would strand every external-CI PR behind a loaded host, for a suite it never runs."""
        calls = self.fake_gh_prs({'plan/F-0001': (41, [{'name': 'ci', 'bucket': 'pass'}]),
                                  'fix/B-0001': (42, [{'name': 'ci', 'bucket': 'pass'}])})
        self.push_plan()
        self.push_fix(['approved'])
        product = self.pr_conv(landing_checks=['ci'],
                               landing_checks_missing={'docs': 'wait', 'code': 'local-gate'})
        with mock.patch.dict(os.environ, {host.READING_ENV: '90 12 87'}), \
                mock.patch.object(harvest, 'product_gate',
                                  side_effect=AssertionError('a suite started under pressure')):
            results, lines = self.harvest(product)
        self.assertEqual(results, {'plan/F-0001': 'landed', 'fix/B-0001': 'waiting'}, lines)
        self.assertEqual([c[:3] for c in self.merges(calls)], [['pr', 'merge', '41']])
        rec = self.record('fix/B-0001')
        self.assertEqual((rec['lane']['state'], rec['lane']['reason']),
                         (lane.WAITING, lane.HOST_PRESSURE))
        self.assertFalse(rec.get('correction'))  # pressure is not a defect: no round, no blame

    def test_a_docs_branch_already_handed_to_the_pr_lane_is_merged(self):
        """A docs branch an earlier harvest marked ``harvest: pr`` is not stranded there."""
        calls = self.fake_gh([])
        self.push_plan()
        harvest.mark_session(self.state_dir, 'plan-f-0001', harvest='pr')  # a v0.1.2 ledger
        self.assertEqual(self.harvest(self.pr_product())[0], {'plan/F-0001': 'landed'})
        self.assertEqual(len(self.merges(calls)), 1)


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
        return lane.hold_with_correction(self.state, 'worker/T-0080', self.record, kind, text,
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
        return lane.hold_with_correction(self.state, 'plan/F-0039', self.record, 'gate',
                                            'FAILED (failures=1)', self.lines.append, files, (),
                                            touched)

    def test_with_no_writes_the_diff_is_the_footprint(self):
        self.assertEqual(self.hold_diff(['asf/other.py'], ['asf/mine.py']), 'foreign')
        self.assertEqual(self.lines, ['foreign plan/F-0039: gate red outside its diff: '
                                      'asf/other.py — re-gated next tick'])
        self.assertEqual(self.hold_diff(['asf/mine.py'], ['asf/mine.py']), 'held')

    def test_a_docs_only_diff_is_never_held_for_a_red_test(self):
        docs = ['docs/plans/f-0039.md', 'docs/specs/f-0039.md']
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
        self.assertEqual(lane.landing_class(conv, ['docs/plans/f-1.md', 'docs/reviews/1-x.md',
                                                   'docs/specs/f-1.md']), lane.DOCS)
        for files in ([], ['docs/config.example.yaml'], ['asf/briefs/templates/plan.md'],
                      ['plugin/skills/x/SKILL.md'], ['docs/plans/f-1.md', 'asf/x.py'],
                      ['README.md']):
            self.assertEqual(lane.landing_class(conv, files), lane.CODE, files)

    def test_only_a_gate_red_can_be_foreign(self):
        self.assertEqual(self.hold(['asf/other.py'], ['asf/harvest/harvest.py'], kind='conflict'),
                         'held')


PRODUCT_REPOS = Template(ProductHarvestTests.build, prefix='harvest_product_')


class GateLedgerTests(unittest.TestCase):
    """§2.2's ledger half: every landing gate — per-branch or combined — appends one timed, named
    line to ``gates.jsonl``, and :func:`asf.metrics.metrics.gates_from_ledger` reads it back."""

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
        conventions.setdefault('lane', {'review': {'code': 'none'}})  # no review before the gate
        return env.Product('sample', {'repo_dir': self.repo, 'main': 'main',
                                      'conventions': conventions, 'steps': {'batch': 'off'}})

    def push_lane(self, branch, commits):
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

    def harvest(self, product):
        lines = []
        results = harvest.run_product_harvest(product, self.state_dir, out=lines.append)
        return results, lines

    def record(self, branch):
        return harvest.sessions_by_branch(self.state_dir).get(branch) or {}

    def ledger(self):
        path = os.path.join(self.state_dir, 'gates.jsonl')
        if not os.path.isfile(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(l) for l in f if l.strip()]

    def lane(self, i, red=False):
        item = f'B-{i:04d}'
        branch = f'fix/{item}'
        files = {f'f{i}.txt': f'{i}\n'}
        if red:
            files[f'checks/test_red{i}.py'] = RED_TEST.replace('test_red_gate', f'test_red_{i}')
        self.push_lane(branch, [(f'fix({item}): change {i}', files)])
        self.session(f'fix-bug-{item.lower()}', item, branch)
        return branch

    def test_a_per_branch_landing_appends_one_line_with_the_branch_the_sha_and_ok_true(self):
        branch = self.lane(1)
        results, _lines = self.harvest(self.product(harvest={'gate': 'per-branch'}))
        self.assertEqual(results, {branch: 'landed'})
        lines = self.ledger()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['branches'], [branch])
        self.assertEqual(lines[0]['sha'], self.record(branch)['harvested'])
        self.assertIs(lines[0]['ok'], True)
        self.assertIsNone(lines[0]['line'])
        self.assertIsInstance(lines[0]['seconds'], (int, float))
        self.assertGreaterEqual(lines[0]['seconds'], 0)
        self.assertIsInstance(lines[0]['at'], str)

    def test_a_combined_gate_over_three_branches_appends_one_line_naming_all_three(self):
        branches = [self.lane(i) for i in (1, 2, 3)]
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {b: 'landed' for b in branches})
        lines = self.ledger()
        self.assertEqual(len(lines), 1)
        self.assertEqual(sorted(lines[0]['branches']), sorted(branches))
        self.assertIs(lines[0]['ok'], True)
        self.assertEqual(lines[0]['sha'], self.record(branches[0])['harvested'])

    def test_a_red_combined_gate_that_bisects_appends_one_line_per_group_it_gated(self):
        branches = [self.lane(i, red=(i == 3)) for i in (1, 2, 3, 4)]
        results, _lines = self.harvest(self.product())
        self.assertEqual(results, {'fix/B-0001': 'landed', 'fix/B-0002': 'landed',
                                   'fix/B-0003': 'held', 'fix/B-0004': 'landed'})
        lines = self.ledger()
        # the full 4, then the bisection: [1,2] green, [3,4] red -> [3] red, [4] green
        self.assertEqual(len(lines), 6, lines)
        self.assertFalse(lines[0]['ok'])
        self.assertEqual(sorted(lines[0]['branches']), sorted(branches))
        solo_red = [l for l in lines if l['branches'] == ['fix/B-0003']]
        self.assertEqual(len(solo_red), 1, lines)
        self.assertFalse(solo_red[0]['ok'])
        self.assertIn('test_red_3', solo_red[0]['line'])
        solo_green = [l for l in lines if l['branches'] == ['fix/B-0004']]
        self.assertEqual(len(solo_green), 1, lines)
        self.assertTrue(solo_green[0]['ok'])
        self.assertIsNone(solo_green[0]['line'])

    def test_gate_groups_with_no_ledger_writes_nothing(self):
        branch = self.lane(1)
        holder = tempfile.mkdtemp(prefix='gate_groups_')
        self.addCleanup(shutil.rmtree, holder, True)
        tmp = os.path.join(holder, 'wt')
        sh(['git', 'worktree', 'add', '-q', '--detach', tmp, 'origin/main'], cwd=self.repo)
        self.addCleanup(sh, ['git', 'worktree', 'remove', '--force', tmp], self.repo)
        conv = Conventions.from_mapping({'test_command': PRODUCT_TEST})
        groups = lane.gate_groups(tmp, 'main', [{'branch': branch}], conv, False,
                                  lambda *a, **k: None, lambda *a, **k: None)
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0][2])
        self.assertEqual(self.ledger(), [])

    def test_the_ledger_a_harvest_writes_is_read_back_into_a_valid_gates_event(self):
        from asf.metrics import metrics
        branch = self.lane(1)
        self.harvest(self.product(harvest={'gate': 'per-branch'}))
        product = env.Product('sample', {})
        with mock.patch.object(env, 'state_dir', lambda p=None: self.state_dir):
            evs = metrics.gates_from_ledger(product)
        self.assertEqual(len(evs), 1)
        ev = metrics.validate('gates', evs[0], {})
        self.assertEqual(ev['branches'], [branch])
        self.assertEqual(ev['conclusion'], 'success')
        self.assertEqual(ev['sha'], self.record(branch)['harvested'])


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


class StaleBranchPushTests(unittest.TestCase):
    """2026-09-25: a factory push from a stale head erased a newer commit a person had pushed to
    the lane branch. ``push_branch`` fetched first, so its lease matched whatever origin held —
    a plain force. The expected head is read before any rebase, and no remote commit may be
    lost."""

    def setUp(self):
        self.base = PRODUCT_REPOS.fresh()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.repo = os.path.join(self.base, 'repo')
        self.worker = os.path.join(self.base, 'worker')
        self.state_dir = os.path.join(self.base, 'state')
        self.branch = 'fix/B-0001'
        sh(['git', 'checkout', '-q', '-B', self.branch, 'origin/main'], cwd=self.worker)
        self.commit(self.worker, 'a.txt', 'fix(B-0001): first')
        sh(['git', 'push', '-q', 'origin', self.branch], cwd=self.worker)
        # the product checkout holds the branch as it was then
        sh(['git', 'fetch', '-q', 'origin', self.branch], cwd=self.repo)
        sh(['git', 'branch', '-q', self.branch, f'origin/{self.branch}'], cwd=self.repo)
        self.stale = self.rev(self.repo, self.branch)

    def commit(self, root, rel, subject):
        with open(os.path.join(root, rel), 'w', encoding='utf-8') as f:
            f.write(subject + '\n')
        sh(['git', 'add', '-A'], cwd=root)
        sh(['git', 'commit', '-qm', subject], cwd=root)

    def rev(self, root, ref):
        return sh(['git', 'rev-parse', ref], cwd=root).stdout.strip()

    def remote_head(self):
        return sh(['git', 'ls-remote', '--heads', 'origin', self.branch],
                  cwd=self.repo).stdout.split()[0]

    def test_a_stale_local_branch_never_overwrites_the_newer_remote_head(self):
        self.commit(self.worker, 'b.txt', 'fix(B-0001): newer, pushed by a person')
        sh(['git', 'push', '-q', 'origin', self.branch], cwd=self.worker)
        newer = self.rev(self.worker, 'HEAD')
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                got = harvest.harvest_branch(self.repo, self.state_dir, False, 'fix-bug-b-0001',
                                             self.branch, False, CONV)
            except Exception:  # noqa: BLE001 — whatever it did after the push, origin decides
                got = 'raised'
        self.assertEqual(self.remote_head(), newer, buf.getvalue())
        self.assertEqual(got, 'held', buf.getvalue())
        self.assertIn('would lose', buf.getvalue())

    def test_push_branch_refuses_a_head_missing_remote_commits(self):
        self.commit(self.worker, 'b.txt', 'fix(B-0001): newer')
        sh(['git', 'push', '-q', 'origin', self.branch], cwd=self.worker)
        newer = self.rev(self.worker, 'HEAD')
        ok, why = harvest.push_branch(self.repo, self.stale, self.branch, newer)
        self.assertFalse(ok)
        self.assertIn(newer[:9], why)
        self.assertEqual(self.remote_head(), newer)

    def test_push_branch_leases_on_the_sha_read_before_the_rebase(self):
        # origin moved after the expected head was read: refused, even with every commit kept
        self.commit(self.worker, 'b.txt', 'fix(B-0001): newer')
        sh(['git', 'push', '-q', 'origin', self.branch], cwd=self.worker)
        newer = self.rev(self.worker, 'HEAD')
        self.commit(self.worker, 'c.txt', 'fix(B-0001): on top, not pushed')
        on_top = self.rev(self.worker, 'HEAD')
        sh(['git', 'fetch', '-q', self.worker, on_top], cwd=self.repo)
        ok, _why = harvest.push_branch(self.repo, on_top, self.branch, self.stale)
        self.assertFalse(ok)
        self.assertEqual(self.remote_head(), newer)

    def test_push_branch_publishes_a_rebase_that_keeps_every_remote_commit(self):
        sh(['git', 'checkout', '-q', 'main'], cwd=self.worker)
        self.commit(self.worker, 'trunk.txt', 'trunk moves')
        sh(['git', 'push', '-q', 'origin', 'main'], cwd=self.worker)
        sh(['git', 'fetch', '-q', 'origin'], cwd=self.repo)
        wt = os.path.join(self.base, 'rebase-wt')
        sh(['git', 'worktree', 'add', '-q', '--detach', wt, self.stale], cwd=self.repo)
        git_identity(wt)
        sh(['git', 'rebase', '-q', 'origin/main'], cwd=wt)
        rebased = self.rev(wt, 'HEAD')
        ok, why = harvest.push_branch(self.repo, rebased, self.branch, self.stale)
        self.assertTrue(ok, why)
        self.assertEqual(self.remote_head(), rebased)
