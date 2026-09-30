"""The flaky-e2e pass of ``asf file-bugs``: a test the CI retry passed is one counted Bug."""
import argparse
import contextlib
import datetime
import io
import os
import shutil
import tempfile
import unittest

from asf.conventions import Conventions
from asf.record import frontmatter
from asf.tick import file_bugs, flaky
from tests.test_file_bugs import make_repo, write_item

ESC = '\x1b'

TWO_BLOCKS = f"""\
2026-09-26T10:00:01.0000000Z Running 12 tests using 2 workers
2026-09-26T10:00:05.0000000Z {ESC}[33m  2 flaky{ESC}[39m
2026-09-26T10:00:05.0000000Z {ESC}[33m    [chromium] › e2e/login.spec.ts:12:5 › login › remembers the user{ESC}[39m
2026-09-26T10:00:05.0000000Z     [firefox] › e2e/cart.spec.ts:40:3 › adds an item
2026-09-26T10:00:05.0000000Z {ESC}[32m  10 passed{ESC}[39m{ESC}[2m (30.1s){ESC}[22m
2026-09-26T10:01:00.0000000Z Running 3 tests using 1 worker
2026-09-26T10:01:05.0000000Z   1 flaky
2026-09-26T10:01:05.0000000Z     [chromium] › e2e/search.spec.ts:7:1 › finds by name
2026-09-26T10:01:05.0000000Z
2026-09-26T10:01:05.0000000Z     [chromium] › e2e/not-in-a-block.spec.ts:1:1 › ignored
2026-09-26T10:01:05.0000000Z   2 passed (4.0s)
"""

NO_FLAKY = """\
Running 12 tests using 2 workers
  12 passed (30.1s)
    [chromium] › e2e/login.spec.ts:12:5 › listed but not flaky
"""

GH_RUN_VIEW = ("e2e\tRun tests\t2026-09-26T10:00:05.0000000Z   1 flaky\n"
               "e2e\tRun tests\t2026-09-26T10:00:05.0000000Z     [webkit] › a/b.spec.ts:3:9 › t\n")


class ParseTests(unittest.TestCase):
    def test_several_blocks_ansi_and_timestamps(self):
        got = flaky.parse_flaky(TWO_BLOCKS)
        self.assertEqual([(t['project'], t['file'], t['line'], t['title']) for t in got], [
            ('chromium', 'e2e/login.spec.ts', 12, 'login › remembers the user'),
            ('firefox', 'e2e/cart.spec.ts', 40, 'adds an item'),
            ('chromium', 'e2e/search.spec.ts', 7, 'finds by name'),
        ])

    def test_no_flaky_log(self):
        self.assertEqual(flaky.parse_flaky(NO_FLAKY), [])
        self.assertEqual(flaky.parse_flaky(''), [])

    def test_gh_run_view_prefix(self):
        got = flaky.parse_flaky(GH_RUN_VIEW)
        self.assertEqual([(t['project'], t['file'], t['line'], t['title']) for t in got],
                         [('webkit', 'a/b.spec.ts', 3, 't')])

    def test_key_and_title(self):
        t = flaky.parse_flaky(TWO_BLOCKS)[0]
        self.assertEqual(flaky.test_key(t), 'e2e/login.spec.ts:12 › login › remembers the user')
        self.assertEqual(flaky.bug_title(t),
                         'Flaky e2e: login › remembers the user (e2e/login.spec.ts:12)')


class _Proc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class _CapturingRun:
    """A ``subprocess.run`` double: canned stdout keyed by a substring of the joined argv, every
    call recorded so a test can count how many times a given endpoint was asked."""

    def __init__(self, replies):
        self.calls = []
        self.replies = replies

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        joined = ' '.join(argv)
        for needle, stdout in self.replies:
            if needle in joined:
                return _Proc(stdout)
        return _Proc('')

    def count(self, needle):
        return sum(1 for argv in self.calls if needle in ' '.join(argv))


WORKFLOWS = ('{"id": 111, "name": "CI", "path": ".github/workflows/ci.yml"}\n'
             '{"id": 222, "name": "Deploy", "path": ".github/workflows/deploy.yml"}\n')

BY_ID_RUN_ROW = ('{"id": 9, "name": "Continuous Integration", "head_branch": "main", '
                 '"created_at": "2026-09-20T10:00:00Z", "updated_at": "2026-09-20T10:30:00Z", '
                 '"html_url": "https://ci/9"}\n')

OLD_FILTER_RUN_ROWS = ('{"id": 5, "name": "legacy-ci", "head_branch": "main", '
                       '"created_at": "2026-09-20T09:00:00Z", "updated_at": '
                       '"2026-09-20T09:05:00Z", "html_url": "https://ci/5"}\n'
                       '{"id": 6, "name": "Deploy", "head_branch": "main", '
                       '"created_at": "2026-09-20T09:00:00Z", "updated_at": '
                       '"2026-09-20T09:05:00Z", "html_url": "https://ci/6"}\n')


class WorkflowSelector(unittest.TestCase):
    """P2's case: ``conventions.ci_workflow`` may hold a workflow's id, its display name or its
    file name, and the pass must resolve whichever it is asked for — through one request, however
    many times (D1, D3)."""

    def _instance(self, replies):
        run = _CapturingRun(replies)
        product = argparse.Namespace(repo_slug='o/r')
        return flaky.GitHubRuns(product, run=run), run

    def test_resolves_name_file_name_or_id_to_the_same_id(self):
        for selector in ('CI', 'ci.yml', 111, '111'):
            g, run = self._instance([('actions/workflows?', WORKFLOWS)])
            self.assertEqual(g.workflow_id(selector), 111)
            self.assertEqual(g.workflow_id(selector), 111)   # asked again: still cached
            self.assertEqual(run.count('actions/workflows?'), 1)

    def test_runs_by_id_applies_no_display_name_filter(self):
        g, run = self._instance([('actions/workflows?', WORKFLOWS),
                                 ('actions/workflows/111/runs', BY_ID_RUN_ROW)])
        rows = g.runs('CI', '2026-09-01T00:00:00Z')
        self.assertEqual([r['id'] for r in rows], [9])
        self.assertNotEqual(rows[0]['name'], 'CI')   # the row today's filter discards (P2)
        self.assertEqual(run.count('actions/workflows/111/runs'), 1)

    def test_unresolvable_selector_prints_a_line_and_falls_back(self):
        g, run = self._instance([('actions/workflows?', WORKFLOWS),
                                 ('repos/o/r/actions/runs?', OLD_FILTER_RUN_ROWS)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows = g.runs('legacy-ci', '2026-09-01T00:00:00Z')
        self.assertIn('legacy-ci', buf.getvalue())
        self.assertIn('CI', buf.getvalue())
        self.assertIn('Deploy', buf.getvalue())
        self.assertEqual([r['id'] for r in rows], [5])   # today's filter: name == workflow


class PassWindow(unittest.TestCase):
    """D4, D5, D6: the window ``collect`` lists from moves forward only over runs it actually
    read, and is always computed from ``created_at`` rather than ``ts``."""

    def setUp(self):
        self.now = datetime.datetime(2026, 9, 26, 12, 0, 0, tzinfo=datetime.timezone.utc)

    def iso(self, dt):
        return dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    def floor(self):
        return self.now - datetime.timedelta(days=flaky.LOOKBACK_DAYS)

    def test_no_window_from_is_the_full_lookback(self):
        self.assertEqual(flaky.window_since({}, self.now), self.iso(self.floor()))

    def test_window_from_inside_the_lookback_is_used_as_is(self):
        wf = self.iso(self.now - datetime.timedelta(days=2))
        self.assertEqual(flaky.window_since({'window_from': wf}, self.now), wf)

    def test_window_from_older_than_the_lookback_is_floored(self):
        wf = self.iso(self.now - datetime.timedelta(days=30))
        self.assertEqual(flaky.window_since({'window_from': wf}, self.now), self.iso(self.floor()))

    def test_next_window_after_reading_everything_is_the_overlap(self):
        expect = self.iso(self.now - datetime.timedelta(hours=flaky.SINCE_OVERLAP_HOURS))
        self.assertEqual(flaky.next_window([], self.now), expect)

    def test_next_window_with_unread_runs_is_before_the_oldest_created_not_ts(self):
        # ts (updated_at) is newer than created_at for both; next_window must use created_at.
        unread = [{'id': 1, 'created': self.iso(self.now - datetime.timedelta(hours=3)),
                  'ts': self.iso(self.now - datetime.timedelta(hours=1))},
                 {'id': 2, 'created': self.iso(self.now - datetime.timedelta(hours=5)),
                  'ts': self.iso(self.now - datetime.timedelta(minutes=30))}]
        expect = self.iso(self.now - datetime.timedelta(hours=5) - datetime.timedelta(seconds=1))
        self.assertEqual(flaky.next_window(unread, self.now), expect)

    def test_next_window_is_floored_at_the_lookback(self):
        unread = [{'id': 1, 'created': self.iso(self.now - datetime.timedelta(days=30)),
                  'ts': self.iso(self.now - datetime.timedelta(days=30))}]
        self.assertEqual(flaky.next_window(unread, self.now), self.iso(self.floor()))

    def test_next_window_falls_back_to_ts_when_created_is_absent(self):
        # a fake source that predates this card (FakeRuns): no KeyError, just less precision.
        unread = [{'id': 1, 'ts': self.iso(self.now - datetime.timedelta(hours=6))}]
        expect = self.iso(self.now - datetime.timedelta(hours=6) - datetime.timedelta(seconds=1))
        self.assertEqual(flaky.next_window(unread, self.now), expect)


class FakeRuns:
    slug = 'o/r'

    def __init__(self):
        self.runs_ = []
        self.logs = {}

    def add(self, run_id, branch, ts, log, runner='r1', labels=('self-hosted', 'class-e2e')):
        self.runs_.append({'id': run_id, 'name': 'ci', 'branch': branch, 'ts': ts,
                           'url': f'https://ci/{run_id}'})
        self.logs[run_id] = ([{'id': run_id * 10, 'runner_name': runner,
                               'labels': list(labels)}], log)

    def runs(self, workflow, since):
        return [r for r in self.runs_ if workflow == 'ci']

    def jobs(self, run_id):
        return self.logs[run_id][0]

    def log(self, job_id):
        return self.logs[job_id // 10][1]


class SinceFilteredRuns:
    """A fake that, unlike :class:`FakeRuns`, actually honours ``since`` against ``created`` —
    the real ``GitHubRuns.runs`` does the filtering server-side, and a test of the window itself
    needs a source that behaves the same way."""
    slug = 'o/r'

    def __init__(self):
        self.runs_ = []
        self.job_fail_ids = set()

    def add(self, run_id, branch, created):
        self.runs_.append({'id': run_id, 'name': 'ci', 'branch': branch, 'created': created,
                           'ts': created, 'url': f'https://ci/{run_id}'})

    def runs(self, workflow, since):
        return [r for r in self.runs_ if workflow == 'ci' and r['created'] >= since]

    def jobs(self, run_id):
        if run_id in self.job_fail_ids:
            return None
        return [{'id': run_id * 10, 'runner_name': 'r', 'labels': []}]

    def log(self, job_id):
        return ''


class WindowIntegration(unittest.TestCase):
    """D4's correctness claim end to end: the window moves forward only over runs the pass
    actually read, so a run a failed ``jobs()`` listing left unread is not stepped over."""

    def setUp(self):
        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.conv = Conventions(ci_workflow='ci')

    def ts(self, hours_ago):
        return (self.now - datetime.timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def test_a_run_two_failed_passes_leave_unread_is_read_by_the_third(self):
        src = SinceFilteredRuns()
        src.add(1, 'main', self.ts(72))
        src.job_fail_ids = {1}
        state = flaky.read_state(os.path.join(tempfile.mkdtemp(prefix='flaky_state_'), 'x.json'))

        for _ in range(2):
            touched = flaky.collect(state, src, 'ci', self.conv, self.now, out=lambda *_: None)
            self.assertEqual(touched, set())
            self.assertNotIn('1', state['seen'])
            # the window must not have moved past the run it just failed to read
            self.assertLessEqual(state['window_from'], src.runs_[0]['created'])

        src.job_fail_ids = set()
        touched = flaky.collect(state, src, 'ci', self.conv, self.now, out=lambda *_: None)
        self.assertIn('1', state['seen'])

    def test_truncated_listing_prints_and_still_reads_its_batch(self):
        src = SinceFilteredRuns()
        for i in range(flaky.TRUNCATED_ROWS):
            src.add(i, 'main', self.ts(1))
        state = flaky.read_state(os.path.join(tempfile.mkdtemp(prefix='flaky_state_'), 'x.json'))
        lines = []
        flaky.collect(state, src, 'ci', self.conv, self.now, out=lines.append)
        msg = next(m for m in lines if 'truncated' in m)
        self.assertIn(str(flaky.TRUNCATED_ROWS), msg)
        self.assertEqual(len(state['seen']), flaky.MAX_RUNS_PER_PASS)   # still reads its batch


class PassTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.state = tempfile.mkdtemp(prefix='flaky_state_')
        write_item(self.root, 'E-0009', 'epic', 'Factory', typed_lines=['decided: true'])
        self.src = FakeRuns()
        self.now = datetime.datetime.now(datetime.timezone.utc)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def ts(self, hours_ago):
        return (self.now - datetime.timedelta(hours=hours_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')

    def file_bugs(self):
        args = argparse.Namespace(default_bug_epic='E-0009', file_bug_level='auto',
                                  conventions=Conventions(ci_workflow='ci'),
                                  state_dir=self.state, flaky_source=self.src,
                                  flaky_state=os.path.join(self.state, 'flaky.json'))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(file_bugs.cmd_file_bugs(args, self.root), 0)
        return buf.getvalue()

    def bugs(self):
        out = []
        for n in sorted(os.listdir(os.path.join(self.root, 'bugs'))):
            with open(os.path.join(self.root, 'bugs', n), encoding='utf-8') as f:
                meta, body = frontmatter.parse(f.read(), path=f'bugs/{n}')
            if str(meta.get('title', '')).startswith('Flaky e2e: '):
                out.append((meta, body))
        return out

    def test_one_bug_per_test_counted_not_refiled(self):
        self.src.add(1, 'feature/x', self.ts(30), TWO_BLOCKS)
        self.file_bugs()
        bugs = self.bugs()
        self.assertEqual(len(bugs), 3)
        meta, body = bugs[0]
        self.assertEqual(meta['count'], 1)
        self.assertEqual(meta['severity'], 'S3')
        self.assertEqual(meta['parent'], 'E-0009')
        self.assertIn('Count: flaky in 1 run(s) (0 on trunk)', body)
        self.assertIn('Runners: r1 (e2e)', body)
        self.assertIn('[1](https://ci/1)', body)

        self.file_bugs()                       # nothing new: nothing changes, nothing re-filed
        self.assertEqual(len(self.bugs()), 3)
        self.assertEqual(self.bugs()[0][0]['count'], 1)

        self.src.add(2, 'feature/y', self.ts(2), GH_RUN_VIEW.replace('a/b.spec.ts:3:9 › t',
                                                                     'e2e/cart.spec.ts:40:3 › adds an item'),
                     runner='r2', labels=('self-hosted', 'big'))
        self.file_bugs()
        bugs = self.bugs()
        self.assertEqual(len(bugs), 3)
        cart = [b for b in bugs if 'adds an item' in b[0]['title']][0]
        self.assertEqual(cart[0]['count'], 2)
        self.assertEqual(cart[0]['links']['runs'], [1, 2])
        self.assertIn('Count: flaky in 2 run(s)', cart[1])
        self.assertIn('Runners: r1 (e2e), r2 (big)', cart[1])
        self.assertEqual(cart[1].count('Count: '), 1)

    def test_three_runs_in_a_week_is_s1(self):
        for i, h in enumerate((100, 50, 1), start=1):
            self.src.add(i, f'feature/{i}', self.ts(h), GH_RUN_VIEW)
        self.file_bugs()
        (meta, _body), = self.bugs()
        self.assertEqual(meta['severity'], 'S1')
        self.assertEqual(meta['count'], 3)
        self.assertTrue(meta['decided'])

    def test_trunk_twice_is_s1_on_the_update(self):
        self.src.add(1, 'main', self.ts(5), GH_RUN_VIEW)
        self.file_bugs()
        self.assertEqual(self.bugs()[0][0]['severity'], 'S3')
        self.src.add(2, 'main', self.ts(1), GH_RUN_VIEW)
        self.file_bugs()
        (meta, _body), = self.bugs()
        self.assertEqual(meta['severity'], 'S1')
        self.assertEqual(meta['count'], 2)

    def test_only_the_ci_workflow_is_read(self):
        args_conv = Conventions()   # no ci_workflow: no pass
        self.src.add(1, 'main', self.ts(5), GH_RUN_VIEW)
        state = flaky.read_state(os.path.join(self.state, 'x.json'))
        flaky.collect(state, self.src, args_conv.get('ci_workflow') or 'other', args_conv,
                      self.now, out=lambda *_: None)
        self.assertEqual(state['tests'], {})


if __name__ == '__main__':
    unittest.main()
