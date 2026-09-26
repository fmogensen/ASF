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

from asf import env
from asf.views import capacity as capacity_view
from asf.views import index_reader as ix
from asf.views import prod, sessions, status, work
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
        self.assertEqual(names[names.index('Features') + 1], 'Decisions')


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


class WorkCountsTests(unittest.TestCase):
    """§3.1: the counts, called directly — no table, no root, no product."""
    NOW = dt.datetime(2026, 9, 24, 12, 0, 0, tzinfo=dt.timezone.utc)

    @staticmethod
    def _bug(id_, sev=None, state='New', stage_since=None):
        b = {'id': id_, 'type': 'bug', 'state': state}
        if sev is not None:
            b['severity'] = sev
        if stage_since is not None:
            b['stage_since'] = stage_since
        return b

    @staticmethod
    def _feature(id_, stage=None, decided=None, stage_since=None):
        f = {'id': id_, 'type': 'feature'}
        if stage is not None:
            f['stage'] = stage
        if decided is not None:
            f['decided'] = decided
        if stage_since is not None:
            f['stage_since'] = stage_since
        return f

    def test_severity_never_falls_back_to_s3(self):
        self.assertEqual(work.severity({'severity': None}), work.UNTYPED)
        self.assertEqual(work.severity({}), work.UNTYPED)
        self.assertEqual(work.severity({'severity': 'S9'}), work.UNTYPED)
        self.assertEqual(work.severity({'severity': 'S1'}), 'S1')

    def test_on_day_is_false_for_absent_or_unparseable(self):
        self.assertFalse(work.on_day(None, self.NOW))
        self.assertFalse(work.on_day('not-a-date', self.NOW))
        self.assertTrue(work.on_day(self.NOW.strftime('%Y-%m-%dT%H:%M:%SZ'), self.NOW))

    def test_within_is_half_open_and_requires_a_parseable_ts(self):
        since, now = '2026-09-22T12:00:00Z', '2026-09-24T12:00:00Z'
        self.assertFalse(work.within(since, since, now))    # exclusive of since
        self.assertTrue(work.within(now, since, now))       # inclusive of now
        self.assertFalse(work.within(None, since, now))
        self.assertFalse(work.within('nope', since, now))

    def test_open_by_severity_includes_untyped_and_excludes_done(self):
        items = {b['id']: b for b in [
            self._bug('B-0001', 'S1', 'Active'),
            self._bug('B-0002', 'S2', 'New'),
            self._bug('B-0003', 'S3', 'New'),
            self._bug('B-0004', 'S2', 'Closed', stage_since='2026-09-24T09:00:00Z'),
            self._bug('B-0005', None, 'New'),
        ]}
        result = work.bugs(items, {}, self.NOW)
        self.assertEqual(result.open_by_sev, {'S1': 1, 'S2': 1, 'S3': 1, 'S?': 1})
        self.assertEqual(result.total, 5)

    def test_fixed_today_is_the_local_day_boundary(self):
        now = dt.datetime(2026, 9, 24, 15, 30, 0, tzinfo=dt.timezone.utc)
        local_midnight = dt.datetime.combine(now.astimezone().date(), dt.time.min,
                                             tzinfo=now.astimezone().tzinfo)
        just_after = (local_midnight + dt.timedelta(minutes=1)).astimezone(dt.timezone.utc)
        just_before = (local_midnight - dt.timedelta(minutes=1)).astimezone(dt.timezone.utc)
        items = {b['id']: b for b in [
            self._bug('B-0001', 'S2', 'Closed', stage_since=just_after.strftime('%Y-%m-%dT%H:%M:%SZ')),
            self._bug('B-0002', 'S2', 'Closed', stage_since=just_before.strftime('%Y-%m-%dT%H:%M:%SZ')),
        ]}
        result = work.bugs(items, {}, now)
        self.assertEqual(result.fixed_today, 1)

    def test_in_fix_counts_each_bug_once_and_excludes_ended_and_non_bugs(self):
        items = {b['id']: b for b in [self._bug('B-0001', 'S1', 'Active'),
                                      self._bug('B-0002', 'S2', 'Active')]}
        items['F-0001'] = self._feature('F-0001', stage='building')
        runs = {
            'fix-b-0001-a': [{'item': 'B-0001'}],
            'fix-b-0001-b': [{'item': 'B-0001'}],
            'fix-b-0002': [{'item': 'B-0002', 'ended': '2026-09-24T00:00:00Z'}],
            'spec-f-0001': [{'item': 'F-0001'}],
        }
        result = work.bugs(items, runs, self.NOW)
        self.assertEqual(result.in_fix, ['B-0001'])
        self.assertEqual(result.in_fix_s1, ['B-0001'])

    def test_oldest_open_s1_s2_breaks_ties_to_the_lower_id(self):
        items = {b['id']: b for b in [
            self._bug('B-0001', 'S1', 'Active', stage_since='2026-09-20T12:00:00Z'),
            self._bug('B-0100', 'S1', 'Active', stage_since='2026-09-20T12:00:00Z'),
            self._bug('B-0002', 'S2', 'Active', stage_since='2026-09-22T12:00:00Z'),
            self._bug('B-0003', 'S3', 'Active', stage_since='2026-09-01T12:00:00Z'),
        ]}
        result = work.bugs(items, {}, self.NOW)
        self.assertEqual(result.oldest, ('B-0001', 'S1', 4 * 86400))

    def test_oldest_is_the_empty_tuple_with_no_measurable_open_high_severity_bug(self):
        items = {b['id']: b for b in [self._bug('B-0001', 'S3', 'Active')]}
        self.assertEqual(work.bugs(items, {}, self.NOW).oldest, ())

    def test_a_bug_with_no_stage_since_is_open_never_fixed_today_or_oldest(self):
        items = {
            'B-0001': {'id': 'B-0001', 'type': 'bug', 'state': 'Active', 'severity': 'S1'},
            'B-0002': self._bug('B-0002', 'S2', 'Closed', stage_since='not-a-date'),
        }
        result = work.bugs(items, {}, self.NOW)
        self.assertEqual(result.open_by_sev['S1'], 1)
        self.assertEqual(result.fixed_today, 0)
        self.assertEqual(result.oldest, ())

    def test_feature_stage_counts_partition_the_five_clauses(self):
        items = {f['id']: f for f in [
            self._feature('F-0001', stage=None, decided=True),
            self._feature('F-0002', stage='card', decided=False),
            self._feature('F-0003', stage='card'),
            self._feature('F-0004', stage='spec-draft'),
            self._feature('F-0005', stage='spec-review'),
            self._feature('F-0006', stage='spec-approved'),
            self._feature('F-0007', stage='plan-draft'),
            self._feature('F-0008', stage='plan-review'),
            self._feature('F-0009', stage='plan-approved'),
            self._feature('F-0010', stage='building'),
        ]}
        result = work.features(items, {}, self.NOW, 7)
        self.assertEqual(result.building, 1)
        self.assertEqual(result.spec_plan, 6)
        self.assertEqual(result.decided_waiting, 1)
        self.assertEqual(result.undecided, 2)
        self.assertEqual(result.total, 10)

    def test_landed_today_covers_landed_and_on_prod_newest_first(self):
        items = {f['id']: f for f in [
            self._feature('F-0001', stage='landed', stage_since='2026-09-24T09:00:00Z'),
            self._feature('F-0002', stage='on-prod', stage_since='2026-09-24T11:00:00Z'),
            self._feature('F-0003', stage='landed', stage_since='2026-09-23T09:00:00Z'),
        ]}
        result = work.features(items, {}, self.NOW, 7)
        self.assertEqual(result.landed_today, ['F-0002', 'F-0001'])

    def test_median_to_land_skips_the_unmeasured_and_the_out_of_window(self):
        items = {
            'F-0010': self._feature('F-0010', stage='landed', stage_since='2026-09-22T12:00:00Z'),
            'F-0011': self._feature('F-0011', stage='landed', stage_since='2026-09-20T12:00:00Z'),
            'F-0012': self._feature('F-0012', stage='landed', stage_since='2026-09-18T12:00:00Z'),
            'F-0013': self._feature('F-0013', stage='landed', stage_since='2026-09-19T12:00:00Z'),
            'F-0014': self._feature('F-0014', stage='landed', stage_since='2026-09-15T12:00:00Z'),
            'T-0012': {'id': 'T-0012', 'type': 'task', 'parent': 'F-0012'},
        }
        runs = {
            'land-f-0010': [{'item': 'F-0010', 'feature': 'F-0010', 'started': '2026-09-21T12:00:00Z'}],
            'land-f-0011': [{'item': 'F-0011', 'feature': 'F-0011', 'started': '2026-09-17T12:00:00Z'}],
            'land-t-0012': [{'item': 'T-0012', 'started': '2026-09-12T12:00:00Z'}],
            'land-f-0014': [{'item': 'F-0014', 'feature': 'F-0014', 'started': '2026-09-13T12:00:00Z'}],
        }
        result = work.features(items, runs, self.NOW, 7)
        self.assertEqual(result.measured, 3)
        self.assertEqual(result.median_to_land, 3 * 86400)

    def test_median_to_land_of_an_even_sample_is_the_mean_of_the_two_middles(self):
        items = {
            'F-0001': self._feature('F-0001', stage='landed', stage_since='2026-09-23T12:00:00Z'),
            'F-0002': self._feature('F-0002', stage='landed', stage_since='2026-09-22T12:00:00Z'),
        }
        runs = {
            'a': [{'item': 'F-0001', 'feature': 'F-0001', 'started': '2026-09-22T12:00:00Z'}],
            'b': [{'item': 'F-0002', 'feature': 'F-0002', 'started': '2026-09-19T12:00:00Z'}],
        }
        result = work.features(items, runs, self.NOW, 7)
        self.assertEqual(result.measured, 2)
        self.assertEqual(result.median_to_land, 2 * 86400)

    def test_no_measurable_feature_is_a_none_median(self):
        items = {'F-0001': self._feature('F-0001', stage='landed', stage_since='2026-09-23T12:00:00Z')}
        result = work.features(items, {}, self.NOW, 7)
        self.assertIsNone(result.median_to_land)
        self.assertEqual(result.measured, 0)

    def test_every_function_is_pure_and_opens_no_subprocess(self):
        items = {'B-0001': self._bug('B-0001', 'S1', 'Active', stage_since='2026-09-20T12:00:00Z')}
        runs = {'fix-b-0001': [{'item': 'B-0001'}]}
        fitems = {'F-0001': self._feature('F-0001', stage='landed', stage_since='2026-09-20T12:00:00Z')}
        with mock.patch('subprocess.run', side_effect=AssertionError('no subprocess')):
            self.assertEqual(work.bugs(items, runs, self.NOW), work.bugs(items, runs, self.NOW))
            self.assertEqual(work.features(fitems, {}, self.NOW, 7),
                             work.features(fitems, {}, self.NOW, 7))


class StatusWorkRows(ViewsTestCase):
    """§3.2: the two cells, through ``status.render`` and the existing ``rows()`` markdown parser."""

    def _index(self, items):
        with open(os.path.join(self.root, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': items}, f)

    def rows(self, cfg=None):
        text = status.render(self.root, self.product, cfg=cfg or {'scheduler': {'kind': 'none'}})
        names = [ln.split(' | ')[0].lstrip('| ') for ln in text.splitlines() if ln.startswith('| ')]
        cells = {ln.split(' | ')[0].lstrip('| '): ln.split(' | ', 1)[1].rstrip(' |')
                for ln in text.splitlines() if ln.startswith('| ') and 'Metric' not in ln}
        return names, cells

    def test_bugs_and_features_rows_render_the_exact_cells(self):
        now = dt.datetime.now(dt.timezone.utc)

        def ago(**kw):
            return (now - dt.timedelta(**kw)).strftime('%Y-%m-%dT%H:%M:%SZ')

        self._index({
            'B-0091': {'id': 'B-0091', 'type': 'bug', 'folder': 'bugs', 'state': 'Active',
                      'severity': 'S1', 'stage_since': ago(days=3, hours=1)},
            'B-0092': {'id': 'B-0092', 'type': 'bug', 'folder': 'bugs', 'state': 'Active',
                      'severity': 'S2', 'stage_since': ago(days=1)},
            'B-0093': {'id': 'B-0093', 'type': 'bug', 'folder': 'bugs', 'state': 'New',
                      'severity': 'S3'},
            'B-0094': {'id': 'B-0094', 'type': 'bug', 'folder': 'bugs', 'state': 'Closed',
                      'severity': 'S2', 'stage_since': ago(hours=1)},
            'F-0090': {'id': 'F-0090', 'type': 'feature', 'folder': 'features', 'stage': 'landed',
                      'stage_since': ago(hours=1), 'decided': True},
            'F-0091': {'id': 'F-0091', 'type': 'feature', 'folder': 'features', 'stage': 'building',
                      'decided': True},
            'F-0092': {'id': 'F-0092', 'type': 'feature', 'folder': 'features', 'stage': 'spec-review',
                      'decided': True},
            'F-0093': {'id': 'F-0093', 'type': 'feature', 'folder': 'features', 'stage': 'card',
                      'decided': True},
            'F-0094': {'id': 'F-0094', 'type': 'feature', 'folder': 'features', 'stage': 'card',
                      'decided': False},
            'T-0090': {'id': 'T-0090', 'type': 'task', 'folder': 'tasks', 'parent': 'F-0090'},
        })
        pool_mod.append_session(self.product, dict(job='fix-b-0091', item='B-0091', kind='fix-bug',
                                                    pid=os.getpid(), account='w1',
                                                    branch='fix-bug/b-0091'))
        pool_mod.append_session(self.product, dict(job='land-t-0090', item='T-0090', kind='task',
                                                    pid=os.getpid(), account='w1', branch='task/t-0090',
                                                    started=ago(days=3, hours=1)))
        _names, rows = self.rows()
        self.assertEqual(rows['Bugs'], 'open S1 1 · S2 1 · S3 1 · in fix 1 (S1 B-0091) · '
                                       'fixed today 1 · oldest open S1/S2 B-0091 S1 3d')
        self.assertEqual(rows['Features'], 'landed today 1 (F-0090) · building 1 · spec/plan 1 · '
                                           'decided, waiting 1 · undecided 1 · median to land 3d (7d, n=1)')

    def test_s_question_clause_only_appears_when_non_zero(self):
        self._index({'B-0001': {'id': 'B-0001', 'type': 'bug', 'folder': 'bugs', 'state': 'New',
                                'severity': 'S2'}})
        self.assertNotIn('S?', self.rows()[1]['Bugs'])
        self._index({
            'B-0001': {'id': 'B-0001', 'type': 'bug', 'folder': 'bugs', 'state': 'New', 'severity': 'S2'},
            'B-0002': {'id': 'B-0002', 'type': 'bug', 'folder': 'bugs', 'state': 'New'},
            'B-0003': {'id': 'B-0003', 'type': 'bug', 'folder': 'bugs', 'state': 'New'},
        })
        self.assertIn('· S? 2', self.rows()[1]['Bugs'])

    def test_the_s1_parenthetical_is_absent_when_only_an_s2_is_in_fix(self):
        self._index({'B-0001': {'id': 'B-0001', 'type': 'bug', 'folder': 'bugs', 'state': 'New',
                                'severity': 'S2'}})
        pool_mod.append_session(self.product, dict(job='fix-b-0001', item='B-0001', kind='fix-bug',
                                                    pid=os.getpid(), account='w1',
                                                    branch='fix-bug/b-0001'))
        cell = self.rows()[1]['Bugs']
        self.assertIn('in fix 1', cell)
        self.assertNotIn('(S1', cell)

    def test_oldest_is_none_when_every_open_bug_is_s3(self):
        self._index({'B-0001': {'id': 'B-0001', 'type': 'bug', 'folder': 'bugs', 'state': 'New',
                                'severity': 'S3'}})
        self.assertIn('oldest open S1/S2 none', self.rows()[1]['Bugs'])

    def test_median_unknown_when_nothing_landed_in_the_window(self):
        self._index({'F-0001': {'id': 'F-0001', 'type': 'feature', 'folder': 'features',
                                'stage': 'building', 'decided': True}})
        self.assertIn('median to land unknown (7d, n=0)', self.rows()[1]['Features'])

    def test_empty_record_forms_are_not_a_bare_dash(self):
        self._index({'F-0001': {'id': 'F-0001', 'type': 'feature', 'folder': 'features',
                                'stage': 'building', 'decided': True}})
        self.assertEqual(self.rows()[1]['Bugs'], 'no Bugs in the record')
        self._index({'B-0001': {'id': 'B-0001', 'type': 'bug', 'folder': 'bugs', 'state': 'New',
                                'severity': 'S3'}})
        self.assertEqual(self.rows()[1]['Features'], 'no Features in the record')

    def test_row_order_is_ready_to_launch_bugs_features(self):
        self._index({'F-0001': {'id': 'F-0001', 'type': 'feature', 'folder': 'features',
                                'state': 'New', 'decided': True}})
        names, _cells = self.rows()
        i = names.index('Ready to launch')
        self.assertEqual(names[i:i + 3], ['Ready to launch', 'Bugs', 'Features'])

    def test_land_window_days_convention_changes_the_window_and_its_label(self):
        self._index({'F-0001': {'id': 'F-0001', 'type': 'feature', 'folder': 'features',
                                'stage': 'building', 'decided': True}})
        product = env.Product('p', {'repo_dir': self.tmp, 'main': 'trunk', 'ci': {'provider': 'none'},
                                    'deploy_sha': 'none', 'conventions': {'land_window_days': 14}})
        text = status.render(self.root, product, cfg={'scheduler': {'kind': 'none'}})
        rows = {ln.split(' | ')[0].lstrip('| '): ln.split(' | ', 1)[1].rstrip(' |')
               for ln in text.splitlines() if ln.startswith('| ') and 'Metric' not in ln}
        self.assertIn('(14d,', rows['Features'])


class WorkFailureTests(ViewsTestCase):
    """§3.3: neither row can cost its reader anything."""

    def test_no_index_names_the_key_for_both_rows(self):
        nowhere = os.path.join(self.tmp, 'nowhere')
        self.assertEqual(work.bugs_cell(nowhere, self.product),
                         '— (not configured: backlog_dir (no index.json))')
        self.assertEqual(work.features_cell(nowhere, self.product),
                         '— (not configured: backlog_dir (no index.json))')

    def test_a_corrupt_index_is_one_cell_and_the_table_survives(self):
        with open(os.path.join(self.root, 'index.json'), 'w') as f:
            f.write('{not json')
        text = status.render(self.root, self.product, cfg={'scheduler': {'kind': 'none'}})
        rows = {ln.split(' | ')[0].lstrip('| '): ln.split(' | ', 1)[1].rstrip(' |')
               for ln in text.splitlines() if ln.startswith('| ') and 'Metric' not in ln}
        self.assertTrue(rows['Bugs'].startswith('? ('), rows['Bugs'])
        self.assertTrue(rows['Features'].startswith('? ('), rows['Features'])
        self.assertIn('Groom', rows)

    def test_an_unreadable_ledger_still_renders_with_in_fix_zero(self):
        with open(os.path.join(self.root, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': {
                'B-0001': {'id': 'B-0001', 'type': 'bug', 'state': 'New', 'severity': 'S1'},
            }}, f)
        with mock.patch('asf.workers.lifecycle.runs', side_effect=OSError('nope')):
            cell = work.bugs_cell(self.root, self.product)
        self.assertIn('in fix 0', cell)

    def test_a_bug_with_no_stage_since_is_open_not_fixed_and_not_oldest(self):
        items = {'B-0001': {'id': 'B-0001', 'type': 'bug', 'state': 'Active', 'severity': 'S1'}}
        result = work.bugs(items, {}, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(result.open_by_sev['S1'], 1)
        self.assertEqual(result.fixed_today, 0)
        self.assertEqual(result.oldest, ())


class ProdViewTests(ViewsTestCase):
    def test_no_deploy_configured(self):
        text = prod.render(self.root, self.product)
        self.assertIn('no deploy configured', text)
        self.assertIn('**trunk**', text)
        self.assertEqual(prod._deploy_sha(self.product, 'prod'), (None, None))
        self.assertFalse(prod.deploy_configured(env.Product('p', {})))


if __name__ == '__main__':
    unittest.main()
