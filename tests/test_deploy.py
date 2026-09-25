"""The prod deploy pass (asf.harvest.deploy): a green trunk ahead of prod is dispatched when the
product opts in, and every other case is one loud ``deploy:`` line naming what prod waits on."""
import json
import types
import unittest

from asf.harvest import deploy

PROD, GREEN, RED, MAIN = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 40


def _product(conventions=None, **deploy_sha):
    return types.SimpleNamespace(
        repo_slug='o/r', repo_dir='/repo', main='main',
        conventions=conventions or {'deploy_workflow': 'deploy-prod.yml', 'ci_workflow': 'ci.yml'},
        deploy_sha=dict({'workflow': 'deploy-prod.yml'}, **deploy_sha))


def _modes(dev=None, prod=None, **extra):
    """A product in the per-environment shape: no top-level workflow, no legacy key."""
    d = {}
    if dev is not None:
        d['dev'] = dict({'mode': dev, 'workflow': 'deploy-dev.yml'}, **extra.pop('dev_extra', {}))
    if prod is not None:
        d['prod'] = dict({'mode': prod, 'workflow': 'deploy-prod.yml'},
                         **extra.pop('prod_extra', {}))
    conv = {'ci_workflow': 'ci.yml', 'deploy_workflow': 'deploy-prod.yml'}
    conv.update(extra.pop('conv', {}))
    return types.SimpleNamespace(repo_slug='o/r', repo_dir='/repo', main='main',
                                 conventions=conv, deploy_sha=d)


class FakeSh:
    def __init__(self, deploys, ci, behind='5', ancestor=True, dispatch_ok=True, dev=None,
                 jobs=None, by_commit=None):
        self.deploys, self.ci, self.behind = deploys, ci, behind
        self.ancestor, self.dispatch_ok, self.calls = ancestor, dispatch_ok, []
        self.dev, self.jobs, self.by_commit = dev or [], jobs or {}, by_commit or {}

    def __call__(self, cmd, cwd=None, timeout=60):
        self.calls.append(cmd)
        if cmd[:3] == ['gh', 'run', 'list']:
            wf = cmd[cmd.index('--workflow') + 1]
            if '--commit' in cmd:
                return json.dumps(self.by_commit.get(cmd[cmd.index('--commit') + 1], []))
            runs = {'deploy-prod.yml': self.deploys, 'deploy-dev.yml': self.dev}.get(wf, self.ci)
            return None if runs is None else json.dumps(runs)
        if cmd[:3] == ['gh', 'run', 'view']:
            return json.dumps({'jobs': self.jobs.get(int(cmd[3]), [])})
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

    def dispatched_workflows(self):
        return [c[3] for c in self.dispatched()]


def _run(sha, status='completed', conclusion='success', rid=1):
    return {'databaseId': rid, 'headSha': sha, 'status': status, 'conclusion': conclusion,
            'updatedAt': '2026-09-24T21:32:10Z'}


def _tick(product, sh):
    lines = []
    sent = deploy.tick(product, out=lines.append, sh=sh)
    return sent.get('prod'), lines


class Dispatch(unittest.TestCase):
    def test_auto_dispatches_the_newest_green_trunk_sha_with_the_input(self):
        sh = FakeSh([_run(PROD)], [_run(RED, conclusion='failure'), _run(GREEN)])
        sha, lines = _tick(_product(auto=True), sh)
        self.assertEqual(sha, GREEN)
        self.assertEqual(sh.dispatched(), [['gh', 'workflow', 'run', 'deploy-prod.yml', '-R', 'o/r',
                                            '--ref', 'main', '-f', f'sha={GREEN}']])
        self.assertIn('dispatching deploy-prod.yml', lines[0])

    def test_a_view_never_says_dispatching_it_says_the_next_tick_does(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        text = deploy.line(_product(auto=True), sh=sh)
        self.assertIn('the next tick dispatches deploy-prod.yml', text)
        self.assertNotIn('— dispatching', text)
        self.assertEqual(sh.dispatched(), [])

    def test_input_none_sends_no_field(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        _tick(_product(auto=True, input='none'), sh)
        self.assertNotIn('-f', sh.dispatched()[0])

    def test_manual_is_the_default_and_names_the_waiting_sha(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        sha, lines = _tick(_product(), sh)
        self.assertIsNone(sha)
        self.assertEqual(sh.dispatched(), [])
        self.assertIn(f'MANUAL: green `{GREEN[:9]}` waits on a hand dispatch of deploy-prod.yml',
                      lines[0])
        self.assertIn('deploy_sha.prod.mode: manual', lines[0])
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

    def test_unreadable_ci_says_so_for_every_environment(self):
        sh = FakeSh([_run(PROD)], None)
        sent, lines = deploy.tick(_modes(dev='auto', prod='auto'), out=[].append, sh=sh), \
            deploy.lines(_modes(dev='auto', prod='auto'), sh=sh)
        self.assertEqual(sent, {})
        self.assertEqual(len(lines), 2)
        self.assertTrue(all('state unknown' in ln for ln in lines))

    def test_a_product_without_a_deploy_workflow_is_untouched(self):
        p = _product(auto=True)
        p.conventions = {'ci_workflow': 'ci.yml'}
        p.deploy_sha = {}
        sh = FakeSh([], [])
        self.assertEqual(_tick(p, sh), (None, []))
        self.assertEqual(sh.calls, [])
        self.assertIsNone(deploy.line(p, sh=sh))
        self.assertEqual(deploy.lines(p, sh=sh), [])


class LegacyAlias(unittest.TestCase):
    def test_auto_true_reads_as_prod_mode_auto(self):
        self.assertEqual(deploy.mode(_product(auto=True)), 'auto')
        self.assertEqual(deploy.mode(_product()), 'manual')
        self.assertIsNone(deploy.mode(_product(auto=True), 'dev'))

    def test_prod_mode_wins_over_the_legacy_key(self):
        p = _product(auto=True, prod={'mode': 'manual'})
        self.assertEqual(deploy.mode(p), 'manual')
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        self.assertEqual(_tick(p, sh)[0], None)
        self.assertEqual(sh.dispatched(), [])

    def test_doctor_names_the_deprecated_key(self):
        found = deploy.findings(_product(auto=True))
        self.assertIn((False, 'deploy_sha.auto is deprecated — write deploy_sha.prod.mode: auto'),
                      found)
        self.assertTrue(any(ok and 'prod auto' in d for ok, d in found))
        self.assertFalse(any('deprecated' in d for _, d in deploy.findings(_modes(prod='auto'))))

    def test_the_new_shape_needs_no_top_level_workflow(self):
        p = _modes(prod='auto', conv={'deploy_workflow': None})
        self.assertEqual(deploy.workflow(p), 'deploy-prod.yml')
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        self.assertEqual(_tick(p, sh)[0], GREEN)


class Modes(unittest.TestCase):
    """Every dev x prod combination: what each environment dispatches and says."""

    def _both(self, dev, prod, **kw):
        sh = FakeSh([_run(PROD)], [_run(GREEN, rid=3)], dev=[_run(PROD)], **kw)
        lines = []
        sent = deploy.tick(_modes(dev=dev, prod=prod), out=lines.append, sh=sh)
        return sent, lines, sh

    def test_auto_auto_dispatches_both_dev_first(self):
        sent, lines, sh = self._both('auto', 'auto')
        self.assertEqual(sent, {'dev': GREEN, 'prod': GREEN})
        self.assertEqual(sh.dispatched_workflows(), ['deploy-dev.yml', 'deploy-prod.yml'])
        self.assertTrue(lines[0].startswith('deploy dev: '))
        self.assertTrue(lines[1].startswith('deploy: '))

    def test_auto_dev_manual_prod(self):
        sent, lines, sh = self._both('auto', 'manual')
        self.assertEqual(sent, {'dev': GREEN})
        self.assertIn('MANUAL: green', lines[1])
        self.assertIn(GREEN[:9], lines[1])

    def test_manual_dev_auto_prod(self):
        sent, lines, sh = self._both('manual', 'auto')
        self.assertEqual(sent, {'prod': GREEN})
        self.assertIn(f'MANUAL: green `{GREEN[:9]}` waits on a hand dispatch of deploy-dev.yml',
                      lines[0])
        self.assertIn('deploy_sha.dev.mode: manual', lines[0])

    def test_manual_manual_dispatches_nothing(self):
        sent, lines, sh = self._both('manual', 'manual')
        self.assertEqual(sent, {})
        self.assertEqual(sh.dispatched(), [])
        self.assertEqual(sum('MANUAL' in ln for ln in lines), 2)

    def test_ci_dev_is_observed_and_auto_prod_dispatches(self):
        sent, lines, sh = self._both('ci', 'auto')
        self.assertEqual(sent, {'prod': GREEN})
        self.assertEqual(sh.dispatched_workflows(), ['deploy-prod.yml'])
        self.assertIn('ci.yml deploys dev on its own (ASF observes)', lines[0])
        self.assertIn(GREEN[:9], lines[0])

    def test_ci_dev_manual_prod(self):
        sent, lines, sh = self._both('ci', 'manual')
        self.assertEqual(sent, {})
        self.assertIn('ASF observes', lines[0])
        self.assertIn('MANUAL', lines[1])

    def test_ci_dev_reads_the_dev_job_of_a_red_run(self):
        p = _modes(dev='ci', prod='manual', conv={'ci_dev_job': 'deploy-dev'})
        sh = FakeSh([_run(PROD)], [_run(RED, conclusion='failure', rid=4), _run(GREEN, rid=3)],
                    jobs={4: [{'name': 'deploy-dev', 'conclusion': 'success'}]})
        f = deploy.facts(p, sh=sh, env='dev')
        self.assertEqual(f['deployed'], RED)  # CI deployed dev even though a later job failed

    def test_a_running_dev_deploy_holds_dev_only(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)],
                    dev=[_run(GREEN, status='queued', conclusion=None, rid=8), _run(PROD)])
        lines = []
        sent = deploy.tick(_modes(dev='auto', prod='auto'), out=lines.append, sh=sh)
        self.assertEqual(sent, {'prod': GREEN})
        self.assertIn('run 8', lines[0])

    def test_a_failed_dev_sha_is_not_retried(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)], dev=[_run(GREEN, conclusion='failure', rid=6),
                                                      _run(PROD)])
        lines = []
        sent = deploy.tick(_modes(dev='auto', prod='manual'), out=lines.append, sh=sh)
        self.assertEqual(sent, {})
        self.assertIn('FAILED (run 6)', lines[0])

    def test_red_trunk_deploys_no_environment(self):
        sh = FakeSh([_run(PROD)], [_run(RED, conclusion='failure')], dev=[_run(PROD)])
        lines = []
        sent = deploy.tick(_modes(dev='auto', prod='auto'), out=lines.append, sh=sh)
        self.assertEqual(sent, {})
        self.assertIn('dev waits on a green main', lines[0])
        self.assertIn('prod waits on a green main', lines[1])

    def test_unmanaged_dev_prints_nothing(self):
        p = _modes(prod='auto')
        p.deploy_sha['dev'] = {'source': 'github-deployments', 'workflow': 'ci.yml'}
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        self.assertEqual(len(deploy.lines(p, sh=sh)), 1)

    def test_lines_are_dev_then_prod_and_line_is_prod(self):
        p = _modes(dev='ci', prod='manual')
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        got = deploy.lines(p, sh=sh)
        self.assertTrue(got[0].startswith('deploy dev: '))
        self.assertEqual(deploy.line(p, sh=sh), got[1])


class PromoteDevToProd(unittest.TestCase):
    def test_prod_follows_the_sha_dev_runs(self):
        newer = 'e' * 40
        p = _modes(dev='auto', prod='auto', prod_extra={'from': 'dev'})
        sh = FakeSh([_run(PROD)], [_run(newer, rid=5), _run(GREEN, rid=3)],
                    dev=[_run(newer, status='in_progress', conclusion=None, rid=9),
                         _run(GREEN, rid=2)])
        lines = []
        sent = deploy.tick(p, out=lines.append, sh=sh)
        self.assertEqual(sent, {'prod': GREEN})  # dev's sha, not the newest green trunk sha
        self.assertIn('run 9', lines[0])

    def test_a_dev_sha_without_green_ci_waits(self):
        p = _modes(dev='manual', prod='auto', prod_extra={'from': 'dev'})
        sh = FakeSh([_run(PROD)], [_run(GREEN)], dev=[_run(RED)])
        lines = []
        self.assertEqual(deploy.tick(p, out=lines.append, sh=sh), {})
        self.assertIn(f"dev's `{RED[:9]}` has no green ci.yml run", lines[1])

    def test_an_older_green_dev_sha_is_found_by_commit(self):
        p = _modes(dev='manual', prod='auto', prod_extra={'from': 'dev'})
        sh = FakeSh([_run(PROD)], [_run(GREEN)], dev=[_run(RED)],
                    by_commit={RED: [_run(RED)]})
        self.assertEqual(deploy.tick(p, out=[].append, sh=sh), {'prod': RED})

    def test_from_dev_without_a_managed_dev_waits_loudly(self):
        p = _modes(prod='auto', prod_extra={'from': 'dev'})
        sh = FakeSh([_run(PROD)], [_run(GREEN)])
        lines = []
        self.assertEqual(deploy.tick(p, out=lines.append, sh=sh), {})
        self.assertIn('dev is not managed', lines[0])
        self.assertTrue(any(not ok and 'prod.from: dev' in d for ok, d in deploy.findings(p)))


class Validation(unittest.TestCase):
    def _problems(self, block):
        from asf import env
        text = 'product: x\nrepo_slug: o/r\nmain: main\ndeploy_sha:\n' + block
        return [(k, why) for _, k, why in env.validate_product_text(text)]

    def test_known_modes_load(self):
        self.assertEqual(self._problems('  dev:\n    mode: ci\n  prod:\n    mode: auto\n'
                                        '    from: dev\n  auto: true\n'), [])

    def test_a_bad_mode_refuses(self):
        got = self._problems('  dev:\n    mode: sometimes\n  prod:\n    mode: ci\n'
                             '    from: staging\n')
        keys = [k for k, _ in got]
        self.assertEqual(sorted(keys), ['deploy_sha.dev.mode', 'deploy_sha.prod.from',
                                        'deploy_sha.prod.mode'])

    def test_the_prod_workflow_folds_into_the_deploy_workflow(self):
        from asf import env
        p = env.Product('x', {'repo_slug': 'o/r', 'main': 'main',
                              'deploy_sha': {'prod': {'mode': 'auto', 'workflow': 'd.yml'}}})
        self.assertEqual(p.conventions.get('deploy_workflow'), 'd.yml')


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
