"""asf.views — sessions (the live registry), status (every row filled or naming its key), prod
(no deploy), against a temp ASF_HOME and a tiny record. No network: every row that would call
``gh`` is either unconfigured here or stubbed."""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import budget, env
from asf.views import capacity as capacity_view
from asf.views import index_reader as ix
from asf.views import board, prod, roadmap, sessions, status
from asf.workers import observe
from asf.workers import pool as pool_mod

INDEX = {'generated': '', 'items': {
    'E-0001': {'id': 'E-0001', 'type': 'epic', 'title': 'goal', 'folder': 'epics', 'state': 'New',
               'decided': True, 'rank': 1},
    'F-0001': {'id': 'F-0001', 'type': 'feature', 'title': 'a feature', 'folder': 'features',
               'parent': 'E-0001', 'state': 'New', 'decided': True, 'rank': 1},
}}


class ViewsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='views_test_')
        self._home = env.ASF_HOME
        env.ASF_HOME = os.path.join(self.tmp, 'home')
        os.makedirs(env.ASF_HOME)
        self.addCleanup(self._restore)
        self.root = os.path.join(self.tmp, 'record')
        os.makedirs(os.path.join(self.root, 'metrics', 'sessions'))
        with open(os.path.join(self.root, 'index.json'), 'w') as f:
            json.dump(INDEX, f)
        self.product = env.Product('p', {'repo_dir': self.tmp, 'main': 'trunk',
                                         'ci': {'provider': 'none'}, 'deploy_sha': 'none'})

        # the test runner's own pid, as a legacy (no ASF_SESSION) session — the real process
        # table never carries it, since this process is not the runtime binary a real source
        # would match (F-0076 D4)
        patcher = mock.patch('asf.workers.observe.source_from_config',
                             return_value=observe.FakeSource([{'pid': os.getpid(), 'ppid': 1,
                                                                'env': {}}]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore(self):
        env.ASF_HOME = self._home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def launch(self, job, pid, **extra):
        pool_mod.append_session(self.product, dict(job=job, item='F-0001', kind='spec', pid=pid,
                                                   account='w1', branch=f'spec/{job}', **extra))


class SessionsViewTests(ViewsTestCase):
    def test_working_dead_and_ended_groups(self):
        self.launch('alive', 101)
        self.launch('gone', 202)
        self.launch('done', 303)
        pool_mod.update_session(self.product, 'done', ended='2026-09-21T10:00:00Z')
        with open(os.path.join(self.root, 'metrics', 'sessions',
                               sessions._today_and_yesterday()[1] + '.jsonl'), 'w') as f:
            f.write(json.dumps({'id': 'done', 'item': 'F-0001', 'result': 'finished'}) + '\n')
        text = sessions.render(self.root, self.product, alive=lambda pid: pid == 101)
        self.assertIn('1 working · 1 dead · 1 ended', text)
        working = text[text.index('**Working**'):text.index('**Dead**')]
        dead = text[text.index('**Dead**'):text.index('**Ended**')]
        self.assertIn('| alive |', working)
        self.assertIn('| gone |', dead)
        self.assertNotIn('done', working + dead)

    def test_an_exited_pid_with_a_success_result_is_finished_not_dead(self):
        logs = os.path.join(self.tmp, 'logs')
        os.makedirs(logs)
        for name, rec in (('ok', {'type': 'result', 'subtype': 'success', 'is_error': False,
                                  'result': 'done'}), ('none', None)):
            with open(os.path.join(logs, name), 'w') as f:
                f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
                if rec:
                    f.write(json.dumps(rec) + '\n')
        self.launch('alive', 101)
        self.launch('finished', 202, log=os.path.join(logs, 'ok'))
        self.launch('gone', 303, log=os.path.join(logs, 'none'))
        alive = lambda pid: pid == 101  # noqa: E731
        text = sessions.render(self.root, self.product, alive=alive)
        self.assertIn('1 working · 1 finished (awaiting harvest) · 1 dead', text)
        finished = text[text.index('**Finished**'):text.index('**Dead**')]
        dead = text[text.index('**Dead**'):]
        self.assertIn('| finished |', finished)
        self.assertIn('| gone |', dead)
        self.assertNotIn('| finished |', dead)
        with mock.patch('asf.workers.health.alive_for', return_value=alive):
            self.assertEqual(status.agents_cell(self.product),
                             '1 working, 1 finished (awaiting harvest), 1 dead')

    def test_no_registry_is_none_not_an_error(self):
        text = sessions.render(self.root, self.product)
        self.assertIn('Working: none', text)
        self.assertIn('Dead: none', text)

    def test_pid_alive(self):
        self.assertTrue(sessions.pid_alive(os.getpid()))
        self.assertFalse(sessions.pid_alive(None))
        self.assertFalse(sessions.pid_alive('x'))


class StatusViewTests(ViewsTestCase):
    def rows(self, cfg):
        text = status.render(self.root, self.product, cfg=cfg)
        return {ln.split(' | ')[0].lstrip('| '): ln.split(' | ', 1)[1].rstrip(' |')
                for ln in text.splitlines() if ln.startswith('| ') and 'Metric' not in ln}

    def test_unconfigured_rows_name_their_key_never_a_bare_dash(self):
        rows = self.rows({'scheduler': {'kind': 'none'}})
        self.assertEqual(rows['Runners'], '— (not configured: ci.provider (none))')
        self.assertEqual(rows['Prod'], '— (not configured: deploy_sha.workflow)')
        self.assertEqual(rows['Quota 5h/7d'], '— (not configured: worker_pool.quota_command)')
        self.assertEqual(rows['Cron'], '— (not configured: scheduler.kind (none has no status adapter))')
        self.assertEqual(rows['Groom'], '— (not configured: approvals.groom)')
        for name, cell in rows.items():
            self.assertFalse(cell.strip() == '—', name)

    #: A value for every key a ``not configured: <key>`` hint names — product-file keys as the
    #: product file writes them, operator-config keys as ``config.yaml`` does.
    HINT_VALUES = {
        'approvals.groom': ('product', 'approvals:\n  groom: auto\n'),
        'ci.provider': ('product', 'ci:\n  provider: github\n'),
        'ci.runner_org': ('product', 'ci:\n  runner_org: example\n'),
        'deploy_sha.workflow': ('product', 'deploy_sha:\n  workflow: deploy.yml\n'),
        'repo_slug': ('product', 'repo_slug: example/sample\n'),
        'backlog_dir': ('product', 'backlog_dir: /tmp/sample-record\n'),
        'capacity': ('product', 'capacity:\n  sessions: 2\n'),
        'worker_pool.quota_command': ('config', 'worker_pool:\n  quota_command: quota --json\n'),
        'worker_pool.accounts': ('config', 'worker_pool:\n  accounts:\n    - name: a\n'),
        'scheduler.kind': ('config', 'scheduler:\n  kind: launchd\n'),
    }

    def test_every_not_configured_hint_names_a_loadable_key(self):
        """A hint the operator follows must load: every key a status or doctor ``not configured:
        <key>`` names, set in the file it belongs to, passes that file's checks (``ci.deploy_workflow``
        once told the operator to write a key the product file refuses)."""
        import re
        from asf import doctor
        keys = set()
        for mod in (status, doctor):
            with open(mod.__file__, encoding='utf-8') as f:
                src = f.read()
            keys |= {m.split(' ')[0] for m in re.findall(r"not_configured\(f?'([^']+)'\)", src)}
            keys |= {m for m in re.findall(r'not configured: ([a-z_.]+)', src)}
        keys.add(status.DEPLOY_WORKFLOW_KEY)
        self.assertEqual(keys - set(self.HINT_VALUES), set(), 'a hint with no value to try')
        for key in sorted(keys):
            where, text = self.HINT_VALUES[key]
            if where == 'product':
                problems = env.validate_product_text(f'product: sample\n{text}')
                self.assertEqual(problems, [], key)
            else:
                self.assertEqual(env.validate_worker_pool(env.loads(text)), [], key)
                self.assertIsInstance(env.loads(text), dict, key)

    def test_the_prod_row_reads_the_documented_key_and_its_aliases(self):
        for data in ({'deploy_sha': {'workflow': 'deploy.yml'}},
                     {'conventions': {'deploy_workflow': 'deploy.yml'}},
                     {'ci': {'deploy_workflow': 'deploy.yml'}}):
            product = env.Product('p', dict(data, repo_slug='x/y', repo_dir=self.tmp))
            with mock.patch.object(status, '_sh', return_value='') as sh:
                cell = status.prod_cell(product)
            self.assertEqual(cell, '? (no successful deploy.yml run readable)', data)
            self.assertIn('deploy.yml', sh.call_args[0][0])

    def test_groom_row_reads_the_newest_digest(self):
        product = env.Product('p', {'repo_dir': self.tmp, 'main': 'trunk', 'ci': {'provider': 'none'},
                                    'deploy_sha': 'none', 'approvals': {'groom': 'auto'}})
        os.makedirs(os.path.join(self.root, 'groom'))
        with open(os.path.join(self.root, 'groom', '2026-09-21-digest.md'), 'w') as f:
            f.write("# Groom digest 2026-09-21\n\n"
                   "2 answered by rule · 1 ruled by the adjudicator · 0 spoken for · 1 for you\n")
        with open(os.path.join(self.root, 'groom', '2026-09-22-digest.md'), 'w') as f:
            f.write("# Groom digest 2026-09-22\n\n"
                   "3 answered by rule · 5 ruled by the adjudicator · 2 spoken for · 1 for you\n")
        text = status.render(self.root, product, cfg={'scheduler': {'kind': 'none'}})
        rows = {ln.split(' | ')[0].lstrip('| '): ln.split(' | ', 1)[1].rstrip(' |')
               for ln in text.splitlines() if ln.startswith('| ') and 'Metric' not in ln}
        self.assertEqual(rows['Groom'], '2026-09-22: 3 by rule, 5 ruled, 1 for you')

    def test_agents_from_the_registry_and_ready_from_the_feeder(self):
        rows = self.rows({'scheduler': {'kind': 'none'}})
        self.assertEqual(rows['Agents'], '0 (no session registry yet — nothing launched)')
        self.assertEqual(rows['Ready to launch'], '1 — first: CARD → SPEC F-0001')
        self.launch('spec-f-0001', os.getpid())
        rows = self.rows({'scheduler': {'kind': 'none'}})
        self.assertEqual(rows['Agents'], '1 working')
        self.assertEqual(rows['Ready to launch'], '0')  # the Feature is in flight

    def test_features_in_build_against_the_cap(self):
        # F-0195: X / N and the inputs auto sized N from
        rows = self.rows({'scheduler': {'kind': 'none'}})
        self.assertRegex(rows['Features in build'],
                         r'^0 / \d+ \(auto: sessions \d+, quota-stopped \S+, CI free \S+\)$')

    def test_quota_through_the_quota_source(self):
        cfg = {'scheduler': {'kind': 'none'},
               'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 12, 'seven_d_pct': 40}):
            self.assertEqual(self.rows(cfg)['Quota 5h/7d'], 'w1 12%/40%')

    def test_quota_cell_names_the_band(self):
        cfg = {'scheduler': {'kind': 'none'},
               'worker_pool': {'quota_command': 'echo', 'accounts': [{'name': 'w1'}]}}
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 12, 'seven_d_pct': 91}):
            self.assertEqual(self.rows(cfg)['Quota 5h/7d'], 'w1 12%/91% cooldown')
        with mock.patch('asf.workers.quota.CommandQuotaSource.read',
                        lambda self, a: {'five_h_pct': 96, 'seven_d_pct': 12}):
            self.assertEqual(self.rows(cfg)['Quota 5h/7d'], 'w1 96%/12% stop')
        with mock.patch('asf.workers.quota.CommandQuotaSource.read', lambda self, a: None):
            self.assertEqual(self.rows(cfg)['Quota 5h/7d'], 'w1 ?%/?% stop')

    def test_cron_from_the_scheduler_adapter(self):
        from asf import scheduler
        jobs = [{'label': 'asf.p.record-health'}, {'label': 'asf.other.record'}]
        with mock.patch.object(scheduler, 'loaded_jobs', lambda cfg=None: jobs), \
                mock.patch.object(scheduler, 'status',
                                  lambda label: {'state': 'waiting', 'last_exit': 0}):
            self.assertEqual(self.rows({})['Cron'], 'asf.p.record-health waiting (exit 0)')
        with mock.patch.object(scheduler, 'loaded_jobs', lambda cfg=None: []):
            self.assertIn('no job loaded for p', self.rows({})['Cron'])

    def test_cron_names_the_upgrade_a_pending_marker_waits_on(self):
        """B-0141: every tick skipped for half an hour on a pending upgrade while the Cron row
        read a healthy 'waiting (exit 0)'. The row says what the ticks are held on."""
        import time

        from asf import scheduler, upgrade
        at = time.time() - 300
        upgrade.write_pending('f5aa236' + 'a' * 33, 'sample', self.product.name, now=at)
        jobs = [{'label': 'asf.p.record-health'}]
        with mock.patch.object(scheduler, 'loaded_jobs', lambda cfg=None: jobs), \
                mock.patch.object(scheduler, 'status',
                                  lambda label: {'state': 'waiting', 'last_exit': 0}):
            cell = status.cron_cell({}, self.product)
        self.assertIn(f'waiting on upgrade to f5aa236 since '
                      f'{time.strftime("%H:%M", time.localtime(at))} (owner sample)', cell)
        self.assertIn('asf.p.record-health waiting (exit 0)', cell)

    def test_cron_tells_the_markers_owner_of_no_wait(self):
        """B-0141 review round 1 C1 and C2: the owner's own ticks go on, and a marker no tick
        honours — a dead operator wait, a future timestamp — holds nobody. Neither prefixes."""
        import time

        from asf import scheduler, upgrade
        jobs = [{'label': 'asf.p.record-health'}]
        healthy = 'asf.p.record-health waiting (exit 0)'

        def cron():
            with mock.patch.object(scheduler, 'loaded_jobs', lambda cfg=None: jobs), \
                    mock.patch.object(scheduler, 'status',
                                      lambda label: {'state': 'waiting', 'last_exit': 0}):
                return status.cron_cell({}, self.product)

        upgrade.write_pending('f5aa236' + 'a' * 33, self.product.name, self.product.name,
                              now=time.time() - 300)
        self.assertEqual(cron(), healthy)

        upgrade.clear_pending(self.product.name)
        upgrade.write_pending('f5aa236' + 'a' * 33, None, self.product.name, now=time.time() - 300)
        data = upgrade.read_pending(self.product.name)
        data['pid'] = 999999  # killed before its own BaseException cleanup cleared the mark
        upgrade._write_json(upgrade.pending_path(self.product.name), data)
        self.assertEqual(cron(), healthy)

        upgrade.clear_pending(self.product.name)
        upgrade.write_pending('f5aa236' + 'a' * 33, 'other', self.product.name, now=time.time() + 3600)
        self.assertEqual(cron(), healthy)

    def test_cron_flags_a_declared_clock_that_is_not_loaded(self):
        """B-0136: a clock the product declares but that launchd does not currently hold must
        say so — the old code only ever looked at what's loaded, so a clock like this simply
        never appeared in the Cron row."""
        from asf import scheduler
        product = env.Product('p', {'repo_dir': self.tmp, 'main': 'trunk',
                                    'ci': {'provider': 'none'}, 'deploy_sha': 'none',
                                    'clocks': {'daily': {'shadow': True, 'at': '06:50'},
                                               'record-health-wave-prs-harvest':
                                                   {'shadow': True, 'every': '10m'}}})
        jobs = [{'label': 'asf.p.daily'}]
        with mock.patch.object(scheduler, 'loaded_jobs', lambda cfg=None: jobs), \
                mock.patch.object(scheduler, 'status',
                                  lambda label: {'state': 'waiting', 'last_exit': 0}):
            cell = status.cron_cell({}, product)
        self.assertIn('asf.p.daily waiting (exit 0)', cell)
        self.assertIn('clock asf.p.record-health-wave-prs-harvest not loaded', cell)

    def test_a_paused_product_shows_a_paused_row_and_its_cron_says_paused(self):
        from asf import scheduler
        self.assertIsNone(status.paused_cell({}, self.product))
        scheduler.pause('p', ['tick'], 'operator reset', 'op')
        cell = status.paused_cell({}, self.product)
        self.assertIn('asf.p.tick paused since', cell)
        self.assertIn('operator reset; by op', cell)
        product = env.Product('p', {'repo_dir': self.tmp, 'main': 'trunk',
                                    'ci': {'provider': 'none'}, 'deploy_sha': 'none',
                                    'clocks': {'tick': {'shadow': True, 'every': '10m'}}})
        with mock.patch.object(scheduler, 'loaded_jobs', lambda cfg=None: []):
            self.assertIn('clock asf.p.tick paused', status.cron_cell({}, product))
        with mock.patch.object(status, 'paused_cell', return_value='X'), \
                mock.patch.object(status, 'cron_cell', return_value='-'):
            self.assertIn('| PAUSED | X |', status.render(self.root, self.product, cfg={}))

    def test_no_index_names_the_backlog(self):
        self.assertEqual(status.ready_cell(os.path.join(self.tmp, 'nowhere'), self.product),
                         '— (not configured: backlog_dir (no index.json))')

    def test_the_ready_cell_counts_rows_failing_to_spawn(self):
        from asf.feeder import rows as feeder_rows
        from asf.tick import step_wave
        mk = lambda iid, action: feeder_rows.Row(tier=2, kind=feeder_rows.PLAN_CODE, item_id=iid,
                                                 feature_id='F-0001', action=action,
                                                 brief_kind='task', branch='', reason='')
        screened = [step_wave.Screened(mk('T-0001', 'would launch — FAILING TO SPAWN: held ×2')),
                    step_wave.Screened(mk('T-0002', 'would launch'))]
        with mock.patch.object(step_wave, 'would_start', return_value=(screened, 5, [])):
            cell = status.ready_cell(self.root, self.product)
        # the wave tries the failing row, but it does not start: N is the one that does
        self.assertEqual(cell, f'1 — first: {feeder_rows.PLAN_CODE} T-0002; 1 failing to spawn')

    def test_the_ready_cell_counts_only_what_the_wave_would_start(self):
        # 2026-10-04: status said 2 ready while the wave launched 0 — an ungrantable approval
        # hold and a relaunch-cap park, both refused by the wave's own filter
        from asf.feeder import rows as feeder_rows
        from asf.tick import step_wave
        mk = lambda iid: feeder_rows.Row(tier=2, kind=feeder_rows.PLAN_CODE, item_id=iid,
                                         feature_id='F-0001', action='would launch',
                                         brief_kind='task', branch='', reason='')
        screened = [step_wave.Screened(mk('T-0001'), 'held touch_amendable_set (human-now)',
                                       step_wave.HELD),
                    step_wave.Screened(mk('T-0002'), 'launched 2 time(s)', step_wave.CAPPED)]
        with mock.patch.object(step_wave, 'would_start', return_value=(screened, 5, [])):
            cell = status.ready_cell(self.root, self.product)
        self.assertEqual(cell, '0 (2 held back (held 1, relaunch cap 1))')


class DecisionsCellTests(ViewsTestCase):
    def _index(self, n, decided=False):
        items = dict(INDEX['items'])
        del items['F-0001']
        for i in range(1, n + 1):
            fid = f'F-{i:04d}'
            items[fid] = {'id': fid, 'type': 'feature', 'title': 't', 'folder': 'features',
                          'parent': 'E-0001', 'state': 'New', 'decided': decided, 'rank': i}
        with open(os.path.join(self.root, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': items}, f)

    def rows(self, cfg=None):
        text = status.render(self.root, self.product, cfg=cfg or {'scheduler': {'kind': 'none'}})
        return [ln.split(' | ')[0].lstrip('| ') for ln in text.splitlines() if ln.startswith('| ')], \
            {ln.split(' | ')[0].lstrip('| '): ln.split(' | ', 1)[1].rstrip(' |')
             for ln in text.splitlines() if ln.startswith('| ') and 'Metric' not in ln}

    def test_the_count_and_the_first_ids(self):
        self._index(73)
        self.assertEqual(status.decisions_cell(self.root, self.product),
                         '73 undecided — next: F-0001, F-0002, F-0003, F-0004, F-0005')
        self.assertEqual(self.rows()[1]['Decisions'],
                         '73 undecided — next: F-0001, F-0002, F-0003, F-0004, F-0005')

    def test_every_card_decided_is_zero(self):
        self._index(3, decided=True)
        self.assertEqual(status.decisions_cell(self.root, self.product), '0')

    def test_no_index_names_the_backlog(self):
        self.assertEqual(status.decisions_cell(os.path.join(self.tmp, 'nowhere'), self.product),
                         status.ready_cell(os.path.join(self.tmp, 'nowhere'), self.product))

    def test_a_raising_cell_leaves_the_table(self):
        with mock.patch.object(status, 'decisions_cell', side_effect=ValueError('boom')):
            names, rows = self.rows()
        self.assertEqual(rows['Decisions'], '? (ValueError: boom)')
        self.assertIn('Groom', rows)

    def test_the_row_sits_directly_after_ready_to_launch(self):
        names, _ = self.rows()
        self.assertEqual(names[names.index('Ready to launch') + 1], 'Decisions')


class RecordCellTests(ViewsTestCase):
    def _index(self, extra):
        items = dict(INDEX['items'])
        items.update(extra)
        with open(os.path.join(self.root, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': items}, f)

    def rows(self):
        text = status.render(self.root, self.product, cfg={'scheduler': {'kind': 'none'}})
        return [ln.split(' | ')[0].lstrip('| ') for ln in text.splitlines() if ln.startswith('| ')], \
            {ln.split(' | ')[0].lstrip('| '): ln.split(' | ', 1)[1].rstrip(' |')
             for ln in text.splitlines() if ln.startswith('| ') and 'Metric' not in ln}

    def test_a_healthy_record_says_zero_no_rule(self):
        self.assertEqual(status.record_cell(self.root), '2 open · 0 Active · 0 blocked · 0 no rule')
        self.assertEqual(self.rows()[1]['Record'], '2 open · 0 Active · 0 blocked · 0 no rule')

    def test_a_shapeless_story_is_counted(self):
        self._index({
            'S-0001': {'id': 'S-0001', 'type': 'story', 'folder': 'stories', 'parent': 'F-0001',
                       'state': 'Active', 'evidence': ['no evidence found (2026-09-23)', 'rule: no-rule']},
            'S-0002': {'id': 'S-0002', 'type': 'story', 'folder': 'stories', 'parent': 'F-0001',
                       'state': 'Closed', 'evidence': ['rule: landed']},
            'S-0003': {'id': 'S-0003', 'type': 'story', 'folder': 'stories', 'parent': 'F-0001',
                       'state': 'New', 'blocked': True, 'evidence': ['rule: no-rule', 'rule: planned']},
            'S-0004': {'id': 'S-0004', 'type': 'story', 'folder': 'stories', 'parent': 'F-0001',
                       'state': 'New', 'removed': 'groom 2026-09-01', 'evidence': ['rule: no-rule']},
        })
        self.assertEqual(status.record_cell(self.root), '4 open · 1 Active · 1 blocked · 1 no rule')

    def test_no_index_or_an_unreadable_one_is_not_configured(self):
        nowhere = os.path.join(self.tmp, 'nowhere')
        self.assertEqual(status.record_cell(nowhere), '— (not configured: backlog_dir (no index.json))')
        with open(os.path.join(self.root, 'index.json'), 'w') as f:
            f.write('{not json')
        self.assertEqual(status.record_cell(self.root),
                         '— (not configured: backlog_dir (index.json unreadable))')
        self.assertIn('Groom', self.rows()[1])

    def test_the_row_sits_directly_above_ready_to_launch(self):
        names, _ = self.rows()
        self.assertEqual(names[names.index('Ready to launch') - 1], 'Record')


class CapacityTable(ViewsTestCase):
    def test_one_row_per_product_with_the_bound_by_column(self):
        asf = env.Product('asf', {'capacity': {'sessions': 3}})
        web = env.Product('web', {})
        cfg = {'capacity': {'per_product': {'sessions': 2}}}
        text = capacity_view.render([asf, web], cfg)
        lines = [ln for ln in text.splitlines() if ln]
        header = next(ln for ln in lines if ln.startswith('CAPACITY'))
        self.assertIn('operator total: sessions ?, ci ?', header)
        col_header = next(ln for ln in lines if ln.startswith('product'))
        for col in ('product', 'sessions', 'in flight', 'free', 'bound by', 'ci', 'runs', 'batch'):
            self.assertIn(col, col_header)
        asf_row = next(ln for ln in lines if ln.startswith('asf '))
        web_row = next(ln for ln in lines if ln.startswith('web '))
        self.assertIn('product', asf_row)
        self.assertIn('operator default', web_row)

    def test_json_shape(self):
        asf = env.Product('asf', {'capacity': {
            'sessions': 3, 'ci': 2, 'batch': {'per_run': 8, 'parallel': 2, 'runners': 4}}})
        cfg = {'capacity': {'total': {'sessions': 6, 'ci': 4}}}
        [data] = capacity_view.as_json([asf], cfg)
        self.assertEqual(data, {
            'product': 'asf',
            'sessions': {'ceiling': 3, 'inflight': 0, 'free': 3, 'bound_by': 'product'},
            'ci': {'ceiling': 2, 'inflight': None, 'free': None, 'bound_by': 'product'},
            'batch': {'per_run': 8, 'parallel': 2, 'runners': 4},
            'deprecated': [],
        })


class StatusRow(ViewsTestCase):
    def test_capacity_row_says_not_configured_when_nothing_is_set(self):
        self.assertEqual(status.capacity_cell({}, self.product), '— (not configured: capacity)')

    def test_capacity_row_names_the_ceilings_when_configured(self):
        product = env.Product('p', {'capacity': {'sessions': 3}})
        cfg = {'capacity': {'total': {'sessions': 6}}}
        self.assertEqual(status.capacity_cell(cfg, product), 'sessions 0/3 (operator total 6)')

    def test_ci_clause_names_the_batch_gate_not_a_breached_cap(self):
        # capacity.ci gates only the batch step; every ci.workflow run counts in flight
        from unittest import mock
        from asf import capacity as capacity_mod
        product = env.Product('p', {'capacity': {'sessions': 3, 'ci': 4}})
        with mock.patch.object(capacity_mod, '_pick_ci_source') as pick:
            pick.return_value.read.return_value = 13
            cell = status.capacity_cell({}, product)
        self.assertIn('ci 13 runs in flight (batch starts below 4 — batch waits)', cell)
        self.assertNotIn('13/4', cell)
        self.assertEqual(status.ci_clause(2, 4), 'ci 2 runs in flight (batch starts below 4)')
        self.assertEqual(status.ci_clause(None, 4), 'ci ? runs in flight (batch starts below 4)')


class SpanTests(unittest.TestCase):
    def test_span_buckets(self):
        self.assertEqual(ix.span(0), '0m')
        self.assertEqual(ix.span(59), '0m')
        self.assertEqual(ix.span(60), '1m')
        self.assertEqual(ix.span(3599), '59m')
        self.assertEqual(ix.span(3600), '1h')
        self.assertEqual(ix.span(172799), '47h')
        self.assertEqual(ix.span(172800), '2d')
        self.assertEqual(ix.span(-5), '0m')

    def test_age_agrees_with_span_over_a_known_timestamp(self):
        now = dt.datetime.now(dt.timezone.utc)
        ts = (now - dt.timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.assertEqual(ix.age(ts), ix.span((now - ix.parse_ts(ts)).total_seconds()))

    def test_age_of_none_is_the_unparseable_dash(self):
        self.assertEqual(ix.age(None), '—')


class SubtreeUsdTests(unittest.TestCase):
    """F-0052 §2.2, T1: the one function the roadmap, the feeder and the groom all sum through."""

    def items(self):
        return {
            'E-0001': {'id': 'E-0001', 'type': 'epic', 'children': ['F-0001', 'F-0002']},
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'parent': 'E-0001',
                       'children': ['T-0001', 'T-0002'], 'cost': {'usd': 1.5}},
            'T-0001': {'id': 'T-0001', 'type': 'task', 'parent': 'F-0001',
                       'cost': {'usd': 10.0}},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'parent': 'F-0001',
                       'cost': {'usd': 20.0}},
            'F-0002': {'id': 'F-0002', 'type': 'feature', 'parent': 'E-0001', 'children': []},
        }

    def test_subtree_usd_equals_the_hand_sum_and_ix_usd_of_ix_subtree(self):
        items = self.items()
        epic = items['E-0001']
        self.assertEqual(ix.subtree_usd(items, epic), 31.5)
        self.assertEqual(ix.subtree_usd(items, epic), ix.usd(ix.subtree(items, epic)))

    def test_a_feature_own_figure_counts_as_well_as_its_tasks(self):
        items = self.items()
        self.assertEqual(ix.subtree_usd(items, items['F-0001']), 31.5)

    def test_none_when_nothing_beneath_it_is_measured(self):
        items = self.items()
        self.assertIsNone(ix.subtree_usd(items, items['F-0002']))

    def test_a_children_list_naming_an_id_twice_is_summed_once(self):
        items = self.items()
        items['E-0001']['children'] = ['F-0001', 'F-0001', 'F-0002']
        self.assertEqual(ix.subtree_usd(items, items['E-0001']), 31.5)


class RoadmapEpicBudgetTests(unittest.TestCase):
    """F-0052 §2.5, T4: the roadmap's Spend / budget cell is budget.epic_spend's verdict."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='roadmap_budget_test_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, epics):
        items = {}
        for i, epic in enumerate(epics, start=1):
            eid = f'E-{i:04d}'
            items[eid] = dict({'id': eid, 'type': 'epic', 'title': 'goal', 'folder': 'epics',
                                'state': 'Active', 'decided': True, 'rank': i}, **epic)
        with open(os.path.join(self.tmp, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': items}, f)

    def _row(self, text, eid):
        line = next(l for l in text.splitlines() if f'| {eid} ' in l)
        return [c.strip() for c in line.strip('|').split('|')]

    def _spend_cell(self, eid='E-0001', **kwargs):
        text = roadmap.render(self.tmp, **kwargs)
        return self._row(text, eid)[-1]

    def test_over_budget_cell_names_both_figures_and_the_marker(self):
        self._write([{'cost': {'usd': 512.40}, 'budget_usd': 500}])
        self.assertEqual(self._spend_cell(), f'$512.40 / $500.00 · {budget.OVER_MARK}')

    def test_under_budget_cell_has_no_marker(self):
        self._write([{'cost': {'usd': 499.99}, 'budget_usd': 500}])
        self.assertEqual(self._spend_cell(), '$499.99 / $500.00')

    def test_no_budget_prints_a_dash_for_it(self):
        self._write([{'cost': {'usd': 512.40}}])
        self.assertEqual(self._spend_cell(), '$512.40 / —')

    def test_no_figures_at_all_is_the_table_own_empty_dash(self):
        self._write([{}])
        self.assertEqual(self._spend_cell(), '—')

    def test_a_text_budget_with_nothing_measured_is_also_empty(self):
        self._write([{'budget_usd': 'lots'}])
        self.assertEqual(self._spend_cell(), '—')

    def test_render_with_no_product_gives_the_same_cells(self):
        self._write([{'cost': {'usd': 512.40}, 'budget_usd': 500}])
        self.assertEqual(self._spend_cell(product=None),
                          f'$512.40 / $500.00 · {budget.OVER_MARK}')

    def test_table_still_has_its_seven_columns_and_row_order(self):
        self._write([{'cost': {'usd': 512.40}, 'budget_usd': 500}, {'cost': {'usd': 1}}])
        text = roadmap.render(self.tmp)
        self.assertIn('| # | Epic | State | On prod | Next | Blocked | Spend / budget |', text)
        self.assertLess(text.index('E-0001'), text.index('E-0002'))


class ProdViewTests(ViewsTestCase):
    def test_no_deploy_configured(self):
        text = prod.render(self.root, self.product)
        self.assertIn('no deploy configured', text)
        self.assertIn('**trunk**', text)
        self.assertEqual(prod._deploy_sha(self.product, 'prod'), (None, None))
        self.assertFalse(prod.deploy_configured(env.Product('p', {})))


class RetirementRuleTests(ViewsTestCase):
    """F-0172: a moved card is retired to the index reader too — ``removed:`` or ``moved_to:``,
    one rule, pinned against ``asf.record.core.is_retired`` so the two can never disagree."""

    def write(self, items):
        with open(os.path.join(self.root, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': items}, f)

    def test_the_index_readers_retirement_is_the_records(self):
        from asf.record.core import is_retired
        from asf.views import index_reader as ix
        for meta in ({}, {'removed': 'superseded'}, {'moved_to': 'other:F-0009'},
                     {'removed': 'moved', 'moved_to': 'other:F-0009'}):
            entry = dict(meta, id='F-0001', type='feature', state='Active')
            self.assertEqual(ix.retired(entry), is_retired(meta), meta)
            self.assertEqual('F-0001' in ix.live({'F-0001': entry}), not is_retired(meta), meta)

    def test_a_moved_feature_is_out_of_the_board(self):
        self.write({
            'E-0001': {'id': 'E-0001', 'type': 'epic', 'title': 'goal', 'folder': 'epics',
                       'state': 'New', 'decided': True, 'rank': 1},
            'F-0001': {'id': 'F-0001', 'type': 'feature', 'title': 'superseded',
                       'folder': 'features', 'parent': 'E-0001', 'state': 'New', 'decided': True,
                       'rank': 1, 'stage': 'plan-approved', 'removed': 'superseded by F-0009'},
            'F-0002': {'id': 'F-0002', 'type': 'feature', 'title': 'elsewhere',
                       'folder': 'features', 'parent': 'E-0001', 'state': 'Active',
                       'decided': True, 'rank': 2, 'stage': 'building 1/3',
                       'moved_to': 'other:F-0009'},
            'F-0003': {'id': 'F-0003', 'type': 'feature', 'title': 'still here',
                       'folder': 'features', 'parent': 'E-0001', 'state': 'Active',
                       'decided': True, 'rank': 3, 'stage': 'card'},
        })
        text = board.render(self.root)
        self.assertIn('1 Features', text)
        self.assertNotIn('F-0002', text)
        self.assertNotIn('F-0001', text)
        self.assertIn('F-0003', text)

    def test_a_moved_card_lands_in_the_right_retirement_set(self):
        from asf.views import index_reader as ix
        raw = {
            'T-0001': {'id': 'T-0001', 'type': 'task', 'state': 'Closed',
                       'moved_to': 'other:T-0010'},
            'T-0002': {'id': 'T-0002', 'type': 'task', 'state': 'Active',
                       'moved_to': 'other:T-0011'},
            'T-0003': {'id': 'T-0003', 'type': 'task', 'state': 'Closed',
                       'removed': 'superseded'},
            'T-0004': {'id': 'T-0004', 'type': 'task', 'state': 'Active',
                       'removed': 'groomed away'},
            'T-0005': {'id': 'T-0005', 'type': 'task', 'state': 'Active'},
        }
        out = ix.live(raw)
        self.assertEqual(set(out), {'T-0005'})
        self.assertEqual(set(out.retired_done), {'T-0001', 'T-0003'})
        self.assertEqual(set(out.retired_open), {'T-0002', 'T-0004'})


if __name__ == '__main__':
    unittest.main()
