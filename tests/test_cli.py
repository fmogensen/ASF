"""asf.cli.main — a refused product file is one NEEDS OPERATOR line and exit 2, never a traceback
(B-0045); a record command uses the product's record, not the cwd, even when the cwd is a
product repo (B-0050)."""
import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest

from asf import cli, env


class RefusedProductFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cli_test_')
        self._orig_home = env.ASF_HOME
        env.ASF_HOME = self.tmp
        os.makedirs(os.path.join(self.tmp, 'products'))
        with open(env.product_path('sample'), 'w') as f:
            f.write('repo_slug: x/y\nnot_a_declared_key: 1\n')

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


class UnparkHome(unittest.TestCase):
    """F-0095 §2.5: shared harness for ``asf unpark <item>`` — the park and its undo."""

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

    def _park(self, at, reason, parked=True):
        """A correction dict at ``at``: parked (the default) with ``reason`` as its release
        text, or a plain hold naming ``reason`` as its ``text`` instead."""
        if parked:
            return {'kind': 'empty', 'text': 'nothing to land', 'at': at, 'parked': True,
                    'reason': reason}
        return {'kind': 'empty', 'text': reason, 'at': at}

    def _run_row(self, job, kind, started, ended):
        """A run of ``T-0017`` opened at ``started``, closed at ``ended`` when given — a second
        run alongside the base ``LAUNCH``."""
        row = {'job': job, 'item': 'T-0017', 'kind': kind, 'pid': 1, 'started': started}
        if ended:
            row['ended'] = ended
        return row


class UnparkTests(UnparkHome):
    """F-0095 §2.5: ``asf unpark <item>`` appends the undo beside the park and clears it."""

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


class UnparkReleasesEveryPark(UnparkHome):
    """F-0218: one call releases every park ``T-0017`` holds, and names what still holds it."""

    def _two_parked(self):
        self._ledger(
            self._run_row('coder-t-0017', 'coder', '2026-09-01T09:00:00Z',
                          '2026-09-01T09:30:00Z'),
            {'job': 'coder-t-0017',
             'correction': self._park('2026-09-01T10:00:00Z',
                                      'ended empty 2 times: the Task is parked')},
            self._run_row('reviewer-t-0017', 'reviewer', '2026-09-01T09:05:00Z',
                          '2026-09-01T09:35:00Z'),
            {'job': 'reviewer-t-0017',
             'correction': self._park('2026-09-01T10:05:00Z',
                                      'ended empty 2 times: the Task is parked')},
        )

    def test_two_parks_on_one_item_go_in_one_call(self):
        from asf.workers import lifecycle
        self._two_parked()
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('coder-t-0017', out)
        self.assertIn('reviewer-t-0017', out)
        self.assertIn('released 2 parks', out)
        self.assertEqual(lifecycle.corrections(self.path), {})
        rc2, out2 = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc2, 1, out2)
        self.assertIn('not parked', out2)

    def test_the_undo_is_one_line_per_park_and_every_park_line_stays(self):
        self._two_parked()
        before = self._read()
        rc, out = self._run(['unpark', 'T-0017', '--why', 'already on the trunk',
                             '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        after = self._read()
        self.assertTrue(after.startswith(before), 'every park line stays in the file')
        appended = self._lines()[len(before.splitlines()):]
        self.assertEqual(len(appended), 2)
        for line in appended:
            self.assertIsNone(line['correction'])
        self.assertEqual(len({line['unparked'] for line in appended}), 1)
        self.assertEqual({line['unpark_why'] for line in appended}, {'already on the trunk'})

    def test_a_park_under_a_newer_plain_hold_is_released_and_the_hold_is_named(self):
        from asf.workers import lifecycle
        self._ledger(
            self._run_row('coder-t-0017', 'coder', '2026-09-01T09:00:00Z',
                          '2026-09-01T09:30:00Z'),
            {'job': 'coder-t-0017',
             'correction': self._park('2026-09-01T10:00:00Z',
                                      'ended empty 2 times: the Task is parked')},
            self._run_row('reviewer-t-0017', 'reviewer', '2026-09-01T09:05:00Z',
                          '2026-09-01T09:35:00Z'),
            {'job': 'reviewer-t-0017',
             'correction': self._park('2026-09-01T10:05:00Z',
                                      "the spec's §2 table is not in the diff", parked=False)},
        )
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('released 1 park', out)
        self.assertIn('1 correction still holds it', out)
        self.assertIn('still held', out)
        run = lifecycle.latest(self.path)['reviewer-t-0017']
        self.assertTrue(lifecycle.pending_correction(run, self.path))
        self.assertIn('T-0017', lifecycle.corrections(self.path))

    def test_a_plain_correction_is_never_cleared(self):
        from asf.workers import lifecycle
        self._ledger(
            self._run_row('coder-t-0017', 'coder', '2026-09-01T09:00:00Z',
                          '2026-09-01T09:30:00Z'),
            {'job': 'coder-t-0017',
             'correction': self._park('2026-09-01T10:00:00Z',
                                      'ended empty 2 times: the Task is parked')},
            self._run_row('reviewer-t-0017', 'reviewer', '2026-09-01T09:05:00Z',
                          '2026-09-01T09:35:00Z'),
            {'job': 'reviewer-t-0017',
             'correction': self._park('2026-09-01T10:05:00Z',
                                      "the spec's §2 table is not in the diff", parked=False)},
        )
        self._run(['unpark', 'T-0017', '--product', 'sample'])
        corrections = lifecycle.corrections(self.path)
        self.assertIn('T-0017', corrections)
        self.assertEqual(corrections['T-0017'].get('text'), "the spec's §2 table is not in the diff")

    def test_three_parks_need_one_call(self):
        from asf.workers import lifecycle
        self._ledger(
            self._run_row('coder-t-0017', 'coder', '2026-09-01T09:00:00Z',
                          '2026-09-01T09:30:00Z'),
            {'job': 'coder-t-0017',
             'correction': self._park('2026-09-01T10:00:00Z',
                                      'ended empty 2 times: the Task is parked')},
            self._run_row('reviewer-t-0017', 'reviewer', '2026-09-01T09:05:00Z',
                          '2026-09-01T09:35:00Z'),
            {'job': 'reviewer-t-0017',
             'correction': self._park('2026-09-01T10:05:00Z',
                                      'ended empty 2 times: the Task is parked')},
            self._run_row('harvest-t-0017', 'harvest', '2026-09-01T09:10:00Z',
                          '2026-09-01T09:40:00Z'),
            {'job': 'harvest-t-0017',
             'correction': self._park('2026-09-01T10:10:00Z',
                                      'ended empty 2 times: the Task is parked')},
        )
        before = self._read()
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('released 3 parks', out)
        self.assertEqual(lifecycle.corrections(self.path), {})
        appended = self._lines()[len(before.splitlines()):]
        self.assertEqual(len(appended), 3)

    def test_one_park_prints_its_count_and_still_exits_zero(self):
        self._parked()
        rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('released 1 park on T-0017', out)
        self.assertNotIn('parks', out)
        self.assertNotIn('still', out)

    def test_parked_jobs_is_oldest_first_and_one_entry_per_job(self):
        from asf.workers import unpark
        self._two_parked()
        parks = unpark.parked_jobs(self.path, 'T-0017')
        self.assertEqual([job for job, _ in parks], ['coder-t-0017', 'reviewer-t-0017'])

        self._ledger(
            self._run_row('coder-t-0017', 'coder', '2026-09-01T09:00:00Z',
                          '2026-09-01T09:30:00Z'),
            {'job': 'coder-t-0017',
             'correction': self._park('2026-09-01T09:31:00Z',
                                      'ended empty 2 times: the Task is parked')},
            self._run_row('coder-t-0017', 'coder', '2026-09-01T11:00:00Z',
                          '2026-09-01T11:30:00Z'),
            {'job': 'coder-t-0017',
             'correction': self._park('2026-09-01T11:31:00Z',
                                      'ended empty 2 times: the Task is parked')},
        )
        parks = unpark.parked_jobs(self.path, 'T-0017')
        self.assertEqual(len(parks), 1)
        self.assertEqual(parks[0][0], 'coder-t-0017')
        self.assertEqual(parks[0][1].get('at'), '2026-09-01T11:31:00Z')

    def test_still_holding_reads_the_file_again_after_the_appends(self):
        from unittest import mock
        from asf.workers import unpark
        self._parked()
        real_update_session = unpark.pool_mod.update_session
        calls = []

        def wrapper(*args, **kwargs):
            result = real_update_session(*args, **kwargs)
            if not calls:
                with open(self.path, 'a') as f:
                    f.write(json.dumps(self._run_row(
                        'harvest-t-0017', 'harvest', '2026-09-01T08:00:00Z',
                        '2026-09-01T08:30:00Z')) + '\n')
                    f.write(json.dumps({'job': 'harvest-t-0017',
                        'correction': self._park('2026-09-01T10:30:00Z',
                                                 'commits do not name T-0017')}) + '\n')
            calls.append(1)
            return result

        with mock.patch.object(unpark.pool_mod, 'update_session', side_effect=wrapper):
            rc, out = self._run(['unpark', 'T-0017', '--product', 'sample'])
        self.assertEqual(rc, 0, out)
        self.assertIn('released 1 park on T-0017 — 1 correction still holds it', out)
        self.assertIn('still parked (harvest-t-0017):', out)
        self.assertIn('— run `asf unpark T-0017` again', out)

    def test_the_feeder_row_goes_after_one_call_when_two_parks_held_it(self):
        from asf.feeder import rows
        from asf.workers import lifecycle
        items = {'T-0017': {'id': 'T-0017', 'type': 'task', 'state': 'New'}}
        self._two_parked()
        held, ids = rows.correction_rows(items, env.load_product('sample'), set(),
                                         lifecycle.corrections(self.path))
        self.assertEqual([(r.item_id, r.waits_on) for r in held], [('T-0017', 'operator')])
        self._run(['unpark', 'T-0017', '--product', 'sample'])
        after, ids = rows.correction_rows(items, env.load_product('sample'), set(),
                                          lifecycle.corrections(self.path))
        self.assertEqual((after, ids), ([], set()))


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
