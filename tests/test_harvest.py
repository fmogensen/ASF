import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from asf.harvest import harvest

DEFAULT_BODY = (
    "## Description\n{desc}\n\n## Acceptance\n- [ ] \n\n## Non-goals\n\n"
    "## History\n- 2026-09-21: created\n\n## Children\n\n## Backlinks\n"
)

# A minimal stand-in for a product repo's own `tools/backlog.py` — just enough of the
# `index`/`check` contract for harvest.py's gate (see harvest.py's module docstring: the gate
# runs only when `tools/backlog.py` is on disk, and is `index` + `check`, the same two
# subcommands this stub implements). The real tool lives in the product repo being harvested,
# not in this package, so the test fixture only needs to honor that contract.
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
    """A bare `origin`, a `repo` clone (the tick clone / main checkout) seeded with a minimal
    backlog.py toolset and one epic, and a `workers` dir with logs/ + worktrees/ + worktrees.tsv."""
    base = tempfile.mkdtemp(prefix='harvest_test_')
    origin = os.path.join(base, 'origin.git')
    repo = os.path.join(base, 'repo')
    sh(['git', 'init', '-q', '--bare', origin])
    sh(['git', 'clone', '-q', origin, repo])
    sh(['git', 'config', 'user.name', 'Test'], cwd=repo)
    sh(['git', 'config', 'user.email', 'test@example.com'], cwd=repo)
    sh(['git', 'config', 'commit.gpgsign', 'false'], cwd=repo)

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

    workers_dir = os.path.join(base, 'workers')
    os.makedirs(os.path.join(workers_dir, 'logs'))
    os.makedirs(os.path.join(workers_dir, 'worktrees'))
    return base, origin, repo, workers_dir


def write_meta(workers_dir, job, rc=0, finished=True):
    path = os.path.join(workers_dir, 'logs', f'{job}.meta')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'job={job} account=test cwd=/tmp/{job} pid=1 started=2026-09-21T00:00:00+00:00\n')
        if finished:
            f.write(f'finished=2026-09-21T00:05:00+00:00 rc={rc}\n')


def write_tsv_row(workers_dir, job, branch):
    with open(os.path.join(workers_dir, 'worktrees.tsv'), 'a', encoding='utf-8') as f:
        f.write(f'{job}\ttest\t{branch}\tabc123\t2026-09-21T00:00:00Z\trunning\n')


def tsv_has_job(workers_dir, job):
    path = os.path.join(workers_dir, 'worktrees.tsv')
    if not os.path.isfile(path):
        return False
    with open(path, encoding='utf-8') as f:
        return any(l.split('\t', 1)[0] == job for l in f)


def add_job_worktree(repo, workers_dir, job, base_ref='main'):
    branch = f'worker/{job}'
    wt = os.path.join(workers_dir, 'worktrees', job)
    sh(['git', 'worktree', 'add', '-q', '-b', branch, wt, base_ref], cwd=repo)
    sh(['git', 'config', 'user.name', 'Test'], cwd=wt)
    sh(['git', 'config', 'user.email', 'test@example.com'], cwd=wt)
    sh(['git', 'config', 'commit.gpgsign', 'false'], cwd=wt)
    return branch, wt


def run_harvest(repo, workers_dir, dry_run=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = harvest.run_harvest(repo, workers_dir, dry_run)
    return rc, buf.getvalue()


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.base, self.origin, self.repo, self.workers_dir = make_repo()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    # -- a fast-forwardable branch lands on main with no controller action -----------------
    def test_green_branch_lands_on_main(self):
        branch, wt = add_job_worktree(self.repo, self.workers_dir, 'ff1')
        write_epic(wt, 'E-0002', 'New epic from ff1')
        index_and_commit(wt, 'ff1: add E-0002')
        write_meta(self.workers_dir, 'ff1', rc=0)
        write_tsv_row(self.workers_dir, 'ff1', branch)

        rc, out = run_harvest(self.repo, self.workers_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST OK ff1', out)

        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        log = sh(['git', 'log', '--oneline', 'origin/main'], cwd=self.repo).stdout
        self.assertIn('ff1: add E-0002', log)

        self.assertFalse(os.path.isdir(os.path.join(self.workers_dir, 'worktrees', 'ff1')))
        branches = sh(['git', 'branch', '--list', branch], cwd=self.repo).stdout
        self.assertEqual(branches.strip(), '')
        self.assertFalse(tsv_has_job(self.workers_dir, 'ff1'))

    # -- a non-machine conflict holds the branch and names the file ------------------------
    def test_non_machine_conflict_is_held(self):
        branch, wt = add_job_worktree(self.repo, self.workers_dir, 'conflict1')
        write_epic(wt, 'E-0001', 'Seed epic', desc='Worker edit')
        index_and_commit(wt, 'conflict1: edit description')
        write_meta(self.workers_dir, 'conflict1', rc=0)
        write_tsv_row(self.workers_dir, 'conflict1', branch)

        # a different edit to the same line lands on main first, out from under the branch
        write_epic(self.repo, 'E-0001', 'Seed epic', desc='Main edit')
        index_and_commit(self.repo, 'main: edit description')
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)
        main_sha = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()

        rc, out = run_harvest(self.repo, self.workers_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST HOLD conflict1', out)
        self.assertIn('conflict in', out)
        self.assertIn('E-0001.md', out)

        # nothing touched: worktree, branch, tsv row and main all unchanged
        self.assertTrue(os.path.isdir(os.path.join(self.workers_dir, 'worktrees', 'conflict1')))
        branches = sh(['git', 'branch', '--list', branch], cwd=self.repo).stdout
        self.assertIn(branch, branches)
        self.assertTrue(tsv_has_job(self.workers_dir, 'conflict1'))
        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        self.assertEqual(sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip(), main_sha)

    # -- a branch whose tests fail is held, never landed ------------------------------------
    def test_failing_tests_are_held(self):
        branch, wt = add_job_worktree(self.repo, self.workers_dir, 'redtest')
        with open(os.path.join(wt, 'tools', 'test_fixture.py'), 'w', encoding='utf-8') as f:
            f.write(
                "import unittest\n\n"
                "class FixtureTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(False)\n"
            )
        sh(['git', 'add', '-A'], cwd=wt)
        sh(['git', 'commit', '-qm', 'redtest: break the gate'], cwd=wt)
        write_meta(self.workers_dir, 'redtest', rc=0)
        write_tsv_row(self.workers_dir, 'redtest', branch)

        rc, out = run_harvest(self.repo, self.workers_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST HOLD redtest', out)
        self.assertIn('tests failed', out)

        self.assertTrue(os.path.isdir(os.path.join(self.workers_dir, 'worktrees', 'redtest')))
        self.assertTrue(tsv_has_job(self.workers_dir, 'redtest'))

    # -- an index.json-only conflict is resolved by regenerating it ------------------------
    def test_index_json_only_conflict_is_resolved(self):
        branch, wt = add_job_worktree(self.repo, self.workers_dir, 'idxconf')
        write_epic(wt, 'E-0003', 'Worker-side epic')
        index_and_commit(wt, 'idxconf: add E-0003')

        # a different new epic lands on main first — same index.json region, different content
        write_epic(self.repo, 'E-0002', 'Main-side epic')
        index_and_commit(self.repo, 'main: add E-0002')
        sh(['git', 'push', '-q', 'origin', 'HEAD:main'], cwd=self.repo)

        write_meta(self.workers_dir, 'idxconf', rc=0)
        write_tsv_row(self.workers_dir, 'idxconf', branch)

        rc, out = run_harvest(self.repo, self.workers_dir)
        self.assertEqual(rc, 0)
        self.assertIn('HARVEST OK idxconf', out)

        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        r = sh(['git', 'show', 'origin/main:index.json'], cwd=self.repo)
        self.assertIn('E-0002', r.stdout)
        self.assertIn('E-0003', r.stdout)
        check = subprocess.run([sys.executable, os.path.join('tools', 'backlog.py'), 'check'],
                                cwd=self.repo, capture_output=True, text=True, env=clean_env())
        # check runs against the checked-out worktree, which is still on main pre-fetch content;
        # fast-forward the local main branch to origin so check sees the landed state
        sh(['git', 'checkout', '-q', 'main'], cwd=self.repo)
        sh(['git', 'merge', '-q', '--ff-only', 'origin/main'], cwd=self.repo)
        check = subprocess.run([sys.executable, os.path.join('tools', 'backlog.py'), 'check'],
                                cwd=self.repo, capture_output=True, text=True, env=clean_env())
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    # -- dry-run reports without mutating anything ------------------------------------------
    def test_dry_run_prints_and_does_not_push(self):
        branch, wt = add_job_worktree(self.repo, self.workers_dir, 'dry1')
        write_epic(wt, 'E-0004', 'Dry run epic')
        index_and_commit(wt, 'dry1: add E-0004')
        write_meta(self.workers_dir, 'dry1', rc=0)
        write_tsv_row(self.workers_dir, 'dry1', branch)

        before = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        rc, out = run_harvest(self.repo, self.workers_dir, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn('DRY: would push dry1', out)

        sh(['git', 'fetch', '-q', 'origin', 'main'], cwd=self.repo)
        after = sh(['git', 'rev-parse', 'origin/main'], cwd=self.repo).stdout.strip()
        self.assertEqual(before, after)
        self.assertTrue(os.path.isdir(os.path.join(self.workers_dir, 'worktrees', 'dry1')))
        self.assertTrue(tsv_has_job(self.workers_dir, 'dry1'))

    # -- an unfinished or non-zero-rc job is left alone, not even attempted -----------------
    def test_ineligible_job_is_skipped(self):
        branch, wt = add_job_worktree(self.repo, self.workers_dir, 'running1')
        write_epic(wt, 'E-0005', 'Still running')
        index_and_commit(wt, 'running1: wip')
        write_meta(self.workers_dir, 'running1', finished=False)

        rc, out = run_harvest(self.repo, self.workers_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), '')
        self.assertTrue(os.path.isdir(os.path.join(self.workers_dir, 'worktrees', 'running1')))


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
