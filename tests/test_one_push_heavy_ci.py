"""One correction round = one push, and review before heavy CI.

* the session's ``pre-push`` shim logs each push the product's hook passed
  (:mod:`asf.workers.pushlog`), warns a correction session on its second, and the health pass
  records a second push as the run's defect;
* a correction brief ends with the one-push rule;
* under ``conventions.ci.heavy_after_review`` the lane labels a PR only once its review approved
  the head (:meth:`asf.harvest.lane.GitHubHost.heavy_gate`), waits for the heavy run the label
  starts, credits a workflow skip only from a run created after that approval, and takes the
  label off when the branch goes back to a session.
"""
import importlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from asf import conventions, env
from asf.harvest import harvest, lane
from asf.workers import githooks, pushlog

HEAD = 'a' * 40
brief_build = importlib.import_module('asf.briefs.build')  # the package shadows the name


def _git(args, cwd, e=None):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=e)


class PrePushLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='onepush_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hooks = os.path.join(self.tmp, 'asf-hooks')
        os.makedirs(self.hooks)
        githooks._write_if_changed(os.path.join(self.hooks, 'asf-hook'), githooks.ASF_HOOK)
        for name in githooks.HOOKS:
            githooks._write_if_changed(os.path.join(self.hooks, name),
                                       githooks._SHIM.format(name=name))
        self.remote = os.path.join(self.tmp, 'remote.git')
        _git(['init', '-q', '--bare', self.remote], self.tmp)
        self.wt = os.path.join(self.tmp, 'wt')
        _git(['init', '-q', '-b', 'fix/B-0001', self.wt], self.tmp)
        _git(['remote', 'add', 'origin', self.remote], self.wt)
        _git(['config', 'core.hooksPath', '.githooks'], self.wt)  # the product's own hooks
        self.own = os.path.join(self.wt, '.githooks')
        os.makedirs(self.own)
        self.log = os.path.join(self.tmp, 'job.pushes')
        self.env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.tmp,
                    'GIT_CONFIG_COUNT': '3', 'GIT_CONFIG_KEY_0': 'user.name',
                    'GIT_CONFIG_VALUE_0': 't', 'GIT_CONFIG_KEY_1': 'user.email',
                    'GIT_CONFIG_VALUE_1': 't@example.com', 'GIT_CONFIG_KEY_2': 'core.hooksPath',
                    'GIT_CONFIG_VALUE_2': self.hooks, 'ASF_PUSH_LOG': self.log,
                    'ASF_ONE_PUSH': '1'}

    def _product_hook(self, body):
        path = os.path.join(self.own, 'pre-push')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n' + body)
        os.chmod(path, 0o755)

    def _commit_and_push(self, n):
        with open(os.path.join(self.wt, f'f{n}'), 'w', encoding='utf-8') as f:
            f.write(str(n))
        _git(['add', '-A'], self.wt, self.env)
        _git(['commit', '-q', '-m', f'fix(B-0001): {n}'], self.wt, self.env)
        return _git(['push', '-q', 'origin', 'fix/B-0001'], self.wt, self.env)

    def _logged(self):
        try:
            with open(self.log, encoding='utf-8') as f:
                return [l.strip() for l in f if l.strip()]
        except OSError:
            return []

    def test_each_push_is_logged_and_the_second_is_told(self):
        seen = os.path.join(self.tmp, 'seen')
        self._product_hook(f'cat > "{seen}"\n')
        p = self._commit_and_push(1)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('correction round', p.stderr)
        with open(seen, encoding='utf-8') as f:  # the product's hook still reads the refs
            self.assertIn('refs/heads/fix/B-0001', f.read())
        p = self._commit_and_push(2)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('push 2 of this correction round', p.stderr)
        head = _git(['rev-parse', 'HEAD'], self.wt).stdout.strip()
        self.assertEqual(len(self._logged()), 2)
        self.assertEqual(self._logged()[-1], head)

    def test_a_push_the_product_hook_refuses_is_not_counted(self):
        self._product_hook('exit 1\n')
        p = self._commit_and_push(1)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self._logged(), [])
        self._product_hook('exit 0\n')
        p = _git(['push', '-q', 'origin', 'fix/B-0001'], self.wt, self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self._logged()), 1)

    def test_no_log_without_the_env(self):
        e = dict(self.env)
        e.pop('ASF_PUSH_LOG')
        with open(os.path.join(self.wt, 'x'), 'w', encoding='utf-8') as f:
            f.write('x')
        _git(['add', '-A'], self.wt, e)
        _git(['commit', '-q', '-m', 'fix(B-0001): x'], self.wt, e)
        p = _git(['push', '-q', 'origin', 'fix/B-0001'], self.wt, e)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(self.log))


class PushCount(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='pushlog_')
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.object(env, 'ASF_HOME', self.home)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, job, lines):
        path = pushlog.path('p', job)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(''.join(l + '\n' for l in lines))

    def test_a_retried_sha_is_one_push(self):
        self._write('correct-t-0001', ['a' * 40, 'a' * 40])
        self.assertEqual(pushlog.defect('p', {'job': 'correct-t-0001', 'kind': 'correct'}),
                         (1, ''))

    def test_two_pushes_in_a_correction_round_are_a_defect(self):
        self._write('correct-t-0001', ['a' * 40, 'b' * 40])
        n, text = pushlog.defect('p', {'job': 'correct-t-0001', 'kind': 'correct'})
        self.assertEqual(n, 2)
        self.assertIn('2 pushes in one correction round', text)

    def test_a_first_build_may_push_more_than_once(self):
        self._write('coder-t-0001', ['a' * 40, 'b' * 40])
        self.assertEqual(pushlog.defect('p', {'job': 'coder-t-0001', 'kind': 'coder'}), (2, ''))

    def test_launch_clears_and_env_names_the_log(self):
        self._write('correct-t-0001', ['a' * 40])
        pushlog.clear('p', 'correct-t-0001')
        self.assertEqual(pushlog.count('p', 'correct-t-0001'), 0)
        e = pushlog.env_for('p', 'correct-t-0001', 'correct')
        self.assertEqual(e['ASF_PUSH_LOG'], pushlog.path('p', 'correct-t-0001'))
        self.assertEqual(e['ASF_ONE_PUSH'], '1')
        self.assertNotIn('ASF_ONE_PUSH', pushlog.env_for('p', 'coder-t-0001', 'coder'))
        self.assertNotIn('..', pushlog.path('p', '../../x'))


class OnePushBrief(unittest.TestCase):
    def test_a_correction_brief_ends_with_the_rule(self):
        row = SimpleNamespace(correction='PR #7 checks red: gate')
        self.assertIn('ONE PUSH', brief_build.correction_text(row, 'correct'))
        self.assertIn('ONE PUSH', brief_build.correction_text(row, 'adjudicate'))
        self.assertNotIn('ONE PUSH', brief_build.correction_text(row, 'plan'))
        self.assertEqual(brief_build.correction_text(SimpleNamespace(correction=''), 'correct'),
                         '')


class HeavyConventions(unittest.TestCase):
    def test_off_by_default(self):
        c = conventions.Conventions.from_mapping({})
        self.assertFalse(c.heavy_after_review())
        self.assertEqual(c.heavy_label(), conventions.DEFAULT_HEAVY_CI_LABEL)

    def test_on_and_a_custom_label(self):
        c = conventions.Conventions.from_mapping(
            {'ci': {'heavy_after_review': True, 'heavy_label': 'ci:full'}})
        self.assertTrue(c.heavy_after_review())
        self.assertEqual(c.heavy_label(), 'ci:full')


class HeavyAfterReview(unittest.TestCase):
    REQUIRED = ['gate', 'm8-e2e']
    CHECKS = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'm8-e2e', 'bucket': 'skipping'}]
    JOBS = [{'name': 'gate', 'conclusion': 'success'}, {'name': 'm8-e2e', 'conclusion': 'skipped'}]

    def _host(self, heavy=True):
        conv = {'landing': 'pull-request', 'landing_checks': self.REQUIRED,
                'landing_checks_missing': 'wait'}
        if heavy:
            conv['ci'] = {'heavy_after_review': True}
        product = env.Product('p', {'repo_slug': 'o/p', 'conventions': conv,
                                     'ci': {'workflow': 'ci.yml'}})
        tmp = tempfile.mkdtemp(prefix='heavy_')
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        runner = lane.Lane.__new__(lane.Lane)
        self.lines = []
        runner.product, runner.conv, runner.out, runner.dry_run = product, product.conventions, \
            self.lines.append, False
        runner.results, runner.now, runner.state_dir, runner.repo = {}, 1_800_000_000.0, tmp, None
        host = lane.GitHubHost(product, None)
        host.lane = runner
        runner.host = host
        self.runner = runner
        return host

    def _gh(self, runs):
        self.calls = []

        def gh(args):
            self.calls.append(args)
            if args[:2] == ['pr', 'checks']:
                return 0, json.dumps(self.CHECKS), ''
            if args[:2] == ['pr', 'edit']:
                return 0, '', ''
            if args[0] == 'api' and '/actions/runs?head_sha=' in args[1]:
                return 0, json.dumps({'workflow_runs': runs}), ''
            if args[0] == 'api' and args[1].split('?')[0].endswith('/jobs'):
                return 0, json.dumps({'jobs': self.JOBS}), ''
            return 1, '', 'unexpected'
        return gh

    def _gate(self, host, prev, runs=()):
        f = {'branch': 'worker/T-0001', 'prev': prev, 'class': lane.CODE, 'head': HEAD}
        written = {}

        def fake_set(ln, f, s, r, result=None, **kw):
            written.update(state=s, reason=r, **kw)
            ln.results[f['branch']] = (s, r)
        with mock.patch.object(harvest, '_gh', side_effect=self._gh(list(runs))), \
                mock.patch.object(lane.Lane, 'set', fake_set):
            how = host.check_gate(f, 7, ['src/a.py'])
        return how, written

    def test_an_approved_head_is_labelled_first_and_waits(self):
        host = self._host()
        how, written = self._gate(host, {'state': lane.GATE, 'head': HEAD, 'pr': 7})
        self.assertIsNone(how)
        self.assertIn(['pr', 'edit', '7', '-R', 'o/p', '--add-label',
                       conventions.DEFAULT_HEAVY_CI_LABEL], self.calls)
        self.assertFalse(any(c[:2] == ['pr', 'checks'] for c in self.calls))
        self.assertEqual(written['state'], lane.WAITING_CI)
        self.assertEqual(written['heavy'], HEAD)
        self.assertTrue(written['heavy_at'])

    def test_off_never_labels(self):
        host = self._host(heavy=False)
        run = {'id': 1, 'status': 'completed', 'created_at': '2027-01-15T08:00:00Z'}
        how, _written = self._gate(host, {'state': lane.GATE, 'head': HEAD, 'pr': 7}, [run])
        self.assertEqual(how, 'ci')
        self.assertFalse(any(c[:2] == ['pr', 'edit'] for c in self.calls))

    def test_a_skip_from_before_the_approval_is_not_green(self):
        host = self._host()
        prev = {'state': lane.WAITING_CI, 'head': HEAD, 'pr': 7, 'heavy': HEAD,
                'heavy_at': '2027-01-15T09:00:00Z'}
        before = {'id': 1, 'status': 'completed', 'created_at': '2027-01-15T08:00:00Z'}
        how, written = self._gate(host, prev, [before])
        self.assertIsNone(how)
        self.assertIn('m8-e2e', written['reason'])
        self.assertFalse(any(c[:2] == ['pr', 'edit'] for c in self.calls))

    def _recheck(self, host, prev, runs):
        f = {'branch': 'worker/T-0001', 'prev': prev, 'class': lane.CODE, 'head': HEAD,
             'on_checks': True}
        with mock.patch.object(harvest, '_gh', side_effect=self._gh(list(runs))):
            return host.recheck(f, 7)

    def test_a_label_less_run_skip_never_merges_at_the_merge_recheck(self):
        # the head was never approved for heavy CI (no `heavy` on its record): the only run on
        # it is the push's light one, which skipped the required heavy job — no merge
        host = self._host()
        before = {'id': 1, 'status': 'completed', 'created_at': '2027-01-15T08:00:00Z'}
        why = self._recheck(host, {'state': lane.GATE, 'head': HEAD, 'pr': 7}, [before])
        self.assertIn('not green at merge: m8-e2e', why)
        self.assertFalse(any(c[0] == 'api' and '/actions/runs' in c[1]
                             for c in self.calls))  # no skip ever credited
        # approved later, but the only run is still the pre-approval one — no merge either
        prev = {'state': lane.GATE, 'head': HEAD, 'pr': 7, 'heavy': HEAD,
                'heavy_at': '2027-01-15T09:00:00Z'}
        self.assertIn('not green at merge: m8-e2e', self._recheck(host, prev, [before]))
        # the approved (labelled) run's own path-scoped skip counts
        after = {'id': 2, 'status': 'completed', 'created_at': '2027-01-15T09:00:05Z'}
        self.assertIsNone(self._recheck(host, prev, [before, after]))

    def test_a_skip_from_the_approved_run_counts(self):
        host = self._host()
        prev = {'state': lane.WAITING_CI, 'head': HEAD, 'pr': 7, 'heavy': HEAD,
                'heavy_at': '2027-01-15T09:00:00Z'}
        after = {'id': 2, 'status': 'completed', 'created_at': '2027-01-15T09:00:05Z'}
        how, _written = self._gate(host, prev, [after])
        self.assertEqual(how, 'ci')

    def test_a_docs_pr_is_never_labelled(self):
        # a spec/plan PR lands on the landing checks alone: no heavy job answers for it
        host = self._host()
        f = {'branch': 'spec/F-0001', 'prev': {'state': lane.GATE, 'head': HEAD, 'pr': 7},
             'class': lane.DOCS, 'head': HEAD}
        self.CHECKS = [{'name': 'gate', 'bucket': 'pass'}, {'name': 'm8-e2e', 'bucket': 'pass'}]
        try:
            with mock.patch.object(harvest, '_gh', side_effect=self._gh([])), \
                    mock.patch.object(lane.Lane, 'set', lambda *a, **k: None):
                how = host.check_gate(f, 7, ['docs/specs/a.md'])
        finally:
            del self.CHECKS
        self.assertEqual(how, 'ci')
        self.assertFalse(any(c[:2] == ['pr', 'edit'] for c in self.calls))

    def _gh_dispatch(self, runs):
        inner = self._gh(runs)

        def gh(args):
            if args[:2] == ['workflow', 'run']:
                self.calls.append(args)
                return 0, '', ''
            return inner(args)
        return gh

    def _gate_dispatch(self, host, prev, runs=()):
        f = {'branch': 'worker/T-0001', 'prev': prev, 'class': lane.CODE, 'head': HEAD}
        written = {}

        def fake_set(ln, f, s, r, result=None, **kw):
            written.update(state=s, reason=r, **kw)
        with mock.patch.object(harvest, '_gh', side_effect=self._gh_dispatch(list(runs))), \
                mock.patch.object(lane.Lane, 'set', fake_set):
            return host.check_gate(f, 7, ['src/a.py']), written

    def test_a_label_that_started_nothing_is_kicked_once(self):
        host = self._host()
        self.runner.now = lane._parse_at('2027-01-15T09:15:00Z')
        prev = {'state': lane.WAITING_CI, 'head': HEAD, 'pr': 7, 'heavy': HEAD,
                'heavy_at': '2027-01-15T09:00:00Z'}
        before = {'id': 1, 'status': 'completed', 'created_at': '2027-01-15T08:00:00Z'}
        how, written = self._gate_dispatch(host, prev, [before])
        self.assertIsNone(how)
        self.assertIn(['workflow', 'run', 'ci.yml', '--ref', 'worker/T-0001', '-R', 'o/p'],
                      self.calls)
        self.assertEqual(written['heavy_kick'], HEAD)
        self.assertTrue(any('dispatched ci.yml' in l for l in self.lines))
        # the next pass: looked at once — neither the runs nor a dispatch again
        how, _ = self._gate_dispatch(host, dict(prev, heavy_kick=HEAD), [before])
        self.assertIsNone(how)
        self.assertFalse(any(c[:2] == ['workflow', 'run'] for c in self.calls))

    def test_no_kick_before_its_time_or_when_the_label_ran(self):
        host = self._host()
        prev = {'state': lane.WAITING_CI, 'head': HEAD, 'pr': 7, 'heavy': HEAD,
                'heavy_at': '2027-01-15T09:00:00Z'}
        before = {'id': 1, 'status': 'completed', 'created_at': '2027-01-15T08:00:00Z'}
        self.runner.now = lane._parse_at('2027-01-15T09:05:00Z')  # 5 min: not due
        _how, written = self._gate_dispatch(host, prev, [before])
        self.assertFalse(any(c[:2] == ['workflow', 'run'] for c in self.calls))
        self.assertNotIn('heavy_kick', written)
        # due, but the label's run exists (cancelled by the queue, say): no dispatch, looked at
        self.runner.now = lane._parse_at('2027-01-15T09:15:00Z')
        after = {'id': 2, 'status': 'completed', 'conclusion': 'cancelled',
                 'created_at': '2027-01-15T09:00:05Z'}
        self.JOBS = [{'name': 'gate', 'conclusion': 'success'},
                     {'name': 'm8-e2e', 'conclusion': 'cancelled'}]
        try:
            _how, written = self._gate_dispatch(host, prev, [before, after])
        finally:
            del self.JOBS
        self.assertFalse(any(c[:2] == ['workflow', 'run'] for c in self.calls))
        self.assertEqual(written.get('heavy_kick'), HEAD)

    def test_a_conflicting_pr_with_checks_missing_goes_back(self):
        host = self._host()
        self.runner.repo, self.runner.trunk = self.runner.state_dir, 'main'
        f = {'branch': 'worker/T-0001', 'class': lane.CODE, 'head': HEAD,
             'prev': {'state': lane.WAITING_CI, 'head': HEAD, 'pr': 7, 'heavy': HEAD,
                      'heavy_at': '2027-01-15T09:00:00Z'}}
        sent = []
        with mock.patch.object(harvest, '_gh', side_effect=self._gh([])), \
                mock.patch.object(lane, 'conflict_files', return_value=['a.sql']), \
                mock.patch.object(lane, 'send_back', lambda ln, f, k, t, files:
                                  sent.append((k, t, files))):
            how = host.check_gate(f, 7, ['src/a.py'])
        self.assertIsNone(how)
        self.assertEqual(sent[0][0], 'conflict')
        self.assertIn('no pull_request workflow', sent[0][1])
        self.assertEqual(sent[0][2], ['a.sql'])

    def test_the_kick_rides_the_head(self):
        runner = lane.Lane.__new__(lane.Lane)
        prev = {'state': lane.WAITING_CI, 'head': HEAD, 'heavy': HEAD, 'heavy_at': 'T',
                'heavy_kick': HEAD}
        f = {'branch': 'b', 'head': HEAD, 'prev': prev}
        self.assertEqual(runner.record(f, lane.WAITING_CI, 'x')['heavy_kick'], HEAD)
        self.assertNotIn('heavy_kick', runner.record(f, lane.BACK, 'kind=gate'))

    def test_the_approval_rides_the_head_and_leaves_with_it(self):
        runner = lane.Lane.__new__(lane.Lane)
        prev = {'state': lane.WAITING_CI, 'head': HEAD, 'heavy': HEAD, 'heavy_at': 'T'}
        f = {'branch': 'b', 'head': HEAD, 'prev': prev}
        self.assertEqual(runner.record(f, lane.WAITING, 'x')['heavy'], HEAD)
        self.assertNotIn('heavy', runner.record(f, lane.BACK, 'kind=gate'))
        self.assertNotIn('heavy', runner.record(dict(f, head='b' * 40), lane.WAITING, 'x'))

    def test_going_back_takes_the_label_off(self):
        host = self._host()
        runner = self.runner
        runner.path = os.path.join(runner.state_dir, 'sessions.jsonl')
        prev = {'state': lane.WAITING_CI, 'head': HEAD, 'pr': 7, 'heavy': HEAD, 'heavy_at': 'T'}
        f = {'branch': 'worker/T-0001', 'head': HEAD, 'prev': prev, 'pr': {'number': 7}}
        with mock.patch.object(harvest, '_gh', side_effect=self._gh([])), \
                mock.patch.object(lane.Lane, 'write', lambda *a, **k: None):
            runner.set(f, lane.BACK, 'kind=gate')
        self.assertIn(['pr', 'edit', '7', '-R', 'o/p', '--remove-label',
                       conventions.DEFAULT_HEAVY_CI_LABEL], self.calls)


class LatestChecks(unittest.TestCase):
    def test_the_labelled_run_replaces_the_light_runs_twin(self):
        old = {'name': 'm8-e2e', 'workflow': 'ci', 'bucket': 'skipping',
               'startedAt': '2027-01-15T08:00:00Z'}
        new = {'name': 'm8-e2e', 'workflow': 'ci', 'bucket': 'pass',
               'startedAt': '2027-01-15T09:00:00Z'}
        gate = {'name': 'gate', 'workflow': 'ci', 'bucket': 'pass',
                'startedAt': '2027-01-15T08:00:00Z'}
        other = dict(old, workflow='nightly')
        self.assertEqual(lane.latest_checks([old, gate, new, other]), [gate, new, other])

    def test_a_check_not_started_is_the_newest(self):
        done = {'name': 'gate', 'workflow': 'ci', 'bucket': 'cancel',
                'startedAt': '2027-01-15T08:00:00Z'}
        queued = {'name': 'gate', 'workflow': 'ci', 'bucket': 'pending', 'startedAt': ''}
        self.assertEqual(lane.latest_checks([queued, done]), [queued])
        with mock.patch.object(harvest, '_gh',
                               return_value=(0, json.dumps([done, queued]), '')):
            self.assertEqual(lane.pr_checks('o/p', 7, ['gate'])[0], 'pending')


if __name__ == '__main__':
    unittest.main()
