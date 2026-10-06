"""tests/test_release_preview.py — the preview release gate (asf.release_preview), its selection
through ``release.gate`` / ``--gate`` (asf.release), the known-issues page, the pre-release tag
(asf.version.prerelease), the stub runtime's writes (asf.workers.runtime.FakeRuntime) and the
privacy sweep (tools/check_privacy.py). Hermetic: git and the forge are fakes, or temp repos."""
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from asf import env, release, release_preview as rp, version
from asf.workers import runtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import check_privacy  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
GREEN_STEPS = ['install from zero, Linux container (pipx, doctor green)',
               'install from zero, macOS fresh HOME (pipx, doctor green)',
               'a minimal product, end to end',
               'README-only first-user run (Quick start to a landed Task, stub runtime)',
               'check generic', 'privacy sweep (no operator paths, e-mails or private links)']
README = '# P\n## Install\n## Quick start\n## Feedback\nIssues.\n'
NOTES = '# Release notes\n## v0.1.0-preview\n- first\n'
KNOWN = '# Known issues\n## Not yet met for 1.0\n- x\n'


def product(release_block=None, slug='o/r'):
    data = {'repo_dir': '/repo', 'main': 'main'}
    if slug:
        data['repo_slug'] = slug
    if release_block is not None:
        data['release'] = release_block
    return env.Product('p', data)


def fakes(runs=None, steps=None, files=None, tag='v0.1.7', templates=True):
    """``(git, gh_json)``: ``runs`` newest first (default: one green run), ``steps`` per run id
    (default: every preview step green on run 1), ``files`` the trunk's files."""
    runs = runs if runs is not None else [{'databaseId': 1, 'status': 'completed', 'conclusion': 'success',
                                           'headSha': 'a' * 40, 'workflowName': 'tests'}]
    steps = steps if steps is not None else {1: [(n, 'success') for n in GREEN_STEPS]}
    files = dict({'README.md': README, 'docs/RELEASE-NOTES.md': NOTES, 'docs/KNOWN-ISSUES.md': KNOWN,
                  'CHANGELOG.md': f'## {tag}\n- a line\n' if tag else ''}, **(files or {}))
    calls = []

    def git(repo, *args):
        calls.append(args)
        if args[0] == 'rev-parse':
            return 'sha\n'
        if args[0] == 'describe':
            return (tag + '\n') if tag else None
        if args[0] == 'show':
            return files.get(args[1].split(':', 1)[1])
        if args[0] == 'ls-tree':
            return '.github/ISSUE_TEMPLATE/bug.yml\n' if templates else ''
        return ''

    def gh(args):
        if args[:2] == ['run', 'list']:
            return runs
        rid = int(args[2])
        return {'jobs': [{'name': 'j', 'steps': [{'name': n, 'conclusion': c}
                                                 for n, c in steps.get(rid, [])]}]}
    git.calls = calls
    return git, gh


def run(prod=None, **kw):
    git, gh = fakes(**kw)
    return rp.compute(prod or product(), now=NOW, git=git, gh_json=gh)


def met(d):
    return {c['key']: c['met'] for c in d['criteria']}


def evidence(d, key):
    return next(c['evidence'] for c in d['criteria'] if c['key'] == key)


class GateSelectionTest(unittest.TestCase):
    def test_the_default_gate_is_1_0(self):
        self.assertEqual(rp.gate_of(product()), '1.0')
        self.assertEqual(rp.gate_of(product({})), '1.0')

    def test_release_gate_picks_preview_and_the_flag_overrides_it(self):
        self.assertEqual(rp.gate_of(product({'gate': 'preview'})), 'preview')
        self.assertEqual(rp.gate_of(product({'gate': 1.0})), '1.0')
        self.assertEqual(rp.gate_of(product({'gate': 'preview'}), '1.0'), '1.0')
        self.assertEqual(rp.gate_of(product(), 'preview'), 'preview')

    def test_an_unknown_gate_is_refused_never_a_silent_fall_back(self):
        with self.assertRaises(ValueError):
            rp.gate_of(product({'gate': 'beta'}))

    def test_release_compute_with_the_preview_gate_prints_only_the_five(self):
        git, gh = fakes()
        d = release.compute('/no-record', product({'gate': 'preview'}), now=NOW, git=git, gh_json=gh)
        self.assertEqual([c['key'] for c in d['criteria']],
                         ['install', 'minimal', 'readme', 'privacy', 'ship'])
        self.assertEqual(d['gate'], 'preview')
        self.assertIn('(gate preview)', release.render(d))

    def test_the_cli_takes_gate_and_known_issues(self):
        from asf.cli import build_parser
        a = build_parser().parse_args(['release-readiness', '--gate', 'preview'])
        self.assertEqual((a.gate, a.known_issues), ('preview', False))
        a = build_parser().parse_args(['release-readiness', '--known-issues'])
        self.assertTrue(a.known_issues)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(['release-readiness', '--gate', 'beta'])

    def test_cmd_passes_the_flag_to_compute(self):
        args = mock.Mock(product='p', json=False, gate='preview', known_issues=False)
        d = {'product': 'p', 'as_of': 'now', 'ready': True, 'gate': 'preview',
             'criteria': [{'key': 'ship', 'name': 'S', 'met': True, 'evidence': 'ok'}]}
        with mock.patch.object(env, 'load_product', return_value=product()), \
                mock.patch.object(release, 'compute', return_value=d) as compute, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(release.cmd_release_readiness(args, '/r'), 0)
        self.assertEqual(compute.call_args.kwargs['gate'], 'preview')


class PreviewCriteriaTest(unittest.TestCase):
    def test_all_green_is_ready(self):
        d = run()
        self.assertTrue(d['ready'], d)
        self.assertEqual(set(met(d).values()), {True})

    def test_a_missing_step_is_red_and_named(self):
        d = run(steps={1: [(n, 'success') for n in GREEN_STEPS if 'macOS' not in n]})
        self.assertFalse(met(d)['install'])
        self.assertIn("'install from zero, macos fresh home' absent", evidence(d, 'install'))
        self.assertTrue(met(d)['readme'])

    def test_a_red_step_is_red(self):
        steps = {1: [(n, 'failure' if n.startswith('privacy') else 'success') for n in GREEN_STEPS]}
        d = run(steps=steps)
        self.assertFalse(met(d)['privacy'])
        self.assertIn("'privacy sweep' red", evidence(d, 'privacy'))

    def test_a_step_is_read_from_the_newest_run_that_ran_it(self):
        runs = [{'databaseId': 2, 'status': 'completed', 'conclusion': 'success', 'headSha': 'b' * 40},
                {'databaseId': 1, 'status': 'completed', 'conclusion': 'success', 'headSha': 'a' * 40}]
        steps = {2: [(n, 'success') for n in GREEN_STEPS if 'install' not in n],
                 1: [(n, 'success') for n in GREEN_STEPS if 'install' in n]}
        d = run(runs=runs, steps=steps)
        self.assertTrue(met(d)['install'], evidence(d, 'install'))
        self.assertIn('at aaaaaaa', evidence(d, 'install'))

    def test_the_minimal_step_skipped_reads_red(self):
        steps = {1: [(n, 'skipped' if 'minimal' in n else 'success') for n in GREEN_STEPS]}
        self.assertFalse(met(run(steps=steps))['minimal'])

    def test_no_forge_is_red_not_a_crash(self):
        d = run(prod=product(slug=None))
        self.assertFalse(d['ready'])
        self.assertIn('no forge', evidence(d, 'install'))

    def test_ship_needs_the_newest_tested_commit_green_in_every_run(self):
        runs = [{'databaseId': 1, 'status': 'completed', 'conclusion': 'success', 'headSha': 'a' * 40,
                 'workflowName': 'tests'},
                {'databaseId': 3, 'status': 'completed', 'conclusion': 'failure', 'headSha': 'a' * 40,
                 'workflowName': 'install'}]
        d = run(runs=runs)
        self.assertFalse(met(d)['ship'])
        self.assertIn('1/2 run(s) green (red: install)', evidence(d, 'ship'))

    def test_the_newest_tested_commit_is_the_newest_that_ran_the_suite(self):
        runs = [{'databaseId': 5, 'status': 'completed', 'conclusion': 'failure', 'headSha': 'c' * 40,
                 'workflowName': 'release'},
                {'databaseId': 1, 'status': 'completed', 'conclusion': 'success', 'headSha': 'a' * 40,
                 'workflowName': 'tests'}]
        d = run(runs=runs)
        self.assertIn('newest tested aaaaaaa: 1/1', evidence(d, 'ship'))

    def test_ship_needs_a_tag_notes_known_issues_and_a_feedback_channel(self):
        cases = [({'tag': None}, 'no version tag'),
                 ({'files': {'docs/RELEASE-NOTES.md': None}}, 'docs/RELEASE-NOTES.md missing'),
                 ({'files': {'docs/KNOWN-ISSUES.md': '# Known issues\n'}}, 'has no section'),
                 ({'templates': False}, 'no template under .github/ISSUE_TEMPLATE'),
                 ({'files': {'README.md': '# P\n## Install\n'}}, 'README has no Feedback heading')]
        for kw, needle in cases:
            with self.subTest(needle=needle):
                d = run(**kw)
                self.assertFalse(met(d)['ship'])
                self.assertIn(needle, evidence(d, 'ship'))

    def test_a_url_feedback_channel_needs_its_url(self):
        d = run(prod=product({'feedback': {'kind': 'url'}}))
        self.assertIn('release.feedback.url unset', evidence(d, 'ship'))
        d = run(prod=product({'feedback': {'kind': 'url', 'url': 'https://forum.example.com/asf'}}),
                templates=False)
        self.assertTrue(met(d)['ship'], evidence(d, 'ship'))

    def test_pre_release_tags_are_not_the_version_tag(self):
        git, gh = fakes()
        rp.compute(product(), now=NOW, git=git, gh_json=gh)
        describe = [c for c in git.calls if c[0] == 'describe']
        self.assertTrue(describe and all('--exclude' in c and '*-*' in c for c in describe), describe)

    def test_configured_step_names_and_files_are_read(self):
        prod = product({'ci_steps': {'privacy': 'my sweep'}, 'preview': {'notes': 'NOTES.md'}})
        steps = {1: [(n, 'success') for n in GREEN_STEPS if not n.startswith('privacy')] + [('my sweep', 'success')]}
        d = run(prod=prod, steps=steps, files={'NOTES.md': 'x\n'})
        self.assertTrue(d['ready'], d)


class KnownIssuesTest(unittest.TestCase):
    def test_the_page_lists_open_criteria_and_defects(self):
        text = rp.known_issues([{'name': 'Stability', 'evidence': '2 hand fixes'}],
                               [('B-0001', 'Spawn fails', 'S1')])
        self.assertIn('## Not yet met for 1.0', text)
        self.assertIn('- **Stability** — 2 hand fixes', text)
        self.assertIn('## Known defects', text)
        self.assertIn('- B-0001 (S1) — Spawn fails', text)

    def test_empty_sections_say_so(self):
        text = rp.known_issues([], [])
        self.assertIn('every 1.0 criterion is met', text)
        self.assertIn('- none open.', text)

    def test_open_defects_reads_open_bugs_of_the_listed_severities(self):
        def bug(iid, state, sev):
            return (f'---\nid: {iid}\ntype: bug\ntitle: "{iid} title"\nparent: E-0001\nseverity: {sev}\n'
                    f'# ---- machine ----\nschema_version: 1\nstate: {state}\n'
                    f'updated: 2026-09-20T12:00:00Z\n---\n## Description\nx\n')
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'bugs'))
            for iid, state, sev in (('B-0001', 'New', 'S2'), ('B-0002', 'Closed', 'S1'),
                                    ('B-0003', 'Active', 'S1'), ('B-0004', 'New', 'S3')):
                with open(os.path.join(d, 'bugs', f'{iid}.md'), 'w') as f:
                    f.write(bug(iid, state, sev))
            self.assertEqual(rp.open_defects(d), [('B-0003', 'B-0003 title', 'S1'),
                                                  ('B-0001', 'B-0001 title', 'S2')])
            self.assertEqual([r[0] for r in rp.open_defects(d, ('S3',))], ['B-0004'])
        self.assertEqual(rp.defect_severities(product({'preview': {'defect_severities': ['s1']}})), ('S1',))

    def test_the_tracked_page_has_both_sections(self):
        with open(os.path.join(ROOT, 'docs', 'KNOWN-ISSUES.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('## Not yet met for 1.0', text)
        self.assertIn('## Known defects', text)


def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(d):
    _git(d, 'init', '-q', '-b', 'main')
    _git(d, 'config', 'user.email', 't@example.com')
    _git(d, 'config', 'user.name', 't')
    _git(d, 'commit', '-q', '--allow-empty', '-m', 'one')
    return _git(d, 'rev-parse', 'HEAD')


class PrereleaseTagTest(unittest.TestCase):
    def test_tags_once_and_never_moves(self):
        with tempfile.TemporaryDirectory() as d:
            first = _repo(d)
            ok, detail = version.prerelease(d, 'v0.1.0-preview', 'HEAD')
            self.assertTrue(ok, detail)
            self.assertEqual(_git(d, 'rev-parse', 'v0.1.0-preview^{commit}'), first)
            self.assertTrue(version.prerelease(d, 'v0.1.0-preview', 'HEAD')[0])     # idempotent
            _git(d, 'commit', '-q', '--allow-empty', '-m', 'two')
            ok, detail = version.prerelease(d, 'v0.1.0-preview', 'HEAD')
            self.assertFalse(ok)
            self.assertIn('never moves', detail)

    def test_refuses_a_tag_that_is_not_a_pre_release(self):
        for tag in ('v0.1.0', 'preview', 'v0.1.0-'):
            with self.subTest(tag=tag):
                self.assertFalse(version.prerelease('/nowhere', tag)[0])

    def test_a_pre_release_tag_is_never_read_as_the_version(self):
        with tempfile.TemporaryDirectory() as d:
            _repo(d)
            _git(d, 'tag', 'v0.1.7')
            _git(d, 'commit', '-q', '--allow-empty', '-m', 'two')
            _git(d, 'tag', '-a', 'v0.1.0-preview', '-m', 'p')
            self.assertEqual(version.of_commit(d, 'HEAD'), '0.1.7+1')
            self.assertEqual(sorted(version.version_tags(d).values()), ['v0.1.7'])


class StubRuntimeWritesTest(unittest.TestCase):
    def test_a_matching_job_commits_its_writes_and_pushes_its_branch(self):
        with tempfile.TemporaryDirectory() as d:
            origin = os.path.join(d, 'origin.git')
            _git(d, 'init', '-q', '--bare', '-b', 'main', origin)
            wt = os.path.join(d, 'wt')
            os.makedirs(wt)
            _repo(wt)
            _git(wt, 'remote', 'add', 'origin', origin)
            _git(wt, 'checkout', '-q', '-b', 'feature/T-0007')
            brief = os.path.join(d, 'brief.md')
            with open(brief, 'w') as f:
                f.write('do it')
            script = {'results': [{'ok': True, 'result': 'other'}],
                      'jobs': {'coder-t-*': {'ok': True, 'result': 'done', 'writes': [
                          {'path': 'notes/{item_lower}.md', 'text': '{item} done\n',
                           'subject': 'task({item}): done'}]}}}
            rt = runtime.FakeRuntime(script)
            job = runtime.Job('p', 'coder-t-0007', wt, brief, 'm', log_path=os.path.join(d, 'log.jsonl'))
            res = rt.run(job)
            self.assertTrue(res.ok)
            self.assertEqual(res.text, 'done')
            with open(os.path.join(wt, 'notes', 't-0007.md')) as f:
                self.assertEqual(f.read(), 'T-0007 done\n')
            self.assertEqual(_git(origin, 'log', '-1', '--format=%s', 'feature/T-0007'), 'task(T-0007): done')
            other = runtime.Job('p', 'spec-f-0001', wt, brief, 'm', log_path=os.path.join(d, 'log2.jsonl'))
            self.assertEqual(rt.run(other).text, 'other')                 # unmatched: the results list

    def test_the_list_form_is_unchanged(self):
        rt = runtime.FakeRuntime([{'ok': False, 'result': 'x'}])
        self.assertEqual((rt.script, rt.jobs), ([{'ok': False, 'result': 'x'}], {}))

    def test_the_sample_first_task_script_parses_and_covers_each_lane(self):
        with open(os.path.join(ROOT, 'sample', 'first_task.json'), encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(sorted(data['jobs']), ['*-b-0001', 'coder-t-*', 'plan-f-*', 'spec-f-*'])


class PrivacySweepTest(unittest.TestCase):
    def test_kinds(self):
        home = '/' + 'Users' + '/' + 'jdoe' + '/code'
        self.assertEqual(check_privacy.scan_line(f'cd {home}/x'), ['path'])
        self.assertEqual(check_privacy.scan_line('cd /home/someone/work'), [])
        self.assertEqual(check_privacy.scan_line('cd <tmp>/home/state/x'), [])
        self.assertEqual(check_privacy.scan_line('mail ' + 'jdoe' + '@' + 'corp.io'), ['email'])
        self.assertEqual(check_privacy.scan_line('ci@localhost a@example.com x@users.noreply.github.com'), [])
        self.assertEqual(check_privacy.scan_line('pipx install git+https://h/x.git@v0.1.0'), [])
        link = 'https://claude.ai/code/' + 'session_' + '01ABCDEFGHIJKLMNOP'
        self.assertEqual(check_privacy.scan_line(link), ['link'])
        self.assertEqual(check_privacy.scan_line('https://claude.ai/code/session_{id}'), [])

    def test_the_tree_is_clean(self):
        self.assertEqual(check_privacy.scan(ROOT, check_privacy.read_allow(
            os.path.join(ROOT, 'tools', 'privacy-allow.txt'))), [])

    def test_an_allow_line_exempts_a_file_and_kind(self):
        rules = [('docs/*.md', 'email')]
        self.assertTrue(check_privacy.allowed(rules, 'docs/a.md', 'email'))
        self.assertFalse(check_privacy.allowed(rules, 'docs/a.md', 'path'))


if __name__ == '__main__':
    unittest.main()
