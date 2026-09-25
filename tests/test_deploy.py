"""The prod deploy pass (asf.harvest.deploy): a green trunk ahead of prod is dispatched when the
product opts in, and every other case is one loud ``deploy:`` line naming what prod waits on."""
import datetime
import json
import os
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
                 jobs=None, by_commit=None, site=None, relevant='2', vercel=None):
        self.deploys, self.ci, self.behind = deploys, ci, behind
        self.ancestor, self.dispatch_ok, self.calls = ancestor, dispatch_ok, []
        self.dev, self.jobs, self.by_commit = dev or [], jobs or {}, by_commit or {}
        self.site, self.relevant, self.vercel = site or [], relevant, vercel

    def __call__(self, cmd, cwd=None, timeout=60):
        self.calls.append(cmd)
        if cmd[:3] == ['gh', 'run', 'list']:
            wf = cmd[cmd.index('--workflow') + 1]
            if '--commit' in cmd:
                return json.dumps(self.by_commit.get(cmd[cmd.index('--commit') + 1], []))
            runs = {'deploy-prod.yml': self.deploys, 'deploy-dev.yml': self.dev,
                    'site-deploy.yml': self.site}.get(wf, self.ci)
            return None if runs is None else json.dumps(runs)
        if cmd[:3] == ['gh', 'run', 'view']:
            return json.dumps({'jobs': self.jobs.get(int(cmd[3]), [])})
        if cmd[:3] == ['gh', 'workflow', 'run']:
            return '' if self.dispatch_ok else None
        if 'rev-parse' in cmd:
            return MAIN
        if cmd[:2] == ['vercel', 'ls']:
            return self.vercel
        if 'rev-list' in cmd:
            return self.relevant if '--' in cmd else self.behind
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


SITE = 'f' * 40


def _site(mode='manual', **cfg):
    """prod manual plus a named ``site`` target scoped to apps/site/**."""
    p = _modes(prod='manual')
    p.deploy_sha['targets'] = {'site': dict({'mode': mode, 'workflow': 'site-deploy.yml',
                                             'paths': ['apps/site/**']}, **cfg)}
    return p


def _site_tick(product, sh):
    lines = []
    sent = deploy.tick(product, out=lines.append, sh=sh)
    return sent, [ln for ln in lines if ln.startswith('deploy site: ')]


class NamedTargets(unittest.TestCase):
    """deploy_sha.targets: any number of targets beside dev and prod, under the same rules."""

    def test_a_manual_target_says_how_many_relevant_commits_it_waits_on(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)], site=[_run(SITE)], behind='237', relevant='15')
        sent, lines = _site_tick(_site(), sh)
        self.assertEqual(sent, {})
        self.assertEqual(sh.dispatched(), [])
        self.assertIn(f'site `{SITE[:9]}` is 15 relevant commits behind main (237 in all', lines[0])
        self.assertIn('mode manual', lines[0])
        self.assertIn('MANUAL: site is 15 relevant commits behind — waits on a hand dispatch of'
                      ' site-deploy.yml', lines[0])
        rel = [c for c in sh.calls if 'rev-list' in c and '--' in c]
        self.assertEqual(rel[0][-1], ':(glob)apps/site/**')

    def test_an_auto_target_dispatches_its_own_workflow(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)], site=[_run(SITE)])
        sent, lines = _site_tick(_site('auto', input='ref'), sh)
        self.assertEqual(sent, {'site': GREEN})
        self.assertEqual(sh.dispatched(), [['gh', 'workflow', 'run', 'site-deploy.yml', '-R', 'o/r',
                                            '--ref', 'main', '-f', f'ref={GREEN}']])

    def test_no_relevant_commit_means_nothing_to_deploy(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)], site=[_run(SITE)], behind='9', relevant='0')
        sent, lines = _site_tick(_site('auto'), sh)
        self.assertEqual(sent, {})
        self.assertIn('has every main commit touching apps/site/**', lines[0])

    def test_the_same_holds_apply_running_failed_red(self):
        running = FakeSh([_run(PROD)], [_run(GREEN)],
                         site=[_run(GREEN, status='queued', conclusion=None, rid=4), _run(SITE)])
        self.assertEqual(_site_tick(_site('auto'), running)[0], {})
        failed = FakeSh([_run(PROD)], [_run(GREEN)],
                        site=[_run(GREEN, conclusion='failure', rid=5), _run(SITE)])
        sent, lines = _site_tick(_site('auto'), failed)
        self.assertEqual(sent, {})
        self.assertIn('FAILED (run 5)', lines[0])
        red = FakeSh([_run(PROD)], [_run(RED, conclusion='failure')], site=[_run(SITE)])
        sent, lines = _site_tick(_site('auto'), red)
        self.assertEqual(sent, {})
        self.assertIn('site waits on a green main', lines[0])

    def test_a_vercel_target_reads_its_sha_and_waits_on_a_hand_deploy(self):
        p = _site(workflow=None, source='vercel', project='site-prod', scope='team')
        sh = FakeSh([_run(PROD)], [_run(GREEN)], relevant='3', vercel=json.dumps(
            {'deployments': [{'state': 'ERROR', 'meta': {'githubCommitSha': RED}},
                             {'state': 'READY', 'meta': {'githubCommitSha': SITE},
                              'createdAt': 1789949910391}]}))
        sent, lines = _site_tick(p, sh)
        self.assertEqual(sent, {})
        self.assertIn(f'site `{SITE[:9]}` is 3 relevant commits behind', lines[0])
        self.assertIn('waits on a hand deploy — no deploy_sha.targets.site.workflow', lines[0])
        self.assertIn(['vercel', 'ls', 'site-prod', '--prod', '--scope', 'team', '--json'],
                      sh.calls)

    def _cli_deploy(self, created=1789949910391):
        """A vercel target whose READY deployment carries no githubCommitSha (a CLI deploy)."""
        p = _site(workflow=None, source='vercel', project='site-prod')
        p.name = 'deploy-record-test'
        path = deploy._records_path(p)
        if os.path.exists(path):
            os.remove(path)
        sh = FakeSh([_run(PROD)], [_run(GREEN)], relevant='3', vercel=json.dumps(
            {'deployments': [{'state': 'READY', 'meta': {}, 'createdAt': created}]}))
        return p, sh

    def test_a_cli_deploy_with_no_sha_says_sha_unknown_never_none(self):
        p, sh = self._cli_deploy()
        sent, lines = _site_tick(p, sh)
        self.assertEqual(sent, {})
        self.assertNotIn('None', lines[0])
        self.assertNotIn('`?`', lines[0])
        self.assertIn('site sha unknown — how far behind main is unknown', lines[0])
        self.assertIn('MANUAL: site runs an unknown sha — waits on a hand deploy', lines[0])

    def test_a_hand_record_names_the_sha_a_cli_deploy_lacks(self):
        p, sh = self._cli_deploy(created=1789949910391)
        after = datetime.datetime.fromtimestamp(1789949910391 / 1000 + 60, tz=datetime.timezone.utc)
        deploy.record(p, 'site', SITE, by='hand', now=after)
        _, lines = _site_tick(p, sh)
        self.assertIn(f'site `{SITE[:9]}` is 3 relevant commits behind', lines[0])
        # a record older than the deployment names some earlier deploy: not trusted
        before = datetime.datetime.fromtimestamp(1789949910391 / 1000 - 3600,
                                                 tz=datetime.timezone.utc)
        deploy.record(p, 'site', SITE, by='hand', now=before)
        self.assertIn('site sha unknown', _site_tick(p, sh)[1][0])
        # an asf dispatch written before the deployment it started is that deployment
        deploy.record(p, 'site', SITE, by='asf', now=before)
        self.assertIn(f'site `{SITE[:9]}`', _site_tick(p, sh)[1][0])

    def test_the_tick_records_what_it_dispatches(self):
        p = _site('auto')
        p.name = 'deploy-record-test'
        sh = FakeSh([_run(PROD)], [_run(GREEN)], site=[_run(SITE)])
        sent, _ = _site_tick(p, sh)
        self.assertEqual(sent, {'site': GREEN})
        self.assertEqual(deploy._load_records(p)['site']['sha'], GREEN)
        self.assertEqual(deploy._load_records(p)['site']['by'], 'asf')

    def test_asf_deploy_record_cli(self):
        from unittest import mock
        from asf import cli
        p = _site(workflow=None, source='vercel')
        p.name = 'deploy-record-cli'
        with mock.patch('asf.env.load_product', return_value=p), \
                mock.patch.object(deploy, '_sh', return_value=None):
            args = cli.build_parser().parse_args(['deploy', 'record', 'site', SITE])
            self.assertEqual(deploy.cmd_record(args, sh=lambda c: None, out=lambda s: None), 0)
            self.assertEqual(deploy._load_records(p)['site']['sha'], SITE)
            bad = cli.build_parser().parse_args(['deploy', 'record', 'nope', SITE])
            self.assertEqual(deploy.cmd_record(bad, sh=lambda c: None, out=lambda s: None), 2)

    def test_an_unreadable_vercel_sha_is_loud(self):
        p = _site(workflow=None, source='vercel', project='site-prod')
        sent, lines = _site_tick(p, FakeSh([_run(PROD)], [_run(GREEN)], vercel=None))
        self.assertIn('site state unknown', lines[0])

    def test_a_ci_target_is_observed(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN)], site=[_run(SITE)])
        sent, lines = _site_tick(_site('ci'), sh)
        self.assertEqual(sent, {})
        self.assertIn('site-deploy.yml deploys site on its own (ASF observes)', lines[0])

    def test_targets_follow_dev_and_prod_and_the_views_list_them(self):
        p = _site()
        p.deploy_sha['dev'] = {'mode': 'ci'}
        sh = FakeSh([_run(PROD)], [_run(GREEN)], site=[_run(SITE)])
        got = deploy.lines(p, sh=sh)
        self.assertEqual([g.split(':')[0] for g in got], ['deploy dev', 'deploy', 'deploy site'])
        self.assertEqual(deploy.names(p), ['dev', 'prod', 'site'])

    def test_doctor_names_a_target_it_cannot_manage(self):
        p = _site('auto', workflow=None)
        found = deploy.findings(p)
        self.assertTrue(any(not ok and 'deploy_sha.targets.site' in d for ok, d in found))
        self.assertFalse(deploy.env_applies(p, 'site'))
        ok = deploy.findings(_site())
        self.assertTrue(any(k and 'site manual (site-deploy.yml, apps/site/**)' in d
                            for k, d in ok))

    def test_a_target_workflow_is_touch_production(self):
        from asf import approvals
        p = _site()
        self.assertTrue(approvals._runs_deploy_workflow(
            'gh workflow run site-deploy.yml -R o/r -f sha=x', p))
        self.assertFalse(approvals._runs_deploy_workflow('gh workflow run ci.yml', p))


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

    def test_named_targets_load_and_a_bad_one_refuses(self):
        self.assertEqual(self._problems(
            '  targets:\n    site:\n      mode: manual\n      from: dev\n'
            '      paths: [apps/site/**]\n'), [])
        got = self._problems('  targets:\n    site:\n      mode: nightly\n      paths: x\n'
                             '    prod:\n      mode: auto\n')
        self.assertEqual(sorted(k for k, _ in got),
                         ['deploy_sha.targets.prod', 'deploy_sha.targets.site.mode',
                          'deploy_sha.targets.site.paths'])

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


class FileSh(FakeSh):
    """FakeSh that also answers ``git show <sha>:<path>`` from ``files[(sha, path)]``."""
    def __init__(self, *a, files=None, **kw):
        super().__init__(*a, **kw)
        self.files = files or {}

    def __call__(self, cmd, cwd=None, timeout=60):
        if 'show' in cmd and cmd[:1] == ['git']:
            self.calls.append(cmd)
            sha, _, path = cmd[-1].partition(':')
            return self.files.get((sha, path))
        return super().__call__(cmd, cwd, timeout)


def _job(name, conclusion='success'):
    return {'name': name, 'conclusion': conclusion, 'status': 'completed'}


class RequiredJobs(unittest.TestCase):
    """The candidate under ``required_jobs``: every listed job green; the rest never blocks."""
    def _p(self, mode='auto', **prod_extra):
        return _modes(prod=mode, prod_extra=prod_extra)

    def test_an_optional_job_cancelled_is_still_a_candidate(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN, conclusion='cancelled', rid=5)],
                    jobs={5: [_job('gate — the gate, without the suites'), _job('gate-tests'),
                              _job('soak', 'cancelled')]})
        lines = []
        sent = deploy.tick(self._p(required_jobs=['gate', 'gate-tests']), out=lines.append, sh=sh)
        self.assertEqual(sent, {'prod': GREEN})
        self.assertIn('green on required jobs [gate, gate-tests] (soak cancelled, not required)',
                      lines[0])

    def test_a_required_job_failed_is_not_a_candidate(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN, conclusion='failure', rid=5)],
                    jobs={5: [_job('gate'), _job('gate-tests', 'failure')]})
        p = self._p(required_jobs=['gate', 'gate-tests'])
        self.assertIsNone(deploy.facts(p, sh=sh)['candidate'])
        self.assertIn('green on required jobs [gate, gate-tests]', deploy.line(p, sh=sh))
        self.assertEqual(deploy.tick(p, out=[].append, sh=sh), {})

    def test_no_list_keeps_the_run_level_rule(self):
        sh = FakeSh([_run(PROD)], [_run(GREEN, conclusion='cancelled', rid=5)],
                    jobs={5: [_job('gate'), _job('soak', 'cancelled')]})
        p = self._p()
        self.assertIsNone(deploy.facts(p, sh=sh)['candidate'])
        self.assertFalse(any(c[:3] == ['gh', 'run', 'view'] for c in sh.calls))
        green = FakeSh([_run(PROD)], [_run(GREEN)])
        self.assertIn('run-level conclusion success', deploy.line(p, sh=green))

    def test_landing_checks_is_the_fallback_list(self):
        p = _modes(prod='auto', conv={'landing_checks': ['gate']})
        sh = FakeSh([_run(PROD)], [_run(GREEN, conclusion='timed_out', rid=5)],
                    jobs={5: [_job('gate'), _job('soak', 'timed_out')]})
        self.assertEqual(deploy.facts(p, sh=sh)['candidate'], GREEN)

    def test_a_job_is_named_up_to_the_first_space_never_by_prefix(self):
        p = self._p(required_jobs=['p1-e2e'])
        sh = FakeSh([_run(PROD)], [_run(GREEN, conclusion='failure', rid=5)],
                    jobs={5: [_job('p1-e2e — the suite'), _job('p1-e2e-b', 'failure')]})
        self.assertEqual(deploy.facts(p, sh=sh)['candidate'], GREEN)
        sh = FakeSh([_run(PROD)], [_run(GREEN, conclusion='failure', rid=5)],
                    jobs={5: [_job('p1-e2e', 'failure'), _job('p1-e2e-b')]})
        self.assertIsNone(deploy.facts(p, sh=sh)['candidate'])

    def test_a_required_job_missing_from_the_run_is_not_green(self):
        p = self._p(required_jobs=['gate'])
        sh = FakeSh([_run(PROD)], [_run(GREEN, conclusion='failure', rid=5)],
                    jobs={5: [_job('gate-tests')]})
        self.assertIsNone(deploy.facts(p, sh=sh)['candidate'])

    def test_the_customer_content_check_still_refuses_first(self):
        p = self._p(required_jobs=['gate'])
        sh = FakeSh([_run(PROD)], [_run(GREEN, conclusion='cancelled', rid=5)],
                    jobs={5: [_job('gate'), _job('soak', 'cancelled')]})
        from unittest import mock
        with mock.patch.object(deploy, 'marker_refusal', return_value='deploy: DISPATCH REFUSED'):
            self.assertEqual(deploy.tick(p, out=[].append, sh=sh), {})
        self.assertEqual(sh.dispatched(), [])


class RequiredJobsFrom(unittest.TestCase):
    SPEC = {'file': 'scripts/merge.sh', 'var': 'REQUIRED_CHECKS'}

    def test_parse_assignment_shapes(self):
        pa = deploy.parse_assignment
        self.assertEqual(pa('X=1\nREQ="${REQ:-gate gate-tests}"\n', 'REQ'), ['gate', 'gate-tests'])
        self.assertEqual(pa('export REQ="a b"', 'REQ'), ['a', 'b'])
        self.assertEqual(pa("REQ=(a 'b')", 'REQ'), ['a', 'b'])
        self.assertIsNone(pa('OTHER=1', 'REQ'))

    def test_the_file_at_the_run_sha_names_the_jobs(self):
        p = _modes(prod='auto', prod_extra={'required_jobs_from': self.SPEC})
        files = {(GREEN, 'scripts/merge.sh'): 'REQUIRED_CHECKS="${REQUIRED_CHECKS:-gate}"\n'}
        sh = FileSh([_run(PROD)], [_run(GREEN, conclusion='cancelled', rid=5)],
                    jobs={5: [_job('gate'), _job('soak', 'cancelled')]}, files=files)
        self.assertEqual(deploy.facts(p, sh=sh)['candidate'], GREEN)
        files[(GREEN, 'scripts/merge.sh')] = 'REQUIRED_CHECKS="gate soak"\n'
        self.assertIsNone(deploy.facts(p, sh=sh)['candidate'])

    def test_an_unreadable_file_with_no_list_is_no_candidate(self):
        p = _modes(prod='auto', prod_extra={'required_jobs_from': self.SPEC})
        sh = FileSh([_run(PROD)], [_run(GREEN)])
        self.assertIsNone(deploy.facts(p, sh=sh)['candidate'])

    def test_doctor_warns_when_the_list_drifts_from_the_file(self):
        p = _modes(prod='auto', prod_extra={'required_jobs_from': self.SPEC,
                                            'required_jobs': ['gate']})
        sh = FileSh([], [], files={('origin/main', 'scripts/merge.sh'):
                                   'REQUIRED_CHECKS="gate gate-tests"'})
        bad = [d for ok, d in deploy.findings(p, sh=sh) if not ok]
        self.assertTrue(any('drifts from REQUIRED_CHECKS' in d and 'gate-tests' in d
                            for d in bad), bad)
        p.deploy_sha['prod']['required_jobs'] = ['gate-tests', 'gate']
        self.assertFalse([d for ok, d in deploy.findings(p, sh=sh) if not ok])


if __name__ == '__main__':
    unittest.main()
