import ast
import json
import os
import subprocess
import tempfile
import unittest

from asf import env
from asf.evidence import sources

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_evidence_sources` does not
    from gitfixture import Template
except ImportError:  # pragma: no cover - import shape only
    from tests.gitfixture import Template

HERE = os.path.dirname(os.path.abspath(__file__))


def _sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _build_repo(root):
    origin = os.path.join(root, 'origin.git')
    repo = os.path.join(root, 'repo')
    _sh(['git', 'init', '-q', '--bare', '-b', 'main', origin], root)
    _sh(['git', 'clone', '-q', origin, repo], root)
    with open(os.path.join(repo, 'README.md'), 'w') as f:
        f.write('hello\n')
    _sh(['git', 'add', '-A'], repo)
    _sh(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '-m', 'seed'], repo)
    _sh(['git', 'push', '-q', 'origin', 'HEAD:main'], repo)


REPO = Template(_build_repo, prefix='evidence_sources_test_')


class BaseShapeTests(unittest.TestCase):
    """§3.1: every public method of the three bases raises NotImplementedError or returns its
    documented default when called on the base."""

    def test_git_source(self):
        base = sources.GitSource()
        self.assertRaises(NotImplementedError, base.run, ['status'])
        self.assertRaises(NotImplementedError, base.cat_file, 'check', ['HEAD:x'])
        self.assertIsNone(base.fetch())

    def test_host_source(self):
        base = sources.HostSource()
        self.assertRaises(NotImplementedError, base.prs)
        self.assertRaises(NotImplementedError, base.runs, 'ci.yml')
        self.assertEqual(base.run_jobs(1), [])

    def test_deploy_source(self):
        base = sources.DeploySource()
        self.assertRaises(NotImplementedError, base.deployment, 'prod')
        self.assertRaises(NotImplementedError, base.sha, 'prod')


class LocalGitTests(unittest.TestCase):
    def setUp(self):
        self.base = REPO.fresh()
        self.repo_dir = os.path.join(self.base, 'repo')
        self.product = env.Product('t', {'repo_dir': self.repo_dir})
        self.git = sources.LocalGit(self.product)

    def test_run_reads_the_trunk_sha(self):
        head = self.git.run(['rev-parse', 'HEAD'])
        self.assertRegex(head, r'^[0-9a-f]{40}$')
        self.assertEqual(self.git.run(['rev-parse', 'origin/main']), head)

    def test_an_unknown_subcommand_is_empty_not_a_raise(self):
        self.assertEqual(self.git.run(['no-such-subcommand']), '')

    def test_a_bad_cwd_is_empty_not_a_raise(self):
        bad = sources.LocalGit(env.Product('t', {'repo_dir': '/no/such/path'}))
        self.assertEqual(bad.run(['rev-parse', 'HEAD']), '')

    def test_cwd_overrides_the_products_repo_dir(self):
        other = tempfile.mkdtemp(prefix='evidence_sources_alt_')
        _sh(['git', 'init', '-q', '-b', 'main', other], other)
        _sh(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty',
             '-m', 'alt'], other)
        alt_head = self.git.run(['rev-parse', 'HEAD'], cwd=other)
        self.assertNotEqual(alt_head, self.git.run(['rev-parse', 'HEAD']))

    def test_cat_file_check_answers_in_order_none_for_missing(self):
        out = self.git.cat_file('check', ['origin/main:README.md', 'origin/main:missing.md'])
        self.assertEqual(len(out), 2)
        self.assertRegex(out[0], r'^[0-9a-f]{40}$')
        self.assertIsNone(out[1])

    def test_cat_file_blob_returns_bytes(self):
        out = self.git.cat_file('blob', ['origin/main:README.md'])
        self.assertEqual(out, [b'hello\n'])

    def test_fetch_prunes_origin(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        git = sources.LocalGit(self.product, run=fake_run)
        self.assertIsNone(git.fetch())
        self.assertEqual(calls, [['git', 'fetch', '--prune', '-q', 'origin']])


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


class HostTests(unittest.TestCase):
    def setUp(self):
        # a distinct product name per test: GitHubHost's PR cache is per-product (F-0087) and
        # would otherwise leak a `prs()` answer from one test into the next
        self.product = env.Product(self.id().rsplit('.', 1)[-1], {'repo_slug': 'org/repo'})

    def test_prs_builds_the_field_list_and_parses_json(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return _FakeProc(json.dumps([{'number': 1, 'state': 'OPEN'}]))

        host = sources.GitHubHost(self.product, run=fake_run)
        self.assertEqual(host.prs(), [{'number': 1, 'state': 'OPEN'}])
        argv = calls[0]
        self.assertEqual(argv[:3], ['gh', 'pr', 'list'])
        self.assertIn('-R', argv)
        self.assertIn('org/repo', argv)
        self.assertIn(','.join(sources.HostSource.PR_FIELDS), argv)

    def test_runs_passes_branch_and_status_when_given(self):
        def fake_run(args, **kwargs):
            return _FakeProc(json.dumps([{'headSha': 'a', 'conclusion': 'success'}]))

        host = sources.GitHubHost(self.product, run=fake_run)
        out = host.runs('ci.yml', branch='main', status='success', limit=5)
        self.assertEqual(out, [{'headSha': 'a', 'conclusion': 'success'}])

    def test_run_jobs_extracts_name_and_conclusion(self):
        def fake_run(args, **kwargs):
            return _FakeProc(json.dumps({'jobs': [{'name': 'gate', 'conclusion': 'success',
                                                    'id': 9}]}))

        host = sources.GitHubHost(self.product, run=fake_run)
        self.assertEqual(host.run_jobs(1), [{'name': 'gate', 'conclusion': 'success'}])

    def test_unreadable_json_answers_empty(self):
        def fake_run(args, **kwargs):
            return _FakeProc('not json')

        host = sources.GitHubHost(self.product, run=fake_run)
        self.assertEqual(host.prs(), [])
        self.assertEqual(host.runs('ci.yml'), [])

    def test_no_host_starts_no_process(self):
        def raising(*a, **k):
            raise AssertionError('NoHost must never call run')

        host = sources.NoHost(run=raising)
        self.assertEqual(host.prs(), [])
        self.assertEqual(host.runs('ci.yml'), [])
        self.assertEqual(host.run_jobs(1), [])


class _RecordingHost(sources.HostSource):
    """A HostSource fed canned `runs`/`run_jobs` answers, for WorkflowDeploy."""

    def __init__(self, runs_by_workflow, jobs_by_run=None):
        self._runs = runs_by_workflow
        self._jobs = jobs_by_run or {}

    def runs(self, workflow, branch=None, status=None, limit=20):
        return self._runs.get(workflow, [])

    def run_jobs(self, run_id):
        return self._jobs.get(run_id, [])


class DeployTests(unittest.TestCase):
    def test_prod_is_the_newest_success_of_deploy_sha_prod_workflow(self):
        product = env.Product('t', {'deploy_sha': {'prod': {'workflow': 'deploy-prod.yml'}}})
        host = _RecordingHost({'deploy-prod.yml': [
            {'headSha': 'old', 'conclusion': 'success', 'updatedAt': '2026-01-01T00:00:00Z'},
            {'headSha': 'new', 'conclusion': 'failure', 'updatedAt': '2026-01-02T00:00:00Z'},
        ]})
        deploy = sources.WorkflowDeploy(product, host)
        self.assertEqual(deploy.sha('prod'), 'old')

    def test_dev_is_the_newest_trunk_run_whose_dev_job_succeeded(self):
        # newest first: 'a' is newer but its dev job failed, so 'b' is the answer
        product = env.Product('t', {'main': 'main',
                                    'ci': {'workflow': 'ci.yml', 'dev_job': 'test'}})
        host = _RecordingHost(
            {'ci.yml': [{'headSha': 'a', 'status': 'completed', 'databaseId': 1},
                       {'headSha': 'b', 'status': 'completed', 'databaseId': 2}]},
            jobs_by_run={1: [{'name': 'test', 'conclusion': 'failure'}],
                        2: [{'name': 'test', 'conclusion': 'success'}]})
        deploy = sources.WorkflowDeploy(product, host)
        self.assertEqual(deploy.sha('dev'), 'b')

    def test_unknown_env_is_none(self):
        product = env.Product('t', {'deploy_sha': {'prod': {'workflow': 'deploy-prod.yml'}}})
        deploy = sources.WorkflowDeploy(product, _RecordingHost({}))
        self.assertIsNone(deploy.sha('unknown-env'))

    def test_no_deploy_is_none_for_every_environment(self):
        deploy = sources.NoDeploy()
        self.assertIsNone(deploy.sha('prod'))
        self.assertIsNone(deploy.sha('dev'))
        self.assertIsNone(deploy.sha('anything'))


class RecordedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='evidence_sources_recorded_')
        with open(os.path.join(self.dir, 'git.json'), 'w') as f:
            json.dump({
                'git.run:rev-parse origin/main': '9f1c2b0',
                'git.cat_file:check:origin/main:docs/specs/f-0002.md': '4b7a',
                'git.cat_file:blob:origin/main:docs/specs/f-0002.md':
                    __import__('base64').b64encode(b'spec text').decode(),
            }, f)
        with open(os.path.join(self.dir, 'host.json'), 'w') as f:
            json.dump({
                'host.prs': [{'number': 41, 'state': 'OPEN', 'mergeable': 'CONFLICTING'}],
                'host.runs:ci.yml:main': [{'headSha': 'a'}],
            }, f)
        with open(os.path.join(self.dir, 'deploy.json'), 'w') as f:
            json.dump({'deploy.deployment:prod': ['9f1c2b0', '2026-01-01T00:00:00Z']}, f)
        self.recorded = sources.Recorded(self.dir)

    def test_a_recorded_key_answers(self):
        self.assertEqual(self.recorded.run(['rev-parse', 'origin/main']), '9f1c2b0')
        self.assertEqual(self.recorded.prs(),
                         [{'number': 41, 'state': 'OPEN', 'mergeable': 'CONFLICTING'}])
        self.assertEqual(self.recorded.runs('ci.yml', branch='main'), [{'headSha': 'a'}])
        self.assertEqual(self.recorded.sha('prod'), '9f1c2b0')
        self.assertEqual(self.recorded.deployment('prod'), ('9f1c2b0', '2026-01-01T00:00:00Z'))

    def test_an_unrecorded_key_raises_key_error_naming_the_call(self):
        with self.assertRaises(KeyError) as cm:
            self.recorded.runs('ci.yml', branch='release')
        self.assertIn('host.runs:ci.yml:release', str(cm.exception))

    def test_a_blob_answer_round_trips_through_base64(self):
        out = self.recorded.cat_file('blob', ['origin/main:docs/specs/f-0002.md'])
        self.assertEqual(out, [b'spec text'])

    def test_a_check_answer_is_read_straight(self):
        out = self.recorded.cat_file('check', ['origin/main:docs/specs/f-0002.md'])
        self.assertEqual(out, ['4b7a'])


class ForProductTests(unittest.TestCase):
    def test_no_host_for_ci_none(self):
        product = env.Product('t', {'repo_dir': '/tmp', 'ci': {'provider': 'none'}})
        src = sources.for_product(product)
        self.assertIsInstance(src.host, sources.NoHost)

    def test_github_host_for_gh_actions(self):
        product = env.Product('t', {'repo_dir': '/tmp', 'ci': {'provider': 'gh-actions'}})
        src = sources.for_product(product)
        self.assertIsInstance(src.host, sources.GitHubHost)

    def test_no_deploy_for_a_product_naming_no_deploy_workflow(self):
        product = env.Product('t', {'repo_dir': '/tmp'})
        src = sources.for_product(product)
        self.assertIsInstance(src.deploy, sources.NoDeploy)

    def test_workflow_deploy_for_a_configured_deploy_sha(self):
        product = env.Product('t', {'repo_dir': '/tmp',
                                    'deploy_sha': {'prod': {'workflow': 'deploy-prod.yml'}}})
        src = sources.for_product(product)
        self.assertIsInstance(src.deploy, sources.WorkflowDeploy)

    def test_each_override_is_honoured(self):
        product = env.Product('t', {'repo_dir': '/tmp'})
        git, host, deploy = object(), object(), object()
        src = sources.for_product(product, git=git, host=host, deploy=deploy)
        self.assertIs(src.git, git)
        self.assertIs(src.host, host)
        self.assertIs(src.deploy, deploy)

    def test_no_host_starts_no_process_even_with_a_raising_run(self):
        def raising(*a, **k):
            raise AssertionError('must never be called')

        product = env.Product('t', {'repo_dir': '/tmp', 'ci': 'none'})
        src = sources.for_product(product, host=sources.NoHost(run=raising))
        self.assertEqual(src.host.prs(), [])


class NoVendorOutsideTheProviderTests(unittest.TestCase):
    """§2.2: the direction of the passes (ingest reads evidence, never the reverse) is enforced
    by the import graph — `asf/evidence/sources.py` never imports `asf.record`."""

    def test_sources_does_not_import_record(self):
        path = os.path.join(HERE, '..', 'asf', 'evidence', 'sources.py')
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(n.name for n in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertFalse(any(m == 'asf.record' or m.startswith('asf.record.') for m in imported))


if __name__ == '__main__':
    unittest.main()
