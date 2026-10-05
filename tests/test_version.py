"""asf.version — ASF's own releases are x.y.z versions cut by code on every merge that changes
asf/, shown wherever ASF prints what it runs, and enforced by the doctor, the tick and CI.

Hermetic: temp git repos (one bare "origin"), a temp ASF_HOME; no network, no gh."""
import argparse
import contextlib
import datetime
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from asf import doctor, drift, env, installs, upgrade, version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(cwd, *argv, env_vars=None):
    return subprocess.run(['git', '-C', cwd, *argv], check=True, capture_output=True, text=True,
                          env=env_vars).stdout.strip()


class RepoCase(unittest.TestCase):
    """A factory-source repo with an origin: v0.1.5 tagged, then three merges — asf/, docs, asf/."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='version_test_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(os.path.join(self.repo, 'asf'))
        os.makedirs(os.path.join(self.repo, 'tools'))
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@example.com')
        git(self.repo, 'config', 'user.name', 't')
        git(self.repo, 'config', 'tag.gpgSign', 'false')
        git(self.repo, 'remote', 'add', 'origin', self.origin)
        self.write('pyproject.toml', '[project]\nname = "asf-factory"\n')
        self.write('tools/forbidden-names.txt', '\\bacmecorp\\b\n')
        self.write('CHANGELOG.md', '# Changelog\n\nOne entry per released version, newest first.\n\n'
                                   '## v0.1.5 — 2026-10-01\n\n- older\n')
        self.base = self.commit('asf/a.py', 'a = 1\n', 'fix(a): the first (#1)')
        git(self.repo, 'tag', '-a', 'v0.1.5', '-m', 'v0.1.5', self.base)
        self.m1 = self.commit('asf/a.py', 'a = 2\n', 'fix(lane): a pushed branch gets its PR (#753)')
        self.docs = self.commit('docs/x.md', 'x\n', 'docs: a page (#754)')
        self.m2 = self.commit('asf/b.py', 'b = 1\n', 'feat(ids): one allocator for acmecorp ids (#756)')
        git(self.repo, 'push', '-q', 'origin', 'main', '--tags')
        git(self.repo, 'fetch', '-q', 'origin')

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as f:
            f.write(text)

    def commit(self, path, text, message, when=None):
        self.write(path, text)
        git(self.repo, 'add', '-A')
        stamp = when or '2026-10-04T12:00:00+00:00'
        git(self.repo, 'commit', '-q', '-m', message,
            env_vars={**os.environ, 'GIT_AUTHOR_DATE': stamp, 'GIT_COMMITTER_DATE': stamp})
        return git(self.repo, 'rev-parse', 'HEAD')


class ReadingAVersion(unittest.TestCase):
    def test_a_version_or_a_tag_is_the_tag_and_anything_else_is_not(self):
        self.assertEqual(version.tag_of('0.1.108'), 'v0.1.108')
        self.assertEqual(version.tag_of('v0.1.108'), 'v0.1.108')
        self.assertIsNone(version.tag_of('96fa0feca'))
        self.assertIsNone(version.tag_of('main'))

    def test_the_label_is_the_version_with_the_sha_as_a_detail(self):
        self.assertEqual(version.label('0.1.108', '96fa0feca0123456789'), '0.1.108 (96fa0feca)')
        self.assertEqual(version.label(None, '96fa0feca0123456789'), '96fa0feca')

    def test_describe_lines_read_as_versions(self):
        self.assertEqual(version.from_describe('v0.1.9'), '0.1.9')
        self.assertEqual(version.from_describe('v0.1.9-4-g205123f'), '0.1.9+4')
        self.assertIsNone(version.from_describe('release-2026-09-01-abc1234'))
        self.assertEqual(version.pep440('v0.1.9'), '0.1.9')
        self.assertEqual(version.pep440('v0.1.9-4-g205123f'), '0.1.9.post4+g205123f')
        self.assertEqual(version.pep440(''), version.FALLBACK)

    def test_the_build_derives_the_version_from_the_tag(self):
        spec = importlib.util.spec_from_file_location('asf_setup', os.path.join(ROOT, 'setup.py'))
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        self.assertEqual(setup.build_version('v0.1.108'), '0.1.108')
        self.assertEqual(setup.build_version('v0.1.107-15-g96fa0feca'), '0.1.107.post15+g96fa0feca')
        with tempfile.TemporaryDirectory() as d:
            setup.stamp('/nonexistent', d)
            ns = {}
            with open(os.path.join(d, 'asf', '_build.py'), encoding='utf-8') as f:
                exec(f.read(), ns)  # noqa: S102 — the file the build writes
        self.assertEqual(ns['VERSION'], '0.0.0')

    def test_the_version_is_never_a_hand_edited_constant(self):
        with open(os.path.join(ROOT, 'asf', '__init__.py'), encoding='utf-8') as f:
            self.assertNotRegex(f.read(), r'(?m)^__version__\s*=\s*["\']')
        with open(os.path.join(ROOT, 'pyproject.toml'), encoding='utf-8') as f:
            self.assertNotIn('asf.__version__', f.read())
        import asf
        self.assertRegex(asf.__version__, r'^\d+\.\d+\.\d+')

    def test_a_commit_reads_as_its_tag_or_the_nearest_one_past_it(self):
        case = RepoCase('setUp')
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.assertEqual(version.of_commit(case.repo, case.base), '0.1.5')
        self.assertEqual(version.of_commit(case.repo, case.m2), '0.1.5+3')
        self.assertEqual(version.pin_label(case.base, repo=case.repo), f'0.1.5 ({case.base[:9]})')


class CuttingReleases(RepoCase):
    def test_every_merge_that_changes_asf_gets_the_next_patch_version_in_order(self):
        self.assertEqual(version.plan(self.repo, 'HEAD'),
                         [(self.m1, 'v0.1.6'), (self.m2, 'v0.1.7')])

    def test_cut_tags_pushes_and_files_one_plain_entry_per_version(self):
        out = []
        tags = version.cut(self.repo, 'HEAD', push=True, pr_body=lambda n: (
            'What changed for you: a pushed branch now gets its pull request at once.'
            if n == '753' else ''), out=out.append)
        self.assertEqual(tags, ['v0.1.6', 'v0.1.7'])
        remote_tags = git(self.repo, 'ls-remote', '--tags', 'origin')
        self.assertIn('refs/tags/v0.1.6', remote_tags)
        self.assertIn('refs/tags/v0.1.7', remote_tags)
        git(self.repo, 'fetch', '-q', 'origin')
        log = git(self.repo, 'show', 'origin/main:CHANGELOG.md')
        self.assertLess(log.index('## v0.1.7'), log.index('## v0.1.6'))
        self.assertLess(log.index('## v0.1.6'), log.index('## v0.1.5'))
        self.assertIn('### Bugs fixed\n\n- A pushed branch now gets its pull request at once (#753)', log)
        self.assertIn('### Features landed\n\n- One allocator for a product ids (#756)', log)
        self.assertIn('git+https://github.com/fmogensen/ASF.git@v0.1.7', log)
        self.assertNotIn('acmecorp', log)
        self.assertNotIn(self.m1[:7], log)
        # the changelog commit changes no package path: nothing more to cut, and the rule holds
        self.assertEqual(version.plan(self.repo, 'origin/main'), [])
        ok, detail = version.health(self.repo, 'origin/main')
        self.assertTrue(ok, detail)
        self.assertIn('origin/main is 0.1.7+1', detail)
        # idempotent: a second run (a re-run, an overlapping push) cuts and files nothing
        self.assertEqual(version.cut(self.repo, 'origin/main', push=True, out=out.append), [])

    def test_the_changelog_commit_is_rebuilt_when_main_moved_under_it(self):
        other = os.path.join(self.tmp, 'other')
        git(self.tmp, 'clone', '-q', self.origin, other)
        git(other, 'config', 'user.email', 't@example.com')
        git(other, 'config', 'user.name', 't')
        real_push = {'n': 0}
        real_run = subprocess.run

        def run(cmd, *a, **kw):
            # the first changelog push races a docs merge landing on main
            if cmd[3:5] == ['push', '-q'] and cmd[-1].endswith(':refs/heads/main') \
                    and not real_push['n']:
                real_push['n'] += 1
                with open(os.path.join(other, 'README'), 'w') as f:
                    f.write('x\n')
                git(other, 'add', '-A')
                git(other, 'commit', '-q', '-m', 'docs: readme')
                git(other, 'push', '-q', 'origin', 'main')
            return real_run(cmd, *a, **kw)
        version.cut(self.repo, 'HEAD', push=True, pr_body=lambda n: '', run=run, out=lambda s: None)
        git(self.repo, 'fetch', '-q', 'origin')
        self.assertIn('## v0.1.7', git(self.repo, 'show', 'origin/main:CHANGELOG.md'))
        self.assertEqual(git(self.repo, 'show', 'origin/main:README'), 'x')


class EnforcingIt(RepoCase):
    NOW = datetime.datetime(2026, 10, 4, 15, 0, tzinfo=datetime.timezone.utc)

    def test_red_when_a_merge_that_changed_asf_has_no_version_past_the_grace(self):
        ok, detail = version.health(self.repo, 'origin/main', now=self.NOW)
        self.assertFalse(ok)
        self.assertIn('2 merge(s) on origin/main changed asf/ with no version tag', detail)

    def test_a_merge_inside_the_grace_is_not_red_yet(self):
        soon = datetime.datetime(2026, 10, 4, 12, 10, tzinfo=datetime.timezone.utc)
        ok, _detail = version.health(self.repo, 'origin/main', now=soon)
        self.assertTrue(ok)

    def test_red_when_the_changelog_has_no_entry_for_the_newest_tag(self):
        git(self.repo, 'tag', '-a', 'v0.1.6', '-m', 'x', self.m1)
        git(self.repo, 'tag', '-a', 'v0.1.7', '-m', 'x', self.m2)
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        ok, detail = version.health(self.repo, 'origin/main', now=later)
        self.assertFalse(ok)
        self.assertIn('CHANGELOG.md on origin/main has no entry for v0.1.7', detail)

    def test_the_doctor_row_is_the_rule_for_the_factory_and_silent_for_a_product(self):
        factory = env.Product('asf', {'repo_dir': self.repo, 'main': 'main',
                                      'ci': {'provider': 'none'}})
        with mock.patch.object(version, 'health', return_value=(False, 'no tag')) as health:
            self.assertEqual(doctor.check_release(factory), (False, 'no tag'))
        health.assert_called_once_with(self.repo, 'origin/main')
        os.remove(os.path.join(self.repo, 'pyproject.toml'))
        product = env.Product('p', {'repo_dir': self.repo, 'main': 'main',
                                    'ci': {'provider': 'none'}})
        ok, detail = doctor.check_release(product)
        self.assertTrue(ok)
        self.assertIn('not the factory source', detail)

    def test_the_tick_raises_it(self):
        lines = []
        calls = []

        def health(repo):
            calls.append(repo)
            return False, 'merge(s) with no version tag'
        ok, _ = drift.release_check(self.repo, lines.append, health=health)
        self.assertFalse(ok)
        self.assertEqual(lines, ['RELEASE RED: merge(s) with no version tag'])
        self.assertEqual(len(calls), 2)     # read again after fetching the tags

    def test_a_pr_that_changes_asf_shows_its_line_and_one_with_nothing_to_say_is_refused(self):
        git(self.repo, 'checkout', '-q', '-b', 'topic', self.base)
        self.commit('asf/c.py', 'c = 1\n', 'wip')
        ok, text = version.pr_entry(self.repo, 'v0.1.5', 'fix(c): the c reader (#9)', '')
        self.assertTrue(ok)
        self.assertEqual(text, 'Bugs fixed: - The c reader (#9)')
        ok, text = version.pr_entry(self.repo, 'v0.1.5', 'fix(c): ', '')
        self.assertFalse(ok)
        self.assertIn('no release-notes line', text)
        git(self.repo, 'checkout', '-q', '-b', 'docs-only', self.base)
        self.commit('docs/y.md', 'y\n', 'docs')
        ok, text = version.pr_entry(self.repo, 'v0.1.5', '', '')
        self.assertTrue(ok)
        self.assertIn('no change to asf/', text)

    def test_the_rollup_no_longer_cuts_the_factorys_own_releases(self):
        from asf.metrics import metrics
        product = env.Product('asf', {'repo_dir': self.repo, 'main': 'main',
                                      'ci': {'provider': 'none'}})
        with mock.patch.object(metrics, 'migrate_releases') as migrate:
            self.assertIsNone(metrics.write_trunk_release(self.tmp, '2026-10-04', {}, product))
        migrate.assert_not_called()

    def test_the_release_workflow_runs_on_every_push_to_main_and_on_prs(self):
        with open(os.path.join(ROOT, '.github', 'workflows', 'release.yml'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('push:\n    branches: [main]', text)
        self.assertIn('pull_request:', text)
        self.assertIn('python3 -m asf.version cut --ref "$GITHUB_SHA" --push', text)
        self.assertIn('python3 -m asf.version pr --base', text)


class ShownAsAVersion(RepoCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(env, 'ASF_HOME', os.path.join(self.tmp, 'home'))
        patch.start()
        self.addCleanup(patch.stop)
        git(self.repo, 'tag', '-a', 'v0.1.6', '-m', 'x', self.m1)
        fr = mock.patch.object(version, 'factory_repo', return_value=self.repo)
        fr.start()
        self.addCleanup(fr.stop)

    def test_the_install_record_writes_and_reads_its_pin_as_a_version(self):
        rec = installs.write('p', self.m1, '/v/asf-factory-p-x', previous={'sha': self.base,
                                                                           'venv': '/v/old'})
        with open(installs.record_path('p'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['version'], '0.1.6')
        rec = installs.read('p')
        self.assertEqual(rec.label, f'0.1.6 ({self.m1[:9]})')
        self.assertEqual(rec.previous_label, f'0.1.5 ({self.base[:9]})')
        self.assertIn(f'pin=0.1.6 ({self.m1[:9]})', doctor._pin_phrase(rec))

    def test_the_upgrade_table_and_the_status_name_each_pin_as_a_version(self):
        installs.write('p', self.m1, '/v/asf-factory-p-x')
        self.assertEqual(upgrade.pin_of('p'), f'0.1.6 ({self.m1[:9]})')
        self.assertEqual(upgrade.pin_of('q'), 'shared')
        from asf.views import status
        product = env.Product('p', {'repo_dir': self.repo, 'main': 'main', 'ci': {'provider': 'none'}})
        with mock.patch('asf.cli.version_string', return_value='0.1.6 (abc)'), \
                mock.patch('asf.cli.latest_release', return_value=('v0.1.6', None)):
            self.assertEqual(status.version_cell(product=product),
                             f'running 0.1.6 (abc) · p pinned 0.1.6 ({self.m1[:9]}) · '
                             'latest release 0.1.6')

    def test_a_pinned_products_tick_names_its_pin_as_a_version(self):
        installs.write('p', self.m1, '/v/asf-factory-p-x')
        lines = []
        drift.pin_line(env.Product('p', {'repo_dir': self.repo, 'main': 'main',
                                         'ci': {'provider': 'none'}}), lines.append)
        self.assertEqual(lines, [f'asf: pinned 0.1.6 ({self.m1[:9]})'])

    def test_the_factorys_tick_line_names_both_ends_as_versions(self):
        d = drift.Drift('0.1.5', self.base, self.m2, 3, True, '0.1.6+2')
        self.assertEqual(drift.line(d), f'factory: asf 0.1.5 ({self.base[:9]}) · trunk '
                                        f'0.1.6+2 ({self.m2[:9]}) · BEHIND by 3 commits')

    def test_a_pin_made_before_its_tag_was_cut_reads_as_that_version(self):
        from asf import cli
        with mock.patch.object(cli, '_build_describe', return_value='v0.1.5-1-gdeadbee'):
            self.assertEqual(cli._release(None, {'vcs_info': {'commit_id': self.m1}}), '0.1.6')

    def test_upgrade_to_takes_a_version(self):
        args = argparse.Namespace(rollback=False, ref='0.1.108', wait_s=None, force_ci=False,
                                  dry_run=True, sleep=None, ops=None, prune=False)
        with mock.patch.object(upgrade, 'move', return_value=0) as move:
            self.assertEqual(upgrade.cmd_move(args, 'p'), 0)
        self.assertEqual(move.call_args.kwargs['to'], 'v0.1.108')
        with mock.patch.object(upgrade, 'other_ticks', return_value=[]), \
                mock.patch.object(upgrade, 'repo_url', return_value='u'), \
                mock.patch.object(upgrade, 'resolve_ref', return_value=None) as resolve, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(upgrade.install('0.1.108', out=lambda s: None), 2)
        self.assertEqual(resolve.call_args.args[1], 'v0.1.108')


if __name__ == '__main__':
    unittest.main()
