"""asf.cli.main — a refused product file is one NEEDS OPERATOR line and exit 2, never a traceback
(B-0045); a record command uses the product's record, not the cwd, even when the cwd is a
product repo (B-0050)."""
import argparse
import contextlib
import dataclasses
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from asf import cli, env
from asf.security import alerts
from asf.views import status


class FakeHost(alerts.Host):
    """Stands in for :class:`asf.security.alerts.GitHubHost` — no network and no ``gh``."""

    def __init__(self, secrets=None, dependencies=None):
        self._secrets = secrets if secrets is not None else []
        self._dependencies = dependencies if dependencies is not None else []

    def secrets(self):
        return self._secrets

    def dependencies(self):
        return self._dependencies


class RefusedProductFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            # a value of the wrong shape refuses the load (an unknown key is only a warning)
            f.write('repo_slug: x/y\ngroom: TODO\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_refused_product_file_is_one_needs_operator_line_and_exit_2(self):
        for argv in (['status', '--product', 'sample'], ['tick', '--product', 'sample']):
            rc, out = self._run(argv)
            self.assertEqual(rc, 2, argv)
            lines = out.strip().splitlines()
            self.assertEqual(len(lines), 1, out)
            self.assertTrue(lines[0].startswith('NEEDS OPERATOR: '), out)
            self.assertIn('asf init --product sample', lines[0])


class CapacityCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('product: sample\nrepo_slug: x/y\ncapacity:\n  sessions: 3\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_capacity_json_parses_and_dispatches(self):
        rc, out = self._run(['capacity', '--product', 'sample', '--json'])
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(data, [{
            'product': 'sample',
            'sessions': {'ceiling': 3, 'inflight': 0, 'free': 3, 'bound_by': 'product'},
            'ci': {'ceiling': None, 'inflight': None, 'free': None, 'bound_by': None},
            'batch': {},
            'deprecated': [],
        }])


class CredentialsCommandTests(unittest.TestCase):
    """``asf credentials check`` (Task 2): the sub-parser's flags and the dispatch that
    resolves ``--product`` into a :class:`asf.env.Product` and ``~/.ASF/config.yaml``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('product: sample\nrepo_slug: x/y\ncredentials: []\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_check_parses_with_each_flag(self):
        for flags in ([], ['--fresh'], ['--json'], ['--quiet']):
            rc, out = self._run(['credentials', 'check', '--product', 'sample'] + flags)
            self.assertEqual(rc, 0, (flags, out))

    def test_product_resolves(self):
        rc, out = self._run(['credentials', 'check', '--product', 'sample', '--json'])
        self.assertEqual(rc, 0, out)
        data = json.loads(out)
        self.assertEqual(data['product'], 'sample')

    def test_an_unknown_sub_command_is_a_parser_error(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(['credentials', 'bogus'])


class RecordCommandUsesTheProductRecordTests(unittest.TestCase):
    """B-0050: ``asf groom`` (and the other record commands) resolve the record from the
    configured product, not from the cwd — a product repo's cwd is never mistaken for one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self._orig_cwd = os.getcwd()
        self._orig_asf_product = os.environ.get('ASF_PRODUCT')
        os.environ.pop('ASF_PRODUCT', None)

        self.product_repo = os.path.join(self.tmp, 'product-repo')
        self.backlog_dir = os.path.join(self.tmp, 'sample-backlog')
        os.makedirs(self.product_repo)
        os.makedirs(os.path.join(self.backlog_dir, 'epics'))
        with open(os.path.join(self.backlog_dir, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': {}}, f)

        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write(
                f"product: sample\nrepo_slug: x/y\nrepo_dir: {self.product_repo}\n"
                f"main: main\nbacklog_dir: {self.backlog_dir}\n"
            )
        os.chdir(self.product_repo)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        env.ASF_HOME = self._orig_home
        if self._orig_asf_product is None:
            os.environ.pop('ASF_PRODUCT', None)
        else:
            os.environ['ASF_PRODUCT'] = self._orig_asf_product
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_groom_from_a_product_repo_grooms_the_product_record_not_the_cwd(self):
        rc, out = self._run(['groom', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"record: {self.backlog_dir}", out)
        self.assertFalse(os.path.isdir(os.path.join(self.product_repo, 'groom')),
                         "groom/ must not land in the product repo cwd")
        today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
        self.assertTrue(os.path.isfile(os.path.join(self.backlog_dir, 'groom', f'{today}.md')),
                        "groom/<date>.md must land in the product's backlog_dir")

    def test_groom_honors_asf_product_env_var_from_a_product_repo(self):
        os.environ['ASF_PRODUCT'] = 'sample'
        rc, out = self._run(['groom'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[0], f"record: {self.backlog_dir}", out)
        self.assertFalse(os.path.isdir(os.path.join(self.product_repo, 'groom')))

    def test_groom_bare_from_the_product_repo_resolves_that_product(self):
        rc, out = self._run(['groom'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[0], f"record: {self.backlog_dir}", out)
        self.assertFalse(os.path.isdir(os.path.join(self.product_repo, 'groom')))

    def test_groom_from_an_unconfigured_product_repo_refuses_rather_than_grooming_nothing(self):
        # a repo no product file claims (a configured product's repo_dir resolves that product)
        self.product_repo = os.path.join(self.tmp, 'unclaimed-repo')
        os.makedirs(self.product_repo)
        os.chdir(self.product_repo)
        rc, out = self._run(['groom'])
        self.assertEqual(rc, 2, out)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1, out)
        self.assertTrue(lines[0].startswith('NEEDS OPERATOR: '), out)
        self.assertFalse(os.path.isdir(os.path.join(self.product_repo, 'groom')),
                         "a product repo with no configured product must never be treated as the record")

    def test_groom_from_a_bare_record_checkout_with_no_product_still_grooms_itself(self):
        os.chdir(self.backlog_dir)
        rc, out = self._run(['groom'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines()[0], f"record: {os.path.realpath(self.backlog_dir)}", out)


class UnparkTests(unittest.TestCase):
    """F-0095 §2.5: ``asf unpark <item>`` appends the undo beside the park and clears it."""

    PARK = {'kind': 'empty', 'text': 'nothing to land', 'at': '2026-09-01T10:00:00Z',
            'parked': True, 'reason': 'ended empty 2 times: the Task is parked'}
    LAUNCH = {'job': 'coder-t-0017', 'item': 'T-0017', 'kind': 'coder', 'pid': 1,
              'started': '2026-09-01T09:00:00Z'}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('product: sample\nrepo_slug: x/y\n')
        self.path = os.path.join(env.state_dir('sample'), 'sessions.jsonl')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger(self, *records):
        with open(self.path, 'w') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def _parked(self):
        ended = dict(self.LAUNCH, ended='2026-09-01T09:30:00Z',
                     end_reason='failed: empty branch: nothing to land')
        self._ledger(ended, {'job': 'coder-t-0017', 'correction': dict(self.PARK),
                             'operator_flagged': 1})

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def _read(self):
        with open(self.path) as f:
            return f.read()

    def _lines(self):
        return [json.loads(x) for x in self._read().splitlines()]

    def test_unpark_clears_the_park_and_appends_one_line(self):
        from asf.workers import lifecycle
        self._parked()
        before = self._read()
        self.assertIn('T-0017', lifecycle.corrections(self.path))
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('T-0017', out)
        self.assertIn('coder-t-0017', out)
        self.assertIn('parked', out)
        self.assertEqual(lifecycle.corrections(self.path), {})
        after = self._read()
        self.assertTrue(after.startswith(before), 'the park line is still in the file')
        self.assertEqual(len(after.splitlines()), len(before.splitlines()) + 1)
        last = self._lines()[-1]
        self.assertEqual(last['job'], 'coder-t-0017')
        self.assertIsNone(last['correction'])
        self.assertTrue(last['unparked'])
        self.assertEqual(last['unpark_why'], '')

    def test_why_is_recorded(self):
        self._parked()
        rc, out = self._run(['unpark', 'T-0017', '--why', 'T-0022 supersedes it',
                             '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._lines()[-1]['unpark_why'], 'T-0022 supersedes it')
        self.assertIn('T-0022 supersedes it', out)

    def test_the_run_keeps_its_end_reason_and_has_no_pending_correction(self):
        from asf.workers import lifecycle
        self._parked()
        self._run(['unpark', 'T-0017', '--product', 'sample'])
        run = lifecycle.latest(self.path)['coder-t-0017']
        self.assertIsNone(lifecycle.pending_correction(run, self.path))
        self.assertEqual(run['end_reason'], 'failed: empty branch: nothing to land')

    def test_an_item_that_is_not_parked_is_refused(self):
        self._ledger(dict(self.LAUNCH, ended='2026-09-01T09:30:00Z', end_reason='finished'))
        before = self._read()
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 1, out)
        self.assertIn('not parked', out)
        self.assertEqual(self._read(), before)

    def test_a_held_but_unparked_correction_is_refused(self):
        held = {'kind': 'empty', 'text': 'x', 'at': '2026-09-01T10:00:00Z'}
        self._ledger(dict(self.LAUNCH, correction=held))
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 1, out)

    def test_an_item_the_ledger_does_not_know_is_refused(self):
        self._parked()
        rc, out = self._run(['unpark', 'T-9999', '--product', 'sample'])
        self.assertEqual(rc, 1, out)
        self.assertIn('not in the ledger', out)

    def test_park_holds_an_item_until_unpark(self):
        from asf.feeder import rows
        from asf.workers import lifecycle
        items = {'T-0017': {'id': 'T-0017', 'type': 'task', 'state': 'New'}}
        self._ledger(dict(self.LAUNCH, ended='2026-09-01T09:30:00Z', end_reason='finished'))
        before = self._read()
        rc, out = self._run(['park', 'T-0017', '--why', 'waits on the vendor', '--product',
                             'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('parked T-0017 at item T-0017 (park job park-t-0017): waits on the vendor',
                      out)
        self.assertEqual(len(self._read().splitlines()), len(before.splitlines()) + 1)
        held, _ids = rows.correction_rows(items, env.load_product('sample'), set(),
                                          lifecycle.corrections(self.path))
        self.assertEqual([(r.item_id, r.waits_on) for r in held], [('T-0017', 'operator')])
        self.assertIn('waits on the vendor', held[0].action)
        rc, out = self._run(['park', 'T-0017', '--why', 'again', '--product', 'sample'])
        self.assertEqual(rc, 1, out)
        self.assertIn('already parked', out)
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(lifecycle.corrections(self.path), {})

    def test_park_on_a_branch_holds_that_branch_not_the_item(self):
        from asf.workers import lifecycle
        self._ledger(dict(self.LAUNCH, branch='cloud/T-0017', ended='2026-09-01T09:30:00Z',
                          end_reason='finished'))
        rc, out = self._run(['park', 'cloud/T-0017', '--why', 'hold the branch',
                             '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('at branch cloud/T-0017', out)
        self.assertNotIn('T-0017', lifecycle.corrections(self.path))
        [park] = lifecycle.parks(self.path)
        self.assertEqual((park['item'], park['scope'], park['branch']),
                         ('T-0017', 'branch', 'cloud/T-0017'))

    def test_park_on_an_untouched_item_holds_it_without_a_launch_or_an_attempt(self):
        from asf.workers import lifecycle
        self._ledger(dict(self.LAUNCH, ended='2026-09-01T09:30:00Z', end_reason='finished'))
        rc, out = self._run(['park', 'T-0099', '--why', 'not yet', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('park-t-0099', out)
        self.assertTrue(lifecycle.corrections(self.path)['T-0099']['parked'])
        self.assertEqual(lifecycle.corrections(self.path)['T-0099']['scope'], 'item')
        self.assertNotIn('T-0099', lifecycle.attempts(self.path))
        self.assertEqual(lifecycle.inflight(self.path), [])
        rc, out = self._run(['unpark', 'T-0099', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertNotIn('T-0099', lifecycle.corrections(self.path))

    def test_park_refuses_a_name_that_is_no_item_and_no_branch(self):
        self._ledger(dict(self.LAUNCH))
        rc, out = self._run(['park', 'no/such', '--why', 'x', '--product', 'sample'])
        self.assertEqual(rc, 1, out)

    def test_the_feeder_stops_holding_the_row_once_unparked(self):
        from asf.feeder import rows
        from asf.workers import lifecycle
        items = {'T-0017': {'id': 'T-0017', 'type': 'task', 'state': 'New'}}
        self._parked()
        held, ids = rows.correction_rows(items, env.load_product('sample'), set(), lifecycle.corrections(self.path))
        self.assertEqual([(r.item_id, r.waits_on) for r in held], [('T-0017', 'operator')])
        self._run(['unpark', 'T-0017', '--product', 'sample'])
        after, ids = rows.correction_rows(items, env.load_product('sample'), set(), lifecycle.corrections(self.path))
        self.assertEqual((after, ids), ([], set()))

    def test_unpark_releases_a_derived_stalemate_park(self):
        self._ledger({'job': 'adjudicate-t-0017', 'item': 'T-0017', 'kind': 'adjudicate',
                      'pid': 1, 'started': '2026-09-01T09:00:00Z',
                      'ended': '2026-09-01T09:30:00Z', 'end_reason': 'finished'})
        before = self._read()
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('T-0017', out)
        self.assertIn('adjudicate-t-0017', out)
        after = self._read()
        self.assertTrue(after.startswith(before), 'the run line is still in the file')
        self.assertEqual(len(after.splitlines()), len(before.splitlines()) + 1)
        last = self._lines()[-1]
        self.assertEqual(last['job'], 'adjudicate-t-0017')
        self.assertTrue(last['unparked'])
        self.assertEqual(last['unpark_why'], '')
        self.assertNotIn('correction', last)

    def test_an_item_with_no_park_and_no_ended_adjudicate_run_is_refused(self):
        self._ledger(dict(self.LAUNCH))  # a live run, nothing ended, nothing parked
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 1, out)
        self.assertIn('is not parked — nothing to undo', out)

    def test_unpark_of_a_branch_does_not_release_a_derived_item_park(self):
        self._ledger({'job': 'adjudicate-t-0017', 'item': 'T-0017', 'kind': 'adjudicate',
                      'branch': 'plan-T-0017', 'pid': 1, 'started': '2026-09-01T09:00:00Z',
                      'ended': '2026-09-01T09:30:00Z', 'end_reason': 'finished'})
        before = self._read()
        rc, out = self._run(['unpark', 'plan-T-0017', '--product', 'sample'])
        self.assertEqual(rc, 1, out)
        self.assertIn('is not parked — nothing to undo', out)
        self.assertEqual(self._read(), before)

    def test_unpark_of_an_item_with_both_parks_releases_both_with_one_stamp(self):
        self._ledger(
            {'job': 'adjudicate-t-0017', 'item': 'T-0017', 'kind': 'adjudicate', 'pid': 1,
             'started': '2026-09-01T09:00:00Z', 'ended': '2026-09-01T09:30:00Z',
             'end_reason': 'finished'},
            dict(self.LAUNCH, ended='2026-09-01T09:45:00Z',
                 end_reason='failed: empty branch: nothing to land'),
            {'job': 'coder-t-0017', 'correction': dict(self.PARK)})
        before = len(self._read().splitlines())
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        after = self._read().splitlines()
        self.assertEqual(len(after), before + 1)
        last = json.loads(after[-1])
        self.assertEqual(last['job'], 'coder-t-0017')
        self.assertIsNone(last['correction'])
        self.assertTrue(last['unparked'])


class ScopedParkTests(unittest.TestCase):
    """``asf park`` at the scope its target names, and ``asf unpark`` its exact undo — a
    product's T-0042, 2026-09-30: ``asf park cloud/plan-T-0042`` parked the item, so its CORRECT
    round on ``cloud/T-0042`` showed PARKED too, and ``asf unpark T-0042`` right after said "not
    parked": a later run of the item on the other branch had "answered" the park."""

    PLAN = {'job': 'adjudicate-t-0042', 'item': 'T-0042', 'kind': 'adjudicate', 'pid': 1,
            'branch': 'cloud/plan-T-0042', 'started': '2026-09-30T07:00:00Z',
            'ended': '2026-09-30T07:10:00Z', 'end_reason': 'finished'}
    CODE = {'job': 'correct-t-0042', 'item': 'T-0042', 'kind': 'correct', 'pid': 2,
            'branch': 'cloud/T-0042', 'started': '2026-09-30T06:00:00Z',
            'ended': '2026-09-30T06:30:00Z', 'end_reason': 'finished'}
    ITEMS = {'T-0042': {'id': 'T-0042', 'type': 'task', 'state': 'New'}}
    setUp, tearDown = UnparkTests.setUp, UnparkTests.tearDown
    _ledger, _run = UnparkTests._ledger, UnparkTests._run

    def _two_branches(self):
        plan_c = {'job': 'adjudicate-t-0042', 'correction': {
            'kind': 'review', 'text': 'answer its C list on cloud/plan-T-0042',
            'at': '2026-09-30T08:18:30Z'}}
        code_c = {'job': 'correct-t-0042', 'correction': {
            'kind': 'review', 'text': 'answer its C list on cloud/T-0042',
            'at': '2026-09-30T07:30:00Z'}}
        self._ledger(dict(self.CODE), dict(self.PLAN), code_c, plan_c)

    def _append(self, *records):
        with open(self.path, 'a') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def _rows(self):
        from asf.feeder import rows
        from asf.workers import lifecycle
        corr, _ids = rows.correction_rows(self.ITEMS, env.load_product('sample'), set(),
                                          lifecycle.corrections(self.path))
        return rows.hold_parks(corr, self.ITEMS, lifecycle.parks(self.path))

    def test_a_branch_park_holds_only_that_branchs_rows(self):
        from asf.feeder import rows
        self._two_branches()
        self.assertEqual([r.branch for r in self._rows()], ['cloud/plan-T-0042'])
        rc, out = self._run(['park', 'cloud/plan-T-0042', '--why', 'looping on #964',
                             '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        got = sorted((r.branch, r.launches, r.action.startswith(rows.PARKED)) for r in self._rows())
        self.assertEqual(got, [('cloud/T-0042', True, False), ('cloud/plan-T-0042', False, True)])
        parked = [r for r in self._rows() if r.action.startswith(rows.PARKED)][0]
        self.assertIn('branch cloud/plan-T-0042', parked.action)
        self.assertIn('looping on #964', parked.action)

    def test_a_job_park_holds_only_that_jobs_rows(self):
        from asf.feeder import rows
        from asf.workers import lifecycle
        self._two_branches()
        rc, out = self._run(['park', 'correct-t-0042', '--why', 'wait', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('at job correct-t-0042', out)
        [park] = lifecycle.parks(self.path)
        self.assertEqual((park['scope'], park['on_job']), ('job', 'correct-t-0042'))
        row = rows.Row(tier=2, kind=rows.FIX_CORRECT, item_id='T-0042', feature_id='',
                       action=rows.LAUNCH, brief_kind='correct', branch='cloud/T-0042',
                       reason='r')
        other = dataclasses.replace(row, brief_kind='review')
        held = rows.hold_parks([row, other], self.ITEMS, lifecycle.parks(self.path))
        self.assertEqual([(r.brief_kind, r.launches) for r in held],
                         [('review', True), ('correct', False)])
        self.assertIn('job correct-t-0042', held[-1].action)

    def test_an_item_park_holds_every_branch(self):
        from asf.feeder import rows
        self._two_branches()
        rc, out = self._run(['park', 'T-0042', '--why', 'vendor', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        got = self._rows()
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].action.startswith(f'{rows.PARKED} operator park on item T-0042'))

    def test_park_round_trips_past_a_later_run_a_reset_and_a_new_correction(self):
        from asf.workers import lifecycle
        for target in ('cloud/plan-T-0042', 'T-0042', 'adjudicate-t-0042'):
            with self.subTest(target=target):
                self._two_branches()
                rc, out = self._run(['park', target, '--why', 'hold', '--product', 'sample'])
                self.assertEqual(rc, 0, out)
                # the other branch's round runs, the parked branch's PR is closed unmerged
                # (the closed-PR reset) and the harvest writes a new correction on the job
                self._append(dict(self.CODE, pid=3, started='2026-09-30T09:23:34Z',
                                  ended='2026-09-30T09:38:16Z'),
                             {'job': 'reset-t-0042', 'item': 'T-0042', 'reset': {
                                 'at': '2026-09-30T09:40:00Z', 'pr': 964,
                                 'branch': 'cloud/plan-T-0042', 'head': 'abc', 'archive': ''}},
                             {'job': 'adjudicate-t-0042', 'correction': {
                                 'kind': 'review', 'text': 'again',
                                 'at': '2026-09-30T09:41:00Z'}})
                self.assertEqual(len(lifecycle.parks(self.path)), 1)
                rc, out = self._run(['unpark', target, '--product', 'sample'])
                self.assertEqual(rc, 0, out)
                self.assertIn('unparked T-0042', out)
                self.assertEqual(lifecycle.parks(self.path), [])
                rc, out = self._run(['unpark', target, '--product', 'sample'])
                if target == 'T-0042':
                    # item scope also releases the item's newest ended adjudicate run — not
                    # actually a stalemate park here, but PD4's accepted harm: the same
                    # relaunch-window reset a factory park's release already performs
                    self.assertEqual(rc, 0, out)
                    self.assertIn('unparked T-0042', out)
                else:
                    self.assertEqual(rc, 1, out)
                    self.assertIn('not parked', out)

    def test_a_branch_park_is_not_refused_by_an_item_that_has_another_branch_parked(self):
        from asf.workers import lifecycle
        self._two_branches()
        self.assertEqual(self._run(['park', 'cloud/plan-T-0042', '--why', 'a',
                                    '--product', 'sample'])[0], 0)
        rc, out = self._run(['park', 'cloud/plan-T-0042', '--why', 'b', '--product', 'sample'])
        self.assertEqual(rc, 1, out)
        self.assertIn('already parked', out)
        self.assertEqual(self._run(['park', 'cloud/T-0042', '--why', 'c',
                                    '--product', 'sample'])[0], 0)
        self.assertEqual(len(lifecycle.parks(self.path)), 2)
        rc, out = self._run(['unpark', 'cloud/T-0042', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertEqual([p['branch'] for p in lifecycle.parks(self.path)], ['cloud/plan-T-0042'])

    def test_unpark_of_an_item_releases_every_park_on_it(self):
        from asf.workers import lifecycle
        cap = {'kind': 'relaunch cap', 'text': 'looped', 'at': '2026-09-30T10:00:00Z',
               'parked': True, 'reason': 'looped'}
        self._ledger(dict(self.CODE), dict(self.PLAN),
                     {'job': 'correct-t-0042', 'correction': dict(cap)},
                     {'job': 'adjudicate-t-0042', 'correction': dict(cap)})
        self._run(['park', 'cloud/plan-T-0042', '--why', 'hold', '--product', 'sample'])
        rc, out = self._run(['unpark', 'T-0042', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count('unparked T-0042'), 3, out)
        self.assertEqual(lifecycle.parks(self.path), [])
        self.assertEqual(lifecycle.corrections(self.path), {})

    def test_occupancy_and_status_name_the_scope(self):
        from asf.views import status
        from asf.workers import lifecycle
        from asf.workers import pool as pool_mod
        self._two_branches()
        self._run(['park', 'cloud/plan-T-0042', '--why', 'hold', '--product', 'sample'])
        occ = lifecycle.occupancy(self.path)
        self.assertEqual([p['branch'] for p in occ['parks']], ['cloud/plan-T-0042'])
        self.assertEqual(occ['corrections']['T-0042']['branch'], 'cloud/T-0042')
        with mock.patch.object(pool_mod, 'sessions_path', lambda _p: self.path):
            cell = status.parked_cell(None)
        self.assertIn('T-0042 [branch cloud/plan-T-0042]', cell)


class ParkedCellTests(unittest.TestCase):
    """F-0239 S-53456: ``parked_cell(product, root=None)`` lists every derived "adjudicated,
    card unchanged" stalemate park (:func:`asf.feeder.rows._capped`) beside the by-hand and
    factory parks it already lists — the NEXT table's own PARKED rows, named in ``asf status``
    too, so the operator does not have to already know `` `asf unpark <item>` `` moves one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        self.product = env.Product('sample', {'conventions': {
            'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}})
        self.path = os.path.join(env.state_dir(self.product), 'sessions.jsonl')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger(self, *records):
        with open(self.path, 'w') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')

    def _root(self, items=None):
        root = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(root, 'index.json'), 'w') as f:
            json.dump({'generated': '', 'items': items or {}}, f)
        return root

    def _over_limit(self, item='T-0338', digest='d'):
        runs = [{'job': f'coder-{item.lower()}-{n}', 'item': item, 'kind': 'coder',
                 'started': f'2026-09-2{n}T09:00:00Z', 'ended': f'2026-09-2{n}T09:30:00Z'}
                for n in range(6, 10)]
        runs.append({'job': f'adjudicate-{item.lower()}', 'item': item, 'kind': 'adjudicate',
                     'started': '2026-10-03T10:00:00Z', 'ended': '2026-10-03T10:10:00Z',
                     'card_digest': digest})
        self._ledger(*runs)

    def test_with_no_root_reads_exactly_as_today(self):
        self._over_limit()
        with mock.patch('asf.briefs.build.card_digest', return_value='d'):
            self.assertIsNone(status.parked_cell(self.product))
        self._ledger({'job': 'coder-t-0338', 'item': 'T-0338', 'kind': 'coder',
                     'correction': {'kind': 'empty', 'text': 'x', 'at': '2026-09-01T10:00:00Z',
                                    'parked': True, 'reason': 'ended empty 2 times'}})
        self.assertEqual(status.parked_cell(self.product), status.parked_cell(self.product, None))

    def test_an_over_limit_item_adjudicated_on_this_same_card_is_listed(self):
        root = self._root({'T-0338': {'id': 'T-0338', 'type': 'task', 'state': 'New'}})
        self._over_limit()
        with mock.patch('asf.briefs.build.card_digest', return_value='d'):
            cell = status.parked_cell(self.product, root)
        self.assertTrue(cell.startswith('1 — '), cell)
        self.assertIn('T-0338 [item]: adjudicated 1 time(s), last 2026-10-03T10:00, on this '
                      'same card', cell)
        self.assertIn('`asf unpark <item|branch|job>` releases one', cell)

    def test_an_item_with_an_uncarried_ruling_is_not_listed(self):
        root = self._root({'T-0338': {'id': 'T-0338', 'type': 'task', 'state': 'New'}})
        self._over_limit()
        card = os.path.join(root, 'tasks', 'T-0338.md')
        os.makedirs(os.path.dirname(card))
        with open(card, 'w', encoding='utf-8') as f:
            f.write('---\nid: T-0338\n---\n\n## Description\nx\n\n## History\n'
                    '- 2026-10-03 11:00 adjudicate (adjudicate-t-0338): C1 upheld; fix a.py:3\n')
        self.product = env.Product('sample', {'backlog_dir': root, 'conventions': {
            'branch_prefixes': {'spec': 'spec', 'plan': 'plan', 'task': 'task'}}})
        with mock.patch('asf.briefs.build.card_digest', return_value='d'):
            cell = status.parked_cell(self.product, root)
        self.assertIsNone(cell)

    def test_a_root_with_no_index_json_adds_nothing_and_raises_nothing(self):
        root = tempfile.mkdtemp(dir=self.tmp)
        self._over_limit()
        with mock.patch('asf.briefs.build.card_digest', return_value='d'):
            self.assertIsNone(status.parked_cell(self.product, root))


class ReadmeParserTests(unittest.TestCase):
    def test_readme_help_parses(self):
        with self.assertRaises(SystemExit) as cm:
            cli.build_parser().parse_args(['readme', '--help'])
        self.assertEqual(cm.exception.code, 0)

    def test_readme_is_a_registered_subcommand(self):
        args = cli.build_parser().parse_args(['readme', '--product', 'sample', '--refresh',
                                              '--check', '--json'])
        self.assertEqual((args.command, args.product, args.refresh, args.check, args.json),
                         ('readme', 'sample', True, True, True))


class SetHelpTests(unittest.TestCase):
    """T-0501: `asf set --help` names the list fields and their add/remove forms, read off the
    real parser rather than a hand-kept string — the same idiom `tests/test_readme.py`'s
    `_install_parser` uses to reach a subparser off `cli.build_parser()`."""

    def _set_parser(self):
        parser = cli.build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action.choices['set']
        raise AssertionError('no set subcommand')

    def test_set_help_exits_0(self):
        with self.assertRaises(SystemExit) as cm:
            cli.build_parser().parse_args(['set', '--help'])
        self.assertEqual(cm.exception.code, 0)

    def test_set_help_names_the_list_fields_and_their_forms(self):
        help_text = self._set_parser().format_help()
        for needle in ('writes', 'after', '+=', '-='):
            self.assertIn(needle, help_text, help_text)


class LineBufferedOutputTests(unittest.TestCase):
    """A scheduled tick writes to a log file: every line reaches it as it is printed."""

    def test_main_flushes_each_line_to_a_file_stream(self):
        import sys
        from unittest import mock
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding='utf-8')  # a file, not a tty: block-buffered
        err = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')

        def fake_main(argv):
            print('first line')
            self.assertEqual(raw.getvalue(), b'first line\n')  # there while the command runs
            return 0
        with mock.patch.object(sys, 'stdout', stream), mock.patch.object(sys, 'stderr', err), \
                mock.patch.object(cli, '_main', fake_main):
            self.assertEqual(cli.main([]), 0)
        self.assertTrue(stream.line_buffering)
        self.assertTrue(err.line_buffering)

    def test_a_stream_that_cannot_reconfigure_is_left_alone(self):
        cli.line_buffered(io.StringIO())  # no error


class SecurityParserTests(unittest.TestCase):
    """``asf security alerts --check`` (T-0365): the `--check` contract ``rules.py`` reads
    (P10) — one line per place on stdout, exit 1 when there is one, 0 when there is none.
    No network and no ``gh``: :class:`FakeHost` stands in for ``alerts.GitHubHost``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('product: sample\nrepo_slug: x/y\n')

    def tearDown(self):
        env.ASF_HOME = self._orig_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv, host):
        out = io.StringIO()
        with mock.patch('asf.security.alerts.GitHubHost', lambda product: host):
            with contextlib.redirect_stdout(out):
                rc = cli.main(argv)
        return rc, out.getvalue()

    def test_alerts_with_no_check_prints_the_lines_and_exits_0(self):
        host = FakeHost(secrets=[{'number': 1, 'kind': 'generic', 'created_at': 't'}])
        rc, out = self._run(['security', 'alerts', '--product', 'sample'], host)
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            out.splitlines(),
            ['R-0009 secret alert open generic x/y#1 since t sev=S1 sig=secret-x/y-1'])

    def test_check_exits_1_with_lines_naming_the_place(self):
        host = FakeHost(secrets=[{'number': 1, 'kind': 'generic', 'created_at': 't'}])
        rc, out = self._run(['security', 'alerts', '--check', '--product', 'sample'], host)
        self.assertEqual(rc, 1, out)
        self.assertEqual(
            out.splitlines(),
            ['R-0009 secret alert open generic x/y#1 since t sev=S1 sig=secret-x/y-1'])

    def test_check_exits_0_clean_over_an_empty_feed(self):
        rc, out = self._run(['security', 'alerts', '--check', '--product', 'sample'], FakeHost())
        self.assertEqual(rc, 0, out)
        self.assertEqual(out, '')

    def test_nothing_but_the_lines_reaches_stdout(self):
        host = FakeHost(secrets=[{'number': 1, 'kind': 'generic', 'created_at': 't'}])
        rc, out = self._run(['security', 'alerts', '--check', '--product', 'sample'], host)
        self.assertEqual(len(out.splitlines()), 1, out)

    def test_an_unknown_subcommand_is_a_parser_error(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(['security', 'bogus'])
