"""A deterministic red is not replayed: :mod:`asf.stale_ref` reopens a PR for a fresh merge ref at
most ``flags.stale_ref_reopens`` times per head commit and only when the trunk changed what the
failing job reads; :mod:`asf.flake` never re-runs a red that already reproduced on another merge
ref (2026-10-05: one PR reopened 4 times in 35 min; 20 of 21 blind re-runs stayed red).

Hermetic: a temp git repo for the trunk, a temp state dir, the host doors mocked."""
import datetime
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import connectors, env, flake, github, stale_ref
from asf.facts import cache as facts_cache

T0 = 1_790_000_000
LINK = 'https://github.com/o/p/actions/runs/{run}/job/{job}'


def git(cwd, *args, when=None):
    e = dict(os.environ, GIT_AUTHOR_NAME='a', GIT_AUTHOR_EMAIL='a@x', GIT_COMMITTER_NAME='a',
             GIT_COMMITTER_EMAIL='a@x')
    if when is not None:
        e['GIT_COMMITTER_DATE'] = e['GIT_AUTHOR_DATE'] = f'@{when} +0000'
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True,
                          env=e).stdout.strip()


def ok(data=None):
    return github.Result(True, data, 0, '', '', github.now_iso(), '')


class Trunk(unittest.TestCase):
    """A trunk with ``src/app.py`` and ``tests/test_app.py`` at T0, one PR red at T0 + 100."""

    def setUp(self):
        facts_cache.clear()
        self.addCleanup(facts_cache.clear)
        self.tmp = tempfile.mkdtemp(prefix='red_replay_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = os.path.join(self.tmp, 'state')
        os.makedirs(self.state)
        p = mock.patch.object(env, 'state_dir', lambda *_a, **_k: self.state)
        p.start()
        self.addCleanup(p.stop)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(os.path.join(self.repo, 'src'))
        os.makedirs(os.path.join(self.repo, 'tests'))
        git(self.tmp, 'init', '-q', '-b', 'main', self.repo)
        self.commit({'src/app.py': 'A = 1\n', 'tests/test_app.py': 'x\n'}, T0)
        self.runs = {}      # run id -> created epoch
        self.forge = mock.Mock()
        self.forge.close_pr.return_value = ok()
        self.forge.reopen_pr.return_value = ok()
        ci = mock.Mock()
        ci.runs_for_sha.return_value = ok({'workflow_runs': []})
        ci.run.side_effect = lambda _slug, rid: ok(
            {'event': 'pull_request', 'created_at': datetime.datetime.fromtimestamp(
                self.runs[rid], datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
        for name, door in (('forge', self.forge), ('ci', ci)):
            p = mock.patch.object(connectors, name, return_value=door)
            p.start()
            self.addCleanup(p.stop)
        self.named = []     # the files the failing log names
        p = mock.patch('asf.merge_queue.failure_findings', side_effect=lambda _s, roots: [
            {'name': r['name'], 'paths': [(f, 1, '') for f in self.named]} for r in roots])
        p.start()
        self.addCleanup(p.stop)
        self.lines = []

    def commit(self, files, when):
        for rel, text in files.items():
            with open(os.path.join(self.repo, rel), 'w', encoding='utf-8') as f:
                f.write(text)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', f'at {when}', when=when)
        return git(self.repo, 'rev-parse', 'HEAD')

    def product(self, flags=None):
        return env.Product('p', {'repo_slug': 'o/p', 'repo_dir': self.repo, 'main': 'main',
                                 'conventions': {'landing': 'pull-request',
                                                 'flags': flags or {}}})

    def red(self, run):
        self.runs.setdefault(str(run), T0 + 100 * run)
        return [{'name': 'tests (3.12)', 'link': LINK.format(run=run, job=run * 10)}]

    def refresh(self, product, head, tip, red, now):
        facts_cache.clear()
        return stale_ref.refresh(product, 'o/p', 7, head, tip, ['tests (3.12)'],
                                 out=self.lines.append, now=now, red=red, repo=self.repo)


class OncePerHead(Trunk):
    def test_a_head_is_reopened_once_however_often_the_trunk_moves(self):
        product = self.product()
        tip1 = self.commit({'src/app.py': 'A = 2\n'}, T0 + 200)
        self.assertEqual(self.refresh(product, 'h1', tip1, self.red(1), T0 + 300), 'fresh')
        # its fresh run (created after the reopen) is red again; meanwhile the trunk moved on
        self.runs['2'] = T0 + 400
        tip2 = self.commit({'src/app.py': 'A = 3\n'}, T0 + 500)
        self.assertIsNone(self.refresh(product, 'h1', tip2, self.red(2), T0 + 600))
        tip3 = self.commit({'src/app.py': 'A = 4\n'}, T0 + 700)
        self.assertIsNone(self.refresh(product, 'h1', tip3, self.red(2), T0 + 800))
        self.assertEqual(self.forge.reopen_pr.call_count, 1)
        self.assertTrue(any('judged as it stands' in l for l in self.lines), self.lines)
        # a new head is a new change: it gets its own reopen
        self.assertEqual(self.refresh(product, 'h2', tip3, self.red(3), T0 + 900), 'fresh')
        self.assertEqual(self.forge.reopen_pr.call_count, 2)

    def test_the_cap_is_config(self):
        product = self.product({'stale_ref_reopens': 2})
        tip1 = self.commit({'src/app.py': 'A = 2\n'}, T0 + 200)
        self.assertEqual(self.refresh(product, 'h1', tip1, self.red(1), T0 + 300), 'fresh')
        self.runs['2'] = T0 + 400
        tip2 = self.commit({'src/app.py': 'A = 3\n'}, T0 + 500)
        self.assertEqual(self.refresh(product, 'h1', tip2, self.red(2), T0 + 600), 'fresh')
        self.assertEqual(stale_ref.settings(self.product({'stale_ref_reopens': '0'}))[0], 0)
        self.assertEqual(stale_ref.settings(self.product())[0], 1)

    def test_a_fresh_run_not_shown_yet_still_waits(self):
        product = self.product()
        tip1 = self.commit({'src/app.py': 'A = 2\n'}, T0 + 200)
        self.assertEqual(self.refresh(product, 'h1', tip1, self.red(1), T0 + 300), 'fresh')
        tip2 = self.commit({'src/app.py': 'A = 3\n'}, T0 + 350)
        self.assertEqual(self.refresh(product, 'h1', tip2, self.red(1), T0 + 360), 'waiting')
        self.assertEqual(stale_ref.in_flight(product, now=T0 + 360), [7])


class OnlyWhenTheTrunkTouchedTheJob(Trunk):
    def test_no_reopen_when_the_trunk_changed_nothing_the_failing_log_names(self):
        self.named = ['tests/test_app.py', 'src/app.py']
        tip = self.commit({'README.md': 'docs\n'}, T0 + 200)
        self.assertIsNone(self.refresh(self.product(), 'h1', tip, self.red(1), T0 + 300))
        self.forge.close_pr.assert_not_called()
        self.assertTrue(any('changed nothing the failing job reads' in l for l in self.lines))
        # read once a tip: the next pass on the same tip asks nothing again
        self.assertIsNone(self.refresh(self.product(), 'h1', tip, self.red(1), T0 + 310))
        self.forge.close_pr.assert_not_called()

    def test_a_reopen_when_the_trunk_changed_a_file_the_log_names(self):
        self.named = ['src/app.py']
        tip = self.commit({'src/app.py': 'A = 2\n'}, T0 + 200)
        self.assertEqual(self.refresh(self.product(), 'h1', tip, self.red(1), T0 + 300), 'fresh')

    def test_job_globs_from_config_decide_when_set(self):
        self.named = ['src/app.py']
        product = self.product({'stale_ref_job_paths': {'tests': ['docs/*']}})
        tip = self.commit({'src/app.py': 'A = 2\n'}, T0 + 200)
        self.assertIsNone(self.refresh(product, 'h1', tip, self.red(1), T0 + 300))
        os.makedirs(os.path.join(self.repo, 'docs'))
        tip = self.commit({'docs/x.md': 'y\n'}, T0 + 400)
        self.assertEqual(self.refresh(product, 'h1', tip, self.red(1), T0 + 500), 'fresh')

    def test_nothing_named_and_no_globs_reopens_as_before(self):
        tip = self.commit({'README.md': 'docs\n'}, T0 + 200)
        self.assertEqual(self.refresh(self.product(), 'h1', tip, self.red(1), T0 + 300), 'fresh')


class ReproducedIsNeverRerun(unittest.TestCase):
    """The same check red on one head in two runs (two merge refs), the same test failing: a
    defect, never a blind re-run."""

    SHA = 'b' * 40

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='flake_repro_')
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.calls = []
        self.tests = {}     # job id -> the failing test its log names

    def gh(self, args):
        self.calls.append(list(args))
        if args[:2] == ['run', 'rerun']:
            return 0, '', ''
        if args[0] == 'api' and args[1].endswith('/logs'):
            job = args[1].split('/')[-2]
            return 0, f'FAILED {self.tests.get(job, "")}\n' if job in self.tests else '', ''
        if args[0] == 'api' and '/annotations' in args[1]:
            return 0, '[]', ''
        if args[0] == 'api':
            return 0, '{"steps": []}', ''
        return 1, '', ''

    def triage(self, run, job, flags=None):
        product = env.Product('p', {'conventions': {'flags': flags or {}}})
        red = [{'name': 'tests (3.12)', 'link': LINK.format(run=run, job=job)}]
        return flake.triage(product, self.state, 'o/p', self.SHA, red, where='PR #7',
                            out=lambda *_: None, gh=self.gh)

    def reruns(self):
        return [c for c in self.calls if c[:2] == ['run', 'rerun']]

    def test_a_red_on_a_second_merge_ref_with_the_same_test_is_a_defect(self):
        self.tests = {'10': 'tests/test_app.py::test_x', '20': 'tests/test_app.py::test_x'}
        self.assertEqual(self.triage(1, 10), ([], ['tests (3.12)']))    # first red: re-run
        # the run is gone from the reruns record (another sha's TTL, a reopen): a new run
        data = flake.load(self.state)
        data['reruns'] = {}
        flake.save(self.state, data)
        self.calls.clear()
        self.assertEqual(self.triage(2, 20), (['tests (3.12)'], []))
        self.assertEqual(self.reruns(), [])

    def test_the_red_a_reopen_left_counts_as_the_first_merge_ref(self):
        self.tests = {'10': 'test_x', '20': 'test_x'}
        stale_ref.save(self.state, {'7': {'head': self.SHA, 'tip': 't', 'at': T0, 'reopens': 1,
                                          'links': {'tests (3.12)': LINK.format(run=1, job=10)}}})
        self.assertEqual(self.triage(2, 20), (['tests (3.12)'], []))
        self.assertEqual(self.reruns(), [])

    def test_a_different_test_failing_is_no_reproduction(self):
        self.tests = {'10': 'test_x', '20': 'test_y'}
        stale_ref.save(self.state, {'7': {'head': self.SHA, 'links': {
            'tests (3.12)': LINK.format(run=1, job=10)}}})
        self.assertEqual(self.triage(2, 20), ([], ['tests (3.12)']))
        self.assertEqual(len(self.reruns()), 1)

    def test_off_by_config(self):
        self.tests = {'10': 'test_x', '20': 'test_x'}
        stale_ref.save(self.state, {'7': {'head': self.SHA, 'links': {
            'tests (3.12)': LINK.format(run=1, job=10)}}})
        self.assertEqual(self.triage(2, 20, {'flake_skip_reproduced': 'off'}),
                         ([], ['tests (3.12)']))
        self.assertTrue(flake.settings(env.Product('p', {}))['skip_reproduced'])


if __name__ == '__main__':
    unittest.main()
