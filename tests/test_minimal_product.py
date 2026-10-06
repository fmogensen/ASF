"""F-0248's headline acceptance: a MINIMAL product runs end to end with no product-specific config.

One developer, one model account, no cloud lane, no merge queue, GitHub-hosted CI (a stub ``gh``
that answers like a hosted repo whose trunk is green), no quota reader, no price table. The
product file names only what identifies the product — ``product``, ``repo_slug``, ``repo_dir``,
``backlog_dir``, ``main`` — and nothing else: no ``release:``, ``ci:``, ``conventions:``,
``capacity:`` or ``cloud:`` block. The operator config names one account on the fake runtime.

``asf doctor``, ``asf release-readiness`` and one full ``asf tick`` must all work on it, and every
criterion that does not apply to such a product must read ``n/a`` — met, never red (one that
applies but has no data yet on a fresh product reads ``pending``).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from asf import hermetic

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.test_minimal_product` does not
    from test_sample_product import _publish
    from test_scheduler import fake_clis
except ImportError:  # pragma: no cover - import shape only
    from tests.test_sample_product import _publish
    from tests.test_scheduler import fake_clis

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, 'sample')

#: A hosted forge whose trunk CI is green: ten finished push runs, no live run, no open PR. Every
#: other call answers empty, the way a repo with nothing in it does.
GH_STUB = r'''#!{python}
import json, sys
a = sys.argv[1:]
with open({log!r}, 'a') as f:
    f.write(' '.join(a) + '\n')
if a[:2] == ['run', 'list']:
    runs = [] if '--status' in a else [
        {{'databaseId': 100 + i, 'status': 'completed', 'conclusion': 'success', 'headSha': 'a' * 40,
          'createdAt': '2026-10-06T00:00:00Z', 'updatedAt': '2026-10-06T00:05:00Z',
          'workflowName': 'tests', 'name': 'tests', 'headBranch': 'main', 'event': 'push'}}
        for i in range(10)]
    print(json.dumps(runs))
elif a[:2] == ['run', 'view']:
    print(json.dumps({{'jobs': [{{'name': 'tests', 'steps': [{{'name': 'tests', 'conclusion': 'success'}}]}}]}}))
elif a[:2] == ['pr', 'list']:
    print('[]')
elif a[:1] == ['api']:
    print('' if '--jq' in a else '[]')
elif a[:2] == ['auth', 'status']:
    print('Logged in')
else:
    sys.stderr.write('gh: not answered in tests\n')
    sys.exit(1)
'''

#: The criteria a minimal product has nothing for: each reads n/a, met.
NOT_APPLICABLE = ('stability', 'repair', 'install', 'upgrade', 'generic', 'docs', 'blocking', 'tune')


class MinimalProductTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = os.path.realpath(tempfile.mkdtemp(prefix='asf_minimal_'))
        shutil.copytree(SAMPLE, os.path.join(cls.tmp, 'sample'))
        repo = os.path.join(cls.tmp, 'sample', 'repo')
        backlog = os.path.join(cls.tmp, 'sample', 'backlog')
        _publish(repo, os.path.join(cls.tmp, 'repo.git'))
        _publish(backlog, os.path.join(cls.tmp, 'backlog.git'))
        cls.home = os.path.join(cls.tmp, 'asf-home')
        os.makedirs(os.path.join(cls.home, 'products'))
        cls.product_file = os.path.join(cls.home, 'products', 'minimal.yaml')
        with open(cls.product_file, 'w', encoding='utf-8') as f:
            f.write(f'product: minimal\nrepo_slug: example/minimal\nrepo_dir: {repo}\n'
                    f'backlog_dir: {backlog}\nmain: main\n')
        with open(os.path.join(cls.home, 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write('default_product: minimal\nscheduler:\n  kind: none\nworker_pool:\n  backend: fake\n'
                    f'  fake_script: {cls.tmp}/sample/fake_script.json\n'
                    '  models: {heavy: fake-heavy, light: fake-light}\n'
                    '  accounts:\n    - name: dev\n      role: local\n')
        bindir = os.path.join(cls.tmp, 'bin')
        fake_clis(bindir)
        cls.gh_log = os.path.join(cls.tmp, 'gh.log')
        gh = os.path.join(bindir, 'gh')
        with open(gh, 'w', encoding='utf-8') as f:
            f.write(GH_STUB.format(python=sys.executable, log=cls.gh_log))
        os.chmod(gh, 0o755)
        cls.env = hermetic.build(dict(os.environ, ASF_HOME=cls.home, PYTHONPATH=ROOT, GH_TOKEN='',
                                      GIT_AUTHOR_NAME='dev', GIT_AUTHOR_EMAIL='dev@example.com',
                                      GIT_COMMITTER_NAME='dev', GIT_COMMITTER_EMAIL='dev@example.com',
                                      PATH=bindir + os.pathsep + os.environ.get('PATH', '')),
                                 home=cls.tmp)
        cls.init = cls.asf('init')
        # the operator's own install step (as tools/install.sh ends), not product config
        cls.asf('console-permissions', 'install', '--scope', 'user')
        cls.doctor = cls.asf('doctor')
        cls.ready = cls.asf('release-readiness', '--json')
        cls.tick = cls.asf('tick')
        cls.ready_after = cls.asf('release-readiness', '--json')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def asf(cls, *argv):
        return subprocess.run([sys.executable, '-m', 'asf.cli', *argv, '--product', 'minimal'],
                              cwd=cls.tmp, env=cls.env, capture_output=True, text=True, timeout=300)

    def test_the_product_file_carries_no_product_specific_config(self):
        with open(self.product_file, encoding='utf-8') as f:
            keys = [ln.split(':', 1)[0] for ln in f if ln.strip() and not ln.startswith((' ', '#'))]
        self.assertEqual(sorted(keys), ['backlog_dir', 'main', 'product', 'repo_dir', 'repo_slug'])

    def test_doctor_is_green(self):
        self.assertEqual(self.init.returncode, 0, self.init.stdout + self.init.stderr)
        self.assertEqual(self.doctor.returncode, 0, self.doctor.stdout + self.doctor.stderr)
        red = [ln for ln in self.doctor.stdout.splitlines() if ' RED ' in ln]
        self.assertEqual(red, [])

    def test_release_readiness_reads_na_never_red(self):
        for proc in (self.ready, self.ready_after):
            self.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)   # a verdict, not a crash
            out = json.loads(proc.stdout[proc.stdout.index('{'):])
            crit = {c['key']: c for c in out['criteria']}
            for key in NOT_APPLICABLE:
                self.assertTrue(crit[key]['met'], crit[key])
                self.assertTrue(crit[key]['evidence'].startswith('n/a'), (key, crit[key]['evidence']))
            # what applies but has no data yet (a fresh product) is pending — never a red reading
            red = {k: c['evidence'] for k, c in crit.items()
                   if not c['met'] and not c['evidence'].startswith('pending')}
            self.assertEqual(red, {})
            self.assertEqual(crit['ci']['evidence'], '10/10 green')
        after = {c['key']: c for c in json.loads(
            self.ready_after.stdout[self.ready_after.stdout.index('{'):])['criteria']}
        self.assertTrue(after['seats']['met'], after['seats'])         # the tick's one seat reading

    def test_one_full_tick_runs_every_step_and_launches(self):
        self.assertEqual(self.tick.returncode, 0, self.tick.stdout + self.tick.stderr)
        self.assertNotIn('FAILED', self.tick.stdout)
        self.assertIn('launched fix-bug-b-0001', self.tick.stdout)
        self.assertIn('→ dev', self.tick.stdout)
        summary = [ln for ln in self.tick.stdout.splitlines() if ln.startswith('TICK — ')]
        self.assertEqual(len(summary), 1, self.tick.stdout)
        steps = summary[0][len('TICK — '):].split(', ')
        self.assertTrue(steps and all(s.endswith(' ok') for s in steps), summary)


if __name__ == '__main__':
    unittest.main()
