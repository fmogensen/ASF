import argparse
import contextlib
import datetime as dt
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from asf import env
from asf.init import STREAM_FOLDERS
from asf.record import frontmatter
from asf.record import match
from asf.record.check import cmd_check
from asf.record.index import do_index
from asf.conventions import Conventions
from asf.groom import groom
from asf.metrics import import_sessions
from asf.metrics import metrics
from asf.scorecard import score

DAY = '2026-09-21'

# The fixture-repo helpers below (make_repo/write_item/item_text) mirror asf.record's own test
# fixtures (see tests/test_match.py's `fixture()`, and the item-file shape asf.record.core.TYPES
# and asf.record.frontmatter define) rather than importing a `tests.test_backlog` — the source
# project's equivalent test helper module hasn't been ported here as its own module; asf.record's
# CLI is a set of `cmd_*` functions (new/check/index/ingest), not one script.
FOLDERS = ['epics', 'features', 'stories', 'tasks', 'bugs', 'decisions', 'rules']
DEFAULT_BODY = (
    "## Description\n\n"
    "## Acceptance\n"
    "- [ ] \n\n"
    "## Non-goals\n\n"
    "## History\n"
    "- 2026-01-01: created\n\n"
    "## Children\n\n"
    "## Backlinks\n"
)


def make_repo():
    root = tempfile.mkdtemp(prefix='metrics_test_')
    for f in FOLDERS:
        os.makedirs(os.path.join(root, f))
    return root


def item_text(id_, type_, title, parent=None, typed_lines=(),
              machine_lines=('schema_version: 1',
                             'state: New',
                              'stage_since: 2026-01-01T00:00:00Z',
                              'updated: 2026-01-01T00:00:00Z'),
              body=None):
    lines = [f"id: {id_}", f"type: {type_}", f"title: {title}"]
    if parent:
        lines.append(f"parent: {parent}")
    lines.extend(typed_lines)
    lines.append('# ---- machine ----')
    lines.extend(machine_lines)
    header = '\n'.join(lines)
    return f"---\n{header}\n---\n{body if body is not None else DEFAULT_BODY}"


def folder_of(type_):
    return {
        'epic': 'epics', 'feature': 'features', 'story': 'stories',
        'task': 'tasks', 'bug': 'bugs', 'decision': 'decisions', 'rule': 'rules',
    }[type_]


def write_item(root, id_, type_, title, **kw):
    path = os.path.join(root, folder_of(type_), f"{id_}.md")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(item_text(id_, type_, title, **kw))
    return path


def build_repo():
    """E-0001 Billing (budget 100) > F-0001 Free plan (FREE-1, PR 601) > S-0001 > T-0002; F-0001 > T-0001 (PR 623,
    FREE-1/T3); E-0001 > F-0002 Other > B-0001; then `asf index`."""
    root = make_repo()
    for f in STREAM_FOLDERS:
        os.makedirs(os.path.join(root, f), exist_ok=True)
    write_item(root, 'E-0001', 'epic', 'Billing', typed_lines=('budget_usd: 100',))
    write_item(root, 'F-0001', 'feature', 'Free plan', parent='E-0001',
               typed_lines=('priority: need', 'legacy_id: FREE-1', 'links:', '  prs: [601]'))
    write_item(root, 'S-0001', 'story', 'Sign-up without a card', parent='F-0001')
    write_item(root, 'T-0001', 'task', 'Plan door copy', parent='F-0001',
               typed_lines=('legacy_id: FREE-1/T3', 'links:', '  prs: [623]', '  branches: [cloud/free-plan-t3]'))
    write_item(root, 'T-0002', 'task', 'Sign-up form', parent='S-0001', typed_lines=('stories: [S-0001]',))
    write_item(root, 'F-0002', 'feature', 'Other feature', parent='E-0001')
    write_item(root, 'B-0001', 'bug', 'Banner shows', parent='F-0002', typed_lines=('severity: S2',))
    reindex(root)
    return root


def reindex(root):
    rc = do_index(root)
    assert rc == 0, f"asf.record.index.do_index failed with rc={rc}"


def job(name, conclusion='success', minutes=5, runner='box1', step=None, cause=None):
    j = {'name': name, 'conclusion': conclusion, 'runner': runner, 'minutes': minutes, 'failed_step': step}
    if cause is not None:
        j['cause'] = cause
    return j


def ci_event(run, **kw):
    ev = {'run': run, 'workflow': 'ci', 'sha': 'a' * 40, 'branch': 'cloud/free-plan-t3', 'conclusion': 'success',
          'attempt': 1, 'minutes': 10, 'jobs': [job('lint', minutes=4), job('e2e', minutes=6)],
          'ts': f'{DAY}T10:00:00Z'}
    ev.update(kw)
    return ev


def run_cli(root, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = metrics.main(['--root', root, *argv])
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = build_repo()
        self.items = match.load_index(self.root)
        self.addCleanup(shutil.rmtree, self.root, True)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as f:
            return f.read()

    def read_bytes(self, rel):
        with open(os.path.join(self.root, rel), 'rb') as f:
            return f.read()

    def put(self, stream, ev):
        return metrics.append_event(self.root, stream, metrics.validate(stream, ev, self.items))


class Schema(Base):
    def bad(self, stream, ev, needle):
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.validate(stream, ev, self.items)
        self.assertIn(needle, str(cm.exception))

    def test_required_keys_and_types(self):
        ev = ci_event(1)
        for k in ('run', 'workflow', 'sha', 'branch', 'conclusion', 'attempt', 'minutes', 'jobs'):
            self.bad('ci', {a: b for a, b in ev.items() if a != k}, f"missing required key '{k}'")
        self.bad('ci', dict(ci_event(1), run='7'), "'run' must be int")
        self.bad('ci', ci_event(1, minutes=True), "'minutes' must be num")
        self.bad('ci', ci_event(1, attempt=0), 'attempt')
        self.bad('ci', ci_event(1, jobs=[{'name': 'x'}]), 'ci.jobs[0]: missing required key')
        self.bad('ci', ci_event(1, wat=1), 'unknown key(s) wat')
        self.bad('sessions', {'task': 't', 'account': 'accta'}, "missing required key 'result'")
        self.bad('sessions', {'task': 't', 'account': 'accta', 'result': 'done', 'kind': 'nope'}, "'kind' must be one of")
        self.bad('ticks', {'tick': 1, 'launches': 0}, "missing required key 'merges'")
        self.bad('ticks', {'tick': 1, 'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0,
                           'quota': {'accta': {'h5': 1}}}, 'quota')
        tick = {'tick': 1, 'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0}
        self.bad('ticks', dict(tick, steps='wave'), "'steps' must be list")
        self.bad('ticks', dict(tick, product=3), "'product' must be str|null")
        ev = metrics.validate('ticks', dict(tick, steps=[{'step': 'wave', 'ok': True, 'seconds': 1.0}],
                                            product='sample'), self.items)
        self.assertEqual((ev['product'], ev['steps'][0]['step']), ('sample', 'wave'))
        self.assertEqual((metrics.validate('ticks', tick, self.items)['steps'],
                          metrics.validate('ticks', tick, self.items)['product']), ([], None))

    def test_ci_run_and_job_figures_default_and_validate(self):
        ev = metrics.validate('ci', ci_event(1), self.items)
        self.assertEqual((ev['wall_minutes'], ev['queue_s']), (0, 0))
        j = ev['jobs'][0]
        self.assertIsNone(j['seconds'])
        self.assertIsNone(j['queued_s'])
        # R5: a job dict carrying both figures validates — this raised SchemaError before JOB_SCHEMA grew them
        ev = metrics.validate('ci', ci_event(2, jobs=[dict(job('x'), seconds=12.3, queued_s=4.5)],
                                             wall_minutes=9, queue_s=4.5), self.items)
        self.assertEqual((ev['jobs'][0]['seconds'], ev['jobs'][0]['queued_s']), (12.3, 4.5))
        self.assertEqual((ev['wall_minutes'], ev['queue_s']), (9, 4.5))

    def test_ts_must_be_utc_iso(self):
        self.bad('ci', ci_event(1, ts='2026-09-21 10:00'), "'ts' must be a UTC ISO timestamp")
        self.bad('ci', ci_event(1, ts='2026-09-21T10:00:00+02:00'), "'ts' must be a UTC ISO timestamp")

    def test_an_id_is_never_invented(self):
        self.bad('ci', ci_event(1, items=['T-9999']), 'T-9999 is not in index.json')
        self.bad('sessions', {'task': 't', 'account': 'a', 'result': 'done', 'item': 'F-7777'}, 'F-7777')
        self.bad('ci', ci_event(1, items=['nope']), 'not an item id')

    def test_ts_and_defaults_are_filled(self):
        now = dt.datetime(2026, 9, 21, 6, 40, 1, tzinfo=dt.timezone.utc)
        ev = metrics.validate('sessions', {'task': 'fix-free-plan-t3-r2', 'account': 'accta', 'result': 'done'},
                              self.items, now=now)
        self.assertEqual(ev['ts'], '2026-09-21T06:40:01Z')
        self.assertEqual((ev['kind'], ev['round'], ev['usd'], ev['pushes'], ev['model']), ('fix', 2, None, None, None))
        self.assertEqual(ev['item'], 'T-0001')

    def test_kind_from_the_task_prefix(self):
        want = {'spec-free-plan': 'spec', 'review-x-r1': 'review', 'fix-x-r3': 'fix', 'p2-t1': 'code', 'plan-x': 'plan',
                'preflight-x': 'preflight', 'probe-x': 'probe', 'rebase-x': 'rebase', 'relaunch-x': 'relaunch',
                'launch-accta': 'launch', 'tick-0001': 'tick', 'adjudicate-x': 'other'}
        for task, kind in want.items():
            self.assertEqual(metrics.derive_kind(task), kind, task)
        self.assertIsNone(metrics.derive_round('p2-t1'))
        self.assertEqual(metrics.derive_round('fix-x-r12'), 12)

    def test_ci_matcher_fills_items_or_says_why_null(self):
        ev = metrics.validate('ci', ci_event(1), self.items)
        self.assertEqual((ev['items'], ev['item_reason']), (['T-0001'], None))
        ev = metrics.validate('ci', ci_event(2, branch='cloud/unrelated'), self.items)
        self.assertIsNone(ev['items'])
        self.assertIn('no rule matched', ev['item_reason'])
        ev = metrics.validate('ci', ci_event(3, branch='worktree-m-batch-20260921-0101', batch='worktree-m-batch-20260921-0101'),
                              self.items)
        self.assertEqual((ev['items'], ev['item_reason']), (None, 'batch run without a PR list'))
        hints = {'prs': [601, 623]}
        ev = metrics.validate('ci', dict(ci_event(4, branch='worktree-m-batch-20260921-0101',
                                                  batch='worktree-m-batch-20260921-0101'), match_hints=hints), self.items)
        self.assertEqual(ev['items'], ['F-0001', 'T-0001'])
        self.assertNotIn('match_hints', ev)

    def test_session_with_two_matches_picks_the_deepest_and_says_so(self):
        ev = metrics.validate('sessions', {'task': 'x', 'account': 'a', 'result': 'done',
                                           'branch': 'cloud/y', 'match_hints': {'title': 'S-0001 and T-0002'}}, self.items)
        self.assertEqual(ev['item'], 'T-0002')
        self.assertEqual(ev['item_reason'], 'ambiguous: S-0001, T-0002')
        ev = metrics.validate('sessions', {'task': 'zzz', 'account': 'a', 'result': 'done'}, self.items)
        self.assertIsNone(ev['item'])
        self.assertIn('no rule matched', ev['item_reason'])


class TickWaveSchemaTests(Base):
    """§2.3, T5: ``ticks``' new ``wave`` key — a plain ``dict``, nothing in ``validate`` keys
    off it (unlike ``kind``/``item``, which stay stream-specific branches)."""

    TICK = {'tick': 1, 'launches': 0, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0}

    def test_a_line_carrying_wave_is_accepted_unchanged(self):
        wave = {'idle': True, 'new_tasks': 0, 'new_task_ids': [], 'gates': {}}
        ev = metrics.validate('ticks', dict(self.TICK, wave=wave), self.items)
        self.assertEqual(ev['wave'], wave)

    def test_a_line_without_wave_comes_back_with_the_empty_default(self):
        ev = metrics.validate('ticks', self.TICK, self.items)
        self.assertEqual(ev['wave'], {})

    def test_a_wave_that_is_not_an_object_raises(self):
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.validate('ticks', dict(self.TICK, wave='idle'), self.items)
        self.assertIn("'wave' must be dict", str(cm.exception))


class CiWorkflowSelection(Base):
    """`ci_from_api` imports every workflow a pull request or the trunk runs, and a batch/queue
    ref's own run, never only the one `conventions.ci_workflow` (or its stand-in default)
    happens to name; a configured workflow is kept too, matched against a run's display name or
    its file's basename. Two real defects this fixes: a product with no `ci.workflow` set made
    the old filter look for a workflow named 'ci', which may match none of its runs; and the old
    filter kept only that one workflow, so a second PR workflow's reds never entered the stream."""

    CONV = Conventions.from_mapping({'branch_prefixes': {'batch': 'worktree-m-batch-'}})

    def make_run(self, id_, name, branch, event=None, path=None, pr=(), conclusion='success',
                created='2026-09-21T01:00:00Z', updated='2026-09-21T01:05:00Z', attempt=1):
        return {'id': id_, 'name': name, 'path': path or f'.github/workflows/{name}.yml',
                'event': event, 'head_branch': branch, 'head_sha': 'a' * 40, 'conclusion': conclusion,
                'created_at': created, 'updated_at': updated, 'run_attempt': attempt, 'pr': list(pr)}

    def fetch(self, runs, workflows='unrelated', jobs=None, conv=None):
        jobs = jobs or {}

        def fake(args, timeout=300):
            joined = ' '.join(args)
            if '/jobs' in joined:
                run_id = int(joined.split('/runs/')[1].split('/')[0])
                return jobs.get(run_id, []), True
            return runs, True
        with mock.patch.object(metrics, 'gh_lines_answered', side_effect=fake), \
                mock.patch.object(metrics, 'pr_info', return_value=None):
            return {e['run']: e for e in metrics.ci_from_api(
                7, workflows, {}, self.items, repo_slug='sample/sample', conv=conv or self.CONV)}

    def test_a_pull_request_run_enters_whatever_its_workflow_is_named(self):
        evs = self.fetch([self.make_run(1, 'lint', 'cloud/x', event='pull_request', pr=[623])])
        self.assertEqual(set(evs), {1})
        self.assertEqual(evs[1]['workflow'], 'lint')

    def test_a_trunk_run_enters_whatever_its_workflow_is_named(self):
        evs = self.fetch([self.make_run(2, 'docs-check', 'main', event='push')])
        self.assertEqual(set(evs), {2})

    def test_a_batch_ref_run_enters_and_is_tagged_batch(self):
        evs = self.fetch([self.make_run(3, 'docs-check', 'worktree-m-batch-20260921-0001', event='push')])
        self.assertEqual(evs[3]['batch'], 'worktree-m-batch-20260921-0001')

    def test_an_unrelated_run_is_dropped(self):
        evs = self.fetch([self.make_run(4, 'docs-check', 'feature/x', event='workflow_dispatch')])
        self.assertEqual(evs, {})

    def test_a_configured_workflow_is_matched_by_display_name(self):
        evs = self.fetch([self.make_run(5, 'Tests', 'feature/x', event='workflow_dispatch',
                                   path='.github/workflows/tests.yml')], workflows='Tests')
        self.assertEqual(set(evs), {5})

    def test_a_configured_workflow_is_matched_by_file_name_too(self):
        # the display name differs from the configured file name — today's single-name filter
        # (a bare `==`) discards this row; the fix matches either form
        evs = self.fetch([self.make_run(6, 'Tests', 'feature/y', event='workflow_dispatch',
                                   path='.github/workflows/tests.yml')], workflows='tests.yml')
        self.assertEqual(set(evs), {6})

    def test_a_configured_list_is_matched_too(self):
        evs = self.fetch([self.make_run(7, 'release', 'feature/z', event='workflow_dispatch')],
                         workflows=['other', 'release'])
        self.assertEqual(set(evs), {7})

    def test_every_workflow_is_recorded_on_its_own_run(self):
        evs = self.fetch([self.make_run(8, 'lint', 'cloud/x', event='pull_request', pr=[623]),
                          self.make_run(9, 'tests', 'cloud/x', event='pull_request', pr=[623])])
        self.assertEqual({evs[8]['workflow'], evs[9]['workflow']}, {'lint', 'tests'})

    def test_superseded_is_scoped_to_its_own_workflow(self):
        # a later run of a DIFFERENT PR workflow on the same branch never supersedes this one
        evs = self.fetch([self.make_run(10, 'lint', 'cloud/x', event='pull_request', pr=[623],
                                   conclusion='cancelled', created='2026-09-21T01:00:00Z'),
                          self.make_run(11, 'tests', 'cloud/x', event='pull_request', pr=[623],
                                  created='2026-09-21T02:00:00Z')])
        self.assertFalse(evs[10]['superseded'])


class CiFigures(Base):
    """`ci_from_api` carries the four figures `ci_measure` and `ci_census` already read: per job
    the seconds it ran and the seconds it waited for a runner, per run its wall clock and the
    worst wait any of its jobs suffered."""

    RUN = {'id': 20, 'name': 'ci', 'head_branch': 'cloud/x', 'head_sha': 'a' * 40, 'conclusion': 'success',
           'created_at': '2026-09-21T01:00:00Z', 'updated_at': '2026-09-21T01:12:00Z', 'run_attempt': 1, 'pr': []}

    def fetch_one(self, jobs, run=None):
        run = run or self.RUN

        def fake(args, timeout=300):
            joined = ' '.join(args)
            if '/jobs' in joined:
                return jobs, True
            return [run], True
        with mock.patch.object(metrics, 'gh_lines_answered', side_effect=fake), \
                mock.patch.object(metrics, 'pr_info', return_value=None):
            evs = metrics.ci_from_api(2, 'ci', {}, self.items, repo_slug='sample/sample')
        self.assertEqual(len(evs), 1)
        return evs[0]

    def test_job_seconds_and_queued_s_come_from_their_stamps(self):
        ev = self.fetch_one([{'name': 'tests', 'conclusion': 'success', 'runner_name': 'box1',
                              'created_at': '2026-09-21T01:00:00Z', 'started_at': '2026-09-21T01:02:00Z',
                              'completed_at': '2026-09-21T01:07:00Z', 'failed': []}])
        j = ev['jobs'][0]
        self.assertEqual((j['seconds'], j['queued_s'], j['minutes']), (300.0, 120.0, 5))

    def test_a_sub_minute_job_still_reads_zero_minutes(self):
        # D4: minutes floors to whole minutes and does not move; seconds sits beside it
        ev = self.fetch_one([{'name': 'tests', 'conclusion': 'success', 'runner_name': 'box1',
                              'created_at': '2026-09-21T01:00:00Z', 'started_at': '2026-09-21T01:00:00Z',
                              'completed_at': '2026-09-21T01:00:40Z', 'failed': []}])
        j = ev['jobs'][0]
        self.assertEqual((j['seconds'], j['minutes']), (40.0, 0))

    def test_job_figures_are_none_without_their_own_stamps(self):
        ev = self.fetch_one([{'name': 'no-completion', 'conclusion': 'cancelled', 'runner_name': 'box1',
                              'created_at': '2026-09-21T01:00:00Z', 'started_at': '2026-09-21T01:02:00Z',
                              'completed_at': None, 'failed': []},
                             {'name': 'no-creation', 'conclusion': 'success', 'runner_name': 'box2',
                              'created_at': None, 'started_at': '2026-09-21T01:02:00Z',
                              'completed_at': '2026-09-21T01:07:00Z', 'failed': []}])
        js = {j['name']: j for j in ev['jobs']}
        self.assertIsNone(js['no-completion']['seconds'])
        self.assertEqual(js['no-completion']['queued_s'], 120.0)
        self.assertIsNone(js['no-creation']['queued_s'])
        self.assertEqual(js['no-creation']['seconds'], 300.0)

    def test_run_queue_s_is_the_max_of_its_jobs_not_their_sum(self):
        # D21: two jobs that queued in parallel — the sum double-counts the overlap
        ev = self.fetch_one([{'name': 'a', 'conclusion': 'success', 'runner_name': 'box1',
                              'created_at': '2026-09-21T01:00:00Z', 'started_at': '2026-09-21T01:01:00Z',
                              'completed_at': '2026-09-21T01:05:00Z', 'failed': []},
                             {'name': 'b', 'conclusion': 'success', 'runner_name': 'box2',
                              'created_at': '2026-09-21T01:00:00Z', 'started_at': '2026-09-21T01:01:30Z',
                              'completed_at': '2026-09-21T01:05:00Z', 'failed': []}])
        self.assertEqual(ev['queue_s'], 90.0)
        self.assertEqual(ev['wall_minutes'], 12)   # the run's own created_at..updated_at, D4's shape

    def test_run_queue_s_defaults_to_zero_with_no_job_wait(self):
        ev = self.fetch_one([{'name': 'a', 'conclusion': 'success', 'runner_name': 'box1',
                              'created_at': None, 'started_at': '2026-09-21T01:01:00Z',
                              'completed_at': '2026-09-21T01:05:00Z', 'failed': []}])
        self.assertEqual(ev['queue_s'], 0)


class JobCauseImport(Base):
    RUNS = [{'id': 10, 'name': 'ci', 'head_branch': 'cloud/x', 'head_sha': 'a' * 40, 'conclusion': 'cancelled',
             'created_at': '2026-09-21T01:00:00Z', 'updated_at': '2026-09-21T01:30:00Z', 'run_attempt': 1, 'pr': []},
            {'id': 11, 'name': 'ci', 'head_branch': 'cloud/y', 'head_sha': 'b' * 40, 'conclusion': 'cancelled',
             'created_at': '2026-09-21T02:00:00Z', 'updated_at': '2026-09-21T02:01:00Z', 'run_attempt': 1, 'pr': []}]
    JOBS = {
        10: [{'name': 'tests (3.12)', 'conclusion': 'cancelled', 'runner_name': 'box1',
              'started_at': '2026-09-21T01:00:00Z', 'completed_at': '2026-09-21T01:30:00Z', 'failed': []},
             {'name': 'tests (3.13)', 'conclusion': 'success', 'runner_name': 'box2',
              'started_at': '2026-09-21T01:00:00Z', 'completed_at': '2026-09-21T01:05:00Z', 'failed': []}],
        11: [{'name': 'tests (3.12)', 'conclusion': 'cancelled', 'runner_name': 'box1',
              'started_at': '2026-09-21T02:00:00Z', 'completed_at': '2026-09-21T02:01:00Z', 'failed': []},
             {'name': 'tests (3.13)', 'conclusion': 'cancelled', 'runner_name': 'box2',
              'started_at': '2026-09-21T02:00:00Z', 'completed_at': '2026-09-21T02:01:00Z', 'failed': []}],
    }

    def fake_gh_lines_answered(self, args, timeout=300):
        joined = ' '.join(args)
        if '/jobs' in joined:
            run = int(joined.split('/runs/')[1].split('/')[0])
            return self.JOBS[run], True
        return self.RUNS, True

    def events(self, limits):
        with mock.patch.object(metrics, 'gh_lines_answered', side_effect=self.fake_gh_lines_answered):
            return {e['run']: e for e in metrics.ci_from_api(
                2, 'ci', {}, self.items, repo_slug='sample/sample', limits=limits)}

    def test_a_job_at_its_limit_is_timeout_its_green_sibling_is_not(self):
        evs = self.events({'tests': 30})
        js = {j['name']: j for j in evs[10]['jobs']}
        self.assertEqual((js['tests (3.12)']['limit'], js['tests (3.12)']['cause']), (30, 'timeout'))
        self.assertEqual((js['tests (3.13)']['limit'], js['tests (3.13)']['cause']), (30, None))

    def test_a_run_wide_cancel_is_failure_on_every_leg(self):
        evs = self.events({'tests': 30})
        js = {j['name']: j for j in evs[11]['jobs']}
        self.assertEqual(js['tests (3.12)']['cause'], 'failure')
        self.assertEqual(js['tests (3.13)']['cause'], 'failure')

    def test_no_limit_known_claims_nothing(self):
        # C4: with limits == {}, the cancelled leg beside a green sibling has no limit to have
        # run to, so it reads failure rather than runner-loss.
        evs = self.events({})
        js = {j['name']: j for j in evs[10]['jobs']}
        self.assertIsNone(js['tests (3.12)']['limit'])
        self.assertEqual(js['tests (3.12)']['cause'], 'failure')

    def test_validate_fills_limit_and_cause_with_none(self):
        self.assertNotIn('limit', job('x'))             # the fixture helper carries neither key
        self.assertNotIn('cause', job('x'))
        ev = metrics.validate('ci', ci_event(99), self.items)
        for j in ev['jobs']:
            self.assertIsNone(j['limit'])
            self.assertIsNone(j['cause'])
        with self.assertRaises(metrics.SchemaError):
            metrics.validate('ci', ci_event(99, jobs=[dict(job('x'), bogus=1)]), self.items)


class Append(Base):
    def test_cli_appends_and_is_idempotent(self):
        payload = json.dumps(ci_event(77))
        rc, out, _ = run_cli(self.root, 'append', 'ci', payload)
        self.assertEqual(rc, 0)
        self.assertIn('appended to metrics/ci/2026-09-21.jsonl', out)
        rc, out, _ = run_cli(self.root, 'append', 'ci', payload)
        self.assertEqual(rc, 0)
        self.assertIn('no-op: already in metrics/ci/2026-09-21.jsonl', out)
        text = self.read('metrics/ci/2026-09-21.jsonl')
        self.assertEqual(len(text.splitlines()), 1)
        # a second attempt of the same run is a different event
        run_cli(self.root, 'append', 'ci', json.dumps(ci_event(77, attempt=2)))
        self.assertEqual(len(self.read('metrics/ci/2026-09-21.jsonl').splitlines()), 2)

    def test_line_format_sorted_keys_no_trailing_space(self):
        run_cli(self.root, 'append', 'ci', json.dumps(ci_event(5)))
        line = self.read('metrics/ci/2026-09-21.jsonl')
        self.assertTrue(line.endswith('\n'))
        for l in line.splitlines():
            self.assertEqual(l, l.rstrip())
            obj = json.loads(l)
            self.assertEqual(list(obj), sorted(obj))
            self.assertEqual(list(obj['jobs'][0]), sorted(obj['jobs'][0]))

    def test_the_file_is_chosen_by_ts_and_created(self):
        run_cli(self.root, 'append', 'ci', json.dumps(ci_event(5, ts='2026-09-19T23:59:59Z')))
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'metrics/ci/2026-09-19.jsonl')))

    def test_natural_keys(self):
        s = {'task': 'fix-x-r1', 'account': 'accta', 'result': 'done', 'ts': f'{DAY}T01:00:00Z'}
        t = {'tick': 12, 'launches': 1, 'merges': 0, 'stalls': 0, 'refusals': 0, 'relaunches': 0}
        self.assertEqual(self.put('sessions', s)[0], 'appended')
        self.assertEqual(self.put('sessions', s)[0], 'exists')
        self.assertEqual(self.put('sessions', dict(s, ts=f'{DAY}T02:00:00Z'))[0], 'appended')
        self.assertEqual(self.put('ticks', dict(t, ts=f'{DAY}T01:00:00Z'))[0], 'appended')
        self.assertEqual(self.put('ticks', dict(t, ts=f'{DAY}T01:00:00Z', launches=9))[0], 'exists')

    def test_schema_error_is_exit_2_with_the_reason(self):
        rc, out, err = run_cli(self.root, 'append', 'ci', json.dumps({'run': 1}))
        self.assertEqual(rc, 2)
        self.assertIn("missing required key", err)
        self.assertFalse(os.listdir(os.path.join(self.root, 'metrics', 'ci')) if os.path.isdir(os.path.join(self.root, 'metrics', 'ci')) else False)
        rc, _o, err = run_cli(self.root, 'append', 'ci', 'not json')
        self.assertEqual(rc, 2)
        self.assertIn('not JSON', err)

    def test_stdin_appends_every_line_and_reports_bad_ones(self):
        data = '\n'.join([json.dumps(ci_event(1)), json.dumps({'run': 2}), json.dumps(ci_event(3))]) + '\n'
        with mock.patch('sys.stdin', io.StringIO(data)):
            rc, out, err = run_cli(self.root, 'append', 'ci', '--stdin')
        self.assertEqual(rc, 2)
        self.assertIn('line 2:', err)
        self.assertEqual(len(self.read('metrics/ci/2026-09-21.jsonl').splitlines()), 2)


class SessionUpdateTests(Base):
    """A corrected `sessions` verdict reaches the stream (§2.5): `append_event`'s third status."""

    def session(self, **kw):
        return dict({'task': 'fix-free-plan-t3-r1', 'account': 'accta', 'ts': f'{DAY}T09:00:00Z',
                     'result': 'failed: not pushed: 3 uncommitted file(s), 0 unpushed commit(s)',
                     'reason': 'original reason', 'usd': 1.5, 'minutes': 12.0,
                     'tokens_input': 100, 'tokens_output': 50, 'tokens_cache_read': 10,
                     'tokens_cache_write': 5}, **kw)

    def test_a_first_event_is_appended(self):
        self.assertEqual(self.put('sessions', self.session())[0], 'appended')

    def test_an_unchanged_result_is_exists_and_writes_no_byte(self):
        self.put('sessions', self.session())
        rel = 'metrics/sessions/2026-09-21.jsonl'
        before = self.read_bytes(rel)
        self.assertEqual(self.put('sessions', self.session())[0], 'exists')
        self.assertEqual(self.read_bytes(rel), before)

    def test_a_changed_result_is_updated_and_leaves_the_owned_fields_alone(self):
        self.put('sessions', self.session())
        status, path = self.put('sessions', self.session(
            result='finished', reason=None, usd=None, minutes=None,
            tokens_input=None, tokens_output=None, tokens_cache_read=None, tokens_cache_write=None))
        self.assertEqual(status, 'updated')
        lines = [json.loads(l) for l in self.read('metrics/sessions/2026-09-21.jsonl').splitlines()]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line['result'], 'finished')
        self.assertEqual((line['reason'], line['usd'], line['minutes']), ('original reason', 1.5, 12.0))
        self.assertEqual((line['tokens_input'], line['tokens_output'], line['tokens_cache_read'],
                          line['tokens_cache_write']), (100, 50, 10, 5))

    def test_every_other_stream_stays_exists_only(self):
        cases = (
            ('ci', ci_event(1)),
            ('ticks', {'tick': 1, 'ts': f'{DAY}T08:00:00Z', 'launches': 3, 'merges': 1, 'stalls': 0,
                      'refusals': 1, 'relaunches': 1}),
            ('landings', {'ts': f'{DAY}T05:00:00Z', 'job': 'code-t-0001', 'sha': 'a' * 40,
                         'branch': 'worker/T-0001', 'kind': 'code'}),
            ('gates', {'ts': f'{DAY}T06:41:12Z', 'seconds': 412.7, 'branches': ['spec/F-0100'],
                      'conclusion': 'failure'}),
        )
        for stream, ev in cases:
            self.put(stream, ev)
            rel = f'metrics/{stream}/2026-09-21.jsonl'
            before = self.read_bytes(rel)
            self.assertEqual(self.put(stream, ev)[0], 'exists')
            self.assertEqual(self.read_bytes(rel), before, stream)

    def test_a_rewrite_keeps_the_other_lines_order_and_bytes(self):
        self.put('sessions', self.session(task='a', ts=f'{DAY}T08:00:00Z'))
        self.put('sessions', self.session(task='b', ts=f'{DAY}T09:00:00Z'))
        self.put('sessions', self.session(task='c', ts=f'{DAY}T10:00:00Z'))
        before = self.read('metrics/sessions/2026-09-21.jsonl').splitlines()
        self.put('sessions', self.session(task='b', ts=f'{DAY}T09:00:00Z', result='finished'))
        after = self.read('metrics/sessions/2026-09-21.jsonl').splitlines()
        self.assertEqual(len(after), 3)
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[2], before[2])
        self.assertNotEqual(after[1], before[1])
        self.assertEqual(json.loads(after[1])['task'], 'b')
        self.assertEqual(json.loads(after[1])['result'], 'finished')

    def test_cmd_append_reports_updated(self):
        run_cli(self.root, 'append', 'sessions', json.dumps(self.session()))
        rc, out, _err = run_cli(self.root, 'append', 'sessions', json.dumps(self.session(result='finished')))
        self.assertEqual(rc, 0)
        self.assertIn('updated metrics/sessions/2026-09-21.jsonl', out)

    def test_a_rejudged_run_is_no_longer_counted_against_its_own_class(self):
        """The end-to-end case: a run backfilled `failed: not pushed`, then re-judged `finished`,
        leaves a stream the scorecard counts as zero runs in that class (F-0094)."""
        home = tempfile.mkdtemp(prefix='session_update_home_')
        self.addCleanup(shutil.rmtree, home, True)
        state = os.path.join(home, 'state', 'sample')
        os.makedirs(state)
        state_path = os.path.join(state, 'sessions.jsonl')
        product = env.Product('sample', {})

        def write_registry(end_reason):
            with open(state_path, 'w', encoding='utf-8') as f:
                f.write(json.dumps({'job': 'fix-free-plan-t3-r1', 'item': 'T-0001', 'kind': 'fix',
                                    'account': 'accta', 'started': f'{DAY}T09:00:00Z'}) + '\n')
                f.write(json.dumps({'job': 'fix-free-plan-t3-r1', 'ended': f'{DAY}T09:20:00Z',
                                    'end_reason': end_reason}) + '\n')

        write_registry('failed: not pushed: 0 uncommitted file(s), 0 unpushed commit(s)')
        with mock.patch.object(env, 'ASF_HOME', home):
            evs = metrics.sessions_from_registry(product, since_day='2026-09-20', items=self.items,
                                                 state_path=state_path)
        self.assertEqual(self.put('sessions', evs[0])[0], 'appended')

        write_registry('finished')
        with mock.patch.object(env, 'ASF_HOME', home):
            evs = metrics.sessions_from_registry(product, since_day='2026-09-20', items=self.items,
                                                 state_path=state_path)
        status, _path = self.put('sessions', evs[0])
        self.assertEqual(status, 'updated')

        lines = metrics.read_stream(self.root, 'sessions', days=['2026-09-21'])
        self.assertEqual(len(lines), 1)
        self.assertEqual(score.failure_class(lines[0]['result']), 'finished')


def fixture_streams(test):
    """A day of events: two ci runs (one red with a cancelled job), three sessions, two ticks."""
    test.put('ci', ci_event(1, ts=f'{DAY}T09:00:00Z', minutes=10, conclusion='failure',
                            jobs=[job('lint', minutes=4, runner='box1'), job('e2e', 'failure', 6, 'box2', 'Run playwright tests')]))
    test.put('ci', ci_event(2, ts=f'{DAY}T10:00:00Z', branch='worktree-m-batch-20260921-0900',
                            batch='worktree-m-batch-20260921-0900', conclusion='cancelled', minutes=20, cancelled_minutes=12,
                            superseded=True, items=['F-0001', 'B-0001'],
                            jobs=[job('e2e', 'cancelled', 12, 'box2', cause='failure'), job('lint', minutes=8, runner='box1')]))
    test.put('ci', ci_event(3, ts=f'{DAY}T11:00:00Z', attempt=2, branch='main', minutes=10, items=None, item_reason='nothing to match on',
                            jobs=[job('lint', minutes=10)]))
    test.put('sessions', {'task': 'spec-free-plan', 'account': 'accta', 'result': 'done', 'ts': f'{DAY}T08:00:00Z', 'usd': 2.5})
    test.put('sessions', {'task': 'review-spec-free-plan-r2', 'account': 'acctb', 'result': 'done', 'ts': f'{DAY}T08:30:00Z',
                          'branch': 'cloud/spec-free-plan'})
    test.put('sessions', {'task': 'fix-free-plan-t3-r1', 'account': 'accta', 'result': 'done', 'ts': f'{DAY}T09:30:00Z', 'usd': 1.25})
    test.put('sessions', {'task': 'mystery-x', 'account': 'acctc', 'result': 'failed', 'ts': f'{DAY}T09:45:00Z'})
    test.put('ticks', {'tick': 1, 'ts': f'{DAY}T08:00:00Z', 'launches': 3, 'merges': 1, 'stalls': 0, 'refusals': 1,
                       'relaunches': 1, 'quota': {'accta': {'h5': 10, 'd7': 20}, 'acctb': {'h5': 5, 'd7': 6}},
                       'refused_files': {'apps/web/x.ts': 1}})
    test.put('ticks', {'tick': 2, 'ts': f'{DAY}T09:00:00Z', 'launches': 3, 'merges': 2, 'stalls': 1, 'refusals': 0,
                       'relaunches': 0, 'quota': {'accta': {'h5': 12, 'd7': 21}}})


EXPECTED_DAILY = """# Factory scorecard 2026-09-21

generated: 2026-09-21 — from metrics/ci (3), metrics/sessions (4), metrics/ticks (2)

## Waste

| Metric | Value | Note |
|---|---|---|
| landing | — | no product resolved — the session registry is machine-local |
| relaunch | — | no product resolved — the session registry is machine-local |
| ci runs | 3 — 1 green, 1 red, 1 reruns | red rate 33 % |
| runner-minutes | 40 (0 h) — useful 70 % | cancelled: batch 12 (30 %) — 30 % of the minutes |
| cancelled: failure | 12 min in 1 job (30 %) | e2e ×1 |
| red job: e2e | 1/2 (50 %) | |
| red signature | 1× | e2e: Run playwright tests |
| batches | 1 cut, 1 refused | refused on: apps/web/x.ts ×1 |
| agents | 6 launches in 2 ticks, 1 relaunches | 3 PRs merged → 2.0 launches per merged PR |
| tokens | in — · out — · cache rd — · cache wr — | 4 sessions, 0 capped |
| spec quality | 1 specs reviewed, 0 in round 1 | mean 2.0 rounds, median 2 |
| bandwidth | quota 5h/7d: accta 12/21 %, acctb 5/6 % | 2 runners seen, 40 runner-minutes |
| ci cancelled minutes | 40 runner-minutes (7d) — under the 60-minute floor | target 15 % — no verdict; today — %; batch 12 |
| ci red rate | 3 runs (7d) — under the 20-run floor | target 15 % — no verdict; today — %; e2e 1/3 |

## Cost per Feature (7 days)

2026-09-15 … 2026-09-21; a Feature's row sums its Tasks, Stories and Bugs; a CI run's minutes are split over the items it names; tokens are four separate numbers and are never added together.

| Feature | Title | Sessions | Fix rounds | Runner-min | USD | In | Out | Cache rd | Cache wr |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| F-0001 | Free plan | 3 | 1 | 20 | 3.75 | — | — | — | — |
| F-0002 | Other feature | 0 | 0 | 10 | — | — | — | — | — |
| (no Feature) | unattributed or outside a Feature | 1 | 0 | 10 | — | — | — | — | — |
| **All** | | 4 | 1 | 40 | 3.75 | — | — | — | — |
"""


def _at(minute):
    return f"{DAY}T{9 + minute // 60:02d}:{minute % 60:02d}:00Z"


def intake_events():
    """Five cards filed at 09:00; A-D decided 10/20/30/40 min later; A, B, D launched 4/8/12 min after
    that, C never; a launch of D before it was decided must not count."""
    evs = []
    for i in 'ABCDE':
        evs.append({'kind': 'intake', 'item': f'T-{i}', 'filed': _at(0), 'ts': _at(3)})
    for i, m in zip('ABCD', (10, 20, 30, 40)):
        evs.append({'kind': 'groom_answer', 'item': f'T-{i}', 'field': 'decided', 'value': 'true', 'ts': _at(m)})
    evs.append({'kind': 'groom_answer', 'item': 'T-E', 'field': 'priority', 'value': 'true', 'ts': _at(15)})
    evs.append({'kind': 'launch', 'item': 'T-D', 'ts': _at(35)})
    for i, m in zip('ABD', (14, 28, 52)):
        evs.append({'kind': 'launch', 'item': f'T-{i}', 'ts': _at(m)})
    return evs


class IntakeLatencyTests(Base):
    def test_median_p90_and_pair_counts(self):
        (_, a, na), (_, b, nb) = metrics.intake_latency_rows(intake_events())
        self.assertEqual(a, 'median 25 min, p90 40 min')
        self.assertEqual(na, '4 of 5 cards measured (1 not decided by a groom answer)')
        self.assertEqual(b, 'median 8 min, p90 12 min')
        self.assertEqual(nb, '3 of 4 decided cards launched')

    def test_empty_stream_divides_by_nothing(self):
        rows = metrics.intake_latency_rows([])
        self.assertEqual([r[1] for r in rows], ['no pairs measured'] * 2)

    def test_render_daily_renders_both_rows(self):
        os.makedirs(os.path.join(self.root, 'metrics', 'events'), exist_ok=True)
        with open(os.path.join(self.root, 'metrics', 'events', f'{DAY}.jsonl'), 'w', encoding='utf-8') as f:
            for ev in intake_events():
                f.write(json.dumps(ev) + '\n')
        md = metrics.render_daily(self.root, DAY, self.items)
        self.assertIn('| intake → decided | median 25 min, p90 40 min | 4 of 5 cards measured', md)
        self.assertIn('| decided → first session | median 8 min, p90 12 min | 3 of 4 decided cards launched |', md)

    def test_no_events_argument_changes_nothing(self):
        fixture_streams(self)
        args = (metrics.read_stream(self.root, 'ci'), metrics.read_stream(self.root, 'sessions'),
                metrics.read_stream(self.root, 'ticks'))
        self.assertEqual(metrics.scorecard_rows(*args), metrics.scorecard_rows(*args, events=()))
        self.assertFalse(any(r[0].startswith('intake') for r in metrics.scorecard_rows(*args)))


class LandingRowTests(Base):
    def test_landing_row_leads_and_carries_both_numbers(self):
        rows = metrics.scorecard_rows([], [], [], landing=(0.35, 3.6, 86))
        self.assertEqual(rows[0], ('landing', '35 % of session time landed nothing',
                                    '$3.60 per landed item (86 landed, 7 days)'))

    def test_no_landed_items_reads_dash_for_the_usd(self):
        rows = metrics.scorecard_rows([], [], [], landing=(0.0, None, 0))
        self.assertEqual(rows[0], ('landing', '0 % of session time landed nothing',
                                    '— per landed item (0 landed, 7 days)'))

    def test_none_reads_dash_with_its_note(self):
        rows = metrics.scorecard_rows([], [], [])
        self.assertEqual(rows[0], ('landing', '—', 'no product resolved — the session registry is machine-local'))

    def test_render_daily_with_no_product_never_touches_measure(self):
        with mock.patch('asf.improve.measure.ended_runs', side_effect=AssertionError('should not be called')):
            md = metrics.render_daily(self.root, DAY, self.items)
        self.assertIn('| landing | — | no product resolved — the session registry is machine-local |', md)

    def test_render_daily_with_a_product_computes_landing(self):
        home = tempfile.mkdtemp(prefix='metrics_test_home_')
        self.addCleanup(shutil.rmtree, home, True)
        state = os.path.join(home, 'state', 'sample')
        os.makedirs(state)
        open(os.path.join(state, 'sessions.jsonl'), 'w').close()
        with mock.patch.object(env, 'ASF_HOME', home), \
                mock.patch('asf.improve.measure.ended_runs', return_value=['a run']) as ended_runs, \
                mock.patch('asf.improve.measure.table',
                            return_value={'non_landing_share': 0.35, 'usd_per_landed_item': 3.6, 'landed_items': 86}):
            md = metrics.render_daily(self.root, DAY, self.items, product='sample')
        ended_runs.assert_called_once_with('sample', since='2026-09-15', as_of=f'{DAY}T23:59:59Z')
        self.assertIn('| landing | 35 % of session time landed nothing | $3.60 per landed item (86 landed, 7 days) |', md)

    def test_a_missing_registry_renders_the_dash_form_not_an_error(self):
        home = tempfile.mkdtemp(prefix='metrics_test_home_')
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.object(env, 'ASF_HOME', home), \
                mock.patch('asf.improve.measure.ended_runs', side_effect=AssertionError('should not be called')):
            md = metrics.render_daily(self.root, DAY, self.items, product='sample')
        self.assertIn('| landing | — | no product resolved — the session registry is machine-local |', md)


class ScorecardRelaunchRowTests(Base):
    RELAUNCHES = {'sessions': 7, 'share': 0.226, 'usd_per_session': 2.1, 'hours_per_session': 1.5,
                  'first_usd_per_session': 3.4, 'first_hours_per_session': 2.0}

    def test_relaunch_row_follows_landing_and_carries_both_numbers(self):
        rows = metrics.scorecard_rows([], [], [], landing=(0.35, 3.6, 86), relaunches=self.RELAUNCHES)
        self.assertEqual(rows[1], ('relaunch', '7 sessions were relaunches (23 %)',
                                    '$2.10 per relaunch vs $3.40 per first launch (7 days)'))

    def test_a_side_with_no_sessions_reads_dash_not_zero(self):
        relaunches = dict(self.RELAUNCHES, usd_per_session=None, hours_per_session=None)
        rows = metrics.scorecard_rows([], [], [], relaunches=relaunches)
        self.assertEqual(rows[1], ('relaunch', '7 sessions were relaunches (23 %)',
                                    '— per relaunch vs $3.40 per first launch (7 days)'))

    def test_none_reads_dash_with_its_note(self):
        rows = metrics.scorecard_rows([], [], [])
        self.assertEqual(rows[1], ('relaunch', '—', 'no product resolved — the session registry is machine-local'))

    def test_render_daily_with_no_product_never_touches_measure(self):
        with mock.patch('asf.improve.measure.ended_runs', side_effect=AssertionError('should not be called')):
            md = metrics.render_daily(self.root, DAY, self.items)
        self.assertIn('| relaunch | — | no product resolved — the session registry is machine-local |', md)

    def test_render_daily_computes_the_row_over_two_windows(self):
        # "two windows of that row" (§2.6's third acceptance) — two render_daily calls, each
        # reading a different `measure.table` result, land two different relaunch rows. The
        # direction between them is not asserted; a test cannot make a week pass.
        home = tempfile.mkdtemp(prefix='metrics_test_home_')
        self.addCleanup(shutil.rmtree, home, True)
        state = os.path.join(home, 'state', 'sample')
        os.makedirs(state)
        open(os.path.join(state, 'sessions.jsonl'), 'w').close()
        windows = [
            {'non_landing_share': 0.35, 'usd_per_landed_item': 3.6, 'landed_items': 86, 'relaunches': self.RELAUNCHES},
            {'non_landing_share': 0.40, 'usd_per_landed_item': 4.0, 'landed_items': 90,
             'relaunches': {'sessions': 9, 'share': 0.28, 'usd_per_session': 2.5, 'hours_per_session': 1.6,
                            'first_usd_per_session': 3.1, 'first_hours_per_session': 1.9}},
        ]
        with mock.patch.object(env, 'ASF_HOME', home), \
                mock.patch('asf.improve.measure.ended_runs', return_value=['a run']), \
                mock.patch('asf.improve.measure.table', side_effect=windows):
            first = metrics.render_daily(self.root, DAY, self.items, product='sample')
            second = metrics.render_daily(self.root, DAY, self.items, product='sample')
        self.assertIn('| relaunch | 7 sessions were relaunches (23 %) | $2.10 per relaunch vs $3.40 per first launch (7 days) |', first)
        self.assertIn('| relaunch | 9 sessions were relaunches (28 %) | $2.50 per relaunch vs $3.10 per first launch (7 days) |', second)

    def test_a_missing_registry_renders_the_dash_form_not_an_error(self):
        home = tempfile.mkdtemp(prefix='metrics_test_home_')
        self.addCleanup(shutil.rmtree, home, True)
        with mock.patch.object(env, 'ASF_HOME', home), \
                mock.patch('asf.improve.measure.ended_runs', side_effect=AssertionError('should not be called')):
            md = metrics.render_daily(self.root, DAY, self.items, product='sample')
        self.assertIn('| relaunch | — | no product resolved — the session registry is machine-local |', md)


class Rollup(Base):
    def test_daily_tables_match_the_expected_md(self):
        fixture_streams(self)
        self.assertEqual(metrics.render_daily(self.root, DAY, self.items), EXPECTED_DAILY)

    def test_empty_day_renders(self):
        md = metrics.render_daily(self.root, '2026-01-01', self.items)
        self.assertIn('| ci runs | 0 — 0 green, 0 red, 0 reruns | red rate 0 % |', md)
        self.assertIn('| **All** | | 0 | 0 | 0 | — |', md)

    def test_cost_sums_through_children(self):
        costs = metrics.compute_costs(
            [{'minutes': 30, 'items': ['T-0002', 'F-0001']}, {'minutes': 9, 'items': ['B-0001']}, {'minutes': 5, 'items': None}],
            [{'item': 'T-0002', 'kind': 'fix', 'usd': 1.5}, {'item': 'S-0001', 'kind': 'code', 'usd': None},
             {'item': 'F-0001', 'kind': 'spec', 'usd': 2.0}, {'item': None, 'kind': 'code', 'usd': 9.0}])
        f1 = metrics.subtree_cost(self.items, costs, 'F-0001')
        self.assertEqual((f1['sessions'], f1['fix_rounds'], f1['runner_min'], f1['usd']), (3, 1, 30.0, 3.5))
        f2 = metrics.subtree_cost(self.items, costs, 'F-0002')
        self.assertEqual((f2['sessions'], f2['runner_min'], f2['usd']), (0, 9.0, None))
        e = metrics.subtree_cost(self.items, costs, 'E-0001')
        self.assertEqual((e['sessions'], e['runner_min'], e['usd']), (3, 39.0, 3.5))

    def test_item_cost_and_epic_spend_are_written(self):
        fixture_streams(self)
        changed, spend = metrics.write_costs(self.root, self.items, metrics.read_stream(self.root, 'ci'),
                                             metrics.read_stream(self.root, 'sessions'),
                                             now=dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(sorted(changed), ['B-0001', 'E-0001', 'F-0001', 'T-0001'])
        idx = self.reindexed()
        self.assertEqual(idx['T-0001']['cost'], {'sessions': 1, 'fix_rounds': 1, 'runner_min': 10, 'usd': 1.25})
        self.assertEqual(idx['F-0001']['cost'], {'sessions': 2, 'fix_rounds': 0, 'runner_min': 10, 'usd': 2.5})
        self.assertEqual(idx['B-0001']['cost'], {'sessions': 0, 'fix_rounds': 0, 'runner_min': 10, 'usd': None})
        self.assertEqual(idx['F-0001']['updated'], '2026-09-21T12:00:00Z')
        self.assertEqual(idx['E-0001']['spend_usd'], 3.75)
        self.assertEqual(idx['E-0001']['budget_usd'], 100)
        self.assertEqual(spend['E-0001'], (3.75, 100))
        self.assertNotIn('cost', idx['F-0002'])

    def reindexed(self):
        reindex(self.root)
        return match.load_index(self.root)

    def test_write_machine_leaves_typed_lines_byte_identical(self):
        fixture_streams(self)
        paths = {i: os.path.join(self.root, 'features' if i.startswith('F') else 'tasks', f'{i}.md') for i in ('F-0001', 'T-0001')}

        def typed(p):
            with open(p, encoding='utf-8') as f:
                text = f.read()
            head, _sep, _rest = text.partition(frontmatter.MARKER)
            return head.encode()

        before = {i: typed(p) for i, p in paths.items()}
        metrics.write_costs(self.root, self.items, metrics.read_stream(self.root, 'ci'),
                            metrics.read_stream(self.root, 'sessions'))
        for i, p in paths.items():
            self.assertEqual(typed(p), before[i], i)
            with open(p, encoding='utf-8') as f:
                self.assertIn('cost: {sessions:', f.read())
        with open(paths['T-0001'], encoding='utf-8') as f:
            self.assertIn('cost: {sessions: 1, fix_rounds: 1, runner_min: 10, usd: 1.25}', f.read())
        with open(os.path.join(self.root, 'features', 'F-0002.md'), encoding='utf-8') as f:
            self.assertNotIn('cost:', f.read())

    def test_null_usd_round_trips(self):
        fixture_streams(self)
        metrics.write_costs(self.root, self.items, metrics.read_stream(self.root, 'ci'), metrics.read_stream(self.root, 'sessions'))
        with open(os.path.join(self.root, 'bugs', 'B-0001.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertIn('cost: {sessions: 0, fix_rounds: 0, runner_min: 10, usd: null}', text)
        meta, _b = frontmatter.parse(text)
        self.assertIsNone(meta['cost']['usd'])

    def test_rollup_twice_changes_nothing_and_check_passes(self):
        fixture_streams(self)
        with mock.patch.object(metrics, 'deploy_runs', return_value=[]):
            rc, out, _ = run_cli(self.root, 'rollup', DAY)
            self.assertEqual(rc, 0, out)
            snap = self.snapshot()
            rc, out, _ = run_cli(self.root, 'rollup', DAY)
        self.assertEqual(rc, 0)
        self.assertIn('unchanged metrics/daily/2026-09-21.md', out)
        self.assertIn('cost: 0 item(s) updated', out)
        self.assertEqual(self.snapshot(), snap)
        self.assertIn('budget: E-0001 Billing: spend $3.75 of budget $100 (4 %)', out)
        check_out = io.StringIO()
        with contextlib.redirect_stdout(check_out):
            check_rc = cmd_check(types.SimpleNamespace(paths=None), self.root)
        self.assertEqual(check_rc, 0, check_out.getvalue())

    def snapshot(self):
        snap = {}
        for base, _d, files in os.walk(self.root):
            for f in files:
                p = os.path.join(base, f)
                with open(p, 'rb') as fh:
                    snap[os.path.relpath(p, self.root)] = fh.read()
        return snap


def waste_ci(n, red, minutes_total=0, cancelled_minutes=0):
    """`n` hand-built `ci` events (no schema, `ci_waste_numbers` reads with `.get`): the first
    carries `minutes_total` and `cancelled_minutes`, the rest zero; the first `red` of them
    conclude `failure`, the rest `success`."""
    rows = [{'minutes': 0, 'cancelled_minutes': 0, 'conclusion': 'failure' if i < red else 'success', 'jobs': []}
            for i in range(n)]
    if rows:
        rows[0]['minutes'] = minutes_total
        rows[0]['cancelled_minutes'] = cancelled_minutes
    return rows


class CiWasteNumberTests(unittest.TestCase):
    def test_scorecard_order_and_both_over_target(self):
        ci = waste_ci(100, 21, 4210, 760)
        cancelled, red = metrics.ci_waste_numbers(ci)
        self.assertEqual([n['key'] for n in metrics.ci_waste_numbers(ci)], ['cancelled', 'red'])
        self.assertEqual((cancelled['num'], cancelled['den'], cancelled['pct'], cancelled['verdict']),
                         (760, 4210, 18, 'over'))
        self.assertEqual((red['num'], red['den'], red['pct'], red['verdict']), (21, 100, 21, 'over'))

    def test_both_ok_at_a_higher_target(self):
        conv = Conventions(ci_cancelled_target_pct=25, ci_red_target_pct=25)
        cancelled, red = metrics.ci_waste_numbers(waste_ci(100, 21, 4210, 760), conv)
        self.assertEqual(cancelled['verdict'], 'ok')
        self.assertEqual(red['verdict'], 'ok')

    def test_a_number_exactly_at_its_target_is_over(self):
        cancelled, red = metrics.ci_waste_numbers(waste_ci(100, 15, 100, 15))
        self.assertEqual((cancelled['pct'], cancelled['verdict']), (15, 'over'))
        self.assertEqual((red['pct'], red['verdict']), (15, 'over'))

    def test_den_under_the_floor_is_no_verdict(self):
        cancelled, _red = metrics.ci_waste_numbers(waste_ci(1, 0, 59, 10))
        self.assertEqual((cancelled['den'], cancelled['pct'], cancelled['verdict']), (59, None, 'no verdict'))

    def test_empty_list_is_den_zero_same_verdict_both(self):
        numbers = metrics.ci_waste_numbers([])
        self.assertEqual([(n['den'], n['pct'], n['verdict']) for n in numbers], [(0, None, 'no verdict')] * 2)

    def test_missing_cancelled_minutes_counts_as_zero_but_minutes_still_contribute(self):
        cancelled, _red = metrics.ci_waste_numbers([{'minutes': 10, 'conclusion': 'success', 'jobs': []}])
        self.assertEqual((cancelled['num'], cancelled['den']), (0, 10))

    def test_an_unparseable_ts_stays_in_the_totals_and_out_of_by_day(self):
        ci = [{'minutes': 10, 'cancelled_minutes': 5, 'conclusion': 'success', 'jobs': [], 'ts': 'not a timestamp'},
              {'minutes': 10, 'cancelled_minutes': 5, 'conclusion': 'success', 'jobs': [], 'ts': '2026-09-17T00:00:00Z'}]
        cancelled, _red = metrics.ci_waste_numbers(ci)
        self.assertEqual((cancelled['num'], cancelled['den']), (10, 20))
        self.assertEqual(cancelled['by_day'], [('2026-09-17', 5, 10)])

    def test_by_day_is_oldest_first_and_only_days_with_runs(self):
        ci = [{'minutes': 5, 'cancelled_minutes': 1, 'conclusion': 'success', 'jobs': [], 'ts': '2026-09-19T00:00:00Z'},
              {'minutes': 5, 'cancelled_minutes': 2, 'conclusion': 'success', 'jobs': [], 'ts': '2026-09-17T00:00:00Z'},
              {'minutes': 5, 'cancelled_minutes': 3, 'conclusion': 'success', 'jobs': [], 'ts': '2026-09-18T00:00:00Z'}]
        cancelled, _red = metrics.ci_waste_numbers(ci)
        self.assertEqual([d for d, _n, _dd in cancelled['by_day']], ['2026-09-17', '2026-09-18', '2026-09-19'])

    def test_split_is_largest_first_by_lane_for_cancelled(self):
        ci = [{'minutes': 1, 'cancelled_minutes': 20, 'conclusion': 'success', 'jobs': [], 'batch': 'm1'},
              {'minutes': 1, 'cancelled_minutes': 5, 'conclusion': 'success', 'jobs': [], 'branch': 'main'},
              {'minutes': 1, 'cancelled_minutes': 10, 'conclusion': 'success', 'jobs': [], 'branch': 'feature/x'}]
        cancelled, _red = metrics.ci_waste_numbers(ci)
        self.assertEqual([(k, v) for k, v, _p in cancelled['split']], [('batch', 20), ('branch', 10), ('trunk', 5)])

    def test_split_is_largest_first_capped_by_failing_job_for_red(self):
        ci = []
        for name, count in (('j1', 5), ('j2', 4), ('j3', 3), ('j4', 2), ('j5', 1)):
            ci.extend({'minutes': 1, 'jobs': [{'name': name, 'conclusion': 'failure'}]} for _ in range(count))
        _cancelled, red = metrics.ci_waste_numbers(ci)
        self.assertEqual([k for k, _v, _p in red['split']], ['j1', 'j2', 'j3', 'j4'])
        self.assertEqual(len(red['split']), metrics.CI_SPLIT_CAP)

    def test_a_non_numeric_target_yields_the_default_and_a_config_sentence(self):
        conv = Conventions(ci_cancelled_target_pct='fifteen')
        cancelled, _red = metrics.ci_waste_numbers(waste_ci(100, 0, 100, 20), conv)
        self.assertEqual(cancelled['target'], 15)
        self.assertEqual(cancelled['config'], "'fifteen' is not a number")
        self.assertEqual(cancelled['verdict'], 'over')

    def test_a_bool_target_is_not_read_as_the_integer_one(self):
        conv = Conventions(ci_cancelled_target_pct=True)
        cancelled, _red = metrics.ci_waste_numbers(waste_ci(100, 0, 100, 20), conv)
        self.assertEqual(cancelled['target'], 15)
        self.assertEqual(cancelled['config'], 'True is not a number')

    def test_pure_no_clock_no_filesystem_same_input_twice(self):
        ci = waste_ci(50, 10, 500, 60)
        self.assertEqual(metrics.ci_waste_numbers(ci), metrics.ci_waste_numbers(ci))


class CiTargetRowTests(Base):
    def put_ci(self, run, **kw):
        self.put('ci', ci_event(run, **kw))

    def test_two_rows_labelled_with_provider_week_value_differs_from_todays_note(self):
        conv = Conventions(ci_provider='gh-actions', ci_min_minutes=1, ci_min_runs=1)
        week = metrics.days_back(DAY, 7)
        run = 1
        for day in week[:-1]:
            self.put_ci(run, ts=f'{day}T09:00:00Z', minutes=100, cancelled_minutes=30, conclusion='failure',
                        branch='main', jobs=[job('e2e', 'failure', 100)])
            run += 1
        self.put_ci(run, ts=f'{DAY}T09:00:00Z', minutes=100, cancelled_minutes=0, conclusion='success',
                    branch='main', jobs=[job('e2e', 'success', 100)])
        rows = {r[0]: r for r in metrics.scorecard_rows(
            metrics.read_stream(self.root, 'ci', [DAY]), [], [], conv,
            ci7=metrics.read_stream(self.root, 'ci', week))}
        self.assertEqual(rows['ci cancelled minutes (gh-actions)'],
                         ('ci cancelled minutes (gh-actions)', '25 % of 700 runner-minutes (7d)',
                          'target 15 % — over; today 0 %; trunk 180'))
        self.assertEqual(rows['ci red rate (gh-actions)'],
                         ('ci red rate (gh-actions)', '85 % of 7 runs (7d)',
                          'target 15 % — over; today 0 %; e2e 6/7'))

    def test_under_target_is_ok(self):
        conv = Conventions(ci_provider='gh-actions')
        week = metrics.days_back(DAY, 7)
        run = 1
        for day in week:
            for _ in range(4):
                self.put_ci(run, ts=f'{day}T09:00:00Z', minutes=20, cancelled_minutes=0, conclusion='success',
                            branch='main', jobs=[job('e2e', 'success', 20)])
                run += 1
        md = metrics.render_daily(self.root, DAY, self.items, conv=conv)
        self.assertIn('| ci cancelled minutes (gh-actions) | 0 % of 560 runner-minutes (7d) | '
                      'target 15 % — ok; today 0 % |', md)
        self.assertIn('| ci red rate (gh-actions) | 0 % of 28 runs (7d) | '
                      'target 15 % — ok; today — % |', md)

    def test_den_under_the_floor_names_the_floor(self):
        conv = Conventions(ci_provider='gh-actions')
        self.put_ci(1, ts=f'{DAY}T09:00:00Z', minutes=10, cancelled_minutes=0, conclusion='success',
                    branch='main', jobs=[job('e2e', 'success', 10)])
        md = metrics.render_daily(self.root, DAY, self.items, conv=conv)
        self.assertIn('| ci cancelled minutes (gh-actions) | 10 runner-minutes (7d) — under the 60-minute floor | '
                      'target 15 % — no verdict; today — % |', md)
        self.assertIn('| ci red rate (gh-actions) | 1 runs (7d) — under the 20-run floor | '
                      'target 15 % — no verdict; today — % |', md)

    def test_no_runs_at_all_with_a_provider_prints_the_rows_anyway(self):
        conv = Conventions(ci_provider='gh-actions')
        md = metrics.render_daily(self.root, DAY, self.items, conv=conv)
        self.assertIn('| ci cancelled minutes (gh-actions) | no runs in 7 days | '
                      'target 15 % — no verdict; today — % |', md)
        self.assertIn('| ci red rate (gh-actions) | no runs in 7 days | '
                      'target 15 % — no verdict; today — % |', md)

    def test_no_runs_and_no_provider_skips_both_rows_and_the_rest_is_unchanged(self):
        md = metrics.render_daily(self.root, DAY, self.items)
        self.assertNotIn('ci cancelled minutes', md)
        self.assertNotIn('ci red rate', md)
        self.assertIn('| ci runs | 0 — 0 green, 0 red, 0 reruns | red rate 0 % |', md)
        self.assertIn('| **All** | | 0 | 0 | 0 | — |', md)

    def test_ci_runs_and_runner_minutes_rows_are_unchanged(self):
        fixture_streams(self)
        md = metrics.render_daily(self.root, DAY, self.items)
        self.assertIn('| ci runs | 3 — 1 green, 1 red, 1 reruns | red rate 33 % |', md)
        self.assertIn('| runner-minutes | 40 (0 h) — useful 70 % | cancelled: batch 12 (30 %) — 30 % of the minutes |', md)


DEPLOYS = [{'headSha': 'b' * 40, 'conclusion': 'success', 'updatedAt': '2026-09-21T05:00:00Z'},
           {'headSha': 'c' * 40, 'conclusion': 'failure', 'updatedAt': '2026-09-20T05:00:00Z'},
           {'headSha': 'd' * 40, 'conclusion': 'success', 'updatedAt': '2026-09-19T05:00:00Z'}]
PRS = {601: {'merged': True, 'merge_commit_sha': '1' * 40, 'body': 'Adds it\n\nTry it: open /billing as a free user\n', 'title': 'x'},
       623: {'merged': True, 'merge_commit_sha': '2' * 40, 'body': 'no try line', 'title': 'y'}}


def resolve_all(items):
    """Every Feature, Story, Task and Bug Resolved: a release that lists them as landed is
    honest only then (I12)."""
    for it in items.values():
        if it.get('type') in ('feature', 'story', 'task', 'bug'):
            it['state'] = 'Resolved'


class Releases(Base):
    def setUp(self):
        super().setUp()
        resolve_all(self.items)

    def run_release(self, anc, deploys=DEPLOYS, prs=PRS):
        with mock.patch.object(metrics, 'deploy_runs', return_value=deploys), \
                mock.patch.object(metrics, 'pr_info', side_effect=lambda n, *a, **kw: prs.get(n)), \
                mock.patch.object(metrics, 'is_ancestor', side_effect=lambda repo, c, of: (c, of) in anc):
            return metrics.write_release(self.root, DAY, self.items, repo='/nonexistent')

    def test_items_merged_since_the_previous_deploy_are_listed_by_type(self):
        anc = {('1' * 40, 'b' * 40), ('2' * 40, 'b' * 40), ('2' * 40, 'd' * 40)}   # 623 was already in the older deploy
        path = self.run_release(anc)
        self.assertEqual(os.path.basename(path), f'{DAY}-bbbbbbb.md')
        text = self.read(f'releases/{DAY}-bbbbbbb.md')
        self.assertIn('# Release 2026-09-21 · bbbbbbb', text)
        self.assertIn('Everything merged since `ddddddd`.', text)   # the failed deploy c… is not a baseline
        self.assertIn('## Features\n\n- [F-0001](../features/F-0001.md) Free plan — #601\n  - Try it: open /billing as a free user', text)
        self.assertNotIn('T-0001', text)

    def test_grouped_by_type_and_missing_try_it(self):
        anc = {('1' * 40, 'b' * 40), ('2' * 40, 'b' * 40)}
        self.run_release(anc)
        text = self.read(f'releases/{DAY}-bbbbbbb.md')
        self.assertLess(text.index('## Features'), text.index('## Tasks'))
        self.assertIn('- [T-0001](../tasks/T-0001.md) Plan door copy — #623\n', text)
        self.assertNotIn('Try it: no try', text)

    def test_existing_release_for_the_sha_is_left_alone(self):
        with open(os.path.join(self.root, 'releases', '2026-09-20-bbbbbbb.md'), 'w') as f:
            f.write('x')
        self.assertIsNone(self.run_release(set()))
        self.assertEqual(os.listdir(os.path.join(self.root, 'releases')), ['2026-09-20-bbbbbbb.md'])

    def test_previous_release_file_is_the_baseline(self):
        with open(os.path.join(self.root, 'releases', '2026-09-19-ddddddd.md'), 'w') as f:
            f.write('x')
        anc = {('1' * 40, 'b' * 40), ('2' * 40, 'b' * 40), ('1' * 40, 'ddddddd')}
        self.run_release(anc)
        text = self.read(f'releases/{DAY}-bbbbbbb.md')
        self.assertNotIn('F-0001', text)
        self.assertIn('T-0001', text)

    def test_empty_list_says_so(self):
        self.run_release(set())
        self.assertIn('No item reached prod in this release.', self.read(f'releases/{DAY}-bbbbbbb.md'))

    def test_unmerged_prs_and_no_successful_deploy(self):
        prs = {601: dict(PRS[601], merged=False), 623: None}
        self.run_release({('1' * 40, 'b' * 40)}, prs=prs)
        self.assertIn('No item reached prod', self.read(f'releases/{DAY}-bbbbbbb.md'))
        self.assertIsNone(self.run_release(set(), deploys=[DEPLOYS[1]]))

    def test_no_release_while_no_item_has_a_pr(self):
        no_prs = {i: {k: v for k, v in it.items() if k != 'links'} for i, it in self.items.items()}
        with mock.patch.object(metrics, 'deploy_runs', return_value=DEPLOYS):
            self.assertIsNone(metrics.write_release(self.root, DAY, no_prs, repo='/nonexistent'))
        self.assertEqual(os.listdir(os.path.join(self.root, 'releases')), [])

    def test_rollup_writes_the_release_once(self):
        anc = {('1' * 40, 'b' * 40)}
        with mock.patch.object(metrics, 'deploy_runs', return_value=DEPLOYS), \
                mock.patch.object(metrics, 'pr_info', side_effect=lambda n, *a, **kw: PRS.get(n)), \
                mock.patch.object(metrics, 'is_ancestor', side_effect=lambda repo, c, of: (c, of) in anc), \
                mock.patch.dict(os.environ, {'BACKLOG_PRODUCT_REPO': '/nonexistent'}):
            _rc, out, _ = run_cli(self.root, 'rollup', DAY)
            self.assertIn('release: releases/2026-09-21-bbbbbbb.md', out)
            _rc, out, _ = run_cli(self.root, 'rollup', DAY)
            self.assertIn('release: none new', out)


def _git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com',
                           '-c', 'core.hooksPath=/dev/null', *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class TrunkReleases(Base):
    """A product that deploys nothing ships its green trunk (B-0077): the rollup cuts the release
    itself — the note in the record, a `v<major>.<minor>.<patch>` tag on the product repo, the
    version's notes in its changelog."""

    def setUp(self):
        super().setUp()
        resolve_all(self.items)
        self.tmp = tempfile.mkdtemp(prefix='trunk_release_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, 'origin.git')
        self.repo = os.path.join(self.tmp, 'work')
        _git(self.tmp, 'init', '-q', '--bare', '-b', 'main', self.origin)
        _git(self.tmp, 'clone', '-q', self.origin, self.repo)
        os.makedirs(os.path.join(self.repo, 'pkg'))
        self.commit('pkg/__init__.py', '__version__ = "0.1.0"\n', 'init: the package')
        self.commit('pkg/a.py', 'a = 1\n', 'task(T-0001): plan door copy')
        self.product = env.Product('trunky', {
            'repo_dir': self.repo, 'repo_slug': 'x/y', 'main': 'main', 'ci': 'none',
            'conventions': {'version_file': 'pkg/__init__.py', 'version': {'cut': True}}})
        # the rollup's clock: each release() runs later than the last, past the default interval
        self.now = metrics.now_utc()
        clock = mock.patch.object(metrics, 'now_utc', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def commit(self, path, text, msg):
        if _git(self.origin, 'for-each-ref', 'refs/heads/main'):
            _git(self.repo, 'pull', '-q', '--rebase', 'origin', 'main')    # the rollup's changelog commits
        with open(os.path.join(self.repo, path), 'w') as f:
            f.write(text)
        _git(self.repo, 'add', path)
        _git(self.repo, 'commit', '-q', '-m', msg)
        _git(self.repo, 'push', '-q', 'origin', 'HEAD:main')
        return _git(self.repo, 'rev-parse', 'HEAD')

    def release(self, day=DAY, later=dt.timedelta(hours=2)):
        self.now += later
        with contextlib.redirect_stdout(io.StringIO()):
            return metrics.write_release(self.root, day, self.items, product=self.product)

    def origin_tags(self):
        out = _git(self.origin, 'for-each-ref', '--format=%(refname:short) %(*objectname)', 'refs/tags')
        return dict(line.split() for line in out.splitlines())

    def changelog(self):
        return _git(self.origin, 'show', 'main:CHANGELOG.md')

    def set_epics(self, *epics):
        """(id, title, state, rank) Epics in the index, in place of the fixture's one."""
        for iid, title, state, rank in epics:
            self.items[iid] = {'id': iid, 'type': 'epic', 'title': title, 'state': state, 'rank': rank,
                               'folder': 'epics'}

    def legacy_release(self, day, sha):
        """A release as the rollup cut it before versions: a `release-*` tag and a note naming it."""
        name = f'release-{day}-{sha[:7]}'
        _git(self.repo, 'tag', '-a', name, sha, '-m', f'Release {day} · {sha[:7]}')
        _git(self.repo, 'push', '-q', 'origin', f'refs/tags/{name}')
        with open(os.path.join(self.root, 'releases', f'{day}-{sha[:7]}.md'), 'w') as f:
            f.write(f'# Release {day} · {sha[:7]}\n\nTag `{name}`.\n')
        return name

    # ---- the rule -------------------------------------------------------------

    def test_an_epic_title_carries_a_version_or_none(self):
        self.assertEqual(metrics.epic_version('Product 0.1 — a working factory'), (0, 1))
        self.assertEqual(metrics.epic_version('Thing 12.3: faster'), (12, 3))
        self.assertIsNone(metrics.epic_version("Control plane — the factory's dashboard"))
        self.assertIsNone(metrics.epic_version('v2 dashboard'))

    def test_the_line_is_the_lowest_ranked_open_versioned_epic(self):
        self.set_epics(('E-0001', 'Billing', 'Active', 0),                 # unversioned: skipped
                       ('E-0002', 'Product 0.1 — a working factory', 'Closed', 1),
                       ('E-0003', 'Product 0.3 — autonomous', 'New', 3),
                       ('E-0004', 'Product 0.2 — self-improvement', 'Active', 2))
        self.assertEqual(metrics.roadmap_line(self.items), (0, 2))
        self.items['E-0004']['removed'] = 'not now'
        self.assertEqual(metrics.roadmap_line(self.items), (0, 3))
        self.assertIsNone(metrics.roadmap_line({'E-0001': self.items['E-0001']}))

    def test_the_next_patch_and_the_first_release_of_a_line(self):
        self.assertEqual(metrics.next_version(set(), (0, 1)), (0, 1, 0))
        self.assertEqual(metrics.next_version({(0, 1, 0), (0, 1, 1)}, (0, 1)), (0, 1, 2))
        self.assertEqual(metrics.next_version({(0, 1, 0), (0, 1, 7)}, (0, 2)), (0, 2, 0))
        self.assertEqual(metrics.next_version({(0, 2, 0)}, (0, 1)), (0, 2, 1))     # never backwards

    # ---- the release ----------------------------------------------------------

    def test_a_product_without_a_deploy_cuts_a_tagged_release_of_its_trunk(self):
        head = _git(self.repo, 'rev-parse', 'HEAD')
        path = self.release()
        self.assertEqual(os.path.basename(path or ''), f'{DAY}-{head[:7]}.md')
        text = self.read(f'releases/{DAY}-{head[:7]}.md')
        self.assertIn('- [T-0001](../tasks/T-0001.md) Plan door copy', text)
        self.assertIn('\nversion: v0.1.0\n', text)          # no versioned Epic: version_file's 0.1
        self.assertIn('Tag `v0.1.0`', text)
        self.assertEqual(self.origin_tags(), {'v0.1.0': head})     # pushed, at the released sha

    def test_none_within_the_interval_and_several_a_day_past_it(self):
        first = self.release()
        self.assertIsNotNone(first)
        head = self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.assertIsNone(self.release(later=dt.timedelta(minutes=30)))     # 30m after: too soon
        self.assertIsNone(self.release(later=dt.timedelta(minutes=29)))     # 59m after: still too soon
        second = self.release(later=dt.timedelta(minutes=2))                # 61m after, the same day
        self.assertEqual(os.path.basename(second or ''), f'{DAY}-{head[:7]}.md')
        self.assertIn('\nversion: v0.1.1\n', self.read(os.path.relpath(second, self.root)))
        self.assertEqual(self.origin_tags()['v0.1.1'], head)
        log = self.changelog()
        self.assertLess(log.index(f'## v0.1.1 — {DAY}'), log.index(f'## v0.1.0 — {DAY}'))
        # a third the same day: its notes start after the second, whatever the sha7s sort as
        third = self.commit('pkg/c.py', 'c = 1\n', 'task(T-0002): sign-up form')
        path = self.release()
        text = self.read(os.path.relpath(path, self.root))
        self.assertIn('\nversion: v0.1.2\n', text)
        self.assertNotIn('B-0001', text)
        self.assertEqual(self.origin_tags()['v0.1.2'], third)
        self.assertEqual(len(os.listdir(os.path.join(self.root, 'releases'))), 3)
        self.assertIsNone(self.release())                           # nothing landed since

    def test_the_interval_is_a_convention(self):
        self.product.conventions.extra['release_min_interval'] = '3h'
        self.assertEqual(metrics.release_min_interval_s(self.product), 3 * 3600)
        self.release()
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.assertIsNone(self.release())                           # 2h < 3h
        self.assertIsNotNone(self.release())
        for value, seconds in ((None, 3600), ('15m', 900), ('30s', 30), ('1d', 86400), (5, 300),
                               ('0m', 0), ('soon', 3600)):
            self.product.conventions.extra['release_min_interval'] = value
            self.assertEqual(metrics.release_min_interval_s(self.product), seconds, value)

    def test_no_release_for_a_day_older_than_the_newest(self):
        self.release()
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.assertIsNone(self.release('2026-09-20'))

    def test_the_next_release_is_the_next_patch(self):
        self.release()
        head = self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        path = self.release('2026-09-22')
        text = self.read(os.path.relpath(path, self.root))
        self.assertIn('- [B-0001](../bugs/B-0001.md) Banner shows', text)
        self.assertNotIn('T-0001', text)                            # already in the first release
        self.assertIn('\nversion: v0.1.1\n', text)
        self.assertEqual(self.origin_tags()['v0.1.1'], head)
        self.assertIsNone(self.release('2026-09-23'))               # nothing landed since

    def test_a_new_active_epic_rolls_the_minor_over(self):
        self.set_epics(('E-0002', 'Product 0.1 — first', 'Active', 1), ('E-0003', 'Product 0.2 — next', 'New', 2))
        self.release()
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.release('2026-09-22')
        self.items['E-0002']['state'] = 'Closed'
        head = self.commit('pkg/c.py', 'c = 1\n', 'task(T-0002): sign-up form')
        self.release('2026-09-23')
        self.assertEqual(self.origin_tags()['v0.2.0'], head)
        self.assertIn('v0.1.1', self.origin_tags())

    def test_release_notes_group_features_bugs_and_item_less_commits(self):
        notes = metrics.render_notes(self.items, {'T-0001': [], 'S-0001': [], 'B-0001': [], 'E-0001': []},
                                     ['hotfix(install): one installer'], 'install it @v1.2.3')
        self.assertEqual(notes, '### Features landed\n\n- F-0001 Free plan\n\n'
                                '### Bugs fixed\n\n- B-0001 Banner shows\n\n'
                                '### Improvements and hotfixes\n\n- hotfix(install): one installer\n\n'
                                '### Upgrade\n\n`install it @v1.2.3`\n')
        many = metrics.render_notes(self.items, {}, [f'c{i}' for i in range(metrics.NOTES_MAX_COMMITS + 3)])
        self.assertIn('- … and 3 more', many)

    def test_the_notes_reach_the_note_the_tag_and_the_changelog(self):
        self.commit('pkg/h.py', 'h = 1\n', 'hotfix(pkg): a quick one')
        path = self.release()
        text = self.read(os.path.relpath(path, self.root))
        notes = metrics.notes_of(text)
        self.assertIn('- F-0001 Free plan', notes)
        self.assertIn('- hotfix(pkg): a quick one', notes)
        self.assertIn('pipx install --force "git+https://github.com/x/y.git@v0.1.0"', notes)
        self.assertIn('### Features landed', _git(self.origin, 'tag', '-l', '--format=%(contents)', 'v0.1.0'))
        self.assertTrue(self.changelog().startswith('# Changelog'))
        self.assertIn(f'## v0.1.0 — {DAY}\n\n{notes}', self.changelog() + '\n')

    def test_under_refguard_refuse_the_changelog_goes_up_as_a_pr(self):
        """``flags.refguard: refuse`` with a person merging (``merge: manual``): the changelog
        commit never goes to the trunk — it is pushed to its own branch and put up as a PR."""
        self.product.conventions.extra['flags'] = {'refguard': 'refuse'}
        gh = mock.Mock(return_value=mock.Mock(ok=True, stderr='', stdout=''))
        with mock.patch.object(metrics.github, 'gh', gh):
            self.release()
        self.assertNotIn('CHANGELOG.md', _git(self.origin, 'ls-tree', '--name-only', 'main'))
        log = _git(self.origin, 'show', f'{metrics.CHANGELOG_BRANCH}:CHANGELOG.md')
        self.assertIn('## v0.1.0', log)
        (call,) = [c for c in gh.call_args_list if c.args[0][:2] == ['pr', 'create']]
        args = call.args[0]
        self.assertEqual(args[args.index('--head') + 1], metrics.CHANGELOG_BRANCH)
        self.assertEqual(args[args.index('--base') + 1], 'main')
        self.assertEqual(args[args.index('-R') + 1], 'x/y')

    def test_under_merge_auto_the_changelog_push_is_a_door(self):
        self.product.conventions.extra['flags'] = {'refguard': 'refuse'}
        self.product.conventions.merge = 'auto'
        self.release()
        self.assertIn('## v0.1.0', self.changelog())

    def test_the_changelog_is_newest_first_and_idempotent(self):
        self.release()
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.release('2026-09-22')
        log = self.changelog()
        self.assertLess(log.index('## v0.1.1'), log.index('## v0.1.0'))
        before = _git(self.origin, 'rev-parse', 'main')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(metrics.sync_changelog(self.root, self.product))
        self.assertEqual(_git(self.origin, 'rev-parse', 'main'), before)          # nothing to add
        self.assertEqual(metrics.merge_changelog(log + '\n', {'v0.1.0': 'x'}), log + '\n')
        merged = metrics.merge_changelog('# Log\n\n## v0.1.0 — d\n\nold\n', {'v0.1.2': '## v0.1.2 — e\n\nnew\n'})
        self.assertEqual(merged, '# Log\n\n## v0.1.2 — e\n\nnew\n\n## v0.1.0 — d\n\nold\n')
        # the changelog commit is not an improvement of the next release
        subject = _git(self.origin, 'log', '-1', '--format=%s', 'main')
        self.assertTrue(metrics.CHANGELOG_SUBJECT_RE.match(subject), subject)
        self.assertEqual(metrics.improvement_commits(self.repo, self.items, 'origin/main', 'origin/main~1'), [])

    # ---- F-0248 amendment 7: the product's own version scheme -----------------

    def unversioned(self, **version):
        """The fixture product without a ``version_file``: the rollup cuts no tag by default."""
        conv = {'version': version} if version else {}
        self.product = env.Product('plain', {'repo_dir': self.repo, 'repo_slug': 'x/y', 'main': 'main',
                                             'ci': 'none', 'conventions': conv})

    def test_by_default_a_product_is_versioned_by_its_short_sha_with_nothing_pushed(self):
        self.unversioned()
        self.assertFalse(metrics.cuts_tags(self.product))
        self.assertFalse(metrics.changelog_on(self.product))
        before = _git(self.origin, 'rev-parse', 'main')
        head = _git(self.repo, 'rev-parse', 'HEAD')
        path = self.release()
        text = self.read(os.path.relpath(path, self.root))
        self.assertIn(f'\nversion: {head[:7]}\n', text)
        self.assertNotIn('Tag `', text)
        self.assertIn('Plan door copy', text)
        self.assertEqual(self.origin_tags(), {})                       # no tag cut
        self.assertEqual(_git(self.origin, 'rev-parse', 'main'), before)   # no changelog commit

    def test_a_tag_pattern_names_the_products_own_tags(self):
        self.unversioned(tag_pattern=r'^release/\d+$')
        head = _git(self.repo, 'rev-parse', 'HEAD')
        _git(self.repo, 'tag', 'release/7', head)
        _git(self.repo, 'tag', 'unrelated', head)
        path = self.release()
        self.assertIn('\nversion: release/7\n', self.read(os.path.relpath(path, self.root)))
        self.assertEqual(self.origin_tags(), {})                       # the rollup cut nothing
        self.assertEqual(metrics.own_version(self.repo, 'f' * 40, self.product), 'fffffff')

    def test_the_changelog_is_opt_in(self):
        self.unversioned(changelog=True)
        head = _git(self.repo, 'rev-parse', 'HEAD')
        self.release()
        self.assertIn(f'## {head[:7]} — {DAY}', self.changelog())
        self.assertEqual(self.origin_tags(), {})
        # idempotent: the sha-versioned entry is found again
        before = _git(self.origin, 'rev-parse', 'main')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(metrics.sync_changelog(self.root, self.product))
        self.assertEqual(_git(self.origin, 'rev-parse', 'main'), before)
        # and a version_file product turns it off explicitly
        self.product.conventions.extra['version'] = {'changelog': False}
        self.assertFalse(metrics.changelog_on(self.product))

    def test_a_version_file_alone_cuts_nothing(self):
        self.product.conventions.extra['version'] = {}
        self.assertFalse(metrics.cuts_tags(self.product))
        self.assertFalse(metrics.changelog_on(self.product))

    def test_the_interval_holds_without_a_tag(self):
        self.unversioned()
        self.assertIsNotNone(self.release())
        self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        self.assertIsNone(self.release(later=dt.timedelta(minutes=30)))     # the released: line holds it
        self.assertIsNotNone(self.release(later=dt.timedelta(minutes=31)))

    def test_the_factorys_own_repo_releases_like_any_product(self):
        # no repository is special: the factory's own source with the same config releases the same
        with open(os.path.join(self.repo, 'pyproject.toml'), 'w') as f:
            f.write('[project]\nname = "asf-factory"\n')
        self.unversioned(tag_pattern=r'^v\d+\.\d+\.\d+$')
        path = self.release()
        self.assertIsNotNone(path)
        self.assertEqual(self.origin_tags(), {})

    def test_cut_opts_into_the_rollups_own_tags(self):
        self.unversioned(cut=True)
        head = _git(self.repo, 'rev-parse', 'HEAD')
        self.release()
        self.assertEqual(self.origin_tags(), {'v0.1.0': head})
        self.assertIn('## v0.1.0', self.changelog())

    # ---- the migration --------------------------------------------------------

    def test_a_legacy_release_tag_is_adopted_as_the_next_patch_once(self):
        first = _git(self.repo, 'rev-parse', 'HEAD')
        _git(self.repo, 'tag', '-a', 'v0.1.0', first, '-m', 'Release')
        _git(self.repo, 'push', '-q', 'origin', 'refs/tags/v0.1.0')
        with open(os.path.join(self.root, 'releases', f'2026-09-20-{first[:7]}.md'), 'w') as f:
            f.write(f'# Release 2026-09-20 · {first[:7]}\n\nTag `v0.1.0`.\n')
        head = self.commit('pkg/b.py', 'b = 1\n', 'fix(B-0001): banner')
        legacy = self.legacy_release(DAY, head)
        with contextlib.redirect_stdout(io.StringIO()):
            done = metrics.migrate_releases(self.root, self.items, self.product)
        self.assertEqual([t for _p, t in done], ['v0.1.0', 'v0.1.1'])
        tags = self.origin_tags()
        self.assertEqual((tags['v0.1.1'], tags[legacy]), (head, head))         # the old tag is kept
        text = self.read(f'releases/{DAY}-{head[:7]}.md')
        self.assertIn('\nversion: v0.1.1\n', text)
        self.assertIn(f'adopted from `{legacy}`', text)
        self.assertIn('- B-0001 Banner shows', metrics.notes_of(text))
        self.assertIn('\nversion: v0.1.0\n', self.read(f'releases/2026-09-20-{first[:7]}.md'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(metrics.migrate_releases(self.root, self.items, self.product), [])   # idempotent
        self.assertEqual(sorted(self.origin_tags()), sorted(['v0.1.0', 'v0.1.1', legacy]))
        # the rollup runs it, then files both versions' notes, then cuts the next patch
        nxt = self.commit('pkg/c.py', 'c = 1\n', 'task(T-0002): sign-up form')
        self.release('2026-09-22')
        self.assertEqual(self.origin_tags()['v0.1.2'], nxt)
        log = self.changelog()
        self.assertLess(log.index('## v0.1.2'), log.index('## v0.1.1'))
        self.assertLess(log.index('## v0.1.1'), log.index('## v0.1.0'))

    # ---- the GitHub Release ---------------------------------------------------

    def test_a_github_release_is_skipped_quietly_without_gh(self):
        hosted = env.Product('hosted', {'repo_dir': self.repo, 'repo_slug': 'x/y', 'main': 'main'})
        err = io.StringIO()
        with mock.patch.object(metrics.shutil, 'which', return_value=None), \
                mock.patch.object(metrics, 'gh') as gh, contextlib.redirect_stderr(err):
            self.assertFalse(metrics.publish_github_release(hosted, 'v0.1.0', 'notes'))
        gh.assert_not_called()
        self.assertIn('gh not found; no GitHub release for v0.1.0', err.getvalue())
        with mock.patch.object(metrics, 'gh') as gh:                       # no hosted repo: not asked
            self.assertFalse(metrics.publish_github_release(self.product, 'v0.1.0', 'notes'))
        gh.assert_not_called()

    def test_a_github_release_is_created_once_and_offline_is_logged(self):
        hosted = env.Product('hosted', {'repo_dir': self.repo, 'repo_slug': 'x/y', 'main': 'main'})
        calls = []

        def fake_gh(args, *a, **kw):
            calls.append(args[:2])
            return None if args[1] == 'view' else ''
        with mock.patch.object(metrics.shutil, 'which', return_value='/bin/gh'), \
                mock.patch.object(metrics, 'gh', side_effect=fake_gh):
            self.assertTrue(metrics.publish_github_release(hosted, 'v0.1.0', 'notes'))
        self.assertEqual(calls, [['release', 'view'], ['release', 'create']])
        err = io.StringIO()
        with mock.patch.object(metrics.shutil, 'which', return_value='/bin/gh'), \
                mock.patch.object(metrics, 'gh', return_value=None), contextlib.redirect_stderr(err):
            self.assertFalse(metrics.publish_github_release(hosted, 'v0.1.0', 'notes'))
        self.assertIn('no GitHub release for v0.1.0', err.getvalue())


class SessionEventTests(Base):
    RECORD = {'job': 'p2-t1', 'account': 'accta', 'started': '2026-09-21T06:00:00Z',
              'ended': '2026-09-21T06:10:00Z', 'end_reason': 'done'}

    def event(self, result):
        return metrics.session_event(self.RECORD, result, self.items)

    def test_input_tokens_are_the_sum_of_the_three(self):
        ev = self.event({'type': 'result', 'num_turns': 9,
                         'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 5,
                                   'cache_read_input_tokens': 100, 'output_tokens': 7}})
        self.assertEqual((ev['in_tokens'], ev['turns']), (115, 9))

    def test_no_usage_is_null_not_zero(self):
        ev = self.event({'type': 'result'})
        self.assertEqual((ev['in_tokens'], ev['turns']), (None, None))
        self.assertEqual((self.event(None)['in_tokens'], self.event(None)['turns']), (None, None))

    def test_the_schema_takes_them(self):
        ev = metrics.validate('sessions', dict(self.event({'num_turns': 3, 'usage': {'input_tokens': 4}}),
                                               item=None), {})
        self.assertEqual((ev['in_tokens'], ev['turns']), (4, 3))
        old = metrics.validate('sessions', {'task': 't', 'account': 'a', 'result': 'done'}, {})
        self.assertEqual((old['in_tokens'], old['turns']), (None, None))


class Backfill(Base):
    def write_results(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, 'accta'))
        with open(os.path.join(d, 'accta', 'dispatched.json'), 'w') as f:
            json.dump([{'id': 'fix-free-plan-t3-r1', 'model': 'claude-sonnet-5', 'status': 'running'}], f)
        res = os.path.join(d, 'results.jsonl')
        with open(res, 'w') as f:
            f.write(json.dumps({'task': 'fix-free-plan-t3-r1', 'account': 'accta', 'branch': 'cloud/free-plan-t3',
                                'result': 'failed', 'reason': 'x', 'sha': 'abc', 'ended_at': '2026-09-21T06:20:00+0200'}) + '\n')
            f.write(json.dumps({'task': 'fix-free-plan-t3-r1', 'account': 'accta', 'branch': 'cloud/free-plan-t3',
                                'result': 'done', 'reason': None, 'sha': 'abc', 'ended_at': '2026-09-21T06:20:00+0200'}) + '\n')
            f.write('garbage\n')
            f.write(json.dumps({'task': 'old', 'account': 'accta', 'result': 'done', 'ended_at': '2026-01-01T00:00:00+0100'}) + '\n')
        return d, res

    def write_registry(self, home):
        """A session registry and a job log, exactly as asf.workers.pool/runtime write them."""
        state = os.path.join(home, 'state', 'sample')
        logs = os.path.join(home, 'logs', 'jobs', 'sample')
        os.makedirs(state)
        os.makedirs(logs)
        with open(os.path.join(state, 'sessions.jsonl'), 'w') as f:
            f.write(json.dumps({'job': 'fix-free-plan-t3-r1', 'item': 'T-0001', 'kind': 'fix-bug',
                                'account': 'acct-a', 'model': 'claude-sonnet-5',
                                'branch': 'feature/free-plan-t3',
                                'started': '2026-09-21T04:16:00Z'}) + '\n')
            f.write(json.dumps({'job': 'fix-free-plan-t3-r1', 'ended': '2026-09-21T04:20:00Z',
                                'end_reason': 'finished'}) + '\n')
            f.write(json.dumps({'job': 'still-running', 'item': 'T-0002', 'kind': 'task',
                                'account': 'acct-a', 'started': '2026-09-21T04:16:00Z'}) + '\n')
        with open(os.path.join(logs, 'fix-free-plan-t3-r1.jsonl'), 'w') as f:
            f.write(json.dumps({'type': 'system', 'subtype': 'init'}) + '\n')
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': 'fixed', 'total_cost_usd': 0.42,
                                'duration_ms': 240000}) + '\n')
        return state, logs

    def test_sessions_come_from_the_registry_and_the_job_log(self):
        d, _res = self.write_results()
        home = os.path.join(d, 'asf-home2')
        self.write_registry(home)
        product = env.Product('sample', {})
        with mock.patch.object(env, 'ASF_HOME', home):
            evs = metrics.sessions_from_registry(product, since_day='2026-09-20', items=self.items)
        self.assertEqual(len(evs), 1)                  # the session still running is not an event
        ev = evs[0]
        # the record may be public: the account is an index into the pool, never a name (B-0023)
        self.assertEqual((ev['task'], ev['result'], ev['kind'], ev['item'], ev['account']),
                         ('fix-free-plan-t3-r1', 'finished', 'fix', 'T-0001', 'a?'))
        self.assertNotIn('acct-a', json.dumps(ev))
        self.assertEqual((ev['minutes'], ev['usd'], ev['reason']), (4.0, 0.42, 'fixed'))
        self.assertEqual(ev['ts'], '2026-09-21T04:20:00Z')
        metrics.validate('sessions', ev, self.items)   # it is a valid event as it stands

    def test_account_index_and_capped_reason(self):
        with mock.patch.object(env, 'load_config',
                               lambda: {'worker_pool': {'accounts': [{'name': 'x1'}, {'name': 'x2'}]}}):
            self.assertEqual(metrics.account_index('x2'), 'a2')
            self.assertEqual(metrics.account_index('nobody'), 'a?')
            self.assertEqual(metrics.account_index(None), 'a?')
        d, _res = self.write_results()
        home = os.path.join(d, 'asf-home3')
        state, logs = self.write_registry(home)
        with open(os.path.join(logs, 'fix-free-plan-t3-r1.jsonl'), 'w') as f:
            f.write(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                                'result': 'first line ' + 'x' * 400 + '\nsecond line with a path /home/someone',
                                'total_cost_usd': 0.1, 'duration_ms': 1000}) + '\n')
        product = env.Product('sample', {})
        with mock.patch.object(env, 'ASF_HOME', home):
            evs = metrics.sessions_from_registry(product, since_day='2026-09-20', items=self.items)
        self.assertEqual(len(evs[0]['reason']), 200)
        self.assertNotIn('second line', evs[0]['reason'])

    def test_import_sessions_reads_a_legacy_results_file(self):
        d, res = self.write_results()
        counts = import_sessions.import_file(self.root, res, 'legacy-results',
                                             launch_dir=d, since_day='2026-09-20')
        self.assertEqual(counts['sessions appended'], 1)
        ev = json.loads(self.read('metrics/sessions/2026-09-21.jsonl').strip())
        self.assertEqual((ev['task'], ev['result'], ev['model'], ev['item']),
                         ('fix-free-plan-t3-r1', 'done', 'claude-sonnet-5', 'T-0001'))
        # a second import of the same file changes nothing
        again = import_sessions.import_file(self.root, res, 'legacy-results',
                                            launch_dir=d, since_day='2026-09-20')
        self.assertEqual(again['sessions exists'], 1)

    def test_sessions_from_results(self):
        d, res = self.write_results()
        start = dt.datetime(2026, 9, 21, 4, 16).astimezone()
        brief = os.path.join(d, 'accta', 'brief-fix-free-plan-t3-r1.md')
        with open(brief, 'w') as f:
            f.write('x')
        os.utime(brief, (start.timestamp(), start.timestamp()))
        evs = import_sessions.sessions_from_results(res, d, self.items, '2026-09-20')
        self.assertEqual(len(evs), 1)                       # last line per task wins; the January one is out of the window
        ev = evs[0]
        self.assertEqual((ev['task'], ev['result'], ev['model'], ev['ts']),
                         ('fix-free-plan-t3-r1', 'done', 'claude-sonnet-5', '2026-09-21T04:20:00Z'))
        self.assertEqual(ev['minutes'], round((dt.datetime(2026, 9, 21, 4, 20, tzinfo=dt.timezone.utc)
                                               - start).total_seconds() / 60, 1))
        out = metrics.validate('sessions', ev, self.items)
        self.assertEqual((out['kind'], out['round'], out['item']), ('fix', 1, 'T-0001'))

    def fake_gh_lines(self, args, timeout=300):
        joined = ' '.join(args)
        if '/jobs' in joined:
            run = int(joined.split('/runs/')[1].split('/')[0])
            if run == 1:
                return [{'name': 'lint', 'conclusion': 'success', 'runner_name': 'box1', 'started_at': '2026-09-21T01:00:00Z',
                         'completed_at': '2026-09-21T01:04:30Z', 'failed': []},
                        {'name': 'e2e', 'conclusion': 'failure', 'runner_name': 'box2', 'started_at': '2026-09-21T01:00:00Z',
                         'completed_at': '2026-09-21T01:06:00Z', 'failed': ['Run tests']}]
            return [{'name': 'lint', 'conclusion': 'cancelled', 'runner_name': 'box1', 'started_at': '2026-09-21T02:00:00Z',
                     'completed_at': '2026-09-21T02:03:00Z', 'failed': []}]
        return [{'id': 1, 'name': 'ci', 'head_branch': 'cloud/free-plan-t3', 'head_sha': 'e' * 40, 'conclusion': 'failure',
                 'created_at': '2026-09-21T01:00:00Z', 'updated_at': '2026-09-21T01:07:00Z', 'run_attempt': 1, 'pr': [623]},
                {'id': 2, 'name': 'ci', 'head_branch': 'worktree-m-batch-20260921-0006', 'head_sha': 'f' * 40,
                 'conclusion': 'cancelled', 'created_at': '2026-09-21T02:00:00Z', 'updated_at': '2026-09-21T02:03:00Z',
                 'run_attempt': 1, 'pr': []},
                {'id': 3, 'name': 'dco', 'head_branch': 'x', 'head_sha': 'e' * 40, 'conclusion': 'success',
                 'created_at': '2026-09-21T01:00:00Z', 'updated_at': '2026-09-21T01:01:00Z', 'run_attempt': 1, 'pr': []}]

    def fake_gh_lines_answered(self, args, timeout=300):
        return self.fake_gh_lines(args, timeout), True

    def test_ci_backfill_and_idempotence(self):
        d, _res = self.write_results()
        argv = ['backfill', '--days', '2']
        prs = {623: {'merged': True, 'merge_commit_sha': '2' * 40, 'body': '', 'title': 'x'},
               11: {'title': 'T-0002 form', 'body': '', 'merged': True, 'merge_commit_sha': None},
               12: None, 13: None}
        # no ~/.ASF is present in CI: stub the product lookup so ci_from_api's repo-slug resolution
        # (used only to build the (mocked) gh_lines request, never a real API call) doesn't need one.
        # the merge-batch lane is a branch prefix this product happens to have, not a literal
        stub_product = env.Product('sample', {
            'repo_slug': 'sample/sample',
            'conventions': {'branch_prefixes': {'code': 'feature/', 'batch': 'worktree-m-batch-'}}})
        home = os.path.join(d, 'asf-home')
        self.write_registry(home)
        with mock.patch.object(metrics, 'gh_lines_answered', side_effect=self.fake_gh_lines_answered), \
                mock.patch.object(metrics, 'pr_info', side_effect=lambda n, *a, **kw: prs.get(n)), \
                mock.patch.object(metrics, 'today', return_value='2026-09-21'), \
                mock.patch.object(env, 'ASF_HOME', home), \
                mock.patch.object(env, 'load_product', return_value=stub_product), \
                mock.patch.object(metrics, 'job_limits', return_value={}):
            rc, out, _ = run_cli(self.root, *argv)
            self.assertEqual(rc, 0)
            self.assertIn('ci appended: 2', out)
            self.assertIn('sessions appended: 1', out)
            snap = {p: self.read(p) for p in ('metrics/ci/2026-09-21.jsonl',)}
            rc, out, _ = run_cli(self.root, *argv)
            self.assertIn('held 2, fetched 0, appended 0', out)
            self.assertNotIn('ci appended', out)
            sessions = [json.loads(l) for l in self.read('metrics/sessions/2026-09-21.jsonl').splitlines()]
            self.assertEqual([(e['task'], e['result'], e['kind'], e['usd'], e['minutes']) for e in sessions],
                             [('fix-free-plan-t3-r1', 'finished', 'fix', 0.42, 4.0)])
            self.assertEqual(sessions[0]['item'], 'T-0001')
        for p, text in snap.items():
            self.assertEqual(self.read(p), text)
        ci = [json.loads(l) for l in snap['metrics/ci/2026-09-21.jsonl'].splitlines()]
        red = next(e for e in ci if e['run'] == 1)
        self.assertEqual((red['minutes'], red['pr'], red['items'], red['conclusion']), (10, 623, ['T-0001'], 'failure'))
        self.assertEqual([j['failed_step'] for j in red['jobs']], [None, 'Run tests'])
        batch = next(e for e in ci if e['run'] == 2)
        # the branch is recognised as the batch lane by the product's `batch` prefix; with no PR
        # list for it (that came from the previous runner's log, now an import) it matches no item
        self.assertEqual((batch['batch'], batch['cancelled_minutes'], batch['items'], batch['item_reason']),
                         ('worktree-m-batch-20260921-0006', 3, None, 'batch run without a PR list'))
        self.assertFalse(batch['superseded'])           # no later run on that branch


class ImportSessionsSurfaceTests(unittest.TestCase):
    """What survives T-0336: the legacy log parser and its `--format legacy-log` are gone, and
    `legacy-results` (the only format left) no longer needs a runner log to find start times."""

    def test_the_parser_names_are_gone(self):
        gone = [n for n in ('parse_log', 'ticks_from_log', 'wave_centres', 'batch_prs_from_log',
                            'LOG_LINE', 'CUT_LINE', 'WAVE_ID', 'WAVE_LINE', 'QUOTA', 'TICK_WINDOW',
                            'STALL_LINE') if hasattr(import_sessions, n)]
        self.assertEqual(gone, [])

    def test_legacy_log_is_not_a_format(self):
        self.assertNotIn('legacy-log', import_sessions.FORMATS)
        self.assertIn('legacy-results', import_sessions.FORMATS)

    def test_import_file_has_no_log_argument_or_branch(self):
        params = inspect.signature(import_sessions.import_file).parameters
        self.assertNotIn('log', params)
        with self.assertRaises(ValueError):
            import_sessions.import_file(tempfile.mkdtemp(), __file__, 'legacy-log')

    def test_the_cli_has_no_log_flag(self):
        p = argparse.ArgumentParser()
        import_sessions.register(p.add_subparsers(dest='command', required=True))
        with self.assertRaises(SystemExit):
            p.parse_args(['import-sessions', '--file', 'x', '--log', 'y'])

    def test_sessions_from_results_takes_no_recs_argument(self):
        self.assertNotIn('recs', inspect.signature(import_sessions.sessions_from_results).parameters)

    def test_the_module_docstring_has_no_legacy_log_section(self):
        self.assertNotIn('legacy-log', import_sessions.__doc__)
        self.assertIn('legacy-results', import_sessions.__doc__)

    def test_legacy_results_still_imports_a_results_file(self):
        self.assertTrue(callable(import_sessions.launch_ledger))
        self.assertTrue(callable(import_sessions.sessions_from_results))
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        res = os.path.join(root, 'results.jsonl')
        with open(res, 'w') as f:
            f.write(json.dumps({'task': 't', 'account': 'a', 'result': 'done',
                                'ended_at': '2026-09-21T00:00:00Z'}) + '\n')
        evs = import_sessions.sessions_from_results(res, None, {}, '2026-09-20')
        self.assertEqual([ev['task'] for ev in evs], ['t'])


class CollectingPass(Base):
    """`metrics.ci_facts`, `ci_watermark`, `ci_window`, `ci_runs_held` and `cmd_backfill`'s
    incremental, cached, loud collecting pass (F-0175 S-37100)."""

    PRODUCT = env.Product('sample', {'repo_slug': 'sample/sample'})

    def seed(self, run, ts, branch='main', **kw):
        self.put('ci', ci_event(run, ts=ts, branch=branch, **kw))

    def api_run(self, run_id, created, branch='main', conclusion='success', attempt=1):
        return {'id': run_id, 'name': 'ci', 'head_branch': branch, 'head_sha': 'a' * 40,
                'conclusion': conclusion, 'created_at': created, 'updated_at': created,
                'run_attempt': attempt, 'pr': []}

    def host(self, runs, jobs=None):
        """A `gh_lines_answered` stub answering `True`: `runs` for the listing call, and
        `jobs.get(run_id, [])` for every `/jobs` call — the same split `fake_gh_lines` makes."""
        jobs = jobs or {}

        def answered(args):
            joined = ' '.join(args)
            if '/jobs' in joined:
                run_id = int(joined.split('/runs/')[1].split('/')[0])
                return jobs.get(run_id, []), True
            return runs, True
        return answered

    def backfill(self, argv, answered, today='2026-09-29', product=None):
        """Runs `backfill <argv>` against a stubbed clock and a stubbed `gh_lines_answered`
        (`answered(args) -> (lines, answered)`); returns `(rc, out, calls)` — `calls` is every
        `args` list `gh_lines_answered` was asked with, the listing before any `/jobs` calls."""
        calls = []

        def stub(args, timeout=300):
            calls.append(args)
            return answered(args)

        with mock.patch.object(metrics, 'gh_lines_answered', side_effect=stub), \
                mock.patch.object(metrics, 'today', return_value=today), \
                mock.patch.object(env, 'load_product', return_value=product or self.PRODUCT):
            rc, out, _err = run_cli(self.root, 'backfill', *argv)
        return rc, out, calls

    def ci_lines(self, out):
        return [l for l in out.splitlines() if l.startswith('ci:')]

    def test_the_window_starts_at_the_newest_trunk_fact(self):
        self.seed(1, '2026-09-21T10:00:00Z')
        _rc, _out, calls = self.backfill([], self.host([]), today='2026-09-29')
        self.assertIn('created=%3E%3D2026-09-21', calls[0][1])

    def test_a_gap_longer_than_the_catch_up_is_floored_and_said(self):
        self.seed(1, '2026-06-01T10:00:00Z')
        _rc, out, calls = self.backfill([], self.host([]), today='2026-09-29')
        self.assertIn('created=%3E%3D2026-09-16', calls[0][1])          # 14 days back from 09-29
        self.assertIn('reached back 14d', out)

    def test_a_true_first_run_asks_for_seven_days_not_the_catch_up_floor(self):
        _rc, out, calls = self.backfill([], self.host([]), today='2026-09-29')
        self.assertIn('created=%3E%3D2026-09-23', calls[0][1])          # 7 days back from 09-29
        self.assertNotIn('reached back', out)

    def test_days_still_overrides_the_watermark(self):
        self.seed(1, '2026-06-01T10:00:00Z')
        _rc, out, calls = self.backfill(['--days', '2'], self.host([]), today='2026-09-29')
        self.assertIn('created=%3E%3D2026-09-28', calls[0][1])          # 2 days back, not the watermark
        self.assertNotIn('reached back', out)

    def test_a_run_the_stream_holds_is_listed_but_not_fetched(self):
        self.seed(1, '2026-09-20T10:00:00Z')
        self.seed(2, '2026-09-20T11:00:00Z')
        runs = [self.api_run(1, '2026-09-20T10:00:00Z'), self.api_run(2, '2026-09-20T11:00:00Z'),
                self.api_run(3, '2026-09-20T12:00:00Z')]
        jobs = {3: [{'name': 'gate', 'conclusion': 'success', 'runner_name': 'box1',
                     'started_at': '2026-09-20T12:00:00Z', 'completed_at': '2026-09-20T12:05:00Z', 'failed': []}]}
        _rc, out, calls = self.backfill(['--days', '10'], self.host(runs, jobs), today='2026-09-29')
        job_calls = [c for c in calls if '/jobs' in ' '.join(c)]
        self.assertEqual(len(job_calls), 1)
        self.assertIn('/runs/3/jobs', ' '.join(job_calls[0]))
        self.assertIn('ci appended: 1', out)
        ci = [json.loads(l) for l in self.read('metrics/ci/2026-09-20.jsonl').splitlines()]
        self.assertEqual(sorted(e['run'] for e in ci), [1, 2, 3])

    def test_superseded_still_sees_the_runs_it_did_not_fetch(self):
        self.seed(2, '2026-09-20T11:00:00Z')
        runs = [self.api_run(1, '2026-09-20T10:00:00Z', conclusion='cancelled'),
                self.api_run(2, '2026-09-20T11:00:00Z')]
        jobs = {1: [{'name': 'gate', 'conclusion': 'cancelled', 'runner_name': 'box1',
                     'started_at': '2026-09-20T10:00:00Z', 'completed_at': '2026-09-20T10:01:00Z', 'failed': []}]}
        _rc, _out, _calls = self.backfill(['--days', '10'], self.host(runs, jobs), today='2026-09-29')
        ci = [json.loads(l) for l in self.read('metrics/ci/2026-09-20.jsonl').splitlines()]
        run1 = next(e for e in ci if e['run'] == 1)
        self.assertTrue(run1['superseded'])      # run 2, on the same branch, was not re-fetched either

    def test_the_pass_says_what_it_did(self):
        runs = [self.api_run(1, '2026-09-20T10:00:00Z'), self.api_run(2, '2026-09-20T11:00:00Z')]
        jobs = {n: [{'name': 'gate', 'conclusion': 'success', 'runner_name': 'box1',
                     'started_at': '2026-09-20T10:00:00Z', 'completed_at': '2026-09-20T10:05:00Z', 'failed': []}]
                for n in (1, 2)}
        _rc, out, _calls = self.backfill(['--days', '10'], self.host(runs, jobs), today='2026-09-29')
        self.assertIn('ci: 2026-09-20..2026-09-29 — listed 2, held 0, fetched 2, appended 2; newest '
                      '2026-09-20T11:00:00Z (', out)

    def test_the_pass_names_why_it_collected_nothing(self):
        def no_answer(args):
            return [], False
        _rc, out1, _calls1 = self.backfill(['--days', '10'], no_answer, today='2026-09-29')
        self.assertIn('the host did not answer (gh)', out1)

        no_slug = env.Product('sample', {})
        _rc, out2, calls2 = self.backfill(['--days', '10'], self.host([]), today='2026-09-29', product=no_slug)
        self.assertIn('the product names no repo_slug', out2)
        self.assertEqual(calls2, [])    # PD7: a falsy repo_slug never reaches gh

        _rc, out3, _calls3 = self.backfill(['--days', '10'], self.host([]), today='2026-09-29')
        self.assertIn('no PR, trunk or batch run in the window', out3)

        lines = {self.ci_lines(out1)[0], self.ci_lines(out2)[0], self.ci_lines(out3)[0]}
        self.assertEqual(len(lines), 3)   # no two of the three print the same string

    def test_trunk_ci_runs_is_the_reverse_of_ci_facts(self):
        day_dir = os.path.join(self.root, 'metrics', 'ci')
        os.makedirs(day_dir, exist_ok=True)
        rows = [
            {'ts': '2026-09-22T10:00:00Z', 'branch': 'main', 'sha': 'b' * 40, 'run': 2, 'attempt': 1,
             'jobs': [{'name': 'gate', 'conclusion': 'failure'}, {'name': 'gate', 'conclusion': 'success'}]},
            {'ts': '2026-09-21T10:00:00Z', 'branch': 'main', 'sha': 'a' * 40, 'run': 1, 'attempt': 1,
             'jobs': [{'name': 'gate', 'conclusion': 'failure'}]},
            {'ts': '2026-09-21T10:00:00Z', 'branch': 'main', 'sha': 'd' * 40, 'run': 3, 'attempt': 1,
             'jobs': [{'name': 'gate', 'conclusion': 'success'}]},
            {'ts': '2026-09-23T10:00:00Z', 'branch': 'cloud/x', 'sha': 'c' * 40, 'run': 4, 'attempt': 1,
             'jobs': [{'name': 'gate', 'conclusion': 'failure'}]},
        ]
        with open(os.path.join(day_dir, '2026-09-22.jsonl'), 'w') as f:
            f.write('\n'.join(json.dumps(r) for r in rows) + '\n')
        conv = Conventions()
        product = env.Product('sample', {})
        facts = metrics.ci_facts(self.root, conv)
        self.assertEqual([f['sha'][0] for f in facts], ['b', 'd', 'a'])   # newest first; ties reversed per day
        self.assertEqual(facts[0]['jobs'], {'gate': 'success'})          # run 2's twice-listed job, last wins
        self.assertEqual(groom.trunk_ci_runs(self.root, product), tuple(reversed(facts)))


class ReleaseHonesty(Base):
    """I12 (a test, never a tick check — R13): release notes list as landed only items whose
    derived state is Resolved or Closed. v0.1.2's changelog listed a plan-approved Feature and
    two with most of their Tasks open as landed, because a merged PR linking the item counted."""

    def test_i12_release_notes_list_only_resolved_or_closed_as_landed(self):
        from asf import invariants
        # a Task landed and merged, its Feature still open (1 of 2 Tasks); a Bug fixed
        self.items['T-0001']['state'] = 'Closed'
        self.items['B-0001']['state'] = 'Resolved'
        found = {'T-0001': [], 'B-0001': []}
        notes = metrics.render_notes(self.items, found, [])
        self.assertEqual(invariants.check_i12(notes, self.items), [])
        landed = notes[:notes.index('### In progress')]
        self.assertNotIn('F-0001', landed)
        self.assertIn('- F-0001 Free plan', notes[notes.index('### In progress'):])
        self.assertIn('### Bugs fixed\n\n- B-0001 Banner shows', notes)

        note = metrics.render_release(DAY, 'b' * 40, None, None, self.items,
                                      {'F-0001': [(601, None)], 'T-0001': [(623, None)]})
        self.assertEqual(invariants.check_i12(note, self.items), [])
        self.assertIn('## Tasks\n\n- [T-0001]', note)
        self.assertIn('## In progress\n\n- [F-0001]', note)

    def test_i12_the_check_names_an_open_item_listed_as_landed(self):
        from asf import invariants
        dishonest = '### Features landed\n\n- F-0001 Free plan\n\n### In progress\n\n- F-0002 x\n'
        findings = invariants.check_i12(dishonest, self.items)
        self.assertEqual([(f.invariant, f.subject) for f in findings], [('I12', 'F-0001')])
        self.items['F-0001']['state'] = 'Resolved'
        self.assertEqual(invariants.check_i12(dishonest, self.items), [])


if __name__ == '__main__':
    unittest.main()


TOKEN_KEYS = ('tokens_input', 'tokens_output', 'tokens_cache_read', 'tokens_cache_write')


def tok_session(item, **dims):
    ev = {'ts': f'{DAY}T09:00:00Z', 'task': 't', 'account': 'a1', 'kind': 'code', 'result': 'finished', 'item': item}
    ev.update({f'tokens_{d}': n for d, n in dims.items()})
    return ev


class LandingStreamTests(Base):
    """`landings`: one event per harvested run, dated by the trunk commit (F-0100 §2.1)."""

    def landing(self, **kw):
        return dict({'ts': f'{DAY}T05:00:00Z', 'job': 'code-t-0001', 'sha': 'a' * 40,
                     'branch': 'worker/T-0001', 'kind': 'code'}, **kw)

    def make_product_repo(self):
        """A repo with two commits of known committer dates → (repo, [sha1, sha2])."""
        repo = tempfile.mkdtemp(prefix='landings_repo_')
        self.addCleanup(shutil.rmtree, repo, True)
        shas = []
        for n, day in enumerate(('2026-09-20T10:00:00+00:00', '2026-09-21T06:48:03+02:00')):
            env_ = dict(os.environ, GIT_COMMITTER_DATE=day, GIT_AUTHOR_DATE=day,
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@e.x', GIT_COMMITTER_NAME='t',
                        GIT_COMMITTER_EMAIL='t@e.x')
            if n == 0:
                subprocess.run(['git', '-C', repo, 'init', '-q'], check=True)
            subprocess.run(['git', '-C', repo, 'commit', '-q', '--allow-empty', '-m', f'c{n}'],
                           check=True, env=env_)
            shas.append(subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True,
                                       text=True, check=True).stdout.strip())
        return repo, shas

    def registry(self, lines):
        path = os.path.join(tempfile.mkdtemp(prefix='landings_reg_'), 'sessions.jsonl')
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, 'w', encoding='utf-8') as f:
            for rec in lines:
                f.write(json.dumps(rec) + '\n')
        return path

    def test_schema_accepts_a_landing_and_refuses_the_rest(self):
        ev = metrics.validate('landings', self.landing(), self.items)
        self.assertEqual((ev['kind'], ev['branch'], ev['item']), ('code', 'worker/T-0001', 'T-0001'))
        for bad, needle in ((dict(wat=1), 'unknown key(s) wat'), (dict(kind='nope'), "'kind' must be one of")):
            with self.assertRaises(metrics.SchemaError) as cm:
                metrics.validate('landings', self.landing(**bad), self.items)
            self.assertIn(needle, str(cm.exception))
        with self.assertRaises(metrics.SchemaError) as cm:
            metrics.validate('landings', {k: v for k, v in self.landing().items() if k != 'ts'}, self.items)
        self.assertIn("missing required key 'ts'", str(cm.exception))
        with self.assertRaises(metrics.SchemaError):
            metrics.validate('landings', self.landing(item='T-9999'), self.items)

    def test_a_five_digit_id_survives_the_item_id_gate(self):
        r"""B-0273/C1: `ID_RE` validates what `match.ID_TOKEN` finds, so it takes 5+ digits too.

        With `^[EFSTBDR]-\d{4}$` the resolved id was refused and the caught SchemaError dropped
        the whole landing, leaving the row absent rather than merely unattributed.
        """
        items = {'T-32850': {'type': 'task'}}
        ev = metrics.validate('landings', self.landing(job='code-t-32850', branch='cloud/T-32850'), items)
        self.assertEqual((ev['item'], ev['item_reason']), ('T-32850', None))
        for good in ('T-32850', 'T-0913', 'E-123456'):
            self.assertTrue(metrics.ID_RE.match(good), good)
        for bad in ('T-123', 'X-0001', 'T-0001x', 't-32850'):
            self.assertFalse(metrics.ID_RE.match(bad), bad)

    def test_kind_falls_back_to_the_job_name(self):
        ev = self.landing()
        del ev['kind']
        ev['job'] = 'spec-f-0100'
        self.assertEqual(metrics.validate('landings', ev, self.items)['kind'], 'spec')

    def test_a_second_import_of_the_same_job_and_sha_exists(self):
        self.assertEqual(self.put('landings', self.landing())[0], 'appended')
        self.assertEqual(self.put('landings', self.landing(branch='other'))[0], 'exists')
        self.assertEqual(self.put('landings', self.landing(job='code-t-0002'))[0], 'appended')

    def test_the_registry_reader(self):
        repo, (old, new) = self.make_product_repo()
        path = self.registry([
            {'job': 'code-t-0001', 'started': '2026-09-20T09:00:00Z', 'pid': 1, 'kind': 'task',
             'branch': 'worker/T-0001', 'item': 'T-0001'},
            {'job': 'code-t-0001', 'harvested': old},
            {'job': 'spec-f-0001', 'started': '2026-09-21T05:00:00Z', 'pid': 2, 'branch': 'spec/F-0001'},
            {'job': 'spec-f-0001', 'harvested': new},
            {'job': 'fix-t-0002', 'started': '2026-09-21T05:00:00Z', 'pid': 3, 'harvested': 'superseded'},
            {'job': 'code-t-0003', 'started': '2026-09-21T05:00:00Z', 'pid': 4, 'harvested': 'f' * 40},
            {'job': 'code-t-0004', 'started': '2026-09-21T05:00:00Z', 'pid': 5},
        ])
        evs, unconfirmed = metrics.landings_from_registry(env.Product('sample', {}), items=self.items,
                                                          state_path=path, repo=repo)
        self.assertEqual(unconfirmed, 1)               # the sha the repo does not have
        self.assertEqual([(e['job'], e['sha'], e['ts'], e['kind']) for e in evs],
                         [('code-t-0001', old, '2026-09-20T10:00:00Z', 'code'),
                          ('spec-f-0001', new, '2026-09-21T04:48:03Z', 'spec')])
        self.assertEqual((evs[0]['branch'], evs[0]['item']), ('worker/T-0001', 'T-0001'))
        for ev in evs:
            metrics.validate('landings', ev, self.items)
        again, _n = metrics.landings_from_registry(env.Product('sample', {}), items=self.items,
                                                   known={('code-t-0001', old)}, state_path=path, repo=repo)
        self.assertEqual([e['job'] for e in again], ['spec-f-0001'])


class GateStreamTests(Base):
    """`gates`: one event per landing gate, read from the ledger (F-0100 §2.2)."""

    def gate(self, **kw):
        return dict({'ts': f'{DAY}T06:41:12Z', 'seconds': 412.7, 'branches': ['spec/F-0100', 'worker/T-0078'],
                     'conclusion': 'failure', 'signature': 'FAILED (failures=1)'}, **kw)

    def test_round_trips_a_green_and_a_red_line(self):
        red = metrics.validate('gates', self.gate(sha='9f3c1ab'), self.items)
        green = metrics.validate('gates', self.gate(conclusion='success', signature=None), self.items)
        self.assertEqual((red['conclusion'], red['sha'], red['items']), ('failure', '9f3c1ab', None))
        self.assertEqual((green['conclusion'], green['signature']), ('success', None))
        self.assertEqual(self.put('gates', self.gate())[0], 'appended')
        self.assertEqual(metrics.read_stream(self.root, 'gates')[0]['branches'], ['spec/F-0100', 'worker/T-0078'])

    def test_refusals(self):
        for bad, needle in ((dict(conclusion='red'), 'success or failure'), (dict(seconds=-1), 'seconds'),
                            (dict(branches=['a', 3]), 'branches'), (dict(wat=1), 'unknown key(s) wat')):
            with self.assertRaises(metrics.SchemaError) as cm:
                metrics.validate('gates', self.gate(**bad), self.items)
            self.assertIn(needle, str(cm.exception))

    def test_natural_key_is_the_start_and_the_branches(self):
        self.assertEqual(self.put('gates', self.gate())[0], 'appended')
        self.assertEqual(self.put('gates', self.gate(seconds=1))[0], 'exists')
        self.assertEqual(self.put('gates', self.gate(branches=['worker/T-0078']))[0], 'appended')

    def test_ledger_reader_truncates_and_skips_known(self):
        home = tempfile.mkdtemp(prefix='gates_home_')
        self.addCleanup(shutil.rmtree, home, True)
        product = env.Product('sample', {})
        with mock.patch.object(env, 'ASF_HOME', home):
            self.assertEqual(metrics.gates_from_ledger(product), [])       # no ledger, no error
            with open(os.path.join(env.state_dir(product), 'gates.jsonl'), 'w', encoding='utf-8') as f:
                f.write(json.dumps({'at': f'{DAY}T06:41:12Z', 'seconds': 412.7, 'branches': ['a', 'b'],
                                    'sha': '9f3c', 'ok': False, 'line': 'x' * 300}) + '\n')
                f.write('not json\n')
                f.write(json.dumps({'at': f'{DAY}T07:00:00Z', 'seconds': 3, 'branches': ['c'],
                                    'sha': '1a2b', 'ok': True, 'line': None}) + '\n')
            evs = metrics.gates_from_ledger(product)
            self.assertEqual([e['conclusion'] for e in evs], ['failure', 'success'])
            self.assertEqual(len(evs[0]['signature']), 120)
            for ev in evs:
                metrics.validate('gates', ev, self.items)
            known = {metrics.natural_key('gates', evs[0])}
            self.assertEqual([e['branches'] for e in metrics.gates_from_ledger(product, known=known)], [['c']])


class SessionTokensTest(Base):
    def registry_events(self, result_record, extra_lines=()):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        home = os.path.join(d, 'asf-home2')
        _state, logs = Backfill.write_registry(self, home)
        with open(os.path.join(logs, 'fix-free-plan-t3-r1.jsonl'), 'w') as f:
            for rec in (*extra_lines, result_record):
                f.write(json.dumps(rec) + '\n')
        with mock.patch.object(env, 'ASF_HOME', home):
            return metrics.sessions_from_registry(env.Product('sample', {}), since_day='2026-09-20', items=self.items)

    def test_every_session_line_carries_four_dimensions(self):
        [ev] = self.registry_events({'type': 'result', 'result': 'fixed', 'total_cost_usd': 0.42,
                                     'usage': {'input_tokens': 10, 'output_tokens': 20,
                                               'cache_read_input_tokens': 300, 'cache_creation_input_tokens': 40}})
        self.assertEqual([ev[k] for k in TOKEN_KEYS], [10, 20, 300, 40])
        metrics.validate('sessions', ev, self.items)

    def test_a_session_with_no_usage_carries_four_nulls(self):
        [ev] = self.registry_events({'type': 'result', 'result': 'fixed', 'total_cost_usd': 0.42})
        self.assertEqual([ev[k] for k in TOKEN_KEYS], [None] * 4)
        self.assertEqual(ev['usd'], 0.42)

    def test_a_capped_session_has_tokens_and_no_money(self):
        from asf import tokens
        capped = tokens.cap_result('code', 'output', 1100, 1000,
                                   {'input': 5, 'output': 1100, 'cache_read': None, 'cache_write': 7}, None)
        [ev] = self.registry_events(capped)
        self.assertIsNone(ev['usd'])
        self.assertEqual([ev[k] for k in TOKEN_KEYS], [5, 1100, None, 7])
        self.assertTrue(ev['reason'].startswith('token cap:'), ev['reason'])

    def test_the_event_has_no_total(self):
        [ev] = self.registry_events({'type': 'result', 'usage': {'input_tokens': 1}})
        self.assertFalse([k for k in ev if re.search(r'^tokens$|total_tokens|tokens_total', k)])
        with self.assertRaises(metrics.SchemaError):
            metrics.validate('sessions', dict(ev, tokens=1), self.items)

    def test_an_old_line_still_reads(self):
        old = {'ts': f'{DAY}T09:00:00Z', 'task': 't', 'account': 'a1', 'kind': 'code', 'result': 'finished',
               'item': 'T-0001', 'usd': 1.0}
        path = os.path.join(self.root, 'metrics', 'sessions', f'{DAY}.jsonl')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(json.dumps(old) + '\n')
        evs = metrics.read_stream(self.root, 'sessions', [DAY])
        self.assertEqual(len(evs), 1)
        c = metrics.compute_costs([], evs)['T-0001']
        self.assertEqual(c['tokens'], {'input': None, 'output': None, 'cache_read': None, 'cache_write': None})


class ScorecardTokensTest(Base):
    def costs_table(self, sessions):
        return metrics.cost_table(self.items, [], sessions)

    def test_cost_table_shows_four_columns_beside_usd(self):
        lines = self.costs_table([
            dict(tok_session('T-0001', input=1500, output=20, cache_read=2_500_000, cache_write=7), usd=1.0),
            dict(tok_session('T-0002', input=500, output=5, cache_read=500_000, cache_write=3), usd=2.0)])
        self.assertTrue(lines[0].endswith('| USD | In | Out | Cache rd | Cache wr |'))
        row = next(l for l in lines if l.startswith('| F-0001'))
        self.assertTrue(row.endswith('| 3.00 | 2.0 k | 25 | 3.0 M | 10 |'), row)

    def test_a_dimension_no_session_carried_is_a_dash(self):
        lines = self.costs_table([tok_session('T-0001', input=42)])
        row = next(l for l in lines if l.startswith('| F-0001'))
        self.assertTrue(row.endswith('| 42 | — | — | — |'), row)

    def test_the_all_row_never_adds_across_dimensions(self):
        lines = self.costs_table([tok_session('T-0001', input=1000, output=2000, cache_read=3000, cache_write=4000),
                                  tok_session(None, input=1000, output=2000, cache_read=3000, cache_write=4000)])
        row = next(l for l in lines if l.startswith('| **All**'))
        self.assertTrue(row.endswith('| 2.0 k | 4.0 k | 6.0 k | 8.0 k |'), row)
        self.assertNotIn('20.0 k', row)

    def test_the_waste_table_itemises_the_day(self):
        sessions = [tok_session('T-0001', input=1000, output=2000, cache_read=3000, cache_write=4000),
                    dict(tok_session('T-0002', input=1), result='failed: token cap')]
        rows = {r[0]: r for r in metrics.scorecard_rows([], sessions, [])}
        self.assertEqual(rows['tokens'][1], 'in 1.0 k · out 2.0 k · cache rd 3.0 k · cache wr 4.0 k')
        self.assertEqual(rows['tokens'][2], '2 sessions, 1 capped')

    def test_the_rendered_scorecard_has_no_total(self):
        fixture_streams(self)
        md = metrics.render_daily(self.root, DAY, self.items)
        self.assertIsNone(re.search(r'total tokens|tokens total', md, re.I))


class CancelRowTests(Base):
    def ci_row(self, jobs, minutes=40, conclusion='cancelled', attempt=1):
        return {'conclusion': conclusion, 'attempt': attempt, 'minutes': minutes, 'jobs': jobs}

    def rows(self, ci):
        return {r[0]: r for r in metrics.scorecard_rows(ci, [], [])}

    def test_one_row_per_cause_in_causes_order(self):
        ci = [self.ci_row([job('c', 'cancelled', 3, cause='failure'),
                           job('b', 'cancelled', 5, cause='runner-loss'),
                           job('a', 'cancelled', 10, cause='timeout')], minutes=18)]
        order = [k for k in self.rows(ci) if k.startswith('cancelled: ')]
        self.assertEqual(order, ['cancelled: timeout', 'cancelled: runner-loss', 'cancelled: failure'])

    def test_minutes_job_count_and_share_of_total(self):
        ci = [self.ci_row([job('a', 'cancelled', 10, cause='timeout'),
                           job('b', 'cancelled', 10, cause='timeout')], minutes=40)]
        rows = self.rows(ci)
        self.assertEqual(rows['cancelled: timeout'], ('cancelled: timeout', '20 min in 2 jobs (50 %)', 'a ×1, b ×1'))

    def test_up_to_three_job_names_most_common(self):
        jobs = ([job('a', 'cancelled', 1, cause='failure')] * 4 + [job('b', 'cancelled', 1, cause='failure')] * 3
                + [job('c', 'cancelled', 1, cause='failure')] * 2 + [job('d', 'cancelled', 1, cause='failure')])
        ci = [self.ci_row(jobs, minutes=100)]
        _, detail, names = self.rows(ci)['cancelled: failure']
        self.assertEqual(detail, '10 min in 10 jobs (10 %)')
        self.assertEqual(names, 'a ×4, b ×3, c ×2')

    def test_no_row_for_a_cause_with_nothing_in_it(self):
        ci = [self.ci_row([job('a', 'cancelled', 10, cause='timeout')], minutes=10)]
        rows = self.rows(ci)
        self.assertIn('cancelled: timeout', rows)
        self.assertNotIn('cancelled: runner-loss', rows)
        self.assertNotIn('cancelled: failure', rows)

    def test_unclassified_cancel_adds_no_row(self):
        ci = [self.ci_row([job('a', 'cancelled', 10)], minutes=10)]
        rows = self.rows(ci)
        self.assertNotIn('cancelled: timeout', rows)
        self.assertNotIn('cancelled: runner-loss', rows)
        self.assertNotIn('cancelled: failure', rows)

    def test_a_stream_imported_before_this_change_reads_as_it_reads_today(self):
        fixture_streams(self)
        rows = {r[0]: r for r in metrics.scorecard_rows(
            metrics.read_stream(self.root, 'ci', [DAY]), [], [])}
        self.assertEqual(rows['cancelled: failure'], ('cancelled: failure', '12 min in 1 job (30 %)', 'e2e ×1'))


class CostBlockTokensTest(Base):
    NOW = dt.datetime(2026, 9, 21, 12, 0, 0, tzinfo=dt.timezone.utc)

    def card(self, iid):
        path = os.path.join(self.root, 'tasks', f'{iid}.md')
        meta, _body = frontmatter.parse(self.read(f'tasks/{iid}.md'), path=path)
        return meta

    def test_cost_block_carries_the_four_keys(self):
        sessions = [tok_session('T-0001', input=1, output=2, cache_read=3, cache_write=4)]
        metrics.write_costs(self.root, self.items, [], sessions, now=self.NOW)
        cost = self.card('T-0001')['cost']
        self.assertEqual((cost['tokens_input'], cost['tokens_output'], cost['tokens_cache_read'],
                          cost['tokens_cache_write']), (1, 2, 3, 4))
        self.assertIn('usd', cost)
        self.assertFalse([v for v in cost.values() if isinstance(v, dict)])

    def test_a_null_dimension_is_omitted_not_zero(self):
        metrics.write_costs(self.root, self.items, [], [tok_session('T-0001', output=9)], now=self.NOW)
        cost = self.card('T-0001')['cost']
        self.assertEqual(cost['tokens_output'], 9)
        for k in ('tokens_input', 'tokens_cache_read', 'tokens_cache_write'):
            self.assertNotIn(k, cost)

    def test_an_epic_subtree_adds_per_dimension(self):
        costs = metrics.compute_costs([], [tok_session('T-0001', input=1, output=10),
                                           tok_session('B-0001', input=2, cache_read=5)])
        e = metrics.subtree_cost(self.items, costs, 'E-0001')
        self.assertEqual(e['tokens'], {'input': 3, 'output': 10, 'cache_read': 5, 'cache_write': None})
        self.assertIsNone(e['usd'])


def _landed(state='Closed', hour=12, **kw):
    return dict({'state': state, 'stage_since': f'{DAY}T{hour:02d}:00:00Z'}, **kw)


def _sess(item, hour=8, usd=None, result='finished'):
    ev = {'ts': f'{DAY}T{hour:02d}:00:00Z', 'task': 't', 'account': 'a1', 'kind': 'code', 'result': result, 'item': item}
    if usd is not None:
        ev['usd'] = usd
    return ev


class DeliveredVsSingleTest(Base):
    def delivery(self):
        items = {'F-0100': _landed(delivers=['F-0100', 'B-0100', 'B-0101']),
                 'B-0100': _landed(delivered_by='F-0100'), 'B-0101': _landed(delivered_by='F-0100'),
                 'T-0100': _landed(), 'T-0101': _landed()}
        sessions = [_sess('F-0100', usd=1.5), _sess('F-0100', usd=1.5)]
        sessions += [_sess(i) for i in ('T-0100', 'T-0101') for _ in range(3)]
        return items, sessions

    def test_table_divides_a_delivery_over_its_items(self):
        rows = metrics.delivered_vs_single(*self.delivery(), DAY)
        d, s = rows['delivered'], rows['single']
        self.assertEqual((d['landed'], d['sessions'], round(d['sessions_per_item'], 2), d['usd'], d['usd_per_item']),
                         (3, 2, 0.67, 3.0, 1.0))
        self.assertEqual((s['landed'], s['sessions'], s['sessions_per_item'], s['usd']), (2, 6, 3.0, None))
        md = '\n'.join(metrics.delivered_table(rows))
        self.assertIn('| delivered | 3 | 2 | 0.67 | 3.00 | 1.00 |', md)
        self.assertIn('| single | 2 | 6 | 3.00 | — |', md)

    def test_hours_to_land_uses_stage_since(self):
        items = {'F-0100': _landed(hour=12, delivers=['F-0100', 'B-0100']),
                 'B-0100': _landed(hour=12, delivered_by='F-0100')}
        rows = metrics.delivered_vs_single(items, [_sess('F-0100', hour=8)], DAY)
        self.assertEqual(rows['delivered']['median_hours'], 4.0)
        self.assertIn('| 4.0 |', '\n'.join(metrics.delivered_table(rows)))

    def test_failed_share(self):
        items = {'T-0100': _landed()}
        sessions = [_sess('T-0100', result='failed: gate red')] + [_sess('T-0100') for _ in range(3)]
        rows = metrics.delivered_vs_single(items, sessions, DAY)
        self.assertIn('25 %', '\n'.join(metrics.delivered_table(rows)))

    def test_section_absent_when_nothing_landed(self):
        self.assertNotIn('## Delivered vs single', metrics.render_daily(self.root, DAY, self.items))
        rows = metrics.delivered_vs_single({'T-0100': _landed()}, [], DAY)
        self.assertEqual(rows['single']['landed'], 1)
        self.assertIsNone(rows['single']['median_hours'])
        self.assertIsNone(rows['delivered']['median_hours'])


def _shipped_fixture():
    """F-0001 shipped twice in this window (newer event wins); F-0002 only in the previous window;
    F-0003 falls back to `stage_since`; F-0004's event wins over its own `stage_since` and lands in
    the previous window only, never both; T-0001 and F-9999 are excluded (not a Feature / not in
    `items`); F-0005 (`landed`) and F-0006 (`on-prod`, no `stage_since`) are excluded outright."""
    items = {
        'F-0001': {'type': 'feature', 'stage': 'building 1/1'},
        'F-0002': {'type': 'feature', 'stage': 'building 1/1'},
        'F-0003': {'type': 'feature', 'stage': 'on-prod', 'stage_since': '2026-09-16T00:00:00Z'},
        'F-0004': {'type': 'feature', 'stage': 'on-prod', 'stage_since': '2026-09-16T00:00:00Z'},
        'F-0005': {'type': 'feature', 'stage': 'landed', 'stage_since': '2026-09-17T00:00:00Z'},
        'F-0006': {'type': 'feature', 'stage': 'on-prod'},
        'T-0001': {'type': 'task', 'stage': 'on-prod'},
    }
    events = [
        {'kind': metrics.ON_PROD_EVENT, 'item': 'F-0001', 'ts': '2026-09-17T08:00:00Z'},
        {'kind': metrics.ON_PROD_EVENT, 'item': 'F-0001', 'ts': '2026-09-19T08:00:00Z'},
        {'kind': metrics.ON_PROD_EVENT, 'item': 'F-0002', 'ts': '2026-09-10T08:00:00Z'},
        {'kind': metrics.ON_PROD_EVENT, 'item': 'F-0004', 'ts': '2026-09-09T08:00:00Z'},
        {'kind': metrics.ON_PROD_EVENT, 'item': 'T-0001', 'ts': '2026-09-17T08:00:00Z'},
        {'kind': metrics.ON_PROD_EVENT, 'item': 'F-9999', 'ts': '2026-09-17T08:00:00Z'},
    ]
    return items, events


class ShippedTests(Base):
    def windows(self, items, events):
        this_wk, prev_wk = metrics.days_back(DAY, 7), metrics.days_back(DAY, 14)[:7]
        return metrics.shipped(events, items, this_wk), metrics.shipped(events, items, prev_wk)

    def test_the_event_wins_over_the_window_it_falls_in(self):
        items, events = _shipped_fixture()
        week, prev = self.windows(items, events)
        self.assertEqual(week['F-0001'], '2026-09-19')
        self.assertNotIn('F-0001', prev)

    def test_an_event_only_in_the_previous_window(self):
        items, events = _shipped_fixture()
        week, prev = self.windows(items, events)
        self.assertNotIn('F-0002', week)
        self.assertEqual(prev['F-0002'], '2026-09-10')

    def test_stage_since_is_the_fallback_for_no_event(self):
        items, events = _shipped_fixture()
        week, prev = self.windows(items, events)
        self.assertEqual(week['F-0003'], '2026-09-16')
        self.assertNotIn('F-0003', prev)

    def test_the_event_wins_even_older_and_outside_the_window_asked_for(self):
        items, events = _shipped_fixture()
        week, prev = self.windows(items, events)
        self.assertNotIn('F-0004', week)
        self.assertEqual(prev['F-0004'], '2026-09-09')

    def test_only_a_feature_counts(self):
        items, events = _shipped_fixture()
        week, prev = self.windows(items, events)
        self.assertNotIn('T-0001', week)
        self.assertNotIn('T-0001', prev)

    def test_an_id_items_does_not_name_is_excluded(self):
        items, events = _shipped_fixture()
        week, prev = self.windows(items, events)
        self.assertNotIn('F-9999', week)
        self.assertNotIn('F-9999', prev)

    def test_landed_with_a_windowed_stage_since_is_excluded(self):
        items, events = _shipped_fixture()
        week, prev = self.windows(items, events)
        self.assertNotIn('F-0005', week)
        self.assertNotIn('F-0005', prev)

    def test_on_prod_with_no_stage_since_is_excluded(self):
        items, events = _shipped_fixture()
        week, prev = self.windows(items, events)
        self.assertNotIn('F-0006', week)
        self.assertNotIn('F-0006', prev)


def _usd_session(item, usd):
    return {'task': 't', 'account': 'a1', 'kind': 'code', 'result': 'finished', 'item': item, 'usd': usd}


class ThroughputTests(Base):
    def test_three_priced_ids(self):
        items = {i: {'type': 'feature'} for i in ('F-0001', 'F-0002', 'F-0003')}
        costs = metrics.compute_costs([], [_usd_session('F-0001', 10.00), _usd_session('F-0002', 12.00),
                                           _usd_session('F-0003', 15.20)])
        self.assertEqual(metrics.throughput(items, costs, list(items)),
                         {'features': 3, 'priced': 3, 'usd': 37.20, 'mean': 12.40})

    def test_a_fourth_unpriced_id_counts_only_in_features(self):
        items = {i: {'type': 'feature'} for i in ('F-0001', 'F-0002', 'F-0003', 'F-0004')}
        costs = metrics.compute_costs([], [_usd_session('F-0001', 10.00), _usd_session('F-0002', 12.00),
                                           _usd_session('F-0003', 15.20)])
        self.assertEqual(metrics.throughput(items, costs, list(items)),
                         {'features': 4, 'priced': 3, 'usd': 37.20, 'mean': 12.40})

    def test_all_unpriced_gives_no_mean(self):
        items = {i: {'type': 'feature'} for i in ('F-0001', 'F-0002', 'F-0003', 'F-0004')}
        self.assertEqual(metrics.throughput(items, {}, list(items)),
                         {'features': 4, 'priced': 0, 'usd': None, 'mean': None})

    def test_no_ids_divides_by_nothing(self):
        self.assertEqual(metrics.throughput({}, {}, []), {'features': 0, 'priced': 0, 'usd': None, 'mean': None})

    def test_a_features_cost_sums_its_stories_tasks_and_bugs(self):
        items = {'F-0001': {'type': 'feature', 'children': ['S-0001', 'T-0001', 'B-0001']},
                 'S-0001': {'type': 'story', 'children': []},
                 'T-0001': {'type': 'task', 'children': []},
                 'B-0001': {'type': 'bug', 'children': []}}
        costs = metrics.compute_costs([], [_usd_session('S-0001', 2.0), _usd_session('T-0001', 3.0),
                                           _usd_session('B-0001', 1.0)])
        self.assertEqual(metrics.throughput(items, costs, ['F-0001']),
                         {'features': 1, 'priced': 1, 'usd': 6.0, 'mean': 6.0})

    def test_whole_life_cost_ignores_the_window(self):
        old_day = (dt.date.fromisoformat(DAY) - dt.timedelta(days=20)).isoformat()
        metrics.append_event(self.root, 'sessions', dict(_usd_session('F-0001', 5.0), ts=f'{DAY}T08:00:00Z'))
        metrics.append_event(self.root, 'sessions', dict(_usd_session('F-0001', 9.0), ts=f'{old_day}T08:00:00Z'))
        items = {'F-0001': {'type': 'feature'}}
        week = metrics.throughput(items, metrics.compute_costs(
            [], metrics.read_stream(self.root, 'sessions', metrics.days_back(DAY, 7))), ['F-0001'])
        month = metrics.throughput(items, metrics.compute_costs(
            [], metrics.read_stream(self.root, 'sessions', metrics.days_back(DAY, 30))), ['F-0001'])
        self.assertEqual(week['features'], month['features'])
        self.assertLess(week['usd'], month['usd'])
