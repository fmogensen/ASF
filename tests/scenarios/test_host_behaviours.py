"""The fake host's check behaviours against their recordings, and the readers that do read checks
(the harness is ``tests/scenarios/__init__.py``).

No close path reads a PR's checks: :class:`ClosePaths` runs the close-path table's rows of each
check behaviour (:data:`scenarios.CHECK_ROWS`) — the Task's open work unmerged on every path,
whatever the host says of its checks. Then each behaviour is held to the real
``gh`` answer it stands for (``tests/fixtures/gh/<behaviour>``): the same keys, the same
verdict-bearing values. Then the lane's check reader — :func:`asf.harvest.lane.pr_checks` with
the trunk's required check, :func:`asf.harvest.lane.passed_names` (what the gate merges on) and
:func:`asf.stale_ref.stale` — is asked about PR #1 under it: a skipped required check is green
only on an attested head (S-M23), a path-filtered one is missing, a run's success never hides a
failed job, and a red from a stale merge ref is no verdict.
"""
import datetime
import json
import re
import unittest

try:  # `unittest discover -s tests` puts tests/ on the path; `-m tests.scenarios…` does not
    import scenarios as S
except ImportError:  # pragma: no cover - import shape only
    from tests import scenarios as S


def _json(f, *argv):
    p = f.gh(*argv)
    if p.returncode != 0:
        raise AssertionError(f'gh {" ".join(argv)}: {p.stderr}')
    return json.loads(p.stdout)


def _fixture(behaviour, call):
    rc, out, _err = S.contracts.load(behaviour, call)
    assert rc == 0, (behaviour, call)
    return json.loads(out) if isinstance(out, str) else out


def _check_run(f, name):
    runs = _json(f, 'api', f'repos/{S.SLUG}/commits/{S.WORLD.hand}/check-runs?per_page=100')
    return [r for r in runs['check_runs'] if r['name'] == name]


def _epoch(iso):
    return datetime.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()


class ClosePaths(unittest.TestCase):
    """One generated test per row of :data:`scenarios.CHECK_ROWS`."""

    def check(self, path, behaviour, expected, gap, edit):
        _scenario, o = S.run(path, behaviour, edit)
        said = f'{path} × {behaviour}: {o}\n' + '\n'.join(o.lines)
        self.assertIsNone(gap)
        self.assertTrue(S.holds(expected, o), f'expected {expected}\n{said}')


def _make(row):
    def test(self):
        self.check(*row)
    test.__doc__ = f'{row[0]} × {row[1]} → {row[2]}'
    return test


for _row in S.CHECK_ROWS:
    _name = 'test_' + '__'.join(re.sub(r'[^a-z0-9]+', '_', t.lower()).strip('_')
                                for t in (_row[0], _row[1]))
    assert not hasattr(ClosePaths, _name), _name
    setattr(ClosePaths, _name, _make(_row))


class TheFakeHost(unittest.TestCase):
    """Each check behaviour of the fake answers in the recorded shape."""

    def fork(self, behaviour):
        f = S.WORLD.fork()
        S.BEHAVIOURS[behaviour](f)
        return f

    def test_a_skipped_job_reads_as_the_recorded_check_run(self):
        f = self.fork('skip-job')
        rec = _fixture('skipped-required-job/not-attested', 'check-runs')['check_runs'][0]
        got = _check_run(f, S.CHECK)
        self.assertEqual(len(got), 1, got)
        self.assertEqual(sorted(set(rec) - set(got[0])), [])
        self.assertEqual((got[0]['status'], got[0]['conclusion']), ('completed', 'skipped'))

    def test_the_attested_pair_reads_as_the_recorded_statuses(self):
        for behaviour, recorded, want in (('skip-job', 'not-attested', None),
                                          ('skip-job-attested', 'attested', 'success')):
            with self.subTest(behaviour):
                from asf import attestation
                f = self.fork(behaviour)
                rec = _fixture(f'skipped-required-job/{recorded}', 'status')
                got = _json(f, 'api', f'repos/{S.SLUG}/commits/{S.WORLD.hand}/status?per_page=100')
                self.assertEqual(sorted(set(rec) - set(got)), [])
                self.assertEqual(attestation.state_of(rec), want)
                self.assertEqual(attestation.state_of(got), want)

    def test_a_hidden_job_failure_reads_as_the_recorded_run(self):
        f = self.fork('hide-job-failure')
        rec = _fixture('run-success-hiding-job', 'run-view')
        run = _json(f, 'api', f'repos/{S.SLUG}/actions/runs?head_sha={S.WORLD.hand}')
        self.assertEqual(len(run['workflow_runs']), 1)
        got = _json(f, 'run', 'view', str(run['workflow_runs'][0]['id']), '-R', S.SLUG,
                    '--json', 'status,conclusion,url,jobs')
        self.assertEqual(sorted(set(rec) - set(got)), [])
        self.assertEqual(sorted(set(rec['jobs'][0]) - set(got['jobs'][0])), [])
        for run_view in (rec, got):
            self.assertEqual((run_view['status'], run_view['conclusion']), ('completed', 'success'))
            self.assertIn('failure', [j['conclusion'] for j in run_view['jobs']])

    def test_a_path_filtered_check_is_absent_while_still_required(self):
        f = self.fork('path-filter')
        rec = _fixture('path-filtered-check', 'check-runs')
        self.assertEqual(_check_run(f, S.CHECK), [])
        required = _json(f, 'api', f'repos/{S.SLUG}/branches/main/protection/'
                                   'required_status_checks')
        self.assertIn(S.CHECK, required['contexts'])
        self.assertIn('check_runs', rec)

    def test_a_stale_merge_ref_reads_as_recorded(self):
        f = self.fork('stale-merge-ref')
        rec = _fixture('stale-merge-ref', 'pr-view')
        got = _json(f, 'pr', 'view', '1', '-R', S.SLUG, '--json',
                    'headRefOid,mergeStateStatus,baseRefOid')
        self.assertEqual(sorted(rec), sorted(got))
        tip = S._git(['rev-parse', 'main'], cwd=f.repo_origin)
        self.assertNotEqual(got['baseRefOid'], tip)
        self.assertEqual((got['headRefOid'], got['mergeStateStatus']), (S.WORLD.hand, 'BEHIND'))
        red = _check_run(f, S.CHECK)
        self.assertEqual([r['conclusion'] for r in red], ['failure'])
        rid = red[0]['html_url'].split('/actions/runs/')[1].split('/')[0]
        run = _json(f, 'api', f'repos/{S.SLUG}/actions/runs/{rid}')
        rec_run = _fixture('stale-merge-ref', 'run')
        self.assertEqual(sorted(set(rec_run) - set(run)), [])
        self.assertEqual(run['event'], 'pull_request')
        tip_at = int(S._git(['log', '-1', '--format=%ct', tip], cwd=f.repo_origin))
        self.assertLess(_epoch(run['created_at']), tip_at)


class Readers(unittest.TestCase):
    """The lane's check reader on PR #1 under each check behaviour, the trunk requiring
    :data:`scenarios.CHECK`."""

    def read(self, behaviour):
        """``(state, passed, checks, unrequired red, stale)``."""
        from asf import stale_ref
        from asf.harvest import lane
        f = S.WORLD.fork()
        S.BEHAVIOURS[behaviour](f)
        with S.deciding(f) as product:
            state, _detail, checks = lane.pr_checks(S.SLUG, 1, (S.CHECK,), head=S.WORLD.hand)
            bare, _d, _c = lane.pr_checks(S.SLUG, 1, (), head=S.WORLD.hand)
            S._git(['fetch', '-q', 'origin'], cwd=f.repo)
            tip = S._git(['rev-parse', 'origin/main'], cwd=f.repo)
            red = [c for c in checks if c.get('bucket') in lane.RED_BUCKETS]
            stale = stale_ref.stale(product, S.SLUG, red, tip, f.repo)
        return (state, bare, lane.passed_names(checks, (S.CHECK,)), checks,
                lane.not_required_red(checks, (S.CHECK,)), stale)

    def test_open_is_green_and_passed(self):
        state, _bare, passed, _checks, _ignored, _stale = self.read('open')
        self.assertEqual((state, passed), ('green', {S.CHECK}))

    def test_a_skipped_required_check_on_a_head_nobody_attested_is_not_passed(self):
        _state, _bare, passed, checks, _ignored, _stale = self.read('skip-job')
        self.assertEqual(passed, set())
        self.assertEqual([c['bucket'] for c in checks if c['name'] == S.CHECK], ['skipping'])

    def test_a_skipped_required_check_on_an_attested_head_is_passed(self):
        _state, _bare, passed, checks, _ignored, _stale = self.read('skip-job-attested')
        self.assertEqual(passed, {S.CHECK})
        self.assertEqual([c.get('attested') for c in checks if c['name'] == S.CHECK],
                         ['asf/attested'])

    def test_a_path_filtered_required_check_is_missing(self):
        _state, _bare, passed, checks, _ignored, _stale = self.read('path-filter')
        self.assertEqual(passed, set())
        self.assertEqual([c for c in checks if c.get('name') == S.CHECK], [])

    def test_a_runs_success_never_hides_its_failed_job(self):
        state, bare, passed, _checks, ignored, _stale = self.read('hide-job-failure')
        # required: the failed job is not the trunk's — told, never acted on; required or not,
        # the job's own conclusion is read, never its run's
        self.assertEqual((state, passed, ignored), ('green', {S.CHECK}, [S.HIDDEN_JOB]))
        self.assertEqual(bare, 'red')

    def test_a_red_on_a_stale_merge_ref_is_no_verdict(self):
        state, _bare, passed, _checks, _ignored, stale = self.read('stale-merge-ref')
        self.assertEqual((state, passed, stale), ('red', set(), [S.CHECK]))


if __name__ == '__main__':
    unittest.main()
