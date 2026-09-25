"""The prod deploy pass (asf.harvest.deploy): a green trunk ahead of prod is dispatched when the
product opts in, and every other case is one loud ``deploy:`` line naming what prod waits on."""
import json
import types
import unittest

from asf.harvest import deploy

PROD, GREEN, RED, MAIN = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 40


def _product(**deploy_sha):
    return types.SimpleNamespace(
        repo_slug='o/r', repo_dir='/repo', main='main',
        conventions={'deploy_workflow': 'deploy-prod.yml', 'ci_workflow': 'ci.yml'},
        deploy_sha=dict({'workflow': 'deploy-prod.yml'}, **deploy_sha))


class FakeSh:
    def __init__(self, deploys, ci, behind='5', ancestor=True, dispatch_ok=True):
        self.deploys, self.ci, self.behind = deploys, ci, behind
        self.ancestor, self.dispatch_ok, self.calls = ancestor, dispatch_ok, []

    def __call__(self, cmd, cwd=None, timeout=60):
        self.calls.append(cmd)
        if cmd[:3] == ['gh', 'run', 'list']:
            wf = cmd[cmd.index('--workflow') + 1]
            runs = self.deploys if wf == 'deploy-prod.yml' else self.ci
            return None if runs is None else json.dumps(runs)
        if cmd[:3] == ['gh', 'workflow', 'run']:
            return '' if self.dispatch_ok else None
        if 'rev-parse' in cmd:
            return MAIN
        if 'rev-list' in cmd:
            return self.behind
        if 'merge-base' in cmd:
            return '' if self.ancestor else None
        return ''

    def dispatched(self):
        return [c for c in self.calls if c[:3] == ['gh', 'workflow', 'run']]


def _run(sha, status='completed', conclusion='success', rid=1):
    return {'databaseId': rid, 'headSha': sha, 'status': status, 'conclusion': conclusion,
            'updatedAt': '2026-09-24T21:32:10Z'}


def _tick(product, sh):
    lines = []
    sha = deploy.tick(product, out=lines.append, sh=sh)
    return sha, lines


class Dispatch(unittest.TestCase):
    def test_auto_dispatches_the_newest_green_trunk_sha_with_the_input(self):
        sh = FakeSh([_run(PROD)], [_run(RED, conclusion='failure'), _run(GREEN)])
        sha, lines = _tick(_product(auto=True), sh)
        self.assertEqual(sha, GREEN)
        self.assertEqual(sh.dispatched(), [['gh', 'workflow', 'run', 'deploy-prod.yml', '-R', 'o/r',
                                            '--ref', 'main', '-f', f'sha={GREEN}']])
        self.assertIn('dispatching deploy-prod.yml', lines[0])

    def test_input_none_sends_no_field(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        _tick(_product(auto=True, input='none'), sh)
        self.assertNotIn('-f', sh.dispatched()[0])

    def test_auto_off_is_loud_and_dispatches_nothing(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        sha, lines = _tick(_product(), sh)
        self.assertIsNone(sha)
        self.assertEqual(sh.dispatched(), [])
        self.assertIn('deploy_sha.auto is off', lines[0])
        self.assertIn('main is 5 commits ahead of prod', lines[0])

    def test_a_running_deploy_waits(self):
        sh = FakeSh([_run(GREEN, status='in_progress', conclusion=None, rid=9), _run(PROD)],
                    [_run(GREEN)])
        sha, lines = _tick(_product(auto=True), sh)
        self.assertIsNone(sha)
        self.assertIn('run 9', lines[0])
        self.assertEqual(sh.dispatched(), [])

    def test_a_failed_sha_is_not_retried(self):
        sh = FakeSh([_run(GREEN, conclusion='failure', rid=7), _run(PROD)], [_run(GREEN)])
        sha, lines = _tick(_product(auto=True), sh)
        self.assertIsNone(sha)
        self.assertIn('FAILED (run 7)', lines[0])

    def test_no_green_trunk_waits_on_green(self):
        sh = FakeSh([_run(PROD)], [_run(RED, conclusion='failure')])
        sha, lines = _tick(_product(auto=True), sh)
        self.assertIsNone(sha)
        self.assertIn('prod waits on a green main', lines[0])

    def test_a_green_sha_behind_prod_is_no_candidate(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)], ancestor=False)
        sha, lines = _tick(_product(auto=True), sh)
        self.assertIsNone(sha)
        self.assertEqual(sh.dispatched(), [])

    def test_prod_at_main_is_quiet_about_deploying(self):
        sh = FakeSh([_run(PROD)], [_run(PROD)], behind='0')
        sha, lines = _tick(_product(auto=True), sh)
        self.assertIsNone(sha)
        self.assertIn('is main', lines[0])

    def test_a_refused_dispatch_is_loud(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)], dispatch_ok=False)
        sha, lines = _tick(_product(auto=True), sh)
        self.assertIsNone(sha)
        self.assertIn('DISPATCH REFUSED', lines[-1])

    def test_unreadable_runs_say_so(self):
        sh = FakeSh(None, [])
        _, lines = _tick(_product(auto=True), sh)
        self.assertIn('prod state unknown', lines[0])

    def test_a_product_without_a_deploy_workflow_is_untouched(self):
        p = _product(auto=True)
        p.conventions = {'ci_workflow': 'ci.yml'}
        p.deploy_sha = {}
        sh = FakeSh([], [])
        self.assertEqual(_tick(p, sh), (None, []))
        self.assertEqual(sh.calls, [])
        self.assertIsNone(deploy.line(p, sh=sh))


class HarvestStep(unittest.TestCase):
    def test_the_harvest_step_runs_the_deploy_pass_and_survives_its_fault(self):
        from unittest import mock
        from asf.tick import step_harvest
        lines = []
        with mock.patch.object(deploy, 'tick', side_effect=RuntimeError('boom')):
            step_harvest.deploy_pass(_product(auto=True), out=lines.append)
        self.assertEqual(lines, ['deploy: FAILED boom'])


if __name__ == '__main__':
    unittest.main()
