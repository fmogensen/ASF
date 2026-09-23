"""asf.views — sessions (the live registry), status (every row filled or naming its key), prod
(no deploy), against a temp ASF_HOME and a tiny record. No network: every row that would call
``gh`` is either unconfigured here or stubbed."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import env
from asf.views import capacity as capacity_view
from asf.views import prod, sessions, status
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
        self.assertEqual(rows['Prod'], '— (not configured: ci.deploy_workflow)')
        self.assertEqual(rows['Quota 5h/7d'], '— (not configured: worker_pool.quota_command)')
        self.assertEqual(rows['Cron'], '— (not configured: scheduler.kind (none has no status adapter))')
        self.assertEqual(rows['Groom'], '— (not configured: approvals.groom)')
        for name, cell in rows.items():
            self.assertFalse(cell.strip() == '—', name)

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

    def test_no_index_names_the_backlog(self):
        self.assertEqual(status.ready_cell(os.path.join(self.tmp, 'nowhere'), self.product),
                         '— (not configured: backlog_dir (no index.json))')


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


class ProdViewTests(ViewsTestCase):
    def test_no_deploy_configured(self):
        text = prod.render(self.root, self.product)
        self.assertIn('no deploy configured', text)
        self.assertIn('**trunk**', text)
        self.assertEqual(prod._deploy_sha(self.product, 'prod'), (None, None))
        self.assertFalse(prod.deploy_configured(env.Product('p', {})))


if __name__ == '__main__':
    unittest.main()
